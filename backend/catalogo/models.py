from django.db import models, transaction


class Categoria(models.Model):
    """Familia: QUESOS, EMBUTIDOS, TOCINOS, CREMAS, LECHES, ABARROTES, etc.

    Sirve para agrupar, filtrar y navegar el catálogo. Ya no forma parte del
    SKU — ese trabajo lo hace LineaProducto.codigo.
    """

    nombre = models.CharField("nombre", max_length=100, unique=True)
    orden = models.PositiveIntegerField(
        "orden", default=0, help_text="Orden de aparición en listas y catálogo"
    )

    class Meta:
        verbose_name = "categoría"
        verbose_name_plural = "categorías"
        ordering = ["orden", "nombre"]

    def __str__(self):
        return self.nombre


class Marca(models.Model):
    """MARCA como modelo propio, para que corregir el nombre nunca toque un SKU.

    Esto resuelve la variación LYNCOTT / LUNCOTT / LYCNOTT y WUNSCH / WUNCH
    del archivo original: un solo registro, un código inmutable.
    """

    nombre = models.CharField("nombre", max_length=100, unique=True)
    codigo = models.CharField(
        "código",
        max_length=3,
        unique=True,
        help_text="Código de 3 letras usado en el SKU, p. ej. BEL, KRO, LYN.",
    )
    notas = models.CharField(
        "notas",
        max_length=255,
        blank=True,
        help_text='',
    )

    class Meta:
        verbose_name = "marca"
        verbose_name_plural = "marcas"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class LineaProducto(models.Model):
    """MASCARPONE, CHEDAR AMARILLO, JAMON DE PAVO...

    Agrupa variantes que sólo difieren en marca, tamaño o forma. Define el
    primer segmento del SKU y sirve de agrupador para el catálogo al cliente
    ("muéstrame todos los mascarpones").
    """

    nombre = models.CharField("nombre", max_length=100, unique=True)
    codigo = models.CharField(
        "código",
        max_length=4,
        unique=True,
        help_text="Código corto usado en el SKU, p. ej. MASC, CHED, JPAV. No cambia.",
    )
    categoria = models.ForeignKey(
        Categoria,
        on_delete=models.PROTECT,
        related_name="lineas",
        verbose_name="categoría",
    )

    class Meta:
        verbose_name = "línea de producto"
        verbose_name_plural = "líneas de producto"
        ordering = ["categoria__orden", "nombre"]

    def __str__(self):
        return self.nombre


