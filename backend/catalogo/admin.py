from django.contrib import admin

from .models import Categoria, LineaProducto, Marca, Producto


@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "orden", "total_lineas", "total_productos")
    ordering = ("orden",)
    search_fields = ("nombre",)  # necesario para el autocompletado de LineaProducto

    @admin.display(description="líneas")
    def total_lineas(self, obj):
        return obj.lineas.count()

    @admin.display(description="productos")
    def total_productos(self, obj):
        return Producto.objects.filter(linea__categoria=obj).count()


@admin.register(Marca)
class MarcaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "codigo", "total_productos", "notas")
    search_fields = ("nombre", "codigo")  # necesario para el autocompletado
    ordering = ("nombre",)

    @admin.display(description="productos")
    def total_productos(self, obj):
        return obj.productos.count()

    def get_readonly_fields(self, request, obj=None):
        # El código alimenta los SKU, así que se congela una vez creada la marca.
        return ("codigo",) if obj else ()


@admin.register(LineaProducto)
class LineaProductoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "codigo", "categoria", "total_productos")
    list_filter = ("categoria",)
    search_fields = ("nombre", "codigo")  # necesario para el autocompletado
    autocomplete_fields = ("categoria",)
    ordering = ("categoria__orden", "nombre")

    @admin.display(description="productos")
    def total_productos(self, obj):
        return obj.productos.count()

    def get_readonly_fields(self, request, obj=None):
        return ("codigo",) if obj else ()


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = (
        "sku",
        "linea",
        "marca",
        "tamano",
        "forma",
        "tipo_precio",
        "precio",
        "precio_kilo",
        "tipo_almacenamiento",
        "requiere_produccion",
        "disponible_para_pedido",
    )
    list_display_links = ("sku",)
    list_editable = ("precio", "disponible_para_pedido")
    list_filter = (
        "linea__categoria",
        "tipo_almacenamiento",
        "forma",
        "requiere_produccion",
        "disponible_para_pedido",
        "visible_para_clientes",
        "tipo_precio",
        "marca",
    )
    search_fields = (
        "sku",
        #"codigo_barras",
        "linea__nombre",
        "marca__nombre",
        "proveedor_1",
        "proveedor_2",
    )
    autocomplete_fields = ("linea", "marca", "producido_de")
    list_select_related = ("linea", "linea__categoria", "marca")
    list_per_page = 50
    save_on_top = True

    fieldsets = (
        (None, {"fields": ("sku", "linea", "marca")}),
        (
            "Presentación",
            {
                "fields": (
                    ("contenido_neto", "unidad_contenido"),
                    "forma",
                )
            },
        ),
        ("Precio", {"fields": ("tipo_precio", "precio")}),
        ("Proveedores", {"fields": ("proveedor_1", "proveedor_2")}),
        (
            "Producción y almacenamiento",
            {
                "fields": (
                    "requiere_produccion",
                    "producido_de",
                    "tipo_almacenamiento",
                    "disponible_para_pedido",
                    "notas",
                )
            },
        ),
        (
            "Catálogo al cliente",
            {
                "fields": ("visible_para_clientes", "descripcion", "imagen"),
                "classes": ("collapse",),
            },
        ),
    )

    actions = ["marcar_no_disponible", "marcar_disponible", "vincular_productos_base"]

    @admin.display(description="tamaño", ordering="contenido_neto")
    def tamano(self, obj):
        return obj.etiqueta_tamano or obj.etiqueta_empaque or "—"

    @admin.display(description="$/kg")
    def precio_kilo(self, obj):
        valor = obj.precio_por_kilo
        return f"{valor:.2f}" if valor is not None else "—"

    def get_readonly_fields(self, request, obj=None):
        return ("sku",) if obj else ()

    @admin.action(description="Marcar como no disponible")
    def marcar_no_disponible(self, request, queryset):
        actualizados = queryset.update(disponible_para_pedido=False)
        self.message_user(request, f"{actualizados} productos marcados como no disponibles.")

    @admin.action(description="Marcar como disponible")
    def marcar_disponible(self, request, queryset):
        actualizados = queryset.update(disponible_para_pedido=True)
        self.message_user(request, f"{actualizados} productos marcados como disponibles.")

    @admin.action(description="Sugerir y vincular producto base (producido de)")
    def vincular_productos_base(self, request, queryset):
        vinculados = 0
        pendientes = queryset.filter(
            requiere_produccion=True, producido_de__isnull=True
        )
        for producto in pendientes:
            base = producto.sugerir_producto_base()
            if base:
                producto.producido_de = base
                producto.save(update_fields=["producido_de"])
                vinculados += 1
        self.message_user(request, f"{vinculados} productos vinculados a un producto base.")