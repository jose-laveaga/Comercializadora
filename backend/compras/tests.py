from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from catalogo.tests import crear_producto
from inventario.models import Almacen, Lote, MovimientoInventario

from .models import OrdenCompra, OrdenCompraDetalle, Proveedor
from .services import RecepcionInvalidaError, cerrar_linea, reabrir_linea, registrar_recepcion


def valor_inventario():
    return sum((l.valor_actual for l in Lote.objects.disponibles()), Decimal("0"))


class BaseComprasTests(TestCase):
    def setUp(self):
        self.producto = crear_producto()
        self.otro = crear_producto(forma="RUE")
        self.almacen = Almacen.objects.create(
            nombre="Cámara fría", clave="ALM-01", tipo_temperatura="frio"
        )
        self.proveedor = Proveedor.objects.create(nombre="Lácteos SA", dias_entrega_estimados=3)
        self.usuario = get_user_model().objects.create_user(
            username="almacen", password="x", is_staff=True
        )
        self.orden = OrdenCompra.objects.create(
            proveedor=self.proveedor, almacen_destino=self.almacen
        )
        self.linea = OrdenCompraDetalle.objects.create(
            orden=self.orden,
            producto=self.producto,
            cantidad_pedida=Decimal("10"),
            unidad_compra="pieza",
            unidades_por_caja=1,
            costo_unitario=Decimal("120.00"),
        )
        self.caducidad = timezone.localdate() + timedelta(days=45)


