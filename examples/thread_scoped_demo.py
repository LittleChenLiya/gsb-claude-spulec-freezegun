"""Two-thread demonstration of the explicit thread-scoped freeze mode.

Run it directly::

    python examples/thread_scoped_demo.py

The *frozen* thread enters ``freeze_time(..., thread_scoped=True)`` and sees
the fixed wall-clock date 2020-01-01 12:00:00, while the *real* thread (and
the main thread) keep observing the injected real clock.  The process-wide
freeze stack is never touched by the scoped freeze, so the two threads cannot
pollute each other.  Monotonic time (``time.monotonic`` /
``time.perf_counter``) is deliberately kept on the real, non-decreasing clock
for the registering thread even though its wall clock is frozen and is later
moved backwards.  The operating-system clock is never modified.

The accompanying pytest acceptance tests are run with::

    python -m pytest tests/test_thread_scoped.py -v
"""
import datetime
import os
import sys
import threading
import time

# Make the demo work when run directly (``python examples/...``): prefer the
# in-repository freezegun that provides the thread_scoped mode over any
# installed copy of the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import freezegun  # noqa: E402,F401
from freezegun import freeze_time  # noqa: E402

FROZEN_MOMENT = datetime.datetime(2020, 1, 1, 12, 0, 0)
OLDER_MOMENT = datetime.datetime(1980, 1, 1, 0, 0, 0)


def frozen_worker(ready: threading.Event, done: threading.Event) -> None:
    """Registering thread: its wall clock is frozen, monotonic is real."""
    with freeze_time(FROZEN_MOMENT, thread_scoped=True):
        monotonic_before = time.monotonic()
        time.sleep(0.02)
        monotonic_after = time.monotonic()
        ready.set()

        done.wait()
        print("[frozen-thread] wall clock while waiting:",
              datetime.datetime.now(),
              "| monotonic advanced during the freeze:",
              monotonic_after > monotonic_before)

        # Nested freeze owned by this thread: exiting it restores the outer
        # freeze (2020), even when the wall clock is moved back to 1980.
        with freeze_time(OLDER_MOMENT, thread_scoped=True):
            print("[frozen-thread] nested wall clock:", datetime.datetime.now())
            print("[frozen-thread] monotonic did not jump backwards:",
                  time.monotonic() >= monotonic_after)
        print("[frozen-thread] outer wall clock restored:",
              datetime.datetime.now())


def real_worker(ready: threading.Event, done: threading.Event) -> None:
    """Unknown thread: it never registered a freeze, so it sees real time."""
    ready.wait()
    print("[real-thread  ] wall clock:", datetime.datetime.now())
    time.sleep(0.02)
    print("[real-thread  ] wall clock a bit later:", datetime.datetime.now())
    done.set()


def main() -> None:
    ready = threading.Event()
    done = threading.Event()
    frozen_thread = threading.Thread(
        target=frozen_worker, args=(ready, done), name="frozen")
    real_thread = threading.Thread(
        target=real_worker, args=(ready, done), name="real")
    frozen_thread.start()
    real_thread.start()
    ready.wait()
    print("[main-thread  ] sees the real clock while the other is frozen:",
          datetime.datetime.now())
    real_thread.join()
    frozen_thread.join()
    print("[main-thread  ] after all scopes stopped:", datetime.datetime.now())


if __name__ == "__main__":
    main()
