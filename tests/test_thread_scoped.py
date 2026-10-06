"""Acceptance tests for the explicit thread-scoped freeze mode.

Run only these tests with::

    python -m pytest tests/test_thread_scoped.py -v

The two-thread demonstration is in ``examples/thread_scoped_demo.py``::

    python examples/thread_scoped_demo.py

Covered acceptance requirements:

* one thread is frozen while another thread keeps observing the injected real
  clock (no temporary push onto the global freeze stack);
* nested thread-scoped freezes restore the outer freeze on exit;
* an exceptional exit restores the real clock as well;
* ``time.monotonic()`` (and ``time.perf_counter()``) never jump backwards even
  when the frozen wall clock is moved backwards;
* an unknown (non-registering) thread always sees the original clock;
* the system clock itself is never modified.
"""
import datetime
import threading
import time

import pytest

from freezegun import freeze_time
import freezegun.api as api

FROZEN = datetime.datetime(2020, 1, 1, 12, 0, 0)
INNER = datetime.datetime(2021, 6, 6, 6, 6, 6)
FROZEN_EPOCH = 1577880000.0  # 2020-01-01 12:00:00 UTC


def _assert_clock_fully_restored() -> None:
    assert api.freeze_factories == []
    assert api._thread_scopes == {}
    assert api._thread_scopes_count == 0
    assert not api._patch_coordinator.installed
    assert time.time is api.real_time
    assert time.monotonic is api.real_monotonic
    assert datetime.datetime is api.real_datetime
    assert datetime.date is api.real_date


def test_scoped_freeze_only_affects_registering_thread_main() -> None:
    """The registering thread is frozen; no global stack entry is created."""
    assert api.freeze_factories == []
    with freeze_time(FROZEN, thread_scoped=True):
        assert api.freeze_factories == []
        assert datetime.datetime.now() == FROZEN
        assert time.time() == pytest.approx(FROZEN_EPOCH, abs=1)
    assert datetime.datetime.now().year >= 2025
    _assert_clock_fully_restored()


def test_two_threads_frozen_and_real() -> None:
    """One thread is frozen while another thread observes the real clock."""
    results = {}
    frozen_ready = threading.Event()
    release_frozen = threading.Event()
    real_start = threading.Event()

    def frozen_worker() -> None:
        with freeze_time(FROZEN, thread_scoped=True):
            results["frozen_dt"] = datetime.datetime.now()
            results["frozen_time"] = time.time()
            frozen_ready.set()
            # Stay inside the freeze while the "real" thread takes its reads.
            release_frozen.wait(timeout=5)
            results["frozen_dt_after"] = datetime.datetime.now()

    def real_worker() -> None:
        real_start.wait(timeout=5)
        results["real_dt"] = datetime.datetime.now()
        results["real_time"] = time.time()
        release_frozen.set()

    frozen_thread = threading.Thread(target=frozen_worker, name="frozen")
    real_thread = threading.Thread(target=real_worker, name="real")
    frozen_thread.start()
    real_thread.start()
    assert frozen_ready.wait(timeout=5)

    # The main thread is an unknown thread: it keeps the real clock even
    # though the frozen thread is currently inside its freeze.
    main_dt = datetime.datetime.now()
    assert main_dt.year >= 2025

    real_start.set()
    frozen_thread.join(timeout=5)
    real_thread.join(timeout=5)

    assert results["frozen_dt"] == FROZEN
    assert results["frozen_dt_after"] == FROZEN
    assert results["frozen_time"] == pytest.approx(FROZEN_EPOCH, abs=1)
    assert results["real_dt"].year >= 2025
    assert results["real_time"] > FROZEN_EPOCH + 365 * 24 * 3600
    _assert_clock_fully_restored()


def test_nested_scoped_freeze_restores_outer() -> None:
    """Exiting an inner freeze returns the registering thread to the outer."""
    with freeze_time(FROZEN, thread_scoped=True):
        assert datetime.datetime.now() == FROZEN
        with freeze_time(INNER, thread_scoped=True):
            assert datetime.datetime.now() == INNER
        # Back to the outer freeze owned by this same thread.
        assert datetime.datetime.now() == FROZEN
        # A second nested freeze on another value works independently.
        with freeze_time(datetime.datetime(1999, 1, 1), thread_scoped=True):
            assert datetime.datetime.now() == datetime.datetime(1999, 1, 1)
        assert datetime.datetime.now() == FROZEN
    assert datetime.datetime.now().year >= 2025
    _assert_clock_fully_restored()


def test_nested_in_two_threads_are_independent() -> None:
    """Nested freezes of two owners never share a stack."""
    results = {}
    first_ready = threading.Event()
    go = threading.Event()

    def worker() -> None:
        with freeze_time(FROZEN, thread_scoped=True):
            with freeze_time(INNER, thread_scoped=True):
                first_ready.set()
                go.wait(timeout=5)
                results["thread_inner"] = datetime.datetime.now()
            results["thread_outer"] = datetime.datetime.now()

    thread = threading.Thread(target=worker)
    thread.start()
    assert first_ready.wait(timeout=5)
    with freeze_time(datetime.datetime(1999, 1, 1), thread_scoped=True):
        results["main"] = datetime.datetime.now()
        go.set()
        thread.join(timeout=5)
    assert results["main"] == datetime.datetime(1999, 1, 1)
    assert results["thread_inner"] == INNER
    assert results["thread_outer"] == FROZEN
    _assert_clock_fully_restored()


