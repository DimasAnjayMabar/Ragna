import hashlib
import logging
import re
import os
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

# Force UTF-8 encoding
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Resolve root project directory
PARENT_DIR = Path(__file__).resolve().parent.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

from config import CONFIG
from neo4j import GraphDatabase

if load_dotenv:
    root_env_path = PARENT_DIR / ".env"
    if root_env_path.exists():
        load_dotenv(dotenv_path=root_env_path)
    else:
        load_dotenv()

logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

SLOT_ORDER = [
    "bot_name",
    "bot_identity",
    "social_guard_rail",
    "knowledge_guard_rail",
    "memory_block_guard_rail",
    "memory_summary_block",
]

SLOT_RISK_LEVEL = {
    "bot_name":                "high",
    "bot_identity":            "high",
    "social_guard_rail":       "high",
    "knowledge_guard_rail":    "high",
    "memory_block_guard_rail": "medium",
    "memory_summary_block":    "low",
}

# Fallback pattern — hanya dipakai jika guard_rails.md tidak punya entry
# untuk slot tertentu. Ini safety net, bukan default utama.
FALLBACK_FORBIDDEN_PATTERNS: Dict[str, List[str]] = {
    "bot_name":                [r"ignore\s+previous", r"abaikan\s+instruksi"],
    "bot_identity":            [r"jailbreak", r"DAN\s+mode"],
    "social_guard_rail":       [r"system\s*:", r"jailbreak"],
    "knowledge_guard_rail":    [r"system\s*:", r"reveal\s+your\s+prompt"],
    "memory_block_guard_rail": [r"ignore\s+memory", r"forget\s+everything"],
    "memory_summary_block":    [r"system\s*:"],
}

DEFAULT_INSTRUCTION_FILE = "instruction_set.md"
DEFAULT_GUARD_RAILS_FILE = "guard_rails.md"
DEFAULT_VERSION = "v1"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class InstructionNode:
    id: str
    lang: str
    slot: str
    text: str
    risk_level: str
    forbidden_patterns: List[str]
    version: str
    content_hash: str = ""
    created_at: int = 0


# =============================================================================
# UTILITIES
# =============================================================================

def instruction_id(lang: str, slot: str, version: str = DEFAULT_VERSION) -> str:
    """UUID v5 deterministik dari (lang, slot, version)."""
    base = f"instruction:{lang}:{slot}:{version}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, base))


def compute_content_hash(
    text: str,
    forbidden_patterns: List[str],
    risk_level: str,
) -> str:
    """
    Hash gabungan: text + forbidden_patterns + risk_level.
    Berubah jika salah satu berubah → trigger update di Neo4j.
    """
    patterns_str = "|".join(sorted(forbidden_patterns))
    payload = f"{text}\x1f{patterns_str}\x1f{risk_level}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_risk(slot: str) -> str:
    return SLOT_RISK_LEVEL.get(slot, "medium")


def get_fallback_patterns(slot: str) -> List[str]:
    return FALLBACK_FORBIDDEN_PATTERNS.get(slot, [])


# =============================================================================
# PARSER: instruction_set.md
# =============================================================================

