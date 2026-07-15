from django.db import models

class Producto(models.Model):
    field_id = models.AutoField(db_column="_id", primary_key=True)
    codigo = models.TextField(blank=True, null=True)
    descripcion = models.TextField(blank=True, null=True)

    proveedor = models.ForeignKey(
        "Proveedor",
        db_column="proveedor",
        on_delete=models.DO_NOTHING,
        blank=True,
        null=True,
        related_name="productos",
    )

    precio_base_pesos = models.FloatField(blank=True, null=True)
    stock_disponible = models.FloatField(blank=True, null=True)
    eliminado = models.FloatField(blank=True, null=True)

    @property
    def familia(self):
        if not self.codigo:
            return None
        partes = self.codigo.split("-")
        if len(partes) < 2:
            return None
        codigo_familia = partes[1]
        return FamiliaProducto.objects.filter(codigo=codigo_familia).first()

    class Meta:
        managed = False
        db_table = "producto"


class Proveedor(models.Model):
    field_id = models.IntegerField(db_column="_id", primary_key=True)
    codigo = models.TextField(blank=True, null=True)
    razon_social = models.TextField(blank=True, null=True)
    marca = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "proveedor"

    def __str__(self):
        return self.marca if self.marca else (self.razon_social or f"Proveedor {self.field_id}")


class FamiliaProducto(models.Model):
    field_id = models.IntegerField(db_column="_id", primary_key=True)
    codigo = models.TextField(blank=True, null=True)
    descripcion = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "familia_de_productos"


# =====================================================================
# VISTA SQL (Se acelera automáticamente con los índices de 'producto' y 'proveedor')
# =====================================================================
class VistaProductoAgrupado(models.Model):
    id = models.IntegerField(primary_key=True) 
    codigo = models.TextField(blank=True, null=True)
    descripcion = models.TextField(blank=True, null=True)
    precio_base_pesos = models.FloatField(blank=True, null=True)
    stock_disponible = models.FloatField(blank=True, null=True)
    eliminado = models.FloatField(blank=True, null=True)
    unidad_medida = models.TextField(blank=True, null=True)

    proveedor = models.ForeignKey(
        "Proveedor",
        db_column="proveedor",
        on_delete=models.DO_NOTHING,
        blank=True,
        null=True,
        related_name="productos_agrupados",
    )

    descripcion_grupo = models.TextField()

    @property
    def familia(self):
        if not self.codigo or "-" not in self.codigo:
            return None
        partes = self.codigo.split("-")
        if len(partes) < 2:
            return None
        return FamiliaProducto.objects.filter(codigo=partes[1]).first()

    class Meta:
        managed = False 
        db_table = "vista_producto_agrupado"


# =====================================================================
# MODELOS ADMINISTRADOS POR DJANGO
# =====================================================================
class ImagenProducto(models.Model):
    # db_index=True es vital aquí y ya lo tienes configurado correctamente.
    grupo_nombre = models.CharField(max_length=255, unique=True, db_index=True)
    imagen = models.ImageField(upload_to='productos/')
    descripcion = models.TextField(blank=True, null=True)
    creado_el = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Datos para {self.grupo_nombre}"


class CatalogCache(models.Model):
    version_number = models.IntegerField()
    pdf_file = models.FileField(upload_to='catalogos/')
    generated_at = models.DateTimeField(auto_now_add=True)
    is_current = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = 'prueba_catalogcache'
        ordering = ['-version_number']

    def __str__(self):
        return f"Catálogo v{self.version_number} - {'Actual' if self.is_current else 'Anterior'}"
    
from django.db import models
import os

class HistorialCatalogo(models.Model):
    nombre = models.CharField(max_length=200, verbose_name="Nombre del Catálogo")
    archivo_pdf = models.FileField(upload_to='catalogos_pdf/', verbose_name="Archivo PDF")
    fecha_creacion = models.DateTimeField(auto_now_add=True, verbose_name="Fecha de Creación")

    class Meta:
        verbose_name = "Historial de Catálogo"
        verbose_name_plural = "Historial de Catálogos"
        # Ordenamos por defecto del más antiguo al más nuevo
        ordering = ['fecha_creacion']

    def __str__(self):
        return f"{self.nombre} - {self.fecha_creacion.strftime('%d/%m/%Y %H:%M')}"

    # Opcional pero recomendado: Borrar el archivo físico cuando se borra el registro de la BD
    def delete(self, *args, **kwargs):
        if self.archivo_pdf and os.path.isfile(self.archivo_pdf.path):
            os.remove(self.archivo_pdf.path)
        super().delete(*args, **kwargs)