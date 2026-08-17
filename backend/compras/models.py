from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from catalogo.models import Producto
from core.models import TimeStampedModel
from inventario.models import Almacen, Lote, MovimientoInventario
from inventario.services import registrar_movimiento


class Proveedor(TimeStampedModel):
    """Un proveedor de la distribuidora.

    `nombre` no tiene unique=True en BD (no lo pide el negocio), pero el
    comando de siembra lo usa como llave natural asumiendo que en la práctica
    no hay dos proveedores con el mismo nombre.
    """

    nombre = models.CharField("nombre", max_length=150)
    razon_social = models.CharField("razón social", max_length=200, blank=True)
    rfc = models.CharField("RFC", max_length=13, blank=True)
    contacto = models.CharField("contacto", max_length=150, blank=True)
    telefono = models.CharField("teléfono", max_length=20, blank=True)
    email = models.EmailField("correo", blank=True)
    direccion = models.TextField("dirección", blank=True)
    dias_entrega_estimados = models.PositiveSmallIntegerField("días de entrega estimados")
    activo = models.BooleanField("activo", default=True)

    class Meta:
        verbose_name = "proveedor"
        verbose_name_plural = "proveedores"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class ProveedorProducto(TimeStampedModel):
    """Qué producto ofrece qué proveedor, a qué costo y presentación."""

    proveedor = models.ForeignKey(
        Proveedor, on_delete=models.CASCADE, related_name="productos_ofrecidos", verbose_name="proveedor"
    )
    producto = models.ForeignKey(
        Producto, on_delete=models.PROTECT, related_name="proveedores_producto", verbose_name="producto"
    )
    sku_proveedor = models.CharField("SKU del proveedor", max_length=50, blank=True)
    costo_actual = models.DecimalField("costo actual", max_digits=10, decimal_places=2)
    unidades_por_caja = models.PositiveIntegerField("unidades por caja")
    pedido_minimo = models.DecimalField("pedido mínimo", max_digits=12, decimal_places=3)
    activo = models.BooleanField("activo", default=True)

    class Meta:
        verbose_name = "producto de proveedor"
        verbose_name_plural = "productos de proveedor"
        ordering = ["proveedor", "producto"]
        constraints = [
            models.UniqueConstraint(fields=["proveedor", "producto"], name="proveedor_producto_unico")
        ]

    def __str__(self):
        return f"{self.proveedor} — {self.producto}"


