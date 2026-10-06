from . import config
from ._async import wrap_coroutine
import asyncio
import copyreg
import dateutil
import datetime
import functools
import sys
import time
import uuid
import calendar
import unittest
import platform
import warnings
import types
import numbers
import inspect
import threading
from typing import TYPE_CHECKING, overload
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Set, Type, TypeVar, Tuple, Union

from dateutil import parser
from dateutil.tz import tzlocal

try:
    from maya import MayaDT  # type: ignore
except ImportError:
    MayaDT = None

if TYPE_CHECKING:
    from typing_extensions import ParamSpec

    P = ParamSpec("P")

T = TypeVar("T")

_TIME_NS_PRESENT = hasattr(time, 'time_ns')
_MONOTONIC_NS_PRESENT = hasattr(time, 'monotonic_ns')
_PERF_COUNTER_NS_PRESENT = hasattr(time, 'perf_counter_ns')
_EPOCH = datetime.datetime(1970, 1, 1)
_EPOCHTZ = datetime.datetime(1970, 1, 1, tzinfo=dateutil.tz.UTC)

T2 = TypeVar("T2")
_Freezable = Union[str, datetime.datetime,  datetime.date,  datetime.timedelta,  types.FunctionType,  Callable[[], Union[str, datetime.datetime, datetime.date, datetime.timedelta]], Iterator[datetime.datetime]]

real_time = time.time
real_localtime = time.localtime
real_gmtime = time.gmtime
real_monotonic = time.monotonic
real_perf_counter = time.perf_counter
real_strftime = time.strftime
real_date = datetime.date
real_datetime = datetime.datetime
real_date_objects = [real_time, real_localtime, real_gmtime, real_monotonic, real_perf_counter, real_strftime, real_date, real_datetime]

if _TIME_NS_PRESENT:
    real_time_ns = time.time_ns
    real_date_objects.append(real_time_ns)

if _MONOTONIC_NS_PRESENT:
    real_monotonic_ns = time.monotonic_ns
    real_date_objects.append(real_monotonic_ns)

if _PERF_COUNTER_NS_PRESENT:
    real_perf_counter_ns = time.perf_counter_ns
    real_date_objects.append(real_perf_counter_ns)

_real_time_object_ids = {id(obj) for obj in real_date_objects}

# time.clock is deprecated and was removed in Python 3.8
real_clock = getattr(time, 'clock', None)

freeze_factories: List[Union["StepTickTimeFactory", "TickingDateTimeFactory", "FrozenDateTimeFactory"]] = []
tz_offsets: List[datetime.timedelta] = []
ignore_lists: List[Tuple[str, ...]] = []
tick_flags: List[bool] = []

# ---------------------------------------------------------------------------
# Explicit thread-scoped freeze support
#
# A thread-scoped freeze only affects time calls made by the registering thread
# (the thread that called ``freeze_time(..., thread_scoped=True).start()``).
# Every other thread keeps observing the real clock and the process wide
# ``freeze_factories`` stack is never touched, so a temporary freeze in one
# thread cannot pollute the global state seen by another thread.
#
# Each registering thread owns an independent stack of ``_ScopedFreeze``
# entries, which gives that thread freezegun-style nested freeze semantics
# (a nested freeze is restored to the outer one on exit, also on exceptional
# exit) without ever sharing a stack between threads.
# ---------------------------------------------------------------------------

#: Lock guarding ``_thread_scopes``, ``_thread_scopes_count`` and the
#: ``_patch_coordinator`` install/uninstall transitions.  It is reentrant so
#: fake time helpers can resolve the active freeze while another helper of the
#: same thread already holds the lock.
_scoped_locks_guard = threading.RLock()

#: Maps the owning thread ident to its stack of ``_ScopedFreeze`` entries.
_thread_scopes: Dict[int, List["_ScopedFreeze"]] = {}

#: Number of currently active thread-scoped freeze entries across all threads.
_thread_scopes_count = 0

#: Module prefixes of thread/process bootstrap frames.  These frames always
#: sit above a worker function started in another thread, so without filtering
#: them out the default ignore list entry ``threading`` would make every time
#: call of a thread-scoped registering thread read the real clock.  The
#: thread-scoped contract is that the freeze belongs to a whole thread, hence
#: those bootstrap prefixes never cause a real-time read for scoped freezes.
_SCOPED_BOOTSTRAP_IGNORES = ("threading", "multiprocessing")


class _ScopedFreeze:
    """A single freeze entry owned by exactly one registering thread.

    Attributes:
        factory: The frozen/ticking time factory for this entry.
        tz_offset: The timezone offset applied to wall-clock reads.
        ignore: Module prefixes that must keep seeing the real clock.
        owner_ident: ``threading.get_ident()`` of the registering thread.
        real_monotonic: When ``True`` (the default for thread-scoped freezes)
            monotonic style clocks (``time.monotonic`` and
            ``time.perf_counter``) keep reporting the real, non-decreasing
            clock even though wall time is frozen.  Monotonic and wall-clock
            policies are therefore independent and a frozen (or backwards
            moved) wall clock can never make a monotonic timer jump
            backwards.
    """

    __slots__ = ("factory", "tz_offset", "ignore", "owner_ident",
                 "real_monotonic")

    def __init__(
        self,
        factory: Union["StepTickTimeFactory", "TickingDateTimeFactory", "FrozenDateTimeFactory"],
        tz_offset: datetime.timedelta,
        ignore: Tuple[str, ...],
        owner_ident: int,
        real_monotonic: bool = True,
    ) -> None:
        self.factory = factory
        self.tz_offset = tz_offset
        # The freeze belongs to the registering thread itself, so the
        # bootstrap frames of the threading/multiprocessing machinery must
        # not force its time calls onto the real clock.
        self.ignore = tuple(
            prefix for prefix in ignore
            if not prefix.startswith(_SCOPED_BOOTSTRAP_IGNORES)
        )
        self.owner_ident = owner_ident
        self.real_monotonic = real_monotonic


class _GlobalFreezeView:
    """Dynamic view over the process-wide (default mode) freeze stack.

    This is returned by :func:`_get_active_freeze` for threads that are not
    registered with a thread-scoped freeze while a default
    :func:`freeze_time` is active.  The attributes are read through at call
    time so that nested default freezes keep working exactly as before.
    """

    __slots__ = ()

    @property
    def factory(self) -> Union["StepTickTimeFactory", "TickingDateTimeFactory", "FrozenDateTimeFactory"]:
        return freeze_factories[-1]

    @property
    def tz_offset(self) -> datetime.timedelta:
        return tz_offsets[-1]

    @property
    def ignore(self) -> Tuple[str, ...]:
        return ignore_lists[-1]

    @property
    def real_monotonic(self) -> bool:
        # The default freezegun policy: monotonic clocks follow the frozen
        # wall clock.
        return False


