from collections import OrderedDict, Counter
from datetime import datetime, timedelta
from types import SimpleNamespace
import time
import os
import base64
import mimetypes
import tempfile
import re
import logging
import difflib

from django.http import HttpResponse, JsonResponse, FileResponse
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import permission_required, login_required, user_passes_test
from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404, redirect
from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from django.db.models import Case, When, Value, IntegerField, Q
from django.core.exceptions import PermissionDenied
from django.conf import settings
from django.templatetags.static import static
from django.contrib import messages
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.utils.text import slugify
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright
import asyncio
import fitz  # PyMuPDF
logger = logging.getLogger(__name__)
from .models import FamiliaProducto, Producto, ImagenProducto, Proveedor, VistaProductoAgrupado, CatalogCache, VistaProductoVariantes, ProductoGrupoManual, SyncLog, TruperFichaTecnica
from .sync_erp_service import ejecutar_sync

# ==========================================
# FUNCIONES AUXILIARES
# ==========================================

def obtener_base64_imagen(ruta_imagen):
    if not ruta_imagen:
        return None

    ruta_limpia = str(ruta_imagen)
    if ruta_limpia.startswith('/'):
        ruta_limpia = ruta_limpia[1:]

    rutas_posibles = [
        os.path.join(settings.BASE_DIR, 'digitalizacionCatalogo', ruta_limpia),
        os.path.join(settings.BASE_DIR, ruta_limpia),
        os.path.join(settings.MEDIA_ROOT, ruta_limpia.replace('media/', '')),
        os.path.join(settings.BASE_DIR, 'static', ruta_limpia.replace('static/', '')),
    ]

    for ruta_fisica in rutas_posibles:
        if os.path.exists(ruta_fisica):
            try:
                with open(ruta_fisica, "rb") as image_file:
                    encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                    tipo_mime, _ = mimetypes.guess_type(ruta_fisica)
                    if not tipo_mime:
                        tipo_mime = 'image/png'
                    return f"data:{tipo_mime};base64,{encoded_string}"
            except Exception:
                continue

    return ruta_imagen

def extraer_medida(nombre_grupo: str, descripcion_variante: str, codigo_de_origen: str = "") -> str:
    if not nombre_grupo or not descripcion_variante:
        return "--"

    # NOTA (Claude): cuando un producto NO pertenece a ningún grupo real
    # (es un "grupo de 1"), el nombre de grupo que llega a esta función es
    # exactamente igual a su propia descripción -- ver
    # _construir_grupos_efectivos_dashboard() y generar_pdf():
    # grupo_final = override_item.grupo_personalizado if override_item else
    # (p.descripcion_grupo or p.descripcion). Si el producto no tiene
    # descripcion_grupo (no quedó agrupado con nadie más por el SQL) ni
    # override manual, "grupo_final" termina siendo p.descripcion misma.
    # Antes, en ese caso, esta función intentaba "restar" el nombre del
    # grupo a la descripción de la variante -- pero como son el mismo
    # texto, la resta daba vacío y el producto quedaba sin medida ("--").
    #
    # Para este caso solo se confía en la FASE 1 (el patrón de medida
    # explícito: números + unidad, ej. "16OZ", "1/4\""), que es preciso.
    # La FASE 2 (heurístico de "restar palabras del grupo y quedarse con
    # lo que sobra") depende de tener un nombre de grupo genérico real
    # contra el cual comparar -- sin eso, adivina y a veces se equivoca
    # (Rodrigo prefiere cargar la medida a mano en Gestionar Agrupaciones
    # para esos casos en vez de mostrar un valor incierto). Por eso, si la
    # FASE 1 no encuentra nada para un producto sin grupo real, se corta
    # ahí y se devuelve "--" en vez de seguir a la FASE 2.
    es_producto_sin_grupo_real = (
        str(nombre_grupo).strip().upper() == str(descripcion_variante).strip().upper()
    )

    nombre_grupo_str = str(nombre_grupo).strip().upper()
    descripcion_limpia = str(descripcion_variante).strip().upper()

    # 1. DESTRUCTOR DE PUNTOS SUSPENSIVOS
    descripcion_limpia = descripcion_limpia.replace('…', ' ').replace('...', ' ')
    nombre_grupo_str = nombre_grupo_str.replace('…', ' ').replace('...', ' ')

    # 2. SEPARADOR INTELIGENTE: Despega números y comillas de las letras
    descripcion_limpia = re.sub(r'([A-Z\.])(\d)', r'\1 \2', descripcion_limpia)
    descripcion_limpia = re.sub(r'(\d|")([A-Z])', r'\1 \2', descripcion_limpia)

    if codigo_de_origen:
        codigo_de_origen = str(codigo_de_origen).strip().upper()
        if codigo_de_origen and descripcion_limpia.startswith(codigo_de_origen):
            descripcion_limpia = descripcion_limpia[len(codigo_de_origen):].strip()

    descripcion_limpia = re.sub(r'^\d{3,7}\s+', '', descripcion_limpia)
    descripcion_limpia = re.sub(r'\s*\([^)]*\)', '', descripcion_limpia)

    # ---------------------------------------------------------
    # FASE 1: FRANCOTIRADOR DE MEDIDAS (Regex Prioritario)
    # ---------------------------------------------------------
    patron_medidas = r'(?<!\d)\d+(?:/\d+)?\s*(?:"|MM|CM|M|OZ|KG|GR|PULG|LB|LT|L|ML|GAL|W|V|A|HP|DTES\.?|DIENTES)(?!\w)'

    medidas_grupo = set() if es_producto_sin_grupo_real else set(re.findall(patron_medidas, nombre_grupo_str))

    medidas_variante = []
    for match in re.finditer(patron_medidas, descripcion_limpia):
        m = match.group().strip()
        if m not in medidas_grupo and m not in medidas_variante:
            medidas_variante.append(m)

    if medidas_variante:
        return " ".join(medidas_variante)

    if es_producto_sin_grupo_real:
        return "--"

    # ---------------------------------------------------------
    # FASE 2: DICCIONARIO Y RESTA (Si NO es un producto de medida numérica)
    # ---------------------------------------------------------
    marcas_pegadas = ['TRUPER', 'TRUPE', 'PRETUL', 'PRETU', 'FOSET', 'VOLTECK', 'FIERO', 'HERMEX']
    for marca in marcas_pegadas:
        descripcion_limpia = descripcion_limpia.replace(marca, ' ')

    basura_erp = [
        r'\bDE\b', r'\bPARA\b', r'\bTIPO\b', r'\bCON\b', r'\bSIN\b',
        r'\bC/MANGO\b', r'\bS/MANGO\b', r'\bMGO\.?', r'\bDENTAD\w*',
        r'\bP\.PAJA\b', r'\bBLISTER\b', r'\bCAJA\b', r'\bGRANEL\b',
        r'\bPAR\b', r'\bJUEGO\b', r'\bSET\b',
        r'\bPROFE\w*\b', r'\bELECTR\w*\b', r'\bESTAND\w*\b',
        r'\bMANGO\b', r'\bNARANJA\b', r'\bROJO\b', r'\bNEGRO\b'
    ]

    texto_filtrado = descripcion_limpia
    for palabra in basura_erp:
        texto_filtrado = re.sub(palabra, ' ', texto_filtrado)
    texto_filtrado = re.sub(r'\s+', ' ', texto_filtrado).strip()

    palabras_grupo = set(nombre_grupo_str.split())
    palabras_variante = texto_filtrado.split()

    diferencias = []
    for p_var in palabras_variante:
        p_var_limpia = p_var.strip('.')
        if not p_var_limpia:
            continue

        if p_var_limpia in palabras_grupo or p_var in palabras_grupo:
            continue

        es_similar = False
        p_var_solo_letras = re.sub(r'[^A-Z]', '', p_var_limpia)
        if len(p_var_solo_letras) > 3:
            for p_grupo in palabras_grupo:
                p_grupo_solo_letras = re.sub(r'[^A-Z]', '', p_grupo)
                if p_grupo_solo_letras and difflib.SequenceMatcher(None, p_grupo_solo_letras, p_var_solo_letras).ratio() > 0.85:
                    es_similar = True
                    break
        if es_similar:
            continue

        diferencias.append(p_var)

    resultado = " ".join(diferencias).strip(" .,-")
    resultado = re.sub(r'^[-,\s/]+', '', resultado)

    if not resultado:
        primer_palabra_grupo = nombre_grupo_str.split()[0] if nombre_grupo_str.split() else ""

        fallback_texto = texto_filtrado
        if primer_palabra_grupo and primer_palabra_grupo in fallback_texto:
            fallback_texto = re.sub(r'\b' + re.escape(primer_palabra_grupo) + r'\b', '', fallback_texto, count=1).strip()

        fallback_texto = re.sub(r'^[-,\s/]+', '', fallback_texto).strip()

        if fallback_texto:
            resultado = fallback_texto
        else:
            resultado = "--"

    return resultado

def obtener_file_uri(imagen_field):
    if not imagen_field:
        return None
    try:
        ruta_fisica = imagen_field.path
    except Exception:
        return None

    if ruta_fisica and os.path.exists(ruta_fisica):
        return 'file://' + os.path.abspath(ruta_fisica).replace('\\', '/')

    return None

def obtener_logo_marca(marca: str) -> str | None:
    if not marca:
        return None

    slug = slugify(marca)
    ruta_relativa = f"img/marcas/{slug}.png"

    posibles_bases = list(getattr(settings, "STATICFILES_DIRS", [])) + [
        getattr(settings, "STATIC_ROOT", "")
    ]

    for base in posibles_bases:
        if base and os.path.exists(os.path.join(base, ruta_relativa)):
            return static(ruta_relativa)

    return None

def es_admin(user):
    if user.is_superuser:
        return True
    raise PermissionDenied

def limpiar_pdfs_huerfanos(sin_precio):
    carpeta = os.path.join(settings.MEDIA_ROOT, 'catalogos')
    if not os.path.isdir(carpeta):
        return

    if sin_precio:
        nombres_validos = {
            os.path.basename(c.pdf_file.name)
            for c in CatalogCache.objects.filter(pdf_file__icontains='Sin_Precio')
        }
        es_del_tipo = lambda f: 'Sin_Precio' in f
    else:
        nombres_validos = {
            os.path.basename(c.pdf_file.name)
            for c in CatalogCache.objects.exclude(pdf_file__icontains='Sin_Precio')
        }
        es_del_tipo = lambda f: f.startswith('Catalogo_Ecosa_') and 'Sin_Precio' not in f

    for nombre_archivo in os.listdir(carpeta):
        if not nombre_archivo.lower().endswith('.pdf'):
            continue
        if not es_del_tipo(nombre_archivo):
            continue
        if nombre_archivo not in nombres_validos:
            ruta_completa = os.path.join(carpeta, nombre_archivo)
            try:
                os.remove(ruta_completa)
                logger.info(f"[limpieza catalogos] Huérfano eliminado: {nombre_archivo}")
            except OSError as e:
                logger.warning(f"[limpieza catalogos] No se pudo eliminar {nombre_archivo}: {e}")


def _eliminar_catalogo_mas_antiguo_si_corresponde(catalogos_existentes):
    """
    NOTA (Claude): antes esto vivía duplicado e inline en generar_pdf() y en
    _ejecutar_generacion_pdf_en_hilo(), y elegía SIEMPRE el catálogo más
    antiguo del tipo (con/sin precio) vía catalogos_existentes.first(), sin
    mirar si ese catálogo estaba marcado como vigente (is_current=True) en
    "Marcar como vigente" del historial. Eso significa que, si alguien fijaba
    como oficial justo la versión más antigua guardada, la siguiente
    generación de un catálogo del mismo tipo la borraba igual -- perdiendo el
    archivo que estaba vigente.

    Ahora: se sigue usando el mismo umbral (3 o más versiones guardadas de
    ese tipo) para decidir SI hay que liberar espacio, pero al elegir CUÁL
    borrar se excluyen los catálogos vigentes (.exclude(is_current=True)) y
    se toma el más antiguo entre los que quedan. Si no queda ninguno
    borrable (por ejemplo, las 3 versiones guardadas están todas vigentes,
    lo cual no debería pasar en la práctica ya que solo puede haber un
    vigente por tipo, pero se cubre por seguridad), no se borra nada esta
    vez.

    `catalogos_existentes` debe venir ya filtrado por tipo (con/sin precio)
    y ordenado por version_number ascendente, igual que antes.

    Devuelve el objeto CatalogCache eliminado, o None si no se eliminó
    ninguno.
    """
    if catalogos_existentes.count() < 3:
        return None

    catalogo_mas_antiguo = catalogos_existentes.exclude(is_current=True).first()
    if not catalogo_mas_antiguo:
        logger.info(
            "[limpieza catalogos] Se alcanzó el umbral de 3 versiones, pero todas las "
            "candidatas a borrado están marcadas como vigentes -- no se borra ninguna."
        )
        return None

    if catalogo_mas_antiguo.pdf_file:
        ruta_fisica = catalogo_mas_antiguo.pdf_file.path
        catalogo_mas_antiguo.pdf_file.delete(save=False)
        if os.path.exists(ruta_fisica):
            logger.warning(f"[limpieza catalogos] El archivo {ruta_fisica} no se eliminó del disco (delete silencioso).")
    catalogo_mas_antiguo.delete()

    return catalogo_mas_antiguo

