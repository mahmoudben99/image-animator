@echo off
REM Image Animator - dev launcher
REM Runs the FastAPI server + pywebview window via uv.

cd /d "%~dp0"

REM Make sure uv is installed
where uv >nul 2>nul
if errorlevel 1 (
    echo Installing uv (one-time)...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)

REM Sync deps + PyTorch CUDA on first run
if not exist ".venv" (
    echo First-time setup: installing dependencies (3-5 minutes)...
    uv sync
    uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
)

REM Launch the app
uv run python launcher.py
