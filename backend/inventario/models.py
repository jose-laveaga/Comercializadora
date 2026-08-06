from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from catalogo.models import Producto
from core.models import TimeStampedModel


class Almacen(TimeStampedModel):
    """Ubicación física donde se guardan lotes: bodega seca, cámara fría, etc."""

    nombre = models.CharField("nombre", max_length=100)
    clave = models.CharField(
        "clave",
        max_length=10,
        unique=True,
        help_text="Código corto para identificar el almacén, p. ej. ALM-01.",
    )
    tipo_temperatura = models.CharField(
        "temperatura de almacenamiento",
        max_length=10,
        choices=Producto.TipoAlmacenamiento.choices,
        help_text="Régimen de temperatura de este almacén.",
    )
    activo = models.BooleanField("activo", default=True)

    class Meta:
        verbose_name = "almacén"
        verbose_name_plural = "almacenes"
        ordering = ["clave"]

    def __str__(self):
        return f"{self.clave} — {self.nombre}"


class LoteQuerySet(models.QuerySet):
    def disponibles(self):
        return self.filter(cantidad_actual__gt=0, activo=True)

    def por_caducar(self, dias=30):
        """Lotes cuya fecha de caducidad cae dentro de los próximos `dias` días.

        No incluye lotes que ya caducaron — eso es responsabilidad de caducados().
        """
        hoy = timezone.now().date()
        return self.filter(fecha_caducidad__gte=hoy, fecha_caducidad__lte=hoy + timedelta(days=dias))

    def caducados(self):
        hoy = timezone.now().date()
        return self.filter(fecha_caducidad__lt=hoy)

    def con_estatus_caducidad(self, estatus):
        """Lotes cuyo semáforo de caducidad (Lote.estatus_caducidad) es `estatus`.

        Traduce los mismos umbrales de Lote.estatus_caducidad a un rango de
        fechas para poder filtrar en BD en vez de evaluar la property en Python
        lote por lote.
        """
        hoy = timezone.now().date()
        limite_amarillo = hoy + timedelta(days=Lote.UMBRAL_AMARILLO)
        limite_rojo = hoy + timedelta(days=Lote.UMBRAL_ROJO)
        limite_negro = hoy + timedelta(days=Lote.UMBRAL_NEGRO)
        if estatus == Lote.EstatusCaducidad.VERDE:
            return self.filter(fecha_caducidad__gt=limite_amarillo)
        if estatus == Lote.EstatusCaducidad.AMARILLO:
            return self.filter(fecha_caducidad__lte=limite_amarillo, fecha_caducidad__gt=limite_rojo)
        if estatus == Lote.EstatusCaducidad.ROJO:
            return self.filter(fecha_caducidad__lte=limite_rojo, fecha_caducidad__gt=limite_negro)
        if estatus == Lote.EstatusCaducidad.NEGRO:
            return self.filter(fecha_caducidad__lte=limite_negro)
        raise ValueError(f"estatus de caducidad desconocido: {estatus!r}")

    def fefo(self, producto, cantidad):
        """Lotes disponibles de `producto`, en orden First-Expired-First-Out,
        suficientes para cubrir `cantidad`.

        Es consultivo: no reserva ni bloquea stock. Dos llamadas concurrentes
        pueden devolver los mismos lotes. Quien consuma este resultado para
        registrar una salida debe volver a bloquear cada lote (select_for_update)
        al momento de registrar el movimiento real.
        """
        seleccionados = []
        acumulado = Decimal("0")
        lotes = self.disponibles().filter(producto=producto).order_by("fecha_caducidad", "pk")
        for lote in lotes:
            if acumulado >= cantidad:
                break
            seleccionados.append(lote)
            acumulado += lote.cantidad_actual
        return seleccionados


