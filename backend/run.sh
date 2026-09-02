#!/usr/bin/env bash
# Launch the meetcfg backend.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f "meetcfg.env" ]]; then
  set -a; source meetcfg.env; set +a
fi

VENV="$REPO_ROOT/.venv"
if [[ ! -x "$VENV/bin/uvicorn" ]]; then
  echo "venv not found at $VENV — run: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt" >&2
  exit 1
fi

# faster-whisper (CTranslate2) loads cuBLAS/cuDNN from the pip-installed
# nvidia wheels in the venv; make those discoverable when running on GPU.
if [[ "${WHISPER_DEVICE:-cpu}" == "cuda" ]]; then
  NVIDIA_LIBS="$(find "$VENV"/lib/python*/site-packages/nvidia -maxdepth 2 -name lib -type d 2>/dev/null | paste -sd: -)"
  if [[ -n "$NVIDIA_LIBS" ]]; then
    export LD_LIBRARY_PATH="${NVIDIA_LIBS}:${LD_LIBRARY_PATH:-}"
  fi
fi

exec "$VENV/bin/uvicorn" app.main:app \
  --app-dir "$REPO_ROOT/backend" \
  --host "${MEETCFG_HOST:-0.0.0.0}" \
  --port "${MEETCFG_PORT:-5005}"
