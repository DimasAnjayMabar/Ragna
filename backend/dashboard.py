# backend/dashboard.py
"""
4-panel TUI dashboard for the RAGNA server.

Panels:
  ⚙️  INIT      — Model loading (embedding, reranker, NLP, Groq client)
  🖥️  SERVER    — HTTP requests, FastAPI, uvicorn, controller
  🧠  PIPELINE  — Query flow: retrieval → rerank → generate
  📥  INGEST    — PDF upload, embedder, prompt_guard

Usage in main.py:
    from dashboard import Dashboard
    dashboard = Dashboard(enable=True)
    dashboard.start()
    ...
    dashboard.stop()

Usage in other modules (pipeline.py, embedder.py):
    from dashboard import log_context
    with log_context("init"):
        log.info("Loading model...")   # → INIT panel
    with log_context("pipeline"):
        log.info("Query received...")  # → PIPELINE panel
    with log_context("ingest"):
        log.info("Scanning PDF...")    # → INGEST panel

Zero dependency — stdlib only.
"""

import contextvars
import logging
import shutil
import sys
import threading
import time
import unicodedata
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Deque, Dict, Optional

# =============================================================================
# ANSI ESCAPE CODES
# =============================================================================

ESC          = "\x1b"
RESET        = f"{ESC}[0m"
BOLD         = f"{ESC}[1m"
DIM          = f"{ESC}[2m"
CLEAR_SCREEN = f"{ESC}[2J{ESC}[H"
HIDE_CURSOR  = f"{ESC}[?25l"
SHOW_CURSOR  = f"{ESC}[?25h"

FG = {
    "grey":    f"{ESC}[38;5;245m",
    "white":   f"{ESC}[37m",
    "cyan":    f"{ESC}[36m",
    "green":   f"{ESC}[32m",
    "yellow":  f"{ESC}[33m",
    "red":     f"{ESC}[31m",
    "magenta": f"{ESC}[35m",
    "blue":    f"{ESC}[34m",
}

LEVEL_COLORS = {
    logging.DEBUG:    FG["grey"],
    logging.INFO:     FG["cyan"],
    logging.WARNING:  FG["yellow"],
    logging.ERROR:    FG["red"],
    logging.CRITICAL: BOLD + FG["red"],
}

LEVEL_SHORT = {
    logging.DEBUG:    "DBG ",
    logging.INFO:     "INFO",
    logging.WARNING:  "WARN",
    logging.ERROR:    "ERR ",
    logging.CRITICAL: "CRIT",
}


# =============================================================================
# VISUAL LENGTH HELPER — P3
# =============================================================================
# len() counts codepoints, not visual width. Emoji and CJK characters are
# 2 columns wide in most terminals. This helper gives a better estimate
# so the dashboard layout doesn't drift.
#
# Note: not 100% precise (depends on font & terminal), but much better
# than plain len().
# =============================================================================

def _vlen(s: str) -> int:
    """Compute visual width of a string (estimate). Emoji/CJK = 2 cols."""
    width = 0
    for ch in s:
        # Zero-width joiner & variation selector
        if ch in ("\u200b", "\u200d", "\ufe0f"):
            continue
        # Combining marks
        if unicodedata.combining(ch):
            continue
        # East Asian Wide/Fullwidth → 2 columns
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


# =============================================================================
# CONTEXT TAG — determines channel at runtime
# =============================================================================
#
# Context tag values:
#   "init"     → INIT panel
#   "server"   → SERVER panel
#   "pipeline" → PIPELINE panel
#   "ingest"   → INGEST panel
#
# If no context tag is set → fall back to logger-name mapping.
# =============================================================================

_context_tag: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "ragna_log_context", default=None
)


@contextmanager
def log_context(tag: str):
    """
    Context manager: all logs inside route to the `tag` channel.

    Nested — the innermost tag wins.

    Thread-safe (contextvars) and async-safe (asyncio fully supported).
    """
    token = _context_tag.set(tag)
    try:
        yield
    finally:
        _context_tag.reset(token)