_GLOBAL_FREEZE_VIEW = _GlobalFreezeView()


def _prune_dead_thread_scopes() -> None:
    """Drop scope stacks whose owning threads have already terminated.

    A freeze left unstopped by a dead thread must never keep the fake clock
    installed for the surviving threads.
    """
    global _thread_scopes_count
    if not _thread_scopes:
        return
    alive_idents = {thread.ident for thread in threading.enumerate()}
    for ident in list(_thread_scopes):
        if ident not in alive_idents:
            _thread_scopes_count -= len(_thread_scopes.pop(ident))


def _get_active_freeze() -> Optional[Union[_ScopedFreeze, _GlobalFreezeView]]:
    """Return the freeze active for the *calling* thread.

    Thread-scoped freezes take precedence for their registering thread.
    Unknown (non-registering) threads fall back to the default process-wide
    freeze and, when none is active, receive ``None`` which makes every fake
    time helper read the injected real clock.  In particular a thread-scoped
    freeze in another thread never reaches an unregistered thread: no
    temporary entry is ever pushed onto the global freeze stack for it.
    """
    with _scoped_locks_guard:
        stack = _thread_scopes.get(threading.get_ident())
        if stack:
            return stack[-1]
    if freeze_factories:
        return _GLOBAL_FREEZE_VIEW
    return None

try:
    # noinspection PyUnresolvedReferences
    real_uuid_generate_time = uuid._uuid_generate_time  # type: ignore
    uuid_generate_time_attr = '_uuid_generate_time'
except AttributeError:
    # noinspection PyUnresolvedReferences
    if hasattr(uuid, '_load_system_functions'):
        # A no-op after Python ~3.9, being removed in 3.13.
        uuid._load_system_functions()
    # noinspection PyUnresolvedReferences
    real_uuid_generate_time = uuid._generate_time_safe  # type: ignore
    uuid_generate_time_attr = '_generate_time_safe'
except ImportError:
    real_uuid_generate_time = None
    uuid_generate_time_attr = None  # type: ignore

try:
    # noinspection PyUnresolvedReferences
    real_uuid_create = uuid._UuidCreate  # type: ignore
except (AttributeError, ImportError):
    real_uuid_create = None


# keep a cache of module attributes otherwise freezegun will need to analyze too many modules all the time
_GLOBAL_MODULES_CACHE: Dict[str, Tuple[str, List[Tuple[str, Any]]]] = {}


def _get_module_attributes(module: types.ModuleType) -> List[Tuple[str, Any]]:
    result: List[Tuple[str, Any]] = []
    try:
        module_attributes = dir(module)
    except (ImportError, TypeError):
        return result
    for attribute_name in module_attributes:
        try:
            attribute_value = getattr(module, attribute_name)
        except (ImportError, AttributeError, TypeError):
            # For certain libraries, this can result in ImportError(_winreg) or AttributeError (celery)
            continue
        else:
            result.append((attribute_name, attribute_value))
    return result


def _setup_module_cache(module: types.ModuleType) -> None:
    date_attrs = []
    all_module_attributes = _get_module_attributes(module)
    for attribute_name, attribute_value in all_module_attributes:
        if id(attribute_value) in _real_time_object_ids:
            date_attrs.append((attribute_name, attribute_value))
    _GLOBAL_MODULES_CACHE[module.__name__] = (_get_module_attributes_hash(module), date_attrs)


def _get_module_attributes_hash(module: types.ModuleType) -> str:
    try:
        module_dir = dir(module)
    except (ImportError, TypeError):
        module_dir = []
    return f'{id(module)}-{hash(frozenset(module_dir))}'


def _get_cached_module_attributes(module: types.ModuleType) -> List[Tuple[str, Any]]:
    module_hash, cached_attrs = _GLOBAL_MODULES_CACHE.get(module.__name__, ('0', []))
    if _get_module_attributes_hash(module) == module_hash:
        return cached_attrs

    # cache miss: update the cache and return the refreshed value
    _setup_module_cache(module)
    # return the newly cached value
    module_hash, cached_attrs = _GLOBAL_MODULES_CACHE[module.__name__]
    return cached_attrs


_is_cpython = (
    hasattr(platform, 'python_implementation') and
    platform.python_implementation().lower() == "cpython"
)


call_stack_inspection_limit = 5


def _should_use_real_time(active: Optional[Union["_ScopedFreeze", "_GlobalFreezeView"]]=None) -> bool:
    if not call_stack_inspection_limit:
        return False

    if active is None:
        # No freeze at all (a default freeze has been stopped, or the calling
        # thread is not registered with a thread-scoped freeze).
        return True

    ignore = active.ignore
    if not ignore:
        return False

    frame = inspect.currentframe().f_back.f_back  # type: ignore

    for _ in range(call_stack_inspection_limit):
        module_name = frame.f_globals.get('__name__')  # type: ignore
        if module_name and module_name.startswith(ignore):
            return True

        frame = frame.f_back  # type: ignore
        if frame is None:
            break

    return False


def get_current_time() -> datetime.datetime:
    return freeze_factories[-1]()


def fake_time() -> float:
    active = _get_active_freeze()
    if _should_use_real_time(active):
        return real_time()
    current_time = active.factory()  # type: ignore[union-attr]
    return calendar.timegm(current_time.timetuple()) + current_time.microsecond / 1000000.0

if _TIME_NS_PRESENT:
    def fake_time_ns() -> int:
        active = _get_active_freeze()
        if _should_use_real_time(active):
            return real_time_ns()
        return int(fake_time() * 1e9)


def fake_localtime(t: Optional[float]=None) -> time.struct_time:
    if t is not None:
        return real_localtime(t)
    active = _get_active_freeze()
    if _should_use_real_time(active):
        return real_localtime()
    shifted_time = active.factory() - datetime.timedelta(seconds=time.timezone)  # type: ignore[union-attr]
    return shifted_time.timetuple()


def fake_gmtime(t: Optional[float]=None) -> time.struct_time:
    if t is not None:
        return real_gmtime(t)
    active = _get_active_freeze()
    if _should_use_real_time(active):
        return real_gmtime()
    return active.factory().timetuple()  # type: ignore[union-attr]


