"""Selenium Chrome session management for MAX web client.

Builds a Chrome WebDriver with:
- Persistent user-data directory (so QR auth survives restarts).
- Russian locale (MAX is a Russian messenger).
- Mild anti-automation flags to look more like a regular browser.
"""
from __future__ import annotations

from pathlib import Path

from loguru import logger
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

import config


def _build_options(headless: bool) -> Options:
    options = Options()

    if headless:
        options.add_argument("--headless=new")

    chrome_binary = getattr(config, "CHROME_BINARY_PATH", None)
    if chrome_binary:
        options.binary_location = str(chrome_binary)

    profile_dir = Path(config.BROWSER_PROFILE_DIR).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")

    options.add_argument(f"--lang={config.BROWSER_LANGUAGE}")
    options.add_argument("--window-size=1280,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")

    download_dir = Path(config.DOWNLOAD_DIR).resolve()
    download_dir.mkdir(parents=True, exist_ok=True)

    options.add_experimental_option(
        "prefs",
        {
            "intl.accept_languages": config.BROWSER_ACCEPT_LANGUAGES,
            "profile.default_content_setting_values.notifications": 2,
            # Auto-save downloads (PDFs and other documents) into a known
            # directory so we can pick them up and re-upload via Telegram.
            "download.default_directory": str(download_dir),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "plugins.always_open_pdf_externally": True,
        },
    )
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    return options


def create_driver(headless: bool = False) -> webdriver.Chrome:
    """Create and return a Chrome WebDriver with Russian locale and persistent profile."""
    logger.info(
        "Starting Chrome (headless={}, lang={}, profile={})",
        headless,
        config.BROWSER_LANGUAGE,
        config.BROWSER_PROFILE_DIR,
    )

    options = _build_options(headless=headless)

    chromedriver_path = getattr(config, "CHROMEDRIVER_PATH", None)
    if chromedriver_path and Path(chromedriver_path).is_file():
        logger.info("Using explicit chromedriver: {}", chromedriver_path)
        service = Service(executable_path=str(chromedriver_path))
        driver = webdriver.Chrome(options=options, service=service)
    else:
        logger.info("Using Selenium Manager to locate chromedriver")
        driver = webdriver.Chrome(options=options)

    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": (
                    "Object.defineProperty(navigator, 'webdriver',"
                    " {get: () => undefined});"
                ),
            },
        )
    except Exception:
        logger.exception("Failed to apply anti-automation CDP override")

    return driver
