from collections import OrderedDict
from datetime import datetime, timedelta
import os
import base64
import mimetypes
import tempfile
import re

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import permission_required, login_required, user_passes_test
from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, FileResponse
from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from django.db.models import Case, When, Value, IntegerField, Q
from django.core.exceptions import PermissionDenied
from django.conf import settings
from django.templatetags.static import static
import difflib
from playwright.sync_api import sync_playwright
import fitz  # PyMuPDF
from django.contrib import messages
from django.views.decorators.cache import never_cache
from django.utils.text import slugify

# AQUÍ IMPORTAMOS EL NUEVO MODELO VistaProductoVariantes Y ProductoGrupoManual
from .models import FamiliaProducto, Producto, ImagenProducto, Proveedor, VistaProductoAgrupado, CatalogCache, VistaProductoVariantes, ProductoGrupoManual

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

import re
import difflib

def extraer_medida(nombre_grupo: str, descripcion_variante: str, codigo_de_origen: str = "") -> str:
    if not nombre_grupo or not descripcion_variante:
        return "--"

    nombre_grupo_str = str(nombre_grupo).strip().upper()
    descripcion_limpia = str(descripcion_variante).strip().upper()

    # 1. DESTRUCTOR DE PUNTOS SUSPENSIVOS
    descripcion_limpia = descripcion_limpia.replace('…', ' ').replace('...', ' ')
    nombre_grupo_str = nombre_grupo_str.replace('…', ' ').replace('...', ' ')

    # 🔥 2. SEPARADOR INTELIGENTE: Despega números y comillas de las letras
    # Convierte 'ESTAND.18"' -> 'ESTAND. 18"' para que Python pueda leer el número solo
    descripcion_limpia = re.sub(r'([A-Z\.])(\d)', r'\1 \2', descripcion_limpia)
    # Convierte '18"MANGO' -> '18" MANGO'
    descripcion_limpia = re.sub(r'(\d|")([A-Z])', r'\1 \2', descripcion_limpia)

    if codigo_de_origen:
        codigo_de_origen = str(codigo_de_origen).strip().upper()
        if codigo_de_origen and descripcion_limpia.startswith(codigo_de_origen):
            descripcion_limpia = descripcion_limpia[len(codigo_de_origen):].strip()

    # Quitar códigos numéricos internos iniciales (ej: 15885, 12350)
    descripcion_limpia = re.sub(r'^\d{3,7}\s+', '', descripcion_limpia)

    # Quitar basura entre paréntesis ej: (M.6)
    descripcion_limpia = re.sub(r'\s*\([^)]*\)', '', descripcion_limpia)

    # ---------------------------------------------------------
    # 🔥 FASE 1: FRANCOTIRADOR DE MEDIDAS (Regex Prioritario)
    # ---------------------------------------------------------
    patron_medidas = r'(?<!\d)\d+(?:/\d+)?\s*(?:"|MM|CM|M|OZ|KG|GR|PULG|LB|LT|L|ML|GAL|W|V|A|HP|DTES\.?|DIENTES)(?!\w)'
    
    medidas_grupo = set(re.findall(patron_medidas, nombre_grupo_str))
    
    medidas_variante = []
    for match in re.finditer(patron_medidas, descripcion_limpia):
        m = match.group().strip()
        if m not in medidas_grupo and m not in medidas_variante:
            medidas_variante.append(m)
    
    if medidas_variante:
        return " ".join(medidas_variante)

    # ---------------------------------------------------------
    # 🔥 FASE 2: DICCIONARIO Y RESTA (Si NO es un producto de medida numérica)
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

# ==========================================
# VISTA: LISTA DE PRODUCTOS (CATÁLOGO PÚBLICO)
# ==========================================
@never_cache
def lista_productos(request):
    familia_seleccionada = request.GET.get("familia", "").strip()
    marca_seleccionada = request.GET.get("marca", "").strip()
    texto_busqueda = request.GET.get("q", "").strip()

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
            Q(descripcion__startswith='*') |
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
        productos = productos.filter(
            Q(descripcion__icontains=texto_busqueda) |
            Q(descripcion_grupo__icontains=texto_busqueda) |
            Q(codigo__icontains=texto_busqueda) |
            Q(proveedor__marca__icontains=texto_busqueda)
        )

    productos = productos.order_by("es_truper", "codigo")

    # Mapeo de asignaciones manuales de grupo
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

    for g in lista_grupos:
        nombre_limpio = str(g["nombre"]).strip().upper()
        g["imagen_url"] = imagenes_dict.get(nombre_limpio, None)

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

    paginator = Paginator(lista_grupos, 12)
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
        },
    )

