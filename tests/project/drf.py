"""A DRF API for the DRF tests: a ViewSet whose serializer runs a query per row, validation, permissions, throttles."""

from typing import ClassVar

from django.contrib.auth.models import Group, User
from django.db.models import QuerySet
from rest_framework import permissions, serializers, status, throttling, viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.routers import SimpleRouter
from rest_framework.views import APIView

from tracepanel._compat import override


class UserSerializer(serializers.ModelSerializer[User]):
    # One query per user while serializing: the ViewSet's queryset doesn't prefetch groups.
    groups = serializers.SlugRelatedField(many=True, read_only=True, slug_field="name")

    class Meta:
        model = User
        fields: ClassVar[list[str]] = ["username", "groups"]


class GroupSerializer(serializers.ModelSerializer[Group]):
    class Meta:
        model = Group
        fields: ClassVar[list[str]] = ["name"]


class IsUser0(permissions.BasePermission):
    message = "Only user0 can be looked at."

    @override
    def has_object_permission(self, request: Request, view: APIView, obj: object) -> bool:  # pyrefly: ignore[bad-override]  # DRF is untyped: its base returns Literal[True]
        return isinstance(obj, User) and obj.username == "user0"


class UserViewSet(viewsets.ReadOnlyModelViewSet[User]):
    serializer_class = UserSerializer
    permission_classes = (permissions.AllowAny, IsUser0)

    @override
    def get_queryset(self) -> QuerySet[User]:
        return User.objects.order_by("id")


class GroupCreate(APIView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request: Request) -> Response:
        serializer = GroupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class OncePerMinute(throttling.AnonRateThrottle):
    rate = "1/min"


class Throttled(APIView):
    permission_classes = (permissions.AllowAny,)
    throttle_classes = (OncePerMinute,)

    def get(self, request: Request) -> Response:
        return Response({"ok": True})


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def whoami(request: Request) -> Response:
    return Response({"username": request.user.get_username()})


router = SimpleRouter()
router.register("users", UserViewSet, basename="drf-user")