def test_exception_exit_restores_real_clock() -> None:
    """Exception propagation out of the scope restores the real clock."""
    before = datetime.datetime.now()
    with pytest.raises(RuntimeError):
        with freeze_time(FROZEN, thread_scoped=True):
            assert datetime.datetime.now() == FROZEN
            raise RuntimeError("boom")
    after = datetime.datetime.now()
    assert after >= before
    assert after.year >= 2025
    _assert_clock_fully_restored()


def test_exception_in_nested_freeze_restores_outer() -> None:
    """An exception leaving the inner scope returns the thread to the outer."""
    with freeze_time(FROZEN, thread_scoped=True):
        with pytest.raises(ValueError):
            with freeze_time(INNER, thread_scoped=True):
                assert datetime.datetime.now() == INNER
                raise ValueError("inner boom")
        assert datetime.datetime.now() == FROZEN
    _assert_clock_fully_restored()


def test_monotonic_not_affected_by_frozen_wall_clock() -> None:
    """Monotonic policy is separate: real and non-decreasing under freeze."""
    with freeze_time(FROZEN, thread_scoped=True):
        m1 = time.monotonic()
        p1 = time.perf_counter()
        time.sleep(0.05)
        m2 = time.monotonic()
        p2 = time.perf_counter()
        assert m2 > m1
        assert p2 > p1
        # Wall time stays frozen while monotonic advances.
        assert datetime.datetime.now() == FROZEN

        # Move the frozen wall clock backwards; monotonic must not go back.
        inner = freeze_time(datetime.datetime(1980, 1, 1), thread_scoped=True)
        inner.start()
        try:
            assert datetime.datetime.now() == datetime.datetime(1980, 1, 1)
            m3 = time.monotonic()
            assert m3 >= m2
        finally:
            inner.stop()

        assert time.monotonic() >= m3
        assert datetime.datetime.now() == FROZEN
    _assert_clock_fully_restored()


def test_unknown_thread_uses_original_clock() -> None:
    """A thread that never registered a freeze always sees the real clock."""
    seen = {}
    ready = threading.Event()

    def worker() -> None:
        ready.wait(timeout=5)
        seen["dt"] = datetime.datetime.now()
        seen["time"] = time.time()

    thread = threading.Thread(target=worker)
    thread.start()
    with freeze_time(datetime.datetime(1980, 1, 1), thread_scoped=True):
        ready.set()
        thread.join(timeout=5)
    assert seen["dt"].year >= 2025
    assert seen["time"] > FROZEN_EPOCH
    _assert_clock_fully_restored()


def test_worker_thread_time_time_is_frozen_with_default_config() -> None:
    """The 'threading' default-ignore entry must not leak real time into the
    registering worker thread's own calls."""
    seen = {}

    def worker() -> None:
        with freeze_time(FROZEN, thread_scoped=True):
            seen["dt"] = datetime.datetime.now()
            seen["epoch"] = time.time()
            seen["gmtime"] = time.gmtime()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert seen["dt"] == FROZEN
    assert seen["epoch"] == pytest.approx(FROZEN_EPOCH, abs=1)
    assert time.struct_time(seen["gmtime"]).tm_year == 2020
    _assert_clock_fully_restored()


def test_dead_registering_thread_scope_is_pruned() -> None:
    """A freeze left unstopped by a dead thread cannot keep patches active."""
    entered = threading.Event()

    def worker() -> None:
        freeze_time(FROZEN, thread_scoped=True).start()
        assert datetime.datetime.now() == FROZEN
        entered.set()
        # Deliberately do NOT call stop(); the thread terminates here.

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(timeout=5)
    thread.join(timeout=5)
    assert not thread.is_alive()

    # Other threads still see the real clock meanwhile.
    assert datetime.datetime.now().year >= 2025

    # A later freeze anywhere prunes the dead owner's stale scope.
    with freeze_time(FROZEN, thread_scoped=True):
        assert datetime.datetime.now() == FROZEN
    _assert_clock_fully_restored()


def test_default_freezegun_mode_is_unchanged() -> None:
    """Without thread_scoped the historical process-wide behavior remains."""
    seen = {}
    ready = threading.Event()

    def worker() -> None:
        # While the main thread holds a process-wide freeze another thread
        # also sees the frozen wall time.
        seen["dt"] = datetime.datetime.now()
        ready.set()

    thread = threading.Thread(target=worker)
    with freeze_time(FROZEN):
        assert api.freeze_factories
        assert datetime.datetime.now() == FROZEN
        thread.start()
        assert ready.wait(timeout=5)
        thread.join(timeout=5)
    assert seen["dt"] == FROZEN
    assert datetime.datetime.now().year >= 2025
    _assert_clock_fully_restored()
