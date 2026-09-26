import sys
import os
from pathlib import Path
import chromadb

# Ensure parent directory is added to sys.path
PARENT_DIR = Path(__file__).resolve().parent.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

from config import CONFIG


def delete_memory_collection():
    raw_chroma_path = CONFIG.get("chroma_path", "chroma_db")
    if os.path.isabs(raw_chroma_path):
        db_path = raw_chroma_path
    else:
        db_path = str((PARENT_DIR / raw_chroma_path).resolve())

    print("=" * 60)
    print("💬 CHAT MEMORY DELETION PROCESS STARTED")
    print("=" * 60)
    print(f"Target ChromaDB path: {db_path}")

    if not os.path.exists(db_path):
        print(f"ℹ️  Directory {db_path} does not exist.")
        return

    try:
        client = chromadb.PersistentClient(path=db_path)
        existing_collections = [c.name for c in client.list_collections()]
        print(f"Existing collections: {existing_collections}")

        if "chat_memory" in existing_collections:
            print("Deleting collection 'chat_memory'...")
            client.delete_collection(name="chat_memory")
            print("✅ Collection 'chat_memory' deleted successfully!")
        else:
            print("ℹ️  Collection 'chat_memory' does not exist.")

    except Exception as e:
        print(f"❌ Error deleting chat memory: {e}")


if __name__ == "__main__":
    delete_memory_collection()