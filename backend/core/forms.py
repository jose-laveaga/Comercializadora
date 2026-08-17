"""Formularios de la página temporal de inventario.

Son deliberadamente mínimos: solo los campos necesarios para dar de alta lo que
hace falta y meter stock. Todo lo demás se edita en el admin.
"""

from datetime import timedelta
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from catalogo.models import Categoria, LineaProducto, Marca, Producto
from compras.models import OrdenCompra, OrdenCompraDetalle, Proveedor
from inventario.models import Almacen


class BaseConsolaForm(forms.ModelForm):
    """Deja los widgets uniformes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in self.fields.values():
            if isinstance(campo.widget, forms.TextInput | forms.NumberInput | forms.EmailInput):
                campo.widget.attrs.setdefault("autocomplete", "off")


class CategoriaForm(BaseConsolaForm):
    class Meta:
        model = Categoria
        fields = ("nombre", "orden")


class MarcaForm(BaseConsolaForm):
    class Meta:
        model = Marca
        fields = ("nombre", "codigo")


class LineaProductoForm(BaseConsolaForm):
    class Meta:
        model = LineaProducto
        fields = ("nombre", "codigo", "categoria")


class AlmacenForm(BaseConsolaForm):
    class Meta:
        model = Almacen
        fields = ("nombre", "clave", "tipo_temperatura")


class ProductoForm(BaseConsolaForm):
    class Meta:
        model = Producto
        fields = (
            "linea",
            "marca",
            "contenido_neto",
            "unidad_contenido",
            "forma",
            "tipo_precio",
            "precio_compra",
            "precio_venta",
            "vida_anaquel",
            "tipo_almacenamiento",
        )


class AjusteInventarioForm(forms.Form):
    """Entrada rápida de stock sin pasar por una orden de compra."""

    producto = forms.ModelChoiceField(queryset=Producto.objects.all())
    almacen = forms.ModelChoiceField(queryset=Almacen.objects.filter(activo=True))
    cantidad = forms.DecimalField(max_digits=12, decimal_places=3, min_value=0.001)
    fecha_caducidad = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=lambda: timezone.localdate() + timedelta(days=30),
    )


# --- compras ---------------------------------------------------------------


class OrdenCompraForm(BaseConsolaForm):
    """Encabezado de la orden. Las líneas se agregan después, en su ficha.

    Se parte en dos pasos a propósito: la consola no usa JavaScript, y un
    formulario que crece con filas dinámicas necesitaría uno. Crear primero la
    orden y luego agregarle líneas de una en una funciona con HTML plano.
    """

    class Meta:
        model = OrdenCompra
        fields = ("proveedor", "almacen_destino", "fecha_entrega_estimada", "notas")
        widgets = {
            "fecha_entrega_estimada": forms.DateInput(attrs={"type": "date"}),
            "notas": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["proveedor"].queryset = Proveedor.objects.filter(activo=True)
        self.fields["almacen_destino"].queryset = Almacen.objects.filter(activo=True)
        self.fields["fecha_entrega_estimada"].required = False


class OrdenCompraDetalleForm(BaseConsolaForm):
    """Una línea de la orden. El costo se prellena desde el catálogo del proveedor."""

    class Meta:
        model = OrdenCompraDetalle
        fields = ("producto", "cantidad_pedida", "unidad_compra", "unidades_por_caja", "costo_unitario")

    def __init__(self, *args, orden=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.orden = orden
        self.fields["unidades_por_caja"].initial = 1
        if orden is not None:
            ya_pedidos = orden.detalles.values_list("producto_id", flat=True)
            self.fields["producto"].queryset = Producto.objects.exclude(pk__in=ya_pedidos)

    def clean(self):
        datos = super().clean()
        producto = datos.get("producto")
        if self.orden is not None and producto is not None:
            if self.orden.detalles.filter(producto=producto).exists():
                raise ValidationError(
                    f"{producto.sku} ya está en esta orden; edita esa línea en vez de agregar otra."
                )
        return datos


class RecepcionForm(forms.Form):
    """Encabezado del acto de recibir: cuándo llegó y con qué factura."""

    fecha_recepcion = forms.DateField(
        label="fecha de recepción",
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=timezone.localdate,
    )
    folio_factura = forms.CharField(label="folio de factura", max_length=50, required=False)
    notas = forms.CharField(label="notas", max_length=255, required=False)


class RecepcionLineaForm(forms.Form):
    """Qué llegó de una línea, y si lo que faltó se da por perdido.

    Todos los campos son opcionales porque el formset trae una fila por línea
    pendiente de la orden y lo normal es capturar solo algunas. La validación
    cruzada es la que exige los datos que sí hacen falta cuando la fila trae
    mercancía.
    """

    detalle_id = forms.IntegerField(widget=forms.HiddenInput)
    cantidad_recibida = forms.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal("0.001"), required=False
    )
    fecha_caducidad = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    costo_unitario_real = forms.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0"), required=False
    )
    codigo_lote_proveedor = forms.CharField(max_length=50, required=False)
    cerrar_faltante = forms.BooleanField(required=False)
    motivo_faltante = forms.ChoiceField(
        choices=[("", "—")] + list(OrdenCompraDetalle.MotivoFaltante.choices), required=False
    )
    notas_faltante = forms.CharField(max_length=255, required=False)

    def clean(self):
        datos = super().clean()
        cantidad = datos.get("cantidad_recibida")
        cerrar = datos.get("cerrar_faltante")

        if cantidad:
            faltantes = [
                etiqueta
                for campo, etiqueta in (
                    ("fecha_caducidad", "la fecha de caducidad"),
                    ("costo_unitario_real", "el costo unitario real"),
                )
                if datos.get(campo) in (None, "")
            ]
            if faltantes:
                raise ValidationError(
                    f"Si capturas cantidad recibida también necesitas {' y '.join(faltantes)}."
                )

        if cerrar and not datos.get("motivo_faltante"):
            raise ValidationError("Para cerrar el faltante hay que indicar el motivo.")

        return datos


RecepcionLineaFormSet = forms.formset_factory(RecepcionLineaForm, extra=0)


class CerrarLineaForm(forms.Form):
    """Cierre suelto de una línea, fuera de un acto de recepción."""

    motivo_faltante = forms.ChoiceField(choices=OrdenCompraDetalle.MotivoFaltante.choices)
    notas_faltante = forms.CharField(max_length=255, required=False)
