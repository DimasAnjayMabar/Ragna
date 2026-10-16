# main.py
"""
BatikBot FastAPI server — main entry point.

Setup order (important, don't change):
  1. Env var           → MUST be set before importing transformers/torch
  2. Logging/dashboard → before importing controller/pipeline
  3. Silence libraries → before model loading
  4. PromptRegistry    → eager load prompt dari Neo4j (SEBELUM pipeline)
  5. Import application → controller, pipeline
  6. Lifespan          → startup/shutdown hooks
"""

# =============================================================================
# BLOCK 1 — ENV VAR (MUST BE AT THE TOP, before any heavy imports)
# =============================================================================
import os
import sys

_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))   # backend/
_PARENT_DIR = os.path.dirname(_THIS_DIR)                    # chatbot3/

if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)


os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("CHROMA_TELEMETRY_ENABLED", "false")

# =============================================================================
# BLOCK 2 — LOGGING / DASHBOARD SETUP
# =============================================================================
import logging

dashboard = None

_is_tty = sys.stdout.isatty()
_dashboard_enabled = os.environ.get("RAGNA_DASHBOARD", "1") == "1"

if _is_tty and _dashboard_enabled:
    try:
        from dashboard import Dashboard

        logging.basicConfig(
            level=logging.DEBUG,
            handlers=[logging.NullHandler()],
            force=True,
        )

        dashboard = Dashboard(enable=True)
        dashboard.start()

        _fh = logging.FileHandler("ragna.log", encoding="utf-8")
        _fh.setLevel(logging.DEBUG)
        _fh.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)-14s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logging.getLogger().addHandler(_fh)

    except ImportError:
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

# =============================================================================
# BLOCK 2b — PROGRESS REPORTER FOR THE GO LAUNCHER
# =============================================================================

if os.environ.get("RAGNA_PROGRESS", "0") == "1":
    try:
        from progress import install_go_log_handler
        install_go_log_handler()
    except ImportError:
        pass


# =============================================================================
# BLOCK 3 — SILENCE NOISY LIBRARIES
# =============================================================================

_NOISY_LOGGERS = {
    "chromadb":                            logging.WARNING,
    "chromadb.telemetry":                  logging.ERROR,
    "chromadb.telemetry.product.posthog":  logging.CRITICAL,
    "chromadb.api":                        logging.WARNING,
    "chromadb.segment":                    logging.WARNING,
    "neo4j":                               logging.WARNING,
    "neo4j.pool":                          logging.WARNING,
    "neo4j.io":                            logging.WARNING,
    "urllib3":                             logging.WARNING,
    "urllib3.connectionpool":              logging.WARNING,
    "httpx":                               logging.WARNING,
    "httpcore":                            logging.WARNING,
    "requests":                            logging.WARNING,
    "transformers":                        logging.WARNING,
    "transformers.modeling_utils":         logging.ERROR,
    "transformers.configuration_utils":    logging.ERROR,
    "sentence_transformers":               logging.WARNING,
    "huggingface_hub":                     logging.WARNING,
    "torch":                               logging.WARNING,
    "torch._dynamo":                       logging.ERROR,
    "torch._inductor":                     logging.ERROR,
    "PIL":                                 logging.WARNING,
    "PIL.Image":                           logging.WARNING,
    "google.generativeai":                 logging.WARNING,
    "groq":                                logging.WARNING,
    "watchfiles":                          logging.WARNING,
    "watchfiles.main":                     logging.WARNING,
    "uvicorn.error":                       logging.INFO,
    "uvicorn.access":                      logging.INFO,
    "multipart":                           logging.WARNING,
    "python_multipart":                    logging.WARNING,
}

for _name, _lvl in _NOISY_LOGGERS.items():
    logging.getLogger(_name).setLevel(_lvl)


# =============================================================================
# BLOCK 3b — APPLICATION LOGGERS
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
# BLOCK 4 — PROMPT REGISTRY (Eager Load)
# =============================================================================
# PromptRegistry: eager load semua prompt dari Neo4j saat startup.
# Fallback ke FALLBACK_PROMPTS[config.py] kalau Neo4j kosong/gagal.
#
# Zero I/O saat runtime — semua dari in-memory dict.
# Reload via POST /admin/reload-prompts (dipanggil editor Go setelah embed).
#
# Struktur cache: {lang: {slot: text}}
# Slot names:
#   bot_name, bot_identity, social_guard_rail, knowledge_guard_rail,
#   product_guard_rail, memory_block_guard_rail, memory_summary_block
# =============================================================================

import threading
from typing import Dict, Optional

from neo4j import GraphDatabase
from config import FALLBACK_PROMPTS, CONFIG


