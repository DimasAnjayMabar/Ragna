from sqlalchemy.orm import Session
from sqlalchemy import or_
from datetime import datetime, timedelta
import secrets
import string
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from passlib.context import CryptContext
from migrate_here.models import User, UserAuth, OTPRegistrasi, OTPResetPassword, OTPChangeEmail
from validation.users import (
    RegisterSchema,
    LoginSchema,
    VerifyOtp,
    ResetPasswordSchema,
    ChangeEmailSchema
)
from database import settings
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

OTP_EXPIRES_MINUTES          = 10
OTP_MAX_PER_DAY              = 5
RESET_TOKEN_EXPIRES_MINUTES  = 15
ACCESS_TOKEN_EXPIRES_MINUTES = 30
REFRESH_TOKEN_EXPIRES_DAYS   = 14
MAX_ACTIVE_SESSIONS          = 5   # Maksimum device yang boleh login bersamaan


class UserService:
    # -------------------------------------------------------------------------
    # HELPER — OTP & RATE LIMITING
    # -------------------------------------------------------------------------

    @staticmethod
    def _generate_otp(length=6) -> str:
        characters = string.ascii_uppercase + string.digits
        return ''.join(secrets.choice(characters) for _ in range(length))

    @staticmethod
    def _get_otp_count_today(db: Session, model, user_id: int) -> int:
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        return db.query(model).filter(
            model.user_id == user_id,
            model.created_at >= today_start
        ).count()

    @staticmethod
    def _check_otp_rate_limit(db: Session, model, user_id: int, last_otp):
        count_today = UserService._get_otp_count_today(db, model, user_id)

        if count_today >= OTP_MAX_PER_DAY:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Batas maksimum {OTP_MAX_PER_DAY} permintaan OTP per hari telah tercapai. Coba lagi besok."
            )

        if last_otp and count_today > 0:
            cooldown_seconds = count_today * 60
            elapsed = (datetime.utcnow() - last_otp.created_at).total_seconds()
            if elapsed < cooldown_seconds:
                wait = int(cooldown_seconds - elapsed)
                minutes, seconds = divmod(wait, 60)
                wait_str = f"{minutes} menit {seconds} detik" if minutes else f"{seconds} detik"
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Tunggu {wait_str} sebelum meminta OTP baru."
                )

    @staticmethod
    def _invalidate_previous_otps(db: Session, model, user_id: int):
        db.query(model).filter(
            model.user_id == user_id,
            model.is_used == False,
            model.is_invalidated == False
        ).update({"is_invalidated": True})

    @staticmethod
    def _get_last_otp(db: Session, model, user_id: int):
        return (
            db.query(model)
            .filter(model.user_id == user_id)
            .order_by(model.created_at.desc())
            .first()
        )

    # -------------------------------------------------------------------------
    # HELPER — SESSION
    # -------------------------------------------------------------------------

    @staticmethod
    def _generate_tokens() -> tuple[str, str]:
        """Generate sepasang access token dan refresh token baru."""
        return secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    @staticmethod
    def _get_session_by_access_token(db: Session, access_token: str) -> UserAuth | None:
        """
        Cari session berdasarkan access token yang masih valid.
        Dipakai untuk validasi request API biasa.
        """
        return db.query(UserAuth).filter(
            UserAuth.access_token == access_token,
            UserAuth.access_token_expires_at > datetime.utcnow()
        ).first()

    @staticmethod
    def _get_session_by_access_token_any(db: Session, access_token: str) -> UserAuth | None:
        """
        Cari session berdasarkan access token TANPA cek expiry.
        Khusus untuk logout — agar tetap bisa logout meski token expired.
        """
        return db.query(UserAuth).filter(
            UserAuth.access_token == access_token,
        ).first()

    @staticmethod
    def _get_session_by_refresh_token(db: Session, refresh_token: str) -> UserAuth | None:
        """
        Cari session berdasarkan refresh token yang masih valid.
        Dipakai untuk refresh access token.
        """
        return db.query(UserAuth).filter(
            UserAuth.refresh_token == refresh_token,
            UserAuth.refresh_token_expires_at > datetime.utcnow()
        ).first()

    @staticmethod
    def _count_active_sessions(db: Session, user_id: int) -> int:
        """
        Hitung jumlah session aktif milik user.
        Karena row = session aktif, cukup COUNT tanpa filter tambahan.
        """
        return db.query(UserAuth).filter(UserAuth.user_id == user_id).count()

    @staticmethod
    def _delete_all_sessions(db: Session, user_id: int):
        """
        Hapus semua session milik user.
        Dipakai saat: ganti password, akun di-suspend.
        """
        db.query(UserAuth).filter(UserAuth.user_id == user_id).delete()

    # -------------------------------------------------------------------------
    # HELPER — EMAIL
    # -------------------------------------------------------------------------

    @staticmethod
    def _build_otp_email_html(otp_code: str, title: str, subtitle: str, expires_minutes: int) -> str:
        return f"""
<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f6f8;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f8;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="520" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:12px;overflow:hidden;
                      box-shadow:0 2px 12px rgba(0,0,0,0.08);">
          <tr>
            <td align="center"
                style="background:linear-gradient(135deg,#2d6a4f,#52b788);
                       padding:36px 40px 28px;">
              <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;">🌿 AgriBot</h1>
              <p style="margin:8px 0 0;color:#d8f3dc;font-size:13px;">{subtitle}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:36px 40px 24px;">
              <p style="margin:0 0 8px;color:#333333;font-size:15px;">Halo,</p>
              <p style="margin:0 0 24px;color:#555555;font-size:14px;line-height:1.6;">
                Gunakan kode OTP berikut untuk {title.lower()}.
                Kode ini hanya berlaku selama <strong>{expires_minutes} menit</strong>
                dan tidak boleh dibagikan kepada siapapun.
              </p>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center" style="padding:8px 0 28px;">
                    <div style="display:inline-block;background:#f0faf4;border:2px dashed #52b788;
                                border-radius:10px;padding:18px 48px;">
                      <span style="font-size:36px;font-weight:800;letter-spacing:10px;
                                   color:#2d6a4f;font-family:'Courier New',monospace;">
                        {otp_code}
                      </span>
                    </div>
                  </td>
                </tr>
              </table>
              <p style="margin:0;color:#888888;font-size:12px;line-height:1.6;">
                Jika kamu tidak merasa melakukan permintaan ini, abaikan email ini.
                Akun kamu tetap aman.
              </p>
            </td>
          </tr>
          <tr>
            <td style="background:#f9fafb;padding:18px 40px;border-top:1px solid #eeeeee;">
              <p style="margin:0;color:#aaaaaa;font-size:11px;text-align:center;">
                © 2025 AgriBot · Email ini dikirim otomatis, mohon tidak membalas.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    @staticmethod
    def _send_otp_email(to_email: str, to_name: str, otp_code: str, purpose: str) -> bool:
        if purpose == "registrasi":
            subject  = "Kode OTP Verifikasi akun AgriBot"
            title    = "Verifikasi Akun"
            subtitle = "Konfirmasi alamat email kamu"
        elif purpose == "reset_password":
            subject  = "Kode OTP Reset Password akun AgriBot"
            title    = "Reset Password"
            subtitle = "Permintaan penggantian password"
        elif purpose == "change_email":
            subject = "Kode OTP change email akun Agribot"
            title = "Change Email"
            subtitle = "Penggantian alamat email"

        html_body = UserService._build_otp_email_html(otp_code, title, subtitle, OTP_EXPIRES_MINUTES)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"AgriBot <{settings.MAIL_FROM}>"
        msg["To"]      = f"{to_name} <{to_email}>"
        msg.attach(MIMEText(html_body, "html"))

        try:
            with smtplib.SMTP(settings.MAIL_HOST, settings.MAIL_PORT) as server:
                server.ehlo()
                server.starttls()
                server.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
                server.sendmail(settings.MAIL_FROM, to_email, msg.as_string())
            logger.info(f"Email OTP [{purpose}] terkirim → {to_email}")
            return True
        except smtplib.SMTPAuthenticationError:
            logger.error(f"Email OTP gagal → autentikasi SMTP gagal untuk {to_email}")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"Email OTP gagal → SMTP error: {e}")
            return False
        except Exception as e:
            logger.error(f"Email OTP gagal → unexpected error: {e}")
            return False

    @staticmethod
    def _build_notification_email_html(title: str, body_text: str) -> str:
        return f"""
