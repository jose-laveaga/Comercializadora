"""Página temporal de reabastecimiento.

Contesta "¿qué tengo que comprar hoy?" cruzando el estándar de cada producto
contra su existencia útil y contra lo que ya viene en camino en órdenes
abiertas. No decide nada por su cuenta: el cálculo vive en
`inventario.reabastecimiento` y esta capa solo lo muestra y deja capturar los
estándares.

Igual que el resto de la consola, es temporal y se borra sin tocar las apps de
negocio.
"""

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from inventario import reabastecimiento
from inventario.models import Almacen, EstandarInventario

from . import forms


def _errores(form):
    partes = []
    for campo, errores in form.errors.items():
        nombre = "" if campo == "__all__" else f"{campo}: "
        partes.append(nombre + " ".join(errores))
    return " | ".join(partes)


@staff_member_required
def pagina_reabastecimiento(request):
    almacen = None
    clave = request.GET.get("almacen") or ""
    if clave:
        almacen = Almacen.objects.filter(clave=clave).first()

    diagnosticos = reabastecimiento.evaluar(almacen=almacen)
    conteo = reabastecimiento.resumen(diagnosticos)

    return render(
        request,
        "core/reabastecimiento.html",
        {
            "pagina": "reabastecimiento",
            "diagnosticos": diagnosticos,
            "por_pedir": [d for d in diagnosticos if d.requiere_pedir],
            "conteo": conteo,
            "almacenes": Almacen.objects.filter(activo=True).order_by("clave"),
            "almacen_activo": almacen,
            "clave_activa": clave,
            "form_estandar": forms.EstandarInventarioForm(),
            "sin_estandar": reabastecimiento.productos_sin_estandar(almacen)[:30],
            "umbral_util": reabastecimiento.Lote.UMBRAL_NEGRO,
        },
    )


@require_POST
@staff_member_required
def guardar_estandar(request):
    """Alta o actualización del estándar de un producto en un almacén.

    Si ya existe uno para esa pareja se actualiza en vez de fallar por la
    restricción de unicidad: quien captura está corrigiendo un número, no
    intentando crear un duplicado.
    """
    form = forms.EstandarInventarioForm(request.POST)
    if not form.is_valid():
        producto = form.data.get("producto")
        almacen = form.data.get("almacen")
        existente = EstandarInventario.objects.filter(
            producto_id=producto or None, almacen_id=almacen or None
        ).first()
        if existente is not None:
            form = forms.EstandarInventarioForm(request.POST, instance=existente)

    if not form.is_valid():
        messages.error(request, f"Estándar: {_errores(form)}")
        return redirect("core:reabastecimiento")

    estandar = form.save()
    messages.success(
        request,
        f"{estandar.producto.sku} en {estandar.almacen.clave}: mínimo "
        f"{estandar.cantidad_minima:g}, objetivo {estandar.cantidad_objetivo:g}.",
    )
    return redirect("core:reabastecimiento")


@require_POST
@staff_member_required
def borrar_estandar(request, pk):
    estandar = get_object_or_404(EstandarInventario, pk=pk)
    etiqueta = f"{estandar.producto.sku} en {estandar.almacen.clave}"
    estandar.delete()
    messages.success(request, f"Estándar de {etiqueta} eliminado.")
    return redirect("core:reabastecimiento")
