"""Tests for the ``preserve_fold`` option of ``freeze_time``.

The fixtures use an ambiguous ("fold") local time: 2023-11-05 01:30 in
America/New_York occurs twice (fold=0 -> UTC 05:30, fold=1 -> UTC 06:30).
ZoneInfo is provided by the standard library plus the ``tzdata`` package, so
no external timezone download is needed.  A custom ``tzinfo`` implementation
is used as well to prove the behaviour does not depend on ZoneInfo.
"""
import datetime

import pytest

from freezegun import freeze_time
from freezegun.api import real_datetime

try:
    from zoneinfo import ZoneInfo
    NYC = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo/tzdata unavailable
    NYC = None

UTC = datetime.timezone.utc

requires_zoneinfo = pytest.mark.skipif(NYC is None, reason="zoneinfo/tzdata not available")


class FoldZone(datetime.tzinfo):
    """Custom tzinfo with a fall-back overlap.

    UTC+1 (DST) before 2023-10-29 01:00 UTC, UTC+0 afterwards, so the local
    times 01:00-02:00 on 2023-10-29 are ambiguous:
    fold=0 -> UTC+1, fold=1 -> UTC+0.
    """

    DST_END_UTC = datetime.datetime(2023, 10, 29, 1, 0)
    AMBIGUOUS_START = datetime.datetime(2023, 10, 29, 1, 0)
    AMBIGUOUS_END = datetime.datetime(2023, 10, 29, 2, 0)

    def _is_dst(self, dt):
        if dt is None:
            return False
        naive = dt.replace(tzinfo=None)
        if naive < self.AMBIGUOUS_START:
            return True
        if naive < self.AMBIGUOUS_END:
            return dt.fold == 0
        return False

    def utcoffset(self, dt):
        return datetime.timedelta(hours=1) if self._is_dst(dt) else datetime.timedelta(0)

    def dst(self, dt):
        return datetime.timedelta(hours=1) if self._is_dst(dt) else datetime.timedelta(0)

    def tzname(self, dt):
        return "FDST" if self._is_dst(dt) else "FSTD"

    def fromutc(self, dt):
        # fold-aware conversion (the default tzinfo.fromutc never sets fold)
        if dt.tzinfo is not self:
            raise ValueError("dt.tzinfo is not self")
        utc_naive = dt.replace(tzinfo=None)
        offset = datetime.timedelta(hours=1) if utc_naive < self.DST_END_UTC else datetime.timedelta(0)
        wall = utc_naive + offset
        fold = 1 if (offset == datetime.timedelta(0)
                     and self.AMBIGUOUS_START <= wall < self.AMBIGUOUS_END) else 0
        return wall.replace(tzinfo=self, fold=fold)


CUSTOM_TZ = FoldZone()
CUSTOM_FOLD0 = datetime.datetime(2023, 10, 29, 1, 30, tzinfo=CUSTOM_TZ, fold=0)  # UTC 00:30
CUSTOM_FOLD1 = datetime.datetime(2023, 10, 29, 1, 30, tzinfo=CUSTOM_TZ, fold=1)  # UTC 01:30


@requires_zoneinfo
@pytest.mark.parametrize("fold,expected_utc", [(0, datetime.datetime(2023, 11, 5, 5, 30)),
                                               (1, datetime.datetime(2023, 11, 5, 6, 30))])
def test_preserve_fold_zoneinfo_both_sides(fold, expected_utc):
    frozen_local = datetime.datetime(2023, 11, 5, 1, 30, tzinfo=NYC, fold=fold)
    with freeze_time(frozen_local, preserve_fold=True):
        now = datetime.datetime.now(NYC)
        assert now.fold == fold
        assert now.replace(tzinfo=None) == datetime.datetime(2023, 11, 5, 1, 30)
        # the UTC instant must not be offset twice because of the fold
        assert now.astimezone(UTC).replace(tzinfo=None) == expected_utc
        assert now.timestamp() == expected_utc.replace(tzinfo=UTC).timestamp()
        assert datetime.datetime.now(UTC).replace(tzinfo=None) == expected_utc
        # utcnow() agrees with the same instant
        assert datetime.datetime.utcnow() == expected_utc


