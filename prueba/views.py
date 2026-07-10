from collections import OrderedDict
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import permission_required, login_required
from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404, redirect

from .models import FamiliaProducto, Producto, ImagenProducto, Proveedor, VistaProductoAgrupado
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.db.models import Case, When, Value, IntegerField
import weasyprint
from weasyprint import HTML
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied

def lista_productos(request):
    # ==========================================
    # Parámetros recibidos
    # ==========================================
    familia_seleccionada = request.GET.get("familia", "")
    marca_seleccionada = request.GET.get("marca", "")
    texto_busqueda = request.GET.get("q", "").strip().lower()

    # ==========================================
    # Familias
    # ==========================================
    familias = {
        f.codigo: f
        for f in FamiliaProducto.objects.all()
    }

    # ==========================================
    # Productos (AHORA LEE DESDE LA VISTA SQL)
    # ==========================================
    productos = (
        VistaProductoAgrupado.objects
        .select_related("proveedor")
        .exclude(descripcion__startswith="***")
        .order_by("descripcion")
    )

    # ==========================================
    # Diccionario de grupos
    # ==========================================
    grupos = OrderedDict()

    for p in productos:
        # -------------------------
        # Grupo (AHORA LO TOMA DIRECTO DE LA BD)
        # -------------------------
        grupo = p.descripcion_grupo

        if not grupo:
            grupo = p.descripcion

        # -------------------------
        # Familia
        # -------------------------
        familia = None
        if p.codigo:
            partes = p.codigo.split("-")
            if len(partes) >= 2:
                familia = familias.get(partes[1])

        # -------------------------
        # Marca
        # -------------------------
        marca = p.proveedor.marca if p.proveedor else ""

        # -------------------------
        # Filtros
        # -------------------------
        if familia_seleccionada:
            if familia is None:
                continue
            if familia.codigo != familia_seleccionada:
                continue

        if marca_seleccionada:
            if not marca:
                continue
            if marca.lower() != marca_seleccionada.lower():
                continue

        if texto_busqueda:
            texto = " ".join([
                p.descripcion or "",
                grupo or "",
                p.codigo or "",
                marca or "",
            ]).lower()
            if texto_busqueda not in texto:
                continue

        # -------------------------
        # Crear grupo
        # -------------------------
        if grupo not in grupos:
            grupos[grupo] = {
                "id_referencia": p.id,
                "nombre": grupo,
                "marca": marca,
                "familia": familia,
                "precio_desde": p.precio_base_pesos,
                "productos": [],
            }

        # -------------------------
        # Agregar variante y precios
        # -------------------------
        grupos[grupo]["productos"].append(p)
        precio = p.precio_base_pesos

        if precio is not None:
            actual = grupos[grupo]["precio_desde"]
            if actual is None or precio < actual:
                grupos[grupo]["precio_desde"] = precio

    # ==========================================
    # Mapeo y conteo final
    # ==========================================
    lista_grupos = list(grupos.values())
    imagenes_dict = {img.grupo_nombre: img.imagen.url for img in ImagenProducto.objects.all() if img.imagen}
    
    for g in lista_grupos:
        g["imagen_url"] = imagenes_dict.get(g["nombre"], None)
        g["cantidad"] = len(g["productos"])

    # ==========================================
    # Sidebar
    # ==========================================
    conteo_familias = {}
    for g in lista_grupos:
        if g["familia"]:
            codigo = g["familia"].codigo
            conteo_familias[codigo] = conteo_familias.get(codigo, 0) + 1

    familias_sidebar = []
    for codigo, familia in familias.items():
        if codigo in conteo_familias:
            familia.total = conteo_familias[codigo]
            familias_sidebar.append(familia)
    familias_sidebar.sort(key=lambda x: x.descripcion)

    conteo_marcas = {}
    for g in lista_grupos:
        if g["marca"]:
            conteo_marcas[g["marca"]] = conteo_marcas.get(g["marca"], 0) + 1

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
    
    nombre_grupo = producto_base.descripcion_grupo
    if not nombre_grupo:
        nombre_grupo = producto_base.descripcion
        
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

    todos_los_productos = VistaProductoAgrupado.objects.select_related("proveedor").exclude(descripcion__startswith="***")
    variantes = []
    for p in todos_los_productos:
        g = p.descripcion_grupo or p.descripcion
        m = p.proveedor.marca if p.proveedor else ""
        if g == nombre_grupo and m == marca_grupo:
            variantes.append(p)

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
    texto_busqueda = request.GET.get("q", "").strip().lower()
    productos_base_qs = VistaProductoAgrupado.objects.exclude(descripcion__startswith="***")
    
    kpi_productos_activos = productos_base_qs.count()
    kpi_familias_activas = FamiliaProducto.objects.count()
    kpi_proveedores = productos_base_qs.values("proveedor__marca").distinct().exclude(proveedor__marca="").count()

    hace_seis_meses = datetime.now() - timedelta(days=180)
    kpi_nuevos_6_meses = productos_base_qs.filter(fecha_creacion__gte=hace_seis_meses).count() if hasattr(VistaProductoAgrupado, 'fecha_creacion') else 0

    productos_qs = productos_base_qs.select_related("proveedor").order_by("descripcion")
    
    if texto_busqueda:
        productos_qs = [
            p for p in productos_qs 
            if texto_busqueda in (p.descripcion or "").lower() or 
               texto_busqueda in (p.codigo or "").lower() or
               texto_busqueda in (p.proveedor.marca if p.proveedor else "").lower()
        ]

    info_grupos_qs = ImagenProducto.objects.all()
    imagenes_dict = {img.grupo_nombre: img.imagen.url for img in info_grupos_qs if img.imagen}
    descripciones_dict = {img.grupo_nombre: img.descripcion for img in info_grupos_qs if img.descripcion}
    
    for p in productos_qs:
        grupo_nombre = p.descripcion_grupo or p.descripcion
        p.imagen_url = imagenes_dict.get(grupo_nombre, None)
        p.descripcion_grupo = descripciones_dict.get(grupo_nombre, "")
        p.grupo_nombre = grupo_nombre
        p.field_id = p.id 

    paginator = Paginator(productos_qs, 20)
    page = request.GET.get("page")
    page_obj = paginator.get_page(page)

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
# VISTA: ACCIÓN EDITAR PRODUCTO (POST)
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
            
    return redirect('dashboard')


