"""Validación de los códigos de barras numéricos del fabricante (familia GTIN).

EAN-13, EAN-8, UPC-A e ITF-14 comparten el mismo dígito verificador: se recorren
los dígitos de datos de derecha a izquierda alternando peso 3 y 1, se suman, y
el verificador es lo que falta para llegar a la siguiente decena.

Vale la pena validarlo desde el primer día: un código mal transcrito se ve igual
que uno bueno, y el error solo aparece meses después, cuando alguien escanea un
producto y el sistema dice que no existe.
"""

import re

# Cuántos dígitos tiene cada simbología, contando el verificador.
LONGITUDES = {
    "ean13": 13,
    "ean8": 8,
    "upca": 12,
    "itf14": 14,
}

_NO_DIGITOS = re.compile(r"\D")


def normalizar(codigo):
    """Quita espacios, guiones y cualquier separador que traiga el código."""
    return _NO_DIGITOS.sub("", codigo or "")


def digito_verificador(digitos_datos):
    """Calcula el dígito verificador de una cadena de dígitos SIN verificador."""
    suma = 0
    for i, digito in enumerate(reversed(digitos_datos)):
        peso = 3 if i % 2 == 0 else 1
        suma += int(digito) * peso
    return (10 - suma % 10) % 10


def es_valido(codigo, simbologia):
    """¿`codigo` tiene la longitud y el dígito verificador correctos?

    Devuelve True para simbologías que no llevan verificador (p. ej. CODE128),
    porque ahí no hay nada que comprobar.
    """
    largo = LONGITUDES.get(simbologia)
    if largo is None:
        return True
    codigo = normalizar(codigo)
    if len(codigo) != largo:
        return False
    return int(codigo[-1]) == digito_verificador(codigo[:-1])
