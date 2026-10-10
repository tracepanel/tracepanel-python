"""A custom user model unlike auth.User: a UUID key, email login, no names, no staff flags."""

import uuid
from typing import Any, ClassVar

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models


class MemberManager(BaseUserManager["Member"]):
    def create_user(self, email: str, password: str | None = None, **extra: Any) -> "Member":  # noqa: ANN401
        member = self.model(email=self.normalize_email(email), **extra)
        member.set_password(password)
        member.save(using=self._db)
        return member


class Member(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)  # pyrefly: ignore[bad-override-mutable-attribute]
    email = models.EmailField(unique=True)
    nickname = models.CharField(max_length=50, blank=True)

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects = MemberManager()
