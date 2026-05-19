#!/usr/bin/env bash
# =============================================================================
# Entrypoint: запускаем Guacamole (через стандартный entrypoint образа)
# в фоне, плюс наш FastAPI-прокси на 9000. Nginx внутри образа
# проксирует / на Tomcat — мы дополнительно перехватываем /api/proxy/*
# для динамических подключений.
# =============================================================================
set -e

# Дефолтный admin для Guacamole REST API (из flcontainers/guacamole)
# Можно переопределить через env GUAC_ADMIN_USER / GUAC_ADMIN_PASS
export GUAC_ADMIN_USER="${GUAC_ADMIN_USER:-guacadmin}"
export GUAC_ADMIN_PASS="${GUAC_ADMIN_PASS:-guacadmin}"

# Запускаем штатный стартап Guacamole в фоне (nginx+tomcat+guacd)
/opt/guacamole/start.sh &
GUAC_PID=$!

echo "[start.sh] Guacamole stack PID=$GUAC_PID, waiting for it to come up..."

# Ждём пока поднимется Tomcat (порт 8080 внутри контейнера, проверяем через nginx)
for i in $(seq 1 60); do
  if curl -sf -o /dev/null "http://127.0.0.1:8080/guacamole/api/session/data/postgresql/users" 2>/dev/null \
     || curl -sf -o /dev/null "http://127.0.0.1:8080/guacamole/" 2>/dev/null; then
    echo "[start.sh] Guacamole UP after ${i}s"
    break
  fi
  sleep 1
done

# Запускаем наш Python-прокси на :9000 (внутренний)
echo "[start.sh] launching FastAPI proxy on :9000"
/opt/proxy-venv/bin/python /opt/proxy.py &
PROXY_PID=$!

# Подменяем nginx-config чтобы /api/proxy/* шли на 9000, а / на Tomcat
cat > /etc/nginx/conf.d/proxy.conf <<'NGINX'
server {
    listen 8080 default_server;
    server_name _;

    # Наш прокси (динамические подключения)
    location /api/proxy/ {
        proxy_pass http://127.0.0.1:9000/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }

    # Guacamole — корень + WebSocket для туннеля
    location / {
        proxy_pass http://127.0.0.1:8081/;   # Tomcat в flcontainers — на 8081
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }
}
NGINX

# Перезапускаем nginx чтобы подхватить новую конфигурацию
nginx -s reload || true

echo "[start.sh] all up. Proxy=$PROXY_PID, Guacamole=$GUAC_PID"
# Ждём чтобы не вышли процессы — Railway убьёт контейнер если он завершится
wait $GUAC_PID $PROXY_PID
