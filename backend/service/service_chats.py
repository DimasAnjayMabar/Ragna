import sys
import os
import threading
import asyncio
from sqlalchemy.orm import Session
from datetime import datetime
import time
import logging
from fastapi import HTTPException, status
from migrate_here.models import Chat, ChatDetail, PipelineLog, Documents
import hashlib

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from pipeline import get_rag_pipeline

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================


# =============================================================================
# IN-MEMORY EVENT STORE
# =============================================================================
# Menyimpan asyncio.Event per detail_id.
# Saat background task selesai → set event → SSE endpoint langsung tahu.
#
# Struktur: { detail_id: asyncio.Event }
# Event di-cleanup otomatis setelah SSE client disconnect atau timeout.

_pending_events: dict[int, asyncio.Event] = {}
_events_lock = threading.Lock()
_stop_events: dict[int, threading.Event] = {}
_stop_lock = threading.Lock()

def _get_stop_event(detail_id: int) -> threading.Event:
    with _stop_lock:
        if detail_id not in _stop_events:
            _stop_events[detail_id] = threading.Event()
        return _stop_events[detail_id]


def _signal_stop(detail_id: int) -> None:
    """Sinyal ke pipeline agar streaming dihentikan."""
    with _stop_lock:
        event = _stop_events.get(detail_id)
        if event:
            event.set()
            logger.info(f"Stop signal dikirim → detail_id={detail_id}")


def _cleanup_stop_event(detail_id: int) -> None:
    with _stop_lock:
        _stop_events.pop(detail_id, None)

def _get_or_create_event(detail_id: int) -> asyncio.Event:
    with _events_lock:
        if detail_id not in _pending_events:
            _pending_events[detail_id] = asyncio.Event()
        return _pending_events[detail_id]


def _signal_done(detail_id: int) -> None:
    """
    Memberikan sinyal 'done' ke event yang sedang ditunggu oleh SSE.
    Aman dipanggil dari background thread.
    """
    with _events_lock:
        event = _pending_events.get(detail_id)
        if event:
            try:
                # Ambil event loop utama yang sedang berjalan
                loop = asyncio.get_event_loop()
                # Set event secara thread-safe
                loop.call_soon_threadsafe(event.set)
            except Exception as e:
                logger.error(f"Gagal mengirim sinyal done untuk {detail_id}: {e}")
                # Fallback jika loop sulit didapat (biasanya pada shutdown)
                event.set()


def _cleanup_event(detail_id: int) -> None:
    with _events_lock:
        _pending_events.pop(detail_id, None)

def _reset_event(detail_id: int) -> asyncio.Event:
    """
    Reset asyncio.Event (SSE) dan threading.Event (stop signal).
    Dipanggil sebelum spawn thread baru untuk edit/regenerate
    agar event lama yang sudah ter-set tidak langsung trigger SSE.
    """
    with _events_lock:
        _pending_events.pop(detail_id, None)
        event = asyncio.Event()
        _pending_events[detail_id] = event

    # Reset stop event juga agar pipeline baru tidak langsung berhenti
    with _stop_lock:
        _stop_events.pop(detail_id, None)

    return event


# =============================================================================
# HELPER — LLM
# =============================================================================

def _call_llm(
    question: str,
    chat_id: int,
    user_id: int | None = None,
    stop_event: threading.Event = None,
    base64_image: str | None = None, # <--- TAMBAHAN
) -> dict:
    start    = time.time()
    pipeline = get_rag_pipeline()
    full_response = ""
    
    # Cukup panggil process_query, pipeline yang akan mengurus pemisahan/penggabungan
    rag_response = pipeline.process_query(
        query=question,
        chat_id=chat_id,
        user_id=user_id,
        stop_event=stop_event,
        base64_image=base64_image # <--- DITERUSKAN
    )
    
    for token in rag_response.answer:
        if stop_event is not None and stop_event.is_set():
            break
        full_response += token

    return {
        "response":      full_response,
        "input_tokens":  len(question.split()),
        "output_tokens": len(full_response.split()),
        "total_cost":    0.0,
        "latency_ms":    int((time.time() - start) * 1000),
    }


