# MAX → Telegram Forwarder

Скрипт следит за одним каналом в веб-версии MAX и пересылает новые
сообщения в указанный чат Telegram. Поддерживаются текст, фотографии и
файлы. Сессия MAX сохраняется в локальном профиле Chrome, поэтому QR-код
нужно подтвердить только при первом запуске.

## Быстрый старт

Нужны Python 3.13+, Google Chrome и Git.

```bash
git clone https://github.com/kstka/max-forwarder.git max-forwarder
cd max-forwarder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.py config.py
```

Заполните в `config.py` значения `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` и
`MAX_CHANNEL_URL`. Затем выполните первый вход в MAX:

```bash
python login.py
```

После подтверждения QR-кода запустите монитор:

```bash
python main.py
```

`config.py` и каталог `data/` не попадают в Git: там хранятся токен бота,
сессия браузера, состояние пересылки, загрузки и логи.

Для запуска на сервере через systemd — [INSTALL.md](INSTALL.md).
