"""Fan-out of freshly observed runtime edges to live subscribers (WebSocket and SSE)."""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from typing import Any

_QUEUE_SIZE = 256


class TelemetryHub:
    """In-process publish/subscribe keyed by repository.

    Subscribers of one API process see the events that process ingested. Running
    several API replicas therefore needs sticky routing for live views (the stored
    counters are shared regardless).
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, repository_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subscribers[repository_id].add(queue)
        return queue

    def unsubscribe(self, repository_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(repository_id)
        if subscribers is not None:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(repository_id, None)

    def subscriber_count(self, repository_id: str) -> int:
        return len(self._subscribers.get(repository_id, ()))

    def _deliver(self, repository_id: str, message: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers.get(repository_id, ())):
            if queue.full():  # a slow consumer loses the oldest update rather than blocking the rest
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    def publish(self, repository_id: str, message: dict[str, Any]) -> None:
        """Publish from any thread (request handlers run in a worker thread)."""
        loop = self._loop
        if loop is None or loop.is_closed() or not self._subscribers.get(repository_id):
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(repository_id, message)
        else:
            loop.call_soon_threadsafe(self._deliver, repository_id, message)
