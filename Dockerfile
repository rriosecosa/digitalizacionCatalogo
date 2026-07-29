# 1. Usar una versión oficial y ligera de Python 3.12
FROM python:3.12-slim

# 2. Evitar que Python escriba archivos .pyc y forzar a que la consola se muestre en tiempo real
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# 3. Crear la carpeta de trabajo dentro de la burbuja
WORKDIR /app

# 4. Copiar el archivo de requerimientos e instalar las librerías de Python
COPY requirements.txt /app/
RUN pip install --upgrade pip && pip install -r requirements.txt

# 5. CRÍTICO: Instalar Chromium y sus dependencias de sistema (Linux) para Playwright
RUN pip install playwright
RUN playwright install chromium
RUN playwright install-deps chromium

# 6. Copiar todo el resto de tu código al contenedor
COPY . /app/

# 7. Exponer el puerto donde corre Django
EXPOSE 8000

# 8. El comando que ejecutará la burbuja al encenderse
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]