def _agrupar_por_super_grupo(grupos_items):
    """
    NOTA (Claude): recibe una lista de tuplas (nombre_grupo, info) ya con
    'posicion_fija', 'variantes', 'prefijo_nombre', 'min_codigo_prod' y
    'min_codigo_str' calculados (se usan exactamente igual que antes para
    ordenar), y le agrega el "Súper Grupo": una agrupación opcional,
    asignada en Gestionar Agrupaciones y guardada en
    ImagenProducto.super_grupo (NO en ProductoGrupoManual, para no tocar
    las agrupaciones manuales existentes), que sirve para juntar varios
    grupos relacionados dentro de una misma familia (ej. que todos los
    "martillos" queden juntos aunque tengan distinta cantidad de
    variantes).

    Se arman "clusters": todos los grupos con el mismo super_grupo quedan
    en un mismo cluster, y cada grupo sin super_grupo asignado forma su
    propio cluster de 1 (para no cambiar su comportamiento actual). Cada
    cluster se posiciona usando el menor "orden individual" de sus
    miembros, y dentro del cluster los miembros se ordenan entre sí con
    ese mismo criterio -- así se conserva la lógica estética de orden por
    cantidad de variantes que ya estaba afinada, sólo que ahora los
    grupos relacionados quedan adyacentes.
    """
    def clave_individual(info):
        return (
            info['posicion_fija'],
            len(info['variantes']),
            info['prefijo_nombre'],
            info['min_codigo_prod'],
            info['min_codigo_str'],
        )

    clusters = OrderedDict()
    for nombre_g, info in grupos_items:
        super_grupo = info.get('super_grupo')
        clave_cluster = super_grupo if super_grupo else f"__sin_super_grupo__{nombre_g}"
        clusters.setdefault(clave_cluster, []).append((nombre_g, info))

    lista_clusters = []
    for miembros in clusters.values():
        miembros_ordenados = sorted(miembros, key=lambda item: clave_individual(item[1]))
        clave_min = min(clave_individual(info) for _, info in miembros_ordenados)
        lista_clusters.append((clave_min, miembros_ordenados))

    lista_clusters.sort(key=lambda c: c[0])

    resultado = []
    for _, miembros_ordenados in lista_clusters:
        resultado.extend(miembros_ordenados)

    return resultado


def _construir_grupos_efectivos_dashboard():
    """
    NOTA (Claude): esto reemplaza por completo el enfoque anterior de
    dashboard_productos (que partía de VistaProductoAgrupado, una fila
    por cluster automático de SQL keyeada por el _id mínimo del cluster,
    ciega a ProductoGrupoManual). Ese enfoque tenía un límite real que un
    primer parche (basado en "override más frecuente dentro del cluster")
    no podía resolver: cuando un cluster automático se DIVIDE a propósito
    en Gestionar Agrupaciones en dos o más grupos manuales distintos (ej.
    43-02-102 -> "3104 - HOJA SIERRA CALADORA..." y 43-02-104 -> "3103 -
    HOJA SIERRA CALADORA...", que el SQL agrupaba juntos por descripción
    automática pero son productos con medidas distintas), una sola fila
    de VistaProductoAgrupado no puede representar dos grupos separados.

    Por eso ahora se arma igual que generar_pdf/gestionar_grupos: se
    recorre VistaProductoVariantes (una fila por SKU, la misma fuente que
    ya usa Gestionar Agrupaciones), se resuelve el grupo EFECTIVO de cada
    variante (su override individual en ProductoGrupoManual si existe, si
    no el nombre automático descripcion_grupo/descripcion), y se agrupan
    en Python por ese nombre efectivo. Un cluster dividido en dos grupos
    manuales aparece ahora como dos filas, exactamente como en Gestionar
    Agrupaciones -- no una sola fila con un nombre "ganador" arbitrario.

    Cada grupo efectivo devuelve un "representante" (la variante de menor
    código dentro del grupo) para los campos que se muestran en una sola
    fila (código, código de origen, familia, marca). El id de ese
    representante es el _id real de una variante concreta (existe en la
    tabla `producto`), así que el botón "Modificar" del dashboard --que
    llama a editar_producto y actualiza Producto.objects.filter(field_id=...)
    y el ImagenProducto por nombre de grupo-- sigue funcionando igual que
    antes; dashboard.html no depende de que ese id exista en
    VistaProductoAgrupado.

    Costo: a diferencia del enfoque anterior (paginado a nivel SQL sobre
    VistaProductoAgrupado), esto carga TODAS las variantes filtradas en
    memoria en cada request para poder agruparlas correctamente -- mismo
    patrón que ya usa lista_productos (el catálogo público) hoy. Si con
    el volumen real de productos esto se siente lento, se puede optimizar
    después (ej. cacheando el resultado agrupado unos minutos), pero
    prioricé que el dashboard muestre exactamente lo mismo que Gestionar
    Agrupaciones.
    """
    overrides_dict = {
        item.producto_id: item.grupo_personalizado
        for item in ProductoGrupoManual.objects.all()
    }

    variantes_qs = VistaProductoVariantes.objects.select_related("proveedor").exclude(
        Q(descripcion__isnull=True) |
        Q(descripcion__exact='') |
        Q(descripcion__startswith='(') |
        Q(descripcion__istartswith='tee') |
        Q(descripcion__regex=r'^.$') |
        Q(proveedor__marca__startswith='*') |
        Q(proveedor__marca__startswith='"') |
        Q(proveedor__marca__iexact='a') |
        Q(proveedor__marca__iexact='KAISER - HEISSNER') |
        Q(proveedor__marca__iexact='HELA') |
        Q(codigo='17-27-105') |
        Q(descripcion__iexact='ANULA FACTURA') |
        Q(descripcion__iexact='BOLSA')
    )

    grupos = OrderedDict()
    for v in variantes_qs:
        grupo_nombre = overrides_dict.get(v.id) or (v.descripcion_grupo or v.descripcion)
        grupos.setdefault(grupo_nombre, []).append(v)

    lista_grupos = []
    for grupo_nombre, variantes in grupos.items():
        representante = min(variantes, key=lambda v: (v.codigo or ""))

        marcas = [v.proveedor.marca for v in variantes if getattr(v, 'proveedor', None) and v.proveedor.marca]
        marca_predominante = (
            Counter(marcas).most_common(1)[0][0] if marcas
            else (representante.proveedor.marca if representante.proveedor else "")
        )

        lista_grupos.append({
            "id": representante.id,
            "codigo": representante.codigo,
            "codigo_de_origen": representante.codigo_de_origen,
            "grupo_nombre": grupo_nombre,
            "descripcion_grupo_original": representante.descripcion_grupo or representante.descripcion,
            "familia_nombre": representante.familia_nombre,
            "proveedor_marca": marca_predominante,
            "unidad_medida": representante.unidad_medida,
            "cantidad": len(variantes),
            "codigos_variantes": [v.codigo for v in variantes if v.codigo],
            "codigos_origen_variantes": [v.codigo_de_origen for v in variantes if v.codigo_de_origen],
        })

    return lista_grupos

# ==========================================
# VISTA: LISTA DE PRODUCTOS (CATÁLOGO PÚBLICO)
# ==========================================

# Opciones de cantidad de productos por página que el usuario puede elegir
# en el catálogo público (selector en productos.html). Si en la URL viene
# un "por_pagina" que no está en esta lista (o algo no numérico, ej.
# alguien editando el querystring a mano), se usa el valor por defecto en
# vez de fallar o de permitir un número arbitrario.
OPCIONES_POR_PAGINA_CATALOGO = [12, 24, 48, 96]
POR_PAGINA_DEFAULT_CATALOGO = 12


@never_cache
def lista_productos(request):
    familia_seleccionada = request.GET.get("familia", "").strip()
    marca_seleccionada = request.GET.get("marca", "").strip()
    texto_busqueda = request.GET.get("q", "").strip()

    try:
        por_pagina = int(request.GET.get("por_pagina", POR_PAGINA_DEFAULT_CATALOGO))
    except (TypeError, ValueError):
        por_pagina = POR_PAGINA_DEFAULT_CATALOGO

    if por_pagina not in OPCIONES_POR_PAGINA_CATALOGO:
        por_pagina = POR_PAGINA_DEFAULT_CATALOGO

    familias = {f.codigo: f for f in FamiliaProducto.objects.all()}

    productos = (
        VistaProductoAgrupado.objects
        .annotate(
            es_truper=Case(
                When(codigo__startswith='17', then=Value(0)),
                default=Value(1),
                output_field=IntegerField()
            )
        )
        .select_related("proveedor")
        .exclude(
            Q(descripcion__isnull=True) |
            Q(descripcion__exact='') |
            Q(descripcion__startswith='(') |
            Q(descripcion__istartswith='tee') |
            Q(descripcion__regex=r'^.$') |
            Q(proveedor__marca__startswith='*') |
            Q(proveedor__marca__startswith='"') |
            Q(proveedor__marca__iexact='a') |
            Q(proveedor__marca__iexact='KAISER - HEISSNER') |
            Q(proveedor__marca__iexact='HELA') |
            Q(codigo='17-27-105') |
            Q(descripcion__iexact='ANULA FACTURA') |
            Q(descripcion__iexact='BOLSA')
        )
    )

    if marca_seleccionada:
        productos = productos.filter(proveedor__marca__iexact=marca_seleccionada)

    if familia_seleccionada:
        productos = productos.filter(codigo__icontains=f"-{familia_seleccionada}")

    if texto_busqueda:
        terminos = texto_busqueda.split()
        for termino in terminos:
            grupos_por_variante = list(
                VistaProductoVariantes.objects.filter(
                    Q(codigo__icontains=termino) |
                    Q(codigo_de_origen__icontains=termino)
                ).values_list('descripcion_grupo', flat=True).distinct()
            )

            # NOTA (Claude): igual que en dashboard_productos -- el buscador
            # no encontraba productos por el nombre de grupo asignado a mano
            # en "Gestionar Agrupaciones" (ProductoGrupoManual.grupo_personalizado),
            # porque ese nombre puede no tener relacion textual con la
            # descripcion original del ERP (ej. "DISCO SIERRA PARA MADERA"
            # vs "SIERRA CIRC.7-1/4" ... TRUPER"). Usamos el mismo producto_id
            # que despues usa overrides_dict.get(p.id, ...) para que quede
            # consistente con como se resuelve el nombre mostrado.
            ids_por_grupo_manual = list(
                ProductoGrupoManual.objects.filter(
                    grupo_personalizado__icontains=termino
                ).values_list('producto_id', flat=True)
            )

            productos = productos.filter(
                Q(descripcion__icontains=termino) |
                Q(descripcion_grupo__icontains=termino) |
                Q(codigo__icontains=termino) |
                Q(codigo_de_origen__icontains=termino) |
                Q(familia_nombre__icontains=termino) |
                Q(unidad_medida__icontains=termino) |
                Q(proveedor__marca__icontains=termino) |
                Q(descripcion_grupo__in=grupos_por_variante) |
                Q(id__in=ids_por_grupo_manual)
            )

    productos = productos.order_by("es_truper", "codigo")

    overrides_dict = {
        item.producto_id: item.grupo_personalizado
        for item in ProductoGrupoManual.objects.all()
    }

    grupos = OrderedDict()

    for p in productos:
        grupo = overrides_dict.get(p.id, p.descripcion_grupo or p.descripcion)
        familia = None

        if p.codigo:
            partes = p.codigo.split("-")
            if len(partes) >= 2:
                familia = familias.get(partes[1])
                if familia_seleccionada and familia and familia.codigo != familia_seleccionada:
                    continue

        marca = p.proveedor.marca if p.proveedor else ""

        if grupo not in grupos:
            grupos[grupo] = {
                "id_referencia": p.id,
                "nombre": grupo,
                "marca": marca,
                "familia": familia,
                "precio_desde": None,
                "unidad_medida": p.unidad_medida,
                "productos": [],
            }

        grupos[grupo]["productos"].append(p)

    lista_grupos = list(grupos.values())

    for g in lista_grupos:
        prod_base = g["productos"][0]
        g["precio_desde"] = prod_base.precio_desde

    imagenes_dict = {
        str(img.grupo_nombre).strip().upper(): img.imagen.url
        for img in ImagenProducto.objects.all() if img.imagen
    }

    destacados_dict = {
        str(img.grupo_nombre).strip().upper(): {
            'es_destacado': img.es_destacado,
            'etiqueta': img.etiqueta_destacado or "DESTACADO"
        }
        for img in ImagenProducto.objects.all()
    }

    for g in lista_grupos:
        nombre_limpio = str(g["nombre"]).strip().upper()
        g["imagen_url"] = imagenes_dict.get(nombre_limpio, None)

        info_dest = destacados_dict.get(nombre_limpio, {'es_destacado': False, 'etiqueta': ''})
        g["es_destacado"] = info_dest['es_destacado']
        g["etiqueta_destacado"] = info_dest['etiqueta']

        prod_base = g["productos"][0]
        cant_var = getattr(prod_base, 'cantidad_variantes', None)
        g["cantidad"] = cant_var if (cant_var is not None and cant_var > 0) else len(g["productos"])

    conteo_familias = {}
    conteo_marcas = {}
    for g in lista_grupos:
        if g["familia"]:
            codigo = g["familia"].codigo
            conteo_familias[codigo] = conteo_familias.get(codigo, 0) + 1
        if g["marca"]:
            conteo_marcas[g["marca"]] = conteo_marcas.get(g["marca"], 0) + 1

    familias_sidebar = []
    for codigo, familia in familias.items():
        if codigo in conteo_familias:
            familia.total = conteo_familias[codigo]
            familias_sidebar.append(familia)
    familias_sidebar.sort(key=lambda x: x.descripcion)

    marcas_sidebar = [{"nombre": nombre, "total": total} for nombre, total in conteo_marcas.items()]
    marcas_sidebar.sort(key=lambda x: x["nombre"])

    paginator = Paginator(lista_grupos, por_pagina)
    page = request.GET.get("page")
    page_obj = paginator.get_page(page)

    return render(
        request,
        "productos.html",
        {
            "grupos": page_obj,
            "page_obj": page_obj,
            "familias": familias_sidebar,
            "marcas": marcas_sidebar,
            "familia_actual": familia_seleccionada,
            "marca_actual": marca_seleccionada,
            "busqueda": texto_busqueda,
            "por_pagina_actual": por_pagina,
            "opciones_por_pagina": OPCIONES_POR_PAGINA_CATALOGO,
        },
    )

