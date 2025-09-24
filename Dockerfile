# Python + Node para conseguir rodar npm build
FROM python:3.9-slim-bullseye

ENV DEBIAN_FRONTEND=noninteractive
# SO deps + Node 18 LTS + ferramentas de build
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg build-essential python3-dev && \
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    node -v && npm -v && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependências Python
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Código
COPY app ./app
COPY static ./static

EXPOSE 8080
ENV PORT=8080
CMD ["python","-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]
