import datetime
import threading
import time

import pytest

from freezegun import freeze_time
from freezegun import api

FROZEN = "2012-01-14 03:21:34"
FROZEN_DT = datetime.datetime(2012, 1, 14, 3, 21, 34)


def test_thread_scoped_freeze_only_affects_own_thread():
    """A thread-scoped freeze freezes the registering thread only; other
    threads keep reading the real (injected) clock."""
    started = threading.Event()
    done = threading.Event()
    other_thread_observations = []

    def worker():
        assert started.wait(5)
        other_thread_observations.append(time.time())
        other_thread_observations.append(datetime.datetime.now())
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    with freeze_time(FROZEN, thread_scoped=True):
        # This (registered) thread is frozen.
        assert datetime.datetime.now() == FROZEN_DT
        started.set()
        assert done.wait(5)
        # Still frozen here while the other thread ran.
        assert datetime.datetime.now() == FROZEN_DT
    t.join()

    other_time, other_now = other_thread_observations
    # The other thread saw the real clock, not the frozen one.
    assert abs(other_time - api.real_time()) < 60
    assert other_now.year != 2012
    assert abs((other_now - api.real_datetime.now()).total_seconds()) < 60


def test_thread_scoped_unknown_thread_uses_real_clock():
    """Threads that never registered a freeze use the original clock even
    while another thread is frozen."""
    frozen_entered = threading.Event()
    release = threading.Event()
    observations = {}

    def frozen_worker():
        with freeze_time(FROZEN, thread_scoped=True):
            frozen_entered.set()
            assert release.wait(5)
            observations["frozen_now"] = datetime.datetime.now()

    def plain_worker():
        assert frozen_entered.wait(5)
        observations["plain_now"] = datetime.datetime.now()
        observations["plain_time"] = time.time()
        release.set()

    t_frozen = threading.Thread(target=frozen_worker)
    t_plain = threading.Thread(target=plain_worker)
    t_frozen.start()
    t_plain.start()
    t_frozen.join()
    t_plain.join()

    assert observations["frozen_now"] == FROZEN_DT
    assert observations["plain_now"].year != 2012
    assert abs(observations["plain_time"] - api.real_time()) < 60


def test_thread_scoped_nested_freezes_restore_by_owner():
    """Nested thread-scoped freezes restore the outer freeze on exit."""
    with freeze_time("2012-01-14", thread_scoped=True):
        assert datetime.datetime.now() == datetime.datetime(2012, 1, 14)
        with freeze_time("2015-06-30", thread_scoped=True):
            assert datetime.datetime.now() == datetime.datetime(2015, 6, 30)
            with freeze_time("2020-12-31", thread_scoped=True):
                assert datetime.datetime.now() == datetime.datetime(2020, 12, 31)
            # Innermost exited -> middle freeze is active again.
            assert datetime.datetime.now() == datetime.datetime(2015, 6, 30)
        # Middle exited -> outer freeze is active again.
        assert datetime.datetime.now() == datetime.datetime(2012, 1, 14)
    # Everything restored: the real clock is back and patches are gone.
    assert time.time is api.real_time
    assert not api._thread_freeze_stacks


def test_thread_scoped_freeze_restores_on_exception():
    """An exception inside the with-block still restores the real clock."""
    with pytest.raises(RuntimeError):
        with freeze_time(FROZEN, thread_scoped=True):
            assert datetime.datetime.now() == FROZEN_DT
            raise RuntimeError("boom")

    assert time.time is api.real_time
    assert time.monotonic is api.real_monotonic
    assert datetime.datetime is api.real_datetime
    assert not api._thread_freeze_stacks
    assert api._thread_scoped_count == 0


def test_thread_scoped_monotonic_does_not_follow_wall_clock_backwards():
    """Monotonic timers have their own policy and never move backwards, even
    when the frozen wall clock is moved backwards."""
    with freeze_time(FROZEN, thread_scoped=True) as frozen:
        wall_before = time.time()
        mono_before = time.monotonic()
        perf_before = time.perf_counter()
        mono_ns_before = time.monotonic_ns()

        # Move the frozen wall clock backwards.
        frozen.move_to("2000-01-01 00:00:00")
        wall_after = time.time()
        assert wall_after < wall_before

        # Monotonic timers must not go backwards with the wall clock.
        assert time.monotonic() >= mono_before
        assert time.perf_counter() >= perf_before
        assert time.monotonic_ns() >= mono_ns_before
        assert time.perf_counter_ns() >= perf_before * 1e9 - 1e6

    # After the freeze, the real monotonic clock is restored.
    assert time.monotonic is api.real_monotonic


def test_thread_scoped_monotonic_keeps_ticking():
    """The thread-scoped monotonic policy advances with the real monotonic
    clock instead of being pinned to the frozen wall clock."""
    with freeze_time(FROZEN, thread_scoped=True):
        m1 = time.monotonic()
        api.real_monotonic()  # real clock keeps running underneath
        end = api.real_monotonic() + 0.05
        while api.real_monotonic() < end:
            pass
        m2 = time.monotonic()
        assert m2 > m1
        # Wall clock stayed frozen the whole time.
        assert datetime.datetime.now() == FROZEN_DT


def test_thread_scoped_two_threads_freeze_independently():
    """Two threads can each hold their own thread-scoped freeze without
    polluting each other."""
    both_frozen = threading.Barrier(3)
    release = threading.Barrier(3)
    observations = {}

    def frozen_worker(name, when):
        with freeze_time(when, thread_scoped=True):
            both_frozen.wait(5)
            release.wait(5)
            observations[name] = datetime.datetime.now()

    t1 = threading.Thread(target=frozen_worker, args=("t1", "2012-01-14"))
    t2 = threading.Thread(target=frozen_worker, args=("t2", "2015-06-30"))
    t1.start()
    t2.start()
    both_frozen.wait(5)
    # Main thread is not registered: it sees the real clock.
    assert datetime.datetime.now().year not in (2012, 2015)
    release.wait(5)
    t1.join()
    t2.join()

    assert observations["t1"] == datetime.datetime(2012, 1, 14)
    assert observations["t2"] == datetime.datetime(2015, 6, 30)
    assert time.time is api.real_time


def test_default_mode_remains_global():
    """The default (non thread-scoped) freeze mode is unchanged: it freezes
    time for every thread."""
    started = threading.Event()
    done = threading.Event()
    observations = []

    def worker():
        assert started.wait(5)
        observations.append(datetime.datetime.now())
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    with freeze_time(FROZEN):
        started.set()
        assert done.wait(5)
    t.join()
    assert observations == [FROZEN_DT]
    assert time.time is api.real_time


def test_thread_scoped_as_decorator():
    @freeze_time(FROZEN, thread_scoped=True)
    def frozen_func():
        return datetime.datetime.now()

    assert frozen_func() == FROZEN_DT
    assert time.time is api.real_time


def test_thread_scoped_concurrent_start_stop_restores_patches():
    """Many threads starting/stopping thread-scoped freezes concurrently must
    not race the global patch install/uninstall; afterwards everything is
    restored."""
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait(5)
        for _ in range(5):
            with freeze_time("2012-01-14", thread_scoped=True):
                assert datetime.datetime.now() == datetime.datetime(2012, 1, 14)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert time.time is api.real_time
    assert time.monotonic is api.real_monotonic
    assert datetime.datetime is api.real_datetime
    assert not api._thread_freeze_stacks
    assert api._thread_scoped_count == 0
