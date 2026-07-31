import re
from django import template

register = template.Library()

@register.filter(name='limpiar_medida')
def limpiar_medida(descripcion_variante, descripcion_grupo):
    """
    Resta el nombre del grupo a la descripción de la variante para dejar
    únicamente la medida, calibre o especificación técnica.
    Ejemplo:
      Grupo: 'ABRAZADERA CREMALLERA INOX'
      Variante: 'ABRAZADERA CREMALLERA INOX 1/2 A 3/4 PLASTIC'
      Resultado: '1/2 A 3/4 PLASTIC'
    """
    if not descripcion_variante:
        return ""
    if not descripcion_grupo:
        return descripcion_variante
    
    desc_var = str(descripcion_variante).strip()
    desc_grp = str(descripcion_grupo).strip()
    
    # Remueve el texto del grupo si está al inicio (sin importar mayúsculas/minúsculas)
    pattern = re.escape(desc_grp)
    resultado = re.sub(f"^{pattern}", "", desc_var, flags=re.IGNORECASE).strip()
    
    # Limpia guiones, puntos o espacios sobrantes al inicio de la medida restante
    resultado = re.sub(r"^[\s\-:\.]+", "", resultado).strip()
    
    # Si por alguna razón la variante se quedó en blanco (ej. era idéntica al título), 
    # retorna la variante original como respaldo.
    return resultado if resultado else desc_var