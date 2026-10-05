"""
Explicit frozen-time subscription interface.

This module provides a way to subscribe to *actual* changes of a frozen clock
(i.e. those caused by an explicit ``tick`` or ``move_to`` call on a time
factory). Merely reading the time through ``now()`` does not emit any event.

Design notes:

* A :class:`_SubscriptionRegistry` keeps the list of subscribers for one
  factory. The registry is guarded by an internal lock that is only held while
  snapshotting / mutating the subscriber list -- it is *never* held while a
  callback runs, so a callback can safely re-enter ``now()`` (or even
  ``tick()``) without deadlocking.
* Each :class:`Subscription` has its own re-entrant lock. The check
  "was this subscription cancelled before the callback started?" and the
  callback invocation are atomic with respect to :meth:`Subscription.unsubscribe`,
  so an unsubscribe that wins before the callback starts suppresses the call.
* A failing callback is captured in a :class:`NotificationReceipt` and recorded
  on the subscription; it never propagates and never rolls back the already
  advanced frozen time.
"""

from __future__ import annotations

import datetime
import threading
from dataclasses import dataclass
from typing import Any, Callable, List, Optional


@dataclass(frozen=True)
class TimeChangeEvent:
    """An event emitted after a frozen clock is explicitly changed.

    Attributes:
        old_time: The frozen instant before the operation.
        new_time: The frozen instant after the operation.
        operation: The category of the operation (``"tick"`` or ``"move_to"``).
    """

    old_time: datetime.datetime
    new_time: datetime.datetime
    operation: str


@dataclass
class NotificationReceipt:
    """The outcome of invoking one subscriber's callback for one event.

    Attributes:
        subscription_id: The id of the subscription that was invoked.
        success: ``True`` if the callback returned without raising.
        error: The exception raised by the callback, if any.
    """

    subscription_id: int
    success: bool
    error: Optional[BaseException] = None


# A callback receives the :class:`TimeChangeEvent` as its only argument.
TimeChangeCallback = Callable[[TimeChangeEvent], Any]


class Subscription:
    """A handle to a subscription on a frozen-time factory.

    Returned by :meth:`_SubscriptionRegistry.subscribe`. Use
    :meth:`unsubscribe` to stop receiving events.
    """

    def __init__(
        self,
        registry: "_SubscriptionRegistry",
        callback: TimeChangeCallback,
        subscription_id: int,
    ) -> None:
        self._registry = registry
        self._callback = callback
        self._id = subscription_id
        self._cancelled = False
        # Re-entrant so a callback may unsubscribe itself without deadlocking.
        self._lock = threading.RLock()
        self._last_error: Optional[BaseException] = None

    @property
    def id(self) -> int:
        """The unique id of this subscription within its registry."""
        return self._id

    @property
    def active(self) -> bool:
        """``True`` unless :meth:`unsubscribe` has taken effect."""
        return not self._cancelled

    @property
    def last_error(self) -> Optional[BaseException]:
        """The exception raised by the most recent callback, if any.

        A failing callback is captured here (and in the
        :class:`NotificationReceipt`) instead of propagating, so it can never
        roll back the already advanced frozen time.
        """
        return self._last_error

    def unsubscribe(self) -> bool:
        """Stop receiving events.

        Returns ``True`` if this call cancelled an active subscription,
        ``False`` if it was already cancelled.
        """
        return self._registry._unsubscribe(self)

    def _notify(self, event: TimeChangeEvent) -> Optional[NotificationReceipt]:
        # Atomically check-and-invoke so an unsubscribe that wins before the
        # callback starts suppresses the call. The registry lock is not held
        # here; callbacks run outside the internal registry lock.
        with self._lock:
            if self._cancelled:
                return None
            try:
                self._callback(event)
            except BaseException as exc:  # capture any failure
                self._last_error = exc
                return NotificationReceipt(self._id, success=False, error=exc)
            return NotificationReceipt(self._id, success=True)


class _SubscriptionRegistry:
    """Per-factory registry of :class:`Subscription` objects."""

    def __init__(self) -> None:
        self._subscriptions: List[Subscription] = []
        self._lock = threading.Lock()
        self._next_id = 0

    def subscribe(self, callback: TimeChangeCallback) -> Subscription:
        if not callable(callback):
            raise TypeError(
                "subscribe() expected a callable, got %r" % type(callback).__name__
            )
        with self._lock:
            self._next_id += 1
            subscription = Subscription(self, callback, self._next_id)
            self._subscriptions.append(subscription)
            return subscription

    def _unsubscribe(self, subscription: Subscription) -> bool:
        with self._lock:
            try:
                self._subscriptions.remove(subscription)
                listed = True
            except ValueError:
                listed = False
        with subscription._lock:
            was_active = not subscription._cancelled
            subscription._cancelled = True
        return bool(listed and was_active)

    def notify(self, event: TimeChangeEvent) -> List[NotificationReceipt]:
        """Invoke every active subscriber for ``event``.

        Returns one :class:`NotificationReceipt` per subscriber that was
        actually invoked. Cancelled subscribers are skipped. Callbacks run
        outside the registry's internal lock.
        """
        with self._lock:
            snapshot = list(self._subscriptions)

        receipts: List[NotificationReceipt] = []
        for subscription in snapshot:
            receipt = subscription._notify(event)
            if receipt is not None:
                receipts.append(receipt)
        return receipts


__all__ = [
    "TimeChangeEvent",
    "NotificationReceipt",
    "Subscription",
    "TimeChangeCallback",
]