def _get_fake_monotonic() -> float:
    # For monotonic timers like .monotonic(), .perf_counter(), etc
    current_time = get_current_time()
    return (
        calendar.timegm(current_time.timetuple()) +
        current_time.microsecond / 1e6
    )


def _get_fake_monotonic_ns() -> int:
    # For monotonic timers like .monotonic(), .perf_counter(), etc
    current_time = get_current_time()
    return (
        calendar.timegm(current_time.timetuple()) * 1000000 +
        current_time.microsecond
    ) * 1000


def fake_monotonic() -> float:
    active = _get_active_freeze()
    if active is None:
        return real_monotonic()
    if active.real_monotonic or _should_use_real_time(active):
        # Thread-scoped freezes deliberately keep monotonic clocks on the
        # real (never decreasing) time, so a frozen wall clock cannot make
        # monotonic time jump backwards.
        return real_monotonic()

    return _get_fake_monotonic()


def fake_perf_counter() -> float:
    active = _get_active_freeze()
    if active is None:
        return real_perf_counter()
    if active.real_monotonic or _should_use_real_time(active):
        return real_perf_counter()

    return _get_fake_monotonic()


if _MONOTONIC_NS_PRESENT:
    def fake_monotonic_ns() -> int:
        active = _get_active_freeze()
        if active is None:
            return real_monotonic_ns()
        if active.real_monotonic or _should_use_real_time(active):
            return real_monotonic_ns()

        return _get_fake_monotonic_ns()


if _PERF_COUNTER_NS_PRESENT:
    def fake_perf_counter_ns() -> int:
        active = _get_active_freeze()
        if active is None:
            return real_perf_counter_ns()
        if active.real_monotonic or _should_use_real_time(active):
            return real_perf_counter_ns()
        return _get_fake_monotonic_ns()


def fake_strftime(format: Any, time_to_format: Any=None) -> str:
    if time_to_format is None:
        if not _should_use_real_time(_get_active_freeze()):
            time_to_format = fake_localtime()

    if time_to_format is None:
        return real_strftime(format)
    else:
        return real_strftime(format, time_to_format)

if real_clock is not None:
    def fake_clock() -> Any:
        active = _get_active_freeze()
        if active is None or active.real_monotonic or _should_use_real_time(active):
            return real_clock()  # type: ignore

        if len(freeze_factories) == 1:
            return 0.0 if not tick_flags[-1] else real_clock()  # type: ignore

        first_frozen_time = freeze_factories[0]()
        last_frozen_time = get_current_time()

        timedelta = (last_frozen_time - first_frozen_time)
        total_seconds = timedelta.total_seconds()

        if tick_flags[-1]:
            total_seconds += real_clock()  # type: ignore

        return total_seconds


class FakeDateMeta(type):
    @classmethod
    def __instancecheck__(self, obj: Any) -> bool:
        return isinstance(obj, real_date)

    @classmethod
    def __subclasscheck__(cls, subclass: Any) -> bool:
        return issubclass(subclass, real_date)


def datetime_to_fakedatetime(datetime: datetime.datetime) -> "FakeDatetime":
    return FakeDatetime(datetime.year,
                        datetime.month,
                        datetime.day,
                        datetime.hour,
                        datetime.minute,
                        datetime.second,
                        datetime.microsecond,
                        datetime.tzinfo)


def date_to_fakedate(date: datetime.date) -> "FakeDate":
    return FakeDate(date.year,
                    date.month,
                    date.day)


class FakeDate(real_date, metaclass=FakeDateMeta):
    def __add__(self, other: Any) -> "FakeDate":
        result = real_date.__add__(self, other)
        if result is NotImplemented:
            return result
        return date_to_fakedate(result)

    def __sub__(self, other: Any) -> "FakeDate":  # type: ignore
        result = real_date.__sub__(self, other)
        if result is NotImplemented:
            return result  # type: ignore
        if isinstance(result, real_date):
            return date_to_fakedate(result)
        else:
            return result  # type: ignore

    @classmethod
    def today(cls: Type["FakeDate"]) -> "FakeDate":
        active = _get_active_freeze()
        if active is None:
            return date_to_fakedate(real_date.today())
        result = active.factory() + active.tz_offset
        return date_to_fakedate(result)

    @staticmethod
    def _date_to_freeze() -> Optional[datetime.datetime]:
        active = _get_active_freeze()
        if active is None:
            return None
        return active.factory()

    @classmethod
    def _tz_offset(cls) -> datetime.timedelta:
        active = _get_active_freeze()
        if active is None:
            return datetime.timedelta()
        return active.tz_offset

FakeDate.min = date_to_fakedate(real_date.min)
FakeDate.max = date_to_fakedate(real_date.max)


class FakeDatetimeMeta(FakeDateMeta):
    @classmethod
    def __instancecheck__(self, obj: Any) -> bool:
        return isinstance(obj, real_datetime)

    @classmethod
    def __subclasscheck__(cls, subclass: Any) -> bool:
        return issubclass(subclass, real_datetime)


class FakeDatetime(real_datetime, FakeDate, metaclass=FakeDatetimeMeta):
    def __add__(self, other: Any) -> "FakeDatetime":  # type: ignore
        result = real_datetime.__add__(self, other)
        if result is NotImplemented:
            return result
        return datetime_to_fakedatetime(result)

    def __sub__(self, other: Any) -> "FakeDatetime":  # type: ignore
        result = real_datetime.__sub__(self, other)
        if result is NotImplemented:
            return result  # type: ignore
        if isinstance(result, real_datetime):
            return datetime_to_fakedatetime(result)
        else:
            return result  # type: ignore

    def astimezone(self, tz: Optional[datetime.tzinfo]=None) -> "FakeDatetime":
        if tz is None:
            tz = tzlocal()
        return datetime_to_fakedatetime(real_datetime.astimezone(self, tz))

    @classmethod
    def fromtimestamp(cls, t: float, tz: Optional[datetime.tzinfo]=None) -> "FakeDatetime":
        if tz is None:
            tz = dateutil.tz.tzoffset("freezegun", cls._tz_offset())
            result = real_datetime.fromtimestamp(t, tz=tz).replace(tzinfo=None)
        else:
            result = real_datetime.fromtimestamp(t, tz)
        return datetime_to_fakedatetime(result)

    def timestamp(self) -> float:
        if self.tzinfo is None:
            return (self - _EPOCH - self._tz_offset()).total_seconds()  # type: ignore
        return (self - _EPOCHTZ).total_seconds()  # type: ignore

    @classmethod
    def now(cls, tz: Optional[datetime.tzinfo] = None) -> "FakeDatetime":
        now = cls._time_to_freeze() or real_datetime.now()
        if tz:
            result = tz.fromutc(now.replace(tzinfo=tz)) + cls._tz_offset()
        else:
            result = now + cls._tz_offset()
        return datetime_to_fakedatetime(result)

    def date(self) -> "FakeDate":
        return date_to_fakedate(self)

    @property
    def nanosecond(self) -> int:
        try:
            # noinspection PyUnresolvedReferences
            return real_datetime.nanosecond  # type: ignore
        except AttributeError:
            return 0

    @classmethod
    def today(cls) -> "FakeDatetime":
        return cls.now(tz=None)

    @classmethod
    def utcnow(cls) -> "FakeDatetime":
        result = cls._time_to_freeze() or real_datetime.now(datetime.timezone.utc)
        return datetime_to_fakedatetime(result)

    @staticmethod
    def _time_to_freeze() -> Optional[datetime.datetime]:
        active = _get_active_freeze()
        if active is None:
            return None
        return active.factory()

    @classmethod
    def _tz_offset(cls) -> datetime.timedelta:
        active = _get_active_freeze()
        if active is None:
            return datetime.timedelta()
        return active.tz_offset


