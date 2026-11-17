"""Logging to HERMES_HOME/logs with secret redaction."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from hermes import config
from hermes.security.redact import redact


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


_configured = False


def setup_logging(verbose: bool = False) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    logs = config.path("logs")
    try:
        logs.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger("hermes")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    main = RotatingFileHandler(logs / "agent.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    main.setFormatter(fmt)
    errors = RotatingFileHandler(logs / "errors.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    errors.setLevel(logging.WARNING)
    errors.setFormatter(fmt)
    root.addHandler(main)
    root.addHandler(errors)
