from collections import OrderedDict
from datetime import datetime, timedelta  # <-- REQUERIDO PARA LOS KPIS

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout  # <-- REQUERIDO PARA LOGOUT
from django.contrib.auth.decorators import permission_required
from django.core.paginator import Paginator
from django.shortcuts import render, get_object_or_404, redirect

from .agrupador import obtener_grupo
from .models import FamiliaProducto, Producto

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
    # Productos
    # ==========================================

    productos = (
        Producto.objects
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
        # Grupo
        # -------------------------

        grupo = obtener_grupo(p.descripcion)

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
        # Filtro familia
        # -------------------------

        if familia_seleccionada:

            if familia is None:
                continue

            if familia.codigo != familia_seleccionada:
                continue

        # -------------------------
        # Filtro marca
        # -------------------------

        if marca_seleccionada:

            if not marca:
                continue

            if marca.lower() != marca_seleccionada.lower():
                continue

        # -------------------------
        # Búsqueda
        # -------------------------

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
                "id_referencia": p.field_id, # <-- GUARDAMOS EL ID PARA LA URL DEL DETALLE

                "nombre": grupo,

                "marca": marca,

                "familia": familia,

                "precio_desde": p.precio_base_pesos,

                "productos": [],

            }

        # -------------------------
        # Agregar variante
        # -------------------------

        grupos[grupo]["productos"].append(p)

        # -------------------------
        # Precio mínimo
        # -------------------------

        precio = p.precio_base_pesos

        if precio is not None:

            actual = grupos[grupo]["precio_desde"]

            if actual is None or precio < actual:

                grupos[grupo]["precio_desde"] = precio

    # ==========================================
    # Convertir a lista
    # ==========================================

    lista_grupos = list(grupos.values())

    # ==========================================
    # Contar variantes
    # ==========================================

    for g in lista_grupos:

        g["cantidad"] = len(g["productos"])

    # ==========================================
    # Sidebar familias
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

    familias_sidebar.sort(
        key=lambda x: x.descripcion
    )

    # ==========================================
    # Sidebar marcas
    # ==========================================

    conteo_marcas = {}

    for g in lista_grupos:

        if g["marca"]:

            conteo_marcas[g["marca"]] = conteo_marcas.get(g["marca"], 0) + 1

    marcas_sidebar = [
        {"nombre": nombre, "total": total}
        for nombre, total in conteo_marcas.items()
    ]

    marcas_sidebar.sort(key=lambda x: x["nombre"])

    # ==========================================
    # Paginación
    # ==========================================

    paginator = Paginator(lista_grupos, 12)

    page = request.GET.get("page")

    page_obj = paginator.get_page(page)

    # ==========================================
    # Render
    # ==========================================

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
# NUEVA VISTA: VISTA DETALLE INDEPENDIENTE
# ==========================================
def detalle_producto(request, producto_id):
    # Conseguimos el producto de ancla para el detalle
    producto_base = get_object_or_404(Producto.objects.select_related("proveedor"), field_id=producto_id)
    
    # Identificamos su grupo raíz con tu función nativa
    nombre_grupo = obtener_grupo(producto_base.descripcion)
    if not nombre_grupo:
        nombre_grupo = producto_base.descripcion
        
    marca_grupo = producto_base.proveedor.marca if producto_base.proveedor else ""

    # Buscamos en el total de productos sólo las variantes que caigan en este mismo grupo
    todos_los_productos = Producto.objects.select_related("proveedor").exclude(descripcion__startswith="***")
    
    variantes = []
    for p in todos_los_productos:
        g = obtener_grupo(p.descripcion) or p.descripcion
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
        },
    )


from django.contrib.auth.decorators import permission_required
from django.contrib import messages
from django.shortcuts import redirect

# ==========================================
# VISTA: PANEL DASHBOARD PRINCIPAL
# ==========================================
@permission_required('prueba.change_producto', login_url='login')
def dashboard_productos(request):
    texto_busqueda = request.GET.get("q", "").strip().lower()
    
    # 1. Consulta base de datos limpia (Sin alterar)
    productos_base_qs = Producto.objects.exclude(descripcion__startswith="***")
    
    # 2. --- CÁLCULO DE KPIS REALES ---
    kpi_productos_activos = productos_base_qs.count()
    kpi_familias_activas = FamiliaProducto.objects.count()
    kpi_proveedores = productos_base_qs.values("proveedor__marca").distinct().exclude(proveedor__marca="").count()

    # Validación de fecha para productos de los últimos 6 meses
    hace_seis_meses = datetime.now() - timedelta(days=180)
    kpi_nuevos_6_meses = productos_base_qs.filter(fecha_creacion__gte=hace_seis_meses).count() if hasattr(Producto, 'fecha_creacion') else 0
    # ----------------------------------

    # 3. Traemos el listado ordenado para la visualización de la tabla del administrador
    productos_qs = productos_base_qs.select_related("proveedor").order_by("descripcion")
    
    if texto_busqueda:
        # Mantenemos tu filtro original exacto en memoria
        productos_qs = [
            p for p in productos_qs 
            if texto_busqueda in (p.descripcion or "").lower() or 
               texto_busqueda in (p.codigo or "").lower() or
               texto_busqueda in (p.proveedor.marca if p.proveedor else "").lower()
        ]

    # Paginación interna del Dashboard
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
            # Variables requeridas por el diseño de tu dashboard.html
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
        
        try:
            precio_float = float(precio) if precio else None
            stock_float = float(stock) if stock else None
            
            Producto.objects.filter(field_id=producto_id).update(
                precio_base_pesos=precio_float,
                stock_disponible=stock_float
            )
            messages.success(request, "Producto actualizado correctamente.")
        except ValueError:
            messages.error(request, "Error: Los valores ingresados no son numéricos válidos.")
            
    return redirect('dashboard')

# ==========================================
# VISTA: CIERRE DE SESIÓN DIRECTO
# ==========================================
def logout_view(request):
    logout(request)
    # Redirecciona instantáneamente usando el 'name' de tu catálogo principal
    return redirect('productos')