"""The built-in ``alerts`` plugin: an overlay that shows pushed alerts (T071).

Implements FR-017 to FR-024 and the ``alerts`` section of
``contracts/builtin-plugins.md``: the ``push``/``dismiss``/``resolve``/``list``/
``show``/``clear-missed`` verbs, the settings ``durations``, ``max_duration_s``,
``max_wait_s``, ``queue_cap``, ``history.retention``, ``colors``, ``font``,
``title_size`` and ``message_size``, and the ``alerts.missed`` bus topic
(``{count, max_severity}``, no TTL).

The plugin declares two capabilities: ``overlay`` (it draws over the whole of
its target region) and ``alert_sink`` (the core routes every ``ctx.alert`` here,
"with the same validation as ``alert push``" — the sink *is* that verb, so the
validation is not duplicated). It owns its schema in ``<home>/state/<name>.db``
through ``ctx.db()``, which is what lets it use only the public SDK (FR-043).

Housekeeping runs every frame: the render pass settles the display countdown (so a
timed-out alert leaves the panel on the next frame) and prunes history on the 60 s
cadence the data model requires. It needs no thread of its own.
"""

from __future__ import annotations

import threading
from typing import Any

from smart_panel.plugins.alerts.model import DEFAULT_DURATIONS, AlertValidationError
from smart_panel.plugins.alerts.queue import AlertManager, AlertNotFound
from smart_panel.plugins.alerts.render import SEVERITY_COLORS, AlertRenderer
from smart_panel.plugins.alerts.store import RETENTIONS, AlertStore
from smart_panel.sdk.plugin import (
    Command,
    CommandError,
    Inactive,
    Manifest,
    Plugin,
)
from smart_panel.sdk.settings import Setting

__all__ = ["AlertsPlugin"]

_FLOOR_PX = 12  # ``display.min_text_px`` lower bound; render clamps to the frame's value

_SETTINGS: tuple[Setting, ...] = (
    Setting(
        name="history.retention",
        type="enum",
        default="1h",
        description="How long alert history is kept",
        choices=list(RETENTIONS),
    ),
    Setting(
        name="durations",
        type="object",
        default=dict(DEFAULT_DURATIONS),
        description="Display seconds per severity (info/warning/critical)",
    ),
    Setting(
        name="max_duration_s",
        type="int",
        default=300,
        description="Upper cap for any alert's display time, in seconds",
        min=1,
        max=86_400,
    ),
    Setting(
        name="max_wait_s",
        type="int",
        default=120,
        description="How long an alert may wait in the queue before it is missed",
        min=1,
        max=86_400,
    ),
    Setting(
        name="queue_cap",
        type="int",
        default=50,
        description="Maximum number of alerts waiting behind the displayed one",
        min=5,
        max=200,
    ),
    Setting(
        name="colors",
        type="object",
        default=dict(SEVERITY_COLORS),
        description="Background colour per severity; the text colour is chosen for contrast",
    ),
    Setting(
        name="font",
        type="str",
        default="DejaVuSans",
        description="Bundled font name or a path to a TTF",
    ),
    Setting(
        name="title_size",
        type="int",
        default=56,
        description="Title size in pixels (never below frame.min_text_px)",
        min=_FLOOR_PX,
        max=512,
    ),
    Setting(
        name="message_size",
        type="int",
        default=40,
        description="Message size in pixels (never below frame.min_text_px)",
        min=_FLOOR_PX,
        max=512,
    ),
)

_COMMANDS: tuple[Command, ...] = (
    Command(
        name="push",
        summary="Push an alert (agent or any local process)",
        args=(
            Setting(
                name="severity",
                type="enum",
                default=None,
                description="info, warning or critical",
                choices=["info", "warning", "critical"],
            ),
            Setting(
                name="message",
                type="str",
                default="",
                description="Alert text (1..280 chars)",
            ),
            Setting(name="title", type="str", default=None, description="Optional short title"),
            Setting(name="source", type="str", default=None, description="Optional source label"),
            Setting(
                name="id",
                type="str",
                default=None,
                description="Optional caller id; a re-push with the same id updates in place",
            ),
            Setting(
                name="ttl",
                type="int",
                default=None,
                description="Display seconds (defaults by severity, capped at max_duration_s)",
                min=1,
            ),
        ),
        spoolable=True,
    ),
    Command(
        name="dismiss",
        summary="Dismiss an alert by id, or the one currently displayed",
        args=(
            Setting(name="id", type="str", default=None, description="Alert id"),
            Setting(
                name="current",
                type="bool",
                default=False,
                description="Dismiss the displayed alert",
            ),
        ),
        spoolable=True,
    ),
    Command(
        name="resolve",
        summary="Resolve an alert wherever it is and un-count it as missed",
        args=(Setting(name="id", type="str", default=None, description="Alert id"),),
        spoolable=True,
    ),
    Command(
        name="list",
        summary="List alert history within the retention window, newest first",
        args=(
            Setting(name="state", type="str", default=None, description="Filter by state"),
            Setting(name="limit", type="int", default=None, description="At most N rows", min=1),
            Setting(
                name="since",
                type="str",
                default=None,
                description="Only alerts newer than this duration (for example 30m, 2h)",
            ),
        ),
    ),
    Command(
        name="show",
        summary="Show the current alert, the queue and the missed count",
    ),
    Command(
        name="clear-missed",
        summary="Clear the missed-alert indicator (all, or one id)",
        args=(Setting(name="id", type="str", default=None, description="Alert id"),),
        spoolable=True,
    ),
)


