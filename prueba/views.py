from collections import OrderedDict
from datetime import datetime, timedelta
import os
import base64
import mimetypes
import tempfile

from django.contrib import messages
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
from django.utils.text import slugify

from playwright.sync_api import sync_playwright
import fitz  # PyMuPDF para la fusión en memoria de los dos pases

from .models import FamiliaProducto, Producto, ImagenProducto, Proveedor, VistaProductoAgrupado, CatalogCache


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
def lista_productos(request):
    familia_seleccionada = request.GET.get("familia", "")
    marca_seleccionada = request.GET.get("marca", "")
    texto_busqueda = request.GET.get("q", "").strip()

    familias = {f.codigo: f for f in FamiliaProducto.objects.all()}

    productos = (
        VistaProductoAgrupado.objects
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
            Q(proveedor__marca__iexact='HELA')
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

    productos = productos.order_by("descripcion")
    grupos = OrderedDict()

    for p in productos:
        grupo = p.descripcion_grupo or p.descripcion
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
                "precio_desde": p.precio_base_pesos,
                "unidad_medida": p.unidad_medida,
                "productos": [],
            }

        grupos[grupo]["productos"].append(p)
        precio = p.precio_base_pesos

        if precio is not None:
            actual = grupos[grupo]["precio_desde"]
            if actual is None or precio < actual:
                grupos[grupo]["precio_desde"] = precio

    lista_grupos = list(grupos.values())
    
    nombres_grupos = [g["nombre"] for g in lista_grupos]
    imagenes_dict = {img.grupo_nombre: img.imagen.url for img in ImagenProducto.objects.filter(grupo_nombre__in=nombres_grupos) if img.imagen}

    for g in lista_grupos:
        g["imagen_url"] = imagenes_dict.get(g["nombre"], None)
        g["cantidad"] = len(g["productos"])

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


# ==========================================
# VISTA: DETALLE INDEPENDIENTE
# ==========================================
def detalle_producto(request, producto_id):
    producto_base = get_object_or_404(VistaProductoAgrupado.objects.select_related("proveedor"), id=producto_id)

    nombre_grupo = producto_base.descripcion_grupo or producto_base.descripcion
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

    variantes = VistaProductoAgrupado.objects.select_related("proveedor").exclude(
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
    ).filter(
        Q(descripcion_grupo=nombre_grupo) | 
        Q(descripcion=nombre_grupo, descripcion_grupo__isnull=True) | 
        Q(descripcion=nombre_grupo, descripcion_grupo=""),
        proveedor__marca=marca_grupo
    )

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
        Q(proveedor__marca__iexact='HELA')
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

    nombres_grupos = [p.descripcion_grupo or p.descripcion for p in page_obj.object_list]
    info_grupos_qs = ImagenProducto.objects.filter(grupo_nombre__in=nombres_grupos)
    
    imagenes_dict = {img.grupo_nombre: img.imagen.url for img in info_grupos_qs if img.imagen}
    descripciones_dict = {img.grupo_nombre: img.descripcion for img in info_grupos_qs if img.descripcion}

    for p in page_obj.object_list:
        grupo_nombre = p.descripcion_grupo or p.descripcion
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

@permission_required('prueba.change_producto', login_url='login')
def editar_producto(request, producto_id):
    if request.method == "POST":
        precio = request.POST.get("precio_base_pesos")
        stock = request.POST.get("stock_disponible")
        ruta_imagen = request.POST.get("ruta_imagen_producto", "").strip()
        grupo_nombre = request.POST.get("grupo_nombre")
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

    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))


def logout_view(request):
    logout(request)
    return redirect('login')


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
        Q(proveedor__marca__iexact='HELA')
    ).order_by('descripcion_grupo')
    
    familias_dict = {f.codigo: f.descripcion for f in FamiliaProducto.objects.all()}
    arbol_familias = {}

    for p in productos:
        familia_desc = "Sin Familia"
        if p.codigo and "-" in p.codigo:
            partes = p.codigo.split("-")
            if len(partes) >= 2:
                familia_desc = familias_dict.get(partes[1], "Sin Familia")

        grupo = p.descripcion_grupo or p.descripcion
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


@login_required(login_url='/login/')
def eliminar_catalogo(request, catalogo_id):
    if not request.user.is_superuser:
        raise PermissionDenied

    catalogo = get_object_or_404(CatalogCache, pk=catalogo_id)
    if catalogo.pdf_file:
        catalogo.pdf_file.delete(save=False)

    catalogo.delete()
    messages.success(request, f"La versión {catalogo.version_number} del catálogo y su archivo PDF fueron eliminados para liberar espacio.")
    return redirect('historial_catalogo')


