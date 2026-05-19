# =============================================================================
# Apache Guacamole для PRIDE J.A.R.V.I.S. (Railway)
# Base: flcontainers/guacamole 1.6.0 (Alpine 3.21 + supervisord-managed
#       Tomcat/guacd/postgres).
#
# Архитектура (target port = 8080):
#   nginx       :8080 (public, Railway target)
#                 ├─ /api/proxy/*  → Python  :9000
#                 └─ /*            → Tomcat  :8081 (guacamole webapp / + /guacamole/)
#   guacd       :4822 (internal)  ← Tomcat → RDP target
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

# Tomcat HTTP connector 8080 → 8081 (освобождаем 8080 для nginx)
RUN sed -i 's/port="8080"/port="8081"/' /opt/tomcat/conf/server.xml

# Logs to stdout/stderr так чтобы Railway их видел
RUN sed -i 's|^error_log .*;|error_log /dev/stderr warn;|' /etc/nginx/nginx.conf \
    && sed -i 's|^access_log .*;|access_log /dev/stdout;|' /etc/nginx/nginx.conf || true

# Test config заранее (fail build если что-то не так)
RUN nginx -t

# Доп. копия guacamole.war под /guacamole/ путь (Tomcat auto-deploy)
RUN if [ -f /opt/tomcat/webapps/ROOT.war ]; then \
        cp /opt/tomcat/webapps/ROOT.war /opt/tomcat/webapps/guacamole.war; \
    fi

# nginx runtime каталоги
RUN mkdir -p /var/lib/nginx/tmp /run/nginx /var/log/nginx \
    && chown -R nginx:nginx /var/lib/nginx /run/nginx /var/log/nginx 2>/dev/null || true

# Wrapper inline — nginx в foreground (background-jobом), proxy в restart-loop
RUN printf '%s\n' \
    '#!/bin/sh' \
    'set -u' \
    '' \
    'echo "[wrapper] nginx config test..."' \
    'nginx -t || { echo "[wrapper] nginx config invalid"; exit 1; }' \
    '' \
    'echo "[wrapper] starting nginx foreground (PID-bg) on :8080..."' \
    "nginx -g 'daemon off;' &" \
    'NGINX_PID=$!' \
    'echo "[wrapper] nginx started, PID=$NGINX_PID"' \
    '' \
    'echo "[wrapper] launching Python FastAPI proxy on :9000 in restart-loop..."' \
    '(' \
    '    while true; do' \
    '        /opt/proxy-venv/bin/python -u /opt/proxy.py 2>&1 | while IFS= read -r line; do' \
    '            echo "[proxy] $line"' \
    '        done' \
    '        echo "[wrapper] proxy exited, restarting in 3s..."' \
    '        sleep 3' \
    '    done' \
    ') &' \
    '' \
    'sleep 2' \
    'if ! kill -0 $NGINX_PID 2>/dev/null; then' \
    '    echo "[wrapper] !!! nginx died immediately after start"' \
    'else' \
    '    echo "[wrapper] nginx PID=$NGINX_PID alive after 2s, probing localhost:8080..."' \
    '    curl -sS -o /dev/null -w "[wrapper] curl :8080 → HTTP %{http_code}\n" http://127.0.0.1:8080/ || echo "[wrapper] !!! curl :8080 failed"' \
    'fi' \
    '' \
    'echo "[wrapper] handing off to base /startup.sh (Tomcat/guacd/postgres)..."' \
    'exec /startup.sh "$@"' \
    > /opt/wrapper.sh \
    && chmod +x /opt/wrapper.sh

EXPOSE 8080
ENTRYPOINT ["/opt/wrapper.sh"]
