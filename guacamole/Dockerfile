# =============================================================================
# Apache Guacamole для PRIDE J.A.R.V.I.S. (Railway)
# =============================================================================
# Базовый образ flcontainers/guacamole — bundle guacd + tomcat + nginx + sqlite.
# Образ основан на Alpine Linux (apk, не apt-get).
# =============================================================================
FROM flcontainers/guacamole:latest

USER root

# Python 3 + pip для нашего FastAPI auth-прокси
RUN apk add --no-cache \
        python3 py3-pip \
        ca-certificates curl

# Виртуальное окружение для прокси
RUN python3 -m venv /opt/proxy-venv
COPY requirements.txt /opt/proxy-venv/requirements.txt
RUN /opt/proxy-venv/bin/pip install --no-cache-dir -r /opt/proxy-venv/requirements.txt

# Исходники прокси
COPY proxy.py /opt/proxy.py
COPY start.sh /opt/start.sh
RUN chmod +x /opt/start.sh

# Railway проксирует $PORT на внутренний порт; nginx Guacamole слушает 8080
EXPOSE 8080

CMD ["/opt/start.sh"]