@never_cache
@login_required(login_url='/login/')
def detalle_producto(request, producto_id):
    producto_base = get_object_or_404(VistaProductoAgrupado.objects.select_related("proveedor"), id=producto_id)

    override_obj = ProductoGrupoManual.objects.filter(producto_id=producto_base.id).first()
    nombre_grupo = override_obj.grupo_personalizado if override_obj else (producto_base.descripcion_grupo or producto_base.descripcion)

    marca_grupo = producto_base.proveedor.marca if producto_base.proveedor else ""

    info_grupo = ImagenProducto.objects.filter(grupo_nombre__iexact=nombre_grupo.strip()).first() if nombre_grupo else None
    imagen_url = None
    descripcion_grupo = ""

    if info_grupo:
        descripcion_grupo = info_grupo.descripcion or ""
        if info_grupo.imagen:
            try:
                imagen_url = info_grupo.imagen.url
            except ValueError:
                imagen_url = info_grupo.imagen

    variantes_qs = VistaProductoVariantes.objects.select_related("proveedor").exclude(
        Q(descripcion__isnull=True) |
        Q(descripcion__exact='') |
        Q(descripcion__startswith='(') |
        Q(descripcion__istartswith='tee') |
        Q(descripcion__regex=r'^.$') |
        Q(proveedor__marca__startswith='"') |
        Q(proveedor__marca__iexact='a') |
        Q(proveedor__marca__iexact='KAISER - HEISSNER') |
        Q(proveedor__marca__iexact='HELA')
    )

    ids_en_grupo = list(ProductoGrupoManual.objects.filter(grupo_personalizado=nombre_grupo).values_list('producto_id', flat=True))

    filtros_grupo = (
        Q(id__in=ids_en_grupo) |
        Q(descripcion_grupo=nombre_grupo) |
        Q(descripcion=nombre_grupo, descripcion_grupo__isnull=True) |
        Q(descripcion=nombre_grupo, descripcion_grupo="")
    )

    if marca_grupo:
        variantes = list(variantes_qs.filter(filtros_grupo, proveedor__marca__iexact=marca_grupo).order_by('codigo'))
    else:
        variantes = list(variantes_qs.filter(filtros_grupo).order_by('codigo'))

    # NOTA (Claude): antes esta vista pasaba las variantes "en crudo" (solo
    # con la descripcion original del ERP, ej. "18298 SIERRA CIRC.7-1/4" 16
    # DIENTES TRUPER"). Ahora calculamos el mismo "nombre_limpio" que ya se
    # usa en gestionar_grupos.html y en el PDF: si el producto tiene un
    # nombre_limpio_personalizado guardado a mano en ProductoGrupoManual lo
    # usamos tal cual; si no, lo calculamos automaticamente con
    # extraer_medida() a partir del nombre del grupo.
    ids_variantes = [v.id for v in variantes]
    overrides_variantes = {
        item.producto_id: item
        for item in ProductoGrupoManual.objects.filter(producto_id__in=ids_variantes)
    }

    for v in variantes:
        override_v = overrides_variantes.get(v.id)
        if override_v and override_v.nombre_limpio_personalizado:
            v.nombre_limpio = override_v.nombre_limpio_personalizado
        else:
            v.nombre_limpio = extraer_medida(nombre_grupo, v.descripcion or "", v.codigo_de_origen or "")

    # NOTA (Claude): mismo cruce que ya usa generar_pdf() para la columna
    # "Ref" del PDF -- fichas técnicas de Truper cacheadas por
    # codigo_de_origen (ver comando "sync_fichas_truper" y modelo
    # TruperFichaTecnica). Rodrigo pidió mostrar este link también en el
    # catálogo público (detalle.html), no solo en el PDF. No se consulta a
    # truper.com en vivo, se usa la cache ya sincronizada -- igual que en
    # el PDF.
    fichas_truper_dict = {
        item.codigo_origen: item.url_ficha_tecnica
        for item in TruperFichaTecnica.objects.all()
    }
    for v in variantes:
        v.url_ficha_tecnica = fichas_truper_dict.get(v.codigo_de_origen) if v.codigo_de_origen else None

    return render(
        request,
        "detalle.html",
        {
            "nombre_grupo": nombre_grupo,
            "marca": marca_grupo,
            "producto_base": producto_base,
            "variantes": variantes,
            "imagen_url": imagen_url,
            "descripcion_grupo": descripcion_grupo
        },
    )

# ==========================================
# VISTA: PANEL DASHBOARD PRINCIPAL
# ==========================================
@never_cache
@login_required(login_url='/login/')
def dashboard_productos(request):
    texto_busqueda = request.GET.get("q", "").strip()

    # NOTA (Claude): lista_grupos ya viene armada por GRUPO EFECTIVO (ver
    # _construir_grupos_efectivos_dashboard), no por cluster automático de
    # SQL -- así coincide siempre con lo que muestra Gestionar Agrupaciones,
    # incluso cuando un cluster automático fue dividido a mano en varios
    # grupos manuales.
    lista_grupos = _construir_grupos_efectivos_dashboard()

    # Los KPIs se calculan sobre el total SIN filtrar por busqueda, igual
    # que antes (productos_base_qs no se filtraba por texto_busqueda).
    kpi_productos_activos = len(lista_grupos)

    # NOTA (Claude): conteo "en bruto" pedido por Rodrigo -- cada SKU/
    # variante individual cuenta por separado, SIN agrupar por nombre de
    # grupo efectivo (a diferencia de kpi_productos_activos, que cuenta
    # grupos). Mismos filtros de exclusión que usa
    # _construir_grupos_efectivos_dashboard, aplicados directo sobre
    # VistaProductoVariantes con .count() -- no toca lista_grupos ni la
    # tabla/paginación del dashboard, es solo un dato informativo extra.
    kpi_productos_activos_bruto = VistaProductoVariantes.objects.exclude(
        Q(descripcion__isnull=True) |
        Q(descripcion__exact='') |
        Q(descripcion__startswith='(') |
        Q(descripcion__istartswith='tee') |
        Q(descripcion__regex=r'^.$') |
        Q(proveedor__marca__startswith='*') |
        Q(proveedor__marca__startswith='"') |
        Q(proveedor__marca__iexact='a') |
        Q(proveedor__marca__iexact='KAISER - HEISSNER') |
        Q(proveedor__marca__iexact='HELA') |
        Q(codigo='17-27-105') |
        Q(descripcion__iexact='ANULA FACTURA') |
        Q(descripcion__iexact='BOLSA')
    ).count()

    kpi_familias_activas = FamiliaProducto.objects.count()
    kpi_proveedores = len({g["proveedor_marca"] for g in lista_grupos if g["proveedor_marca"]})
    # NOTA (Claude): igual que en el código original, no existe un campo
    # "fecha_creacion" en las vistas SQL actuales (VistaProductoAgrupado
    # tampoco lo tenía declarado en models.py), así que esto ya devolvía
    # siempre 0 antes del cambio -- se preserva el mismo comportamiento.
    kpi_nuevos_6_meses = 0

    if texto_busqueda:
        # NOTA (Claude): ya no hace falta el cruce manual con
        # VistaProductoVariantes ni con ProductoGrupoManual para "encontrar
        # el grupo por código de variante" o "por nombre de grupo manual"
        # -- cada grupo efectivo ya trae su nombre YA resuelto y los
        # códigos de TODAS sus variantes, así que buscar por cualquiera de
        # esos códigos encuentra directamente el grupo correcto.
        def _coincide(grupo, termino_l):
            if termino_l in str(grupo["grupo_nombre"] or "").lower():
                return True
            if termino_l in str(grupo["descripcion_grupo_original"] or "").lower():
                return True
            if termino_l in str(grupo["proveedor_marca"] or "").lower():
                return True
            if any(termino_l in str(c or "").lower() for c in grupo["codigos_variantes"]):
                return True
            if any(termino_l in str(c or "").lower() for c in grupo["codigos_origen_variantes"]):
                return True
            return False

        for termino in texto_busqueda.split():
            termino_l = termino.lower()
            lista_grupos = [g for g in lista_grupos if _coincide(g, termino_l)]

    orden_actual = request.GET.get("orden", "")
    dir_actual = request.GET.get("dir", "asc")

    claves_orden = {
        "codigo": lambda g: (g["codigo"] or ""),
        "producto": lambda g: (g["grupo_nombre"] or ""),
        "proveedor": lambda g: (g["proveedor_marca"] or ""),
        "origen": lambda g: (g["codigo_de_origen"] or ""),
    }

    if orden_actual in claves_orden:
        lista_grupos.sort(key=claves_orden[orden_actual], reverse=(dir_actual == "desc"))
    else:
        orden_actual = ""
        lista_grupos.sort(key=lambda g: (g["grupo_nombre"] or ""))

    paginator = Paginator(lista_grupos, 20)
    page = request.GET.get("page")
    page_obj = paginator.get_page(page)

    nombres_grupos_norm = [str(g["grupo_nombre"]).strip().upper() for g in page_obj.object_list if g["grupo_nombre"]]
    info_grupos_qs = ImagenProducto.objects.filter(grupo_nombre__in=nombres_grupos_norm)

    imagenes_dict = {str(img.grupo_nombre).strip().upper(): img.imagen.url for img in info_grupos_qs if img.imagen}
    descripciones_dict = {str(img.grupo_nombre).strip().upper(): img.descripcion for img in info_grupos_qs if img.descripcion}

    # NOTA (Claude): dashboard.html no depende de que estas filas sean
    # instancias de VistaProductoAgrupado -- solo lee atributos (codigo,
    # grupo_nombre, descripcion_grupo, familia_nombre, proveedor.marca,
    # codigo_de_origen, field_id, imagen_url). SimpleNamespace alcanza y
    # evita inventar una clase de modelo falsa.
    filas = []
    for g in page_obj.object_list:
        grupo_nombre_norm = str(g["grupo_nombre"]).strip().upper() if g["grupo_nombre"] else ""
        filas.append(SimpleNamespace(
            field_id=g["id"],
            codigo=g["codigo"],
            codigo_de_origen=g["codigo_de_origen"],
            grupo_nombre=g["grupo_nombre"],
            familia_nombre=g["familia_nombre"],
            descripcion_grupo=descripciones_dict.get(grupo_nombre_norm, g["descripcion_grupo_original"] or ""),
            imagen_url=imagenes_dict.get(grupo_nombre_norm, None),
            proveedor=SimpleNamespace(marca=g["proveedor_marca"]),
        ))
    page_obj.object_list = filas

    catalogo_vigente_con_precio = CatalogCache.objects.exclude(pdf_file__icontains='Sin_Precio').filter(is_current=True).first()
    catalogo_vigente_sin_precio = CatalogCache.objects.filter(pdf_file__icontains='Sin_Precio', is_current=True).first()

    return render(
        request,
        "dashboard.html",
        {
            "productos": page_obj,
            "page_obj": page_obj,
            "busqueda": texto_busqueda,
            "query": texto_busqueda,
            "kpi_productos_activos": kpi_productos_activos,
            "kpi_productos_activos_bruto": kpi_productos_activos_bruto,
            "kpi_familias_activas": kpi_familias_activas,
            "kpi_proveedores": kpi_proveedores,
            "kpi_nuevos_6_meses": kpi_nuevos_6_meses,
            "catalogo_vigente_con_precio": catalogo_vigente_con_precio,
            "catalogo_vigente_sin_precio": catalogo_vigente_sin_precio,
            "orden_actual": orden_actual,
            "dir_actual": dir_actual,
        }
    )
# ==========================================
# OTRAS VISTAS DEL SISTEMA
# ==========================================
@never_cache
@permission_required('prueba.change_producto', login_url='login')
def editar_producto(request, producto_id):
    if request.method == "POST":
        ruta_imagen = request.POST.get("ruta_imagen_producto", "").strip()
        grupo_nombre = request.POST.get("grupo_nombre", "").strip().upper()
        descripcion_grupo = request.POST.get("descripcion_grupo")

        try:
            campos_producto = {}
            if "precio_base_pesos" in request.POST:
                precio = request.POST.get("precio_base_pesos")
                campos_producto["precio_base_pesos"] = float(precio) if precio else None
            if "stock_disponible" in request.POST:
                stock = request.POST.get("stock_disponible")
                campos_producto["stock_disponible"] = float(stock) if stock else None

            if campos_producto:
                Producto.objects.filter(field_id=producto_id).update(**campos_producto)

            if grupo_nombre:
                img_obj, created = ImagenProducto.objects.get_or_create(grupo_nombre=grupo_nombre)
                if ruta_imagen:
                    img_obj.imagen = ruta_imagen
                if descripcion_grupo is not None:
                    img_obj.descripcion = descripcion_grupo.strip()
                img_obj.save()

            messages.success(request, "Producto actualizado correctamente.")
        except ValueError:
            messages.error(request, "Error: Los valores ingresados no son numéricos válidos.")
        except Exception as e:
            messages.error(request, f"Error al guardar: {e}")

    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))

