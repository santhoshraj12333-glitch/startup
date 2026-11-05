#!/usr/bin/env bash
# ---------------------------------------------------------------------
# One-command demo (Linux / macOS).
#
#   ./demo.sh                          first clip in data/videos
#   ./demo.sh data/videos/my_clip.mp4  a specific clip
#   ./demo.sh --multi-bus              3 buses -> cross-bus dedup visible
# ---------------------------------------------------------------------
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "--- checking the environment ---"
"$PY" scripts/check_setup.py || {
  echo
  echo "Setup check failed.  Try:  $PY -m pip install -r requirements.txt"
  exit 1
}

echo
echo "--- running the demo ---"
if [ $# -gt 0 ] && [ -f "$1" ]; then
  SRC="$1"; shift
  exec "$PY" scripts/run_demo.py --source "$SRC" "$@"
else
  exec "$PY" scripts/run_demo.py "$@"
fi
