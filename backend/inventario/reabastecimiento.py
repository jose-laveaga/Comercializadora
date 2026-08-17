"""Compara la existencia contra el estándar y dice qué hay que pedir.

Responde tres preguntas que hoy se contestan de memoria: cuánto debería haber,
cuánto hay de verdad, y si lo que falta ya viene en camino en alguna orden.

La tercera es la que hace útil a las otras dos. Sin ella, el sistema alerta por
un desabasto que alguien ya resolvió el lunes, y a la tercera alerta falsa nadie
vuelve a mirar la pantalla.

Este módulo importa `compras` mientras que `compras` importa `inventario.models`:
no hay ciclo porque nadie en `compras` importa este archivo, y nada dentro de
`inventario.models` lo importa tampoco. Vive en `inventario` y no en `compras`
porque la pregunta es sobre existencias; las órdenes solo son una de las
respuestas.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce

from compras.models import OrdenCompra, OrdenCompraDetalle

from .models import EstandarInventario, Lote

CERO = Decimal("0")

# Órdenes que cuentan como reabastecimiento en camino. Un borrador no salió al
# proveedor y no garantiza nada, así que se reporta aparte en vez de sumarse:
# dar por cubierto un faltante con una orden que lleva semanas sin enviarse es
# peor que no tener el dato.
ESTATUS_EN_CAMINO = (
    OrdenCompra.Estatus.ENVIADA,
    OrdenCompra.Estatus.CONFIRMADA,
    OrdenCompra.Estatus.RECIBIDA_PARCIAL,
)


class EstatusAbasto:
    SUFICIENTE = "suficiente"
    CUBIERTO = "cubierto"
    PEDIR = "pedir"

    ETIQUETAS = {
        SUFICIENTE: "Suficiente",
        CUBIERTO: "Cubierto por orden",
        PEDIR: "Hay que pedir",
    }


@dataclass
class OrdenEnCamino:
    """Una orden abierta que traería mercancía de este producto."""

    pk: int
    folio: str
    estatus: str
    estatus_display: str
    pendiente: Decimal
    fecha_entrega_estimada: object = None


@dataclass
class Diagnostico:
    """El veredicto de un producto en un almacén."""

    estandar: EstandarInventario
    existencia: Decimal = CERO
    excluido: Decimal = CERO
    en_camino: Decimal = CERO
    en_borrador: Decimal = CERO
    ordenes: list = field(default_factory=list)
    borradores: list = field(default_factory=list)

    @property
    def proyectado(self):
        """Lo que se va a tener cuando llegue lo que ya se pidió."""
        return self.existencia + self.en_camino

    @property
    def estatus(self):
        if self.existencia >= self.estandar.cantidad_minima:
            return EstatusAbasto.SUFICIENTE
        if self.proyectado >= self.estandar.cantidad_minima:
            return EstatusAbasto.CUBIERTO
        return EstatusAbasto.PEDIR

    @property
    def estatus_display(self):
        return EstatusAbasto.ETIQUETAS[self.estatus]

    @property
    def requiere_pedir(self):
        return self.estatus == EstatusAbasto.PEDIR

    @property
    def sugerido(self):
        """Cuánto pedir para llegar al objetivo, descontando lo que ya viene.

        Se descuenta lo que está en camino a propósito: pedir hasta el objetivo
        ignorando una orden en tránsito es como se acumula el sobreinventario
        que después se caduca.
        """
        if not self.requiere_pedir:
            return CERO
        return max(self.estandar.cantidad_objetivo - self.proyectado, CERO)

    @property
    def faltante_contra_minimo(self):
        return max(self.estandar.cantidad_minima - self.existencia, CERO)

    @property
    def cobertura(self):
        """Qué porcentaje del objetivo cubre la existencia actual."""
        if not self.estandar.cantidad_objetivo:
            return None
        return round(self.existencia / self.estandar.cantidad_objetivo * 100, 1)


def _existencias_por_llave(almacen=None):
    """Existencia útil y existencia descartada, agrupadas por (producto, almacén)."""
    utiles = Lote.objects.utiles()
    descartados = Lote.objects.disponibles().exclude(
        pk__in=Lote.objects.utiles().values("pk")
    )
    if almacen is not None:
        utiles = utiles.filter(almacen=almacen)
        descartados = descartados.filter(almacen=almacen)

    def agrupar(queryset):
        filas = queryset.values("producto_id", "almacen_id").annotate(total=Sum("cantidad_actual"))
        return {(f["producto_id"], f["almacen_id"]): f["total"] or CERO for f in filas}

    return agrupar(utiles), agrupar(descartados)


def _lineas_abiertas(estatus, almacen=None):
    """Líneas de orden que todavía esperan mercancía, con su pendiente calculado.

    El pendiente se anota en SQL en vez de usar la property del modelo para no
    disparar una consulta por línea. Las líneas cerradas quedan fuera: su
    faltante ya se dio por perdido y contarlo haría creer que viene mercancía
    que nadie va a mandar.
    """
    lineas = (
        OrdenCompraDetalle.objects.filter(cerrado=False, orden__estatus__in=estatus)
        .select_related("orden")
        .annotate(
            recibida=Coalesce(
                Sum("recepciones__cantidad_recibida"),
                Value(CERO, output_field=DecimalField(max_digits=12, decimal_places=3)),
            )
        )
        .annotate(pendiente=F("cantidad_pedida") - F("recibida"))
        .filter(pendiente__gt=0)
    )
    if almacen is not None:
        lineas = lineas.filter(orden__almacen_destino=almacen)
    return lineas


def _en_camino_por_llave(estatus, almacen=None):
    """Suma el pendiente por (producto, almacén) y guarda de qué órdenes viene."""
    totales = {}
    detalle = {}
    for linea in _lineas_abiertas(estatus, almacen):
        llave = (linea.producto_id, linea.orden.almacen_destino_id)
        totales[llave] = totales.get(llave, CERO) + linea.pendiente
        detalle.setdefault(llave, []).append(
            OrdenEnCamino(
                pk=linea.orden.pk,
                folio=linea.orden.folio,
                estatus=linea.orden.estatus,
                estatus_display=linea.orden.get_estatus_display(),
                pendiente=linea.pendiente,
                fecha_entrega_estimada=linea.orden.fecha_entrega_estimada,
            )
        )
    return totales, detalle


def evaluar(almacen=None, solo_activos=True):
    """Diagnostica todos los estándares definidos, en orden de urgencia.

    Devuelve primero lo que hay que pedir, luego lo cubierto por una orden y al
    final lo suficiente; dentro de cada grupo, lo más lejos del objetivo arriba.
    """
    estandares = EstandarInventario.objects.select_related(
        "producto__linea", "producto__marca", "almacen"
    )
    if solo_activos:
        estandares = estandares.filter(activo=True)
    if almacen is not None:
        estandares = estandares.filter(almacen=almacen)

    utiles, descartados = _existencias_por_llave(almacen)
    en_camino, ordenes = _en_camino_por_llave(ESTATUS_EN_CAMINO, almacen)
    en_borrador, borradores = _en_camino_por_llave((OrdenCompra.Estatus.BORRADOR,), almacen)

    diagnosticos = []
    for estandar in estandares:
        llave = (estandar.producto_id, estandar.almacen_id)
        diagnosticos.append(
            Diagnostico(
                estandar=estandar,
                existencia=utiles.get(llave, CERO),
                excluido=descartados.get(llave, CERO),
                en_camino=en_camino.get(llave, CERO),
                en_borrador=en_borrador.get(llave, CERO),
                ordenes=ordenes.get(llave, []),
                borradores=borradores.get(llave, []),
            )
        )

    orden_estatus = {EstatusAbasto.PEDIR: 0, EstatusAbasto.CUBIERTO: 1, EstatusAbasto.SUFICIENTE: 2}
    diagnosticos.sort(key=lambda d: (orden_estatus[d.estatus], -(d.cobertura or 0)))
    return diagnosticos


def resumen(diagnosticos):
    """Cuántos productos hay en cada estatus, para el encabezado de la página."""
    conteo = {estatus: 0 for estatus in EstatusAbasto.ETIQUETAS}
    for diagnostico in diagnosticos:
        conteo[diagnostico.estatus] += 1
    return conteo


def productos_sin_estandar(almacen=None):
    """Productos que se mueven pero a los que nadie les fijó un estándar.

    Son los huecos del tablero: mientras no tengan mínimo y objetivo, ningún
    desabasto suyo va a aparecer en la lista.
    """
    from catalogo.models import Producto

    con_estandar = EstandarInventario.objects.all()
    if almacen is not None:
        con_estandar = con_estandar.filter(almacen=almacen)
    return (
        Producto.objects.exclude(pk__in=con_estandar.values("producto_id"))
        .select_related("linea", "marca")
        .order_by("sku")
    )
