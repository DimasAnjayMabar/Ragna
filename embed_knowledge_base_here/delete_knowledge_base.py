import os
import sys
import shutil
from pathlib import Path

# Force UTF-8 output streams to prevent CP1252 UnicodeEncodeError on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Optional/Safe dotenv import to load .env variables
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Ensure root directory (..) is in sys.path to import config.py
PARENT_DIR = Path(__file__).resolve().parent.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

if load_dotenv:
    root_env_path = PARENT_DIR / ".env"
    if root_env_path.exists():
        load_dotenv(dotenv_path=root_env_path)
    else:
        load_dotenv()

from config import CONFIG
import chromadb
from neo4j import GraphDatabase

NEO4J_URI = CONFIG.get("neo4j_uri", "bolt://localhost:7687")
NEO4J_USER = CONFIG.get("neo4j_user", "neo4j")
NEO4J_PASSWORD = CONFIG.get("neo4j_password", "password")

raw_chroma_path = CONFIG.get("chroma_path", "chroma_db")
CHROMA_PATH = raw_chroma_path if os.path.isabs(raw_chroma_path) else str((PARENT_DIR / raw_chroma_path).resolve())
CHROMA_COLLECTION = CONFIG.get("chroma_collection", "konten_isi")

def delete_neo4j_data():
    print("🧹 Cleaning Neo4j Knowledge Base nodes...")
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as session:
            # Delete Jurnal and Isi nodes along with their relationships
            result = session.run("""
                MATCH (j:Jurnal)
                OPTIONAL MATCH (j)-[*0..2]-(connected)
                DETACH DELETE j, connected
            """)
            print("  ✓ Neo4j Jurnal & Isi nodes deleted.")
            
            # Detach and delete any remaining orphan Isi nodes if any exist
            session.run("MATCH (i:Isi) DETACH DELETE i")
            print("  ✓ Remaining Isi orphan nodes cleared.")
            
        driver.close()
    except Exception as e:
        print(f"  ❌ Error deleting Neo4j data: {e}")

def delete_chroma_data():
    print("🧹 Cleaning ChromaDB vector store...")
    try:
        if os.path.exists(CHROMA_PATH):
            client = chromadb.PersistentClient(path=CHROMA_PATH)
            try:
                client.delete_collection(name=CHROMA_COLLECTION)
                print(f"  ✓ Collection '{CHROMA_COLLECTION}' deleted from ChromaDB.")
            except Exception as e:
                print(f"  ℹ️ Collection notice: {e}")

            # Optionally remove persistent directory store if needed
            shutil.rmtree(CHROMA_PATH, ignore_errors=True)
            print(f"  ✓ ChromaDB directory wiped: {CHROMA_PATH}")
        else:
            print("  ℹ️ ChromaDB directory does not exist. Skipping.")
    except Exception as e:
        print(f"  ❌ Error clearing ChromaDB: {e}")

def delete_knowledge_base():
    print("🗑️  KNOWLEDGE BASE DELETION PROCESS STARTED")
    print("=" * 60)
    delete_neo4j_data()
    delete_chroma_data()
    print("=" * 60)
    print("✅ Knowledge Base successfully deleted!")

if __name__ == "__main__":
    delete_knowledge_base()