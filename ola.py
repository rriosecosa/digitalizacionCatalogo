import os
import django

# 1. Configuración del entorno (Ajusta 'tu_proyecto' por el nombre de tu carpeta principal)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pruebabd.settings') 
django.setup()

# 2. IMPORTAMOS LA VISTA EN LUGAR DE LA TABLA BASE (Ajusta 'tu_app')
from prueba.models import VistaProductoAgrupado, ImagenProducto 

def previncular_imagenes():
    print("--- INICIANDO PRE-VINCULACIÓN MASIVA DE IMÁGENES ---")

    # Cargamos en memoria los grupos que ya tienen foto para no sobreescribir
    grupos_procesados = set(ImagenProducto.objects.values_list('grupo_nombre', flat=True))
    
    # 3. CONSULTAMOS LA VISTA SQL DONDE SÍ VIVEN LOS GRUPOS
    productos = VistaProductoAgrupado.objects.all()

    for p in productos:
        # Ahora sí, descripcion_grupo existe nativamente aquí
        grupo_nombre = p.descripcion_grupo or p.descripcion
        codigo = p.codigo 

        if not grupo_nombre or not codigo:
            continue

        # Evitamos procesar grupos repetidos o que ya tienen imagen
        if grupo_nombre in grupos_procesados:
            continue

        # Ruta estándar a asignar
        ruta_imagen = f"productos/{codigo}.png"

        try:
            img_obj, created = ImagenProducto.objects.update_or_create(
                grupo_nombre=grupo_nombre,
                defaults={'imagen': ruta_imagen}
            )
            
            if created:
                print(f"✅ Vinculado: {grupo_nombre} -> {ruta_imagen}")
            
            grupos_procesados.add(grupo_nombre)

        except Exception as e:
            print(f"⚠️ Error al procesar el grupo '{grupo_nombre}': {e}")
            continue

    print("--- PROCESO FINALIZADO ---")

if __name__ == '__main__':
    previncular_imagenes()