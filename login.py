"""First-run helper: log in to MAX via QR and verify a channel can be opened.

Usage:
    python login.py

Steps:
1. Launches Chrome with the persistent profile in headful mode.
2. Opens the MAX web home page so you can scan the QR code from the mobile app.
3. Waits for you to confirm in the terminal that login succeeded.
4. Navigates to the test channel and waits again so you can inspect the page.
5. Closes the browser, leaving the authenticated session in the profile directory.
"""
from __future__ import annotations

import sys

from loguru import logger

import config
from logging_setup import setup_logging
from session_manager import create_driver


TEST_CHANNEL_URL = config.MAX_CHANNEL_URL


def _wait_for_user(prompt: str) -> None:
    print()
    print("=" * 70)
    print(prompt)
    print("=" * 70)
    try:
        input()
    except EOFError:
        pass


def main() -> int:
    setup_logging()

    driver = create_driver(headless=False)
    try:
        logger.info("Opening MAX home: {}", config.MAX_HOME_URL)
        driver.get(config.MAX_HOME_URL)

        _wait_for_user(
            "Откройте мобильное приложение MAX и отсканируйте QR-код в браузере.\n"
            "Когда увидите интерфейс мессенджера, нажмите Enter в этом терминале."
        )

        logger.info("Opening channel: {}", TEST_CHANNEL_URL)
        driver.get(TEST_CHANNEL_URL)

        _wait_for_user(
            "Канал открыт. Осмотрите страницу в браузере.\n"
            "Нажмите Enter, чтобы закрыть браузер (сессия сохранится в профиле)."
        )
        return 0
    finally:
        try:
            driver.quit()
            logger.info("Chrome closed; profile preserved at {}", config.BROWSER_PROFILE_DIR)
        except Exception:
            logger.exception("Error while closing the driver")


if __name__ == "__main__":
    sys.exit(main())
