#!/bin/sh
# =============================================================================
# Wrapper entrypoint: запускает nginx + Python proxy в фоне,
# затем exec'ает оригинальный /startup.sh (supervisord базового образа).
#
# Базовый supervisord конфиг не читает /etc/supervisor.d/, поэтому drop-ins
# не подхватываются. Запускаем наши процессы напрямую.
# =============================================================================
set -e

echo "[wrapper] starting nginx..."
# nginx в daemon-режиме (он сам форкается)
nginx || { echo "[wrapper] nginx failed to start"; }

echo "[wrapper] starting Python FastAPI proxy on :9000..."
(
    while true; do
        /opt/proxy-venv/bin/python /opt/proxy.py >> /tmp/proxy.log 2>&1
        echo "[wrapper] proxy exited, restarting in 3s..."
        sleep 3
    done
) &

echo "[wrapper] handing off to base /startup.sh..."
exec /startup.sh "$@"
