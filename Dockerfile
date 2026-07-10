# 1. Usar una versión oficial y ligera de Python 3.12 (la que usas en tu entorno)
FROM python:3.12-slim

# 2. Evitar que Python escriba archivos .pyc y forzar a que la consola se muestre en tiempo real
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Crear la carpeta de trabajo dentro de la burbuja
WORKDIR /app

# 4. CRÍTICO: Instalar las dependencias de Linux necesarias para WeasyPrint
RUN apt-get update && apt-get install -y \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz-subset0 \
    libjpeg-dev \
    libopenjp2-7-dev \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*
# 5. Copiar el archivo de requerimientos e instalar las librerías de Python
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

# 6. Copiar todo el resto de tu código al contenedor
COPY . /app/

# 7. Exponer el puerto donde corre Django
EXPOSE 8000

# 8. El comando que ejecutará la burbuja al encenderse
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]