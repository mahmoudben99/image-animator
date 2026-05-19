"""Image Animator — local FastAPI server that drives DepthFlow renders."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


# ---------- Paths and version ----------
HERE = Path(__file__).resolve().parent
PUBLIC_DIR = HERE / "public"
VERSION_PATH = HERE / "version.json"

with open(VERSION_PATH, encoding="utf-8") as fh:
    VERSION_INFO = json.load(fh)

APP_VERSION = VERSION_INFO.get("version", "0.0.0")
APP_BUILD = VERSION_INFO.get("build", 0)
GITHUB_REPO = VERSION_INFO.get("github_repo", "")

# Default output folder on user's Desktop
DEFAULT_OUTPUT_DIR = Path.home() / "Desktop" / "Image Animator Output"
DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Where we stash uploaded images + thumbnails for the running session
WORK_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ImageAnimator"
UPLOADS_DIR = WORK_DIR / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_PATH = WORK_DIR / "settings.json"


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def current_output_dir() -> Path:
    custom = load_settings().get("output_dir")
    if custom:
        p = Path(custom)
        try:
            p.mkdir(parents=True, exist_ok=True)
            return p
        except Exception:  # noqa: BLE001
            pass
    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_OUTPUT_DIR


DURATION_MIN = 1
DURATION_MAX = 30
DURATION_FALLBACK = 7

# Strength multiplier applied to motion intensity. 1.0 = "normal".
STRENGTH_MIN = 0.25
STRENGTH_MAX = 2.5
STRENGTH_DEFAULT = 1.0

# 5s is the duration the per-motion --intensity values in MOTIONS were tuned
# for. effective_intensity is scaled linearly with duration so a 10s clip
# travels twice as far as a 5s clip and feels comparably alive — clamped to
# INTENSITY_CAP so DepthFlow never enters absurd parallax territory.
REFERENCE_DURATION = 5
INTENSITY_FLOOR = 0.05
INTENSITY_CAP = 2.0

# Preset names DepthFlow recognizes that take their own --intensity flag.
_DEPTHFLOW_PRESETS = {"horizontal", "vertical", "zoom", "dolly", "circle", "orbital"}


def current_default_duration() -> int:
    raw = load_settings().get("default_duration", DURATION_FALLBACK)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DURATION_FALLBACK
    return max(DURATION_MIN, min(DURATION_MAX, value))


def clamp_strength(raw) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return STRENGTH_DEFAULT
    return max(STRENGTH_MIN, min(STRENGTH_MAX, value))


# ---------- Motion presets ----------
MOTIONS = [
    {
        "id": "pan_right",
        "label": "Pan right",
        "args": ["horizontal", "--intensity", "0.5", "--linear", "--no-loop"],
    },
    {
        "id": "pan_left",
        "label": "Pan left",
        "args": ["horizontal", "--intensity", "0.5", "--linear", "--no-loop", "--reverse"],
    },
    {
        "id": "pan_down",
        "label": "Pan down",
        "args": ["vertical", "--intensity", "0.5", "--linear", "--no-loop"],
    },
    {
        "id": "pan_up",
        "label": "Pan up",
        "args": ["vertical", "--intensity", "0.5", "--linear", "--no-loop", "--reverse"],
    },
    {
        "id": "zoom_in",
        "label": "Zoom in",
        "args": ["zoom", "--intensity", "0.5", "--linear", "--no-loop"],
    },
    {
        "id": "zoom_out",
        "label": "Zoom out",
        "args": ["zoom", "--intensity", "0.5", "--linear", "--no-loop", "--reverse"],
    },
    {
        "id": "dolly_in",
        "label": "Dolly in (3D push)",
        "args": ["dolly", "--intensity", "0.5", "--linear", "--no-loop"],
    },
    {
        "id": "dolly_out",
        "label": "Dolly out (3D pull)",
        "args": ["dolly", "--intensity", "0.5", "--linear", "--no-loop", "--reverse"],
    },
    {
        "id": "subtle_drift",
        "label": "Subtle drift",
        "args": ["dolly", "--intensity", "0.2", "--linear", "--no-loop"],
    },
    {
        "id": "diagonal_pan_zoom",
        "label": "Diagonal pan + zoom",
        "args": [
            "horizontal", "--intensity", "0.3", "--linear", "--no-loop",
            "zoom", "--intensity", "0.4", "--linear", "--no-loop",
        ],
    },
]

# Motions excluded from the random pool — still selectable manually from the
# dropdown, but never auto-assigned. subtle_drift is too subtle to read as
# motion on first glance; diagonal_pan_zoom chains two presets and tends to
# wobble.
_NON_RANDOM_MOTIONS = {"subtle_drift", "diagonal_pan_zoom"}
DEFAULT_MOTION_IDS = [m["id"] for m in MOTIONS if m["id"] not in _NON_RANDOM_MOTIONS]


def motion_by_id(motion_id: str) -> dict:
    for m in MOTIONS:
        if m["id"] == motion_id:
            return m
    raise HTTPException(status_code=400, detail=f"Unknown motion: {motion_id}")


def build_motion_args(motion: dict, duration: int, strength: float) -> list[str]:
    """Return depthflow CLI args with each --intensity scaled for the requested
    duration and strength. Walks the args list and rewrites any value that
    follows --intensity, so compound presets (e.g. diagonal_pan_zoom) get every
    intensity rescaled."""
    args = list(motion["args"])
    duration_scale = max(0.2, duration / REFERENCE_DURATION)
    multiplier = duration_scale * strength
    for i in range(len(args) - 1):
        if args[i] == "--intensity":
            try:
                base = float(args[i + 1])
            except (TypeError, ValueError):
                continue
            effective = max(INTENSITY_FLOOR, min(INTENSITY_CAP, base * multiplier))
            args[i + 1] = f"{effective:.3f}"
    return args


# ---------- Job tracking ----------
@dataclass
class Job:
    id: str
    image_path: Path
    motion_id: str
    duration: int
    output_path: Path
    strength: float = STRENGTH_DEFAULT
    status: str = "queued"  # queued / rendering / done / error
    error: Optional[str] = None
    started_at: float = 0.0
    finished_at: float = 0.0


JOBS: dict[str, Job] = {}
JOB_QUEUE: list[str] = []
QUEUE_LOCK = threading.Lock()
WORKER_STARTED = False


def queue_job(job: Job) -> None:
    with QUEUE_LOCK:
        JOBS[job.id] = job
        JOB_QUEUE.append(job.id)


def next_job() -> Optional[Job]:
    with QUEUE_LOCK:
        while JOB_QUEUE:
            job_id = JOB_QUEUE.pop(0)
            job = JOBS.get(job_id)
            if job and job.status == "queued":
                return job
    return None


MAX_INPUT_WIDTH = 1920  # cap input to 1080p-equivalent before depthflow


def _maybe_resize_input(image_path: Path, job_id: str) -> tuple[Path, bool]:
    """If the input image is larger than MAX_INPUT_WIDTH on its longest edge,
    write a downscaled copy to a temp file and return its path. Returns
    (path_to_use, is_temp). Caller is responsible for deleting if is_temp.

    Reason: depthflow renders at input resolution by default. On 4K+ source
    images x264 needs ~100MB of encoder buffer space and OOMs on machines
    with limited RAM. A team member hit this with an upscaled landscape
    image — error was "x264 [error]: malloc of size 102506624 failed".
    """
    try:
        from PIL import Image
    except ImportError:
        return image_path, False
    try:
        with Image.open(image_path) as img:
            iw, ih = img.size
            if max(iw, ih) <= MAX_INPUT_WIDTH:
                return image_path, False
            ratio = MAX_INPUT_WIDTH / max(iw, ih)
            new_size = (int(iw * ratio), int(ih * ratio))
            img = img.convert("RGB")  # depthflow prefers RGB, drops alpha
            img = img.resize(new_size, Image.LANCZOS)
            resized = image_path.parent / f".resized_{job_id}.png"
            img.save(resized, "PNG")
            return resized, True
    except Exception:
        return image_path, False


def run_depthflow(job: Job) -> None:
    motion = motion_by_id(job.motion_id)
    args = build_motion_args(motion, job.duration, job.strength)
    input_path, is_temp = _maybe_resize_input(job.image_path, job.id)
    cmd = [
        sys.executable, "-m", "depthflow",
        "input", "-i", str(input_path),
        *args,
        "main", "-o", str(job.output_path), "-t", str(job.duration),
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Suppress the cp1252 logging error chatter on Windows
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    finally:
        if is_temp:
            try: input_path.unlink()
            except Exception: pass
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise RuntimeError(f"DepthFlow failed:\n{tail}")
    if not job.output_path.exists() or job.output_path.stat().st_size < 1024:
        raise RuntimeError("DepthFlow produced no output")


def worker_loop() -> None:
    while True:
        job = next_job()
        if not job:
            time.sleep(0.5)
            continue
        job.status = "rendering"
        job.started_at = time.time()
        try:
            run_depthflow(job)
            job.status = "done"
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = str(exc)
        finally:
            job.finished_at = time.time()


def ensure_worker() -> None:
    global WORKER_STARTED
    if not WORKER_STARTED:
        WORKER_STARTED = True
        t = threading.Thread(target=worker_loop, daemon=True)
        t.start()


# ---------- FastAPI app ----------
app = FastAPI(title="Image Animator", version=APP_VERSION)


@app.on_event("startup")
def on_startup() -> None:
    ensure_worker()


STRENGTH_PRESETS = [
    {"value": 0.5, "label": "Subtle"},
    {"value": 1.0, "label": "Normal"},
    {"value": 1.5, "label": "Strong"},
    {"value": 2.0, "label": "Very strong"},
]


@app.get("/api/config")
def get_config() -> dict:
    return {
        "version": APP_VERSION,
        "build": APP_BUILD,
        "github_repo": GITHUB_REPO,
        "output_dir": str(current_output_dir()),
        "motions": MOTIONS,
        "default_motion_ids": DEFAULT_MOTION_IDS,
        "default_duration": current_default_duration(),
        "duration_min": DURATION_MIN,
        "duration_max": DURATION_MAX,
        "default_strength": STRENGTH_DEFAULT,
        "strength_presets": STRENGTH_PRESETS,
    }


@app.post("/api/set-default-duration")
def set_default_duration(payload: dict) -> dict:
    raw = payload.get("seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="seconds must be an integer")
    if value < DURATION_MIN or value > DURATION_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"seconds must be between {DURATION_MIN} and {DURATION_MAX}",
        )
    settings = load_settings()
    settings["default_duration"] = value
    save_settings(settings)
    return {"default_duration": value}


@app.post("/api/pick-output-dir")
def pick_output_dir() -> dict:
    # webview lives in the GUI thread; importing locally to avoid loading it
    # if the server is ever run headless (e.g. tests).
    try:
        import webview  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=500, detail=f"webview unavailable: {exc}")
    if not webview.windows:
        raise HTTPException(status_code=500, detail="no window")
    result = webview.windows[0].create_file_dialog(
        webview.FOLDER_DIALOG, allow_multiple=False,
    )
    if not result:
        return {"cancelled": True, "output_dir": str(current_output_dir())}
    new_dir = Path(result[0])
    new_dir.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    settings["output_dir"] = str(new_dir)
    save_settings(settings)
    return {"output_dir": str(new_dir)}


@app.get("/api/version")
def get_version() -> dict:
    return {"version": APP_VERSION, "build": APP_BUILD}


def _fetch_latest_release() -> dict:
    if not GITHUB_REPO:
        raise HTTPException(status_code=400, detail="no repo configured in version.json")
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def _find_patch_asset(release: dict) -> Optional[dict]:
    for a in (release.get("assets") or []):
        name = (a.get("name") or "").lower()
        if "patch" in name and name.endswith(".zip"):
            return a
    return None


@app.get("/api/check-update")
def check_update() -> dict:
    try:
        data = _fetch_latest_release()
    except HTTPException as exc:
        return {"available": False, "reason": exc.detail}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)}
    tag = (data.get("tag_name") or "").lstrip("v")
    patch_asset = _find_patch_asset(data)
    return {
        "available": bool(tag and tag != APP_VERSION),
        "latest_version": tag,
        "current_version": APP_VERSION,
        "patch_available": bool(patch_asset),
        "patch_asset_url": patch_asset.get("browser_download_url") if patch_asset else None,
        "release_url": data.get("html_url"),
        "release_notes": data.get("body") or "",
    }


# A self-deleting Windows .bat that waits for the running app to exit, extracts
# the downloaded patch over the app folder, then relaunches the main .bat.
# Uses `ping` as a sleep (works without a console — `timeout` requires one).
# Logs every step to %TEMP%\image_animator_updater.log so we can diagnose
# failed updates after the fact.
UPDATER_BAT = r"""@echo off
setlocal EnableExtensions
set "LOG=%TEMP%\image_animator_updater.log"
echo. >> "%LOG%"
echo [%DATE% %TIME%] updater started >> "%LOG%"
echo PID=%~1 PATCH=%~2 APPDIR=%~3 LAUNCHBAT=%~4 >> "%LOG%"

