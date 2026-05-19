# =============================================================================
# Apache Guacamole для PRIDE J.A.R.V.I.S. (Railway)
# Base: flcontainers/guacamole 1.6.0 (Alpine 3.21 + supervisord-managed
#       Tomcat/guacd/postgres). ENTRYPOINT базы = /startup.sh.
#
# Мы оборачиваем его в /opt/wrapper.sh который:
#   1) запускает nginx (slot port 8080 наружу)
#   2) запускает Python FastAPI proxy на :9000
#   3) exec /startup.sh — передаёт управление штатному supervisord
#
# Архитектура:
#   nginx :8080 (public)  → /api/proxy/*  → Python :9000 (auth shim)
#                         → /*            → Tomcat :8081 (Guacamole UI + WS)
#   guacd :4822 (internal) ← Tomcat → RDP target
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

# Tomcat → 8081 (освобождаем 8080 для nginx-фронта)
RUN sed -i 's/port="8080"/port="8081"/' /opt/tomcat/conf/server.xml

# nginx runtime каталоги
RUN mkdir -p /var/lib/nginx/tmp /run/nginx /var/log/nginx \
    && chown -R nginx:nginx /var/lib/nginx /run/nginx /var/log/nginx 2>/dev/null || true

# Пишем wrapper inline (heredoc — гарантирует LF, никаких CRLF от Windows git)
RUN cat > /opt/wrapper.sh <<'SHEOF' && chmod +x /opt/wrapper.sh
#!/bin/sh
set -u

echo "[wrapper] starting nginx (daemon)..."
nginx || echo "[wrapper] nginx failed to start"

echo "[wrapper] launching Python FastAPI proxy on :9000 in restart-loop..."
(
    while true; do
        /opt/proxy-venv/bin/python -u /opt/proxy.py 2>&1 | while IFS= read -r line; do
            echo "[proxy] $line"
        done
        echo "[wrapper] proxy exited, restarting in 3s..."
        sleep 3
    done
) &

echo "[wrapper] handing off to base /startup.sh..."
exec /startup.sh "$@"
SHEOF

EXPOSE 8080
ENTRYPOINT ["/opt/wrapper.sh"]
