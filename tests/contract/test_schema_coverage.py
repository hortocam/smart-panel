"""Contract test for the ``schema`` surface (T117, SC-008).

``contracts/cli.md`` (Discovery for agents) promises that ``smart-panel schema``
is "one JSON document describing every command, argument, exit code, config key
and plugin verb, so an agent can discover the whole surface (SC-008)". This test
holds that promise to the two documents that define the surface:

1. **quickstart.md** — every CLI command and plugin verb the guide uses must be
   present in the schema document, with its arguments. The test extracts the
   invocations from the guide and resolves each one through the CLI's *own*
   dispatcher (:func:`smart_panel.cli.main._static_command` and
   ``_build_groups``/``_match_verb``), so the test cannot drift from the tree the
   CLI really builds:
   * a command the CLI resolves to a control request is looked up in
     ``commands`` (the control surface);
   * a plugin verb is looked up in ``plugin_verbs`` (generated from manifests);
   * a command handled entirely in the CLI process — ``start``/``restart``/``run``
     and ``service``/``version``, which never cross the control channel
     (``contracts/control-protocol.md`` has no such ``cmd``) — is looked up in
     ``cli_commands``.
   A third-party verb reached through the generic ``plugin call`` route (the
   quickstart's ``plugin call c1 crash``) is resolved against ``plugin_verbs``
   too, so the sample drop-in plugin is covered like a built-in.

2. **data-model.md** — every configuration key the data model declares must be
   reachable through ``config set`` (FR-004/FR-006, SC-008). The test parses the
   Configuration block of the data model into dotted paths and drives the real
   ``config set`` handler (the runner-down path, which validates the whole
   candidate before writing it) for each one.

Both halves fail loudly when a document gains a command, a verb or a key the
schema does not describe, so SC-008 keeps being checked rather than assumed.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from smart_panel.cli import main as cli_main
from smart_panel.cli.commands import CliContext
from smart_panel.cli.commands import config as config_commands
from smart_panel.cli.output import Output
from smart_panel.core import commands as core_commands
from smart_panel.core.plugins import PluginLoader

REPO_ROOT = Path(__file__).resolve().parents[2]
FEATURE_DIR = REPO_ROOT / "specs" / "001-status-display-platform"
QUICKSTART = FEATURE_DIR / "quickstart.md"
DATA_MODEL = FEATURE_DIR / "data-model.md"
SAMPLE_PLUGIN = REPO_ROOT / "examples" / "plugins" / "clock.py"

# The CLI's plugin groups are aliases of the plugin names (``main.PLUGIN_GROUP_ALIASES``);
# invert it so a quickstart verb group resolves to the schema's plugin key.
_GROUP_TO_PLUGIN = {alias: name for name, alias in cli_main.PLUGIN_GROUP_ALIASES.items()}

# Commands whose CLI spelling is not simply segments joined with dots.
_CLI_PATH_TO_CONTROL = {
    ("status",): "runner.status",
    ("reload",): "runner.reload",
    ("stop",): "runner.stop",
    ("preview",): "display.preview",
    ("logs",): "logs.tail",
    ("schema",): "schema.get",
}

# The arguments each command/verb must *declare*, for the commands the
# quickstart exercises. Only the arguments the control/plugin surface really
# carries are listed: a CLI-only convenience (``preview --output``, the ``--set``
# / ``--arg`` shorthand for ``settings``/``args``) is consumed by the CLI process
# and is asserted against ``cli_commands`` where it belongs.
_REQUIRED_ARGS: dict[str, set[str]] = {
    "start": {"sink", "files-dir"},
    "restart": {"sink", "files-dir"},
    "config.set": {"path", "value"},
    "plugin.instance.add": {"name", "plugin", "settings"},
    "plugin.instance.set": {"name", "settings"},
    "plugin.list": set(),
    "plugin.call": {"instance", "verb", "args"},
    "layout.region.set": {"name", "x", "y", "w", "h", "instance"},
    "layout.overlay.add": {"region", "instance"},
    "access.allow_user": {"name"},
    "alerts.push": {"severity", "message", "title", "id"},
    "alerts.resolve": {"id"},
    "alerts.list": {"state", "limit", "since"},
    "alerts.clear-missed": {"id"},
    "crawl.items set": {"items", "source"},
    "crawl.source add": {"id", "kind", "url", "preset", "topic"},
    "gauges.add": {"id", "label", "topic", "aggregation", "window", "style"},
    "gauges.push": {"id", "value"},
    "sidebar.entry add": {"id", "icon", "label", "topic"},
    "sidebar.count set": {"id", "value"},
    "clock.crash": set(),
}

# A value ``config set`` accepts for every key the data model declares, so each
# key is proven reachable with a *new* value (``None`` = a structural key, set to
# its own subtree).
_CONFIG_SAMPLES: dict[str, object] = {
    "version": 1,
    "display.fps": 25,
    "display.brightness": 40,
    "display.jpeg_quality.start": 80,
    "display.jpeg_quality.min": 40,
    "display.min_text_px": 30,
    "display.sink": "files",
    "display.files_dir": "/tmp/smart-panel-frames",
    "access.allowed_users": ["hermes"],
    "access.allowed_group": "plugdev",
    "paths.spool": "/tmp/smart-panel-spool",
    "layout": None,
    "plugins": None,
    "sources": None,
}


# ------------------------------------------------------------------- fixtures


@pytest.fixture
def loader(tmp_path: Path) -> PluginLoader:
    """The built-in registry plus the sample drop-in plugin (quickstart G)."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "clock.py").write_text(SAMPLE_PLUGIN.read_text(encoding="utf-8"), encoding="utf-8")
    return PluginLoader(home=plugins)


