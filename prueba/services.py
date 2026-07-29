from django.db.models import Q

from .models import (
    Producto,
    FamiliaProducto,
)

from .agrupador import obtener_grupo


def obtener_familias():

    """
    Devuelve todas las familias ordenadas
    junto con la cantidad de productos.
    """

    familias = []

    productos = (
        Producto.objects
        .exclude(descripcion__startswith="***")
        .only("codigo")
    )

    contador = {}

    for p in productos:

        if not p.codigo:
            continue

        partes = p.codigo.split("-")

        if len(partes) < 2:
            continue

        codigo = partes[1]

        contador[codigo] = contador.get(codigo, 0) + 1

    for f in FamiliaProducto.objects.all().order_by("descripcion"):

        f.total = contador.get(f.codigo, 0)

        familias.append(f)

    return familias


def obtener_catalogo(request):

    familia = request.GET.get("familia")

    buscar = request.GET.get("q")

    productos = (
        Producto.objects
        .select_related("proveedor")
        .exclude(descripcion__startswith="***")
    )

    # -------------------------
    # FILTRO POR FAMILIA
    # -------------------------

    if familia:

        productos_filtrados = []

        for p in productos:

            if not p.codigo:
                continue

            partes = p.codigo.split("-")

            if len(partes) < 2:
                continue

            if partes[1] == familia:

                productos_filtrados.append(p)

        productos = productos_filtrados

    else:

        productos = list(productos)

    # -------------------------
    # BUSCADOR
    # -------------------------

    if buscar:

        texto = buscar.lower()

        temporal = []

        for p in productos:

            marca = ""

            if p.proveedor:
                marca = (p.proveedor.marca or "").lower()

            descripcion = (p.descripcion or "").lower()

            codigo = (p.codigo or "").lower()

            if (
                texto in descripcion
                or texto in codigo
                or texto in marca
            ):

                temporal.append(p)

        productos = temporal

    # -------------------------
    # COMPLETAR DATOS
    # -------------------------

    familias = {
        f.codigo: f
        for f in FamiliaProducto.objects.all()
    }

    for p in productos:

        p.grupo = obtener_grupo(p.descripcion)

        p.familia_obj = None

        if p.codigo:

            partes = p.codigo.split("-")

            if len(partes) >= 2:

                p.familia_obj = familias.get(partes[1])

    productos.sort(
        key=lambda x: (
            x.descripcion or ""
        )
    )

    return productos