"""The alerts state machine: queue, preemption and the missed indicator (T069).

Implements the state machine, queue rules and retention pass of ``data-model.md``
("Alert", "Queue rules", "History retention"):

* one alert is displayed at a time; the rest wait in a queue ordered by severity
  (critical, warning, info) and then by arrival;
* a strictly higher severity preempts the displayed alert, which returns to the
  front of its severity band, is **not** counted as missed, and gets its full
  display time again when it is shown next;
* the queue cap (default 50) drops the oldest ``info`` first, then ``warning``,
  recorded ``expired_in_queue`` and counted missed;
* ``max_wait_s`` (default 120) moves a waiting alert to ``expired_in_queue``;
* the display countdown runs on the **monotonic** clock, so a wall-clock jump or
  a suspend/resume neither extends nor cuts short a displayed alert;
* alerts that were displayed when the runner stopped are treated as timed out at
  the next start, and a queued alert that has already waited longer than its own
  duration while the runner was down is ``expired_in_queue``;
* history is pruned on retention; missed rows survive until cleared.

The module imports only ``smart_panel.sdk`` (through :mod:`.model`) and the
standard library (FR-043).
"""

from __future__ import annotations

import threading
from typing import Any

from smart_panel.plugins.alerts.model import (
    DEFAULT_DURATIONS,
    DEFAULT_MAX_DURATION_S,
    MAX_MESSAGE_CHARS,
    SEVERITY_RANK,
    Alert,
    AlertValidationError,
    build_alert,
    parse_duration,
)
from smart_panel.plugins.alerts.store import AlertStore, retention_cutoff

__all__ = ["AlertManager", "AlertNotFound", "MISSED_TOPIC"]

MISSED_TOPIC = "alerts.missed"
PRUNE_INTERVAL_S = 60.0  # "pruning at least every 60 s"


class AlertNotFound(KeyError):
    """No alert matches the requested id (the CLI reports ``not_found``)."""


