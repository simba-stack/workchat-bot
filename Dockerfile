FROM python:3.11-slim

WORKDIR /app

# Системные пакеты для сборки cryptg (ускоряет Telethon)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# -u для немедленного flush логов в Railway/Docker
CMD ["python", "-u", "bot.py"]