@never_cache
def logout_view(request):
    logout(request)
    return redirect('login')

@never_cache
@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_superuser)
def menu_exportar(request):
    proveedores_excluidos_ids = Proveedor.objects.filter(
        Q(marca__startswith='*') |
        Q(marca__startswith='"') |
        Q(marca__iexact='a') |
        Q(marca__iexact='KAISER - HEISSNER') |
        Q(marca__iexact='HELA')
    ).values_list('field_id', flat=True)

    productos = VistaProductoAgrupado.objects.exclude(
        Q(descripcion__isnull=True) |
        Q(descripcion__exact='') |
        Q(descripcion__startswith='(') |
        Q(descripcion__istartswith='tee') |
        Q(descripcion__regex=r'^.$') |
        Q(proveedor__marca__startswith='*') |
        Q(proveedor__marca__startswith='"') |
        Q(proveedor__marca__iexact='a') |
        Q(proveedor__marca__iexact='KAISER - HEISSNER') |
        Q(proveedor__marca__iexact='HELA') |
        Q(codigo='17-27-105')
    ).order_by('descripcion_grupo')

    overrides_dict = {
        item.producto_id: item.grupo_personalizado
        for item in ProductoGrupoManual.objects.all()
    }

    familias_dict = {f.codigo: f.descripcion for f in FamiliaProducto.objects.all()}

    arbol_todo = {}
    arbol_truper = {}
    arbol_ecosa = {}
    arbol_familias = {}

    for p in productos:
        familia_desc = "Sin Familia"
        es_truper = False
        if p.codigo and "-" in p.codigo:
            partes = p.codigo.split("-")
            if len(partes) >= 2:
                familia_desc = familias_dict.get(partes[1], "Sin Familia")
            if partes[0] in ['17', '18']:
                es_truper = True

        grupo = overrides_dict.get(p.id, p.descripcion_grupo or p.descripcion)
        if familia_desc not in arbol_familias:
            arbol_familias[familia_desc] = set()
        arbol_familias[familia_desc].add(grupo)

        if familia_desc not in arbol_todo:
            arbol_todo[familia_desc] = set()
        arbol_todo[familia_desc].add(grupo)

        if es_truper:
            if familia_desc not in arbol_truper:
                arbol_truper[familia_desc] = set()
            arbol_truper[familia_desc].add(grupo)
        else:
            if familia_desc not in arbol_ecosa:
                arbol_ecosa[familia_desc] = set()
            arbol_ecosa[familia_desc].add(grupo)

    def ordenar_arbol(arbol):
        for f in arbol:
            arbol[f] = sorted(list(arbol[f]))
        return dict(sorted(arbol.items()))

    cantidad_catalogos = CatalogCache.objects.count()

    return render(request, 'exportar.html', {
        'arbol_todo': ordenar_arbol(arbol_todo),
        'arbol_truper': ordenar_arbol(arbol_truper),
        'arbol_ecosa': ordenar_arbol(arbol_ecosa),
        'cantidad_catalogos': cantidad_catalogos
    })

@never_cache
@login_required(login_url='/login/')
def historial_catalogo(request):
    catalogos_con_precio = CatalogCache.objects.exclude(
        pdf_file__icontains='Sin_Precio'
    ).order_by('-version_number')

    catalogos_sin_precio = CatalogCache.objects.filter(
        pdf_file__icontains='Sin_Precio'
    ).order_by('-version_number')

    return render(request, 'historial_catalogo.html', {
        'catalogos_con_precio': catalogos_con_precio,
        'catalogos_sin_precio': catalogos_sin_precio,
    })

@never_cache
@login_required(login_url='/login/')
def descargar_catalogo(request):
    catalogo_actual = CatalogCache.objects.filter(is_current=True).order_by('-version_number').first()

    if not catalogo_actual or not catalogo_actual.pdf_file:
        messages.error(request, "Todavía no se ha generado ningún catálogo en PDF.")
        return redirect('dashboard')

    nombre_archivo = catalogo_actual.pdf_file.name.split('/')[-1]
    return FileResponse(
        catalogo_actual.pdf_file.open('rb'),
        as_attachment=True,
        filename=nombre_archivo,
        content_type='application/pdf',
    )

@never_cache
@login_required(login_url='/login/')
def descargar_catalogo_version(request, catalogo_id):
    catalogo = get_object_or_404(CatalogCache, pk=catalogo_id)

    if not catalogo.pdf_file:
        messages.error(request, "Esta versión no tiene un archivo asociado.")
        return redirect('historial_catalogo')

    nombre_archivo = catalogo.pdf_file.name.split('/')[-1]
    return FileResponse(
        catalogo.pdf_file.open('rb'),
        as_attachment=True,
        filename=nombre_archivo,
        content_type='application/pdf',
    )

@never_cache
@login_required(login_url='/login/')
def eliminar_catalogo(request, catalogo_id):
    if not request.user.is_superuser:
        raise PermissionDenied

    catalogos = CatalogCache.objects.filter(pk=catalogo_id)

    if not catalogos.exists():
        catalogos = CatalogCache.objects.filter(id=catalogo_id)

    if not catalogos.exists():
        messages.error(request, "El catálogo solicitado no existe o ya fue eliminado.")
        return redirect('historial_catalogo')

    version_num = None
    for catalogo in catalogos:
        version_num = catalogo.version_number
        if catalogo.pdf_file:
            catalogo.pdf_file.delete(save=False)
        catalogo.delete()

    messages.success(
        request,
        f"La versión {version_num or catalogo_id} del catálogo y su archivo PDF fueron eliminados para liberar espacio."
    )
    return redirect('historial_catalogo')

# ==========================================
# GESTIÓN DE VIGENCIA Y BLOQUEO DE CATÁLOGOS
# ==========================================
@never_cache
@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_superuser)
def marcar_catalogo_vigente(request, catalogo_id):
    catalogo = get_object_or_404(CatalogCache, id=catalogo_id)

    is_sin_precio = 'Sin_Precio' in catalogo.pdf_file.name

    if is_sin_precio:
        CatalogCache.objects.filter(pdf_file__icontains='Sin_Precio').update(is_current=False)
    else:
        CatalogCache.objects.exclude(pdf_file__icontains='Sin_Precio').update(is_current=False)

    catalogo.is_current = True
    catalogo.save()

    messages.success(request, f"Se ha fijado el catálogo Versión {catalogo.version_number} como la versión vigente oficial.")
    return redirect('historial_catalogo')


