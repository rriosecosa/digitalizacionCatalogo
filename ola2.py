import os
import sys
import django

# 1. Configurar el entorno de Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pruebabd.settings')
django.setup()

from django.db import close_old_connections, transaction
from prueba.models import VistaProductoAgrupado, ImagenProducto

def asignar_rutas_pendientes():
    print("--- BUSCANDO GRUPOS Y PRODUCTOS SIN IMAGEN ---")
    
    # Cerrar conexiones viejas/inactivas para evitar errores de socket
    close_old_connections()
    
    # 1. Cargar grupos de la vista
    grupos = list(VistaProductoAgrupado.objects.all())
    
    # 2. Cargar mapa de imágenes existentes en memoria
    imagenes_existentes = {
        img.grupo_nombre: img 
        for img in ImagenProducto.objects.all()
    }

    nuevos_objetos = []
    actualizaciones = []
    lista_codigos_pendientes = []
    
    ya_existentes = 0
    nuevos_preasignados = 0

    for grupo in grupos:
        nombre_grupo = grupo.descripcion_grupo or grupo.descripcion
        codigo_rep = grupo.codigo

        if not nombre_grupo or not codigo_rep:
            continue

        ruta_esperada = f"productos/{codigo_rep}.png"
        
        # Verificar si el grupo ya existe en la tabla ImagenProducto
        if nombre_grupo in imagenes_existentes:
            img_obj = imagenes_existentes[nombre_grupo]
            # Si ya tiene foto asignada, no la tocamos
            if img_obj.imagen and str(img_obj.imagen).strip():
                ya_existentes += 1
            else:
                # Si existe pero está vacía, le asignamos la ruta esperada
                img_obj.imagen = ruta_esperada
                actualizaciones.append(img_obj)
                nuevos_preasignados += 1
                lista_codigos_pendientes.append(
                    f"Código: {codigo_rep} | Foto esperada: {codigo_rep}.png | Grupo: {nombre_grupo}"
                )
        else:
            # Si no está en ImagenProducto, se genera nuevo registro
            nuevos_objetos.append(
                ImagenProducto(
                    grupo_nombre=nombre_grupo,
                    imagen=ruta_esperada
                )
            )
            nuevos_preasignados += 1
            lista_codigos_pendientes.append(
                f"Código: {codigo_rep} | Foto esperada: {codigo_rep}.png | Grupo: {nombre_grupo}"
            )

    # 3. Guardar cambios masivamente
    print("💾 Guardando cambios masivamente en Neon BD...")
    close_old_connections()
    
    with transaction.atomic():
        if nuevos_objetos:
            ImagenProducto.objects.bulk_create(nuevos_objetos, batch_size=500)
        if actualizaciones:
            ImagenProducto.objects.bulk_update(actualizaciones, ['imagen'], batch_size=500)

    print("\n" + "="*60)
    print(f"📊 RESUMEN DE AUDITORÍA DE IMÁGENES:")
    print(f"✅ Grupos que ya tenían imagen: {ya_existentes}")
    print(f"📌 Nuevas rutas asignadas en BD: {nuevos_preasignados}")
    print("="*60)

    # Generar el reporte de fotos pendientes
    if lista_codigos_pendientes:
        archivo_reporte = "fotos_faltantes.txt"
        with open(archivo_reporte, "w", encoding="utf-8") as f:
            f.write("LISTA DE FOTOS QUE DEBES MOVER A media/productos/\n")
            f.write("===================================================\n\n")
            for item in lista_codigos_pendientes:
                f.write(item + "\n")
        
        print(f"\n📝 Se ha generado el archivo '{archivo_reporte}' con el listado exacto de fotos que debes conseguir.")

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asignar_rutas_pendientes()