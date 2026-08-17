"""Etiquetas de lote imprimibles.

Genera una hoja de etiquetas Avery 5163 (10 por hoja, 4"×2") lista para mandar
a la impresora láser desde el navegador. El código impreso es `Lote.codigo`,
que ya existía y ya es único e inmutable — esta vista no inventa identidad
nueva, solo la hace legible para un escáner.

Deliberadamente fuera de alcance por ahora: ZPL e impresoras térmicas, colas de
impresión, y el registro de qué etiqueta se imprimió cuándo y por quién.
"""

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render

from core import code128

from .models import Lote

# Avery 5163: 2 columnas × 5 filas en hoja carta.
ETIQUETAS_POR_HOJA = 10

# Ancho útil para el código de barras dentro de la etiqueta, en mm. Sirve para
# avisar cuando un código queda con módulos demasiado finos para leerse.
ANCHO_BARRAS_MM = 92

# Tope de seguridad: una selección grande con muchas copias puede volverse un
# documento imprimible de cientos de hojas sin querer.
MAXIMO_ETIQUETAS = 200


def _ids_solicitados(request):
    crudo = request.GET.get("lotes", "")
    ids = []
    for parte in crudo.split(","):
        parte = parte.strip()
        if parte.isdigit():
            ids.append(int(parte))
    return ids


def _copias(request):
    try:
        copias = int(request.GET.get("copias", 1))
    except (TypeError, ValueError):
        return 1
    return max(1, min(copias, 50))


@staff_member_required
def etiquetas_lotes(request):
    ids = _ids_solicitados(request)
    copias = _copias(request)

    lotes = (
        Lote.objects.filter(pk__in=ids)
        .select_related("producto__linea", "producto__marca", "almacen")
        .order_by("producto__sku", "fecha_caducidad")
    )

    etiquetas = []
    estrechos = []
    for lote in lotes:
        ancho_modulo = code128.ancho_modulo_mm(lote.codigo, ANCHO_BARRAS_MM)
        if ancho_modulo < code128.ANCHO_MODULO_MINIMO_MM:
            estrechos.append((lote.codigo, ancho_modulo))
        etiqueta = {"lote": lote, "barras": code128.svg(lote.codigo)}
        etiquetas.extend([etiqueta] * copias)

    truncado = len(etiquetas) > MAXIMO_ETIQUETAS
    etiquetas = etiquetas[:MAXIMO_ETIQUETAS]

    hojas = [
        etiquetas[i : i + ETIQUETAS_POR_HOJA]
        for i in range(0, len(etiquetas), ETIQUETAS_POR_HOJA)
    ]

    return render(
        request,
        "inventario/etiquetas_lote.html",
        {
            "hojas": hojas,
            "total": len(etiquetas),
            "copias": copias,
            "truncado": truncado,
            "maximo": MAXIMO_ETIQUETAS,
            "estrechos": estrechos,
            "minimo_mm": code128.ANCHO_MODULO_MINIMO_MM,
            "sin_seleccion": not ids,
        },
    )
