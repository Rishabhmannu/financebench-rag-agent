from fastapi import APIRouter

from src.models.schemas import LoginRequest, TokenResponse
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
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_token(
        user_id=request.username,
        name=user["name"],
        role=user["role"],
        department=user["department"],
    )
    return TokenResponse(access_token=token, role=user["role"])
