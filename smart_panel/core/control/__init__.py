"""The local control channel between the CLI and the runner (T044 to T049).

Everything the runner and the CLI say to each other over the Unix socket lives
here: the JSON-lines request/response schema (:mod:`protocol`), the pure
allow-list policy (:mod:`access`), the platform peer-credential lookup
(:mod:`peercred`), the socket server (:mod:`server`), the client with its spool
fallback (:mod:`client`) and the runner-side spool ingest (:mod:`spool`).
"""
