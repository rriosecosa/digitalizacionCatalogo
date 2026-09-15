"""
Agregar este bloque a tu prueba/admin.py existente (no reemplaces todo el
archivo, solo agrega estos imports y esta clase).
"""

from django.contrib import admin, messages
from django.urls import path
from django.shortcuts import redirect
from django.utils.html import format_html

# TODO: ajustar el import si tu SyncLog vive en otro módulo
from .models import SyncLog



@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    change_list_template = "admin/prueba/synclog/change_list.html"

    list_display = (
        "fecha",
        "estado_coloreado",
        "creados",
        "actualizados",
        "errores",
        "resumen_cambios",
    )
    list_filter = ("estado",)
    ordering = ("-fecha",)
    readonly_fields = ("fecha", "estado", "creados", "actualizados", "errores", "detalle_formateado")
    fields = ("fecha", "estado", "creados", "actualizados", "errores", "detalle_formateado")

    def has_add_permission(self, request):
        # Los registros solo se crean desde el botón "Sincronizar ahora"
        # o desde el comando de terminal, nunca a mano.
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def estado_coloreado(self, obj):
        colores = {"ok": "#1a7f37", "parcial": "#b08800", "error": "#cf222e"}
        color = colores.get(obj.estado, "#333")
        return format_html('<b style="color:{}">{}</b>', color, obj.estado.upper())
    estado_coloreado.short_description = "Estado"

    def resumen_cambios(self, obj):
        if not obj.detalle:
            return "-"
        lineas = obj.detalle.count("\n") + 1
        return f"{lineas} líneas de detalle (ver registro)"
    resumen_cambios.short_description = "Detalle"

    def detalle_formateado(self, obj):
        return format_html("<pre style='white-space:pre-wrap'>{}</pre>", obj.detalle or "(sin detalle)")
    detalle_formateado.short_description = "Detalle completo"

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path("sincronizar/", self.admin_site.admin_view(self.sincronizar_view), name="sync_erp_ejecutar"),
            path("sincronizar-prueba/", self.admin_site.admin_view(self.sincronizar_dry_run_view), name="sync_erp_dry_run"),
        ]
        return custom + urls

    def sincronizar_view(self, request):
        log = ejecutar_sync(dry_run=False)
        self._mensaje_resultado(request, log)
        return redirect("admin:prueba_synclog_changelist")

    def sincronizar_dry_run_view(self, request):
        log = ejecutar_sync(dry_run=True)
        self._mensaje_resultado(request, log, prueba=True)
        return redirect("admin:prueba_synclog_changelist")

    def _mensaje_resultado(self, request, log, prueba=False):
        prefijo = "[PRUEBA, no se guardó nada] " if prueba else ""
        if log.estado == "error":
            messages.error(request, f"{prefijo}Sync abortada: {log.detalle}")
        else:
            nivel = messages.SUCCESS if log.errores == 0 else messages.WARNING
            messages.add_message(
                request, nivel,
                f"{prefijo}Sync finalizada. Nuevos: {log.creados} | "
                f"Actualizados: {log.actualizados} | Errores: {log.errores}"
            )