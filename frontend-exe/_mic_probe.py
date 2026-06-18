"""Diagnostic: reproduce the WKWebView getUserMedia request in isolation.

Applies the same media-permission patch as app.py, serves a tiny page over
http://127.0.0.1 (a secure context, like the real bundle), then calls
navigator.mediaDevices.getUserMedia({audio:true}) + enumerateDevices() and
prints the exact result/error name back to the terminal.

Run:  .venv/bin/python _mic_probe.py
"""
from __future__ import annotations

import http.server
import socket
import socketserver
import threading
import time

import webview

import app as exe_app  # reuse the real permission patch

HTML = """<!doctype html><html><head><meta charset=utf-8></head><body>
<h1 id=s>pending</h1>
<script>
window.__r = {phase:'start'};
async function go() {
  try {
    const devsBefore = await navigator.mediaDevices.enumerateDevices();
    window.__r.before = devsBefore.map(d => d.kind + ':' + (d.label||'(no-label)'));
    const stream = await navigator.mediaDevices.getUserMedia({audio:true});
    const tracks = stream.getAudioTracks();
    window.__r.ok = true;
    window.__r.tracks = tracks.map(t => t.label || '(unnamed)');
    const devsAfter = await navigator.mediaDevices.enumerateDevices();
    window.__r.after = devsAfter.map(d => d.kind + ':' + (d.label||'(no-label)'));
    document.getElementById('s').textContent = 'OK';
  } catch (e) {
    window.__r.ok = false;
    window.__r.errName = e.name;
    window.__r.errMsg = e.message;
    document.getElementById('s').textContent = 'ERR ' + e.name;
  }
  window.__r.phase = 'done';
}
go();
</script></body></html>"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def log_message(self, *_a):  # noqa: N802
        return


def main() -> None:
    exe_app._enable_webview_media_permissions()
    port = _free_port()
    srv = socketserver.TCPServer(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"

    window = webview.create_window("mic-probe", url=url, width=420, height=240)

    def probe():
        result = None
        for _ in range(30):  # up to ~15s (TCC prompt may need a click)
            time.sleep(0.5)
            try:
                r = window.evaluate_js("JSON.stringify(window.__r)")
            except Exception:  # noqa: BLE001
                continue
            if r and '"phase":"done"' in r:
                result = r
                break
        print("\n=== MIC PROBE RESULT ===")
        print(result or "(no result — getUserMedia never resolved; TCC prompt may be open)")
        print("========================\n")
        window.destroy()

    webview.start(probe, private_mode=False)


if __name__ == "__main__":
    main()