class AlertManager:
    """The queue, history and missed bookkeeping for one ``alerts`` instance.

    Args:
        store: The plugin's :class:`~smart_panel.plugins.alerts.store.AlertStore`.
        clock: The SDK ``Clock`` (``monotonic()``/``time()``); a fake clock in tests.
        bus: Optional bus; ``alerts.missed`` is published to it on every change.
        log: Optional structured logger; discarded and expired alerts are logged.
        durations / max_duration_s / max_wait_s / queue_cap / retention: The
            plugin settings of the same names (``contracts/builtin-plugins.md``).
        request_render: Called when the visible state changed.
    """

    def __init__(
        self,
        store: AlertStore,
        clock: Any,
        bus: Any = None,
        log: Any = None,
        *,
        durations: dict[str, Any] | None = None,
        max_duration_s: Any = DEFAULT_MAX_DURATION_S,
        max_wait_s: Any = 120,
        queue_cap: Any = 50,
        retention: str = "1h",
        request_render: Any = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.bus = bus
        self.log = log
        self.request_render = request_render if request_render is not None else (lambda: None)
        self._lock = threading.RLock()
        self._current: Alert | None = None
        self._queue: list[Alert] = []
        self._sequence = 0
        self._last_prune_monotonic: float | None = None
        self.apply_settings(
            durations=durations,
            max_duration_s=max_duration_s,
            max_wait_s=max_wait_s,
            queue_cap=queue_cap,
            retention=retention,
        )

    # ---------------------------------------------------------------- settings

    def apply_settings(
        self,
        *,
        durations: dict[str, Any] | None = None,
        max_duration_s: Any = DEFAULT_MAX_DURATION_S,
        max_wait_s: Any = 120,
        queue_cap: Any = 50,
        retention: str = "1h",
    ) -> None:
        """Adopt a validated settings change (a live ``config set``)."""
        table = dict(DEFAULT_DURATIONS)
        for name, value in (durations or {}).items():
            if name in DEFAULT_DURATIONS:
                try:
                    table[name] = parse_duration(value, default=DEFAULT_DURATIONS[name])
                except AlertValidationError:  # pragma: no cover - guarded by the schema
                    continue
        with self._lock:
            self._durations = table
            self.max_duration_s = parse_duration(max_duration_s, default=DEFAULT_MAX_DURATION_S)
            self.max_wait_s = max(1, parse_duration(max_wait_s, default=120))
            self.queue_cap = max(1, int(queue_cap))
            self.retention = str(retention)

    # ----------------------------------------------------------------- start

    def start(self) -> None:
        """Initialise the schema and settle the rows a previous run left behind."""
        self.store.init()
        now_monotonic = float(self.clock.monotonic())
        now_wall = float(self.clock.time())
        with self._lock:
            self._current = None
            self._queue = []
            # A displayed alert cannot outlive its own runner: the panel has no
            # input and the runner is the only clock that matters, so it is
            # recorded timed_out (data-model.md, T069 startup rule).
            for alert in self.store.list(state="displayed"):
                self._expire(alert, "timed_out", now_wall)
            for alert in self.store.list(state="queued"):
                waited = now_wall - alert.received_at
                if waited > self.max_wait_s or waited > alert.duration_s:
                    self._expire(alert, "expired_in_queue", now_wall)
                    continue
                alert.waiting_since = now_monotonic - max(0.0, waited)
                self._queue.append(alert)
            self._sort_queue()
            self._last_prune_monotonic = now_monotonic
            self._publish_missed()

    # ------------------------------------------------------------------ verbs

    def submit(
        self,
        severity: Any,
        message: Any,
        *,
        title: Any = None,
        source: Any = None,
        id: Any = None,  # noqa: A002 - the field is named ``id`` by the contract
        ttl: Any = None,
        received_at: float | None = None,
    ) -> dict[str, Any]:
        """Accept one alert; return ``{id, state, position}`` (plus a warning)."""
        with self._lock:
            now_monotonic = float(self.clock.monotonic())
            now_wall = float(self.clock.time())
            self._advance_locked(now_monotonic, now_wall)
            moment = float(received_at) if received_at is not None else now_wall
            alert = build_alert(
                severity,
                message,
                title=title,
                source=source,
                id=id,
                ttl=ttl,
                received_at=moment,
                durations=self._durations,
                max_duration_s=self.max_duration_s,
            )
            alert.seq = self._next_seq()
            warning = None
            if isinstance(message, str) and len(message) > MAX_MESSAGE_CHARS:
                warning = f"message truncated to {MAX_MESSAGE_CHARS} characters"

            existing = self._find(alert.id)
            if existing is not None:
                # "re-pushing an existing id updates it in place"
                was_displayed = existing.state == "displayed"
                self._adopt(existing, alert)
                if was_displayed:
                    existing.state = "displayed"
                    existing.shown_at = now_wall
                    existing.shown_monotonic = now_monotonic
                self.store.upsert(existing)
                self.store.unmark_missed(existing.id)
                position = 0 if existing.state == "displayed" else self._position_of(existing)
                result = {"id": existing.id, "state": existing.state, "position": position}
                self._publish_missed()
                self.request_render()
                return self._with_warning(result, warning)

            if now_wall - alert.received_at > alert.duration_s:
                # It waited longer than it could ever have been shown: expire it
                # now rather than flashing a stale notification on screen.
                self._expire(alert, "expired_in_queue", now_wall)
                self._publish_missed()
                self.request_render()
                return self._with_warning(
                    {"id": alert.id, "state": "expired_in_queue", "position": 0}, warning
                )

            if self._current is None:
                self._display(alert, now_monotonic, now_wall)
                position = 0
            elif SEVERITY_RANK[alert.severity] > SEVERITY_RANK[self._current.severity]:
                preempted = self._current
                preempted.state = "queued"
                preempted.shown_at = None
                preempted.shown_monotonic = None
                preempted.ended_at = None
                preempted.waiting_since = now_monotonic
                self._queue.append(preempted)
                self._sort_queue()
                self.store.upsert(preempted)
                self._display(alert, now_monotonic, now_wall)
                position = 0
            else:
                alert.waiting_since = now_monotonic
                self._queue.append(alert)
                self._sort_queue()
                self.store.upsert(alert)
                position = self._position_of(alert)

            self._enforce_cap(now_wall)
            self._publish_missed()
            self.request_render()
            state = "displayed" if position == 0 else "queued"
            return self._with_warning(
                {"id": alert.id, "state": state, "position": position}, warning
            )

    def dismiss(self, id: Any = None, current: Any = False) -> dict[str, Any]:  # noqa: A002
        """Dismiss one alert (by id, or the one currently displayed)."""
        with self._lock:
            self._advance_locked(self.clock.monotonic(), self.clock.time())
            if current:
                target = self._current
            elif id is None:
                raise AlertValidationError("dismiss needs either an id or --current")
            else:
                target = self._find(str(id)) if id is not None else None
                if target is None or not target.is_open:
                    raise AlertNotFound(str(id))
            if target is None:
                raise AlertNotFound("no alert is currently displayed")
            self._finish(target, "dismissed", float(self.clock.time()))
            self._advance_locked(self.clock.monotonic(), self.clock.time())
            self._publish_missed()
            self.request_render()
            return {"id": target.id, "outcome": "dismissed"}

    def resolve(self, id: Any = None) -> dict[str, Any]:  # noqa: A002
        """Resolve any alert by id, wherever it is, and un-count it as missed."""
        with self._lock:
            if id is None:
                raise AlertValidationError("resolve needs an id")
            self._advance_locked(self.clock.monotonic(), self.clock.time())
            target = self._find(str(id))
            if target is None:
                raise AlertNotFound(str(id))
            was = target.state
            self._finish(target, "resolved", float(self.clock.time()))
            self._advance_locked(self.clock.monotonic(), self.clock.time())
            self._publish_missed()
            self.request_render()
            return {"id": target.id, "outcome": "resolved", "was": was}

    def clear_missed(self, id: Any = None) -> dict[str, Any]:  # noqa: A002
        """Clear the missed indicator: one id, or every missed alert."""
        with self._lock:
            cleared = self.store.clear_missed(None if id is None else str(id))
            self._publish_missed()
            self.request_render()
            return {"cleared": cleared}

    def list(  # noqa: A003 - the verb is named ``list`` by the contract
        self,
        state: str | None = None,
        limit: Any = None,
        since: Any = None,
    ) -> list[dict[str, Any]]:
        """History within retention, newest first (``list`` verb)."""
        with self._lock:
            now_wall = float(self.clock.time())
            cutoff = retention_cutoff(now_wall, self.retention)
            if since is not None:
                cutoff = max(cutoff, now_wall - parse_duration(since))
            alerts = self.store.list(
                state=state if isinstance(state, str) and state else None,
                since=cutoff,
                limit=int(limit) if limit is not None else None,
            )
            return [alert.summary() for alert in alerts]

    def show(self) -> dict[str, Any]:
        """The current alert, the queue and the missed count (``show`` verb)."""
        with self._lock:
            changed = self._advance_locked(self.clock.monotonic(), self.clock.time())
            if changed:
                # This call may be the first to notice that an alert's time ran
                # out: ``alert show`` and every ``status`` (through
                # ``describe_state``) reach the timers here. The missed set the
                # advance just changed has to reach the bus, or a consumer -- the
                # sidebar's built-in alert entry, a count plugin -- keeps the
                # previous value until some later render tick publishes it.
                self._publish_missed()
            current = None
            if self._current is not None:
                current = {**self._current.summary(), "position": 0}
            return {
                "current": current,
                "queue": [
                    {**alert.summary(), "position": index + 1}
                    for index, alert in enumerate(self._queue)
                ],
                "missed": self.missed_summary(),
            }

    def missed_summary(self) -> dict[str, Any]:
        """``{count, max_severity}`` — the ``alerts.missed`` payload."""
        with self._lock:
            return self.store.missed_summary()

    def current(self) -> Alert | None:
        """The alert currently displayed, or ``None`` (the render path reads this)."""
        with self._lock:
            return self._current

    def queued(self) -> list[Alert]:
        """The waiting alerts, in display order (the ``+N queued`` count)."""
        with self._lock:
            return list(self._queue)

    # ------------------------------------------------------------- time passes

    def advance(self) -> bool:
        """Expire what the clock says is due; return whether the display changed."""
        with self._lock:
            changed = self._advance_locked(self.clock.monotonic(), self.clock.time())
            if changed:
                self._publish_missed()
                self.request_render()
            return changed

    def tick(self) -> None:
        """One render pass worth of time passing (the plugin's per-frame hook).

        The display countdown has to be settled on *every* frame: an alert whose
        duration elapsed must leave the panel on the next frame, not at the next
        retention pass. Only the retention prune keeps its 60 s cadence.
        """
        self._settle(force_prune=False)

    def housekeeping(self) -> None:
        """Settle timers and prune history now (the forced periodic pass)."""
        self._settle(force_prune=True)

    def _settle(self, *, force_prune: bool) -> None:
        """Settle due timers and prune on the 60 s cadence (or when forced)."""
        with self._lock:
            now_monotonic = float(self.clock.monotonic())
            now_wall = float(self.clock.time())
            changed = self._advance_locked(now_monotonic, now_wall)
            due = (
                force_prune
                or self._last_prune_monotonic is None
                or now_monotonic - self._last_prune_monotonic >= PRUNE_INTERVAL_S
            )
            if due:
                self.store.prune(now_wall, self.retention)
                self._last_prune_monotonic = now_monotonic
            if changed or due:
                self._publish_missed()
            if changed:
                self.request_render()

    # ---------------------------------------------------------------- internals

    def _advance_locked(self, now_monotonic: float, now_wall: float) -> bool:
        """Settle every timer that has come due; return whether anything changed."""
        changed = False
        current = self._current
        if current is not None and current.shown_monotonic is not None:
            elapsed = now_monotonic - current.shown_monotonic
            if elapsed >= current.duration_s:
                self._expire(current, "timed_out", now_wall)
                self._current = None
                changed = True
        for alert in list(self._queue):
            if alert.waiting_since is None:
                continue
            if now_monotonic - alert.waiting_since >= self.max_wait_s:
                self._queue.remove(alert)
                self._expire(alert, "expired_in_queue", now_wall)
                changed = True
        if self._current is None and self._queue:
            self._display(self._queue.pop(0), now_monotonic, now_wall)
            changed = True
        return changed

    def _display(self, alert: Alert, now_monotonic: float, now_wall: float) -> None:
        alert.state = "displayed"
        alert.shown_at = now_wall
        alert.shown_monotonic = now_monotonic
        alert.waiting_since = None
        alert.ended_at = None
        alert.outcome = None
        self._current = alert
        self.store.upsert(alert)

    def _expire(self, alert: Alert, outcome: str, now_wall: float) -> None:
        alert.state = outcome
        alert.outcome = outcome
        alert.ended_at = now_wall
        alert.shown_monotonic = None
        alert.waiting_since = None
        self.store.upsert(alert)
        self.store.mark_missed(alert, outcome)
        self._log("alert_expired", alert=alert, reason=outcome)

    def _finish(self, alert: Alert, outcome: str, now_wall: float) -> None:
        alert.state = outcome
        alert.outcome = outcome
        alert.ended_at = now_wall
        alert.shown_monotonic = None
        alert.waiting_since = None
        if self._current is alert:
            self._current = None
        if alert in self._queue:
            self._queue.remove(alert)
        self.store.upsert(alert)
        self.store.unmark_missed(alert.id)
        self._log("alert_finished", alert=alert, reason=outcome)

    def _enforce_cap(self, now_wall: float) -> None:
        """Drop the oldest alert of the lowest severity band while over the cap."""
        while len(self._queue) > self.queue_cap:
            victim = self._overflow_victim()
            if victim is None:  # pragma: no cover - the queue is non-empty here
                return
            self._queue.remove(victim)
            self._expire(victim, "expired_in_queue", now_wall)
            self._log("alert_discarded", alert=victim, reason="queue_full")

    def _overflow_victim(self) -> Alert | None:
        for severity in ("info", "warning", "critical"):
            band = [alert for alert in self._queue if alert.severity == severity]
            if band:
                return min(band, key=lambda alert: (alert.received_at, alert.seq))
        return None

    def _sort_queue(self) -> None:
        self._queue.sort(
            key=lambda alert: (
                -SEVERITY_RANK.get(alert.severity, 0),
                alert.received_at,
                alert.seq,
            )
        )

    def _next_seq(self) -> int:
        """Arrival order, so two alerts received in the same clock tick still order."""
        self._sequence += 1
        return self._sequence

    def _find(self, alert_id: str) -> Alert | None:
        if self._current is not None and self._current.id == alert_id:
            return self._current
        for alert in self._queue:
            if alert.id == alert_id:
                return alert
        return self.store.get(alert_id)

    def _position_of(self, alert: Alert) -> int:
        for index, queued in enumerate(self._queue):
            if queued.id == alert.id:
                return index + 1
        return 0

    def _adopt(self, existing: Alert, fresh: Alert) -> None:
        """Copy the new fields onto the row already holding this id."""
        for name in (
            "severity",
            "message",
            "title",
            "source",
            "duration_s",
            "received_at",
        ):
            setattr(existing, name, getattr(fresh, name))

    def _publish_missed(self) -> None:
        if self.bus is None:
            return
        self.bus.publish(MISSED_TOPIC, self.store.missed_summary(), ttl_s=None)

    def _log(self, event: str, alert: Alert, reason: str) -> None:
        if self.log is None:
            return
        self.log.info(event, id=alert.id, severity=alert.severity, reason=reason)

    @staticmethod
    def _with_warning(result: dict[str, Any], warning: str | None) -> dict[str, Any]:
        if warning is not None:
            result = {**result, "warning": warning}
        return result
