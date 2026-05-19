# =============================================================================
# Apache Guacamole для PRIDE J.A.R.V.I.S. (Railway)
# =============================================================================
# Базовый: flcontainers/guacamole — Alpine 3.21, supervisor, Tomcat на 8080,
# guacd, postgres внутри. Entrypoint = /startup.sh (НЕ переопределяем!).
#
# Что добавляем:
#   1) nginx как ОТДЕЛЬНЫЙ supervisor-сервис → слушает 8080 наружу
#   2) Python FastAPI прокси на 9000 (внутренний)
#   3) Передвигаем Tomcat на 8081 чтобы освободить 8080 для nginx
#   4) nginx маршрутизирует:
#        /api/proxy/* → 127.0.0.1:9000  (наш auth-прокси)
#        /*           → 127.0.0.1:8081  (Tomcat → Guacamole UI + WebSocket)
# =============================================================================
FROM flcontainers/guacamole:1.6.0

USER root

# Альпайн-пакеты: nginx + Python для нашего прокси
RUN apk add --no-cache \
        nginx \
        python3 py3-pip \
        ca-certificates curl

# Python venv для прокси
RUN python3 -m venv /opt/proxy-venv
COPY requirements.txt /opt/proxy-venv/req.txt
RUN /opt/proxy-venv/bin/pip install --no-cache-dir -r /opt/proxy-venv/req.txt

# Файлы прокси и конфиги
COPY proxy.py /opt/proxy.py
COPY nginx-site.conf /etc/nginx/http.d/default.conf
COPY supervisor-nginx.ini /etc/supervisor.d/nginx.ini
COPY supervisor-proxy.ini /etc/supervisor.d/proxy.ini

# Перенастраиваем Tomcat: слушает 8081 (а не 8080) — освобождаем порт для nginx
# CATALINA_HOME=/opt/tomcat (см. base image)
RUN sed -i 's/port="8080"/port="8081"/' /opt/tomcat/conf/server.xml

# Каталог для логов supervisor (на всякий случай — должен быть от базы)
RUN mkdir -p /var/log/supervisor

EXPOSE 8080
# ENTRYPOINT остаётся /startup.sh из базового образа — НЕ переопределяем!
