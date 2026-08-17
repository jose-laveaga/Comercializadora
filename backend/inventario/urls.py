from django.urls import path

from . import views

app_name = "inventario"

urlpatterns = [
    path("etiquetas/", views.etiquetas_lotes, name="etiquetas_lote"),
]
