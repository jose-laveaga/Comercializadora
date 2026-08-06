from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from catalogo.models import Producto
from inventario.models import Almacen, Lote, MovimientoInventario
from inventario.services import registrar_movimiento
from pedidos.models import Cliente, Pedido, PedidoDetalle, Ruta, Surtido
from pedidos.services import surtir_pedido_detalle

SENTINEL = "[seed:cargar_ejemplo_pedidos]"


class Command(BaseCommand):
    help = (
        "Siembra 2 rutas, 4 clientes y 2 pedidos de ejemplo (uno surtido completo, "
        "uno en borrador) para poder ver el módulo funcionando. Si no hay stock, "
        "mete existencias con un ajuste positivo para que el surtido tenga de dónde "
        "tomar. Rutas y clientes usan get_or_create y son seguros de correr varias "
        "veces; los pedidos se crean una sola vez, detectados por un sentinel en "
        "Pedido.notas — no se puede usar get_or_create en ellos porque su folio se "
        "autogenera y no existe hasta que ya se guardaron."
    )

    def handle(self, *args, **options):
        productos = list(Producto.objects.all().order_by("pk")[:3])
        if not productos:
            self.stdout.write(self.style.WARNING("No hay productos en catalogo; no se puede sembrar pedidos."))
            return

        almacen = self._crear_almacen()
        rutas = self._crear_rutas()
        clientes = self._crear_clientes(rutas)

        if Pedido.objects.filter(notas__startswith=SENTINEL).exists():
            self.stdout.write(self.style.WARNING("Ya existen pedidos de ejemplo; se omite crear nuevos."))
            return

        self._asegurar_stock(almacen, productos)
        self._crear_pedido_surtido(clientes[0], almacen, productos)
        self._crear_pedido_borrador(clientes[1], almacen, productos)
        self.stdout.write(self.style.SUCCESS("Datos de ejemplo de pedidos sembrados correctamente."))

    def _crear_almacen(self):
        almacen, creado = Almacen.objects.get_or_create(
            clave="ALM-01",
            defaults={
                "nombre": "Almacén central",
                "tipo_temperatura": Producto.TipoAlmacenamiento.FRIO,
                "activo": True,
            },
        )
        self.stdout.write(f"Almacén: {almacen} ({'creado' if creado else 'ya existía'})")
        return almacen

    def _crear_rutas(self):
        datos = [
            dict(clave="RT-01", nombre="Centro"),
            dict(clave="RT-02", nombre="Poniente"),
        ]
        rutas = []
        for entrada in datos:
            ruta, creado = Ruta.objects.get_or_create(
                clave=entrada["clave"], defaults={"nombre": entrada["nombre"], "activa": True}
            )
            self.stdout.write(f"Ruta: {ruta} ({'creada' if creado else 'ya existía'})")
            rutas.append(ruta)
        return rutas

    def _crear_clientes(self, rutas):
        datos = [
            dict(
                nombre_comercial="La Trattoria",
                razon_social="Grupo Trattoria S.A. de C.V.",
                rfc="GTR150410AB1",
                email="compras@latrattoria.mx",
                telefono_restaurante="4771112233",
                direccion_entrega="Av. Juárez 145, Col. Centro",
                nombre_chef="Marco Bellini",
                whatsapp_chef="4779998877",
                encargado_pedidos="Sofía Ramírez",
                whatsapp_encargado_pedidos="4778887766",
                ruta=rutas[0],
                pide_lunes=True,
                pide_miercoles=True,
                pide_viernes=True,
                dias_credito=15,
                limite_credito=Decimal("50000.00"),
                anotaciones="Entregar antes de las 10:00; después no reciben.",
            ),
            dict(
                nombre_comercial="Sushi Nami",
                razon_social="Nami Restaurantes S. de R.L.",
                rfc="NRE180722CD2",
                email="admin@sushinami.mx",
                telefono_restaurante="4772223344",
                direccion_entrega="Blvd. Campestre 890, Local 4",
                nombre_chef="Kenji Watanabe",
                whatsapp_chef="4776665544",
                encargado_pedidos="Diana Cortés",
                whatsapp_encargado_pedidos="4775554433",
                ruta=rutas[0],
                pide_martes=True,
                pide_jueves=True,
                dias_credito=30,
                limite_credito=Decimal("80000.00"),
                tarea_pendiente="Van a cambiar de encargado de pedidos en septiembre.",
            ),
            dict(
                nombre_comercial="Panadería El Molino",
                razon_social="Molino Artesanal S.A. de C.V.",
                rfc="MAR200315EF3",
                email="pedidos@elmolino.mx",
                telefono_restaurante="4773334455",
                direccion_entrega="Calle Hidalgo 22, Col. San Miguel",
                nombre_chef="Rosa Delgado",
                whatsapp_chef="4774443322",
                encargado_pedidos="Rosa Delgado",
                whatsapp_encargado_pedidos="4774443322",
                ruta=rutas[1],
                pide_lunes=True,
                pide_martes=True,
                pide_miercoles=True,
                pide_jueves=True,
                pide_viernes=True,
                pide_sabado=True,
                dias_credito=0,
                limite_credito=Decimal("0.00"),
                anotaciones="Solo contado. La chef es la misma que recibe.",
            ),
            dict(
                nombre_comercial="Hotel Casa Verde",
                razon_social="Operadora Casa Verde S.A. de C.V.",
                rfc="OCV120901GH4",
                email="ama.llaves@casaverde.mx",
                telefono_restaurante="4774445566",
                direccion_entrega="Paseo del Bosque 1200",
                nombre_chef="Iván Prieto",
                whatsapp_chef="4773332211",
                encargado_pedidos="Alejandra Núñez",
                whatsapp_encargado_pedidos="4772221100",
                ruta=rutas[1],
                pide_miercoles=True,
                pide_sabado=True,
                dias_credito=30,
                anotaciones="Sin límite de crédito definido todavía.",
            ),
        ]
        clientes = []
        for entrada in datos:
            nombre = entrada.pop("nombre_comercial")
            cliente, creado = Cliente.objects.get_or_create(
                nombre_comercial=nombre, defaults={**entrada, "activo": True}
            )
            self.stdout.write(
                f"Cliente: {cliente} — {cliente.ruta.clave}, "
                f"{cliente.frecuencia_semanal} día(s)/semana ({'creado' if creado else 'ya existía'})"
            )
            clientes.append(cliente)
        return clientes

    def _asegurar_stock(self, almacen, productos):
        """Mete existencias solo si el almacén está vacío para ese producto.

        Se usa AJUSTE_POSITIVO y no ENTRADA_COMPRA porque no hay una orden de
        compra detrás: es stock inicial de demostración.
        """
        hoy = timezone.localdate()
        for i, producto in enumerate(productos):
            if Lote.objects.disponibles().filter(producto=producto, almacen=almacen).exists():
                continue
            # Dos lotes con caducidades distintas para que se note el orden FEFO.
            for dias, cantidad in ((20 + i * 5, Decimal("12.000")), (75 + i * 5, Decimal("30.000"))):
                lote = Lote.objects.create(
                    producto=producto,
                    almacen=almacen,
                    fecha_caducidad=hoy + timedelta(days=dias),
                    costo_unitario=producto.precio_compra or Decimal("50.00"),
                    cantidad_inicial=Decimal("0"),
                    cantidad_actual=Decimal("0"),
                )
                registrar_movimiento(
                    lote,
                    MovimientoInventario.Tipo.AJUSTE_POSITIVO,
                    cantidad,
                    notas=f"{SENTINEL} stock inicial de demostración",
                )
                self.stdout.write(f"Lote: {lote} → {cantidad}")

    def _crear_pedido_surtido(self, cliente, almacen, productos):
        hoy = timezone.localdate()
        pedido = Pedido.objects.create(
            cliente=cliente,
            almacen_origen=almacen,
            fecha_pedido=hoy - timedelta(days=2),
            fecha_entrega_estimada=hoy - timedelta(days=1),
            estatus=Pedido.Estatus.CONFIRMADO,
            notas=f"{SENTINEL} pedido surtido completo",
        )
        detalles = [
            PedidoDetalle.objects.create(
                pedido=pedido,
                producto=producto,
                cantidad_pedida=cantidad,
                precio_unitario=producto.precio_venta,
            )
            for producto, cantidad in zip(productos, (Decimal("15.000"), Decimal("6.000")))
        ]
        surtido = Surtido.objects.create(
            pedido=pedido,
            fecha_surtido=hoy - timedelta(days=1),
            notas="Surtido completo de ejemplo.",
        )
        for detalle in detalles:
            surtir_pedido_detalle(detalle, detalle.cantidad_pedida, surtido)
        pedido.refresh_from_db()
        self.stdout.write(
            f"Pedido surtido: {pedido} → estatus {pedido.get_estatus_display()}, "
            f"total {pedido.total:.2f}"
        )

    def _crear_pedido_borrador(self, cliente, almacen, productos):
        hoy = timezone.localdate()
        pedido = Pedido.objects.create(
            cliente=cliente,
            almacen_origen=almacen,
            fecha_pedido=hoy,
            fecha_entrega_estimada=hoy + timedelta(days=1),
            notas=f"{SENTINEL} pedido en borrador, sin surtir",
        )
        for producto, cantidad in zip(productos, (Decimal("8.000"), Decimal("4.000"), Decimal("2.000"))):
            PedidoDetalle.objects.create(
                pedido=pedido,
                producto=producto,
                cantidad_pedida=cantidad,
                precio_unitario=producto.precio_venta,
            )
        self.stdout.write(
            f"Pedido borrador: {pedido} → estatus {pedido.get_estatus_display()}, "
            f"total {pedido.total:.2f}"
        )
