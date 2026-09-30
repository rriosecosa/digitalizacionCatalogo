"""
Comando de management: sync_fichas_truper

Resuelve y cachea la URL de ficha técnica de truper.com para cada
código_de_origen distinto presente en VistaProductoVariantes, guardándolo
en TruperFichaTecnica. Correr una vez, y después solo cuando haya
productos Truper nuevos.

Uso:
    docker compose exec web python manage.py sync_fichas_truper
    docker compose exec web python manage.py sync_fichas_truper --forzar
"""
import time

from django.core.management.base import BaseCommand

from prueba.models import VistaProductoVariantes, TruperFichaTecnica
from prueba.truper_ficha_service import buscar_url_ficha_truper


class Command(BaseCommand):
    help = "Resuelve y cachea las URLs de ficha técnica de Truper por código_de_origen."

    def add_arguments(self, parser):
        parser.add_argument(
            "--forzar", action="store_true",
            help="Re-consulta incluso los códigos que ya están cacheados."
        )

    def handle(self, *args, **options):
        codigos = list(
            VistaProductoVariantes.objects
            .exclude(codigo_de_origen__isnull=True)
            .exclude(codigo_de_origen__exact="")
            .values_list("codigo_de_origen", flat=True)
            .distinct()
        )

        if not options["forzar"]:
            ya_cacheados = set(TruperFichaTecnica.objects.values_list("codigo_origen", flat=True))
            codigos = [c for c in codigos if c not in ya_cacheados]

        self.stdout.write(f"Resolviendo {len(codigos)} códigos...")

        encontrados = 0
        sin_match = 0

        for i, codigo in enumerate(codigos, start=1):
            url = buscar_url_ficha_truper(codigo)
            if url:
                TruperFichaTecnica.objects.update_or_create(
                    codigo_origen=codigo,
                    defaults={"url_ficha_tecnica": url},
                )
                encontrados += 1
                self.stdout.write(f"[{i}/{len(codigos)}] OK  {codigo} -> {url}")
            else:
                sin_match += 1
                self.stdout.write(f"[{i}/{len(codigos)}] --  {codigo} (sin match)")

            time.sleep(0.3)  # no golpear el endpoint de Truper sin pausas

        self.stdout.write(self.style.SUCCESS(
            f"Listo. Encontrados: {encontrados} | Sin match: {sin_match}"
        ))