def _auto_title(question: str) -> str:
    words = question.strip().split()
    return " ".join(words[:5]) + ("..." if len(words) > 5 else "")


def _save_pipeline_log(
    db:          Session,
    detail_id:   int,
    llm_result:  dict,
    llm_status:  str,
    error_msg:   str | None,
    existing_log = None,
) -> PipelineLog:
    if existing_log:
        log = existing_log
    else:
        log = PipelineLog(chat_detail_id=detail_id)
        db.add(log)

    log.latency_ms    = llm_result["latency_ms"]
    log.status        = llm_status
    log.error_message = error_msg
    log.input_tokens  = llm_result["input_tokens"]
    log.output_tokens = llm_result["output_tokens"]
    log.total_cost    = llm_result["total_cost"]
    return log


def _invoke_llm_safe(
    question: str,
    chat_id: int,
    context: str,
    user_id: int | None = None,
    stop_event: threading.Event = None,
    base64_image: str | None = None, # <--- TAMBAHAN
) -> tuple[dict, str, str | None]:
    try:
        result = _call_llm(
            question, chat_id=chat_id, user_id=user_id, 
            stop_event=stop_event, base64_image=base64_image # <--- DITERUSKAN
        )
        return result, "success", None
    except Exception as exc:
        logger.error(f"LLM call failed: {exc}")
        return {
            "response":      "Maaf, terjadi kesalahan saat memproses pertanyaan Anda.",
            "input_tokens":  0,
            "output_tokens": 0,
            "total_cost":    0.0,
            "latency_ms":    0,
        }, "failed", str(exc)


# =============================================================================
# BACKGROUND TASK — Memory Worker
# =============================================================================

def _delete_memory_by_chat(chat_id: int) -> None:
    """
    Hapus semua entry memory di ChromaDB untuk chat_id tertentu.
    Mencakup dua jenis entry:
      - summary_{chat_id}        → running summary
      - recent_{chat_id}_*       → semua entry episodik (recent window)

    Dipanggil oleh delete_topic sebelum menghapus Chat dari SQL.
    Berjalan di thread pemanggil (bukan daemon) karena harus selesai
    sebelum SQL commit — urutan penting untuk konsistensi data.
    """
    try:
        pipeline  = get_rag_pipeline()
        collection = pipeline.chroma.client.get_or_create_collection("chat_memory")

        # ── Hapus running summary ─────────────────────────────────────────────
        try:
            collection.delete(ids=[f"summary_{chat_id}"])
            logger.info(f"[MemoryDelete] Summary dihapus → chat_id={chat_id}")
        except Exception:
            # Belum ada summary — tidak masalah
            logger.debug(f"[MemoryDelete] Tidak ada summary untuk chat_id={chat_id}")

        # ── Hapus semua recent entries ────────────────────────────────────────
        try:
            # Filter by id prefix — lebih reliable daripada where filter
            all_results = collection.get(include=[])
            prefix      = f"recent_{chat_id}_"
            ids_to_delete = [
                doc_id for doc_id in all_results["ids"]
                if doc_id.startswith(prefix)
            ]
            if ids_to_delete:
                collection.delete(ids=ids_to_delete)
                logger.info(
                    f"[MemoryDelete] {len(ids_to_delete)} recent entries dihapus "
                    f"→ chat_id={chat_id}"
                )
            else:
                logger.debug(
                    f"[MemoryDelete] Tidak ada recent entries untuk chat_id={chat_id}"
                )
        except Exception:
            logger.debug(
                f"[MemoryDelete] Tidak ada recent entries untuk chat_id={chat_id}"
            )

    except Exception as exc:
        # Memory delete gagal tidak boleh menghentikan delete topic
        # — log saja, lanjutkan proses
        logger.error(
            f"[MemoryDelete] Gagal hapus memory chat_id={chat_id}: {exc}",
            exc_info=True,
        )


