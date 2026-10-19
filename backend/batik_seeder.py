# seed_products.py
"""
Import data UMKM batik dari Excel ke tabel `umkm_batik`.
Jalankan: python seed_products.py

Menggunakan database yang sama dengan aplikasi (lewat SessionLocal),
BUKAN SQLite terpisah.
"""
import pandas as pd
from database import SessionLocal
from migrate_here.models import UmkmBatik


def seed(excel_path: str = "data_umkm_batik_trenggalek.xls"):
    db = SessionLocal()
    try:
        # Idempotent — hapus dulu supaya re-run tidak duplikat
        deleted = db.query(UmkmBatik).delete()
        print(f"  Menghapus {deleted} baris lama...")

        df = pd.read_excel(excel_path)
        inserted = 0
        for _, row in df.iterrows():
            db.add(UmkmBatik(
                id_umkm           = str(row["id_umkm"]),
                nama_umkm         = str(row["nama_umkm"]),
                nama_motif        = str(row["nama_motif"]),
                harga_per_meter   = int(row["harga_per_meter"]) if pd.notna(row["harga_per_meter"]) else None,
                kontak_wa         = str(row["kontak_wa"]),
                alamat            = str(row["alamat"]) if pd.notna(row["alamat"]) else None,
                harga_kemeja_jadi = int(row["harga_kemeja_jadi"]) if pd.notna(row.get("harga_kemeja_jadi")) else None,
                harga_kiloan      = int(row["harga_kiloan"]) if pd.notna(row.get("harga_kiloan")) else None,
                file_gambar       = str(row["file_gambar"]) if pd.notna(row.get("file_gambar")) else None,
            ))
            inserted += 1

        db.commit()
        print(f"✓ Seeded {inserted} produk ke tabel umkm_batik")
    except Exception as e:
        db.rollback()
        print(f"✗ Gagal seed: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed()