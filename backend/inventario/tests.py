import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalogo.models import CodigoBarras
from catalogo.tests import crear_producto
from core import code128

from . import views
from .identificacion import TipoCodigo, existencias_de, resolver_codigo
from .models import Almacen, Lote


def decodificar_svg(svg, quiet_zone=10):
    """Lee de vuelta el texto de un SVG generado por core.code128.

    Existe solo para las pruebas: el encoder está escrito a mano, así que la
    garantía que de verdad importa es que un lector independiente recupere el
    texto original a partir de las barras dibujadas.
    """
    rects = re.findall(r'<rect x="(\d+)" y="0" width="(\d+)"', svg)
    if not rects:
        raise AssertionError("El SVG no tiene barras.")

    # Reconstruye la secuencia de anchos alternando barra/espacio: el ancho de
    # cada barra sale del rect, y el del espacio, del hueco hasta la siguiente.
    elementos = []
    for i, (x, ancho) in enumerate(rects):
        x, ancho = int(x), int(ancho)
        elementos.append(ancho)
        if i + 1 < len(rects):
            elementos.append(int(rects[i + 1][0]) - (x + ancho))
    assert int(rects[0][0]) == quiet_zone, "Falta la zona muda izquierda."

    # Símbolos de 6 elementos, salvo el paro final que tiene 7.
    simbolos = [elementos[i : i + 6] for i in range(0, len(elementos) - 7, 6)]
    simbolos.append(elementos[-7:])

    valores = []
    for simbolo in simbolos:
        patron = "".join(str(e) for e in simbolo)
        valores.append(code128.PATRONES.index(patron))

    if valores[0] != code128.INICIO_B or valores[-1] != code128.PARO:
        raise AssertionError("Faltan el símbolo de inicio o el de paro.")

    datos, control = valores[1:-2], valores[-2]
    suma = valores[0] + sum(i * v for i, v in enumerate(datos, start=1))
    if suma % 103 != control:
        raise AssertionError("El dígito de control no cuadra.")

    return "".join(chr(v + 32) for v in datos)


class Code128Tests(TestCase):
    def test_ida_y_vuelta(self):
        for texto in ("A", "12345", "MASC-LYN-2260G-BAR-260813-1", "ADRO-AGC-400G-POR-260826-1"):
            with self.subTest(texto=texto):
                self.assertEqual(decodificar_svg(code128.svg(texto)), texto)

    def test_ida_y_vuelta_con_todo_el_subconjunto_b(self):
        texto = "".join(chr(c) for c in range(32, 127))
        self.assertEqual(decodificar_svg(code128.svg(texto)), texto)

    def test_ancho_es_once_modulos_por_simbolo_mas_el_paro(self):
        # inicio + n caracteres + verificador, a 11 módulos cada uno, más los
        # 13 del patrón de paro.
        for texto in ("A", "12345", "MASC-LYN-260813-1"):
            self.assertEqual(code128.modulos(texto), 11 * (len(texto) + 2) + 13)

    def test_digito_de_control(self):
        # "12345" en subconjunto B: inicio 104, valores 17..21 con pesos 1..5.
        # 104 + 17 + 36 + 57 + 80 + 105 = 399; 399 % 103 = 90.
        valores = code128._valores("12345")
        self.assertEqual(valores[0], code128.INICIO_B)
        self.assertEqual(valores[-2], 90)
        self.assertEqual(valores[-1], code128.PARO)

    def test_rechaza_caracteres_fuera_del_subconjunto_b(self):
        with self.assertRaises(code128.Code128Error):
            code128.svg("MASCARPÓN")

    def test_rechaza_texto_vacio(self):
        with self.assertRaises(code128.Code128Error):
            code128.svg("")

    def test_svg_lleva_zona_muda_a_ambos_lados(self):
        svg = code128.svg("ABC", quiet_zone=10)
        ancho = code128.modulos("ABC") + 20
        self.assertIn(f'viewBox="0 0 {ancho} 100"', svg)
        # La primera barra arranca justo después de la zona muda izquierda.
        self.assertIn('<rect x="10"', svg)

    def test_ancho_de_modulo_avisa_de_codigos_largos(self):
        corto = code128.ancho_modulo_mm("ABC-1", 92)
        largo = code128.ancho_modulo_mm("MASC-LYN-2260G-BAR-260813-1", 92)
        self.assertGreater(corto, largo)
        self.assertGreater(largo, code128.ANCHO_MODULO_MINIMO_MM)


