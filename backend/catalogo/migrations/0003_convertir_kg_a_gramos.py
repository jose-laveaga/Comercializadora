from decimal import Decimal

from django.db import migrations


def convertir_kg_a_gramos(apps, schema_editor):
    """Pasa el contenido neto de kg a g y regenera el SKU de los productos afectados.

    Antes de este cambio, 0.4 kg se truncaba a "0KG" en el SKU
    (int(0.4) == 0). Guardado en gramos, 400 g queda como "400G".
    """
    Producto = apps.get_model("catalogo", "Producto")
    afectados = Producto.objects.filter(unidad_contenido="kg")
    for producto in afectados:
        producto.contenido_neto = producto.contenido_neto * Decimal(1000)
        producto.unidad_contenido = "g"
        producto.sku = ""
        producto.save(update_fields=["contenido_neto", "unidad_contenido"])

    # Regenerar el SKU de los productos afectados con la nueva unidad.
    ocupados = set(Producto.objects.exclude(pk__in=afectados.values("pk")).values_list("sku", flat=True))
    for producto in afectados:
        partes = [producto.linea.codigo]
        if producto.marca_id:
            partes.append(producto.marca.codigo)
        if producto.contenido_neto and producto.unidad_contenido:
            partes.append(f"{int(producto.contenido_neto)}{producto.unidad_contenido.upper()}")
        if producto.forma:
            partes.append(producto.forma)
        base = "-".join(partes)
        sku = base
        n = 2
        while sku in ocupados:
            sku = f"{base}-{n}"
            n += 1
        ocupados.add(sku)
        producto.sku = sku
        producto.save(update_fields=["sku"])


class Migration(migrations.Migration):

    dependencies = [
        ("catalogo", "0002_precio_split_unidad_gramos"),
    ]

    operations = [
        migrations.RunPython(convertir_kg_a_gramos, migrations.RunPython.noop),
    ]
