"""Tests for the ``preserve_fold`` option of ``freeze_time``.

These tests only rely on the standard library: a small custom ``tzinfo``
fixture modelling a backward (ambiguous) transition, plus ``zoneinfo``
when a time zone database is available locally.  No external time zone
downloads are required.
"""
import calendar
import datetime
import time

import pytest

from freezegun import freeze_time
from freezegun.api import real_datetime

UTC = datetime.timezone.utc


class AmbiguousTzInfo(datetime.tzinfo):
    """Minimal tzinfo with a one-hour backward transition on 2024-11-03.

    * UTC-4 before the transition,
    * local times 01:00-02:00 occur twice (fold=0 -> UTC-4, fold=1 -> UTC-5),
    * UTC-5 after the transition.

    ``fromutc`` deliberately never sets ``fold=1`` to emulate tzinfo
    implementations that are not fold-aware.
    """

    def utcoffset(self, dt):
        if dt is None:
            return datetime.timedelta(hours=-5)
        wall = dt.replace(tzinfo=None)
        if datetime.datetime(2024, 11, 3, 1, 0) <= wall < datetime.datetime(2024, 11, 3, 2, 0):
            return datetime.timedelta(hours=-5 if dt.fold else -4)
        if wall < datetime.datetime(2024, 11, 3, 1, 0):
            return datetime.timedelta(hours=-4)
        return datetime.timedelta(hours=-5)

    def dst(self, dt):
        return datetime.timedelta(0)

    def tzname(self, dt):
        return "AMB"

    def fromutc(self, dt):
        assert dt.tzinfo is self
        wall = dt.replace(tzinfo=None)
        if wall < datetime.datetime(2024, 11, 3, 6, 0):
            offset = datetime.timedelta(hours=-4)
        else:
            offset = datetime.timedelta(hours=-5)
        # deliberately fold-unaware: the result always carries fold=0
        return (wall + offset).replace(tzinfo=self)


@pytest.fixture()
def ambiguous_tz():
    return AmbiguousTzInfo()


def test_preserve_fold_second_occurrence_keeps_utc_instant(ambiguous_tz):
    frozen = real_datetime(2024, 11, 3, 1, 30, tzinfo=ambiguous_tz, fold=1)
    expected_utc = real_datetime(2024, 11, 3, 6, 30, tzinfo=UTC)
    assert frozen.astimezone(UTC) == expected_utc

    with freeze_time(frozen, preserve_fold=True):
        now = datetime.datetime.now(ambiguous_tz)
        assert now.fold == 1
        assert now.utcoffset() == datetime.timedelta(hours=-5)
        # the UTC instant must not be shifted again because of the fold
        assert now.astimezone(UTC) == expected_utc
        assert now.timestamp() == frozen.timestamp()
        assert datetime.datetime.now(UTC).astimezone(UTC) == expected_utc
        assert datetime.datetime.utcnow() == real_datetime(2024, 11, 3, 6, 30)
        assert time.time() == calendar.timegm((2024, 11, 3, 6, 30, 0, 0, 0, 0))


def test_preserve_fold_first_occurrence(ambiguous_tz):
    frozen = real_datetime(2024, 11, 3, 1, 30, tzinfo=ambiguous_tz, fold=0)

    with freeze_time(frozen, preserve_fold=True):
        now = datetime.datetime.now(ambiguous_tz)
        assert now.fold == 0
        assert now.utcoffset() == datetime.timedelta(hours=-4)
        assert now.astimezone(UTC) == real_datetime(2024, 11, 3, 5, 30, tzinfo=UTC)


def test_without_preserve_fold_original_interface_unchanged(ambiguous_tz):
    # Option not enabled: the original interface keeps working and no fold
    # marker is forced onto the result of now(tz).
    frozen = real_datetime(2024, 11, 3, 1, 30, tzinfo=ambiguous_tz, fold=1)

    with freeze_time(frozen):
        now = datetime.datetime.now(ambiguous_tz)
        assert now.fold == 0
        assert (now.year, now.month, now.day, now.hour, now.minute) == (2024, 11, 3, 1, 30)
        assert datetime.datetime.utcnow() == real_datetime(2024, 11, 3, 6, 30)


def test_nested_freeze_restores_outer_fold(ambiguous_tz):
    frozen = real_datetime(2024, 11, 3, 1, 30, tzinfo=ambiguous_tz, fold=1)

    with freeze_time(frozen, preserve_fold=True):
        assert datetime.datetime.now(ambiguous_tz).fold == 1
        with freeze_time("2024-01-01"):
            inner_now = datetime.datetime.now(ambiguous_tz)
            assert inner_now.fold == 0
            assert inner_now.replace(tzinfo=None) == real_datetime(2023, 12, 31, 20, 0)
        # exiting the nested freeze restores the outer fold marker
        now = datetime.datetime.now(ambiguous_tz)
        assert now.fold == 1
        assert now.astimezone(UTC) == real_datetime(2024, 11, 3, 6, 30, tzinfo=UTC)


def test_semantics_survive_unfreeze(ambiguous_tz):
    frozen = real_datetime(2024, 11, 3, 1, 30, tzinfo=ambiguous_tz, fold=1)

    with freeze_time(frozen, preserve_fold=True):
        captured = datetime.datetime.now(ambiguous_tz)

    # values produced while frozen keep their meaning after unfreezing
    assert captured.fold == 1
    assert captured.astimezone(UTC) == real_datetime(2024, 11, 3, 6, 30, tzinfo=UTC)
    # and the real clock is restored
    assert datetime.datetime is real_datetime


def test_plain_dates_and_naive_freeze_unaffected(ambiguous_tz):
    with freeze_time("2024-05-05 12:00:00", preserve_fold=True):
        assert datetime.datetime.now() == real_datetime(2024, 5, 5, 12, 0, 0)
        assert datetime.datetime.now().fold == 0
        assert datetime.date.today() == datetime.date(2024, 5, 5)

    with freeze_time(real_datetime(2024, 11, 3, 0, 30, tzinfo=ambiguous_tz), preserve_fold=True):
        # non-ambiguous local time: fold stays 0
        assert datetime.datetime.now(ambiguous_tz).fold == 0


@freeze_time(real_datetime(2024, 11, 3, 1, 30), preserve_fold=True)
def test_decorator_interface_still_works():
    assert datetime.datetime.now() == real_datetime(2024, 11, 3, 1, 30)


def test_zoneinfo_ambiguous_time_fold_preserved():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        tz = zoneinfo.ZoneInfo("America/New_York")
    except Exception:
        pytest.skip("local time zone database not available")

    frozen = real_datetime(2024, 11, 3, 1, 30, tzinfo=tz, fold=1)
    expected_utc = real_datetime(2024, 11, 3, 6, 30, tzinfo=UTC)

    with freeze_time(frozen, preserve_fold=True):
        now = datetime.datetime.now(tz)
        assert now.fold == 1
        assert now.utcoffset() == datetime.timedelta(hours=-5)
        assert now.astimezone(UTC) == expected_utc
        assert datetime.datetime.now(UTC).astimezone(UTC) == expected_utc

    assert datetime.datetime is real_datetime
