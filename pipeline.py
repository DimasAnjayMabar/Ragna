# pipeline.py
"""
RAG Pipeline — orchestrates two separate pipelines with a defense layer
for prompt injection.

Log channels:
  - "init"     → during model loading (embedding, reranker, NLP, Groq client)
  - "pipeline" → during query processing (retrieval, rerank, generation)
  - "server"   → fallback for logs not wrapped in a context tag

Logging setup is NOT done here — it's done in main.py (server) or
embedder.py (standalone).
"""

import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Generator, List, Optional, Tuple

import base64
import io
from PIL import Image
import google.generativeai as genai

import torch
from groq import Groq
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer as AutoTok,
    TextIteratorStreamer,
    GenerationConfig,
    BitsAndBytesConfig,
)
from sentence_transformers import SentenceTransformer, CrossEncoder
import chromadb
from chromadb.config import Settings
from neo4j import GraphDatabase
from dotenv import load_dotenv
from transformers import (
    AutoTokenizer,
    pipeline as hf_pipeline,
)

from config import (
    CONFIG,
    PROMPTS,
    set_llm_mode,
    list_local_models,
    GROQ_MODEL_SAFE_TOKEN_BUDGET,
    FIXED_OVERHEAD_TOKENS,
)

load_dotenv()

# =============================================================================
# CONTEXT TAG FOR DASHBOARD
# =============================================================================
# If dashboard.py is not available, fall back to a no-op stub.
try:
    from dashboard import log_context
except ImportError:
    @contextmanager
    def log_context(tag: str):  # type: ignore
        """No-op fallback when dashboard.py is not available."""
        yield

# =============================================================================
# IMPORT PROGRESS REPORTER (for Go launcher)
# =============================================================================
# If progress.py is not available, fall back to no-op stubs.
try:
    from progress import (
        step_start,
        step_done,
        step_error,
        phase_start,
        phase_done,
    )
except ImportError:
    def step_start(*a, **k): pass
    def step_done(*a, **k): pass
    def step_error(*a, **k): pass
    def phase_start(*a, **k): pass
    def phase_done(*a, **k): pass


# =============================================================================
# LOGGER — no handlers, inherits from root logger
# =============================================================================
log = logging.getLogger("ragna")


# =============================================================================
# PROMPT INJECTION SCANNER
# =============================================================================

# P0-B: cache stores (data, timestamp) tuples for TTL.
#       Only populated on successful Neo4j query.
_INSTRUCTION_CACHE:      Dict[str, Tuple[List[Dict], float]] = {}
_INSTRUCTION_CACHE_LOCK  = threading.Lock()
_INSTRUCTION_CACHE_TTL   = 300  # seconds — 5 minutes; 0 to disable TTL
_COMPILED_PATTERN_CACHE: Dict[str, re.Pattern] = {}
_RISK_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}


def _get_compiled_pattern(pattern: str) -> re.Pattern:
    """Pre-compile regex — cached to avoid recompiling on every query."""
    if pattern not in _COMPILED_PATTERN_CACHE:
        try:
            _COMPILED_PATTERN_CACHE[pattern] = re.compile(pattern, re.IGNORECASE)
        except re.error:
            _COMPILED_PATTERN_CACHE[pattern] = re.compile(r"(?!x)x")
    return _COMPILED_PATTERN_CACHE[pattern]


def invalidate_instruction_cache(lang: Optional[str] = None):
    """
    Force refresh the instruction set cache.
    Call this after prompt_guard.py finishes embedding new instructions.
    """
    with _INSTRUCTION_CACHE_LOCK:
        if lang is None:
            _INSTRUCTION_CACHE.clear()
        else:
            _INSTRUCTION_CACHE.pop(lang, None)
    log.info("[InputGuard] Instruction cache invalidated (lang=%s)", lang or "all")


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class CandidateChunk:
    """Result from ChromaDB wide retrieval (Stage 1)."""
    isi_id:       str
    jurnal_id:    str
    konten_chunk: str
    vector_score: float


@dataclass
class EnrichedChunk:
    """Result after Neo4j enrichment (Stage 2)."""
    isi_id:        str
    jurnal_id:     str
    sub_judul:     str
    halaman:       int
    konten_chunk:  str
    context_text:  str
    judul_jurnal:  str
    doi:           str
    penulis:       str
    tanggal_rilis: str
    vector_score:  float
    rerank_score:  float = 0.0


@dataclass
class RAGResponse:
    """Final pipeline response."""
    answer:          object
    sources:         List[Dict]
    final_chunks:    List[EnrichedChunk]
    processing_time: float
    retrieval_time:  float = 0.0
    enrichment_time: float = 0.0
    rerank_time:     float = 0.0
    intent:          str = "knowledge"  # 'knowledge' | 'social' | 'blocked' | 'vision'


# =============================================================================
# MODELS LOADER (Singleton) — EAGER LOADING
# =============================================================================

