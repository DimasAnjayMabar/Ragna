# progress.py
"""
Progress + log reporter for the Go launcher (main.go / dashboard.go).

Two types of output to stdout (with special prefixes):

  1. State events — progress for INIT/INGEST panels:
       @@RAGNA_STATE@@ {"phase":"init","step":"embedding","status":"done","elapsed":2.31}

  2. Log events — streaming logs for SERVER/PIPELINE/INGEST panels:
       @@RAGNA_LOG@@ {"channel":"pipeline","level":"INFO","msg":"..."}

When the env var RAGNA_PROGRESS != "1", all output is suppressed.
Python still functions normally without the Go launcher.

This file is DELIBERATELY split from main.py because it is imported by:
  - main.py     → install_go_log_handler, ready, fatal_error
  - pipeline.py → step_start/step_done for the INIT phase
  - embedder.py → step_start/step_done for the INGEST phase

If merged into main.py, a circular import would occur
(main → pipeline → main).
"""

import json
import logging
import os
import sys
import threading
import time

# =============================================================================
# GLOBAL STATE
# =============================================================================

_ENABLED = os.environ.get("RAGNA_PROGRESS", "1") == "1"
_LOCK = threading.Lock()


def is_enabled() -> bool:
    """Check whether the progress reporter is active."""
    return _ENABLED


def _write_line(line: str) -> None:
    """Write one line to stdout (thread-safe)."""
    try:
        with _LOCK:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except Exception:
        # Never let the progress reporter crash the application
        pass


# =============================================================================
# STATE EVENTS (for INIT / INGEST panels)
# =============================================================================

def _emit_state(payload: dict) -> None:
    """Send a state event as a JSON line with the @@RAGNA_STATE@@ prefix."""
    if not _ENABLED:
        return
    try:
        payload.setdefault("ts", time.time())
        line = "@@RAGNA_STATE@@ " + json.dumps(payload, ensure_ascii=False)
        _write_line(line)
    except Exception:
        pass


def phase_start(phase: str, total_steps: int = 0) -> None:
    """Start a phase (init, ingest, etc.)."""
    _emit_state({
        "phase": phase,
        "status": "start",
        "total_steps": total_steps,
    })


def step_start(phase: str, step: str, label: str = "") -> None:
    """A step has started."""
    _emit_state({
        "phase": phase,
        "step": step,
        "label": label or step,
        "status": "start",
    })


def step_done(phase: str, step: str, elapsed: float = 0.0) -> None:
    """A step finished successfully."""
    _emit_state({
        "phase": phase,
        "step": step,
        "status": "done",
        "elapsed": round(elapsed, 3),
    })


def step_error(phase: str, step: str, message: str = "") -> None:
    """A step failed."""
    _emit_state({
        "phase": phase,
        "step": step,
        "status": "error",
        "message": message,
    })


def phase_done(phase: str, elapsed: float = 0.0) -> None:
    """A phase finished successfully."""
    _emit_state({
        "phase": phase,
        "status": "done",
        "elapsed": round(elapsed, 3),
    })


def ready() -> None:
    """Signal: the server is ready to accept requests."""
    _emit_state({"phase": "ready"})


def fatal_error(message: str, phase: str = "unknown") -> None:
    """Fatal error — the server cannot start."""
    _emit_state({
        "phase": phase,
        "status": "fatal",
        "message": message,
    })


# =============================================================================
# LOG HANDLER (for SERVER / PIPELINE / INGEST panels)
# =============================================================================
# This handler is attached to the root logger. For each log record:
#   1. Determine the channel via contextvar (if dashboard.py is available)
#      or fall back to the logger-name mapping.
#   2. Format as a JSON line with the @@RAGNA_LOG@@ prefix.
#
# This handler is ADDITIVE — it does not replace other handlers. Normal
# logs (StreamHandler on the console, FileHandler to ragna.log) still run.
# =============================================================================

class GoLogHandler(logging.Handler):
    """
    Handler that sends logs as JSON lines to stdout for the Go launcher.
    """

    # Fallback mapping if dashboard._channel_for is not available.
    # Must stay in sync with CHANNEL_RULES in dashboard.py.
    _FALLBACK_RULES = (
        # SERVER — HTTP / controller / access logs only
        ("controller",   "server"),
        ("api.access",   "server"),
        ("uvicorn",      "server"),
        ("fastapi",      "server"),

        # INIT — lifecycle & startup
        ("main",         "init"),
        ("__main__",     "init"),

        # INGEST
        ("embedder",     "ingest"),
        ("prompt_guard", "ingest"),

        # PIPELINE
        ("ragna",        "pipeline"),
        ("pipeline",     "pipeline"),
    )

    def __init__(self):
        super().__init__()
        self.setLevel(logging.DEBUG)

    def emit(self, record: logging.LogRecord) -> None:
        if not _ENABLED:
            return
        try:
            channel = self._resolve_channel(record)
            payload = {
                "ts":      time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level":   record.levelname,
                "logger":  record.name,
                "channel": channel,
                "msg":     record.getMessage(),
            }
            line = "@@RAGNA_LOG@@ " + json.dumps(payload, ensure_ascii=False)
            _write_line(line)
        except Exception:
            self.handleError(record)

    @classmethod
    def _resolve_channel(cls, record: logging.LogRecord) -> str:
        """
        Determine the channel:
          1. Try dashboard._channel_for (consistent with the Python TUI).
          2. Fall back to the logger-name mapping.
          3. Default: "server".
        """
        # Try dashboard._channel_for if available
        try:
            from dashboard import _channel_for
            return _channel_for(record)
        except Exception:
            pass

        # Fallback mapping
        name = record.name
        for prefix, ch in cls._FALLBACK_RULES:
            if name == prefix or name.startswith(prefix + "."):
                return ch
        return "server"


def install_go_log_handler() -> None:
    """
    Attach GoLogHandler to the root logger.

    Idempotent — won't duplicate if already attached.
    """
    if not _ENABLED:
        return
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, GoLogHandler):
            return
    root.addHandler(GoLogHandler())