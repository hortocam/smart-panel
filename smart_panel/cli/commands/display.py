"""Display controls: brightness, layout and preview (T057).

``brightness set N`` is 0-100; ``0`` turns the backlight off. ``layout`` reads
and edits the live layout, and ``preview`` renders the composed canvas to a PNG
with no panel attached (so a headless host can still check what would be shown).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from smart_panel.cli.commands import (
    CliContext,
    no_args,
    parse_options,
    require_args,
)
from smart_panel.cli.output import CliError, UsageError

HANDLERS: dict[tuple[str, ...], Any] = {}


def cmd_brightness_set(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Set the backlight brightness (0-100; 0 turns the backlight off)."""
    positionals, _opts = parse_options(tokens)
    (raw,) = require_args(positionals, 1, "smart-panel brightness set N")
    try:
        value = int(raw)
    except ValueError as exc:
        raise UsageError(f"brightness must be an integer 0-100, got {raw!r}") from exc
    if not 0 <= value <= 100:
        raise UsageError(f"brightness must be 0-100, got {value}")
    return ctx.call("display.brightness.set", {"value": value})


def cmd_brightness_get(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Read the current backlight brightness."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel brightness get")
    return ctx.call("display.brightness.get")


def cmd_layout_show(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Show the active layout (regions and overlays)."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel layout show")
    return ctx.call("layout.show")


def cmd_layout_set(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Replace the whole layout from a JSON file."""
    positionals, opts = parse_options(tokens, value_opts={"file": str})
    no_args(positionals, "smart-panel layout set --file F")
    if "file" not in opts:
        raise UsageError("layout set needs --file F")
    document = _read_json(opts["file"])
    return ctx.call("layout.set", {"layout": document})


def cmd_layout_region_set(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Add or edit one region."""
    positionals, opts = parse_options(
        tokens,
        value_opts={"x": int, "y": int, "w": int, "h": int, "instance": str},
    )
    (name,) = require_args(
        positionals, 1, "smart-panel layout region set NAME --x --y --w --h --instance I"
    )
    args: dict[str, Any] = {"name": name}
    for key in ("x", "y", "w", "h", "instance"):
        if key in opts:
            args[key] = opts[key]
    return ctx.call("layout.region.set", args)


def cmd_layout_overlay_add(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Add an overlay over a region."""
    positionals, _opts = parse_options(tokens)
    region, instance = require_args(
        positionals, 2, "smart-panel layout overlay add REGION INSTANCE"
    )
    return ctx.call("layout.overlay.add", {"region": region, "instance": instance})


def cmd_layout_overlay_remove(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Remove an overlay."""
    positionals, _opts = parse_options(tokens)
    region, instance = require_args(
        positionals, 2, "smart-panel layout overlay remove REGION INSTANCE"
    )
    return ctx.call("layout.overlay.remove", {"region": region, "instance": instance})


def cmd_preview(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Render the composed canvas to a PNG (works with no panel)."""
    positionals, opts = parse_options(tokens, value_opts={"output": str})
    no_args(positionals, "smart-panel preview [--output FILE.png]")
    data = ctx.call("display.preview")
    encoded = data.get("png_b64")
    if not isinstance(encoded, str):
        raise CliError("the runner returned no preview image")
    target = Path(opts.get("output") or (ctx.home / "preview.png"))
    try:
        payload = base64.b64decode(encoded)
    except (ValueError, TypeError) as exc:
        raise CliError(f"the runner returned an invalid preview image: {exc}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return {"output": str(target), "bytes": len(payload)}


def _read_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CliError(f"could not read {path}: {exc}", code="invalid_input") from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"{path} is not valid JSON: {exc}", code="invalid_input") from exc


HANDLERS.update(
    {
        ("brightness", "set"): cmd_brightness_set,
        ("brightness", "get"): cmd_brightness_get,
        ("layout", "show"): cmd_layout_show,
        ("layout", "set"): cmd_layout_set,
        ("layout", "region", "set"): cmd_layout_region_set,
        ("layout", "overlay", "add"): cmd_layout_overlay_add,
        ("layout", "overlay", "remove"): cmd_layout_overlay_remove,
        ("preview",): cmd_preview,
    }
)
