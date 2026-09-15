import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pruebabd.settings")
django.setup()

from django.conf import settings
from django.db import IntegrityError
from prueba.models import (
    VistaProductoVariantes,
    ImagenProducto,
    ProductoGrupoManual
)


def ejecutar_vinculacion():
    print("=" * 65)
    print("🚀 INICIANDO VINCULACIÓN INTELIGENTE DE IMÁGENES")
    print("=" * 65)

    # 1. Definir directorio físico de fotos
    ruta_media_productos = os.path.join(settings.BASE_DIR, "media", "productos")
    if not os.path.exists(ruta_media_productos):
        os.makedirs(ruta_media_productos, exist_ok=True)
        print(f"📁 Carpeta creada: {ruta_media_productos}")

    # 2. Obtener mapa de archivos físicos disponibles (código limpio -> nombre_archivo)
    extensiones_validas = {".png", ".jpg", ".jpeg", ".webp"}
    archivos_en_disco = {}
    for archivo in os.listdir(ruta_media_productos):
        nombre, ext = os.path.splitext(archivo)
        if ext.lower() in extensiones_validas:
            archivos_en_disco[nombre.strip()] = archivo

    print(f"📸 Fotos físicas detectadas en media/productos/: {len(archivos_en_disco)}")

    # 3. Cargar asignaciones manuales y mapear grupos con sus variantes
    overrides_dict = {
        item.producto_id: item.grupo_personalizado
        for item in ProductoGrupoManual.objects.all()
    }

    productos_qs = VistaProductoVariantes.objects.all().order_by("codigo")

    # grupos_map = { 'GRUPO_NORMALIZADO': {'nombre_original': str, 'codigos': [str, ...]} }
    grupos_map = {}
    for p in productos_qs:
        nombre_grupo_real = overrides_dict.get(p.id, p.descripcion_grupo or p.descripcion)
        if not nombre_grupo_real:
            continue

        grupo_key = str(nombre_grupo_real).strip().upper()
        if grupo_key not in grupos_map:
            grupos_map[grupo_key] = {
                "nombre_original": str(nombre_grupo_real).strip(),
                "codigos": []
            }
        if p.codigo and str(p.codigo).strip():
            grupos_map[grupo_key]["codigos"].append(str(p.codigo).strip())

    print(f"📦 Grupos únicos detectados en catálogo: {len(grupos_map)}")

    # 4. Cargar estado actual de ImagenProducto
    imagenes_existentes = {
        str(img.grupo_nombre).strip().upper(): img
        for img in ImagenProducto.objects.all()
    }

    def es_placeholder_propio(ruta_actual, codigos_del_grupo):
        """
        True si la ruta actual coincide con el patrón que este mismo script
        genera automáticamente ('productos/{codigo}.png' para algún código
        del grupo) -> es seguro sobreescribirla.
        False si el nombre de archivo no corresponde a ningún código del
        grupo -> fue asignada a mano, no se toca.
        """
        if not ruta_actual:
            return True  # sin imagen asignada, no hay nada que proteger
        nombre_archivo = os.path.splitext(os.path.basename(str(ruta_actual)))[0].strip()
        return nombre_archivo in codigos_del_grupo

    fotos_fisicas_vinculadas = 0
    fotos_actualizadas_desde_placeholder = 0
    rutas_previas_asignadas = 0
    grupos_ya_listos = 0
    grupos_respetados_manual = []

    for grupo_key, data in grupos_map.items():
        nombre_original = data["nombre_original"]
        codigos = data["codigos"]
        codigo_representante = codigos[0] if codigos else "sin_codigo"

        # Verificar si alguna variante del grupo tiene foto física en disco
        archivo_encontrado = None
        for cod in codigos:
            if cod in archivos_en_disco:
                archivo_encontrado = archivos_en_disco[cod]
                break

        img_obj = imagenes_existentes.get(grupo_key)

        if archivo_encontrado:
            ruta_asignar = f"productos/{archivo_encontrado}"
            if img_obj:
                if str(img_obj.imagen) == ruta_asignar:
                    grupos_ya_listos += 1
                elif es_placeholder_propio(img_obj.imagen, codigos):
                    # Era un placeholder nuestro o estaba vacío -> seguro sobreescribir
                    era_placeholder_vacio = not str(img_obj.imagen)
                    img_obj.imagen = ruta_asignar
                    img_obj.save()
                    if era_placeholder_vacio:
                        fotos_fisicas_vinculadas += 1
                    else:
                        fotos_actualizadas_desde_placeholder += 1
                else:
                    # Imagen asignada a mano, con nombre que no sigue el patrón -> NO TOCAR
                    grupos_respetados_manual.append((nombre_original, str(img_obj.imagen)))
            else:
                try:
                    ImagenProducto.objects.create(
                        grupo_nombre=nombre_original,
                        imagen=ruta_asignar
                    )
                    fotos_fisicas_vinculadas += 1
                except IntegrityError:
                    pass
        else:
            # Si no existe archivo físico en disco, pre-asignar ruta estándar
            # (queda "a la espera" de que se suba la foto con ese nombre exacto)
            ruta_estandar = f"productos/{codigo_representante}.png"
            if img_obj:
                if not img_obj.imagen or str(img_obj.imagen).strip() == "":
                    img_obj.imagen = ruta_estandar
                    img_obj.save()
                    rutas_previas_asignadas += 1
                else:
                    grupos_ya_listos += 1
            else:
                try:
                    ImagenProducto.objects.create(
                        grupo_nombre=nombre_original,
                        imagen=ruta_estandar
                    )
                    rutas_previas_asignadas += 1
                except IntegrityError:
                    pass

    print("-" * 65)
    print(f"✅ Grupos vinculados a foto existente (nuevos): {fotos_fisicas_vinculadas}")
    print(f"🔄 Grupos actualizados (placeholder -> foto real): {fotos_actualizadas_desde_placeholder}")
    print(f"⏳ Grupos pre-vinculados (esperando foto): {rutas_previas_asignadas}")
    print(f"🔒 Grupos que ya estaban configurados: {grupos_ya_listos}")
    print(f"🛡️  Grupos con imagen manual respetada (sin tocar): {len(grupos_respetados_manual)}")

    if grupos_respetados_manual:
        print("-" * 65)
        print("Detalle de grupos con imagen manual que NO se modificaron:")
        for nombre, ruta in grupos_respetados_manual:
            print(f"   - {nombre}: {ruta}")

    print("=" * 65)
    print("🎉 Proceso finalizado. Todo el catálogo quedó enlazado.")


if __name__ == "__main__":
    ejecutar_vinculacion()
