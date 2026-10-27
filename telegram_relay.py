"""Telegram Bot API relay.

Uses raw HTTP Bot API via ``requests`` (simple, no async loop friction).
Falls back to dry-run mode automatically when the bot token is not configured,
which is convenient during DOM/heuristic development.

Output format (per requirement):
- First line: ``От: <b>author</b>`` (only when a bold direct sender name was
  extracted from MAX). The whole sender header that MAX renders in bold
  inside the bubble (e.g. ``Кристина Павловна Учитель 🎓``) is captured as
  the author string and rendered in bold on this line.
- Then the reply / forwarded quote (when MAX rendered one), as
  ``<blockquote><b>{quote.author}</b>\n{quote.text}</blockquote>``.
  Long originals are wrapped in ``<blockquote expandable>`` so the user
  can collapse them.
- Then the message body text.
- Then any non-image / non-file attachments (links/videos/audio) as plain
  ``<type>: <url>`` lines.
- When the MAX message has only media (image / file) and no body text, no
  quote, the ``От:`` header is omitted as well: the document or photo is
  forwarded without any caption.

Media handling:
- **Images** from the post are passed to Telegram which fetches them
  server-side. If Telegram refuses, we fall back to a text-only message
  that includes the URLs inline.
- **Files** (PDF and the like) are uploaded as documents via
  ``sendDocument`` (multipart upload of bytes from
  ``Attachment.local_path``). The first document carries the message
  caption when no images already absorbed it.
- Author avatars and message reactions are NOT forwarded.
"""
from __future__ import annotations

import time
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
from loguru import logger

import config

if TYPE_CHECKING:
    from collector import Attachment, Message


# Telegram limits.
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_MEDIA_GROUP_MAX = 10


