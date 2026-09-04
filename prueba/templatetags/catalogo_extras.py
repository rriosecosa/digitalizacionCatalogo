from django import template

register = template.Library()


@register.filter(name='formato_clp')
def formato_clp(valor):
    """
    Formatea un número al estilo chileno: puntos como separador de miles,
    sin símbolo de moneda, terminando en '.-'. Ej: 23081 -> "23.081.-"
    Devuelve None si el valor es None, 0 o no numérico, para que el
    template pueda mostrar el fallback ("Consultar precio").
    """
    if valor is None:
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if numero <= 0:
        return None
    entero = int(round(numero))
    formateado = f"{entero:,}".replace(",", ".")
    return f"{formateado}.-"

@register.filter(name='chunked')
def chunked(iterable, n):
    """Divide un iterable en sublistas de tamaño máximo n. Ej: |chunked:9"""
    items = list(iterable)
    n = int(n)
    return [items[i:i + n] for i in range(0, len(items), n)]