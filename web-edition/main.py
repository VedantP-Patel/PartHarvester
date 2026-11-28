"""PartHarvester Application Entry Point.

Starts the FastAPI server with Uvicorn and launches the user's default web
browser to the dashboard once the server socket is actually accepting
connections.
"""

import asyncio
import os
import socket
import sys
import threading
import time
import webbrowser

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
try:
    os.chdir(APP_DIR)
except Exception:
    pass

if sys.platform == "win32":
    # Uvicorn >=0.30 directly instantiates asyncio.ProactorEventLoop on win32;
    # alias it to SelectorEventLoop so curl_cffi runs natively with zero bridge threads,
    # without calling deprecated set_event_loop_policy APIs.
    asyncio.ProactorEventLoop = asyncio.SelectorEventLoop  # type: ignore[misc]

import uvicorn

HOST = "127.0.0.1"
DEFAULT_PORT = 5000


def pick_available_port(host: str, preferred: int = DEFAULT_PORT, span: int = 25) -> int:
    """Finds the first bindable local TCP port (avoids Hyper-V excluded/AirPlay occupied ports)."""
    for port in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def open_browser(port: int) -> None:
    """Polls the server socket until bound, then launches the dashboard."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((HOST, port)) == 0:
                webbrowser.open(f"http://{HOST}:{port}")
                return
        time.sleep(0.1)


if __name__ == "__main__":
    active_port = pick_available_port(HOST, DEFAULT_PORT)
    print("=" * 60)
    print("    PartHarvester - Multi-Part Repack Batch Downloader")
    print(f"    Dashboard: http://{HOST}:{active_port}")
    print("=" * 60)

    threading.Thread(target=open_browser, args=(active_port,), daemon=True).start()

    uvicorn.run(
        "server.app:app",
        host=HOST,
        port=active_port,
        log_level="info",
        access_log=False,
    )