class TelegramRelay:
    """Sends formatted MAX messages to a Telegram chat."""

    BASE_URL = "https://api.telegram.org"

    def __init__(
        self,
        token: str | None = None,
        dry_run: bool | None = None,
        max_attempts: int = 4,
    ) -> None:
        self._token = token or config.TELEGRAM_BOT_TOKEN
        self._dry_run = (
            dry_run
            if dry_run is not None
            else (
                not self._token
                or self._token == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE"
            )
        )
        self._max_attempts = max_attempts
        self._session = requests.Session()
        if self._dry_run:
            logger.warning(
                "TelegramRelay running in DRY-RUN mode "
                "(no real messages will be sent)"
            )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def send_admin_alert(self, chat_id: str, text: str) -> bool:
        """Send a one-off plain-text alert to the bot operator.

        Used by the scheduler to notify the operator about runtime health
        issues that need a human (for example, the live monitor stopped
        seeing messages in the MAX channel). Returns ``True`` on success
        or in dry-run mode; ``False`` on transport failure or when no
        admin chat id is configured.
        """
        if not chat_id:
            logger.debug("Admin alert skipped: no admin chat id configured")
            return False
        if self._dry_run:
            logger.info(
                "[DRY-RUN] would send admin alert to {}: {}", chat_id, text
            )
            return True
        return self._send_text(chat_id, text, log_label="admin_alert")

    def send(self, chat_id: str, msg: "Message") -> bool:
        """Send a single normalized MAX message to the given Telegram chat."""
        text = self._format_text(msg)
        photo_urls = self._collect_photo_urls(msg)
        files = self._collect_files(msg)

        if self._dry_run:
            logger.info(
                "[DRY-RUN] would send to {}: id={} chars={} photos={} files={}",
                chat_id, msg.id, len(text), len(photo_urls), len(files),
            )
            logger.debug("[DRY-RUN] payload:\n{}", text)
            return True

        # Caption goes on whichever payload is sent first. The remaining
        # payloads are sent without caption.
        caption: str | None = text or None
        caption_fits_in_media = bool(caption) and len(caption) <= TELEGRAM_CAPTION_LIMIT

        # Long captions are sent as a separate text message first; media and
        # documents follow without their own caption.
        header_sent = False
        if caption and not caption_fits_in_media:
            if not self._send_text(chat_id, caption, msg, log_label="header"):
                return False
            caption = None
            header_sent = True

        photos_ok = True
        if photo_urls:
            if not self._send_photos(chat_id, photo_urls, caption, msg):
                logger.warning(
                    "Photo send failed for {}, falling back to text",
                    msg.id,
                )
                fallback_text = self._append_image_urls(text, photo_urls)
                # If we already sent the text header above, just append URLs.
                if header_sent:
                    fallback_text = self._append_image_urls("", photo_urls)
                if fallback_text:
                    if not self._send_text(chat_id, fallback_text, msg):
                        return False
                photos_ok = False
            else:
                # Caption was consumed by sendPhoto/sendMediaGroup.
                caption = None

        files_ok = True
        if files:
            if not self._send_documents(chat_id, files, caption, msg):
                files_ok = False
                logger.warning("Document send failed for {}", msg.id)
            else:
                caption = None

        if not photo_urls and not files:
            if not text:
                logger.info(
                    "Skipping empty message id={} (no text, no media, no files)",
                    msg.id,
                )
                return True
            if header_sent:
                # The body text already went out as the header message
                # above; sending it again duplicated a long no-media
                # post in the group (2026-09-17 incident).
                return True
            return self._send_text(chat_id, text, msg)

        return photos_ok and files_ok

    # ------------------------------------------------------------------
    # Payload assembly
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_photo_urls(msg: "Message") -> list[str]:
        """Build the ordered list of photo URLs to send to Telegram."""
        urls: list[str] = []
        seen: set[str] = set()

        for a in msg.attachments:
            if a.type != "image" or not a.url:
                continue
            if a.url in seen:
                continue
            seen.add(a.url)
            urls.append(a.url)

        return urls

    @staticmethod
    def _collect_files(msg: "Message") -> list["Attachment"]:
        """Files that have a downloaded local path ready for upload."""
        return [
            a
            for a in msg.attachments
            if a.type == "file" and a.local_path and Path(a.local_path).is_file()
        ]

    # Long quotes are rendered as <blockquote expandable> so the user can
    # collapse the original post in the Telegram bubble.
    QUOTE_EXPANDABLE_THRESHOLD = 200

    @staticmethod
    def _format_text(msg: "Message") -> str:
        # Media-only messages (image / file with no body text, no quote and
        # no extra link/video/audio attachments) are forwarded as bare media
        # without any caption -- per requirement, no "От:" header is
        # generated for such posts.
        non_media_lines = TelegramRelay._format_non_media_attachments(msg.attachments)
        missing_files = TelegramRelay._format_missing_file_mentions(msg.attachments)
        has_quote = bool(msg.quote and (msg.quote.author or msg.quote.text))
        has_extra_payload = bool(
            msg.text or non_media_lines or missing_files or has_quote
        )
        has_native_media = any(
            (a.type == "image" and a.url)
            or (a.type == "file" and a.local_path and Path(a.local_path).is_file())
            for a in msg.attachments
        )
        if not has_extra_payload and has_native_media:
            return ""

        parts: list[str] = []

        if msg.author:
            # The whole sender header (name + optional role / title /
            # emoji that MAX renders right next to it) is rendered in bold
            # on the first line.
            parts.append(f"От: <b>{escape(msg.author)}</b>")

        quote_html = TelegramRelay._format_quote(msg.quote)
        if quote_html:
            parts.append(quote_html)

        if msg.text:
            parts.append(escape(msg.text))

        if non_media_lines:
            parts.append("\n".join(non_media_lines))

        # Surface unsent files as plain text mentions when they had no
        # downloaded payload (e.g. download failed). Skipping these silently
        # would lose visible info in the post.
        if missing_files:
            parts.append("\n".join(missing_files))

        return "\n".join(parts)

    @staticmethod
    def _format_quote(quote) -> str:  # type: ignore[no-untyped-def]
        """Render a reply / forwarded quote as Telegram <blockquote>.

        Telegram HTML supports both ``<blockquote>`` and ``<blockquote
        expandable>`` (collapsed by default; tap to expand). We use the
        expandable variant for long originals so the reply itself stays
        easy to scan.
        """
        if not quote:
            return ""
        author = (quote.author or "").strip()
        body = (quote.text or "").strip()
        if not author and not body:
            return ""

        inner_parts: list[str] = []
        if author:
            inner_parts.append(f"<b>{escape(author)}</b>")
        if body:
            inner_parts.append(escape(body))
        inner = "\n".join(inner_parts)

        expandable = len(body) >= TelegramRelay.QUOTE_EXPANDABLE_THRESHOLD
        tag = "blockquote expandable" if expandable else "blockquote"
        return f"<{tag}>{inner}</blockquote>"

    @staticmethod
    def _format_non_media_attachments(attachments: list["Attachment"]) -> list[str]:
        """Render link/video/audio attachments without bracketed type markers.

        Photos and files are sent as native Telegram media; tile-grid
        placeholders (``media_tile``) carry no URL and exist only for
        fingerprinting, so they are dropped from the visible payload.
        """
        out: list[str] = []
        for a in attachments:
            if a.type in {"image", "media_tile", "file"}:
                continue
            if not a.url:
                continue
            label = (a.text or a.url).strip()
            link = f'<a href="{escape(a.url)}">{escape(label)}</a>'
            out.append(f"{escape(a.type)}: {link}")
        return out

    @staticmethod
    def _format_missing_file_mentions(
        attachments: list["Attachment"],
    ) -> list[str]:
        out: list[str] = []
        for a in attachments:
            if a.type != "file":
                continue
            if a.local_path and Path(a.local_path).is_file():
                continue
            label_bits = []
            if a.file_type:
                label_bits.append(a.file_type.upper())
            if a.filename:
                label_bits.append(a.filename)
            if a.size_text:
                label_bits.append(f"({a.size_text})")
            if not label_bits:
                continue
            out.append(f"📎 {escape(' '.join(label_bits))}")
        return out

    @staticmethod
    def _append_image_urls(text: str, image_urls: list[str]) -> str:
        if not image_urls:
            return text
        url_lines = "\n".join(escape(u) for u in image_urls)
        if not text:
            return url_lines
        return f"{text}\n\n{url_lines}"

    # ------------------------------------------------------------------
    # Telegram API methods
    # ------------------------------------------------------------------

    def _send_photos(
        self,
        chat_id: str,
        urls: list[str],
        caption: str | None,
        msg: "Message",
    ) -> bool:
        # Telegram accepts at most TELEGRAM_MEDIA_GROUP_MAX items per
        # sendMediaGroup call, while MAX albums can be longer. Split the
        # stream into consecutive chunks of at most 10 and send them in
        # order; the caption rides on the first chunk only. A single
        # photo still goes through plain sendPhoto as before.
        if len(urls) == 1:
            return self._send_photo(chat_id, urls[0], caption, msg)

        all_ok = True
        for start in range(0, len(urls), TELEGRAM_MEDIA_GROUP_MAX):
            chunk = urls[start : start + TELEGRAM_MEDIA_GROUP_MAX]
            chunk_caption = caption if start == 0 else None
            ok = self._send_media_group(chat_id, chunk, chunk_caption, msg)
            if not ok:
                logger.warning(
                    "Photo chunk {}-{} failed for msg {}; stopping album send",
                    start + 1,
                    start + len(chunk),
                    msg.id,
                )
                all_ok = False
                break
        return all_ok

    def _send_text(
        self,
        chat_id: str,
        text: str,
        msg: "Message | None" = None,
        log_label: str = "text",
    ) -> bool:
        if len(text) > TELEGRAM_TEXT_LIMIT:
            logger.warning(
                "Text exceeds Telegram limit ({} > {}), truncating",
                len(text), TELEGRAM_TEXT_LIMIT,
            )
            text = text[: TELEGRAM_TEXT_LIMIT - 1] + "…"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        return self._call_api_json(
            "sendMessage",
            payload,
            kind=log_label,
            msg=msg,
            extra_log={"chars": len(text)},
        )

    def _send_photo(
        self,
        chat_id: str,
        url: str,
        caption: str | None,
        msg: "Message",
    ) -> bool:
        payload: dict[str, Any] = {"chat_id": chat_id, "photo": url}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        return self._call_api_json(
            "sendPhoto",
            payload,
            kind="photo",
            msg=msg,
            extra_log={"caption_chars": len(caption or "")},
        )

    def _send_media_group(
        self,
        chat_id: str,
        urls: list[str],
        caption: str | None,
        msg: "Message",
    ) -> bool:
        media: list[dict[str, Any]] = []
        for i, url in enumerate(urls):
            item: dict[str, Any] = {"type": "photo", "media": url}
            if i == 0 and caption:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)
        payload = {"chat_id": chat_id, "media": media}
        return self._call_api_json(
            "sendMediaGroup",
            payload,
            kind="media_group",
            msg=msg,
            extra_log={"images": len(urls), "caption_chars": len(caption or "")},
        )

    def _send_documents(
        self,
        chat_id: str,
        files: list["Attachment"],
        caption: str | None,
        msg: "Message",
    ) -> bool:
        """Upload ``files`` as Telegram documents.

        Telegram has a ``sendMediaGroup`` for documents too, but only if all
        items in the group are documents and the caption goes on the first
        item. To keep things simple we send documents one by one and put
        the caption on the very first call.
        """
        all_ok = True
        first = True
        for att in files:
            local_path = Path(att.local_path) if att.local_path else None
            if not local_path or not local_path.is_file():
                logger.warning(
                    "Document missing on disk for {!r}, skipping", att.filename
                )
                all_ok = False
                continue

            data: dict[str, Any] = {"chat_id": chat_id}
            if first and caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
                first = False

            try:
                with local_path.open("rb") as fh:
                    files_field = {
                        "document": (
                            att.filename or local_path.name,
                            fh,
                            "application/octet-stream",
                        )
                    }
                    ok = self._call_api_multipart(
                        "sendDocument",
                        data=data,
                        files=files_field,
                        kind="document",
                        msg=msg,
                        extra_log={
                            "filename": att.filename or local_path.name,
                            "bytes": local_path.stat().st_size,
                        },
                    )
            except OSError as e:
                logger.error("Failed to read {} for upload: {}", local_path, e)
                ok = False

            if not ok:
                all_ok = False
        return all_ok

    # ------------------------------------------------------------------
    # Low-level HTTP helpers
    # ------------------------------------------------------------------

    def _call_api_json(
        self,
        method: str,
        payload: dict[str, Any],
        kind: str,
        msg: "Message | None" = None,
        extra_log: dict[str, Any] | None = None,
    ) -> bool:
        url = f"{self.BASE_URL}/bot{self._token}/{method}"
        return self._call_api(
            method, kind, msg, extra_log,
            lambda: self._session.post(url, json=payload, timeout=60),
        )

    def _call_api_multipart(
        self,
        method: str,
        data: dict[str, Any],
        files: dict[str, Any],
        kind: str,
        msg: "Message | None" = None,
        extra_log: dict[str, Any] | None = None,
    ) -> bool:
        url = f"{self.BASE_URL}/bot{self._token}/{method}"
        # Multipart bodies for retries: re-open the file handle each attempt
        # because requests consumes the stream on the first send.
        def do_request() -> requests.Response:
            return self._session.post(url, data=data, files=files, timeout=120)

        return self._call_api(method, kind, msg, extra_log, do_request)

    def _call_api(
        self,
        method: str,
        kind: str,
        msg: "Message | None",
        extra_log: dict[str, Any] | None,
        do_request,
    ) -> bool:
        msg_id = msg.id if msg else "?"
        attachments = len(msg.attachments) if msg else 0

        for attempt in range(1, self._max_attempts + 1):
            try:
                resp = do_request()
            except requests.RequestException as e:
                logger.warning(
                    "Telegram request error (method={} attempt={}): {}",
                    method, attempt, e,
                )
                time.sleep(min(2 ** attempt, 30))
                continue

            if resp.status_code == 200:
                attempt_note = f" (attempt {attempt})" if attempt > 1 else ""
                extra = " ".join(f"{k}={v}" for k, v in (extra_log or {}).items())
                logger.info(
                    "Telegram {} sent: id={} attachments={} {}{}",
                    kind, msg_id, attachments, extra, attempt_note,
                )
                return True

            if resp.status_code == 429:
                retry_after = self._parse_retry_after(resp)
                logger.warning(
                    "Telegram rate-limited on {}; sleeping {}s",
                    method, retry_after,
                )
                time.sleep(retry_after + 0.5)
                continue

            if 500 <= resp.status_code < 600:
                logger.warning(
                    "Telegram server error on {} [{}]: {} (attempt {})",
                    method, resp.status_code, resp.text[:200], attempt,
                )
                time.sleep(min(2 ** attempt, 30))
                continue

            logger.error(
                "Telegram {} failed [{}]: {}",
                method, resp.status_code, resp.text[:500],
            )
            return False

        logger.error(
            "Telegram {} failed after {} attempts", method, self._max_attempts
        )
        return False

    @staticmethod
    def _parse_retry_after(resp: requests.Response) -> float:
        try:
            return float(resp.json().get("parameters", {}).get("retry_after", 5))
        except Exception:
            return 5.0
