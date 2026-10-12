"""The control protocol: JSON-lines messages, error codes, exit codes (T044).

The wire format is one JSON object per ``\\n``-terminated UTF-8 line, at most
1 MiB (``contracts/control-protocol.md``). This module owns the three message
shapes — :class:`Hello`, :class:`Request`, :class:`Response` (with its
:class:`Error`) — their encode/decode, the handshake validation, and the closed
set of error codes together with the code-to-exit-code mapping the CLI publishes
(``contracts/cli.md``).

Both sets are deliberately closed: a code the contracts do not name cannot be
built, so a typo becomes an exception at the boundary instead of an undocumented
exit code on an agent's screen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 1024 * 1024  # 1 MiB, content only (the newline is not counted)
CLIENT_NAME = "smart-panel-cli/0.1.0"

# ``contracts/control-protocol.md``: "error.code is one of ...".
ERROR_CODES = frozenset(
    {
        "invalid_input",
        "not_found",
        "not_permitted",
        "conflict",
        "panel_unavailable",
        "plugin_error",
        "spool_full",
        "timeout",
        "protocol_mismatch",
        "internal",
    }
)

# ``contracts/cli.md``: the exit-code table plus the extra error codes listed
# beneath it. ``runner_not_running`` is named as exit code 3; the client raises
# it itself when no runner answers, so it is never a server-sent error code.
EXIT_CODES: dict[str, int] = {
    "invalid_input": 2,
    "not_found": 2,
    "runner_not_running": 3,
    "panel_unavailable": 4,
    "not_permitted": 5,
    "conflict": 6,
    "plugin_error": 7,
    "spool_full": 1,
    "timeout": 1,
    "protocol_mismatch": 1,
    "internal": 1,
}

EXIT_CODE_NAMES: dict[int, str] = {
    0: "ok",
    1: "error",
    2: "invalid_input",
    3: "runner_not_running",
    4: "panel_unavailable",
    5: "not_permitted",
    6: "conflict",
    7: "plugin_error",
}


def exit_code_for(code: str) -> int:
    """Return the CLI exit code for an error code.

    An unknown code is an unexpected internal error, never a silent success.
    """
    return EXIT_CODES.get(code, 1)


class ProtocolError(Exception):
    """A message that violates the protocol; carries the error code to reply with."""

    def __init__(self, code: str, message: str, setting: str | None = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.message = message
        self.setting = setting
        super().__init__(f"{code}: {message}")

    def as_response(self, request_id: int | str | None) -> Response:
        """The error response the server sends for this violation."""
        return Response.failure(request_id, self.code, self.message, self.setting)


@dataclass(frozen=True)
class Hello:
    """The mandatory first exchange of a connection."""

    v: int = PROTOCOL_VERSION
    client: str | None = None
    server: str | None = None
    plugins_rev: int | None = None

    def to_obj(self) -> dict[str, Any]:
        body: dict[str, Any] = {"v": self.v}
        if self.client is not None:
            body["client"] = self.client
        if self.server is not None:
            body["server"] = self.server
        if self.plugins_rev is not None:
            body["plugins_rev"] = self.plugins_rev
        return {"hello": body}


@dataclass(frozen=True)
class Request:
    """One command from a client."""

    cmd: str
    args: dict[str, Any] = field(default_factory=dict)
    id: int | str | None = None

    def to_obj(self) -> dict[str, Any]:
        return {"id": self.id, "cmd": self.cmd, "args": self.args}


@dataclass(frozen=True)
class Error:
    """A failed response's payload."""

    code: str
    message: str
    setting: str | None = None

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown error code {self.code!r}")

    def to_obj(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.setting is not None:
            body["setting"] = self.setting
        return body


@dataclass(frozen=True)
class Response:
    """One answer: ``data`` on success, ``error`` on failure, always the same ``id``."""

    id: int | str | None
    ok: bool
    data: dict[str, Any] | None = None
    error: Error | None = None

    @classmethod
    def success(cls, request_id: int | str | None, data: dict[str, Any] | None = None) -> Response:
        return cls(id=request_id, ok=True, data=data if data is not None else {})

    @classmethod
    def failure(
        cls,
        request_id: int | str | None,
        code: str,
        message: str,
        setting: str | None = None,
    ) -> Response:
        return cls(id=request_id, ok=False, error=Error(code, message, setting))

    @classmethod
    def from_obj(cls, obj: dict[str, Any]) -> Response:
        """Rebuild a response received from the server."""
        error_obj = obj.get("error")
        error = (
            Error(
                str(error_obj.get("code", "internal")),
                str(error_obj.get("message", "")),
                error_obj.get("setting"),
            )
            if isinstance(error_obj, dict)
            else None
        )
        data = obj.get("data")
        return cls(
            id=obj.get("id"),
            ok=bool(obj.get("ok", False)),
            data=data if isinstance(data, dict) else None,
            error=error,
        )

    @property
    def exit_code(self) -> int:
        """The CLI exit code this response maps to (0 when it succeeded)."""
        if self.ok:
            return 0
        return exit_code_for(self.error.code if self.error is not None else "internal")

    def to_obj(self) -> dict[str, Any]:
        if self.ok:
            return {"id": self.id, "ok": True, "data": self.data if self.data is not None else {}}
        error = self.error or Error("internal", "response carried no error")
        return {"id": self.id, "ok": False, "error": error.to_obj()}


def hello_request(client: str = CLIENT_NAME) -> Hello:
    """The client's first line."""
    return Hello(v=PROTOCOL_VERSION, client=client)


def hello_reply(server: str, plugins_rev: int) -> Hello:
    """The server's answer to a valid hello."""
    return Hello(v=PROTOCOL_VERSION, server=server, plugins_rev=plugins_rev)


def encode(obj: dict[str, Any]) -> str:
    """Serialise one message as a JSON line body (no terminator)."""
    return json.dumps(obj, separators=(",", ":"), default=str)


def encode_line(obj: dict[str, Any]) -> bytes:
    """Serialise one message as a ``\\n``-terminated UTF-8 line."""
    return encode(obj).encode("utf-8") + b"\n"


def is_oversize_line(raw: bytes) -> bool:
    """Whether a raw line (with its terminator) breaks the 1 MiB content limit."""
    content = raw[:-1] if raw.endswith(b"\n") else raw
    return len(content) > MAX_LINE_BYTES


def decode_line(raw: bytes) -> dict[str, Any]:
    """Parse one raw line; raise :class:`ProtocolError` on anything malformed."""
    if is_oversize_line(raw):
        raise ProtocolError("invalid_input", f"line exceeds the {MAX_LINE_BYTES} byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("invalid_input", "line is not valid UTF-8") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid_input", "line is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("invalid_input", "a message must be a JSON object")
    return obj


def parse_hello(obj: dict[str, Any]) -> Hello:
    """Validate the first line of a connection.

    Raises:
        ProtocolError: ``protocol_mismatch`` when the first line is not a hello
            or its major version is not :data:`PROTOCOL_VERSION`.
    """
    body = obj.get("hello")
    if not isinstance(body, dict):
        raise ProtocolError("protocol_mismatch", "the first line must be a hello")
    version = body.get("v")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProtocolError("protocol_mismatch", "hello must carry an integer version")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            "protocol_mismatch",
            f"protocol version {version} is not {PROTOCOL_VERSION}",
        )
    client = body.get("client")
    server = body.get("server")
    plugins_rev = body.get("plugins_rev")
    return Hello(
        v=version,
        client=client if isinstance(client, str) else None,
        server=server if isinstance(server, str) else None,
        plugins_rev=plugins_rev if isinstance(plugins_rev, int) else None,
    )


def parse_request(obj: dict[str, Any]) -> Request:
    """Validate one request line.

    Raises:
        ProtocolError: ``invalid_input`` for a missing or mistyped field.
    """
    cmd = obj.get("cmd")
    if not isinstance(cmd, str) or not cmd:
        raise ProtocolError("invalid_input", "a request needs a non-empty string 'cmd'")
    args = obj.get("args", {})
    if not isinstance(args, dict):
        raise ProtocolError("invalid_input", "'args' must be a JSON object")
    request_id = obj.get("id")
    if request_id is not None and (
        isinstance(request_id, bool) or not isinstance(request_id, (int, str))
    ):
        raise ProtocolError("invalid_input", "'id' must be an integer or a string")
    return Request(cmd=cmd, args=args, id=request_id)
