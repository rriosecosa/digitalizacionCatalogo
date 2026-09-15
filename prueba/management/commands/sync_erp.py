"""
Comando de management: sync_erp

Wrapper de terminal sobre el servicio prueba.services.sync_erp. Toda la
lógica real vive ahí; este comando solo la invoca e imprime el resultado.

Ubicar en: prueba/management/commands/sync_erp.py

Uso:
    docker compose exec web python manage.py sync_erp
    docker compose exec web python manage.py sync_erp --dry-run
    docker compose exec web python manage.py sync_erp --refresh-views
"""

from django.core.management.base import BaseCommand

# TODO: ajustar el import si moviste el servicio a otra ruta
from prueba.sync_erp_service import ejecutar_sync


class Command(BaseCommand):
    help = "Sincroniza productos desde la BD ERP hacia la BD del proyecto (upsert seguro, fila por fila)."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--refresh-views", action="store_true")

    def handle(self, *args, **options):
        self.stdout.write(f"Iniciando sync (dry_run={options['dry_run']})...")

        log = ejecutar_sync(
            dry_run=options["dry_run"],
            batch_size=options["batch_size"],
            refresh_views=options["refresh_views"],
        )

        if log.estado == "error":
            self.stderr.write(self.style.ERROR(f"Sync abortada: {log.detalle}"))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Sync finalizada. Nuevos: {log.creados} | Actualizados: {log.actualizados} | "
            f"Errores: {log.errores}"
        ))
        if log.detalle:
            self.stdout.write(log.detalle)