class RecepcionParcialTests(BaseComprasTests):
    def test_recibir_parcial_deja_la_orden_esperando(self):
        registrar_recepcion(
            self.orden,
            [
                {
                    "detalle": self.linea,
                    "cantidad": Decimal("7"),
                    "fecha_caducidad": self.caducidad,
                    "costo_unitario_real": Decimal("125.50"),
                    "cerrar": False,
                }
            ],
            usuario=self.usuario,
        )
        self.orden.refresh_from_db()
        self.linea.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.RECIBIDA_PARCIAL)
        self.assertEqual(self.linea.cantidad_pendiente, Decimal("3"))
        self.assertEqual(self.linea.cantidad_faltante, Decimal("0"))
        self.assertFalse(self.orden.esta_completa)

    def test_el_costo_real_es_el_que_valua_el_inventario(self):
        # Se cotizó a 120 y llegó facturado a 125.50: manda la factura.
        registrar_recepcion(
            self.orden,
            [
                {
                    "detalle": self.linea,
                    "cantidad": Decimal("7"),
                    "fecha_caducidad": self.caducidad,
                    "costo_unitario_real": Decimal("125.50"),
                    "cerrar": False,
                }
            ],
            usuario=self.usuario,
        )
        self.assertEqual(valor_inventario(), Decimal("878.500"))

    def test_el_movimiento_guarda_quien_recibio(self):
        registrar_recepcion(
            self.orden,
            [
                {
                    "detalle": self.linea,
                    "cantidad": Decimal("7"),
                    "fecha_caducidad": self.caducidad,
                    "costo_unitario_real": Decimal("125.50"),
                    "cerrar": False,
                }
            ],
            usuario=self.usuario,
        )
        movimiento = MovimientoInventario.objects.get(tipo="entrada_compra")
        self.assertEqual(movimiento.usuario, self.usuario)

    def test_costos_distintos_generan_lotes_distintos(self):
        for cantidad, costo in ((Decimal("7"), Decimal("125.50")), (Decimal("3"), Decimal("130.00"))):
            registrar_recepcion(
                self.orden,
                [
                    {
                        "detalle": self.linea,
                        "cantidad": cantidad,
                        "fecha_caducidad": self.caducidad,
                        "costo_unitario_real": costo,
                        "cerrar": False,
                    }
                ],
                usuario=self.usuario,
            )
        self.assertEqual(Lote.objects.count(), 2)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.RECIBIDA)

    def test_recibir_completo_cierra_la_orden(self):
        registrar_recepcion(
            self.orden,
            [
                {
                    "detalle": self.linea,
                    "cantidad": Decimal("10"),
                    "fecha_caducidad": self.caducidad,
                    "costo_unitario_real": Decimal("120.00"),
                    "cerrar": False,
                }
            ],
            usuario=self.usuario,
        )
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.RECIBIDA)
        self.assertFalse(self.orden.esta_abierta)

    def test_una_linea_invalida_no_deja_nada_a_medias(self):
        """La atomicidad es el motivo de que exista el servicio."""
        otra = OrdenCompraDetalle.objects.create(
            orden=self.orden,
            producto=self.otro,
            cantidad_pedida=Decimal("5"),
            unidad_compra="pieza",
            unidades_por_caja=1,
            costo_unitario=Decimal("80.00"),
        )
        with self.assertRaises(ValidationError):
            registrar_recepcion(
                self.orden,
                [
                    {
                        "detalle": self.linea,
                        "cantidad": Decimal("7"),
                        "fecha_caducidad": self.caducidad,
                        "costo_unitario_real": Decimal("125.50"),
                        "cerrar": False,
                    },
                    {
                        # Caducidad pasada: la valida RecepcionDetalle.clean().
                        "detalle": otra,
                        "cantidad": Decimal("5"),
                        "fecha_caducidad": timezone.localdate() - timedelta(days=1),
                        "costo_unitario_real": Decimal("80.00"),
                        "cerrar": False,
                    },
                ],
                usuario=self.usuario,
            )
        self.assertEqual(Lote.objects.count(), 0)
        self.assertEqual(MovimientoInventario.objects.count(), 0)
        self.assertEqual(valor_inventario(), Decimal("0"))
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.BORRADOR)

    def test_no_recibe_lineas_de_otra_orden(self):
        ajena = OrdenCompra.objects.create(proveedor=self.proveedor, almacen_destino=self.almacen)
        linea_ajena = OrdenCompraDetalle.objects.create(
            orden=ajena,
            producto=self.otro,
            cantidad_pedida=Decimal("5"),
            unidad_compra="pieza",
            unidades_por_caja=1,
            costo_unitario=Decimal("80.00"),
        )
        with self.assertRaises(RecepcionInvalidaError):
            registrar_recepcion(
                self.orden,
                [
                    {
                        "detalle": linea_ajena,
                        "cantidad": Decimal("5"),
                        "fecha_caducidad": self.caducidad,
                        "costo_unitario_real": Decimal("80.00"),
                        "cerrar": False,
                    }
                ],
                usuario=self.usuario,
            )
        self.assertEqual(Lote.objects.count(), 0)

    def test_solo_cerrar_no_crea_recepcion_vacia(self):
        recepcion, detalles, cerradas = registrar_recepcion(
            self.orden,
            [{"detalle": self.linea, "cantidad": None, "cerrar": True, "motivo": "agotado"}],
            usuario=self.usuario,
        )
        self.assertIsNone(recepcion)
        self.assertEqual(detalles, [])
        self.assertEqual(len(cerradas), 1)
        self.assertEqual(self.orden.recepciones.count(), 0)


