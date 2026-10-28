"""In-process pub/sub hub used to push events to browser websockets.

Topics:
    ``project:<pid>``   board / tracker / session-list / fs / git-view events
    ``session:<sid>``   normalized chat items streamed from a live session
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any


class Subscriber:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)
        self.topics: set[str] = set()
        self.active: bool = False  # the browser page is visible and focused (reported over the socket)


class Hub:
    def __init__(self) -> None:
        self._subs: dict[str, set[Subscriber]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, sub: Subscriber, topic: str) -> None:
        sub.topics.add(topic)
        self._subs[topic].add(sub)

    def unsubscribe(self, sub: Subscriber, topic: str) -> None:
        sub.topics.discard(topic)
        self._subs[topic].discard(sub)

    def drop(self, sub: Subscriber) -> None:
        for t in list(sub.topics):
            self.unsubscribe(sub, t)

    def watching(self, topic: str) -> bool:
        """Whether someone is looking at ``topic``: an active page is subscribed to it."""
        return any(sub.active for sub in self._subs.get(topic, ()))

    def publish(self, topic: str, event: dict[str, Any]) -> None:
        """Publish from the event loop thread."""
        msg = {"topic": topic, "ts": time.time(), **event}
        for sub in list(self._subs.get(topic, ())):
            try:
                sub.queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow consumer; it will resync on reconnect

    def publish_threadsafe(self, topic: str, event: dict[str, Any]) -> None:
        """Publish from a worker thread (e.g. the file watcher)."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self.publish, topic, event)


hub = Hub()
