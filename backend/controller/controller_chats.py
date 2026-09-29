# controller_chats.py - UPDATE LANGSUNG

import asyncio
import json
import logging
import io
from gtts import gTTS
import os
import base64
import sys  # <-- TAMBAHKAN

# Tambahkan path ke root project untuk import config
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from database import get_db, SessionLocal
from middleware.auth import get_current_session
from migrate_here.models import UserAuth, ChatDetail, Chat
from service.service_chats import ChatService, KnowledgeService, _get_or_create_event, _cleanup_event, _signal_stop
from validation.chats import (
    RenameTitleSchema,
    SendMessageSchema,
    EditMessageSchema,
)

# ── IMPORT CONFIG ──────────────────────────────────────────────────────────────
from config import CONFIG, GROQ_ALLOWED_MODELS, GROQ_MODEL_TPM_LIMITS, GROQ_MODEL_REGISTRY

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Chats"])

_SSE_TIMEOUT_SECONDS    = 3600
_SSE_HEARTBEAT_SECONDS  = 15


# =============================================================================
# HELPERS — Serializer
# =============================================================================

def _serialize_detail(d) -> dict:
    return {
        "id":                d.id,
        "chat_id":           d.chat_id,
        "question":          d.question,
        "response":          d.response,
        "processing_status": d.processing_status,
        "created_at":        d.created_at.isoformat(),
        "pipeline_log": {
            "latency_ms":    d.pipeline_log.latency_ms,
            "status":        d.pipeline_log.status,
            "input_tokens":  d.pipeline_log.input_tokens,
            "output_tokens": d.pipeline_log.output_tokens,
            "total_cost":    d.pipeline_log.total_cost,
        } if d.pipeline_log else None,
    }


def _serialize_topic(chat, include_details: bool = False) -> dict:
    data = {
        "id":         chat.id,
        "title":      chat.title,
        "created_at": chat.created_at.isoformat(),
    }
    if include_details:
        data["messages"]       = [_serialize_detail(d) for d in chat.details]
        data["total_messages"] = len(chat.details)
    return data


def _sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _sse_heartbeat() -> str:
    return ": heartbeat\n\n"


# =============================================================================
# HELPER — Model Metadata (dinamis dari config.py)
# =============================================================================

def _get_model_metadata() -> dict:
    """
    Bangun metadata model dari GROQ_MODEL_REGISTRY di config.py.
    GROQ_MODEL_REGISTRY adalah SATU-SATUNYA source of truth untuk semua
    model (nama, provider, tier, deskripsi, TPS, TPM limit) — tidak ada
    lagi duplikasi TPS_MAP/METADATA di file ini. Mengganti model Groq
    cukup edit config.py sekali (manual atau lewat tool "Apply").
    """
    models = []
    for model_id in sorted(GROQ_ALLOWED_MODELS):
        meta = GROQ_MODEL_REGISTRY.get(model_id, {})
        models.append({
            "id": model_id,
            "name": meta.get("name", model_id.split('/')[-1].replace('-', ' ').title()),
            "provider": meta.get("provider", "Groq"),
            "tier": meta.get("tier", "medium"),
            "description": meta.get("description", f"Model {model_id}"),
            "tps": meta.get("tps", 100),
            "tpm_limit": meta.get("tpm_limit", GROQ_MODEL_TPM_LIMITS.get(model_id, 6000)),
        })
    return models


# =============================================================================
# TOPICS
# =============================================================================

@router.get("/topics", status_code=status.HTTP_200_OK)
def get_topics(
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    try:
        chats = ChatService.get_topics(db, current_session.user_id)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "Daftar topik berhasil diambil.",
                "data": {
                    "topics": [_serialize_topic(c) for c in chats],
                    "total":  len(chats),
                },
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"GET /topics error → {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat mengambil topik.")


