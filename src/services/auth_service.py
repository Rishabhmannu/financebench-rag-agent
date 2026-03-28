from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, status

from src.config.settings import settings
from src.models.auth import User


def create_token(user_id: str, name: str, role: str, department: str = "") -> str:
    """Create a JWT token for a user."""
    payload = {
        "sub": user_id,
        "name": name,
        "role": role,
        "department": department,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRY_HOURS),
        "iss": "rag-agent-auth",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> User:
    """Decode and validate a JWT token. Returns User on success, raises HTTPException on failure."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return User(
            user_id=payload["sub"],
            name=payload.get("name", ""),
            role=payload.get("role", "analyst"),
            department=payload.get("department", ""),
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
