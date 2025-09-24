# Imagem leve e estável
FROM python:3.11-slim

# Dependências básicas (opcionais; pode remover se quiser mais leve)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala libs Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia código
COPY app ./app
COPY static ./static

# Porta (o Render injeta $PORT; usamos fallback 8080 localmente)
ENV PORT=8080

EXPOSE 8080

# IMPORTANTE: aponta para o módulo certo (app.main:app) e respeita $PORT
CMD bash -lc 'python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}'