@router.get("/topics/{chat_id}", status_code=status.HTTP_200_OK)
def get_topic(
    chat_id: int,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    try:
        chat = ChatService.get_topic(db, current_session.user_id, chat_id)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "Topik berhasil diambil.",
                "data":    _serialize_topic(chat, include_details=True),
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"GET /topics/{chat_id} error → {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat mengambil topik.")


@router.delete("/topics/{chat_id}", status_code=status.HTTP_200_OK)
def delete_topic(
    chat_id: int,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    try:
        ChatService.delete_topic(db, current_session.user_id, chat_id)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"success": True, "message": "Topik berhasil dihapus."},
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"DELETE /topics/{chat_id} error → {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat menghapus topik.")


@router.patch("/topics/{chat_id}", status_code=status.HTTP_200_OK)
def rename_topic(
    chat_id: int,
    body: RenameTitleSchema,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    try:
        chat = ChatService.rename_topic(db, current_session.user_id, chat_id, body.title)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "Judul topik berhasil diubah.",
                "data":    _serialize_topic(chat),
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"PATCH /topics/{chat_id} error → {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat mengubah judul topik.")


# =============================================================================
# CHAT MESSAGES
# =============================================================================

@router.post("/chat/send", status_code=status.HTTP_202_ACCEPTED)
async def send_message(
    request: Request,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    """
    Satu Endpoint Untuk Semua!
    Secara otomatis mendeteksi apakah request berupa JSON (teks saja) atau Multipart (teks + gambar).
    """
    content_type = request.headers.get("content-type", "")
    
    question = ""
    chat_id = None
    base64_image = None
    
    # 1. Jika mengandung file gambar (Multipart Form-Data)
    if "multipart/form-data" in content_type:
        form = await request.form()
        question = form.get("question", "")
        chat_id_str = form.get("chat_id")
        if chat_id_str and str(chat_id_str).lower() != "null":
            chat_id = int(chat_id_str)
        
        file = form.get("file")
        if file and hasattr(file, "read"):
            image_bytes = await file.read()
            if image_bytes:
                base64_image = base64.b64encode(image_bytes).decode("utf-8")
                
    # 2. Jika hanya teks (JSON Raw)
    else:
        body = await request.json()
        question = body.get("question", "")
        chat_id = body.get("chat_id")
        
    try:
        detail = ChatService.send_message(
            db=db,
            user_id=current_session.user_id,
            chat_id=chat_id,
            question=question,
            db_factory=SessionLocal,
            base64_image=base64_image
        )
        
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "success": True,
                "message": "Pertanyaan diterima, sedang diproses.",
                "data": _serialize_detail(detail)
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"POST /chat/send error → {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat mengirim pesan.")


@router.get("/chat/message/{detail_id}", status_code=status.HTTP_200_OK)
def get_message(
    detail_id: int,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    """
    Ambil satu pesan lengkap (beserta jawaban AI) dari DB.

    Frontend memanggil endpoint ini setelah SSE mengirim event 'done'.
    Dengan cara ini, payload SSE hanya berupa sinyal — jawaban AI
    selalu diambil langsung dari DB, bukan dari response JSON.
    """
    try:
        detail = ChatService.get_detail(db, current_session.user_id, detail_id)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "Pesan berhasil diambil.",
                "data":    _serialize_detail(detail),
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"GET /chat/message/{detail_id} error → {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat mengambil pesan.")


@router.get("/chat/message/{detail_id}/tts", status_code=status.HTTP_200_OK)
def get_tts_audio(
    detail_id: int,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    """
    Mengubah teks jawaban AI (response) menjadi audio (Text-to-Speech).
    """
    try:
        detail = ChatService.get_detail(db, current_session.user_id, detail_id)
        
        if not detail.response:
            raise HTTPException(status_code=400, detail="Belum ada jawaban dari AI untuk diubah menjadi suara.")

        tts = gTTS(text=detail.response, lang='id', slow=False)
        
        audio_io = io.BytesIO()
        tts.write_to_fp(audio_io)
        audio_io.seek(0)

        return StreamingResponse(
            audio_io,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": f"inline; filename=agribot_response_{detail_id}.mp3"
            }
        )
        
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"GET /chat/message/{detail_id}/tts error → {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat men-generate audio TTS.")


