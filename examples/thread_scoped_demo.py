"""Two-thread demo of freezegun's thread-scoped freeze mode.

A "frozen" thread pins its wall clock at 2012-01-14 while a "real" thread
running concurrently keeps reading the real (injected) clock.  Nested
freezes restore by owner, exceptions restore the real clock, and monotonic
timers never follow the wall clock backwards.  The system clock is never
modified -- only Python-level references are patched.

Run with:  python examples/thread_scoped_demo.py
Run tests: python -m pytest tests/test_thread_scoped.py -v
"""

import datetime
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from freezegun import freeze_time

FROZEN = "2012-01-14 03:21:34"


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def main():
    frozen_entered = threading.Event()
    release = threading.Event()

    def frozen_worker():
        print("[frozen] entering thread-scoped freeze at", FROZEN)
        with freeze_time(FROZEN, thread_scoped=True) as frozen:
            frozen_entered.set()
            release.wait(5)
            print("[frozen] wall clock inside freeze :", fmt(datetime.datetime.now()))
            mono_before = time.monotonic()
            frozen.move_to("2000-01-01 00:00:00")  # wall clock jumps backwards
            time.sleep(0.05)  # real sleep; monotonic keeps ticking
            mono_after = time.monotonic()
            print("[frozen] wall clock after move_to :", fmt(datetime.datetime.now()))
            print("[frozen] monotonic did NOT go backwards:",
                  f"{mono_before:.3f} -> {mono_after:.3f}")
            with freeze_time("2015-06-30 12:00:00", thread_scoped=True):
                print("[frozen] nested freeze          :", fmt(datetime.datetime.now()))
            print("[frozen] nested exit, outer again :", fmt(datetime.datetime.now()))
        print("[frozen] after exit, real clock     :", fmt(datetime.datetime.now()))

    def real_worker():
        frozen_entered.wait(5)
        for _ in range(3):
            print("[real  ] real clock while other thread frozen:",
                  fmt(datetime.datetime.now()))
            time.sleep(0.05)
        release.set()

    t_frozen = threading.Thread(target=frozen_worker, name="frozen")
    t_real = threading.Thread(target=real_worker, name="real")
    t_frozen.start()
    t_real.start()
    t_frozen.join()
    t_real.join()

    # Exceptions also restore the real clock.
    try:
        with freeze_time(FROZEN, thread_scoped=True):
            raise RuntimeError("boom")
    except RuntimeError:
        print("[main  ] exception escaped; real clock restored:",
              fmt(datetime.datetime.now()),
              "| time.time is real:", time.time.__module__ == "time" and "fake" not in getattr(time.time, "__name__", ""))


if __name__ == "__main__":
    main()
