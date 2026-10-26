from typing import ClassVar

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies: ClassVar[list[tuple[str, str]]] = [("catalog", "0002_product_sku")]

    operations: ClassVar[list[migrations.operations.base.Operation]] = [
        migrations.AlterField(model_name="product", name="name", field=models.CharField(max_length=100)),
        migrations.AddField(model_name="product", name="price", field=models.IntegerField(null=True)),
        migrations.AlterField(model_name="product", name="price", field=models.BigIntegerField(default=0)),
        migrations.AddField(model_name="product", name="note", field=models.CharField(max_length=200, null=True)),
        migrations.AlterField(model_name="product", name="note", field=models.CharField(default="", max_length=200)),
        migrations.AddIndex(model_name="product", index=models.Index(fields=["sku"], name="catalog_sku_idx")),
    ]