FakeDatetime.min = datetime_to_fakedatetime(real_datetime.min)
FakeDatetime.max = datetime_to_fakedatetime(real_datetime.max)


def convert_to_timezone_naive(time_to_freeze: datetime.datetime) -> datetime.datetime:
    """
    Converts a potentially timezone-aware datetime to be a naive UTC datetime
    """
    if time_to_freeze.tzinfo:
        time_to_freeze -= time_to_freeze.utcoffset()  # type: ignore
        time_to_freeze = time_to_freeze.replace(tzinfo=None)
    return time_to_freeze


def pickle_fake_date(datetime_: datetime.date) -> Tuple[Type[FakeDate], Tuple[int, int, int]]:
    # A pickle function for FakeDate
    return FakeDate, (
        datetime_.year,
        datetime_.month,
        datetime_.day,
    )


def pickle_fake_datetime(datetime_: datetime.datetime) -> Tuple[Type[FakeDatetime], Tuple[int, int, int, int, int, int, int, Optional[datetime.tzinfo]]]:
    # A pickle function for FakeDatetime
    return FakeDatetime, (
        datetime_.year,
        datetime_.month,
        datetime_.day,
        datetime_.hour,
        datetime_.minute,
        datetime_.second,
        datetime_.microsecond,
        datetime_.tzinfo,
    )


def _parse_time_to_freeze(time_to_freeze_str: Optional[_Freezable]) -> datetime.datetime:
    """Parses all the possible inputs for freeze_time
    :returns: a naive ``datetime.datetime`` object
    """
    if time_to_freeze_str is None:
        time_to_freeze_str = datetime.datetime.now(datetime.timezone.utc)

    if isinstance(time_to_freeze_str, datetime.datetime):
        time_to_freeze = time_to_freeze_str
    elif isinstance(time_to_freeze_str, datetime.date):
        time_to_freeze = datetime.datetime.combine(time_to_freeze_str, datetime.time())
    elif isinstance(time_to_freeze_str, datetime.timedelta):
        time_to_freeze = datetime.datetime.now(datetime.timezone.utc) + time_to_freeze_str
    else:
        time_to_freeze = parser.parse(time_to_freeze_str)  # type: ignore

    return convert_to_timezone_naive(time_to_freeze)


def _parse_tz_offset(tz_offset: Union[datetime.timedelta, float]) -> datetime.timedelta:
    if isinstance(tz_offset, datetime.timedelta):
        return tz_offset
    else:
        return datetime.timedelta(hours=tz_offset)


class TickingDateTimeFactory:

    def __init__(self, time_to_freeze: datetime.datetime, start: datetime.datetime):
        self.time_to_freeze = time_to_freeze
        self.start = start

    def __call__(self) -> datetime.datetime:
        return self.time_to_freeze + (real_datetime.now() - self.start)

    def tick(self, delta: Union[datetime.timedelta, float]=datetime.timedelta(seconds=1)) -> datetime.datetime:
        if isinstance(delta, numbers.Integral):
            self.move_to(self.time_to_freeze + datetime.timedelta(seconds=int(delta)))
        elif isinstance(delta, numbers.Real):
            self.move_to(self.time_to_freeze + datetime.timedelta(seconds=float(delta)))
        else:
            self.move_to(self.time_to_freeze + delta)  # type: ignore
        return self.time_to_freeze

    def move_to(self, target_datetime: _Freezable) -> None:
        """Moves frozen date to the given ``target_datetime``"""
        self.start = real_datetime.now()
        self.time_to_freeze = _parse_time_to_freeze(target_datetime)


class FrozenDateTimeFactory:

    def __init__(self, time_to_freeze: datetime.datetime):
        self.time_to_freeze = time_to_freeze

    def __call__(self) -> datetime.datetime:
        return self.time_to_freeze

    def tick(self, delta: Union[datetime.timedelta, float]=datetime.timedelta(seconds=1)) -> datetime.datetime:
        if isinstance(delta, numbers.Integral):
            self.move_to(self.time_to_freeze + datetime.timedelta(seconds=int(delta)))
        elif isinstance(delta, numbers.Real):
            self.move_to(self.time_to_freeze + datetime.timedelta(seconds=float(delta)))
        else:
            self.time_to_freeze += delta  # type: ignore
        return self.time_to_freeze

    def move_to(self, target_datetime: _Freezable) -> None:
        """Moves frozen date to the given ``target_datetime``"""
        target_datetime = _parse_time_to_freeze(target_datetime)
        delta = target_datetime - self.time_to_freeze
        self.tick(delta=delta)


class StepTickTimeFactory:

    def __init__(self, time_to_freeze: datetime.datetime, step_width: float):
        self.time_to_freeze = time_to_freeze
        self.step_width = step_width

    def __call__(self) -> datetime.datetime:
        return_time = self.time_to_freeze
        self.tick()
        return return_time

    def tick(self, delta: Union[datetime.timedelta, float, None]=None) -> datetime.datetime:
        if not delta:
            delta = datetime.timedelta(seconds=self.step_width)
        elif isinstance(delta, numbers.Integral):
            delta = datetime.timedelta(seconds=int(delta))
        elif isinstance(delta, numbers.Real):
            delta = datetime.timedelta(seconds=float(delta))
        self.time_to_freeze += delta  # type: ignore
        return self.time_to_freeze

    def update_step_width(self, step_width: float) -> None:
        self.step_width = step_width

    def move_to(self, target_datetime: _Freezable) -> None:
        """Moves frozen date to the given ``target_datetime``"""
        target_datetime = _parse_time_to_freeze(target_datetime)
        delta = target_datetime - self.time_to_freeze
        self.tick(delta=delta)


