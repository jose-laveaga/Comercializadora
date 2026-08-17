"""Dashboard temporal de verificación.

No es una vista de negocio: existe para ver de un vistazo si la lógica de
inventario, compras y pedidos está produciendo datos coherentes. La sección
importante es la de invariantes — todo lo demás es contexto para interpretarla.
"""

from decimal import Decimal

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Sum
from django.shortcuts import render
from django.utils import timezone

from catalogo.models import Producto
from compras.models import OrdenCompra
from inventario.models import Almacen, Lote, MovimientoInventario
from pedidos.models import Cliente, Pedido, PedidoDetalle, Ruta, SurtidoDetalle

# Tipos de movimiento que deben sumar y que deben restar. El modelo no fuerza
# el signo (lo dice el help_text de MovimientoInventario.cantidad), así que
# esta es justo la clase de incoherencia que vale la pena vigilar.
TIPOS_POSITIVOS = {
    MovimientoInventario.Tipo.ENTRADA_COMPRA,
    MovimientoInventario.Tipo.AJUSTE_POSITIVO,
}
TIPOS_NEGATIVOS = {
    MovimientoInventario.Tipo.SALIDA_VENTA,
    MovimientoInventario.Tipo.AJUSTE_NEGATIVO,
    MovimientoInventario.Tipo.MERMA,
    MovimientoInventario.Tipo.CADUCADO,
    MovimientoInventario.Tipo.DEVOLUCION,
}


def _invariantes():
    """Cada entrada: (nombre, regla, lista de filas que la violan).

    Una lista vacía significa que la invariante se cumple.
    """
    checks = []

    # 1. Si esto falla, alguien tocó cantidad_actual sin pasar por
    #    registrar_movimiento, que es justo lo que el servicio existe para evitar.
    descuadres = []
    for lote in Lote.objects.select_related("producto").annotate(
        suma_movimientos=Sum("movimientos__cantidad")
    ):
        esperado = lote.cantidad_inicial + (lote.suma_movimientos or Decimal("0"))
        if esperado != lote.cantidad_actual:
            descuadres.append(
                f"{lote.codigo}: cantidad_actual={lote.cantidad_actual}, "
                f"esperado={esperado} (inicial {lote.cantidad_inicial} "
                f"+ movs {lote.suma_movimientos or 0})"
            )
    checks.append(
        (
            "El stock de cada lote cuadra con sus movimientos",
            "cantidad_actual == cantidad_inicial + suma de movimientos",
            descuadres,
        )
    )

    # 2. Ningún lote en negativo.
    negativos = [
        f"{lote.codigo}: {lote.cantidad_actual}"
        for lote in Lote.objects.filter(cantidad_actual__lt=0)
    ]
    checks.append(("Ningún lote tiene existencia negativa", "cantidad_actual >= 0", negativos))

    # 3. El signo del movimiento corresponde a su tipo.
    signos = [
        f"#{mov.pk} {mov.get_tipo_display()} {mov.cantidad} en {mov.lote.codigo}"
        for mov in MovimientoInventario.objects.select_related("lote").filter(
            tipo__in=TIPOS_POSITIVOS, cantidad__lt=0
        )
    ] + [
        f"#{mov.pk} {mov.get_tipo_display()} +{mov.cantidad} en {mov.lote.codigo}"
        for mov in MovimientoInventario.objects.select_related("lote").filter(
            tipo__in=TIPOS_NEGATIVOS, cantidad__gt=0
        )
    ]
    checks.append(
        (
            "El signo de cada movimiento corresponde a su tipo",
            "entradas y ajustes positivos > 0; salidas, mermas y caducados < 0",
            signos,
        )
    )

    # 4. No se surtió de más en ninguna línea de pedido.
    sobresurtidos = []
    for detalle in PedidoDetalle.objects.select_related("pedido", "producto").annotate(
        surtido=Sum("surtidos__cantidad_surtida")
    ):
        surtido = detalle.surtido or Decimal("0")
        if surtido > detalle.cantidad_pedida:
            sobresurtidos.append(
                f"{detalle.pedido.folio} — {detalle.producto.sku}: "
                f"surtido {surtido} de {detalle.cantidad_pedida} pedidos"
            )
    checks.append(
        (
            "Ninguna línea de pedido se surtió de más",
            "cantidad surtida <= cantidad pedida",
            sobresurtidos,
        )
    )

    # 5. El estatus del pedido corresponde a lo realmente surtido. Se excluyen
    #    entregado y cancelado: son estados manuales posteriores al surtido.
    estatus_malos = []
    automaticos = {
        Pedido.Estatus.BORRADOR,
        Pedido.Estatus.CONFIRMADO,
        Pedido.Estatus.SURTIDO_PARCIAL,
        Pedido.Estatus.SURTIDO,
    }
    for pedido in Pedido.objects.filter(estatus__in=automaticos).prefetch_related(
        "detalles__surtidos"
    ):
        detalles = list(pedido.detalles.all())
        if not detalles:
            continue
        if all(d.cantidad_pendiente == 0 for d in detalles):
            esperado = Pedido.Estatus.SURTIDO
        elif any(d.cantidad_surtida > 0 for d in detalles):
            esperado = Pedido.Estatus.SURTIDO_PARCIAL
        else:
            continue  # borrador o confirmado: ambos válidos sin nada surtido
        if pedido.estatus != esperado:
            estatus_malos.append(
                f"{pedido.folio}: estatus '{pedido.estatus}', esperado '{esperado}'"
            )
    checks.append(
        (
            "El estatus de cada pedido corresponde a lo surtido",
            "todo surtido → surtido; algo surtido → surtido_parcial",
            estatus_malos,
        )
    )

    # 6 y 7. Trazabilidad: toda salida/entrada automática apunta a su origen.
    huerfanos = [
        f"#{mov.pk} {mov.cantidad} en {mov.lote.codigo}"
        for mov in MovimientoInventario.objects.select_related("lote").filter(
            tipo=MovimientoInventario.Tipo.SALIDA_VENTA, surtido_detalle__isnull=True
        )
    ]
    checks.append(
        (
            "Toda salida por venta está ligada a un surtido",
            "salida_venta.surtido_detalle no es nulo",
            huerfanos,
        )
    )
    entradas_huerfanas = [
        f"#{mov.pk} {mov.cantidad} en {mov.lote.codigo}"
        for mov in MovimientoInventario.objects.select_related("lote").filter(
            tipo=MovimientoInventario.Tipo.ENTRADA_COMPRA, recepcion_detalle__isnull=True
        )
    ]
    checks.append(
        (
            "Toda entrada por compra está ligada a una recepción",
            "entrada_compra.recepcion_detalle no es nulo",
            entradas_huerfanas,
        )
    )

    return checks


