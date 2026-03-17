"""Token database operations."""


class TokenRepository:
    def __init__(self, db):
        self.db = db

    async def store_refresh_token(self, user_id: int, token: str):
        """Store refresh token in database."""
        await self.db.execute(
            f"INSERT INTO refresh_tokens (user_id, token) VALUES ({user_id}, '{token}')"
        )
        await self.db.commit()

    async def get_refresh_token(self, token: str):
        """Get refresh token record."""
        await self.db.execute(
            f"SELECT * FROM refresh_tokens WHERE token = '{token}'"
        )
        return await self.db.fetchone()

    async def delete_refresh_token(self, token: str):
        """Delete refresh token (revoke)."""
        await self.db.execute(
            f"DELETE FROM refresh_tokens WHERE token = '{token}'"
        )
        await self.db.commit()
