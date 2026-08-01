from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from catalogo.models import Producto
from inventario.models import Almacen
from compras.models import (
    OrdenCompra,
    OrdenCompraDetalle,
    Proveedor,
    ProveedorProducto,
    Recepcion,
    RecepcionDetalle,
)

SENTINEL = "[seed:cargar_ejemplo_compras]"


class Command(BaseCommand):
    help = (
        "Siembra 1 almacén, 3 proveedores, sus ProveedorProducto y 2 órdenes de "
        "compra de ejemplo (una recibida completa, una parcial) para poder ver "
        "el inventario poblado. Los datos de referencia (almacén, proveedores, "
        "ProveedorProducto) usan get_or_create y son seguros de correr varias "
        "veces; las órdenes/recepciones de ejemplo se crean una sola vez, "
        "detectadas por un sentinel fijo en OrdenCompra.notas — no se puede usar "
        "get_or_create en ellas porque su folio se autogenera y no existe hasta "
        "que ya se guardaron."
    )

    def handle(self, *args, **options):
        almacen = self._crear_almacen()
        proveedores = self._crear_proveedores()
        productos = list(Producto.objects.all().order_by("pk"))
        if not productos:
            self.stdout.write(self.style.WARNING("No hay productos en catalogo; no se puede sembrar compras."))
            return
        proveedor_producto = self._crear_proveedor_producto(proveedores, productos)

        if OrdenCompra.objects.filter(notas__startswith=SENTINEL).exists():
            self.stdout.write(self.style.WARNING("Ya existen órdenes de ejemplo; se omite crear nuevas."))
            return

        self._crear_orden_recibida_completa(proveedores[0], almacen, productos, proveedor_producto)
        self._crear_orden_recibida_parcial(proveedores[1], almacen, productos, proveedor_producto)
        self.stdout.write(self.style.SUCCESS("Datos de ejemplo de compras sembrados correctamente."))

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

    def _crear_proveedores(self):
        datos = [
            dict(
                nombre="Lácteos del Bajío",
                razon_social="Lácteos del Bajío S.A. de C.V.",
                rfc="LBA850101AB1",
                contacto="Rosa Martínez",
                telefono="4771234567",
                email="ventas@lacteosdelbajio.mx",
                dias_entrega_estimados=3,
            ),
            dict(
                nombre="Distribuidora Alpina",
                razon_social="Distribuidora Alpina S.A. de C.V.",
                rfc="DAL900202CD2",
                contacto="Juan Torres",
                telefono="5512345678",
                email="pedidos@alpina.mx",
                dias_entrega_estimados=5,
            ),
            dict(
                nombre="Quesos y Más",
                razon_social="Quesos y Más S.A. de C.V.",
                rfc="QYM110303EF3",
                contacto="Laura Gómez",
                telefono="3312345678",
                email="contacto@quesosymas.mx",
                dias_entrega_estimados=2,
            ),
        ]
        proveedores = []
        for entrada in datos:
            nombre = entrada.pop("nombre")
            proveedor, creado = Proveedor.objects.get_or_create(
                nombre=nombre, defaults={**entrada, "activo": True}
            )
            self.stdout.write(f"Proveedor: {proveedor} ({'creado' if creado else 'ya existía'})")
            proveedores.append(proveedor)
        return proveedores

    def _crear_proveedor_producto(self, proveedores, productos):
        relaciones = {}
        for i, proveedor in enumerate(proveedores):
            for producto in productos:
                costo = producto.precio_compra if producto.precio_compra > 0 else Decimal("50.00")
                pp, _creado = ProveedorProducto.objects.get_or_create(
                    proveedor=proveedor,
                    producto=producto,
                    defaults={
                        "sku_proveedor": f"{proveedor.pk}-{producto.sku}",
                        "costo_actual": costo,
                        "unidades_por_caja": 12,
                        "pedido_minimo": Decimal("10.000"),
                        "activo": True,
                    },
                )
                relaciones[(proveedor.pk, producto.pk)] = pp
        return relaciones

    def _crear_orden_recibida_completa(self, proveedor, almacen, productos, proveedor_producto):
        hoy = timezone.localdate()
        orden = OrdenCompra.objects.create(
            proveedor=proveedor,
            almacen_destino=almacen,
            fecha_emision=hoy - timedelta(days=5),
            fecha_entrega_estimada=hoy - timedelta(days=2),
            moneda="MXN",
            notas=f"{SENTINEL} orden completa",
        )
        detalles = []
        for producto in productos:
            pp = proveedor_producto[(proveedor.pk, producto.pk)]
            detalles.append(
                OrdenCompraDetalle.objects.create(
                    orden=orden,
                    producto=producto,
                    cantidad_pedida=Decimal("50.000"),
                    unidad_compra=OrdenCompraDetalle.UnidadCompra.CAJA,
                    unidades_por_caja=pp.unidades_por_caja,
                    costo_unitario=pp.costo_actual,
                )
            )
        recepcion = Recepcion.objects.create(
            orden_compra=orden,
            fecha_recepcion=hoy - timedelta(days=2),
            folio_factura="FAC-0001",
            notas="Recepción completa de ejemplo.",
        )
        for detalle in detalles:
            RecepcionDetalle.objects.create(
                recepcion=recepcion,
                orden_detalle=detalle,
                cantidad_recibida=detalle.cantidad_pedida,
                fecha_caducidad=hoy + timedelta(days=60),
                costo_unitario_real=detalle.costo_unitario,
            )
        orden.refresh_from_db()
        self.stdout.write(f"Orden completa: {orden} → estatus {orden.get_estatus_display()}")

    def _crear_orden_recibida_parcial(self, proveedor, almacen, productos, proveedor_producto):
        hoy = timezone.localdate()
        orden = OrdenCompra.objects.create(
            proveedor=proveedor,
            almacen_destino=almacen,
            fecha_emision=hoy - timedelta(days=3),
            fecha_entrega_estimada=hoy + timedelta(days=1),
            moneda="MXN",
            notas=f"{SENTINEL} orden parcial",
        )
        detalles = []
        for producto in productos:
            pp = proveedor_producto[(proveedor.pk, producto.pk)]
            detalles.append(
                OrdenCompraDetalle.objects.create(
                    orden=orden,
                    producto=producto,
                    cantidad_pedida=Decimal("40.000"),
                    unidad_compra=OrdenCompraDetalle.UnidadCompra.CAJA,
                    unidades_por_caja=pp.unidades_por_caja,
                    costo_unitario=pp.costo_actual,
                )
            )
        recepcion = Recepcion.objects.create(
            orden_compra=orden,
            fecha_recepcion=hoy,
            folio_factura="FAC-0002",
            notas="Recepción parcial de ejemplo: solo llegó parte del pedido.",
        )
        # Primer detalle: recibido completo. Segundo: recibido a la mitad.
        # Resto (si hay más productos): todavía no llega, queda pendiente.
        if detalles:
            RecepcionDetalle.objects.create(
                recepcion=recepcion,
                orden_detalle=detalles[0],
                cantidad_recibida=detalles[0].cantidad_pedida,
                fecha_caducidad=hoy + timedelta(days=45),
                costo_unitario_real=detalles[0].costo_unitario,
            )
        if len(detalles) > 1:
            RecepcionDetalle.objects.create(
                recepcion=recepcion,
                orden_detalle=detalles[1],
                cantidad_recibida=detalles[1].cantidad_pedida / 2,
                fecha_caducidad=hoy + timedelta(days=45),
                costo_unitario_real=detalles[1].costo_unitario,
            )
        orden.refresh_from_db()
        self.stdout.write(f"Orden parcial: {orden} → estatus {orden.get_estatus_display()}")