@router.get("/chat/stream/{detail_id}")
async def stream_response(
    detail_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    try:
        detail = ChatService.get_detail(db, current_session.user_id, detail_id)
    except HTTPException as e:
        raise e

    async def event_stream():
        if detail.processing_status in ("done", "failed", "stopped"):
            event_type = (
                "done"    if detail.processing_status == "done"
                else "stopped" if detail.processing_status == "stopped"
                else "error"
            )
            yield _sse_event(event_type, {
                "detail_id":         detail_id,
                "processing_status": detail.processing_status,
            })
            _cleanup_event(detail_id)
            return

        yield _sse_event("waiting", {
            "detail_id": detail_id,
            "processing_status": "pending",
            "message": "Sedang memproses...",
        })

        done_event = _get_or_create_event(detail_id)
        elapsed = 0.0

        try:
            while elapsed < _SSE_TIMEOUT_SECONDS:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(done_event.wait()),
                        timeout=_SSE_HEARTBEAT_SECONDS,
                    )
                    logger.info(f"Event triggered for detail_id={detail_id}")
                    break
                except asyncio.TimeoutError:
                    elapsed += _SSE_HEARTBEAT_SECONDS
                    
                    if await request.is_disconnected():
                        logger.info(f"SSE client disconnected — detail_id={detail_id}")
                        _cleanup_event(detail_id)
                        return
                    
                    yield _sse_heartbeat()
            else:
                logger.warning(f"SSE timeout — detail_id={detail_id}")
                yield _sse_event("timeout", {
                    "detail_id": detail_id,
                    "processing_status": "pending",
                    "message": f"Timeout setelah {_SSE_TIMEOUT_SECONDS} detik.",
                })
                _cleanup_event(detail_id)
                return

        except Exception as exc:
            logger.error(f"SSE stream error — detail_id={detail_id}: {exc}")
            _cleanup_event(detail_id)
            return

        if await request.is_disconnected():
            logger.info(f"SSE client disconnected (after done) — detail_id={detail_id}")
            _cleanup_event(detail_id)
            return

        fresh_db = SessionLocal()
        try:
            fresh_detail = fresh_db.query(ChatDetail).filter_by(id=detail_id).first()
            final_status = fresh_detail.processing_status if fresh_detail else "failed"
            event_type = (
                "done"    if final_status == "done"
                else "stopped" if final_status == "stopped"
                else "error"
            )
            
            logger.info(f"Sending {event_type} event for detail_id={detail_id}")
            yield _sse_event(event_type, {
                "detail_id": detail_id,
                "processing_status": final_status,
            })
        finally:
            fresh_db.close()
            _cleanup_event(detail_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.patch("/chat/edit/{detail_id}", status_code=status.HTTP_202_ACCEPTED)
def edit_message(
    detail_id: int,
    body: EditMessageSchema,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    try:
        detail = ChatService.edit_message(
            db,
            current_session.user_id,
            detail_id,
            body.question,
            db_factory=SessionLocal,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "success": True,
                "message": "Pertanyaan diedit, sedang diproses ulang.",
                "data": {
                    "id":                detail.id,
                    "chat_id":           detail.chat_id,
                    "question":          detail.question,
                    "processing_status": detail.processing_status,
                    "created_at":        detail.created_at.isoformat(),
                },
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"PATCH /chat/edit/{detail_id} error → {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat mengedit pesan.")


@router.post("/chat/regenerate/{detail_id}", status_code=status.HTTP_202_ACCEPTED)
def regenerate_response(
    detail_id: int,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    try:
        detail = ChatService.regenerate_response(
            db,
            current_session.user_id,
            detail_id,
            db_factory=SessionLocal,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "success": True,
                "message": "Sedang men-generate ulang jawaban.",
                "data": {
                    "id":                detail.id,
                    "chat_id":           detail.chat_id,
                    "question":          detail.question,
                    "processing_status": detail.processing_status,
                    "created_at":        detail.created_at.isoformat(),
                },
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"POST /chat/regenerate/{detail_id} error → {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat regenerate jawaban.")


@router.post("/chat/stop/{detail_id}", status_code=status.HTTP_200_OK)
def stop_generation(
    detail_id: int,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    try:
        detail = ChatService.stop_generation(db, current_session.user_id, detail_id)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "Pipeline dihentikan.",
                "data": {
                    "id":                detail.id,
                    "chat_id":           detail.chat_id,
                    "processing_status": detail.processing_status,
                },
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"POST /chat/stop/{detail_id} error → {e}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan saat menghentikan pipeline.")


# =============================================================================
# KNOWLEDGE BASE — PDF UPLOAD
# =============================================================================

@router.post("/knowledge/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_knowledge_pdf(
    file: UploadFile | None = File(default=None),          # kompatibel dengan klien lama (1 file)
    files: list[UploadFile] | None = File(default=None),   # baru: banyak file dalam 1 request
    judul: str | None = Form(default=None),                # hanya dipakai jika request berisi 1 file
    penulis: str | None = Form(default=None),
    tahun: str | None = Form(default=None),
    embedder_type: str = Form(default="improved"),
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session),
):
    if embedder_type not in ("improved", "raw"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Parameter 'embedder_type' harus 'improved' atau 'raw'.",
        )

    incoming: list[UploadFile] = list(files or [])
    if file is not None:
        incoming.append(file)
    if not incoming:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Minimal satu file PDF harus diupload.",
        )

    MAX_SIZE = 50 * 1024 * 1024
    valid: list[tuple[bytes, str]] = []
    rejected: list[dict] = []

    def _reject(name: str, code: int, detail: str):
        rejected.append({"filename": name, "status_code": code, "detail": detail})

    # Validasi per file: file yang tidak valid ditolak sendiri-sendiri,
    # file lain dalam request yang sama tetap diproses.
    for f in incoming:
        name = f.filename or "upload.pdf"
        if f.content_type not in ("application/pdf", "application/octet-stream"):
            _reject(name, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Hanya file PDF yang diperbolehkan.")
            continue
        if not name.lower().endswith(".pdf"):
            _reject(name, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Ekstensi file harus .pdf")
            continue
        data = await f.read()
        if len(data) > MAX_SIZE:
            _reject(name, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Ukuran file melebihi batas 50 MB.")
            continue
        if len(data) == 0:
            _reject(name, status.HTTP_400_BAD_REQUEST, "File PDF kosong.")
            continue
        valid.append((data, name))

    if not valid:
        # Tidak ada file valid → perilaku sama seperti sebelumnya (error dari file pertama)
        raise HTTPException(status_code=rejected[0]["status_code"], detail=rejected[0]["detail"])

    try:
        result = KnowledgeService.upload_pdf_batch(
            files         = valid,
            db            = db,
            judul         = judul,
            penulis       = penulis,
            tahun         = tahun,
            user_id       = current_session.user_id,
            embedder_type = embedder_type,
        )
        rejected.extend(result["rejected"])
        accepted = result["files"]

        if not accepted:
            # Semua file ditolak (mis. duplikat) → error dari file pertama
            raise HTTPException(status_code=rejected[0]["status_code"], detail=rejected[0]["detail"])

        single = len(incoming) == 1
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "success": True,
                "message": (
                    "PDF diterima dan sedang diproses ke knowledge base."
                    if single else
                    f"{len(accepted)} PDF diterima dan sedang diproses ke knowledge base."
                ),
                # 1 file → bentuk lama (dict). Banyak file → daftar + yang ditolak.
                "data": accepted[0] if single else {"files": accepted, "rejected": rejected},
            },
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"POST /knowledge/upload error → {e}")
        raise HTTPException(
            status_code=500,
            detail="Terjadi kesalahan saat memproses file PDF.",
        )


# =============================================================================
# MODEL MANAGEMENT — DINAMIS DARI CONFIG.PY
# =============================================================================

@router.get("/models", status_code=status.HTTP_200_OK)
def get_available_models(
    current_session: UserAuth = Depends(get_current_session),
):
    """
    Daftar model Groq yang tersedia — diambil dari config.py secara dinamis.
    """
    models = _get_model_metadata()
    active_model = CONFIG.get("groq_model", "llama-3.3-70b-versatile")

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "message": "Daftar model berhasil diambil.",
            "models": models,
            "active": active_model,
        },
    )


