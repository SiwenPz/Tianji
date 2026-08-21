"""驾驶舱 Web 常驻入口(票 15 最小版→票 03 完整页)。

完整页实现在 webapp.py(FastAPI 单页应用,19.2);本模块只留端口顺延
(18.2 只绑回环、固定默认号、冲突顺延+1)与进程入口,供 daemon 守护拉起。
"""

import socket
import sys

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


def run_web(port: "int | None" = None) -> None:
    """启动驾驶舱 Web(阻塞)。port 为空则用默认号顺延。"""
    import uvicorn

    from .webapp import app
    port = _find_port(port if port is not None else DEFAULT_PORT)
    print(f"驾驶舱 Web 启动: http://127.0.0.1:{port} (Ctrl+C 停止)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    _port = DEFAULT_PORT
    if len(sys.argv) > 1 and sys.argv[1] == "--port":
        _port = int(sys.argv[2])
    run_web(_port)
