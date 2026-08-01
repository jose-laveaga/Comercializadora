from django.db import transaction
from django.db.models import F

from .models import Lote, MovimientoInventario


class StockInsuficienteError(Exception):
    """Una salida dejaría Lote.cantidad_actual en negativo."""


def registrar_movimiento(lote, tipo, cantidad, usuario=None, notas="", recepcion_detalle=None):
    """Única vía soportada para cambiar Lote.cantidad_actual.

    Bloquea la fila del lote, valida que el movimiento no la deje negativa,
    crea el MovimientoInventario y actualiza la cantidad con una expresión F()
    para evitar condiciones de carrera entre movimientos concurrentes.
    """
    with transaction.atomic():
        lote_bloqueado = Lote.objects.select_for_update().get(pk=lote.pk)
        if lote_bloqueado.cantidad_actual + cantidad < 0:
            raise StockInsuficienteError(
                f"El movimiento dejaría a {lote_bloqueado} con cantidad negativa "
                f"({lote_bloqueado.cantidad_actual} + {cantidad})."
            )
        movimiento = MovimientoInventario.objects.create(
            lote=lote_bloqueado,
            tipo=tipo,
            cantidad=cantidad,
            usuario=usuario,
            notas=notas,
            recepcion_detalle=recepcion_detalle,
        )
        Lote.objects.filter(pk=lote.pk).update(cantidad_actual=F("cantidad_actual") + cantidad)
    lote.refresh_from_db(fields=["cantidad_actual"])
    return movimiento
