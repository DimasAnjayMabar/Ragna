# check_status.py
import chromadb

client = chromadb.PersistentClient(path="./chroma_db")

print("=" * 50)
print("STATUS COLLECTIONS")
print("=" * 50)

for name in ["konten_isi", "konten_isi_raw", "chat_memory", "user_identity"]:
    try:
        col = client.get_collection(name)
        count = col.count()
        
        # Test query dengan dummy
        try:
            test = col.query(
                query_embeddings=[[0.0] * 384],  # Sesuaikan dimensi
                n_results=1,
                include=[]
            )
            status = "✓ OK"
        except Exception as e:
            if "Nothing found on disk" in str(e):
                status = "❌ CORRUPT"
            else:
                status = f"⚠️ Error: {str(e)[:50]}"
        
        print(f"{name:20} → {count:6} docs  {status}")
        
    except Exception as e:
        if "does not exist" in str(e):
            print(f"{name:20} → {'NOT FOUND':^10}")
        else:
            print(f"{name:20} → ERROR: {str(e)[:50]}")

print("\n" + "=" * 50)