class _PatchCoordinator:
    """Owns the process-wide replacement of real time objects with fakes.

    Both the default (process-wide) freezes and the explicit thread-scoped
    freezes share one installation of the patches.  Patches are installed on
    the first freeze of either kind and removed only once every default
    freeze and every thread-scoped freeze has been stopped.  This makes it
    possible for a thread-scoped freeze to keep working while a default
    freeze is active (or vice versa), and guarantees that stopping a default
    freeze never leaves another thread without the fake entry points it still
    needs.

    The real system clock and system time settings are never modified: only
    the Python module attributes (``time.time``, ``datetime.datetime`` ...)
    are temporarily replaced with thread-aware wrappers.
    """

    def __init__(self) -> None:
        self.installed = False
        self.undo_changes: List[Tuple[Any, str, Any]] = []
        self.modules_at_start: Set[str] = set()
        self.fake_names: Tuple[str, ...] = ()
        self.reals: Dict[int, Any] = {}

    def install(self, ignore: Tuple[str, ...], real_asyncio: bool) -> List[Tuple[Any, str, Any]]:
        """Install the fake time objects. Returns the shared undo list."""
        if self.installed:
            return self.undo_changes

        # Change the modules
        datetime.datetime = FakeDatetime  # type: ignore[misc]
        datetime.date = FakeDate  # type: ignore[misc]

        time.time = fake_time
        time.monotonic = fake_monotonic
        time.perf_counter = fake_perf_counter
        time.localtime = fake_localtime  # type: ignore
        time.gmtime = fake_gmtime  # type: ignore
        time.strftime = fake_strftime  # type: ignore
        if uuid_generate_time_attr:
            setattr(uuid, uuid_generate_time_attr, None)
        uuid._UuidCreate = None  # type: ignore[attr-defined]
        uuid._last_timestamp = None  # type: ignore[attr-defined]

        copyreg.dispatch_table[real_datetime] = pickle_fake_datetime
        copyreg.dispatch_table[real_date] = pickle_fake_date

        # Change any place where the module had already been imported
        to_patch = [
            ('real_date', real_date, FakeDate),
            ('real_datetime', real_datetime, FakeDatetime),
            ('real_gmtime', real_gmtime, fake_gmtime),
            ('real_localtime', real_localtime, fake_localtime),
            ('real_monotonic', real_monotonic, fake_monotonic),
            ('real_perf_counter', real_perf_counter, fake_perf_counter),
            ('real_strftime', real_strftime, fake_strftime),
            ('real_time', real_time, fake_time),
        ]

        if _TIME_NS_PRESENT:
            time.time_ns = fake_time_ns
            to_patch.append(('real_time_ns', real_time_ns, fake_time_ns))

        if _MONOTONIC_NS_PRESENT:
            time.monotonic_ns = fake_monotonic_ns
            to_patch.append(('real_monotonic_ns', real_monotonic_ns, fake_monotonic_ns))

        if _PERF_COUNTER_NS_PRESENT:
            time.perf_counter_ns = fake_perf_counter_ns
            to_patch.append(('real_perf_counter_ns', real_perf_counter_ns, fake_perf_counter_ns))

        if real_clock is not None:
            # time.clock is deprecated and was removed in Python 3.8
            time.clock = fake_clock  # type: ignore[attr-defined]
            to_patch.append(('real_clock', real_clock, fake_clock))

        self.fake_names = tuple(fake.__name__ for real_name, real, fake in to_patch)  # type: ignore
        self.reals = {id(fake): real for real_name, real, fake in to_patch}
        fakes = {id(real): fake for real_name, real, fake in to_patch}
        add_change = self.undo_changes.append

        # Save the current loaded modules
        self.modules_at_start = set(sys.modules.keys())

        with warnings.catch_warnings():
            warnings.filterwarnings('ignore')

            for mod_name, module in list(sys.modules.items()):
                if mod_name is None or module is None or mod_name == __name__:
                    continue
                elif mod_name.startswith(ignore) or mod_name.endswith('.six.moves'):
                    continue
                elif (not hasattr(module, "__name__") or module.__name__ in ('datetime', 'time')):
                    continue

                module_attrs = _get_cached_module_attributes(module)
                for attribute_name, attribute_value in module_attrs:
                    fake = fakes.get(id(attribute_value))
                    if fake:
                        setattr(module, attribute_name, fake)
                        add_change((module, attribute_name, attribute_value))

        if real_asyncio:
            # To avoid breaking `asyncio.sleep()`, let asyncio event loops see real
            # monotonic time even though we've just frozen `time.monotonic()` which
            # is normally used there. If we didn't do this, `await asyncio.sleep()`
            # would be hanging forever breaking many tests that use `freeze_time`.
            #
            # Note that we cannot statically tell the class of asyncio event loops
            # because it is not officially documented and can actually be changed
            # at run time using `asyncio.set_event_loop_policy`. That's why we check
            # the type by creating a loop here and destroying it immediately.
            event_loop = asyncio.new_event_loop()
            event_loop.close()
            EventLoopClass = type(event_loop)
            add_change((EventLoopClass, "time", EventLoopClass.time))  # type: ignore
            EventLoopClass.time = lambda self: real_monotonic()  # type: ignore[method-assign]

        self.installed = True
        return self.undo_changes

    def uninstall(self, ignore: Tuple[str, ...]) -> None:
        """Restore all real time objects. Must be called with no freeze active."""
        if not self.installed:
            return

        datetime.datetime = real_datetime  # type: ignore[misc]
        datetime.date = real_date  # type: ignore[misc]
        copyreg.dispatch_table.pop(real_datetime, None)
        copyreg.dispatch_table.pop(real_date, None)
        for module_or_object, attribute, original_value in self.undo_changes:
            setattr(module_or_object, attribute, original_value)
        self.undo_changes = []

        # Restore modules loaded after install()
        modules_to_restore = set(sys.modules.keys()) - self.modules_at_start
        self.modules_at_start = set()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            for mod_name in modules_to_restore:
                module = sys.modules.get(mod_name, None)
                if mod_name is None or module is None:
                    continue
                elif mod_name.startswith(ignore) or mod_name.endswith('.six.moves'):
                    continue
                elif not hasattr(module, "__name__") or module.__name__ in ('datetime', 'time'):
                    continue
                for module_attribute in dir(module):

                    if module_attribute in self.fake_names:
                        continue
                    try:
                        attribute_value = getattr(module, module_attribute)
                    except (ImportError, AttributeError, TypeError):
                        # For certain libraries, this can result in ImportError(_winreg) or AttributeError (celery)
                        continue

                    real = self.reals.get(id(attribute_value))
                    if real:
                        setattr(module, module_attribute, real)

        time.time = real_time
        time.monotonic = real_monotonic
        time.perf_counter = real_perf_counter
        time.gmtime = real_gmtime
        time.localtime = real_localtime
        time.strftime = real_strftime
        time.clock = real_clock  # type: ignore[attr-defined]

        if _TIME_NS_PRESENT:
            time.time_ns = real_time_ns

        if _MONOTONIC_NS_PRESENT:
            time.monotonic_ns = real_monotonic_ns

        if _PERF_COUNTER_NS_PRESENT:
            time.perf_counter_ns = real_perf_counter_ns

        if uuid_generate_time_attr:
            setattr(uuid, uuid_generate_time_attr, real_uuid_generate_time)
        uuid._UuidCreate = real_uuid_create  # type: ignore[attr-defined]
        uuid._last_timestamp = None  # type: ignore[attr-defined]

        self.installed = False


