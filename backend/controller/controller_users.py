from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
import logging
from database import get_db
from service.service_users import UserService
from validation.users import (
    RegisterSchema,
    LoginSchema,
    RequestOtpSchema,
    VerifyOtp,
    ResetPasswordSchema,
    RefreshTokenSchema,
    BulkLogoutSchema,
    ChangeEmailSchema
)
from middleware.auth import get_current_session
from migrate_here.models import UserAuth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["Users"])


def _get_access_token(request: Request) -> str:
    """Ambil access token dari Authorization header. Format: Bearer <token>"""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header tidak ditemukan atau format salah. Gunakan: Bearer <token>"
        )
    return auth_header.split(" ", 1)[1]


# =============================================================================
# REGISTRASI
# =============================================================================

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(user_input: RegisterSchema, db: Session = Depends(get_db)):
    logger.debug(f"POST /register → {user_input.email}")
    try:
        new_user = UserService.create_user(db, user_input)
        logger.info(f"POST /register success → user_id: {new_user.id}")
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "success": True,
                "message": "Registrasi berhasil. Silakan cek email untuk kode OTP verifikasi.",
                "data": {
                    "id": new_user.id,
                    "username": new_user.username,
                    "email": new_user.email,
                    "name": new_user.name,
                }
            }
        )
    except HTTPException as e:
        logger.warning(f"POST /register failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /register error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat registrasi.")


@router.post("/register/request-otp", status_code=status.HTTP_200_OK)
def resend_registration_otp(body: RequestOtpSchema, db: Session = Depends(get_db)):
    logger.debug(f"POST /register/request-otp → {body.email}")
    try:
        UserService.request_registration_otp(db, body.email)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"success": True, "message": "Jika email terdaftar dan belum terverifikasi, OTP akan dikirimkan."}
        )
    except HTTPException as e:
        logger.warning(f"POST /register/request-otp failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /register/request-otp error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat mengirim OTP.")


@router.post("/register/verify-otp", status_code=status.HTTP_200_OK)
def verify_registration_otp(otp_input: VerifyOtp, db: Session = Depends(get_db)):
    logger.debug(f"POST /register/verify-otp → {otp_input.email}")
    try:
        UserService.verify_registration_otp(db, otp_input)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"success": True, "message": "Akun berhasil diverifikasi. Silakan login."}
        )
    except HTTPException as e:
        logger.warning(f"POST /register/verify-otp failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /register/verify-otp error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat verifikasi OTP.")


# =============================================================================
# LOGIN
# =============================================================================

@router.post("/login", status_code=status.HTTP_200_OK)
def login(request: Request, login_input: LoginSchema, db: Session = Depends(get_db)):
    logger.debug(f"POST /login → {login_input.identifier}")
    try:
        device_info = request.headers.get("User-Agent", "Unknown Device")[:255]
        auth = UserService.authenticate_user(db, login_input, device_info=device_info)
        logger.info(f"POST /login success → user_id: {auth.user_id}")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "Login berhasil.",
                "data": {
                    "user_id": auth.user_id,
                    "access_token": auth.access_token,
                    "access_token_expires_at": auth.access_token_expires_at.isoformat(),
                    "refresh_token": auth.refresh_token,
                    "refresh_token_expires_at": auth.refresh_token_expires_at.isoformat(),
                    "device_info": auth.device_info,
                }
            }
        )
    except HTTPException as e:
        logger.warning(f"POST /login failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /login error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat login.")

# =============================================================================
# REFRESH TOKEN
# =============================================================================