def _save_memory_entry(chat_id: int, detail_id: int, question: str, answer: str) -> None:
    """
    Simpan satu Q&A pair ke ChromaDB 'chat_memory' sebagai entry episodik.

    Dipanggil oleh _rag_worker setelah response berhasil di-commit ke DB.
    Berjalan di thread daemon terpisah agar tidak memblokir sinyal SSE.

    Identitas user (nama dll.) TIDAK disimpan di sini — sudah ditangani
    oleh _save_identity_entry yang dipanggil terpisah di _rag_worker.
    """
    try:
        pipeline = get_rag_pipeline()
        pipeline.save_memory(chat_id, detail_id, question, answer)
        logger.info(f"[MemorySave] Selesai → chat_id={chat_id} detail_id={detail_id}")
    except Exception as exc:
        logger.error(f"[MemorySave] Error — chat_id={chat_id}: {exc}", exc_info=True)


# =============================================================================
# BACKGROUND TASK — RAG Worker
# =============================================================================

def _rag_worker(
    detail_id: int,
    question: str,
    chat_id: int,
    db_factory,
    user_id: int,
    is_edit: bool = False,
    base64_image: str | None = None, # <--- TAMBAHAN
) -> None:
    db: Session = db_factory()
    stop_event = _get_stop_event(detail_id)
    try:
        from backend.migrate_here.models import User
        user      = db.query(User).filter(User.id == user_id).first()
        user_name = user.name if user else None

        if user_name:
            try:
                pipeline_obj = get_rag_pipeline()
                pipeline_obj.save_identity(user_id, user_name)
            except Exception as exc:
                logger.warning(f"[Identity] Gagal simpan identity: {exc}")

        if is_edit:
            try:
                pipeline_obj = get_rag_pipeline()
                collection   = pipeline_obj.chroma.client.get_or_create_collection("chat_memory")
                collection.delete(ids=[f"summary_{chat_id}"])
                collection.delete(ids=[f"recent_{chat_id}_{detail_id}"])
            except Exception as exc:
                logger.warning(f"[MemoryReset] Gagal hapus memory lama: {exc}")

        llm_result, llm_status, error_msg = _invoke_llm_safe(
            question,
            chat_id=chat_id,
            context=f"bg_detail_id={detail_id}",
            user_id=user_id,
            stop_event=stop_event,
            base64_image=base64_image,
        )

        was_stopped = stop_event.is_set()
        if was_stopped:
            llm_status = "stopped"
            logger.info(f"RAG worker dihentikan oleh user → detail_id={detail_id}")

        detail = db.query(ChatDetail).filter(ChatDetail.id == detail_id).first()
        if detail is None:
            return

        detail.response = llm_result["response"]  
        detail.processing_status = (
            "stopped" if was_stopped
            else ("done" if llm_status == "success" else "failed")
        )

        existing_log = db.query(PipelineLog).filter(
            PipelineLog.chat_detail_id == detail_id
        ).first()

        _save_pipeline_log(
            db, detail_id, llm_result,
            llm_status if not was_stopped else "stopped",
            error_msg, existing_log=existing_log,
        )

        db.commit()
        
        if llm_status == "success" and not was_stopped:
            m = threading.Thread(
                target=_save_memory_entry,
                args=(chat_id, detail_id, question, llm_result["response"]),
                daemon=True,
            )
            m.start()

    except Exception as exc:
        logger.error(f"RAG worker error — detail_id={detail_id}: {exc}")
        try:
            detail = db.query(ChatDetail).filter(ChatDetail.id == detail_id).first()
            if detail:
                detail.processing_status = "failed"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
        _cleanup_stop_event(detail_id)
        _signal_done(detail_id)


# =============================================================================
# CHAT SERVICE
# =============================================================================

