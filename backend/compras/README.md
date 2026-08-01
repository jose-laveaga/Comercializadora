# compras + inventario

Cómo se modela la cadena de abastecimiento: qué se le pide a un proveedor, qué
llegó realmente, y qué existencias hay en cada almacén.

## Flujo

```
OrdenCompra (lo que se pidió)
  └── OrdenCompraDetalle (una línea: producto, cantidad, costo — snapshot al pedir)
        ↓ se recibe mercancía
Recepcion (un evento de recepción contra una orden)
  └── RecepcionDetalle (una línea: cuánto llegó, caducidad, costo real)
        ↓ al guardarse dispara, en una sola transacción:
        1. Lote (se crea o se reutiliza uno existente con mismo
           producto+almacén+caducidad+costo)
        2. MovimientoInventario tipo ENTRADA_COMPRA (registro permanente)
        3. Recalcula OrdenCompra.estatus (RECIBIDA_PARCIAL / RECIBIDA)
```

## Semáforo de caducidad

`Lote.estatus_caducidad` clasifica cada lote según `dias_para_caducar`:

| Estatus  | Días para caducar        |
|----------|---------------------------|
| Verde    | > 30                       |
| Amarillo | 30 ≥ días > 15              |
| Rojo     | 15 ≥ días > 5                |
| Negro    | días ≤ 5 (incluye ya caducado) |

Los límites (30/15/5, en `Lote.UMBRAL_AMARILLO/ROJO/NEGRO`) son cerrados hacia
el estatus más urgente: exactamente 30 días es Amarillo, no Verde. Se puede
filtrar en BD con `Lote.objects.con_estatus_caducidad(Lote.EstatusCaducidad.ROJO)`,
que usa los mismos umbrales traducidos a rango de fechas. En el admin de `Lote`
aparece como una columna con pastilla de color y como filtro ("estatus de
caducidad"), junto al filtro existente "por caducar" (ventana de N días).

**Invariante central: la única forma de cambiar `Lote.cantidad_actual` es
`inventario.services.registrar_movimiento()`.** Por eso `Lote` y
`MovimientoInventario` son de solo lectura en el admin (sin alta, sin edición,
sin borrado) — se generan exclusivamente por recepciones de compra hoy, o por
`registrar_movimiento()` desde shell/código para otros casos (ajustes, mermas,
devoluciones, futuras salidas por venta). Para cargar stock inicial sin pasar
por una orden de compra (p. ej. migrar inventario existente), usar
`registrar_movimiento()` desde el shell; no hay comando dedicado a eso todavía.

`RecepcionDetalle` solo dispara sus efectos de inventario **al crearse**. El
admin lo refleja congelando `orden_detalle`, `cantidad_recibida`,
`fecha_caducidad` y `costo_unitario_real` una vez guardada la línea, y
deshabilitando su borrado desde el inline de `Recepcion` — editar o borrar esa
línea después no deshace ni vuelve a calcular el movimiento/lote asociado.

`Lote.cantidad_inicial` nace siempre en 0 y crece vía movimientos — incluida
la primera recepción. No es un bug: representa "cantidad al insertar la fila",
no "cantidad de la recepción fundadora". `verificar_inventario` sigue siendo
válido bajo este esquema (`cantidad_inicial + Σmovimientos == cantidad_actual`).

`inventario` nunca importa de `compras` (evita el ciclo); la única referencia
en sentido inverso, `MovimientoInventario.recepcion_detalle`, usa un FK con
referencia string (`"compras.RecepcionDetalle"`).

`Lote.objects.fefo(producto, cantidad)` es **consultivo**: da los lotes en
orden de caducidad que cubrirían esa cantidad, pero no reserva ni bloquea
stock. No hay flujo de salida/venta todavía — cuando exista, debe volver a
bloquear cada lote (`select_for_update`) al momento de consumir, no confiar en
el resultado de `fefo()` directamente.

## Comandos

```bash
python manage.py check
python manage.py makemigrations
python manage.py migrate
python manage.py cargar_ejemplo_compras   # 1 almacén, 3 proveedores, 2 órdenes de ejemplo
python manage.py verificar_inventario     # reconcilia cantidad_actual contra los movimientos
```

`cargar_ejemplo_compras` es seguro de correr varias veces: los datos de
referencia (almacén, proveedores, ProveedorProducto) usan `get_or_create`; las
dos órdenes de ejemplo se marcan con un sentinel fijo en `notas` y no se
duplican si ya existen.

## Riesgos conocidos (no resueltos en este alcance)

- Sin `ATOMIC_REQUESTS` en el proyecto, guardar varias líneas de
  `RecepcionDetalle` en un solo POST del admin no está en una transacción
  externa — cada línea abre/cierra su propia transacción. Si la línea 2 de 3
  falla, la 1 ya quedó comprometida. Activar `ATOMIC_REQUESTS` es una decisión
  a nivel de todo el proyecto (afecta catalogo/pedidos también).
- Si un mismo POST agrega dos `RecepcionDetalle` contra la misma
  `OrdenCompraDetalle`, cada una valida el límite de 110% contra el estado en
  BD sin ver a su hermana todavía sin guardar — juntas podrían exceder el
  límite aunque cada una individualmente lo respete.
