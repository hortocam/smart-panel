"""Render and test a plugin without a panel (T031, FR-047).

``smart_panel.sdk.testing`` is part of the public SDK (contract
``plugin-sdk.md``). It provides:

* :class:`FakeClock` — deterministic monotonic and wall time so animation,
  timeout and freshness logic is testable without sleeping.
* :class:`PluginHarness` — build a plugin from its class and settings, drive
  ``render``/``handle_command``/``on_settings_changed``/``describe_state``, and
  read the last image, all against a real bus and a fake clock.
* :func:`assert_conforms` — the standard conformance suite every built-in and
  sample plugin runs in CI: manifest valid, settings validate, commands
  declared, render returns allowed types at the exact size, ``stop`` is prompt,
  and the plugin imports nothing from ``smart_panel.core``.
"""

from __future__ import annotations

import ast
import inspect
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL import ImageFont

from smart_panel.core.bus import Bus
from smart_panel.sdk.context import (
    FrameContext,
    InstanceContext,
    ReadOnlyBus,
)
from smart_panel.sdk.plugin import (
    Command,
    Inactive,
    Plugin,
    RenderResult,
    Unchanged,
)
from smart_panel.sdk.plugin import (
    Image as RenderImage,
)
from smart_panel.sdk.settings import validate

_STOP_DEADLINE_S = 2.0


class FakeClock:
    """A deterministic clock implementing the SDK ``Clock`` protocol."""

    def __init__(self, start: float = 0.0, wall: float = 1_700_000_000.0) -> None:
        self._monotonic = float(start)
        self._wall = float(wall)

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; only differences are meaningful."""
        return self._monotonic

    def time(self) -> float:
        """Wall-clock POSIX seconds."""
        return self._wall

    def advance(self, seconds: float) -> None:
        """Move both clocks forward by ``seconds`` (which may be negative)."""
        self._monotonic += float(seconds)
        self._wall += float(seconds)


class _NullLog:
    """A tiny structured logger so plugins that call ``ctx.log`` still work."""

    def debug(self, event: str, **fields: Any) -> None:
        pass

    def info(self, event: str, **fields: Any) -> None:
        pass

    def warning(self, event: str, **fields: Any) -> None:
        pass

    def error(self, event: str, **fields: Any) -> None:
        pass


class _DictKV:
    """In-memory ``ctx.kv`` for the harness (the runner wires the SQLite one)."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def keys(self) -> list[str]:
        return sorted(self._data)


