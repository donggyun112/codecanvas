"""User business logic."""
from app.repositories.user_repo import UserRepository


class UserService:
    def __init__(self, db):
        self.user_repo = UserRepository(db)

    async def get_user_by_id(self, user_id: int):
        """Get user by ID."""
        return await self.user_repo.find_by_id(user_id)

    async def update_user(self, user_id: int):
        """Update user profile."""
        user = await self.user_repo.find_by_id(user_id)
        if user is None:
            return None
        return await self.user_repo.update(user_id, user)
