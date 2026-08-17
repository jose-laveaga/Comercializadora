"""Página temporal de compras.

Permite armar una orden y recibirla sin pasar por el admin, incluyendo el caso
que en la práctica es el normal y no el excepcional: que la entrega no venga
completa. Cada línea se puede recibir parcial, y lo que faltó se puede dejar
pendiente (llega después) o cerrar con motivo (ya no llega).

No reimplementa lógica de negocio: todo pasa por `compras.services`, que a su
vez pasa por `RecepcionDetalle.save()` y `inventario.services`. Si esta página
miente, el sistema miente.

Igual que el resto de la consola, este módulo es temporal y se borra sin tocar
las apps de negocio.
"""

from decimal import Decimal

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from compras.models import OrdenCompra, OrdenCompraDetalle, Proveedor
from compras.services import (
    RecepcionInvalidaError,
    cerrar_linea,
    reabrir_linea,
    registrar_recepcion,
)
from inventario.models import Almacen, MovimientoInventario

from . import forms


def _errores(form):
    partes = []
    for campo, errores in form.errors.items():
        nombre = "" if campo == "__all__" else f"{campo}: "
        partes.append(nombre + " ".join(errores))
    return " | ".join(partes)


def _errores_formset(formset):
    partes = []
    if formset.non_form_errors():
        partes.append(" ".join(formset.non_form_errors()))
    for i, form in enumerate(formset.forms, start=1):
        if form.errors:
            partes.append(f"línea {i}: {_errores(form)}")
    return " | ".join(partes)


@staff_member_required
def pagina_compras(request):
    """Listado de órdenes y alta de una nueva."""
    ordenes = (
        OrdenCompra.objects.select_related("proveedor", "almacen_destino")
        .prefetch_related("detalles__producto", "detalles__recepciones")
        .annotate(n_lineas=Count("detalles", distinct=True))
        .order_by("-fecha_emision", "-folio")
    )
    abiertas = [o for o in ordenes if o.esta_abierta]
    cerradas = [o for o in ordenes if not o.esta_abierta]

    return render(
        request,
        "core/compras.html",
        {
            "pagina": "compras",
            "form_orden": forms.OrdenCompraForm(),
            "abiertas": abiertas,
            "cerradas": cerradas[:20],
            "hay_proveedores": Proveedor.objects.filter(activo=True).exists(),
            "hay_almacenes": Almacen.objects.filter(activo=True).exists(),
            "faltantes": _resumen_faltantes(),
        },
    )


def _resumen_faltantes(limite=15):
    """Qué se ha dado por no recibido, por proveedor.

    Es la razón de ser de cerrar líneas con motivo: sin esto, un faltante es
    solo una orden que nunca se completó y no se puede preguntar en qué falla
    cada quién.
    """
    lineas = (
        OrdenCompraDetalle.objects.filter(cerrado=True)
        .select_related("producto", "orden__proveedor")
        .order_by("-cerrado_en")[:limite]
    )
    return [l for l in lineas if l.cantidad_faltante > 0]


@staff_member_required
def ficha_orden(request, pk):
    """Detalle de una orden: sus líneas, su historial y la captura de recepción."""
    orden = get_object_or_404(
        OrdenCompra.objects.select_related("proveedor", "almacen_destino"), pk=pk
    )
    detalles = list(
        orden.detalles.select_related("producto__linea", "producto__marca", "cerrado_por")
    )
    pendientes = [d for d in detalles if d.cantidad_pendiente > 0]
    formset = _formset_inicial(pendientes)

    return render(
        request,
        "core/compra_ficha.html",
        {
            "pagina": "compras",
            "orden": orden,
            "detalles": detalles,
            "pendientes": pendientes,
            "form_linea": forms.OrdenCompraDetalleForm(orden=orden),
            "form_recepcion": forms.RecepcionForm(),
            "formset": formset,
            # El formset no sabe de qué línea es cada fila; se emparejan aquí
            # para que la plantilla pueda mostrar el producto junto a su captura.
            "filas_recepcion": list(zip(formset.forms, pendientes)),
            "recepciones": orden.recepciones.select_related("recibido_por").prefetch_related(
                "detalles__orden_detalle__producto"
            ),
            "motivos": OrdenCompraDetalle.MotivoFaltante.choices,
            "estatus_editables": _estatus_editables(orden),
        },
    )


def _formset_inicial(pendientes):
    """Una fila del formset por línea que todavía espera mercancía.

    El costo real se prellena con lo cotizado porque es lo que suele coincidir;
    quien captura lo corrige cuando la factura trae otro número, y ese número
    es el que termina valuando el lote.
    """
    return forms.RecepcionLineaFormSet(
        initial=[
            {
                "detalle_id": d.pk,
                "costo_unitario_real": d.costo_unitario,
            }
            for d in pendientes
        ],
        prefix="lineas",
    )


