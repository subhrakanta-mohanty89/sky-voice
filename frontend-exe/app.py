"""
Sky Voice AI — Desktop Operator Console
========================================
Native shell around the existing React frontend (``frontend/dist``)
using pywebview. Built once with ``build.sh`` (macOS) or ``build.bat``
(Windows), this produces a single double-clickable

  * macOS:    ``dist/Sky Voice AI.app``
  * Windows:  ``dist/SkyVoiceAI/SkyVoiceAI.exe``
  * Linux:    ``dist/SkyVoiceAI/SkyVoiceAI``

that:

  * serves the React bundle from a local HTTP server bound to
    ``127.0.0.1`` (the Twilio Voice SDK requires a secure-context origin
    for ``getUserMedia`` / WebRTC; ``file://`` is rejected),
  * opens a native window via pywebview backed by WKWebView (macOS),
    EdgeWebView2 (Windows) or WebKitGTK (Linux),
  * talks to the same Cloud Run backend the web app uses — URLs are
    baked into the React bundle at build time via Vite's
    ``frontend/.env.production`` (``VITE_API_BASE`` etc.). No code or
    design is duplicated; this exe IS the same UI.

Run modes
---------
1. **Bundled** (production): ``./web`` exists, served over a random
   localhost port.
2. **Dev**: set ``SKYVOICE_URL=http://localhost:5173`` to point the
   window at a running ``vite dev`` server (in ``frontend/``). Useful
   for live-reload debugging without rebuilding.
3. **Live**: if neither of the above is available, falls back to
   ``https://skyvoice.web.app`` so the exe still works on a fresh
   install with no bundled assets.

Examples::

    python3 app.py                                    # bundled
    SKYVOICE_URL=http://localhost:5173 python3 app.py # dev
    SKYVOICE_DEBUG=1 python3 app.py                   # devtools
"""

from __future__ import annotations

import http.server
import logging
import os
import socket
import socketserver
import sys
import threading
from pathlib import Path

import webview

# --------------------------------------------------------------------------- #
#  Constants
# --------------------------------------------------------------------------- #

# When frozen by PyInstaller, datas added via the spec land under
# ``sys._MEIPASS``. In dev they live next to this file.
_BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()
WEB_DIR = (_BASE_DIR / "web").resolve()

TITLE = "Sky Voice AI — Operator Console"
LIVE_FALLBACK_URL = "https://skyvoice.web.app"

DEFAULT_WIDTH = 1320
DEFAULT_HEIGHT = 860
MIN_WIDTH = 1024
MIN_HEIGHT = 720

logger = logging.getLogger("skyvoice-exe")
logging.basicConfig(
    level=os.getenv("SKYVOICE_LOG", "WARNING").upper(),
    format="[%(levelname)s] %(message)s",
)


# --------------------------------------------------------------------------- #
#  Local HTTP server (SPA-aware)
# --------------------------------------------------------------------------- #