# =============================================================================
# FALLBACK MAPPING (when no context tag is set)
# =============================================================================
# Order matters: more specific prefixes must be listed first.
# Matching is done with startswith(prefix) for "prefix." or exact match.
# =============================================================================

CHANNEL_RULES = [
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
]

CHANNEL_TITLES = {
    "init":     "⚙️  INIT (Model Loading)",
    "server":   "🖥️  SERVER",
    "pipeline": "🧠 PIPELINE",
    "ingest":   "📥 INGEST",
}

CHANNEL_COLORS = {
    "init":     FG["yellow"],
    "server":   FG["blue"],
    "pipeline": FG["magenta"],
    "ingest":   FG["cyan"],
}

VALID_CHANNELS = set(CHANNEL_TITLES.keys())


def _channel_for(record: logging.LogRecord) -> str:
    """
    Determine channel:
      1. Context tag wins
      2. Fallback: logger-name mapping
      3. Default: "server"
    """
    tag = _context_tag.get()
    if tag in VALID_CHANNELS:
        return tag

    name = record.name
    for prefix, ch in CHANNEL_RULES:
        if name == prefix or name.startswith(prefix + "."):
            return ch
    return "server"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class LogEntry:
    time_str: str
    level:    int
    message:  str


# =============================================================================
# LOGGING HANDLER — writes to per-channel buffers (not directly to stdout)
# =============================================================================

class DashboardHandler(logging.Handler):
    """
    Captures logs from the root logger and places them in per-channel
    queues. The renderer thread periodically redraws.

    IMPORTANT: does not write to stdout here — only enqueues.
    This prevents race conditions with the renderer thread.
    """

    def __init__(self, dashboard: "Dashboard"):
        super().__init__()
        self.dashboard = dashboard

    def emit(self, record: logging.LogRecord) -> None:
        try:
            channel = _channel_for(record)
            entry = LogEntry(
                time_str=time.strftime("%H:%M:%S", time.localtime(record.created)),
                level=record.levelno,
                message=record.getMessage(),
            )
            # deque.append is atomic in CPython — safe without a lock
            self.dashboard.buffers[channel].append(entry)
            self.dashboard.dirty = True
        except Exception:
            self.handleError(record)


# =============================================================================
# DASHBOARD — 4 panels
# =============================================================================

