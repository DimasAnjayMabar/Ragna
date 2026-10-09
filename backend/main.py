# main.py
"""
Agribot FastAPI server — main entry point.

Setup order (important, don't change):
  1. Env var           → MUST be set before importing transformers/torch
  2. Logging/dashboard → before importing controller/pipeline
  3. Silence libraries → before model loading
  4. Import application → controller, pipeline
  5. Lifespan          → startup/shutdown hooks
"""

# =============================================================================
# BLOCK 1 — ENV VAR (MUST BE AT THE TOP, before any heavy imports)
# =============================================================================
import os

# Hugging Face & Transformers — disable progress bars & telemetry
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# ChromaDB — disable telemetry
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "false")

# =============================================================================
# BLOCK 2 — LOGGING / DASHBOARD SETUP
# =============================================================================
import logging
import sys

dashboard = None  # will be set if TTY mode + dashboard enabled

# Detect: are we running in an interactive terminal?
_is_tty = sys.stdout.isatty()
_dashboard_enabled = os.environ.get("RAGNA_DASHBOARD", "1") == "1"

if _is_tty and _dashboard_enabled:
    # ── Dashboard TUI mode ──────────────────────────────────────────────
    try:
        from dashboard import Dashboard

        # Root logger at DEBUG so the DashboardHandler (setLevel DEBUG)
        # actually receives DEBUG records. Noisy libraries are already
        # silenced in Block 3, and app loggers (_APP_LOGGERS) are forced
        # to INFO in Block 3b.
        logging.basicConfig(
            level=logging.DEBUG,
            handlers=[logging.NullHandler()],
            force=True,
        )

        dashboard = Dashboard(enable=True)
        dashboard.start()

        # File log kept for backup & grep (DEBUG level)
        _fh = logging.FileHandler("ragna.log", encoding="utf-8")
        _fh.setLevel(logging.DEBUG)
        _fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)-14s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logging.getLogger().addHandler(_fh)

    except ImportError:
        # dashboard.py missing → fall back to plain console logging
        if os.environ.get("RAGNA_PROGRESS", "0") == "1":
            logging.basicConfig(
                level=logging.INFO,
                handlers=[logging.NullHandler()],
                force=True,
            )
        else:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                force=True,
            )
        sys.stderr.write(
            "ℹ️  dashboard.py not found — using plain console logging.\n"
        )
else:
    # ── Plain console mode (pipe / CI / non-TTY) ────────────────────────
    if os.environ.get("RAGNA_PROGRESS", "0") == "1":
        # Go launcher mode — suppress default StreamHandler (which writes
        # to stderr). GoLogHandler will write JSON to stdout instead.
        logging.basicConfig(
            level=logging.INFO,
            handlers=[logging.NullHandler()],
            force=True,
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            force=True,
        )

# =============================================================================
# BLOCK 2b — PROGRESS REPORTER FOR THE GO LAUNCHER
# =============================================================================
# Active only when RAGNA_PROGRESS=1 (set by main.go when spawning).
# Otherwise no-op — zero overhead.
#
# This handler is ADDITIVE — it doesn't replace other handlers. Normal
# logs still get written by the existing handlers (dashboard or console).
# =============================================================================

if os.environ.get("RAGNA_PROGRESS", "0") == "1":
    try:
        from progress import install_go_log_handler
        install_go_log_handler()
    except ImportError:
        # progress.py missing — skip (non-fatal)
        pass


# =============================================================================
# BLOCK 3 — SILENCE NOISY LIBRARIES
# =============================================================================
# Lower the log level of libraries that normally spam at INFO/DEBUG.
# Important logs (WARNING/ERROR) still appear.
# =============================================================================

_NOISY_LOGGERS = {
    # Vector DB
    "chromadb":                            logging.WARNING,
    "chromadb.telemetry":                  logging.ERROR,
    "chromadb.telemetry.product.posthog":  logging.CRITICAL,
    "chromadb.api":                        logging.WARNING,
    "chromadb.segment":                    logging.WARNING,

    # Graph DB
    "neo4j":                               logging.WARNING,
    "neo4j.pool":                          logging.WARNING,
    "neo4j.io":                            logging.WARNING,

    # HTTP clients
    "urllib3":                             logging.WARNING,
    "urllib3.connectionpool":              logging.WARNING,
    "httpx":                               logging.WARNING,
    "httpcore":                            logging.WARNING,
    "requests":                            logging.WARNING,

    # ML / DL framework
    "transformers":                        logging.WARNING,
    "transformers.modeling_utils":         logging.ERROR,
    "transformers.configuration_utils":    logging.ERROR,
    "sentence_transformers":               logging.WARNING,
    "huggingface_hub":                     logging.WARNING,
    "torch":                               logging.WARNING,
    "torch._dynamo":                       logging.ERROR,
    "torch._inductor":                     logging.ERROR,

    # Vision
    "PIL":                                 logging.WARNING,
    "PIL.Image":                           logging.WARNING,

    # LLM / API clients
    "google.generativeai":                 logging.WARNING,
    "groq":                                logging.WARNING,

    # Dev tooling
    "watchfiles":                          logging.WARNING,
    "watchfiles.main":                     logging.WARNING,
    "uvicorn.error":                       logging.INFO,   # still visible
    "uvicorn.access":                      logging.INFO,   # still visible

    # Multipart upload
    "multipart":                           logging.WARNING,
    "python_multipart":                    logging.WARNING,
}

