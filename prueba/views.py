from collections import OrderedDict

from django.core.paginator import Paginator
from django.shortcuts import render

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