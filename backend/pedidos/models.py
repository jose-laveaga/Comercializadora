from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from catalogo.models import Producto
from core.models import TimeStampedModel
from inventario.models import Almacen, Lote


class Ruta(TimeStampedModel):
    """Ruta de reparto a la que se asigna un cliente."""

    nombre = models.CharField("nombre", max_length=100, unique=True)
    clave = models.CharField(
        "clave",
        max_length=10,
        unique=True,
        help_text="Código corto para identificar la ruta, p. ej. RT-01.",
    )
    activa = models.BooleanField("activa", default=True)

    class Meta:
        verbose_name = "ruta"
        verbose_name_plural = "rutas"
        ordering = ["clave"]

    def __str__(self):
        return f"{self.clave} — {self.nombre}"


class Cliente(TimeStampedModel):
    """Un cliente de la distribuidora, típicamente un restaurante.

    Contraparte de compras.Proveedor. Los datos fiscales quedan opcionales a
    propósito: en la práctica se da de alta al cliente en la ruta antes de
    tener su RFC, y exigirlo bloquearía el alta operativa.
    """

    DIAS_SEMANA = (
        ("pide_lunes", "Lunes"),
        ("pide_martes", "Martes"),
        ("pide_miercoles", "Miércoles"),
        ("pide_jueves", "Jueves"),
        ("pide_viernes", "Viernes"),
        ("pide_sabado", "Sábado"),
    )

    # --- identidad ----------------------------------------------------------
    nombre_comercial = models.CharField("nombre comercial", max_length=150)
    razon_social = models.CharField("razón social", max_length=200, blank=True)
    rfc = models.CharField("RFC", max_length=13, blank=True)

    # --- contacto general ---------------------------------------------------
    email = models.EmailField("correo", blank=True)
    telefono_restaurante = models.CharField("teléfono del restaurante", max_length=20, blank=True)
    direccion_entrega = models.TextField("dirección de entrega", blank=True)

    # --- contactos operativos -----------------------------------------------
    nombre_chef = models.CharField("nombre del chef", max_length=150, blank=True)
    whatsapp_chef = models.CharField("WhatsApp del chef", max_length=20, blank=True)
    encargado_pedidos = models.CharField("encargado de pedidos", max_length=150, blank=True)
    whatsapp_encargado_pedidos = models.CharField(
        "WhatsApp del encargado de pedidos", max_length=20, blank=True
    )

    # --- reparto ------------------------------------------------------------
    ruta = models.ForeignKey(
        Ruta, on_delete=models.PROTECT, related_name="clientes", verbose_name="ruta"
    )
    pide_lunes = models.BooleanField("lunes", default=False)
    pide_martes = models.BooleanField("martes", default=False)
    pide_miercoles = models.BooleanField("miércoles", default=False)
    pide_jueves = models.BooleanField("jueves", default=False)
    pide_viernes = models.BooleanField("viernes", default=False)
    pide_sabado = models.BooleanField("sábado", default=False)

    # --- crédito ------------------------------------------------------------
    dias_credito = models.PositiveSmallIntegerField("días de crédito", default=0)
    limite_credito = models.DecimalField(
        "límite de crédito",
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Vacío significa sin límite definido; 0 significa solo contado.",
    )

    # --- notas --------------------------------------------------------------
    tarea_pendiente = models.TextField(
        "tarea", blank=True, help_text="Algo que va a cambiar y hay que atender."
    )
    anotaciones = models.TextField("anotaciones", blank=True)

    activo = models.BooleanField("activo", default=True)

    class Meta:
        verbose_name = "cliente"
        verbose_name_plural = "clientes"
        ordering = ["nombre_comercial"]
        indexes = [models.Index(fields=["ruta", "activo"])]

    def __str__(self):
        return self.nombre_comercial

    @property
    def dias_pedido(self):
        return [etiqueta for campo, etiqueta in self.DIAS_SEMANA if getattr(self, campo)]

    @property
    def frecuencia_semanal(self):
        return len(self.dias_pedido)