for _name, _lvl in _NOISY_LOGGERS.items():
    logging.getLogger(_name).setLevel(_lvl)


# =============================================================================
# BLOCK 3b — APPLICATION LOGGERS (controller, service, middleware)
# =============================================================================
# Ensure our own app loggers are always at INFO and propagate to the
# root logger → automatically routed to the dashboard.
#
# Mapping in dashboard.py:
#   "controller.*"  → SERVER panel  (see CHANNEL_RULES)
#   "service.*"     → default fallback "server"
#   "ragna"         → PIPELINE panel (see CHANNEL_RULES)
#   "api.access"    → default fallback "server" (from RequestLoggerMiddleware)
#   "main"          → INIT panel (CHANNEL_RULES: main → init)
# =============================================================================

_APP_LOGGERS = (
    "controller",
    "controller.controller_users",
    "controller.controller_chats",
    "service",
    "service.service_users",
    "service.service_chats",
    "middleware",
    "middleware.auth",
    "pipeline",
    "ragna",
    "embedder",
    "api.access",
)

for _app_name in _APP_LOGGERS:
    _lg = logging.getLogger(_app_name)
    _lg.setLevel(logging.INFO)
    _lg.propagate = True


logger = logging.getLogger("main")


# =============================================================================
# BLOCK 4 — APPLICATION IMPORTS
# =============================================================================
import time as _time
import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from controller.controller_users import router as users_router
from controller.controller_chats import router as chats_router
from pipeline import get_rag_pipeline


# =============================================================================
# BLOCK 5 — LIFESPAN (startup / shutdown)
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ─────────────────────────────────────────────────────────
    logger.info("Loading RAG pipeline at startup...")
    try:
        get_rag_pipeline()
        logger.info("RAG pipeline ready.")

        # Signal the Go launcher that the server is ready
        if os.environ.get("RAGNA_PROGRESS", "0") == "1":
            try:
                from progress import ready
                ready()
            except ImportError:
                pass

    except Exception as e:
        logger.critical("Failed to load RAG pipeline: %s", e)

        # Signal fatal error to the Go launcher
        if os.environ.get("RAGNA_PROGRESS", "0") == "1":
            try:
                from progress import fatal_error
                fatal_error(str(e), phase="init")
            except ImportError:
                pass

        # Stop the dashboard before raising so the terminal isn't messy
        if dashboard is not None:
            dashboard.stop()
        raise

    yield  # ── Server runs here ─────────────────────────────────────────

    # ── Shutdown ────────────────────────────────────────────────────────
    logger.info("Server shutdown — closing RAG pipeline...")
    try:
        from pipeline import _rag_pipeline
        if _rag_pipeline is not None:
            _rag_pipeline.close()
            logger.info("RAG pipeline closed.")
        else:
            logger.info("RAG pipeline already inactive, skipping.")
    except Exception as e:
        logger.warning("Failed to close RAG pipeline: %s", e)

    # Stop the dashboard last so shutdown logs remain visible
    if dashboard is not None:
        try:
            dashboard.stop()
        except Exception as e:
            sys.stderr.write(f"Warning: failed to stop dashboard: {e}\n")


# =============================================================================
# BLOCK 6 — FASTAPI APP
# =============================================================================

app = FastAPI(
    title="Agribot API",
    description="Backend API for the Agribot AI-powered chatbot",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ────────────────────────────────────────────────────────────────
# NOTE:
#   Combining allow_origins=["*"] + allow_credentials=True is FORBIDDEN
#   by browsers. If forced, preflight OPTIONS fails silently → the real
#   request is never sent → DevTools Network shows nothing (the classic
#   "Flutter Web no network" symptom).
#
#   We use origin_regex to be flexible in dev (localhost, LAN, etc.)
#   without wildcard + credentials. Since auth uses Bearer tokens (not
#   cookies), allow_credentials=False is fine.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://.*",   # permissive for dev; tighten in prod
    allow_credentials=False,             # Bearer token → no cookies needed
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=600,                         # cache preflight for 10 minutes
)


