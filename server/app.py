"""FastAPI Web Server and WebSocket Telemetry Engine.

Provides RESTful management endpoints and a high-frequency WebSocket stream
for real-time progress, speedometers, and queue interactions.
"""

import asyncio
from contextlib import asynccontextmanager
import os
import time
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.downloader import AsyncDownloader
from engine.network import NetworkManager
from engine.session import SessionManager
from engine.task import DownloadTask, TaskStatus
from extractor.unpacker import ArchiveExtractor
from resolvers.debrider import RealDebridClient
from resolvers.manager import LinkManager

# Initialize core services
session_mgr = SessionManager()
saved_state = session_mgr.load_session()
settings = saved_state.get("settings", {})
settings.setdefault("auto_tune", True)
settings.setdefault("auto_resume_on_start", True)
settings.setdefault("auto_retry_failed", True)

rd_client = RealDebridClient(api_token=settings.get("rd_token", ""))
link_mgr = LinkManager(rd_client=rd_client)
net_mgr = NetworkManager()
archive_extractor = ArchiveExtractor()

downloader = AsyncDownloader(
    link_manager=link_mgr,
    net_manager=net_mgr,
    session_manager=session_mgr,
    max_concurrency=settings.get("max_concurrency", 2),
)
downloader.segments_per_file = settings.get("segments_per_file", 2)
downloader.buffer_size_kb = settings.get("buffer_size_kb", 256)
downloader.set_speed_limit(settings.get("speed_limit_mbps", 0.0))
downloader.max_retries = settings.get("max_retries", 5)
downloader.min_segment_file_size_mb = settings.get("min_segment_file_size_mb", 10)
downloader.bonding_strategy = settings.get("bonding_strategy", "ewma")
downloader.delete_archive_after_extract = settings.get("delete_archive_after_extract", False)
downloader.work_stealing_enabled = settings.get("work_stealing_enabled", True)
downloader.set_auto_tune(settings.get("auto_tune", True))
downloader.auto_retry_failed = settings.get("auto_retry_failed", True)

# Restore previous session tasks and reconcile with crash-safe .part.meta journals
auto_resume_boot = settings.get("auto_resume_on_start", True)
for t_data in saved_state.get("tasks", []):
    task = DownloadTask(**t_data)
    if task.status in (TaskStatus.DOWNLOADING, TaskStatus.RESOLVING):
        task.status = TaskStatus.QUEUED if auto_resume_boot else TaskStatus.PAUSED
    task.speed_bytes_sec = 0.0
    task.eta_seconds = None
    task.segments = []

    if task.status != TaskStatus.COMPLETED and task.destination_dir and task.filename:
        part_path = os.path.join(task.destination_dir, f"{task.filename}.part")
        final_path = os.path.join(task.destination_dir, task.filename)
        if os.path.exists(final_path) and not os.path.exists(part_path):
            sz = os.path.getsize(final_path)
            if sz > 0:
                task.file_size = sz
                task.downloaded_bytes = sz
                task.status = TaskStatus.COMPLETED
        elif os.path.exists(f"{part_path}.meta") or os.path.exists(part_path):
            j = downloader._get_journal(task, part_path)
            if j.file_size > 0:
                task.file_size = j.file_size
                task.downloaded_bytes = j.total_downloaded()

    downloader.tasks[task.id] = task


async def _on_task_complete(completed_task: DownloadTask) -> None:
    if settings.get("auto_extract", True):
        all_tasks = list(downloader.tasks.values())
        groups = archive_extractor.group_tasks_by_sequence(all_tasks)
        for group_key, gdata in groups.items():
            if gdata["is_complete"] and gdata["first_part"] and gdata["extension"] != "bin":
                clean_prefix = gdata.get("prefix", group_key).strip("._-")
                first_file = os.path.join(gdata["destination_dir"], gdata["first_part"])
                out_dir = os.path.join(gdata["destination_dir"], clean_prefix)
                job = archive_extractor.extraction_jobs.get(group_key)
                if not job:
                    part_paths = [
                        os.path.join(t.destination_dir, t.filename)
                        for t in gdata["parts"].values()
                    ]
                    asyncio.create_task(
                        archive_extractor.extract_archive(
                            first_file,
                            out_dir,
                            group_key,
                            part_files=part_paths,
                            delete_after_extract=downloader.delete_archive_after_extract,
                        )
                    )