class CierreDeFaltantesTests(BaseComprasTests):
    def test_recibir_parcial_y_cerrar_el_resto_en_un_solo_acto(self):
        registrar_recepcion(
            self.orden,
            [
                {
                    "detalle": self.linea,
                    "cantidad": Decimal("7"),
                    "fecha_caducidad": self.caducidad,
                    "costo_unitario_real": Decimal("125.50"),
                    "cerrar": True,
                    "motivo": OrdenCompraDetalle.MotivoFaltante.AGOTADO,
                    "notas_faltante": "El proveedor solo traía 7.",
                }
            ],
            usuario=self.usuario,
        )
        self.orden.refresh_from_db()
        self.linea.refresh_from_db()

        # El cierre se aplica sobre el remanente que quedó tras esta entrega.
        self.assertEqual(self.linea.cantidad_recibida, Decimal("7"))
        self.assertEqual(self.linea.cantidad_faltante, Decimal("3"))
        self.assertEqual(self.linea.cantidad_pendiente, Decimal("0"))
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.CERRADA_INCOMPLETA)
        self.assertTrue(self.orden.esta_completa)
        self.assertFalse(self.orden.esta_abierta)
        self.assertEqual(self.orden.cantidad_faltante, Decimal("3"))

    def test_el_cierre_guarda_motivo_y_quien_lo_cerro(self):
        cerrar_linea(
            self.linea,
            motivo=OrdenCompraDetalle.MotivoFaltante.CALIDAD,
            notas="Llegó con la cadena de frío rota.",
            usuario=self.usuario,
        )
        self.linea.refresh_from_db()
        self.assertTrue(self.linea.cerrado)
        self.assertEqual(self.linea.motivo_faltante, "calidad")
        self.assertEqual(self.linea.cerrado_por, self.usuario)
        self.assertIsNotNone(self.linea.cerrado_en)

    def test_cerrar_sin_haber_recibido_nada(self):
        cerrar_linea(self.linea, motivo="no_surtido", usuario=self.usuario)
        self.orden.refresh_from_db()
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.cantidad_faltante, Decimal("10"))
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.CERRADA_INCOMPLETA)

    def test_no_se_puede_cerrar_una_linea_ya_completa(self):
        registrar_recepcion(
            self.orden,
            [
                {
                    "detalle": self.linea,
                    "cantidad": Decimal("10"),
                    "fecha_caducidad": self.caducidad,
                    "costo_unitario_real": Decimal("120.00"),
                    "cerrar": False,
                }
            ],
            usuario=self.usuario,
        )
        self.linea.refresh_from_db()
        with self.assertRaises(ValidationError):
            cerrar_linea(self.linea, motivo="agotado", usuario=self.usuario)

    def test_no_se_puede_cerrar_dos_veces(self):
        cerrar_linea(self.linea, motivo="agotado", usuario=self.usuario)
        with self.assertRaises(ValidationError):
            cerrar_linea(self.linea, motivo="agotado", usuario=self.usuario)

    def test_una_linea_cerrada_rechaza_mercancia_nueva(self):
        cerrar_linea(self.linea, motivo="agotado", usuario=self.usuario)
        self.linea.refresh_from_db()
        with self.assertRaises(ValidationError) as ctx:
            registrar_recepcion(
                self.orden,
                [
                    {
                        "detalle": self.linea,
                        "cantidad": Decimal("3"),
                        "fecha_caducidad": self.caducidad,
                        "costo_unitario_real": Decimal("120.00"),
                        "cerrar": False,
                    }
                ],
                usuario=self.usuario,
            )
        self.assertIn("cerrada", " ".join(ctx.exception.messages).lower())

    def test_reabrir_devuelve_la_linea_a_pendiente(self):
        cerrar_linea(self.linea, motivo="agotado", usuario=self.usuario)
        reabrir_linea(self.linea, usuario=self.usuario)
        self.orden.refresh_from_db()
        self.linea.refresh_from_db()
        self.assertFalse(self.linea.cerrado)
        self.assertEqual(self.linea.cantidad_pendiente, Decimal("10"))
        self.assertEqual(self.linea.cantidad_faltante, Decimal("0"))
        # Sin nada recibido y sin nada cerrado, la orden vuelve a esperar
        # mercancía. No se puede saber si estaba en borrador o enviada antes del
        # cierre, así que aterriza en confirmada — lo único afirmable.
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.CONFIRMADA)
        self.assertTrue(self.orden.esta_abierta)

    def test_reabrir_tras_recibir_parcial_vuelve_a_parcial(self):
        registrar_recepcion(
            self.orden,
            [
                {
                    "detalle": self.linea,
                    "cantidad": Decimal("7"),
                    "fecha_caducidad": self.caducidad,
                    "costo_unitario_real": Decimal("120.00"),
                    "cerrar": True,
                    "motivo": "agotado",
                }
            ],
            usuario=self.usuario,
        )
        self.linea.refresh_from_db()
        reabrir_linea(self.linea, usuario=self.usuario)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.RECIBIDA_PARCIAL)

    def test_completa_sin_faltante_es_recibida_no_cerrada_incompleta(self):
        """La distinción entre los dos estatus finales es todo el punto."""
        registrar_recepcion(
            self.orden,
            [
                {
                    "detalle": self.linea,
                    "cantidad": Decimal("10"),
                    "fecha_caducidad": self.caducidad,
                    "costo_unitario_real": Decimal("120.00"),
                    "cerrar": False,
                }
            ],
            usuario=self.usuario,
        )
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.RECIBIDA)
        self.assertEqual(self.orden.cantidad_faltante, Decimal("0"))

    def test_una_orden_cancelada_no_cambia_de_estatus_sola(self):
        self.orden.estatus = OrdenCompra.Estatus.CANCELADA
        self.orden.save(update_fields=["estatus"])
        cerrar_linea(self.linea, motivo="cancelado", usuario=self.usuario)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.CANCELADA)