class ResolverCodigoTests(TestCase):
    def setUp(self):
        self.producto = crear_producto()
        self.almacen = Almacen.objects.create(
            nombre="Cámara fría", clave="ALM-01", tipo_temperatura="frio"
        )
        self.lote = Lote.objects.create(
            producto=self.producto,
            almacen=self.almacen,
            fecha_caducidad=timezone.localdate() + timedelta(days=60),
            costo_unitario=Decimal("100.00"),
            cantidad_inicial=Decimal("0"),
            cantidad_actual=Decimal("12"),
        )

    def test_resuelve_codigo_de_lote(self):
        resultado = resolver_codigo(self.lote.codigo)
        self.assertEqual(resultado.tipo, TipoCodigo.LOTE)
        self.assertEqual(resultado.lote, self.lote)
        self.assertEqual(resultado.producto, self.producto)
        self.assertEqual(resultado.unidades, 1)

    def test_resuelve_sku(self):
        resultado = resolver_codigo(self.producto.sku)
        self.assertEqual(resultado.tipo, TipoCodigo.SKU)
        self.assertEqual(resultado.producto, self.producto)

    def test_resuelve_codigo_de_barras_de_pieza(self):
        CodigoBarras.objects.create(
            producto=self.producto, codigo="4006381333931", simbologia="ean13"
        )
        resultado = resolver_codigo("4006381333931")
        self.assertEqual(resultado.tipo, TipoCodigo.CODIGO_BARRAS)
        self.assertEqual(resultado.producto, self.producto)
        self.assertEqual(resultado.unidades, 1)

    def test_codigo_de_caja_devuelve_las_unidades_que_representa(self):
        CodigoBarras.objects.create(
            producto=self.producto,
            codigo="1400638133393",
            simbologia="itf14",
            nivel_empaque=CodigoBarras.NivelEmpaque.CAJA,
            unidades=6,
        )
        resultado = resolver_codigo("1400638133393")
        self.assertEqual(resultado.unidades, 6)

    def test_codigo_de_barras_inactivo_no_resuelve(self):
        CodigoBarras.objects.create(
            producto=self.producto, codigo="4006381333931", simbologia="ean13", activo=False
        )
        self.assertFalse(resolver_codigo("4006381333931"))

    def test_normaliza_lo_que_manda_el_escaner(self):
        # Los escáneres agregan retorno de carro y a veces espacios.
        resultado = resolver_codigo(f"  {self.lote.codigo.lower()}\r\n")
        self.assertEqual(resultado.tipo, TipoCodigo.LOTE)

    def test_codigo_desconocido_es_falsy(self):
        resultado = resolver_codigo("NO-EXISTE-123")
        self.assertEqual(resultado.tipo, TipoCodigo.DESCONOCIDO)
        self.assertFalse(resultado)
        self.assertIsNone(resultado.producto)

    def test_texto_vacio_no_revienta(self):
        self.assertFalse(resolver_codigo(""))
        self.assertFalse(resolver_codigo(None))

    def test_el_lote_gana_sobre_el_sku(self):
        # El código de lote contiene el SKU adentro; si el orden de resolución
        # se invirtiera, escanear una etiqueta devolvería el producto genérico.
        self.assertTrue(self.lote.codigo.startswith(self.producto.sku))
        self.assertEqual(resolver_codigo(self.lote.codigo).tipo, TipoCodigo.LOTE)

    def test_existencias_de_un_sku_lista_sus_lotes_en_orden_fefo(self):
        urgente = Lote.objects.create(
            producto=self.producto,
            almacen=self.almacen,
            fecha_caducidad=timezone.localdate() + timedelta(days=5),
            costo_unitario=Decimal("100.00"),
            cantidad_inicial=Decimal("0"),
            cantidad_actual=Decimal("3"),
        )
        lotes = list(existencias_de(resolver_codigo(self.producto.sku)))
        self.assertEqual(lotes, [urgente, self.lote])

    def test_existencias_de_un_lote_es_solo_ese_lote(self):
        lotes = list(existencias_de(resolver_codigo(self.lote.codigo)))
        self.assertEqual(lotes, [self.lote])

    def test_existencias_de_un_codigo_desconocido_es_vacio(self):
        self.assertEqual(list(existencias_de(resolver_codigo("NADA"))), [])


