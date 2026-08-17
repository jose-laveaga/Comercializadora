from django.urls import path

from . import compras_consola, consola, views

app_name = "core"

urlpatterns = [
    # Página temporal de inventario.
    path("inventario/", consola.pagina_inventario, name="inventario"),
    path("inventario/ajuste/", consola.ajustar_inventario, name="ajuste"),
    path("crear/<str:que>/", consola.crear, name="crear"),
    # Página temporal de compras.
    path("compras/", compras_consola.pagina_compras, name="compras"),
    path("compras/crear/", compras_consola.crear_orden, name="compra_crear"),
    path("compras/<int:pk>/", compras_consola.ficha_orden, name="compra_ficha"),
    path("compras/<int:pk>/linea/", compras_consola.agregar_linea, name="compra_linea"),
    path(
        "compras/<int:pk>/linea/<int:linea_pk>/borrar/",
        compras_consola.borrar_linea,
        name="compra_linea_borrar",
    ),
    path("compras/<int:pk>/recibir/", compras_consola.recibir_orden, name="compra_recibir"),
    path(
        "compras/<int:pk>/linea/<int:linea_pk>/cerrar/",
        compras_consola.cerrar_faltante,
        name="compra_cerrar_faltante",
    ),
    path(
        "compras/<int:pk>/linea/<int:linea_pk>/reabrir/",
        compras_consola.reabrir_faltante,
        name="compra_reabrir_faltante",
    ),
    path("compras/<int:pk>/estatus/", compras_consola.cambiar_estatus, name="compra_estatus"),
    # Dashboard de verificación (solo lectura).
    path("verificacion/", views.dashboard, name="dashboard"),
]