# ==========================================
# GESTIÓN DE VIGENCIA Y BLOQUEO DE CATÁLOGOS
# ==========================================

@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_superuser)
def marcar_catalogo_vigente(request, catalogo_id):
    catalogo = get_object_or_404(CatalogCache, id=catalogo_id)
    
    # Identificar si el catálogo a marcar es "Sin Precio" o "Con Precio"
    is_sin_precio = 'Sin_Precio' in catalogo.pdf_file.name
    
    # Desmarcar los otros catálogos de la misma modalidad
    if is_sin_precio:
        CatalogCache.objects.filter(pdf_file__icontains='Sin_Precio').update(is_current=False)
    else:
        CatalogCache.objects.exclude(pdf_file__icontains='Sin_Precio').update(is_current=False)
    
    # Marcar el catálogo seleccionado como el vigente
    catalogo.is_current = True
    catalogo.save()

    messages.success(request, f"Se ha fijado el catálogo Versión {catalogo.version_number} como la versión vigente oficial.")
    return redirect('historial_catalogo')


# ==========================================
# GENERACIÓN PDF (PLAYWRIGHT EN DOS PASES + PYMUPDF)
# ==========================================

@login_required(login_url='/login/')
@user_passes_test(lambda u: u.is_superuser)
def generar_pdf(request):
    if request.method == 'POST':
        grupos_seleccionados = request.POST.getlist('grupos_seleccionados')
        tipo_catalogo = request.POST.get('tipo_catalogo', 'con_precio')
        sin_precio = (tipo_catalogo == 'sin_precio')

        # 1. FECHA DINÁMICA DE HOY EN ESPAÑOL
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

        qs = VistaProductoAgrupado.objects.select_related("proveedor").filter(
            descripcion_grupo__in=grupos_seleccionados
        ).annotate(
            es_truper=Case(
                When(proveedor__marca__iexact='truper', then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )

        productos = list(qs)
        familias_dict = {f.codigo: f.descripcion for f in FamiliaProducto.objects.all()}

        for p in productos:
            p.familia_temporal = "Sin Familia"
            if p.codigo and "-" in p.codigo:
                partes = p.codigo.split("-")
                if len(partes) >= 2:
                    p.familia_temporal = familias_dict.get(partes[1], "Sin Familia")

        productos.sort(key=lambda p: (
            p.es_truper,
            p.familia_temporal,
            p.descripcion_grupo or p.descripcion or ""
        ))

        
        imagenes_dict = {
            # Usamos .name para pasar solo la ruta relativa (ej: 'productos/foto.jpg')
            img.grupo_nombre: obtener_base64_imagen(img.imagen.name)
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
            grupo = p.descripcion_grupo or p.descripcion

            familias_de_marca = catalogo[marca_grupo]
            if familia not in familias_de_marca:
                familias_de_marca[familia] = OrderedDict()

            if grupo not in familias_de_marca[familia]:
                familias_de_marca[familia][grupo] = {
                    'imagen_url': imagenes_dict.get(grupo, None),
                    'descripcion': descripciones_dict.get(grupo, ""),
                    'variantes': []
                }

            familias_de_marca[familia][grupo]['variantes'].append(p)

        catalogo = OrderedDict((k, v) for k, v in catalogo.items() if v)

        for familias_de_marca in catalogo.values():
            for familia, grupos in list(familias_de_marca.items()):
                for info in grupos.values():
                    info['es_ancha'] = len(info['variantes']) > UMBRAL_VARIANTES_TARJETA_ANCHA

                familias_de_marca[familia] = OrderedDict(
                    sorted(grupos.items(), key=lambda item: (item[1]['es_ancha'], item[0]))
                )

        # 2. CATEGORÍA DINÁMICA DE LOS PRODUCTOS SELECCIONADOS
        categoria_post = request.POST.get('categoria', '').strip()
        if categoria_post:
            categoria_general = categoria_post
        else:
            familias_presentes = []
            for familias_de_marca in catalogo.values():
                for fam in familias_de_marca.keys():
                    if fam and fam != "Sin Familia" and fam not in familias_presentes:
                        familias_presentes.append(fam)
            
            if familias_presentes:
                categoria_general = ", ".join(familias_presentes[:2])
            else:
                categoria_general = "General"

        logo_base64 = obtener_base64_imagen('static/img/logo_ecosa.png')
        portada_base64 = obtener_base64_imagen('static/img/portada.png')

        # 3. RENDERIZADO EN DOS PASES SEPARADOS
        html_inicio = render_to_string('catalogo_pdf.html', {
            'catalogo': catalogo,
            'request': request,
            'logo_base64': logo_base64,
            'portada_base64': portada_base64,
            'sin_precio': sin_precio,
            'seccion': 'inicio',
        })

        html_productos = render_to_string('catalogo_pdf.html', {
            'catalogo': catalogo,
            'request': request,
            'logo_base64': logo_base64,
            'portada_base64': portada_base64,
            'sin_precio': sin_precio,
            'seccion': 'productos',
        })

        # PLANTILLAS PLAYWRIGHT PARA EL PASO 2 (PRODUCTOS)
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
                <span style="color: #D67A00; font-weight: bold;">{codigo_catalogo}</span>
                <span style="color: #000000; margin-left: 4px;">{categoria_general}</span>
            </div>
            <div style="flex: 1; text-align: right; color: #444444;">
                Página <span class="pageNumber"></span> de <span class="totalPages"></span>
            </div>
        </div>
        """

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu'
                ]
            )
            try:
                # -------------------------------------------------------------
                # PASO 1: PORTADA E ÍNDICE (0mm margen, SIN HEADER NI FOOTER)
                # -------------------------------------------------------------
                page_inicio = browser.new_page()
                page_inicio.set_content(html_inicio, wait_until="load", timeout=120000)
                pdf_bytes_inicio = page_inicio.pdf(
                    format="Letter",
                    print_background=True,
                    prefer_css_page_size=True,
                    display_header_footer=False,
                    margin={"top": "0mm", "bottom": "0mm", "left": "0mm", "right": "0mm"}
                )
                page_inicio.close()

                # -------------------------------------------------------------
                # PASO 2: PRODUCTOS (28mm MARGEN SUPERIOR PARA CABECERA + FOOTER)
                # -------------------------------------------------------------
                page_productos = browser.new_page()
                page_productos.set_content(html_productos, wait_until="load", timeout=120000)

                page_productos.evaluate("""() => {
                    const PAGE_HEIGHT_PX = 1056; 
                    const sections = document.querySelectorAll('.seccion-familia');
                    sections.forEach(sec => {
                        const id = sec.getAttribute('id');
                        if (id) {
                            const rect = sec.getBoundingClientRect();
                            const top = rect.top + window.scrollY;
                            const pageNum = Math.floor(top / PAGE_HEIGHT_PX) + 1;
                            const targetSpans = document.querySelectorAll(`[data-target-page="${id}"]`);
                            targetSpans.forEach(span => {
                                span.textContent = pageNum;
                            });
                        }
                    });
                }""")

                pdf_bytes_productos = page_productos.pdf(
                    format="Letter",
                    print_background=True,
                    prefer_css_page_size=True,
                    display_header_footer=True,
                    header_template=header_template,
                    footer_template=footer_template,
                    margin={
                        "top": "28mm",     # RESERVA ~105PX PARA EL LOGO ECOSA
                        "bottom": "18mm",  # RESERVA ESPACIO FOOTER
                        "left": "10mm",
                        "right": "10mm"
                    }
                )
                page_productos.close()

                # FUSIÓN EN MEMORIA CON PYMUPDF
                doc_inicio = fitz.open(stream=pdf_bytes_inicio, filetype="pdf")
                doc_productos = fitz.open(stream=pdf_bytes_productos, filetype="pdf")

                doc_final = fitz.open()
                doc_final.insert_pdf(doc_inicio)
                doc_final.insert_pdf(doc_productos)

                pdf_bytes = doc_final.write()

                doc_inicio.close()
                doc_productos.close()
                doc_final.close()

            finally:
                browser.close()

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
                catalogo_mas_antiguo.pdf_file.delete(save=False)
            catalogo_mas_antiguo.delete()
            messages.warning(request, f"Se ha eliminado el catálogo {texto_tipo} más antiguo para liberar espacio.")

        # Guardar en el historial pero con is_current=False para no alterar la versión vigente fijada manualmente
        ultima_version = CatalogCache.objects.order_by('-version_number').first()
        siguiente_version = (ultima_version.version_number + 1) if ultima_version else 1

        nuevo_registro = CatalogCache(version_number=siguiente_version, is_current=False)
        nuevo_registro.pdf_file.save(nombre_archivo, ContentFile(pdf_bytes), save=True)

        messages.success(request, f"Catálogo {texto_tipo} generado exitosamente (Versión {siguiente_version}). Recuerda marcarlo como vigente en el historial si deseas asignarlo como oficial.")

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{nombre_archivo}"'
        return response

    return redirect('dashboard')