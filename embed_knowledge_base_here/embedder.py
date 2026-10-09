import hashlib
import logging
import re
import os
import sys
import uuid
import threading
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass, field
import pdfplumber
import chromadb
from neo4j import GraphDatabase

# Force UTF-8 encoding for standard output/error to avoid Windows CP1252 stream crashes
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Optional/Safe dotenv import to load .env variables
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# =============================================================================
# RESOLVE ROOT PROJECT DIRECTORY (cd ..)
# =============================================================================
PARENT_DIR = Path(__file__).resolve().parent.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

# Import centralized configuration and pipeline models from root directory
from config import CONFIG
from pipeline import RAGModels

# =============================================================================
# ENVIRONMENT & HF_TOKEN SETUP (WITH FALLBACK)
# =============================================================================
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
    print("🔑 HF_TOKEN detected and configured for Hugging Face access.")
else:
    print("ℹ️ HF_TOKEN not found in environment or .env. Proceeding without token.")

logger = logging.getLogger(__name__)

# =============================================================================
# LOAD CONFIGURATION FROM CONFIG.PY
# =============================================================================

# Neo4j Settings
NEO4J_URI = CONFIG.get("neo4j_uri", "neo4j://127.0.0.1:7687")
NEO4J_USER = CONFIG.get("neo4j_user", "neo4j")
NEO4J_PASSWORD = CONFIG.get("neo4j_password", "password")

# ChromaDB Settings
raw_chroma_path = CONFIG.get("chroma_path", "chroma_db")
CHROMA_PATH = raw_chroma_path if os.path.isabs(raw_chroma_path) else str((PARENT_DIR / raw_chroma_path).resolve())
CHROMA_COLLECTION = CONFIG.get("chroma_collection", "konten_isi")
RAW_COLLECTION = CONFIG.get("raw_collection", "konten_isi_raw")

# Dataset & Ingestion Parameters
raw_dataset_path = CONFIG.get("dataset_path", "./dataset")
DATASET_PATH = raw_dataset_path if os.path.isabs(raw_dataset_path) else str((PARENT_DIR / raw_dataset_path).resolve())

MAX_TOKENS_PER_CHUNK = CONFIG.get("max_tokens_per_chunk", 512)
SUBHEADING_SCORE_THRESHOLD = CONFIG.get("subheading_score_threshold", 4)

# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class JurnalNode:
    id: str
    judul: str
    doi: Optional[str]
    penulis: str
    tanggal_rilis: str
    source_file: str
    file_hash: str

@dataclass
class IsiNode:
    id: str
    jurnal_id: str
    sub_judul: str
    konten_chunk: str
    halaman: int
    quarantined: bool = False          # BARU: hasil scanner
    scan_score: float = 0.0            # BARU: risk score scanner
    scan_matched: Optional[list] = None  # BARU: pattern yang match

# =============================================================================
# FILE HASHING (MD5) FOR DUPLICATE DETECTION
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
# PROMPT INJECTION SCANNER (BARU)
# =============================================================================
#
# Scanner ini mendeteksi pola prompt injection di dalam chunk/baris yang
# berasal dari PDF yang di-upload user. Referensi pattern diambil dari
# node :Instruction di Neo4j (lihat prompt_guard.py).
#
# Alur:
#   1. Load instruction set dari Neo4j per bahasa (cached in-memory).
#   2. Untuk setiap chunk, cek apakah ada pattern regex dari
#      forbidden_patterns yang match.
#   3. Return risk score & matched slot.
#
# Cache di-invalidate dengan invalidate_instruction_cache() jika
# instruction set di Neo4j diperbarui.
# =============================================================================

_INSTRUCTION_CACHE: Dict[str, List[Dict]] = {}
_INSTRUCTION_CACHE_LOCK = threading.Lock()
_COMPILED_PATTERN_CACHE: Dict[str, re.Pattern] = {}
_RISK_WEIGHT = {"high": 1.0, "medium": 0.7, "low": 0.4}


def _get_compiled_pattern(pattern: str) -> re.Pattern:
    """Pre-compile regex — cache agar tidak compile ulang tiap chunk."""
    if pattern not in _COMPILED_PATTERN_CACHE:
        try:
            _COMPILED_PATTERN_CACHE[pattern] = re.compile(pattern, re.IGNORECASE)
        except re.error:
            # Pattern invalid → compile pattern yang tidak pernah match
            _COMPILED_PATTERN_CACHE[pattern] = re.compile(r"(?!x)x")
    return _COMPILED_PATTERN_CACHE[pattern]