class ChatService:

    # -------------------------------------------------------------------------
    # TOPICS
    # -------------------------------------------------------------------------

    @staticmethod
    def get_topics(db: Session, user_id: int) -> list[Chat]:
        return (
            db.query(Chat)
            .filter(Chat.user_id == user_id)
            .order_by(Chat.created_at.desc())
            .all()
        )

    @staticmethod
    def get_topic(db: Session, user_id: int, chat_id: int) -> Chat:
        chat = db.query(Chat).filter(
            Chat.id == chat_id,
            Chat.user_id == user_id,
        ).first()
        if not chat:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat tidak ditemukan.",
            )
        return chat

    @staticmethod
    def delete_topic(db: Session, user_id: int, chat_id: int) -> bool:
        chat = db.query(Chat).filter(
            Chat.id == chat_id,
            Chat.user_id == user_id,
        ).first()
        if not chat:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat tidak ditemukan.",
            )

        # Hapus memory ChromaDB lebih dulu sebelum SQL commit
        # — urutan penting agar tidak ada orphan memory jika SQL gagal
        _delete_memory_by_chat(chat_id)

        db.delete(chat)
        db.commit()
        logger.info(f"Topic deleted → chat_id={chat_id}")
        return True

    @staticmethod
    def rename_topic(db: Session, user_id: int, chat_id: int, new_title: str) -> Chat:
        chat = db.query(Chat).filter(
            Chat.id == chat_id,
            Chat.user_id == user_id,
        ).first()
        if not chat:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Chat tidak ditemukan.",
            )
        chat.title = new_title.strip()
        db.commit()
        db.refresh(chat)
        logger.info(f"Topic renamed → chat_id={chat_id}, title={chat.title}")
        return chat

    # -------------------------------------------------------------------------
    # MESSAGES
    # -------------------------------------------------------------------------
    
    @staticmethod
    def send_message(
        db:          Session,
        user_id:     int,
        chat_id:     int | None,
        question:    str,
        db_factory,
        base64_image: str | None = None,
    ) -> ChatDetail:
        """
        Kirim pertanyaan ke RAG pipeline secara async.
        Langsung return ChatDetail dengan processing_status='pending'.

        Alur:
          1. Resolve atau buat topic baru (jika chat_id=None)
          2. Buat ChatDetail + PipelineLog placeholder
          3. Siapkan asyncio.Event untuk SSE
          4. Spawn _rag_worker (thread daemon) dengan chat_id
          5. Return detail — frontend polling via SSE

        db_factory diperlukan karena SQLAlchemy Session tidak thread-safe.
        Background thread membuat Session sendiri via db_factory().
        """
        # ── Resolve atau buat topic ───────────────────────────────────────────
        if chat_id is None:
            chat = Chat(
                user_id=user_id,
                title=_auto_title(question),
                created_at=datetime.utcnow(),
            )
            db.add(chat)
            db.flush()
            logger.info(
                f"Auto-create topic → chat_id={chat.id}, title='{chat.title}'"
            )
        else:
            chat = db.query(Chat).filter(
                Chat.id == chat_id,
                Chat.user_id == user_id,
            ).first()
            if not chat:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Chat tidak ditemukan.",
                )

        # ── Buat ChatDetail dengan status pending ─────────────────────────────
        detail = ChatDetail(
            chat_id           = chat.id,
            question          = question.strip(),
            response          = "",
            processing_status = "pending",
            created_at        = datetime.utcnow(),
        )
        db.add(detail)
        db.flush()

        # ── Buat PipelineLog placeholder ──────────────────────────────────────
        placeholder_log = PipelineLog(
            chat_detail_id = detail.id,
            latency_ms     = 0,
            status         = "pending",
            input_tokens   = 0,
            output_tokens  = 0,
            total_cost     = 0.0,
        )
        db.add(placeholder_log)

        db.commit()
        db.refresh(detail)

        # ── Siapkan event SEBELUM spawn thread ────────────────────────────────
        _get_or_create_event(detail.id)

        # ── Spawn RAG worker dengan chat_id + user_id ─────────────────────────
        t = threading.Thread(
            target=_rag_worker,
            args=(detail.id, question.strip(), chat.id, db_factory, user_id),
            kwargs={"base64_image": base64_image}, # <--- DITERUSKAN KE WORKER
            daemon=True,
            name=f"rag-worker-{detail.id}",
        )
        t.start()
        return detail

    @staticmethod
    def get_detail(db: Session, user_id: int, detail_id: int) -> ChatDetail:
        """Ambil satu ChatDetail — dipakai SSE endpoint untuk baca hasil."""
        detail = (
            db.query(ChatDetail)
            .join(Chat)
            .filter(ChatDetail.id == detail_id, Chat.user_id == user_id)
            .first()
        )
        if not detail:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Pesan tidak ditemukan.",
            )
        return detail

    @staticmethod
    def edit_message(
        db:           Session,
        user_id:      int,
        detail_id:    int,
        new_question: str,
        db_factory,
    ) -> ChatDetail:
        """
        Edit pertanyaan → jalankan ulang RAG di background.
        Memory yang dipakai tetap memory chat yang sama (chat_id tidak berubah).
        Langsung return detail dengan status 'pending'.
        """
        detail = (
            db.query(ChatDetail)
            .join(Chat)
            .filter(ChatDetail.id == detail_id, Chat.user_id == user_id)
            .first()
        )
        if not detail:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Pesan tidak ditemukan.",
            )

        chat_id = detail.chat_id  # simpan sebelum modifikasi

        detail.question           = new_question.strip()
        detail.response           = ""
        detail.processing_status  = "pending"
        db.commit()
        db.refresh(detail)

        _reset_event(detail.id)

        t = threading.Thread(
            target=_rag_worker,
            args=(detail.id, new_question.strip(), chat_id, db_factory, user_id),
            kwargs={"is_edit": True},
            daemon=True,
            name=f"rag-edit-{detail.id}",
        )
        t.start()
        return detail

    @staticmethod
    def regenerate_response(
        db:        Session,
        user_id:   int,
        detail_id: int,
        db_factory,
    ) -> ChatDetail:
        """
        Regenerate → jalankan ulang RAG di background dengan question sama.
        Memory yang dipakai tetap memory chat yang sama (chat_id tidak berubah).
        """
        detail = (
            db.query(ChatDetail)
            .join(Chat)
            .filter(ChatDetail.id == detail_id, Chat.user_id == user_id)
            .first()
        )
        if not detail:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Pesan tidak ditemukan.",
            )

        chat_id  = detail.chat_id  # simpan sebelum modifikasi
        question = detail.question

        detail.response           = ""
        detail.processing_status  = "pending"
        db.commit()
        db.refresh(detail)

        _reset_event(detail.id)

        t = threading.Thread(
            target=_rag_worker,
            args=(detail.id, question, chat_id, db_factory, user_id),
            kwargs={"is_edit": True},
            daemon=True,
            name=f"rag-regen-{detail.id}",
        )
        t.start()
        return detail

    @staticmethod
    def delete_message(db: Session, user_id: int, detail_id: int) -> bool:
        detail = (
            db.query(ChatDetail)
            .join(Chat)
            .filter(ChatDetail.id == detail_id, Chat.user_id == user_id)
            .first()
        )
        if not detail:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Pesan tidak ditemukan.",
            )

        chat_id = detail.chat_id  # simpan sebelum delete

        db.delete(detail)
        db.commit()
        logger.info(f"Message deleted → detail_id={detail_id}  chat_id={chat_id}")

        # Hapus recent entry dari ChromaDB setelah SQL commit berhasil
        # Summary tidak disentuh — satu pesan dihapus tidak perlu recalculate ringkasan
        _delete_memory_entry(chat_id, detail_id)

        return True

    @staticmethod
    def stop_generation(db: Session, user_id: int, detail_id: int) -> ChatDetail:
        detail = (
            db.query(ChatDetail)
            .join(Chat)
            .filter(ChatDetail.id == detail_id, Chat.user_id == user_id)
            .first()
        )
        if not detail:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Pesan tidak ditemukan.",
            )
        if detail.processing_status != "pending":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Pesan tidak sedang diproses (status: {detail.processing_status}).",
            )

        _signal_stop(detail_id)
        logger.info(f"Stop diminta → detail_id={detail_id}  user_id={user_id}")
        return detail


