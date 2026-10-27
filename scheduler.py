"""Single-channel MAX live monitor.

Watches one MAX channel in the open browser tab and forwards new messages via
the Telegram relay. The bot opens the channel **once** and then keeps the
SPA tab alive, so live websocket updates from MAX populate the timeline as
new messages arrive. We re-read the visible timeline at a short
``LIVE_POLL_INTERVAL`` and forward whatever ids are missing from the store.
Periodically (see ``config.HISTORY_SCROLL_INTERVAL``) we scroll the history
pane to the bottom so virtualized rows for recent messages stay visible to
the collector.

Startup rules:
- On each process start, the first non-empty collected DOM batch is silently
  registered as "already seen". This makes the monitor live-only: Telegram
  receives only messages that appear after startup.
- On subsequent polls, only messages whose ids are missing from the store are
  forwarded.

For file attachments (e.g. PDFs) the relay needs the bytes, but MAX exposes
no direct download URL — the file is fetched by clicking a JS-driven button.
The scheduler clicks each file's button and waits for Chrome to save the
file into ``config.DOWNLOAD_DIR`` before passing the local path to the relay.
"""
from __future__ import annotations

import os
import random
import time
from pathlib import Path

from loguru import logger
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait

import config
from collector import (
    Attachment,
    COLLECT_FAILURE_INVALID_RESPONSE,
    COLLECT_FAILURE_NO_HISTORY,
    COLLECT_FAILURE_SCRIPT_TIMEOUT,
    COLLECT_FAILURE_WEBDRIVER,
    Message,
    MessageCollector,
    _is_fatal_driver_error,
)
from store import ForwardedStore
from telegram_relay import TelegramRelay


# Chrome marks in-progress downloads with a ".crdownload" suffix.
_CHROME_PARTIAL_SUFFIX = ".crdownload"

# MAX renders poll controls as ordinary bubble text.  Keep both static
# instructions together to avoid treating a regular discussion of voting as
# a poll.
_MAX_POLL_TEXT_MARKERS = (
    "выберите вариант ответа",
    "выбор сразу отправит голос",
)

# Reason codes for "the bot lost sight of the channel". Used to pick a
# human-readable line for the admin alert and to keep log strings stable.
_REASON_NAVIGATION_FAILED = "navigation_failed"
_REASON_LOGIN_PAGE = "login_page"
_REASON_HISTORY_TIMEOUT = "history_timeout"
_REASON_EMPTY_TIMELINE = "empty_timeline"
_REASON_SCRIPT_TIMEOUT = "script_timeout"
_REASON_WEBDRIVER = "webdriver_error"
_REASON_COLLECT_RESPONSE = "invalid_collect_response"

# Russian-language admin alert lines, indexed by reason code. The admin
# chat is the operator's; per project convention only code/comments stay
# English.
_OUTAGE_ALERT_TEXTS: dict[str, str] = {
    _REASON_NAVIGATION_FAILED: (
        "не удалось загрузить страницу канала (ошибка WebDriver)."
    ),
    _REASON_LOGIN_PAGE: (
        "сессия в MAX истекла — требуется новый QR-код. "
        "Запусти `python login.py`."
    ),
    _REASON_HISTORY_TIMEOUT: (
        "канал не отрисовал контейнер истории. "
        "Возможно, поменялся DOM или сломалась загрузка SPA."
    ),
    _REASON_EMPTY_TIMELINE: (
        "канал открыт и скрипт коллектора выполнился, но не обнаружил "
        "ни одного сообщения. Возможно, изменилась разметка канала."
    ),
    _REASON_SCRIPT_TIMEOUT: (
        "истекло время выполнения скрипта сбора в браузере. "
        "Это не подтверждает поломку DOM-селекторов; проверь нагрузку "
        "сервера и состояние Chrome."
    ),
    _REASON_WEBDRIVER: (
        "временная ошибка WebDriver при чтении канала. "
        "Это не подтверждает поломку DOM-селекторов; проверь Chrome и "
        "нагрузку сервера."
    ),
    _REASON_COLLECT_RESPONSE: (
        "коллектор получил некорректный ответ от страницы канала. "
        "Проверь состояние SPA и журналы браузера."
    ),
}

