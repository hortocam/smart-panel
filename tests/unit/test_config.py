"""Unit tests for ``smart_panel.core.config`` (T024).

``config.json`` is a single JSON document validated as a whole before it is
applied. Unknown keys are rejected with the dotted path of the offender, the
write is atomic (temp file plus rename), and dotted-path ``set``/``unset`` edit a
candidate copy so the live config is left untouched when a value does not
conform.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from smart_panel.core import config as config_module
from smart_panel.core.config import Config, default_config, validate_config
from smart_panel.core.layout import LayoutError
from smart_panel.sdk.settings import SettingError


def test_defaults() -> None:
    cfg = default_config()
    assert cfg["version"] == 1
    assert cfg["display"]["fps"] == 15
    assert cfg["display"]["brightness"] == 50
    assert cfg["display"]["jpeg_quality"] == {"start": 70, "min": 30}
    assert cfg["display"]["min_text_px"] == 24
    assert cfg["display"]["sink"] == "hid"
    assert cfg["display"]["files_dir"] is None
    assert cfg["access"] == {"allowed_users": [], "allowed_group": None}
    assert cfg["paths"] == {"spool": None}
    assert cfg["layout"] == {"regions": [], "overlays": []}
    assert cfg["plugins"] == {}
    assert cfg["sources"] == {}


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    data = Config(tmp_path / "config.json").load()
    assert data["display"]["fps"] == 15


def test_range_edges_are_accepted() -> None:
    assert validate_config({"display": {"fps": 5}})["display"]["fps"] == 5
    assert validate_config({"display": {"fps": 30}})["display"]["fps"] == 30
    assert validate_config({"display": {"brightness": 0}})["display"]["brightness"] == 0
    assert validate_config({"display": {"brightness": 100}})["display"]["brightness"] == 100
    assert validate_config({"display": {"min_text_px": 12}})["display"]["min_text_px"] == 12
    assert validate_config({"display": {"min_text_px": 64}})["display"]["min_text_px"] == 64
    assert validate_config({"display": {"sink": "files"}})["display"]["sink"] == "files"


@pytest.mark.parametrize("fps", [4, 31])
def test_out_of_range_is_rejected_naming_the_path(fps: int) -> None:
    with pytest.raises(SettingError) as excinfo:
        validate_config({"display": {"fps": fps}})
    assert excinfo.value.path == "display.fps"


def test_jpeg_quality_bounds() -> None:
    assert validate_config(
        {"display": {"jpeg_quality": {"start": 95, "min": 20}}}
    )["display"]["jpeg_quality"] == {"start": 95, "min": 20}
    with pytest.raises(SettingError):
        validate_config({"display": {"jpeg_quality": {"start": 70, "min": 71}}})
    with pytest.raises(SettingError):
        validate_config({"display": {"jpeg_quality": {"start": 29}}})


def test_unknown_top_level_key_names_the_path() -> None:
    with pytest.raises(SettingError) as excinfo:
        validate_config({"display": {}, "bogus": 1})
    assert excinfo.value.path == "bogus"


def test_unknown_nested_key_names_the_dotted_path() -> None:
    with pytest.raises(SettingError) as excinfo:
        validate_config({"display": {"fpsx": 1}})
    assert excinfo.value.path == "display.fpsx"


def test_defaults_are_filled_for_a_partial_config() -> None:
    validated = validate_config({"display": {"fps": 20}})
    assert validated["display"]["fps"] == 20
    assert validated["display"]["brightness"] == 50
    assert validated["access"] == {"allowed_users": [], "allowed_group": None}


def test_set_unset_round_trip(tmp_path: Path) -> None:
    cfg = Config(tmp_path / "config.json")
    cfg.load()
    cfg.set("display.fps", 20)
    assert cfg.get("display.fps") == 20
    cfg.unset("display.fps")
    assert cfg.get("display.fps") == 15


def test_set_rejects_and_leaves_the_live_config_untouched(tmp_path: Path) -> None:
    cfg = Config(tmp_path / "config.json")
    cfg.load()
    cfg.set("display.fps", 20)
    with pytest.raises(SettingError):
        cfg.set("display.fps", 999)
    assert cfg.get("display.fps") == 20


def test_save_then_reload(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    cfg = Config(path)
    cfg.load()
    cfg.set("display.fps", 20)
    cfg.save()
    assert Config(path).load()["display"]["fps"] == 20


def test_atomic_write_survives_a_simulated_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    cfg = Config(path)
    cfg.load()
    cfg.set("display.fps", 20)
    cfg.save()
    original = path.read_text()

    def crash(*args: object, **kwargs: object) -> None:
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(config_module.os, "replace", crash)
    with pytest.raises(OSError):
        cfg.save({"display": {"fps": 25}})

    assert path.read_text() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_layout_is_validated(tmp_path: Path) -> None:
    cfg = Config(tmp_path / "config.json")
    cfg.load()
    with pytest.raises(LayoutError):
        cfg.set(
            "layout",
            {"regions": [{"name": "r", "x": 0, "y": 0, "w": 10, "h": 10, "instance": "i"}]},
        )


def test_valid_layout_is_accepted() -> None:
    validated = validate_config(
        {
            "layout": {
                "regions": [
                    {"name": "main", "x": 0, "y": 0, "w": 1920, "h": 462, "instance": "text"}
                ],
                "overlays": [],
            }
        }
    )
    assert validated["layout"]["regions"][0]["name"] == "main"


def test_version_mismatch_is_reported() -> None:
    with pytest.raises(SettingError):
        validate_config({"version": 99})