@pytest.fixture
def schema_document(loader: PluginLoader) -> dict:
    """The ``schema`` payload the CLI serves, built from the real core helper."""
    document = core_commands.schema(SimpleNamespace(loader=loader))
    # The fixture is only meaningful if the sample plugin really loaded.
    assert "clock.crash" in document["plugin_verbs"], sorted(document["plugin_verbs"])
    return document


@pytest.fixture(scope="module")
def group_loader() -> PluginLoader:
    """A loader with the built-in manifests, for resolving plugin verb groups."""
    return PluginLoader(home=Path("/nonexistent-plugins"))


# ------------------------------------------------------- quickstart extraction


def _quickstart_invocations() -> list[list[str]]:
    """Every ``smart-panel ...`` token list in quickstart.md, in file order."""
    text = QUICKSTART.read_text(encoding="utf-8")
    invocations: list[list[str]] = []
    for line in text.splitlines():
        # Split shell chaining so ``start && status`` yields both commands.
        for segment in re.split(r"&&|\|\||;", line):
            match = re.search(r"(?<![\w-])smart-panel\b(.*)", segment)
            if match is None:
                continue
            tokens = _tokenize(match.group(1))
            if tokens:
                invocations.append(tokens)
    return invocations


def _tokenize(rest: str) -> list[str]:
    """Split the words after ``smart-panel``, dropping prose and comments."""
    rest = rest.split("#", 1)[0]  # a trailing comment ends the command
    try:
        raw = shlex.split(rest, posix=False)
    except ValueError:  # an unbalanced quote in prose: fall back to words
        raw = rest.split()
    tokens = [token.strip("`") for token in raw]
    return _drop_globals([token for token in tokens if token])


def _drop_globals(tokens: list[str]) -> list[str]:
    """Remove the leading global flags (``--json``, ``--timeout 5``, ...)."""
    value_flags = {"--home", "--timeout"}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("--json", "--quiet", "--refresh"):
            index += 1
        elif token.startswith(("--home=", "--timeout=")):
            index += 1
        elif token in value_flags:
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            break
    return tokens[index:]


