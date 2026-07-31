from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="producto",
            old_name="precio",
            new_name="precio_venta",
        ),
        migrations.AlterField(
            model_name="producto",
            name="precio_venta",
            field=models.DecimalField(decimal_places=2, max_digits=10, verbose_name="precio venta"),
        ),
        migrations.AddField(
            model_name="producto",
            name="precio_compra",
            field=models.DecimalField(decimal_places=2, max_digits=10, default=0, verbose_name="precio compra"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="producto",
            name="vida_anaquel",
            field=models.IntegerField(default=0, verbose_name="dias anaquel"),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="producto",
            name="requiere_produccion",
            field=models.BooleanField(default=False, help_text="", verbose_name="requiere producción"),
        ),
        migrations.AlterField(
            model_name="producto",
            name="tipo_precio",
            field=models.CharField(
                choices=[("pieza", "Por pieza"), ("kilo", "Por kilo"), ("litro", "Por litro")],
                max_length=10,
                verbose_name="tipo de precio",
            ),
        ),
        migrations.AlterField(
            model_name="producto",
            name="forma",
            field=models.CharField(
                blank=True,
                choices=[
                    ("BAR", "Barra"),
                    ("RUE", "Rueda"),
                    ("REB", "Rebanado"),
                    ("POR", "Porción"),
                    ("CUN", "Cuña"),
                    ("TPK", "Tetrapak"),
                    ("CJA", "Caja"),
                    ("GRA", "Granel"),
                    ("CUB", "Cubeta"),
                ],
                max_length=3,
                verbose_name="forma",
            ),
        ),
        migrations.AlterField(
            model_name="producto",
            name="unidad_contenido",
            field=models.CharField(
                blank=True,
                choices=[("g", "g"), ("ml", "ml"), ("pza", "pza")],
                max_length=3,
                verbose_name="unidad",
            ),
        ),
    ]
