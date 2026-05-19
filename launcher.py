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
    # Force the EdgeChromium backend (Windows' built-in WebView2). The default
    # 'winforms' backend goes through pythonnet → .NET Framework, which fails
    # on machines without .NET 4.8 installed (common on fresh Windows installs).
    # WebView2 is pre-installed on Windows 10 1803+ and Windows 11.
    try:
        webview.start(gui="edgechromium")
    except Exception:
        # If EdgeChromium fails (very old Windows), fall back to default.
        webview.start()


if __name__ == "__main__":
    main()
