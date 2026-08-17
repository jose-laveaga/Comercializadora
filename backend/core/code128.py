"""Generador de códigos de barras Code 128-B, sin dependencias externas.

Se implementa a mano en vez de usar una librería porque el proyecto todavía no
tiene ninguna dependencia fuera de Django y Pillow, y lo que se necesita aquí
es un subconjunto pequeño y estable del estándar: subconjunto B (ASCII 32-126),
que cubre de sobra el formato de `inventario.Lote.codigo` (SKU + fecha +
consecutivo, todo mayúsculas, dígitos y guiones).

Lo que NO hace, a propósito:
  - Subconjunto C (compresión de pares de dígitos). Produciría códigos más
    cortos para lotes muy numéricos, pero duplica la complejidad del encoder.
  - GS1-128 con identificadores de aplicación (01/10/17). Cuando se necesite,
    entra como una capa que arma el texto antes de llamar aquí — este módulo no
    la estorba.

El SVG sale con viewBox en módulos y sin ancho físico: quien lo inserta decide
el tamaño con CSS. Usa `modulos()` para saber cuántos módulos ocupa un texto y
comprobar que el ancho de módulo resultante sea legible para el escáner.
"""

from xml.sax.saxutils import escape

# Patrones del estándar Code 128, uno por valor 0-106. Cada dígito es el ancho
# en módulos de un elemento, alternando barra/espacio y empezando por barra.
PATRONES = (
    "212222", "222122", "222221", "121223", "121322", "131222", "122213", "122312", "132212", "221213",
    "221312", "231212", "112232", "122132", "122231", "113222", "123122", "123221", "223211", "221132",
    "221231", "213212", "223112", "312131", "311222", "321122", "321221", "312212", "322112", "322211",
    "212123", "212321", "232121", "111323", "131123", "131321", "112313", "132113", "132311", "211313",
    "231113", "231311", "112133", "112331", "132131", "113123", "113321", "133121", "313121", "211331",
    "231131", "213113", "213311", "213131", "311123", "311321", "331121", "312113", "312311", "332111",
    "314111", "221411", "431111", "111224", "111422", "121124", "121421", "141122", "141221", "112214",
    "112412", "122114", "122411", "142112", "142211", "241211", "221114", "413111", "241112", "134111",
    "111242", "121142", "121241", "141142", "141241", "114212", "124112", "124211", "411212", "421112",
    "421211", "212141", "214121", "412121", "111143", "111341", "131141", "114113", "114311", "411113",
    "411311", "113141", "114131", "211412", "211214", "211232", "2331112",
)

INICIO_B = 104
PARO = 106

# Ancho de módulo mínimo recomendado para lectura confiable con escáner láser
# a distancia corta. Por debajo de esto la etiqueta imprime, pero falla al leer.
ANCHO_MODULO_MINIMO_MM = 0.19


class Code128Error(ValueError):
    """El texto no se puede representar en Code 128-B."""


def _valores(texto):
    """Traduce el texto a valores Code 128-B, con inicio y dígito de control.

    En el subconjunto B el valor de cada carácter es su código ASCII menos 32,
    así que solo entran los imprimibles del 32 al 126.
    """
    valores = [INICIO_B]
    for i, caracter in enumerate(texto):
        codigo = ord(caracter)
        if not 32 <= codigo <= 126:
            raise Code128Error(
                f"El carácter {caracter!r} (posición {i}) no existe en Code 128-B; "
                "solo se admiten los ASCII imprimibles del 32 al 126."
            )
        valores.append(codigo - 32)

    # Suma ponderada: el inicio pesa 1 y cada carácter pesa su posición.
    suma = valores[0] + sum(i * v for i, v in enumerate(valores[1:], start=1))
    valores.append(suma % 103)
    valores.append(PARO)
    return valores


def modulos(texto):
    """Cuántos módulos de ancho ocupa el código de barras de `texto`.

    Sirve para decidir el ancho físico: ancho_mm / modulos(texto) es el ancho de
    módulo, que conviene comparar contra ANCHO_MODULO_MINIMO_MM.
    """
    return sum(sum(int(d) for d in PATRONES[v]) for v in _valores(texto))


def ancho_modulo_mm(texto, ancho_mm):
    """Ancho de módulo en mm si el código se imprime en `ancho_mm` de ancho."""
    return ancho_mm / modulos(texto)


def svg(texto, alto=100, quiet_zone=10, clase="code128"):
    """Devuelve el Code 128-B de `texto` como SVG listo para insertar en HTML.

    El viewBox está en módulos y el SVG no declara ancho ni alto físicos: se
    estira a lo que le diga el CSS del contenedor. `quiet_zone` es el margen
    blanco obligatorio a cada lado, en módulos — sin él, el escáner no lee.
    """
    if not texto:
        raise Code128Error("No se puede generar un código de barras vacío.")

    barras = []
    x = quiet_zone
    for valor in _valores(texto):
        es_barra = True
        for ancho in PATRONES[valor]:
            ancho = int(ancho)
            if es_barra:
                barras.append(f'<rect x="{x}" y="0" width="{ancho}" height="{alto}"/>')
            x += ancho
            es_barra = not es_barra

    ancho_total = x + quiet_zone
    return (
        f'<svg class="{escape(clase)}" viewBox="0 0 {ancho_total} {alto}" '
        f'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="{escape(texto)}" fill="#000">'
        f"{''.join(barras)}"
        "</svg>"
    )
