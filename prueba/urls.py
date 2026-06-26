from django.urls import path
from .views import lista_productos

urlpatterns = [
    path('', lista_productos, name='productos'),
]