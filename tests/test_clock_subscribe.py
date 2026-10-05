"""
Tests for the explicit frozen-clock subscription interface.

Local, focused verification command:

    python -m pytest tests/test_clock_subscribe.py -v

Four basic operations, end to end (subscribe / tick / move_to / unsubscribe):

    import datetime
    from freezegun import freeze_time

    with freeze_time("2020-01-01 00:00:00") as clock:
        events = []
        token = clock.subscribe(events.append)   # 1. subscribe
        clock.tick(datetime.timedelta(seconds=5))  # 2. tick -> one tick event
        clock.move_to("2020-01-02 00:00:00")     # 3. move_to -> one move_to event
        clock.unsubscribe(token)                 # 4. unsubscribe -> no more events
        clock.tick()                             # not delivered
        assert [e.operation for e in events] == ["tick", "move_to"]
"""
import datetime
import threading

import pytest

from freezegun import freeze_time
from freezegun.api import CallbackFailure, ClockEvent
from tests import utils


def test_now_calls_emit_no_events() -> None:
    with freeze_time("2020-01-01 00:00:00") as clock:
        events = []
        clock.subscribe(events.append)

        datetime.datetime.now()
        datetime.datetime.now()
        datetime.datetime.now()

        assert events == []


def test_tick_emits_exactly_one_event_with_old_new_and_operation() -> None:
    with freeze_time("2020-01-01 00:00:00") as clock:
        events = []
        clock.subscribe(events.append)

        clock.tick(datetime.timedelta(seconds=5))

        assert len(events) == 1
        event = events[0]
        assert isinstance(event, ClockEvent)
        assert event.clock_id == clock.clock_id
        assert event.operation == "tick"
        assert event.old == datetime.datetime(2020, 1, 1, 0, 0, 0)
        assert event.new == datetime.datetime(2020, 1, 1, 0, 0, 5)


def test_move_to_emits_move_to_event() -> None:
    with freeze_time("2020-01-01 00:00:00") as clock:
        events = []
        clock.subscribe(events.append)

        clock.move_to("2021-06-15 12:30:00")

        assert len(events) == 1
        assert events[0].operation == "move_to"
        assert events[0].old == datetime.datetime(2020, 1, 1, 0, 0, 0)
        assert events[0].new == datetime.datetime(2021, 6, 15, 12, 30, 0)


def test_no_event_when_time_does_not_actually_change() -> None:
    with freeze_time("2020-01-01 00:00:00") as clock:
        events = []
        clock.subscribe(events.append)

        clock.tick(0)
        clock.move_to("2020-01-01 00:00:00")

        assert events == []


def test_callback_may_reenter_now_without_deadlock() -> None:
    with freeze_time("2020-01-01 00:00:00") as clock:
        seen_inside_callback = []

        def callback(event: ClockEvent) -> None:
            # Re-entering now() from inside the callback must not deadlock,
            # because callbacks run outside the internal clock lock.
            seen_inside_callback.append(datetime.datetime.now())

        clock.subscribe(callback)

        worker = threading.Thread(target=clock.tick)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), "callback re-entering now() deadlocked"

        assert seen_inside_callback == [datetime.datetime(2020, 1, 1, 0, 0, 1)]


def test_nested_freezes_have_independent_identities_and_do_not_cross() -> None:
    with freeze_time("2020-01-01 00:00:00") as outer:
        outer_events = []
        outer.subscribe(outer_events.append)

        with freeze_time("2030-06-15 12:00:00") as inner:
            inner_events = []
            inner.subscribe(inner_events.append)

            assert inner.clock_id != outer.clock_id

            inner.tick(datetime.timedelta(seconds=7))

            # The inner clock notified only its own subscribers.
            assert len(inner_events) == 1
            assert inner_events[0].clock_id == inner.clock_id
            assert inner_events[0].new == datetime.datetime(2030, 6, 15, 12, 0, 7)
            assert outer_events == []

            # The inner tick did not leak into the outer clock.
            assert datetime.datetime.now() == datetime.datetime(2030, 6, 15, 12, 0, 7)

        # The outer clock is untouched by everything the inner clock did.
        assert datetime.datetime.now() == datetime.datetime(2020, 1, 1, 0, 0, 0)

        outer.tick()
        assert len(outer_events) == 1
        assert outer_events[0].clock_id == outer.clock_id
        assert outer_events[0].new == datetime.datetime(2020, 1, 1, 0, 0, 1)


def test_unsubscribe_before_operation_means_callback_is_not_called() -> None:
    with freeze_time("2020-01-01 00:00:00") as clock:
        events = []
        token = clock.subscribe(events.append)

        assert clock.unsubscribe(token) is True

        clock.tick()
        clock.move_to("2020-01-02 00:00:00")

        assert events == []
        # Unsubscribing an already-removed token reports False.
        assert clock.unsubscribe(token) is False


