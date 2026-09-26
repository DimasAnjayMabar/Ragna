from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Float, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base
import datetime

Base = declarative_base()


# --- 1. TABEL USER (Data Utama) ---
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    name = Column(String(100), nullable=False)
    profile_image_url = Column(String(255), nullable=True)

    # Status akun
    is_verified = Column(Boolean, default=False, nullable=False)  # True setelah verifikasi OTP registrasi
    is_active = Column(Boolean, default=True, nullable=False)     # False jika di-suspend/ban admin

    last_active = Column(DateTime, default=datetime.datetime.utcnow)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    # uselist=True (default) — satu user bisa punya banyak session/device
    auth_sessions = relationship("UserAuth", back_populates="user", cascade="all, delete-orphan")
    registration_otps = relationship("OTPRegistrasi", back_populates="user", cascade="all, delete-orphan")
    reset_password_otps = relationship("OTPResetPassword", back_populates="user", cascade="all, delete-orphan")
    change_email_otps = relationship("OTPChangeEmail", back_populates="user", cascade="all, delete-orphan")
    chats = relationship("Chat", back_populates="owner", cascade="all, delete-orphan")


# --- 2. TABEL AUTH (Token Manajemen — Multiple Device) ---
class UserAuth(Base):
    __tablename__ = "user_auth"

    id = Column(Integer, primary_key=True, index=True)
    # Tidak unique — satu user bisa punya banyak baris (satu per device/session)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    access_token = Column(String(255), unique=True, nullable=True)
    access_token_expires_at = Column(DateTime, nullable=True)

    refresh_token = Column(String(255), unique=True, nullable=True)
    refresh_token_expires_at = Column(DateTime, nullable=True)

    # Informasi device untuk identifikasi session
    # Diisi dari User-Agent header request login
    device_info = Column(String(255), nullable=True)  # e.g. "Chrome/Windows", "Mozilla/iPhone"

    is_active = Column(Boolean, default=True, nullable=False)  # False = sudah logout
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="auth_sessions")


# --- 3. TABEL OTP REGISTRASI ---
class OTPRegistrasi(Base):
    __tablename__ = "otp_registrasi"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Gunakan String(6) agar kode seperti 001234 tidak terbaca sebagai 1234
    otp = Column(String(6), nullable=False)
    otp_expires_at = Column(DateTime, nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)

    # Untuk invalidate OTP lama & rate limiting
    is_invalidated = Column(Boolean, default=False, nullable=False)

    # Urutan request OTP hari ini — untuk exponential backoff & limit harian
    request_count_today = Column(Integer, default=1, nullable=False)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="registration_otps")


# --- 4. TABEL OTP RESET PASSWORD ---
class OTPResetPassword(Base):
    __tablename__ = "otp_reset_password"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Gunakan String(6) agar kode seperti 001234 tidak terbaca sebagai 1234
    otp = Column(String(6), nullable=False)
    otp_expires_at = Column(DateTime, nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)

    # Untuk invalidate OTP lama & rate limiting
    is_invalidated = Column(Boolean, default=False, nullable=False)

    # Urutan request OTP hari ini — untuk exponential backoff & limit harian
    request_count_today = Column(Integer, default=1, nullable=False)

    reset_token = Column(String(255), unique=True, nullable=True)
    reset_token_expires_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="reset_password_otps")

# --- 4. TABEL OTP CHANGE EMAIL ---
class OTPChangeEmail(Base): 
    __tablename__ = "otp_change_email"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Gunakan String(6) agar kode seperti 001234 tidak terbaca sebagai 1234
    otp = Column(String(6), nullable=False)
    otp_expires_at = Column(DateTime, nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)

    # Untuk invalidate OTP lama & rate limiting
    is_invalidated = Column(Boolean, default=False, nullable=False)

    # Urutan request OTP hari ini — untuk exponential backoff & limit harian
    request_count_today = Column(Integer, default=1, nullable=False)

    change_token = Column(String(255), unique=True, nullable=True)
    change_token_expires_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="change_email_otps")

# --- 5. TABEL CHAT (HEADER) ---
class Chat(Base):
    __tablename__ = "chats"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(Text, nullable=False)  # Judul chat otomatis dari 5 kata pertama
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Relationships
    owner = relationship("User", back_populates="chats")
    details = relationship("ChatDetail", back_populates="chat", cascade="all, delete-orphan")


# --- 6. TABEL CHAT DETAILS (ISI PERCAKAPAN) ---
class ChatDetail(Base):
    __tablename__ = "chat_details"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(Integer, ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    question = Column(Text, nullable=False)
    response = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    processing_status = Column(String(10), nullable=False, default="done")

    # Relationships
    chat = relationship("Chat", back_populates="details")
    # uselist=False karena 1 pesan punya 1 log pipeline
    pipeline_log = relationship("PipelineLog", back_populates="chat_detail", uselist=False, cascade="all, delete-orphan")


# --- 7. TABEL PIPELINE LOG (MONITORING AI) ---
class PipelineLog(Base):
    __tablename__ = "pipeline_logs"

    id = Column(Integer, primary_key=True, index=True)
    chat_detail_id = Column(Integer, ForeignKey("chat_details.id", ondelete="CASCADE"), nullable=False)

    # Metrik Performa
    latency_ms = Column(Float)          # Kecepatan respon AI dalam milidetik
    status = Column(String(20))           # 'success' atau 'failed'
    error_message = Column(Text, nullable=True)

    # Metrik Token (Penting jika pakai OpenAI/LLM berbayar)
    input_tokens = Column(Float, default=0)
    output_tokens = Column(Float, default=0)
    total_cost = Column(Float, default=0.0)  # Opsional: hitung biaya API

    # Relationships
    chat_detail = relationship("ChatDetail", back_populates="pipeline_log")

class Documents(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True, index=True)
    hash_value = Column(Text, unique=True, index=True)