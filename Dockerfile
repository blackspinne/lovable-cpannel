# Dockerfile
FROM node:20-bullseye
RUN apt-get update && apt-get install -y python3 python3-venv python3-pip && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# deps Python
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# código
COPY app ./app
COPY static ./static

ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-keep-alive", "120"]
