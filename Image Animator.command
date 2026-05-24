#!/bin/bash
# Image Animator — macOS launcher.
# Builds a native Mac Python environment with uv (depthflow + torch for
# Apple Silicon/Intel, no CUDA) on first run, then launches the app.
# Double-click in Finder, or run: ./Image\ Animator.command

cd "$(dirname "$0")" || exit 1

echo "=================================================="
echo " Image Animator (macOS)"
echo "=================================================="

# 1. Ensure uv is installed (manages its own Python + venv)
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (one-time)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
# uv installs to one of these; make sure it's on PATH for this session
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo ""
  echo "ERROR: uv did not install. Open https://astral.sh/uv and install manually,"
  echo "then run this again."
  echo "Press Return to close..."; read -r _; exit 1
fi

# 2. First-time setup: create venv + install deps (~3-5 min)
if [ ! -d ".venv" ]; then
  echo "First-time setup: installing dependencies (~3-5 min, one time)..."
  uv sync || { echo "uv sync failed"; echo "Press Return to close..."; read -r _; exit 1; }
  # Mac PyTorch (MPS on Apple Silicon / CPU on Intel) — default index, no CUDA.
  uv pip install torch torchvision || { echo "torch install failed"; echo "Press Return to close..."; read -r _; exit 1; }
fi

# 3. Launch
echo "Launching..."
uv run python launcher.py

# Keep the window open if the app exited with an error so it's readable.
code=$?
if [ $code -ne 0 ]; then
  echo ""
  echo "App exited with code $code. Scroll up for the error."
  echo "Press Return to close..."; read -r _
fi