def _load_instructions_for_lang(neo4j_driver, lang: str) -> List[Dict]:
    """
    Ambil instruction set dari Neo4j untuk bahasa tertentu.
    Hasil di-cache di memory — panggil ulang dengan bahasa sama
    tidak akan query Neo4j lagi.
    """
    with _INSTRUCTION_CACHE_LOCK:
        if lang in _INSTRUCTION_CACHE:
            return _INSTRUCTION_CACHE[lang]

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
                "slot": r["slot"],
                "patterns": r["patterns"] or [],
                "risk_level": r["risk_level"] or "medium",
            }
            for r in result
        ]
    except Exception as e:
        logger.warning("[Scanner] Gagal ambil instruction lang=%s: %s", lang, e)
        instructions = []

    with _INSTRUCTION_CACHE_LOCK:
        _INSTRUCTION_CACHE[lang] = instructions
    return instructions


def invalidate_instruction_cache(lang: Optional[str] = None):
    """Paksa refresh cache instruction set. Dipanggil setelah re-ingest."""
    with _INSTRUCTION_CACHE_LOCK:
        if lang is None:
            _INSTRUCTION_CACHE.clear()
        else:
            _INSTRUCTION_CACHE.pop(lang, None)
    logger.info("[Scanner] Instruction cache di-invalidate (lang=%s)", lang or "all")


def scan_chunk_against_instructions(
    text: str,
    neo4j_driver,
    lang: str = "id",
) -> Dict:
    """
    Scan teks terhadap instruction set graph.

    Return:
        {
            "is_suspicious": bool,
            "matched_patterns": [str],
            "risk_score": float,  # 0..1
            "matched_slot": str | None,
        }

    Catatan:
        - Pattern di-pre-compile & di-cache.
        - Instruction set di-cache per bahasa.
        - Risk score: 1.0 untuk match di slot high, 0.7 medium, 0.4 low.
    """
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
        "is_suspicious": len(matched_patterns) > 0,
        "matched_patterns": matched_patterns,
        "risk_score": max_risk,
        "matched_slot": matched_slot,
    }


def scan_lines_against_instructions(
    lines: List[Dict],
    neo4j_driver,
    lang: str = "id",
) -> List[Dict]:
    """
    Scan setiap baris hasil parse PDF — mendeteksi injeksi di level baris.
    Return: list baris yang mencurigakan (untuk audit log).
    """
    suspicious = []
    for line in lines:
        text = line.get("text", "")
        if not text:
            continue
        result = scan_chunk_against_instructions(text, neo4j_driver, lang)
        if result["is_suspicious"]:
            suspicious.append({
                "page": line.get("page"),
                "text": text,
                "matched_slot": result["matched_slot"],
                "matched_patterns": result["matched_patterns"],
                "risk_score": result["risk_score"],
            })
    return suspicious


def scan_chunks_against_instructions(
    chunks: List[str],
    neo4j_driver,
    lang: str = "id",
) -> List[Dict]:
    """
    Scan list chunk (untuk Raw pipeline).
    Return: list hasil scan per chunk yang mencurigakan.
    """
    suspicious = []
    for idx, chunk in enumerate(chunks):
        result = scan_chunk_against_instructions(chunk, neo4j_driver, lang)
        if result["is_suspicious"]:
            suspicious.append({
                "chunk_index": idx,
                "matched_slot": result["matched_slot"],
                "matched_patterns": result["matched_patterns"],
                "risk_score": result["risk_score"],
            })
    return suspicious


# =============================================================================
# STEP 1 — PDF INGESTION (2-COLUMN AWARE)
# =============================================================================

def _detect_column_split(words: List[Dict], page_width: float) -> Optional[float]:
    if not words or page_width <= 0:
        return None

    bucket_count = 20
    bucket_size = page_width / bucket_count
    buckets = [0] * bucket_count

    for w in words:
        mid_x = (w['x0'] + w['x1']) / 2
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

    for word in sorted(words, key=lambda w: (w['top'], w['x0'])):
        if current_y is None:
            current_y = word['top']
            current_line = [word]
        elif abs(word['top'] - current_y) < 5:
            current_line.append(word)
        else:
            if current_line:
                lines.append(_build_line_dict(current_line, page_num, current_y, page_height))
            current_y = word['top']
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
                left_words  = [w for w in words if w['x1'] <= col_split]
                right_words = [w for w in words if w['x0'] >  col_split]

                all_lines.extend(_group_words_into_lines(left_words,  page_num, page_height))
                all_lines.extend(_group_words_into_lines(right_words, page_num, page_height))
            else:
                all_lines.extend(_group_words_into_lines(words, page_num, page_height))

    return all_lines