def _estatus_editables(orden):
    """Los estatus que tiene sentido poner a mano.

    Los de avance (recibida, parcial, cerrada con faltante) los deduce
    `recalcular_estatus` a partir de los hechos, así que ponerlos a mano solo
    serviría para que la orden mintiera.
    """
    if not orden.esta_abierta:
        return []
    return [
        (OrdenCompra.Estatus.BORRADOR, "Borrador"),
        (OrdenCompra.Estatus.ENVIADA, "Enviada"),
        (OrdenCompra.Estatus.CONFIRMADA, "Confirmada"),
        (OrdenCompra.Estatus.CANCELADA, "Cancelada"),
    ]


@require_POST
@staff_member_required
def crear_orden(request):
    form = forms.OrdenCompraForm(request.POST)
    if not form.is_valid():
        messages.error(request, f"Orden: {_errores(form)}")
        return redirect("core:compras")

    orden = form.save()
    messages.success(request, f"Orden {orden.folio} creada. Agrégale sus líneas.")
    return redirect("core:compra_ficha", pk=orden.pk)


@require_POST
@staff_member_required
def agregar_linea(request, pk):
    orden = get_object_or_404(OrdenCompra, pk=pk)
    if not orden.esta_abierta:
        messages.error(request, f"{orden.folio} ya está cerrada; no admite líneas nuevas.")
        return redirect("core:compra_ficha", pk=orden.pk)

    form = forms.OrdenCompraDetalleForm(request.POST, orden=orden)
    if not form.is_valid():
        messages.error(request, f"Línea: {_errores(form)}")
        return redirect("core:compra_ficha", pk=orden.pk)

    linea = form.save(commit=False)
    linea.orden = orden
    linea.save()
    messages.success(
        request,
        f"{linea.producto.sku}: {linea.cantidad_pedida} {linea.get_unidad_compra_display().lower()} "
        f"por ${linea.importe:,.2f}.",
    )
    return redirect("core:compra_ficha", pk=orden.pk)


@require_POST
@staff_member_required
def borrar_linea(request, pk, linea_pk):
    orden = get_object_or_404(OrdenCompra, pk=pk)
    linea = get_object_or_404(OrdenCompraDetalle, pk=linea_pk, orden=orden)
    if linea.cantidad_recibida > 0:
        messages.error(
            request,
            f"{linea.producto.sku} ya tiene {linea.cantidad_recibida} recibidas; "
            "no se puede borrar la línea sin borrar su historial de inventario.",
        )
        return redirect("core:compra_ficha", pk=orden.pk)

    sku = linea.producto.sku
    linea.delete()
    messages.success(request, f"Línea de {sku} eliminada.")
    return redirect("core:compra_ficha", pk=orden.pk)


@require_POST
@staff_member_required
def recibir_orden(request, pk):
    """Aplica una entrega completa: lo que llegó y lo que se da por perdido."""
    orden = get_object_or_404(OrdenCompra, pk=pk)
    pendientes = {d.pk: d for d in orden.detalles.all() if d.cantidad_pendiente > 0}

    form_recepcion = forms.RecepcionForm(request.POST)
    formset = forms.RecepcionLineaFormSet(request.POST, prefix="lineas")
    if not form_recepcion.is_valid() or not formset.is_valid():
        problemas = " | ".join(
            p for p in (_errores(form_recepcion), _errores_formset(formset)) if p
        )
        messages.error(request, f"Recepción: {problemas}")
        return redirect("core:compra_ficha", pk=orden.pk)

    lineas = []
    for datos in formset.cleaned_data:
        detalle = pendientes.get(datos.get("detalle_id"))
        if detalle is None:
            continue
        if not datos.get("cantidad_recibida") and not datos.get("cerrar_faltante"):
            continue
        lineas.append(
            {
                "detalle": detalle,
                "cantidad": datos.get("cantidad_recibida"),
                "fecha_caducidad": datos.get("fecha_caducidad"),
                "costo_unitario_real": datos.get("costo_unitario_real"),
                "codigo_lote_proveedor": datos.get("codigo_lote_proveedor", ""),
                "cerrar": datos.get("cerrar_faltante", False),
                "motivo": datos.get("motivo_faltante"),
                "notas_faltante": datos.get("notas_faltante", ""),
            }
        )

    if not lineas:
        messages.error(request, "No capturaste ninguna cantidad ni marcaste ningún faltante.")
        return redirect("core:compra_ficha", pk=orden.pk)

    try:
        recepcion, detalles, cerradas = registrar_recepcion(
            orden,
            lineas,
            usuario=request.user,
            fecha_recepcion=form_recepcion.cleaned_data["fecha_recepcion"],
            folio_factura=form_recepcion.cleaned_data["folio_factura"],
            notas=form_recepcion.cleaned_data["notas"],
        )
    except (ValidationError, RecepcionInvalidaError) as error:
        mensajes = getattr(error, "messages", None) or [str(error)]
        messages.error(request, f"No se registró nada: {' | '.join(mensajes)}")
        return redirect("core:compra_ficha", pk=orden.pk)

    _reportar_recepcion(request, orden, recepcion, detalles, cerradas)
    return redirect("core:compra_ficha", pk=orden.pk)


