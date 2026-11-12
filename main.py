"""PartHarvester Application Entry Point.

Starts the FastAPI server with Uvicorn and launches the user's default web
browser to the dashboard once the server socket is actually accepting
connections.
"""

import socket
import threading
import time
import webbrowser

import uvicorn

HOST = "127.0.0.1"
PORT = 5000


def open_browser():
    """Polls the server socket until bound, then launches the dashboard."""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((HOST, PORT)) == 0:
                webbrowser.open(f"http://{HOST}:{PORT}")
                return
        time.sleep(0.1)


if __name__ == "__main__":
    print("=" * 60)
    print("    PartHarvester - Multi-Part Repack Batch Downloader")
    print(f"    Dashboard: http://{HOST}:{PORT}")
    print("=" * 60)

    # Launch browser in a background thread
    threading.Thread(target=open_browser, daemon=True).start()

    # Start FastAPI server
    uvicorn.run(
        "server.app:app",
        host=HOST,
        port=PORT,
        log_level="info",
        access_log=False,
    )