class EtiquetasLoteViewTests(TestCase):
    def setUp(self):
        self.producto = crear_producto()
        self.almacen = Almacen.objects.create(
            nombre="Cámara fría", clave="ALM-01", tipo_temperatura="frio"
        )
        self.lote = Lote.objects.create(
            producto=self.producto,
            almacen=self.almacen,
            fecha_caducidad=timezone.localdate() + timedelta(days=60),
            costo_unitario=Decimal("100.00"),
            cantidad_inicial=Decimal("0"),
            cantidad_actual=Decimal("12"),
        )
        self.url = reverse("inventario:etiquetas_lote")
        usuario = get_user_model().objects.create_user(
            username="almacen", password="x", is_staff=True
        )
        self.client.force_login(usuario)

    def test_exige_sesion_de_staff(self):
        self.client.logout()
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 302)

    def test_imprime_el_codigo_del_lote(self):
        respuesta = self.client.get(self.url, {"lotes": self.lote.pk})
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, self.lote.codigo)
        self.assertContains(respuesta, "<svg")

    def test_copias_repite_la_etiqueta(self):
        respuesta = self.client.get(self.url, {"lotes": self.lote.pk, "copias": 3})
        self.assertEqual(respuesta.context["total"], 3)
        self.assertEqual(len(respuesta.context["hojas"]), 1)

    def test_agrupa_de_diez_en_diez_por_hoja(self):
        respuesta = self.client.get(self.url, {"lotes": self.lote.pk, "copias": 12})
        self.assertEqual(respuesta.context["total"], 12)
        self.assertEqual([len(h) for h in respuesta.context["hojas"]], [10, 2])

    def test_recorta_selecciones_desmedidas(self):
        # 5 lotes × 50 copias = 250 etiquetas: pasa del tope de seguridad.
        ids = [self.lote.pk]
        for dias in (10, 20, 30, 40):
            ids.append(
                Lote.objects.create(
                    producto=self.producto,
                    almacen=self.almacen,
                    fecha_caducidad=timezone.localdate() + timedelta(days=dias),
                    costo_unitario=Decimal("100.00"),
                    cantidad_inicial=Decimal("0"),
                    cantidad_actual=Decimal("1"),
                ).pk
            )
        respuesta = self.client.get(
            self.url, {"lotes": ",".join(str(i) for i in ids), "copias": 50}
        )
        self.assertTrue(respuesta.context["truncado"])
        self.assertEqual(respuesta.context["total"], views.MAXIMO_ETIQUETAS)

    def test_sin_seleccion_explica_como_usarla(self):
        respuesta = self.client.get(self.url)
        self.assertTrue(respuesta.context["sin_seleccion"])
        self.assertEqual(respuesta.context["hojas"], [])

    def test_ignora_ids_basura(self):
        respuesta = self.client.get(self.url, {"lotes": "abc,,999999"})
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.context["total"], 0)


class AccionAdminEtiquetasTests(TestCase):
    """LoteAdmin usa SoloLecturaAdminMixin, que apaga alta, edición y borrado.

    Vale la pena fijarlo con una prueba: si esa restricción llegara a esconder
    también las acciones, el botón de imprimir etiquetas desaparecería del
    admin sin que nada falle ni avise.
    """

    def setUp(self):
        self.producto = crear_producto()
        self.almacen = Almacen.objects.create(
            nombre="Cámara fría", clave="ALM-01", tipo_temperatura="frio"
        )
        self.lote = Lote.objects.create(
            producto=self.producto,
            almacen=self.almacen,
            fecha_caducidad=timezone.localdate() + timedelta(days=60),
            costo_unitario=Decimal("100.00"),
            cantidad_inicial=Decimal("0"),
            cantidad_actual=Decimal("12"),
        )
        usuario = get_user_model().objects.create_superuser(username="jefe", password="x")
        self.client.force_login(usuario)
        self.url = reverse("admin:inventario_lote_changelist")

    def test_la_accion_aparece_en_el_listado(self):
        respuesta = self.client.get(self.url)
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "imprimir_etiquetas")

    def test_la_accion_redirige_a_la_hoja_de_etiquetas(self):
        respuesta = self.client.post(
            self.url,
            {"action": "imprimir_etiquetas", "_selected_action": [str(self.lote.pk)]},
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertIn(reverse("inventario:etiquetas_lote"), respuesta["Location"])
        self.assertIn(f"lotes={self.lote.pk}", respuesta["Location"])
