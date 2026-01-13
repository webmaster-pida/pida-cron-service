# Usar la imagen oficial de Python estable
FROM python:3.11-slim

# Evitar que Python genere archivos .pyc y permitir logs en tiempo real
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Directorio de trabajo
WORKDIR /app

# Instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código fuente
COPY . .

# Comando de ejecución profesional con Gunicorn (servidor de producción)
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 main:app
