from django.db import models


class TimeStampedModel(models.Model):
    """Base abstracta para timestamps de creación/actualización.

    Compartida por los modelos de inventario y compras. catalogo.Producto no
    hereda de aquí: declara sus propios creado_en/actualizado_en desde antes.
    """

    creado_en = models.DateTimeField("creado en", auto_now_add=True)
    actualizado_en = models.DateTimeField("actualizado en", auto_now=True)

    class Meta:
        abstract = True