class _DefaultFonts:
    """A small font resolver so a render that draws text still works headless."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, int], Any] = {}

    def get(self, name: str | None = None, size: int = 12) -> Any:
        key = (str(name), int(size))
        if key not in self._cache:
            self._cache[key] = ImageFont.load_default()
        return self._cache[key]


class _DefaultIcons:
    """A 1:1 placeholder icon resolver for the harness."""

    def get(self, name: str | None = None, size: int = 64) -> Any:
        return PILImage.new("RGBA", (int(size), int(size)), (0, 0, 0, 0))


class PluginHarness:
    """Drive one plugin instance without a panel.

    Args:
        plugin_class: The plugin subclass to instantiate.
        settings: Partial settings; validated (defaults filled) before use.
        size: Region size for the render ``FrameContext``.
        clock: Clock to use (a fresh :class:`FakeClock` by default).
    """

    def __init__(
        self,
        plugin_class: type[Plugin],
        settings: dict[str, Any] | None = None,
        size: tuple[int, int] = (1920, 462),
        clock: Any = None,
    ) -> None:
        self.plugin_class = plugin_class
        self.manifest = plugin_class.manifest
        self.size = (int(size[0]), int(size[1]))
        self.clock = clock if clock is not None else FakeClock()
        self.bus = Bus(clock=self.clock)
        self.log = _NullLog()
        self.alerts: list[dict[str, Any]] = []
        self.validated_settings = validate(self.manifest.settings, settings or {})

        self.ctx = InstanceContext(
            name="harness",
            manifest=self.manifest,
            region_size=self.size,
            log=self.log,
            bus=self.bus,
            kv=_DictKV(),
            clock=self.clock,
            alert_handler=self._record_alert,
        )
        self.plugin: Plugin = plugin_class(self.ctx, self.validated_settings)
        self._image: PILImage.Image | None = None
        self._frame_index = 0
        self.plugin.start()

    def _record_alert(self, **kwargs: Any) -> dict[str, Any]:
        self.alerts.append(kwargs)
        return {"state": "queued"}

    def frame_context(self) -> FrameContext:
        """Build the next ``FrameContext`` and advance the frame counter."""
        self._frame_index += 1
        return FrameContext(
            size=self.size,
            now=self.clock.monotonic(),
            wall=self.clock.time(),
            frame_index=self._frame_index,
            fonts=_DefaultFonts(),
            icons=_DefaultIcons(),
            bus=ReadOnlyBus(self.bus),
            min_text_px=24,
        )

    def render(self, advance_s: float = 0.0) -> PILImage.Image | None:
        """Render once, returning the current image (or ``None`` when inactive)."""
        if advance_s:
            self.clock.advance(advance_s)
        result = self.plugin.render(self.frame_context())
        return self._image_from(result)

    def _image_from(self, result: RenderResult) -> PILImage.Image | None:
        if isinstance(result, RenderImage):
            self._image = result.image
            return self._image
        if isinstance(result, Unchanged):
            return self._image
        return None  # Inactive: an overlay draws nothing

    @property
    def image(self) -> PILImage.Image | None:
        """The last image produced by ``render``."""
        return self._image

    def command(self, name: str, **args: Any) -> dict[str, Any]:
        """Invoke a plugin command and return its result dict."""
        return self.plugin.handle_command(name, args)

    def set_settings(self, **settings: Any) -> dict[str, Any]:
        """Validate and apply a settings change (raises ``SettingsRejected`` to refuse)."""
        self.validated_settings = validate(self.manifest.settings, settings)
        self.plugin.on_settings_changed(self.validated_settings)
        return self.validated_settings

    def state(self) -> dict[str, Any]:
        """The plugin's ``describe_state`` summary."""
        return self.plugin.describe_state()

    def stop(self) -> None:
        """Stop the plugin (cancels background work)."""
        self.plugin.stop()


def assert_conforms(plugin_class: type[Plugin]) -> None:
    """Run the standard conformance suite against ``plugin_class``.

    Raises:
        AssertionError: On the first failed check (with an explanatory message).
    """
    assert isinstance(plugin_class, type) and issubclass(plugin_class, Plugin), (
        f"{plugin_class!r} is not a Plugin subclass"
    )
    manifest = getattr(plugin_class, "manifest", None)
    assert manifest is not None, f"{plugin_class.__name__} has no manifest"
    try:
        manifest.validate()
    except ValueError as exc:
        raise AssertionError(f"manifest is invalid: {exc}") from exc

    try:
        validate(manifest.settings, {})
    except ValueError as exc:
        raise AssertionError(f"settings do not validate: {exc}") from exc

    seen: set[str] = set()
    for command in manifest.commands:
        assert isinstance(command, Command), f"command {command!r} is not a Command"
        assert command.name, "a command must be named"
        assert command.name not in seen, f"duplicate command {command.name!r}"
        seen.add(command.name)

    harness = PluginHarness(plugin_class, size=(320, 240))
    try:
        result = harness.plugin.render(harness.frame_context())
        assert isinstance(result, (Unchanged, Inactive, RenderImage)), (
            f"render returned {type(result).__name__}, not a RenderResult"
        )
        if isinstance(result, RenderImage):
            assert result.image.size == harness.size, (
                f"render returned a {result.image.size} image for a {harness.size} region"
            )
        started = time.monotonic()
        harness.plugin.stop()
        assert time.monotonic() - started < _STOP_DEADLINE_S, "stop() exceeded 2 s"
    finally:
        harness.plugin.stop()

    _assert_no_core_imports(plugin_class)


def _assert_no_core_imports(plugin_class: type[Plugin]) -> None:
    source_file = None
    module = sys.modules.get(getattr(plugin_class, "__module__", ""))
    if module is not None:
        source_file = getattr(module, "__file__", None)
    if source_file is None:
        try:
            source_file = inspect.getfile(plugin_class)
        except (TypeError, OSError):  # pragma: no cover - exec-defined classes
            return
    path = Path(source_file)
    if not path.exists():
        return
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            if module == "smart_panel.core" or module.startswith("smart_panel.core."):
                raise AssertionError(
                    f"{plugin_class.__name__} imports {module!r}; plugins may import only "
                    "smart_panel.sdk (FR-043)"
                )