class OrdenCompra(TimeStampedModel):
    """Lo que se le pidió a un proveedor. folio es inmutable, autogenerado."""

    class Estatus(models.TextChoices):
        BORRADOR = "borrador", "Borrador"
        ENVIADA = "enviada", "Enviada"
        CONFIRMADA = "confirmada", "Confirmada"
        RECIBIDA_PARCIAL = "recibida_parcial", "Recibida parcial"
        RECIBIDA = "recibida", "Recibida"
        CERRADA_INCOMPLETA = "cerrada_incompleta", "Cerrada con faltante"
        CANCELADA = "cancelada", "Cancelada"

    # Estatus en los que la orden ya no espera más mercancía.
    ESTATUS_FINALES = frozenset(
        {Estatus.RECIBIDA, Estatus.CERRADA_INCOMPLETA, Estatus.CANCELADA}
    )

    # Los que deduce `recalcular_estatus` de los hechos. El resto son manuales:
    # dicen en qué punto del trámite va la orden, cosa que el sistema no sabe.
    ESTATUS_DEDUCIDOS = frozenset(
        {Estatus.RECIBIDA_PARCIAL, Estatus.RECIBIDA, Estatus.CERRADA_INCOMPLETA}
    )

    folio = models.CharField(
        "folio",
        max_length=20,
        blank=True,
        help_text="Se genera solo al guardar por primera vez. No editar después.",
    )
    proveedor = models.ForeignKey(
        Proveedor, on_delete=models.PROTECT, related_name="ordenes_compra", verbose_name="proveedor"
    )
    almacen_destino = models.ForeignKey(
        Almacen, on_delete=models.PROTECT, related_name="ordenes_compra", verbose_name="almacén destino"
    )
    fecha_emision = models.DateField("fecha de emisión", default=timezone.localdate)
    fecha_entrega_estimada = models.DateField("fecha de entrega estimada", null=True, blank=True)
    estatus = models.CharField("estatus", max_length=20, choices=Estatus.choices, default=Estatus.BORRADOR)
    moneda = models.CharField("moneda", max_length=3, default="MXN")
    notas = models.TextField("notas", blank=True)

    class Meta:
        verbose_name = "orden de compra"
        verbose_name_plural = "órdenes de compra"
        ordering = ["-fecha_emision", "-folio"]

    def __str__(self):
        return self.folio or f"orden sin folio ({self.proveedor})"

    @property
    def subtotal(self):
        return sum((d.importe for d in self.detalles.all()), Decimal("0"))

    @property
    def total(self):
        # Punto de extensión futuro para impuestos/flete; por ahora igual al subtotal.
        return self.subtotal

    @property
    def esta_completa(self):
        """Ya no queda nada por recibir — llegó todo o lo que faltó se cerró."""
        return all(d.cantidad_pendiente == 0 for d in self.detalles.all())

    @property
    def cantidad_faltante(self):
        """Lo que se dio por no recibido al cerrar líneas, sumado."""
        return sum((d.cantidad_faltante for d in self.detalles.all()), Decimal("0"))

    @property
    def esta_abierta(self):
        return self.estatus not in self.ESTATUS_FINALES

    def recalcular_estatus(self, guardar=True):
        """Deduce el estatus a partir de lo recibido y lo cerrado.

        Vive aquí y no en RecepcionDetalle porque ahora hay dos cosas que
        cambian el avance de una orden: recibir mercancía y cerrar una línea
        declarando que el resto ya no llega. Ambas deben terminar en el mismo
        cálculo o la orden queda contando una historia distinta según por dónde
        se le mueva.

        No toca las órdenes canceladas ni las que no tienen líneas todavía.
        """
        if self.estatus == self.Estatus.CANCELADA:
            return self.estatus

        detalles = list(self.detalles.all())
        if not detalles:
            return self.estatus

        if all(d.cantidad_pendiente == 0 for d in detalles):
            hubo_faltante = any(d.cantidad_faltante > 0 for d in detalles)
            nuevo = self.Estatus.CERRADA_INCOMPLETA if hubo_faltante else self.Estatus.RECIBIDA
        elif any(d.cantidad_recibida > 0 for d in detalles):
            nuevo = self.Estatus.RECIBIDA_PARCIAL
        elif self.estatus in self.ESTATUS_DEDUCIDOS:
            # Los hechos ya no sostienen el estatus que traía: pasa al reabrir
            # una línea que se había cerrado sin recibir nada. Si no se corrige
            # aquí, la orden se queda marcada como cerrada esperando mercancía.
            # Vuelve a «confirmada» porque es lo único que se puede afirmar: la
            # orden existe y espera al proveedor.
            nuevo = self.Estatus.CONFIRMADA
        else:
            # Nada recibido y nada cerrado: sigue donde el usuario la haya dejado
            # (borrador, enviada o confirmada).
            return self.estatus

        if nuevo != self.estatus:
            self.estatus = nuevo
            if guardar:
                self.save(update_fields=["estatus", "actualizado_en"])
        return self.estatus

    def save(self, *args, **kwargs):
        if not self.folio:
            self.folio = self._generar_folio()
        super().save(*args, **kwargs)

    def _generar_folio(self):
        anio = self.fecha_emision.year
        prefijo = f"OC-{anio}-"
        with transaction.atomic():
            ultimo = (
                OrdenCompra.objects.select_for_update()
                .filter(folio__startswith=prefijo)
                .order_by("-folio")
                .first()
            )
            siguiente = int(ultimo.folio.rsplit("-", 1)[-1]) + 1 if ultimo else 1
            return f"{prefijo}{siguiente:04d}"