def _build_line_dict(words: List[Dict], page_num: int, y_pos: float, page_height: float = 0.0) -> Dict:
    line_text = ' '.join(w['text'] for w in words)
    avg_size  = sum(float(w.get('height', 10)) for w in words) / len(words)
    is_bold   = any('bold' in str(w.get('fontname', '')).lower() for w in words)
    return {
        'text':        line_text.strip(),
        'page':        page_num,
        'font_size':   avg_size,
        'is_bold':     is_bold,
        'y_position':  y_pos,
        'page_height': page_height,
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


def score_subheading(line: Dict, prev_line: Optional[Dict], dominant_font_size: float, normal_line_gap: float) -> int:
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

    if not text.endswith('.'):
        score += 1

    if is_all_caps(text):
        score += 1

    if len(text) > 60:
        score -= 2

    page_height = line.get("page_height", 0)
    if page_height > 0 and line["y_position"] > (page_height * 0.9):
        score -= 1

    return score


def is_subheading(line: Dict, prev_line: Optional[Dict], dominant_font_size: float, normal_line_gap: float) -> bool:
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
    sentences = re.split(r'([.!?]+\s+)', text)

    for i in range(0, len(sentences), 2):
        sentence = sentences[i]
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
    normal_line_gap = compute_normal_line_gap(lines)

    print(f"  [Heuristic] dominant_font_size={dominant_font_size:.1f}  "
          f"normal_line_gap={normal_line_gap:.1f}  "
          f"threshold={SUBHEADING_SCORE_THRESHOLD}")

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
                scan_matched=[],  # BARU: default list kosong
            ))
            global_chunk_counter += 1

    for i, line in enumerate(lines):
        prev_line = lines[i - 1] if i > 0 else None

        if is_subheading(line, prev_line, dominant_font_size, normal_line_gap):
            flush_buffer()
            current_heading = line["text"].strip().rstrip(':.').strip()
            current_buffer = []
            current_page_start = line["page"]
        else:
            current_buffer.append(line)

    flush_buffer()
    return isi_nodes

# =============================================================================
# NEO4J & CHROMADB INGESTORS
# =============================================================================

class Neo4jIngestor:
    def __init__(self, uri: str = NEO4J_URI, user: str = NEO4J_USER, password: str = NEO4J_PASSWORD):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        print(f"Neo4j connected: {uri}")

    def close(self):
        self.driver.close()

    def create_constraints(self):
        with self.driver.session() as session:
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (j:Jurnal) REQUIRE j.id IS UNIQUE")
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (i:Isi) REQUIRE i.id IS UNIQUE")
        print("Neo4j constraints ensured.")

    def ingest_jurnal(self, jurnal: JurnalNode):
        query = """
        MERGE (j:Jurnal {id: $id})
        SET j.judul        = $judul,
            j.doi          = $doi,
            j.penulis      = $penulis,
            j.tanggal_rilis = $tanggal_rilis,
            j.file_hash    = $file_hash,
            j.source_file  = $source_file
        """
        with self.driver.session() as session:
            session.run(query,
                        id=jurnal.id,
                        judul=jurnal.judul,
                        doi=jurnal.doi or "",
                        penulis=jurnal.penulis,
                        tanggal_rilis=jurnal.tanggal_rilis,
                        file_hash=jurnal.file_hash,
                        source_file=jurnal.source_file)
        print(f"  ✓ Neo4j: Jurnal ingested ({jurnal.id})")

    def ingest_isi_nodes(self, isi_nodes: List[IsiNode]):
        if not isi_nodes:
            return

        batch = [
            {
                "id": n.id,
                "jurnal_id": n.jurnal_id,
                "sub_judul": n.sub_judul,
                "konten_chunk": n.konten_chunk,
                "halaman": n.halaman,
                "quarantined": getattr(n, "quarantined", False),          # BARU
                "scan_score": getattr(n, "scan_score", 0.0),              # BARU
                "scan_matched": getattr(n, "scan_matched", []) or [],     # BARU
            }
            for n in isi_nodes
        ]

        with self.driver.session() as session:
            session.run("""
                UNWIND $batch AS row
                MERGE (i:Isi {id: row.id})
                SET i.sub_judul    = row.sub_judul,
                    i.konten_chunk = row.konten_chunk,
                    i.halaman      = row.halaman,
                    i.quarantined  = row.quarantined,
                    i.scan_score   = row.scan_score,
                    i.scan_matched = row.scan_matched
            """, batch=batch)

            session.run("""
                UNWIND $batch AS row
                MATCH (j:Jurnal {id: row.jurnal_id})
                MATCH (i:Isi    {id: row.id})
                MERGE (j)-[:HAS_SECTION]->(i)
            """, batch=batch)

            next_pairs = [
                {"from_id": isi_nodes[i].id, "to_id": isi_nodes[i + 1].id}
                for i in range(len(isi_nodes) - 1)
            ]
            if next_pairs:
                session.run("""
                    UNWIND $pairs AS pair
                    MATCH (a:Isi {id: pair.from_id})
                    MATCH (b:Isi {id: pair.to_id})
                    MERGE (a)-[:NEXT]->(b)
                """, pairs=next_pairs)

        print(f"  ✓ Neo4j: {len(isi_nodes)} Isi nodes ingested with HAS_SECTION & NEXT edges")