def _resolve(
    tokens: list[str], document: dict, groups: dict[str, dict[str, dict]]
) -> list[tuple[str, str]]:
    """Resolve one invocation to the ``(schema section, schema key)`` pairs it names.

    ``plugin call INSTANCE VERB`` names two: the ``plugin.call`` route and the
    plugin verb it carries in its argument.
    """
    found = cli_main._static_command(tokens)  # noqa: SLF001 - the CLI's own resolver
    if found is not None:
        path, _handler = found
        if path == ("plugin", "call"):
            return [
                ("commands", "plugin.call"),
                ("plugin_verbs", _verb_key_from_call(tokens, document)),
            ]
        key = _CLI_PATH_TO_CONTROL.get(path) or ".".join(
            part.replace("-", "_") for part in path
        )
        section = "commands" if key in document["commands"] else "cli_commands"
        return [(section, key)]

    spec, _consumed = cli_main._match_verb(groups, tokens)  # noqa: SLF001
    plugin = _GROUP_TO_PLUGIN.get(tokens[0], tokens[0])
    return [("plugin_verbs", f"{plugin}.{spec['verb']}")]


def _verb_key_from_call(tokens: list[str], document: dict) -> str:
    """Resolve ``plugin call INSTANCE VERB`` to ``<plugin>.<verb>``.

    The instance name usually matches the plugin name (``plugin call sources ...``);
    a third-party instance (``plugin call c1 crash``) does not, so the verb alone
    must be unambiguous for it.
    """
    instance = tokens[2]
    verb = " ".join(tokens[3].replace("-", " ").split())
    keys = list(document["plugin_verbs"])
    candidates = [
        key
        for key in keys
        if key.startswith(f"{instance}.") and key.endswith(f".{verb}")
    ]
    if len(candidates) != 1:
        candidates = [key for key in keys if key.endswith(f".{verb}")]
    assert len(candidates) == 1, (
        f"`plugin call {instance} {verb}` resolves to {len(candidates)} verbs: {candidates}"
    )
    return candidates[0]


# ----------------------------------------------------------- command coverage


def test_the_quickstart_exercises_a_real_surface() -> None:
    invocations = _quickstart_invocations()
    assert len(invocations) >= 20, invocations


def test_every_quickstart_command_appears_in_the_schema(
    schema_document: dict, group_loader: PluginLoader
) -> None:
    groups = cli_main._build_groups(  # noqa: SLF001
        CliContext(home=Path("/nonexistent"), timeout=1.0, refresh=True, out=Output()),
        group_loader,
    )
    missing: list[str] = []
    for tokens in _quickstart_invocations():
        for section, key in _resolve(tokens, schema_document, groups):
            if key not in schema_document.get(section, {}):
                missing.append(f"{' '.join(tokens)} -> {section}[{key}]")
    assert missing == [], f"quickstart commands absent from `smart-panel schema`: {missing}"


def test_every_quickstart_command_declares_its_arguments(schema_document: dict) -> None:
    for key, required in _REQUIRED_ARGS.items():
        section = _section_of(schema_document, key)
        entry = schema_document[section][key]
        declared = _declared_args(section, entry)
        missing = required - declared
        assert not missing, (
            f"{section}[{key}] declares {sorted(declared)}, missing {sorted(missing)}"
        )


def _section_of(document: dict, key: str) -> str:
    for section in ("commands", "cli_commands", "plugin_verbs"):
        if key in document[section]:
            return section
    raise KeyError(f"no schema section declares {key!r}")


def test_the_schema_documents_the_exit_codes_an_agent_needs(schema_document: dict) -> None:
    codes = schema_document["exit_codes"]
    for name in ("ok", "invalid_input", "runner_not_running", "not_permitted", "conflict"):
        assert name in codes, sorted(codes)


def _declared_args(section: str, entry: object) -> set[str]:
    if section == "plugin_verbs":
        assert isinstance(entry, dict) and isinstance(entry.get("args"), list), entry
        return {str(arg["name"]) for arg in entry["args"]}
    assert isinstance(entry, dict), entry
    # A control command with no arguments omits ``args`` entirely.
    return set(entry.get("args") or {})


