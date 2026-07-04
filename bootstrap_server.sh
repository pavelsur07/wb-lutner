#!/usr/bin/env bash
# ОДНОРАЗОВЫЙ провижининг свежего VPS. Запускать ОТ ROOT, ПОСЛЕ того как
# код уже залит в /opt/wb-lutner (см. первый деплой в README).
#
#   ssh root@IP 'bash /opt/wb-lutner/bootstrap_server.sh'
#
# Идемпотентно: повторный запуск не ломает уже настроенное.
set -euo pipefail

APP=/opt/wb-lutner
APPUSER=wblutner

echo ">>> [1/6] Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git sqlite3 rsync \
                      nginx certbot python3-certbot-nginx
timedatectl set-timezone Europe/Moscow || true

echo ">>> [2/6] Пользователь $APPUSER"
id -u "$APPUSER" &>/dev/null || adduser --disabled-password --gecos "" "$APPUSER"

echo ">>> [3/6] Права на $APP"
mkdir -p "$APP/data" "$APP/logs"
chown -R "$APPUSER:$APPUSER" "$APP"

echo ">>> [4/6] venv + зависимости"
if [ ! -x "$APP/venv/bin/python" ]; then
    sudo -u "$APPUSER" python3 -m venv "$APP/venv"
fi
sudo -u "$APPUSER" "$APP/venv/bin/pip" install --upgrade pip -q
sudo -u "$APPUSER" "$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

echo ">>> [5/6] .env"
if [ ! -f "$APP/.env" ]; then
    sudo -u "$APPUSER" cp "$APP/.env.example" "$APP/.env"
    chmod 600 "$APP/.env"
    chown "$APPUSER:$APPUSER" "$APP/.env"
    NEED_ENV=1
fi

echo ">>> [6/6] systemd-юнит"
cp "$APP/systemd/wb-lutner-webhook.service" /etc/systemd/system/
systemctl daemon-reload

echo
echo "=========================================================="
echo " Провижининг завершён. Дальше ВРУЧНУЮ:"
echo
if [ "${NEED_ENV:-0}" = "1" ]; then
echo " 1) Заполнить секреты:   nano $APP/.env   (chmod 600 уже стоит)"
echo "    WEBHOOK_SECRET_PATH: python3 -c 'import secrets;print(secrets.token_urlsafe(24))'"
fi
echo " 2) Создать БД:          sudo -u $APPUSER $APP/venv/bin/python -m lib.db"
echo " 3) Запустить сервис:    systemctl enable --now wb-lutner-webhook"
echo "                         systemctl status wb-lutner-webhook"
echo " 4) Nginx + HTTPS и cron — по SETUP.md (нужен домен, не голый IP)."
echo "=========================================================="