@never_cache
def detalle_producto(request, producto_id):
    producto_base = get_object_or_404(VistaProductoAgrupado.objects.select_related("proveedor"), id=producto_id)

    # Revisamos si tiene asignación manual
    override_obj = ProductoGrupoManual.objects.filter(producto_id=producto_base.id).first()
    nombre_grupo = override_obj.grupo_personalizado if override_obj else (producto_base.descripcion_grupo or producto_base.descripcion)
    
    marca_grupo = producto_base.proveedor.marca if producto_base.proveedor else ""
    
    info_grupo = ImagenProducto.objects.filter(grupo_nombre=nombre_grupo).first()
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
        Q(descripcion__startswith='*') | 
        Q(descripcion__startswith='(') | 
        Q(descripcion__istartswith='tee') | 
        Q(descripcion__regex=r'^.$') | 
        Q(proveedor__marca__startswith='*') | 
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
        variantes = variantes_qs.filter(filtros_grupo, proveedor__marca__iexact=marca_grupo).order_by('codigo')
    else:
        variantes = variantes_qs.filter(filtros_grupo).order_by('codigo')

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

    productos_base_qs = VistaProductoAgrupado.objects.exclude(
        Q(descripcion__isnull=True) | 
        Q(descripcion__exact='') | 
        Q(descripcion__startswith='*') | 
        Q(descripcion__startswith='(') | 
        Q(descripcion__istartswith='tee') | 
        Q(descripcion__regex=r'^.$') | 
        Q(proveedor__marca__startswith='*') | 
        Q(proveedor__marca__startswith='"') | 
        Q(proveedor__marca__iexact='a') | 
        Q(proveedor__marca__iexact='KAISER - HEISSNER') | 
        Q(proveedor__marca__iexact='HELA') | 
        Q(codigo='17-27-105')
    )

    kpi_productos_activos = productos_base_qs.count()
    kpi_familias_activas = FamiliaProducto.objects.count()
    kpi_proveedores = productos_base_qs.values("proveedor__marca").distinct().exclude(proveedor__marca="").count()

    hace_seis_meses = datetime.now() - timedelta(days=180)
    try:
        kpi_nuevos_6_meses = productos_base_qs.filter(fecha_creacion__gte=hace_seis_meses).count()
    except Exception:
        kpi_nuevos_6_meses = 0

    productos_qs = productos_base_qs.select_related("proveedor").order_by("descripcion")

    if texto_busqueda:
        productos_qs = productos_qs.filter(
            Q(descripcion__icontains=texto_busqueda) |
            Q(codigo__icontains=texto_busqueda) |
            Q(proveedor__marca__icontains=texto_busqueda)
        )

    paginator = Paginator(productos_qs, 20)
    page = request.GET.get("page")
    page_obj = paginator.get_page(page)

    overrides_dict = {
        item.producto_id: item.grupo_personalizado
        for item in ProductoGrupoManual.objects.all()
    }

    nombres_grupos = [overrides_dict.get(p.id, p.descripcion_grupo or p.descripcion) for p in page_obj.object_list]
    info_grupos_qs = ImagenProducto.objects.filter(grupo_nombre__in=nombres_grupos)
    
    imagenes_dict = {img.grupo_nombre: img.imagen.url for img in info_grupos_qs if img.imagen}
    descripciones_dict = {img.grupo_nombre: img.descripcion for img in info_grupos_qs if img.descripcion}

    for p in page_obj.object_list:
        grupo_nombre = overrides_dict.get(p.id, p.descripcion_grupo or p.descripcion)
        p.imagen_url = imagenes_dict.get(grupo_nombre, None)
        p.descripcion_grupo = descripciones_dict.get(grupo_nombre, "")
        p.grupo_nombre = grupo_nombre
        p.field_id = p.id

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
            "kpi_familias_activas": kpi_familias_activas,
            "kpi_proveedores": kpi_proveedores,
            "kpi_nuevos_6_meses": kpi_nuevos_6_meses,
            "catalogo_vigente_con_precio": catalogo_vigente_con_precio,
            "catalogo_vigente_sin_precio": catalogo_vigente_sin_precio,
        }
    )


