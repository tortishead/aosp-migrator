import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

from fake_jira.app import create_fake_jira
from fake_jira.store import FakeJiraStore


@dataclass
class FakeJiraServer:
    url: str
    app: FastAPI

    @property
    def store(self) -> FakeJiraStore:
        return self.app.state.store

    @store.setter
    def store(self, store: FakeJiraStore) -> None:
        self.app.state.store = store


@contextmanager
def run_fake_jira(store: FakeJiraStore, host: str = "127.0.0.1", port: int = 0) -> Iterator[FakeJiraServer]:
    """Serve the fake Jira over real HTTP in a background thread."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    port = sock.getsockname()[1]

    app = create_fake_jira(store)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("Fake Jira failed to start")
        threading.Event().wait(0.01)
    try:
        yield FakeJiraServer(f"http://{host}:{port}", app)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
