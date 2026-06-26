from django.shortcuts import render
from django.core.paginator import Paginator

from .models import (
    Producto,
    FamiliaProducto,
)

from .agrupador import obtener_grupo


def lista_productos(request):

    # -----------------------------
    # Filtro recibido desde la URL
    # -----------------------------

    familia_seleccionada = request.GET.get("familia", "")
    texto_busqueda = request.GET.get("q", "").strip()

    # -----------------------------
    # Todas las familias
    # -----------------------------

    familias = {
        f.codigo: f
        for f in FamiliaProducto.objects.all()
    }

    # -----------------------------
    # Productos
    # -----------------------------

    productos = (
        Producto.objects
        .select_related("proveedor")
        .exclude(descripcion__startswith="***")
        .order_by("descripcion")
    )

    # -----------------------------
    # Agregar grupo y familia
    # -----------------------------

    lista = []

    for p in productos:

        # -----------------
        # Grupo
        # -----------------

        p.grupo = obtener_grupo(p.descripcion)

        # -----------------
        # Familia
        # -----------------

        p.familia_obj = None

        if p.codigo:

            partes = p.codigo.split("-")

            if len(partes) >= 2:

                codigo_familia = partes[1]

                p.familia_obj = familias.get(codigo_familia)

        # -----------------
        # Filtro familia
        # -----------------

        if familia_seleccionada:

            if (
                p.familia_obj is None
                or p.familia_obj.codigo != familia_seleccionada
            ):
                continue

        # -----------------
        # Buscador
        # -----------------

        if texto_busqueda:

            texto = texto_busqueda.lower()

            encontrado = False

            if p.descripcion and texto in p.descripcion.lower():
                encontrado = True

            elif p.codigo and texto in p.codigo.lower():
                encontrado = True

            elif (
                p.proveedor
                and p.proveedor.marca
                and texto in p.proveedor.marca.lower()
            ):
                encontrado = True

            if not encontrado:
                continue

        lista.append(p)

    # -----------------------------
    # Contar productos por familia
    # -----------------------------

    conteo_familias = {}

    for p in lista:

        if p.familia_obj:

            codigo = p.familia_obj.codigo

            conteo_familias[codigo] = (
                conteo_familias.get(codigo, 0) + 1
            )

    familias_sidebar = []

    for codigo, familia in familias.items():

        if codigo in conteo_familias:

            familia.total = conteo_familias[codigo]

            familias_sidebar.append(familia)

    familias_sidebar.sort(
        key=lambda x: x.descripcion
    )

    # -----------------------------
    # PAGINACIÓN
    # -----------------------------

    paginator = Paginator(lista, 12)

    page_number = request.GET.get("page")

    page_obj = paginator.get_page(page_number)

    return render(

        request,

        "productos.html",

        {

            "page_obj": page_obj,

            "productos": page_obj,

            "familias": familias_sidebar,

            "familia_actual": familia_seleccionada,

            "busqueda": texto_busqueda,

        },

    )