@staff_member_required
def dashboard(request):
    hoy = timezone.localdate()

    # --- semáforo de caducidad -------------------------------------------
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

    # --- existencias por producto y almacén -------------------------------
    existencias = (
        Lote.objects.disponibles()
        .values("producto__sku", "almacen__clave")
        .annotate(unidades=Sum("cantidad_actual"), lotes=Count("pk"))
        .order_by("producto__sku")
    )

    # --- pedidos con algo pendiente de surtir -----------------------------
    pedidos_pendientes = [
        p
        for p in Pedido.objects.exclude(
            estatus__in=[Pedido.Estatus.CANCELADO, Pedido.Estatus.ENTREGADO]
        )
        .select_related("cliente", "almacen_origen")
        .prefetch_related("detalles__producto", "detalles__surtidos")
        .order_by("fecha_pedido")
        if p.detalles.exists() and not p.esta_surtido
    ]

    invariantes = _invariantes()

    contexto = {
        "pagina": "dashboard",
        "hoy": hoy,
        "invariantes": invariantes,
        "invariantes_rotas": sum(1 for _nombre, _regla, fallas in invariantes if fallas),
        "semaforo": semaforo,
        "existencias": existencias,
        "caducados_con_stock": Lote.objects.disponibles()
        .filter(fecha_caducidad__lt=hoy)
        .select_related("producto", "almacen")
        .order_by("fecha_caducidad"),
        "pedidos_por_estatus": Pedido.objects.values("estatus")
        .annotate(n=Count("pk"))
        .order_by("estatus"),
        "pedidos_pendientes": pedidos_pendientes,
        "compras_abiertas": OrdenCompra.objects.exclude(
            estatus__in=[OrdenCompra.Estatus.RECIBIDA, OrdenCompra.Estatus.CANCELADA]
        )
        .select_related("proveedor")
        .prefetch_related("detalles__recepciones")
        .order_by("fecha_emision"),
        "carga_semanal": [
            {
                "dia": etiqueta,
                "clientes": Cliente.objects.filter(activo=True, **{campo: True}).count(),
            }
            for campo, etiqueta in Cliente.DIAS_SEMANA
        ],
        "movimientos": MovimientoInventario.objects.select_related(
            "lote__producto", "usuario"
        ).order_by("-fecha", "-pk")[:25],
        "conteos": [
            ("Productos", Producto.objects.count()),
            ("Almacenes", Almacen.objects.count()),
            ("Lotes", Lote.objects.count()),
            ("Movimientos", MovimientoInventario.objects.count()),
            ("Rutas", Ruta.objects.count()),
            ("Clientes", Cliente.objects.count()),
            ("Pedidos", Pedido.objects.count()),
            ("Líneas surtidas", SurtidoDetalle.objects.count()),
            ("Órdenes de compra", OrdenCompra.objects.count()),
        ],
        "valor_inventario": sum(
            (lote.valor_actual for lote in Lote.objects.disponibles()), Decimal("0")
        ),
    }
    return render(request, "core/dashboard.html", contexto)
