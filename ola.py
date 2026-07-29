import os
import sys
import django

# 1. Configurar el entorno de Django (Asegúrate de que 'pruebabd' sea el nombre de tu proyecto)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pruebabd.settings')
django.setup()

from prueba.models import VistaProductoAgrupado, ImagenProducto

def vincular_imagenes_automatico():
    print("--- INICIANDO VINCULACIÓN MASIVA DE IMÁGENES ---")
    
    # Ruta directa a la carpeta donde extrajiste las fotos
    carpeta_img = os.path.join('media', 'productos')
    
    if not os.path.exists(carpeta_img):
        print(f"❌ Error: No se encontró la carpeta {carpeta_img}")
        return

    # Leer todos los archivos de la carpeta
    archivos = os.listdir(carpeta_img)
    vinculados = 0

    for archivo in archivos:
        # Filtrar solo imágenes
        if archivo.lower().endswith(('.png', '.jpg', '.jpeg')):
            # Extraer el código del nombre del archivo (ej: 11-11-111.png -> 11-11-111)
            codigo_producto = os.path.splitext(archivo)[0]

            try:
                # 2. Buscar si existe algún producto con ese código en la vista agrupada
                producto = VistaProductoAgrupado.objects.filter(codigo=codigo_producto).first()

                if producto:
                    # 3. Identificar el grupo de ese producto
                    grupo_nombre = producto.descripcion_grupo or producto.descripcion
                    
                    if grupo_nombre:
                        # 4. Vincular la ruta relativa a la tabla ImagenProducto
                        ruta_relativa = f'productos/{archivo}'
                        
                        # update_or_create actualizará el grupo si ya existe, o lo creará si es nuevo
                        ImagenProducto.objects.update_or_create(
                            grupo_nombre=grupo_nombre,
                            defaults={'imagen': ruta_relativa}
                        )
                        vinculados += 1
                        print(f"✅ Éxito: Foto '{archivo}' vinculada al grupo -> {grupo_nombre}")
                else:
                    print(f"⚠️ Omisión: No se encontró el código {codigo_producto} en la base de datos.")

            except Exception as e:
                # Captura errores de codificación o de base de datos sin detener el bucle
                print(f"❌ Error procesando el código {codigo_producto}: {e}")

    print(f"\n--- PROCESO TERMINADO: {vinculados} grupos actualizados con éxito ---")

if __name__ == '__main__':
    # Forzar la consola a usar UTF-8 para evitar caídas visuales en Windows
    sys.stdout.reconfigure(encoding='utf-8')
    vincular_imagenes_automatico()