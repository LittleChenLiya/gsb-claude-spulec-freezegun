"""
Tests for the explicit frozen-time subscription interface.

Acceptance covered here:

* ``now()`` three times emits no event; one ``tick()`` emits exactly one.
* A callback that re-enters ``now()`` does not deadlock.
* Nested freezes have independent subscription identities.
* An unsubscribe that wins before the callback starts suppresses the call.
* A failing callback gets an independent receipt and never rolls back the
  already advanced frozen time.
* Internal (frozen) and external (real) clocks do not get mixed up.
* A four-operation example: subscribe -> now (no event) -> tick (event) ->
  unsubscribe.
"""

from __future__ import annotations

import datetime
import threading
from typing import Any, Dict, List

import pytest

from freezegun import freeze_time
from freezegun.api import (
    FrozenDateTimeFactory,
    StepTickTimeFactory,
    TickingDateTimeFactory,
)
from freezegun.notifications import (
    NotificationReceipt,
    Subscription,
    TimeChangeEvent,
)


class Recorder:
    """Collects events and re-entrant now() calls."""

    def __init__(self) -> None:
        self.events: List[TimeChangeEvent] = []
        self.reentrant_nows: List[datetime.datetime] = []

    def __call__(self, event: TimeChangeEvent) -> None:
        self.events.append(event)
        self.reentrant_nows.append(datetime.datetime.now())


@pytest.mark.parametrize(
    "factory_kwargs,expected_old",
    [
        pytest.param({}, datetime.datetime(2020, 1, 1), id="FrozenDateTimeFactory"),
        pytest.param(
            {"auto_tick_seconds": 15},
            datetime.datetime(2020, 1, 1, 0, 0, 45),
            id="StepTickTimeFactory",
        ),
    ],
)
def test_now_three_times_no_event_tick_once_one_event(
    factory_kwargs: Dict[str, Any], expected_old: datetime.datetime
) -> None:
    with freeze_time("2020-01-01", **factory_kwargs) as frozen:
        recorder = Recorder()
        frozen.subscribe(recorder)

        for _ in range(3):
            datetime.datetime.now()
        assert recorder.events == []

        frozen.tick(10)
        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert event.operation == "tick"
        assert event.old_time == expected_old
        assert event.new_time == expected_old + datetime.timedelta(seconds=10)
        assert recorder.reentrant_nows == [expected_old + datetime.timedelta(seconds=10)]


def test_ticking_factory_now_does_not_notify() -> None:
    with freeze_time("2020-01-01", tick=True) as frozen:
        recorder = Recorder()
        frozen.subscribe(recorder)
        datetime.datetime.now()
        assert recorder.events == []
        frozen.tick(10)
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == "tick"


def test_move_to_notifies_move_to() -> None:
    with freeze_time("2020-01-01") as frozen:
        recorder = Recorder()
        frozen.subscribe(recorder)
        frozen.move_to("2020-02-01 03:04:05")
        assert len(recorder.events) == 1
        event = recorder.events[0]
        assert event.operation == "move_to"
        assert event.old_time == datetime.datetime(2020, 1, 1)
        assert event.new_time == datetime.datetime(2020, 2, 1, 3, 4, 5)


def test_callback_reentrant_now_no_deadlock() -> None:
    with freeze_time("2020-01-01") as frozen:
        seen: List[Any] = []

        def callback(event: TimeChangeEvent) -> None:
            seen.append(datetime.datetime.now())
            seen.append(datetime.datetime.utcnow())
            seen.append(datetime.date.today())

        frozen.subscribe(callback)
        frozen.tick(5)
        assert seen[0] == datetime.datetime(2020, 1, 1, 0, 0, 5)
        assert seen[1] == datetime.datetime(2020, 1, 1, 0, 0, 5)
        assert seen[2] == datetime.date(2020, 1, 1)


def test_nested_freezes_independent_identities() -> None:
    with freeze_time("2020-01-01") as outer:
        outer_events: List[TimeChangeEvent] = []
        outer.subscribe(lambda ev: outer_events.append(ev))

        with freeze_time("2021-01-01") as inner:
            inner_events: List[TimeChangeEvent] = []
            inner.subscribe(lambda ev: inner_events.append(ev))
            inner.tick(5)
            assert len(inner_events) == 1
            assert len(outer_events) == 0
            assert inner_events[0].old_time == datetime.datetime(2021, 1, 1)

        outer.tick(3)
        assert len(outer_events) == 1
        assert len(inner_events) == 1
        assert outer_events[0].old_time == datetime.datetime(2020, 1, 1)


def test_unsubscribe_before_start_suppresses_call() -> None:
    with freeze_time("2020-01-01") as frozen:
        calls: List[TimeChangeEvent] = []
        subscription = frozen.subscribe(lambda ev: calls.append(ev))
        assert subscription.active is True
        assert subscription.unsubscribe() is True
        assert subscription.active is False
        assert subscription.unsubscribe() is False

        frozen.tick(10)
        assert calls == []


