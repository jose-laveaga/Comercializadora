import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalogo.models import CodigoBarras
from catalogo.tests import crear_producto
from core import code128

from . import reabastecimiento, views
from .identificacion import TipoCodigo, existencias_de, resolver_codigo
from .models import Almacen, EstandarInventario, Lote, MovimientoInventario
from .services import registrar_movimiento


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


class ReabastecimientoTests(TestCase):
    """El estándar contra la existencia útil y contra lo que viene en camino."""

    def setUp(self):
        from compras.models import OrdenCompra, OrdenCompraDetalle, Proveedor

        self.producto = crear_producto()
        self.almacen = Almacen.objects.create(
            nombre="Cámara fría", clave="ALM-01", tipo_temperatura="frio"
        )
        self.proveedor = Proveedor.objects.create(nombre="Lácteos SA", dias_entrega_estimados=3)
        self.estandar = EstandarInventario.objects.create(
            producto=self.producto,
            almacen=self.almacen,
            cantidad_minima=Decimal("20"),
            cantidad_objetivo=Decimal("48"),
        )
        self.OrdenCompra = OrdenCompra
        self.OrdenCompraDetalle = OrdenCompraDetalle

    def _meter_stock(self, cantidad, dias_para_caducar=60):
        lote = Lote.objects.create(
            producto=self.producto,
            almacen=self.almacen,
            fecha_caducidad=timezone.localdate() + timedelta(days=dias_para_caducar),
            costo_unitario=Decimal("100"),
            cantidad_inicial=Decimal("0"),
            cantidad_actual=Decimal("0"),
        )
        registrar_movimiento(lote, MovimientoInventario.Tipo.AJUSTE_POSITIVO, Decimal(cantidad))
        return lote

    def _orden(self, cantidad, estatus):
        orden = self.OrdenCompra.objects.create(
            proveedor=self.proveedor, almacen_destino=self.almacen, estatus=estatus
        )
        detalle = self.OrdenCompraDetalle.objects.create(
            orden=orden,
            producto=self.producto,
            cantidad_pedida=Decimal(cantidad),
            unidad_compra="pieza",
            unidades_por_caja=1,
            costo_unitario=Decimal("100"),
        )
        return orden, detalle

    def _unico(self):
        diagnosticos = reabastecimiento.evaluar()
        self.assertEqual(len(diagnosticos), 1)
        return diagnosticos[0]

    # --- comparación básica ---------------------------------------------------

    def test_sobre_el_minimo_es_suficiente(self):
        self._meter_stock(30)
        d = self._unico()
        self.assertEqual(d.existencia, Decimal("30.000"))
        self.assertEqual(d.estatus, reabastecimiento.EstatusAbasto.SUFICIENTE)
        self.assertEqual(d.sugerido, Decimal("0"))

    def test_exactamente_en_el_minimo_todavia_es_suficiente(self):
        self._meter_stock(20)
        self.assertEqual(self._unico().estatus, reabastecimiento.EstatusAbasto.SUFICIENTE)

    def test_bajo_el_minimo_y_sin_orden_hay_que_pedir(self):
        self._meter_stock(8)
        d = self._unico()
        self.assertEqual(d.estatus, reabastecimiento.EstatusAbasto.PEDIR)
        self.assertTrue(d.requiere_pedir)
        self.assertEqual(d.sugerido, Decimal("40.000"))  # objetivo 48 - existencia 8
        self.assertEqual(d.faltante_contra_minimo, Decimal("12.000"))

    def test_sin_existencia_pide_el_objetivo_completo(self):
        d = self._unico()
        self.assertEqual(d.existencia, Decimal("0"))
        self.assertEqual(d.sugerido, Decimal("48"))

    def test_la_cobertura_se_calcula_contra_el_objetivo(self):
        self._meter_stock(24)
        self.assertEqual(self._unico().cobertura, Decimal("50.0"))

    # --- qué cuenta como existencia -------------------------------------------

    def test_lo_caducado_no_cuenta_como_cobertura(self):
        lote = self._meter_stock(30)
        Lote.objects.filter(pk=lote.pk).update(
            fecha_caducidad=timezone.localdate() - timedelta(days=1)
        )
        d = self._unico()
        self.assertEqual(d.existencia, Decimal("0"))
        self.assertEqual(d.excluido, Decimal("30.000"))
        self.assertEqual(d.estatus, reabastecimiento.EstatusAbasto.PEDIR)

    def test_lo_que_vence_en_menos_de_cinco_dias_tampoco_cuenta(self):
        """Mercancía en semáforo negro no salva de un desabasto."""
        self._meter_stock(30, dias_para_caducar=3)
        d = self._unico()
        self.assertEqual(d.existencia, Decimal("0"))
        self.assertEqual(d.excluido, Decimal("30.000"))
        self.assertEqual(d.estatus, reabastecimiento.EstatusAbasto.PEDIR)

    def test_la_existencia_util_y_la_descartada_se_reportan_por_separado(self):
        self._meter_stock(25, dias_para_caducar=60)
        self._meter_stock(30, dias_para_caducar=2)
        d = self._unico()
        self.assertEqual(d.existencia, Decimal("25.000"))
        self.assertEqual(d.excluido, Decimal("30.000"))
        self.assertEqual(d.estatus, reabastecimiento.EstatusAbasto.SUFICIENTE)

    def test_un_lote_agotado_no_cuenta_aunque_no_haya_caducado(self):
        self._meter_stock(10)
        lote = self._meter_stock(5)
        registrar_movimiento(lote, MovimientoInventario.Tipo.MERMA, Decimal("-5"))
        self.assertEqual(self._unico().existencia, Decimal("10.000"))

    # --- qué cuenta como en camino --------------------------------------------

    def test_una_orden_enviada_cubre_el_faltante(self):
        self._meter_stock(8)
        self._orden(40, self.OrdenCompra.Estatus.ENVIADA)
        d = self._unico()
        self.assertEqual(d.en_camino, Decimal("40.000"))
        self.assertEqual(d.proyectado, Decimal("48.000"))
        self.assertEqual(d.estatus, reabastecimiento.EstatusAbasto.CUBIERTO)
        self.assertFalse(d.requiere_pedir)
        self.assertEqual(d.sugerido, Decimal("0"))

    def test_la_orden_que_cubre_se_identifica_por_folio(self):
        self._meter_stock(8)
        orden, _ = self._orden(40, self.OrdenCompra.Estatus.CONFIRMADA)
        d = self._unico()
        self.assertEqual(len(d.ordenes), 1)
        self.assertEqual(d.ordenes[0].folio, orden.folio)
        self.assertEqual(d.ordenes[0].pendiente, Decimal("40.000"))

    def test_un_borrador_no_cuenta_como_en_camino(self):
        self._meter_stock(8)
        self._orden(40, self.OrdenCompra.Estatus.BORRADOR)
        d = self._unico()
        self.assertEqual(d.en_camino, Decimal("0"))
        self.assertEqual(d.en_borrador, Decimal("40.000"))
        self.assertEqual(d.estatus, reabastecimiento.EstatusAbasto.PEDIR)
        self.assertEqual(len(d.borradores), 1)

    def test_una_orden_insuficiente_deja_el_estatus_en_pedir(self):
        self._meter_stock(5)
        self._orden(10, self.OrdenCompra.Estatus.ENVIADA)
        d = self._unico()
        self.assertEqual(d.proyectado, Decimal("15.000"))  # sigue bajo el mínimo de 20
        self.assertEqual(d.estatus, reabastecimiento.EstatusAbasto.PEDIR)
        self.assertEqual(d.sugerido, Decimal("33.000"))  # 48 - 15, descontando lo que viene

    def test_solo_cuenta_lo_pendiente_de_una_orden_parcial(self):
        from compras.services import registrar_recepcion

        self._meter_stock(2)
        orden, detalle = self._orden(40, self.OrdenCompra.Estatus.CONFIRMADA)
        registrar_recepcion(
            orden,
            [
                {
                    "detalle": detalle,
                    "cantidad": Decimal("30"),
                    "fecha_caducidad": timezone.localdate() + timedelta(days=60),
                    "costo_unitario_real": Decimal("100"),
                    "cerrar": False,
                }
            ],
        )
        d = self._unico()
        self.assertEqual(d.existencia, Decimal("32.000"))  # 2 + 30 recibidos
        self.assertEqual(d.en_camino, Decimal("10.000"))  # solo el pendiente
        self.assertEqual(d.estatus, reabastecimiento.EstatusAbasto.SUFICIENTE)

    def test_una_linea_cerrada_deja_de_contar_como_en_camino(self):
        """Lo que se dio por perdido no puede seguir figurando como cobertura."""
        from compras.services import cerrar_linea

        self._meter_stock(8)
        _, detalle = self._orden(40, self.OrdenCompra.Estatus.ENVIADA)
        self.assertEqual(self._unico().estatus, reabastecimiento.EstatusAbasto.CUBIERTO)

        cerrar_linea(detalle, motivo="agotado")
        d = self._unico()
        self.assertEqual(d.en_camino, Decimal("0"))
        self.assertEqual(d.estatus, reabastecimiento.EstatusAbasto.PEDIR)

    def test_una_orden_recibida_ya_no_esta_en_camino(self):
        from compras.services import registrar_recepcion

        _, detalle = self._orden(40, self.OrdenCompra.Estatus.CONFIRMADA)
        registrar_recepcion(
            detalle.orden,
            [
                {
                    "detalle": detalle,
                    "cantidad": Decimal("40"),
                    "fecha_caducidad": timezone.localdate() + timedelta(days=60),
                    "costo_unitario_real": Decimal("100"),
                    "cerrar": False,
                }
            ],
        )
        d = self._unico()
        self.assertEqual(d.en_camino, Decimal("0"))
        self.assertEqual(d.existencia, Decimal("40.000"))

    def test_una_orden_a_otro_almacen_no_cuenta(self):
        otro = Almacen.objects.create(nombre="Seco", clave="ALM-02", tipo_temperatura="seco")
        orden = self.OrdenCompra.objects.create(
            proveedor=self.proveedor,
            almacen_destino=otro,
            estatus=self.OrdenCompra.Estatus.ENVIADA,
        )
        self.OrdenCompraDetalle.objects.create(
            orden=orden,
            producto=self.producto,
            cantidad_pedida=Decimal("40"),
            unidad_compra="pieza",
            unidades_por_caja=1,
            costo_unitario=Decimal("100"),
        )
        self.assertEqual(self._unico().en_camino, Decimal("0"))

    # --- alcance y ordenamiento -----------------------------------------------

    def test_el_estandar_es_por_almacen(self):
        otro = Almacen.objects.create(nombre="Seco", clave="ALM-02", tipo_temperatura="seco")
        EstandarInventario.objects.create(
            producto=self.producto,
            almacen=otro,
            cantidad_minima=Decimal("5"),
            cantidad_objetivo=Decimal("10"),
        )
        self._meter_stock(30)  # solo en ALM-01
        por_almacen = {d.estandar.almacen.clave: d for d in reabastecimiento.evaluar()}
        self.assertEqual(por_almacen["ALM-01"].estatus, reabastecimiento.EstatusAbasto.SUFICIENTE)
        self.assertEqual(por_almacen["ALM-02"].estatus, reabastecimiento.EstatusAbasto.PEDIR)

    def test_filtrar_por_almacen(self):
        otro = Almacen.objects.create(nombre="Seco", clave="ALM-02", tipo_temperatura="seco")
        EstandarInventario.objects.create(
            producto=self.producto,
            almacen=otro,
            cantidad_minima=Decimal("5"),
            cantidad_objetivo=Decimal("10"),
        )
        self.assertEqual(len(reabastecimiento.evaluar(almacen=self.almacen)), 1)

    def test_lo_urgente_sale_primero(self):
        otro_producto = crear_producto(forma="RUE")
        EstandarInventario.objects.create(
            producto=otro_producto,
            almacen=self.almacen,
            cantidad_minima=Decimal("5"),
            cantidad_objetivo=Decimal("10"),
        )
        Lote.objects.create(
            producto=otro_producto,
            almacen=self.almacen,
            fecha_caducidad=timezone.localdate() + timedelta(days=60),
            costo_unitario=Decimal("100"),
            cantidad_inicial=Decimal("0"),
            cantidad_actual=Decimal("50"),
        )
        # self.producto queda en cero: debe salir antes que el que está surtido.
        diagnosticos = reabastecimiento.evaluar()
        self.assertEqual(diagnosticos[0].estandar.producto, self.producto)
        self.assertEqual(diagnosticos[0].estatus, reabastecimiento.EstatusAbasto.PEDIR)

    def test_un_estandar_inactivo_no_se_evalua(self):
        self.estandar.activo = False
        self.estandar.save(update_fields=["activo"])
        self.assertEqual(reabastecimiento.evaluar(), [])

    def test_el_resumen_cuenta_por_estatus(self):
        self._meter_stock(8)
        conteo = reabastecimiento.resumen(reabastecimiento.evaluar())
        self.assertEqual(conteo[reabastecimiento.EstatusAbasto.PEDIR], 1)
        self.assertEqual(conteo[reabastecimiento.EstatusAbasto.SUFICIENTE], 0)

    def test_productos_sin_estandar_lista_los_huecos(self):
        otro = crear_producto(forma="RUE")
        sin_estandar = list(reabastecimiento.productos_sin_estandar())
        self.assertIn(otro, sin_estandar)
        self.assertNotIn(self.producto, sin_estandar)

    # --- validación del modelo ------------------------------------------------

    def test_el_objetivo_no_puede_ser_menor_al_minimo(self):
        estandar = EstandarInventario(
            producto=crear_producto(forma="RUE"),
            almacen=self.almacen,
            cantidad_minima=Decimal("50"),
            cantidad_objetivo=Decimal("10"),
        )
        with self.assertRaises(ValidationError):
            estandar.full_clean()

    def test_no_se_puede_duplicar_el_estandar_de_un_producto_en_un_almacen(self):
        with self.assertRaises(IntegrityError):
            EstandarInventario.objects.create(
                producto=self.producto,
                almacen=self.almacen,
                cantidad_minima=Decimal("1"),
                cantidad_objetivo=Decimal("2"),
            )


