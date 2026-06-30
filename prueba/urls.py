from django.urls import path
from .views import lista_productos, detalle_producto
from django.contrib.auth import views as auth_views
from . import views

urlpatterns = [
    path('', lista_productos, name='productos'),
    path('producto/<int:producto_id>/', detalle_producto, name='detalle_producto'),
    path('detalle/<int:producto_id>/', detalle_producto, name='detalle'), 
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
]