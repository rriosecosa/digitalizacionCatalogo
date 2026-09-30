"""
Servicio: resolución de URL de ficha técnica en truper.com a partir del
código de origen (código catalográfico de Truper).

Usa el mismo endpoint que consume el buscador de truper.com
(restDataSheet/api/search/products_ficha_new.php), que devuelve la URL
canónica de la página de ficha técnica (con slug) para un código dado.

No se llama en vivo durante generar_pdf -- se corre una vez (y luego solo
para códigos nuevos) vía el management command sync_fichas_truper.
"""
import requests

TRUPER_SEARCH_URL = "https://www.truper.com/restDataSheet/api/search/products_ficha_new.php"


def buscar_url_ficha_truper(codigo: str) -> str | None:
    """Devuelve la URL de ficha técnica en truper.com para un código dado,
    o None si no hay match exacto (código no existe en Truper, o error de red)."""
    codigo = (codigo or "").strip()
    if not codigo:
        return None

    try:
        resp = requests.post(
            TRUPER_SEARCH_URL,
            data={"word": codigo},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        resultados = resp.json()
    except (requests.RequestException, ValueError):
        return None

    for item in resultados:
        if str(item.get("code")) == str(codigo):
            return item.get("url")
    return None