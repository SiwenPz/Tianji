"""驾驶舱 Web 最小常驻(18.2/19.2): 只读快照 JSON,回环端口冲突顺延+1。

本票(15)只提供可被 daemon 守护的常驻最小形态: 读账本渲染只读快照,
不写账本、不注册消费者(与 cockpit.py 同源)。完整页面/SSE 归票 03。
端口: 只绑回环 127.0.0.1,固定默认号,冲突顺延 +1(18.2)。
"""

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import cockpit, plugins
from .db import connect

DEFAULT_PORT = 8787


def _find_port(start: int = DEFAULT_PORT) -> int:
    """从 start 起顺延找可绑定回环端口(+1 规则,18.2)。"""
    for port in range(start, start + 100):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    return start


class _Handler(BaseHTTPRequestHandler):
    """只读快照端点: GET / → 文本快照;GET /api/snapshot → JSON。"""

    def do_GET(self):
        if self.path == "/api/snapshot":
            body = json.dumps(self._snapshot(), ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        elif self.path in ("/", "/index.html"):
            body = self._snapshot_text().encode("utf-8")
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _snapshot(self) -> dict:
        conn = connect()
        try:
            return cockpit.snapshot(conn)
        finally:
            conn.close()

    def _snapshot_text(self) -> str:
        conn = connect()
        try:
            snap = cockpit.snapshot(conn)
            text = cockpit.render_snapshot(
                snap, plugins.render_view_blocks(conn, snap))
        finally:
            conn.close()
        esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<html><meta charset='utf-8'><body><pre>{esc}</pre></body></html>"

    def log_message(self, fmt, *args):
        pass  # 静默,不刷日志


def run_web(port: int | None = None) -> None:
    """启动驾驶舱 Web(阻塞)。port 为空则用默认号顺延。"""
    port = _find_port(port if port is not None else DEFAULT_PORT)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"驾驶舱 Web 启动: http://127.0.0.1:{port} (Ctrl+C 停止)", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    _port = DEFAULT_PORT
    if len(sys.argv) > 1 and sys.argv[1] == "--port":
        _port = int(sys.argv[2])
    run_web(_port)
