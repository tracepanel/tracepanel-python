"""A Django Ninja API for the Ninja tests: two methods on one path, a sync and an async operation, an auth check."""

from django.contrib.auth.models import User
from django.http import HttpRequest
from ninja import NinjaAPI, Schema
from ninja.security import django_auth

api = NinjaAPI(urls_namespace="ninja-tests")


class UserOut(Schema):
    username: str
    groups: list[str]

    @staticmethod
    def resolve_groups(user: User) -> list[str]:
        # One query per user while the response is serialized.
        return [group.name for group in user.groups.all()]


class GroupIn(Schema):
    name: str


@api.get("/users", response=list[UserOut])
def list_users(request: HttpRequest, limit: int = 10) -> list[User]:
    return list(User.objects.order_by("id")[:limit])


@api.post("/users", operation_id="create_group")
def create_group(request: HttpRequest, payload: GroupIn) -> dict[str, str]:
    return {"name": payload.name}


@api.get("/count")
async def count_users(request: HttpRequest) -> dict[str, int]:
    return {"users": await User.objects.acount()}


@api.get("/me", auth=django_auth)
def me(request: HttpRequest) -> dict[str, str]:
    return {"username": request.user.get_username()}
