# embedder.py
"""
Embedder — PDF ingestion pipeline for the RAG knowledge base.

Operating modes:
  1. IMPORTED — called by main.py when user uploads a PDF
     → Logging: inherits main.py's configuration (no extra setup)
     → Context tag: "ingest" → routes to the INGEST panel

  2. STANDALONE — run as `python embedder.py` from CMD
     → Logging: sets up its own (INFO level, silences noisy libs)
     → Context tag: "ingest" → still writes to plain stdout

All ingest-phase logs automatically route to the INGEST panel
thanks to the `log_context("ingest")` context manager.
"""

import hashlib
import logging
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import chromadb
import pdfplumber
from neo4j import GraphDatabase

# =============================================================================
# UTF-8 ENCODING SAFETY (moved to function — FIX P1)
# =============================================================================
# Previously sys.stdout.reconfigure(...) was called at module level,
# which is a dangerous side effect when embedder.py is imported from
# another context (pytest, uvicorn worker, dashboard).
#
# Now: only called in _setup_cli_logging() (standalone mode).
# =============================================================================

def _reconfigure_stdout_utf8() -> None:
    """Reconfigure stdout/stderr to UTF-8 (safe to call once)."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        # If stdout is already wrapped by another framework, skip silently.
        pass


# =============================================================================
# RESOLVE ROOT PROJECT DIRECTORY (cd ..)
# =============================================================================
PARENT_DIR = Path(__file__).resolve().parent.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

# =============================================================================
# IMPORTS FROM ROOT
# =============================================================================
from config import CONFIG
from pipeline import RAGModels

# ── Context tag for the dashboard ────────────────────────────────────────
# If dashboard.py is not available (headless / CI), use a no-op stub.
try:
    from dashboard import log_context
except ImportError:
    from contextlib import contextmanager

    @contextmanager
    def log_context(tag: str):  # type: ignore
        """No-op fallback when dashboard.py is not available."""
        yield

# ── Progress reporter (for the Go launcher) ─────────────────────────────
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

# ── Instruction cache invalidation (optional) ───────────────────────────
# Used at the end of ingest so the server (if any) refreshes the instruction set.
try:
    from pipeline import invalidate_instruction_cache
except ImportError:
    def invalidate_instruction_cache(lang: Optional[str] = None):  # type: ignore
        pass


# =============================================================================
# ENV & HF_TOKEN SETUP (WITH FALLBACK)
# =============================================================================
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    root_env_path = PARENT_DIR / ".env"
    if root_env_path.exists():
        load_dotenv(dotenv_path=root_env_path)
    else:
        load_dotenv()

HF_TOKEN = os.getenv("HF_TOKEN")
if HF_TOKEN:
    os.environ["HF_TOKEN"] = HF_TOKEN
    os.environ["HUGGING_FACE_HUB_TOKEN"] = HF_TOKEN

# No print() at module level — HF_TOKEN info is logged in _setup_cli_logging().

logger = logging.getLogger(__name__)


# =============================================================================
# LOAD CONFIGURATION FROM CONFIG.PY
# =============================================================================

# Neo4j Settings
NEO4J_URI      = CONFIG.get("neo4j_uri", "neo4j://127.0.0.1:7687")
NEO4J_USER     = CONFIG.get("neo4j_user", "neo4j")
NEO4J_PASSWORD = CONFIG.get("neo4j_password", "password")

# ChromaDB Settings
raw_chroma_path = CONFIG.get("chroma_path", "chroma_db")
CHROMA_PATH = (
    raw_chroma_path
    if os.path.isabs(raw_chroma_path)
    else str((PARENT_DIR / raw_chroma_path).resolve())
)
CHROMA_COLLECTION = CONFIG.get("chroma_collection", "konten_isi")
RAW_COLLECTION    = CONFIG.get("raw_collection", "konten_isi_raw")

# Dataset & Ingestion Parameters
raw_dataset_path = CONFIG.get("dataset_path", "./dataset")
DATASET_PATH = (
    raw_dataset_path
    if os.path.isabs(raw_dataset_path)
    else str((PARENT_DIR / raw_dataset_path).resolve())
)

MAX_TOKENS_PER_CHUNK        = CONFIG.get("max_tokens_per_chunk", 512)
SUBHEADING_SCORE_THRESHOLD  = CONFIG.get("subheading_score_threshold", 4)


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class JurnalNode:
    id:            str
    judul:         str
    doi:           Optional[str]
    penulis:       str
    tanggal_rilis: str
    source_file:   str
    file_hash:     str


@dataclass
class IsiNode:
    id:            str
    jurnal_id:     str
    sub_judul:     str
    konten_chunk:  str
    halaman:       int
    quarantined:   bool = False
    scan_score:    float = 0.0
    scan_matched:  Optional[list] = None


# =============================================================================
# FILE HASHING (MD5)
# =============================================================================

def calculate_file_hash(file_path: str) -> str:
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def is_file_processed(neo4j_driver, file_hash: str) -> bool:
    query = """
    MATCH (j:Jurnal {file_hash: $file_hash})
    RETURN count(j) > 0 AS exists
    """
    with neo4j_driver.session() as session:
        result = session.run(query, file_hash=file_hash).single()
        return result["exists"] if result else False


# =============================================================================
# PROMPT INJECTION SCANNER
# =============================================================================
#
# This scanner detects prompt injection patterns in chunks/lines that
# come from user-uploaded PDFs. Reference patterns are read from
# :Instruction nodes in Neo4j (see prompt_guard.py).
#
# P0-B: cache stores (data, timestamp) tuples for TTL and is only
#       populated on SUCCESSFUL Neo4j queries.
# =============================================================================

_INSTRUCTION_CACHE:       Dict[str, tuple] = {}   # lang -> (data, timestamp)
_INSTRUCTION_CACHE_LOCK   = threading.Lock()
_INSTRUCTION_CACHE_TTL    = 300   # seconds — 5 min; 0 to disable
_COMPILED_PATTERN_CACHE:  Dict[str, re.Pattern] = {}
_RISK_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}


def _get_compiled_pattern(pattern: str) -> re.Pattern:
    """Pre-compile regex — cached so we don't recompile on every chunk."""
    if pattern not in _COMPILED_PATTERN_CACHE:
        try:
            _COMPILED_PATTERN_CACHE[pattern] = re.compile(pattern, re.IGNORECASE)
        except re.error:
            _COMPILED_PATTERN_CACHE[pattern] = re.compile(r"(?!x)x")
    return _COMPILED_PATTERN_CACHE[pattern]


