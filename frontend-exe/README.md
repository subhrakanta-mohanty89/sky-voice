# Sky Voice AI — Desktop (`frontend-exe/`)

A native, double-clickable build of the operator console — same UI,
same code, same Cloud Run backend as the web app at
[skyvoice.web.app](https://skyvoice.web.app). The React bundle is
*reused 1:1* (not rewritten) and embedded inside a
[pywebview](https://pywebview.flowrl.com) window, then packaged with
PyInstaller.

| OS | Output |
|---|---|
| macOS 11+ | `dist/Sky Voice AI.app` |
| Windows 10/11 | `dist/SkyVoiceAI/SkyVoiceAI.exe` |
| Linux (GTK / Qt) | `dist/SkyVoiceAI/SkyVoiceAI` |

## Why a wrapper, not a Python rewrite?

The frontend is React + TypeScript + Twilio Voice SDK + WebRTC. Re-
implementing it in pure Python would (a) lose the Twilio Voice SDK
which only ships for JS, (b) break parity with the web app on every
future change, (c) duplicate ~10k lines of UI code. The wrapper
approach gives the user a real `.app`/`.exe`, while every fix shipped
to the web app gets picked up by the next desktop build automatically.

## Architecture

```
┌─────────────────────────────────────────────┐
│  Sky Voice AI.app   (PyInstaller bundle)    │
│  ┌────────────────────────────────────────┐ │
│  │ app.py                                 │ │
│  │  ├─ http.server on 127.0.0.1:<random>  │ │   ←  serves web/
│  │  └─ webview.create_window(url=…)       │ │
│  ├────────────────────────────────────────┤ │
│  │ web/    (= the React `dist/`)          │ │
│  │   ├─ index.html                        │ │
│  │   └─ assets/index-*.js,*.css           │ │
│  └────────────────────────────────────────┘ │
│              ↓ HTTPS                         │
│   https://mi-sky-ai-backend-…run.app         │
│   (same Cloud Run backend used by the web)   │
└─────────────────────────────────────────────┘
```

The local 127.0.0.1 server is required because the Twilio Voice SDK
won't grant `getUserMedia` (microphone) on a `file://` origin. Serving
from `127.0.0.1` is treated as a secure context by every modern
WebView engine.

## Quick start

### Run from source (no build, fastest)

```bash
cd frontend-exe
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Option A — point the window at the live deployed app:
SKYVOICE_URL=https://skyvoice.web.app python3 app.py

# Option B — point it at a local vite dev server (live-reload):
cd ../frontend && npm run dev &        # starts http://localhost:5173
cd ../frontend-exe
SKYVOICE_URL=http://localhost:5173 python3 app.py

# Option C — bundled mode (need to build the frontend once):
cd ../frontend && npm run build
cp -R dist ../frontend-exe/web
cd ../frontend-exe && python3 app.py
```

### Build the .app / .exe

**macOS / Linux**:

```bash
cd frontend-exe
./build.sh                   # ~2-5 min on first run
open "dist/Sky Voice AI.app" # launch
```

**Windows**:

```bat
cd frontend-exe
build.bat
dist\SkyVoiceAI\SkyVoiceAI.exe
```

`build.sh` / `build.bat` automatically:

1. runs `npm run build` in `../frontend/`,
2. copies `dist/` to `frontend-exe/web/`,
3. creates `.venv` and installs `pywebview` + `pyinstaller`,
4. runs PyInstaller against [`SkyVoiceAI.spec`](SkyVoiceAI.spec).

### Get a Windows `.exe` from a Mac (GitHub Actions)

PyInstaller **cannot cross-compile** — a Windows `.exe` can only be built
on Windows, and a macOS `.app` only on macOS. If you're on a Mac and have
no Windows machine, let CI build it for you. This folder is self-contained
(the built React bundle ships in `web/`), so the workflow needs neither
Node nor the private frontend repo.

1. Put this `frontend-exe/` folder in its own GitHub repo and push it:

   ```bash
   cd frontend-exe
   git init && git add . && git commit -m "Sky Voice AI desktop wrapper"
   git branch -M main
   git remote add origin https://github.com/<you>/sky-voice-ai-desktop.git
   git push -u origin main
   ```

2. The included workflow
   [`.github/workflows/build-desktop.yml`](.github/workflows/build-desktop.yml)
   runs automatically on every push to `main` (or trigger it from the repo's
   **Actions** tab → *Run workflow*). It builds on `windows-latest` **and**
   `macos-latest` in parallel.

3. Open the finished run and download the artifacts:
   * **`SkyVoiceAI-windows`** → unzip → share the `SkyVoiceAI/` folder;
     recipients double-click `SkyVoiceAI.exe`.
   * **`SkyVoiceAI-macos`** → unzip → `Sky Voice AI.app`.

> Whenever the web app changes, re-run `./build.sh` (or `build.bat`) locally
> to refresh `web/`, commit it, and push — CI rebuilds fresh binaries.

## Configuration

| Env var | Purpose |
|---|---|
| `SKYVOICE_URL` | Override the URL the window loads. Useful for dev (`http://localhost:5173`) or to point the desktop app at a staging deployment. |
| `SKYVOICE_DEBUG=1` | Open the WebView's devtools (right-click → Inspect on macOS). |
| `SKYVOICE_LOG=DEBUG` | Verbose Python logging. |

The Cloud Run backend URL itself is **not** configurable here — it's
baked into the React bundle at `npm run build` time via Vite's
`frontend/.env.production` (`VITE_API_BASE`, `VITE_WS_BASE`). To
re-target the desktop build at a different backend, edit those env
vars in `frontend/` and rerun `build.sh`.

## App icon

The bundle is already branded with the Sky Voice AI mark — a multi-
resolution [`assets/icon.icns`](assets) (macOS) and [`assets/icon.ico`](assets)
(Windows) are committed, and [`SkyVoiceAI.spec`](SkyVoiceAI.spec) picks the
right one per platform automatically. To regenerate them from a new logo:

```bash
cd frontend-exe
mkdir -p assets

# macOS — convert from the React app's logo:
sips -s format icns ../frontend/public/logo-512.png --out assets/icon.icns

# Windows — convert from PNG to ICO using ImageMagick or pillow:
python -c "from PIL import Image; Image.open('../frontend/public/logo-512.png').save('assets/icon.ico', sizes=[(256,256),(128,128),(64,64),(32,32),(16,16)])"
```

The spec file ([`SkyVoiceAI.spec`](SkyVoiceAI.spec)) automatically
picks `assets/icon.icns` on macOS and `assets/icon.ico` on Windows when
they exist.

## Permissions

| OS | Prompt | First-run behaviour |
|---|---|---|
| macOS | Microphone (`NSMicrophoneUsageDescription`) | System dialog appears the first time the operator clicks **Dial**. Allowed/denied state persists. |
| Windows | Edge WebView2 mic | Edge WebView2 surfaces a popup; allow once. |
| Linux | PulseAudio / PipeWire | Handled by the system mixer; no popup. |

## Codesigning & distribution (macOS)

Out of the box the .app is **unsigned**, which means Gatekeeper will
warn the first time it's launched. For internal use that's fine
(right-click → Open). For external distribution:

1. Get a Developer ID Application certificate.
2. Add `codesign_identity="Developer ID Application: Your Name (TEAMID)"`
   to the `EXE(...)` block in `SkyVoiceAI.spec`.
3. Notarize: `xcrun notarytool submit "dist/Sky Voice AI.app" --keychain-profile=AC_PASSWORD --wait`.

## Troubleshooting

* **"missing dependency: pyobjc-framework-WebKit"** on macOS — your
  system Python is too old. Use Python 3.11+ (Homebrew or python.org).
* **White window** on Windows — install the
  [Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/).
  Bundled with Win11 by default.
* **Mic permission denied** — quit, then on macOS go to *System
  Settings → Privacy & Security → Microphone* and enable Sky Voice AI.
* **App opens but UI is blank** — make sure `web/index.html` exists.
  Re-run `build.sh`.
* **Voice SDK error 31000 ("getUserMedia")** — the page must be loaded
  over `127.0.0.1` or `https://`. Don't open `web/index.html` directly
  from `file://`; let `app.py` start the local server.
