#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use core_engine::checksum::{verify_sfv_file, ChecksumResult};
use core_engine::downloader::DownloadEngine;
use core_engine::models::{AppSettings, TelemetrySnapshot};
use core_engine::platform;
use core_engine::unpacker::ArchiveExtractor;
use std::path::PathBuf;
use std::sync::Arc;
use tauri::{Emitter, Manager, State};

struct AppState {
    engine: Arc<DownloadEngine>,
}

#[tauri::command]
fn get_status(state: State<'_, AppState>) -> TelemetrySnapshot {
    state.engine.snapshot()
}

#[tauri::command]
async fn add_links(
    state: State<'_, AppState>,
    input_text: String,
    download_dir: Option<String>,
) -> Result<usize, String> {
    let created = state.engine.add_links(&input_text, download_dir).await;
    Ok(created.len())
}

#[tauri::command]
fn pause_task(state: State<'_, AppState>, task_id: String) -> bool {
    state.engine.pause_task(&task_id)
}

#[tauri::command]
fn resume_task(state: State<'_, AppState>, task_id: String) -> bool {
    state.engine.resume_task(&task_id)
}

#[tauri::command]
fn remove_task(state: State<'_, AppState>, task_id: String, delete_file: Option<bool>) -> bool {
    state.engine.remove_task(&task_id, delete_file.unwrap_or(false))
}

#[tauri::command]
fn pause_all(state: State<'_, AppState>) {
    state.engine.pause_all();
}

#[tauri::command]
fn resume_all(state: State<'_, AppState>) {
    state.engine.resume_all();
}

#[tauri::command]
fn retry_failed(state: State<'_, AppState>) {
    state.engine.retry_failed();
}

#[tauri::command]
fn reorder_task(state: State<'_, AppState>, task_id: String, direction: String) -> bool {
    state.engine.reorder_task(&task_id, &direction)
}

#[tauri::command]
fn handoff_task(state: State<'_, AppState>, task_id: String, target_ip: String) -> bool {
    state.engine.handoff_task(&task_id, &target_ip)
}

#[tauri::command]
fn clear_completed(state: State<'_, AppState>) -> usize {
    state.engine.clear_completed()
}

#[tauri::command]
fn clear_all(state: State<'_, AppState>, delete_files: Option<bool>) -> usize {
    state.engine.clear_all(delete_files.unwrap_or(false))
}

#[tauri::command]
fn sort_queue(state: State<'_, AppState>, criterion: String) {
    state.engine.sort_tasks(&criterion);
}

#[tauri::command]
fn toggle_adapter(state: State<'_, AppState>, ip_address: String, enabled: bool) {
    state.engine.net_mgr.set_adapter_enabled(&ip_address, enabled);
    state.engine.apply_auto_tune();
}

#[tauri::command]
fn update_settings(state: State<'_, AppState>, settings: AppSettings) {
    state.engine.update_settings(settings);
}

#[tauri::command]
fn open_folder(state: State<'_, AppState>, folder_path: Option<String>) -> bool {
    let target = folder_path
        .filter(|p| !p.trim().is_empty())
        .unwrap_or_else(|| state.engine.settings.lock().download_dir.clone());
    platform::open_folder(&PathBuf::from(target))
}

#[tauri::command]
fn reveal_task_file(state: State<'_, AppState>, task_id: String) -> bool {
    let tasks = state.engine.tasks.lock();
    let Some(t) = tasks.iter().find(|t| t.id == task_id) else {
        return false;
    };
    let final_p = PathBuf::from(&t.destination_dir).join(&t.filename);
    let part_p = PathBuf::from(&t.destination_dir).join(format!("{}.part", t.filename));
    if final_p.exists() {
        platform::reveal_file(&final_p)
    } else {
        platform::reveal_file(&part_p)
    }
}