_patch_coordinator = _PatchCoordinator()


class _freeze_time:
    """
    A class to freeze time for testing purposes.

    This class can be used as a context manager or a decorator to freeze time
    during the execution of a block of code or a function. It provides various
    options to customize the behavior of the frozen time.

    Attributes:
        time_to_freeze (datetime.datetime): The datetime to freeze time at.
        tz_offset (datetime.timedelta): The timezone offset to apply to the frozen time.
        ignore (List[str]): A list of module names to ignore when freezing time.
        tick (bool): Whether to allow time to tick forward.
        auto_tick_seconds (float): The number of seconds to auto-tick the frozen time.
        undo_changes (List[Tuple[types.ModuleType, str, Any]]): A list of changes to undo when stopping the frozen time.
        modules_at_start (Set[str]): A set of module names that were loaded at the start of freezing time.
        as_arg (bool): Whether to pass the frozen time as an argument to the decorated function.
        as_kwarg (str): The name of the keyword argument to pass the frozen time to the decorated function.
        real_asyncio (Optional[bool]): Whether to allow asyncio event loops to see real monotonic time.

    Methods:
        __call__(func): Decorates a function or class to freeze time during its execution.
        decorate_class(klass): Decorates a class to freeze time during its execution.
        __enter__(): Starts freezing time and returns the time factory.
        __exit__(*args): Stops freezing time.
        start(): Starts freezing time and returns the time factory.
        stop(): Stops freezing time and restores the original time functions.
        decorate_coroutine(coroutine): Decorates a coroutine to freeze time during its execution.
        decorate_callable(func): Decorates a callable to freeze time during its execution.
    """

    def __init__(
        self,
        time_to_freeze_str: Optional[_Freezable],
        tz_offset: Union[int, datetime.timedelta],
        ignore: List[str],
        tick: bool,
        as_arg: bool,
        as_kwarg: str,
        auto_tick_seconds: float,
        real_asyncio: Optional[bool],
        thread_scoped: bool = False,
    ):
        self.time_to_freeze = _parse_time_to_freeze(time_to_freeze_str)
        self.tz_offset = _parse_tz_offset(tz_offset)
        self.ignore = tuple(ignore)
        self.tick = tick
        self.auto_tick_seconds = auto_tick_seconds
        self.as_arg = as_arg
        self.as_kwarg = as_kwarg
        self.real_asyncio = real_asyncio
        # When True the freeze only affects the registering thread (the one
        # calling start()).  Default False preserves freezegun's historical
        # process-wide behavior.
        self.thread_scoped = thread_scoped
        # The scoped freeze entry created by start(); kept for owner based
        # removal in stop().
        self._scoped_freeze: Optional[_ScopedFreeze] = None
        # Backwards compatible views of the shared patch bookkeeping.
        self.undo_changes = _patch_coordinator.undo_changes
        self.modules_at_start = _patch_coordinator.modules_at_start

    # mypy objects to this because Type is Callable, but Pytype needs it because
    # (unlike mypy's) its inference does not assume class decorators always leave
    # the type unchanged.
    @overload
    def __call__(self, func: Type[T2]) -> Type[T2]:  # type: ignore[overload-overlap]
        ...

    @overload
    def __call__(self, func: "Callable[P, Awaitable[Any]]") -> "Callable[P, Awaitable[Any]]":
        ...

    @overload
    def __call__(self, func: "Callable[P, T]") -> "Callable[P, T]":
        ...

    def __call__(self, func: Union[Type[T2], "Callable[P, Awaitable[Any]]", "Callable[P, T]"]) -> Union[Type[T2], "Callable[P, Awaitable[Any]]", "Callable[P, T]"]:  # type: ignore
        if inspect.isclass(func):
            return self.decorate_class(func)
        elif inspect.iscoroutinefunction(func):
            return self.decorate_coroutine(func)
        elif inspect.isgeneratorfunction(func):
            return self.decorate_generator_function(func) # type: ignore
        return self.decorate_callable(func)  # type: ignore

    def decorate_class(self, klass: Type[T2]) -> Type[T2]:
        if issubclass(klass, unittest.TestCase):
            # If it's a TestCase, we freeze time around setup and teardown, as well
            # as for every test case. This requires some care to avoid freezing
            # the time pytest sees, as otherwise this would distort the reported
            # timings.

            orig_setUpClass = klass.setUpClass
            orig_tearDownClass = klass.tearDownClass

            # noinspection PyDecorator
            @classmethod  # type: ignore
            def setUpClass(cls: type) -> None:
                self.start()
                if orig_setUpClass is not None:
                    orig_setUpClass()
                self.stop()

            # noinspection PyDecorator
            @classmethod  # type: ignore
            def tearDownClass(cls: type) -> None:
                self.start()
                if orig_tearDownClass is not None:
                    orig_tearDownClass()
                self.stop()

            klass.setUpClass = setUpClass  # type: ignore
            klass.tearDownClass = tearDownClass  # type: ignore

            orig_setUp = klass.setUp
            orig_tearDown = klass.tearDown

            def setUp(*args: Any, **kwargs: Any) -> None:
                self.start()
                if orig_setUp is not None:
                    orig_setUp(*args, **kwargs)

            def tearDown(*args: Any, **kwargs: Any) -> None:
                if orig_tearDown is not None:
                    orig_tearDown(*args, **kwargs)
                self.stop()

            klass.setUp = setUp  # type: ignore[method-assign]
            klass.tearDown = tearDown  # type: ignore[method-assign]

        else:
            seen = set()

            klasses = klass.mro()
            for base_klass in klasses:
                for (attr, attr_value) in base_klass.__dict__.items():
                    if attr.startswith('_') or attr in seen:
                        continue
                    seen.add(attr)

                    if not callable(attr_value) or inspect.isclass(attr_value) or isinstance(attr_value, staticmethod):
                        continue

                    try:
                        if attr_value.__dict__.get("_pytestfixturefunction") and hasattr(attr_value, "__pytest_wrapped__"):
                            # PYTEST==8.2.x (and maybe others)
                            # attr_value is a pytest fixture
                            # In other words: attr_value == fixture(original_method)
                            # We need to keep the fixture itself intact to ensure pytest still treats it as a fixture
                            # We still want to freeze time inside the original_method though
                            attr_value.__pytest_wrapped__.obj = self(attr_value.__pytest_wrapped__.obj)
                        elif attr_value.__dict__.get("_fixture_function"):
                            # PYTEST==8.4.x
                            # Same
                            attr_value._fixture_function = self(attr_value._fixture_function)
                        else:
                            # Wrap the entire method inside 'freeze_time'
                            setattr(klass, attr, self(attr_value))
                    except (AttributeError, TypeError):
                        # Sometimes we can't set this for built-in types and custom callables
                        continue
        return klass

    def __enter__(self) -> Union[StepTickTimeFactory, TickingDateTimeFactory, FrozenDateTimeFactory]:
        return self.start()

    def __exit__(self, *args: Any) -> None:
        self.stop()

    def _build_freeze_factory(self) -> Union[StepTickTimeFactory, TickingDateTimeFactory, FrozenDateTimeFactory]:
        if self.auto_tick_seconds:
            return StepTickTimeFactory(self.time_to_freeze, self.auto_tick_seconds)
        elif self.tick:
            return TickingDateTimeFactory(self.time_to_freeze, real_datetime.now())
        return FrozenDateTimeFactory(self.time_to_freeze)

    def start(self) -> Union[StepTickTimeFactory, TickingDateTimeFactory, FrozenDateTimeFactory]:
        if self.thread_scoped:
            return self._start_scoped()
        return self._start_global()

    def _start_global(self) -> Union[StepTickTimeFactory, TickingDateTimeFactory, FrozenDateTimeFactory]:
        freeze_factory = self._build_freeze_factory()

        with _scoped_locks_guard:
            is_already_started = len(freeze_factories) > 0
            freeze_factories.append(freeze_factory)
            tz_offsets.append(self.tz_offset)
            ignore_lists.append(self.ignore)
            tick_flags.append(self.tick)

            # The fake time objects are shared between default and
            # thread-scoped freezes: only install them when no other freeze
            # (of either kind) has already done so.
            if not is_already_started and not _patch_coordinator.installed:
                _patch_coordinator.install(self.ignore, bool(self.real_asyncio))

        return freeze_factory

    def _start_scoped(self) -> Union[StepTickTimeFactory, TickingDateTimeFactory, FrozenDateTimeFactory]:
        freeze_factory = self._build_freeze_factory()
        owner_ident = threading.get_ident()
        scoped_freeze = _ScopedFreeze(
            factory=freeze_factory,
            tz_offset=self.tz_offset,
            ignore=self.ignore,
            owner_ident=owner_ident,
            # Monotonic and wall time policies are separate: thread-scoped
            # freezes freeze the wall clock while monotonic style clocks keep
            # advancing with the real, never decreasing clock.
            real_monotonic=True,
        )
        self._scoped_freeze = scoped_freeze

        global _thread_scopes_count
        with _scoped_locks_guard:
            _prune_dead_thread_scopes()
            stack = _thread_scopes.get(owner_ident)
            if stack is None:
                stack = []
                _thread_scopes[owner_ident] = stack
            stack.append(scoped_freeze)
            _thread_scopes_count += 1

            if not _patch_coordinator.installed:
                _patch_coordinator.install(self.ignore, bool(self.real_asyncio))

        return freeze_factory

    def stop(self) -> None:
        if self.thread_scoped:
            self._stop_scoped()
        else:
            self._stop_global()

    def _stop_global(self) -> None:
        with _scoped_locks_guard:
            freeze_factories.pop()
            ignore_lists.pop()
            tick_flags.pop()
            tz_offsets.pop()

            # Keep the patches in place while thread-scoped freezes are still
            # active in other threads; only the last freeze of any kind may
            # restore the real time objects.
            if not freeze_factories and _thread_scopes_count == 0:
                _patch_coordinator.uninstall(self.ignore)

    def _stop_scoped(self) -> None:
        global _thread_scopes_count
        scoped_freeze = self._scoped_freeze
        with _scoped_locks_guard:
            if scoped_freeze is not None:
                stack = _thread_scopes.get(scoped_freeze.owner_ident)
                if stack is not None:
                    # Nested freezes are popped by identity so that stopping
                    # an outer freeze is impossible without the inner one
                    # having been stopped first; this restores the exact
                    # owner's previous (outer) entry.
                    for index in range(len(stack) - 1, -1, -1):
                        if stack[index] is scoped_freeze:
                            del stack[index]
                            _thread_scopes_count -= 1
                            break
                    if scoped_freeze.owner_ident in _thread_scopes and not stack:
                        _thread_scopes.pop(scoped_freeze.owner_ident, None)
                self._scoped_freeze = None

            # Stacks of threads that terminated without calling stop() cannot
            # keep the fake clock installed for the surviving threads.
            _prune_dead_thread_scopes()

            if not freeze_factories and _thread_scopes_count == 0:
                _thread_scopes.clear()
                _patch_coordinator.uninstall(self.ignore)
        self._scoped_freeze = None

    def decorate_coroutine(self, coroutine: "Callable[P, Awaitable[T]]") -> "Callable[P, Awaitable[T]]":
        return wrap_coroutine(self, coroutine)

    def _call_with_time_factory(self, time_factory: Union[StepTickTimeFactory, TickingDateTimeFactory, FrozenDateTimeFactory], func: "Callable[P, T]", args: Any, kwargs: Any) -> T:
        """
        Invoke a function and pass in the TimeFactory if necessary

        :args: Original arguments to the function.
        :kwargs: Original keyword arguments. Passed in as a dict in case the keys conflict with the other arguments to this function ('time_factory' or 'func')
        """
        if self.as_arg and self.as_kwarg:
            assert False, "You can't specify both as_arg and as_kwarg at the same time. Pick one."
        if self.as_arg:
            result = func(time_factory, *args, **kwargs)  # type: ignore
        elif self.as_kwarg:
            kwargs[self.as_kwarg] = time_factory
            result = func(*args, **kwargs)
        else:
            result = func(*args, **kwargs)
        return result

    def decorate_generator_function(self, func: "Callable[P, Iterator[T]]") -> "Callable[P, Iterator[T]]":

        @functools.wraps(func)
        def wrapper(*args: "P.args", **kwargs: "P.kwargs") -> Iterator[T]:
            with self as time_factory:
                yield from self._call_with_time_factory(time_factory, func=func, args=args, kwargs=kwargs)

        return wrapper

    def decorate_callable(self, func: "Callable[P, T]") -> "Callable[P, T]":

        @functools.wraps(func)
        def wrapper(*args: "P.args", **kwargs: "P.kwargs") -> T:
            with self as time_factory:
                return self._call_with_time_factory(time_factory, func=func, args=args, kwargs=kwargs)

        return wrapper