class Lote(TimeStampedModel):
    """Una entrada de inventario: cierta cantidad de un producto, en un almacén,
    con una fecha de caducidad y costo específicos.

    Su código es inmutable una vez generado. cantidad_actual solo debe cambiar
    a través de inventario.services.registrar_movimiento — nunca editándolo
    directamente (por eso el admin lo deja de solo lectura).
    """

    class EstatusCaducidad(models.TextChoices):
        """Semáforo de caducidad según los días restantes (dias_para_caducar).

        Los límites son cerrados hacia el estatus más urgente: exactamente 30
        días cuenta como AMARILLO (no VERDE), exactamente 15 como ROJO, y
        exactamente 5 (o menos, incluido ya caducado) como NEGRO.
        """

        VERDE = "verde", "Verde"
        AMARILLO = "amarillo", "Amarillo"
        ROJO = "rojo", "Rojo"
        NEGRO = "negro", "Negro"

    UMBRAL_AMARILLO = 30
    UMBRAL_ROJO = 15
    UMBRAL_NEGRO = 5

    producto = models.ForeignKey(
        Producto, on_delete=models.PROTECT, related_name="lotes", verbose_name="producto"
    )
    almacen = models.ForeignKey(
        Almacen, on_delete=models.PROTECT, related_name="lotes", verbose_name="almacén"
    )
    codigo = models.CharField(
        "código",
        max_length=48,
        blank=True,
        help_text="Se genera solo al guardar por primera vez. No editar después.",
    )
    codigo_proveedor = models.CharField(
        "código del proveedor", max_length=50, blank=True, help_text="Lote/lote tal como lo identifica el proveedor."
    )
    fecha_caducidad = models.DateField("fecha de caducidad")
    costo_unitario = models.DecimalField("costo unitario", max_digits=10, decimal_places=2)
    cantidad_inicial = models.DecimalField(
        "cantidad inicial",
        max_digits=12,
        decimal_places=3,
        help_text="Cantidad con la que se creó el registro (normalmente 0: el lote crece vía movimientos).",
    )
    cantidad_actual = models.DecimalField("cantidad actual", max_digits=12, decimal_places=3)
    activo = models.BooleanField("activo", default=True)

    objects = LoteQuerySet.as_manager()

    class Meta:
        verbose_name = "lote"
        verbose_name_plural = "lotes"
        ordering = ["fecha_caducidad", "codigo"]
        constraints = [
            models.UniqueConstraint(fields=["producto", "almacen", "codigo"], name="lote_unico_producto_almacen_codigo")
        ]
        indexes = [
            models.Index(fields=["producto", "almacen"]),
            models.Index(fields=["fecha_caducidad"]),
        ]

    def __str__(self):
        return self.codigo or f"lote sin código ({self.producto})"

    @property
    def dias_para_caducar(self):
        return (self.fecha_caducidad - timezone.now().date()).days

    @property
    def esta_caducado(self):
        return self.dias_para_caducar < 0

    @property
    def estatus_caducidad(self):
        dias = self.dias_para_caducar
        if dias > self.UMBRAL_AMARILLO:
            return self.EstatusCaducidad.VERDE
        if dias > self.UMBRAL_ROJO:
            return self.EstatusCaducidad.AMARILLO
        if dias > self.UMBRAL_NEGRO:
            return self.EstatusCaducidad.ROJO
        return self.EstatusCaducidad.NEGRO

    @property
    def valor_actual(self):
        return self.cantidad_actual * self.costo_unitario

    def save(self, *args, **kwargs):
        if not self.codigo:
            self.codigo = self._generar_codigo()
        super().save(*args, **kwargs)

    def _generar_codigo(self):
        base = f"{self.producto.sku}-{self.fecha_caducidad:%y%m%d}"
        with transaction.atomic():
            ocupados = set(
                Lote.objects.select_for_update()
                .filter(producto=self.producto, almacen=self.almacen, codigo__startswith=f"{base}-")
                .values_list("codigo", flat=True)
            )
            n = 1
            while f"{base}-{n}" in ocupados:
                n += 1
            return f"{base}-{n}"


class MovimientoInmutableError(Exception):
    """Se intentó modificar o borrar un MovimientoInventario existente."""


class MovimientoInventario(TimeStampedModel):
    """Registro permanente de un cambio en la cantidad de un lote.

    Append-only: no se puede editar ni borrar un movimiento una vez creado.
    La única forma soportada de crear uno es inventario.services.registrar_movimiento.
    """

    class Tipo(models.TextChoices):
        ENTRADA_COMPRA = "entrada_compra", "Entrada por compra"
        SALIDA_VENTA = "salida_venta", "Salida por venta"
        AJUSTE_POSITIVO = "ajuste_positivo", "Ajuste positivo"
        AJUSTE_NEGATIVO = "ajuste_negativo", "Ajuste negativo"
        MERMA = "merma", "Merma"
        CADUCADO = "caducado", "Caducado"
        DEVOLUCION = "devolucion", "Devolución a proveedor"

    lote = models.ForeignKey(
        Lote, on_delete=models.PROTECT, related_name="movimientos", verbose_name="lote"
    )
    tipo = models.CharField("tipo", max_length=20, choices=Tipo.choices)
    cantidad = models.DecimalField(
        "cantidad",
        max_digits=12,
        decimal_places=3,
        help_text="Con signo: positiva para entradas, negativa para salidas. El tipo no fuerza el signo.",
    )
    fecha = models.DateTimeField("fecha", default=timezone.now)
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="movimientos_inventario",
        verbose_name="usuario",
    )
    notas = models.CharField("notas", max_length=255, blank=True)
    recepcion_detalle = models.ForeignKey(
        "compras.RecepcionDetalle",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movimientos",
        verbose_name="detalle de recepción",
    )
    surtido_detalle = models.ForeignKey(
        "pedidos.SurtidoDetalle",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movimientos",
        verbose_name="detalle de surtido",
    )

    class Meta:
        verbose_name = "movimiento de inventario"
        verbose_name_plural = "movimientos de inventario"
        ordering = ["-fecha"]

    def __str__(self):
        return f"{self.get_tipo_display()} {self.cantidad} — {self.lote}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise MovimientoInmutableError("No se puede modificar un movimiento existente.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise MovimientoInmutableError("No se puede eliminar un movimiento; es un registro permanente.")
