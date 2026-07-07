from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from . import views
from .views import detalle_producto, lista_productos

urlpatterns = [
    path("", lista_productos, name="productos"),
    path("producto/<int:producto_id>/", detalle_producto, name="detalle_producto"),
    path("detalle/<int:producto_id>/", detalle_producto, name="detalle"),
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("dashboard/", views.dashboard_productos, name="dashboard"),
    path("editar/<int:producto_id>/", views.editar_producto, name="editar_producto"),
    path("logout/", views.logout_view, name="logout"),
    path('exportar/', views.menu_exportar_pdf, name='menu_exportar'),
    path('generar-pdf/', views.generar_pdf, name='generar_pdf'),
]

# ESTAS LÍNEAS LE DICEN A DJANGO DÓNDE ENCONTRAR LAS IMÁGENES DE /media/ EN ENTORNO DE DESARROLLO
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)