class PromptRegistry:
    """
    Singleton registry untuk semua prompt LLM.

    - load_all()      : eager load dari Neo4j + fallback config
    - get(lang, slot) : runtime lookup (no I/O, no lock)
    - reload()        : panggil ulang load_all() — dipakai endpoint admin
    """

    _instance: Optional["PromptRegistry"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get(cls) -> "PromptRegistry":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # Slot yang WAJIB ada di setiap lang. Kalau tidak ada di Neo4j,
    # akan diambil dari FALLBACK_PROMPTS.
    REQUIRED_SLOTS = (
        "bot_name",
        "bot_identity",
        "social_guard_rail",
        "knowledge_guard_rail",
        "product_guard_rail",
        "memory_block_guard_rail",
        "memory_summary_block",
    )

    def __init__(self):
        self._cache: Dict[str, Dict[str, str]] = {}
        self._stats: Dict[str, int] = {
            "from_neo4j": 0,
            "from_fallback": 0,
            "missing": 0,
        }
        self._loaded = False
        self._lock = threading.Lock()
        self._driver = None

    # ── Startup load ──────────────────────────────────────────────────────

    def load_all(self) -> None:
        """
        Eager load semua prompt dari Neo4j.
        Kalau Neo4j kosong / gagal → fallback ke FALLBACK_PROMPTS.
        Dipanggil SEKALI saat startup, sebelum server ready.
        """
        with self._lock:
            logger.info("[PromptRegistry] Eager loading prompts from Neo4j...")

            new_cache: Dict[str, Dict[str, str]] = {}
            stats = {"from_neo4j": 0, "from_fallback": 0, "missing": 0}

            # ── 1. Query Neo4j ────────────────────────────────────────────
            try:
                if self._driver is None:
                    self._driver = GraphDatabase.driver(
                        CONFIG["neo4j_uri"],
                        auth=(CONFIG["neo4j_user"], CONFIG["neo4j_password"]),
                    )
                with self._driver.session() as session:
                    records = session.run("""
                        MATCH (i:Instruction)
                        RETURN i.lang AS lang, i.slot AS slot, i.text AS text
                    """).data()

                for r in records:
                    lang = r.get("lang")
                    slot = r.get("slot")
                    text = r.get("text")
                    if lang and slot and text:
                        new_cache.setdefault(lang, {})[slot] = text
                        stats["from_neo4j"] += 1

                logger.info(
                    "[PromptRegistry] Neo4j returned %d slots across %d langs",
                    stats["from_neo4j"], len(new_cache),
                )
            except Exception as exc:
                logger.error("[PromptRegistry] Neo4j query failed: %s", exc)
                logger.warning(
                    "[PromptRegistry] Falling back to FALLBACK_PROMPTS for all slots"
                )

            # ── 2. Fallback untuk slot yang kosong ────────────────────────
            # Iterasi semua lang yang ada di FALLBACK_PROMPTS (id, en)
            for lang, fallback_slots in FALLBACK_PROMPTS.items():
                for slot in self.REQUIRED_SLOTS:
                    if slot in new_cache.get(lang, {}):
                        continue  # sudah ada dari Neo4j
                    text = fallback_slots.get(slot)
                    if text:
                        new_cache.setdefault(lang, {})[slot] = text
                        stats["from_fallback"] += 1
                    else:
                        stats["missing"] += 1
                        logger.warning(
                            "[PromptRegistry] Slot '%s' missing for lang='%s' "
                            "(no Neo4j, no fallback)",
                            slot, lang,
                        )

            # ── 3. Atomic swap ────────────────────────────────────────────
            self._cache = new_cache
            self._stats = stats
            self._loaded = True

            logger.info(
                "[PromptRegistry] Loaded — from_neo4j=%d  from_fallback=%d  "
                "missing=%d  langs=%s",
                stats["from_neo4j"],
                stats["from_fallback"],
                stats["missing"],
                sorted(new_cache.keys()),
            )

    # ── Reload (endpoint admin) ───────────────────────────────────────────

    def reload(self) -> Dict[str, int]:
        """Reload cache — dipanggil setelah editor Go selesai embed."""
        logger.info("[PromptRegistry] Reload requested via admin endpoint")
        self.load_all()
        return dict(self._stats)

    # ── Runtime read (no lock, no I/O) ────────────────────────────────────

    def lookup(self, lang: str, slot: str) -> Optional[str]:
        """
        Ambil prompt dari cache. Pure dict lookup, zero I/O.
        Return None kalau tidak ada.
        """
        return self._cache.get(lang, {}).get(slot)

    def has(self, lang: str, slot: str) -> bool:
        return slot in self._cache.get(lang, {})

    def list_slots(self, lang: str) -> list:
        return sorted(self._cache.get(lang, {}).keys())

    @property
    def stats(self) -> Dict[str, int]:
        return dict(self._stats)

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def close(self):
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
            self._driver = None


# =============================================================================
# BLOCK 5 — APPLICATION IMPORTS
# =============================================================================
import time as _time
import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Header
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from controller.controller_users import router as users_router
from controller.controller_chats import router as chats_router
from pipeline import get_rag_pipeline


# =============================================================================
# BLOCK 6 — LIFESPAN (startup / shutdown)
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ─────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("BatikBot server — startup sequence")
    logger.info("=" * 60)

    # Step 1: Eager load PromptRegistry — WAJIB sebelum pipeline
    logger.info("Step 1/2: Eager loading PromptRegistry...")
    try:
        registry = PromptRegistry.get()
        registry.load_all()

        if not registry.is_loaded:
            raise RuntimeError("PromptRegistry gagal load — server tidak boleh start")

        logger.info(
            "✓ PromptRegistry ready — neo4j=%d fallback=%d missing=%d",
            registry.stats["from_neo4j"],
            registry.stats["from_fallback"],
            registry.stats["missing"],
        )
    except Exception as e:
        logger.critical("Failed to load PromptRegistry: %s", e)
        if os.environ.get("RAGNA_PROGRESS", "0") == "1":
            try:
                from progress import fatal_error
                fatal_error(str(e), phase="init")
            except ImportError:
                pass
        if dashboard is not None:
            dashboard.stop()
        raise

    # Step 2: Load RAG pipeline
    logger.info("Step 2/2: Loading RAG pipeline...")
    try:
        get_rag_pipeline()
        logger.info("✓ RAG pipeline ready.")

        if os.environ.get("RAGNA_PROGRESS", "0") == "1":
            try:
                from progress import ready
                ready()
            except ImportError:
                pass

    except Exception as e:
        logger.critical("Failed to load RAG pipeline: %s", e)

        if os.environ.get("RAGNA_PROGRESS", "0") == "1":
            try:
                from progress import fatal_error
                fatal_error(str(e), phase="init")
            except ImportError:
                pass

        if dashboard is not None:
            dashboard.stop()
        raise

    logger.info("=" * 60)
    logger.info("✓ Server ready — accepting requests")
    logger.info("=" * 60)

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

    # Close PromptRegistry
    try:
        registry = PromptRegistry.get()
        registry.close()
        logger.info("PromptRegistry closed.")
    except Exception as e:
        logger.warning("Failed to close PromptRegistry: %s", e)

    if dashboard is not None:
        try:
            dashboard.stop()
        except Exception as e:
            sys.stderr.write(f"Warning: failed to stop dashboard: {e}\n")


# =============================================================================
# BLOCK 7 — FASTAPI APP
# =============================================================================

app = FastAPI(
    title="BatikBot API",
    description="Backend API for the BatikBot AI-powered chatbot",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://.*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=600,
)


# ── Request Logger Middleware ───────────────────────────────────────────
_access_logger = logging.getLogger("api.access")


class RequestLoggerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
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
        "message": "BatikBot Backend is running successfully",
        "agent":   "FastAPI",
    }


