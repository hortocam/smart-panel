"""Unit tests for ``smart_panel.sdk.settings`` (T017).

Settings are validated before they reach a plugin (and before a config change
is applied), so the validator is the single gate for types, ranges, enums,
colours and secret references. Unknown keys are rejected with the dotted path
of the offender so an agent editing ``config set`` gets an actionable message.
"""

from __future__ import annotations

import pytest

from smart_panel.sdk.settings import Setting, SettingError, validate


def _specs() -> list[Setting]:
    return [
        Setting(name="fps", type="int", default=15, description="", min=5, max=30),
        Setting(name="brightness", type="int", default=50, description="", min=0, max=100),
        Setting(
            name="sink",
            type="enum",
            default="hid",
            description="",
            choices=["hid", "files", "null"],
        ),
        Setting(name="scale", type="float", default=1.5, description="", min=0.0, max=3.0),
        Setting(name="flag", type="bool", default=False, description=""),
        Setting(name="fg", type="color", default="#FFFFFF", description=""),
        Setting(
            name="title",
            type="str",
            default="hi",
            description="",
            pattern=r"^[a-z ]+$",
        ),
        Setting(name="token", type="str", default="${secret:TOK}", description="", secret=True),
        Setting(name="window_s", type="duration", default=60, description="", min=10, max=86400),
    ]


def test_defaults_are_filled_in() -> None:
    values = validate(_specs(), {})
    assert values["fps"] == 15
    assert values["sink"] == "hid"
    assert values["fg"] == "#FFFFFF"
    assert values["token"] == "${secret:TOK}"


def test_int_accepts_inclusive_range_edges() -> None:
    assert validate(_specs(), {"fps": 5})["fps"] == 5
    assert validate(_specs(), {"fps": 30})["fps"] == 30


@pytest.mark.parametrize("bad,word", [(4, "minimum"), (31, "maximum")])
def test_int_below_or_above_range_is_rejected(bad: int, word: str) -> None:
    with pytest.raises(SettingError) as excinfo:
        validate(_specs(), {"fps": bad})
    assert excinfo.value.path == "fps"
    assert word in excinfo.value.message
    assert str(bad) in excinfo.value.message


@pytest.mark.parametrize("bad", [True, "15", 15.5, None])
def test_int_rejects_non_int_values(bad: object) -> None:
    with pytest.raises(SettingError) as excinfo:
        validate(_specs(), {"fps": bad})
    assert excinfo.value.path == "fps"


def test_bool_rejects_int() -> None:
    with pytest.raises(SettingError):
        validate(_specs(), {"flag": 1})
    assert validate(_specs(), {"flag": True})["flag"] is True


def test_float_accepts_int_and_rejects_out_of_range() -> None:
    assert validate(_specs(), {"scale": 2})["scale"] == 2.0
    with pytest.raises(SettingError):
        validate(_specs(), {"scale": 4.0})


def test_enum_rejects_unknown_choice() -> None:
    assert validate(_specs(), {"sink": "files"})["sink"] == "files"
    with pytest.raises(SettingError) as excinfo:
        validate(_specs(), {"sink": "bogus"})
    assert "bogus" in excinfo.value.message


def test_unknown_key_is_rejected_naming_the_dotted_path() -> None:
    with pytest.raises(SettingError) as excinfo:
        validate(_specs(), {"nope": 1}, prefix="plugins.text.settings")
    assert excinfo.value.path == "plugins.text.settings.nope"
    assert "plugins.text.settings.nope" in str(excinfo.value)


def test_string_pattern_is_enforced() -> None:
    assert validate(_specs(), {"title": "hello there"})["title"] == "hello there"
    with pytest.raises(SettingError):
        validate(_specs(), {"title": "Bad!"})


@pytest.mark.parametrize("value", ["#001122", "#00FF00", "#00112233", "#ABCDEFAA"])
def test_color_accepts_hex_forms(value: str) -> None:
    assert validate(_specs(), {"fg": value})["fg"] == value


def test_color_accepts_a_css_style_name() -> None:
    # The SDK contract allows a name from a small CSS-style table in addition to
    # the #RRGGBB/#RRGGBBAA forms required by tasks.md T017.
    assert validate(_specs(), {"fg": "red"})["fg"] == "red"


@pytest.mark.parametrize("value", ["#123", "#GGGGGG", "112233", "not-a-colour"])
def test_color_rejects_bad_forms(value: str) -> None:
    with pytest.raises(SettingError) as excinfo:
        validate(_specs(), {"fg": value})
    assert excinfo.value.path == "fg"


def test_secret_setting_accepts_only_references() -> None:
    assert validate(_specs(), {"token": "${secret:MY_KEY}"})["token"] == "${secret:MY_KEY}"
    with pytest.raises(SettingError) as excinfo:
        validate(_specs(), {"token": "plain-text"})
    assert excinfo.value.path == "token"


def test_duration_parses_units_and_enforces_range() -> None:
    assert validate(_specs(), {"window_s": "5m"})["window_s"] == 300
    assert validate(_specs(), {"window_s": 3600})["window_s"] == 3600
    with pytest.raises(SettingError):
        validate(_specs(), {"window_s": "1s"})


def test_validate_rejects_a_non_mapping() -> None:
    with pytest.raises(SettingError):
        validate(_specs(), ["not", "a", "dict"])  # type: ignore[arg-type]
