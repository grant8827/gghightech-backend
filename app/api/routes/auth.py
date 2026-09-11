from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, LoginResponse
from app.services.auth import AuthenticatedUser, get_current_user
from app.services.local_auth import create_access_token, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    if not settings.SECRET_KEY:
        raise HTTPException(503, "Local login is not configured on this server")

    user = db.query(User).filter(User.email == payload.email).first()
    # Deliberately identical error for "no such user" and "wrong password" —
    # doesn't tell an attacker which part was wrong.
    if not user or not user.password_hash or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")

    token = create_access_token(str(user.id), user.email, user.role)
    return LoginResponse(access_token=token, role=user.role, email=user.email, full_name=user.full_name)


@router.get("/me")
def me(user: AuthenticatedUser = Depends(get_current_user)) -> dict:
    """Lets the frontend verify a stored token is still valid and see who
    it belongs to, without needing to decode the JWT client-side."""
    return {"email": user.email, "role": user.role}