class ConsolaReabastecimientoTests(TestCase):
    def setUp(self):
        self.producto = crear_producto()
        self.almacen = Almacen.objects.create(
            nombre="Cámara fría", clave="ALM-01", tipo_temperatura="frio"
        )
        self.usuario = get_user_model().objects.create_user(
            username="compras", password="x", is_staff=True
        )
        self.client.force_login(self.usuario)

    def test_exige_sesion_de_staff(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("core:reabastecimiento")).status_code, 302)

    def test_la_pagina_carga_sin_estandares(self):
        respuesta = self.client.get(reverse("core:reabastecimiento"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Ningún producto tiene estándar")

    def test_guardar_un_estandar_desde_la_consola(self):
        self.client.post(
            reverse("core:estandar_guardar"),
            {
                "producto": self.producto.pk,
                "almacen": self.almacen.pk,
                "cantidad_minima": "20",
                "cantidad_objetivo": "48",
                "notas": "",
            },
        )
        estandar = EstandarInventario.objects.get()
        self.assertEqual(estandar.cantidad_minima, Decimal("20.000"))
        self.assertEqual(estandar.cantidad_objetivo, Decimal("48.000"))

    def test_volver_a_guardar_actualiza_en_vez_de_fallar(self):
        datos = {
            "producto": self.producto.pk,
            "almacen": self.almacen.pk,
            "cantidad_minima": "20",
            "cantidad_objetivo": "48",
            "notas": "",
        }
        self.client.post(reverse("core:estandar_guardar"), datos)
        self.client.post(reverse("core:estandar_guardar"), {**datos, "cantidad_objetivo": "60"})
        self.assertEqual(EstandarInventario.objects.count(), 1)
        self.assertEqual(EstandarInventario.objects.get().cantidad_objetivo, Decimal("60.000"))

    def test_rechaza_un_objetivo_menor_al_minimo(self):
        respuesta = self.client.post(
            reverse("core:estandar_guardar"),
            {
                "producto": self.producto.pk,
                "almacen": self.almacen.pk,
                "cantidad_minima": "50",
                "cantidad_objetivo": "10",
                "notas": "",
            },
            follow=True,
        )
        self.assertEqual(EstandarInventario.objects.count(), 0)
        self.assertContains(respuesta, "no puede ser menor al mínimo")

    def test_la_pagina_muestra_el_diagnostico(self):
        EstandarInventario.objects.create(
            producto=self.producto,
            almacen=self.almacen,
            cantidad_minima=Decimal("20"),
            cantidad_objetivo=Decimal("48"),
        )
        respuesta = self.client.get(reverse("core:reabastecimiento"))
        self.assertContains(respuesta, self.producto.sku)
        self.assertContains(respuesta, "Hay que pedir")

    def test_borrar_un_estandar(self):
        estandar = EstandarInventario.objects.create(
            producto=self.producto,
            almacen=self.almacen,
            cantidad_minima=Decimal("20"),
            cantidad_objetivo=Decimal("48"),
        )
        self.client.post(reverse("core:estandar_borrar", args=[estandar.pk]))
        self.assertEqual(EstandarInventario.objects.count(), 0)
