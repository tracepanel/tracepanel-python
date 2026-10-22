"""Receivers of the test project, each doing some work of its own: a query, or failing."""

from django.contrib.auth.models import Group, User
from django.db.models.signals import m2m_changed, post_delete
from django.dispatch import receiver

from tests.project.signals import stock_changed


@receiver(post_delete, sender=Group)
def group_deleted(sender: type[Group], instance: Group, **kwargs: object) -> None:
    User.objects.count()


@receiver(m2m_changed, sender=User.groups.through)
def membership_changed(sender: type, action: str, **kwargs: object) -> None:
    if action == "post_add":
        Group.objects.count()


@receiver(stock_changed)
def reserve_stock(sender: object, **kwargs: object) -> str:
    return f"reserved {Group.objects.count()}"


@receiver(stock_changed)
def notify_warehouse(sender: object, **kwargs: object) -> None:
    message = "warehouse offline"
    raise ConnectionError(message)