@never_cache
@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_superuser)
def generar_pdf(request):
    if request.method == 'POST':
        grupos_seleccionados = request.POST.getlist('grupos_seleccionados')
        tipo_catalogo = request.POST.get('tipo_catalogo', 'con_precio')
        sin_precio = (tipo_catalogo == 'sin_precio')
        modo_filtro = request.POST.get('modo_filtro', 'completo')
        es_solo_truper = (modo_filtro == 'solo_truper')

        MESES_ES = [
            "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
        ]
        ahora = datetime.now()
        fecha_actualizacion = f"{ahora.day} de {MESES_ES[ahora.month - 1]} {ahora.year}"

        codigo_catalogo = request.POST.get('codigo_catalogo', '01')

        if not grupos_seleccionados:
            messages.error(request, "Debes seleccionar al menos un grupo para generar el catálogo.")
            return redirect('menu_exportar')

        overrides_dict = {
            item.producto_id: item
            for item in ProductoGrupoManual.objects.all()
        }

        ids_con_override = [
            pid for pid, item in overrides_dict.items()
            if item.grupo_personalizado in grupos_seleccionados
        ]

        qs = VistaProductoVariantes.objects.select_related("proveedor").filter(
            Q(descripcion_grupo__in=grupos_seleccionados) | Q(id__in=ids_con_override)
        )

        if es_solo_truper:
            qs = qs.filter(Q(codigo__startswith='17') | Q(codigo__startswith='18'))

        qs = qs.annotate(
            es_truper=Case(
                When(proveedor__marca__iexact='truper', then=Value(0)),
                When(codigo__startswith='17', then=Value(0)),
                When(codigo__startswith='18', then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )

        productos_raw = list(qs)
        familias_dict = {f.codigo: f.descripcion for f in FamiliaProducto.objects.all()}
        # NOTA (Claude): fichas técnicas de Truper cacheadas por código_de_origen
        # (ver comando "sync_fichas_truper" y modelo TruperFichaTecnica). No se
        # consulta a truper.com en vivo durante la generación del PDF.
        fichas_truper_dict = {
            item.codigo_origen: item.url_ficha_tecnica
            for item in TruperFichaTecnica.objects.all()
        }
        FAMILIAS_EXCLUIDAS = {'70', '90', '95', '98', '99'}

        productos = []
        for p in productos_raw:
            override_item = overrides_dict.get(p.id)
            grupo_final = override_item.grupo_personalizado if override_item else (p.descripcion_grupo or p.descripcion)
            subgrupo_final = override_item.subgrupo_personalizado if (override_item and override_item.subgrupo_personalizado) else grupo_final

            if grupo_final not in grupos_seleccionados:
                continue

            p.grupo_final = grupo_final
            p.subgrupo_final = subgrupo_final
            p.familia_temporal = "Sin Familia"
            p.codigo_familia_num = 9999
            p.codigo_prod_num = 9999
            if p.codigo and "-" in p.codigo:
                partes = p.codigo.split("-")

                if len(partes) >= 2 and partes[1] in FAMILIAS_EXCLUIDAS:
                    continue

                if len(partes) >= 2:
                    p.familia_temporal = familias_dict.get(partes[1], "Sin Familia")
                    if partes[1].isdigit():
                        p.codigo_familia_num = int(partes[1])
                if len(partes) >= 3 and partes[2].isdigit():
                    p.codigo_prod_num = int(partes[2])

            if override_item and override_item.nombre_limpio_personalizado:
                p.medida_mostrar = override_item.nombre_limpio_personalizado
            else:
                p.medida_mostrar = extraer_medida(p.grupo_final, p.descripcion or "", p.codigo_de_origen or "")

            p.url_ficha_tecnica = fichas_truper_dict.get(p.codigo_de_origen) if p.codigo_de_origen else None

            if p.precio_base_pesos:
                p.precio_clp = f"{int(round(p.precio_base_pesos)):,}".replace(",", ".") + ".-"
            else:
                p.precio_clp = None

            productos.append(p)

        imagenes_dict = {
            str(img.grupo_nombre).strip().upper(): obtener_file_uri(img.imagen)
            for img in ImagenProducto.objects.all() if img.imagen
        }

        descripciones_dict = {
            str(img.grupo_nombre).strip().upper(): img.descripcion
            for img in ImagenProducto.objects.all() if img.descripcion
        }

        destacados_dict = {
            str(img.grupo_nombre).strip().upper(): {
                'es_destacado': img.es_destacado,
                'etiqueta': img.etiqueta_destacado or "DESTACADO"
            }
            for img in ImagenProducto.objects.all()
        }

        ordenes_dict = {
            str(img.grupo_nombre).strip().upper(): img.orden_grupo
            for img in ImagenProducto.objects.all()
        }

        super_grupos_dict = {
            str(img.grupo_nombre).strip().upper(): img.super_grupo
            for img in ImagenProducto.objects.all() if img.super_grupo
        }

        catalogo = OrderedDict()
        catalogo["Truper"] = OrderedDict()
        if not es_solo_truper:
            catalogo["Otras Marcas"] = OrderedDict()

        familias_orden_num = {}

        for p in productos:
            marca_grupo = "Truper" if p.es_truper == 0 else "Otras Marcas"
            familia = p.familia_temporal
            grupo = p.grupo_final

            if marca_grupo not in familias_orden_num:
                familias_orden_num[marca_grupo] = {}
            if familia not in familias_orden_num[marca_grupo]:
                familias_orden_num[marca_grupo][familia] = p.codigo_familia_num

            if marca_grupo not in catalogo:
                catalogo[marca_grupo] = OrderedDict()

            familias_de_marca = catalogo[marca_grupo]
            if familia not in familias_de_marca:
                familias_de_marca[familia] = OrderedDict()

            if grupo not in familias_de_marca[familia]:
                dest_info = destacados_dict.get(str(grupo).strip().upper(), {'es_destacado': False, 'etiqueta': ''})
                familias_de_marca[familia][grupo] = {
                    'imagen_url': imagenes_dict.get(str(grupo).strip().upper(), None),
                    'descripcion': descripciones_dict.get(str(grupo).strip().upper(), ""),
                    'es_destacado': dest_info['es_destacado'],
                    'etiqueta_destacado': dest_info['etiqueta'],
                    'variantes': []
                }

            if p.es_truper != 0:
                p.empaque_inner = None

            familias_de_marca[familia][grupo]['variantes'].append(p)

        for marca_k, familias_dict_items in list(catalogo.items()):
            familias_ordenadas = OrderedDict(
                sorted(
                    familias_dict_items.items(),
                    key=lambda item: (familias_orden_num.get(marca_k, {}).get(item[0], 9999), item[0])
                )
            )
            catalogo[marca_k] = familias_ordenadas

        catalogo = OrderedDict((k, v) for k, v in catalogo.items() if v)

        UMBRAL_TARJETA_ANCHA = 8
        UMBRAL_VARIANTES_EXTREMO = 20
        catalogo_paginado = OrderedDict()

        for marca_k, familias_de_marca in catalogo.items():
            for familia, grupos in list(familias_de_marca.items()):

                for nombre_g, info in grupos.items():
                    info['tiene_empaque_inner'] = any(
                        v.empaque_inner for v in info['variantes']
                    )

                    info['variantes'].sort(key=lambda v: (str(v.subgrupo_final), v.codigo_prod_num, v.codigo))

                    subgrupos_unicos = set(
                        v.subgrupo_final for v in info['variantes']
                        if v.subgrupo_final and v.subgrupo_final != nombre_g
                    )
                    info['tiene_subgrupos'] = len(subgrupos_unicos) > 0

                    min_codigo_prod = info['variantes'][0].codigo_prod_num if info['variantes'] else 9999
                    min_codigo_str = info['variantes'][0].codigo if info['variantes'] else ""
                    info['min_codigo_prod'] = min_codigo_prod
                    info['min_codigo_str'] = min_codigo_str

                    pos_db = ordenes_dict.get(str(nombre_g).strip().upper(), 0)
                    info['posicion_fija'] = pos_db if pos_db > 0 else 9999

                    info['filas_cabecera_subgrupos'] = len(subgrupos_unicos)
                    info['filas_totales'] = len(info['variantes']) + len(subgrupos_unicos)

                    info['es_ancha'] = len(info['variantes']) > UMBRAL_TARJETA_ANCHA
                    info['es_extremo'] = len(info['variantes']) > UMBRAL_VARIANTES_EXTREMO

                    info['prefijo_nombre'] = str(nombre_g).strip().split(' ')[0].upper() if nombre_g else ""

                    marcas_grupo = [
                        v.proveedor.marca for v in info['variantes']
                        if getattr(v, 'proveedor', None) and v.proveedor.marca
                    ]
                    info['marca_grupo'] = Counter(marcas_grupo).most_common(1)[0][0] if marcas_grupo else ""

                    info['super_grupo'] = super_grupos_dict.get(str(nombre_g).strip().upper())

                familias_de_marca[familia] = OrderedDict(
                    _agrupar_por_super_grupo(list(grupos.items()))
                )

                SLOTS_POR_FILA = 3
                FILAS_POR_PAGINA = 3
                MAX_ANCHAS_POR_PAGINA = 2

                filas_paginado = []
                fila_actual = []
                slots_usados = 0

                for nombre_g, info in familias_de_marca[familia].items():
                    if info['es_extremo']:
                        if fila_actual:
                            filas_paginado.append(fila_actual)
                            fila_actual = []
                            slots_usados = 0
                        filas_paginado.append([(nombre_g, info)])
                        continue

                    costo = 2 if info['es_ancha'] else 1
                    if slots_usados + costo > SLOTS_POR_FILA and fila_actual:
                        filas_paginado.append(fila_actual)
                        fila_actual = []
                        slots_usados = 0
                    fila_actual.append((nombre_g, info))
                    slots_usados += costo

                if fila_actual:
                    filas_paginado.append(fila_actual)

                paginas_familia = []
                pagina_actual = []
                anchas_en_pagina = 0

                for fila in filas_paginado:
                    trae_ancha = any(info['es_ancha'] for _, info in fila)
                    trae_extremo = any(info['es_extremo'] for _, info in fila)

                    necesita_pagina_nueva = (
                        trae_extremo
                        or (pagina_actual and any(info['es_extremo'] for f in pagina_actual for _, info in f))
                        or len(pagina_actual) >= FILAS_POR_PAGINA
                        or (anchas_en_pagina + (1 if trae_ancha else 0)) > MAX_ANCHAS_POR_PAGINA
                    )

                    if necesita_pagina_nueva and pagina_actual:
                        paginas_familia.append(pagina_actual)
                        pagina_actual = []
                        anchas_en_pagina = 0

                    pagina_actual.append(fila)
                    if trae_ancha:
                        anchas_en_pagina += 1

                    if trae_extremo:
                        paginas_familia.append(pagina_actual)
                        pagina_actual = []
                        anchas_en_pagina = 0

                if pagina_actual:
                    paginas_familia.append(pagina_actual)

                paginas_con_info = []
                for pagina in paginas_familia:
                    pagina_sin_vacias = [fila for fila in pagina if fila]
                    filas_con_info = [{'items': fila} for fila in pagina_sin_vacias]
                    paginas_con_info.append({'filas': filas_con_info})

                catalogo_paginado.setdefault(marca_k, OrderedDict())[familia] = paginas_con_info

        logo_base64 = obtener_base64_imagen('static/img/logo_ecosa.png')
        portada_base64 = obtener_base64_imagen('static/img/portada.png')

        html_productos = render_to_string('catalogo_pdf.html', {
            'catalogo': catalogo,
            'catalogo_paginado': catalogo_paginado,
            'request': request,
            'logo_base64': logo_base64,
            'portada_base64': portada_base64,
            'sin_precio': sin_precio,
            'seccion': 'productos',
        })

        header_template = f"""
        <style>
            #header, #footer {{ padding: 0 !important; margin: 0 !important; width: 100%; }}
            .header-box {{
                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                font-size: 8pt;
                width: 100%;
                padding: 2mm 10mm 0 10mm;
                display: flex;
                align-items: center;
                justify-content: flex-start;
                box-sizing: border-box;
            }}
        </style>
        <div class="header-box">
            {"<img src='" + logo_base64 + "' style='height: 7mm; width: auto;' />" if logo_base64 else ""}
        </div>
        """

        footer_template = f"""
        <style>
            #header, #footer {{ padding: 0 !important; margin: 0 !important; width: 100%; }}
            .footer-box {{
                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                font-size: 8pt;
                line-height: 1;
                width: 100%;
                padding: 0 10mm 18px 10mm;
                display: flex;
                justify-content: space-between;
                align-items: center;
                box-sizing: border-box;
            }}
        </style>
        <div class="footer-box">
            <div style="flex: 1; text-align: left; color: #444444;">
                Actualizada al {fecha_actualizacion}
            </div>
            <div style="flex: 1; text-align: center;">
                </div>
            <div style="flex: 1; text-align: right; color: #444444;">
                Página <span class="pageNumber"></span> de <span class="totalPages"></span>
            </div>
        </div>
        """

        with tempfile.NamedTemporaryFile(delete=False, suffix='.html', mode='w', encoding='utf-8') as tmp_file:
            tmp_file.write(html_productos)
            tmp_html_productos_path = tmp_file.name

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--js-flags=--max-old-space-size=4096',
                        '--allow-file-access-from-files'
                    ]
                )
                try:
                    page_productos = browser.new_page()
                    page_productos.goto(f"file://{tmp_html_productos_path}", wait_until="load", timeout=180000)

                    pdf_bytes_productos = page_productos.pdf(
                        format="Letter",
                        print_background=True,
                        prefer_css_page_size=True,
                        display_header_footer=True,
                        header_template=header_template,
                        footer_template=footer_template,
                        margin={"top": "18mm", "bottom": "18mm", "left": "10mm", "right": "10mm"}
                    )
                    page_productos.close()

                    doc_productos = fitz.open(stream=pdf_bytes_productos, filetype="pdf")
                    page_map = {}

                    for i in range(doc_productos.page_count):
                        page = doc_productos[i]
                        text = page.get_text("text")
                        matches = re.findall(r'\[\[(sec-[^\]]+)\]\]', text)
                        for m in matches:
                            if m not in page_map:
                                page_map[m] = i + 1

                    html_inicio_dummy = render_to_string('catalogo_pdf.html', {
                        'catalogo': catalogo,
                        'request': request,
                        'logo_base64': logo_base64,
                        'portada_base64': portada_base64,
                        'sin_precio': sin_precio,
                        'seccion': 'inicio',
                    })

                    page_inicio_dummy = browser.new_page()
                    page_inicio_dummy.set_content(html_inicio_dummy, wait_until="load", timeout=120000)
                    pdf_dummy = page_inicio_dummy.pdf(
                        format="Letter", margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"}
                    )
                    page_inicio_dummy.close()

                    doc_dummy = fitz.open(stream=pdf_dummy, filetype="pdf")
                    offset_paginas = doc_dummy.page_count
                    doc_dummy.close()

                    indice_datos = []
                    for marca, familias in catalogo.items():
                        marcas_data = {'marca': marca, 'familias': []}
                        for familia in familias.keys():
                            id_sec = f"sec-{slugify(marca)}-{slugify(familia)}"
                            pag_relativa = page_map.get(id_sec, 1)
                            pag_absoluta = offset_paginas + pag_relativa

                            marcas_data['familias'].append({
                                'nombre': familia,
                                'id_sec': id_sec,
                                'pagina': pag_absoluta
                            })
                        if marcas_data['familias']:
                            indice_datos.append(marcas_data)

                    html_inicio_final = render_to_string('catalogo_pdf.html', {
                        'catalogo': catalogo,
                        'indice_datos': indice_datos,
                        'request': request,
                        'logo_base64': logo_base64,
                        'portada_base64': portada_base64,
                        'sin_precio': sin_precio,
                        'seccion': 'inicio',
                    })

                    page_inicio_final = browser.new_page()
                    page_inicio_final.set_content(html_inicio_final, wait_until="load", timeout=120000)
                    pdf_bytes_inicio = page_inicio_final.pdf(
                        format="Letter",
                        print_background=True,
                        prefer_css_page_size=True,
                        display_header_footer=False,
                        margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"}
                    )
                    page_inicio_final.close()

                    doc_inicio = fitz.open(stream=pdf_bytes_inicio, filetype="pdf")
                    doc_final = fitz.open()
                    doc_final.insert_pdf(doc_inicio)
                    doc_final.insert_pdf(doc_productos)
                    doc_inicio.close()
                    doc_productos.close()

                    for m in indice_datos:
                        for f in m['familias']:
                            target_page = int(f['pagina']) - 1
                            for i in range(offset_paginas):
                                page = doc_final[i]
                                areas = page.search_for(f['nombre'])
                                for rect in areas:
                                    link = {"kind": fitz.LINK_GOTO, "from": rect, "page": target_page}
                                    page.insert_link(link)

                    toc_pdf = []
                    for m in indice_datos:
                        toc_pdf.append([1, m['marca'], 1])
                        for f in m['familias']:
                            toc_pdf.append([2, f['nombre'], int(f['pagina'])])
                    doc_final.set_toc(toc_pdf)

                    hitos_familias = []
                    for m in indice_datos:
                        for f in m['familias']:
                            hitos_familias.append({
                                'pagina': int(f['pagina']),
                                'texto': f"{m['marca']} - {f['nombre']}"
                            })

                    hitos_familias.sort(key=lambda x: x['pagina'])

                    def obtener_cat_para_pagina(num_pag):
                        cat_actual = ""
                        for h in hitos_familias:
                            if num_pag >= h['pagina']:
                                cat_actual = h['texto']
                            else:
                                break
                        return cat_actual

                    for i in range(offset_paginas, doc_final.page_count):
                        num_hoja = i + 1
                        cat_texto = obtener_cat_para_pagina(num_hoja)
                        if cat_texto:
                            page = doc_final[i]
                            rect_centro = fitz.Rect(150, 755, 462, 780)
                            page.insert_textbox(
                                rect_centro,
                                cat_texto,
                                fontsize=8,
                                fontname="helv",
                                color=(0, 0, 0),
                                align=fitz.TEXT_ALIGN_CENTER
                            )

                    pdf_bytes = doc_final.write()
                    doc_final.close()

                finally:
                    browser.close()
        finally:
            if os.path.exists(tmp_html_productos_path):
                os.remove(tmp_html_productos_path)

        fecha_archivo = datetime.now().strftime('%d-%m-%Y')
        prefijo_nombre = "Catalogo_Truper" if es_solo_truper else "Catalogo_Ecosa"

        if sin_precio:
            nombre_archivo = f"{prefijo_nombre}_Sin_Precio_{fecha_archivo}.pdf"
            catalogos_existentes = CatalogCache.objects.filter(pdf_file__icontains='Sin_Precio').order_by('version_number')
            texto_tipo = "sin precio (Solo Truper)" if es_solo_truper else "sin precio"
        else:
            nombre_archivo = f"{prefijo_nombre}_{fecha_archivo}.pdf"
            catalogos_existentes = CatalogCache.objects.exclude(pdf_file__icontains='Sin_Precio').order_by('version_number')
            texto_tipo = "con precio (Solo Truper)" if es_solo_truper else "con precio"

        catalogo_eliminado = _eliminar_catalogo_mas_antiguo_si_corresponde(catalogos_existentes)
        if catalogo_eliminado:
            messages.warning(request, f"Se ha eliminado el catálogo {texto_tipo} más antiguo para liberar espacio.")

        limpiar_pdfs_huerfanos(sin_precio)

        ultima_version = CatalogCache.objects.order_by('-version_number').first()
        siguiente_version = (ultima_version.version_number + 1) if ultima_version else 1

        nuevo_registro = CatalogCache(version_number=siguiente_version, is_current=False)
        nuevo_registro.pdf_file.save(nombre_archivo, ContentFile(pdf_bytes), save=True)

        messages.success(request, f"Catálogo {texto_tipo} generado exitosamente (Versión {siguiente_version}). Recuerda marcarlo como vigente en el historial si deseas asignarlo como oficial.")

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{nombre_archivo}"'
        return response

    return redirect('dashboard')


