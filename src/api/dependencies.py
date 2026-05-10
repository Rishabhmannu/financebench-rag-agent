from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.models.auth import User
from src.services.auth_service import decode_token
from src.services.request_context import current_user_id

security = HTTPBearer()


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> User:
    """FastAPI dependency: extract and validate JWT from Authorization header.

    Side effect (Sprint 8 8d): pin the authenticated user id into a request-scoped
    `ContextVar` so downstream LLM calls can tag traces with it. The contextvar
    is bound inside this async task and isolates per-request state automatically.
    """
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authorization header")
    user = decode_token(credentials.credentials)
    current_user_id.set(user.user_id)
    return user
