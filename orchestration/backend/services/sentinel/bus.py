#  Sentinel Message Bus
#
#  Async pub/sub router for sentinel messages using asyncio queues.
#  Topics: resource_alert, stall_notification, intervention_proposal, sentinel_heartbeat.
#
#  Depends on: backend/services/sentinel/models.py
#  Used by:    sentinel monitors, intervention handlers

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Awaitable
from typing import Literal

from backend.services.sentinel.models import SentinelMessage

logger = logging.getLogger(__name__)

SentinelTopic = Literal[
    "resource_alert",
    "contention_advisory",
    "stall_notification",
    "intervention_proposal",
    "sentinel_heartbeat",
]

VALID_TOPICS: frozenset[str] = frozenset({
    # Existing observation topics
    "resource_alert",
    "contention_advisory",
    "stall_notification",
    "intervention_proposal",
    "sentinel_heartbeat",
    "plan_sentinel_stopped",
    # Orchestrator command topics (Phase 0+)
    "dispatch_command",
    "worker_event",
    "decision_made",
    "state_change",
    "dispatch_advisory",
})

# Type alias for async subscriber callbacks
SubscriberCallback = Callable[[SentinelMessage], Awaitable[None]]


class SentinelBus:
    """Async message bus with topic-based routing for sentinel messages.

    Supports two consumption patterns:
    - Queue-based: subscribe() returns an async iterator of messages
    - Callback-based: on() registers an async handler invoked on publish
    """

    def __init__(self, max_queue_size: int = 256):
        self._max_queue_size = max_queue_size
        # topic -> list of subscriber queues
        self._queues: dict[str, list[asyncio.Queue[SentinelMessage]]] = {}
        # topic -> list of async callbacks
        self._callbacks: dict[str, list[SubscriberCallback]] = {}
        self._running = True

    async def publish(self, message: SentinelMessage) -> int:
        """Publish a message to all subscribers of its topic.

        Returns the number of subscribers that received the message.
        """
        topic = message.topic
        if topic not in VALID_TOPICS:
            raise ValueError(f"Invalid topic: {topic!r}. Must be one of {VALID_TOPICS}")

        delivered = 0

        # Deliver to queue-based subscribers
        for queue in self._queues.get(topic, []):
            try:
                queue.put_nowait(message)
                delivered += 1
            except asyncio.QueueFull:
                logger.warning(
                    "Dropping sentinel message for slow subscriber on topic %s",
                    topic,
                )

        # Deliver to callback-based subscribers
        for callback in self._callbacks.get(topic, []):
            try:
                await callback(message)
                delivered += 1
            except Exception:
                logger.exception(
                    "Callback error on topic %s", topic,
                )

        return delivered

    def subscribe(
        self, *topics: SentinelTopic,
    ) -> _Subscription:
        """Subscribe to one or more topics via async iteration.

        Usage:
            sub = bus.subscribe("resource_alert", "stall_notification")
            async for msg in sub:
                handle(msg)
            # When done:
            sub.unsubscribe()
        """
        resolved = set(topics) if topics else VALID_TOPICS
        invalid = resolved - VALID_TOPICS
        if invalid:
            raise ValueError(f"Invalid topics: {invalid}")

        queue: asyncio.Queue[SentinelMessage] = asyncio.Queue(
            maxsize=self._max_queue_size,
        )
        for topic in resolved:
            self._queues.setdefault(topic, []).append(queue)

        return _Subscription(bus=self, queue=queue, topics=resolved)

    def on(
        self, topic: SentinelTopic, callback: SubscriberCallback,
    ) -> Callable[[], None]:
        """Register an async callback for a topic.

        Returns an unsubscribe function.
        """
        if topic not in VALID_TOPICS:
            raise ValueError(f"Invalid topic: {topic!r}")

        self._callbacks.setdefault(topic, []).append(callback)

        def unsubscribe():
            cbs = self._callbacks.get(topic, [])
            if callback in cbs:
                cbs.remove(callback)

        return unsubscribe

    def _remove_queue(self, queue: asyncio.Queue, topics: set[str]) -> None:
        for topic in topics:
            queues = self._queues.get(topic, [])
            if queue in queues:
                queues.remove(queue)
            if not queues and topic in self._queues:
                del self._queues[topic]

    async def shutdown(self) -> None:
        """Signal all queue-based subscribers to stop."""
        self._running = False
        sentinel = None  # type: ignore[assignment]
        for topic_queues in self._queues.values():
            for queue in topic_queues:
                try:
                    queue.put_nowait(sentinel)  # type: ignore[arg-type]
                except asyncio.QueueFull:
                    pass
        self._queues.clear()
        self._callbacks.clear()


class _Subscription:
    """Handle returned by SentinelBus.subscribe() for async iteration."""

    def __init__(
        self,
        bus: SentinelBus,
        queue: asyncio.Queue[SentinelMessage],
        topics: set[str],
    ):
        self._bus = bus
        self._queue = queue
        self._topics = topics

    def __aiter__(self) -> AsyncIterator[SentinelMessage]:
        return self

    async def __anext__(self) -> SentinelMessage:
        msg = await self._queue.get()
        if msg is None:
            raise StopAsyncIteration
        return msg

    def unsubscribe(self) -> None:
        self._bus._remove_queue(self._queue, self._topics)
