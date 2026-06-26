import re


PATRONES = [

    # Pulgadas
    r'\d+\s*/\s*\d+"',
    r'\d+"',

    # Medidas tipo 10 X 160
    r'\d+\s*[Xx]\s*\d+\s*[Xx]\s*\d+',
    r'\d+\s*[Xx]\s*\d+',

    # mm
    r'\d+\s*MM',

    # cm
    r'\d+\s*CM',

    # metros
    r'\d+\s*MTS?',
    r'\d+\s*MT',
    r'\d+\s*M\b',

    # litros
    r'\d+\s*ML',
    r'\d+\s*LT',
    r'\d+\s*L\b',

    # peso
    r'\d+\s*KG',
    r'\d+\s*GR',
    r'\d+\s*G\b',

    # G40, G80...
    r'G\d+',

    # números
    r'\b\d+\b',
]


def obtener_variante(texto):

    if not texto:
        return ""

    texto = texto.upper()

    for patron in PATRONES:

        m = re.search(patron, texto)

        if m:
            return m.group(0).strip()

    return ""


def obtener_grupo(texto):

    if not texto:
        return ""

    grupo = texto.upper()

    for patron in PATRONES:
        grupo = re.sub(patron, "", grupo)

    grupo = re.sub(r'[-_/()]', ' ', grupo)
    grupo = re.sub(r'\s+', ' ', grupo)

    return grupo.strip()