def sugerencias_busqueda(request):
    termino = request.GET.get('term', '').strip()
    resultados = []

    if len(termino) >= 2:
        qs = VistaProductoVariantes.objects.filter(
            Q(codigo__icontains=termino) |
            Q(codigo_de_origen__icontains=termino) |
            Q(descripcion__icontains=termino) |
            Q(descripcion_grupo__icontains=termino)
        ).values('codigo', 'descripcion', 'descripcion_grupo')[:7]

        vistos = set()
        for p in qs:
            nombre_grupo = p['descripcion_grupo'] or p['descripcion']
            etiqueta = f"{p['codigo']} - {nombre_grupo}"
            if etiqueta not in vistos:
                vistos.add(etiqueta)
                resultados.append({
                    'label': etiqueta,
                    'valor': p['codigo'],
                    'codigo': p['codigo']
                })

    return JsonResponse(resultados, safe=False)

# ==========================================================================
# GENERACIÓN DE PDF EN SEGUNDO PLANO (pantalla de carga real)
# ==========================================================================
import threading
import uuid

_trabajos_pdf_lock = threading.Lock()
_trabajos_pdf = {}  # job_id (str) -> dict con el estado del trabajo


def _actualizar_estado_trabajo(job_id, **campos):
    with _trabajos_pdf_lock:
        if job_id in _trabajos_pdf:
            _trabajos_pdf[job_id].update(campos)


# ==========================================================================
# RENDERIZADO PARALELO DEL PDF DE PRODUCTOS (EXPERIMENTAL)
# ==========================================================================
def _dividir_catalogo_paginado_en_chunks(catalogo_paginado, n_workers):
    unidades = []
    for marca, familias in catalogo_paginado.items():
        for familia, paginas in familias.items():
            unidades.append((marca, familia, paginas))

    if not unidades:
        return []

    n_workers = max(1, min(n_workers, len(unidades)))
    tamano_base = len(unidades) // n_workers
    resto = len(unidades) % n_workers

    chunks = []
    idx = 0
    for i in range(n_workers):
        tamano = tamano_base + (1 if i < resto else 0)
        if tamano == 0:
            continue
        trozo = unidades[idx: idx + tamano]
        idx += tamano

        chunk_dict = OrderedDict()
        for marca, familia, paginas in trozo:
            chunk_dict.setdefault(marca, OrderedDict())[familia] = paginas
        chunks.append(chunk_dict)

    return chunks


async def _renderizar_chunk_pdf_async(browser, chunk_catalogo_paginado, catalogo, request,
                                       logo_base64, portada_base64, sin_precio,
                                       header_template, footer_template, es_primer_chunk):
    html_chunk = render_to_string('catalogo_pdf.html', {
        'catalogo': catalogo,
        'catalogo_paginado': chunk_catalogo_paginado,
        'request': request,
        'logo_base64': logo_base64,
        'portada_base64': portada_base64,
        'sin_precio': sin_precio,
        'seccion': 'productos',
        'primera_familia_global': es_primer_chunk,
    })

    with tempfile.NamedTemporaryFile(delete=False, suffix='.html', mode='w', encoding='utf-8') as tmp_file:
        tmp_file.write(html_chunk)
        tmp_path = tmp_file.name

    try:
        page = await browser.new_page()
        try:
            await page.goto(f"file://{tmp_path}", wait_until="load", timeout=180000)
            pdf_bytes = await page.pdf(
                format="Letter",
                print_background=True,
                prefer_css_page_size=True,
                display_header_footer=True,
                header_template=header_template,
                footer_template=footer_template,
                margin={"top": "18mm", "bottom": "18mm", "left": "10mm", "right": "10mm"}
            )
        finally:
            await page.close()
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return pdf_bytes


async def _renderizar_productos_en_paralelo(catalogo_paginado, catalogo, request,
                                             logo_base64, portada_base64, sin_precio,
                                             header_template, footer_template, n_workers=4):
    chunks = _dividir_catalogo_paginado_en_chunks(catalogo_paginado, n_workers)
    if not chunks:
        return []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--js-flags=--max-old-space-size=4096',
                '--allow-file-access-from-files'
            ]
        )
        try:
            tareas = [
                _renderizar_chunk_pdf_async(
                    browser, chunk, catalogo, request, logo_base64, portada_base64,
                    sin_precio, header_template, footer_template, es_primer_chunk=(i == 0)
                )
                for i, chunk in enumerate(chunks)
            ]
            resultados = await asyncio.gather(*tareas)
        finally:
            await browser.close()

    return resultados