@pytest.mark.parametrize("frozen,expected_utc", [
    (CUSTOM_FOLD0, datetime.datetime(2023, 10, 29, 0, 30)),
    (CUSTOM_FOLD1, datetime.datetime(2023, 10, 29, 1, 30)),
])
def test_preserve_fold_custom_tzinfo_both_sides(frozen, expected_utc):
    with freeze_time(frozen, preserve_fold=True):
        now = datetime.datetime.now(CUSTOM_TZ)
        assert now.fold == frozen.fold
        assert now.replace(tzinfo=None) == datetime.datetime(2023, 10, 29, 1, 30)
        assert now.utcoffset() == frozen.utcoffset()
        assert now.astimezone(UTC).replace(tzinfo=None) == expected_utc
        assert datetime.datetime.now(UTC).replace(tzinfo=None) == expected_utc


@requires_zoneinfo
def test_preserve_fold_not_enabled_keeps_original_behaviour():
    frozen_local = datetime.datetime(2023, 11, 5, 1, 30, tzinfo=NYC, fold=1)
    with freeze_time(frozen_local):
        now = datetime.datetime.now(NYC)
        # original interface: the fold marker is not preserved
        assert now.fold == 0
        assert now.replace(tzinfo=None) == datetime.datetime(2023, 11, 5, 1, 30)
        # the frozen UTC instant itself is still the fold=1 one
        assert datetime.datetime.now(UTC).replace(tzinfo=None) == datetime.datetime(2023, 11, 5, 6, 30)


@requires_zoneinfo
def test_preserve_fold_normal_datetime_unchanged():
    # a non-ambiguous time keeps fold == 0 and the usual semantics
    frozen_local = datetime.datetime(2023, 11, 4, 12, 0, tzinfo=NYC)
    with freeze_time(frozen_local, preserve_fold=True):
        now = datetime.datetime.now(NYC)
        assert now.fold == 0
        assert now.replace(tzinfo=None) == datetime.datetime(2023, 11, 4, 12, 0)
        assert now.astimezone(UTC).replace(tzinfo=None) == datetime.datetime(2023, 11, 4, 16, 0)
    # plain naive freeze is unaffected too
    with freeze_time("2020-01-14", preserve_fold=True):
        assert datetime.datetime.now() == datetime.datetime(2020, 1, 14)


@requires_zoneinfo
def test_preserve_fold_nested_freeze_and_unfreeze():
    fold1 = datetime.datetime(2023, 11, 5, 1, 30, tzinfo=NYC, fold=1)
    with freeze_time(fold1, preserve_fold=True):
        assert datetime.datetime.now(NYC).fold == 1
        with freeze_time("2020-01-01 00:00:00"):
            assert datetime.datetime.now() == datetime.datetime(2020, 1, 1, 0, 0)
        # existing restore logic: the outer frozen time (and its fold) is back
        outer = datetime.datetime.now(NYC)
        assert outer.fold == 1
        assert outer.astimezone(UTC).replace(tzinfo=None) == datetime.datetime(2023, 11, 5, 6, 30)
    # after unfreezing, real time semantics are restored
    assert datetime.datetime is real_datetime
    assert isinstance(datetime.datetime.now(NYC), real_datetime)


@requires_zoneinfo
def test_preserve_fold_nested_inner_without_option():
    fold1 = datetime.datetime(2023, 11, 5, 1, 30, tzinfo=NYC, fold=1)
    with freeze_time(fold1, preserve_fold=True):
        assert datetime.datetime.now(NYC).fold == 1
        with freeze_time(fold1):
            # inner freeze without the option keeps the original behaviour
            assert datetime.datetime.now(NYC).fold == 0
        assert datetime.datetime.now(NYC).fold == 1


@requires_zoneinfo
def test_preserve_fold_as_decorator():
    fold1 = datetime.datetime(2023, 11, 5, 1, 30, tzinfo=NYC, fold=1)

    @freeze_time(fold1, preserve_fold=True)
    def check():
        return datetime.datetime.now(NYC).fold

    assert check() == 1
