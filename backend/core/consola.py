"""Vista temporal de inventario.

Muestra lo que hay, dónde está y cómo se movió, y permite meter stock con un
ajuste positivo para poder probar el resto del sistema.

No reimplementa lógica de negocio: la entrada de stock pasa por
`inventario.services.registrar_movimiento`, igual que el admin. Si esta página
miente, el sistema miente — que es justamente para lo que sirve.

Todo este módulo es temporal y se borra sin tocar las apps de negocio.
"""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Count, Sum
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from catalogo.models import Producto
from inventario.identificacion import existencias_de, resolver_codigo
from inventario.models import Almacen, Lote, MovimientoInventario
from inventario.services import registrar_movimiento

from . import forms

# clave del POST -> (formulario, etiqueta)
FORMULARIOS = {
    "producto": (forms.ProductoForm, "Producto"),
    "categoria": (forms.CategoriaForm, "Categoría"),
    "marca": (forms.MarcaForm, "Marca"),
    "linea": (forms.LineaProductoForm, "Línea"),
    "almacen": (forms.AlmacenForm, "Almacén"),
}


def _volver():
    return redirect("core:inventario")


def _errores(form):
    partes = []
    for campo, errores in form.errors.items():
        nombre = "" if campo == "__all__" else f"{campo}: "
        partes.append(nombre + " ".join(errores))
    return " | ".join(partes)


@staff_member_required
def pagina_inventario(request):
    hoy = timezone.localdate()

    semaforo = []
    for estatus in Lote.EstatusCaducidad:
        qs = Lote.objects.disponibles().con_estatus_caducidad(estatus)
        agregado = qs.aggregate(lotes=Count("pk"), unidades=Sum("cantidad_actual"))
        semaforo.append(
            {
                "clave": estatus.value,
                "etiqueta": estatus.label,
                "lotes": agregado["lotes"] or 0,
                "unidades": agregado["unidades"] or Decimal("0"),
                "valor": sum((lote.valor_actual for lote in qs), Decimal("0")),
            }
        )

    # Filtro opcional de movimientos por tipo.
    tipo = request.GET.get("tipo") or ""
    movimientos = MovimientoInventario.objects.select_related(
        "lote__producto", "usuario"
    ).order_by("-fecha", "-pk")
    if tipo:
        movimientos = movimientos.filter(tipo=tipo)

    return render(
        request,
        "core/inventario.html",
        {
            "pagina": "inventario",
            "hoy": hoy,
            "form_producto": forms.ProductoForm(),
            "form_almacen": forms.AlmacenForm(),
            "form_categoria": forms.CategoriaForm(),
            "form_marca": forms.MarcaForm(),
            "form_linea": forms.LineaProductoForm(),
            "form_ajuste": forms.AjusteInventarioForm(),
            "semaforo": semaforo,
            "valor_total": sum(
                (lote.valor_actual for lote in Lote.objects.disponibles()), Decimal("0")
            ),
            "lotes": Lote.objects.disponibles()
            .select_related("producto", "almacen")
            .order_by("producto__sku", "fecha_caducidad"),
            "agotados": Lote.objects.filter(cantidad_actual=0)
            .select_related("producto")
            .order_by("-pk")[:10],
            "por_producto": Lote.objects.disponibles()
            .values("producto__sku", "almacen__clave")
            .annotate(unidades=Sum("cantidad_actual"), lotes=Count("pk"))
            .order_by("producto__sku"),
            "almacenes": Almacen.objects.annotate(n_lotes=Count("lotes")).order_by("clave"),
            "productos": Producto.objects.select_related("linea", "marca").order_by("sku"),
            "movimientos": movimientos[:40],
            "tipos": MovimientoInventario.Tipo.choices,
            "tipo_activo": tipo,
            "hay_productos": Producto.objects.exists(),
            "hay_almacenes": Almacen.objects.exists(),
            "simulacion": _simular_fefo(request),
            "escaneo": _resolver_escaneo(request),
        },
    )


