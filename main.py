"""Entry point: run the MAX -> Telegram forwarder.

The bot watches a single MAX channel (``config.MAX_CHANNEL_URL``) and
forwards every new message to ``config.TELEGRAM_CHAT_ID``.

Defaults to a visible (headful) Chrome window for development. Pass
``--headless`` when running on a server. Use ``--discover`` to dump the channel
page and inspect the DOM when refining selectors. Use ``--once`` to process the
channel exactly once, ``--dry-run`` to skip real Telegram sends.
"""
from __future__ import annotations

import argparse
import sys

from loguru import logger

import config
from collector import MessageCollector
from logging_setup import setup_logging
from scheduler import ChannelScheduler
from session_manager import create_driver
from store import ForwardedStore
from telegram_relay import TelegramRelay


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MAX -> Telegram forwarder")
    parser.add_argument(
        "--headless",
        action="store_true",
        help=(
            "Run Chrome in headless mode. Default is a visible window for "
            "easier development; pass this flag when deploying to a server."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process the configured channel exactly once and exit.",
    )
    parser.add_argument(
        "--catchup",
        action="store_true",
        help=(
            "One-shot recovery pass: forward everything collected after "
            "the stored tail anchor and not yet forwarded (the tail the "
            "live monitor missed during an outage). Run while the live "
            "service is stopped. Combine with --dry-run to preview the "
            "batch without sending or marking anything."
        ),
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help=(
            "Open the configured channel, save HTML and a screenshot, "
            "log a sample of detected messages, and exit. Useful for tuning "
            "DOM selectors."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not send real Telegram messages; only log what would be sent.",
    )
    return parser.parse_args()


def _run_discover(driver, channel_url: str) -> int:
    logger.info("Discovery: opening {}", channel_url)
    driver.get(channel_url)
    try:
        input(
            "Press Enter after the channel is fully loaded "
            "to dump HTML and a screenshot... "
        )
    except EOFError:
        pass

    collector = MessageCollector(driver)
    html_path, png_path = collector.dump_for_discovery(
        config.STATE_DIR / "discover"
    )
    logger.info("Saved page HTML -> {}", html_path)
    logger.info("Saved screenshot -> {}", png_path)

    sample = collector.collect(channel_url, limit=20)
    logger.info("Collector returned {} message(s)", len(sample))
    for m in sample[-10:]:
        logger.info(
            "  id={} author={!r} ts={!r} text={!r} att={}",
            m.id, m.author, m.timestamp, m.text[:120], len(m.attachments),
        )
    return 0


def main() -> int:
    args = parse_args()
    setup_logging()

    driver = create_driver(headless=args.headless)
    try:
        if not config.MAX_CHANNEL_URL:
            logger.error("No MAX_CHANNEL_URL configured in config.py")
            return 2
        if not config.TELEGRAM_CHAT_ID:
            logger.error("No TELEGRAM_CHAT_ID configured in config.py")
            return 2

        if args.discover:
            return _run_discover(driver, config.MAX_CHANNEL_URL)

        store = ForwardedStore()
        relay = TelegramRelay(dry_run=True if args.dry_run else None)
        scheduler = ChannelScheduler(
            driver=driver,
            store=store,
            relay=relay,
            channel_url=config.MAX_CHANNEL_URL,
            chat_id=config.TELEGRAM_CHAT_ID,
            admin_chat_id=getattr(config, "TELEGRAM_ADMIN_CHAT_ID", ""),
            message_filters=getattr(config, "MESSAGE_FILTERS", []),
        )

        if args.once:
            scheduler.run_once()
        elif args.catchup:
            if not scheduler.run_catchup(dry_run=args.dry_run):
                return 1
        else:
            scheduler.run_forever()
        return 0

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130
    finally:
        try:
            driver.quit()
            logger.info(
                "Chrome closed; profile preserved at {}",
                config.BROWSER_PROFILE_DIR,
            )
        except Exception:
            logger.exception("Error while closing the driver")


if __name__ == "__main__":
    sys.exit(main())
