from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('prueba.urls')), # Esto delega todo el control a tu app 'prueba'
]