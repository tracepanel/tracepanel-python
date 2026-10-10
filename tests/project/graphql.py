"""A Strawberry schema for the GraphQL tests: users with an N+1 resolver, a failing field, an async field."""

import strawberry
from django.contrib.auth.models import Group, User


@strawberry.type
class UserType:
    id: strawberry.ID
    username: str

    @strawberry.field
    def groups(self) -> list[str]:
        # One query per user: an N+1 that a DataLoader would batch.
        return [group.name for group in Group.objects.filter(user__id=self.id)]


@strawberry.type
class Query:
    @strawberry.field
    def users(self) -> list[UserType]:
        return [
            UserType(id=strawberry.ID(str(user.pk)), username=user.username) for user in User.objects.order_by("id")
        ]

    @strawberry.field
    def broken(self) -> str:
        msg = "the reporting service is down"
        raise RuntimeError(msg)

    @strawberry.field
    async def user_count(self) -> int:
        return await User.objects.acount()


schema = strawberry.Schema(query=Query)