_COLLECT_FAILURE_TO_OUTAGE_REASON = {
    COLLECT_FAILURE_SCRIPT_TIMEOUT: _REASON_SCRIPT_TIMEOUT,
    COLLECT_FAILURE_WEBDRIVER: _REASON_WEBDRIVER,
    COLLECT_FAILURE_NO_HISTORY: _REASON_HISTORY_TIMEOUT,
    COLLECT_FAILURE_INVALID_RESPONSE: _REASON_COLLECT_RESPONSE,
}


# Scroll the channel history scroller to the bottom (virtualized list anchor).
_HISTORY_SCROLL_JS = r"""
return (function() {
    const history = document.querySelector('div.history');
    if (!history) return false;
    const scroller = history.querySelector('.scrollable.scrollListScrollable')
        || history.querySelector('.scrollable');
    if (!scroller) return false;
    scroller.scrollTop = scroller.scrollHeight;
    return true;
})();
"""


def _sleep_random(rng: tuple[float, float]) -> None:
    lo, hi = rng
    delay = random.uniform(lo, hi)
    logger.debug("Sleeping for {:.1f}s in [{:.1f}, {:.1f}]", delay, lo, hi)
    time.sleep(delay)


def _normalize_filters(raw: list[str] | None) -> list[str]:
    """Lower-case + drop empty entries from MESSAGE_FILTERS once at startup."""
    if not raw:
        return []
    return [item.lower() for item in raw if isinstance(item, str) and item.strip()]


def _message_haystack(msg: Message) -> str:
    """Lower-cased blob of every searchable text field on the message.

    The body, the quote text and any attachment captions / filenames
    contribute. Author and explicit URLs are intentionally excluded -
    matching against author would be a different feature, and URLs
    rarely contain meaningful keywords.
    """
    parts: list[str] = []
    if msg.text:
        parts.append(msg.text)
    if msg.quote and msg.quote.text:
        parts.append(msg.quote.text)
    for a in msg.attachments:
        if a.text:
            parts.append(a.text)
        if a.filename:
            parts.append(a.filename)
    return " ".join(parts).lower()


def _message_passes_filters(msg: Message, filters: list[str]) -> bool:
    """True when the message should be forwarded.

    Empty filter list means everything passes. Otherwise the message
    must contain at least one of the (already lower-cased) filter
    substrings somewhere in its searchable text.
    """
    if not filters:
        return True
    haystack = _message_haystack(msg)
    if not haystack:
        return False
    return any(needle in haystack for needle in filters)


def _is_max_poll_message(msg: Message) -> bool:
    """True when MAX's built-in poll controls are present in a bubble."""
    haystack = _message_haystack(msg)
    return all(marker in haystack for marker in _MAX_POLL_TEXT_MARKERS)


def _split_max_polls(messages: list[Message]) -> tuple[list[Message], list[Message]]:
    """Return (forwardable messages, MAX poll messages)."""
    forwarded: list[Message] = []
    polls: list[Message] = []
    for message in messages:
        (polls if _is_max_poll_message(message) else forwarded).append(message)
    return forwarded, polls


