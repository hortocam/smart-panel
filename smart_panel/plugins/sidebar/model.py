"""The sidebar model: one entry and the instance's entry set (T106, US5).

Shapes come from ``data-model.md`` (Sidebar Entry) and ``contracts/builtin-plugins.md``
(``sidebar``): ``id`` (unique in the instance), ``icon`` (a bundled icon name or a
path to a PNG), ``label`` (at most 12 characters, optional), ``topic`` (a bus topic
carrying an integer count), ``hide_when_zero`` (default true), ``max_display``
(default 999 — a larger count shows as ``999+``) and ``stale_after_s``
(``int | null``).

An instance holds **at most 6 entries including the built-in alert entry**
(FR-037). The alert entry is implicit: :func:`alert_entry` builds it, it is bound
to the ``alerts.missed`` topic, and it does not count against the configurable
list — a sixth *configured* entry would make seven, so :class:`SidebarEntrySet`
refuses it with a message naming the maximum.

The layout arithmetic (:func:`entry_height`) is here too, because it is what makes
the six-entry limit a real constraint: the sidebar is 462 px tall and every entry
must be at least 72 px.

Validation is structural only (``data-model.md``): whether an icon *file* is
readable is decided when it is resolved for a frame (``core/icons.py``), and
whether an icon *name* is known is decided by the ``entry add``/``entry set``
verbs, so a config file stays schema-shaped and a bad icon shows the generic icon
with a warning rather than refusing the whole instance.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ALERT_ENTRY_ID",
    "ALERT_ICON",
    "ALERT_TOPIC",
    "BUNDLED_ICONS",
    "DEFAULT_MAX_DISPLAY",
    "DEFAULT_STALE_TTL_S",
    "LABEL_MAX_CHARS",
    "MAX_ENTRIES",
    "MIN_ENTRY_HEIGHT_PX",
    "SEVERITY_COLORS",
    "STALE_TEXT",
    "SidebarEntry",
    "SidebarEntrySet",
    "SidebarError",
    "alert_entry",
    "entry_height",
]

# The bundled icon names (``core/icons.py`` discovers the same set from
# ``smart_panel/assets/icons/``; a contract test asserts the two agree).
BUNDLED_ICONS: tuple[str, ...] = (
    "alert",
    "bell",
    "calendar",
    "chat",
    "check",
    "cloud",
    "clock",
    "git",
    "home",
    "mail",
    "server",
    "shield",
)

MAX_ENTRIES = 6
LABEL_MAX_CHARS = 12
DEFAULT_MAX_DISPLAY = 999
DEFAULT_STALE_TTL_S = 300
MIN_ENTRY_HEIGHT_PX = 72
STALE_TEXT = "?"

ALERT_ENTRY_ID = "alert"
ALERT_ICON = "alert"
ALERT_TOPIC = "alerts.missed"

# The missed-alert severity colours, the same palette as the ``alerts`` plugin
# (``data-model.md``); a built-in cannot import a sibling plugin, so the table is
# repeated here.
SEVERITY_COLORS: dict[str, str] = {
    "info": "#2E7DD7",
    "warning": "#E6A700",
    "critical": "#D62839",
}

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class SidebarError(ValueError):
    """An entry, or the entry set, did not conform."""


def entry_height(entry_count: int, region_height: int) -> int:
    """The height of one entry row in a ``region_height``-tall sidebar.

    The region is divided evenly, so six entries in the default 462 px sidebar
    are 77 px each — comfortably above the 72 px floor.
    """
    count = max(1, int(entry_count))
    return max(MIN_ENTRY_HEIGHT_PX, int(region_height) // count)


def _text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise SidebarError(f"{what} must be a string, got {value!r}")
    if len(value) > limit:
        raise SidebarError(f"{what} must be at most {limit} characters, got {len(value)}")
    return value


@dataclass(frozen=True)
class SidebarEntry:
    """One configured sidebar entry."""

    id: str
    icon: str
    topic: str
    label: str = ""
    hide_when_zero: bool = True
    max_display: int = DEFAULT_MAX_DISPLAY
    stale_after_s: int | None = None

    # ---------------------------------------------------------------- parsing

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SidebarEntry:
        """Build and validate an entry from a config, CLI or ``to_dict`` mapping.

        Raises:
            SidebarError: A field is missing or out of its documented range.
        """
        data = dict(raw or {})
        identifier = str(data.get("id") or "").strip()
        if not _ID_PATTERN.match(identifier):
            raise SidebarError(
                "a sidebar entry needs an 'id' of 1-32 characters "
                "([A-Za-z0-9._-], starting alphanumeric)"
            )
        icon = str(data.get("icon") or "").strip()
        if not icon:
            raise SidebarError(f"sidebar entry {identifier!r} needs an 'icon'")
        topic = str(data.get("topic") or "").strip()
        if not _TOPIC_PATTERN.match(topic):
            raise SidebarError(f"sidebar entry {identifier!r} needs a 'topic' to read")
        label = _text(
            data.get("label") or identifier, f"sidebar entry {identifier!r} label", LABEL_MAX_CHARS
        )

        hide_when_zero = data.get("hide_when_zero")
        if hide_when_zero is None:
            hide_when_zero = True
        if not isinstance(hide_when_zero, bool):
            raise SidebarError(
                f"sidebar entry {identifier!r} hide_when_zero must be a boolean, "
                f"got {hide_when_zero!r}"
            )

        max_display = data.get("max_display")
        if max_display is None:
            max_display = DEFAULT_MAX_DISPLAY
        if isinstance(max_display, bool) or not isinstance(max_display, int):
            raise SidebarError(
                f"sidebar entry {identifier!r} max_display must be an integer, "
                f"got {max_display!r}"
            )
        if max_display < 1:
            raise SidebarError(
                f"sidebar entry {identifier!r} max_display must be at least 1, "
                f"got {max_display}"
            )

        stale = data.get("stale_after_s")
        if stale is None:
            stale_after: int | None = None
        else:
            if isinstance(stale, bool) or not isinstance(stale, int):
                raise SidebarError(
                    f"sidebar entry {identifier!r} stale_after_s must be an integer, "
                    f"got {stale!r}"
                )
            stale_after = int(stale)
            if stale_after < 1:
                raise SidebarError(
                    f"sidebar entry {identifier!r} stale_after_s must be at least 1, "
                    f"got {stale_after}"
                )

        return cls(
            id=identifier,
            icon=icon,
            topic=topic,
            label=label,
            hide_when_zero=hide_when_zero,
            max_display=max_display,
            stale_after_s=stale_after,
        )

    def to_dict(self) -> dict[str, Any]:
        """The JSON shape stored in the config and reported by ``entry list``."""
        return {
            "id": self.id,
            "icon": self.icon,
            "label": self.label,
            "topic": self.topic,
            "hide_when_zero": self.hide_when_zero,
            "max_display": self.max_display,
            "stale_after_s": self.stale_after_s,
        }

    # ------------------------------------------------------------- behaviour

    def display_count(self, count: int) -> str:
        """``count`` as text: ``999+`` above :attr:`max_display` (FR-040)."""
        number = int(count)
        if number > self.max_display:
            return f"{self.max_display}+"
        return str(number)


@dataclass
class SidebarEntrySet:
    """The configured entries of one instance, with the 6-entry limit enforced."""

    entries: list[SidebarEntry] = field(default_factory=list)

    @classmethod
    def from_settings(cls, raw: Any) -> SidebarEntrySet:
        """Build the set from an ``entries`` setting value.

        Raises:
            SidebarError: An entry is invalid, an id repeats, or the list plus the
                built-in alert entry would exceed :data:`MAX_ENTRIES`.
        """
        if raw is None:
            return cls()
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise SidebarError(f"entries must be a list of entry objects, got {raw!r}")
        _check_capacity(len(raw))
        parsed = cls()
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise SidebarError(f"every entry must be an object, got {entry!r}")
            parsed.add(SidebarEntry.from_dict(entry))
        return parsed

    def __len__(self) -> int:
        """The number of configured entries (excluding the built-in alert entry)."""
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def ids(self) -> list[str]:
        return [entry.id for entry in self.entries]

    def get(self, entry_id: str) -> SidebarEntry | None:
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def add(self, entry: SidebarEntry) -> SidebarEntry:
        """Append ``entry``, refusing a duplicate id or a seventh entry."""
        if self.get(entry.id) is not None:
            raise SidebarError(f"sidebar entry {entry.id!r} already exists")
        _check_capacity(len(self.entries) + 1)
        self.entries.append(entry)
        return entry

    def replace(self, entry_id: str, entry: SidebarEntry) -> SidebarEntry:
        """Replace the entry named ``entry_id`` with ``entry``."""
        for index, existing in enumerate(self.entries):
            if existing.id == entry_id:
                other = self.get(entry.id)
                if entry.id != entry_id and other is not None:
                    raise SidebarError(f"sidebar entry {entry.id!r} already exists")
                self.entries[index] = entry
                return entry
        raise SidebarError(f"no sidebar entry named {entry_id!r}")

    def remove(self, entry_id: str) -> SidebarEntry:
        for index, existing in enumerate(self.entries):
            if existing.id == entry_id:
                return self.entries.pop(index)
        raise SidebarError(f"no sidebar entry named {entry_id!r}")

    def to_list(self) -> list[dict[str, Any]]:
        return [entry.to_dict() for entry in self.entries]


def _check_capacity(configured: int) -> None:
    """Refuse a ``configured`` entry count that would exceed the entry limit."""
    if int(configured) + 1 > MAX_ENTRIES:
        raise SidebarError(
            f"a sidebar holds at most {MAX_ENTRIES} entries including the built-in "
            f"alert entry; the maximum is {MAX_ENTRIES}, so no further entry can be added"
        )


def alert_entry() -> SidebarEntry:
    """The implicit alert entry (FR-041): hidden at zero, coloured by severity."""
    return SidebarEntry(
        id=ALERT_ENTRY_ID,
        icon=ALERT_ICON,
        topic=ALERT_TOPIC,
        label="Alerts",
        hide_when_zero=True,
        max_display=DEFAULT_MAX_DISPLAY,
    )