def freeze_time(time_to_freeze: Optional[_Freezable]=None, tz_offset: Union[int, datetime.timedelta]=0, ignore: Optional[List[str]]=None, tick: bool=False, as_arg: bool=False, as_kwarg: str='',
                auto_tick_seconds: float=0, real_asyncio: bool=False, thread_scoped: bool=False) -> _freeze_time:
    """
    Freezes time for testing purposes.

    This function can be used as a decorator or a context manager to freeze time
    during the execution of a block of code or a function. It provides various
    options to customize the behavior of the frozen time.

    Args:
        time_to_freeze (Optional[_Freezable]): The datetime to freeze time at.
        tz_offset (Union[int, datetime.timedelta]): The timezone offset to apply to the frozen time.
        ignore (Optional[List[str]]): A list of module names to ignore when freezing time.
        tick (bool): Whether to allow time to tick forward.
        as_arg (bool): Whether to pass the frozen time as an argument to the decorated function.
        as_kwarg (str): The name of the keyword argument to pass the frozen time to the decorated function.
        auto_tick_seconds (float): The number of seconds to auto-tick the frozen time.
        real_asyncio (bool): Whether to allow asyncio event loops to see real monotonic time.
        thread_scoped (bool): When True the freeze is *explicitly scoped to the
            registering thread* (the thread that enters the context manager or
            calls ``start()``). Time calls made by that thread are frozen while
            every other thread keeps observing the injected real clock. The
            process-wide freeze stack is never modified, so a temporary
            thread-scoped freeze cannot pollute another thread. Nested
            thread-scoped freezes are restored to the outer freeze on exit
            (including exceptional exit). Wall time and monotonic time use
            separate policies: the wall clock is frozen while
            ``time.monotonic()`` and ``time.perf_counter()`` keep reporting the
            real, non-decreasing clock, so moving the frozen wall clock
            backwards cannot make monotonic time jump backwards. The system
            clock is never modified. Defaults to False which preserves the
            historical process-wide freeze behavior.

    Returns:
        _freeze_time: An instance of the _freeze_time class.
    """
    acceptable_times: Any = (type(None), str, datetime.date, datetime.timedelta,
             types.FunctionType, types.GeneratorType)

    if MayaDT is not None:
        acceptable_times += MayaDT,

    if not isinstance(time_to_freeze, acceptable_times):
        raise TypeError(('freeze_time() expected None, a string, date instance, datetime '
                         'instance, MayaDT, timedelta instance, function or a generator, but got '
                         'type {}.').format(type(time_to_freeze)))
    if tick and not _is_cpython:
        raise SystemError('Calling freeze_time with tick=True is only compatible with CPython')

    if isinstance(time_to_freeze, types.FunctionType):
        return freeze_time(time_to_freeze(), tz_offset, ignore, tick, as_arg, as_kwarg, auto_tick_seconds, real_asyncio=real_asyncio, thread_scoped=thread_scoped)

    if isinstance(time_to_freeze, types.GeneratorType):
        return freeze_time(next(time_to_freeze), tz_offset, ignore, tick, as_arg, as_kwarg, auto_tick_seconds, real_asyncio=real_asyncio, thread_scoped=thread_scoped)

    if MayaDT is not None and isinstance(time_to_freeze, MayaDT):
        return freeze_time(time_to_freeze.datetime(), tz_offset, ignore,
                           tick, as_arg, as_kwarg, auto_tick_seconds, real_asyncio=real_asyncio, thread_scoped=thread_scoped)

    if ignore is None:
        ignore = []
    ignore = ignore[:]
    if config.settings.default_ignore_list:
        ignore.extend(config.settings.default_ignore_list)

    return _freeze_time(
        time_to_freeze_str=time_to_freeze,
        tz_offset=tz_offset,
        ignore=ignore,
        tick=tick,
        as_arg=as_arg,
        as_kwarg=as_kwarg,
        auto_tick_seconds=auto_tick_seconds,
        real_asyncio=real_asyncio,
        thread_scoped=thread_scoped,
    )


# Setup adapters for sqlite
try:
    # noinspection PyUnresolvedReferences
    import sqlite3
except ImportError:
    # Some systems have trouble with this
    pass
else:
    # These are copied from Python sqlite3.dbapi2
    def adapt_date(val: datetime.date) -> str:
        return val.isoformat()

    def adapt_datetime(val: datetime.datetime) -> str:
        return val.isoformat(" ")

    sqlite3.register_adapter(FakeDate, adapt_date)
    sqlite3.register_adapter(FakeDatetime, adapt_datetime)


# Setup converters for pymysql
try:
    import pymysql.converters
except ImportError:
    pass
else:
    pymysql.converters.encoders[FakeDate] = pymysql.converters.encoders[real_date]
    pymysql.converters.conversions[FakeDate] = pymysql.converters.encoders[real_date]
    pymysql.converters.encoders[FakeDatetime] = pymysql.converters.encoders[real_datetime]
    pymysql.converters.conversions[FakeDatetime] = pymysql.converters.encoders[real_datetime]