class Producto(models.Model):
    """Una variante vendible: línea + marca + tamaño + forma."""

    class TipoPrecio(models.TextChoices):
        PIEZA = "pieza", "Por pieza"
        KILO = "kilo", "Por kilo"
        LITRO = "litro", "Por litro"

    class TipoAlmacenamiento(models.TextChoices):
        SECO = "seco", "Seco"
        FRIO = "frio", "Frío"
        CONGELADO = "congelado", "Congelado"

    class Unidad(models.TextChoices):
        GRAMO = "g", "g"
        MILILITRO = "ml", "ml"
        PIEZA = "pza", "pza"

    class Forma(models.TextChoices):
        BARRA = "BAR", "Barra"
        RUEDA = "RUE", "Rueda"
        REBANADO = "REB", "Rebanado"
        PORCION = "POR", "Porción"
        CUNA = "CUN", "Cuña"
        TETRAPAK = "TPK", "Tetrapak"
        CAJA = "CJA", "Caja"
        GRANEL = "GRA", "Granel"
        CUBETA = "CUB", "Cubeta"


    # --- identidad ----------------------------------------------------------
    sku = models.CharField(
        "SKU",
        max_length=32,
        unique=True,
        blank=True,
        help_text="Se genera solo al guardar por primera vez. No editar después.",
    )

    # --- clasificación ------------------------------------------------------
    linea = models.ForeignKey(
        LineaProducto,
        on_delete=models.PROTECT,
        related_name="productos",
        verbose_name="línea",
    )
    marca = models.ForeignKey(
        Marca,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="productos",
        verbose_name="marca",
        help_text=" ",
    )

    # --- tamaño y forma -----------------------------------------------------
    contenido_neto = models.DecimalField(
        "contenido neto",
        max_digits=9,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=" ",
    )
    unidad_contenido = models.CharField(
        "unidad", max_length=3, choices=Unidad.choices, blank=True
    )
    forma = models.CharField("forma", max_length=3, choices=Forma.choices, blank=True)

    """
    etiqueta_empaque = models.CharField(
        "empaque (texto original)",
        max_length=100,
        blank=True,
        help_text='Texto tal cual venía en el archivo, p. ej. "13 kg. / 1 kg"',
    )
    """


    # --- comercial ----------------------------------------------------------
    tipo_precio = models.CharField(
        "tipo de precio", max_length=10, choices=TipoPrecio.choices
    )
    precio_compra = models.DecimalField("precio compra", max_digits=10, decimal_places=2)
    precio_venta = models.DecimalField("precio venta", max_digits=10, decimal_places=2)
    proveedor_1 = models.CharField("proveedor 1", max_length=150, blank=True)
    proveedor_2 = models.CharField("proveedor 2", max_length=150, blank=True)

    # --- operación ----------------------------------------------------------
    vida_anaquel = models.IntegerField(verbose_name="días anaquel")
    requiere_produccion = models.BooleanField(
        "requiere producción",
        default=False,
        help_text="",
    )
    producido_de = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="salidas_produccion",
        verbose_name="producido a partir de",
        help_text="Producto base del que sale, p. ej. CHEDAR REBANADO ← CHEDAR BARRA",
    )
    tipo_almacenamiento = models.CharField(
        "almacenamiento",
        max_length=10,
        choices=TipoAlmacenamiento.choices,
        blank=True,
    )
    """
    dias_entrega = models.PositiveSmallIntegerField(
        "días de entrega",
        null=True,
        blank=True,
        help_text='Para productos bajo pedido — normalmente 4',
    )
    """

    disponible_para_pedido = models.BooleanField("disponible para pedido", default=True)
    notas = models.CharField("notas", max_length=255, blank=True)

    # --- catálogo al cliente (fase 2) ---------------------------------------
    visible_para_clientes = models.BooleanField("visible para clientes", default=False)
    descripcion = models.TextField("descripción", blank=True)
    imagen = models.ImageField("imagen", upload_to="productos/", blank=True, null=True)

    creado_en = models.DateTimeField("creado en", auto_now_add=True)
    actualizado_en = models.DateTimeField("actualizado en", auto_now=True)

    class Meta:
        verbose_name = "producto"
        verbose_name_plural = "productos"
        ordering = ["linea__categoria__orden", "linea__nombre", "marca__nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["linea", "marca", "contenido_neto", "unidad_contenido", "forma"],
                name="variante_producto_unica",
            )
        ]
        indexes = [
            models.Index(fields=["linea", "marca"]),
            models.Index(fields=["disponible_para_pedido", "visible_para_clientes"]),
        ]

    def __str__(self):
        return f"{self.sku} — {self.nombre_completo}"

    # --- derivados ----------------------------------------------------------
    @property
    def etiqueta_tamano(self):
        """"2260 g", "0.5 ml" — sin ceros de más."""
        if self.contenido_neto and self.unidad_contenido:
            return f"{self.contenido_neto.normalize():f} {self.unidad_contenido}"
        return ""

    @property
    def nombre_completo(self):
        """Nombre armado con los campos estructurados, para que nunca se desfase."""
        partes = [self.linea.nombre]
        if self.marca:
            partes.append(self.marca.nombre)
        if self.etiqueta_tamano:
            partes.append(self.etiqueta_tamano)
        if self.forma:
            partes.append(self.get_forma_display())
        return " · ".join(partes)

    @property
    def precio_por_kilo(self):
        """Precio comparable entre marcas, para ordenar en el listado."""
        if self.tipo_precio == self.TipoPrecio.KILO:
            return self.precio_venta
        if self.contenido_neto and self.unidad_contenido == self.Unidad.GRAMO:
            return self.precio_venta / (self.contenido_neto / 1000)
        return None

    @property
    def precio_por_litro(self):
        """Precio comparable entre marcas, para ordenar en el listado."""
        if self.tipo_precio == self.TipoPrecio.LITRO:
            return self.precio_venta
        if self.contenido_neto and self.unidad_contenido == self.Unidad.MILILITRO:
            return self.precio_venta / (self.contenido_neto / 1000)
        return None

    @property
    def margen_del_producto(self):
        if self.precio_compra and self.precio_venta:
            return round(((self.precio_venta - self.precio_compra) / self.precio_venta) * 100, ndigits=2)
        return None

    # --- SKU ----------------------------------------------------------------
    def save(self, *args, **kwargs):
        if not self.sku:
            self.sku = self._generar_sku()
        super().save(*args, **kwargs)

    def _sku_base(self):
        partes = [self.linea.codigo]
        if self.marca:
            partes.append(self.marca.codigo)
        if self.contenido_neto and self.unidad_contenido:
            partes.append(f"{int(self.contenido_neto)}{self.unidad_contenido.upper()}")
        if self.forma:
            partes.append(self.forma)
        return "-".join(partes)

    def _generar_sku(self):
        base = self._sku_base()
        with transaction.atomic():
            ocupados = set(
                Producto.objects.select_for_update()
                .filter(models.Q(sku=base) | models.Q(sku__startswith=f"{base}-"))
                .values_list("sku", flat=True)
            )
            if base not in ocupados:
                return base
            # Choque residual — poco probable, pero la unicidad queda garantizada.
            n = 2
            while f"{base}-{n}" in ocupados:
                n += 1
            return f"{base}-{n}"

    def sugerir_producto_base(self):
        """Busca el producido_de probable para un producto procesado.

        Misma línea y marca, pero en presentación a granel. Se usa en el script
        de importación y como apoyo en el admin, no como regla dura.
        """
        if not self.requiere_produccion:
            return None
        formas_granel = [self.Forma.BARRA, self.Forma.RUEDA, self.Forma.GRANEL, ""]
        return (
            Producto.objects.filter(
                linea=self.linea, marca=self.marca, forma__in=formas_granel
            )
            .exclude(pk=self.pk)
            .order_by("-contenido_neto")
            .first()
        )