# ==========================================
# OTRAS VISTAS DEL SISTEMA
# ==========================================
@never_cache
@permission_required('prueba.change_producto', login_url='login')
def editar_producto(request, producto_id):
    if request.method == "POST":
        precio = request.POST.get("precio_base_pesos")
        stock = request.POST.get("stock_disponible")
        ruta_imagen = request.POST.get("ruta_imagen_producto", "").strip()
        grupo_nombre = request.POST.get("grupo_nombre", "").strip().upper()
        descripcion_grupo = request.POST.get("descripcion_grupo")

        try:
            precio_float = float(precio) if precio else None
            stock_float = float(stock) if stock else None

            Producto.objects.filter(field_id=producto_id).update(
                precio_base_pesos=precio_float,
                stock_disponible=stock_float
            )

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
@user_passes_test(es_admin)
def menu_exportar(request):
    productos = VistaProductoAgrupado.objects.exclude(
        Q(descripcion__isnull=True) | 
        Q(descripcion__exact='') | 
        Q(descripcion__startswith='*') | 
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
    
    familias_dict = {f.codigo: f.descripcion for f in FamiliaProducto.objects.all()}
    overrides_dict = {
        item.producto_id: item.grupo_personalizado
        for item in ProductoGrupoManual.objects.all()
    }
    
    arbol_familias = {}

    for p in productos:
        familia_desc = "Sin Familia"
        if p.codigo and "-" in p.codigo:
            partes = p.codigo.split("-")
            if len(partes) >= 2:
                familia_desc = familias_dict.get(partes[1], "Sin Familia")

        grupo = overrides_dict.get(p.id, p.descripcion_grupo or p.descripcion)
        if familia_desc not in arbol_familias:
            arbol_familias[familia_desc] = set()
        arbol_familias[familia_desc].add(grupo)

    for f in arbol_familias:
        arbol_familias[f] = sorted(list(arbol_familias[f]))

    arbol_familias = dict(sorted(arbol_familias.items()))
    cantidad_catalogos = CatalogCache.objects.count()

    return render(request, 'exportar.html', {
        'arbol_familias': arbol_familias,
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

# ==========================================
# GENERACIÓN DE PDF
# ==========================================
@never_cache
@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_superuser)
def generar_pdf(request):
    if request.method == 'POST':
        grupos_seleccionados = request.POST.getlist('grupos_seleccionados')
        tipo_catalogo = request.POST.get('tipo_catalogo', 'con_precio')
        sin_precio = (tipo_catalogo == 'sin_precio')

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

        # 1. Obtenemos el diccionario completo de reasignaciones manuales
        overrides_dict = {
            item.producto_id: item
            for item in ProductoGrupoManual.objects.all()
        }

        # IDs que fueron reasignados manualmente a alguno de los grupos seleccionados
        ids_con_override = [
            pid for pid, item in overrides_dict.items()
            if item.grupo_personalizado in grupos_seleccionados
        ]

        # 2. Consultamos tanto por el grupo SQL como por los productos reasignados manualmente
        qs = VistaProductoVariantes.objects.select_related("proveedor").filter(
            Q(descripcion_grupo__in=grupos_seleccionados) | Q(id__in=ids_con_override)
        ).annotate(
            es_truper=Case(
                When(proveedor__marca__iexact='truper', then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )

        productos_raw = list(qs)
        familias_dict = {f.codigo: f.descripcion for f in FamiliaProducto.objects.all()}

        productos = []
        for p in productos_raw:
            override_item = overrides_dict.get(p.id)
            
            # Determinamos el grupo final del producto (manual o automático)
            grupo_final = override_item.grupo_personalizado if override_item else (p.descripcion_grupo or p.descripcion)

            # Si el producto fue movido a otro grupo que NO está en grupos_seleccionados, lo descartamos
            if grupo_final not in grupos_seleccionados:
                continue

            p.grupo_final = grupo_final

            p.familia_temporal = "Sin Familia"
            if p.codigo and "-" in p.codigo:
                partes = p.codigo.split("-")
                if len(partes) >= 2:
                    p.familia_temporal = familias_dict.get(partes[1], "Sin Familia")

            # Asignamos la medida: si fue editada a mano usamos esa, sino usamos extraer_medida
            if override_item and override_item.nombre_limpio_personalizado:
                p.medida_mostrar = override_item.nombre_limpio_personalizado
            else:
                p.medida_mostrar = extraer_medida(p.grupo_final, p.descripcion or "", p.codigo_de_origen or "")

            productos.append(p)

        # 3. Ordenamos respetando el grupo final
        productos.sort(key=lambda p: (
            p.es_truper,
            p.familia_temporal,
            p.grupo_final or "",
            p.codigo
        ))

        imagenes_dict = {
            str(img.grupo_nombre).strip().upper(): obtener_base64_imagen(img.imagen.name)
            for img in ImagenProducto.objects.all() if img.imagen
        }

        descripciones_dict = {img.grupo_nombre: img.descripcion for img in ImagenProducto.objects.all() if img.descripcion}

        UMBRAL_VARIANTES_TARJETA_ANCHA = 8
        catalogo = OrderedDict()
        catalogo["Truper"] = OrderedDict()
        catalogo["Otras Marcas"] = OrderedDict()

        for p in productos:
            marca_grupo = "Truper" if p.es_truper == 0 else "Otras Marcas"
            familia = p.familia_temporal
            grupo = p.grupo_final

            familias_de_marca = catalogo[marca_grupo]
            if familia not in familias_de_marca:
                familias_de_marca[familia] = OrderedDict()

            if grupo not in familias_de_marca[familia]:
                familias_de_marca[familia][grupo] = {
                    'imagen_url': imagenes_dict.get(grupo, None),
                    'descripcion': descripciones_dict.get(grupo, ""),
                    'variantes': []
                }

            if p.es_truper != 0:
                p.empaque_inner = None

            familias_de_marca[familia][grupo]['variantes'].append(p)

        catalogo = OrderedDict((k, v) for k, v in catalogo.items() if v)

        for familias_de_marca in catalogo.values():
            for familia, grupos in list(familias_de_marca.items()):
                for info in grupos.values():
                    info['es_ancha'] = len(info['variantes']) > UMBRAL_VARIANTES_TARJETA_ANCHA
                    info['tiene_empaque_inner'] = any(
                        v.empaque_inner for v in info['variantes']
                    )

                familias_de_marca[familia] = OrderedDict(
                    sorted(grupos.items(), key=lambda item: (item[1]['es_ancha'], item[0]))
                )

        logo_base64 = obtener_base64_imagen('static/img/logo_ecosa.png')
        portada_base64 = obtener_base64_imagen('static/img/portada.png')

        html_productos = render_to_string('catalogo_pdf.html', {
            'catalogo': catalogo,
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
                padding: 0 10mm;
                display: flex;
                align-items: center;
                justify-content: flex-start;
                box-sizing: border-box;
            }}
        </style>
        <div class="header-box">
            {"<img src='" + logo_base64 + "' style='height: 12mm; width: auto;' />" if logo_base64 else ""}
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
                        '--js-flags=--max-old-space-size=4096'
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
                        margin={"top": "28mm", "bottom": "18mm", "left": "10mm", "right": "10mm"}
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

        if sin_precio:
            nombre_archivo = f"Catalogo_Ecosa_Sin_Precio_{fecha_archivo}.pdf"
            catalogos_existentes = CatalogCache.objects.filter(pdf_file__icontains='Sin_Precio').order_by('version_number')
            texto_tipo = "sin precio"
        else:
            nombre_archivo = f"Catalogo_Ecosa_{fecha_archivo}.pdf"
            catalogos_existentes = CatalogCache.objects.exclude(pdf_file__icontains='Sin_Precio').order_by('version_number')
            texto_tipo = "con precio"

        if catalogos_existentes.count() >= 3:
            catalogo_mas_antiguo = catalogos_existentes.first()
            if catalogo_mas_antiguo.pdf_file:
                ruta_fisica = catalogo_mas_antiguo.pdf_file.path
                catalogo_mas_antiguo.pdf_file.delete(save=False)
                if os.path.exists(ruta_fisica):
                    logger.warning(f"[generar_pdf] El archivo {ruta_fisica} no se eliminó del disco (delete silencioso).")
            catalogo_mas_antiguo.delete()
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

import logging
logger = logging.getLogger(__name__)

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

# ==========================================
# VISTA: GESTIÓN Y REASIGNACIÓN DE GRUPOS (PÁGINA APARTE)
# ==========================================
@never_cache
@login_required(login_url='/login/')
@permission_required('prueba.change_producto', login_url='login')
def gestionar_grupos(request):
    if request.method == "POST":
        accion = request.POST.get("accion")

        # ----------------------------------------------------
        # 1. GUARDAR UN SOLO PRODUCTO (INDIVIDUAL)
        # ----------------------------------------------------
        if accion == "guardar_individual":
            p_id = request.POST.get("producto_id_individual")
            nueva_desc = request.POST.get("nueva_descripcion_individual", "").strip()
            nuevo_grp = request.POST.get("nuevo_grupo_individual", "").strip().upper()
            nuevo_limpio = request.POST.get("nuevo_nombre_limpio_individual", "").strip()

            if p_id:
                if nueva_desc:
                    Producto.objects.filter(field_id=p_id).update(descripcion=nueva_desc)

                if nuevo_grp or nuevo_limpio:
                    ProductoGrupoManual.objects.update_or_create(
                        producto_id=p_id,
                        defaults={
                            'grupo_personalizado': nuevo_grp,
                            'nombre_limpio_personalizado': nuevo_limpio if nuevo_limpio else None
                        }
                    )
                messages.success(request, f"Producto #{p_id} guardado correctamente.")

        # ----------------------------------------------------
        # 2. GUARDAR TODA LA PÁGINA (MASIVO)
        # ----------------------------------------------------
        elif accion == "guardar_pagina":
            producto_ids = request.POST.getlist("producto_id[]")
            descripciones = request.POST.getlist("nueva_descripcion[]")
            grupos = request.POST.getlist("nuevo_grupo[]")
            nombres_limpios = request.POST.getlist("nuevo_nombre_limpio[]")

            for i, p_id in enumerate(producto_ids):
                if not p_id:
                    continue

                nueva_desc = descripciones[i].strip() if i < len(descripciones) else ""
                nuevo_grp = grupos[i].strip().upper() if i < len(grupos) else ""
                nuevo_limpio = nombres_limpios[i].strip() if i < len(nombres_limpios) else ""

                if nueva_desc:
                    Producto.objects.filter(field_id=p_id).update(descripcion=nueva_desc)

                if nuevo_grp or nuevo_limpio:
                    ProductoGrupoManual.objects.update_or_create(
                        producto_id=p_id,
                        defaults={
                            'grupo_personalizado': nuevo_grp,
                            'nombre_limpio_personalizado': nuevo_limpio if nuevo_limpio else None
                        }
                    )

            messages.success(request, f"Se han guardado y actualizado los {len(producto_ids)} productos de esta página.")

        # ----------------------------------------------------
        # 3. RESTAURAR INDIVIDUAL
        # ----------------------------------------------------
        elif accion == "restaurar_individual":
            prod_id_restaurar = request.POST.get("producto_id_restaurar")
            if prod_id_restaurar:
                ProductoGrupoManual.objects.filter(producto_id=prod_id_restaurar).delete()
                messages.success(request, f"Producto #{prod_id_restaurar} restaurado a sus valores automáticos.")

        return redirect(request.META.get('HTTP_REFERER', 'gestionar_grupos'))

    texto_busqueda = request.GET.get("q", "").strip()

    productos_qs = VistaProductoVariantes.objects.select_related("proveedor").exclude(
        Q(descripcion__isnull=True) |
        Q(descripcion__exact='') |
        Q(descripcion__startswith='*') |
        Q(descripcion__startswith='(') |
        Q(descripcion__istartswith='tee') |
        Q(descripcion__regex=r'^.$') |
        Q(proveedor__marca__startswith='*') |
        Q(proveedor__marca__startswith='"') |
        Q(proveedor__marca__iexact='a') |
        Q(proveedor__marca__iexact='KAISER - HEISSNER') |
        Q(proveedor__marca__iexact='HELA') |
        Q(codigo='17-27-105')
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

    grupos_sql = set(
        VistaProductoAgrupado.objects.exclude(descripcion_grupo__isnull=True)
        .exclude(descripcion_grupo__exact="")
        .values_list("descripcion_grupo", flat=True)
    )
    grupos_manuales = set(ProductoGrupoManual.objects.values_list("grupo_personalizado", flat=True))
    todos_los_grupos = sorted(list(grupos_sql.union(grupos_manuales)))

    paginator = Paginator(productos_qs, 25)
    page = request.GET.get("page")
    page_obj = paginator.get_page(page)

    for p in page_obj.object_list:
        override_obj = overrides.get(p.id, None)
        p.grupo_manual = override_obj.grupo_personalizado if override_obj else None
        p.grupo_activo = p.grupo_manual or p.descripcion_grupo or p.descripcion
        
        if override_obj and override_obj.nombre_limpio_personalizado:
            p.nombre_limpio = override_obj.nombre_limpio_personalizado
            p.nombre_limpio_es_manual = True
        else:
            p.nombre_limpio = extraer_medida(p.grupo_activo, p.descripcion or "", p.codigo_de_origen or "")
            p.nombre_limpio_es_manual = False

    return render(request, "gestionar_grupos.html", {
        "page_obj": page_obj,
        "productos": page_obj,
        "busqueda": texto_busqueda,
        "todos_los_grupos": todos_los_grupos,
    })