@router.post("/refresh-token", status_code=status.HTTP_200_OK)
def refresh_token(body: RefreshTokenSchema, db: Session = Depends(get_db)):
    """
    Perbarui access token yang sudah expired menggunakan refresh token.
    Refresh token juga dirotasi dan di-rolling reset 2 minggu.

    Dipanggil secara silent di background oleh frontend (menit ke-25)
    sehingga user tidak merasakan gangguan apapun.

    Body:
        refresh_token (str): refresh token yang didapat saat login
    """
    logger.debug(f"POST /refresh-token → token: {body.refresh_token[:10]}...")
    try:
        auth = UserService.refresh_access_token(db, body.refresh_token)
        logger.info(f"POST /refresh-token success → user_id: {auth.user_id}")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "Access token berhasil diperbarui.",
                "data": {
                    "user_id": auth.user_id,
                    "access_token": auth.access_token,
                    "access_token_expires_at": auth.access_token_expires_at.isoformat(),
                    "refresh_token": auth.refresh_token,
                    "refresh_token_expires_at": auth.refresh_token_expires_at.isoformat(),
                }
            }
        )
    except HTTPException as e:
        logger.warning(f"POST /refresh-token failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /refresh-token error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat memperbarui token.")


# =============================================================================
# SESSION MANAGEMENT
# =============================================================================

@router.get("/sessions", status_code=status.HTTP_200_OK)
def get_active_sessions(
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session)
):
    """
    Lihat semua device yang sedang login.
    Device saat ini ditandai dengan is_current = true.

    Headers:
        Authorization: Bearer <access_token>
    """
    try:
        result = UserService.get_active_sessions(db, current_session.access_token)
        logger.info("GET /sessions success")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "Daftar session aktif berhasil diambil.",
                "data": result,
            }
        )
    except HTTPException as e:
        logger.warning(f"GET /sessions failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"GET /sessions error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat mengambil daftar session.")

# =============================================================================
# LOGOUT
# =============================================================================

@router.post("/logout/logout-selected", status_code=status.HTTP_200_OK)
def logout_selected_devices(
    body: BulkLogoutSchema,
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session)
):
    """
    Logout device-device tertentu berdasarkan session_id (bulk select).
    Device saat ini tidak bisa ikut dipilih — gunakan /logout untuk logout sendiri.
    
    Headers:
        Authorization: Bearer <access_token>
    Body:
        session_ids (list[int]): daftar session_id yang ingin di-logout
    """
    try:
        deleted = UserService.logout_selected_devices(db, current_session.access_token, body.session_ids)
        logger.info(f"POST /sessions/logout-selected success → {deleted} session(s) deleted")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": f"Berhasil logout dari {deleted} device.",
                "data": {"deleted_sessions": deleted}
            }
        )
    except HTTPException as e:
        logger.warning(f"POST /sessions/logout-selected failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /sessions/logout-selected error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat logout device.")

@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(request: Request, db: Session = Depends(get_db)):
    """
    Logout device ini saja — hapus row session dari DB.
    Tetap bisa dilakukan meskipun access token sudah expired.

    Headers:
        Authorization: Bearer <access_token>
    """
    try:
        access_token = _get_access_token(request)
        UserService.logout(db, access_token)
        logger.info("POST /logout success")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"success": True, "message": "Logout berhasil."}
        )
    except HTTPException as e:
        logger.warning(f"POST /logout failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /logout error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat logout.")


@router.post("/logout/other-devices", status_code=status.HTTP_200_OK)
def logout_other_devices(
    db: Session = Depends(get_db),
    current_session: UserAuth = Depends(get_current_session)
):
    """
    Logout semua device lain kecuali device ini.
    Row session device lain dihapus dari DB.

    Headers:
        Authorization: Bearer <access_token>
    """
    try:
        deleted = UserService.logout_other_devices(db, current_session.access_token)
        logger.info(f"POST /logout/other-devices success → {deleted} session(s) deleted")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": f"Berhasil logout dari {deleted} device lain.",
                "data": {"invalidated_sessions": deleted}
            }
        )
    except HTTPException as e:
        logger.warning(f"POST /logout/other-devices failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /logout/other-devices error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat logout.")


# =============================================================================
# LUPA PASSWORD
# =============================================================================

