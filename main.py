"""PartHarvester Application Entry Point.

Starts the FastAPI server with Uvicorn and automatically launches the user's
default web browser to the dashboard.
"""

import sys
import threading
import time
import webbrowser
import uvicorn


def open_browser():
    """Opens browser after slight delay to ensure server is listening."""
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    print("=" * 60)
    print("  🚀 PartHarvester — Multi-Part Repack Batch Downloader")
    print("  🌐 Dashboard: http://127.0.0.1:5000")
    print("=" * 60)

    # Launch browser in a background thread
    threading.Thread(target=open_browser, daemon=True).start()

    # Start FastAPI server
    uvicorn.run(
        "server.app:app",
        host="127.0.0.1",
        port=5000,
        log_level="info",
        access_log=False,
    )

