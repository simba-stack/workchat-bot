FROM python:3.12-slim

WORKDIR /app

# gcc нужен для сборки cryptg (ускоряет шифрование Telethon в ~10x)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# -u для немедленного flush логов в Railway
CMD ["python", "-u", "bot.py"]