# =============================================================================
# KNOWLEDGE SERVICE — PDF Upload & Embedding
# =============================================================================

def _embed_and_commit(
    saved_path:      str,
    jurnal_metadata: dict,
    file_hash:       str,
    dest_path,                   # pathlib.Path — untuk hapus file jika duplikat
    db_factory,                  # callable → SQLAlchemy Session
    target_func,                 # run_pipeline_with_shared_resources atau _raw
    log_prefix:      str,
) -> None:
    """
    Background thread: jalankan embedder lalu — hanya jika berhasil —
    commit hash ke tabel Documents.

    Tiga kemungkinan hasil:
      1. Sukses (result != None)  → simpan hash ke DB, file tetap di disk.
      2. Duplikat (result == None, tanpa exception) → file dihapus dari disk,
         hash TIDAK disimpan (sudah ada di DB dari upload sebelumnya).
      3. Error transient (exception) → file TETAP di disk agar user bisa retry,
         hash TIDAK disimpan ke DB. Backend tidak crash.
    """
    result    = None
    has_error = False

    try:
        result = target_func(saved_path, jurnal_metadata)
        if result:
            stats = result.get("stats", {})
            logger.info(
                f"[KnowledgeEmbed-{log_prefix}] Selesai → file={saved_path}  stats={stats}"
            )
        else:
            # Pipeline mengembalikan None → file duplikat di ChromaDB
            logger.warning(
                f"[KnowledgeEmbed-{log_prefix}] Pipeline mengembalikan None "
                f"(file mungkin sudah pernah diproses di ChromaDB) → {saved_path}"
            )
    except Exception as exc:
        has_error = True
        logger.error(
            f"[KnowledgeEmbed-{log_prefix}] Error saat embed → file={saved_path}: {exc}",
            exc_info=True,
        )
        logger.info(
            f"[KnowledgeEmbed-{log_prefix}] File dipertahankan di disk — "
            f"user dapat mencoba upload ulang → {saved_path}"
        )

    if result is not None:
        # ── Embedding sukses → simpan hash ke DB ──────────────────────────────
        db = db_factory()
        try:
            from backend.migrate_here.models import Documents
            doc = Documents(hash_value=file_hash)
            db.add(doc)
            db.commit()
            logger.info(
                f"[KnowledgeEmbed-{log_prefix}] Hash disimpan ke DB → {file_hash[:8]}..."
            )
        except Exception as exc:
            db.rollback()
            logger.error(
                f"[KnowledgeEmbed-{log_prefix}] Gagal simpan hash ke DB: {exc}",
                exc_info=True,
            )
        finally:
            db.close()

    elif not has_error:
        # ── Duplikat di ChromaDB (bukan error) → hapus file dari disk ─────────
        # Ini terjadi ketika hash file sama sudah ada di ChromaDB tapi belum
        # ada di tabel Documents (edge case). File tidak diperlukan lagi.
        try:
            dest_path.unlink(missing_ok=True)
            logger.warning(
                f"[KnowledgeEmbed-{log_prefix}] File dihapus dari disk karena "
                f"duplikat di ChromaDB → {saved_path}"
            )
        except Exception as exc:
            logger.error(
                f"[KnowledgeEmbed-{log_prefix}] Gagal hapus file dari disk: {exc}"
            )
    # has_error=True → file sengaja dipertahankan, tidak ada aksi tambahan


