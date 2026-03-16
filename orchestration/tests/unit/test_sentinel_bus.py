"""Unit tests for the SentinelBus."""

import asyncio
from unittest.mock import Mock, AsyncMock

import pytest

from backend.services.sentinel.bus import SentinelBus, VALID_TOPICS
from backend.services.sentinel.models import SentinelMessage


@pytest.fixture
def bus():
    """Provides a fresh SentinelBus instance for each test."""
    return SentinelBus()


@pytest.fixture
def small_bus():
    """Bus with tiny queue for overflow testing."""
    return SentinelBus(max_queue_size=2)


def _msg(topic: str, **payload) -> SentinelMessage:
    return SentinelMessage(topic=topic, source="test", payload=payload)


# ── Initialization ──────────────────────────────────────────────


async def test_bus_initialization(bus: SentinelBus):
    assert bus._queues == {}
    assert bus._callbacks == {}


async def test_bus_custom_queue_size():
    b = SentinelBus(max_queue_size=10)
    assert b._max_queue_size == 10


# ── Topic routing: new Phase 0 topics ──────────────────────────


NEW_TOPICS = [
    "dispatch_command",
    "worker_event",
    "decision_made",
    "state_change",
    "dispatch_advisory",
]


@pytest.mark.parametrize("topic", NEW_TOPICS)
async def test_publish_and_subscribe_new_topics(bus: SentinelBus, topic: str):
    """Verify that new topics can be published to and subscribed from."""
    message = _msg(topic, test="data")
    received = []

    sub = bus.subscribe(topic)

    async def consume():
        async for msg in sub:
            received.append(msg)

    consumer_task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    delivered = await bus.publish(message)
    await asyncio.sleep(0.01)

    await bus.shutdown()
    sub.unsubscribe()
    await consumer_task

    assert delivered == 1
    assert len(received) == 1
    assert received[0].topic == topic
    assert received[0].payload == {"test": "data"}


@pytest.mark.parametrize("topic", NEW_TOPICS)
async def test_callback_subscription_new_topics(bus: SentinelBus, topic: str):
    """Test 'on' method for new topics."""
    message = _msg(topic, value=42)
    mock_callback = AsyncMock()

    unsubscribe = bus.on(topic, mock_callback)
    delivered = await bus.publish(message)

    assert delivered == 1
    mock_callback.assert_awaited_once_with(message)

    # After unsubscribe, callback should not fire again
    unsubscribe()
    await bus.publish(message)
    mock_callback.assert_awaited_once()


# ── Validation ──────────────────────────────────────────────────


async def test_publish_invalid_topic_raises_error(bus: SentinelBus):
    message = _msg("no_such_topic")
    with pytest.raises(ValueError, match="Invalid topic"):
        await bus.publish(message)


async def test_subscribe_invalid_topic_raises_error(bus: SentinelBus):
    with pytest.raises(ValueError, match="Invalid topics"):
        bus.subscribe("no_such_topic")


async def test_on_invalid_topic_raises_error(bus: SentinelBus):
    with pytest.raises(ValueError, match="Invalid topic"):
        bus.on("bogus_topic", AsyncMock())


# ── Multiple subscribers ────────────────────────────────────────


async def test_multiple_subscribers(bus: SentinelBus):
    """Message delivered to both queue and callback subscribers."""
    topic = "decision_made"
    message = _msg(topic, decision="proceed")

    # Queue subscriber
    sub1 = bus.subscribe(topic)
    received1 = []

    async def consume1():
        async for msg in sub1:
            received1.append(msg)

    task1 = asyncio.create_task(consume1())

    # Callback subscriber
    callback2 = AsyncMock()
    bus.on(topic, callback2)

    await asyncio.sleep(0.01)
    delivered = await bus.publish(message)
    await asyncio.sleep(0.01)

    await bus.shutdown()
    await task1

    assert delivered == 2
    assert len(received1) == 1
    assert received1[0] == message
    callback2.assert_awaited_once_with(message)


async def test_multiple_queue_subscribers_same_topic(bus: SentinelBus):
    """Two queue subscribers on the same topic both get the message."""
    topic = "worker_event"
    message = _msg(topic, worker="w1")

    sub_a = bus.subscribe(topic)
    sub_b = bus.subscribe(topic)
    received_a, received_b = [], []

    async def consume(sub, dest):
        async for msg in sub:
            dest.append(msg)

    ta = asyncio.create_task(consume(sub_a, received_a))
    tb = asyncio.create_task(consume(sub_b, received_b))
    await asyncio.sleep(0.01)

    delivered = await bus.publish(message)
    await asyncio.sleep(0.01)

    await bus.shutdown()
    await ta
    await tb

    assert delivered == 2
    assert len(received_a) == 1
    assert len(received_b) == 1


