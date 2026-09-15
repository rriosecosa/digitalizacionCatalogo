"""
Servicio de sincronización ERP -> Proyecto.

Contiene toda la lógica real del sync. Tanto el comando de terminal
(management command) como la vista del dashboard llaman a la función
`ejecutar_sync()` de aquí, para no duplicar código.

Ubicar en: prueba/sync_erp_service.py
(archivo suelto, NO dentro de una carpeta "services/", para no chocar
con tu prueba/services.py existente)
"""

import logging

import psycopg2
from decouple import config

from django.db import connections, transaction
from django.utils import timezone

# TODO: ajustar el import a tu app/modelo real de SyncLog
from prueba.models import SyncLog

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
ERP_TABLE = "producto"
PROJECT_TABLE = "producto"

MATERIALIZED_VIEWS_TO_REFRESH = [
    # "vista_producto_agrupado",
    # "vista_producto_variantes",
]

ERP_COLUMNS = [
    "_id",
    "proveedor",
    "familia_de_producto",
    "codigo",
    "codigo_familia",
    "codigo_proveedor",
    "codigo_producto",
    "descripcion",
    "comision_pct",
    "foto",
    "costo_moneda_extranjera",
    "moneda",
    "paridad_pesos",
    "costo_moneda_nacional_pesos",
    "precio_base_pesos",
    "unidad_de_medida",
    "gramaje_gr",
    "stock_fisico",
    "unidad_de_medida_fisica",
    "stock_disponible",
    "unidad_de_medida_disponible",
    "stock_por_recibir",
    "stock_por_recibir_um",
    "stock_minimo",
    "unidad_de_medida_minima",
    "receta",
    "codigo_de_origen",
    "descripcion_de_origen",
    "margen_pct",
    "liquidacion_pct",
    "bodega",
    "empaque_original",
    "fecha_ultima_recepcion",
    "pais",
    "ciudad",
    "_cantidad_toma_inv",
    "historico",
    "costo_origen",
    "eliminado",
]

PK_COLUMN = "_id"


class _DryRunRollback(Exception):
    """Excepción interna usada solo para forzar rollback en modo dry_run."""
    pass


def get_erp_connection():
    return psycopg2.connect(
        dbname=config("ERP_DB_NAME"),
        user=config("ERP_DB_USER"),
        password=config("ERP_DB_PASSWORD"),
        host=config("ERP_DB_HOST"),
        port=config("ERP_DB_PORT", cast=int, default=5432),
        options="-c default_transaction_read_only=on",
    )


def _valor_serializable(v):
    """
    Convierte valores que el JSONField no puede guardar tal cual
    (Decimal, date, datetime) a algo serializable, sin perder legibilidad.
    """
    if v is None:
        return None
    if hasattr(v, "isoformat"):  # date / datetime
        return v.isoformat()
    import decimal
    if isinstance(v, decimal.Decimal):
        return str(v)
    return v


