import os
import sys
import django
from django.db import connection

# 1. Configurar el entorno de Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pruebabd.settings')
django.setup()

from prueba.models import ImagenProducto

def vincular_imagenes_automatico():
    print("--- INICIANDO VINCULACIÓN MASIVA DE IMÁGENES ---")
    
    carpeta_img = os.path.join('media', 'productos')
    
    if not os.path.exists(carpeta_img):
        print(f"❌ Error: No se encontró la carpeta {carpeta_img}")
        return

    archivos = os.listdir(carpeta_img)
    vinculados = 0
    omitidos = 0

    for archivo in archivos:
        if archivo.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
            codigo_producto = os.path.splitext(archivo)[0].strip()

            try:
                # 2. BUSCAR EN LA TABLA BASE MEDIANTE SQL
                # Se utiliza r"""...""" para evitar advertencias de sintaxis con \d y \s
                with connection.cursor() as cursor:
                    cursor.execute(r"""
                        SELECT TRIM(BOTH FROM regexp_replace(
                            regexp_replace(
                                regexp_replace(upper((descripcion)::text), '\d+\s/\s*\d+"|\d+"|\d+\s*[Xx]\s*\d+\s*[Xx]\s*\d+|\d+\s*[Xx]\s*\d+|\d+\s CM|\d+\s MT|\d+\s ML|\d+\s L\y|\d+\s GR|\d+\sG\y|G\d+|\y\d+\y'::text, ''::text, 'g'::text), '[- /()]'::text, ' '::text, 'g'::text
                            ), 
                            '\s+'::text, ' '::text, 'g'::text
                        )) 
                        FROM producto 
                        WHERE codigo = %s
                        LIMIT 1
                    """, [codigo_producto])
                    
                    row = cursor.fetchone()

                if row and row[0]:
                    grupo_nombre = row[0]
                    ruta_relativa = f'productos/{archivo}'
                    
                    ImagenProducto.objects.update_or_create(
                        grupo_nombre=grupo_nombre,
                        defaults={'imagen': ruta_relativa}
                    )
                    vinculados += 1
                    print(f"✅ Éxito: Foto '{archivo}' vinculada al grupo -> {grupo_nombre}")
                else:
                    omitidos += 1
                    print(f"⚠️ Omisión: No se encontró el código '{codigo_producto}' en la base de datos.")

            except Exception as e:
                print(f"❌ Error procesando el código {codigo_producto}: {e}")

    print(f"\n--- PROCESO TERMINADO ---")
    print(f"✅ Grupos actualizados: {vinculados}")
    print(f"⚠️ Imágenes ignoradas por no existir código: {omitidos}")

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    vincular_imagenes_automatico()