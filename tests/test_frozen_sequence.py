import datetime
import json

import pytest

from freezegun import freeze_sequence, freeze_time
from freezegun.api import real_datetime


def test_sequence_reusable_entries_and_exhaustion():
    """A two-moment sequence entered three times with the same object:
    first two entries yield the two moments, the third fails explicitly,
    and real time is restored afterwards."""
    seq = freeze_sequence(["2026-01-01 10:00:00", "2026-01-02 11:00:00"])

    with seq:
        assert datetime.datetime.now() == datetime.datetime(2026, 1, 1, 10, 0, 0)

    with seq:
        assert datetime.datetime.now() == datetime.datetime(2026, 1, 2, 11, 0, 0)

    with pytest.raises(RuntimeError, match="exhausted"):
        with seq:
            raise AssertionError("exhausted sequence must not enter the body")

    # Real time is restored after the sequence is done.
    assert abs((datetime.datetime.now() - real_datetime.now()).total_seconds()) < 60


def test_exhaustion_does_not_break_outer_freeze():
    """Exhausting an inner sequence must not disturb an outer freeze."""
    seq = freeze_sequence(["2026-03-01 08:30:00"])

    with freeze_time("2026-02-14 12:00:00"):
        with seq:
            assert datetime.datetime.now() == datetime.datetime(2026, 3, 1, 8, 30, 0)

        # Inner sequence is exhausted: fails before installing any new patch.
        with pytest.raises(RuntimeError, match="exhausted"):
            with seq:
                raise AssertionError("exhausted sequence must not enter the body")

        # The outer freeze is still in effect.
        assert datetime.datetime.now() == datetime.datetime(2026, 2, 14, 12, 0, 0)

    # And real time is back once everything has exited.
    assert abs((datetime.datetime.now() - real_datetime.now()).total_seconds()) < 60


def test_sequence_results_with_tmp_path(tmp_path):
    """Small temp-dir fixture: record the observed frozen moments to a file
    and verify the recorded results match the sequence."""
    moments = ["2026-01-01 10:00:00", "2026-01-02 11:00:00"]
    seq = freeze_sequence(moments)

    observed = []
    with seq:
        observed.append(datetime.datetime.now().isoformat())
    with seq:
        observed.append(datetime.datetime.now().isoformat())

    result_file = tmp_path / "frozen_sequence_results.json"
    result_file.write_text(json.dumps({"expected": moments, "observed": observed}))
    recorded = json.loads(result_file.read_text())

    assert recorded["observed"] == [
        "2026-01-01T10:00:00",
        "2026-01-02T11:00:00",
    ]
    assert recorded["observed"] == [
        datetime.datetime.fromisoformat(m).isoformat() for m in recorded["expected"]
    ]


def test_freeze_time_generator_single_value_regression():
    """Regression: freeze_time() with a generator keeps its original
    single-value semantics -- one value is consumed at call time and the
    same frozen moment is used on every entry of that freezer."""
    def gen():
        yield "2026-01-01 00:00:00"
        yield "2026-01-02 00:00:00"  # never consumed by the freezer below

    freezer = freeze_time(gen())

    with freezer:
        assert datetime.datetime.now() == datetime.datetime(2026, 1, 1, 0, 0, 0)

    # Re-entering the same freezer reuses the single value taken at call time.
    with freezer:
        assert datetime.datetime.now() == datetime.datetime(2026, 1, 1, 0, 0, 0)

    assert abs((datetime.datetime.now() - real_datetime.now()).total_seconds()) < 60