@router.post("/models/set-model", status_code=status.HTTP_200_OK)
async def set_active_model(
    request: Request,
    current_session: UserAuth = Depends(get_current_session),
):
    import json

    try:
        body = await request.json()
        model_id = body.get("model_id", "").strip()

        if not model_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Field 'model_id' wajib diisi.",
            )

        # Cek apakah model diizinkan — dari GROQ_ALLOWED_MODELS di config.py
        if model_id not in GROQ_ALLOWED_MODELS:
            allowed = ", ".join(sorted(GROQ_ALLOWED_MODELS))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Model '{model_id}' tidak dikenali. Gunakan salah satu dari: {allowed}",
            )

        # Update CONFIG
        CONFIG["groq_model"] = model_id

        from pipeline import reload_with_model
        reload_with_model("groq")

        logger.info(f"Groq model switched → model={model_id}  user_id={current_session.user_id}")

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": f"Berhasil beralih ke model {model_id}.",
                "data": {
                    "mode": "groq",
                    "model_id": model_id,
                },
            },
        )

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body.",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST /models/set-model error → {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengganti model: {str(e)}",
        )


@router.get("/models/active", status_code=status.HTTP_200_OK)
def get_active_model(
    current_session: UserAuth = Depends(get_current_session),
):
    """
    Mendapatkan model Groq yang sedang aktif beserta mode-nya.
    Endpoint ini digunakan oleh Flutter untuk sinkronisasi state setelah hot reload.
    """
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "message": "Model aktif berhasil diambil.",
            "data": {
                "model_id": CONFIG.get("groq_model", "llama-3.3-70b-versatile"),
                "mode": CONFIG.get("llm_mode", "groq"),
            }
        },
    )


