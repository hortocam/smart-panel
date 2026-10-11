"""The public plugin API: manifest, lifecycle, render results and errors (T018).

This module (with :mod:`smart_panel.sdk.settings` and
:mod:`smart_panel.sdk.context`) is the *only* API a plugin may import from this
project — built-ins included (contract ``plugin-sdk.md``, FR-043). Nothing here
imports :mod:`smart_panel.core`, so a plugin can depend on the SDK alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from smart_panel.sdk.settings import Setting

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL import Image as PILImage

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
CAPABILITIES = frozenset({"region", "overlay", "datasource", "alert_source", "alert_sink"})
SDK_VERSION = "1"


class SettingsRejected(Exception):
    """Raised by ``on_settings_changed`` to refuse a validated change."""

    def __init__(self, message: str, setting: str | None = None) -> None:
        self.message = message
        self.setting = setting
        super().__init__(message)


class CommandError(Exception):
    """An expected failure from ``handle_command`` (mapped to a CLI error)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Command:
    """A CLI verb contributed by a plugin."""

    name: str
    summary: str = ""
    args: tuple[Setting, ...] = ()
    spoolable: bool = False
    mutates_config: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.args, tuple):
            object.__setattr__(self, "args", tuple(self.args))


@dataclass(frozen=True)
class SourceType:
    """A data-source kind contributed by a ``datasource`` plugin."""

    name: str
    settings: tuple[Setting, ...] = ()
    metrics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.settings, tuple):
            object.__setattr__(self, "settings", tuple(self.settings))
        if not isinstance(self.metrics, tuple):
            object.__setattr__(self, "metrics", tuple(self.metrics))


@dataclass
class Manifest:
    """Declarative description of a plugin, read by the loader and the CLI."""

    name: str
    version: str = "0.1.0"
    sdk_version: str = SDK_VERSION
    summary: str = ""
    capabilities: frozenset[str] = field(default_factory=frozenset)
    settings: tuple[Setting, ...] = ()
    commands: tuple[Command, ...] = ()
    source_types: tuple[SourceType, ...] = ()
    animated: bool = False
    min_size: tuple[int, int] | None = None
    max_size: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, frozenset):
            self.capabilities = frozenset(self.capabilities)
        if not isinstance(self.settings, tuple):
            self.settings = tuple(self.settings)
        if not isinstance(self.commands, tuple):
            self.commands = tuple(self.commands)
        if not isinstance(self.source_types, tuple):
            self.source_types = tuple(self.source_types)

    def validate(self) -> None:
        """Validate the manifest in place; raise ``ValueError`` on any problem.

        The loader (T032) and ``sdk.testing.assert_conforms`` (T031) call this.
        """
        if not NAME_PATTERN.match(self.name):
            raise ValueError(
                f"plugin name {self.name!r} must match [a-z][a-z0-9_-]{{1,31}}"
            )
        unknown = set(self.capabilities) - CAPABILITIES
        if unknown:
            raise ValueError(f"manifest declares unknown capabilities {sorted(unknown)}")
        if not isinstance(self.sdk_version, str) or not self.sdk_version:
            raise ValueError("sdk_version must be a non-empty string")
        for command in self.commands:
            if not isinstance(command, Command):
                raise ValueError(f"commands must be Command instances, got {command!r}")
        for source_type in self.source_types:
            if not isinstance(source_type, SourceType):
                raise ValueError(f"source_types must be SourceType instances, got {source_type!r}")
        for size in (self.min_size, self.max_size):
            if size is not None and (len(size) != 2 or not all(isinstance(v, int) for v in size)):
                raise ValueError(f"size limits must be (width, height) int pairs, got {size!r}")


class RenderResult:
    """Base class for the three render outcomes."""

    __slots__ = ()


@dataclass(frozen=True)
class Unchanged(RenderResult):
    """Reuse the previous image for this region (the cheap, static path)."""


@dataclass(frozen=True)
class Inactive(RenderResult):
    """Overlay only: draw nothing this frame."""


@dataclass(frozen=True)
class Image(RenderResult):
    """A new image for the region, exactly ``frame.size``."""

    image: PILImage.Image


class Plugin:
    """Base class a plugin subclasses and declares ``manifest`` on.

    Lifecycle: ``load -> __init__(ctx, settings) -> start() ->
    [render()/handle_command()/on_settings_changed()]* -> stop()``.
    """

    manifest: Manifest

    def __init__(self, ctx: Any, settings: dict[str, Any]) -> None:
        self.ctx = ctx
        self.settings: dict[str, Any] = dict(settings or {})

    def start(self) -> None:
        """Start background work (through ``ctx.spawn`` only). Default: no-op."""

    def stop(self) -> None:
        """Stop background work; must return within 2 s. Default: no-op."""

    def on_settings_changed(self, settings: dict[str, Any]) -> None:
        """Accept a validated settings change; raise ``SettingsRejected`` to refuse."""
        self.settings = dict(settings)

    def render(self, frame: Any) -> RenderResult:
        """Return this region's image. Default: reuse the previous image."""
        return Unchanged()

    def handle_command(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Handle a CLI verb; raise ``CommandError`` for expected failures."""
        raise CommandError("invalid_input", f"unknown command {name!r}")

    def describe_state(self) -> dict[str, Any]:
        """Small JSON-compatible health summary; never contains secrets."""
        return {}