class RAGModels:
    """
    Singleton — all local models loaded once at startup.

    Embedding + Reranker → GPU
    LLM                  → Groq API

    GROQ_API_KEY is read from environment variable at initialization.
    """

    _instance = None

    @classmethod
    def reset(cls):
        """Force re-initialization of the singleton — called on module reload."""
        cls._instance = None
        try:
            invalidate_instruction_cache()
        except Exception:
            pass

    def __new__(cls, embedding_only: bool = False):
        # If instance already exists but embedding_only is requested,
        # return the same instance anyway (more efficient).
        if cls._instance is None:
            instance = super().__new__(cls)
            instance._initialize(embedding_only=embedding_only)
            cls._instance = instance
        return cls._instance

    def _initialize(self, embedding_only: bool = False):
        # ── All model loading logs go to the INIT panel ─────────────────
        with log_context("init"):
            log.info(
                "Starting RAG model loading%s...",
                " (embedding-only)" if embedding_only else "",
            )

            self._embedding_only = embedding_only

            # ── Default state ────────────────────────────────────────────
            self.llm_mode        = "groq"
            self.local_llm       = None
            self.local_tokenizer = None
            self._nlp_loaded     = True
            self._reranker_lock  = threading.Lock()
            self._groq_lock      = threading.Lock()
            self._nlp_lock       = threading.Lock()

            # ── 1. Embedding model ───────────────────────────────────────
            step_start("init", "embedding", "Embedding model")
            log.info("[1/4] Loading %s on %s", CONFIG["embedding_model"], CONFIG["embedding_device"])
            _t = time.perf_counter()
            try:
                self.embedding_model = SentenceTransformer(
                    CONFIG["embedding_model"],
                    device=CONFIG["embedding_device"],
                )
                self.embedding_lock = threading.Lock()
                elapsed = time.perf_counter() - _t
                step_done("init", "embedding", elapsed)
                log.info("[1/4] Embedding ready  (%.2fs)", elapsed)
            except Exception as e:
                step_error("init", "embedding", str(e))
                raise

            if embedding_only:
                phase_done("init", time.perf_counter() - _t)
                log.info("✓ Embedding-only mode — reranker/Groq/NLP skipped.")
                return

            # ── 2. Reranker ──────────────────────────────────────────────
            step_start("init", "reranker", "Reranker model")
            log.info(
                "[2/4] Reranker: %s → %s",
                CONFIG["reranker_model"], CONFIG["reranker_device"],
            )
            _t = time.perf_counter()
            try:
                self._reranker = CrossEncoder(
                    CONFIG["reranker_model"],
                    device=CONFIG["reranker_device"],
                )
                elapsed = time.perf_counter() - _t
                step_done("init", "reranker", elapsed)
                log.info("[2/4] Reranker ready  (%.2fs)", elapsed)
            except Exception as e:
                step_error("init", "reranker", str(e))
                raise

            # ── 3. Groq client ───────────────────────────────────────────
            step_start("init", "groq", "Groq API client")
            log.info("[3/4] Groq API client → model=%s", CONFIG["groq_model"])
            _t = time.perf_counter()
            try:
                api_key = os.environ.get("GROQ_API_KEY")
                if not api_key:
                    raise EnvironmentError(
                        "GROQ_API_KEY not found in environment. "
                        "Set the environment variable before running the server."
                    )
                self._groq_client = Groq(api_key=api_key)
                elapsed = time.perf_counter() - _t
                step_done("init", "groq", elapsed)
                log.info("[3/4] Groq client ready  (%.2fs)", elapsed)
            except Exception as e:
                step_error("init", "groq", str(e))
                raise

            # ── 4. NLP — IndoBERT (ID) & BERT-NER (EN) ──────────────────
            step_start("init", "nlp", "NLP (IndoBERT + BERT-NER)")
            log.info("[4/4] Loading NLP: IndoBERT (ID) & BERT-NER (EN)...")
            _t = time.perf_counter()
            try:
                self.nlp_id_tokenizer = AutoTokenizer.from_pretrained(
                    CONFIG["nlp_id_model"]
                )
                self.nlp_id_fillmask = hf_pipeline(
                    "fill-mask",
                    model=CONFIG["nlp_id_model"],
                    tokenizer=CONFIG["nlp_id_model"],
                    device=CONFIG["nlp_device"],
                    top_k=5,
                )
                self.nlp_en_pipeline = hf_pipeline(
                    "ner",
                    model=CONFIG["nlp_en_model"],
                    tokenizer=CONFIG["nlp_en_model"],
                    aggregation_strategy="simple",
                    device=CONFIG["nlp_device"],
                )
                elapsed = time.perf_counter() - _t
                step_done("init", "nlp", elapsed)
                log.info("[4/4] NLP ready  (%.2fs)", elapsed)
            except Exception as e:
                step_error("init", "nlp", str(e))
                raise

            phase_done("init", time.perf_counter() - _t)
            log.info("✓ All models loaded successfully.")

    # ── Alias properties (kept for backward compatibility) ────────────────

    @property
    def reranker(self) -> CrossEncoder:
        """Return the reranker (loaded in _initialize)."""
        return self._reranker

    @property
    def groq_client(self) -> Groq:
        """Return the Groq client (loaded in _initialize)."""
        return self._groq_client

    # ── Embedding helpers ─────────────────────────────────────────────────

    def get_embedding(self, text: str) -> List[float]:
        """Embed a single text → float vector (GPU, no_grad, thread-safe)."""
        with self.embedding_lock:
            with torch.no_grad():
                return self.embedding_model.encode(
                    text, convert_to_tensor=False
                ).tolist()

    def embed_batch_safe(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts → list of float vectors (GPU, no_grad, thread-safe)."""
        with self.embedding_lock:
            with torch.no_grad():
                embeddings = self.embedding_model.encode(
                    texts,
                    convert_to_tensor=False,
                    show_progress_bar=False,
                )
                return [e.tolist() for e in embeddings]

    def rerank(self, query: str, texts: List[str]) -> List[float]:
        """Cross-encoder scoring (query, text) on GPU."""
        if not texts:
            return []
        pairs = [[query, t] for t in texts]
        with torch.no_grad():
            scores = self._reranker.predict(pairs)
        return scores.tolist() if hasattr(scores, "tolist") else list(scores)

    # ── NLP constants ─────────────────────────────────────────────────────

    _ID_STOPWORDS = {
        "yang", "dan", "di", "ke", "dari", "ini", "itu",
        "dengan", "untuk", "pada", "adalah", "ada", "atau",
        "juga", "oleh", "sebagai", "dalam", "tidak", "akan",
        "dapat", "bisa", "sudah", "telah", "lebih", "serta",
        "apakah", "apa", "bagaimana", "mengapa", "kenapa",
        "jelaskan", "sebutkan", "coba", "tolong", "mohon",
    }

    _DOMAIN_VOCAB = {
        "fusarium", "antraknosa", "nematoda", "aflatoksin", "alternaria",
        "pythium", "phytophthora", "rhizoctonia", "sclerotinia", "botrytis",
        "xanthomonas", "pseudomonas", "erwinia", "agrobacterium", "ralstonia",
        "tungro", "blas", "kresek", "hawar", "busuk", "layu", "bercak",
        "embun", "tepung", "karat", "virus", "bakteri", "jamur", "cendawan",
        "aphid", "thrips", "whitefly", "mealybug", "wereng", "penggerek",
        "ulat", "kutu", "tungau", "nematoda", "belalang", "lalat",
        "kentang", "tomat", "cabai", "jagung", "padi", "kedelai", "singkong",
        "ubi", "terong", "bawang", "wortel", "kubis", "selada", "kangkung",
    }

    def correct_typo_mlm(self, text: str) -> str:
        """Correct typos in Indonesian queries using IndoBERT MLM."""
        words = text.split()
        corrected_words: list = []
        any_corrected = False

        for word in words:
            word_lower = word.lower()

            if word_lower in self._DOMAIN_VOCAB:
                corrected_words.append(word)
                continue

            tokens = self.nlp_id_tokenizer.tokenize(word_lower)
            is_unk = "[UNK]" in tokens
            is_heavily_split = len(tokens) >= 4 and all(
                t.startswith("##") or len(t) <= 2 for t in tokens[1:]
            )

            if not (is_unk or is_heavily_split):
                corrected_words.append(word)
                continue

            masked_sentence = " ".join(
                "[MASK]" if w.lower() == word_lower else w
                for w in words
            )

            try:
                predictions = self.nlp_id_fillmask(masked_sentence)
                best = None
                for pred in predictions:
                    candidate = pred["token_str"].strip().lower()
                    score     = pred["score"]
                    if (score >= 0.15
                            and len(candidate) > 2
                            and candidate != word_lower):
                        best = candidate
                        break

                if best:
                    log.debug(
                        "[MLM-Typo] '%s' → '%s' (score=%.3f)",
                        word, best, predictions[0]["score"],
                    )
                    corrected_words.append(best)
                    any_corrected = True
                else:
                    corrected_words.append(word)

            except Exception:
                log.warning("[MLM-Typo] fill-mask failed for word '%s'", word, exc_info=False)
                corrected_words.append(word)

        corrected_text = " ".join(corrected_words)
        if any_corrected:
            log.info("[MLM-Typo] Query corrected: %r → %r", text, corrected_text)

        return corrected_text

    def extract_keywords_nlp(self, text: str, lang: str) -> str:
        """Extract keywords/entities from the query using NLP."""
        try:
            if lang == "id":
                tokens = self.nlp_id_tokenizer.tokenize(text)
                clean_tokens = [
                    t.replace("##", "").lower()
                    for t in tokens
                    if not t.startswith("[") and len(t.replace("##", "")) > 2
                ]
                keywords = [
                    t for t in dict.fromkeys(clean_tokens)
                    if t not in self._ID_STOPWORDS
                ]
                extra = " ".join(keywords[:10])
                log.debug("[NLP-ID] keywords: %s", extra)

            else:  # lang == 'en'
                ner_results = self.nlp_en_pipeline(text)
                keywords = list(dict.fromkeys(
                    entity["word"]
                    for entity in ner_results
                    if entity.get("score", 0) >= 0.7
                ))
                extra = " ".join(keywords[:10])
                log.debug("[NLP-EN] entities: %s", extra)

        except Exception:
            log.warning("[NLP] Keyword extraction failed, continuing without enrichment", exc_info=True)
            extra = ""

        return extra


# =============================================================================
# STAGE 1 — CHROMADB RETRIEVER
# =============================================================================

class ChromaRetriever:
    """Wide retrieval from ChromaDB collection 'konten_isi'."""

    def __init__(self, persist_directory: str = CONFIG["chroma_path"]):
        log.info("ChromaDB: %s", persist_directory)
        self.client = chromadb.PersistentClient(
            path=persist_directory,
            settings=Settings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_collection(CONFIG["chroma_collection"])
        log.info(
            "ChromaDB ready — '%s'  (%d documents)",
            CONFIG["chroma_collection"],
            self.collection.count(),
        )

    def retrieve(
        self,
        query_embedding: List[float],
        k: int = CONFIG["chroma_retrieval_k"],
    ) -> List[CandidateChunk]:
        """Retrieve the k most similar chunks to query_embedding."""
        try:
            results = self.collection.query(
                query_embeddings=[query_embedding],
                n_results=k,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            log.exception("ChromaDB query failed (k=%d)", k)
            return []

        candidates: List[CandidateChunk] = []
        if not results["ids"] or not results["ids"][0]:
            return candidates

        for i, doc_id in enumerate(results["ids"][0]):
            meta = (results["metadatas"][0][i]
                    if results["metadatas"] and results["metadatas"][0] else {})
            dist = (float(results["distances"][0][i])
                    if results["distances"] and results["distances"][0] else 1.0)

            candidates.append(CandidateChunk(
                isi_id=meta.get("isi_id", doc_id),
                jurnal_id=meta.get("jurnal_id", ""),
                konten_chunk=results["documents"][0][i],
                vector_score=dist,
            ))

        log.debug("ChromaDB: %d candidates found (k=%d)", len(candidates), k)
        return candidates


# =============================================================================
# STAGE 2 — NEO4J ENRICHER — P0-A: FILTER QUARANTINED IN CYPHER
# =============================================================================

class Neo4jEnricher:
    """Neo4j context enrichment (prev/next chunk window)."""

    def __init__(
        self,
        uri:      str = CONFIG["neo4j_uri"],
        user:     str = CONFIG["neo4j_user"],
        password: str = CONFIG["neo4j_password"],
        max_wait_seconds: int = 300,
        retry_interval:   int = 5,
    ):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

        waited = 0
        while True:
            try:
                self.driver.verify_connectivity()
                log.info("Neo4j: %s (connected)", uri)
                break
            except Exception as e:
                # P1: use log.warning (not print) so the dashboard TUI isn't corrupted.
                if waited == 0:
                    log.warning("⚠️  Neo4j is not active at %s", uri)
                    log.warning("   Please activate Neo4j now. Waiting for connection...")
                log.warning(
                    "   Waiting for Neo4j connection... (%ds / %ds)",
                    waited, max_wait_seconds,
                )

                if waited >= max_wait_seconds:
                    raise ConnectionError(
                        f"Neo4j could not be reached after {max_wait_seconds}s at {uri}. "
                        f"Make sure the server is running and try again."
                    ) from e

                time.sleep(retry_interval)
                waited += retry_interval

    def close(self):
        self.driver.close()

    def enrich(
        self,
        candidates: List[CandidateChunk],
        context_window: int = CONFIG["context_window"],
    ) -> List[EnrichedChunk]:
        """
        Run a single Cypher UNWIND for all isi_ids at once.

        P0-A: quarantined chunks MUST NOT enter context_text.
              Target, prev, and next are all filtered.
        """
        if not candidates:
            return []

        isi_ids  = [c.isi_id for c in candidates]
        cand_map = {c.isi_id: c for c in candidates}

        cypher = (
            "UNWIND $isi_ids AS target_id "
            "MATCH (isi:Isi {id: target_id}) "
            "WHERE coalesce(isi.quarantined, false) = false "
            "MATCH (j:Jurnal)-[:HAS_SECTION]->(isi) "
            "OPTIONAL MATCH (prev_isi:Isi)-[:NEXT*1..%(cw)d]->(isi) "
            "  WHERE coalesce(prev_isi.quarantined, false) = false "
            "WITH isi, j, target_id, "
            "     collect(DISTINCT prev_isi.konten_chunk) AS prev_chunks "
            "OPTIONAL MATCH (isi)-[:NEXT*1..%(cw)d]->(next_isi:Isi) "
            "  WHERE coalesce(next_isi.quarantined, false) = false "
            "WITH isi, j, target_id, prev_chunks, "
            "     collect(DISTINCT next_isi.konten_chunk) AS next_chunks "
            "RETURN "
            "  target_id        AS isi_id, "
            "  j.id             AS jurnal_id, "
            "  isi.sub_judul    AS sub_judul, "
            "  isi.halaman      AS halaman, "
            "  isi.konten_chunk AS konten_chunk, "
            "  j.judul          AS judul_jurnal, "
            "  j.doi            AS doi, "
            "  j.penulis        AS penulis, "
            "  j.tanggal_rilis  AS tanggal_rilis, "
            "  prev_chunks      AS prev_chunks, "
            "  next_chunks      AS next_chunks"
        ) % {"cw": context_window}

        enriched: List[EnrichedChunk] = []
        quarantined_skipped = 0

        try:
            with self.driver.session() as session:
                for rec in session.run(cypher, isi_ids=isi_ids):
                    isi_id = rec["isi_id"]
                    cand   = cand_map.get(isi_id)
                    if cand is None:
                        continue

                    prev_list = [t for t in (rec["prev_chunks"] or []) if t]
                    next_list = [t for t in (rec["next_chunks"] or []) if t]
                    target    = rec["konten_chunk"] or ""

                    context_text = " ".join([*prev_list, target, *next_list]).strip()

                    enriched.append(EnrichedChunk(
                        isi_id=isi_id,
                        jurnal_id=rec["jurnal_id"] or cand.jurnal_id,
                        sub_judul=rec["sub_judul"] or "Unknown",
                        halaman=int(rec["halaman"] or 0),
                        konten_chunk=target,
                        context_text=context_text,
                        judul_jurnal=rec["judul_jurnal"] or "Unknown",
                        doi=rec["doi"] or "",
                        penulis=rec["penulis"] or "Unknown",
                        tanggal_rilis=rec["tanggal_rilis"] or "Unknown",
                        vector_score=cand.vector_score,
                    ))

                quarantined_skipped = len(candidates) - len(enriched)

        except Exception:
            log.exception("Neo4j enrichment failed (%d isi_ids)", len(isi_ids))

        if quarantined_skipped > 0:
            log.warning(
                "[Neo4j] %d candidates skipped due to quarantined target chunk",
                quarantined_skipped,
            )

        log.debug(
            "Neo4j enrich: %d/%d enriched (quarantined filter active)",
            len(enriched), len(candidates),
        )
        return enriched


# =============================================================================
# MAIN PIPELINE — ROUTER + 2 SEPARATE PATHS
# =============================================================================

class RAGPipeline:
    """
    Orchestrates two separate pipelines with a defense layer for prompt injection.
    """

    def __init__(self):
        # ── All pipeline init logs go to the INIT panel ─────────────────
        with log_context("init"):
            log.info("Initializing RAGPipeline...")
            self.models = RAGModels()
            self.chroma = ChromaRetriever()
            self.neo4j  = Neo4jEnricher()
            log.info("✓ RAGPipeline ready.")

    def close(self):
        self.neo4j.close()

    # ── Public API — Router ────────────────────────────────────────────────

    def process_query(
        self,
        query:        str,
        chat_id:      int | None = None,
        stop_event:   threading.Event = None,
        user_id:      int | None = None,
        base64_image: str | None = None,
    ) -> RAGResponse:
        """
        Main entry point. Detects intent and delegates to the appropriate pipeline.
        """
        with log_context("pipeline"):
            if base64_image:
                log.info("═" * 60)
                log.info("[Vision RAG] Step 1: Analyzing image using Gemini...")

                vision_ok, image_description = self._analyze_image(base64_image)
                log.info("[Vision RAG] Extraction result: %s...", image_description[:100])

                if not vision_ok:
                    def error_stream():
                        yield (
                            f"⚠️ **Sistem Vision Error:**\n\n{image_description}\n\n"
                            f"_Tips: Ini biasanya terjadi karena batas limit API gratis per menit. "
                            f"Silakan tunggu sekitar 1 menit lalu coba kirim ulang gambar Anda._"
                        )

                    return RAGResponse(
                        answer=error_stream(),
                        sources=[],
                        final_chunks=[],
                        processing_time=0.0,
                        intent="vision_error",
                    )

                # Scan query text (not image) before merging
                if query and query.strip():
                    _vlang = self._detect_language(query)
                    _vscan = self._scan_query(query, lang=_vlang)
                    if _vscan["is_suspicious"]:
                        log.warning(
                            "[InputGuard] Vision query rejected — slot=%s patterns=%s risk=%.2f",
                            _vscan["matched_slot"],
                            _vscan["matched_patterns"][:5],
                            _vscan["risk_score"],
                        )
                        return RAGResponse(
                            answer=self._refuse_injection_stream("umum"),
                            sources=[],
                            final_chunks=[],
                            processing_time=0.0,
                            intent="blocked",
                        )

                # Merge vision output with the original query
                if query.strip() and query.strip() != "Tolong jelaskan gambar tanaman ini.":
                    enriched_query = (
                        f"Pengguna mengunggah gambar dengan hasil analisis visi dari pakar berikut:\n"
                        f"'{image_description}'\n\n"
                        f"Berdasarkan analisis visual tersebut, pengguna bertanya: '{query}'. "
                        f"Tolong berikan jawaban yang komprehensif."
                    )
                else:
                    enriched_query = (
                        f"Pengguna mengunggah gambar dengan hasil analisis visi dari pakar berikut:\n"
                        f"'{image_description}'\n\n"
                        f"Tolong jelaskan kondisi tanaman tersebut, kemungkinan penyebab, "
                        f"dan cara penanganannya."
                    )

                log.info("[Vision RAG] Step 2: Sending merged text to Knowledge Retrieval pipeline...")
                return self.process_knowledge_query(
                    enriched_query, chat_id=chat_id, stop_event=stop_event, user_id=user_id
                )

            # ── Typo correction via IndoBERT MLM (Indonesian only) ───────
            lang_pre = self._detect_language(query)
            if lang_pre == "id":
                query = self.models.correct_typo_mlm(query)

            # ── Scan query against the instruction set graph ─────────────
            scan_result = self._scan_query(query, lang=lang_pre)
            if scan_result["is_suspicious"]:
                log.warning(
                    "[InputGuard] Query rejected — slot=%s patterns=%s risk=%.2f",
                    scan_result["matched_slot"],
                    scan_result["matched_patterns"][:5],
                    scan_result["risk_score"],
                )
                return RAGResponse(
                    answer=self._refuse_injection_stream("umum"),
                    sources=[],
                    final_chunks=[],
                    processing_time=0.0,
                    intent="blocked",
                )

            log.info("[InputGuard] Query is clean — proceeding to pipeline (lang=%s)", lang_pre)

            intent = self._detect_query_intent(query)
            log.info("Detected intent: %s — query=%r", intent, query[:80])

            rag_mode = CONFIG.get("rag_mode", "improved")

            if intent == "social":
                return self.process_social_query(
                    query, chat_id=chat_id, stop_event=stop_event, user_id=user_id
                )

            if rag_mode == "regular":
                log.info("[Router] Using Regular RAG pipeline")
                return self.process_regular_query(
                    query, chat_id=chat_id, stop_event=stop_event, user_id=user_id
                )

            return self.process_knowledge_query(
                query, chat_id=chat_id, stop_event=stop_event, user_id=user_id
            )

    # ── Scanner helpers ────────────────────────────────────────────────────

    def _get_cached_instructions(self, lang: str) -> List[Dict]:
        """
        Fetch instruction set from Neo4j for a given language.

        P0-B: results are ONLY cached on successful Neo4j queries.
              On exception, [] is returned but NOT cached, so the next
              attempt will retry the Neo4j query (prevents permanently
              disabled guard until restart).

        P3:   TTL cache — old instructions auto-refresh after
              _INSTRUCTION_CACHE_TTL seconds. Set 0 to disable TTL.
        """
        now = time.time()

        # ── Cache lookup (with TTL) ──────────────────────────────────────
        with _INSTRUCTION_CACHE_LOCK:
            entry = _INSTRUCTION_CACHE.get(lang)
            if entry is not None:
                cached_data, cached_at = entry
                if _INSTRUCTION_CACHE_TTL <= 0 or (now - cached_at) < _INSTRUCTION_CACHE_TTL:
                    return cached_data
                _INSTRUCTION_CACHE.pop(lang, None)

        # ── Query Neo4j ───────────────────────────────────────────────────
        try:
            with self.neo4j.driver.session() as session:
                result = session.run(
                    """
                    MATCH (i:Instruction {lang: $lang})
                    RETURN i.slot               AS slot,
                           i.forbidden_patterns AS patterns,
                           i.risk_level         AS risk_level
                    """,
                    lang=lang,
                ).data()
            instructions = [
                {
                    "slot":       r["slot"],
                    "patterns":   r["patterns"] or [],
                    "risk_level": r["risk_level"] or "medium",
                }
                for r in result
            ]
            log.info(
                "[InputGuard] Instruction set loaded for lang=%s (%d slots, %d total patterns)",
                lang,
                len(instructions),
                sum(len(i["patterns"]) for i in instructions),
            )

            # P0-B: ONLY cache on success
            with _INSTRUCTION_CACHE_LOCK:
                _INSTRUCTION_CACHE[lang] = (instructions, now)

            return instructions

        except Exception:
            log.warning(
                "[InputGuard] Failed to fetch instruction lang=%s — scan skipped. "
                "Cache NOT updated so the next attempt can retry.",
                lang, exc_info=False,
            )
            # P0-B: do NOT cache []
            return []

    def _scan_query(self, query: str, lang: str = "id") -> Dict:
        """Scan user query against the instruction set graph."""
        instructions = self._get_cached_instructions(lang)

        matched_patterns: List[str] = []
        matched_slot: Optional[str] = None
        max_risk = 0.0

        for instr in instructions:
            slot_weight = _RISK_WEIGHT.get(instr["risk_level"], 0.5)
            for pattern in instr["patterns"]:
                try:
                    rx = _get_compiled_pattern(pattern)
                    if rx.search(query):
                        matched_patterns.append(pattern)
                        if slot_weight > max_risk:
                            max_risk = slot_weight
                            matched_slot = instr["slot"]
                except Exception:
                    continue

        total_patterns = sum(len(i["patterns"]) for i in instructions)
        log.debug(
            "[InputGuard] Scan done — lang=%s slots=%d patterns=%d matched=%d",
            lang, len(instructions), total_patterns, len(matched_patterns),
        )

        return {
            "is_suspicious":    len(matched_patterns) > 0,
            "matched_patterns": matched_patterns,
            "risk_score":       max_risk,
            "matched_slot":     matched_slot,
        }

    def _refuse_injection_stream(self, reason: str = "umum"):
        """Generator that refuses a query due to detected prompt injection."""
        if reason == "social":
            msg = (
                "Maaf, saya tidak dapat memproses permintaan ini. "
                "Silakan ajukan pertanyaan seputar penyakit dan hama tanaman."
            )
        else:
            msg = (
                "Maaf, saya tidak dapat memproses permintaan ini karena "
                "terdeteksi pola yang tidak sesuai dengan pedoman saya. "
                "Silakan ajukan pertanyaan seputar penyakit dan hama tanaman."
            )

        def _gen():
            yield msg
        return _gen()

    # ── Memory System ──────────────────────────────────────────────────────

    def get_memory(self, chat_id: int, query: str, user_id: int | None = None) -> str | None:
        """Fetch hybrid memory from ChromaDB collection 'chat_memory'."""
        try:
            collection = self.chroma.client.get_or_create_collection(
                CONFIG["memory_collection"]
            )

            # ── Block 0: User identity ──────────────────────────────────
            identity: str = ""
            if user_id is not None:
                identity = self.get_identity(user_id) or ""

            # ── Block 1: Running summary ────────────────────────────────
            summary: str = ""
            try:
                result = collection.get(
                    ids=[f"summary_{chat_id}"],
                    include=["documents"],
                )
                if result["ids"]:
                    summary = result["documents"][0]
                    log.info(
                        "[Memory] Summary found for chat_id=%d  (%d chars)",
                        chat_id, len(summary),
                    )
            except Exception:
                log.debug("[Memory] No summary yet for chat_id=%d", chat_id)

            # ── Block 2: Recent window ──────────────────────────────────
            recent_text: str = ""
            try:
                all_results = collection.get(include=["documents", "metadatas"])

                prefix       = f"recent_{chat_id}_"
                matched_ids  = []
                matched_docs = []
                matched_meta = []

                for i, doc_id in enumerate(all_results["ids"]):
                    if doc_id.startswith(prefix):
                        matched_ids.append(doc_id)
                        matched_docs.append(all_results["documents"][i])
                        matched_meta.append(all_results["metadatas"][i])

                if matched_ids:
                    entries = sorted(
                        zip(matched_docs, matched_meta),
                        key=lambda x: x[1].get("timestamp", 0),
                    )

                    n = CONFIG["memory_recent_window"]
                    entries = entries[-n:]

                    lines = [doc for doc, _ in entries]
                    recent_text = "\n\n".join(lines)
                    log.info(
                        "[Memory] Recent window: %d entries (of %d total) chat_id=%d",
                        len(entries), len(matched_ids), chat_id,
                    )
                else:
                    log.debug(
                        "[Memory] No recent entries yet for chat_id=%d", chat_id
                    )

            except Exception:
                log.debug(
                    "[Memory] Failed to fetch recent entries for chat_id=%d", chat_id,
                    exc_info=True,
                )

            # ── Merge three blocks ──────────────────────────────────────
            if not identity and not summary and not recent_text:
                log.debug("[Memory] No memory for chat_id=%d", chat_id)
                return None

            parts = []
            if identity:
                parts.append(f"### IDENTITAS PENGGUNA ###\n{identity}")
            if summary:
                parts.append(f"### RINGKASAN SESI ###\n{summary}")
            if recent_text:
                parts.append(f"### PERCAKAPAN TERAKHIR ###\n{recent_text}")

            combined = "\n\n".join(parts)
            log.info(
                "[Memory] Hybrid memory ready — chat_id=%d  (%d chars)",
                chat_id, len(combined),
            )
            return combined

        except Exception:
            log.warning(
                "[Memory] Failed to fetch memory for chat_id=%d", chat_id,
                exc_info=False,
            )
            return None

    def get_identity(self, user_id: int) -> str | None:
        """Fetch user identity from ChromaDB collection 'user_identity'."""
        try:
            collection = self.chroma.client.get_or_create_collection(
                CONFIG["identity_collection"]
            )
            result = collection.get(
                ids=[f"identity_{user_id}"],
                include=["documents"],
            )
            if result["ids"]:
                identity_text = result["documents"][0]
                log.info(
                    "[Identity] Found for user_id=%d  (%d chars)",
                    user_id, len(identity_text),
                )
                return identity_text
            log.debug("[Identity] No identity yet for user_id=%d", user_id)
            return None
        except Exception:
            log.warning(
                "[Identity] Failed to fetch identity for user_id=%d", user_id,
                exc_info=False,
            )
            return None

    def save_identity(self, user_id: int, user_name: str) -> None:
        """Save or update user identity in ChromaDB collection 'user_identity'."""
        if not user_name or not user_name.strip():
            log.debug("[Identity] Empty user_name — skipping save for user_id=%d", user_id)
            return
        try:
            collection = self.chroma.client.get_or_create_collection(
                CONFIG["identity_collection"]
            )
            identity_text = f"Nama pengguna: {user_name.strip()}"
            identity_embedding = self.models.get_embedding(identity_text)
            collection.upsert(
                ids=[f"identity_{user_id}"],
                documents=[identity_text],
                embeddings=[identity_embedding],
                metadatas=[{
                    "user_id":    user_id,
                    "user_name":  user_name.strip(),
                    "updated_at": int(time.time()),
                }],
            )
            log.info(
                "[Identity] Saved → user_id=%d  user_name=%r",
                user_id, user_name,
            )
        except Exception:
            log.exception(
                "[Identity] Failed to save identity for user_id=%d", user_id
            )

    def save_memory(self, chat_id: int, detail_id: int, question: str, answer: str) -> None:
        """Save hybrid memory to ChromaDB collection 'chat_memory'."""
        if not answer or not answer.strip():
            log.warning(
                "[Memory] Empty answer — skipping save chat_id=%d detail_id=%d",
                chat_id, detail_id,
            )
            return

        try:
            collection = self.chroma.client.get_or_create_collection(
                CONFIG["memory_collection"]
            )

            # ══════════════════════════════════════════════════════════════
            # PART 1 — Update running summary
            # ══════════════════════════════════════════════════════════════
            previous_summary: str = ""
            try:
                existing = collection.get(
                    ids=[f"summary_{chat_id}"],
                    include=["documents"],
                )
                if existing["ids"]:
                    previous_summary = existing["documents"][0]
            except Exception:
                pass

            max_words = CONFIG["memory_summary_max_words"]

            _max_answer_chars   = CONFIG["memory_summary_max_tokens"] * 3
            _max_summary_chars  = CONFIG["memory_summary_max_tokens"] * 2
            _max_question_chars = 400

            answer_trunc   = answer.strip()[:_max_answer_chars]
            question_trunc = question.strip()[:_max_question_chars]
            prev_trunc     = previous_summary[:_max_summary_chars] if previous_summary else ""

            if prev_trunc:
                summary_prompt = PROMPTS["memory_summary_update"].format(
                    max_words=max_words,
                    previous_summary=prev_trunc,
                    question=question_trunc,
                    answer=answer_trunc,
                )
            else:
                summary_prompt = PROMPTS["memory_summary_new"].format(
                    max_words=max_words,
                    question=question_trunc,
                    answer=answer_trunc,
                )

            log.info(
                "[Memory] Summarizing new summary — chat_id=%d  detail_id=%d  prev_summary=%d chars",
                chat_id, detail_id, len(previous_summary),
            )

            if self.models.llm_mode == "groq":
                summary_response = self.models.groq_client.chat.completions.create(
                    model=CONFIG["memory_summary_model"],
                    messages=[{"role": "user", "content": summary_prompt}],
                    max_tokens=CONFIG["memory_summary_max_tokens"],
                    temperature=0.3,
                )
                new_summary = summary_response.choices[0].message.content.strip()
            else:
                tokenizer = self.models.local_tokenizer
                model     = self.models.local_llm

                inputs = tokenizer(
                    summary_prompt,
                    return_tensors="pt",
                    truncation=True,
                    max_length=2048,
                ).input_ids.to(model.device)

                with torch.no_grad():
                    output_ids = model.generate(
                        inputs,
                        max_new_tokens=CONFIG["memory_summary_max_tokens"],
                        temperature=0.3,
                        do_sample=True,
                        pad_token_id=tokenizer.pad_token_id,
                        eos_token_id=tokenizer.eos_token_id,
                    )

                generated = output_ids[0][inputs.shape[-1]:]
                new_summary = tokenizer.decode(generated, skip_special_tokens=True).strip()

            summary_embedding = self.models.get_embedding(new_summary)
            collection.upsert(
                ids=[f"summary_{chat_id}"],
                documents=[new_summary],
                embeddings=[summary_embedding],
                metadatas=[{
                    "type":           "summary",
                    "chat_id":        chat_id,
                    "last_detail_id": detail_id,
                }],
            )
            log.info(
                "[Memory] Summary updated → chat_id=%d  detail_id=%d  (%d chars)",
                chat_id, detail_id, len(new_summary),
            )

            # ══════════════════════════════════════════════════════════════
            # PART 2 — Save episodic entry (recent window)
            # ══════════════════════════════════════════════════════════════
            recent_doc = (
                f"User: {question.strip()}\n"
                f"ragna: {answer.strip()}"
            )
            recent_embedding = self.models.get_embedding(recent_doc)
            collection.upsert(
                ids=[f"recent_{chat_id}_{detail_id}"],
                documents=[recent_doc],
                embeddings=[recent_embedding],
                metadatas=[{
                    "type":      "recent",
                    "chat_id":   chat_id,
                    "detail_id": detail_id,
                    "timestamp": int(time.time()),
                }],
            )
            log.info(
                "[Memory] Recent entry saved → chat_id=%d  detail_id=%d",
                chat_id, detail_id,
            )

        except Exception:
            log.exception(
                "[Memory] Failed to update memory chat_id=%d detail_id=%d",
                chat_id, detail_id,
            )

    # ── Social Pipeline ────────────────────────────────────────────────────

    def process_social_query(
        self,
        query:      str,
        chat_id:    int | None = None,
        stop_event: threading.Event = None,
        user_id:    int | None = None,
    ) -> RAGResponse:
        with log_context("pipeline"):
            t_start = time.perf_counter()
            lang    = self._detect_language(query)
            tier    = self._get_model_tier()

            # Defense-in-depth scan
            scan_result = self._scan_query(query, lang=lang)
            if scan_result["is_suspicious"]:
                log.warning(
                    "[InputGuard] Social query rejected — slot=%s patterns=%s",
                    scan_result["matched_slot"],
                    scan_result["matched_patterns"][:5],
                )
                return RAGResponse(
                    answer=self._refuse_injection_stream("social"),
                    sources=[], final_chunks=[],
                    processing_time=0.0, intent="blocked",
                )

            log.info("[InputGuard] Social query is clean (lang=%s)", lang)

            # ── Fetch memory + user identity if available ───────────────
            memory_text: str | None = None
            if chat_id is not None:
                memory_text = self.get_memory(chat_id, query, user_id=user_id)
                if memory_text:
                    log.info("[Social] Memory found (%d chars)", len(memory_text))
                else:
                    log.debug("[Social] No memory yet for chat_id=%d", chat_id)

            use_compact = tier in ("small", "medium")

            if lang == "id":
                memory_section = (
                    PROMPTS["social_memory_block_id"].format(memory=memory_text)
                    if memory_text else ""
                )
                prompt_key = "social_system_id_local" if use_compact else "social_system_id"
                system_msg = PROMPTS[prompt_key].format(memory_section=memory_section)
            else:
                memory_section = (
                    PROMPTS["social_memory_block_en"].format(memory=memory_text)
                    if memory_text else ""
                )
                prompt_key = "social_system_en_local" if use_compact else "social_system_en"
                system_msg = PROMPTS[prompt_key].format(memory_section=memory_section)

            messages = [
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": query},
            ]

            log.info(
                "[Social] tier=%s  model=%s  prompt=%s  lang=%s  memory=%s  user_id=%s  query=%r",
                tier,
                CONFIG.get("groq_model", "?"),
                prompt_key,
                lang,
                "yes" if memory_text else "no",
                user_id or "-",
                query[:60],
            )

            answer_gen = self._generate_stream(
                messages,
                stop_event=stop_event,
                temperature=CONFIG["social_temperature"],
                top_p=CONFIG["social_top_p"],
                max_new_tokens=CONFIG["social_max_new_tokens"],
            )

            elapsed = time.perf_counter() - t_start
            return RAGResponse(
                answer=answer_gen,
                sources=[],
                final_chunks=[],
                processing_time=elapsed,
                intent="social",
            )

    # ── Knowledge Pipeline ────────────────────────────────────────────────

    def process_knowledge_query(
        self,
        query:      str,
        chat_id:    int | None = None,
        stop_event: threading.Event = None,
        user_id:    int | None = None,
    ) -> RAGResponse:
        """Knowledge pipeline — WITH retrieval (6 stages)."""
        with log_context("pipeline"):
            t_start = time.perf_counter()
            log.info("═" * 60)
            log.info("[Knowledge] Query: %r  chat_id=%s", query[:120], chat_id)

            # ── Language detection & NLP keyword enrichment ─────────────
            lang = self._detect_language(query)
            log.info("[Knowledge] Detected language: %s", lang)

            # Defense-in-depth scan
            scan_result = self._scan_query(query, lang=lang)
            if scan_result["is_suspicious"]:
                log.warning(
                    "[InputGuard] Knowledge query rejected — slot=%s patterns=%s risk=%.2f",
                    scan_result["matched_slot"],
                    scan_result["matched_patterns"][:5],
                    scan_result["risk_score"],
                )
                return RAGResponse(
                    answer=self._refuse_injection_stream("umum"),
                    sources=[], final_chunks=[],
                    processing_time=0.0, intent="blocked",
                )

            log.info("[InputGuard] Knowledge query is clean (lang=%s)", lang)

            nlp_keywords   = self.models.extract_keywords_nlp(query, lang)
            enriched_query = f"{query} {nlp_keywords}".strip() if nlp_keywords else query
            log.debug("[Knowledge] Enriched query: %r", enriched_query[:200])

            # ══════════════════════════════════════════════════════════════
            # STAGE 1 — ChromaDB Wide Retrieval
            # ══════════════════════════════════════════════════════════════
            k = CONFIG["chroma_retrieval_k"]
            log.info("[Stage 1] ChromaDB retrieval (k=%d)...", k)
            t1_start = time.perf_counter()

            query_emb  = self.models.get_embedding(enriched_query)
            candidates = self.chroma.retrieve(query_emb, k=k)

            retrieval_elapsed = time.perf_counter() - t1_start
            log.info("[Stage 1] %d candidates found  (%.3fs)", len(candidates), retrieval_elapsed)

            if not candidates:
                log.warning("[Stage 1] No candidates — pipeline stopped.")
                return RAGResponse(
                    answer="Maaf, tidak menemukan informasi relevan di database.",
                    sources=[],
                    final_chunks=[],
                    processing_time=time.perf_counter() - t_start,
                    retrieval_time=retrieval_elapsed,
                    intent="knowledge",
                )

            # ══════════════════════════════════════════════════════════════
            # STAGE 2 — Neo4j Context Enrichment
            # ══════════════════════════════════════════════════════════════
            log.info(
                "[Stage 2] Neo4j enrichment (%d candidates, window=±%d)...",
                len(candidates), CONFIG["context_window"],
            )
            t2_start = time.perf_counter()

            enriched = self.neo4j.enrich(candidates, CONFIG["context_window"])

            enrichment_elapsed = time.perf_counter() - t2_start
            log.info("[Stage 2] %d chunks enriched  (%.3fs)", len(enriched), enrichment_elapsed)

            if not enriched:
                log.warning("[Stage 2] Enrichment empty — pipeline stopped.")
                return RAGResponse(
                    answer="Maaf, gagal mengambil konteks dari graph database.",
                    sources=[],
                    final_chunks=[],
                    processing_time=time.perf_counter() - t_start,
                    retrieval_time=retrieval_elapsed,
                    enrichment_time=enrichment_elapsed,
                    intent="knowledge",
                )

            # ══════════════════════════════════════════════════════════════
            # STAGE 3 — BGE Reranking
            # ══════════════════════════════════════════════════════════════
            reranked_k = CONFIG["reranked_k"]
            log.info(
                "[Stage 3] BGE reranking (%d → top %d) @ GPU...",
                len(enriched), reranked_k,
            )

            t3_start = time.perf_counter()

            scores = self.models.rerank(query, [c.context_text for c in enriched])

            for i, score in enumerate(scores):
                if i < len(enriched):
                    enriched[i].rerank_score = float(score)

            enriched.sort(key=lambda x: x.rerank_score, reverse=True)
            top_chunks = enriched[:reranked_k]

            rerank_elapsed = time.perf_counter() - t3_start
            log.info(
                "[Stage 3] Top %d selected  (%.3fs)  scores: min=%.4f  max=%.4f",
                len(top_chunks), rerank_elapsed,
                min(c.rerank_score for c in top_chunks),
                max(c.rerank_score for c in top_chunks),
            )

            # ══════════════════════════════════════════════════════════════
            # STAGE 4 — Filtering & Source Diversification
            # ══════════════════════════════════════════════════════════════
            max_per_j = CONFIG["max_chunks_per_jurnal"]
            final_k   = CONFIG["final_context_k"]
            log.info(
                "[Stage 4] Filtering: max %d/journal → taking top %d...",
                max_per_j, final_k,
            )

            jurnal_count: Dict[str, int] = {}
            final_chunks: List[EnrichedChunk] = []

            for chunk in top_chunks:
                jid = chunk.jurnal_id
                if jurnal_count.get(jid, 0) < max_per_j:
                    final_chunks.append(chunk)
                    jurnal_count[jid] = jurnal_count.get(jid, 0) + 1
                if len(final_chunks) >= final_k:
                    break

            log.info(
                "[Stage 4] Final: %d chunks from %d journals",
                len(final_chunks), len(jurnal_count),
            )

            # ══════════════════════════════════════════════════════════════
            # STAGE 5 — Memory Inject
            # ══════════════════════════════════════════════════════════════
            memory_text: str | None = None
            if chat_id is not None:
                log.info("[Stage 5] Fetching memory for chat_id=%d  user_id=%s...", chat_id, user_id)
                memory_text = self.get_memory(chat_id, query, user_id=user_id)
                if memory_text:
                    log.info("[Stage 5] Memory found  (%d chars)", len(memory_text))
                else:
                    log.info("[Stage 5] No memory yet — first question or no relevant entry.")
            else:
                log.debug("[Stage 5] chat_id=None — memory skipped.")

            # ══════════════════════════════════════════════════════════════
            # STAGE 6 — LLM Generation (Groq API, streaming)
            # ══════════════════════════════════════════════════════════════
            messages   = self._build_messages(query, final_chunks, lang=lang, memory=memory_text)
            answer_gen = self._generate_stream(
                messages,
                stop_event=stop_event,
                temperature=CONFIG["temperature"],
                top_p=CONFIG["top_p"],
                max_new_tokens=CONFIG["max_new_tokens"],
            )

            sources = [
                {
                    "sub_judul":    c.sub_judul,
                    "jurnal":       c.judul_jurnal,
                    "penulis":      c.penulis,
                    "tahun":        c.tanggal_rilis,
                    "doi":          c.doi or "-",
                    "halaman":      c.halaman,
                    "rerank_score": f"{c.rerank_score:.4f}",
                    "vector_score": f"{c.vector_score:.4f}",
                }
                for c in final_chunks
            ]

            elapsed = time.perf_counter() - t_start
            log.info(
                "[Knowledge] Pipeline done — %.3fs  |  chunks=%d  sources=%d  memory=%s",
                elapsed, len(final_chunks), len(sources),
                "yes" if memory_text else "no",
            )
            log.info("═" * 60)

            return RAGResponse(
                answer=answer_gen,
                sources=sources,
                final_chunks=top_chunks,
                processing_time=elapsed,
                retrieval_time=retrieval_elapsed,
                enrichment_time=enrichment_elapsed,
                rerank_time=rerank_elapsed,
                intent="knowledge",
            )

    def process_vision_query(
        self,
        query:        str,
        base64_image: str,
        chat_id:      int | None = None,
        user_id:      int | None = None,
        stop_event:   threading.Event = None,
    ) -> RAGResponse:
        with log_context("pipeline"):
            t_start = time.perf_counter()
            log.info("═" * 60)
            log.info("[Vision] Processing image with Gemini 3 Flash Preview...")

            # Scan query text before the image
            if query and query.strip():
                _vlang = self._detect_language(query)
                _vscan = self._scan_query(query, lang=_vlang)
                if _vscan["is_suspicious"]:
                    log.warning(
                        "[InputGuard] Vision query rejected — slot=%s patterns=%s",
                        _vscan["matched_slot"], _vscan["matched_patterns"][:5],
                    )
                    return RAGResponse(
                        answer=self._refuse_injection_stream("umum"),
                        sources=[], final_chunks=[],
                        processing_time=0.0, intent="blocked",
                    )

            gemini_api_key = os.environ.get("GEMINI_API_KEY")
            if not gemini_api_key:
                raise EnvironmentError("GEMINI_API_KEY not found in .env file")

            genai.configure(api_key=gemini_api_key)
            model = genai.GenerativeModel("gemini-3-flash-preview")

            try:
                image_data = base64.b64decode(base64_image)
                img = Image.open(io.BytesIO(image_data))
            except Exception as e:
                log.error("[Vision] Failed to load image: %s", e)
                raise ValueError("Invalid or corrupted image data.")

            prompt = (
                f"Sebagai pakar pertanian (ragna), tolong analisis gambar ini secara detail.\n\n"
                f"Konteks/Pertanyaan pengguna: {query}"
            )

            def stream_generator():
                try:
                    response = model.generate_content([prompt, img], stream=True)
                    for chunk in response:
                        if stop_event and stop_event.is_set():
                            log.info("[Gemini] Streaming stopped by user.")
                            break
                        if chunk.text:
                            yield chunk.text
                except Exception as e:
                    log.error("[Gemini] Error during API streaming: %s", e)
                    yield f"\n\n[Sistem] Maaf, terjadi kesalahan dari server Gemini. Detail: {e}"

            elapsed = time.perf_counter() - t_start
            log.info("[Vision] Image sent to Gemini (%.3fs)", elapsed)
            log.info("═" * 60)

            return RAGResponse(
                answer=stream_generator(),
                sources=[],
                final_chunks=[],
                processing_time=elapsed,
                intent="vision",
            )

    def process_regular_query(
        self,
        query:      str,
        chat_id:    int | None = None,
        stop_event: threading.Event = None,
        user_id:    int | None = None,
    ) -> RAGResponse:
        """Regular RAG Pipeline (without Neo4j enrichment)."""
        with log_context("pipeline"):
            t_start = time.perf_counter()
            log.info("═" * 60)
            log.info("[RegularRAG] Query: %r  chat_id=%s", query[:120], chat_id)

            _rlang = self._detect_language(query)
            _rscan = self._scan_query(query, lang=_rlang)
            if _rscan["is_suspicious"]:
                log.warning(
                    "[InputGuard] Regular query rejected — slot=%s patterns=%s",
                    _rscan["matched_slot"], _rscan["matched_patterns"][:5],
                )
                return RAGResponse(
                    answer=self._refuse_injection_stream("umum"),
                    sources=[], final_chunks=[],
                    processing_time=0.0, intent="blocked",
                )

            log.info("[InputGuard] Regular query is clean (lang=%s)", _rlang)

            # ── Language detection & NLP keyword enrichment ─────────────
            lang         = self._detect_language(query)
            nlp_keywords = self.models.extract_keywords_nlp(query, lang)
            enriched_query = f"{query} {nlp_keywords}".strip() if nlp_keywords else query

            # ══════════════════════════════════════════════════════════════
            # STAGE 1 — ChromaDB Retrieval (RAW COLLECTION)
            # ══════════════════════════════════════════════════════════════
            k = CONFIG.get("regular_retrieval_k", 6)
            log.info("[RegularRAG Stage 1] ChromaDB retrieval (k=%d) from raw_collection...", k)
            t1 = time.perf_counter()

            query_emb = self.models.get_embedding(enriched_query)

            raw_collection = self.chroma.client.get_collection(CONFIG["raw_collection"])
            results = raw_collection.query(
                query_embeddings=[query_emb],
                n_results=k,
                include=["documents", "metadatas", "distances"],
            )

            raw_chunks = []
            if results["ids"] and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    meta = results["metadatas"][0][i] if results["metadatas"] else {}
                    dist = results["distances"][0][i] if results["distances"] else 1.0
                    raw_chunks.append({
                        "id":           doc_id,
                        "text":         results["documents"][0][i],
                        "metadata":     meta,
                        "vector_score": dist,
                    })

            raw_retrieval_elapsed = time.perf_counter() - t1
            log.info("[RegularRAG Stage 1] %d chunks found (%.3fs)", len(raw_chunks), raw_retrieval_elapsed)

            if not raw_chunks:
                return RAGResponse(
                    answer="Maaf, tidak menemukan informasi relevan di database.",
                    sources=[], final_chunks=[],
                    processing_time=time.perf_counter() - t_start,
                    retrieval_time=raw_retrieval_elapsed,
                    intent="knowledge",
                )

            # ══════════════════════════════════════════════════════════════
            # STAGE 2 — Reranking (BGE Cross-Encoder)
            # ══════════════════════════════════════════════════════════════
            reranked_k = CONFIG.get("regular_reranked_k", 3)
            log.info("[RegularRAG Stage 2] BGE reranking (%d → top %d)...", len(raw_chunks), reranked_k)
            t2 = time.perf_counter()

            chunk_texts = [c["text"] for c in raw_chunks]
            scores      = self.models.rerank(query, chunk_texts)

            for i, score in enumerate(scores):
                raw_chunks[i]["rerank_score"] = float(score)

            raw_chunks.sort(key=lambda x: x["rerank_score"], reverse=True)
            top_chunks = raw_chunks[:reranked_k]

            raw_rerank_elapsed = time.perf_counter() - t2
            log.info("[RegularRAG Stage 2] Top %d selected (%.3fs)", len(top_chunks), raw_rerank_elapsed)

            # ══════════════════════════════════════════════════════════════
            # STAGE 3 — Memory Inject
            # ══════════════════════════════════════════════════════════════
            memory_text: str | None = None
            if chat_id is not None:
                memory_text = self.get_memory(chat_id, query, user_id=user_id)

            # ══════════════════════════════════════════════════════════════
            # STAGE 4 — LLM Generation
            # ══════════════════════════════════════════════════════════════
            messages   = self._build_regular_messages(query, top_chunks, lang=lang, memory=memory_text)
            answer_gen = self._generate_stream(
                messages,
                stop_event=stop_event,
                temperature=CONFIG["temperature"],
                top_p=CONFIG["top_p"],
                max_new_tokens=CONFIG["max_new_tokens"],
            )

            sources = [
                {
                    "chunk_text":   c["text"][:300] + ("…" if len(c["text"]) > 300 else ""),
                    "rerank_score": f"{c.get('rerank_score', 0):.4f}",
                    "vector_score": f"{c['vector_score']:.4f}",
                    "metadata":     c.get("metadata", {}),
                }
                for c in top_chunks
            ]

            elapsed = time.perf_counter() - t_start
            log.info("[RegularRAG] Pipeline done — %.3fs  |  chunks=%d", elapsed, len(top_chunks))
            log.info("═" * 60)

            return RAGResponse(
                answer=answer_gen,
                sources=sources,
                final_chunks=top_chunks,
                processing_time=elapsed,
                retrieval_time=raw_retrieval_elapsed,
                enrichment_time=0.0,
                rerank_time=raw_rerank_elapsed,
                intent="knowledge",
            )

    # ── Internal helpers ───────────────────────────────────────────────────

    @staticmethod
    def _detect_query_intent(text: str) -> str:
        """Route intent: 'knowledge' → RAG pipeline, 'social' → social pipeline."""
        normalized = text.lower().strip()
        words      = set(normalized.split())

        SOCIAL_PHRASES = {
            "apa kabar", "apakabar", "terima kasih", "terimakasih",
            "sampai jumpa", "selamat tinggal", "thank you",
        }
        SOCIAL_WORDS = {
            "hai", "halo", "hello", "hi", "hey",
            "kabar", "makasih", "thanks",
            "maaf", "sorry", "permisi",
            "dadah", "bye",
            "oke", "ok", "baik", "siap", "sip",
            "namaku", "ingat", "siapa aku"
        }
        for phrase in SOCIAL_PHRASES:
            if phrase in normalized:
                log.debug("[Intent] social — phrase: %r", phrase)
                return "social"
        if words & SOCIAL_WORDS:
            log.debug("[Intent] social — social keyword detected")
            return "social"

        if "?" in text:
            log.debug("[Intent] knowledge — question mark")
            return "knowledge"

        KNOWLEDGE_PHRASES = {
            "apa itu", "yang mana", "di mana",
            "coba ranking", "coba urutkan", "coba sebutkan", "coba jelaskan",
            "coba bandingkan", "coba ceritakan", "coba buat", "coba berikan",
            "coba tampilkan", "coba tunjukkan",
            "tolong jelaskan", "tolong sebutkan", "tolong ranking",
            "tolong urutkan", "tolong buat", "tolong berikan", "tolong ceritakan",
            "bisa jelaskan", "bisa sebutkan", "bisa ranking", "bisa urutkan",
            "dari yang", "mulai dari", "urutan dari",
            "dari terbanyak", "dari terbesar", "dari tertinggi",
            "sampai yang sedikit", "sampai yang kecil", "sampai yang rendah",
        }
        KNOWLEDGE_WORDS = {
            "apa", "apakah", "bagaimana", "mengapa", "kenapa",
            "siapa", "kapan", "dimana", "berapa", "seberapa", "manakah",
            "jelaskan", "sebutkan", "ceritakan", "gambarkan",
            "deskripsikan", "definisikan", "definisi", "contoh", "contohkan",
            "bandingkan", "bedakan", "perbedaan", "persamaan",
            "cara", "langkah", "proses", "prosedur", "metode",
            "penyebab", "akibat", "dampak", "gejala", "tanda",
            "pengertian", "maksud", "artinya", "fungsi", "manfaat",
            "ciri", "karakteristik", "jenis", "macam", "klasifikasi",
            "penanganan", "pengobatan", "pengendalian", "pencegahan",
            "ranking", "rangking", "urutan", "urutkan",
            "peringkat", "daftar", "susun", "susunkan",
            "terbanyak", "tersedikit", "terbesar", "terkecil",
            "tertinggi", "terendah", "terluas",
            "buatkan", "berikan", "tampilkan", "tunjukkan",
            "rekomendasikan", "rekomendasi",
            "hama", "penyakit", "patogen", "serangan", "infeksi",
            "tanaman", "tumbuhan", "pertanian", "agronomi", "pestisida",
            "pupuk", "lahan", "sawah", "kebun", "panen", "benih", "bibit",
            "what", "how", "why", "when", "where", "who", "which",
            "explain", "describe", "list", "define", "compare", "rank",
            "causes", "symptoms", "treatment", "control", "prevention",
            "give", "show", "recommend", "provide",
        }
        for phrase in KNOWLEDGE_PHRASES:
            if phrase in normalized:
                log.debug("[Intent] knowledge — phrase: %r", phrase)
                return "knowledge"
        if words & KNOWLEDGE_WORDS:
            log.debug("[Intent] knowledge — question keyword detected")
            return "knowledge"

        log.debug("[Intent] social — no knowledge indicators")
        return "social"

    @staticmethod
    def _detect_language(text: str) -> str:
        """Detect query language. Default is Indonesian."""
        en_markers = {
            "what", "how", "why", "when", "where", "who", "which",
            "explain", "describe", "tell", "list", "give", "show",
            "define", "compare", "is", "are", "does", "do", "can",
            "could", "the", "of", "in", "and", "or", "with", "for",
            "about", "symptoms", "disease", "plant", "fungus",
            "bacteria", "treatment", "control",
        }
        words    = set(text.lower().split())
        en_score = len(words & en_markers)
        return "en" if en_score >= 2 else "id"

    @staticmethod
    def _get_model_tier() -> str:
        model = CONFIG.get("groq_model", "").lower()

        import re as _r
        matches = _r.findall(r"(\d+)b", model)
        if matches:
            size = max(int(m) for m in matches)
            if size <= 4:
                return "small"
            if size <= 40:
                return "medium"
            return "large"

        if any(k in model for k in ("70b", "72b", "8x22b", "mixtral")):
            return "large"
        if any(k in model for k in ("32b",)):
            return "large"
        if any(k in model for k in ("7b", "8b", "12b", "13b")):
            return "medium"
        if any(k in model for k in ("3b", "1b")):
            return "small"

        return "large"

    def _build_messages(
        self,
        query:  str,
        chunks: List[EnrichedChunk],
        lang:   str = None,
        memory: str | None = None,
    ) -> List[Dict]:
        tier        = self._get_model_tier()
        safe_budget = GROQ_MODEL_SAFE_TOKEN_BUDGET.get(CONFIG["groq_model"], 4_800)
        _usable     = safe_budget - FIXED_OVERHEAD_TOKENS
        max_chars   = min(int(_usable * 0.30 * 4), CONFIG["context_max_chars"])

        if tier == "small":
            max_chars = min(max_chars, 3_000)
        elif tier == "medium":
            max_chars = min(max_chars, 6_000)

        log.info(
            "[BuildMessages] tier=%s  context_max_chars(CONFIG)=%d  max_chars(effective)=%d",
            tier, CONFIG["context_max_chars"], max_chars,
        )

        context_parts: List[str] = []
        used_chars = 0

        for i, c in enumerate(chunks, 1):
            part = f"[{i}] {c.sub_judul}\n{c.context_text}"
            if used_chars + len(part) > max_chars:
                remaining = max_chars - used_chars
                if remaining > 200:
                    context_parts.append(part[:remaining] + "…")
                break
            context_parts.append(part)
            used_chars += len(part)

        context_str = "\n\n".join(context_parts)

        source_lines = [
            f"[{i}] {c.judul_jurnal} — {c.penulis} ({c.tanggal_rilis})"
            + (f"  DOI: {c.doi}" if c.doi else "")
            + f"  hal. {c.halaman}"
            for i, c in enumerate(chunks, 1)
        ]
        source_str = "\n".join(source_lines)

        lang = lang if lang is not None else self._detect_language(query)

        if lang == "id":
            _max_memory_chars = 1_200
            if memory and len(memory) > _max_memory_chars:
                memory = memory[:_max_memory_chars] + "…"
                log.debug("[BuildMessages] Memory truncated to %d chars", _max_memory_chars)
            memory_section = (
                PROMPTS["knowledge_memory_block_id"].format(memory=memory)
                if memory else ""
            )
            prompt_key = {
                "small":  "knowledge_system_id_local",
                "medium": "knowledge_system_id_local",
                "large":  "knowledge_system_id",
            }[tier]
            system_content = PROMPTS[prompt_key].format(
                memory_section=memory_section,
                context_str=context_str,
                source_str=source_str,
            )
            question_label = "Pertanyaan"
        else:
            memory_section = (
                PROMPTS["knowledge_memory_block_en"].format(memory=memory)
                if memory else ""
            )
            prompt_key = {
                "small":  "knowledge_system_en_local",
                "medium": "knowledge_system_en_local",
                "large":  "knowledge_system_en",
            }[tier]
            system_content = PROMPTS[prompt_key].format(
                memory_section=memory_section,
                context_str=context_str,
                source_str=source_str,
            )
            question_label = "Question"

        log.debug(
            "[BuildMessages] tier=%s  model=%s  prompt=%s  lang=%s  context=%d chars  memory=%s",
            tier,
            CONFIG.get("groq_model", "?"),
            prompt_key,
            lang,
            len(context_str),
            "yes" if memory else "no",
        )

        return [
            {"role": "system", "content": system_content},
            {"role": "user",   "content": f"{question_label}: {query}"},
        ]

    def _build_regular_messages(
        self,
        query:  str,
        chunks: List[Dict],
        lang:   str = None,
        memory: str | None = None,
    ) -> List[Dict]:
        """Build messages for Regular RAG (without complex journal metadata)."""
        lang = lang if lang is not None else self._detect_language(query)
        tier = self._get_model_tier()

        safe_budget = GROQ_MODEL_SAFE_TOKEN_BUDGET.get(CONFIG["groq_model"], 4_800)
        _usable     = safe_budget - FIXED_OVERHEAD_TOKENS
        max_chars   = min(int(_usable * 0.30 * 4), CONFIG["context_max_chars"])

        if tier == "small":
            max_chars = min(max_chars, 3_000)
        elif tier == "medium":
            max_chars = min(max_chars, 6_000)

        context_parts = []
        used_chars = 0

        for i, chunk in enumerate(chunks, 1):
            part = f"[{i}] {chunk['text']}"
            if used_chars + len(part) > max_chars:
                remaining = max_chars - used_chars
                if remaining > 200:
                    context_parts.append(part[:remaining] + "…")
                break
            context_parts.append(part)
            used_chars += len(part)

        context_str = "\n\n".join(context_parts)
        source_str  = "\n".join([f"[{i}] Chunk {i}" for i in range(1, len(chunks) + 1)])

        if lang == "id":
            memory_section = (
                PROMPTS["knowledge_memory_block_id"].format(memory=memory)
                if memory else ""
            )
            prompt_key = {
                "small":  "knowledge_system_id_local",
                "medium": "knowledge_system_id_local",
                "large":  "knowledge_system_id",
            }[tier]
            system_content = PROMPTS[prompt_key].format(
                memory_section=memory_section,
                context_str=context_str,
                source_str=source_str,
            )
            question_label = "Pertanyaan"
        else:
            memory_section = (
                PROMPTS["knowledge_memory_block_en"].format(memory=memory)
                if memory else ""
            )
            prompt_key = {
                "small":  "knowledge_system_en_local",
                "medium": "knowledge_system_en_local",
                "large":  "knowledge_system_en",
            }[tier]
            system_content = PROMPTS[prompt_key].format(
                memory_section=memory_section,
                context_str=context_str,
                source_str=source_str,
            )
            question_label = "Question"

        return [
            {"role": "system", "content": system_content},
            {"role": "user",   "content": f"{question_label}: {query}"},
        ]

    def _generate_stream(
        self,
        messages:       List[Dict],
        stop_event:     threading.Event = None,
        temperature:    float = None,
        top_p:          float = None,
        max_new_tokens: int   = None,
    ) -> Generator[str, None, None]:
        """Generate answer — routing based on CONFIG['llm_mode']."""
        _temperature    = temperature    if temperature    is not None else CONFIG["temperature"]
        _top_p          = top_p          if top_p          is not None else CONFIG["top_p"]
        _max_new_tokens = max_new_tokens if max_new_tokens is not None else CONFIG["max_new_tokens"]

        llm_mode = self.models.llm_mode

        if llm_mode == "local":
            yield from self._generate_stream_local(
                messages, stop_event, _temperature, _top_p, _max_new_tokens
            )
        else:
            yield from self._generate_stream_groq(
                messages, stop_event, _temperature, _top_p, _max_new_tokens
            )

    def _generate_stream_groq(
        self,
        messages:       List[Dict],
        stop_event:     threading.Event,
        temperature:    float,
        top_p:          float,
        max_new_tokens: int,
    ) -> Generator[str, None, None]:
        """Generate via Groq API with SSE streaming."""
        log.info(
            "[Groq] Generate — model=%s  max_tokens=%d  temperature=%.2f  top_p=%.2f",
            CONFIG["groq_model"], max_new_tokens, temperature, top_p,
        )
        gen_start   = time.perf_counter()
        token_count = 0
        stream      = None

        try:
            stream = self.models.groq_client.chat.completions.create(
                model=CONFIG["groq_model"],
                messages=messages,
                max_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
                stream=True,
            )

            for chunk in stream:
                if stop_event is not None and stop_event.is_set():
                    log.info("[Groq] Stop event at token %d", token_count)
                    break

                delta = chunk.choices[0].delta
                text  = getattr(delta, "content", None)
                if text:
                    token_count += 1
                    yield text

        except GeneratorExit:
            log.info("[Groq] GeneratorExit at token %d", token_count)
        except Exception:
            log.exception("[Groq] Error during streaming")
            raise
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            elapsed = time.perf_counter() - gen_start
            log.info("[Groq] ✓ Done — %d chunks  %.3fs", token_count, elapsed)

    def _generate_stream_local(
        self,
        messages:       List[Dict],
        stop_event:     threading.Event,
        temperature:    float,
        top_p:          float,
        max_new_tokens: int,
    ) -> Generator[str, None, None]:
        """Generate via HuggingFace AutoModelForCausalLM (folder cloned from HF Hub)."""
        model_name = os.path.basename(CONFIG.get("local_llm_path", "local"))
        log.info(
            "[LocalLLM] Generate — model=%s  max_tokens=%d  temperature=%.2f  top_p=%.2f",
            model_name, max_new_tokens, temperature, top_p,
        )
        gen_start   = time.perf_counter()
        token_count = 0
        tokenizer   = self.models.local_tokenizer
        model       = self.models.local_llm

        try:
            try:
                encoding = tokenizer.apply_chat_template(
                    messages,
                    add_generation_prompt=True,
                    return_tensors="pt",
                )
                input_ids = encoding.input_ids.to(model.device)
            except Exception:
                log.warning("[LocalLLM] Chat template unavailable, using fallback")
                raw = "".join(
                    f"{m['role'].upper()}: {m['content']}" for m in messages
                ) + "ASSISTANT:"
                prompt_ids = tokenizer(raw, return_tensors="pt").input_ids.to(model.device)
                input_ids = prompt_ids

            streamer = TextIteratorStreamer(
                tokenizer,
                skip_prompt=True,
                skip_special_tokens=True,
            )

            gen_kwargs = dict(
                input_ids=input_ids,
                streamer=streamer,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
                do_sample=temperature > 0,
                pad_token_id=tokenizer.pad_token_id,
                eos_token_id=tokenizer.eos_token_id,
            )

            gen_thread = threading.Thread(
                target=model.generate,
                kwargs=gen_kwargs,
                daemon=True,
            )
            gen_thread.start()

            for text in streamer:
                if stop_event is not None and stop_event.is_set():
                    log.info("[LocalLLM] Stop event at token %d", token_count)
                    break
                if text:
                    token_count += 1
                    yield text

            gen_thread.join(timeout=5)

        except GeneratorExit:
            log.info("[LocalLLM] GeneratorExit at token %d", token_count)
        except Exception:
            log.exception("[LocalLLM] Error during streaming")
            raise
        finally:
            elapsed = time.perf_counter() - gen_start
            log.info("[LocalLLM] ✓ Done — %d tokens  %.3fs", token_count, elapsed)

    # ── Utility ────────────────────────────────────────────────────────────

    def simple_retrieval(self, query: str, k: int = 5) -> List[Dict]:
        """Testing retrieval without LLM."""
        log.info("simple_retrieval: query=%r  k=%d", query[:80], k)

        emb        = self.models.get_embedding(query)
        candidates = self.chroma.retrieve(emb, k=k)
        enriched   = self.neo4j.enrich(candidates)

        return [
            {
                "sub_judul":    c.sub_judul,
                "konten_chunk": c.konten_chunk[:500] + ("…" if len(c.konten_chunk) > 500 else ""),
                "jurnal":       c.judul_jurnal,
                "penulis":      c.penulis,
                "tahun":        c.tanggal_rilis,
                "halaman":      c.halaman,
                "vector_score": c.vector_score,
            }
            for c in enriched
        ]

    def _analyze_image(self, base64_image: str) -> Tuple[bool, str]:
        """
        Step 1: Extract information from the image into descriptive text.

        P3: returns (success, message) tuple instead of a raw string,
            so the caller doesn't need fragile startswith() checks.
        """
        gemini_api_key = os.environ.get("GEMINI_API_KEY")
        if not gemini_api_key:
            return (False, "GEMINI_API_KEY not found in environment.")

        genai.configure(api_key=gemini_api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")

        try:
            image_data = base64.b64decode(base64_image)
            img = Image.open(io.BytesIO(image_data))
        except Exception as e:
            log.error("[Vision] Failed to load image: %s", e)
            return (False, "Uploaded image is corrupted or unreadable.")

        prompt = (
            "Sebagai pakar pertanian, tolong identifikasi dan jelaskan "
            "apa yang terlihat pada gambar tanaman ini secara detail, "
            "khususnya jika terdapat gejala penyakit, hama, atau kondisi abnormal."
        )

        try:
            response = model.generate_content([prompt, img])
            return (True, response.text)
        except Exception as e:
            log.error("[Gemini] Image analysis error: %s", e)
            return (False, f"Failed to analyze image from server: {e}")


# =============================================================================
# SINGLETON
# =============================================================================

_rag_pipeline: Optional[RAGPipeline] = None


def get_rag_pipeline() -> RAGPipeline:
    """Get-or-create the RAGPipeline singleton."""
    global _rag_pipeline
    if _rag_pipeline is None:
        _rag_pipeline = RAGPipeline()
    return _rag_pipeline


def reset_pipeline() -> None:
    """Force destroy and rebuild the whole pipeline + models."""
    global _rag_pipeline
    log.warning("reset_pipeline() called — rebuilding from scratch.")
    if _rag_pipeline is not None:
        try:
            _rag_pipeline.close()
        except Exception:
            pass
        _rag_pipeline = None
    RAGModels.reset()
    _rag_pipeline = RAGPipeline()


def reload_with_model(mode: str, local_llm_path: str = None) -> None:
    if mode != "groq":
        raise ValueError(
            "Mode 'local' is disabled. Only 'groq' mode is supported."
        )
    log.info(
        "reload_with_model() → mode=groq  model=%s",
        CONFIG.get("groq_model"),
    )
    set_llm_mode("groq", None)

    log.info(
        "reload_with_model() done — embedding=%s  reranker=%s  nlp=%s  llm=groq(%s)",
        CONFIG["embedding_device"],
        CONFIG["reranker_device"],
        "cuda" if CONFIG["nlp_device"] >= 0 else "cpu",
        CONFIG["groq_model"],
    )