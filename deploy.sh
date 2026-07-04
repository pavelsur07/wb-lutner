#!/usr/bin/env bash
# Повторный деплой кода с локальной машины на прод-VPS.
# ПЕРВЫЙ раз сервер надо провижинить: bootstrap_server.sh (см. README).
#
#   ./deploy.sh root@1.2.3.4        (или wblutner@1.2.3.4)
#
# Код — синхронизируется. .env / data / logs — НЕТ.
set -euo pipefail

PROD="${1:-root@YOUR_PROD_IP}"
APP=/opt/wb-lutner
APPUSER=wblutner

echo ">>> rsync -> ${PROD}:${APP}"
rsync -az --delete \
  --exclude='.env' --exclude='data/' --exclude='logs/' \
  --exclude='venv/' --exclude='.git/' --exclude='__pycache__/' \
  --exclude='*.sqlite*' \
  ./ "${PROD}:${APP}/"

echo ">>> remote: deps + миграции + рестарт"
ssh "${PROD}" bash -s <<REMOTE
set -e
# app-команды всегда от ${APPUSER} (иначе БД будет root-owned и gunicorn не запишет)
chown -R ${APPUSER}:${APPUSER} ${APP} 2>/dev/null || true
as_app() {
  if [ "\$(id -un)" = "root" ]; then sudo -u ${APPUSER} bash -c "cd ${APP} && \$1";
  else bash -c "cd ${APP} && \$1"; fi
}
as_app "venv/bin/pip install -q -r requirements.txt"
as_app "venv/bin/python -m lib.db"
systemctl restart wb-lutner-webhook 2>/dev/null || sudo systemctl restart wb-lutner-webhook
echo -n "service: "; systemctl is-active wb-lutner-webhook
REMOTE
echo ">>> Деплой завершён."
