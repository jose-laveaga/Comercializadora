from django import forms
from django.contrib import admin, messages

from inventario.services import StockInsuficienteError

from .models import Cliente, Pedido, PedidoDetalle, Ruta, Surtido, SurtidoDetalle
from .services import SurtidoInvalidoError, surtir_pedido_detalle


@admin.register(Ruta)
class RutaAdmin(admin.ModelAdmin):
    list_display = ("clave", "nombre", "activa")
    list_filter = ("activa",)
    search_fields = ("nombre", "clave")  # necesario para el autocompletado
    ordering = ("clave",)


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = (
        "nombre_comercial",
        "ruta",
        "telefono_restaurante",
        "encargado_pedidos",
        "frecuencia_semanal",
        "dias_pedido_display",
        "activo",
    )
    list_filter = ("ruta", "activo", *[campo for campo, _ in Cliente.DIAS_SEMANA])
    search_fields = (
        "nombre_comercial",
        "razon_social",
        "rfc",
        "nombre_chef",
        "encargado_pedidos",
    )
    list_select_related = ("ruta",)
    list_per_page = 50
    save_on_top = True
    fieldsets = (
        ("Identidad", {"fields": ("nombre_comercial", "razon_social", "rfc", "activo")}),
        ("Contacto", {"fields": ("email", "telefono_restaurante", "direccion_entrega")}),
        (
            "Contactos operativos",
            {
                "fields": (
                    ("nombre_chef", "whatsapp_chef"),
                    ("encargado_pedidos", "whatsapp_encargado_pedidos"),
                )
            },
        ),
        (
            "Reparto",
            {"fields": ("ruta", tuple(campo for campo, _ in Cliente.DIAS_SEMANA))},
        ),
        ("Crédito", {"fields": (("dias_credito", "limite_credito"),)}),
        ("Notas", {"fields": ("tarea_pendiente", "anotaciones")}),
    )

    @admin.display(description="días de pedido")
    def dias_pedido_display(self, obj):
        return ", ".join(obj.dias_pedido) or "—"


class PedidoDetalleInline(admin.TabularInline):
    model = PedidoDetalle
    extra = 0
    autocomplete_fields = ("producto",)


@admin.register(Pedido)
class PedidoAdmin(admin.ModelAdmin):
    list_display = (
        "folio",
        "cliente",
        "almacen_origen",
        "fecha_pedido",
        "estatus",
        "subtotal_display",
        "esta_surtido",
    )
    list_display_links = ("folio",)
    list_filter = ("estatus", "cliente__ruta", "almacen_origen")
    search_fields = ("folio", "cliente__nombre_comercial")
    autocomplete_fields = ("cliente", "almacen_origen")
    list_select_related = ("cliente", "almacen_origen")
    list_per_page = 50
    save_on_top = True
    inlines = [PedidoDetalleInline]

    def get_readonly_fields(self, request, obj=None):
        return ("folio",) if obj else ()

    @admin.display(description="subtotal")
    def subtotal_display(self, obj):
        return f"{obj.subtotal:.2f}"

    @admin.display(description="surtido", boolean=True)
    def esta_surtido(self, obj):
        return obj.esta_surtido


class SurtidoDetalleForm(forms.ModelForm):
    """Captura solo qué línea y cuánto; el lote lo elige el servicio por FEFO.

    Las filas ya guardadas se muestran deshabilitadas: reeditarlas no volvería
    a mover el inventario, así que se corrigen con un movimiento de ajuste.
    """

    class Meta:
        model = SurtidoDetalle
        fields = ("pedido_detalle", "cantidad_surtida")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            for field in self.fields.values():
                field.disabled = True


class SurtidoDetalleInline(admin.TabularInline):
    model = SurtidoDetalle
    form = SurtidoDetalleForm
    extra = 1
    can_delete = False
    readonly_fields = ("lote",)

    def get_formset(self, request, obj=None, **kwargs):
        formset = super().get_formset(request, obj, **kwargs)
        if obj:
            formset.form.base_fields["pedido_detalle"].queryset = PedidoDetalle.objects.filter(
                pedido=obj.pedido
            )
        return formset


@admin.register(Surtido)
class SurtidoAdmin(admin.ModelAdmin):
    list_display = ("id", "pedido", "fecha_surtido", "surtido_por")
    list_filter = ("fecha_surtido",)
    search_fields = ("pedido__folio", "pedido__cliente__nombre_comercial")
    autocomplete_fields = ("pedido",)
    list_select_related = ("pedido", "surtido_por")
    save_on_top = True
    inlines = [SurtidoDetalleInline]

    def save_formset(self, request, form, formset, change):
        if formset.model is not SurtidoDetalle:
            super().save_formset(request, form, formset, change)
            return
        # Las filas no se guardan aquí: surtir_pedido_detalle decide los lotes
        # y crea un SurtidoDetalle por cada uno que consume.
        surtido = form.instance
        creados = []
        for subform in formset.forms:
            if subform.instance.pk or not subform.has_changed():
                continue
            pedido_detalle = subform.cleaned_data.get("pedido_detalle")
            cantidad = subform.cleaned_data.get("cantidad_surtida")
            if not pedido_detalle or not cantidad:
                continue
            try:
                creados += surtir_pedido_detalle(
                    pedido_detalle, cantidad, surtido, usuario=request.user
                )
            except (StockInsuficienteError, SurtidoInvalidoError) as error:
                self.message_user(request, str(error), level=messages.ERROR)
        # construct_change_message() los espera; normalmente los deja formset.save().
        formset.new_objects = creados
        formset.changed_objects = []
        formset.deleted_objects = []