def ejecutar_sync(dry_run=False, batch_size=500, refresh_views=False):
    """
    Ejecuta la sincronización completa y devuelve el objeto SyncLog creado.
    No borra nada. No hace TRUNCATE. Cada producto se procesa en su propio
    savepoint, así que un error en una fila nunca afecta al resto.
    """
    select_cols = ", ".join(ERP_COLUMNS)
    query = f"SELECT {select_cols} FROM {ERP_TABLE} ORDER BY {PK_COLUMN}"
    select_local_sql = f"SELECT {select_cols} FROM {PROJECT_TABLE} WHERE {PK_COLUMN} = %s"

    insert_cols = ", ".join(ERP_COLUMNS)
    placeholders = ", ".join(["%s"] * len(ERP_COLUMNS))
    insert_sql = f"INSERT INTO {PROJECT_TABLE} ({insert_cols}) VALUES ({placeholders})"

    creados = 0
    actualizados = 0
    sin_cambios = 0
    errores = 0
    detalle_errores = []
    detalle_cambios = []
    cambios_estructurados = []
    errores_estructurados = []
    total_leidos = 0

    try:
        erp_conn = get_erp_connection()
        try:
            erp_cursor = erp_conn.cursor()
            erp_cursor.execute(query)

            while True:
                rows = erp_cursor.fetchmany(batch_size)
                if not rows:
                    break

                for row in rows:
                    total_leidos += 1
                    pk_value = row[ERP_COLUMNS.index(PK_COLUMN)]

                    try:
                        with transaction.atomic(using="default"):
                            with connections["default"].cursor() as pcursor:
                                pcursor.execute(select_local_sql, [pk_value])
                                existing = pcursor.fetchone()

                                if existing is None:
                                    pcursor.execute(insert_sql, row)
                                    creados += 1
                                else:
                                    cambios = []
                                    for col, old_val, new_val in zip(ERP_COLUMNS, existing, row):
                                        if col == PK_COLUMN:
                                            continue
                                        if old_val != new_val:
                                            cambios.append((col, old_val, new_val))

                                    if not cambios:
                                        sin_cambios += 1
                                    else:
                                        set_clause = ", ".join(f"{c} = %s" for c, _, _ in cambios)
                                        update_sql = (
                                            f"UPDATE {PROJECT_TABLE} SET {set_clause} "
                                            f"WHERE {PK_COLUMN} = %s"
                                        )
                                        params = [nv for _, _, nv in cambios] + [pk_value]
                                        pcursor.execute(update_sql, params)
                                        actualizados += 1

                                        campos_txt = ", ".join(
                                            f"{c}: {ov!r} -> {nv!r}" for c, ov, nv in cambios
                                        )
                                        detalle_cambios.append(f"{PK_COLUMN}={pk_value}: {campos_txt}")
                                        cambios_estructurados.append({
                                            "id": pk_value,
                                            "cambios": [
                                                {
                                                    "campo": c,
                                                    "antes": _valor_serializable(ov),
                                                    "despues": _valor_serializable(nv),
                                                }
                                                for c, ov, nv in cambios
                                            ],
                                        })

                            if dry_run:
                                raise _DryRunRollback()

                    except _DryRunRollback:
                        continue
                    except Exception as e:
                        errores += 1
                        detalle_errores.append(f"{PK_COLUMN}={pk_value}: {e}")
                        errores_estructurados.append({"id": pk_value, "error": str(e)})
                        logger.exception("Error sincronizando producto %s=%s", PK_COLUMN, pk_value)
                        continue
        finally:
            erp_conn.close()

        if refresh_views and not dry_run:
            for view in MATERIALIZED_VIEWS_TO_REFRESH:
                try:
                    with connections["default"].cursor() as pcursor:
                        pcursor.execute(f"REFRESH MATERIALIZED VIEW {view}")
                except Exception as e:
                    errores += 1
                    detalle_errores.append(f"refresh {view}: {e}")
                    errores_estructurados.append({"id": f"vista:{view}", "error": str(e)})
                    logger.exception("Error refrescando vista %s", view)

    except Exception as e:
        return SyncLog.objects.create(
            fecha=timezone.now(),
            estado="error",
            creados=creados,
            actualizados=actualizados,
            errores=errores,
            detalle=f"Fallo de conexión/consulta al ERP: {e}",
        )

    estado = "ok" if errores == 0 else "parcial"

    partes_detalle = []
    if dry_run:
        partes_detalle.append("=== MODO PRUEBA (dry-run): nada se guardó realmente ===")
    if detalle_cambios:
        partes_detalle.append("=== CAMBIOS ===")
        partes_detalle.extend(detalle_cambios[:500])
    if detalle_errores:
        partes_detalle.append("=== ERRORES ===")
        partes_detalle.extend(detalle_errores[:500])

    return SyncLog.objects.create(
        fecha=timezone.now(),
        estado=estado,
        creados=creados,
        actualizados=actualizados,
        errores=errores,
        detalle="\n".join(partes_detalle),
        detalle_cambios=cambios_estructurados[:500] or None,
        detalle_errores=errores_estructurados[:500] or None,
    )