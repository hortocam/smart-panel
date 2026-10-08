"""Test package for smart-panel.

T003 creates the scaffold. Tests are grouped into ``unit``, ``contract``,
``integration`` and ``hardware`` subpackages; binary fixtures extracted from
the USB capture live in ``tests/fixtures``. ``hardware`` tests are skipped by
default via the ``addopts = "-m 'not hardware'"`` setting in ``pyproject.toml``
(constitution Principle IV: the suite must run with no panel attached).
"""