def _resolver_escaneo(request):
    """Buscador por código: pega o escanea algo y dice qué es y cuánto hay.

    Es la única cosa que hoy consume `inventario.identificacion.resolver_codigo`,
    y su valor real es ese: deja el contrato ejercitado y probado antes de que
    existan las pantallas de operación que de verdad lo van a usar.
    """
    texto = request.GET.get("codigo")
    if not texto:
        return None

    resultado = resolver_codigo(texto)
    lotes = list(existencias_de(resultado))
    return {
        "resultado": resultado,
        "lotes": lotes,
        "existencia": sum((lote.cantidad_actual for lote in lotes), Decimal("0")),
    }


def _simular_fefo(request):
    """Muestra qué lotes tomaría un surtido, sin tocar nada.

    Es una lectura pura de `LoteQuerySet.fefo`, el mismo método que usa el
    surtido real. Sirve para entender el orden de salida antes de ejecutarlo.
    """
    producto_id = request.GET.get("sim_producto")
    cantidad_txt = request.GET.get("sim_cantidad")
    if not producto_id or not cantidad_txt:
        return None
    try:
        producto = Producto.objects.get(pk=producto_id)
        cantidad = Decimal(cantidad_txt)
    except (Producto.DoesNotExist, ValueError, TypeError, InvalidOperation):
        return {"error": "Producto o cantidad inválidos."}
    if cantidad <= 0:
        return {"error": "La cantidad debe ser mayor a cero."}

    lotes = Lote.objects.fefo(producto, cantidad)
    pasos, restante = [], cantidad
    for lote in lotes:
        toma = min(restante, lote.cantidad_actual)
        restante -= toma
        pasos.append({"lote": lote, "toma": toma, "queda": lote.cantidad_actual - toma})
    return {
        "producto": producto,
        "cantidad": cantidad,
        "pasos": pasos,
        "faltante": restante if restante > 0 else Decimal("0"),
        "alcanza": restante <= 0,
    }


@require_POST
@staff_member_required
def ajustar_inventario(request):
    """Mete stock sin orden de compra, para poder probar el resto del sistema."""
    form = forms.AjusteInventarioForm(request.POST)
    if not form.is_valid():
        messages.error(request, f"Ajuste: {_errores(form)}")
        return _volver()

    datos = form.cleaned_data
    with transaction.atomic():
        lote = Lote.objects.create(
            producto=datos["producto"],
            almacen=datos["almacen"],
            fecha_caducidad=datos["fecha_caducidad"],
            costo_unitario=datos["producto"].precio_compra or Decimal("0"),
            cantidad_inicial=Decimal("0"),
            cantidad_actual=Decimal("0"),
        )
        registrar_movimiento(
            lote,
            MovimientoInventario.Tipo.AJUSTE_POSITIVO,
            datos["cantidad"],
            usuario=request.user,
            notas="Ajuste desde la página de inventario.",
        )
    messages.success(
        request,
        f"Lote {lote.codigo} creado con {datos['cantidad']} "
        f"(ajuste positivo, sin orden de compra detrás).",
    )
    if not lote.costo_unitario:
        messages.warning(
            request,
            f"{datos['producto'].sku} tiene precio_compra en 0, así que el lote "
            "entró con costo cero y no suma al valor del inventario.",
        )
    return _volver()


@require_POST
@staff_member_required
def crear(request, que):
    """Alta de catálogo desde la página de inventario."""
    if que not in FORMULARIOS:
        messages.error(request, f"No sé crear '{que}'.")
        return _volver()

    clase, etiqueta = FORMULARIOS[que]
    form = clase(request.POST)
    if not form.is_valid():
        messages.error(request, f"{etiqueta}: {_errores(form)}")
        return _volver()

    objeto = form.save()
    messages.success(request, f"{etiqueta} creada: {objeto}")
    return _volver()
