# =============================================================================
# Apache Guacamole для PRIDE J.A.R.V.I.S. (Railway)
# =============================================================================
# Базовый образ flcontainers/guacamole — самый популярный all-in-one bundle:
# содержит guacd + guacamole-client (Tomcat) + nginx + sqlite-auth.
# Поверх него прикручиваем Python FastAPI-прокси который умеет:
#   1) Создавать одноразовые RDP-подключения через Guacamole REST API
#   2) Возвращать iframe-friendly URL с auto-login токеном
#
# Расход: ~600-900 MB RAM. На Railway Hobby plan (8GB) уверенно влезает.
# =============================================================================
FROM flcontainers/guacamole:latest

USER root

# Python + pip для нашего прокси-сервиса
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Виртуальное окружение для прокси (изолированно от системного Python)
RUN python3 -m venv /opt/proxy-venv
COPY requirements.txt /opt/proxy-venv/requirements.txt
RUN /opt/proxy-venv/bin/pip install --no-cache-dir -r /opt/proxy-venv/requirements.txt

# Исходники прокси
COPY proxy.py /opt/proxy.py
COPY start.sh /opt/start.sh
RUN chmod +x /opt/start.sh

# Railway ожидает что сервис слушает на $PORT (по умолчанию 8080 у nginx
# Guacamole — но если Railway проставит свой, мы перенаправим через nginx).
# Наш прокси слушает на 9000 (внутренний), nginx Guacamole — на 8080.
# Внешний URL = прокси, который роутит /api/* себе и / → Guacamole.
EXPOSE 8080

CMD ["/opt/start.sh"]
