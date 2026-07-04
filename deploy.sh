#!/usr/bin/env bash
# Деплой с локальной машины на прод-VPS. Код — да, .env/data/logs — нет.
set -euo pipefail

PROD="${1:-wblutner@YOUR_PROD_IP}"   # ./deploy.sh wblutner@1.2.3.4
DEST="/opt/wb-lutner"

rsync -az --delete \
  --exclude='.env' --exclude='data/' --exclude='logs/' \
  --exclude='venv/' --exclude='.git/' --exclude='__pycache__/' \
  ./ "${PROD}:${DEST}/"

ssh "${PROD}" "cd ${DEST} && venv/bin/pip install -q -r requirements.txt \
  && venv/bin/python -m lib.db \
  && sudo systemctl restart wb-lutner-webhook \
  && systemctl is-active wb-lutner-webhook"
echo 'Деплой завершён.'