class ChromaIngestor:
    def __init__(self, persist_directory: str = CHROMA_PATH):
        self.client = chromadb.PersistentClient(path=persist_directory)
        self.collection = self.client.get_or_create_collection(
            name=CHROMA_COLLECTION,
            metadata={"description": "Embeddings konten_chunk dari Node Isi"}
        )
        print(f"ChromaDB initialized at: {persist_directory}")

    def ingest_isi_nodes(self, isi_nodes: List[IsiNode], rag_models: RAGModels, judul_jurnal: str = ""):
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

        self.collection.add(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        logger.info(
            "ChromaDB: %d konten_chunk ingested ke '%s'",
            len(isi_nodes), CHROMA_COLLECTION,
        )

# =============================================================================
# PIPELINE
# =============================================================================

def run_pipeline(pdf_path: str,
                 jurnal_metadata: Dict,
                 rag_models: RAGModels,
                 neo4j: Neo4jIngestor,
                 chroma: ChromaIngestor) -> Optional[Dict]:
    logger.info("Processing: %s", pdf_path)
    file_hash = calculate_file_hash(pdf_path)
    logger.info("File hash (MD5): %s", file_hash)

    if is_file_processed(neo4j.driver, file_hash):
        logger.warning("File hash %s already processed. Skipping...", file_hash[:8])
        return None

    lines = parse_pdf_to_lines(pdf_path)
    lines = clean_lines(lines)

    # ── BARU: Scan per baris (audit level — log saja, tidak blokir) ─────
    try:
        line_suspicious = scan_lines_against_instructions(lines, neo4j.driver, lang="id")
        if line_suspicious:
            logger.warning(
                "[Scanner] %d baris suspicious terdeteksi di %s",
                len(line_suspicious), os.path.basename(pdf_path),
            )
            for item in line_suspicious[:5]:  # limit log
                logger.warning(
                    "[Scanner]   hal.%s slot=%s patterns=%s",
                    item["page"], item["matched_slot"], item["matched_patterns"],
                )
    except Exception as e:
        logger.warning("[Scanner] Line scan gagal: %s", e)
    # ──────────────────────────────────────────────────────────────────────

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

    # ── BARU: Scan per chunk → tandai quarantined ────────────────────────
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
                node.quarantined = True
                node.scan_score = scan_result["risk_score"]
                node.scan_matched = scan_result["matched_patterns"]
            else:
                node.quarantined = False
                node.scan_score = 0.0
                node.scan_matched = []
    except Exception as e:
        logger.warning("[Scanner] Chunk scan gagal — semua chunk dianggap aman: %s", e)
        for node in isi_nodes:
            node.quarantined = False
            node.scan_score = 0.0
            node.scan_matched = []

    # Filter: hanya chunk aman yang masuk ChromaDB
    safe_nodes = [n for n in isi_nodes if not n.quarantined]
    if len(safe_nodes) < len(isi_nodes):
        logger.warning(
            "[Scanner] %d dari %d chunk di-quarantine — tidak di-ingest ke ChromaDB.",
            len(isi_nodes) - len(safe_nodes), len(isi_nodes),
        )
    # ──────────────────────────────────────────────────────────────────────

    neo4j.ingest_jurnal(jurnal)
    neo4j.ingest_isi_nodes(isi_nodes)  # tetap ingest semua, tapi dengan flag quarantined
    chroma.ingest_isi_nodes(safe_nodes, rag_models, judul_jurnal=jurnal.judul)

    return {
        "jurnal": jurnal,
        "isi_nodes": isi_nodes,
        "stats": {
            "total_isi_nodes": len(isi_nodes),
            "safe_isi_nodes": len(safe_nodes),
            "quarantined_isi_nodes": len(isi_nodes) - len(safe_nodes),
        },
    }

# =============================================================================
# CHROMADB INGESTOR — RAW (tanpa heading detection & Neo4j)
# =============================================================================

def is_file_processed_raw(chroma_client, file_hash: str) -> bool:
    """Cek apakah file dengan hash tertentu sudah ada di collection konten_isi_raw."""
    try:
        col = chroma_client.get_or_create_collection(RAW_COLLECTION)
        results = col.get(where={"file_hash": file_hash}, limit=1, include=[])
        return len(results["ids"]) > 0
    except Exception:
        return False


class ChromaIngestorRaw:
    """
    ChromaDB ingestion untuk mode RAW (tanpa Neo4j, tanpa heading detection).
    Collection : konten_isi_raw
    Embedding  : teks chunk murni (tanpa prefix judul/sub_judul)
    Metadata   : {file_hash, jurnal_id, chunk_index, source_file}
    """

    def __init__(self, persist_directory: str = CHROMA_PATH, chroma_client=None):
        if chroma_client is not None:
            self.client = chroma_client
        else:
            self.client = chromadb.PersistentClient(path=persist_directory)
        self.collection = self.client.get_or_create_collection(
            name=RAW_COLLECTION,
            metadata={"description": "Embeddings raw — flat chunks tanpa heading/Neo4j"}
        )

    def ingest_chunks(self, chunks: List[str], file_hash: str, jurnal_id: str,
                      source_file: str, rag_models: RAGModels):
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

        self.collection.add(
            ids=ids,
            documents=chunks,
            embeddings=embeddings,
            metadatas=metadatas,
        )
        logger.info("ChromaDB (raw): %d chunks ingested ke '%s'", len(chunks), RAW_COLLECTION)


def run_pipeline_raw(pdf_path: str,
                     jurnal_metadata: Dict,
                     rag_models: RAGModels,
                     chroma_raw: ChromaIngestorRaw) -> Optional[Dict]:
    """
    Pipeline RAW: PDF -> 2-column detection -> boilerplate removal
                  -> flat chunking -> scan injection -> embed -> konten_isi_raw
    Return None jika file duplikat (hash sudah ada) atau tidak ada konten.
    """
    logger.info("[RAW] Processing: %s", pdf_path)

    file_hash = calculate_file_hash(pdf_path)
    if is_file_processed_raw(chroma_raw.client, file_hash):
        logger.warning("[RAW] File hash %s sudah diproses. Melewati...", file_hash[:8])
        return None

    lines = parse_pdf_to_lines(pdf_path)
    lines = clean_lines(lines)
    if not lines:
        logger.warning("[RAW] Tidak ada konten setelah cleaning — pipeline berhenti.")
        return None

    full_text = " ".join(l["text"] for l in lines)
    chunks = split_text_word_safe(full_text, MAX_TOKENS_PER_CHUNK)

    # ── BARU: Scan setiap chunk terhadap instruction set ─────────────────
    # Raw pipeline tidak punya Neo4j ingestor — buat koneksi sementara.
    safe_chunks: List[str] = []
    try:
        from neo4j import GraphDatabase as _GD
        _raw_driver = _GD.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD)
        )
        try:
            _raw_driver.verify_connectivity()
            suspicious = scan_chunks_against_instructions(
                chunks, _raw_driver, lang="id"
            )
            suspicious_idx = {s["chunk_index"] for s in suspicious}

            for idx, chunk in enumerate(chunks):
                if idx in suspicious_idx:
                    match = next(
                        (s for s in suspicious if s["chunk_index"] == idx), None
                    )
                    logger.warning(
                        "[Scanner RAW] Chunk #%d suspicious — dibuang (slot=%s risk=%.2f patterns=%s)",
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
            "[Scanner RAW] Scan gagal — pakai semua chunk tanpa filter: %s", e
        )
        safe_chunks = list(chunks)
    # ──────────────────────────────────────────────────────────────────────

    if not safe_chunks:
        logger.warning(
            "[RAW] Semua chunk ter-quarantine (%d/%d) — tidak ada yang di-ingest.",
            len(chunks), len(chunks),
        )
        return None

    jurnal_id = str(uuid.uuid4())
    chroma_raw.ingest_chunks(
        chunks=safe_chunks,
        file_hash=file_hash,
        jurnal_id=jurnal_id,
        source_file=pdf_path,
        rag_models=rag_models,
    )

    return {
        "jurnal_id":   jurnal_id,
        "source_file": pdf_path,
        "stats": {
            "total_lines": len(lines),
            "total_chunks": len(chunks),
            "safe_chunks": len(safe_chunks),
            "quarantined_chunks": len(chunks) - len(safe_chunks),
        },
    }