def _reportar_recepcion(request, orden, recepcion, detalles, cerradas):
    """Dice qué entró al inventario y por cuánto, no solo que 'se guardó'."""
    if detalles:
        valor = sum((d.cantidad_recibida * d.costo_unitario_real for d in detalles), Decimal("0"))
        messages.success(
            request,
            f"Recepción #{recepcion.pk}: {len(detalles)} línea(s), "
            f"${valor:,.2f} agregados al valor del inventario.",
        )
        for detalle in detalles:
            movimiento = detalle.movimientos.first()
            if movimiento:
                messages.info(
                    request,
                    f"{detalle.orden_detalle.producto.sku}: {detalle.cantidad_recibida} "
                    f"a ${detalle.costo_unitario_real} → lote {movimiento.lote.codigo}.",
                )

    for detalle in cerradas:
        messages.warning(
            request,
            f"{detalle.producto.sku}: {detalle.cantidad_faltante} sin recibir, "
            f"cerrado como «{detalle.get_motivo_faltante_display()}».",
        )

    orden.refresh_from_db()
    messages.info(request, f"{orden.folio} quedó en «{orden.get_estatus_display()}».")


@require_POST
@staff_member_required
def cerrar_faltante(request, pk, linea_pk):
    """Cierra una línea sin que haya llegado mercancía en este acto."""
    orden = get_object_or_404(OrdenCompra, pk=pk)
    linea = get_object_or_404(OrdenCompraDetalle, pk=linea_pk, orden=orden)

    form = forms.CerrarLineaForm(request.POST)
    if not form.is_valid():
        messages.error(request, f"Cierre: {_errores(form)}")
        return redirect("core:compra_ficha", pk=orden.pk)

    try:
        cerrar_linea(
            linea,
            motivo=form.cleaned_data["motivo_faltante"],
            notas=form.cleaned_data["notas_faltante"],
            usuario=request.user,
        )
    except ValidationError as error:
        messages.error(request, " | ".join(error.messages))
        return redirect("core:compra_ficha", pk=orden.pk)

    orden.refresh_from_db()
    messages.warning(
        request,
        f"{linea.producto.sku}: {linea.cantidad_faltante} dados por no recibidos "
        f"({linea.get_motivo_faltante_display()}). {orden.folio} quedó en "
        f"«{orden.get_estatus_display()}».",
    )
    return redirect("core:compra_ficha", pk=orden.pk)


@require_POST
@staff_member_required
def reabrir_faltante(request, pk, linea_pk):
    orden = get_object_or_404(OrdenCompra, pk=pk)
    linea = get_object_or_404(OrdenCompraDetalle, pk=linea_pk, orden=orden)
    try:
        reabrir_linea(linea, usuario=request.user)
    except ValidationError as error:
        messages.error(request, " | ".join(error.messages))
        return redirect("core:compra_ficha", pk=orden.pk)

    orden.refresh_from_db()
    messages.success(
        request,
        f"{linea.producto.sku}: vuelve a esperar {linea.cantidad_pendiente}. "
        f"{orden.folio} quedó en «{orden.get_estatus_display()}».",
    )
    return redirect("core:compra_ficha", pk=orden.pk)


@require_POST
@staff_member_required
def cambiar_estatus(request, pk):
    """Solo para los estatus que no se deducen de los hechos."""
    orden = get_object_or_404(OrdenCompra, pk=pk)
    nuevo = request.POST.get("estatus")
    permitidos = {valor for valor, _ in _estatus_editables(orden)}
    if nuevo not in permitidos:
        messages.error(
            request,
            f"«{nuevo}» no se pone a mano: el avance de la orden lo deducen las recepciones.",
        )
        return redirect("core:compra_ficha", pk=orden.pk)

    orden.estatus = nuevo
    orden.save(update_fields=["estatus", "actualizado_en"])
    messages.success(request, f"{orden.folio} quedó en «{orden.get_estatus_display()}».")
    return redirect("core:compra_ficha", pk=orden.pk)