class _SPAHandler(http.server.SimpleHTTPRequestHandler):
    """Static file handler that rewrites unknown non-asset paths to
    ``/index.html`` so React Router (hash-based here) and any
    server-rendered fallbacks work."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self):
        # No cache so a rebuild + relaunch always picks up the fresh JS.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_GET(self):  # noqa: N802 - http.server signature
        path = self.path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        if path:
            try:
                candidate = (WEB_DIR / path).resolve()
                # Reject path traversal (../).
                candidate.relative_to(WEB_DIR)
            except ValueError:
                self.send_error(403, "forbidden")
                return
            if not candidate.exists() and "." not in os.path.basename(path):
                self.path = "/index.html"
        return super().do_GET()

    def log_message(self, *_args, **_kwargs):  # noqa: D401, N802
        # Silence per-request stdout noise.
        return


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start_local_server() -> str:
    port = _free_port()
    server = _ThreadingTCPServer(("127.0.0.1", port), _SPAHandler)
    threading.Thread(
        target=server.serve_forever,
        name="skyvoice-http",
        daemon=True,
    ).start()
    url = f"http://127.0.0.1:{port}"
    logger.info("Serving %s on %s", WEB_DIR, url)
    return url


# --------------------------------------------------------------------------- #
#  Entry point
# --------------------------------------------------------------------------- #

def _enable_webview_media_permissions() -> None:
    """Grant the embedded webview access to the microphone.

    The Twilio Voice SDK calls ``getUserMedia`` the moment a call is
    answered. WKWebView (macOS) **denies** that by default unless the host
    app implements the WKUIDelegate media-capture permission callback — so
    without this the operator's mic never opens, the Twilio call raises an
    error ('Error' pill), and the still-connected caller falls through to
    the AI receptionist (Sara) instead of the human who answered.

    We patch pywebview's Cocoa delegate to auto-grant the request. Safe and
    best-effort: any failure is logged and the app continues (the browser
    build still works; only the packaged-app mic is affected).
    """
    if sys.platform == "darwin":
        _enable_macos_media_permissions()


def _enable_macos_media_permissions() -> None:
    try:
        import WebKit  # noqa: F401  ensures WKUIDelegate metadata is loaded
        from webview.platforms import cocoa
    except Exception as exc:  # noqa: BLE001
        logger.warning("Skipping macOS media-permission patch: %s", exc)
        return

    browser_view = getattr(cocoa, "BrowserView", None)
    base_delegate = getattr(browser_view, "BrowserDelegate", None)
    if browser_view is None or base_delegate is None:
        logger.warning("pywebview Cocoa BrowserDelegate not found — mic may stay blocked")
        return

    # Idempotent: once our subclass is installed it carries this marker.
    if getattr(base_delegate, "_skyvoice_media_granted", False):
        return

    _WK_PERMISSION_GRANT = 1  # WKPermissionDecisionGrant

    class _MediaPermissiveDelegate(base_delegate):
        # WKUIDelegate (macOS 12+): asked before the page may use the
        # camera/microphone. Grant unconditionally — the only page loaded is
        # our own trusted bundle talking to the Twilio Voice SDK.
        def webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_(  # noqa: N802,E501
            self, web_view, origin, frame, media_type, decision_handler
        ):
            decision_handler(_WK_PERMISSION_GRANT)

    _MediaPermissiveDelegate._skyvoice_media_granted = True
    browser_view.BrowserDelegate = _MediaPermissiveDelegate
    logger.info("macOS webview microphone permission auto-granted")


def _resolve_url() -> str:
    """Pick which URL the pywebview window should load.

    Order of preference:
      1. ``SKYVOICE_URL`` env var (dev / live override).
      2. Local HTTP server over ``./web`` (bundled production mode).
      3. ``LIVE_FALLBACK_URL`` (if neither of the above is available).
    """
    override = os.getenv("SKYVOICE_URL", "").strip()
    if override:
        logger.info("Override URL: %s", override)
        return override

    if (WEB_DIR / "index.html").exists():
        return _start_local_server()

    logger.warning(
        "No bundled web/ folder and SKYVOICE_URL not set — falling back "
        "to %s. Run build.sh / build.bat to bundle the React app for "
        "fully-offline-capable launches.",
        LIVE_FALLBACK_URL,
    )
    return LIVE_FALLBACK_URL


def main() -> None:
    # Grant the embedded webview microphone access BEFORE the GUI loop
    # instantiates the delegate, so answered calls get two-way audio.
    _enable_webview_media_permissions()

    url = _resolve_url()

    webview.create_window(
        TITLE,
        url=url,
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        resizable=True,
        confirm_close=False,
        text_select=True,
        background_color="#0f172a",  # matches the React app's dark slate
    )

    webview.start(
        debug=bool(os.getenv("SKYVOICE_DEBUG")),
        # Persist localStorage so the operator's JWT / theme survive
        # restarts. (pywebview defaults to private mode, which would
        # log them out every launch.)
        private_mode=False,
        http_server=False,  # we run our own SPA-aware one above
    )


if __name__ == "__main__":
    main()