# ── Request Logger Middleware ───────────────────────────────────────────
# Every HTTP request in/out is logged via the "api.access" logger
# → routed to the SERVER panel automatically (fallback mapping default).
#
# IMPORTANT: must be added AFTER CORS so it becomes the outermost
# middleware (sees all requests, including preflight OPTIONS).
# =============================================================================

_access_logger = logging.getLogger("api.access")


class RequestLoggerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Preflight OPTIONS — short log only (avoid spamming the panel)
        if request.method == "OPTIONS":
            response = await call_next(request)
            _access_logger.debug(
                "↳ OPTIONS %s → %d", request.url.path, response.status_code
            )
            return response

        client = request.client.host if request.client else "unknown"
        ua     = request.headers.get("user-agent", "-")[:60]
        start  = _time.perf_counter()

        _access_logger.info(
            "→ %s %s  client=%s  ua=%s",
            request.method, request.url.path, client, ua,
        )

        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed = (_time.perf_counter() - start) * 1000
            _access_logger.error(
                "✗ %s %s  client=%s  %.0fms  error=%s",
                request.method, request.url.path, client, elapsed, exc,
            )
            raise

        elapsed = (_time.perf_counter() - start) * 1000
        level   = logging.WARNING if response.status_code >= 400 else logging.INFO
        _access_logger.log(
            level,
            "← %s %s  %d  %.0fms  client=%s",
            request.method, request.url.path,
            response.status_code, elapsed, client,
        )
        return response


app.add_middleware(RequestLoggerMiddleware)


# ── Exception handler ───────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Preflight OPTIONS MUST always get a CORS-valid response.
    # If this handler swallows it, the browser never sends the real
    # request → "Flutter Web no network".
    if request.method == "OPTIONS":
        return JSONResponse(status_code=200, content={})

    logger.error("Unhandled error on %s %s → %s", request.method, request.url, exc)
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": "Internal server error"},
    )


# ── Routers ─────────────────────────────────────────────────────────────
app.include_router(users_router)
app.include_router(chats_router)


# ── Root endpoint ───────────────────────────────────────────────────────
@app.get("/", tags=["Root"])
async def root():
    return {
        "status":  "online",
        "message": "Agribot Backend is running successfully",
        "agent":   "FastAPI",
    }


# =============================================================================
# BLOCK 6b — ADMIN ENDPOINTS (OPTIONAL)
# =============================================================================
# These endpoints are useful when prompt_guard.py is run via Go (a
# separate process) — the server doesn't know about new instructions.
# Call this endpoint after embedding instructions to refresh the cache.
#
# ENABLE ONLY IF NEEDED (uncomment this block).
# Protect with an API key / auth before using in production.
# =============================================================================

# from fastapi import Header
#
# @app.post("/admin/invalidate-instruction-cache", tags=["Admin"])
# async def invalidate_instruction_cache_endpoint(
#     x_admin_key: str = Header(default=""),
# ):
#     # Change "secret" to a secure API key
#     if x_admin_key != os.environ.get("ADMIN_API_KEY", "secret"):
#         return JSONResponse(status_code=403, content={"success": False, "message": "Forbidden"})
#
#     try:
#         from pipeline import invalidate_instruction_cache
#         invalidate_instruction_cache()
#         logger.info("[Admin] Instruction cache invalidated via endpoint.")
#         return {"success": True, "message": "Instruction cache invalidated."}
#     except Exception as e:
#         logger.exception("[Admin] Failed to invalidate cache: %s", e)
#         return JSONResponse(status_code=500, content={"success": False, "message": str(e)})


# =============================================================================
# BLOCK 7 — ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    # Default OFF — so `python main.py` run directly doesn't emit JSON.
    # main.go sets RAGNA_PROGRESS=1 when spawning.
    os.environ.setdefault("RAGNA_PROGRESS", "0")

    # reload=True conflicts with the dashboard.
    # The uvicorn reloader runs main.py in both parent and child processes,
    # causing Dashboard() to be instantiated multiple times → multiple
    # renderer threads → CLEAR_SCREEN races → screen glitches.
    #
    # When dashboard is active, force reload=False. When inactive
    # (non-TTY / RAGNA_DASHBOARD=0), reload stays True for dev convenience.
    _use_reload = dashboard is None

    if not _use_reload:
        logger.info("[Entry] Dashboard active — reload disabled (reload=False).")
    else:
        logger.info("[Entry] Dashboard inactive — reload=True for dev.")

    # Important: log_config=None → uvicorn doesn't override our logging setup.
    # All uvicorn logs go to the root logger → routed to the dashboard.
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=_use_reload,
        log_config=None,
    )