set "PID=%~1"
set "PATCH=%~2"
set "APPDIR=%~3"
set "LAUNCHBAT=%~4"

set /a TRIES=0
:waitloop
tasklist /FI "PID eq %PID%" 2>NUL | find "%PID%" >NUL
if errorlevel 1 goto do_extract
set /a TRIES+=1
if %TRIES% GEQ 30 goto do_extract
ping -n 2 127.0.0.1 >NUL
goto waitloop

:do_extract
echo [%DATE% %TIME%] extracting %PATCH% to %APPDIR% >> "%LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%PATCH%' -DestinationPath '%APPDIR%' -Force" >>"%LOG%" 2>&1
echo [%DATE% %TIME%] extract errorlevel=%ERRORLEVEL% >> "%LOG%"
del "%PATCH%" 2>NUL
echo [%DATE% %TIME%] launching %LAUNCHBAT% >> "%LOG%"
start "" "%LAUNCHBAT%"
echo [%DATE% %TIME%] updater done >> "%LOG%"
(goto) 2>NUL & del "%~f0"
"""


def _spawn_updater(patch_path: Path) -> None:
    bat_path = Path(tempfile.gettempdir()) / f"image_animator_updater_{uuid.uuid4().hex[:8]}.bat"
    bat_path.write_text(UPDATER_BAT, encoding="ascii", newline="\r\n")

    # Image Animator.bat sits one level above this app/ folder
    launcher_bat = HERE.parent / "Image Animator.bat"

    args = [
        "cmd.exe", "/c", str(bat_path),
        str(os.getpid()),
        str(patch_path),
        str(HERE),
        str(launcher_bat),
    ]
    creationflags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW hides the console window but still gives the bat a
        # console so `tasklist` / `ping` / `start` behave normally.
        # CREATE_NEW_PROCESS_GROUP keeps the child alive when we os._exit().
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(args, creationflags=creationflags, close_fds=True)

    # Give the response a moment to flush back to the browser, then exit hard
    threading.Timer(1.5, lambda: os._exit(0)).start()


@app.post("/api/perform-update")
def perform_update() -> dict:
    try:
        release = _fetch_latest_release()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"GitHub fetch failed: {exc}")

    tag = (release.get("tag_name") or "").lstrip("v")
    if not tag or tag == APP_VERSION:
        raise HTTPException(status_code=400, detail="already on the latest version")

    asset = _find_patch_asset(release)
    if not asset:
        raise HTTPException(
            status_code=400,
            detail="no patch asset for this release — manual full re-download required",
        )

    patch_url = asset["browser_download_url"]
    staging = HERE / ".update-staging"
    staging.mkdir(exist_ok=True)
    patch_path = staging / f"patch-{tag}.zip"

    try:
        with requests.get(patch_url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(patch_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"patch download failed: {exc}")

    _spawn_updater(patch_path)
    return {"ok": True, "new_version": tag}


@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...)) -> dict:
    if not file.filename:
        raise HTTPException(status_code=400, detail="missing filename")
    ext = Path(file.filename).suffix.lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise HTTPException(status_code=400, detail=f"unsupported type: {ext}")
    image_id = uuid.uuid4().hex[:12]
    target = UPLOADS_DIR / f"{image_id}{ext}"
    data = await file.read()
    target.write_bytes(data)
    return {
        "image_id": image_id,
        "filename": file.filename,
        "size": len(data),
        "preview_url": f"/api/image/{image_id}",
    }


@app.get("/api/image/{image_id}")
def get_image(image_id: str):
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = UPLOADS_DIR / f"{image_id}{ext}"
        if p.exists():
            return FileResponse(p)
    raise HTTPException(status_code=404, detail="image not found")


@app.post("/api/render")
def start_render(payload: dict) -> dict:
    image_id = payload.get("image_id")
    motion_id = payload.get("motion_id")
    try:
        duration = int(payload.get("duration") or current_default_duration())
    except (TypeError, ValueError):
        duration = current_default_duration()
    duration = max(DURATION_MIN, min(DURATION_MAX, duration))
    strength = clamp_strength(payload.get("strength", STRENGTH_DEFAULT))
    if not image_id or not motion_id:
        raise HTTPException(status_code=400, detail="image_id and motion_id required")
    motion_by_id(motion_id)
    image_path = None
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        p = UPLOADS_DIR / f"{image_id}{ext}"
        if p.exists():
            image_path = p
            break
    if not image_path:
        raise HTTPException(status_code=404, detail="image not found")

    job_id = uuid.uuid4().hex[:12]
    stem = image_path.stem
    output_path = current_output_dir() / f"{stem}__{motion_id}__{job_id}.mp4"
    job = Job(
        id=job_id,
        image_path=image_path,
        motion_id=motion_id,
        duration=duration,
        output_path=output_path,
        strength=strength,
    )
    queue_job(job)
    return {"job_id": job_id, "status": job.status}


@app.get("/api/job/{job_id}")
def job_status(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="unknown job")
    return {
        "job_id": job.id,
        "status": job.status,
        "error": job.error,
        "motion_id": job.motion_id,
        "duration": job.duration,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "output_url": f"/api/video/{job.id}" if job.status == "done" else None,
        "output_path": str(job.output_path) if job.status == "done" else None,
    }


@app.get("/api/video/{job_id}")
def get_video(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.status != "done":
        raise HTTPException(status_code=404, detail="not ready")
    return FileResponse(job.output_path, media_type="video/mp4")


@app.post("/api/reveal-output")
def reveal_output() -> dict:
    """Open the output folder in Explorer."""
    out = current_output_dir()
    if sys.platform == "win32":
        os.startfile(str(out))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(out)])  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", str(out)])  # noqa: S603,S607
    return {"ok": True, "path": str(out)}


# Static frontend
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")


def serve() -> None:
    import uvicorn

    port = int(os.environ.get("IMAGE_ANIMATOR_PORT", "5179"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    serve()
