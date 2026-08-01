from django.contrib import admin
from django.utils.html import format_html

from .models import Almacen, Lote, MovimientoInventario

COLOR_ESTATUS_CADUCIDAD = {
    Lote.EstatusCaducidad.VERDE: "#2e7d32",
    Lote.EstatusCaducidad.AMARILLO: "#f9a825",
    Lote.EstatusCaducidad.ROJO: "#c62828",
    Lote.EstatusCaducidad.NEGRO: "#212121",
}


@admin.register(Almacen)
class AlmacenAdmin(admin.ModelAdmin):
    list_display = ("clave", "nombre", "tipo_temperatura", "activo")
    list_filter = ("tipo_temperatura", "activo")
    search_fields = ("nombre", "clave")  # necesario para el autocompletado
    ordering = ("clave",)


class PorCaducarListFilter(admin.SimpleListFilter):
    title = "por caducar"
    parameter_name = "por_caducar"

    def lookups(self, request, model_admin):
        return (("7", "en 7 días"), ("15", "en 15 días"), ("30", "en 30 días"))

    def queryset(self, request, queryset):
        if not self.value():
            return queryset
        dias_por_caducar = Lote.objects.por_caducar(dias=int(self.value())).values_list("pk", flat=True)
        return queryset.filter(pk__in=dias_por_caducar)


class EstatusCaducidadListFilter(admin.SimpleListFilter):
    title = "estatus de caducidad"
    parameter_name = "estatus_caducidad"

    def lookups(self, request, model_admin):
        return Lote.EstatusCaducidad.choices

    def queryset(self, request, queryset):
        if not self.value():
            return queryset
        return queryset.con_estatus_caducidad(self.value())


class SoloLecturaAdminMixin:
    """Deja un modelo navegable en el admin pero sin alta/edición/borrado.

    Se usa en Lote y MovimientoInventario: ambos solo deben cambiar a través
    de inventario.services.registrar_movimiento (directo o vía recepciones de
    compras), nunca a mano desde el admin.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Lote)
class LoteAdmin(SoloLecturaAdminMixin, admin.ModelAdmin):
    list_display = (
        "codigo",
        "producto",
        "almacen",
        "fecha_caducidad",
        "dias_para_caducar",
        "estatus_caducidad_badge",
        "cantidad_actual",
        "valor_actual",
        "activo",
    )
    list_filter = ("almacen", "activo", PorCaducarListFilter, EstatusCaducidadListFilter)
    search_fields = ("codigo", "producto__sku", "codigo_proveedor")
    list_select_related = ("producto", "almacen")
    list_per_page = 50
    readonly_fields = [f.name for f in Lote._meta.fields]

    @admin.display(description="días para caducar", ordering="fecha_caducidad")
    def dias_para_caducar(self, obj):
        return obj.dias_para_caducar

    @admin.display(description="estatus", ordering="fecha_caducidad")
    def estatus_caducidad_badge(self, obj):
        estatus = obj.estatus_caducidad
        return format_html(
            '<span style="background:{}; color:#fff; padding:2px 8px; '
            'border-radius:10px; font-size:11px; white-space:nowrap;">{}</span>',
            COLOR_ESTATUS_CADUCIDAD[estatus],
            estatus.label,
        )

    @admin.display(description="valor actual")
    def valor_actual(self, obj):
        return f"{obj.valor_actual:.2f}"


@admin.register(MovimientoInventario)
class MovimientoInventarioAdmin(SoloLecturaAdminMixin, admin.ModelAdmin):
    list_display = ("fecha", "lote", "tipo", "cantidad", "usuario", "recepcion_detalle")
    list_filter = ("tipo",)
    search_fields = ("lote__codigo", "notas")
    list_select_related = ("lote", "usuario")
    list_per_page = 50
    readonly_fields = [f.name for f in MovimientoInventario._meta.fields]
