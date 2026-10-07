"""smart_panel — application layer for the HOTSPOTEK status display.

This package sits on top of ``panel_driver`` (the transport) and holds the
runner, the plugin SDK, the CLI and the rendering layer. The dependency arrow
only ever points this way: ``panel_driver`` must not import from ``smart_panel``
(constitution, Principle III — Layered and Minimal).
"""

__version__ = "0.1.0"