#[tauri::command]
async fn manual_extract(state: State<'_, AppState>, group_key: String) -> Result<String, String> {
    let tasks = state.engine.tasks.lock().clone();
    let groups = ArchiveExtractor::group_tasks_by_sequence(&tasks);
    let g = groups
        .get(&group_key)
        .ok_or_else(|| "Group not found".to_string())?;
    let first_part = g
        .first_part
        .as_ref()
        .ok_or_else(|| "First part missing".to_string())?;
    let first_file = PathBuf::from(&g.destination_dir).join(first_part);
    let clean_prefix = g.prefix.trim_matches(|c| c == '.' || c == '_' || c == '-');
    let out_dir = PathBuf::from(&g.destination_dir).join(clean_prefix);
    let part_files: Vec<PathBuf> = tasks
        .iter()
        .filter(|t| g.task_ids.contains(&t.id))
        .map(|t| PathBuf::from(&t.destination_dir).join(&t.filename))
        .collect();
    let delete_after = state.engine.settings.lock().delete_archive_after_extract;

    let jobs_arc = Arc::clone(&state.engine.extraction_jobs);
    let store_arc = Arc::clone(&state.engine.store);
    let gk = group_key.clone();
    tokio::spawn(async move {
        let _ = ArchiveExtractor::extract_archive(
            first_file,
            out_dir,
            gk.clone(),
            part_files,
            delete_after,
            |job| {
                jobs_arc.lock().insert(gk.clone(), job.clone());
                let _ = store_arc.save_extraction_job(&job);
            },
        )
        .await;
    });
    Ok(format!("Extraction started for {clean_prefix}"))
}

#[tauri::command]
async fn verify_sfv(sfv_path: String) -> Result<Vec<ChecksumResult>, String> {
    tokio::task::spawn_blocking(move || verify_sfv_file(&PathBuf::from(sfv_path)))
        .await
        .map_err(|e| e.to_string())?
        .map_err(|e| e.to_string())
}

fn resolve_data_dir() -> PathBuf {
    // Preserve portable mode when an existing writable database lives next to the binary/cwd
    if let Ok(cwd) = std::env::current_dir() {
        let local_db = cwd.join("partharvester.db");
        if local_db.exists() {
            if std::fs::OpenOptions::new().write(true).append(true).open(&local_db).is_ok() {
                return cwd;
            }
        }
    }

    // Standard per-user writable data directory (avoids Permission Denied under Program Files / /usr/bin)
    #[cfg(windows)]
    let base = std::env::var_os("APPDATA")
        .or_else(|| std::env::var_os("LOCALAPPDATA"))
        .map(PathBuf::from);
    #[cfg(not(windows))]
    let base = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/share")));

    if let Some(dir) = base.map(|b| b.join("PartHarvester")) {
        if std::fs::create_dir_all(&dir).is_ok() {
            return dir;
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn main() {
    let data_dir = resolve_data_dir();
    let db_path = data_dir.join("partharvester.db");
    let legacy_json = data_dir.join("download_session.json");

    let engine = tauri::async_runtime::block_on(async {
        DownloadEngine::new(&db_path, Some(&legacy_json))
            .expect("Failed to initialize DownloadEngine")
    });

    let eng_for_loop = Arc::clone(&engine);

    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_notification::init())
        .manage(AppState { engine })
        .invoke_handler(tauri::generate_handler![
            get_status,
            add_links,
            pause_task,
            resume_task,
            remove_task,
            pause_all,
            resume_all,
            retry_failed,
            reorder_task,
            handoff_task,
            clear_completed,
            clear_all,
            sort_queue,
            toggle_adapter,
            update_settings,
            open_folder,
            reveal_task_file,
            manual_extract,
            verify_sfv,
        ])
        .setup(move |app| {
            if let (Some(win), Some(icon)) = (
                app.get_webview_window("main"),
                app.default_window_icon().cloned(),
            ) {
                let _ = win.set_icon(icon);
            }
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                loop {
                    tokio::time::sleep(std::time::Duration::from_millis(250)).await;
                    let snap = eng_for_loop.snapshot();
                    let _ = app_handle.emit("telemetry", &snap);

                    if let Some(win) = app_handle.get_webview_window("main") {
                        let known_bytes: u64 = snap.tasks.iter().map(|t| t.file_size).sum();
                        let known_cnt = snap.tasks.iter().filter(|t| t.file_size > 0).count();
                        let unknown_cnt = snap.tasks.len().saturating_sub(known_cnt);
                        let est_total = if known_cnt > 0 {
                            known_bytes + (unknown_cnt as u64) * (known_bytes / known_cnt as u64)
                        } else {
                            known_bytes
                        };
                        if est_total > 0 && snap.metrics.active_downloads > 0 {
                            let pct = ((snap.metrics.total_downloaded_bytes as f64
                                / est_total as f64)
                                * 100.0)
                                .clamp(0.0, 100.0) as u64;
                            let _ = win.set_progress_bar(tauri::window::ProgressBarState {
                                status: Some(tauri::window::ProgressBarStatus::Normal),
                                progress: Some(pct),
                            });
                        } else {
                            let _ = win.set_progress_bar(tauri::window::ProgressBarState {
                                status: Some(tauri::window::ProgressBarStatus::None),
                                progress: None,
                            });
                        }
                    }
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
