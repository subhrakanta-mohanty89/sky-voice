#!/usr/bin/env bash
#
# Sky Voice AI — desktop bundle build (macOS / Linux)
#
# Steps:
#   1. npm run build in ../frontend (produces ../frontend/dist)
#   2. copy dist/ → ./web (PyInstaller will pick this up via the spec)
#   3. ensure ./.venv exists with pywebview + pyinstaller installed
#   4. run pyinstaller against SkyVoiceAI.spec
#
# Output:
#   ./dist/Sky Voice AI.app           (macOS)
#   ./dist/SkyVoiceAI/SkyVoiceAI       (Linux)
#
set -euo pipefail

cd "$(dirname "$0")"
ROOT=$(pwd -P)
FRONTEND_DIR="$ROOT/../frontend"

if [[ ! -d "$FRONTEND_DIR" ]]; then
  echo "✗ frontend/ not found at $FRONTEND_DIR" >&2
  exit 1
fi

echo "▶ npm run build (frontend)"
( cd "$FRONTEND_DIR" && npm run build )

echo "▶ syncing dist → web/"
rm -rf "$ROOT/web"
cp -R "$FRONTEND_DIR/dist" "$ROOT/web"

if [[ ! -d "$ROOT/.venv" ]]; then
  echo "▶ creating venv (.venv)"
  python3 -m venv "$ROOT/.venv"
fi

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

echo "▶ installing python deps"
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet

echo "▶ pyinstaller"
rm -rf "$ROOT/build" "$ROOT/dist"
pyinstaller --noconfirm --log-level=WARN SkyVoiceAI.spec

echo
echo "✔ done — see ./dist/"
ls -1 dist/
