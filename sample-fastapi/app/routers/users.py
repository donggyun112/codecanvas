"""User management routes."""
from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_db, get_current_user
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def get_me(current_user=Depends(get_current_user)):
    """Get current user profile."""
    return current_user


@router.get("/{user_id}")
async def get_user(
    user_id: int,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get user by ID."""
    service = UserService(db)
    user = await service.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.put("/{user_id}")
async def update_user(
    user_id: int,
    db=Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update user profile."""
    service = UserService(db)
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    updated = await service.update_user(user_id)
    return updated