async def test_multiple_callbacks_same_topic(bus: SentinelBus):
    """Multiple callbacks on one topic all fire."""
    topic = "state_change"
    cb1, cb2, cb3 = AsyncMock(), AsyncMock(), AsyncMock()
    bus.on(topic, cb1)
    bus.on(topic, cb2)
    bus.on(topic, cb3)

    message = _msg(topic)
    delivered = await bus.publish(message)

    assert delivered == 3
    cb1.assert_awaited_once_with(message)
    cb2.assert_awaited_once_with(message)
    cb3.assert_awaited_once_with(message)


# ── Wildcard subscription (no args = all topics) ───────────────


async def test_wildcard_subscribe_receives_all_topics(bus: SentinelBus):
    """subscribe() with no args subscribes to every valid topic."""
    sub = bus.subscribe()
    received = []

    async def consume():
        async for msg in sub:
            received.append(msg)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    # Publish to several different topics
    topics_to_test = [
        "dispatch_command",
        "worker_event",
        "resource_alert",
        "sentinel_heartbeat",
        "decision_made",
    ]
    for t in topics_to_test:
        await bus.publish(_msg(t, src=t))

    await asyncio.sleep(0.01)
    await bus.shutdown()
    await task

    assert len(received) == len(topics_to_test)
    received_topics = {m.topic for m in received}
    assert received_topics == set(topics_to_test)


async def test_wildcard_subscribe_registers_all_valid_topics(bus: SentinelBus):
    """subscribe() with no args registers queues for ALL valid topics."""
    sub = bus.subscribe()
    # The bus should have a queue entry for every valid topic
    for topic in VALID_TOPICS:
        assert topic in bus._queues
        assert len(bus._queues[topic]) == 1


# ── Multi-topic subscription ───────────────────────────────────


async def test_multi_topic_subscribe(bus: SentinelBus):
    """subscribe('a', 'b') receives messages from both topics."""
    sub = bus.subscribe("dispatch_command", "worker_event")
    received = []

    async def consume():
        async for msg in sub:
            received.append(msg)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    await bus.publish(_msg("dispatch_command", cmd="go"))
    await bus.publish(_msg("worker_event", evt="done"))
    # This one should NOT be received
    await bus.publish(_msg("decision_made", d="x"))
    await asyncio.sleep(0.01)

    await bus.shutdown()
    await task

    assert len(received) == 2
    assert {m.topic for m in received} == {"dispatch_command", "worker_event"}


async def test_multi_topic_subscribe_ignores_other_topics(bus: SentinelBus):
    """A multi-topic subscription does not register for unsubscribed topics."""
    sub = bus.subscribe("dispatch_command", "state_change")
    # Should not have a queue for topics not subscribed to
    assert "worker_event" not in bus._queues
    assert "decision_made" not in bus._queues
    sub.unsubscribe()


# ── Queue overflow ──────────────────────────────────────────────


async def test_queue_overflow_drops_message(small_bus: SentinelBus):
    """When queue is full, publish drops the message (no raise) and returns 0."""
    topic = "dispatch_command"
    sub = small_bus.subscribe(topic)

    # Fill the queue (max_queue_size=2) without consuming
    msg1 = _msg(topic, seq=1)
    msg2 = _msg(topic, seq=2)
    msg3 = _msg(topic, seq=3)

    d1 = await small_bus.publish(msg1)
    d2 = await small_bus.publish(msg2)
    assert d1 == 1
    assert d2 == 1

    # Third message should be dropped — queue is full
    d3 = await small_bus.publish(msg3)
    assert d3 == 0  # dropped, not delivered

    sub.unsubscribe()


async def test_queue_overflow_does_not_affect_callbacks(small_bus: SentinelBus):
    """Callbacks still fire even when queue subscribers overflow."""
    topic = "worker_event"
    sub = small_bus.subscribe(topic)
    cb = AsyncMock()
    small_bus.on(topic, cb)

    # Fill queue
    await small_bus.publish(_msg(topic, seq=1))
    await small_bus.publish(_msg(topic, seq=2))

    # Queue full, but callback should still work
    msg3 = _msg(topic, seq=3)
    delivered = await small_bus.publish(msg3)
    # 0 from queue (dropped) + 1 from callback
    assert delivered == 1
    assert cb.await_count == 3  # all 3 messages hit the callback


async def test_queue_overflow_resumes_after_drain(small_bus: SentinelBus):
    """After draining the queue, new messages are delivered again."""
    topic = "dispatch_advisory"
    sub = small_bus.subscribe(topic)

    # Fill the queue
    await small_bus.publish(_msg(topic, seq=1))
    await small_bus.publish(_msg(topic, seq=2))
    # Overflow
    await small_bus.publish(_msg(topic, seq=3))

    # Drain one
    msg = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert msg.payload["seq"] == 1

    # Now there's room — next publish should succeed
    d = await small_bus.publish(_msg(topic, seq=4))
    assert d == 1

    sub.unsubscribe()


# ── Callback error handling ─────────────────────────────────────


