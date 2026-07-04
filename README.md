# wb-lutner

Скрипт-мост Wildberries FBS ↔ Lutner (дропшиппинг). Python 3.11+, SQLite (WAL), Flask+Gunicorn+Nginx.

## Топология

- **Git (private)** — единственный источник правды для кода. Секретов в нём нет.
- **Dev-машина (Claude Code)** — разработка: `git pull` → правки → `git push`.
- **Локальная (PhpStorm)** — хранит боевой `.env`, отсюда деплой на прод: `git pull` → `./deploy.sh`.
- **Прод-VPS** — Ubuntu 22.04, 1 CPU / 1 GB. Свой `.env` (chmod 600), запуск через systemd.

## Локальная разработка (dev-машина)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env        # заполнить плейсхолдерами для локальных тестов
venv/bin/python -m lib.db   # создать/мигрировать БД
```

## Деплой (с локальной машины)

```bash
./deploy.sh wblutner@PROD_IP
```

Секреты (`.env`) на прод кладутся **вручную один раз** и не перезаписываются деплоем:

```bash
scp .env wblutner@PROD_IP:/opt/wb-lutner/.env
ssh wblutner@PROD_IP 'chmod 600 /opt/wb-lutner/.env'
```

## Диагностика

```bash
sudo systemctl status wb-lutner-webhook
sudo journalctl -u wb-lutner-webhook -f
sqlite3 data/db.sqlite ".schema"
sqlite3 data/db.sqlite "SELECT key,value,updated_at FROM system_state"
curl https://YOUR_DOMAIN/health
```

## Этапы (см. ТЗ)

1. Инфраструктура + каркас — **этот скелет** (`lib/config.py`, `lib/db.py`).
2. `sync_catalog.py`, `lib/mailer.py`.
3. `webhook_server.py`, nginx, systemd.
4. `check_heartbeat.py`, бэкапы, UptimeRobot.
5. `main.py`, `initial_mapping.py`, `sync_stock_to_wb.py`, `lib/{wb,lutner}_api.py`.
6. `morning_report.py`, `daily_summary.py`, `check_balance.py`.
