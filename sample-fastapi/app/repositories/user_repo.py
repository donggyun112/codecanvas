"""User database operations."""


class UserRepository:
    def __init__(self, db):
        self.db = db

    async def find_by_email(self, email: str):
        """Find user by email address."""
        await self.db.execute(f"SELECT * FROM users WHERE email = '{email}'")
        return await self.db.fetchone()

    async def find_by_id(self, user_id: int):
        """Find user by ID."""
        await self.db.execute(f"SELECT * FROM users WHERE id = {user_id}")
        return await self.db.fetchone()

    async def update(self, user_id: int, data):
        """Update user record."""
        await self.db.execute(f"UPDATE users SET ... WHERE id = {user_id}")
        await self.db.commit()
        return await self.find_by_id(user_id)
