from collections import OrderedDict
from datetime import datetime, timedelta

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
from django.urls import reverse

import weasyprint
from weasyprint import HTML

from .models import FamiliaProducto, Producto, ImagenProducto, Proveedor, VistaProductoAgrupado, CatalogCache


# ==========================================
# VISTA: LISTA DE PRODUCTOS (CATÁLOGO PÚBLICO)
# ==========================================
def lista_productos(request):
    familia_seleccionada = request.GET.get("familia", "")
    marca_seleccionada = request.GET.get("marca", "")
    texto_busqueda = request.GET.get("q", "").strip()

    familias = {f.codigo: f for f in FamiliaProducto.objects.all()}

    # 1. OPTIMIZACIÓN MÁXIMA: Filtramos en Base de Datos (PostgreSQL), NO en RAM.
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
        # Filtramos por el código que contenga la familia seleccionada
        productos = productos.filter(codigo__icontains=f"-{familia_seleccionada}")

    if texto_busqueda:
        # Uso de Q objects para búsquedas complejas directas en BD
        productos = productos.filter(
            Q(descripcion__icontains=texto_busqueda) |
            Q(descripcion_grupo__icontains=texto_busqueda) |
            Q(codigo__icontains=texto_busqueda) |
            Q(proveedor__marca__icontains=texto_busqueda)
        )

    # Ordenamos después de filtrar para ser más eficientes
    productos = productos.order_by("descripcion")

    grupos = OrderedDict()

    # Ahora este bucle procesa solo una fracción mínima de los datos
    for p in productos:
        grupo = p.descripcion_grupo or p.descripcion
        familia = None

        if p.codigo:
            partes = p.codigo.split("-")
            if len(partes) >= 2:
                familia = familias.get(partes[1])
                # Validación extra por si el filtro SQL trajo algo similar pero no exacto
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
    
    # 2. OPTIMIZACIÓN: Solo traemos las imágenes de los grupos que realmente existen en el resultado
    nombres_grupos = [g["nombre"] for g in lista_grupos]
    imagenes_dict = {img.grupo_nombre: img.imagen.url for img in ImagenProducto.objects.filter(grupo_nombre__in=nombres_grupos) if img.imagen}

    for g in lista_grupos:
        g["imagen_url"] = imagenes_dict.get(g["nombre"], None)
        g["cantidad"] = len(g["productos"])

    # Generación de Sidebar Dinámico
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

    # 3. OPTIMIZACIÓN: Dejamos de iterar sobre TODOS los productos. PostgreSQL hace la búsqueda.
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

    # Paginamos ANTES de extraer atributos, así solo procesamos 20 a la vez.
    paginator = Paginator(productos_qs, 20)
    page = request.GET.get("page")
    page_obj = paginator.get_page(page)

    # Procesamos imágenes SOLO para los 20 productos de la página actual
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

    return render(
        request,
        "dashboard.html",
        {
            "productos": page_obj,
            "page_obj": page_obj,
            "busqueda": texto_busqueda,
            "kpi_productos_activos": kpi_productos_activos,
            "kpi_familias_activas": kpi_familias_activas,
            "kpi_proveedores": kpi_proveedores,
            "kpi_nuevos_6_meses": kpi_nuevos_6_meses,
        }
    )


# ==========================================
# VISTA: ACCIÓN EDITAR PRODUCTO (CON RETORNO DE PÁGINA SEGURO)
# ==========================================
@permission_required('prueba.change_producto', login_url='login')
def editar_producto(request, producto_id):
    if request.method == "POST":
        precio = request.POST.get("precio_base_pesos")
        stock = request.POST.get("stock_disponible")
        ruta_imagen = request.POST.get("ruta_imagen_producto", "").strip()
        grupo_nombre = request.POST.get("grupo_nombre")
        descripcion_grupo = request.POST.get("descripcion_grupo")
        
        # CAPTURAMOS LA RUTA COMPLETA DE RETORNO O EL REFERER
        next_url = request.POST.get("next") or request.META.get('HTTP_REFERER') or reverse('dashboard')

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

        # REDIRECCIÓN EXACTA AL PUNTO DE ORIGEN
        return redirect(next_url)

    return redirect('dashboard')


def logout_view(request):
    logout(request)
    return redirect('login')


def es_admin(user):
    if user.is_superuser:
        return True
    raise PermissionDenied


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
@user_passes_test(es_admin)
def generar_pdf(request):
    if request.method == 'POST':
        grupos_seleccionados = request.POST.getlist('grupos_seleccionados')

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

        imagenes_dict = {img.grupo_nombre: img.imagen.url for img in ImagenProducto.objects.all() if img.imagen}
        descripciones_dict = {img.grupo_nombre: img.descripcion for img in ImagenProducto.objects.all() if img.descripcion}

        catalogo = {}
        for p in productos:
            familia = p.familia_temporal
            grupo = p.descripcion_grupo or p.descripcion

            if familia not in catalogo:
                catalogo[familia] = {}

            if grupo not in catalogo[familia]:
                catalogo[familia][grupo] = {
                    'imagen_url': images_dict.get(grupo, None),
                    'descripcion': descripciones_dict.get(grupo, ""),
                    'variantes': []
                }

            catalogo[familia][grupo]['variantes'].append(p)

        html_string = render_to_string('catalogo_pdf.html', {
            'catalogo': catalogo,
            'request': request,
        })

        html = HTML(string=html_string, base_url=request.build_absolute_uri('/'))
        pdf_bytes = html.write_pdf()

        fecha_actual = datetime.now().strftime('%d-%m-%Y')
        nombre_archivo = f"Catalogo_Ecosa_{fecha_actual}.pdf"

        catalogos_existentes = CatalogCache.objects.all().order_by('version_number')
        if catalogos_existentes.count() >= 3:
            catalogo_mas_antiguo = catalogos_existentes.first()
            if catalogo_mas_antiguo.pdf_file:
                catalogo_mas_antiguo.pdf_file.delete(save=False)
            catalogo_mas_antiguo.delete()
            messages.warning(request, "Se ha eliminado el catálogo más antiguo para liberar espacio.")

        ultima_version = CatalogCache.objects.order_by('-version_number').first()
        siguiente_version = (ultima_version.version_number + 1) if ultima_version else 1
        CatalogCache.objects.filter(is_current=True).update(is_current=False)

        nuevo_registro = CatalogCache(version_number=siguiente_version, is_current=True)
        nuevo_registro.pdf_file.save(nombre_archivo, ContentFile(pdf_bytes), save=True)

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{nombre_archivo}"'
        return response

    return redirect('dashboard')


@login_required(login_url='/login/')
def historial_catalogo(request):
    historial = CatalogCache.objects.order_by('-version_number')
    return render(request, 'historial_catalogo.html', {'historial': historial})


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