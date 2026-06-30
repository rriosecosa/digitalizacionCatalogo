from django.urls import path
from . import views # Importa el archivo views.py de tu app 'prueba'

urlpatterns = [
    # Si mantuviste el nombre original 'lista_productos' en tu views.py:
    path('', views.lista_productos, name='productos'),
    
 
    path('login/', views.login_view, name='login'),
      path('logout/', views.logout_view, name='logout'),
]