def test_callback_failure_has_independent_receipt_and_time_is_not_rolled_back() -> None:
    with freeze_time("2020-01-01 00:00:00") as clock:
        delivered = []

        def bad_value(event: ClockEvent) -> None:
            raise ValueError("boom-1")

        def bad_runtime(event: ClockEvent) -> None:
            raise RuntimeError("boom-2")

        clock.subscribe(bad_value)
        clock.subscribe(delivered.append)
        clock.subscribe(bad_runtime)

        result = clock.tick(datetime.timedelta(seconds=3))

        # The advanced time was not rolled back by the failing callbacks.
        assert result == datetime.datetime(2020, 1, 1, 0, 0, 3)
        assert datetime.datetime.now() == datetime.datetime(2020, 1, 1, 0, 0, 3)

        # The healthy subscriber still received the event.
        assert len(delivered) == 1

        # Each failure got its own independent receipt.
        assert len(clock.failures) == 2
        first, second = clock.failures
        assert isinstance(first, CallbackFailure)
        assert first.clock_id == clock.clock_id
        assert first.callback is bad_value
        assert isinstance(first.exception, ValueError)
        assert second.callback is bad_runtime
        assert isinstance(second.exception, RuntimeError)
        assert first.event == second.event == delivered[0]

        # A later failure adds another independent receipt.
        clock.tick()
        assert len(clock.failures) == 4
        assert clock.failures[2].event != clock.failures[0].event


def test_tick_return_value_and_behavior_unchanged() -> None:
    with freeze_time("2020-01-01 00:00:00") as clock:
        assert clock.tick() == datetime.datetime(2020, 1, 1, 0, 0, 1)
        assert clock.tick(2) == datetime.datetime(2020, 1, 1, 0, 0, 3)
        assert clock.tick(1.5) == datetime.datetime(2020, 1, 1, 0, 0, 4, 500000)
        assert clock.tick(datetime.timedelta(days=1)) == datetime.datetime(2020, 1, 2, 0, 0, 4, 500000)
        assert clock.move_to("2020-02-03 04:05:06") is None
        assert datetime.datetime.now() == datetime.datetime(2020, 2, 3, 4, 5, 6)


@utils.cpython_only
def test_ticking_factory_emits_tick_and_move_to_events() -> None:
    with freeze_time("2020-01-01 00:00:00", tick=True) as clock:
        events = []
        clock.subscribe(events.append)

        clock.tick(5)
        clock.move_to("2020-01-02 00:00:00")

        assert [event.operation for event in events] == ["tick", "move_to"]
        assert events[0].old == datetime.datetime(2020, 1, 1, 0, 0, 0)
        assert events[0].new == datetime.datetime(2020, 1, 1, 0, 0, 5)
        assert events[1].new == datetime.datetime(2020, 1, 2, 0, 0, 0)


def test_auto_tick_factory_now_reads_do_not_notify_but_explicit_ops_do() -> None:
    with freeze_time("2020-01-01 00:00:00", auto_tick_seconds=10) as clock:
        events = []
        clock.subscribe(events.append)

        # The implicit per-read step of auto_tick must not notify.
        datetime.datetime.now()
        datetime.datetime.now()
        datetime.datetime.now()
        assert events == []

        clock.tick()
        assert len(events) == 1
        assert events[0].operation == "tick"
        assert events[0].new == datetime.datetime(2020, 1, 1, 0, 0, 40)

        clock.move_to("2020-01-02 00:00:00")
        assert [event.operation for event in events] == ["tick", "move_to"]


def test_four_operations_example() -> None:
    """subscribe -> tick -> move_to -> unsubscribe, as in the module docstring."""
    with freeze_time("2020-01-01 00:00:00") as clock:
        events = []
        token = clock.subscribe(events.append)         # 1. subscribe
        clock.tick(datetime.timedelta(seconds=5))      # 2. tick
        clock.move_to("2020-01-02 00:00:00")           # 3. move_to
        assert clock.unsubscribe(token) is True        # 4. unsubscribe
        clock.tick()

        assert [event.operation for event in events] == ["tick", "move_to"]
        assert events[0].old == datetime.datetime(2020, 1, 1, 0, 0, 0)
        assert events[0].new == datetime.datetime(2020, 1, 1, 0, 0, 5)
        assert events[1].old == datetime.datetime(2020, 1, 1, 0, 0, 5)
        assert events[1].new == datetime.datetime(2020, 1, 2, 0, 0, 0)
