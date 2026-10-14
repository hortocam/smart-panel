"""The runner: lifecycle, threads, the frame pipeline and live config (T051).

The runner is the long-lived process that owns the panel (or a file/null sink).
One runner per machine: it takes an exclusive ``flock`` on
``<runtime>/runner.lock`` and records its pid in ``<runtime>/runner.pid``, so a
second runner exits with a conflict (FR-008).

Thread layout (``research.md`` decision 3):

* a **compositor loop** at the target fps, which submits a frame to each plugin
  worker and composes the canvas;
* a **sender thread** holding a single-slot "latest frame wins" buffer, so a slow
  USB write never queues stale frames;
* the **control server** (its own accept thread, T047);
* a **scheduler** for ``ctx.spawn`` tasks and data-source fetches, pumped by
  * a **housekeeping** thread that also advances the governor (T050).

Startup is safe with no panel attached: the sink retries opening in the
background while the runner keeps composing. The spool is ingested *before the
first frame* (contract ``control-protocol.md``), and the first frame is itself
held back until a compose pass carries region content (or until every instance
has delivered its first render, so an ``Inactive``-only layout still starts) —
so ``start``/``restart`` never return on the bare background and an immediate
``preview`` is not blank. ``status`` aggregates the panel, frame, layout, plugin
and spool state.

A test-only ``clock_file`` (honored only when ``SMART_PANEL_TESTING=1``) lets a
subprocess integration test advance a fake clock by writing the file; it can
never be reached on a default path.
"""

from __future__ import annotations

import base64
import copy
import fcntl
import io
import os
import signal
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from smart_panel import __version__
from smart_panel.core import paths
from smart_panel.core.bus import Bus
from smart_panel.core.commands import make_dispatch
from smart_panel.core.compositor import Compositor
from smart_panel.core.config import Config, validate_config
from smart_panel.core.control import access, protocol
from smart_panel.core.control.server import ControlServer
from smart_panel.core.control.spool import SpoolIngest
from smart_panel.core.encoder import Encoder
from smart_panel.core.fonts import FontRegistry
from smart_panel.core.governor import Governor
from smart_panel.core.http import HttpFetcher
from smart_panel.core.icons import IconRegistry
from smart_panel.core.layout import Layout, Overlay, Region
from smart_panel.core.logging import RunnerLog
from smart_panel.core.plugins import AlertRouter, PluginLoader
from smart_panel.core.scheduler import Scheduler
from smart_panel.core.secrets import Secrets
from smart_panel.core.sinks import FileSink, HidSink, NullSink, SinkState
from smart_panel.core.store import Store, open_instance_db
from smart_panel.core.workers import InstanceState, RenderWorker, bind_alert_router
from smart_panel.sdk.context import FrameContext, InstanceContext, SystemClock

__all__ = ["FileClock", "Runner", "RunnerConflict", "RunnerError"]

HOUSEKEEPING_S = 0.25
_DEFAULT_FRAME_WINDOW = 120


class RunnerError(RuntimeError):
    """A runner failure; :attr:`code` is the control error code to report."""

    code = "internal"
    exit_code = 1


class RunnerConflict(RunnerError):
    """Another runner already holds the single-instance lock."""

    code = "conflict"
    exit_code = 6