class OrdenCompraDetalle(TimeStampedModel):
    """Una línea de una orden de compra: cuánto se pidió de un producto.

    Una línea puede cerrarse antes de recibirse completa. Eso distingue las dos
    situaciones que antes se veían igual: "faltan 3 y llegan el jueves" (línea
    abierta) contra "faltan 3 y ya no llegan" (línea cerrada, con motivo). Sin
    la distinción, una orden mal surtida se quedaba en «recibida parcial» para
    siempre y nadie podía preguntar en qué falla cada proveedor.
    """

    class UnidadCompra(models.TextChoices):
        CAJA = "caja", "Caja"
        PIEZA = "pieza", "Pieza"
        KILOGRAMO = "kilogramo", "Kilogramo"

    class MotivoFaltante(models.TextChoices):
        NO_SURTIDO = "no_surtido", "No lo surtió el proveedor"
        AGOTADO = "agotado", "Agotado con el proveedor"
        CALIDAD = "calidad", "Rechazado por calidad"
        CANCELADO = "cancelado", "Cancelado por nosotros"

    orden = models.ForeignKey(OrdenCompra, on_delete=models.CASCADE, related_name="detalles", verbose_name="orden")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="detalles_orden_compra", verbose_name="producto")
    cantidad_pedida = models.DecimalField("cantidad pedida", max_digits=12, decimal_places=3)
    unidad_compra = models.CharField("unidad de compra", max_length=10, choices=UnidadCompra.choices)
    unidades_por_caja = models.PositiveIntegerField(
        "unidades por caja", help_text="Fotografía al momento del pedido; no se actualiza si cambia después."
    )
    costo_unitario = models.DecimalField(
        "costo unitario", max_digits=10, decimal_places=2, help_text="Fotografía al momento del pedido."
    )

    # --- cierre con faltante -------------------------------------------------
    cerrado = models.BooleanField(
        "cerrado",
        default=False,
        help_text="Lo que faltó de esta línea ya no va a llegar; deja de contar como pendiente.",
    )
    motivo_faltante = models.CharField(
        "motivo del faltante", max_length=20, choices=MotivoFaltante.choices, blank=True
    )
    notas_faltante = models.CharField("notas del faltante", max_length=255, blank=True)
    cerrado_en = models.DateTimeField("cerrado en", null=True, blank=True)
    cerrado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lineas_compra_cerradas",
        verbose_name="cerrado por",
    )

    class Meta:
        verbose_name = "detalle de orden de compra"
        verbose_name_plural = "detalles de orden de compra"
        ordering = ["orden", "producto"]
        constraints = [models.UniqueConstraint(fields=["orden", "producto"], name="orden_compra_detalle_unico")]

    def __str__(self):
        return f"{self.orden} — {self.producto} ({self.cantidad_pedida})"

    @property
    def importe(self):
        return self.cantidad_pedida * self.costo_unitario

    @property
    def cantidad_recibida(self):
        total = self.recepciones.aggregate(total=models.Sum("cantidad_recibida"))["total"]
        return total or Decimal("0")

    @property
    def cantidad_pendiente(self):
        """Lo que todavía se espera del proveedor.

        Una línea cerrada no espera nada, aunque no haya llegado completa: lo
        que faltó se contabiliza en `cantidad_faltante`, no aquí.
        """
        if self.cerrado:
            return Decimal("0")
        return self.cantidad_pedida - self.cantidad_recibida

    @property
    def cantidad_faltante(self):
        """Lo que se pidió, no llegó, y se dio por perdido al cerrar la línea."""
        if not self.cerrado:
            return Decimal("0")
        return max(self.cantidad_pedida - self.cantidad_recibida, Decimal("0"))

    @property
    def puede_cerrarse(self):
        return not self.cerrado and self.cantidad_pendiente > 0