class Dashboard:
    """
    Layout:

    ┌────────────────────────┬──────────────────────────┐
    │  ⚙️  INIT              │  🖥️  SERVER              │
    │                        │                          │
    ├────────────────────────┴──────────────────────────┤
    │  🧠  PIPELINE                                     │
    │                                                   │
    ├───────────────────────────────────────────────────┤
    │  📥  INGEST                                       │
    └───────────────────────────────────────────────────┘
    """

    BUFFER_SIZE      = 500       # max entries per panel
    REFRESH_INTERVAL = 0.25      # seconds — redraw if there are new logs
    MIN_WIDTH        = 100       # minimum terminal width (columns)
    MIN_HEIGHT       = 30        # minimum terminal height (rows)

    def __init__(self, enable: bool = True):
        # Auto-disable if stdout is not a TTY
        self.enable = bool(enable) and sys.stdout.isatty()

        self.buffers: Dict[str, Deque[LogEntry]] = {
            "init":     deque(maxlen=self.BUFFER_SIZE),
            "server":   deque(maxlen=self.BUFFER_SIZE),
            "pipeline": deque(maxlen=self.BUFFER_SIZE),
            "ingest":   deque(maxlen=self.BUFFER_SIZE),
        }

        self.dirty = True
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._handler: Optional[DashboardHandler] = None
        self._start_time = time.time()
        self._lock = threading.Lock()

        # Store last terminal size for redraw on resize
        self._last_size = (0, 0)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Attach handler to root logger, hide cursor, start renderer."""
        if not self.enable:
            return

        # Attach handler to root logger
        self._handler = DashboardHandler(self)
        self._handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self._handler)

        # Hide cursor
        try:
            sys.stdout.write(HIDE_CURSOR)
            sys.stdout.flush()
        except Exception:
            pass

        # Start renderer thread
        self._thread = threading.Thread(
            target=self._render_loop,
            name="RagnaDashboard",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop renderer, restore cursor, clean up."""
        if not self.enable:
            return

        self._stop.set()

        if self._thread is not None:
            self._thread.join(timeout=2.0)

        # Remove handler from root logger
        if self._handler is not None:
            try:
                logging.getLogger().removeHandler(self._handler)
            except Exception:
                pass
            self._handler = None

        # Restore cursor
        try:
            sys.stdout.write(SHOW_CURSOR)
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass

    # ── Render loop ───────────────────────────────────────────────────────

    def _render_loop(self) -> None:
        """
        Background loop: redraw every REFRESH_INTERVAL if there are new logs.

        P1: set self.dirty = False BEFORE _draw() to prevent logs that
            arrive during _draw() from being "lost" from the display.
            If a log arrives during _draw(), dirty is set to True again
            by the handler → the next redraw picks it up.
        """
        while not self._stop.is_set():
            try:
                if self.dirty:
                    self.dirty = False
                    self._draw()
                time.sleep(self.REFRESH_INTERVAL)
            except Exception:
                # Never crash the renderer → sleep 1 second on error
                time.sleep(1.0)

    # ── Terminal utils ────────────────────────────────────────────────────

    def _term_size(self) -> tuple:
        """Get terminal size. Fallback (140, 44) if it fails."""
        try:
            size = shutil.get_terminal_size(fallback=(140, 44))
            return size.columns, size.lines
        except Exception:
            return 140, 44

    # ── Drawing ───────────────────────────────────────────────────────────

    def _draw(self) -> None:
        """Draw the whole dashboard in a single write."""
        w, h = self._term_size()

        # Skip if the terminal is too small
        if w < self.MIN_WIDTH or h < self.MIN_HEIGHT:
            return

        # Vertical layout
        header_h  = 2                      # title + separator
        footer_h  = 2                      # separator + hint
        content_h = h - header_h - footer_h

        # Height distribution: INIT+SERVER = 1/3, PIPELINE = 1/3, INGEST = 1/3
        top_h    = max(6, content_h // 3)
        mid_h    = max(6, content_h // 3)
        bottom_h = max(6, content_h - top_h - mid_h)

        # Widths for the top row
        left_w  = w // 2
        right_w = w - left_w - 1           # -1 for the divider "│"

        # Build buffer — write once to minimize flicker
        buf = []
        buf.append(CLEAR_SCREEN)
        buf.extend(self._draw_header(w))
        buf.extend(self._draw_pair("init", "server", left_w, right_w, top_h))
        buf.extend(self._draw_full("pipeline", w, mid_h))
        buf.extend(self._draw_full("ingest", w, bottom_h))
        buf.extend(self._draw_footer(w))

        try:
            with self._lock:
                sys.stdout.write("".join(buf))
                sys.stdout.flush()
        except Exception:
            pass

        self._last_size = (w, h)

    def _draw_header(self, w: int) -> list:
        """
        Title row + horizontal rule.

        P3: uses _vlen() to measure title & right text, so padding is
            correct even with emoji.
        """
        uptime = time.time() - self._start_time
        hh, rem = divmod(int(uptime), 3600)
        mm, ss  = divmod(rem, 60)
        up_str  = f"{hh:02d}:{mm:02d}:{ss:02d}"

        title = f"{BOLD}{FG['cyan']}🤖 RAGNA Server Dashboard{RESET}"
        right = f"{FG['grey']}Uptime: {up_str}{RESET}"

        title_visual = _vlen("🤖 RAGNA Server Dashboard")
        right_visual = _vlen(f"Uptime: {up_str}")
        pad = max(1, w - title_visual - right_visual - 2)

        line1 = f"{title}{' ' * pad}{right}\n"
        line2 = f"{FG['grey']}{'─' * w}{RESET}\n"
        return [line1, line2]

    def _draw_pair(self, left_ch: str, right_ch: str, lw: int, rw: int, h: int) -> list:
        """Draw two panels side by side."""
        left  = self._render_panel(left_ch, lw, h)
        right = self._render_panel(right_ch, rw, h)
        divider = f"{FG['grey']}│{RESET}"

        lines = []
        for i in range(h):
            l = left[i]  if i < len(left)  else " " * lw
            r = right[i] if i < len(right) else " " * rw
            lines.append(f"{l}{divider}{r}\n")
        return lines

    def _draw_full(self, ch: str, w: int, h: int) -> list:
        """Draw a single full-width panel."""
        panel = self._render_panel(ch, w, h)
        return [line + "\n" for line in panel]

    def _render_panel(self, channel: str, width: int, height: int) -> list:
        """
        Render one panel with height `height` rows.
        Format: row 0 = title, row 1 = separator, rest = log entries.

        P1: title row padded to panel width so the "│" divider stays
            aligned across rows.
        P3: uses _vlen() to measure title width.
        """
        title = CHANNEL_TITLES.get(channel, channel.upper())
        color = CHANNEL_COLORS.get(channel, FG["white"])

        content_lines = height - 2
        if content_lines < 1:
            return [" " * width] * height

        # Get latest entries, display in chronological order (top → bottom)
        entries = list(self.buffers[channel])[-content_lines:]
        rendered = []

        # ── Row 0 — title (padded to width) ─────────────────────────
        title_visual = 2 + _vlen(title)
        pad_title    = max(0, width - title_visual)
        header = f"  {color}{BOLD}{title}{RESET}" + (" " * pad_title)
        rendered.append(header)

        # ── Row 1 — separator ───────────────────────────────────────
        sep = f"{FG['grey']}{'─' * width}{RESET}"
        rendered.append(sep)

        # ── Content rows ────────────────────────────────────────────
        for e in entries:
            rendered.append(self._format_entry(e, width))

        # ── Padding if needed ───────────────────────────────────────
        while len(rendered) < height:
            rendered.append(" " * width)

        return rendered

    def _format_entry(self, e: LogEntry, width: int) -> str:
        """
        Format one entry:
            HH:MM:SS  LEVEL  message

        Columns: 8 + 2 + 4 + 2 = 16 prefix characters, rest for message.

        P1: max_msg = width - 17, pad = width - visual_len (no -2).
            Previously it used width - 18 and width - visual_len - 2,
            making each row 2 columns short of the separator → slanted lines.

        P3: uses _vlen() to measure message width (for messages with
            emoji / CJK characters).
        """
        time_col  = f"{FG['grey']}{e.time_str}{RESET}"
        level_col = LEVEL_COLORS.get(e.level, "")
        level_str = LEVEL_SHORT.get(e.level, "???")
        msg       = e.message

        # Replace newlines with spaces (entries must be single-line)
        msg = msg.replace("\n", " ").replace("\r", " ")

        # P1: max_msg = width - 17
        #   prefix visual = 8 (time) + 2 + 4 (level) + 2 = 16
        #   reserve 1 column for safety → width - 17.
        max_msg = max(10, width - 17)
        if len(msg) > max_msg:
            msg = msg[:max_msg - 1] + "…"

        msg_visual = _vlen(msg)

        # Total visual length: 16 (prefix) + msg_visual
        visual_len = 16 + msg_visual

        # P1: pad = width - visual_len (no -2)
        pad = " " * max(0, width - visual_len)

        return f"{time_col}  {level_col}{level_str}{RESET}  {msg}{pad}"

    def _draw_footer(self, w: int) -> list:
        """Separator row + hint."""
        line = f"{FG['grey']}{'─' * w}{RESET}\n"
        hint = (
            f"{DIM}  Ctrl+C to stop   "
            f"│   RAGNA_DASHBOARD=0 to disable   "
            f"│   Ctrl+Shift+P in VSCode to screenshot{RESET}\n"
        )
        return [line, hint]


# =============================================================================
# SELF-TEST (run directly: python backend/dashboard.py)
# =============================================================================

if __name__ == "__main__":
    """
    Demo: run this script to see the dashboard.
    Sends synthetic logs to all 4 panels for 15 seconds.
    """
    import random

    logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()], force=True)

    db = Dashboard(enable=True)
    db.start()

    try:
        log = logging.getLogger("demo")

        # ── INIT phase ────────────────────────────────────────────────────
        with log_context("init"):
            log.info("Starting RAG model loading...")
            time.sleep(0.4)
            log.info("[1/4] Embedding: intfloat/multilingual-e5-large → cuda")
            time.sleep(0.6)
            log.info("[1/4] Embedding ready  (2.31s)")
            time.sleep(0.4)
            log.info("[2/4] Reranker ready  (4.87s)")
            time.sleep(0.4)
            log.info("[3/4] Groq client ready.")
            time.sleep(0.4)
            log.info("[4/4] NLP ready  (7.12s)")
            log.info("✓ All models loaded successfully.")

        # ── SERVER phase ──────────────────────────────────────────────────
        with log_context("server"):
            log.info("Uvicorn running on http://0.0.0.0:8000")
            for i in range(3):
                time.sleep(0.5)
                log.info("GET /health 200")

        # ── PIPELINE phase ────────────────────────────────────────────────
        for q in range(3):
            with log_context("pipeline"):
                log.info("═" * 40)
                log.info("[Knowledge] Query: 'gejala blas pada padi'  chat_id=%d", q)
                time.sleep(0.3)
                log.info("[Stage 1] ChromaDB retrieval (k=12)...")
                time.sleep(0.3)
                log.info("[Stage 1] 12 candidates found  (0.045s)")
                time.sleep(0.3)
                log.info("[Stage 3] Top 6 selected  (0.612s)")
                time.sleep(0.4)
                log.info("[Groq] ✓ Done — 47 chunks  2.13s")
                log.info("[Knowledge] Pipeline done — 2.81s")

            # Interleave with server
            with log_context("server"):
                time.sleep(0.2)
                log.info("POST /chat/stream 200")

        # ── INGEST phase ──────────────────────────────────────────────────
        with log_context("ingest"):
            log.info("Processing: jurnal_blas_padi.pdf")
            log.info("File hash (MD5): a3f2b1c4...")
            time.sleep(0.4)
            log.info("[Scanner] Starting line scan on 142 lines for jurnal_blas_padi.pdf")
            time.sleep(0.5)
            log.warning("[Scanner] 2 suspicious lines detected in jurnal_blas_padi.pdf")
            time.sleep(0.3)
            log.info("[Scanner] Line scan done — 2/142 suspicious lines")
            time.sleep(0.4)
            log.info("ChromaDB: 23 konten_chunk ingested into 'konten_isi'")

        # ── Warning & error demo ─────────────────────────────────────────
        with log_context("server"):
            time.sleep(0.5)
            log.warning("Rate limit approaching (4500/6000 TPM)")
            time.sleep(0.5)
            log.error("Failed to connect to Neo4j — retrying in 5s")

        # Hold the dashboard briefly so it's visible
        log.info("Demo done — dashboard will close in 3 seconds...")
        time.sleep(3)

    except KeyboardInterrupt:
        pass
    finally:
        db.stop()
        print("Dashboard stopped.")