# SETUP — запуск на прод-VPS

Порядок вывода в бой. Все команды на прод-сервере (Ubuntu 22.04), если не сказано иное.

## 0. Пользователь и пакеты
```bash
sudo adduser --disabled-password --gecos "" wblutner
sudo apt update && sudo apt install -y python3-venv git sqlite3 nginx certbot python3-certbot-nginx
sudo timedatectl set-timezone Europe/Moscow
sudo mkdir -p /opt/wb-lutner && sudo chown wblutner:wblutner /opt/wb-lutner
```

## 1. Код и окружение
С локальной машины: `./deploy.sh wblutner@PROD_IP` (или `rsync`/`git pull`).
Затем на сервере:
```bash
cd /opt/wb-lutner
python3 -m venv venv
venv/bin/pip install -r requirements.txt
# при желании PDF-этикеток: venv/bin/pip install reportlab
```

## 2. Секреты (вручную, один раз)
```bash
cp .env.example .env && nano .env          # заполнить реальные значения
python3 -c "import secrets;print(secrets.token_urlsafe(24))"   # -> WEBHOOK_SECRET_PATH
chmod 600 .env
venv/bin/python -m lib.db                    # создать БД (journal_mode=wal)
```

## 3. Webhook-сервис
```bash
sudo cp systemd/wb-lutner-webhook.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now wb-lutner-webhook
sudo systemctl status wb-lutner-webhook
```

## 4. Nginx + HTTPS
В `nginx/wb-lutner.conf` заменить `YOUR_DOMAIN`. В `/etc/nginx/nginx.conf` (http-блок):
```
limit_req_zone $binary_remote_addr zone=webhook_limit:10m rate=30r/m;
```
Затем:
```bash
sudo cp nginx/wb-lutner.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/wb-lutner.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d YOUR_DOMAIN
curl https://YOUR_DOMAIN/health          # {"status":"ok"}
```
URL вебхука `https://YOUR_DOMAIN/webhook/stock/<WEBHOOK_SECRET_PATH>` передать менеджеру Lutner.

## 5. Cron
```bash
sudo -u wblutner crontab crontab.example   # ПЕРВУЮ НЕДЕЛЮ main.py держать в --dry-run
```

## 6. Первичный маппинг (после первой синхронизации каталога)
```bash
venv/bin/python sync_catalog.py
sqlite3 data/db.sqlite "SELECT COUNT(*) FROM catalog WHERE barcode!=''"
venv/bin/python initial_mapping.py         # отчёт: сматчилось / не найдено
```

## 7. Мониторинг
- UptimeRobot: HTTP-monitor на `https://YOUR_DOMAIN/health`, интервал 5 мин, alert на email.
- Через 2–3 дня: по `webhook_log` определить IP Lutner и добавить allowlist в nginx.

## Диагностика
```bash
sudo journalctl -u wb-lutner-webhook -f
tail -f logs/*.log
sqlite3 data/db.sqlite "SELECT key,value,updated_at FROM system_state"
sqlite3 data/db.sqlite "SELECT status,COUNT(*) FROM orders GROUP BY status"
```

## Чек-лист перед боем
- [ ] `.env` chmod 600, в git не попал
- [ ] `venv/bin/python -m lib.db` -> journal_mode=wal, 6 таблиц
- [ ] `/health` отвечает 200 по HTTPS
- [ ] тестовый webhook кладёт строку в `stock` и `webhook_log`
- [ ] systemd автозапуск проверен через `sudo reboot`
- [ ] cron стоит, `main.py` в `--dry-run` первую неделю
- [ ] бэкап БД создаётся, ротация 7 дней
- [ ] UptimeRobot и heartbeat активны