class Recepcion(TimeStampedModel):
    """Un evento de recepción de mercancía contra una orden de compra."""

    orden_compra = models.ForeignKey(
        OrdenCompra, on_delete=models.PROTECT, related_name="recepciones", verbose_name="orden de compra"
    )
    fecha_recepcion = models.DateField("fecha de recepción", default=timezone.localdate)
    folio_factura = models.CharField("folio de factura", max_length=50, blank=True)
    recibido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recepciones_recibidas",
        verbose_name="recibido por",
    )
    notas = models.TextField("notas", blank=True)

    class Meta:
        verbose_name = "recepción"
        verbose_name_plural = "recepciones"
        ordering = ["-fecha_recepcion"]

    def __str__(self):
        return f"Recepción {self.pk} — {self.orden_compra}"


class RecepcionDetalle(TimeStampedModel):
    """Una línea de recepción: cuánto llegó realmente de una línea de orden.

    Al crearse (no al editarse) dispara, dentro de una transacción: crear o
    aumentar el Lote correspondiente, registrar el MovimientoInventario de
    entrada, y recalcular el estatus de la orden de compra. Por eso el admin
    congela sus campos clave una vez guardada — una edición posterior no
    vuelve a disparar estos efectos.
    """

    recepcion = models.ForeignKey(Recepcion, on_delete=models.CASCADE, related_name="detalles", verbose_name="recepción")
    orden_detalle = models.ForeignKey(
        OrdenCompraDetalle, on_delete=models.PROTECT, related_name="recepciones", verbose_name="detalle de orden"
    )
    cantidad_recibida = models.DecimalField("cantidad recibida", max_digits=12, decimal_places=3)
    fecha_caducidad = models.DateField("fecha de caducidad")
    codigo_lote_proveedor = models.CharField("código de lote del proveedor", max_length=50, blank=True)
    costo_unitario_real = models.DecimalField("costo unitario real", max_digits=10, decimal_places=2)

    class Meta:
        verbose_name = "detalle de recepción"
        verbose_name_plural = "detalles de recepción"
        ordering = ["recepcion", "orden_detalle"]

    def __str__(self):
        return f"{self.recepcion} — {self.orden_detalle.producto} ({self.cantidad_recibida})"

    @property
    def importe(self):
        """Lo que esta línea agregó al valor del inventario."""
        return self.cantidad_recibida * self.costo_unitario_real

    def clean(self):
        super().clean()
        if self.fecha_caducidad and self.fecha_caducidad < timezone.now().date():
            raise ValidationError({"fecha_caducidad": "La fecha de caducidad no puede ser pasada."})
        if self.orden_detalle_id:
            if self.orden_detalle.cerrado:
                raise ValidationError(
                    {
                        "orden_detalle": (
                            f"La línea de {self.orden_detalle.producto} está cerrada "
                            f"({self.orden_detalle.get_motivo_faltante_display()}); "
                            "reábrela antes de recibir más."
                        )
                    }
                )
            limite = self.orden_detalle.cantidad_pendiente * Decimal("1.10")
            if self.cantidad_recibida > limite:
                raise ValidationError(
                    {
                        "cantidad_recibida": (
                            "Excede en más de 10% la cantidad pendiente "
                            f"({self.orden_detalle.cantidad_pendiente})."
                        )
                    }
                )

    def save(self, *args, **kwargs):
        es_creacion = self.pk is None
        if not es_creacion:
            super().save(*args, **kwargs)
            return
        with transaction.atomic():
            super().save(*args, **kwargs)
            lote, _creado = Lote.objects.select_for_update().get_or_create(
                producto=self.orden_detalle.producto,
                almacen=self.recepcion.orden_compra.almacen_destino,
                fecha_caducidad=self.fecha_caducidad,
                costo_unitario=self.costo_unitario_real,
                defaults={
                    "codigo_proveedor": self.codigo_lote_proveedor,
                    "cantidad_inicial": Decimal("0"),
                    "cantidad_actual": Decimal("0"),
                },
            )
            registrar_movimiento(
                lote,
                MovimientoInventario.Tipo.ENTRADA_COMPRA,
                self.cantidad_recibida,
                usuario=self.recepcion.recibido_por,
                recepcion_detalle=self,
            )
            self.orden_detalle.orden.recalcular_estatus()
