from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from . import gtin
from .models import Categoria, CodigoBarras, LineaProducto, Marca, Producto


def crear_producto(**extra):
    """Producto mínimo válido. Reutiliza el catálogo base entre llamadas."""
    categoria, _ = Categoria.objects.get_or_create(nombre="QUESOS")
    linea, _ = LineaProducto.objects.get_or_create(
        codigo="MASC", defaults={"nombre": "MASCARPONE", "categoria": categoria}
    )
    marca, _ = Marca.objects.get_or_create(codigo="LYN", defaults={"nombre": "LYNCOTT"})
    datos = {
        "linea": linea,
        "marca": marca,
        "contenido_neto": Decimal("2260"),
        "unidad_contenido": Producto.Unidad.GRAMO,
        "forma": Producto.Forma.BARRA,
        "tipo_precio": Producto.TipoPrecio.PIEZA,
        "precio_compra": Decimal("100.00"),
        "precio_venta": Decimal("150.00"),
        "vida_anaquel": 90,
    }
    datos.update(extra)
    return Producto.objects.create(**datos)


class DigitoVerificadorTests(TestCase):
    """Casos con dígito verificador conocido, uno por simbología."""

    def test_ean13(self):
        self.assertEqual(gtin.digito_verificador("400638133393"), 1)
        self.assertTrue(gtin.es_valido("4006381333931", "ean13"))
        self.assertFalse(gtin.es_valido("4006381333932", "ean13"))

    def test_ean8(self):
        self.assertEqual(gtin.digito_verificador("9638507"), 4)
        self.assertTrue(gtin.es_valido("96385074", "ean8"))

    def test_upca(self):
        self.assertEqual(gtin.digito_verificador("03600029145"), 2)
        self.assertTrue(gtin.es_valido("036000291452", "upca"))

    def test_longitud_incorrecta_no_es_valida(self):
        self.assertFalse(gtin.es_valido("400638133393", "ean13"))

    def test_simbologia_sin_verificador_siempre_pasa(self):
        self.assertTrue(gtin.es_valido("LO-QUE-SEA", "code128"))

    def test_normalizar_quita_separadores(self):
        self.assertEqual(gtin.normalizar("4 006381-333931"), "4006381333931")


class CodigoBarrasTests(TestCase):
    def setUp(self):
        self.producto = crear_producto()

    def test_normaliza_separadores_al_guardar(self):
        codigo = CodigoBarras(producto=self.producto, codigo="4-006381 333931", simbologia="ean13")
        codigo.full_clean()
        codigo.save()
        self.assertEqual(codigo.codigo, "4006381333931")

    def test_rechaza_digito_verificador_malo(self):
        codigo = CodigoBarras(producto=self.producto, codigo="4006381333932", simbologia="ean13")
        with self.assertRaises(ValidationError) as ctx:
            codigo.full_clean()
        self.assertIn("codigo", ctx.exception.message_dict)

    def test_rechaza_longitud_incorrecta(self):
        codigo = CodigoBarras(producto=self.producto, codigo="12345", simbologia="ean13")
        with self.assertRaises(ValidationError):
            codigo.full_clean()

    def test_pieza_representa_una_unidad(self):
        codigo = CodigoBarras(
            producto=self.producto,
            codigo="4006381333931",
            simbologia="ean13",
            nivel_empaque=CodigoBarras.NivelEmpaque.PIEZA,
            unidades=6,
        )
        with self.assertRaises(ValidationError) as ctx:
            codigo.full_clean()
        self.assertIn("unidades", ctx.exception.message_dict)

    def test_caja_debe_representar_mas_de_una(self):
        codigo = CodigoBarras(
            producto=self.producto,
            codigo="1400638133393",
            simbologia="itf14",
            nivel_empaque=CodigoBarras.NivelEmpaque.CAJA,
            unidades=1,
        )
        with self.assertRaises(ValidationError):
            codigo.full_clean()

    def test_solo_un_principal_por_producto(self):
        CodigoBarras.objects.create(
            producto=self.producto, codigo="4006381333931", simbologia="ean13", principal=True
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CodigoBarras.objects.create(
                    producto=self.producto, codigo="96385074", simbologia="ean8", principal=True
                )

    def test_codigo_unico_entre_productos(self):
        otro = crear_producto(forma=Producto.Forma.RUEDA)
        CodigoBarras.objects.create(producto=self.producto, codigo="4006381333931", simbologia="ean13")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CodigoBarras.objects.create(producto=otro, codigo="4006381333931", simbologia="ean13")