def _load_instructions_for_lang(neo4j_driver, lang: str) -> List[Dict]:
    """
    Fetch instruction set from Neo4j for a given language.

    P0-B: results are ONLY cached on successful Neo4j queries.
          On exception, [] is returned but NOT cached, so the next
          attempt will retry the Neo4j query (prevents permanently
          disabled guard until restart).

    P3:   TTL cache — old instructions auto-refresh after
          _INSTRUCTION_CACHE_TTL seconds.
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
        with neo4j_driver.session() as session:
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

        # P0-B: only cache on success
        with _INSTRUCTION_CACHE_LOCK:
            _INSTRUCTION_CACHE[lang] = (instructions, now)

        return instructions

    except Exception as e:
        logger.warning(
            "[Scanner] Failed to fetch instructions lang=%s: %s "
            "(cache NOT updated, will retry when needed)",
            lang, e,
        )
        # P0-B: do NOT cache []
        return []


def invalidate_instruction_cache_local(lang: Optional[str] = None):
    """
    Force refresh the instruction set cache (embedder's local version).

    Note: this only clears the cache in this embedder module. The cache
    in pipeline.py (if the server is running) is cleared via
    invalidate_instruction_cache() imported from pipeline.
    """
    with _INSTRUCTION_CACHE_LOCK:
        if lang is None:
            _INSTRUCTION_CACHE.clear()
        else:
            _INSTRUCTION_CACHE.pop(lang, None)
    logger.info("[Scanner] Instruction cache (embedder) invalidated (lang=%s)", lang or "all")


def scan_chunk_against_instructions(
    text: str,
    neo4j_driver,
    lang: str = "id",
) -> Dict:
    """Scan text against the instruction set graph."""
    instructions = _load_instructions_for_lang(neo4j_driver, lang)

    matched_patterns: List[str] = []
    matched_slot: Optional[str] = None
    max_risk = 0.0

    for instr in instructions:
        slot_weight = _RISK_WEIGHT.get(instr["risk_level"], 0.5)
        for pattern in instr["patterns"]:
            try:
                rx = _get_compiled_pattern(pattern)
                if rx.search(text):
                    matched_patterns.append(pattern)
                    if slot_weight > max_risk:
                        max_risk = slot_weight
                        matched_slot = instr["slot"]
            except Exception:
                continue

    return {
        "is_suspicious":    len(matched_patterns) > 0,
        "matched_patterns": matched_patterns,
        "risk_score":       max_risk,
        "matched_slot":     matched_slot,
    }


def scan_lines_against_instructions(
    lines: List[Dict],
    neo4j_driver,
    lang: str = "id",
) -> List[Dict]:
    """Scan each line from the parsed PDF — detects line-level injections."""
    suspicious = []
    for line in lines:
        text = line.get("text", "")
        if not text:
            continue
        result = scan_chunk_against_instructions(text, neo4j_driver, lang)
        if result["is_suspicious"]:
            suspicious.append({
                "page":             line.get("page"),
                "text":             text,
                "matched_slot":     result["matched_slot"],
                "matched_patterns": result["matched_patterns"],
                "risk_score":       result["risk_score"],
            })
    return suspicious


def scan_chunks_against_instructions(
    chunks: List[str],
    neo4j_driver,
    lang: str = "id",
) -> List[Dict]:
    """Scan a list of chunks (for the RAW pipeline)."""
    suspicious = []
    for idx, chunk in enumerate(chunks):
        result = scan_chunk_against_instructions(chunk, neo4j_driver, lang)
        if result["is_suspicious"]:
            suspicious.append({
                "chunk_index":      idx,
                "matched_slot":     result["matched_slot"],
                "matched_patterns": result["matched_patterns"],
                "risk_score":       result["risk_score"],
            })
    return suspicious


# =============================================================================
# STEP 1 — PDF INGESTION (2-COLUMN AWARE)
# =============================================================================

def _detect_column_split(words: List[Dict], page_width: float) -> Optional[float]:
    if not words or page_width <= 0:
        return None

    bucket_count = 20
    bucket_size  = page_width / bucket_count
    buckets      = [0] * bucket_count

    for w in words:
        mid_x = (w["x0"] + w["x1"]) / 2
        idx = min(int(mid_x / bucket_size), bucket_count - 1)
        buckets[idx] += 1

    center_start = int(bucket_count * 0.30)
    center_end   = int(bucket_count * 0.70)

    gap_buckets = [i for i in range(center_start, center_end) if buckets[i] == 0]
    if not gap_buckets:
        return None

    gap_center_idx = gap_buckets[len(gap_buckets) // 2]
    return (gap_center_idx + 0.5) * bucket_size


def _group_words_into_lines(words: List[Dict], page_num: int, page_height: float) -> List[Dict]:
    lines: List[Dict] = []
    current_line: List[Dict] = []
    current_y: Optional[float] = None

    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if current_y is None:
            current_y = word["top"]
            current_line = [word]
        elif abs(word["top"] - current_y) < 5:
            current_line.append(word)
        else:
            if current_line:
                lines.append(_build_line_dict(current_line, page_num, current_y, page_height))
            current_y = word["top"]
            current_line = [word]

    if current_line:
        lines.append(_build_line_dict(current_line, page_num, current_y or 0, page_height))

    return lines


def parse_pdf_to_lines(pdf_path: str) -> List[Dict]:
    all_lines: List[Dict] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(x_tolerance=3, y_tolerance=3, keep_blank_chars=False)
            if not words:
                continue

            page_width  = float(page.width)
            page_height = float(page.height)

            col_split = _detect_column_split(words, page_width)

            if col_split is not None:
                left_words  = [w for w in words if w["x1"] <= col_split]
                right_words = [w for w in words if w["x0"] >  col_split]

                all_lines.extend(_group_words_into_lines(left_words,  page_num, page_height))
                all_lines.extend(_group_words_into_lines(right_words, page_num, page_height))
            else:
                all_lines.extend(_group_words_into_lines(words, page_num, page_height))

    return all_lines


def _build_line_dict(words: List[Dict], page_num: int, y_pos: float, page_height: float = 0.0) -> Dict:
    line_text = " ".join(w["text"] for w in words)
    avg_size  = sum(float(w.get("height", 10)) for w in words) / len(words)
    is_bold   = any("bold" in str(w.get("fontname", "")).lower() for w in words)
    return {
        "text":        line_text.strip(),
        "page":        page_num,
        "font_size":   avg_size,
        "is_bold":     is_bold,
        "y_position":  y_pos,
        "page_height": page_height,
    }


# =============================================================================
# STEP 2 — REMOVE BOILERPLATE
# =============================================================================

BOILERPLATE_PATTERNS = [
    r"Prosiding SEMNAS BIO",
    r"ISSN",
    r"Quo Vadis",
    r"^\d+$",
    r"^halaman\s+\d+",
    r"©\s*\d{4}",
    r"www\.",
    r"http://",
    r"https://",
]


def is_boilerplate(text: str) -> bool:
    text = text.strip()
    if len(text) < 3:
        return True
    for pattern in BOILERPLATE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def clean_lines(lines: List[Dict]) -> List[Dict]:
    return [l for l in lines if not is_boilerplate(l["text"])]


# =============================================================================
# STEP 3 — HEADING DETECTION
# =============================================================================

def is_all_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) > 0.8


def compute_dominant_font_size(lines: List[Dict]) -> float:
    from statistics import mode
    sizes = [round(l["font_size"], 1) for l in lines if l.get("font_size")]
    if not sizes:
        return 10.0
    try:
        return mode(sizes)
    except Exception:
        return sorted(sizes)[len(sizes) // 2]


def compute_normal_line_gap(lines: List[Dict]) -> float:
    gaps = []
    for i in range(1, len(lines)):
        if lines[i]["page"] == lines[i - 1]["page"]:
            gap = lines[i]["y_position"] - lines[i - 1]["y_position"]
            if gap > 0:
                gaps.append(gap)
    if not gaps:
        return 12.0
    gaps_sorted = sorted(gaps)
    return gaps_sorted[len(gaps_sorted) // 2]


def score_subheading(
    line: Dict,
    prev_line: Optional[Dict],
    dominant_font_size: float,
    normal_line_gap: float,
) -> int:
    text = line["text"].strip()
    score = 0

    if line.get("is_bold"):
        score += 2

    if prev_line and prev_line["page"] == line["page"]:
        gap = line["y_position"] - prev_line["y_position"]
        if gap > 1.5 * normal_line_gap:
            score += 2
    else:
        score += 2

    if line.get("font_size", 0) > dominant_font_size:
        score += 2

    if not text.endswith("."):
        score += 1

    if is_all_caps(text):
        score += 1

    if len(text) > 60:
        score -= 2

    page_height = line.get("page_height", 0)
    if page_height > 0 and line["y_position"] > (page_height * 0.9):
        score -= 1

    return score


def is_subheading(
    line: Dict,
    prev_line: Optional[Dict],
    dominant_font_size: float,
    normal_line_gap: float,
) -> bool:
    text = line["text"].strip()
    if len(text) < 3:
        return False
    score = score_subheading(line, prev_line, dominant_font_size, normal_line_gap)
    return score >= SUBHEADING_SCORE_THRESHOLD


# =============================================================================
# STEP 4 — TOKEN COUNTING & CHUNKING
# =============================================================================

def count_tokens(text: str) -> int:
    return len(text) // 4


def split_text_word_safe(text: str, max_tokens: int) -> List[str]:
    chunks: List[str] = []
    current_chunk = ""
    sentences = re.split(r"([.!?]+\s+)", text)

    for i in range(0, len(sentences), 2):
        sentence  = sentences[i]
        delimiter = sentences[i + 1] if i + 1 < len(sentences) else ""
        full_sentence = sentence + delimiter

        if current_chunk and count_tokens(current_chunk + full_sentence) > max_tokens:
            chunks.append(current_chunk.strip())
            current_chunk = full_sentence
        else:
            current_chunk += full_sentence

        if count_tokens(current_chunk) > max_tokens:
            words = current_chunk.split()
            temp_chunk = ""
            for word in words:
                if count_tokens(temp_chunk + " " + word) > max_tokens:
                    if temp_chunk:
                        chunks.append(temp_chunk.strip())
                        temp_chunk = word
                    else:
                        chunks.append(word)
                        temp_chunk = ""
                else:
                    temp_chunk += " " + word if temp_chunk else word
            current_chunk = temp_chunk

    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return chunks


# =============================================================================
# STEP 5 — BUILD ISI NODES
# =============================================================================

def deterministic_id(jurnal_id: str, sub_judul: str, chunk_index: int) -> str:
    base = f"{jurnal_id}:{sub_judul}:{chunk_index}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, base))


def build_isi_nodes(lines: List[Dict], jurnal_id: str) -> List[IsiNode]:
    if not lines:
        return []

    dominant_font_size = compute_dominant_font_size(lines)
    normal_line_gap    = compute_normal_line_gap(lines)

    logger.debug(
        "[Heuristic] dominant_font_size=%.1f  normal_line_gap=%.1f  threshold=%d",
        dominant_font_size, normal_line_gap, SUBHEADING_SCORE_THRESHOLD,
    )

    isi_nodes: List[IsiNode] = []
    current_heading = "Pendahuluan"
    current_buffer: List[Dict] = []
    current_page_start = lines[0]["page"]
    global_chunk_counter = 0

    def flush_buffer():
        nonlocal current_heading, global_chunk_counter
        if not current_buffer:
            return
        full_text = " ".join(l["text"] for l in current_buffer)
        chunks = split_text_word_safe(full_text, MAX_TOKENS_PER_CHUNK)
        for chunk in chunks:
            node_id = deterministic_id(jurnal_id, current_heading, global_chunk_counter)
            isi_nodes.append(IsiNode(
                id=node_id,
                jurnal_id=jurnal_id,
                sub_judul=current_heading,
                konten_chunk=chunk,
                halaman=current_page_start,
                scan_matched=[],
            ))
            global_chunk_counter += 1

    for i, line in enumerate(lines):
        prev_line = lines[i - 1] if i > 0 else None

        if is_subheading(line, prev_line, dominant_font_size, normal_line_gap):
            flush_buffer()
            current_heading = line["text"].strip().rstrip(":.").strip()
            current_buffer = []
            current_page_start = line["page"]
        else:
            current_buffer.append(line)

    flush_buffer()
    return isi_nodes


# =============================================================================
# NEO4J INGESTOR
# =============================================================================

class Neo4jIngestor:
    def __init__(self, uri: str = NEO4J_URI, user: str = NEO4J_USER, password: str = NEO4J_PASSWORD):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        logger.info("Neo4j connected: %s", uri)

    def close(self):
        self.driver.close()

    def create_constraints(self):
        with self.driver.session() as session:
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (j:Jurnal) REQUIRE j.id IS UNIQUE")
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (i:Isi) REQUIRE i.id IS UNIQUE")
        logger.info("Neo4j constraints ensured.")

    def ingest_jurnal(self, jurnal: JurnalNode):
        query = """
        MERGE (j:Jurnal {id: $id})
        SET j.judul         = $judul,
            j.doi           = $doi,
            j.penulis       = $penulis,
            j.tanggal_rilis = $tanggal_rilis,
            j.file_hash     = $file_hash,
            j.source_file   = $source_file
        """
        with self.driver.session() as session:
            session.run(
                query,
                id=jurnal.id,
                judul=jurnal.judul,
                doi=jurnal.doi or "",
                penulis=jurnal.penulis,
                tanggal_rilis=jurnal.tanggal_rilis,
                file_hash=jurnal.file_hash,
                source_file=jurnal.source_file,
            )
        logger.info("  ✓ Neo4j: Jurnal ingested (%s)", jurnal.id)

    def ingest_isi_nodes(self, isi_nodes: List[IsiNode]):
        """
        Save ALL Isi nodes to Neo4j, including quarantined ones.

        Note: quarantine only applies to ChromaDB. Quarantined nodes
        remain in Neo4j for audit trail, BUT pipeline.py already
        filters `quarantined = true` from Cypher enrichment (P0-A).
        """
        if not isi_nodes:
            return

        batch = [
            {
                "id":           n.id,
                "jurnal_id":    n.jurnal_id,
                "sub_judul":    n.sub_judul,
                "konten_chunk": n.konten_chunk,
                "halaman":      n.halaman,
                "quarantined":  getattr(n, "quarantined", False),
                "scan_score":   getattr(n, "scan_score", 0.0),
                "scan_matched": getattr(n, "scan_matched", []) or [],
            }
            for n in isi_nodes
        ]

        with self.driver.session() as session:
            session.run(
                """
                UNWIND $batch AS row
                MERGE (i:Isi {id: row.id})
                SET i.sub_judul    = row.sub_judul,
                    i.konten_chunk = row.konten_chunk,
                    i.halaman      = row.halaman,
                    i.quarantined  = row.quarantined,
                    i.scan_score   = row.scan_score,
                    i.scan_matched = row.scan_matched
                """,
                batch=batch,
            )

            session.run(
                """
                UNWIND $batch AS row
                MATCH (j:Jurnal {id: row.jurnal_id})
                MATCH (i:Isi    {id: row.id})
                MERGE (j)-[:HAS_SECTION]->(i)
                """,
                batch=batch,
            )

            next_pairs = [
                {"from_id": isi_nodes[i].id, "to_id": isi_nodes[i + 1].id}
                for i in range(len(isi_nodes) - 1)
            ]
            if next_pairs:
                session.run(
                    """
                    UNWIND $pairs AS pair
                    MATCH (a:Isi {id: pair.from_id})
                    MATCH (b:Isi {id: pair.to_id})
                    MERGE (a)-[:NEXT]->(b)
                    """,
                    pairs=next_pairs,
                )

        logger.info(
            "  ✓ Neo4j: %d Isi nodes ingested with HAS_SECTION & NEXT edges",
            len(isi_nodes),
        )


# =============================================================================
# CHROMADB INGESTOR (IMPROVED)
# =============================================================================
#
# P1: added `client` parameter so it can share the client with
#     ChromaRetriever in pipeline.py.
#
# P1: use collection.upsert() instead of add(), since IDs are
#     deterministic (uuid5). If ingest fails midway and is retried,
#     add() would fail on duplicate IDs.
# =============================================================================

class ChromaIngestor:
    def __init__(
        self,
        persist_directory: str = CHROMA_PATH,
        client: Optional[chromadb.ClientAPI] = None,
    ):
        self.client = client or chromadb.PersistentClient(path=persist_directory)
        self.collection = self.client.get_or_create_collection(
            name=CHROMA_COLLECTION,
            metadata={"description": "Embeddings of konten_chunk from Isi nodes"},
        )
        logger.info("ChromaDB initialized at: %s", persist_directory)

    def ingest_isi_nodes(
        self,
        isi_nodes: List[IsiNode],
        rag_models: RAGModels,
        judul_jurnal: str = "",
    ):
        if not isi_nodes:
            return

        ids       = [n.id for n in isi_nodes]
        documents = [n.konten_chunk for n in isi_nodes]

        texts_to_embed = [
            f"{judul_jurnal} | {n.sub_judul} | {n.konten_chunk}"
            for n in isi_nodes
        ]
        embeddings = rag_models.embed_batch_safe(texts_to_embed)

        metadatas = [
            {"isi_id": n.id, "jurnal_id": n.jurnal_id}
            for n in isi_nodes
        ]

        # P1: upsert, not add — safe to re-run
        self.collection.upsert(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        logger.info(
            "ChromaDB: %d konten_chunk ingested into '%s' (upsert)",
            len(isi_nodes), CHROMA_COLLECTION,
        )


# =============================================================================
# PIPELINE — IMPROVED (with progress events for the INGEST panel)
# =============================================================================

def run_pipeline(
    pdf_path: str,
    jurnal_metadata: Dict,
    rag_models: RAGModels,
    neo4j: Neo4jIngestor,
    chroma: ChromaIngestor,
) -> Optional[Dict]:
    """
    IMPROVED pipeline — called from main.py (via the upload service)
    or from the standalone CLI. All logs go to the INGEST panel.
    """
    with log_context("ingest"):
        phase_start("ingest", total_steps=5)
        _t_phase = time.perf_counter()

        logger.info("Processing: %s", pdf_path)

        # ── Step 1: Hash ────────────────────────────────────────────
        step_start("ingest", "hash", "Compute file hash")
        _t = time.perf_counter()
        file_hash = calculate_file_hash(pdf_path)
        step_done("ingest", "hash", time.perf_counter() - _t)
        logger.info("File hash (MD5): %s", file_hash)

        if is_file_processed(neo4j.driver, file_hash):
            logger.warning("File hash %s already processed. Skipping...", file_hash[:8])
            phase_done("ingest", time.perf_counter() - _t_phase)
            return None

        # ── Step 2: Parse PDF ───────────────────────────────────────
        step_start("ingest", "parse", "Parse PDF")
        _t = time.perf_counter()
        lines = parse_pdf_to_lines(pdf_path)
        lines = clean_lines(lines)
        step_done("ingest", "parse", time.perf_counter() - _t)

        logger.info(
            "[Scanner] Starting line scan on %d lines for %s",
            len(lines), os.path.basename(pdf_path),
        )

        # ── Step 3: Line scan ───────────────────────────────────────
        step_start("ingest", "line_scan", "Scan lines")
        _t = time.perf_counter()
        try:
            line_suspicious = scan_lines_against_instructions(lines, neo4j.driver, lang="id")
            if line_suspicious:
                logger.warning(
                    "[Scanner] %d suspicious lines detected in %s",
                    len(line_suspicious), os.path.basename(pdf_path),
                )
                for item in line_suspicious[:5]:
                    logger.warning(
                        "[Scanner]   page.%s slot=%s patterns=%s",
                        item["page"], item["matched_slot"], item["matched_patterns"],
                    )
            logger.info(
                "[Scanner] Line scan done — %d/%d suspicious lines",
                len(line_suspicious), len(lines),
            )
            step_done("ingest", "line_scan", time.perf_counter() - _t)
        except Exception as e:
            logger.warning("[Scanner] Line scan failed: %s", e)
            step_error("ingest", "line_scan", str(e))

        # ── Step 4: Build & scan chunks ─────────────────────────────
        step_start("ingest", "chunk_scan", "Build & scan chunks")
        _t = time.perf_counter()

        jurnal_id = str(uuid.uuid4())
        jurnal = JurnalNode(
            id=jurnal_id,
            judul=jurnal_metadata.get("judul", "Unknown"),
            doi=jurnal_metadata.get("doi"),
            penulis=jurnal_metadata.get("penulis", "Unknown"),
            tanggal_rilis=str(jurnal_metadata.get("tanggal_rilis", "2024")),
            source_file=pdf_path,
            file_hash=file_hash,
        )

        isi_nodes = build_isi_nodes(lines, jurnal.id)

        # Chunk-level scan → mark as quarantined
        try:
            for node in isi_nodes:
                scan_result = scan_chunk_against_instructions(
                    node.konten_chunk, neo4j.driver, lang="id"
                )
                if scan_result["is_suspicious"]:
                    logger.warning(
                        "[Scanner] Chunk suspicious — sub_judul=%r slot=%s risk=%.2f patterns=%s",
                        node.sub_judul,
                        scan_result["matched_slot"],
                        scan_result["risk_score"],
                        scan_result["matched_patterns"],
                    )
                    node.quarantined  = True
                    node.scan_score   = scan_result["risk_score"]
                    node.scan_matched = scan_result["matched_patterns"]
                else:
                    node.quarantined  = False
                    node.scan_score   = 0.0
                    node.scan_matched = []
        except Exception as e:
            logger.warning("[Scanner] Chunk scan failed — treating all chunks as safe: %s", e)
            for node in isi_nodes:
                node.quarantined  = False
                node.scan_score   = 0.0
                node.scan_matched = []

        _total_chunks       = len(isi_nodes)
        _quarantined_chunks = sum(1 for n in isi_nodes if n.quarantined)
        logger.info(
            "[Scanner] Chunk scan done — %d/%d chunks quarantined",
            _quarantined_chunks, _total_chunks,
        )

        safe_nodes = [n for n in isi_nodes if not n.quarantined]
        if len(safe_nodes) < len(isi_nodes):
            logger.warning(
                "[Scanner] %d of %d chunks quarantined — not ingested into ChromaDB.",
                len(isi_nodes) - len(safe_nodes), len(isi_nodes),
            )

        step_done("ingest", "chunk_scan", time.perf_counter() - _t)

        # ── Step 5: Write to DB ─────────────────────────────────────
        step_start("ingest", "db_write", "Write to Neo4j & ChromaDB")
        _t = time.perf_counter()
        neo4j.ingest_jurnal(jurnal)
        neo4j.ingest_isi_nodes(isi_nodes)
        chroma.ingest_isi_nodes(safe_nodes, rag_models, judul_jurnal=jurnal.judul)
        step_done("ingest", "db_write", time.perf_counter() - _t)

        phase_done("ingest", time.perf_counter() - _t_phase)

        return {
            "jurnal":    jurnal,
            "isi_nodes": isi_nodes,
            "stats": {
                "total_isi_nodes":       len(isi_nodes),
                "safe_isi_nodes":        len(safe_nodes),
                "quarantined_isi_nodes": len(isi_nodes) - len(safe_nodes),
            },
        }


# =============================================================================
# CHROMADB INGESTOR — RAW (no heading detection, no Neo4j)
# =============================================================================

def is_file_processed_raw(chroma_client, file_hash: str) -> bool:
    """Check whether a file with the given hash already exists in the raw collection."""
    try:
        col = chroma_client.get_or_create_collection(RAW_COLLECTION)
        results = col.get(where={"file_hash": file_hash}, limit=1, include=[])
        return len(results["ids"]) > 0
    except Exception:
        return False


class ChromaIngestorRaw:
    """
    ChromaDB ingestion for RAW mode (no Neo4j, no heading detection).
    Collection : konten_isi_raw
    Embedding  : raw chunk text (no title/sub_heading prefix)
    Metadata   : {file_hash, jurnal_id, chunk_index, source_file}

    P1: added `client` parameter + use upsert() instead of add().
    """

    def __init__(
        self,
        persist_directory: str = CHROMA_PATH,
        chroma_client: Optional[chromadb.ClientAPI] = None,
    ):
        if chroma_client is not None:
            self.client = chroma_client
        else:
            self.client = chromadb.PersistentClient(path=persist_directory)
        self.collection = self.client.get_or_create_collection(
            name=RAW_COLLECTION,
            metadata={"description": "Raw embeddings — flat chunks without heading/Neo4j"},
        )

    def ingest_chunks(
        self,
        chunks: List[str],
        file_hash: str,
        jurnal_id: str,
        source_file: str,
        rag_models: RAGModels,
    ):
        if not chunks:
            return

        ids = [
            str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{jurnal_id}:raw:{i}"))
            for i in range(len(chunks))
        ]
        embeddings = rag_models.embed_batch_safe(chunks)
        metadatas = [
            {
                "file_hash":   file_hash,
                "jurnal_id":   jurnal_id,
                "chunk_index": i,
                "source_file": source_file,
            }
            for i in range(len(chunks))
        ]

        # P1: upsert, not add — safe to re-run
        self.collection.upsert(
            ids=ids,
            documents=chunks,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        logger.info(
            "ChromaDB (raw): %d chunks ingested into '%s' (upsert)",
            len(chunks), RAW_COLLECTION,
        )


def run_pipeline_raw(
    pdf_path: str,
    jurnal_metadata: Dict,
    rag_models: RAGModels,
    chroma_raw: ChromaIngestorRaw,
) -> Optional[Dict]:
    """
    RAW pipeline — all logs go to the INGEST panel.
    """
    with log_context("ingest"):
        phase_start("ingest", total_steps=4)
        _t_phase = time.perf_counter()

        logger.info("[RAW] Processing: %s", pdf_path)

        # ── Step 1: Hash ────────────────────────────────────────────
        step_start("ingest", "hash", "Compute file hash")
        _t = time.perf_counter()
        file_hash = calculate_file_hash(pdf_path)
        step_done("ingest", "hash", time.perf_counter() - _t)

        if is_file_processed_raw(chroma_raw.client, file_hash):
            logger.warning("[RAW] File hash %s already processed. Skipping...", file_hash[:8])
            phase_done("ingest", time.perf_counter() - _t_phase)
            return None

        # ── Step 2: Parse PDF ───────────────────────────────────────
        step_start("ingest", "parse", "Parse PDF")
        _t = time.perf_counter()
        lines = parse_pdf_to_lines(pdf_path)
        lines = clean_lines(lines)
        step_done("ingest", "parse", time.perf_counter() - _t)

        if not lines:
            logger.warning("[RAW] No content after cleaning — pipeline halted.")
            phase_done("ingest", time.perf_counter() - _t_phase)
            return None

        full_text = " ".join(l["text"] for l in lines)
        chunks    = split_text_word_safe(full_text, MAX_TOKENS_PER_CHUNK)

        logger.info(
            "[Scanner RAW] Starting chunk scan on %d chunks for %s",
            len(chunks), os.path.basename(pdf_path),
        )

        # ── Step 3: Chunk scan ──────────────────────────────────────
        step_start("ingest", "chunk_scan", "Scan chunks")
        _t = time.perf_counter()

        safe_chunks: List[str] = []
        try:
            _raw_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            try:
                _raw_driver.verify_connectivity()
                suspicious = scan_chunks_against_instructions(chunks, _raw_driver, lang="id")
                suspicious_idx = {s["chunk_index"] for s in suspicious}

                for idx, chunk in enumerate(chunks):
                    if idx in suspicious_idx:
                        match = next(
                            (s for s in suspicious if s["chunk_index"] == idx), None
                        )
                        logger.warning(
                            "[Scanner RAW] Chunk #%d suspicious — discarded (slot=%s risk=%.2f patterns=%s)",
                            idx,
                            match["matched_slot"] if match else "?",
                            match["risk_score"] if match else 0.0,
                            match["matched_patterns"] if match else [],
                        )
                    else:
                        safe_chunks.append(chunk)
            finally:
                _raw_driver.close()
        except Exception as e:
            logger.warning(
                "[Scanner RAW] Scan failed — using all chunks without filtering: %s", e
            )
            safe_chunks = list(chunks)

        logger.info(
            "[Scanner RAW] Scan done — %d/%d chunks safe",
            len(safe_chunks), len(chunks),
        )

        step_done("ingest", "chunk_scan", time.perf_counter() - _t)

        if not safe_chunks:
            logger.warning(
                "[RAW] All chunks quarantined (%d/%d) — nothing to ingest.",
                len(chunks), len(chunks),
            )
            phase_done("ingest", time.perf_counter() - _t_phase)
            return None

        # ── Step 4: Write to ChromaDB ───────────────────────────────
        step_start("ingest", "db_write", "Write to ChromaDB")
        _t = time.perf_counter()

        jurnal_id = str(uuid.uuid4())
        chroma_raw.ingest_chunks(
            chunks=safe_chunks,
            file_hash=file_hash,
            jurnal_id=jurnal_id,
            source_file=pdf_path,
            rag_models=rag_models,
        )
        step_done("ingest", "db_write", time.perf_counter() - _t)

        phase_done("ingest", time.perf_counter() - _t_phase)

        return {
            "jurnal_id":   jurnal_id,
            "source_file": pdf_path,
            "stats": {
                "total_lines":        len(lines),
                "total_chunks":       len(chunks),
                "safe_chunks":        len(safe_chunks),
                "quarantined_chunks": len(chunks) - len(safe_chunks),
            },
        }


# =============================================================================
# ENTRY POINT FOR BACKEND SERVICE (called from service_chats.py)
# =============================================================================
#
# P1: ChromaIngestor now uses the shared client from pipeline.chroma.
# P2: RAGModels is obtained via the pipeline singleton (already loaded).
#     For standalone CLI mode, we use RAGModels(embedding_only=True).
# =============================================================================

def run_pipeline_with_shared_resources(
    pdf_path: str,
    jurnal_metadata: Dict,
) -> Optional[Dict]:
    """
    IMPROVED mode (Neo4j + ChromaDB). Borrows RAGModels & Chroma client
    from the running RAGPipeline singleton.
    """
    with log_context("ingest"):
        from pipeline import get_rag_pipeline

        pipeline    = get_rag_pipeline()
        rag_models  = pipeline.models

        # P1: use the pipeline's Chroma client (shared, not a new one)
        neo4j  = Neo4jIngestor(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD)
        chroma = ChromaIngestor(client=pipeline.chroma.client)
        try:
            return run_pipeline(pdf_path, jurnal_metadata, rag_models, neo4j, chroma)
        finally:
            neo4j.close()
            # Do NOT close chroma.client — it belongs to the pipeline!


def run_pipeline_with_shared_resources_raw(
    pdf_path: str,
    jurnal_metadata: Dict,
) -> Optional[Dict]:
    """
    RAW mode (ChromaDB only). Uses the Chroma client from the singleton.
    """
    with log_context("ingest"):
        from pipeline import get_rag_pipeline

        pipeline   = get_rag_pipeline()
        rag_models = pipeline.models
        chroma_raw = ChromaIngestorRaw(chroma_client=pipeline.chroma.client)
        return run_pipeline_raw(pdf_path, jurnal_metadata, rag_models, chroma_raw)


# =============================================================================
# STANDALONE LOGGING SETUP (only for `python embedder.py`)
# =============================================================================

def _setup_cli_logging():
    """
    Logging setup specific to CLI standalone mode.
    When imported from main.py, this function is NOT called —
    embedder inherits main.py's logging configuration.

    P1: sys.stdout.reconfigure() and HF_TOKEN info moved here.
    """
    # ── UTF-8 safety (standalone CLI only) ───────────────────────────────
    _reconfigure_stdout_utf8()

    root = logging.getLogger()
    # Don't override if handlers already exist (e.g. called from main.py)
    if any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
           for h in root.handlers):
        return

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)-14s  %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    # Silence noisy libraries in standalone mode
    for noisy in (
        "chromadb", "chromadb.telemetry",
        "neo4j", "neo4j.pool",
        "urllib3", "urllib3.connectionpool",
        "httpx", "httpcore",
        "transformers", "sentence_transformers",
        "huggingface_hub", "torch",
        "PIL", "google.generativeai", "groq",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # HF_TOKEN info — moved here from module level
    if HF_TOKEN:
        logger.info("🔑 HF_TOKEN detected and configured for Hugging Face access.")
    else:
        logger.info("ℹ️ HF_TOKEN not found in environment or .env. Proceeding without token.")


# =============================================================================
# MAIN
# =============================================================================

def main():
    global MAX_TOKENS_PER_CHUNK
    import argparse

    parser = argparse.ArgumentParser(description="Disease Journal PDF Embedder")
    parser.add_argument("--dataset",         default=DATASET_PATH)
    parser.add_argument("--chroma",          default=CHROMA_PATH)
    parser.add_argument("--neo4j-uri",       default=NEO4J_URI)
    parser.add_argument("--neo4j-user",      default=NEO4J_USER)
    parser.add_argument("--neo4j-password",  default=NEO4J_PASSWORD)
    parser.add_argument("--file",            help="Process a single PDF")
    parser.add_argument("--max-tokens",      type=int, default=MAX_TOKENS_PER_CHUNK)
    args = parser.parse_args()

    MAX_TOKENS_PER_CHUNK = args.max_tokens

    logger.info("Dataset path : %s", args.dataset)
    logger.info("ChromaDB path: %s", args.chroma)
    logger.info("Neo4j URI    : %s", args.neo4j_uri)

    # P2: use embedding_only=True — no reranker/Groq/NLP needed.
    #     This avoids EnvironmentError when GROQ_API_KEY is not set.
    logger.info("Initializing RAGModels (embedding-only)...")
    rag_models = RAGModels(embedding_only=True)

    logger.info("Initializing Neo4j...")
    neo4j = Neo4jIngestor(
        uri=args.neo4j_uri,
        user=args.neo4j_user,
        password=args.neo4j_password,
    )
    neo4j.create_constraints()

    logger.info("Initializing ChromaDB...")
    # P1: standalone mode may keep its own client
    #     (the server is not running in CLI mode).
    chroma = ChromaIngestor(persist_directory=args.chroma)

    if args.file:
        pdf_files = [Path(args.file)]
    else:
        dataset_path = Path(args.dataset)
        if not dataset_path.exists():
            logger.error("Dataset path not found: %s", dataset_path)
            neo4j.close()
            return
        pdf_files = list(dataset_path.glob("**/*.pdf"))

    if not pdf_files:
        logger.warning("No PDF files found!")
        neo4j.close()
        return

    logger.info("Found %d PDF file(s) to process", len(pdf_files))

    all_results   = []
    skipped_files = 0

    for pdf_file in pdf_files:
        try:
            jurnal_metadata = {
                "judul":         pdf_file.stem,
                "doi":           None,
                "penulis":       "Unknown Author",
                "tanggal_rilis": "2024",
            }
            result = run_pipeline(str(pdf_file), jurnal_metadata, rag_models, neo4j, chroma)
            if result:
                all_results.append(result)
            else:
                skipped_files += 1
        except Exception as e:
            logger.exception("Error processing %s", pdf_file)

    neo4j.close()

    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info("=" * 60)
    logger.info("Processed : %d file(s)", len(all_results))
    logger.info("Skipped   : %d file(s)", skipped_files)

    # P1/P3: invalidate instruction cache after ingest.
    #        Since CLI standalone, this only clears the local cache
    #        (which may be unused). If the server is running, its cache
    #        is not refreshed from here (separate process). For that,
    #        the server should have an admin endpoint.
    try:
        invalidate_instruction_cache()
        invalidate_instruction_cache_local()
    except Exception:
        pass


if __name__ == "__main__":
    # Setup logging ONLY in standalone mode (not when imported from main.py)
    _setup_cli_logging()
    main()