def _ejecutar_generacion_pdf_en_hilo(job_id, request, grupos_seleccionados, tipo_catalogo, modo_filtro):
    try:
        _t0 = time.perf_counter()
        _t_ultimo = _t0

        def _marcar(etiqueta):
            nonlocal _t_ultimo
            _ahora = time.perf_counter()
            print(f"[timing pdf][{job_id}] {etiqueta}: {_ahora - _t_ultimo:.2f}s (acumulado: {_ahora - _t0:.2f}s)", flush=True)
            _t_ultimo = _ahora

        sin_precio = (tipo_catalogo == 'sin_precio')
        es_solo_truper = (modo_filtro == 'solo_truper')

        MESES_ES = [
            "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
        ]
        ahora = datetime.now()
        fecha_actualizacion = f"{ahora.day} de {MESES_ES[ahora.month - 1]} {ahora.year}"

        overrides_dict = {
            item.producto_id: item
            for item in ProductoGrupoManual.objects.all()
        }

        ids_con_override = [
            pid for pid, item in overrides_dict.items()
            if item.grupo_personalizado in grupos_seleccionados
        ]

        qs = VistaProductoVariantes.objects.select_related("proveedor").filter(
            Q(descripcion_grupo__in=grupos_seleccionados) | Q(id__in=ids_con_override)
        )

        if es_solo_truper:
            qs = qs.filter(Q(codigo__startswith='17') | Q(codigo__startswith='18'))

        qs = qs.annotate(
            es_truper=Case(
                When(proveedor__marca__iexact='truper', then=Value(0)),
                When(codigo__startswith='17', then=Value(0)),
                When(codigo__startswith='18', then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )

        productos_raw = list(qs)
        familias_dict = {f.codigo: f.descripcion for f in FamiliaProducto.objects.all()}
        # NOTA (Claude): fichas técnicas de Truper cacheadas por código_de_origen
        # (ver comando "sync_fichas_truper" y modelo TruperFichaTecnica). No se
        # consulta a truper.com en vivo durante la generación del PDF.
        fichas_truper_dict = {
            item.codigo_origen: item.url_ficha_tecnica
            for item in TruperFichaTecnica.objects.all()
        }
        FAMILIAS_EXCLUIDAS = {'70', '90', '95', '98', '99'}

        productos = []
        for p in productos_raw:
            override_item = overrides_dict.get(p.id)
            grupo_final = override_item.grupo_personalizado if override_item else (p.descripcion_grupo or p.descripcion)
            subgrupo_final = override_item.subgrupo_personalizado if (override_item and override_item.subgrupo_personalizado) else grupo_final

            if grupo_final not in grupos_seleccionados:
                continue

            p.grupo_final = grupo_final
            p.subgrupo_final = subgrupo_final
            p.familia_temporal = "Sin Familia"
            p.codigo_familia_num = 9999
            p.codigo_prod_num = 9999
            if p.codigo and "-" in p.codigo:
                partes = p.codigo.split("-")

                if len(partes) >= 2 and partes[1] in FAMILIAS_EXCLUIDAS:
                    continue

                if len(partes) >= 2:
                    p.familia_temporal = familias_dict.get(partes[1], "Sin Familia")
                    if partes[1].isdigit():
                        p.codigo_familia_num = int(partes[1])
                if len(partes) >= 3 and partes[2].isdigit():
                    p.codigo_prod_num = int(partes[2])

            if override_item and override_item.nombre_limpio_personalizado:
                p.medida_mostrar = override_item.nombre_limpio_personalizado
            else:
                p.medida_mostrar = extraer_medida(p.grupo_final, p.descripcion or "", p.codigo_de_origen or "")

            p.url_ficha_tecnica = fichas_truper_dict.get(p.codigo_de_origen) if p.codigo_de_origen else None

            if p.precio_base_pesos:
                p.precio_clp = f"{int(round(p.precio_base_pesos)):,}".replace(",", ".") + ".-"
            else:
                p.precio_clp = None

            productos.append(p)

        _marcar("Consulta BD y armado de lista de productos")
        imagenes_dict = {
            str(img.grupo_nombre).strip().upper(): obtener_file_uri(img.imagen)
            for img in ImagenProducto.objects.all() if img.imagen
        }

        descripciones_dict = {
            str(img.grupo_nombre).strip().upper(): img.descripcion
            for img in ImagenProducto.objects.all() if img.descripcion
        }

        destacados_dict = {
            str(img.grupo_nombre).strip().upper(): {
                'es_destacado': img.es_destacado,
                'etiqueta': img.etiqueta_destacado or "DESTACADO"
            }
            for img in ImagenProducto.objects.all()
        }

        ordenes_dict = {
            str(img.grupo_nombre).strip().upper(): img.orden_grupo
            for img in ImagenProducto.objects.all()
        }

        super_grupos_dict = {
            str(img.grupo_nombre).strip().upper(): img.super_grupo
            for img in ImagenProducto.objects.all() if img.super_grupo
        }

        _marcar("Construccion de 5 diccionarios de ImagenProducto (imagenes/descripciones/destacados/ordenes/super_grupos)")
        catalogo = OrderedDict()
        catalogo["Truper"] = OrderedDict()
        if not es_solo_truper:
            catalogo["Otras Marcas"] = OrderedDict()

        familias_orden_num = {}

        for p in productos:
            marca_grupo = "Truper" if p.es_truper == 0 else "Otras Marcas"
            familia = p.familia_temporal
            grupo = p.grupo_final

            if marca_grupo not in familias_orden_num:
                familias_orden_num[marca_grupo] = {}
            if familia not in familias_orden_num[marca_grupo]:
                familias_orden_num[marca_grupo][familia] = p.codigo_familia_num

            if marca_grupo not in catalogo:
                catalogo[marca_grupo] = OrderedDict()

            familias_de_marca = catalogo[marca_grupo]
            if familia not in familias_de_marca:
                familias_de_marca[familia] = OrderedDict()

            if grupo not in familias_de_marca[familia]:
                dest_info = destacados_dict.get(str(grupo).strip().upper(), {'es_destacado': False, 'etiqueta': ''})
                familias_de_marca[familia][grupo] = {
                    'imagen_url': imagenes_dict.get(str(grupo).strip().upper(), None),
                    'descripcion': descripciones_dict.get(str(grupo).strip().upper(), ""),
                    'es_destacado': dest_info['es_destacado'],
                    'etiqueta_destacado': dest_info['etiqueta'],
                    'variantes': []
                }

            if p.es_truper != 0:
                p.empaque_inner = None

            familias_de_marca[familia][grupo]['variantes'].append(p)

        for marca_k, familias_dict_items in list(catalogo.items()):
            familias_ordenadas = OrderedDict(
                sorted(
                    familias_dict_items.items(),
                    key=lambda item: (familias_orden_num.get(marca_k, {}).get(item[0], 9999), item[0])
                )
            )
            catalogo[marca_k] = familias_ordenadas

        catalogo = OrderedDict((k, v) for k, v in catalogo.items() if v)

        _marcar("Armado de estructura 'catalogo' (agrupar por marca/familia/grupo)")
        UMBRAL_TARJETA_ANCHA = 8
        UMBRAL_VARIANTES_EXTREMO = 20
        catalogo_paginado = OrderedDict()

        for marca_k, familias_de_marca in catalogo.items():
            for familia, grupos in list(familias_de_marca.items()):

                for nombre_g, info in grupos.items():
                    info['tiene_empaque_inner'] = any(
                        v.empaque_inner for v in info['variantes']
                    )

                    info['variantes'].sort(key=lambda v: (str(v.subgrupo_final), v.codigo_prod_num, v.codigo))

                    subgrupos_unicos = set(
                        v.subgrupo_final for v in info['variantes']
                        if v.subgrupo_final and v.subgrupo_final != nombre_g
                    )
                    info['tiene_subgrupos'] = len(subgrupos_unicos) > 0

                    min_codigo_prod = info['variantes'][0].codigo_prod_num if info['variantes'] else 9999
                    min_codigo_str = info['variantes'][0].codigo if info['variantes'] else ""
                    info['min_codigo_prod'] = min_codigo_prod
                    info['min_codigo_str'] = min_codigo_str

                    pos_db = ordenes_dict.get(str(nombre_g).strip().upper(), 0)
                    info['posicion_fija'] = pos_db if pos_db > 0 else 9999

                    info['filas_cabecera_subgrupos'] = len(subgrupos_unicos)
                    info['filas_totales'] = len(info['variantes']) + len(subgrupos_unicos)

                    info['es_ancha'] = len(info['variantes']) > UMBRAL_TARJETA_ANCHA
                    info['es_extremo'] = len(info['variantes']) > UMBRAL_VARIANTES_EXTREMO

                    info['prefijo_nombre'] = str(nombre_g).strip().split(' ')[0].upper() if nombre_g else ""

                    marcas_grupo = [
                        v.proveedor.marca for v in info['variantes']
                        if getattr(v, 'proveedor', None) and v.proveedor.marca
                    ]
                    info['marca_grupo'] = Counter(marcas_grupo).most_common(1)[0][0] if marcas_grupo else ""

                    info['super_grupo'] = super_grupos_dict.get(str(nombre_g).strip().upper())

                familias_de_marca[familia] = OrderedDict(
                    _agrupar_por_super_grupo(list(grupos.items()))
                )

                SLOTS_POR_FILA = 3
                FILAS_POR_PAGINA = 3
                MAX_ANCHAS_POR_PAGINA = 2

                filas_paginado = []
                fila_actual = []
                slots_usados = 0

                for nombre_g, info in familias_de_marca[familia].items():
                    if info['es_extremo']:
                        if fila_actual:
                            filas_paginado.append(fila_actual)
                            fila_actual = []
                            slots_usados = 0
                        filas_paginado.append([(nombre_g, info)])
                        continue

                    costo = 2 if info['es_ancha'] else 1
                    if slots_usados + costo > SLOTS_POR_FILA and fila_actual:
                        filas_paginado.append(fila_actual)
                        fila_actual = []
                        slots_usados = 0
                    fila_actual.append((nombre_g, info))
                    slots_usados += costo

                if fila_actual:
                    filas_paginado.append(fila_actual)

                paginas_familia = []
                pagina_actual = []
                anchas_en_pagina = 0

                for fila in filas_paginado:
                    trae_ancha = any(info['es_ancha'] for _, info in fila)
                    trae_extremo = any(info['es_extremo'] for _, info in fila)

                    necesita_pagina_nueva = (
                        trae_extremo
                        or (pagina_actual and any(info['es_extremo'] for f in pagina_actual for _, info in f))
                        or len(pagina_actual) >= FILAS_POR_PAGINA
                        or (anchas_en_pagina + (1 if trae_ancha else 0)) > MAX_ANCHAS_POR_PAGINA
                    )

                    if necesita_pagina_nueva and pagina_actual:
                        paginas_familia.append(pagina_actual)
                        pagina_actual = []
                        anchas_en_pagina = 0

                    pagina_actual.append(fila)
                    if trae_ancha:
                        anchas_en_pagina += 1

                    if trae_extremo:
                        paginas_familia.append(pagina_actual)
                        pagina_actual = []
                        anchas_en_pagina = 0

                if pagina_actual:
                    paginas_familia.append(pagina_actual)

                paginas_con_info = []
                for pagina in paginas_familia:
                    pagina_sin_vacias = [fila for fila in pagina if fila]
                    filas_con_info = [{'items': fila} for fila in pagina_sin_vacias]
                    paginas_con_info.append({'filas': filas_con_info})

                catalogo_paginado.setdefault(marca_k, OrderedDict())[familia] = paginas_con_info

        _marcar("Paginado y ordenamiento (calculo de layout de tarjetas)")
        logo_base64 = obtener_base64_imagen('static/img/logo_ecosa.png')
        portada_base64 = obtener_base64_imagen('static/img/portada.png')

        _marcar("Logo y portada a base64")

        header_template = f"""
        <style>
            #header, #footer {{ padding: 0 !important; margin: 0 !important; width: 100%; }}
            .header-box {{
                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                font-size: 8pt;
                width: 100%;
                padding: 2mm 10mm 0 10mm;
                display: flex;
                align-items: center;
                justify-content: flex-start;
                box-sizing: border-box;
            }}
        </style>
        <div class="header-box">
            {"<img src='" + logo_base64 + "' style='height: 7mm; width: auto;' />" if logo_base64 else ""}
        </div>
        """

        footer_template = f"""
        <style>
            #header, #footer {{ padding: 0 !important; margin: 0 !important; width: 100%; }}
            .footer-box {{
                font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
                font-size: 8pt;
                line-height: 1;
                width: 100%;
                padding: 0 10mm 18px 10mm;
                display: flex;
                justify-content: space-between;
                align-items: center;
                box-sizing: border-box;
            }}
        </style>
        <div class="footer-box">
            <div style="flex: 1; text-align: left; color: #444444;">
                Actualizada al {fecha_actualizacion}
            </div>
            <div style="flex: 1; text-align: center;">
                </div>
            <div style="flex: 1; text-align: right; color: #444444;">
                Página <span class="pageNumber"></span> de <span class="totalPages"></span>
            </div>
        </div>
        """

        _marcar("Render de headers/footers de PDF")

        N_WORKERS_PDF = 8
        pdf_bytes_lista = asyncio.run(_renderizar_productos_en_paralelo(
            catalogo_paginado, catalogo, request, logo_base64, portada_base64,
            sin_precio, header_template, footer_template, n_workers=N_WORKERS_PDF
        ))

        _marcar(f"Playwright: renderizar PDF de productos EN PARALELO ({N_WORKERS_PDF} workers)")

        doc_productos = fitz.open()
        for _pdf_bytes_chunk in pdf_bytes_lista:
            _doc_chunk = fitz.open(stream=_pdf_bytes_chunk, filetype="pdf")
            doc_productos.insert_pdf(_doc_chunk)
            _doc_chunk.close()

        _marcar("Fusion de los PDFs parciales de cada worker (PyMuPDF)")

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--js-flags=--max-old-space-size=4096',
                    '--allow-file-access-from-files'
                ]
            )
            _marcar("Lanzar navegador Playwright (indice)")
            try:
                page_map = {}

                for i in range(doc_productos.page_count):
                    page = doc_productos[i]
                    text = page.get_text("text")
                    matches = re.findall(r'\[\[(sec-[^\]]+)\]\]', text)
                    for m in matches:
                        if m not in page_map:
                            page_map[m] = i + 1

                _marcar("Extraer mapa de paginas con PyMuPDF")
                html_inicio_dummy = render_to_string('catalogo_pdf.html', {
                    'catalogo': catalogo,
                    'request': request,
                    'logo_base64': logo_base64,
                    'portada_base64': portada_base64,
                    'sin_precio': sin_precio,
                    'seccion': 'inicio',
                })

                page_inicio_dummy = browser.new_page()
                page_inicio_dummy.set_content(html_inicio_dummy, wait_until="load", timeout=120000)
                pdf_dummy = page_inicio_dummy.pdf(
                    format="Letter", margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"}
                )
                page_inicio_dummy.close()

                doc_dummy = fitz.open(stream=pdf_dummy, filetype="pdf")
                offset_paginas = doc_dummy.page_count
                doc_dummy.close()

                _marcar("Render indice 'dummy' (Playwright+PyMuPDF, solo para contar paginas)")
                indice_datos = []
                for marca, familias in catalogo.items():
                    marcas_data = {'marca': marca, 'familias': []}
                    for familia in familias.keys():
                        id_sec = f"sec-{slugify(marca)}-{slugify(familia)}"
                        pag_relativa = page_map.get(id_sec, 1)
                        pag_absoluta = offset_paginas + pag_relativa

                        marcas_data['familias'].append({
                            'nombre': familia,
                            'id_sec': id_sec,
                            'pagina': pag_absoluta
                        })
                    if marcas_data['familias']:
                        indice_datos.append(marcas_data)

                html_inicio_final = render_to_string('catalogo_pdf.html', {
                    'catalogo': catalogo,
                    'indice_datos': indice_datos,
                    'request': request,
                    'logo_base64': logo_base64,
                    'portada_base64': portada_base64,
                    'sin_precio': sin_precio,
                    'seccion': 'inicio',
                })

                page_inicio_final = browser.new_page()
                page_inicio_final.set_content(html_inicio_final, wait_until="load", timeout=120000)
                pdf_bytes_inicio = page_inicio_final.pdf(
                    format="Letter",
                    print_background=True,
                    prefer_css_page_size=True,
                    display_header_footer=False,
                    margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"}
                )
                page_inicio_final.close()

                _marcar("Armar indice final + render Playwright del indice")
                doc_inicio = fitz.open(stream=pdf_bytes_inicio, filetype="pdf")
                doc_final = fitz.open()
                doc_final.insert_pdf(doc_inicio)
                doc_final.insert_pdf(doc_productos)
                doc_inicio.close()
                doc_productos.close()

                for m in indice_datos:
                    for f in m['familias']:
                        target_page = int(f['pagina']) - 1
                        for i in range(offset_paginas):
                            page = doc_final[i]
                            areas = page.search_for(f['nombre'])
                            for rect in areas:
                                link = {"kind": fitz.LINK_GOTO, "from": rect, "page": target_page}
                                page.insert_link(link)

                toc_pdf = []
                for m in indice_datos:
                    toc_pdf.append([1, m['marca'], 1])
                    for f in m['familias']:
                        toc_pdf.append([2, f['nombre'], int(f['pagina'])])
                doc_final.set_toc(toc_pdf)

                hitos_familias = []
                for m in indice_datos:
                    for f in m['familias']:
                        hitos_familias.append({
                            'pagina': int(f['pagina']),
                            'texto': f"{m['marca']} - {f['nombre']}"
                        })

                hitos_familias.sort(key=lambda x: x['pagina'])

                def obtener_cat_para_pagina(num_pag):
                    cat_actual = ""
                    for h in hitos_familias:
                        if num_pag >= h['pagina']:
                            cat_actual = h['texto']
                        else:
                            break
                    return cat_actual

                for i in range(offset_paginas, doc_final.page_count):
                    num_hoja = i + 1
                    cat_texto = obtener_cat_para_pagina(num_hoja)
                    if cat_texto:
                        page = doc_final[i]
                        rect_centro = fitz.Rect(150, 755, 462, 780)
                        page.insert_textbox(
                            rect_centro,
                            cat_texto,
                            fontsize=8,
                            fontname="helv",
                            color=(0, 0, 0),
                            align=fitz.TEXT_ALIGN_CENTER
                        )

                pdf_bytes = doc_final.write()
                doc_final.close()
                _marcar("Fusion de PDFs + enlaces + tabla de contenido + pie de pagina por categoria")

            finally:
                browser.close()

        _marcar("Cerrar navegador del indice")
        fecha_archivo = datetime.now().strftime('%d-%m-%Y')
        prefijo_nombre = "Catalogo_Truper" if es_solo_truper else "Catalogo_Ecosa"

        if sin_precio:
            nombre_archivo = f"{prefijo_nombre}_Sin_Precio_{fecha_archivo}.pdf"
            catalogos_existentes = CatalogCache.objects.filter(pdf_file__icontains='Sin_Precio').order_by('version_number')
        else:
            nombre_archivo = f"{prefijo_nombre}_{fecha_archivo}.pdf"
            catalogos_existentes = CatalogCache.objects.exclude(pdf_file__icontains='Sin_Precio').order_by('version_number')

        _eliminar_catalogo_mas_antiguo_si_corresponde(catalogos_existentes)

        limpiar_pdfs_huerfanos(sin_precio)

        ultima_version = CatalogCache.objects.order_by('-version_number').first()
        siguiente_version = (ultima_version.version_number + 1) if ultima_version else 1

        nuevo_registro = CatalogCache(version_number=siguiente_version, is_current=False)
        nuevo_registro.pdf_file.save(nombre_archivo, ContentFile(pdf_bytes), save=True)

        _marcar("Guardar archivo PDF y registro CatalogCache (incluye limpieza de catalogos viejos)")
        logger.info(f"[generar_pdf_async] Job {job_id}: catálogo v{siguiente_version} generado correctamente.")
        _actualizar_estado_trabajo(job_id, estado='listo', catalogo_id=nuevo_registro.id)

    except Exception as e:
        logger.exception(f"[generar_pdf_async] Job {job_id} falló")
        _actualizar_estado_trabajo(job_id, estado='error', error=str(e))


@never_cache
@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_superuser)
def iniciar_generacion_pdf(request):
    if request.method != 'POST':
        return redirect('menu_exportar')

    grupos_seleccionados = request.POST.getlist('grupos_seleccionados')
    tipo_catalogo = request.POST.get('tipo_catalogo', 'con_precio')
    modo_filtro = request.POST.get('modo_filtro', 'completo')

    if not grupos_seleccionados:
        messages.error(request, "Debes seleccionar al menos un grupo para generar el catálogo.")
        return redirect('menu_exportar')

    job_id = uuid.uuid4().hex
    with _trabajos_pdf_lock:
        _trabajos_pdf[job_id] = {'estado': 'procesando'}

    hilo = threading.Thread(
        target=_ejecutar_generacion_pdf_en_hilo,
        args=(job_id, request, grupos_seleccionados, tipo_catalogo, modo_filtro),
        daemon=True,
    )
    hilo.start()

    return render(request, 'cargando_pdf.html', {'job_id': job_id})


@never_cache
@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_superuser)
def estado_generacion_pdf(request, job_id):
    with _trabajos_pdf_lock:
        trabajo = _trabajos_pdf.get(job_id)

    if not trabajo:
        return JsonResponse({'estado': 'desconocido'}, status=404)

    return JsonResponse(trabajo)