# ==========================================
# VISTA: CIERRE DE SESIÓN DIRECTO
# --- 1. FUNCIÓN DE CERRAR SESIÓN SEGURA ---
def logout_view(request):
    logout(request)
    return redirect('login')
# ==========================================
# VISTA: MENÚ EXPORTAR A PDF
# ==========================================
@login_required
def menu_exportar(request):
    productos = VistaProductoAgrupado.objects.exclude(descripcion__startswith="***").order_by('descripcion_grupo')
    
    # Traemos todas las familias a la memoria RAM
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
        
    return render(request, 'exportar.html', {'arbol_familias': arbol_familias})


# ==========================================
# VISTA: GENERAR PDF FINAL CON WEASYPRINT
# ==========================================
def es_admin(user):
    if user.is_superuser:
        return True
    raise PermissionDenied # Muestra el error 403 de "Acceso Denegado"

@login_required(login_url='/login/')
@user_passes_test(es_admin)
def generar_pdf(request):
    if request.method == 'POST':
        grupos_seleccionados = request.POST.getlist('grupos_seleccionados')
        
        if not grupos_seleccionados:
            messages.error(request, "Debes seleccionar al menos un grupo para generar el catálogo.")
            return redirect('menu_exportar')
        
        # Filtramos correctamente usando descripcion_grupo
        qs = VistaProductoAgrupado.objects.filter(
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

        # Asignamos la familia temporalmente en RAM
        for p in productos:
            p.familia_temporal = "Sin Familia"
            if p.codigo and "-" in p.codigo:
                partes = p.codigo.split("-")
                if len(partes) >= 2:
                    p.familia_temporal = familias_dict.get(partes[1], "Sin Familia")

        # REGLA DE ORO DE TRUPER (Orden rápido en RAM)
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
                    'imagen_url': imagenes_dict.get(grupo, None),
                    'descripcion': descripciones_dict.get(grupo, ""),
                    'variantes': []
                }
            
            catalogo[familia][grupo]['variantes'].append(p)

        # Tu plantilla original parece llamarse catalogo_pdf.html
        html_string = render_to_string('catalogo_pdf.html', {
            'catalogo': catalogo,
            'request': request,
        })

        html = HTML(string=html_string, base_url=request.build_absolute_uri('/'))
        pdf_file = html.write_pdf()

        # =========================================================
        # CAMBIO SOLICITADO: Fecha dinámica en el nombre de descarga
        # =========================================================
        fecha_actual = datetime.now().strftime('%d-%m-%Y')
        nombre_archivo = f"Catalogo_Ecosa_{fecha_actual}.pdf"

        response = HttpResponse(pdf_file, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{nombre_archivo}"'
        return response
    
    return redirect('dashboard')