import datetime
import time

import pytest

from freezegun import freeze_time, freeze_time_sequence, FrozenSequenceError
from freezegun import api


MOMENT_1 = datetime.datetime(2020, 1, 1, 12, 0, 0)
MOMENT_2 = datetime.datetime(2021, 6, 15, 8, 30, 0)
OUTER_MOMENT = datetime.datetime(2030, 3, 3, 3, 3, 3)


def test_sequence_reused_three_times_and_real_time_restored() -> None:
    sequence = freeze_time_sequence([MOMENT_1, MOMENT_2])

    # first entry: first moment
    with sequence as factory:
        assert datetime.datetime.now() == MOMENT_1
        assert factory() == MOMENT_1

    # second entry on the SAME object: next moment
    with sequence:
        assert datetime.datetime.now() == MOMENT_2

    # third entry: sequence is exhausted -> clear failure
    with pytest.raises(FrozenSequenceError):
        with sequence:
            pass  # pragma: no cover - never reached

    # exhaustion installed nothing and real time is restored
    assert not api.freeze_factories
    assert datetime.datetime is api.real_datetime
    assert time.time is api.real_time
    now = datetime.datetime.now()
    assert now not in (MOMENT_1, MOMENT_2)


def test_exhaustion_does_not_break_outer_freeze() -> None:
    sequence = freeze_time_sequence([MOMENT_1])
    with freeze_time(OUTER_MOMENT):
        with sequence:
            assert datetime.datetime.now() == MOMENT_1
        # exiting the inner sequence restores the outer patch
        assert datetime.datetime.now() == OUTER_MOMENT

        # exhausting the sequence fails before installing any new patch,
        # so the outer freeze is still in effect
        with pytest.raises(FrozenSequenceError):
            with sequence:
                pass  # pragma: no cover - never reached
        assert datetime.datetime.now() == OUTER_MOMENT
        assert len(api.freeze_factories) == 1

    assert not api.freeze_factories
    assert datetime.datetime is api.real_datetime


def test_freeze_time_generator_single_value_regression() -> None:
    # Regression: the original freeze_time interface keeps consuming a
    # single value from a generator per call.
    moments = (datetime.datetime(year=y, month=1, day=1) for y in (2010, 2011))

    with freeze_time(moments):
        assert datetime.datetime.now() == datetime.datetime(2010, 1, 1)

    with freeze_time(moments):
        assert datetime.datetime.now() == datetime.datetime(2011, 1, 1)

    with pytest.raises(StopIteration):
        freeze_time(moments)


def test_sequence_demo_report(tmp_path) -> None:
    # Small tmp-folder fixture demonstrating the observed results.
    sequence = freeze_time_sequence([MOMENT_1, MOMENT_2])
    observed = []

    with sequence:
        observed.append("enter#1: %s" % datetime.datetime.now().isoformat())
    with sequence:
        observed.append("enter#2: %s" % datetime.datetime.now().isoformat())
    try:
        with sequence:
            pass  # pragma: no cover - never reached
    except FrozenSequenceError as exc:
        observed.append("enter#3: %s: %s" % (type(exc).__name__, exc))
    observed.append("after exit, real time restored: %s" % (datetime.datetime is api.real_datetime))

    report = tmp_path / "sequence_report.txt"
    report.write_text("\n".join(observed) + "\n", encoding="utf-8")

    lines = report.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "enter#1: 2020-01-01T12:00:00",
        "enter#2: 2021-06-15T08:30:00",
        "enter#3: FrozenSequenceError: freeze_time_sequence() is exhausted: "
        "no more moments to freeze at",
        "after exit, real time restored: True",
    ]
