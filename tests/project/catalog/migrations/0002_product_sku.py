from typing import Any, ClassVar

from django.db import migrations, models


def backfill_sku(apps: Any, schema_editor: Any) -> None:  # noqa: ANN401 (RunPython's signature)
    apps.get_model("catalog", "Product").objects.filter(sku="").update(sku="unknown")


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [("catalog", "0001_initial")]

    operations: ClassVar[list[migrations.operations.base.Operation]] = [
        migrations.AddField(model_name="product", name="sku", field=models.CharField(default="", max_length=20)),
        migrations.RunPython(backfill_sku, migrations.RunPython.noop),
    ]
