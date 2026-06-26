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

        return FamiliaProducto.objects.filter(
            codigo=codigo_familia
        ).first()

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
        return self.razon_social or f"Proveedor {self.field_id}"

class FamiliaProducto(models.Model):

    field_id = models.IntegerField(db_column="_id", primary_key=True)

    codigo = models.CharField(max_length=2)

    descripcion = models.TextField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "familia_de_productos"