async def _periodic_checkpoint_loop() -> None:
    last_fingerprint = None
    while True:
        await asyncio.sleep(5.0)
        tasks_snapshot = list(downloader.tasks.values())
        fp = tuple((t.id, t.status, t.downloaded_bytes) for t in tasks_snapshot)
        if fp != last_fingerprint and any(t.status == TaskStatus.DOWNLOADING for t in tasks_snapshot):
            last_fingerprint = fp
            await asyncio.to_thread(session_mgr.save_session, settings, tasks_snapshot)


@asynccontextmanager
async def lifespan(app: FastAPI):
    downloader.on_complete_callback = _on_task_complete
    checkpoint_task = asyncio.create_task(_periodic_checkpoint_loop())
    await downloader.start()
    try:
        yield
    finally:
        checkpoint_task.cancel()
        await downloader.stop()
        await link_mgr.close()
        session_mgr.save_session(settings, list(downloader.tasks.values()))


app = FastAPI(title="PartHarvester API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AddLinksRequest(BaseModel):
    input_text: str
    download_dir: Optional[str] = None
    auto_extract: Optional[bool] = None


class SettingsRequest(BaseModel):
    download_dir: Optional[str] = None
    auto_tune: Optional[bool] = None
    max_concurrency: Optional[int] = None
    auto_extract: Optional[bool] = None
    rd_token: Optional[str] = None
    segments_per_file: Optional[int] = None
    buffer_size_kb: Optional[int] = None
    speed_limit_mbps: Optional[float] = None
    max_retries: Optional[int] = None
    bonding_strategy: Optional[str] = None
    work_stealing_enabled: Optional[bool] = None
    delete_archive_after_extract: Optional[bool] = None
    min_segment_file_size_mb: Optional[int] = None
    auto_resume_on_start: Optional[bool] = None
    auto_retry_failed: Optional[bool] = None


class SortQueueRequest(BaseModel):
    criterion: str = "natural_part"


class ReorderTaskRequest(BaseModel):
    direction: str = "top"  # top, up, down


class AdapterToggleRequest(BaseModel):
    ip_address: str
    enabled: bool


class HandoffRequest(BaseModel):
    target_ip: str


def _get_autotune_profile_label() -> str:
    if not downloader.auto_tune_enabled:
        return f"Manual: {downloader.max_concurrency} files × {downloader.segments_per_file} segs"
    rec = downloader.auto_tuner.last_recommendation or downloader.apply_auto_tune()
    return rec.profile_label


def _build_enriched_adapters():
    adapters = net_mgr.get_available_adapters()
    stream_budget = max(1, downloader.max_concurrency * downloader.segments_per_file)
    quotas = net_mgr.calculate_adaptive_quotas(stream_budget, adapters=adapters)
    for a in adapters:
        a.current_speed_bytes = net_mgr.get_adapter_speed(a.ip_address)
        if a.is_enabled and a.is_up:
            a.allocated_slots = quotas.get(a.ip_address, a.max_concurrency_cap)
        else:
            a.allocated_slots = 0
    return adapters


@app.get("/api/status")
async def get_status():
    """Returns overall application telemetry and task lists."""
    tasks = list(downloader.tasks.values())
    total_speed = sum(t.speed_bytes_sec for t in tasks if t.status == TaskStatus.DOWNLOADING)
    total_downloaded = sum(t.downloaded_bytes for t in tasks)
    total_size = sum(t.file_size for t in tasks if t.file_size > 0)

    adapters = _build_enriched_adapters()
    groups = archive_extractor.group_tasks_by_sequence(tasks)

    return {
        "tasks": [t.model_dump() for t in tasks],
        "adapters": [a.model_dump() for a in adapters],
        "handoff_events": downloader.handoff_events,
        "groups": groups,
        "extraction_jobs": archive_extractor.extraction_jobs,
        "metrics": {
            "total_speed_bytes_sec": total_speed,
            "total_downloaded_bytes": total_downloaded,
            "total_size_bytes": total_size,
            "active_downloads": len([t for t in tasks if t.status == TaskStatus.DOWNLOADING]),
            "queued_downloads": len([t for t in tasks if t.status == TaskStatus.QUEUED]),
            "completed_downloads": len([t for t in tasks if t.status == TaskStatus.COMPLETED]),
            "failed_downloads": len([t for t in tasks if t.status == TaskStatus.FAILED]),
            "auto_tune_enabled": downloader.auto_tune_enabled,
            "auto_tune_profile": _get_autotune_profile_label(),
        },
        "settings": settings,
    }


@app.post("/api/add")
async def add_links(req: AddLinksRequest):
    """Parses input links and queues download tasks."""
    target_dir = req.download_dir or settings.get(
        "download_dir", os.path.join(os.path.expanduser("~"), "Downloads", "PartHarvester")
    )
    if req.auto_extract is not None:
        settings["auto_extract"] = req.auto_extract

    links = await link_mgr.ingest_input(req.input_text)
    created_tasks = downloader.add_tasks_batch(links, target_dir)

    await asyncio.to_thread(session_mgr.save_session, settings, list(downloader.tasks.values()))
    return {"status": "ok", "count": len(created_tasks), "tasks": [t.id for t in created_tasks]}


@app.post("/api/pause/{task_id}")
async def pause_task(task_id: str):
    success = downloader.pause_task(task_id)
    return {"status": "ok" if success else "error"}


@app.post("/api/resume/{task_id}")
async def resume_task(task_id: str):
    success = downloader.resume_task(task_id)
    return {"status": "ok" if success else "error"}


@app.post("/api/pause-all")
async def pause_all():
    downloader.pause_all()
    return {"status": "ok"}


@app.post("/api/resume-all")
async def resume_all():
    downloader.resume_all()
    return {"status": "ok"}


@app.post("/api/retry-failed")
async def retry_failed():
    downloader.retry_failed()
    return {"status": "ok"}


@app.delete("/api/task/{task_id}")
async def remove_task(task_id: str):
    success = downloader.remove_task(task_id)
    await asyncio.to_thread(session_mgr.save_session, settings, list(downloader.tasks.values()))
    return {"status": "ok" if success else "error"}


@app.post("/api/task/{task_id}/handoff")
async def handoff_task(task_id: str, req: HandoffRequest):
    """Manually hands off an active download to a specific network adapter."""
    success = await downloader.handoff_task_to_adapter(task_id, req.target_ip)
    return {"status": "ok" if success else "error"}


@app.post("/api/queue/sort")
async def sort_queue(req: SortQueueRequest):
    """Sorts tasks by natural part number, status, progress, or size."""
    downloader.sort_tasks(req.criterion)
    await asyncio.to_thread(session_mgr.save_session, settings, list(downloader.tasks.values()))
    return {"status": "ok", "criterion": req.criterion}


@app.post("/api/queue/reorder/{task_id}")
async def reorder_queue_task(task_id: str, req: ReorderTaskRequest):
    """Moves a task to top, up, or down in priority."""
    success = downloader.reorder_task(task_id, req.direction)
    await asyncio.to_thread(session_mgr.save_session, settings, list(downloader.tasks.values()))
    return {"status": "ok" if success else "error"}


@app.post("/api/queue/clear-completed")
async def clear_completed_tasks():
    """Purges all completed downloads from the queue."""
    count = downloader.clear_completed()
    await asyncio.to_thread(session_mgr.save_session, settings, list(downloader.tasks.values()))
    return {"status": "ok", "removed_count": count}


@app.post("/api/queue/clear-all")
async def clear_all_tasks():
    """Cancels and clears the entire queue."""
    count = downloader.clear_all()
    await asyncio.to_thread(session_mgr.save_session, settings, list(downloader.tasks.values()))
    return {"status": "ok", "removed_count": count}


@app.post("/api/open-folder")
async def open_download_folder(req: Optional[dict] = None):
    """Opens download destination directory in Windows File Explorer."""
    target_dir = (req or {}).get("folder_path") or settings.get(
        "download_dir", os.path.join(os.path.expanduser("~"), "Downloads", "PartHarvester")
    )
    success = downloader.open_download_folder(target_dir)
    return {"status": "ok" if success else "error"}


@app.post("/api/open-file/{task_id}")
async def open_task_file(task_id: str):
    """Reveals the downloaded part file in Windows File Explorer."""
    success = downloader.reveal_task_file(task_id)
    return {"status": "ok" if success else "error"}


@app.get("/api/settings")
async def get_settings():
    return {
        **settings,
        "auto_tune": downloader.auto_tune_enabled,
        "max_concurrency": downloader.max_concurrency,
        "segments_per_file": downloader.segments_per_file,
        "buffer_size_kb": downloader.buffer_size_kb,
    }


@app.post("/api/settings")
async def update_settings(req: SettingsRequest):
    if req.download_dir:
        settings["download_dir"] = req.download_dir
    if req.auto_tune is not None:
        settings["auto_tune"] = bool(req.auto_tune)
        downloader.set_auto_tune(settings["auto_tune"])
    if not downloader.auto_tune_enabled:
        if req.max_concurrency:
            settings["max_concurrency"] = req.max_concurrency
            downloader.set_concurrency(req.max_concurrency)
        if req.segments_per_file is not None:
            settings["segments_per_file"] = max(1, min(16, req.segments_per_file))
            downloader.segments_per_file = settings["segments_per_file"]
        if req.buffer_size_kb is not None:
            settings["buffer_size_kb"] = max(64, min(1024, req.buffer_size_kb))
            downloader.buffer_size_kb = settings["buffer_size_kb"]
    if req.auto_extract is not None:
        settings["auto_extract"] = req.auto_extract
    if req.rd_token is not None:
        settings["rd_token"] = req.rd_token.strip()
        rd_client.api_token = settings["rd_token"]
    if req.speed_limit_mbps is not None:
        settings["speed_limit_mbps"] = max(0.0, req.speed_limit_mbps)
        downloader.set_speed_limit(settings["speed_limit_mbps"])
    if req.max_retries is not None:
        settings["max_retries"] = max(1, min(10, req.max_retries))
        downloader.max_retries = settings["max_retries"]
    if req.bonding_strategy is not None:
        settings["bonding_strategy"] = req.bonding_strategy
        downloader.bonding_strategy = req.bonding_strategy
    if req.work_stealing_enabled is not None:
        settings["work_stealing_enabled"] = req.work_stealing_enabled
        downloader.work_stealing_enabled = req.work_stealing_enabled
    if req.delete_archive_after_extract is not None:
        settings["delete_archive_after_extract"] = req.delete_archive_after_extract
        downloader.delete_archive_after_extract = req.delete_archive_after_extract
    if req.min_segment_file_size_mb is not None:
        settings["min_segment_file_size_mb"] = max(1, min(100, req.min_segment_file_size_mb))
        downloader.min_segment_file_size_mb = settings["min_segment_file_size_mb"]
    if req.auto_resume_on_start is not None:
        settings["auto_resume_on_start"] = bool(req.auto_resume_on_start)
    if req.auto_retry_failed is not None:
        settings["auto_retry_failed"] = bool(req.auto_retry_failed)
        downloader.auto_retry_failed = settings["auto_retry_failed"]

    await asyncio.to_thread(session_mgr.save_session, settings, list(downloader.tasks.values()))
    return {"status": "ok", "settings": settings}


@app.post("/api/adapters/toggle")
async def toggle_adapter(req: AdapterToggleRequest):
    net_mgr.set_adapter_enabled(req.ip_address, req.enabled)
    downloader.apply_auto_tune()
    downloader.trigger_queue_processing()
    return {"status": "ok"}


@app.post("/api/extract/{group_key:path}")
async def manual_extract(group_key: str):
    tasks = list(downloader.tasks.values())
    groups = archive_extractor.group_tasks_by_sequence(tasks)
    gdata = groups.get(group_key)
    if not gdata or not gdata.get("first_part"):
        return {"status": "error", "message": "Group or first part archive not found."}

    clean_prefix = gdata.get("prefix", group_key).strip("._-")
    first_file = os.path.join(gdata["destination_dir"], gdata["first_part"])
    out_dir = os.path.join(gdata["destination_dir"], clean_prefix)
    asyncio.create_task(archive_extractor.extract_archive(first_file, out_dir, group_key))
    return {"status": "ok", "message": f"Extraction started for {clean_prefix}"}


@app.websocket("/ws")
async def websocket_telemetry(websocket: WebSocket):
    """Sub-second live telemetry broadcaster with serialization caching."""
    await websocket.accept()
    task_cache: Dict[str, tuple[Any, dict]] = {}
    last_payload: Optional[dict] = None
    last_sent = 0.0
    try:
        while True:
            tasks = list(downloader.tasks.values())
            active_ids = {t.id for t in tasks}
            for tid in list(task_cache.keys()):
                if tid not in active_ids:
                    del task_cache[tid]

            total_speed = 0.0
            total_downloaded = 0
            total_size = 0
            active_downloads = 0
            queued_downloads = 0
            completed_downloads = 0
            failed_downloads = 0

            serialized_tasks = []
            for t in tasks:
                if t.status == TaskStatus.DOWNLOADING:
                    total_speed += t.speed_bytes_sec
                    active_downloads += 1
                elif t.status == TaskStatus.QUEUED:
                    queued_downloads += 1
                elif t.status == TaskStatus.COMPLETED:
                    completed_downloads += 1
                elif t.status == TaskStatus.FAILED:
                    failed_downloads += 1

                total_downloaded += t.downloaded_bytes
                if t.file_size > 0:
                    total_size += t.file_size

                seg_fp = tuple(
                    (s.status, s.downloaded_bytes, s.assigned_adapter) for s in t.segments
                )
                fp = (
                    t.status,
                    t.downloaded_bytes,
                    t.file_size,
                    t.speed_bytes_sec,
                    t.eta_seconds,
                    t.assigned_adapter,
                    t.error_message,
                    t.priority,
                    seg_fp,
                )
                cached = task_cache.get(t.id)
                if cached and cached[0] == fp:
                    serialized_tasks.append(cached[1])
                else:
                    d = t.model_dump()
                    task_cache[t.id] = (fp, d)
                    serialized_tasks.append(d)

            adapters = _build_enriched_adapters()

            data = {
                "tasks": serialized_tasks,
                "adapters": [a.model_dump() for a in adapters],
                "handoff_events": downloader.handoff_events,
                "metrics": {
                    "total_speed_bytes_sec": total_speed,
                    "total_downloaded_bytes": total_downloaded,
                    "total_size_bytes": total_size,
                    "active_downloads": active_downloads,
                    "queued_downloads": queued_downloads,
                    "completed_downloads": completed_downloads,
                    "failed_downloads": failed_downloads,
                    "auto_tune_enabled": downloader.auto_tune_enabled,
                    "auto_tune_profile": _get_autotune_profile_label(),
                },
                "extraction_jobs": archive_extractor.extraction_jobs,
            }
            # Skip identical frames (idle UI) but keep a heartbeat so clients detect liveness.
            now_mono = time.monotonic()
            if data != last_payload or (now_mono - last_sent) >= 5.0:
                await websocket.send_json(data)
                last_payload = data
                last_sent = now_mono
            # Full-rate telemetry only while something is moving; relax when idle.
            busy = active_downloads > 0 or any(t.status == TaskStatus.RESOLVING for t in tasks)
            await asyncio.sleep(0.3 if busy else 1.0)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# Mount Web frontend
web_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
if os.path.exists(web_dir):
    app.mount("/static", StaticFiles(directory=web_dir), name="static")

    _no_cache = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    @app.get("/style.css")
    async def serve_css():
        return FileResponse(os.path.join(web_dir, "style.css"), media_type="text/css", headers=_no_cache)

    @app.get("/app.js")
    async def serve_js():
        return FileResponse(os.path.join(web_dir, "app.js"), media_type="application/javascript", headers=_no_cache)

    @app.get("/")
    async def serve_index():
        return FileResponse(os.path.join(web_dir, "index.html"), headers=_no_cache)