@never_cache
@login_required(login_url='/login/')
def ver_pdf_generado(request, catalogo_id):
    catalogo = get_object_or_404(CatalogCache, pk=catalogo_id)

    if not catalogo.pdf_file:
        messages.error(request, "El archivo de este catálogo ya no está disponible.")
        return redirect('dashboard')

    with catalogo.pdf_file.open('rb') as f:
        pdf_bytes = f.read()

    nombre_archivo = catalogo.pdf_file.name.split('/')[-1]
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{nombre_archivo}"'
    return response

# ==========================================
# VISTA: GESTIÓN Y REASIGNACIÓN DE GRUPOS (PÁGINA APARTE)
# ==========================================
# NOTA (Claude): esta vista fusiona lo que hacían las dos versiones
# duplicadas que había antes: edita `descripcion` del Producto, y además
# guarda subgrupo_personalizado, orden_grupo, es_destacado y
# etiqueta_destacado, que es exactamente lo que gestionar_grupos.html
# manda en sus campos ocultos y de tabla.
@never_cache
@login_required(login_url='/login/')
@permission_required('prueba.change_producto', login_url='login')
def gestionar_grupos(request):
    if request.method == "POST":
        accion = request.POST.get("accion")

        if accion == "guardar_individual":
            p_id = request.POST.get("producto_id_individual")
            nueva_desc = request.POST.get("nueva_descripcion_individual", "").strip()
            nuevo_grp = request.POST.get("nuevo_grupo_individual", "").strip().upper()
            nuevo_subgrupo = request.POST.get("nuevo_subgrupo_individual", "").strip()
            nuevo_limpio = request.POST.get("nuevo_nombre_limpio_individual", "").strip()
            nuevo_super_grupo = request.POST.get("nuevo_super_grupo_individual", "").strip().upper()
            orden = request.POST.get("orden_grupo_individual", "0").strip()
            es_destacado = request.POST.get("es_destacado_individual") == "1"
            etiqueta = request.POST.get("etiqueta_destacado_individual", "OFERTA").strip()

            if p_id:
                if nueva_desc:
                    Producto.objects.filter(field_id=p_id).update(descripcion=nueva_desc)

                if nuevo_grp or nuevo_subgrupo or nuevo_limpio:
                    subgrupo_val = nuevo_subgrupo if nuevo_subgrupo else nuevo_grp
                    ProductoGrupoManual.objects.update_or_create(
                        producto_id=p_id,
                        defaults={
                            'grupo_personalizado': nuevo_grp,
                            'subgrupo_personalizado': subgrupo_val,
                            'nombre_limpio_personalizado': nuevo_limpio if nuevo_limpio else None
                        }
                    )

                if nuevo_grp:
                    img_obj, _ = ImagenProducto.objects.get_or_create(grupo_nombre=nuevo_grp)
                    img_obj.orden_grupo = int(orden) if orden.isdigit() else 0
                    img_obj.es_destacado = es_destacado
                    img_obj.etiqueta_destacado = etiqueta if etiqueta else "OFERTA"
                    img_obj.super_grupo = nuevo_super_grupo if nuevo_super_grupo else None
                    img_obj.save()

                messages.success(request, f"Producto #{p_id} guardado correctamente.")

        elif accion == "guardar_pagina":
            producto_ids = request.POST.getlist("producto_id[]")
            descripciones = request.POST.getlist("nueva_descripcion[]")
            grupos = request.POST.getlist("nuevo_grupo[]")
            subgrupos = request.POST.getlist("nuevo_subgrupo[]")
            nombres_limpios = request.POST.getlist("nuevo_nombre_limpio[]")
            super_grupos = request.POST.getlist("nuevo_super_grupo[]")
            ordenes = request.POST.getlist("orden_grupo[]")
            destacados_checks = request.POST.getlist("es_destacado[]")
            etiquetas = request.POST.getlist("etiqueta_destacado[]")

            for i, p_id in enumerate(producto_ids):
                if not p_id:
                    continue

                nueva_desc = descripciones[i].strip() if i < len(descripciones) else ""
                nuevo_grp = grupos[i].strip().upper() if i < len(grupos) else ""
                nuevo_subgrupo = subgrupos[i].strip() if i < len(subgrupos) else ""
                nuevo_limpio = nombres_limpios[i].strip() if i < len(nombres_limpios) else ""
                nuevo_super_grupo_val = super_grupos[i].strip().upper() if i < len(super_grupos) else ""
                orden_val = ordenes[i].strip() if i < len(ordenes) else "0"
                etiqueta_val = etiquetas[i].strip() if i < len(etiquetas) else "OFERTA"
                es_dest = str(p_id) in destacados_checks

                if nueva_desc:
                    Producto.objects.filter(field_id=p_id).update(descripcion=nueva_desc)

                if nuevo_grp or nuevo_subgrupo or nuevo_limpio:
                    subgrupo_val = nuevo_subgrupo if nuevo_subgrupo else nuevo_grp
                    ProductoGrupoManual.objects.update_or_create(
                        producto_id=p_id,
                        defaults={
                            'grupo_personalizado': nuevo_grp,
                            'subgrupo_personalizado': subgrupo_val,
                            'nombre_limpio_personalizado': nuevo_limpio if nuevo_limpio else None
                        }
                    )

                if nuevo_grp:
                    img_obj, _ = ImagenProducto.objects.get_or_create(grupo_nombre=nuevo_grp)
                    img_obj.orden_grupo = int(orden_val) if orden_val.isdigit() else 0
                    img_obj.es_destacado = es_dest
                    img_obj.etiqueta_destacado = etiqueta_val if etiqueta_val else "OFERTA"
                    img_obj.super_grupo = nuevo_super_grupo_val if nuevo_super_grupo_val else None
                    img_obj.save()

            messages.success(request, f"Se han guardado y actualizado los {len(producto_ids)} productos de esta página.")

        elif accion == "restaurar_individual":
            prod_id_restaurar = request.POST.get("producto_id_restaurar")
            grupo_restaurar = request.POST.get("grupo_restaurar")
            if prod_id_restaurar:
                ProductoGrupoManual.objects.filter(producto_id=prod_id_restaurar).delete()
            if grupo_restaurar:
                # NOTA (Claude): agregamos super_grupo=None a este reset porque
                # es un atributo del mismo tipo que orden_grupo/es_destacado
                # (metadata del grupo, no del producto individual). Si NO
                # quieres que "Restaurar" borre el Súper Grupo asignado,
                # quita esta línea.
                ImagenProducto.objects.filter(grupo_nombre=grupo_restaurar).update(
                    orden_grupo=0,
                    es_destacado=False,
                    etiqueta_destacado="OFERTA",
                    super_grupo=None
                )
            messages.success(request, f"Producto #{prod_id_restaurar} restaurado a sus valores automáticos.")

        return redirect(request.META.get('HTTP_REFERER', 'gestionar_grupos'))

    texto_busqueda = request.GET.get("q", "").strip()

    productos_qs = VistaProductoVariantes.objects.select_related("proveedor").exclude(
        Q(descripcion__isnull=True) |
        Q(descripcion__exact='') |
        Q(descripcion__startswith='(') |
        Q(descripcion__istartswith='tee') |
        Q(descripcion__regex=r'^.$') |
        Q(proveedor__marca__startswith='*') |
        Q(proveedor__marca__startswith='"') |
        Q(proveedor__marca__iexact='a') |
        Q(proveedor__marca__iexact='KAISER - HEISSNER') |
        Q(proveedor__marca__iexact='HELA') |
        Q(codigo='17-27-105') |
        Q(descripcion__iexact='ANULA FACTURA') |
        Q(descripcion__iexact='BOLSA')
    ).order_by('codigo')

    if texto_busqueda:
        productos_qs = productos_qs.filter(
            Q(descripcion__icontains=texto_busqueda) |
            Q(descripcion_grupo__icontains=texto_busqueda) |
            Q(codigo__icontains=texto_busqueda) |
            Q(proveedor__marca__icontains=texto_busqueda)
        )

    overrides = {
        item.producto_id: item
        for item in ProductoGrupoManual.objects.all()
    }

    imagenes_meta = {
        str(img.grupo_nombre).strip().upper(): img
        for img in ImagenProducto.objects.all()
    }

    grupos_sql = set(
        VistaProductoAgrupado.objects.exclude(descripcion_grupo__isnull=True)
        .exclude(descripcion_grupo__exact="")
        .values_list("descripcion_grupo", flat=True)
    )
    grupos_manuales = set(ProductoGrupoManual.objects.values_list("grupo_personalizado", flat=True))
    todos_los_grupos = sorted(list(grupos_sql.union(grupos_manuales)))

    # NOTA (Claude): lista de Súper Grupos ya usados, para el <datalist>
    # del selector en gestionar_grupos.html. Se guardan en
    # ImagenProducto.super_grupo (NO en ProductoGrupoManual), así que esto
    # no toca ninguna agrupación manual existente.
    todos_los_super_grupos = sorted(
        set(
            ImagenProducto.objects.exclude(super_grupo__isnull=True)
            .exclude(super_grupo__exact="")
            .values_list("super_grupo", flat=True)
        )
    )

    paginator = Paginator(productos_qs, 25)
    page = request.GET.get("page")
    page_obj = paginator.get_page(page)

    for p in page_obj.object_list:
        override_obj = overrides.get(p.id, None)
        p.grupo_manual = bool(override_obj)
        p.grupo_activo = (override_obj.grupo_personalizado if override_obj else None) or p.descripcion_grupo or p.descripcion
        p.subgrupo_activo = (
            override_obj.subgrupo_personalizado
            if (override_obj and override_obj.subgrupo_personalizado)
            else p.grupo_activo
        )

        if override_obj and override_obj.nombre_limpio_personalizado:
            p.nombre_limpio = override_obj.nombre_limpio_personalizado
            p.nombre_limpio_es_manual = True
        else:
            p.nombre_limpio = extraer_medida(p.grupo_activo, p.descripcion or "", p.codigo_de_origen or "")
            p.nombre_limpio_es_manual = False

        meta_grupo = imagenes_meta.get(str(p.grupo_activo).strip().upper())
        p.orden_grupo = meta_grupo.orden_grupo if meta_grupo else 0
        p.es_destacado = meta_grupo.es_destacado if meta_grupo else False
        p.etiqueta_destacado = meta_grupo.etiqueta_destacado if (meta_grupo and meta_grupo.etiqueta_destacado) else "OFERTA"
        p.super_grupo = meta_grupo.super_grupo if (meta_grupo and meta_grupo.super_grupo) else ""

    return render(request, "gestionar_grupos.html", {
        "page_obj": page_obj,
        "productos": page_obj,
        "busqueda": texto_busqueda,
        "todos_los_grupos": todos_los_grupos,
        "todos_los_super_grupos": todos_los_super_grupos,
    })

# ==========================================
# SINCRONIZACIÓN DE PRODUCTOS CON EL ERP
# ==========================================
solo_superusuarios = user_passes_test(lambda u: u.is_superuser)

# Campos que SIEMPRE se muestran como columna propia en la tabla, en este
# orden. El resto de los campos que cambien para un producto se agrupan
# en la columna "Otros cambios", plegados por defecto.
CAMPOS_PRINCIPALES = ["descripcion", "precio_base_pesos", "stock_disponible"]


def _pivotar_cambios(detalle_cambios):
    """
    Convierte [{"id": 123, "cambios": [{"campo":.., "antes":.., "despues":..}]}]
    en filas listas para la tabla: cada producto es una fila, con los campos
    principales ya separados y el resto agrupado en "otros".
    """
    filas = []
    for item in detalle_cambios or []:
        principales = {}
        otros = []
        for c in item.get("cambios", []):
            if c["campo"] in CAMPOS_PRINCIPALES:
                principales[c["campo"]] = c
            else:
                otros.append(c)
        filas.append({
            "id": item.get("id"),
            "principales": principales,
            "otros": otros,
        })
    return filas


@never_cache
@login_required(login_url='/login/')
@solo_superusuarios
def sincronizar_productos(request):
    """Página principal: historial de sincronizaciones + botones de acción."""
    historial = list(SyncLog.objects.order_by("-fecha")[:30])
    for log in historial:
        log.filas_tabla = _pivotar_cambios(log.detalle_cambios)
    return render(request, "sincronizar_productos.html", {
        "historial": historial,
        "campos_principales": CAMPOS_PRINCIPALES,
    })


@never_cache
@login_required(login_url='/login/')
@solo_superusuarios
@require_POST
def sincronizar_productos_ejecutar(request):
    log = ejecutar_sync(dry_run=False)
    _mensaje_resultado_sync(request, log)
    return redirect("sincronizar_productos")


@never_cache
@login_required(login_url='/login/')
@solo_superusuarios
@require_POST
def sincronizar_productos_dry_run(request):
    log = ejecutar_sync(dry_run=True)
    _mensaje_resultado_sync(request, log, prueba=True)
    return redirect("sincronizar_productos")


def _mensaje_resultado_sync(request, log, prueba=False):
    prefijo = "[PRUEBA, no se guardó nada] " if prueba else ""
    if log.estado == "error":
        messages.error(request, f"{prefijo}Sync abortada: {log.detalle}")
    elif log.errores == 0:
        messages.success(
            request,
            f"{prefijo}Sync finalizada sin errores. "
            f"Nuevos: {log.creados} | Actualizados: {log.actualizados}"
        )
    else:
        messages.warning(
            request,
            f"{prefijo}Sync finalizada con errores. "
            f"Nuevos: {log.creados} | Actualizados: {log.actualizados} | Errores: {log.errores}"
        )