@router.post("/reset-password/request-otp", status_code=status.HTTP_200_OK)
def request_reset_password_otp(body: RequestOtpSchema, db: Session = Depends(get_db)):
    logger.debug(f"POST /reset-password/request-otp → {body.email}")
    try:
        result = UserService.request_reset_password_otp(db, body.email)
        if result["status"] == "not_found":
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "success": True,
                    "message": "Email tidak terdaftar.",
                    "data": {"email_exists": False}
                }
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "OTP berhasil dikirim.",
                "data": {"email_exists": True}
            }
        )
    except HTTPException as e:
        logger.warning(f"POST /reset-password/request-otp failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /reset-password/request-otp error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat mengirim OTP.")

@router.post("/reset-password/verify-otp", status_code=status.HTTP_200_OK)
def verify_reset_password_otp(otp_input: VerifyOtp, db: Session = Depends(get_db)):
    logger.debug(f"POST /reset-password/verify-otp → {otp_input.email}")
    try:
        reset_token = UserService.verify_reset_password_otp(db, otp_input)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={   
                "success": True,
                "message": "OTP berhasil diverifikasi.",
                "data": {"reset_token": reset_token}
            }
        )
    except HTTPException as e:
        logger.warning(f"POST /reset-password/verify-otp failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /reset-password/verify-otp error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat verifikasi OTP.")

@router.post("/reset-password", status_code=status.HTTP_200_OK)
def reset_password(reset_input: ResetPasswordSchema, db: Session = Depends(get_db)):
    """
    Reset password dan otomatis logout semua device yang sedang aktif.
    Semua device harus login ulang setelah password diganti.
    """
    logger.debug(f"POST /reset-password → token: {reset_input.token}")
    try:
        UserService.reset_password(db, reset_input)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"success": True, "message": "Password berhasil direset. Silakan login kembali di semua device."}
        )
    except HTTPException as e:
        logger.warning(f"POST /reset-password failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /reset-password error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat reset password.")
    
# =============================================================================
# CHANGE EMAIL
# =============================================================================

@router.post("/change-email/request-otp", status_code=status.HTTP_200_OK)
def request_change_email_otp(body: RequestOtpSchema, db: Session = Depends(get_db)):
    logger.debug(f"POST /change-email/request-otp → {body.email}")
    try:
        UserService.request_change_email_otp(db, body.email)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"success": True, "message": "Jika email terdaftar, OTP akan dikirimkan."}
        )
    except HTTPException as e:
        logger.warning(f"POST /change-email/request-otp failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /change-email/request-otp error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat mengirim OTP.")
    
@router.post("/change-email/verify-otp", status_code=status.HTTP_200_OK)
def verify_change_email_otp(otp_input: VerifyOtp, db: Session = Depends(get_db)):
    logger.debug(f"POST /change-email/verify-otp → {otp_input.email}")
    try:
        change_token = UserService.verify_change_email_otp(db, otp_input)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "message": "OTP berhasil diverifikasi.",
                "data": {"change_token": change_token}
            }
        )
    except HTTPException as e:
        logger.warning(f"POST /change-email/verify-otp failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /change-email/verify-otp error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat verifikasi OTP.")
    
@router.post("/change-email", status_code=status.HTTP_200_OK)
def change_email(change_input: ChangeEmailSchema, db: Session = Depends(get_db)):
    logger.debug(f"POST /change-email → token: {change_input.token}")
    try:
        UserService.change_email(db, change_input)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"success": True, "message": "Email berhasil diubah"}
        )
    except HTTPException as e:
        logger.warning(f"POST /change-email failed → {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"POST /change-email error → {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Terjadi kesalahan saat mengubah email.")

# =============================================================================
# USER DATA
# =============================================================================

@router.get("/{user_id}", status_code=status.HTTP_200_OK)
def get_user(user_id: int, db: Session = Depends(get_db)):
    logger.debug(f"GET /users/{user_id}")
    user = UserService.get_user_by_id(db, user_id)

    if not user:
        logger.warning(f"GET /users/{user_id} → not found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User tidak ditemukan.")

    logger.info(f"GET /users/{user_id} success")
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "success": True,
            "message": "Data user berhasil diambil.",
            "data": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "name": user.name,
                "is_verified": user.is_verified,
                "is_active": user.is_active,
            }
        }
    )