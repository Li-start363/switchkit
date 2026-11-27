#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""switchkit.app 入口(2026-11-25)——原生窗口版:内起回环 HTTP 服务,pywebview 加载现网页 UI。
零 JS 改动复用 switchkit.PAGE;打包:pyinstaller --windowed --name switchkit --add-data exclude_map.json:. switchkit_app.py"""
import socket, sys, threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import switchkit  # noqa: E402
import webview    # noqa: E402

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def main():
    import urllib.request
    log = Path("/tmp/skapp_debug.log")
    def dbg(msg):
        with open(log, "a") as f: f.write(msg + "\n")
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), switchkit.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    dbg(f"port={port}")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
            dbg(f"selftest_http={r.status} bytes={len(r.read())}")
    except Exception as e:
        dbg(f"selftest_FAIL {e!r}")
    win = webview.create_window("switchkit · 双层分发台", f"http://127.0.0.1:{port}/",
                                width=900, height=700, min_size=(680, 520))
    dbg("window_created")
    win.events.loaded += lambda: dbg("page_loaded")
    win.events.shown += lambda: dbg("window_shown")
    try:
        webview.start()
    except Exception as e:
        dbg(f"webview_start_FAIL {e!r}")
    dbg("webview_exited")

if __name__ == "__main__":
    main()
