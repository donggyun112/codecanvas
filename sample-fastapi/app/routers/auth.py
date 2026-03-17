"""Authentication routes."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_db
from app.services.auth_service import AuthService
from app.schemas import LoginRequest, LoginResponse, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    db=Depends(get_db),
):
    """Authenticate user and return tokens."""
    service = AuthService(db)
    user = await service.verify_user(body.email, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    tokens = await service.issue_tokens(user)
    return LoginResponse(user=user, **tokens)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    token: str,
    db=Depends(get_db),
):
    """Refresh an expired access token."""
    service = AuthService(db)
    new_tokens = await service.refresh_access_token(token)
    if new_tokens is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    return new_tokens


@router.post("/logout")
async def logout(
    token: str,
    db=Depends(get_db),
):
    """Revoke refresh token."""
    service = AuthService(db)
    await service.revoke_token(token)
    return {"status": "ok"}
