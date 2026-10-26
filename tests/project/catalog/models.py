"""A model whose migrations the migrate tests apply and unapply (tests/django/test_migrate.py): not installed."""

from typing import ClassVar

from django.db import models


class Product(models.Model):
    name = models.CharField(max_length=100)
    sku = models.CharField(max_length=20, default="")
    price = models.BigIntegerField(default=0)
    note = models.CharField(max_length=200, default="")

    class Meta:
        indexes: ClassVar[list[models.Index]] = [models.Index(fields=["sku"], name="catalog_sku_idx")]

    def __str__(self) -> str:
        return self.name