class ConsolaComprasTests(BaseComprasTests):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.usuario)

    def test_la_pagina_lista_las_ordenes_abiertas(self):
        respuesta = self.client.get(reverse("core:compras"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, self.orden.folio)

    def test_exige_sesion_de_staff(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("core:compras")).status_code, 302)

    def test_crear_orden_desde_la_consola(self):
        respuesta = self.client.post(
            reverse("core:compra_crear"),
            {
                "proveedor": self.proveedor.pk,
                "almacen_destino": self.almacen.pk,
                "fecha_entrega_estimada": "",
                "notas": "",
            },
        )
        self.assertEqual(respuesta.status_code, 302)
        self.assertEqual(OrdenCompra.objects.count(), 2)

    def test_agregar_linea_desde_la_consola(self):
        self.client.post(
            reverse("core:compra_linea", args=[self.orden.pk]),
            {
                "producto": self.otro.pk,
                "cantidad_pedida": "5",
                "unidad_compra": "pieza",
                "unidades_por_caja": "1",
                "costo_unitario": "80.00",
            },
        )
        self.assertEqual(self.orden.detalles.count(), 2)

    def test_no_admite_el_mismo_producto_dos_veces(self):
        self.client.post(
            reverse("core:compra_linea", args=[self.orden.pk]),
            {
                "producto": self.producto.pk,
                "cantidad_pedida": "5",
                "unidad_compra": "pieza",
                "unidades_por_caja": "1",
                "costo_unitario": "80.00",
            },
        )
        self.assertEqual(self.orden.detalles.count(), 1)

    def _post_recepcion(self, **campos):
        datos = {
            "fecha_recepcion": timezone.localdate().isoformat(),
            "folio_factura": "A-123",
            "notas": "",
            "lineas-TOTAL_FORMS": "1",
            "lineas-INITIAL_FORMS": "1",
            "lineas-MIN_NUM_FORMS": "0",
            "lineas-MAX_NUM_FORMS": "1000",
            "lineas-0-detalle_id": str(self.linea.pk),
            "lineas-0-cantidad_recibida": "",
            "lineas-0-fecha_caducidad": "",
            "lineas-0-costo_unitario_real": "",
            "lineas-0-codigo_lote_proveedor": "",
            "lineas-0-motivo_faltante": "",
            "lineas-0-notas_faltante": "",
        }
        datos.update(campos)
        return self.client.post(
            reverse("core:compra_recibir", args=[self.orden.pk]), datos, follow=True
        )

    def test_recibir_parcial_desde_la_consola(self):
        respuesta = self._post_recepcion(
            **{
                "lineas-0-cantidad_recibida": "7",
                "lineas-0-fecha_caducidad": self.caducidad.isoformat(),
                "lineas-0-costo_unitario_real": "125.50",
            }
        )
        self.assertEqual(respuesta.status_code, 200)
        self.orden.refresh_from_db()
        self.linea.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.RECIBIDA_PARCIAL)
        self.assertEqual(self.linea.cantidad_pendiente, Decimal("3"))
        self.assertEqual(valor_inventario(), Decimal("878.500"))

    def test_recibir_parcial_y_cerrar_desde_la_consola(self):
        self._post_recepcion(
            **{
                "lineas-0-cantidad_recibida": "7",
                "lineas-0-fecha_caducidad": self.caducidad.isoformat(),
                "lineas-0-costo_unitario_real": "125.50",
                "lineas-0-cerrar_faltante": "on",
                "lineas-0-motivo_faltante": "agotado",
                "lineas-0-notas_faltante": "Solo traía 7.",
            }
        )
        self.orden.refresh_from_db()
        self.linea.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.CERRADA_INCOMPLETA)
        self.assertEqual(self.linea.cantidad_faltante, Decimal("3"))
        self.assertEqual(self.linea.notas_faltante, "Solo traía 7.")

    def test_cantidad_sin_caducidad_no_registra_nada(self):
        respuesta = self._post_recepcion(**{"lineas-0-cantidad_recibida": "7"})
        self.assertEqual(Lote.objects.count(), 0)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.BORRADOR)
        self.assertContains(respuesta, "fecha de caducidad")

    def test_cerrar_sin_motivo_no_registra_nada(self):
        self._post_recepcion(**{"lineas-0-cerrar_faltante": "on"})
        self.linea.refresh_from_db()
        self.assertFalse(self.linea.cerrado)

    def test_formulario_vacio_avisa_y_no_hace_nada(self):
        respuesta = self._post_recepcion()
        self.assertContains(respuesta, "No capturaste ninguna cantidad")
        self.assertEqual(Lote.objects.count(), 0)

    def test_cerrar_faltante_suelto_desde_la_consola(self):
        self.client.post(
            reverse("core:compra_cerrar_faltante", args=[self.orden.pk, self.linea.pk]),
            {"motivo_faltante": "no_surtido", "notas_faltante": "No lo tenía."},
        )
        self.linea.refresh_from_db()
        self.assertTrue(self.linea.cerrado)
        self.assertEqual(self.linea.cantidad_faltante, Decimal("10"))

    def test_reabrir_desde_la_consola(self):
        cerrar_linea(self.linea, motivo="agotado", usuario=self.usuario)
        self.client.post(
            reverse("core:compra_reabrir_faltante", args=[self.orden.pk, self.linea.pk])
        )
        self.linea.refresh_from_db()
        self.assertFalse(self.linea.cerrado)

    def test_no_deja_poner_a_mano_un_estatus_que_se_deduce(self):
        respuesta = self.client.post(
            reverse("core:compra_estatus", args=[self.orden.pk]),
            {"estatus": OrdenCompra.Estatus.RECIBIDA},
            follow=True,
        )
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.BORRADOR)
        self.assertContains(respuesta, "no se pone a mano")

    def test_si_deja_marcarla_como_enviada(self):
        self.client.post(
            reverse("core:compra_estatus", args=[self.orden.pk]),
            {"estatus": OrdenCompra.Estatus.ENVIADA},
        )
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estatus, OrdenCompra.Estatus.ENVIADA)

    def test_no_borra_una_linea_que_ya_recibio_mercancia(self):
        registrar_recepcion(
            self.orden,
            [
                {
                    "detalle": self.linea,
                    "cantidad": Decimal("7"),
                    "fecha_caducidad": self.caducidad,
                    "costo_unitario_real": Decimal("120.00"),
                    "cerrar": False,
                }
            ],
            usuario=self.usuario,
        )
        self.client.post(
            reverse("core:compra_linea_borrar", args=[self.orden.pk, self.linea.pk])
        )
        self.assertEqual(self.orden.detalles.count(), 1)

    def test_la_ficha_muestra_el_faltante_y_su_motivo(self):
        cerrar_linea(
            self.linea, motivo="calidad", notas="Cadena de frío rota.", usuario=self.usuario
        )
        respuesta = self.client.get(reverse("core:compra_ficha", args=[self.orden.pk]))
        self.assertContains(respuesta, "Rechazado por calidad")
        self.assertContains(respuesta, "Cadena de frío rota.")