def parse_instruction_file(file_path: str) -> Dict[str, Dict[str, str]]:
    """
    Baca instruction_set.md.
    Format: ## LANG: <lang>  →  ### SLOT: <slot>  →  <text>
    Return: {lang: {slot: text}}
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Instruction file tidak ditemukan: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    parsed: Dict[str, Dict[str, str]] = {}
    current_lang: Optional[str] = None
    current_slot: Optional[str] = None
    buffer: List[str] = []

    def flush_slot():
        nonlocal buffer, current_slot, current_lang
        if current_lang and current_slot:
            text = "\n".join(buffer).strip()
            if text:
                parsed.setdefault(current_lang, {})[current_slot] = text
        buffer = []

    for raw_line in content.splitlines():
        line = raw_line.rstrip()

        lang_match = re.match(r"^##\s*LANG\s*:\s*(\S+)\s*$", line, re.IGNORECASE)
        if lang_match:
            flush_slot()
            current_lang = lang_match.group(1).strip().lower()
            current_slot = None
            continue

        slot_match = re.match(r"^###\s*SLOT\s*:\s*(\S+)\s*$", line, re.IGNORECASE)
        if slot_match:
            flush_slot()
            current_slot = slot_match.group(1).strip().lower()
            continue

        if line.strip() == "---":
            flush_slot()
            current_slot = None
            continue

        if current_slot is not None:
            buffer.append(line)

    flush_slot()
    return parsed


# =============================================================================
# PARSER: guard_rails.md
# =============================================================================

def parse_guard_rails_file(file_path: str) -> Dict[str, Dict[str, List[str]]]:
    """
    Baca guard_rails.md.
    Format:
        ## LANG: <lang>
        ### SLOT: <slot>
        - <regex pattern 1>
        - <regex pattern 2>
        ...
    Return: {lang: {slot: [pattern, ...]}}

    Catatan:
      - Pattern yang diawali '-' akan di-trim spasi dan diambil sebagai satu pattern.
      - Baris kosong di-skip.
      - Baris yang tidak diawali '-' di dalam slot juga di-skip (mis. komentar).
      - Multiple "### SLOT" dalam satu bahasa akan di-merge (append).
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"Guard rails file tidak ditemukan: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    parsed: Dict[str, Dict[str, List[str]]] = {}
    current_lang: Optional[str] = None
    current_slot: Optional[str] = None

    for raw_line in content.splitlines():
        line = raw_line.rstrip()

        # Skip empty lines
        if not line.strip():
            continue

        # Skip komentar (baris diawali #, tapi bukan ## atau ###)
        stripped = line.strip()
        if stripped.startswith("#") and not stripped.startswith("##"):
            continue

        # Deteksi LANG
        lang_match = re.match(r"^##\s*LANG\s*:\s*(\S+)\s*$", line, re.IGNORECASE)
        if lang_match:
            current_lang = lang_match.group(1).strip().lower()
            current_slot = None
            continue

        # Deteksi SLOT
        slot_match = re.match(r"^###\s*SLOT\s*:\s*(\S+)\s*$", line, re.IGNORECASE)
        if slot_match:
            current_slot = slot_match.group(1).strip().lower()
            continue

        # Pattern (diawali "- ")
        if stripped.startswith("-"):
            pattern = stripped[1:].strip()
            if not pattern:
                continue
            if current_lang and current_slot:
                parsed.setdefault(current_lang, {}).setdefault(current_slot, []).append(pattern)
            continue

        # Baris lain di dalam slot — bisa jadi bagian komentar atau multiline
        # pattern. Kita abaikan (hanya pattern dengan prefix "- " yang valid).
        # Jika kamu ingin support multiline pattern, modifikasi di sini.

    return parsed


# =============================================================================
# NEO4J INGESTOR — dengan content hash check
# =============================================================================

