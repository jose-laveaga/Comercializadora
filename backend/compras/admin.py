from django.contrib import admin, messages

from .models import (
    OrdenCompra,
    OrdenCompraDetalle,
    Proveedor,
    ProveedorProducto,
    Recepcion,
    RecepcionDetalle,
)


class ProveedorProductoInline(admin.TabularInline):
    model = ProveedorProducto
    extra = 0
    autocomplete_fields = ("producto",)


@admin.register(Proveedor)
class ProveedorAdmin(admin.ModelAdmin):
    list_display = ("nombre", "rfc", "contacto", "telefono", "dias_entrega_estimados", "activo")
    list_filter = ("activo",)
    search_fields = ("nombre", "razon_social", "rfc")  # necesario para el autocompletado
    inlines = [ProveedorProductoInline]


class OrdenCompraDetalleInline(admin.TabularInline):
    model = OrdenCompraDetalle
    extra = 0
    autocomplete_fields = ("producto",)


@admin.register(OrdenCompra)
class OrdenCompraAdmin(admin.ModelAdmin):
    list_display = ("folio", "proveedor", "almacen_destino", "fecha_emision", "estatus", "subtotal_display", "esta_completa")
    list_display_links = ("folio",)
    list_filter = ("estatus", "proveedor", "almacen_destino")
    search_fields = ("folio", "proveedor__nombre")
    autocomplete_fields = ("proveedor", "almacen_destino")
    list_select_related = ("proveedor", "almacen_destino")
    list_per_page = 50
    save_on_top = True
    inlines = [OrdenCompraDetalleInline]

    def get_readonly_fields(self, request, obj=None):
        return ("folio",) if obj else ()

    @admin.display(description="subtotal")
    def subtotal_display(self, obj):
        return f"{obj.subtotal:.2f}"

    @admin.display(description="completa", boolean=True)
    def esta_completa(self, obj):
        return obj.esta_completa


class RecepcionDetalleInline(admin.TabularInline):
    model = RecepcionDetalle
    extra = 0
    can_delete = False
    # orden_detalle no usa autocomplete_fields: OrdenCompraDetalle no tiene su
    # propio ModelAdmin registrado (solo existe como inline de OrdenCompra), y
    # el autocompletado de Django exige que el modelo destino esté registrado
    # con search_fields propios.

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return ("orden_detalle", "cantidad_recibida", "fecha_caducidad", "costo_unitario_real")
        return ()


@admin.register(Recepcion)
class RecepcionAdmin(admin.ModelAdmin):
    list_display = ("id", "orden_compra", "fecha_recepcion", "folio_factura", "recibido_por")
    list_filter = ("fecha_recepcion",)
    search_fields = ("orden_compra__folio", "folio_factura")
    autocomplete_fields = ("orden_compra",)
    list_select_related = ("orden_compra", "recibido_por")
    save_on_top = True
    inlines = [RecepcionDetalleInline]

    def save_formset(self, request, form, formset, change):
        if formset.model is not RecepcionDetalle:
            super().save_formset(request, form, formset, change)
            return
        instances = formset.save(commit=False)
        for obj in instances:
            obj.save()
            producto = obj.orden_detalle.producto
            dias_totales = (obj.fecha_caducidad - obj.recepcion.fecha_recepcion).days
            if producto.vida_anaquel and dias_totales < 0.6 * producto.vida_anaquel:
                self.message_user(
                    request,
                    f"{producto}: recibido con solo {dias_totales}/{producto.vida_anaquel} "
                    "días de vida de anaquel restante.",
                    level=messages.WARNING,
                )
        formset.save_m2m()
