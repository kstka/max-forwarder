"""Project configuration template.

Copy this file to ``config.py`` and fill in real values.
``config.py`` is intentionally git-ignored.
"""
from __future__ import annotations

from pathlib import Path

# Project root and data directories
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
BROWSER_PROFILE_DIR = DATA_DIR / "browser-profile"
STATE_DIR = DATA_DIR / "state"
LOGS_DIR = DATA_DIR / "logs"
DRIVERS_DIR = DATA_DIR / "drivers"
DOWNLOAD_DIR = DATA_DIR / "downloads"

# Optional explicit paths for Chrome and chromedriver.
# When set, Selenium uses these instead of trying to auto-download via Selenium Manager.
# Leave as ``None`` to rely on Selenium Manager (requires network access to
# googlechromelabs.github.io / storage.googleapis.com).
CHROME_BINARY_PATH: str | None = "/usr/bin/google-chrome"
CHROMEDRIVER_PATH: str | None = str(DRIVERS_DIR / "chromedriver-linux64" / "chromedriver")

# Forwarded messages persistent store (JSON)
FORWARDED_STORE_PATH = STATE_DIR / "forwarded.json"

# Browser locale (MAX is a Russian-language messenger)
BROWSER_LANGUAGE = "ru-RU"
BROWSER_ACCEPT_LANGUAGES = "ru-RU,ru"

# MAX web entry point
MAX_HOME_URL = "https://web.max.ru/"

# Telegram Bot API
TELEGRAM_BOT_TOKEN = "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE"

# Single MAX channel to monitor and the Telegram chat id to forward into.
# Use a full MAX channel URL and a Telegram chat id (e.g. "-1001234567890" or "@channel").
MAX_CHANNEL_URL = "https://web.max.ru/<channel-id>"
TELEGRAM_CHAT_ID = "PUT_TELEGRAM_CHAT_ID_HERE"

# Telegram chat id of the bot operator. When set, the bot pings this chat
# once if it stops seeing messages in the MAX channel (e.g. session lost or
# DOM selectors broke). Leave as an empty string to disable.
TELEGRAM_ADMIN_CHAT_ID = ""

# Optional whitelist filters. If empty, every message is forwarded.
# If non-empty, a message is forwarded only when at least one filter
# substring is found in its searchable text (body, quote text,
# attachment captions, attachment filenames). Matching is
# case-insensitive. Messages that fail the filter are silently marked
# as seen so they are not re-evaluated on every poll.
MESSAGE_FILTERS: list[str] = []

# Human-like timing ranges (seconds). Used by the monitor/collector.
DELAY_AFTER_OPEN = (2.0, 5.0)
# How often to re-scan the channel timeline (without reloading the page).
LIVE_POLL_INTERVAL = (3.0, 8.0)

# Grace period after the first successful collect on startup (seconds).
# During this window every collected message is silently marked as seen
# without being forwarded. This gives the Svelte virtualized list time to
# finish rendering rows that were not yet in the DOM when the channel first
# opened. Set to 0 to disable (forward immediately).
STARTUP_GRACE_SECONDS = 45.0

# Safety guard for the public group: when a single poll suddenly sees
# this many or more unseen messages it is almost certainly store /
# fingerprint drift or a history-window shift, not a live burst. Such
# batches are absorbed (marked seen) with a loud WARNING instead of
# being forwarded.
NEW_MESSAGE_BURST_LIMIT = 5

# Periodically scroll the MAX history pane to the bottom so virtualized rows
# for the newest messages stay mounted and visible to the collector.
# Random delay between scroll attempts (seconds); set to None to disable.
HISTORY_SCROLL_INTERVAL: tuple[float, float] | None = (45.0, 90.0)

# Maximum time to wait for a file download triggered by clicking the
# "Скачать" button on a MAX file card.
FILE_DOWNLOAD_TIMEOUT = 60.0

# Logging
LOG_LEVEL = "INFO"
LOG_FILE = LOGS_DIR / "app.log"
LOG_ROTATION = "10 MB"
LOG_RETENTION = "14 days"
LOG_COMPRESSION = "zip"
