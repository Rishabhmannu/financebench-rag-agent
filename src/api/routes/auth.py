from fastapi import APIRouter, Depends, HTTPException, status

from src.api.dependencies import get_current_user
from src.config.rbac_config import get_permissions
from src.models.auth import User
from src.models.schemas import (
    LoginRequest,
    TokenResponse,
    UserMeResponse,
    UserPermissions,
)
from src.services.auth_service import create_token

router = APIRouter(prefix="/auth", tags=["auth"])

# Simple in-memory users for development. Replace with a real user store in production.
DEV_USERS = {
    "analyst": {"password": "analyst123", "name": "Test Analyst", "role": "analyst", "department": "Research"},
    "finance": {"password": "finance123", "name": "Test Finance", "role": "finance", "department": "FP&A"},
    "hr": {"password": "hr123", "name": "Test HR", "role": "hr", "department": "Human Resources"},
    "clevel": {"password": "clevel123", "name": "Test CEO", "role": "c_level", "department": "Executive"},
    "admin": {"password": "admin123", "name": "Test Admin", "role": "admin", "department": "IT"},
}


@router.post("/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    user = DEV_USERS.get(request.username)
    if not user or user["password"] != request.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_token(
        user_id=request.username,
        name=user["name"],
        role=user["role"],
        department=user["department"],
    )
    return TokenResponse(
        access_token=token,
        user_id=request.username,
        name=user["name"],
        role=user["role"],
        department=user["department"],
    )


@router.get("/me", response_model=UserMeResponse)
async def me(user: User = Depends(get_current_user)) -> UserMeResponse:
    """Return the current user's identity + role-derived permissions.

    The frontend calls this on app boot (or after a token refresh) to
    rehydrate the user state without keeping JWT payload state-of-truth
    in the client. The permissions block lets the UI gate admin nav,
    upload buttons, HITL approval surfaces, etc., without a separate
    config endpoint.
    """
    perms = get_permissions(user.role)
    return UserMeResponse(
        user_id=user.user_id,
        name=user.name,
        role=user.role,
        department=user.department,
        permissions=UserPermissions(
            allowed_doc_types=perms["allowed_doc_types"],
            allowed_confidentiality=perms["allowed_confidentiality"],
            max_results=perms["max_results"],
            requires_hitl_above=perms.get("requires_hitl_above"),
        ),
    )