# =============================================================================
# ENTRY POINT UNTUK SERVICE BACKEND (dipanggil dari service_chats.py)
# =============================================================================

def run_pipeline_with_shared_resources(pdf_path: str,
                                       jurnal_metadata: Dict) -> Optional[Dict]:
    """Mode IMPROVED (Neo4j + ChromaDB). Meminjam RAGModels dari singleton pipeline."""
    from pipeline import get_rag_pipeline

    pipeline = get_rag_pipeline()
    rag_models = pipeline.models

    neo4j = Neo4jIngestor(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD)
    chroma = ChromaIngestor(persist_directory=CHROMA_PATH)
    try:
        return run_pipeline(pdf_path, jurnal_metadata, rag_models, neo4j, chroma)
    finally:
        neo4j.close()


def run_pipeline_with_shared_resources_raw(pdf_path: str,
                                           jurnal_metadata: Dict) -> Optional[Dict]:
    """Mode RAW (ChromaDB only). Memakai client ChromaDB dari singleton pipeline."""
    from pipeline import get_rag_pipeline

    pipeline = get_rag_pipeline()
    rag_models = pipeline.models
    chroma_raw = ChromaIngestorRaw(chroma_client=pipeline.chroma.client)
    return run_pipeline_raw(pdf_path, jurnal_metadata, rag_models, chroma_raw)