class ChannelScheduler:
    """Monitors a single MAX channel and forwards new messages to Telegram."""

    def __init__(
        self,
        driver: WebDriver,
        store: ForwardedStore,
        relay: TelegramRelay,
        channel_url: str,
        chat_id: str,
        admin_chat_id: str | None = None,
        message_filters: list[str] | None = None,
    ) -> None:
        self._driver = driver
        self._store = store
        self._relay = relay
        self._channel_url = channel_url
        self._chat_id = chat_id
        self._admin_chat_id = (admin_chat_id or "").strip()
        self._filters = _normalize_filters(message_filters)
        self._collector = MessageCollector(driver)
        self._channel_opened = False
        self._download_dir = Path(config.DOWNLOAD_DIR).resolve()
        self._download_dir.mkdir(parents=True, exist_ok=True)
        if self._filters:
            logger.info(
                "Message whitelist active: {} filter(s)", len(self._filters)
            )
        # One-shot guard: stay True after the operator was pinged about an
        # outage (open failure or empty timeline), so we don't spam the
        # admin chat. Reset back to False as soon as the channel returns
        # messages so a fresh outage triggers a fresh alert. _open_channel
        # failures keep the flag set forever because the process exits
        # right after.
        self._admin_alert_sent = False
        # Populated by _open_channel() whenever it returns False, so the
        # caller can pick a precise admin alert wording.
        self._last_open_failure_reason: str | None = None
        # Deadline (monotonic seconds) until which every collected message is
        # silently marked as seen.  None = grace period not started yet (we
        # are waiting for the first non-empty collect).  Once started, the
        # window lasts config.STARTUP_GRACE_SECONDS.  After the window
        # expires the deadline is reset to None and a separate guard
        # (_grace_ever_ended) prevents re-entering grace mode.
        self._startup_grace_deadline: float | None = None
        self._grace_ever_ended = False
        # Next monotonic time when we may scroll history to bottom (0 = due immediately).
        self._next_history_scroll_deadline = 0.0

    def run_forever(self) -> None:
        if not self._channel_url:
            logger.error("No MAX_CHANNEL_URL configured")
            return

        if not self._open_channel():
            self._notify_admin_open_failed()
            logger.error("Could not open channel; exiting monitor")
            return

        logger.info("Live monitor started for {}", self._channel_url)
        while True:
            try:
                self._poll_once()
            except WebDriverException as exc:
                logger.exception(
                    "WebDriver error while polling {}", self._channel_url
                )
                if _is_fatal_driver_error(exc):
                    # A renderer crash cannot be repaired by another poll or
                    # by navigating within the same poisoned session. Exit
                    # non-zero so systemd launches an entirely fresh Chrome.
                    raise RuntimeError(
                        "Fatal browser session error; requesting systemd restart"
                    ) from exc
                # Try to recover by re-opening the channel on next iteration.
                self._channel_opened = False
                if not self._open_channel():
                    self._notify_admin_open_failed()
                    raise RuntimeError(
                        "Recovery navigation failed; requesting systemd restart"
                    )
            _sleep_random(config.LIVE_POLL_INTERVAL)

    def run_once(self) -> None:
        if not self._open_channel():
            self._notify_admin_open_failed()
            return
        try:
            self._poll_once()
        except WebDriverException as exc:
            logger.exception(
                "WebDriver error while polling {}", self._channel_url
            )
            if _is_fatal_driver_error(exc):
                raise

    def run_catchup(self, dry_run: bool = False) -> bool:
        """One-shot operator-invoked catch-up forward.

        Forwards everything collected after the persisted tail anchor and
        not yet in the store -- exactly the tail ``run_forever`` would have
        forwarded had the tab stayed alive -- but without the startup
        grace and without the burst guard (both exist to protect the
        public group from *automatic* sends; this pass is explicitly
        operator-invoked and anchor-gated). Meant for manual recovery
        after an outage; run it while the live service is STOPPED,
        because the Chrome profile admits a single browser.

        With ``dry_run`` nothing is sent and nothing is marked: the
        anchor and the store stay untouched, so a subsequent real run
        sees the same tail again.
        """
        if not self._open_channel():
            self._notify_admin_open_failed()
            logger.error("Catch-up: could not open {}", self._channel_url)
            return False

        messages = self._collector.collect(self._channel_url, limit=50)
        if not messages:
            logger.error(
                "Catch-up: no messages collected for {}", self._channel_url
            )
            return False

        anchor = self._store.get_anchor(self._channel_url)
        if anchor is None:
            logger.error(
                "Catch-up: no anchor in store for {}; refusing to forward",
                self._channel_url,
            )
            return False
        known = self._store.get_known_ids(self._channel_url)
        try:
            idx = next(i for i, m in enumerate(messages) if m.id == anchor)
        except StopIteration:
            # The anchor bubble is gone from the window: its fingerprint
            # drifted (documented residual risk) or the message was
            # deleted upstream. Fall back to the store frontier -- the
            # newest store-known message in the window. Everything after
            # it is newer than anything ever seen; unknown messages
            # BEFORE it are pre-anchor history that was never absorbed
            # and must not be forwarded.
            frontier = max(
                (i for i, m in enumerate(messages) if m.id in known),
                default=None,
            )
            if frontier is None:
                logger.error(
                    "Catch-up: anchor {} is not within the {} collected "
                    "message(s) and no store-known message either; "
                    "refusing to forward and leaving the store untouched",
                    anchor, len(messages),
                )
                return False
            logger.warning(
                "Catch-up: anchor {} not in current window; falling back "
                "to store frontier at position {}/{} ({} earlier "
                "unknown message(s) skipped as pre-anchor history)",
                anchor, frontier + 1, len(messages), frontier,
            )
            idx = frontier

        appended = messages[idx + 1:]
        fresh = [m for m in appended if m.id not in known]
        if len(fresh) != len(appended):
            logger.info(
                "Catch-up: {} tail message(s) already in store, skipped",
                len(appended) - len(fresh),
            )

        if not fresh:
            logger.info(
                "Catch-up: nothing to forward ({} collected)", len(messages)
            )
            return True

        fresh, polls = _split_max_polls(fresh)
        if polls:
            logger.info(
                "Catch-up: skipped {} MAX poll message(s)", len(polls)
            )
            if not dry_run:
                self._store.mark_many(
                    self._channel_url, [message.id for message in polls]
                )

        with_payload = [m for m in fresh if self._has_forwardable_payload(m)]
        if len(with_payload) != len(fresh):
            logger.warning(
                "Catch-up: skipping {} empty (header-only) message(s)",
                len(fresh) - len(with_payload),
            )
            if not dry_run:
                self._store.mark_many(
                    self._channel_url,
                    [m.id for m in fresh if not self._has_forwardable_payload(m)],
                )
        fresh = self._drop_duplicate_ids(with_payload)
        if not fresh:
            if not dry_run:
                self._advance_anchor(messages)
            logger.info("Catch-up: no forwardable messages remain")
            return True

        logger.warning(
            "Catch-up {} for {}: {} message(s) ({} collected, anchor at "
            "position {}/{})",
            "DRY-RUN preview" if dry_run else "SENDING to Telegram",
            self._channel_url, len(fresh), len(messages), idx + 1, len(messages),
        )
        for m in fresh:
            logger.info(
                "Catch-up item: ts={} author={!r} text={!r} attachments={}",
                m.timestamp, m.author, (m.text or "")[:200], len(m.attachments),
            )
        if dry_run:
            return True

        completed = True
        for m in fresh:
            self._download_files_for(m)
            ok = self._relay.send(self._chat_id, m)
            self._cleanup_files_for(m)
            if ok:
                self._store.mark_forwarded(
                    self._channel_url, m.id, last_seen_at=m.timestamp
                )
                _sleep_random((1.0, 2.5))
            else:
                logger.warning(
                    "Catch-up: send failed at message {}; aborting, "
                    "anchor held for retry",
                    m.id,
                )
                completed = False
                break
        if completed:
            self._advance_anchor(messages)
            logger.info("Catch-up complete; anchor advanced to newest")
        return completed

    def _open_channel(self) -> bool:
        if self._channel_opened:
            return True

        logger.info("Opening channel: {}", self._channel_url)
        try:
            self._driver.get(self._channel_url)
        except WebDriverException:
            logger.exception("Failed to open channel {}", self._channel_url)
            self._last_open_failure_reason = _REASON_NAVIGATION_FAILED
            return False

        _sleep_random(config.DELAY_AFTER_OPEN)

        try:
            WebDriverWait(self._driver, 20).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            logger.warning(
                "Channel page did not finish loading: {}", self._channel_url
            )

        try:
            WebDriverWait(self._driver, 15).until(
                lambda d: d.execute_script(
                    "return !!(document.querySelector('div.history .scrollListContent--bottom')"
                    " || document.querySelector('.scrollListContent--bottom'))"
                )
            )
        except TimeoutException:
            # No history container -- either MAX showed the QR/login screen
            # or the SPA did not finish rendering. Tell them apart so the
            # admin alert can be specific.
            if self._is_login_page():
                logger.warning(
                    "Login screen detected instead of channel: {}",
                    self._channel_url,
                )
                self._last_open_failure_reason = _REASON_LOGIN_PAGE
            else:
                logger.warning(
                    "History container did not appear within timeout: {}",
                    self._channel_url,
                )
                self._last_open_failure_reason = _REASON_HISTORY_TIMEOUT
            return False

        _sleep_random((1.5, 3.5))
        self._channel_opened = True
        self._last_open_failure_reason = None
        self._next_history_scroll_deadline = 0.0
        return True

    # JS heuristic for the MAX login / QR screen. We match three signals
    # and treat any of them as "operator must scan a QR":
    #   1. URL path contains "login" or "auth".
    #   2. There's a <canvas> on the page (MAX renders the QR onto canvas).
    #   3. There's no `div.history` container.
    # If `div.history` *is* present we always return False -- the user is
    # logged in, we're just looking at a different state of the channel.
    _LOGIN_DETECT_JS = (
        "const path = (location.pathname || '').toLowerCase();"
        "const hasHistory = !!document.querySelector('div.history');"
        "if (hasHistory) return false;"
        "if (path.includes('login') || path.includes('auth')) return true;"
        "if (document.querySelector('canvas')) return true;"
        "return false;"
    )

    def _maybe_scroll_history_to_bottom(self) -> None:
        """Nudge the virtualized history toward the newest messages when due."""
        interval = getattr(config, "HISTORY_SCROLL_INTERVAL", None)
        if not interval or len(interval) != 2:
            return
        lo, hi = float(interval[0]), float(interval[1])
        if lo <= 0 or hi <= 0:
            return
        if hi < lo:
            lo, hi = hi, lo

        now = time.monotonic()
        if now < self._next_history_scroll_deadline:
            return

        if self._scroll_history_to_bottom():
            logger.info("Scrolled MAX history pane to bottom")
            time.sleep(random.uniform(0.15, 0.45))

        self._next_history_scroll_deadline = now + random.uniform(lo, hi)

    def _scroll_history_to_bottom(self) -> bool:
        try:
            return bool(self._driver.execute_script(_HISTORY_SCROLL_JS))
        except WebDriverException as exc:
            if _is_fatal_driver_error(exc):
                raise
            logger.debug("History scroll script failed", exc_info=True)
            return False

    def _is_login_page(self) -> bool:
        """Best-effort check whether the current tab shows MAX login / QR."""
        try:
            return bool(
                self._driver.execute_script(
                    f"return (function() {{ {self._LOGIN_DETECT_JS} }})();"
                )
            )
        except WebDriverException as exc:
            if _is_fatal_driver_error(exc):
                raise
            logger.debug("Login-page heuristic threw; treating as unknown")
            return False

    def _poll_once(self) -> None:
        self._maybe_scroll_history_to_bottom()

        messages = self._collector.collect(self._channel_url, limit=50)
        if not messages:
            logger.warning("No messages collected for {}", self._channel_url)
            self._notify_admin_no_messages(self._collector.last_failure_reason)
            return

        # The timeline is alive again -- arm the one-shot alert for the
        # next outage.
        self._admin_alert_sent = False

        grace = getattr(config, "STARTUP_GRACE_SECONDS", 45.0)
        now = time.monotonic()
        in_grace = (
            self._startup_grace_deadline is not None
            and now < self._startup_grace_deadline
        )
        needs_setup = (
            self._startup_grace_deadline is None
            and not self._grace_ever_ended
        )

        if needs_setup:
            # First non-empty collect → start the grace window and
            # establish the tail anchor on the newest message.
            self._startup_grace_deadline = now + max(0.0, float(grace))
            self._absorb_as_seen(messages)
            self._store.set_anchor(self._channel_url, messages[-1].id)
            logger.info(
                "Startup grace period activated for {}s at {}: "
                "{} current message(s) absorbed, anchor={}",
                max(0.0, float(grace)),
                self._channel_url,
                len(messages),
                messages[-1].id,
            )
            to_send = []
        elif in_grace:
            # Still inside the grace window — absorb everything silently
            # and keep the anchor on the newest message.
            self._absorb_as_seen(messages)
            self._store.set_anchor(self._channel_url, messages[-1].id)
            logger.info(
                "Grace: absorbed {} message(s), {:.0f}s remaining",
                len(messages),
                self._startup_grace_deadline - now,
            )
            to_send = []
        else:
            # Grace expired or never needed — normal live forwarding.
            if (
                self._startup_grace_deadline is not None
                and not self._grace_ever_ended
            ):
                logger.info(
                    "Startup grace period ended for {}; live forwarding active",
                    self._channel_url,
                )
                self._grace_ever_ended = True
                self._startup_grace_deadline = None
            to_send = self._handle_regular_visit(messages)

        to_send = self._apply_filters(to_send)
        to_send = self._drop_duplicate_ids(to_send)

        if not to_send:
            # Nothing to forward: advance the anchor so the window can
            # move on (no-op when the absorb paths already set it).
            self._advance_anchor(messages)
            logger.debug("No new messages for {}", self._channel_url)
            return

        logger.info(
            "Forwarding {} message(s) from {}", len(to_send), self._channel_url
        )
        completed = True
        for msg in to_send:
            self._download_files_for(msg)
            ok = self._relay.send(self._chat_id, msg)
            self._cleanup_files_for(msg)
            if ok:
                self._store.mark_forwarded(
                    self._channel_url, msg.id, last_seen_at=msg.timestamp
                )
                _sleep_random((1.0, 2.5))
            else:
                logger.warning(
                    "Send failed; aborting batch for {} at message {}",
                    self._channel_url, msg.id,
                )
                completed = False
                break
        if completed:
            # Whole batch sent: advance the anchor past it.
            self._advance_anchor(messages)
        else:
            # Hold the anchor: the failed message (and everything after
            # it) stays "appended" and is retried on the next poll;
            # already-sent ones are filtered by the store check.
            logger.info(
                "Anchor held; {} pending message(s) will be retried",
                len(to_send),
            )

    def _absorb_as_seen(self, messages: list[Message]) -> None:
        """Silently mark every message as seen — used during the startup grace window."""
        if not messages:
            return
        self._store.mark_many(self._channel_url, [m.id for m in messages])

    def _advance_anchor(self, messages: list[Message]) -> None:
        """Move the tail anchor to the newest collected message."""
        if messages:
            self._store.set_anchor(self._channel_url, messages[-1].id)

    def _handle_regular_visit(self, messages: list[Message]) -> list[Message]:
        """Pick the live new arrivals out of a collect.

        The channel timeline is append-only: a genuinely new message
        appears at the tail, after the newest message seen on the
        previous collect (the anchor).  Everything at or before the
        anchor is already seen by definition — window expansion
        (hydration scrolls loading older history), virtualization
        churn and fingerprint drift included.  Forwarding by
        "not in store" alone let unmarked old messages leak through
        whenever the mounted window grew (2026-09-15: the same old
        messages were re-forwarded twice as the window expanded);
        the tail-since-anchor gate closes that hole.  The store check
        below stays as defense in depth.
        """
        anchor = self._store.get_anchor(self._channel_url)
        if anchor is None:
            # No anchor established yet: refuse to forward, baseline.
            logger.warning(
                "Channel {}: no anchor established; absorbing {} "
                "message(s) WITHOUT forwarding",
                self._channel_url, len(messages),
            )
            self._absorb_as_seen(messages)
            self._store.set_anchor(self._channel_url, messages[-1].id)
            return []

        try:
            idx = next(i for i, m in enumerate(messages) if m.id == anchor)
        except StopIteration:
            # Anchor id not in the current window (fingerprint drift on
            # the anchor bubble, or the window slid): conservative
            # re-anchor, nothing forwarded.
            logger.warning(
                "Channel {}: anchor {} not in current window ({} "
                "message(s)); absorbing WITHOUT forwarding and "
                "re-anchoring on newest",
                self._channel_url, anchor, len(messages),
            )
            self._absorb_as_seen(messages)
            self._store.set_anchor(self._channel_url, messages[-1].id)
            return []

        appended = messages[idx + 1:]
        if not appended:
            return []

        # Defense in depth: skip anything the store already knows.
        known = self._store.get_known_ids(self._channel_url)
        fresh = [m for m in appended if m.id not in known]
        if len(fresh) != len(appended):
            logger.info(
                "Channel {}: {} tail message(s) already in store, skipped",
                self._channel_url, len(appended) - len(fresh),
            )

        # Public-group safety net: a quiet channel delivers one or two
        # messages per poll. A sudden cluster means something is wrong;
        # absorb loudly instead of forwarding.
        limit = getattr(config, "NEW_MESSAGE_BURST_LIMIT", 5)
        if len(fresh) >= limit:
            logger.warning(
                "Channel {}: {} fresh message(s) in one poll (limit {}) — "
                "treating as anomaly; absorbing WITHOUT forwarding. "
                "Oldest ts={} newest ts={}",
                self._channel_url, len(fresh), limit,
                min((m.timestamp or "" for m in fresh), default="?"),
                max((m.timestamp or "" for m in fresh), default="?"),
            )
            self._absorb_as_seen(fresh)
            return []
        if fresh:
            logger.info(
                "Channel {}: {} new message(s) appended after anchor",
                self._channel_url, len(fresh),
            )
        return fresh

    @staticmethod
    def _has_forwardable_payload(msg: Message) -> bool:
        """True when the message carries anything a recipient can see.

        Header-only messages -- a sender name and/or a bare forward
        label like ``Переслано: Имя`` with no body text, no resolved
        media and no file/link -- must never reach the public group:
        they render as contentless stubs (2026-10-26 incident: seven
        identical empty cards). Unresolved media placeholders
        (``media_tile`` without a URL) do not count; by the time this
        runs, the collector's hydration pass already had its chance.
        """
        if (msg.text or "").strip():
            return True
        if msg.quote and (msg.quote.text or "").strip():
            return True
        for att in msg.attachments:
            if att.type in {"file", "video", "audio", "link"}:
                return True
            if att.type == "image" and (att.url or "").startswith("http"):
                return True
        return False

    @staticmethod
    def _drop_duplicate_ids(messages: list[Message]) -> list[Message]:
        """Keep the first message per id within one send batch.

        Content fingerprints collide when several visually identical
        messages arrive in the same minute (e.g. a burst of same-size
        photo forwards). Without this guard every copy was sent, which
        put the same message into the public group 7 times on
        2026-10-26. The dropped copies share the id that gets marked
        as forwarded with the sent one.
        """
        seen: set[str] = set()
        unique: list[Message] = []
        for msg in messages:
            if msg.id in seen:
                logger.warning(
                    "Duplicate id {} within batch (fingerprint collision); "
                    "sending it only once",
                    msg.id,
                )
                continue
            seen.add(msg.id)
            unique.append(msg)
        return unique

    def _apply_filters(self, messages: list[Message]) -> list[Message]:
        """Drop unforwardable / filtered messages and MAX polls.

        Skipped messages are immediately marked as seen so the next
        poll does not re-evaluate (and re-skip) them.
        """
        if not messages:
            return messages

        passed, polls = _split_max_polls(messages)
        if polls:
            logger.info(
                "Skipped {} MAX poll message(s) in {}",
                len(polls), self._channel_url,
            )

        with_payload: list[Message] = []
        empty: list[Message] = []
        for m in passed:
            if self._has_forwardable_payload(m):
                with_payload.append(m)
            else:
                empty.append(m)
        passed = with_payload
        if empty:
            lost_media = sum(
                1 for m in empty if any(a.type == "media_tile" for a in m.attachments)
            )
            logger.warning(
                "Skipped {} empty message(s) in {} (header-only; no text, "
                "no media, no files){}",
                len(empty),
                self._channel_url,
                f"; {lost_media} had unresolved photo(s) never sent"
                if lost_media
                else "",
            )


        if self._filters:
            whitelist_passed: list[Message] = []
            filtered: list[Message] = []
            for m in passed:
                if _message_passes_filters(m, self._filters):
                    whitelist_passed.append(m)
                else:
                    filtered.append(m)
            passed = whitelist_passed
        else:
            filtered = []

        skipped = polls + empty + filtered
        if skipped:
            self._store.mark_many(
                self._channel_url, [message.id for message in skipped]
            )
        if filtered:
            logger.info(
                "Filter dropped {} message(s) in {} (kept {})",
                len(filtered), self._channel_url, len(passed),
            )
        return passed

    def _notify_admin_no_messages(self, collect_failure: str | None) -> None:
        """Ping the operator once when the live timeline goes empty.

        A collector failure has a concrete reason (for example a script
        timeout) which must take precedence over selector-drift wording.
        An ordinary, successfully collected empty timeline is checked for a
        login page first, then reported as a possible DOM change.
        """
        reason = _COLLECT_FAILURE_TO_OUTAGE_REASON.get(collect_failure or "")
        if reason is None:
            reason = (
                _REASON_LOGIN_PAGE
                if self._is_login_page()
                else _REASON_EMPTY_TIMELINE
            )
        self._send_outage_alert(reason)

    def _notify_admin_open_failed(self) -> None:
        """Ping the operator once when the channel never opens.

        Called from ``run_forever`` / ``run_once`` right before the bot
        gives up and exits. The reason was recorded by ``_open_channel``;
        defaults to a generic history-timeout message if missing.
        """
        reason = self._last_open_failure_reason or _REASON_HISTORY_TIMEOUT
        self._send_outage_alert(reason)

    def _send_outage_alert(self, reason: str) -> None:
        """Send a one-shot health alert to the admin chat, if configured.

        The same ``_admin_alert_sent`` guard covers every outage type, so
        the operator gets at most one ping per process while the channel
        stays unhealthy.
        """
        if not self._admin_chat_id or self._admin_alert_sent:
            return

        detail = _OUTAGE_ALERT_TEXTS.get(
            reason, _OUTAGE_ALERT_TEXTS[_REASON_EMPTY_TIMELINE]
        )
        text = (
            "\u26a0\ufe0f MAX forwarder: " + detail + "\n"
            f"Канал: {self._channel_url}"
        )
        if self._relay.send_admin_alert(self._admin_chat_id, text):
            self._admin_alert_sent = True
            logger.info(
                "Admin alert sent to {}: reason={} channel={}",
                self._admin_chat_id, reason, self._channel_url,
            )
        else:
            logger.warning(
                "Failed to deliver admin alert (reason={}) for {}",
                reason, self._channel_url,
            )

    def _download_files_for(self, msg: Message) -> None:
        """Click each file card in ``msg`` and wait for the download to finish.

        On success, ``Attachment.local_path`` is populated for the relay.
        On failure, the attachment is left without a local path and the relay
        will skip / fall back accordingly.
        """
        file_atts = [a for a in msg.attachments if a.type == "file" and a.filename]
        if not file_atts:
            return

        for att in file_atts:
            before = self._snapshot_download_dir()
            clicked = self._collector.click_file_download(att.filename or "")
            if not clicked:
                logger.warning(
                    "Cannot download {!r}: button not found in DOM", att.filename
                )
                continue

            new_path = self._wait_for_new_download(before, att.filename or "")
            if new_path:
                att.local_path = str(new_path)
                logger.info(
                    "Downloaded file {!r} -> {} ({} bytes)",
                    att.filename, new_path, new_path.stat().st_size,
                )
            else:
                logger.warning(
                    "Timed out waiting for download of {!r}", att.filename
                )

    def _cleanup_files_for(self, msg: Message) -> None:
        for att in msg.attachments:
            if att.type != "file" or not att.local_path:
                continue
            try:
                Path(att.local_path).unlink(missing_ok=True)
            except OSError as e:
                logger.warning(
                    "Could not remove downloaded file {}: {}", att.local_path, e
                )

    def _snapshot_download_dir(self) -> set[str]:
        try:
            return {
                p.name
                for p in self._download_dir.iterdir()
                if not p.name.endswith(_CHROME_PARTIAL_SUFFIX)
            }
        except OSError:
            return set()

    def _wait_for_new_download(
        self, before: set[str], expected_filename: str
    ) -> Path | None:
        """Wait until a new fully-downloaded file appears in the directory.

        We prefer a file whose name matches ``expected_filename`` (Chrome
        usually preserves the server-supplied name). If no exact match
        appears, we accept any newly added non-partial file.
        """
        deadline = time.monotonic() + max(5.0, config.FILE_DOWNLOAD_TIMEOUT)

        while time.monotonic() < deadline:
            time.sleep(0.5)
            if any(
                p.suffix == _CHROME_PARTIAL_SUFFIX
                for p in self._download_dir.iterdir()
            ):
                continue

            current = self._snapshot_download_dir()
            new_names = current - before
            if not new_names:
                continue

            # Prefer exact filename match if present; otherwise pick the
            # newest of the freshly-appeared files.
            if expected_filename in new_names:
                return self._download_dir / expected_filename

            newest_name = max(
                new_names,
                key=lambda n: os.path.getmtime(self._download_dir / n),
            )
            return self._download_dir / newest_name

        return None