# service_chats.py - Perbaikan bagian KnowledgeService
# service_chats.py - Perbaikan method upload_pdf

class KnowledgeService:
    """
    Service untuk mengelola knowledge base chatbot via upload PDF.

    Alur upload:
      1. Controller menerima UploadFile, validasi tipe & ukuran
      2. Controller memanggil KnowledgeService.upload_pdf() dengan bytes mentah
      3. Service menyimpan file ke ./dataset/ (DATASET_DIR)
      4. Service spawn _embed_worker di daemon thread (untuk improved mode)
         atau _embed_worker_raw (untuk raw mode)
      5. Return info file — embedder jalan di background tanpa memblokir response

    embedder_type:
      - "improved": pipeline lengkap dengan Neo4j + ChromaDB konten_isi
      - "raw":      pipeline sederhana hanya ChromaDB konten_isi_raw
    """

    # Sesuai CONFIG["dataset_path"] di embedder.py
    DATASET_DIR = "../uploaded_datasets"

    @staticmethod
    def upload_pdf(
        file_bytes: bytes,
        filename:   str,
        db: Session,  # ← tambahkan db parameter
        judul:      str | None = None,
        penulis:    str | None = None,
        tahun:      str | None = None,
        user_id:    int | None = None,
        embedder_type: str = "improved",
    ) -> dict:
        """
        Simpan PDF lalu jalankan embedder di background thread.

        Parameters:
          file_bytes     : konten file PDF sebagai bytes (sudah dibaca oleh controller)
          filename       : nama file asli dari user, e.g. "jurnal_padi.pdf"
          db             : SQLAlchemy Session untuk menyimpan hash file
          judul          : judul jurnal/dokumen (opsional, fallback ke nama file)
          penulis        : nama penulis (opsional, fallback ke "Unknown Author")
          tahun          : tahun terbit e.g. "2024" (opsional, fallback ke "2024")
          user_id        : id user yang upload — untuk logging saja, tidak disimpan ke DB
          embedder_type  : "improved" (Neo4j + ChromaDB) atau "raw" (ChromaDB only)

        Returns:
          dict berisi info file yang disimpan + status "processing"
        """
        import re
        from pathlib import Path

        # ── Pastikan folder dataset ada ───────────────────────────────────────
        dataset_dir = Path(KnowledgeService.DATASET_DIR)
        dataset_dir.mkdir(parents=True, exist_ok=True)

        # ── Sanitasi nama file asli ───────────────────────────────────────────
        # Hapus karakter selain huruf, angka, titik, dash, underscore, spasi
        clean_original = re.sub(r"[^\w\s.\-]", "_", filename).strip()
        # Pastikan ekstensi .pdf tetap ada setelah sanitasi
        if not clean_original.lower().endswith(".pdf"):
            clean_original += ".pdf"

        # ── Tambahkan prefix datetime server ─────────────────────────────────────
        # Format: YYYYMMDD_HHMMSS_<nama_asli>.pdf
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_name = f"{ts}_{clean_original}"

        dest_path = dataset_dir / safe_name

        file_hash = hashlib.md5(file_bytes).hexdigest()

        # ── Cek duplikat di Documents table ───────────────────────────────────
        existing = db.query(Documents).filter(Documents.hash_value == file_hash).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="File ini sudah pernah diupload sebelumnya.",
            )

        # ── Simpan ke disk dulu ───────────────────────────────────────────────
        dest_path.write_bytes(file_bytes)
        logger.info(
            f"[KnowledgeUpload] File disimpan → {dest_path}  "
            f"size={len(file_bytes)} bytes  user_id={user_id}  mode={embedder_type}"
        )

        # ── Susun metadata jurnal ─────────────────────────────────────────────
        stem = Path(clean_original).stem
        jurnal_metadata = {
            "judul":         (judul or stem).strip(),
            "doi":           None,
            "penulis":       (penulis or "Unknown Author").strip(),
            "tanggal_rilis": (tahun or "2024").strip(),
        }

        # ── Pilih target func berdasarkan embedder_type ───────────────────────
        if embedder_type == "raw":
            from embed_knowledge_base_here.embedder import run_pipeline_with_shared_resources_raw
            target_func = run_pipeline_with_shared_resources_raw
            log_prefix = "RAW"
        else:
            from embed_knowledge_base_here.embedder import run_pipeline_with_shared_resources
            target_func = run_pipeline_with_shared_resources
            log_prefix = "IMPROVED"

        from database import SessionLocal

        # ── Spawn _embed_and_commit di background ─────────────────────────────
        # Hash ke DB hanya disimpan SETELAH embedding sukses (di dalam thread).
        t = threading.Thread(
            target=_embed_and_commit,
            args=(str(dest_path), jurnal_metadata, file_hash, dest_path, SessionLocal, target_func, log_prefix),
            daemon=True,
            name=f"embed-{log_prefix.lower()}-{safe_name}",
        )
        t.start()
        logger.info(
            f"[KnowledgeUpload] Embedder ({embedder_type}) dimulai di background → "
            f"file={safe_name}  thread={t.name}"
        )

        return {
            "filename":        safe_name,
            "size_bytes":      len(file_bytes),
            "saved_path":      str(dest_path),
            "judul":           jurnal_metadata["judul"],
            "penulis":         jurnal_metadata["penulis"],
            "tahun":           jurnal_metadata["tanggal_rilis"],
            "status":          "processing",
            "embedder_type":   embedder_type,
        }