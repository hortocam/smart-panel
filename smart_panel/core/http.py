"""Bounded HTTP GET for plugins: the runner's side of ``ctx.http`` (T080, US3).

``research.md`` decision 11 fixes the shape of every fetch a source may make:
``urllib.request`` with a 10 s timeout, a 2 MB response cap, ``http``/``https``
only, a fixed ``User-Agent``, a limited number of redirects, and ``${secret:NAME}``
substitution in headers. Secrets are redacted from every error message, so a
failure can be logged and reported in ``status`` without leaking one (FR-049).

Importing this module pulls in nothing beyond the standard library, and the
panel transport is not involved at all (constitution III: rendering and network
access live in ``smart_panel``).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from smart_panel import __version__

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_BYTES",
    "MAX_REDIRECTS",
    "USER_AGENT",
    "HttpDecodeError",
    "HttpError",
    "HttpFetcher",
    "HttpRedirectError",
    "HttpSchemeError",
    "HttpStatusError",
    "HttpTooLargeError",
    "read_bounded",
]

MAX_BYTES = 2 * 1024 * 1024  # 2 MB (research.md decision 11)
DEFAULT_TIMEOUT_S = 10.0
MAX_REDIRECTS = 5
USER_AGENT = f"smart-panel/{__version__} (+https://github.com/hortocam/smart-panel)"

_SECRET_REF = re.compile(r"^\$\{secret:([A-Za-z0-9_.-]+)\}$")
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class HttpError(RuntimeError):
    """A fetch failed; :attr:`message` is safe to log (secrets redacted)."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class HttpSchemeError(HttpError):
    """The URL is not ``http`` or ``https``."""


class HttpTooLargeError(HttpError):
    """The response is larger than the 2 MB cap."""


class HttpRedirectError(HttpError):
    """The response redirected more times than the limit allows."""


class HttpStatusError(HttpError):
    """The server answered with a non-success status."""


class HttpDecodeError(HttpError):
    """The response body was not the JSON the caller asked for."""


def read_bounded(stream: Any, max_bytes: int = MAX_BYTES) -> bytes:
    """Read at most ``max_bytes`` from ``stream``; raise :class:`HttpTooLargeError`.

    The cap is enforced while reading, so a hostile or oversized endpoint cannot
    make the runner hold an unbounded body in memory.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(65536, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise HttpTooLargeError(f"the response is larger than {max_bytes} bytes")
    return b"".join(chunks)


class _LimitedRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that stops after ``MAX_REDIRECTS`` hops."""

    max_redirections = MAX_REDIRECTS


class HttpFetcher:
    """A bounded ``GET`` helper, injected into plugins as ``ctx.http``.

    Args:
        timeout_s: Socket timeout; the default is the documented 10 s.
        max_bytes: Response cap; the default is the documented 2 MB.
        user_agent: Fixed ``User-Agent`` sent with every request.
        secret_resolver: ``name -> value`` used for ``${secret:NAME}`` header
            values (the runner passes :meth:`~smart_panel.core.secrets.Secrets.resolve`).
        log: Optional structured logger for failures.
    """

    def __init__(
        self,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = MAX_BYTES,
        user_agent: str = USER_AGENT,
        secret_resolver: Any = None,
        log: Any = None,
    ) -> None:
        self.timeout_s = float(timeout_s)
        self.max_bytes = int(max_bytes)
        self.user_agent = str(user_agent)
        self._secret_resolver = secret_resolver
        self._log = log
        self._secrets: list[str] = []
        self._opener = urllib.request.build_opener(_LimitedRedirect())

    # ----------------------------------------------------------- header helpers

    def resolve_headers(self, headers: Mapping[str, Any] | None) -> dict[str, str]:
        """Substitute ``${secret:NAME}`` header values and remember the secrets.

        Raises:
            HttpError: A header references a secret that is not configured.
        """
        if not headers:
            return {}
        resolved: dict[str, str] = {}
        for name, value in headers.items():
            text = str(value)
            match = _SECRET_REF.match(text.strip())
            if match is None:
                resolved[str(name)] = text
                continue
            secret_name = match.group(1)
            if self._secret_resolver is None:
                raise HttpError(
                    f"header {name!r} needs the secret {secret_name!r}, "
                    "but no secrets are available"
                )
            secret = self._secret_resolver(secret_name)
            if not secret:
                raise HttpError(
                    f"header {name!r} needs the secret {secret_name!r}, which is not set"
                )
            self._secrets.append(str(secret))
            resolved[str(name)] = str(secret)
        return resolved

    def redact(self, message: Any) -> str:
        """Replace every secret seen so far with ``***``."""
        text = str(message)
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "***")
        return text

    # ------------------------------------------------------------------ fetching

    def get(self, url: str, headers: Mapping[str, Any] | None = None) -> bytes:
        """Fetch ``url`` and return its body (at most :attr:`max_bytes`).

        Raises:
            HttpSchemeError: The URL is not ``http``/``https``.
            HttpTooLargeError, HttpRedirectError, HttpStatusError, HttpError.
        """
        scheme = urlparse(str(url)).scheme.lower()
        if scheme not in ("http", "https"):
            raise HttpSchemeError(
                f"only http and https URLs are allowed, got {str(url)!r}"
            )
        resolved = self.resolve_headers(headers)
        request = urllib.request.Request(
            str(url),
            headers={"User-Agent": self.user_agent, "Accept": "*/*", **resolved},
        )
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                return read_bounded(response, self.max_bytes)
        except HttpError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in _REDIRECT_CODES:
                raise HttpRedirectError(
                    self.redact(f"{url} redirected more than {MAX_REDIRECTS} times")
                ) from exc
            raise HttpStatusError(self.redact(f"{url} returned HTTP {exc.code}")) from exc
        except urllib.error.URLError as exc:
            raise HttpError(
                self.redact(f"{url} could not be fetched: {getattr(exc, 'reason', exc)}")
            ) from exc
        except OSError as exc:  # sockets, timeouts, a truncated body
            raise HttpError(self.redact(f"{url} could not be fetched: {exc}")) from exc

    def get_json(self, url: str, headers: Mapping[str, Any] | None = None) -> Any:
        """Fetch ``url`` and decode its body as JSON.

        Raises:
            HttpDecodeError: The body is not valid JSON.
        """
        body = self.get(url, headers)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HttpDecodeError(self.redact(f"{url} did not return JSON: {exc}")) from exc