# =============================================================================
# RAG MODE MANAGEMENT
# =============================================================================

@router.get("/rag/mode", status_code=status.HTTP_200_OK)
def get_rag_mode(
    current_session: UserAuth = Depends(get_current_session),
):
    """
    Mendapatkan mode RAG yang sedang aktif ('improved' atau 'regular').
    """
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "message": "Mode RAG berhasil diambil.",
            "data": {
                "mode": CONFIG.get("rag_mode", "improved"),
            }
        },
    )


@router.post("/rag/set-mode", status_code=status.HTTP_200_OK)
async def set_rag_mode(
    request: Request,
    current_session: UserAuth = Depends(get_current_session),
):
    """
    Mengganti mode RAG antara 'improved' dan 'regular'.
    
    - improved: pipeline lengkap dengan Neo4j context enrichment
    - regular:  pipeline sederhana (retrieval + rerank + LLM)
    """
    import json
    
    try:
        body = await request.json()
        mode = body.get("mode", "").strip().lower()
        
        if mode not in ("improved", "regular"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parameter 'mode' harus 'improved' atau 'regular'.",
            )
        
        CONFIG["rag_mode"] = mode
        
        logger.info(f"RAG mode switched → mode={mode}  user_id={current_session.user_id}")
        
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": f"Berhasil beralih ke mode RAG {mode}.",
                "data": {
                    "mode": mode,
                },
            },
        )
        
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body.",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"POST /rag/set-mode error → {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Gagal mengganti mode RAG: {str(e)}",
        )