class AlertsPlugin(Plugin):
    """Show pushed alerts over the region named by the layout's overlay."""

    manifest = Manifest(
        name="alerts",
        version="0.1.0",
        summary="Show alerts pushed through the CLI, over the main region",
        capabilities=frozenset({"overlay", "alert_sink"}),
        settings=_SETTINGS,
        commands=_COMMANDS,
    )

    def __init__(self, ctx: Any, settings: dict[str, Any]) -> None:
        # Contract: construction is cheap and does no I/O; the database is opened
        # in ``start`` (the layout, and therefore the region size, is applied then).
        super().__init__(ctx, settings)
        self._lock = threading.RLock()
        self._manager: AlertManager | None = None
        self._renderer = AlertRenderer()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Open the plugin database and settle whatever a previous run left."""
        self._manager = self._build_manager()
        self._manager.start()

    def stop(self) -> None:
        """No background work of our own; the manager has none either."""
        self._renderer.invalidate()

    def on_settings_changed(self, settings: dict[str, Any]) -> None:
        """Adopt a validated settings change live (nothing here can be refused)."""
        self.settings = dict(settings)
        with self._lock:
            if self._manager is not None:
                self._manager.apply_settings(
                    durations=self.settings.get("durations"),
                    max_duration_s=self.settings.get("max_duration_s", 300),
                    max_wait_s=self.settings.get("max_wait_s", 120),
                    queue_cap=self.settings.get("queue_cap", 50),
                    retention=self.settings.get("history.retention", "1h"),
                )
            self._renderer.invalidate()

    # --------------------------------------------------------------- render

    def render(self, frame: Any) -> Any:
        """Draw the displayed alert sized to the overlay's target region."""
        manager = self._manager
        if manager is None:
            return Inactive()
        # Every frame: settle the display countdown (so a timed-out alert leaves
        # the panel on the next frame) and prune history on its 60 s cadence.
        manager.tick()
        return self._renderer.render(frame, manager, self.settings)

    # -------------------------------------------------------------- commands

    def handle_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one ``alert`` verb; raise ``CommandError`` for expected failures."""
        if self._manager is None:
            raise CommandError("internal", "the alerts instance is not started")
        try:
            if name == "push":
                return self._push(args)
            if name == "dismiss":
                return self._manager.dismiss(
                    id=args.get("id"), current=bool(args.get("current"))
                )
            if name == "resolve":
                return self._manager.resolve(id=args.get("id"))
            if name == "list":
                return {
                    "alerts": self._manager.list(
                        state=args.get("state"),
                        limit=args.get("limit"),
                        since=args.get("since"),
                    )
                }
            if name == "show":
                return self._manager.show()
            if name == "clear-missed":
                return self._manager.clear_missed(id=args.get("id"))
        except AlertValidationError as exc:
            raise CommandError("invalid_input", str(exc)) from exc
        except AlertNotFound as exc:
            raise CommandError("not_found", f"no alert {exc.args[0]!r}") from exc
        raise CommandError("invalid_input", f"unknown command {name!r}")

    def _push(self, args: dict[str, Any]) -> dict[str, Any]:
        manager = self._manager
        assert manager is not None  # guarded by ``handle_command``
        severity = args.get("severity")
        if not isinstance(severity, str) or not severity:
            raise CommandError("invalid_input", "push needs --severity {info,warning,critical}")
        message = args.get("message")
        if not isinstance(message, str) or not message:
            raise CommandError("invalid_input", "push needs --message")
        # ``_received_at`` is set by the runner when it replays a spool entry, so a
        # push that waited while the runner was down carries its real receive time
        # (``data-model.md``: received_at is "CLI or spool receive time").
        received_at = args.get("_received_at")
        return manager.submit(
            severity=severity,
            message=message,
            title=args.get("title"),
            source=args.get("source"),
            id=args.get("id"),
            ttl=args.get("ttl"),
            received_at=float(received_at) if received_at is not None else None,
        )

    def describe_state(self) -> dict[str, Any]:
        """The current alert id, queue depth and missed count (never a secret)."""
        manager = self._manager
        if manager is None:
            return {"current": None, "queued": 0, "missed": 0}
        show = manager.show()
        current = show.get("current") or {}
        return {
            "current": current.get("id"),
            "queued": len(show.get("queue", [])),
            "missed": show.get("missed", {}).get("count", 0),
        }

    # -------------------------------------------------------------- internals

    def _build_manager(self) -> AlertManager:
        clock = getattr(self.ctx, "clock", None)
        store = AlertStore(self.ctx.db(), clock=clock)
        return AlertManager(
            store,
            clock,
            bus=getattr(self.ctx, "bus", None),
            log=getattr(self.ctx, "log", None),
            durations=self.settings.get("durations"),
            max_duration_s=self.settings.get("max_duration_s", 300),
            max_wait_s=self.settings.get("max_wait_s", 120),
            queue_cap=self.settings.get("queue_cap", 50),
            retention=self.settings.get("history.retention", "1h"),
            request_render=getattr(self.ctx, "request_render", None),
        )