# =============================================================================
# MAIN
# =============================================================================

def main():
    global MAX_TOKENS_PER_CHUNK
    import argparse

    parser = argparse.ArgumentParser(description="Disease Journal PDF Embedder")
    parser.add_argument("--dataset", default=DATASET_PATH)
    parser.add_argument("--chroma", default=CHROMA_PATH)
    parser.add_argument("--neo4j-uri", default=NEO4J_URI)
    parser.add_argument("--neo4j-user", default=NEO4J_USER)
    parser.add_argument("--neo4j-password", default=NEO4J_PASSWORD)
    parser.add_argument("--file", help="Process single PDF")
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS_PER_CHUNK)
    args = parser.parse_args()

    MAX_TOKENS_PER_CHUNK = args.max_tokens

    print(f"Dataset path : {args.dataset}")
    print(f"ChromaDB path: {args.chroma}")
    print(f"Neo4j URI    : {args.neo4j_uri}")

    print("\nInitializing RAGModels...")
    rag_models = RAGModels()

    print("Initializing Neo4j...")
    neo4j = Neo4jIngestor(
        uri=args.neo4j_uri,
        user=args.neo4j_user,
        password=args.neo4j_password,
    )
    neo4j.create_constraints()

    print("Initializing ChromaDB...")
    chroma = ChromaIngestor(persist_directory=args.chroma)

    if args.file:
        pdf_files = [Path(args.file)]
    else:
        dataset_path = Path(args.dataset)
        if not dataset_path.exists():
            print(f"Error: Dataset path not found: {dataset_path}")
            neo4j.close()
            return
        pdf_files = list(dataset_path.glob("**/*.pdf"))

    if not pdf_files:
        print("No PDF files found!")
        neo4j.close()
        return

    print(f"\nFound {len(pdf_files)} PDF file(s) to process")

    all_results = []
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
            print(f"Error processing {pdf_file}: {e}")

    neo4j.close()

    print(f"\n{'='*60}")
    print("PIPELINE COMPLETE")
    print(f"{'='*60}")
    print(f"Processed       : {len(all_results)} file(s)")
    print(f"Skipped         : {skipped_files} file(s)")

if __name__ == "__main__":
    main()