"""Traduce un código escaneado o tecleado a lo que representa en el sistema.

Este módulo es el contrato: todo lo que en el futuro lea un código —el surtido
dirigido, el conteo cíclico, la recepción con terminal— debe entrar por
`resolver_codigo` y no volver a decidir por su cuenta qué significa una cadena
de caracteres. Si mañana hay que soportar GS1-128 o matrículas de tarima, se
agrega un eslabón a la cadena aquí adentro y nadie más se entera.

Hoy solo lo consume el buscador de la consola de inventario. Esa es la parte
"placeholder": la funcionalidad de operación no existe todavía, pero el punto
por donde va a entrar sí, y ya está probado.
"""

from dataclasses import dataclass

from catalogo.models import CodigoBarras, Producto

from .models import Lote


class TipoCodigo:
    """Qué resultó ser el código escaneado."""

    LOTE = "lote"
    SKU = "sku"
    CODIGO_BARRAS = "codigo_barras"
    DESCONOCIDO = "desconocido"


@dataclass(frozen=True)
class ResultadoEscaneo:
    """Lo que se supo del código, en una forma que sirva para actuar.

    `unidades` es cuántas piezas representa un escaneo: 1 casi siempre, pero N
    si lo que se leyó fue el código de la caja. Quien registre un movimiento a
    partir de un escaneo debe multiplicar por este número, no asumir 1.
    """

    tipo: str
    codigo: str
    objeto: object = None
    producto: Producto = None
    lote: Lote = None
    unidades: int = 1

    def __bool__(self):
        return self.tipo != TipoCodigo.DESCONOCIDO

    @property
    def descripcion(self):
        if self.tipo == TipoCodigo.LOTE:
            return f"Lote {self.lote.codigo} — {self.producto.sku}"
        if self.tipo == TipoCodigo.SKU:
            return f"Producto {self.producto.sku}"
        if self.tipo == TipoCodigo.CODIGO_BARRAS:
            empaque = self.objeto.get_nivel_empaque_display().lower()
            return f"Producto {self.producto.sku} — código de {empaque}"
        return f"No se reconoció «{self.codigo}»"


def normalizar(texto):
    """Limpia lo que manda el escáner antes de compararlo.

    Los escáneres agregan un retorno de carro al final y a veces espacios; los
    códigos de lote y los SKU se guardan en mayúsculas.
    """
    return (texto or "").strip().upper()


def resolver_codigo(texto, almacen=None):
    """Averigua qué es `texto` y devuelve un ResultadoEscaneo.

    Prueba en orden de especificidad: primero lo que identifica un ejemplar
    concreto (el lote), luego lo que identifica un producto (SKU y códigos del
    fabricante). El orden importa: un lote lleva el SKU adentro, así que buscar
    por SKU primero daría respuestas ambiguas.

    `almacen` no filtra la resolución —un código significa lo mismo en toda la
    empresa— pero se acepta desde ahora porque las pantallas de operación van a
    querer pasarlo para acotar las existencias que muestran después.
    """
    codigo = normalizar(texto)
    if not codigo:
        return ResultadoEscaneo(tipo=TipoCodigo.DESCONOCIDO, codigo=codigo)

    lote = Lote.objects.select_related("producto", "almacen").filter(codigo=codigo).first()
    if lote is not None:
        return ResultadoEscaneo(
            tipo=TipoCodigo.LOTE,
            codigo=codigo,
            objeto=lote,
            producto=lote.producto,
            lote=lote,
        )

    producto = Producto.objects.select_related("linea", "marca").filter(sku=codigo).first()
    if producto is not None:
        return ResultadoEscaneo(
            tipo=TipoCodigo.SKU, codigo=codigo, objeto=producto, producto=producto
        )

    codigo_barras = (
        CodigoBarras.objects.select_related("producto__linea", "producto__marca")
        .filter(codigo=codigo, activo=True)
        .first()
    )
    if codigo_barras is not None:
        return ResultadoEscaneo(
            tipo=TipoCodigo.CODIGO_BARRAS,
            codigo=codigo,
            objeto=codigo_barras,
            producto=codigo_barras.producto,
            unidades=codigo_barras.unidades,
        )

    return ResultadoEscaneo(tipo=TipoCodigo.DESCONOCIDO, codigo=codigo)


def existencias_de(resultado, almacen=None):
    """Lotes con existencia relacionados con lo que se escaneó.

    Si se escaneó un lote, es ese lote. Si se escaneó un producto (por SKU o
    por código de barras), son todos sus lotes disponibles en orden FEFO, que
    es el mismo orden en que saldrían.
    """
    if not resultado:
        return Lote.objects.none()

    lotes = Lote.objects.disponibles().select_related("producto", "almacen")
    if almacen is not None:
        lotes = lotes.filter(almacen=almacen)

    if resultado.tipo == TipoCodigo.LOTE:
        return lotes.filter(pk=resultado.lote.pk)
    return lotes.filter(producto=resultado.producto).order_by("fecha_caducidad", "pk")
