"""Operaciones de compra que tocan varias líneas a la vez.

`RecepcionDetalle.save()` ya sabe crear el lote y registrar el movimiento de
una línea. Lo que falta y vive aquí es el nivel de arriba: recibir varias
líneas como un solo acto, de modo que si la tercera falla no quede una
recepción a medias con dos entradas ya aplicadas al inventario.

También vive aquí el cierre de una línea con faltante, que no es una recepción
—no entra mercancía— pero sí cambia el avance de la orden.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import OrdenCompraDetalle, Recepcion, RecepcionDetalle


class RecepcionInvalidaError(Exception):
    """Lo que se intentó recibir no corresponde con la orden."""


@transaction.atomic
def registrar_recepcion(orden, lineas, usuario=None, fecha_recepcion=None, folio_factura="", notas=""):
    """Registra en un solo acto lo que llegó de `orden` y lo que se da por perdido.

    `lineas` es una secuencia de diccionarios, uno por línea de la orden que se
    va a tocar:

        {
            "detalle": OrdenCompraDetalle,
            "cantidad": Decimal | None,      # lo que llegó; None u 0 = no llegó nada
            "fecha_caducidad": date,         # obligatorio si hay cantidad
            "costo_unitario_real": Decimal,  # obligatorio si hay cantidad
            "codigo_lote_proveedor": str,
            "cerrar": bool,                  # el resto ya no va a llegar
            "motivo": OrdenCompraDetalle.MotivoFaltante,
            "notas_faltante": str,
        }

    Devuelve `(recepcion, detalles, cerradas)`. `recepcion` es None cuando no
    llegó nada y el acto consistió solo en cerrar líneas — no tiene sentido
    dejar una recepción vacía en el historial.
    """
    lineas = list(lineas)
    if not lineas:
        raise RecepcionInvalidaError("No se indicó ninguna línea que recibir o cerrar.")

    for linea in lineas:
        if linea["detalle"].orden_id != orden.pk:
            raise RecepcionInvalidaError(
                f"La línea de {linea['detalle'].producto} no pertenece a {orden.folio}."
            )

    con_mercancia = [l for l in lineas if l.get("cantidad")]
    a_cerrar = [l for l in lineas if l.get("cerrar")]
    if not con_mercancia and not a_cerrar:
        raise RecepcionInvalidaError("No hay nada que recibir ni que cerrar.")

    recepcion = None
    detalles = []
    if con_mercancia:
        recepcion = Recepcion.objects.create(
            orden_compra=orden,
            fecha_recepcion=fecha_recepcion or timezone.localdate(),
            folio_factura=folio_factura,
            recibido_por=usuario,
            notas=notas,
        )
        for linea in con_mercancia:
            detalle = RecepcionDetalle(
                recepcion=recepcion,
                orden_detalle=linea["detalle"],
                cantidad_recibida=linea["cantidad"],
                fecha_caducidad=linea["fecha_caducidad"],
                codigo_lote_proveedor=linea.get("codigo_lote_proveedor", ""),
                costo_unitario_real=linea["costo_unitario_real"],
            )
            # full_clean antes de save: save() ya mueve inventario, así que la
            # validación tiene que correr antes y no después.
            detalle.full_clean(exclude=["recepcion"])
            detalle.save()
            detalles.append(detalle)

    # El cierre va después de recibir: lo que se cierra es el remanente que
    # quedó tras aplicar la mercancía de esta misma entrega.
    cerradas = []
    for linea in a_cerrar:
        detalle_orden = linea["detalle"]
        detalle_orden.refresh_from_db()
        if detalle_orden.cantidad_pendiente > 0:
            cerrar_linea(
                detalle_orden,
                motivo=linea.get("motivo") or OrdenCompraDetalle.MotivoFaltante.NO_SURTIDO,
                notas=linea.get("notas_faltante", ""),
                usuario=usuario,
                recalcular=False,
            )
            cerradas.append(detalle_orden)

    orden.recalcular_estatus()
    return recepcion, detalles, cerradas


@transaction.atomic
def cerrar_linea(detalle, motivo, notas="", usuario=None, recalcular=True):
    """Declara que lo que falta de `detalle` ya no va a llegar."""
    if detalle.cerrado:
        raise ValidationError(f"La línea de {detalle.producto} ya estaba cerrada.")
    if detalle.cantidad_pendiente <= 0:
        raise ValidationError(
            f"La línea de {detalle.producto} ya se recibió completa; no hay faltante que cerrar."
        )

    detalle.cerrado = True
    detalle.motivo_faltante = motivo
    detalle.notas_faltante = notas
    detalle.cerrado_en = timezone.now()
    detalle.cerrado_por = usuario
    detalle.save(
        update_fields=[
            "cerrado",
            "motivo_faltante",
            "notas_faltante",
            "cerrado_en",
            "cerrado_por",
            "actualizado_en",
        ]
    )
    if recalcular:
        detalle.orden.recalcular_estatus()
    return detalle


@transaction.atomic
def reabrir_linea(detalle, usuario=None):
    """Deshace un cierre: el proveedor sí va a mandar lo que faltaba."""
    if not detalle.cerrado:
        raise ValidationError(f"La línea de {detalle.producto} no está cerrada.")

    detalle.cerrado = False
    detalle.motivo_faltante = ""
    detalle.notas_faltante = ""
    detalle.cerrado_en = None
    detalle.cerrado_por = None
    detalle.save(
        update_fields=[
            "cerrado",
            "motivo_faltante",
            "notas_faltante",
            "cerrado_en",
            "cerrado_por",
            "actualizado_en",
        ]
    )
    detalle.orden.recalcular_estatus()
    return detalle
