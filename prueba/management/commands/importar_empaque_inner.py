"""
Management command para importar empaque_inner extraído del catálogo PDF.

Colócalo en: <tu_app>/management/commands/importar_empaque_inner.py
(crea las carpetas management/ y management/commands/ con __init__.py si no existen)

Ajusta el import de abajo (`from <app>.models import Producto`) a tu app real.

Uso:
    # Dry-run (no toca la BD, solo muestra el reporte):
    python manage.py importar_empaque_inner ecosa_empaque_inner_extraccion.csv

    # Aplicar los cambios de verdad:
    python manage.py importar_empaque_inner ecosa_empaque_inner_extraccion.csv --apply

    # Incluir también las filas marcadas como "revisar" (valores ambiguos, ej. "2 / 20"):
    python manage.py importar_empaque_inner ecosa_empaque_inner_extraccion.csv --apply --incluir-revisar
"""
import csv

from django.core.management.base import BaseCommand
from django.db import transaction

from prueba.models import Producto  # <-- ajusta este import a tu app


class Command(BaseCommand):
    help = "Importa empaque_inner extraído del catálogo PDF (CSV: codigo,pagina_pdf,empaque_inner,estado,valor_crudo)"

    def add_arguments(self, parser):
        parser.add_argument('csv_path', type=str)
        parser.add_argument(
            '--apply', action='store_true',
            help='Aplica los cambios en la BD. Sin este flag solo se muestra el reporte (dry-run).'
        )
        parser.add_argument(
            '--incluir-revisar', action='store_true',
            help='Incluye también las filas marcadas como "revisar" (valores ambiguos, ej. "2 / 20").'
        )

    def handle(self, *args, **options):
        csv_path = options['csv_path']
        aplicar = options['apply']
        incluir_revisar = options['incluir_revisar']

        estados_validos = {'ok'}
        if incluir_revisar:
            estados_validos.add('revisar')

        with open(csv_path, encoding='utf-8') as f:
            reader = csv.DictReader(f)
            filas = [r for r in reader if r['estado'] in estados_validos and r['empaque_inner']]

        codigos = [r['codigo'] for r in filas]
        productos = {p.codigo: p for p in Producto.objects.filter(codigo__in=codigos)}

        con_match_sin_cambio = []
        con_match_con_cambio = []
        sin_match = []

        for r in filas:
            codigo = r['codigo']
            nuevo_valor = r['empaque_inner'].strip()
            producto = productos.get(codigo)
            if not producto:
                sin_match.append(codigo)
                continue
            actual = (producto.empaque_inner or '').strip()
            if actual == nuevo_valor:
                con_match_sin_cambio.append(codigo)
                continue
            con_match_con_cambio.append((codigo, actual, nuevo_valor))

        self.stdout.write(f"Filas en CSV consideradas ({'+'.join(sorted(estados_validos))}): {len(filas)}")
        self.stdout.write(f"  Coinciden en BD, sin cambios: {len(con_match_sin_cambio)}")
        self.stdout.write(f"  Coinciden en BD, con cambio de valor: {len(con_match_con_cambio)}")
        self.stdout.write(f"  Código no encontrado en BD: {len(sin_match)}")

        if con_match_con_cambio:
            self.stdout.write("\n--- Cambios detectados (código: actual -> nuevo) ---")
            for codigo, actual, nuevo in con_match_con_cambio[:50]:
                self.stdout.write(f"  {codigo}: {actual!r} -> {nuevo!r}")
            if len(con_match_con_cambio) > 50:
                self.stdout.write(f"  ... y {len(con_match_con_cambio) - 50} más")

        if sin_match:
            self.stdout.write("\n--- Códigos del PDF sin match en la BD (revisar) ---")
            for codigo in sin_match[:50]:
                self.stdout.write(f"  {codigo}")
            if len(sin_match) > 50:
                self.stdout.write(f"  ... y {len(sin_match) - 50} más")

        if not aplicar:
            self.stdout.write(self.style.WARNING(
                "\nDRY-RUN: no se modificó la base de datos. "
                "Vuelve a correr con --apply para guardar los cambios."
            ))
            return

        with transaction.atomic():
            for codigo, actual, nuevo in con_match_con_cambio:
                productos[codigo].empaque_inner = nuevo
                productos[codigo].save(update_fields=['empaque_inner'])

        self.stdout.write(self.style.SUCCESS(f"\nListo: se actualizaron {len(con_match_con_cambio)} productos."))