class FileClock:
    """A clock read from a file, for subprocess integration tests (T051).

    The file holds either one float (monotonic seconds; wall time is a fixed
    epoch plus it) or two whitespace/comma-separated floats (monotonic, wall).
    A missing or unreadable file keeps the last value, so a test can create the
    file lazily. This clock is only ever constructed for an explicit
    ``clock_file`` when ``SMART_PANEL_TESTING=1``.
    """

    EPOCH = 1_700_000_000.0

    def __init__(self, path: str | os.PathLike[str], *, epoch: float = EPOCH) -> None:
        self.path = Path(path)
        self._epoch = float(epoch)
        self._monotonic = 0.0
        self._wall = self._epoch

    def _refresh(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        parts = raw.replace(",", " ").split()
        if not parts:
            return
        try:
            self._monotonic = float(parts[0])
        except ValueError:
            return
        if len(parts) > 1:
            try:
                self._wall = float(parts[1])
                return
            except ValueError:
                pass
        self._wall = self._epoch + self._monotonic

    def monotonic(self) -> float:
        self._refresh()
        return self._monotonic

    def time(self) -> float:
        self._refresh()
        return self._wall


class Runner:
    """The long-lived status-display process.

    Args:
        config: The :class:`~smart_panel.core.config.Config` (or a plain mapping)
            to drive; defaults to ``<home>/config.json``.
        sink: A pre-built sink (tests inject one); otherwise built from the
            config's ``display.sink``.
        clock: Clock used for timers and status; defaults to :class:`SystemClock`.
        log: Structured logger; defaults to :class:`RunnerLog`.
        loader: Plugin loader; defaults to a fresh :class:`PluginLoader`.
        governor: A governor; defaults to one driven by the config's fps.
        clock_file: Test-only fake-clock file (honored only under
            ``SMART_PANEL_TESTING=1``).
        frame_interval_s: Override the frame period (tests); defaults to
            ``1 / target_fps``.
    """

    def __init__(
        self,
        config: Config | Mapping[str, Any] | None = None,
        *,
        sink: Any = None,
        clock: Any = None,
        log: Any = None,
        loader: PluginLoader | None = None,
        governor: Governor | None = None,
        clock_file: str | os.PathLike[str] | None = None,
        frame_interval_s: float | None = None,
    ) -> None:
        if config is None:
            self._config = Config()
        elif isinstance(config, Config):
            self._config = config
        else:
            self._config = Config()
            self._config._data = validate_config(config)  # noqa: SLF001 - adopt in-memory config

        self._clock = self._resolve_clock(clock, clock_file)
        self._log = log
        self._loader = loader
        self._provided_governor = governor
        self._provided_sink = sink
        self._frame_interval_s = frame_interval_s

        self._config_data: dict[str, Any] = {}
        self._sink: Any = None
        self._sink_kind: str | None = None
        self._compositor: Compositor | None = None
        self._encoder = Encoder()
        self._scheduler: Scheduler | None = None
        self._governor: Governor | None = None
        self._store: Store | None = None
        self._bus: Bus | None = None
        self._fonts: FontRegistry | None = None
        self._icons: IconRegistry | None = None
        self._secrets: Secrets | None = None
        self._http: HttpFetcher | None = None
        self._control: ControlServer | None = None
        self._ingest: SpoolIngest | None = None
        self._dispatch: Any = None

        self._workers: dict[str, RenderWorker] = {}
        self._instances: dict[str, Any] = {}
        self._last_images: dict[str, Any] = {}
        # ``ctx.alert`` is routed generically to whichever instance's manifest
        # declares ``alert_sink``; the core names no plugin (T132). The lambda
        # reads the live instance map, so a config change is picked up at once.
        self._alert_router = AlertRouter(lambda: self._instances)

        self._stop = threading.Event()
        self._stop_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._slot_lock = threading.Lock()
        self._slot_event = threading.Event()
        self._pending: Any = None
        self._pending_at: float | None = None

        self._state = "stopped"
        self._started_at: float | None = None
        self._frame_index = 0
        self._frames_sent = 0
        # The first frame is held back until a pass carries region content (see
        # ``_release_first_frame``); until then the compositor canvas is the bare
        # background because no worker has produced an image yet.
        self._first_frame_published = False
        self._frame_stamps: deque[float] = deque(maxlen=_DEFAULT_FRAME_WINDOW)
        self._last_bytes: int | None = None
        self._reconnects = 0
        self._last_panel_state: SinkState | None = None
        self._panel_message: str | None = None
        self._lock_fd: int | None = None

    # ------------------------------------------------------------- properties

    @property
    def config(self) -> Config:
        return self._config

    @property
    def state(self) -> str:
        """``stopped``, ``starting``, ``running`` or ``stopping``."""
        return self._state

    @property
    def stopping(self) -> bool:
        """Whether a stop has been requested (used by ``logs.tail --follow``)."""
        return self._stop.is_set()

    @property
    def log(self) -> Any:
        return self._log

    @property
    def secrets(self) -> Secrets | None:
        return self._secrets

    @property
    def loader(self) -> PluginLoader | None:
        return self._loader

    @property
    def sink(self) -> Any:
        return self._sink

    @property
    def governor(self) -> Governor | None:
        return self._governor

    @property
    def started(self) -> bool:
        return self._state == "running"

    @property
    def config_data(self) -> dict[str, Any]:
        """A defensive copy of the live config document."""
        return copy.deepcopy(self._config_data)

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Prepare the runtime, take the lock and start the threads."""
        if self._state in ("running", "starting"):
            raise RunnerError("the runner is already started")
        self._prepare()
        self._install_signal_handlers()
        self._started_at = time.monotonic()
        self._state = "running"
        self._start_threads()

    def run(self) -> int:
        """Start and block until stopped (the service entry point)."""
        self.start()
        self._stop.wait()
        self.stop()
        return 0

    def request_stop(self, timeout_s: float | None = None) -> None:
        """Ask the run loop to stop (safe to call from the control thread)."""
        self._stop.set()

    def stop(self, timeout: float | None = 5.0) -> None:
        """Stop the threads and release the single-instance lock.

        Idempotent and safe to call from several places at once (the ``run``
        loop, the supervisor thread, and a test's teardown).
        """
        with self._stop_lock:
            if self._state == "stopped":
                return
            self._state = "stopping"
            self._stop.set()
            self._slot_event.set()
            self._teardown(timeout)

    def _teardown(self, timeout: float | None) -> None:
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []
        for worker in list(self._workers.values()):
            try:
                worker.stop(timeout=1.0)
            except Exception:  # pragma: no cover - stop must never raise
                pass
        for plugin in list(self._instances.values()):
            try:
                plugin.stop()
            except Exception:  # pragma: no cover - stop must never raise
                pass
        self._workers.clear()
        self._instances.clear()
        if self._control is not None:
            self._control.stop()
            self._control = None
        if self._scheduler is not None:
            self._scheduler.stop()
        if self._sink is not None:
            try:
                self._sink.close()
            except Exception:  # pragma: no cover - close must never raise
                pass
        self._release_lock()
        self._state = "stopped"

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        """The aggregated ``runner.status`` payload (``contracts/cli.md``)."""
        sink_state = self._sink.state() if self._sink is not None else None
        panel_state = str(sink_state.state) if sink_state is not None else SinkState.DISCONNECTED
        layout = self._layout_dict()
        return {
            "state": self._state,
            "uptime_s": self._uptime(),
            "pid": os.getpid(),
            "version": __version__,
            "panel": {
                "state": panel_state,
                "sink": self._sink_kind,
                "last_error": sink_state.last_error if sink_state is not None else None,
                "reconnects": self._reconnects,
                # A human hint; the machine state is ``panel.state``.
                "message": self._panel_message,
            },
            "frames": {
                "fps": self._measured_fps(),
                "target_fps": self._governor.target_fps
                if self._governor is not None
                else self._config_data.get("display", {}).get("fps", 15),
                "governor_level": self._governor.level if self._governor is not None else 0,
                "last_bytes": self._last_bytes,
                "quality": self._encoder.last_quality,
                "sent": self._frames_sent,
            },
            "layout": layout,
            "plugins": {
                name: {
                    "state": str(worker.state),
                    "last_error": worker.last_error,
                    # ``plugin-sdk.md``: ``describe_state`` is the health summary
                    # ``status`` and ``plugin describe`` report (T087).
                    "detail": self._plugin_detail(name),
                }
                for name, worker in self._workers.items()
            },
            "spool": {"pending": self._ingest.pending_count() if self._ingest is not None else 0},
            "errors": self._recent_errors(),
        }

    # ----------------------------------------------------------- live config

    def set_config(self, dotted: str, value: Any) -> dict[str, Any]:
        """Validate, apply live and persist one dotted-path edit (FR-006/FR-007).

        Raises:
            SettingError: The candidate (the whole document) does not conform;
                the live config is left untouched.
            SettingsRejected: A plugin refused the change; the previous config
                stays in effect.
            OSError: The candidate could not be written; the live apply is
                rolled back so the last good configuration stays in effect.
        """
        previous = self._config_data
        candidate_raw = copy.deepcopy(previous)
        _assign(candidate_raw, dotted, value)
        candidate = validate_config(candidate_raw)
        self._apply_live(previous, candidate)
        self._persist(previous, candidate)
        self._config_data = candidate
        return {"path": dotted, "value": _resolve(candidate, dotted), "applied": True}

    def unset_config(self, dotted: str) -> dict[str, Any]:
        """Remove a dotted path, apply the schema default live and persist."""
        previous = self._config_data
        candidate_raw = copy.deepcopy(previous)
        _remove(candidate_raw, dotted)
        candidate = validate_config(candidate_raw)
        self._apply_live(previous, candidate)
        self._persist(previous, candidate)
        self._config_data = candidate
        return {"path": dotted, "unset": True, "applied": True}

    def reload(self) -> dict[str, Any]:
        """Re-read ``config.json``, apply it live, keep the last good on error."""
        previous = self._config_data
        candidate = self._config.load()
        self._apply_live(previous, candidate)
        self._config_data = candidate
        return {"reloaded": True, "version": candidate.get("version")}

    def set_brightness(self, value: Any) -> dict[str, Any]:
        """Validate and apply a backlight brightness (FR-011) and persist it."""
        previous = self._config_data
        candidate_raw = copy.deepcopy(previous)
        candidate_raw.setdefault("display", {})["brightness"] = value
        candidate = validate_config(candidate_raw)
        self._apply_live(previous, candidate)
        self._persist(previous, candidate)
        self._config_data = candidate
        return {"value": candidate["display"]["brightness"], "applied": True}

    def _persist(self, previous: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
        """Write ``candidate`` to disk, rolling the live apply back on failure.

        The change is applied live *before* it is persisted (a plugin may refuse
        it, and the apply order matters), so a persist failure must undo the live
        apply — otherwise the sink, governor or layout would follow a
        configuration that is not the last good one (FR-006).
        """
        try:
            self._config.save(candidate)
        except Exception as exc:
            try:
                self._apply_live(candidate, previous)
            except Exception as rollback_exc:  # pragma: no cover - best effort
                self._log.warning("config_rollback_failed", error=str(rollback_exc))
            self._log.warning("config_save_failed", error=str(exc))
            raise

    def brightness(self) -> int:
        return int(self._config_data.get("display", {}).get("brightness", 50))

    def preview_png(self) -> str:
        """Render the current composed canvas to a base64 PNG (``display.preview``)."""
        if self._compositor is None:
            raise RunnerError("the runner is not started")
        canvas = self._compositor.composite()
        buffer = io.BytesIO()
        canvas.convert("RGB").save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    # ------------------------------------------------------------- plugin call

    def call_plugin(self, instance: str, verb: str, args: Mapping[str, Any] | None = None) -> Any:
        """Route a ``plugin.call`` to an instance's ``handle_command`` (T052)."""
        plugin = self._instances.get(instance)
        if plugin is None:
            raise protocol.ProtocolError("not_found", f"no plugin instance {instance!r}")
        result = plugin.handle_command(verb, dict(args or {}))
        return result if isinstance(result, dict) else {"result": result}

    def plugin_instances(self) -> dict[str, Any]:
        """Configured instances and their health, for ``plugin instance list``."""
        configured = self._config_data.get("plugins", {}) or {}
        result: dict[str, Any] = {}
        for name, instance in configured.items():
            worker = self._workers.get(name)
            result[name] = {
                "plugin": instance.get("plugin"),
                "enabled": bool(instance.get("enabled", True)),
                "state": str(worker.state) if worker is not None else "unloaded",
                "last_error": worker.last_error if worker is not None else None,
                "settings": dict(instance.get("settings", {})),
            }
        return result

    # ------------------------------------------------------------ control bits

    def policy(self) -> access.Policy:
        """The resolved access policy from the live config."""
        return access.Policy.from_config(
            self._config_data.get("access", {}) or {}, runner_uid=os.geteuid()
        )

    def access_list(self) -> dict[str, Any]:
        section = self._config_data.get("access", {}) or {}
        return {
            "allowed_users": list(section.get("allowed_users", []) or []),
            "allowed_group": section.get("allowed_group"),
            "runner_uid": os.geteuid(),
        }

    # ------------------------------------------------------------------ prepare

    def _resolve_clock(self, clock: Any, clock_file: str | os.PathLike[str] | None) -> Any:
        if clock is not None:
            return clock
        if clock_file and os.environ.get("SMART_PANEL_TESTING") == "1":
            return FileClock(clock_file)
        return SystemClock()

    def _prepare(self) -> None:
        self._state = "starting"
        if self._log is None:
            self._log = RunnerLog()
        paths.ensure_runtime()  # raises RuntimeOwnershipError for a foreign uid
        self._acquire_lock()

        self._config_data = self._config.load()
        self._secrets = Secrets(redactor=self._log.redactor)
        # ``ctx.http`` (T080): the one bounded way a plugin reaches the network.
        # It resolves ${secret:NAME} headers through the secrets store and redacts
        # them from every error message (FR-049).
        self._http = HttpFetcher(secret_resolver=self._resolve_secret, log=self._log)
        self._store = Store(clock=self._clock)
        self._bus = Bus(clock=self._clock)
        # fonts is contract (``contracts/plugin-sdk.md``): ``fonts.get(name, size)``
        # with the configured fallback chain. Built here so a missing font still
        # warns through the runner's log (T034).
        self._fonts = FontRegistry(notify=self._font_notify)
        # icons is contract too (``contracts/plugin-sdk.md``: the sidebar's icons);
        # built here so an unusable icon warns through the runner's log (T105).
        self._icons = IconRegistry(notify=self._icon_notify)
        if self._loader is None:
            self._loader = PluginLoader()

        self._scheduler = Scheduler(clock=self._clock)
        self._governor = self._provided_governor or Governor(
            self._scheduler,
            target_fps=self._config_data.get("display", {}).get("fps", 15),
            clock=self._clock,
            log=self._log,
        )

        if self._provided_sink is not None:
            self._sink = self._provided_sink
        else:
            self._sink = self._build_sink(self._config_data)
        self._sink_kind = self._config_data.get("display", {}).get("sink", "hid")
        self._sink.open()

        self._compositor = Compositor(_layout_from(self._config_data))
        self._build_encoder(self._config_data)
        self._sync_instances(self._config_data, {})
        self._apply_brightness(self._config_data["display"]["brightness"])

        self._dispatch = make_dispatch(self)
        self._ingest = SpoolIngest(
            self._replay_spool, policy_provider=self.policy, log=self._log
        )
        self._control = ControlServer(
            self._dispatch,
            policy_provider=self.policy,
            log=self._log,
            plugins_rev=lambda: self._loader.plugins_rev if self._loader is not None else 0,
        )
        self._control.start()

        # The spool replays before the first frame (contract control-protocol.md).
        result = self._ingest.ingest()
        if result.replayed or result.rejected:
            self._log.info(
                "spool_ingested",
                replayed=result.replayed,
                rejected=result.rejected,
                pending=result.pending,
            )

        if str(self._sink.state().state) != str(SinkState.CONNECTED):
            self._panel_message = "waiting for panel"
            self._log.info("panel_waiting", message="waiting for panel")
        # ``_last_panel_state`` stays ``None`` until the first observation, so the
        # *initial* connection is not counted as a reconnect.
        self._governor.start()

    def _build_sink(self, data: Mapping[str, Any]) -> Any:
        kind = data.get("display", {}).get("sink", "hid")
        if kind == "hid":
            return HidSink(
                brightness=data["display"]["brightness"], clock=self._clock, log=self._log
            )
        if kind == "files":
            directory = data["display"].get("files_dir") or (paths.home() / "frames")
            return FileSink(directory)
        return NullSink()

    def _build_encoder(self, data: Mapping[str, Any]) -> None:
        quality = data.get("display", {}).get("jpeg_quality", {}) or {}
        self._encoder = Encoder(
            start_quality=int(quality.get("start", 70)),
            min_quality=int(quality.get("min", 30)),
        )

    def _plugin_detail(self, name: str) -> Any:
        """The instance's ``describe_state`` summary, or ``None`` when unavailable.

        ``plugin-sdk.md`` names ``status`` as a consumer of ``describe_state``; a
        plugin whose method raises (or a not-yet-started instance) reports ``None``
        rather than failing the whole ``status`` call.
        """
        plugin = self._instances.get(name)
        if plugin is None or not hasattr(plugin, "describe_state"):
            return None
        try:
            return plugin.describe_state()
        except Exception as exc:  # pragma: no cover - a bad plugin must not break status
            return {"error": str(exc)}

    def _resolve_secret(self, name: str) -> str | None:
        """Resolve a ``${secret:NAME}`` header value, or ``None`` when it is unset."""
        if self._secrets is None:
            return None
        try:
            return self._secrets.get(name)
        except Exception:  # missing or unreadable secrets file
            return None

    def _font_notify(self, event: Any) -> None:
        """Forward a :class:`FontRegistry` warning to the structured log (T034).

        The registry emits ``{"event": "font_missing", "font": ..., "fallback":
        ...}`` when a requested font is not found; the runner logs it under the
        registry's own event name so ``status``/``logs`` show a missing font
        instead of silently drawing the fallback.
        """
        fields = dict(event) if isinstance(event, Mapping) else {"detail": event}
        name = str(fields.pop("event", "font_missing"))
        self._log.warning(name, **fields)

    def _icon_notify(self, event: Any) -> None:
        """Forward an :class:`IconRegistry` warning to the structured log (T105).

        The registry emits ``{"event": "icon_unusable", "icon": ..., "reason":
        ...}`` when an icon cannot be loaded; the runner logs it under the
        registry's own event name so ``status``/``logs`` show the generic-icon
        fallback instead of hiding a broken icon (T105).
        """
        fields = dict(event) if isinstance(event, Mapping) else {"detail": event}
        name = str(fields.pop("event", "icon_unusable"))
        self._log.warning(name, **fields)

    def _apply_brightness(self, value: Any) -> None:
        if self._sink is None:
            return
        try:
            self._sink.set_brightness(int(value))
        except Exception as exc:  # a bad brightness must not stop the runner
            self._log.warning("brightness_failed", error=str(exc), value=value)

    def _acquire_lock(self) -> None:
        lock_path = paths.runner_lock()
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(descriptor)
            raise RunnerConflict(f"another runner already holds {lock_path}") from exc
        self._lock_fd = descriptor
        try:
            paths.runner_pid().write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError:  # pragma: no cover - best effort
            pass

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover
                pass
            os.close(self._lock_fd)
            self._lock_fd = None
        try:
            paths.runner_pid().unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum: int, _frame: Any) -> None:
            self._log.info("signal", signum=signum)
            self._stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - not on the main thread
                pass

    def _start_threads(self) -> None:
        self._threads = [
            threading.Thread(target=self._compose_loop, name="compositor", daemon=True),
            threading.Thread(target=self._send_loop, name="sender", daemon=True),
            threading.Thread(target=self._housekeeping_loop, name="housekeeping", daemon=True),
        ]
        # A supervisor performs the teardown when *anything* asks the runner to
        # stop (the SIGTERM/SIGINT handler, a `runner.stop` command, run()); it
        # is not one of the loops joined by `_teardown`.
        supervisor = threading.Thread(target=self._wait_and_stop, name="supervisor", daemon=True)
        for thread in self._threads:
            thread.start()
        supervisor.start()

    def _wait_and_stop(self) -> None:
        self._stop.wait()
        self.stop()

    # ------------------------------------------------------------- the loops

    def _compose_loop(self) -> None:
        while not self._stop.is_set():
            period = self._frame_period()
            start = time.monotonic()
            try:
                self._compose_once()
            except Exception as exc:  # one bad frame must not stop the loop
                self._log.error("compose_failed", error=str(exc))
            self._stop.wait(max(0.0, period - (time.monotonic() - start)))

    def _frame_period(self) -> float:
        if self._frame_interval_s is not None:
            return float(self._frame_interval_s)
        fps = self._governor.target_fps if self._governor is not None else 15
        return 1.0 / max(1, fps)

    def _compose_once(self) -> None:
        if self._compositor is None:
            return
        self._frame_index += 1
        base = FrameContext(
            size=self._compositor.size,
            now=self._clock.monotonic(),
            wall=self._clock.time(),
            frame_index=self._frame_index,
            fonts=self._fonts,
            icons=self._icons,
            bus=self._bus,
            min_text_px=int(self._config_data.get("display", {}).get("min_text_px", 24)),
        )
        # ``delivered`` is read from the same state the compositor is given, so
        # the gate and the canvas it releases cannot disagree (a worker that
        # finishes its first render between ``set_state`` and the gate would
        # otherwise release a canvas composed before its state was known).
        delivered = True
        for name, worker in list(self._workers.items()):
            worker.submit(self._region_frame(base, worker))
            worker.check_deadlines()
            state = worker.state
            self._compositor.set_state(name, state)
            if state == InstanceState.STARTING:
                delivered = False
            image = worker.last_image
            if image is not None:
                if self._last_images.get(name) is not image:
                    self._last_images[name] = image
                    self._compositor.update(name, image)
            elif self._last_images.get(name) is not None:
                # The instance went from an image to ``Inactive`` (an overlay with
                # nothing to draw): clear its layer, so the region underneath shows
                # through again instead of the stale image (contract
                # ``plugin-sdk.md``: ``Inactive`` = "draw nothing"). Without this an
                # alert that timed out would stay on the panel forever (T073).
                self._last_images[name] = None
                self._compositor.update(name, None)
        # Read the compositor's dirty set *before* composing (composing clears
        # it): it is the "something visible moved this pass" signal the first
        # frame gate needs.
        changed = bool(self._compositor.dirty_regions())
        canvas = self._compositor.composite()
        if not self._release_first_frame(changed, delivered):
            return
        self._publish(canvas)

    def _release_first_frame(self, changed: bool, delivered: bool) -> bool:
        """Whether this pass may publish, holding the *first* frame back (T051).

        A :class:`~smart_panel.core.workers.RenderWorker` renders on its own
        thread, so the first pass after ``start`` submits a frame and reads
        ``worker.last_image`` before any worker has produced one: publishing
        then sends the bare background as the runner's first frame, and
        ``start``/``restart`` return on it (``frames.sent > 0``), so an
        immediate ``preview`` could catch an empty canvas — observed on a loaded
        macOS CI runner, where the gap to the first content frame is longer than
        the next CLI round-trip.

        The first frame is therefore held back until a pass that carries
        something visible (``changed``: a new region layer, or a
        ``degraded``/``stalled`` instance's error badge, which is never blank),
        or until every instance has delivered its first render result
        (``delivered``) — so a layout whose instances never draw (``Inactive``
        forever) still starts, bounded, and a plugin that never returns is
        bounded by its worker's hard deadline. That first frame really is the
        bare background, so it is explained in ``status`` rather than reported
        as content.
        """
        if self._first_frame_published:
            return True
        if not changed and not delivered:
            return False
        self._first_frame_published = True
        if not changed and self._workers:
            self._log_blank_first_frame()
        return True

    def _log_blank_first_frame(self) -> None:
        """Record why the first frame carries no region content (T051)."""
        plugins = {name: str(worker.state) for name, worker in self._workers.items()}
        self._log.warning(
            "first_frame_without_content",
            message=(
                "sending a first frame with no region content: no instance has "
                "drawn anything, so the canvas is the bare background until one does"
            ),
            plugins=plugins,
        )

    @staticmethod
    def _region_frame(base: FrameContext, worker: RenderWorker) -> FrameContext:
        """A frame sized to ``worker``'s region.

        The render contract (``contracts/plugin-sdk.md``) says an ``Image``
        result is "exactly ``frame.size``", and the worker validates against its
        region size — so the frame handed to a region plugin must carry that
        region size, not the whole 1920x462 canvas.
        """
        expected = worker.size
        if expected is None or (int(expected[0]), int(expected[1])) == tuple(base.size):
            return base
        return replace(base, size=(int(expected[0]), int(expected[1])))

    def _publish(self, canvas: Any) -> None:
        with self._slot_lock:
            self._pending = canvas
            self._pending_at = time.monotonic()
        self._slot_event.set()

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            self._slot_event.wait(0.05)
            with self._slot_lock:
                canvas = self._pending
                published_at = self._pending_at
                self._pending = None
                self._slot_event.clear()
            if canvas is None:
                continue
            try:
                self._send_canvas(canvas)
            except Exception as exc:  # a failed send must not stop the pump
                self._log.error("send_failed", error=str(exc))
                self._note_error(str(exc))
                continue
            finished = time.monotonic()
            if self._governor is not None:
                self._governor.record_frame(finished - (published_at or finished))
            self._frames_sent += 1
            self._frame_stamps.append(finished)
            self._track_panel_state()

    def _send_canvas(self, canvas: Any) -> None:
        if isinstance(self._sink, HidSink):
            packets = self._encoder.encode(canvas)
            self._last_bytes = self._encoder.last_jpeg_bytes
            self._sink.send(packets)
        else:
            self._sink.send(canvas)

    def _housekeeping_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(HOUSEKEEPING_S)
            if self._stop.is_set():
                break
            try:
                if self._scheduler is not None:
                    self._scheduler.run_pending()
                for worker in list(self._workers.values()):
                    worker.check_deadlines()
                if self._governor is not None:
                    self._governor.tick()
                self._track_panel_state()
            except Exception as exc:  # pragma: no cover - defensive
                self._log.error("housekeeping_failed", error=str(exc))

    def _track_panel_state(self) -> None:
        if self._sink is None:
            return
        state = self._sink.state().state
        if state == SinkState.CONNECTED and self._last_panel_state not in (
            None,
            SinkState.CONNECTED,
        ):
            self._reconnects += 1
            self._panel_message = None
            self._log.info("panel_connected", reconnects=self._reconnects)
        elif state != SinkState.CONNECTED:
            self._panel_message = "waiting for panel"
        self._last_panel_state = state

    # ---------------------------------------------------------- live apply

    def _apply_live(self, previous: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
        """Apply a validated config change; raise to keep the previous config.

        Plugin settings changes are offered first (they are the changes a plugin
        can refuse), then brightness, then the layout and the instance set.
        """
        self._sync_instances(candidate, previous)
        old_display = previous.get("display", {}) or {}
        new_display = candidate.get("display", {}) or {}
        if old_display.get("brightness") != new_display.get("brightness"):
            self._apply_brightness(new_display.get("brightness", 50))
        if old_display.get("fps") != new_display.get("fps") and self._governor is not None:
            # The configured frame rate is the governor's top rung; retarget it
            # so a live `config set display.fps` actually changes the pacing.
            self._governor.retarget(new_display.get("fps"))
        if (previous.get("layout") or {}) != (candidate.get("layout") or {}):
            if self._compositor is not None:
                self._compositor.set_layout(_layout_from(candidate))
                self._refresh_worker_sizes()
        quality = new_display.get("jpeg_quality", {}) or {}
        if (old_display.get("jpeg_quality") or {}) != quality:
            self._build_encoder(candidate)

    def _sync_instances(self, data: Mapping[str, Any], previous: Mapping[str, Any]) -> None:
        configured = data.get("plugins", {}) or {}
        before = previous.get("plugins", {}) or {}

        # 1. Offer settings changes first: a refusal must not leave half-applied
        #    additions or removals behind.
        for name, instance in configured.items():
            if not instance.get("enabled", True):
                continue
            old = before.get(name, {})
            if not old.get("enabled", True):
                continue
            new_settings = instance.get("settings", {}) or {}
            if old.get("settings", {}) != new_settings and name in self._instances:
                self._instances[name].on_settings_changed(dict(new_settings))

        # 2. Stop instances that were removed or disabled.
        for name in list(self._instances):
            instance = configured.get(name)
            if instance is None or not instance.get("enabled", True):
                self._stop_instance(name)

        # 3. Start instances that are new or were re-enabled.
        for name, instance in configured.items():
            if not instance.get("enabled", True) or name in self._instances:
                continue
            self._start_instance(name, instance)

    def _stop_instance(self, name: str) -> None:
        worker = self._workers.pop(name, None)
        plugin = self._instances.pop(name, None)
        self._last_images.pop(name, None)
        if worker is not None:
            worker.stop(timeout=1.0)
        if plugin is not None:
            try:
                plugin.stop()
            except Exception:  # pragma: no cover
                pass

    def _start_instance(self, name: str, instance: Mapping[str, Any]) -> None:
        plugin_name = instance.get("plugin")
        loaded = self._loader.loaded() if self._loader is not None else {}
        plugin_class = loaded.get(str(plugin_name))
        if plugin_class is None:
            self._log.warning(
                "instance_skipped",
                instance=name,
                plugin=plugin_name,
                reason="plugin not available",
            )
            return
        context = self._instance_context(name, plugin_class)
        plugin = plugin_class(context, dict(instance.get("settings", {}) or {}))
        worker = RenderWorker(plugin, clock=self._clock, log=self._log, size=context.region_size)
        # ``ctx.alert`` from this instance's render thread or spawned task reaches
        # the same generic router the control channel uses (T132).
        bind_alert_router(worker, self._alert_router)
        try:
            plugin.start()
        except Exception as exc:  # a failing start marks the instance failed
            worker.mark_failed(str(exc))
            self._log.error("instance_start_failed", instance=name, error=str(exc))
        self._instances[name] = plugin
        self._workers[name] = worker
        if self._compositor is not None:
            self._compositor.set_state(name, worker.state)

    def _instance_context(self, name: str, plugin_class: Any) -> InstanceContext:
        manifest = getattr(plugin_class, "manifest", None)
        scheduler = self._scheduler
        return InstanceContext(
            name=name,
            manifest=manifest,
            region_size=self._region_size_for(name),
            log=self._log,
            bus=self._bus,
            kv=self._store.kv(name) if self._store is not None else None,
            clock=self._clock,
            http=self._http,
            db_factory=lambda: open_instance_db(name),
            spawn_handler=(
                None
                if scheduler is None
                else lambda fn, interval_s, timeout_s, task_name: scheduler.spawn(
                    fn, interval_s, timeout_s, name=task_name or f"{name}-task"
                )
            ),
            notify_handler=lambda payload: self._log.info(
                "instance_notify", instance=name, payload=str(payload)
            ),
        )

    def _region_size_for(self, instance: str) -> tuple[int, int] | None:
        if self._compositor is None:
            return None
        layout = self._compositor.layout
        for region in layout.regions:
            if region.instance == instance:
                return (region.w, region.h)
        for overlay in layout.overlays:
            if overlay.instance == instance:
                for region in layout.regions:
                    if region.name == overlay.region:
                        return (region.w, region.h)
        return None

    def _refresh_worker_sizes(self) -> None:
        """Re-point each worker at its (possibly resized) region.

        A live layout change can move or resize the rectangle an instance draws
        into; the worker's expected size (used both to size the render frame and
        to validate the result) follows it.
        """
        for name, worker in self._workers.items():
            worker.set_size(self._region_size_for(name))

    def _replay_spool(self, request: protocol.Request, received_at: float) -> Any:
        """Replay one spool entry through the normal command path.

        The entry's receive time is carried to the plugin command as
        ``_received_at``: it is the only reference a plugin has for "how long
        did this wait while the runner was down" (``data-model.md``: an alert's
        ``received_at`` is the "CLI or spool receive time").
        """
        args = dict(request.args or {})
        inner = args.get("args")
        if isinstance(inner, Mapping):
            args["args"] = {**inner, "_received_at": received_at}
        response = self._dispatch(replace(request, args=args))
        if isinstance(response, protocol.Response) and not response.ok:
            error = response.error.message if response.error is not None else "replay failed"
            raise RunnerError(error)
        return response

    # ------------------------------------------------------------- reporting

    def _uptime(self) -> int:
        if self._started_at is None:
            return 0
        return int(time.monotonic() - self._started_at)

    def _measured_fps(self) -> float:
        stamps = self._frame_stamps
        if len(stamps) < 2:
            return 0.0
        span = stamps[-1] - stamps[0]
        if span <= 0:
            return 0.0
        return round((len(stamps) - 1) / span, 1)

    def _recent_errors(self) -> list[dict[str, Any]]:
        if self._log is None:
            return []
        recent = self._log.recent(50)
        errors = [event for event in recent if event.get("level") in ("error", "warning")]
        return errors[-10:]

    def _note_error(self, message: str) -> None:
        if self._log is not None:
            self._log.error("frame_error", message=message)

    def _layout_dict(self) -> dict[str, Any]:
        layout = (self._config_data.get("layout") or {})
        regions = layout.get("regions", [])
        overlays = layout.get("overlays", [])
        return {
            "regions": [
                {"name": region.get("name"), "instance": region.get("instance")}
                for region in regions
            ],
            "overlays": [
                {"region": overlay.get("region"), "instance": overlay.get("instance")}
                for overlay in overlays
            ],
        }


# ------------------------------------------------------------------- helpers


def _layout_from(data: Mapping[str, Any]) -> Layout:
    section = data.get("layout", {}) or {}
    regions = [
        Region(
            name=region["name"],
            x=int(region["x"]),
            y=int(region["y"]),
            w=int(region["w"]),
            h=int(region["h"]),
            instance=region["instance"],
        )
        for region in section.get("regions", [])
    ]
    overlays = [
        Overlay(
            region=overlay["region"],
            instance=overlay["instance"],
            z=int(overlay.get("z", 0)),
        )
        for overlay in section.get("overlays", [])
    ]
    return Layout(regions=regions, overlays=overlays)


def _resolve(data: Mapping[str, Any], dotted: str) -> Any:
    current: Any = data
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return copy.deepcopy(current)


def _assign(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node: Any = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _remove(target: dict[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    node: Any = target
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        node = node[part]
    if isinstance(node, dict):
        node.pop(parts[-1], None)


# ``InstanceState`` is re-exported so callers do not need the workers module.
_ = InstanceState
