from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, include

from . import views
from .views import detalle_producto, lista_productos

urlpatterns = [
    # Panel de administrador de Django
    path('admin/', admin.site.urls),

    # 1. TUS RUTAS PERSONALIZADAS TIENEN PRIORIDAD (El logout seguro)
    path("logout/", views.logout_view, name="logout"),

    # 2. RUTAS NATIVAS DE DJANGO (Se cargan después para no pisar las tuyas)
    path('', include('django.contrib.auth.urls')),

    # El resto de tus rutas normales
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
    path('exportar/', views.menu_exportar, name='menu_exportar'),
    path('generar-pdf/', views.generar_pdf, name='generar_pdf'),

    # --------------------------------------------------------
    # NUEVO: historial de catálogos generados y descargas
    # --------------------------------------------------------
    path('catalogo/historial/', views.historial_catalogo, name='historial_catalogo'),
    path('catalogo/descargar/', views.descargar_catalogo, name='descargar_catalogo'),
    path('catalogo/descargar/<int:catalogo_id>/', views.descargar_catalogo_version, name='descargar_catalogo_version'),

    # --------------------------------------------------------
    # NUEVO: Eliminar catálogo (Solo Superusuarios)
    # --------------------------------------------------------
    path('catalogo/historial/eliminar/<int:catalogo_id>/', views.eliminar_catalogo, name='eliminar_catalogo'),
    path('catalogo/marcar-vigente/<int:catalogo_id>/', views.marcar_catalogo_vigente, name='marcar_catalogo_vigente'),
    path('dashboard/grupos/', views.gestionar_grupos, name='gestionar_grupos'),
    path('api/sugerencias/', views.sugerencias_busqueda, name='sugerencias_busqueda'),
    path('generar-pdf/iniciar/', views.iniciar_generacion_pdf, name='iniciar_generacion_pdf'),
    path('generar-pdf/estado/<str:job_id>/', views.estado_generacion_pdf, name='estado_generacion_pdf'),
    path('generar-pdf/ver/<int:catalogo_id>/', views.ver_pdf_generado, name='ver_pdf_generado'),

    # --------------------------------------------------------
    # NUEVO: Sincronización de productos con el ERP
    # --------------------------------------------------------
    path("productos/sincronizar/", views.sincronizar_productos, name="sincronizar_productos"),
    path("productos/sincronizar/ejecutar/", views.sincronizar_productos_ejecutar, name="sincronizar_productos_ejecutar"),
    path("productos/sincronizar/prueba/", views.sincronizar_productos_dry_run, name="sincronizar_productos_dry_run"),
]

# ESTAS LÍNEAS LE DICEN A DJANGO DÓNDE ENCONTRAR LAS IMÁGENES DE /media/ EN ENTORNO DE DESARROLLO
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)