async def test_callback_exception_does_not_propagate(bus: SentinelBus):
    """A failing callback doesn't crash publish or block other callbacks."""
    topic = "decision_made"

    failing_cb = AsyncMock(side_effect=RuntimeError("boom"))
    healthy_cb = AsyncMock()

    bus.on(topic, failing_cb)
    bus.on(topic, healthy_cb)

    message = _msg(topic, test=True)
    delivered = await bus.publish(message)

    # Failing callback doesn't count as delivered
    # Healthy callback does count
    assert delivered == 1
    failing_cb.assert_awaited_once_with(message)
    healthy_cb.assert_awaited_once_with(message)


async def test_callback_exception_does_not_block_queue_subscribers(bus: SentinelBus):
    """Queue subscribers receive message even if a callback throws."""
    topic = "state_change"
    sub = bus.subscribe(topic)
    bad_cb = AsyncMock(side_effect=ValueError("kaboom"))
    bus.on(topic, bad_cb)

    received = []

    async def consume():
        async for msg in sub:
            received.append(msg)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    message = _msg(topic)
    delivered = await bus.publish(message)
    await asyncio.sleep(0.01)

    await bus.shutdown()
    await task

    # Queue got the message (1) + bad callback didn't count (0) = 1
    assert delivered == 1
    assert len(received) == 1


# ── Unsubscribe (queue-based) ──────────────────────────────────


async def test_unsubscribe_removes_queue(bus: SentinelBus):
    """After unsubscribe, the queue is removed from the bus."""
    topic = "dispatch_command"
    sub = bus.subscribe(topic)
    assert len(bus._queues[topic]) == 1

    sub.unsubscribe()
    # Queue list should be removed entirely when empty
    assert topic not in bus._queues


async def test_unsubscribe_stops_delivery(bus: SentinelBus):
    """After unsubscribe, publish returns 0 for that subscriber."""
    topic = "worker_event"
    sub = bus.subscribe(topic)

    d1 = await bus.publish(_msg(topic))
    assert d1 == 1

    sub.unsubscribe()
    d2 = await bus.publish(_msg(topic))
    assert d2 == 0


async def test_double_unsubscribe_is_safe(bus: SentinelBus):
    """Calling unsubscribe twice doesn't raise."""
    sub = bus.subscribe("decision_made")
    sub.unsubscribe()
    sub.unsubscribe()  # should not raise


# ── Shutdown ────────────────────────────────────────────────────


async def test_shutdown_terminates_async_iteration(bus: SentinelBus):
    """shutdown() causes async for loops to end."""
    sub = bus.subscribe("dispatch_command")
    received = []

    async def consume():
        async for msg in sub:
            received.append(msg)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)

    await bus.publish(_msg("dispatch_command"))
    await asyncio.sleep(0.01)

    await bus.shutdown()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(received) == 1


async def test_shutdown_clears_state(bus: SentinelBus):
    """After shutdown, queues and callbacks are cleared."""
    bus.subscribe("state_change")
    bus.on("worker_event", AsyncMock())

    await bus.shutdown()
    assert bus._queues == {}
    assert bus._callbacks == {}
    assert bus._running is False


async def test_shutdown_with_full_queue_does_not_raise(small_bus: SentinelBus):
    """Shutdown is safe even when subscriber queues are full."""
    sub = small_bus.subscribe("dispatch_advisory")
    await small_bus.publish(_msg("dispatch_advisory", seq=1))
    await small_bus.publish(_msg("dispatch_advisory", seq=2))
    # Queue is now full — shutdown should still succeed
    await small_bus.shutdown()


# ── No subscribers ──────────────────────────────────────────────


async def test_publish_with_no_subscribers(bus: SentinelBus):
    """Publishing to a valid topic with no subscribers returns 0."""
    delivered = await bus.publish(_msg("dispatch_command"))
    assert delivered == 0


# ── Topic isolation ─────────────────────────────────────────────


async def test_topic_isolation(bus: SentinelBus):
    """Subscribers only receive messages for their subscribed topic."""
    cb_cmd = AsyncMock()
    cb_evt = AsyncMock()
    bus.on("dispatch_command", cb_cmd)
    bus.on("worker_event", cb_evt)

    await bus.publish(_msg("dispatch_command", x=1))
    await bus.publish(_msg("worker_event", y=2))

    cb_cmd.assert_awaited_once()
    assert cb_cmd.call_args[0][0].topic == "dispatch_command"
    cb_evt.assert_awaited_once()
    assert cb_evt.call_args[0][0].topic == "worker_event"


# ── All valid topics are recognized ────────────────────────────


@pytest.mark.parametrize("topic", sorted(VALID_TOPICS))
async def test_all_valid_topics_publishable(bus: SentinelBus, topic: str):
    """Every topic in VALID_TOPICS can be published without error."""
    delivered = await bus.publish(_msg(topic))
    assert delivered == 0  # no subscribers, but no error


@pytest.mark.parametrize("topic", sorted(VALID_TOPICS))
async def test_all_valid_topics_subscribable(bus: SentinelBus, topic: str):
    """Every topic in VALID_TOPICS can be subscribed to without error."""
    sub = bus.subscribe(topic)
    assert topic in bus._queues
    sub.unsubscribe()
