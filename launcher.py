"""Image Animator launcher — starts the FastAPI server and opens a desktop window."""

from __future__ import annotations

import os
import sys
import threading
import time

import requests
import webview

PORT = int(os.environ.get("IMAGE_ANIMATOR_PORT", "5179"))
URL = f"http://127.0.0.1:{PORT}/"


def start_server() -> None:
    # Import here so the server's startup hooks fire inside this thread
    from server import serve

    serve()


def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(url + "api/version", timeout=1)
            if r.ok:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def main() -> None:
    # Background server thread
    t = threading.Thread(target=start_server, daemon=True)
    t.start()

    if not wait_for_server(URL):
        print("Server failed to start", file=sys.stderr)
        sys.exit(1)

    webview.create_window(
        "Image Animator",
        URL,
        width=1200,
        height=820,
        min_size=(900, 640),
        text_select=True,
    )
    # Use pywebview's default backend selection. On Windows this is the proven
    # path (winforms via pythonnet) that worked in v0.1.0–0.1.4. NOTE: do NOT
    # force gui="edgechromium" — it ALSO routes through pythonnet (so it
    # doesn't dodge the .NET dependency) and it failed to open the window on
    # some machines. The real fix for the original launch failure was
    # unblocking the downloaded files (Zone.Identifier), handled in the .bat.
    webview.start()


if __name__ == "__main__":
    main()