# =============================================================================
# BLOCK 8 — ADMIN ENDPOINTS
# =============================================================================

@app.post("/admin/reload-prompts", tags=["Admin"])
async def reload_prompts(
    x_admin_secret: str = Header(default=""),
):
    """
    Reload prompt cache dari Neo4j.
    Dipanggil oleh editor Go (embed_knowledge_base.go) setelah 'Save & Embed'.
    """
    expected_secret = os.environ.get("ADMIN_SECRET", "")
    if expected_secret and x_admin_secret != expected_secret:
        logger.warning("[Admin] Reload rejected — invalid secret")
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Forbidden"},
        )

    try:
        registry = PromptRegistry.get()
        stats = registry.reload()
        logger.info("[Admin] Prompt cache reloaded → %s", stats)
        return {
            "success": True,
            "message": "Prompt cache reloaded.",
            "data": stats,
        }
    except Exception as e:
        logger.exception("[Admin] Failed to reload prompt cache: %s", e)
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": str(e)},
        )


@app.get("/admin/prompt-status", tags=["Admin"])
async def prompt_status(
    x_admin_secret: str = Header(default=""),
):
    """
    Debug endpoint: cek slot apa saja yang ter-load per lang.
    Berguna untuk verifikasi tanpa restart server.
    """
    expected_secret = os.environ.get("ADMIN_SECRET", "")
    if expected_secret and x_admin_secret != expected_secret:
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "Forbidden"},
        )

    registry = PromptRegistry.get()
    return {
        "success": True,
        "data": {
            "is_loaded": registry.is_loaded,
            "stats": registry.stats,
            "langs": {
                lang: registry.list_slots(lang)
                for lang in ["id", "en"]
            },
        },
    }


# =============================================================================
# BLOCK 9 — ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    os.environ.setdefault("RAGNA_PROGRESS", "0")

    _use_reload = dashboard is None

    if not _use_reload:
        logger.info("[Entry] Dashboard active — reload disabled (reload=False).")
    else:
        logger.info("[Entry] Dashboard inactive — reload=True for dev.")

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=_use_reload,
        log_config=None,
    )