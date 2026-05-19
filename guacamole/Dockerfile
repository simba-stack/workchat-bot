# =============================================================================
# Apache Guacamole для PRIDE J.A.R.V.I.S. (Railway)
# =============================================================================
# Base: flcontainers/guacamole (Alpine 3.21, supervisord-managed Tomcat+guacd+postgres)
# ENTRYPOINT базового образа = /startup.sh. Мы оборачиваем его в /opt/wrapper.sh
# который дополнительно запускает nginx и наш Python FastAPI proxy.
#
# Архитектура:
#   nginx (:8080, public)  →  /api/proxy/*  → Python (:9000)
#                          →  /*            → Tomcat (:8081)
#   guacd (:4822, internal) ← Tomcat → RDP target
# =============================================================================
FROM flcontainers/guacamole:1.6.0

USER root

RUN apk add --no-cache \
        nginx \
        python3 py3-pip \
        ca-certificates curl

# Python venv для прокси
RUN python3 -m venv /opt/proxy-venv
COPY requirements.txt /opt/proxy-venv/req.txt
RUN /opt/proxy-venv/bin/pip install --no-cache-dir -r /opt/proxy-venv/req.txt

# Файлы прокси + nginx
COPY proxy.py /opt/proxy.py
COPY nginx-site.conf /etc/nginx/http.d/default.conf
COPY wrapper.sh /opt/wrapper.sh
RUN chmod +x /opt/wrapper.sh

# Tomcat → 8081 (освобождаем 8080 для nginx-фронта)
RUN sed -i 's/port="8080"/port="8081"/' /opt/tomcat/conf/server.xml

# nginx нужен runtime-каталог для tmp
RUN mkdir -p /var/lib/nginx/tmp /run/nginx \
    && chown -R nginx:nginx /var/lib/nginx /run/nginx 2>/dev/null || true

EXPOSE 8080
ENTRYPOINT ["/opt/wrapper.sh"]
