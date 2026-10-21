"""Helpers for the remote-transport tests: run the real app under uvicorn on a free port."""
import socket
import threading
import time

import uvicorn


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Served:
    def __init__(self, app, port=None):
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                             lifespan="on")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


def mcp_client(url, token, **kw):
    """An mcp Client over Streamable HTTP with the Bearer header."""
    from mcp.client.client import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    return Client(streamable_http_client(url + "/mcp", http_client=http), **kw), http
