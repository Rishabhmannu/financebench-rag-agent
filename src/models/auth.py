from pydantic import BaseModel


class User(BaseModel):
    """Authenticated user representation."""

    user_id: str
    name: str
    role: str  # "analyst", "finance", "hr", "c_level", "admin"
    department: str = ""
