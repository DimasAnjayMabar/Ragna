# product_tool.py
"""Tool query produk UMKM batik dari tabel umkm_batik."""
import logging
from typing import Optional
from sqlalchemy import or_
from database import SessionLocal
from migrate_here.models import UmkmBatik

log = logging.getLogger("ragna")


class ProductTool:
    """
    Query produk dari tabel umkm_batik.
    Memakai SessionLocal dari database.py — tidak ada engine/Session terpisah.
    """

    def search(
        self,
        keyword:   Optional[str] = None,
        motif:     Optional[str] = None,
        max_harga: Optional[int] = None,
        limit:     int = 5,
    ) -> list[dict]:
        db = SessionLocal()
        try:
            q = db.query(UmkmBatik)

            if keyword:
                q = q.filter(or_(
                    UmkmBatik.nama_motif.ilike(f"%{keyword}%"),
                    UmkmBatik.nama_umkm.ilike(f"%{keyword}%"),
                ))
            if motif:
                q = q.filter(UmkmBatik.nama_motif.ilike(f"%{motif}%"))
            if max_harga:
                q = q.filter(UmkmBatik.harga_per_meter <= max_harga)

            rows = q.limit(limit).all()
            return [{
                "id_umkm":         r.id_umkm,
                "nama_umkm":       r.nama_umkm,
                "nama_motif":      r.nama_motif,
                "harga_per_meter": r.harga_per_meter,
                "kontak_wa":       r.kontak_wa,
                "alamat":          r.alamat,
            } for r in rows]
        finally:
            db.close()