class InstructionIngestor:
    """
    Ingest node Instruction ke Neo4j.

    Sumber data:
      - text               ← instruction_set.md
      - forbidden_patterns ← guard_rails.md (fallback: FALLBACK_FORBIDDEN_PATTERNS)
      - risk_level         ← SLOT_RISK_LEVEL (dari kode, per slot)

    Idempotency:
      - MERGE by deterministic id → tidak buat node duplikat
      - Compare content_hash → skip jika konten tidak berubah
      - Log ringkasan: created / updated / skipped
    """

    def __init__(self, uri: str = None, user: str = None, password: str = None):
        self.uri = uri or CONFIG.get("neo4j_uri", "neo4j://127.0.0.1:7687")
        self.user = user or CONFIG.get("neo4j_user", "neo4j")
        self.password = password or CONFIG.get("neo4j_password", "password")
        self.driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        self.driver.verify_connectivity()
        print(f"Neo4j connected: {self.uri}")

    def close(self):
        self.driver.close()

    def create_constraints(self):
        with self.driver.session() as session:
            session.run(
                "CREATE CONSTRAINT IF NOT EXISTS "
                "FOR (i:Instruction) REQUIRE i.id IS UNIQUE"
            )
        print("Neo4j constraint untuk :Instruction ensured.")

    def fetch_existing_hashes(self) -> Dict[str, str]:
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (i:Instruction)
                RETURN i.id AS id,
                       COALESCE(i.content_hash, '') AS hash
                """
            ).data()
        return {r["id"]: r["hash"] for r in result}

    def ingest_nodes(
        self,
        parsed_instructions: Dict[str, Dict[str, str]],
        parsed_guard_rails: Dict[str, Dict[str, List[str]]],
        version: str = DEFAULT_VERSION,
    ) -> Dict[str, int]:
        """
        Upsert node Instruction.

        Untuk setiap (lang, slot) di parsed_instructions:
          - text    = parsed_instructions[lang][slot]
          - patterns = parsed_guard_rails.get(lang, {}).get(slot) 
                       atau fallback jika tidak ada di guard_rails.md
          - risk     = SLOT_RISK_LEVEL[slot]
        """
        existing_hashes = self.fetch_existing_hashes()

        created = 0
        updated = 0
        skipped = 0

        with self.driver.session() as session:
            for lang, slots in parsed_instructions.items():
                for slot, text in slots.items():
                    node_id = instruction_id(lang, slot, version)
                    risk_level = classify_risk(slot)

                    # Ambil pattern dari guard_rails.md — fallback ke hardcoded
                    patterns = parsed_guard_rails.get(lang, {}).get(slot)
                    if patterns is None:
                        # Guard rail tidak didefinisikan untuk (lang, slot) ini
                        logger.warning(
                            "[GuardRails] Pattern tidak ditemukan untuk lang=%s slot=%s, "
                            "gunakan fallback.",
                            lang, slot,
                        )
                        patterns = get_fallback_patterns(slot)

                    new_hash = compute_content_hash(text, patterns, risk_level)
                    old_hash = existing_hashes.get(node_id)

                    # Node baru → CREATE
                    if old_hash is None:
                        session.run(
                            """
                            CREATE (i:Instruction {
                                id:                 $id,
                                lang:               $lang,
                                slot:               $slot,
                                text:               $text,
                                risk_level:         $risk_level,
                                forbidden_patterns: $patterns,
                                content_hash:       $hash,
                                created_at:         timestamp(),
                                version:            $version
                            })
                            """,
                            id=node_id,
                            lang=lang,
                            slot=slot,
                            text=text,
                            risk_level=risk_level,
                            patterns=patterns,
                            hash=new_hash,
                            version=version,
                        )
                        created += 1

                    # Hash sama → SKIP
                    elif old_hash == new_hash:
                        skipped += 1

                    # Hash beda → UPDATE
                    else:
                        session.run(
                            """
                            MATCH (i:Instruction {id: $id})
                            SET i.text               = $text,
                                i.risk_level         = $risk_level,
                                i.forbidden_patterns = $patterns,
                                i.content_hash       = $hash,
                                i.updated_at         = timestamp(),
                                i.version            = $version
                            """,
                            id=node_id,
                            text=text,
                            risk_level=risk_level,
                            patterns=patterns,
                            hash=new_hash,
                            version=version,
                        )
                        updated += 1

        print(
            f"  ✓ Neo4j nodes: created={created}  updated={updated}  skipped={skipped}"
        )
        return {"created": created, "updated": updated, "skipped": skipped}

    def ingest_next_edges(
        self,
        parsed: Dict[str, Dict[str, str]],
        version: str = DEFAULT_VERSION,
    ) -> int:
        count = 0
        with self.driver.session() as session:
            for lang, slots in parsed.items():
                available = [s for s in SLOT_ORDER if s in slots]
                for i in range(len(available) - 1):
                    a_slot = available[i]
                    b_slot = available[i + 1]
                    session.run(
                        """
                        MATCH (a:Instruction {id: $a_id})
                        MATCH (b:Instruction {id: $b_id})
                        MERGE (a)-[:NEXT]->(b)
                        """,
                        a_id=instruction_id(lang, a_slot, version),
                        b_id=instruction_id(lang, b_slot, version),
                    )
                    count += 1
        print(f"  ✓ Neo4j edges :NEXT: {count} MERGE")
        return count

    def ingest_translation_edges(
        self,
        parsed: Dict[str, Dict[str, str]],
        version: str = DEFAULT_VERSION,
    ) -> int:
        count = 0
        langs = list(parsed.keys())
        if len(langs) < 2:
            print("  ℹ Hanya satu bahasa — skip edge :TRANSLATION_OF")
            return 0

        with self.driver.session() as session:
            for slot in SLOT_ORDER:
                langs_with_slot = [l for l in langs if slot in parsed.get(l, {})]
                if len(langs_with_slot) < 2:
                    continue
                base_lang = langs_with_slot[0]
                for other_lang in langs_with_slot[1:]:
                    session.run(
                        """
                        MATCH (a:Instruction {id: $a_id})
                        MATCH (b:Instruction {id: $b_id})
                        MERGE (a)-[:TRANSLATION_OF]->(b)
                        """,
                        a_id=instruction_id(base_lang, slot, version),
                        b_id=instruction_id(other_lang, slot, version),
                    )
                    count += 1
        print(f"  ✓ Neo4j edges :TRANSLATION_OF: {count} MERGE")
        return count

    def ingest_all(
        self,
        parsed_instructions: Dict[str, Dict[str, str]],
        parsed_guard_rails: Dict[str, Dict[str, List[str]]],
        version: str = DEFAULT_VERSION,
    ) -> Dict[str, int]:
        print("─" * 60)
        print(f"Ingesting instruction set — {len(parsed_instructions)} bahasa terdeteksi")
        for lang, slots in parsed_instructions.items():
            print(f"  • {lang}: {len(slots)} slot → {sorted(slots.keys())}")

        # Cek konsistensi guard_rails
        print()
        print("  Guard rails coverage:")
        for lang, slots in parsed_instructions.items():
            gr_slots = parsed_guard_rails.get(lang, {})
            for slot in slots:
                count = len(gr_slots.get(slot, []))
                status = "✓" if count > 0 else "⚠ fallback"
                print(f"    {lang}/{slot:<28} : {count:>3} patterns {status}")
        print("─" * 60)

        self.create_constraints()
        node_stats = self.ingest_nodes(parsed_instructions, parsed_guard_rails, version)
        n_next = self.ingest_next_edges(parsed_instructions, version)
        n_trans = self.ingest_translation_edges(parsed_instructions, version)

        print()
        print("─" * 60)
        print("RINGKASAN:")
        print(f"  Nodes  created : {node_stats['created']}")
        print(f"  Nodes  updated : {node_stats['updated']}")
        print(f"  Nodes  skipped : {node_stats['skipped']}  (konten tidak berubah)")
        print(f"  Edges  :NEXT        : {n_next}")
        print(f"  Edges  :TRANSLATION : {n_trans}")
        print("─" * 60)

        if node_stats["created"] == 0 and node_stats["updated"] == 0 and node_stats["skipped"] > 0:
            print("ℹ  Tidak ada perubahan — semua node sudah up-to-date.")

        return {
            "nodes_created": node_stats["created"],
            "nodes_updated": node_stats["updated"],
            "nodes_skipped": node_stats["skipped"],
            "next_edges": n_next,
            "translation_edges": n_trans,
        }


# =============================================================================
# QUERY HELPER
# =============================================================================

def fetch_instructions_for_lang(driver, lang: str) -> List[Dict]:
    with driver.session() as session:
        result = session.run(
            """
            MATCH (i:Instruction {lang: $lang})
            RETURN i.slot               AS slot,
                   i.forbidden_patterns AS patterns,
                   i.risk_level         AS risk_level
            """,
            lang=lang,
        ).data()
    return [
        {
            "slot": r["slot"],
            "patterns": r["patterns"] or [],
            "risk_level": r["risk_level"] or "medium",
        }
        for r in result
    ]


def fetch_all_instructions(driver) -> Dict[str, List[Dict]]:
    with driver.session() as session:
        result = session.run(
            """
            MATCH (i:Instruction)
            RETURN i.lang               AS lang,
                   i.slot               AS slot,
                   i.forbidden_patterns AS patterns,
                   i.risk_level         AS risk_level
            ORDER BY i.lang, i.slot
            """
        ).data()

    grouped: Dict[str, List[Dict]] = {}
    for r in result:
        grouped.setdefault(r["lang"], []).append({
            "slot": r["slot"],
            "patterns": r["patterns"] or [],
            "risk_level": r["risk_level"] or "medium",
        })
    return grouped


def fetch_instruction_slots(driver, lang: str) -> Dict[str, str]:
    with driver.session() as session:
        result = session.run(
            """
            MATCH (i:Instruction {lang: $lang})
            RETURN i.slot AS slot, i.text AS text
            """,
            lang=lang,
        ).data()
    return {r["slot"]: r["text"] for r in result}


# =============================================================================
# CLI
# =============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Instruction Set + Guard Rails Embedder"
    )
    parser.add_argument(
        "--instruction-file",
        default=DEFAULT_INSTRUCTION_FILE,
        help="Path ke instruction_set.md",
    )
    parser.add_argument(
        "--guard-rails-file",
        default=DEFAULT_GUARD_RAILS_FILE,
        help="Path ke guard_rails.md",
    )
    parser.add_argument(
        "--version",
        default=DEFAULT_VERSION,
        help="Versi instruction set (default: v1)",
    )
    parser.add_argument("--neo4j-uri", default=None)
    parser.add_argument("--neo4j-user", default=None)
    parser.add_argument("--neo4j-password", default=None)
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Paksa update semua node (abaikan content hash check)",
    )
    args = parser.parse_args()

    # Resolve path
    instr_path = args.instruction_file
    if not os.path.isabs(instr_path):
        instr_path = str((Path(__file__).resolve().parent / instr_path).resolve())

    gr_path = args.guard_rails_file
    if not os.path.isabs(gr_path):
        gr_path = str((Path(__file__).resolve().parent / gr_path).resolve())

    print("=" * 60)
    print("Instruction Set + Guard Rails Embedder")
    print("=" * 60)
    print(f"Instruction file : {instr_path}")
    print(f"Guard rails file : {gr_path}")
    print(f"Version          : {args.version}")

    # ── Parse instruction_set.md ─────────────────────────────────────────
    try:
        parsed_instructions = parse_instruction_file(instr_path)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Gagal parse instruction file: {e}")
        sys.exit(1)

    if not parsed_instructions:
        print("⚠️  Tidak ada instruction yang ter-parse. Cek format file.")
        sys.exit(1)

    # ── Parse guard_rails.md ─────────────────────────────────────────────
    try:
        parsed_guard_rails = parse_guard_rails_file(gr_path)
        print(f"\n✓ Guard rails di-parse: {len(parsed_guard_rails)} bahasa")
        for lang, slots in parsed_guard_rails.items():
            total = sum(len(p) for p in slots.values())
            print(f"  • {lang}: {len(slots)} slot, {total} pattern total")
    except FileNotFoundError:
        print(f"\n⚠️  Guard rails file tidak ditemukan: {gr_path}")
        print(f"   Lanjut dengan FALLBACK pattern untuk semua slot.")
        parsed_guard_rails = {}
    except Exception as e:
        print(f"\n❌ Gagal parse guard rails: {e}")
        sys.exit(1)

    # ── Preview ─────────────────────────────────────────────────────────
    print(f"\n📖 Hasil parse instruction:")
    for lang, slots in parsed_instructions.items():
        print(f"  • {lang}: {len(slots)} slot")
        for slot in SLOT_ORDER:
            if slot in slots:
                preview = slots[slot][:60].replace("\n", " ")
                print(f"      - {slot:<28} : {preview}…")

    if args.parse_only:
        print("\n✅ Parse-only mode — selesai tanpa ingest.")
        return

    # ── Ingest ──────────────────────────────────────────────────────────
    print()
    ingestor = InstructionIngestor(
        uri=args.neo4j_uri,
        user=args.neo4j_user,
        password=args.neo4j_password,
    )
    try:
        if args.force:
            print("⚠️  --force: hapus semua content_hash → paksa update.")
            with ingestor.driver.session() as session:
                session.run("MATCH (i:Instruction) REMOVE i.content_hash")

        stats = ingestor.ingest_all(
            parsed_instructions, parsed_guard_rails, version=args.version
        )
    finally:
        ingestor.close()

    print()
    print("=" * 60)
    print("INGEST COMPLETE")
    print("=" * 60)
    print(f"Nodes created      : {stats['nodes_created']}")
    print(f"Nodes updated      : {stats['nodes_updated']}")
    print(f"Nodes skipped      : {stats['nodes_skipped']}  (konten tidak berubah)")
    print(f"Edges :NEXT        : {stats['next_edges']}")
    print(f"Edges :TRANSLATION : {stats['translation_edges']}")
    print("=" * 60)


if __name__ == "__main__":
    main()