<!DOCTYPE html>
<html lang="id">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f6f8;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f6f8;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="520" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border-radius:12px;overflow:hidden;
                      box-shadow:0 2px 12px rgba(0,0,0,0.08);">
          <tr>
            <td align="center"
                style="background:linear-gradient(135deg,#2d6a4f,#52b788);
                       padding:36px 40px 28px;">
              <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;">🌿 AgriBot</h1>
              <p style="margin:8px 0 0;color:#d8f3dc;font-size:13px;">{title}</p>
            </td>
          </tr>
          <tr>
            <td style="padding:36px 40px 24px;">
              <p style="margin:0 0 16px;color:#333333;font-size:15px;">Halo,</p>
              <p style="margin:0 0 24px;color:#555555;font-size:14px;line-height:1.7;">
                {body_text}
              </p>
              <p style="margin:0;color:#888888;font-size:12px;line-height:1.6;">
                Jika kamu tidak merasa melakukan perubahan ini, segera hubungi tim support AgriBot.
              </p>
            </td>
          </tr>
          <tr>
            <td style="background:#f9fafb;padding:18px 40px;border-top:1px solid #eeeeee;">
              <p style="margin:0;color:#aaaaaa;font-size:11px;text-align:center;">
                © 2025 AgriBot · Email ini dikirim otomatis, mohon tidak membalas.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""
 
    @staticmethod
    def _send_notification_email(to_email: str, to_name: str, subject: str, title: str, body_text: str) -> bool:
        html_body = UserService._build_notification_email_html(title, body_text)
 
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"AgriBot <{settings.MAIL_FROM}>"
        msg["To"]      = f"{to_name} <{to_email}>"
        msg.attach(MIMEText(html_body, "html"))
 
        try:
            with smtplib.SMTP(settings.MAIL_HOST, settings.MAIL_PORT) as server:
                server.ehlo()
                server.starttls()
                server.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
                server.sendmail(settings.MAIL_FROM, to_email, msg.as_string())
            logger.info(f"Notification email terkirim → {to_email}")
            return True
        except smtplib.SMTPAuthenticationError:
            logger.error(f"Notification email gagal → autentikasi SMTP gagal untuk {to_email}")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"Notification email gagal → SMTP error: {e}")
            return False
        except Exception as e:
            logger.error(f"Notification email gagal → unexpected error: {e}")
            return False

    # -------------------------------------------------------------------------
    # REGISTRASI
    # -------------------------------------------------------------------------

    @staticmethod
    def create_user(db: Session, user_input: RegisterSchema):
        logger.debug(f"Register attempt → email: {user_input.email}, username: {user_input.username}")

        existing_user = db.query(User).filter(
            or_(User.email == user_input.email, User.username == user_input.username)
        ).first()

        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username atau Email sudah terdaftar."
            )

        hashed_pwd = pwd_context.hash(user_input.password)
        new_user = User(
            username=user_input.username,
            email=user_input.email,
            hashed_password=hashed_pwd,
            name=user_input.name,
            is_verified=False,
            is_active=True,
            created_at=datetime.utcnow()
        )
        db.add(new_user)
        db.flush()

        otp_code = UserService._generate_otp()
        db.add(OTPRegistrasi(
            user_id=new_user.id,
            otp=otp_code,
            otp_expires_at=datetime.utcnow() + timedelta(minutes=OTP_EXPIRES_MINUTES),
            request_count_today=1,
        ))
        db.commit()
        db.refresh(new_user)

        sent = UserService._send_otp_email(new_user.email, new_user.name, otp_code, "registrasi")
        if not sent:
            logger.warning(f"Register OTP email gagal → user_id: {new_user.id}")

        logger.info(f"Register success → user_id: {new_user.id}")
        return new_user

    @staticmethod
    def request_registration_otp(db: Session, email: str):
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return True
        if user.is_verified:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Akun sudah terverifikasi.")

        last_otp = UserService._get_last_otp(db, OTPRegistrasi, user.id)
        UserService._check_otp_rate_limit(db, OTPRegistrasi, user.id, last_otp)
        UserService._invalidate_previous_otps(db, OTPRegistrasi, user.id)

        count_today = UserService._get_otp_count_today(db, OTPRegistrasi, user.id)
        otp_code = UserService._generate_otp()
        db.add(OTPRegistrasi(
            user_id=user.id,
            otp=otp_code,
            otp_expires_at=datetime.utcnow() + timedelta(minutes=OTP_EXPIRES_MINUTES),
            request_count_today=count_today + 1,
        ))
        db.commit()

        sent = UserService._send_otp_email(user.email, user.name, otp_code, "registrasi")
        if not sent:
            logger.warning(f"Resend registration OTP gagal → user_id: {user.id}")

        return True

    @staticmethod
    def verify_registration_otp(db: Session, otp_input: VerifyOtp):
        user = db.query(User).filter(User.email == otp_input.email).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User tidak ditemukan.")
        if user.is_verified:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Akun sudah terverifikasi.")

        otp_entry = db.query(OTPRegistrasi).filter(
            OTPRegistrasi.user_id == user.id,
            OTPRegistrasi.otp == otp_input.otp,
            OTPRegistrasi.is_used == False,
            OTPRegistrasi.is_invalidated == False,
            OTPRegistrasi.otp_expires_at > datetime.utcnow()
        ).first()

        if not otp_entry:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP tidak valid atau sudah kadaluarsa.")

        user.is_verified = True

        # Hapus semua row OTP registrasi milik user — tidak diperlukan lagi
        db.query(OTPRegistrasi).filter(OTPRegistrasi.user_id == user.id).delete()

        db.commit()

        logger.info(f"Registration OTP verified → user_id: {user.id}, semua OTP registrasi dihapus")
        return True

    # -------------------------------------------------------------------------
    # LOGIN — Multiple Device + Max Session
    # -------------------------------------------------------------------------

    @staticmethod
    def authenticate_user(db: Session, login_input: LoginSchema, device_info: str = None):
        """
        Login user dan buat session baru.
        - Mendukung multiple device (maks MAX_ACTIVE_SESSIONS)
        - Jika sudah maks, login device baru ditolak
        - Setiap row di user_auth = 1 session aktif
        """
        logger.debug(f"Login attempt → identifier: {login_input.identifier}")

        user = db.query(User).filter(
            or_(User.username == login_input.identifier, User.email == login_input.identifier)
        ).first()

        if not user or not pwd_context.verify(login_input.password, user.hashed_password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Username/email atau password salah.")

        if not user.is_verified:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun belum diverifikasi. Silakan cek email untuk OTP.")

        if not user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun Anda telah dinonaktifkan. Hubungi admin.")

        # Cek batas maksimum session
        active_count = UserService._count_active_sessions(db, user.id)
        if active_count >= MAX_ACTIVE_SESSIONS:
            logger.warning(f"Login ditolak → user_id: {user.id} sudah memiliki {active_count} session aktif")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Batas maksimum {MAX_ACTIVE_SESSIONS} device tercapai. "
                       f"Silakan logout dari salah satu device terlebih dahulu."
            )

        access_token, refresh_token = UserService._generate_tokens()

        new_session = UserAuth(
            user_id=user.id,
            access_token=access_token,
            access_token_expires_at=datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRES_MINUTES),
            refresh_token=refresh_token,
            refresh_token_expires_at=datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRES_DAYS),
            device_info=device_info,
        )
        db.add(new_session)
        user.last_active = datetime.utcnow()
        db.commit()
        db.refresh(new_session)

        logger.info(f"Login success → user_id: {user.id}, device: {device_info}, active sessions: {active_count + 1}")
        return new_session

    # -------------------------------------------------------------------------
    # REFRESH TOKEN
    # -------------------------------------------------------------------------

    @staticmethod
    def refresh_access_token(db: Session, refresh_token: str):
        """
        Perbarui access token menggunakan refresh token.
        - Validasi refresh token dari DB
        - Generate access token + refresh token baru (rotation + rolling 2 minggu)
        - Dipanggil secara silent di background oleh frontend (menit ke-25)
        """
        logger.debug(f"Refresh token attempt → token: {refresh_token[:10]}...")

        session = UserService._get_session_by_refresh_token(db, refresh_token)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Refresh token tidak valid atau sudah expired. Silakan login kembali."
            )

        # Cek apakah user masih aktif
        user = db.query(User).filter(User.id == session.user_id).first()
        if not user or not user.is_active:
            # Hapus session ini sekalian
            db.delete(session)
            db.commit()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun telah dinonaktifkan. Hubungi admin.")

        # Update token di row yang sama (bukan buat row baru)
        new_access_token, new_refresh_token = UserService._generate_tokens()

        session.access_token             = new_access_token
        session.access_token_expires_at  = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRES_MINUTES)
        session.refresh_token            = new_refresh_token
        session.refresh_token_expires_at = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRES_DAYS)
        user.last_active                 = datetime.utcnow()
        db.commit()
        db.refresh(session)

        logger.info(f"Token refreshed → user_id: {session.user_id}, device: {session.device_info}")
        return session

    # -------------------------------------------------------------------------
    # SESSION MANAGEMENT
    # -------------------------------------------------------------------------

    @staticmethod
    def get_active_sessions(db: Session, access_token: str) -> dict:
        """
        Ambil semua session aktif milik user.
        Device saat ini ditandai dengan flag is_current = True.
        """
        current_session = UserService._get_session_by_access_token_any(db, access_token)
        if not current_session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session tidak ditemukan atau sudah tidak aktif.")

        all_sessions = db.query(UserAuth).filter(
            UserAuth.user_id == current_session.user_id
        ).order_by(UserAuth.created_at.desc()).all()

        return {
            "sessions": [
                {
                    "session_id": s.id,
                    "device_info": s.device_info,
                    "created_at": s.created_at.isoformat(),
                    "access_token_expires_at": s.access_token_expires_at.isoformat(),
                    "refresh_token_expires_at": s.refresh_token_expires_at.isoformat(),
                    "is_current": s.id == current_session.id,
                }
                for s in all_sessions
            ],
            "total": len(all_sessions),
        }

    @staticmethod
    def logout_selected_devices(db: Session, access_token: str, session_ids: list[int]) -> int:
        """
        Logout device-device tertentu berdasarkan session_id.
        Device saat ini tidak bisa ikut di-logout via endpoint ini —
        gunakan endpoint /logout biasa untuk logout device sendiri.
        """
        current_session = UserService._get_session_by_access_token_any(db, access_token)
        if not current_session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session tidak ditemukan atau sudah tidak aktif.")

        # Cegah user logout device sendiri via bulk logout
        if current_session.id in session_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tidak bisa logout device yang sedang digunakan via bulk logout. Gunakan endpoint /logout."
            )

        deleted = db.query(UserAuth).filter(
            UserAuth.user_id == current_session.user_id,
            UserAuth.id.in_(session_ids),
        ).delete(synchronize_session=False)

        db.commit()

        logger.info(f"Logout selected devices → user_id: {current_session.user_id}, deleted: {deleted} session(s)")
        return deleted

    # -------------------------------------------------------------------------
    # LOGOUT
    # -------------------------------------------------------------------------

    @staticmethod
    def logout(db: Session, access_token: str):
        """
        Logout device ini saja — hapus row session dari DB.
        Tetap bisa dilakukan meskipun access token sudah expired.
        """
        logger.debug(f"Logout attempt → token: {access_token[:10]}...")

        session = UserService._get_session_by_access_token_any(db, access_token)
        if not session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session tidak ditemukan atau sudah tidak aktif.")

        user_id     = session.user_id
        device_info = session.device_info

        db.delete(session)
        db.commit()

        logger.info(f"Logout success → user_id: {user_id}, device: {device_info}")
        return True

    @staticmethod
    def logout_other_devices(db: Session, access_token: str):
        """
        Logout semua device KECUALI device ini.
        Row session device lain dihapus dari DB.
        """
        logger.debug(f"Logout other devices → token: {access_token[:10]}...")

        current_session = UserService._get_session_by_access_token_any(db, access_token)
        if not current_session:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session tidak ditemukan atau sudah tidak aktif.")

        deleted = db.query(UserAuth).filter(
            UserAuth.user_id == current_session.user_id,
            UserAuth.id != current_session.id,
        ).delete()

        db.commit()

        logger.info(f"Logout other devices → user_id: {current_session.user_id}, deleted: {deleted} session(s)")
        return deleted

    # -------------------------------------------------------------------------
    # LUPA PASSWORD
    # -------------------------------------------------------------------------

    @staticmethod
    def request_reset_password_otp(db: Session, email: str):
        user = db.query(User).filter(User.email == email).first()
        if not user:
            # Kembalikan status khusus untuk memberi tahu frontend
            return {"status": "not_found", "message": "Email tidak terdaftar"}
        if not user.is_verified:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun belum diverifikasi.")

        last_otp = UserService._get_last_otp(db, OTPResetPassword, user.id)
        UserService._check_otp_rate_limit(db, OTPResetPassword, user.id, last_otp)
        UserService._invalidate_previous_otps(db, OTPResetPassword, user.id)

        count_today = UserService._get_otp_count_today(db, OTPResetPassword, user.id)
        otp_code = UserService._generate_otp()
        db.add(OTPResetPassword(
            user_id=user.id,
            otp=otp_code,
            otp_expires_at=datetime.utcnow() + timedelta(minutes=OTP_EXPIRES_MINUTES),
            request_count_today=count_today + 1,
        ))
        db.commit()

        sent = UserService._send_otp_email(user.email, user.name, otp_code, "reset_password")
        if not sent:
            logger.warning(f"Reset OTP email gagal → user_id: {user.id}")

        return {"status": "success", "message": "OTP berhasil dikirim"}

    @staticmethod
    def verify_reset_password_otp(db: Session, otp_input: VerifyOtp):
        user = db.query(User).filter(User.email == otp_input.email).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User tidak ditemukan.")

        otp_entry = db.query(OTPResetPassword).filter(
            OTPResetPassword.user_id == user.id,
            OTPResetPassword.otp == otp_input.otp,
            OTPResetPassword.is_used == False,
            OTPResetPassword.is_invalidated == False,
            OTPResetPassword.otp_expires_at > datetime.utcnow()
        ).first()

        if not otp_entry:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP tidak valid atau sudah kadaluarsa.")

        reset_token = secrets.token_urlsafe(32)
        otp_entry.is_used                = True
        otp_entry.reset_token            = reset_token
        otp_entry.reset_token_expires_at = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_EXPIRES_MINUTES)
        db.commit()

        logger.info(f"Reset OTP verified → user_id: {user.id}")
        return reset_token

    @staticmethod
    def reset_password(db: Session, reset_input: ResetPasswordSchema):
        """
        Reset password dan hapus SEMUA session aktif.
        Semua device yang sedang login akan dipaksa login ulang.
        """
        otp_entry = db.query(OTPResetPassword).filter(
            OTPResetPassword.reset_token == reset_input.token,
            OTPResetPassword.is_used == True,
            OTPResetPassword.reset_token_expires_at > datetime.utcnow()
        ).first()

        if not otp_entry:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Reset token tidak valid atau sudah kadaluarsa.")

        user = db.query(User).filter(User.id == otp_entry.user_id).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User tidak ditemukan.")

        # Cek apakah password baru sama dengan password lama
        if pwd_context.verify(reset_input.new_password, user.hashed_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password baru tidak boleh sama dengan password sebelumnya."
            )

        user.hashed_password = pwd_context.hash(reset_input.new_password)

        # Hapus semua session — semua device harus login ulang
        UserService._delete_all_sessions(db, user.id)

        # Hapus semua row OTP reset password milik user — tidak diperlukan lagi
        db.query(OTPResetPassword).filter(OTPResetPassword.user_id == user.id).delete()

        db.commit()

        logger.info(f"Password reset success → user_id: {user.id}, semua session dan OTP reset dihapus")
        return True

    # -------------------------------------------------------------------------
    # CHANGE EMAIL
    # -------------------------------------------------------------------------
    @staticmethod
    def request_change_email_otp(db: Session, email: str):
        user = db.query(User).filter(User.email == email).first()
        if not user:
            return True
        if not user.is_verified:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Akun belum diverifikasi.")

        last_otp = UserService._get_last_otp(db, OTPChangeEmail, user.id)
        UserService._check_otp_rate_limit(db, OTPChangeEmail, user.id, last_otp)
        UserService._invalidate_previous_otps(db, OTPChangeEmail, user.id)

        count_today = UserService._get_otp_count_today(db, OTPChangeEmail, user.id)
        otp_code = UserService._generate_otp()
        db.add(OTPChangeEmail(
            user_id=user.id,
            otp=otp_code,
            otp_expires_at=datetime.utcnow() + timedelta(minutes=OTP_EXPIRES_MINUTES),
            request_count_today=count_today + 1,
        ))
        db.commit()

        sent = UserService._send_otp_email(user.email, user.name, otp_code, "change_email")
        if not sent:
            logger.warning(f"Change email OTP email gagal → user_id: {user.id}")

        return True
    
    @staticmethod
    def verify_change_email_otp(db: Session, otp_input: VerifyOtp):
        user = db.query(User).filter(User.email == otp_input.email).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User tidak ditemukan.")

        otp_entry = db.query(OTPChangeEmail).filter(
            OTPChangeEmail.user_id == user.id,
            OTPChangeEmail.otp == otp_input.otp,
            OTPChangeEmail.is_used == False,
            OTPChangeEmail.is_invalidated == False,
            OTPChangeEmail.otp_expires_at > datetime.utcnow()
        ).first()

        if not otp_entry:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP tidak valid atau sudah kadaluarsa.")

        change_token = secrets.token_urlsafe(32)
        otp_entry.is_used                = True
        otp_entry.change_token            = change_token
        otp_entry.change_token_expires_at = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_EXPIRES_MINUTES)
        db.commit()

        logger.info(f"Change email OTP verified → user_id: {user.id}")
        return change_token
    
    @staticmethod
    def change_email(db: Session, change_input: ChangeEmailSchema):
        """
        Ganti email user setelah verifikasi change_email token.
        Kirim notifikasi ke alamat email LAMA setelah perubahan berhasil.
        """
        otp_entry = db.query(OTPChangeEmail).filter(
            OTPChangeEmail.change_token == change_input.token,
            OTPChangeEmail.is_used == True,
            OTPChangeEmail.change_token_expires_at > datetime.utcnow()
        ).first()
 
        if not otp_entry:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Change email token tidak valid atau sudah kadaluarsa."
            )
 
        user = db.query(User).filter(User.id == otp_entry.user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User tidak ditemukan."
            )
 
        if change_input.new_email == user.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email baru tidak boleh sama dengan email sebelumnya"
            )
 
        existing = db.query(User).filter(User.email == change_input.new_email).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email sudah digunakan di akun lain"
            )
 
        old_email = user.email  # simpan sebelum diubah untuk notifikasi
        user.email = change_input.new_email
 
        # Hapus semua OTP change email milik user
        db.query(OTPChangeEmail).filter(OTPChangeEmail.user_id == user.id).delete()
 
        db.commit()
 
        logger.info(f"Change email success → user_id: {user.id}, {old_email} → {change_input.new_email}")
 
        # Kirim notifikasi ke email LAMA — non-blocking, tidak pengaruhi response
        sent = UserService._send_notification_email(
            to_email  = old_email,
            to_name   = user.name,
            subject   = "Notifikasi Perubahan Email Akun AgriBot",
            title     = "Email Akun Diubah",
            body_text = (
                f"Email akun AgriBot kamu telah berhasil diubah dari "
                f"<strong>{old_email}</strong> menjadi "
                f"<strong>{change_input.new_email}</strong>.<br><br>"
                f"Jika kamu tidak merasa melakukan perubahan ini, segera hubungi tim support AgriBot."
            ),
        )
        if not sent:
            logger.warning(f"Notifikasi email lama gagal dikirim → {old_email}")
 
        return True

    # -------------------------------------------------------------------------
    # UTILITY
    # -------------------------------------------------------------------------

    @staticmethod
    def get_user_by_id(db: Session, user_id: int):
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.warning(f"Get user failed → user_id {user_id} not found")
        return user