def test_concurrent_unsubscribe_does_not_deadlock() -> None:
    with freeze_time("2020-01-01") as frozen:
        started = threading.Event()
        proceed = threading.Event()
        calls: List[TimeChangeEvent] = []

        def slow_callback(event: TimeChangeEvent) -> None:
            started.set()
            proceed.wait(timeout=5)
            calls.append(event)

        subscription = frozen.subscribe(slow_callback)
        worker = threading.Thread(target=lambda: frozen.tick(10))
        worker.start()
        assert started.wait(timeout=5)
        assert subscription.unsubscribe() is True
        proceed.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert subscription.active is False


def test_callback_failure_has_independent_receipt_and_no_rollback() -> None:
    with freeze_time("2020-01-01") as frozen:
        good_calls: List[TimeChangeEvent] = []

        def bad_callback(event: TimeChangeEvent) -> None:
            raise RuntimeError("boom")

        good_sub = frozen.subscribe(lambda ev: good_calls.append(ev))
        bad_sub = frozen.subscribe(bad_callback)

        frozen.tick(10)

        assert datetime.datetime.now() == datetime.datetime(2020, 1, 1, 0, 0, 10)

        assert isinstance(bad_sub.last_error, RuntimeError)
        assert str(bad_sub.last_error) == "boom"
        assert good_sub.last_error is None
        assert len(good_calls) == 1

        frozen.tick(1)
        assert isinstance(bad_sub.last_error, RuntimeError)
        assert datetime.datetime.now() == datetime.datetime(2020, 1, 1, 0, 0, 11)


def test_notify_returns_receipts_per_subscriber() -> None:
    with freeze_time("2020-01-01") as frozen:
        ok_sub = frozen.subscribe(lambda ev: None)
        fail_sub = frozen.subscribe(lambda ev: (_ for _ in ()).throw(RuntimeError("x")))
        event = TimeChangeEvent(
            datetime.datetime(2020, 1, 1),
            datetime.datetime(2020, 1, 1, 0, 0, 10),
            "tick",
        )
        receipts = frozen._subscriptions.notify(event)
        assert len(receipts) == 2
        by_id = {r.subscription_id: r for r in receipts}
        assert by_id[ok_sub.id].success is True
        assert by_id[fail_sub.id].success is False
        assert isinstance(by_id[fail_sub.id].error, RuntimeError)


def test_event_times_are_frozen_not_real() -> None:
    with freeze_time("2020-01-01") as frozen:
        captured: Dict[str, Any] = {}

        def callback(event: TimeChangeEvent) -> None:
            captured["old"] = event.old_time
            captured["new"] = event.new_time
            captured["operation"] = event.operation
            captured["now"] = datetime.datetime.now()
            captured["today"] = datetime.date.today()

        frozen.subscribe(callback)
        frozen.tick(10)
        assert captured["old"] == datetime.datetime(2020, 1, 1)
        assert captured["new"] == datetime.datetime(2020, 1, 1, 0, 0, 10)
        assert captured["operation"] == "tick"
        assert captured["now"] == datetime.datetime(2020, 1, 1, 0, 0, 10)
        assert captured["today"] == datetime.date(2020, 1, 1)
    # After stop(), real time is restored and is not the frozen instant.
    assert datetime.datetime.now() != datetime.datetime(2020, 1, 1, 0, 0, 10)


def test_four_operation_example() -> None:
    """End-to-end example of the four core operations.

    1. subscribe   -> register a callback
    2. now()       -> read the clock, no event emitted
    3. tick()      -> advance the clock, one event emitted
    4. unsubscribe -> stop receiving events
    """
    with freeze_time("2020-01-01") as frozen:
        log: List[str] = []

        def on_change(event: TimeChangeEvent) -> None:
            log.append(
                "%s: %s -> %s" % (event.operation, event.old_time, event.new_time)
            )

        subscription = frozen.subscribe(on_change)
        assert subscription.active

        datetime.datetime.now()
        datetime.date.today()
        assert log == []

        frozen.tick(datetime.timedelta(seconds=30))
        assert len(log) == 1
        assert log[0].startswith("tick:")

        subscription.unsubscribe()
        assert not subscription.active
        frozen.tick(datetime.timedelta(seconds=30))
        assert len(log) == 1


def test_subscription_ids_are_unique() -> None:
    with freeze_time("2020-01-01") as frozen:
        s1 = frozen.subscribe(lambda ev: None)
        s2 = frozen.subscribe(lambda ev: None)
        s3 = frozen.subscribe(lambda ev: None)
        assert s1.id != s2.id
        assert s2.id != s3.id
        assert s1.active and s2.active and s3.active


def test_subscribe_requires_callable() -> None:
    with freeze_time("2020-01-01") as frozen:
        with pytest.raises(TypeError):
            frozen.subscribe("not callable")  # type: ignore[arg-type]


def test_time_change_event_is_frozen_dataclass() -> None:
    event = TimeChangeEvent(
        datetime.datetime(2020, 1, 1),
        datetime.datetime(2020, 1, 2),
        "move_to",
    )
    assert event.old_time == datetime.datetime(2020, 1, 1)
    assert event.new_time == datetime.datetime(2020, 1, 2)
    assert event.operation == "move_to"
    with pytest.raises(Exception):
        event.operation = "tick"  # type: ignore[misc]
