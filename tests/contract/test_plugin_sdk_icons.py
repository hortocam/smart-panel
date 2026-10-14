"""The plugin-sdk contract must document the icon accessor the built-ins use (T105).

``contracts/plugin-sdk.md`` is where a plugin author reads what ``FrameContext``
provides. The built-in sidebar reads ``frame.icons.get_with_status(name, size)`` to
learn whether it drew the generic fallback, so that accessor is SDK surface: these
tests keep the contract and the registry the runner injects in step, so a built-in
cannot quietly lean on an undocumented method again.
"""

from __future__ import annotations

from pathlib import Path

from smart_panel.core.icons import IconRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "specs" / "001-status-display-platform" / "contracts" / "plugin-sdk.md"


def test_the_contract_documents_the_icon_accessors() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    assert "icons.get(" in text, "the contract must document icons.get"
    assert "get_with_status" in text, (
        "the contract must document the ok-flag accessor the built-in sidebar uses"
    )


def test_the_icon_registry_provides_the_documented_accessors() -> None:
    registry = IconRegistry()
    assert callable(getattr(registry, "get", None))
    assert callable(getattr(registry, "get_with_status", None))

    image, ok = registry.get_with_status("mail", 64)
    assert ok is True
    assert image.size == (64, 64)
    assert registry.get("mail", 64).tobytes() == image.tobytes(), (
        "get must delegate to get_with_status, as the contract describes"
    )