class Pedido(TimeStampedModel):
    """Lo que pidió un cliente. folio es inmutable, autogenerado."""

    class Estatus(models.TextChoices):
        BORRADOR = "borrador", "Borrador"
        CONFIRMADO = "confirmado", "Confirmado"
        SURTIDO_PARCIAL = "surtido_parcial", "Surtido parcial"
        SURTIDO = "surtido", "Surtido"
        ENTREGADO = "entregado", "Entregado"
        CANCELADO = "cancelado", "Cancelado"

    folio = models.CharField(
        "folio",
        max_length=20,
        blank=True,
        help_text="Se genera solo al guardar por primera vez. No editar después.",
    )
    cliente = models.ForeignKey(
        Cliente, on_delete=models.PROTECT, related_name="pedidos", verbose_name="cliente"
    )
    almacen_origen = models.ForeignKey(
        Almacen, on_delete=models.PROTECT, related_name="pedidos", verbose_name="almacén origen"
    )
    fecha_pedido = models.DateField("fecha del pedido", default=timezone.localdate)
    fecha_entrega_estimada = models.DateField("fecha de entrega estimada", null=True, blank=True)
    estatus = models.CharField(
        "estatus", max_length=20, choices=Estatus.choices, default=Estatus.BORRADOR
    )
    notas = models.TextField("notas", blank=True)

    class Meta:
        verbose_name = "pedido"
        verbose_name_plural = "pedidos"
        ordering = ["-fecha_pedido", "-folio"]

    def __str__(self):
        return self.folio or f"pedido sin folio ({self.cliente})"

    @property
    def subtotal(self):
        return sum((d.importe for d in self.detalles.all()), Decimal("0"))

    @property
    def total(self):
        # Punto de extensión futuro para impuestos/flete; por ahora igual al subtotal.
        return self.subtotal

    @property
    def esta_surtido(self):
        return all(d.cantidad_pendiente == 0 for d in self.detalles.all())

    def save(self, *args, **kwargs):
        if not self.folio:
            self.folio = self._generar_folio()
        super().save(*args, **kwargs)

    def _generar_folio(self):
        anio = self.fecha_pedido.year
        prefijo = f"PED-{anio}-"
        with transaction.atomic():
            ultimo = (
                Pedido.objects.select_for_update()
                .filter(folio__startswith=prefijo)
                .order_by("-folio")
                .first()
            )
            siguiente = int(ultimo.folio.rsplit("-", 1)[-1]) + 1 if ultimo else 1
            return f"{prefijo}{siguiente:04d}"


class PedidoDetalle(TimeStampedModel):
    """Una línea de un pedido: cuánto se pidió de un producto."""

    pedido = models.ForeignKey(
        Pedido, on_delete=models.CASCADE, related_name="detalles", verbose_name="pedido"
    )
    producto = models.ForeignKey(
        Producto, on_delete=models.PROTECT, related_name="detalles_pedido", verbose_name="producto"
    )
    cantidad_pedida = models.DecimalField("cantidad pedida", max_digits=12, decimal_places=3)
    precio_unitario = models.DecimalField(
        "precio unitario",
        max_digits=10,
        decimal_places=2,
        help_text="Fotografía al momento del pedido.",
    )

    class Meta:
        verbose_name = "detalle de pedido"
        verbose_name_plural = "detalles de pedido"
        ordering = ["pedido", "producto"]
        constraints = [
            models.UniqueConstraint(fields=["pedido", "producto"], name="pedido_detalle_unico")
        ]

    def __str__(self):
        return f"{self.pedido} — {self.producto} ({self.cantidad_pedida})"

    @property
    def importe(self):
        return self.cantidad_pedida * self.precio_unitario

    @property
    def cantidad_surtida(self):
        total = self.surtidos.aggregate(total=models.Sum("cantidad_surtida"))["total"]
        return total or Decimal("0")

    @property
    def cantidad_pendiente(self):
        return self.cantidad_pedida - self.cantidad_surtida


class Surtido(TimeStampedModel):
    """Un evento de salida de mercancía contra un pedido."""

    pedido = models.ForeignKey(
        Pedido, on_delete=models.PROTECT, related_name="surtidos", verbose_name="pedido"
    )
    fecha_surtido = models.DateField("fecha de surtido", default=timezone.localdate)
    surtido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="surtidos_realizados",
        verbose_name="surtido por",
    )
    notas = models.TextField("notas", blank=True)

    class Meta:
        verbose_name = "surtido"
        verbose_name_plural = "surtidos"
        ordering = ["-fecha_surtido"]

    def __str__(self):
        return f"Surtido {self.pk} — {self.pedido}"


class SurtidoDetalle(TimeStampedModel):
    """Cuánto se tomó de un lote concreto para cubrir una línea de pedido.

    A diferencia de compras.RecepcionDetalle, aquí hay una fila por lote
    consumido y no por línea de pedido: una sola línea puede necesitar varios
    lotes si el más próximo a caducar no alcanza.

    No dispara efectos desde save(); quien las crea es
    pedidos.services.surtir_pedido_detalle, que es donde se decide qué lotes
    usar y se registran los movimientos de inventario.
    """

    surtido = models.ForeignKey(
        Surtido, on_delete=models.CASCADE, related_name="detalles", verbose_name="surtido"
    )
    pedido_detalle = models.ForeignKey(
        PedidoDetalle,
        on_delete=models.PROTECT,
        related_name="surtidos",
        verbose_name="detalle de pedido",
    )
    lote = models.ForeignKey(
        Lote, on_delete=models.PROTECT, related_name="surtidos", verbose_name="lote"
    )
    cantidad_surtida = models.DecimalField(
        "cantidad surtida",
        max_digits=12,
        decimal_places=3,
        help_text="Siempre positiva; el signo negativo se aplica al movimiento de inventario.",
    )

    class Meta:
        verbose_name = "detalle de surtido"
        verbose_name_plural = "detalles de surtido"
        ordering = ["surtido", "pedido_detalle"]

    def __str__(self):
        return f"{self.pedido_detalle.producto} — {self.lote} ({self.cantidad_surtida})"
