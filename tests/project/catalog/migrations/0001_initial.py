from typing import Any, ClassVar

from django.db import migrations, models


def add_products(apps: Any, schema_editor: Any) -> None:  # noqa: ANN401 (RunPython's signature)
    product = apps.get_model("catalog", "Product")
    product.objects.bulk_create(product(name=f"product {i}") for i in range(3))


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar[list[tuple[str, str]]] = []

    operations: ClassVar[list[migrations.operations.base.Operation]] = [
        migrations.CreateModel(
            name="Product",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=50)),
            ],
        ),
        migrations.RunPython(add_products, migrations.RunPython.noop),
    ]
