"""``smart-panel service install|uninstall|status`` (T060).

Thin wrappers over :mod:`smart_panel.service`; the unit generation, the runtime
directory creation and the systemd/launchd invocations all live there so they can
be unit-tested without a service manager.
"""

from __future__ import annotations

from typing import Any

from smart_panel import service
from smart_panel.cli.commands import CliContext, no_args, parse_options
from smart_panel.cli.output import CliError

HANDLERS: dict[tuple[str, ...], Any] = {}


def cmd_service_install(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Install and enable the user service for this host."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel service install")
    spec = service.default_spec(home=ctx.home)
    try:
        return service.install(spec, run=service.default_runner())
    except service.ServiceError as exc:
        raise CliError(str(exc), code=exc.code) from exc


def cmd_service_uninstall(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Disable and remove the user service."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel service uninstall")
    spec = service.default_spec(home=ctx.home)
    return service.uninstall(spec, run=service.default_runner())


def cmd_service_status(ctx: CliContext, tokens: list[str]) -> dict[str, Any]:
    """Report whether the service is installed, active and enabled."""
    positionals, _opts = parse_options(tokens)
    no_args(positionals, "smart-panel service status")
    spec = service.default_spec(home=ctx.home)
    return service.status(spec, output=service.default_capture())


HANDLERS.update(
    {
        ("service", "install"): cmd_service_install,
        ("service", "uninstall"): cmd_service_uninstall,
        ("service", "status"): cmd_service_status,
    }
)
