# Команда для синхронизации секретов и временных папок
cd ~/projects/wb-lutner
rsync -az --exclude='.env' --exclude='data/' --exclude='logs/' \
--exclude='venv/' --exclude='.git/' --exclude='__pycache__/' \
./ root@201.51.31.211:/opt/wb-lutner/


# Первый деплой на голый сервер (не deploy.sh, он для повторных)
## 1) залить код (rsync; если нет — sudo apt install rsync)
rsync -az --exclude='.env' --exclude='data/' --exclude='logs/' \
--exclude='venv/' --exclude='.git/' --exclude='__pycache__/' \
./ root@201.51.31.211:/opt/wb-lutner/

# 2) провижининг: пакеты, пользователь wblutner, venv, зависимости, systemd
ssh root@201.51.31.211 'bash /opt/wb-lutner/bootstrap_server.sh'