"""FastAPI dependency injection functions."""


async def get_db():
    """Get database session."""
    db = FakeDB()
    try:
        yield db
    finally:
        await db.close()


async def get_current_user(token: str = ""):
    """Extract current user from JWT token."""
    from app.services.auth_service import AuthService
    service = AuthService(None)
    user = await service.verify_token(token)
    if user is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


class FakeDB:
    async def execute(self, query):
        pass

    async def fetchone(self):
        pass

    async def fetchall(self):
        pass

    async def commit(self):
        pass

    async def close(self):
        pass
