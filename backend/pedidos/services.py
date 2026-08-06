from django.db import transaction

from inventario.models import Lote, MovimientoInventario
from inventario.services import StockInsuficienteError, registrar_movimiento

from .models import Pedido, SurtidoDetalle


class SurtidoInvalidoError(Exception):
    """El surtido pedido no es válido contra la línea del pedido."""


def surtir_pedido_detalle(pedido_detalle, cantidad, surtido, usuario=None):
    """Descuenta `cantidad` del inventario para una línea de pedido, por FEFO.

    Consume los lotes del almacén origen del pedido empezando por los más
    próximos a caducar, creando un SurtidoDetalle y un MovimientoInventario de
    salida por cada lote usado. Todo ocurre en una sola transacción: si el
    stock no alcanza, no queda nada a medias.
    """
    if cantidad <= 0:
        raise ValueError("La cantidad a surtir debe ser positiva.")
    if pedido_detalle.pedido_id != surtido.pedido_id:
        raise SurtidoInvalidoError(
            f"{pedido_detalle.producto} pertenece a {pedido_detalle.pedido}, "
            f"no a {surtido.pedido}."
        )

    with transaction.atomic():
        pendiente = pedido_detalle.cantidad_pendiente
        if cantidad > pendiente:
            raise SurtidoInvalidoError(
                f"Se intentó surtir {cantidad} de {pedido_detalle.producto}, "
                f"pero solo quedan {pendiente} pendientes."
            )

        almacen = surtido.pedido.almacen_origen
        candidatos = Lote.objects.filter(almacen=almacen).fefo(pedido_detalle.producto, cantidad)

        detalles = []
        por_surtir = cantidad
        for candidato in candidatos:
            if por_surtir <= 0:
                break
            # fefo() es consultivo y no bloquea: hay que releer el lote bajo
            # lock antes de consumirlo, porque otro surtido concurrente pudo
            # haberlo vaciado entre la consulta y este punto.
            lote = Lote.objects.select_for_update().get(pk=candidato.pk)
            del_lote = min(por_surtir, lote.cantidad_actual)
            if del_lote <= 0:
                continue

            detalle = SurtidoDetalle.objects.create(
                surtido=surtido,
                pedido_detalle=pedido_detalle,
                lote=lote,
                cantidad_surtida=del_lote,
            )
            registrar_movimiento(
                lote,
                MovimientoInventario.Tipo.SALIDA_VENTA,
                -del_lote,
                usuario=usuario,
                surtido_detalle=detalle,
            )
            detalles.append(detalle)
            por_surtir -= del_lote

        if por_surtir > 0:
            raise StockInsuficienteError(
                f"No hay stock suficiente de {pedido_detalle.producto} en {almacen}: "
                f"faltan {por_surtir} de {cantidad}."
            )

        _recalcular_estatus_pedido(surtido.pedido)

    return detalles


def _recalcular_estatus_pedido(pedido):
    detalles = list(pedido.detalles.all())
    if all(d.cantidad_pendiente == 0 for d in detalles):
        nuevo_estatus = Pedido.Estatus.SURTIDO
    elif any(d.cantidad_surtida > 0 for d in detalles):
        nuevo_estatus = Pedido.Estatus.SURTIDO_PARCIAL
    else:
        return
    if pedido.estatus != nuevo_estatus:
        pedido.estatus = nuevo_estatus
        pedido.save(update_fields=["estatus"])
