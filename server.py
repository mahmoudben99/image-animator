"""Image Animator — local FastAPI server that drives DepthFlow renders."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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

DEFAULT_MOTION_IDS = [m["id"] for m in MOTIONS if m["id"] != "diagonal_pan_zoom"]


def motion_by_id(motion_id: str) -> dict:
    for m in MOTIONS:
        if m["id"] == motion_id:
            return m
    raise HTTPException(status_code=400, detail=f"Unknown motion: {motion_id}")


# ---------- Job tracking ----------
@dataclass
class Job:
    id: str
    image_path: Path
    motion_id: str
    duration: int
    output_path: Path
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


def run_depthflow(job: Job) -> None:
    motion = motion_by_id(job.motion_id)
    cmd = [
        sys.executable, "-m", "depthflow",
        "input", "-i", str(job.image_path),
        *motion["args"],
        "main", "-o", str(job.output_path), "-t", str(job.duration),
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Suppress the cp1252 logging error chatter on Windows
    proc = subprocess.run(
        cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
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


@app.get("/api/config")
def get_config() -> dict:
    return {
        "version": APP_VERSION,
        "build": APP_BUILD,
        "github_repo": GITHUB_REPO,
        "output_dir": str(DEFAULT_OUTPUT_DIR),
        "motions": MOTIONS,
        "default_motion_ids": DEFAULT_MOTION_IDS,
    }


@app.get("/api/version")
def get_version() -> dict:
    return {"version": APP_VERSION, "build": APP_BUILD}


@app.get("/api/check-update")
def check_update() -> dict:
    if not GITHUB_REPO:
        return {"available": False, "reason": "no repo configured"}
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)}
    tag = (data.get("tag_name") or "").lstrip("v")
    asset = None
    for a in data.get("assets") or []:
        if a.get("name", "").endswith(".zip"):
            asset = a
            break
    return {
        "available": bool(tag and tag != APP_VERSION),
        "latest_version": tag,
        "current_version": APP_VERSION,
        "asset_url": asset.get("browser_download_url") if asset else None,
        "asset_name": asset.get("name") if asset else None,
        "release_notes": data.get("body") or "",
    }


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
    duration = int(payload.get("duration") or 5)
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
    output_path = DEFAULT_OUTPUT_DIR / f"{stem}__{motion_id}__{job_id}.mp4"
    job = Job(
        id=job_id,
        image_path=image_path,
        motion_id=motion_id,
        duration=duration,
        output_path=output_path,
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
    if sys.platform == "win32":
        os.startfile(str(DEFAULT_OUTPUT_DIR))  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(DEFAULT_OUTPUT_DIR)])  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", str(DEFAULT_OUTPUT_DIR)])  # noqa: S603,S607
    return {"ok": True, "path": str(DEFAULT_OUTPUT_DIR)}


# Static frontend
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")


def serve() -> None:
    import uvicorn

    port = int(os.environ.get("IMAGE_ANIMATOR_PORT", "5179"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    serve()
