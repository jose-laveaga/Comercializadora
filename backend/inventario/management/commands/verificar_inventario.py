from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db.models import Sum

from inventario.models import Lote


class Command(BaseCommand):
    help = "Compara Lote.cantidad_actual contra cantidad_inicial + suma de sus movimientos y reporta discrepancias."

    def handle(self, *args, **options):
        discrepancias = 0
        for lote in Lote.objects.select_related("producto", "almacen").order_by("codigo"):
            total_movimientos = lote.movimientos.aggregate(total=Sum("cantidad"))["total"] or Decimal("0")
            esperado = lote.cantidad_inicial + total_movimientos
            if esperado != lote.cantidad_actual:
                discrepancias += 1
                self.stdout.write(
                    self.style.ERROR(
                        f"{lote.codigo}: actual={lote.cantidad_actual} esperado={esperado} "
                        f"(diferencia={lote.cantidad_actual - esperado})"
                    )
                )
            else:
                self.stdout.write(self.style.SUCCESS(f"{lote.codigo}: OK ({lote.cantidad_actual})"))

        if discrepancias:
            self.stdout.write(self.style.ERROR(f"\n{discrepancias} lote(s) con discrepancias."))
        else:
            self.stdout.write(self.style.SUCCESS("\nSin discrepancias. Todos los lotes cuadran."))
