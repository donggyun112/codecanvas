"""Authentication business logic."""
from app.repositories.user_repo import UserRepository
from app.repositories.token_repo import TokenRepository


class AuthService:
    def __init__(self, db):
        self.user_repo = UserRepository(db)
        self.token_repo = TokenRepository(db)
        self.db = db

    async def verify_user(self, email: str, password: str):
        """Verify user credentials."""
        user = await self.user_repo.find_by_email(email)
        if user is None:
            return None
        if not self._check_password(password, user.get("hashed_password", "")):
            return None
        return user

    async def issue_tokens(self, user):
        """Generate access + refresh tokens."""
        access_token = self._create_jwt(user, expires_in=3600)
        refresh_token = self._create_jwt(user, expires_in=86400)
        await self.token_repo.store_refresh_token(user["id"], refresh_token)
        return {"access_token": access_token, "refresh_token": refresh_token}

    async def refresh_access_token(self, refresh_token: str):
        """Validate refresh token and issue new access token."""
        stored = await self.token_repo.get_refresh_token(refresh_token)
        if stored is None:
            return None
        user = await self.user_repo.find_by_id(stored["user_id"])
        if user is None:
            return None
        new_access = self._create_jwt(user, expires_in=3600)
        return {"access_token": new_access, "refresh_token": refresh_token}

    async def verify_token(self, token: str):
        """Verify JWT token and return user."""
        payload = self._decode_jwt(token)
        if payload is None:
            return None
        return await self.user_repo.find_by_id(payload.get("user_id"))

    async def revoke_token(self, token: str):
        """Revoke a refresh token."""
        await self.token_repo.delete_refresh_token(token)

    def _check_password(self, plain: str, hashed: str) -> bool:
        return plain == hashed  # Placeholder

    def _create_jwt(self, user, expires_in: int) -> str:
        return f"jwt_{user.get('id', '')}_{expires_in}"  # Placeholder

    def _decode_jwt(self, token: str):
        if token.startswith("jwt_"):
            parts = token.split("_")
            return {"user_id": int(parts[1])} if len(parts) >= 2 else None
        return None