# ----------------------------------------------------- data-model config keys


_TREE_LINE = re.compile(
    r"^(?P<prefix>[│ \t]*)(?:├──|└──)?\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::\s*(?P<type>.*))?$"
)
_INLINE_KEYS = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:")


def config_keys_from_data_model() -> list[str]:
    """Parse the Configuration block of ``data-model.md`` into dotted leaf keys."""
    text = DATA_MODEL.read_text(encoding="utf-8")
    block = re.search(r"## Configuration.*?```text\n(.*?)```", text, re.DOTALL)
    assert block is not None, "data-model.md has no Configuration text block"
    keys: list[str] = []
    stack: list[str] = []
    for raw in block.group(1).splitlines():
        if "├──" not in raw and "└──" not in raw:
            continue  # the bare ``Config`` root line has no tree marker
        match = _TREE_LINE.match(raw.rstrip())
        if match is None or not match.group("name"):
            continue
        depth = len(match.group("prefix")) // 4
        name = match.group("name")
        kind = (match.group("type") or "").strip()
        del stack[depth:]
        if not kind:
            stack.append(name)
            continue
        path = ".".join([*stack, name])
        if kind.startswith("{"):
            # An inline object (``jpeg_quality: {start: ..., min: ...}``): the
            # children are the keys; ``name`` inside ``{name: ...}`` is the
            # placeholder for a map key, not a key itself, so a map like
            # ``plugins: {name: PluginInstance}`` stays the container path.
            inner_keys = [inner for inner in _INLINE_KEYS.findall(kind[1:]) if inner != "name"]
            if inner_keys:
                keys.extend(f"{path}.{inner}" for inner in inner_keys)
            else:
                keys.append(path)
            continue
        keys.append(path)
    return keys


def test_the_data_model_config_keys_are_covered_by_the_sample_table() -> None:
    # A key added to the data model without a ``config set`` sample fails here,
    # so the reachability check below can never silently shrink.
    assert sorted(config_keys_from_data_model()) == sorted(_CONFIG_SAMPLES)


@pytest.fixture
def cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SMART_PANEL_HOME", str(home))
    monkeypatch.setenv("SMART_PANEL_RUNTIME", str(tmp_path / "no-such-runtime"))
    monkeypatch.delenv("SMART_PANEL_RUNNER_UID", raising=False)
    return home


def _config_set(home: Path, path: str, raw: str) -> dict:
    """Drive the real ``config set`` handler (runner down, local write)."""
    ctx = CliContext(home=home, timeout=1.0, refresh=False, out=Output())
    return config_commands.cmd_config_set(ctx, [path, raw])


def _written_home_config(home: Path) -> dict:
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


def test_every_data_model_config_key_is_reachable_through_config_set(cli_home: Path) -> None:
    # A first write seeds the file from the initial config (built-ins + layout).
    _config_set(cli_home, "display.fps", "15")
    seeded = _written_home_config(cli_home)

    for path in config_keys_from_data_model():
        sample = _CONFIG_SAMPLES[path]
        if sample is None:
            # A structural key: prove the path is settable with its own subtree.
            sample = _value_at(seeded, path)
        result = _config_set(cli_home, path, json.dumps(sample))
        assert result.get("applied") is True, (path, result)
        resolved = _value_at(_written_home_config(cli_home), path)
        assert resolved == sample, (path, resolved, sample)


def _value_at(document: dict, dotted: str) -> object:
    current: object = document
    for part in dotted.split("."):
        assert isinstance(current, dict), (dotted, current)
        current = current[part]
    return current


def test_an_unknown_config_key_is_still_rejected(cli_home: Path) -> None:
    # Negative control: reachability is not "anything goes".
    from smart_panel.cli.output import CliError

    with pytest.raises(CliError) as excinfo:
        _config_set(cli_home, "display.no_such_key", "1")
    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.setting == "display.no_such_key"
    assert "no_such_key" in excinfo.value.message
