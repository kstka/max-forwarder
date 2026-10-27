# Установка на сервере

Нужны Linux с systemd, Python 3.13+, Google Chrome и Git. Команды ниже
рассчитаны на каталог `/opt/max-forwarder` и пользователя `max-forwarder`.

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin max-forwarder
sudo git clone https://github.com/kstka/max-forwarder.git /opt/max-forwarder
sudo chown -R max-forwarder:max-forwarder /opt/max-forwarder
sudo -u max-forwarder python3 -m venv /opt/max-forwarder/.venv
sudo -u max-forwarder /opt/max-forwarder/.venv/bin/pip install -r /opt/max-forwarder/requirements.txt
sudo -u max-forwarder cp /opt/max-forwarder/config.example.py /opt/max-forwarder/config.py
sudo -u max-forwarder editor /opt/max-forwarder/config.py
```

В `config.py` укажите токен Telegram-бота, ID чата и ссылку на канал MAX.
Если Chrome или chromedriver установлены не в стандартных местах, задайте их
пути в этом же файле.

Первый вход по QR-коду выполните в локальной копии проекта командой
`python login.py`. Затем перенесите профиль на сервер под своим SSH-пользователем
и выдайте права служебному пользователю:

```bash
rsync -av data/browser-profile/ \
  <ssh_user>@<сервер>:/tmp/max-browser-profile/
ssh <ssh_user>@<сервер> \
  'sudo mkdir -p /opt/max-forwarder/data/browser-profile && \
   sudo rsync -a /tmp/max-browser-profile/ /opt/max-forwarder/data/browser-profile/ && \
   sudo chown -R max-forwarder:max-forwarder /opt/max-forwarder/data/browser-profile'
```

Установите и запустите службу:

```bash
sudo cp /opt/max-forwarder/deploy/max-forwarder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now max-forwarder
journalctl -u max-forwarder -f
```
