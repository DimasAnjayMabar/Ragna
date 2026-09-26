# Buat file baru: dependencies/auth.py

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from database import get_db
from migrate_here.models import UserAuth, User
from datetime import datetime


def get_current_session(request: Request, db: Session = Depends(get_db)) -> UserAuth:
    """
    Dependency untuk validasi access token.
    Dipakai di semua endpoint yang butuh user sudah login.

    - Cek Authorization header
    - Cek token ada di DB
    - Cek token belum expired
    - Cek user masih aktif (is_active)

    Cara pakai di router:
        from dependencies.auth import get_current_session, get_current_user
        from models import UserAuth, User

        @router.get("/something")
        def something(session: UserAuth = Depends(get_current_session)):
            ...

        # Atau kalau butuh data user langsung:
        @router.get("/something")
        def something(user: User = Depends(get_current_user)):
            ...
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header tidak ditemukan atau format salah. Gunakan: Bearer <token>"
        )

    access_token = auth_header.split(" ", 1)[1]

    # Cek token di DB + validasi expiry sekaligus
    session = db.query(UserAuth).filter(
        UserAuth.access_token == access_token,
        UserAuth.access_token_expires_at > datetime.utcnow()
    ).first()

    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access token tidak valid atau sudah expired. Silakan refresh token."
        )

    # Cek user masih aktif
    if not session.user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Akun Anda telah dinonaktifkan. Hubungi admin."
        )

    return session


def get_current_user(session: UserAuth = Depends(get_current_session)) -> User:
    """
    Dependency shortcut untuk langsung dapat objek User.
    Gunakan ini kalau endpoint butuh data user, bukan data session.
    """
    return session.user