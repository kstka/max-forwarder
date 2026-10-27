"""DOM-based collector for MAX channel messages.

MAX (web.max.ru) is a SvelteKit SPA. The channel/chat history is rendered as
a virtualized list inside ``div.history``. Selectors used here are based on
the observed DOM:

- History scroll container:
    ``div.history .scrollable.scrollListScrollable``
- Content wrapper (direct parent of message rows):
    ``div.history .scrollListContent--bottom`` (fallback: ``.scrollListContent``)
- Each message row:
    ``div.item.svelte-rg2upy[data-index="N"]``
  ``data-index`` is just the virtualized position, NOT a stable id.
- Inside a row, the actual message lives in ``.bubble`` with
    ``[data-bubbles-variant="incoming|outgoing"]`` near it.
- Direct sender (the bold name above / inside the bubble for group/channel
  posts) is detected structurally as the non-structural elements that come
  before the body in the bubble (see ``readSenderHeaderText``). The full
  text -- name + role / title -- is captured into ``Message.author`` and
  rendered in bold by the relay.
- Reply / forwarded quote is rendered as ``<div class="link"><button class="mark">``
  inside the bubble, between the sender header and the main ``.text``. It
  is captured separately into ``Message.quote = {author, text}`` and
  removed from the body so the quoted text doesn't get concatenated with
  the actual message.
- Inline images: ``<img>`` / ``<video>`` / ``<audio>`` and ``.media .grid .tile``.
- File cards (PDF, etc.): ``.attaches button[aria-label="Скачать"]`` with
  ``.title`` (filename) and ``.info`` (size string ``"Скачать • 521.95 KB"``).
  The card carries no download URL: clicking the button triggers a JS-driven
  download. To re-upload the file we have to click and let the browser save
  it into Chrome's configured ``download.default_directory``.

Because there is no stable message id in the DOM, ``Message.id`` is a SHA-1
of a stable fingerprint built from text, timestamp, author and attachment
metadata.

Use :py:meth:`MessageCollector.dump_for_discovery` to save HTML and a
screenshot when refining selectors after MAX UI changes.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver


@dataclass
class Attachment:
    type: str  # "image" | "video" | "audio" | "link" | "file" | "media_tile"
    url: str | None = None
    filename: str | None = None
    text: str | None = None
    file_type: str | None = None  # e.g. "PDF", "DOCX"
    size_text: str | None = None  # human-readable size, e.g. "521.95 KB"
    local_path: str | None = None  # populated by the downloader before sending


@dataclass
class Quote:
    """Reply / forwarded quote captured from the bubble's `.link > button.mark`."""

    author: str | None = None
    text: str | None = None


@dataclass
class Message:
    id: str
    channel: str
    text: str = ""
    author: str | None = None  # bold direct sender, used for the "От:" header
    quote: Quote | None = None  # reply / forwarded quote, when present
    timestamp: str | None = None
    direction: str | None = None  # "incoming" | "outgoing" | None
    attachments: list[Attachment] = field(default_factory=list)
    raw_html_sample: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# JS that scans the MAX history container and returns serializable info about
# messages. Returning a single payload from execute_script avoids per-element
# round-trips and stale-element issues during virtualization.
# Shared JS helpers (bubble text / author / timestamp / attachment
# readers). Embedded verbatim into both collection scripts so the
# extraction logic stays defined exactly once.
_SHARED_JS = r"""
function isVisible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    return true;
}

function findHistoryContent() {
    const history = document.querySelector('div.history');
    if (history) {
        const c = history.querySelector('.scrollListContent--bottom')
              || history.querySelector('.scrollListContent');
        if (c) return c;
    }
    return document.querySelector('.scrollListContent--bottom');
}

function readDirection(row) {
    const v = row.querySelector('[data-bubbles-variant]');
    return v ? v.getAttribute('data-bubbles-variant') : null;
}

function readMetaTime(bubble) {
    const meta = bubble.querySelector('.meta');
    if (!meta) return null;
    const t = (meta.innerText || '').trim();
    return t || null;
}

// Class names that mark "structural" blocks inside a `.bubble`. They are
// either part of the message body (.text), media (.attaches/.media/.share),
// chrome (.meta/.messageControls), reactions, or a forwarded / reply-quote
// header (.link). They are NEVER part of the sender header.
const _STRUCTURAL_SELECTOR = (
    '.text, .attaches, .media, .share, .reactions, .meta, ' +
    '.messageControls, .link, .bordersWrapper'
);
const _BODY_BOUNDARY_SELECTOR = '.text, .attaches, .media, .share';

function findBubbleBodyBoundary(bubble) {
    // The bubble's body content starts at the first direct child that
    // belongs to the body itself (text span, file cards, photo grid, or
    // a link preview). Anything BEFORE that boundary is treated as the
    // sender header.
    for (const child of Array.from(bubble.children)) {
        if (child.matches && child.matches(_BODY_BOUNDARY_SELECTOR)) {
            return child;
        }
    }
    return null;
}

function collectSenderHeaderElements(bubble) {
    // Preferred structural signal (current MAX UI): a dedicated .header
    // block carrying the bold sender name and optional role / title
    // (e.g. "Учитель 🎓"). It is rendered before the body and is never
    // part of the message text. The current UI also wraps the bubble
    // content in a .bubbleContent element, so the block may sit one
    // level deeper than in older revisions -- querySelector covers both.
    const headerBlock = bubble.querySelector('.header');
    if (headerBlock &&
        (headerBlock.querySelector('.name') || headerBlock.querySelector('.role'))) {
        return [headerBlock];
    }
    // Fallback (legacy layouts): walk the bubble's direct children up to
    // the body boundary; collect the ones that are not known structural
    // blocks.
    const boundary = findBubbleBodyBoundary(bubble);
    const out = [];
    for (const child of Array.from(bubble.children)) {
        if (boundary && (child === boundary || child.contains(boundary))) {
            break;
        }
        if (child.matches && child.matches(_STRUCTURAL_SELECTOR)) continue;
        const t = (child.innerText || child.textContent || '').trim();
        if (t) out.push(child);
    }
    // The bubble may wrap all content in a single non-structural
    // container (e.g. .bubbleContent) that also holds the body; the walk
    // above would then treat the whole wrapper as the "header". Re-run
    // the walk inside the wrapper instead.
    if (out.length === 1 &&
        !boundary &&
        out[0].children.length &&
        Array.from(out[0].children).some(
            c => c.matches && c.matches(_BODY_BOUNDARY_SELECTOR)
        )) {
        return collectSenderHeaderElements(out[0]);
    }
    return out;
}

function substituteEmojisInPlace(root) {
    // Inline emojis live in <span class="emoji" data-lexical-emoji="X">
    // wrapping a lazy-loaded <img alt="X">. Neither attribute is picked
    // up by innerText, so substitute the actual symbol before extraction.
    root.querySelectorAll('.emoji').forEach(e => {
        const sym = e.getAttribute('data-lexical-emoji')
                 || (e.querySelector('img') && e.querySelector('img').getAttribute('alt'))
                 || '';
        if (sym) {
            e.replaceWith(document.createTextNode(sym));
        } else {
            e.remove();
        }
    });
}

function readSenderHeaderText(headerEls) {
    // Concatenate text from the sender-header elements into a single
    // string ("Кристина Павловна Учитель 🎓"). The whole string is later
    // rendered in bold on the first line of the Telegram message.
    if (!headerEls.length) return null;
    const segments = [];
    for (const el of headerEls) {
        const c = el.cloneNode(true);
        substituteEmojisInPlace(c);
        const t = (c.innerText || c.textContent || '').trim();
        if (t) segments.push(t);
    }
    const joined = segments.join(' ').replace(/\s+/g, ' ').trim();
    return joined || null;
}

function readAuthor(bubble, row) {
    // Channel/group bubbles render the bold sender name (and sometimes a
    // role/title like "Учитель") inside the bubble itself, BEFORE the body.
    // We detect this structurally instead of relying on class names which
    // change between MAX UI revisions. The full header text (name + role)
    // becomes the author string and is rendered in bold by the relay.
    const headerText = readSenderHeaderText(collectSenderHeaderElements(bubble));
    if (headerText) {
        return { kind: 'sender', name: headerText };
    }
    // Fallback: legacy class-based selectors near .authorAvatar (used when
    // MAX renders the sender outside the bubble).
    // Forwarded / reply-quote authors live inside `.link .author` and are
    // intentionally skipped -- they remain inline in the body text with
    // their MAX label (e.g. "Переслано: ...").
    const candidates = [
        '.authorAvatar .name',
        '.authorAvatar [class*="name" i]',
        '.authorName',
        '.messageAuthor',
        '[class*="senderName" i]',
        '[class*="authorName" i]',
    ];
    const scope = row || bubble;
    for (const sel of candidates) {
        const el = scope.querySelector(sel);
        if (!el) continue;
        if (el.closest && el.closest('.link')) continue;
        if (el.closest && el.closest('.bubble')) continue;
        const t = (el.innerText || '').trim();
        if (t) return { kind: 'sender', name: t };
    }
    return null;
}

function findQuoteBlocks(bubble) {
    // Reply / forwarded quote in MAX. Two known layouts:
    //   legacy:  <div class="link"><button class="mark">
    //               <span class="author">…<span class="text">{name}</span>…</span>
    //               <span class="text">{quoted body}</span>
    //           </button></div>
    //   2026-10: <div class="link"><div class="mark">
    //               <span class="label">Переслано:</span>
    //               <button class="author" aria-label="Открыть профиль">
    //                 …avatar…<span class="text"><span class="name">…</span></span>
    //               </button>
    //           </div></div>
    // and lives inside the bubble between the sender header and the
    // main `.text`. Plain hyperlinks (<a class="link">) and link preview
    // wrappers do NOT have a `.mark` child, so this selector picks up
    // the quote block specifically, in either layout (tag-agnostic).
    return Array.from(bubble.querySelectorAll('.link > .mark'))
        .map(mark => mark.parentElement)
        .filter(Boolean);
}

function readQuote(bubble) {
    // Return the structured quote ({author, text}) when the bubble has
    // a reply / forward block, otherwise null.
    const blocks = findQuoteBlocks(bubble);
    if (!blocks.length) return null;
    // MAX renders only one quote per bubble; if multiple ever appear,
    // we keep the first.
    const root = blocks[0];
    const mark = root.querySelector('.mark');
    if (!mark) return null;

    // The author element is nested inside the mark: a span in the
    // legacy layout, the profile button itself in the 2026-10 layout.
    const authorEl = mark.matches('.author')
        ? mark
        : mark.querySelector('.author');
    let author = '';
    if (authorEl) {
        const c = authorEl.cloneNode(true);
        // Avatar inside .author contributes nothing useful to the text.
        c.querySelectorAll('.avatar, img').forEach(el => el.remove());
        substituteEmojisInPlace(c);
        author = (c.innerText || c.textContent || '').replace(/\s+/g, ' ').trim();
    }

    // The forward label ("Переслано:") is a sibling of the author
    // button in the 2026-10 layout; keep it in the quote author so the
    // Telegram blockquote reads exactly like MAX ("Переслано: Имя").
    const labelEl = mark.querySelector(':scope > .label');
    if (labelEl) {
        const label = (labelEl.innerText || '').replace(/\s+/g, ' ').trim();
        if (label) author = author ? `${label} ${author}` : label;
    }

    let text = '';
    // The quoted body is the direct `span.text` child of the mark
    // element -- the .author block also contains nested .text nodes
    // (the author name itself), so we look at the mark's own children.
    const directText = Array.from(mark.children).find(
        el => el.matches && el.matches('span.text')
    );
    if (directText) {
        const c = directText.cloneNode(true);
        substituteEmojisInPlace(c);
        text = (c.innerText || c.textContent || '').trim();
    }

    if (!author && !text) return null;
    return { author: author || null, text: text || null };
}

function readText(bubble) {
    const clone = bubble.cloneNode(true);

    // Drop the sender-header elements first so the body text doesn't
    // include the bold name / role line that we already surfaced as the
    // structured author.
    collectSenderHeaderElements(clone).forEach(el => el.remove());

    // Strip pieces that are not part of the user-visible message body.
    clone.querySelectorAll('.meta').forEach(m => m.remove());
    // Reactions panel renders animoji via <canvas> + counters; we don't
    // forward reactions, so drop them.
    clone.querySelectorAll('.reactions').forEach(m => m.remove());
    // File cards live in .attaches and are surfaced separately as
    // structured file attachments; remove them so their visual label
    // ("Скачать • 521.95 KB") doesn't pollute the message body.
    clone.querySelectorAll('.attaches').forEach(m => m.remove());
    // Reply / forwarded quote blocks are surfaced separately as
    // Message.quote; remove them so the quoted text doesn't get
    // concatenated into the body. Plain hyperlinks (<a class="link">)
    // and link previews stay -- they have no `.mark` child.
    clone.querySelectorAll('.link > .mark').forEach(mark => {
        const wrapper = mark.parentElement;
        if (wrapper) wrapper.remove();
    });


    substituteEmojisInPlace(clone);
    return (clone.innerText || '').trim();
}

function readFiles(bubble) {
    const out = [];
    bubble.querySelectorAll('.attaches button[aria-label="Скачать"]').forEach(btn => {
        const titleEl = btn.querySelector('.title');
        const infoEl = btn.querySelector('.info');
        const typeEl = btn.querySelector('.fileIcon svg text');

        const filename = titleEl ? (titleEl.innerText || '').trim() : '';
        let size = '';
        if (infoEl) {
            const raw = (infoEl.innerText || '').trim();
            // The visible info reads "Скачать • 521.95 KB"; strip the action prefix.
            size = raw.replace(/^Скачать\s*[\u2022•]\s*/u, '').trim();
        }
        const fileType = typeEl ? (typeEl.textContent || '').trim() : '';

        out.push({
            type: 'file',
            filename: filename || null,
            size_text: size || null,
            file_type: fileType || null,
        });
    });
    return out;
}

function readAttachments(bubble) {
    const out = [];
    bubble.querySelectorAll('img').forEach(img => {
        if (!isVisible(img)) return;
        // Skip avatar / emoji images.
        if (img.classList && (img.classList.contains('avatarImage')
            || img.classList.contains('emoji'))) return;
        const inAvatar = img.closest && img.closest('.authorAvatar, .avatar');
        if (inAvatar) return;
        const inEmoji = img.closest && img.closest('.emoji');
        if (inEmoji) return;
        const src = img.currentSrc || img.src || '';
        if (!src || src.startsWith('data:') || src.startsWith('blob:')) return;
        if ((img.naturalWidth || 0) < 64) return;
        out.push({ type: 'image', url: src, text: img.alt || null });
    });
    bubble.querySelectorAll('video').forEach(v => {
        const s = v.currentSrc || v.src
            || (v.querySelector('source') && v.querySelector('source').src) || '';
        if (s && !s.startsWith('blob:')) {
            out.push({ type: 'video', url: s });
        }
    });
    bubble.querySelectorAll('audio').forEach(a => {
        const s = a.currentSrc || a.src || '';
        if (s && !s.startsWith('blob:')) {
            out.push({ type: 'audio', url: s });
        }
    });
    bubble.querySelectorAll('a[href]').forEach(a => {
        const href = a.href || '';
        const t = (a.innerText || '').trim();
        if (href && href.startsWith('http')) {
            out.push({ type: 'link', url: href, text: t || null });
        }
    });
    // Media grids (tiles) are useful even when individual images have not
    // loaded yet: their pixel dimensions vary per attachment and help build
    // a stable per-message fingerprint when src is still unresolved.
    const grids = bubble.querySelectorAll('.media .grid');
    grids.forEach(g => {
        const w = (g.style && g.style.width) ? g.style.width : '';
        const h = (g.style && g.style.height) ? g.style.height : '';
        const tiles = g.querySelectorAll('.tile');
        if (tiles.length) {
            const aria = g.getAttribute('aria-label') || '';
            out.push({
                type: 'media_tile',
                url: null,
                text: `tiles=${tiles.length} size=${w}x${h} aria=${aria}`,
            });
        }
    });
    return out.concat(readFiles(bubble));
}
"""


_COLLECT_JS = _SHARED_JS + r"""
const limit = arguments[0] || 50;

const content = findHistoryContent();
if (!content) {
    return { ok: false, reason: 'no-history-container' };
}

// Virtualized scroll lists in MAX (Svelte) reuse DOM nodes and position
// them via CSS transforms, so the order of `content.children` does NOT
// match the visual / chronological order. `data-index` is the
// authoritative slot in the underlying data array (lower index = older
// message in the `--bottom` anchored history), so we sort by it
// numerically before slicing the tail.
const rows = Array.from(content.children)
    .filter(el => el.hasAttribute && el.hasAttribute('data-index'))
    .filter(isVisible)
    .sort((a, b) => {
        const ai = parseInt(a.getAttribute('data-index'), 10);
        const bi = parseInt(b.getAttribute('data-index'), 10);
        return ai - bi;
    });

const tail = rows.slice(-limit);
const items = tail.map((row, i) => {
    // Stable-enough handle for the post-extraction hydration pass: the
    // marker is re-stamped on every collect run, so a lookup always
    // resolves to this run's row even when Svelte reuses DOM nodes.
    const marker = String(i);
    row.setAttribute('data-omf-n', marker);
    const bubble = row.querySelector('.bubble');
    if (!bubble) {
        return {
            type: 'separator',
            dataIndex: row.getAttribute('data-index'),
            text: (row.innerText || '').trim(),
            outerHTMLSample: row.outerHTML.slice(0, 400),
        };
    }
    return {
        marker,
        type: 'message',
        dataIndex: row.getAttribute('data-index'),
        direction: readDirection(row),
        author: readAuthor(bubble, row),
        quote: readQuote(bubble),
        text: readText(bubble),
        timestamp: readMetaTime(bubble),
        attachments: readAttachments(bubble),
        outerHTMLSample: row.outerHTML.slice(0, 800),
    };
});

return {
    ok: true,
    containerSelector: 'div.history .scrollListContent',
    totalRows: rows.length,
    items,
};
"""


# Reads only the attachment list of the row stamped with the given
# collect marker. Used by the hydration pass to capture media URLs
# while the bubble is in the viewport, before virtualization may
# recycle the DOM node.
_READ_ATTACHMENTS_JS = _SHARED_JS + r"""
const el = document.querySelector('[data-omf-n="' + arguments[0] + '"]');
const bubble = el ? el.querySelector('.bubble') : null;
return bubble ? readAttachments(bubble) : null;
"""

# Seconds to wait for a lazy media tile to resolve its src after the
# bubble is scrolled into view.
_MEDIA_HYDRATION_TIMEOUT = 10.0


# A media bubble is hydrated at most once per this window. Keeps the
# steady-state poll fast: bubbles that already hydrated (their media
# went out with the message) or stubbornly refuse to would otherwise
# cost a scroll plus a full timeout on every poll.
_HYDRATION_RETRY_COOLDOWN = 1800.0


# Scrolls the row stamped with the given collect marker into the
# viewport, which triggers MAX's lazy media hydration.
_SCROLL_MARKER_JS = r"""
const el = document.querySelector('[data-omf-n="' + arguments[0] + '"]');
if (el) el.scrollIntoView({ block: 'center' });
return !!el;
"""


# True when the marked row's media has fully resolved: every non-avatar
# <img> in it has an http src and loaded pixels, or a <video> has an
# http currentSrc (blob: sources are unusable for forwarding).
_MEDIA_READY_JS = r"""
const el = document.querySelector('[data-omf-n="' + arguments[0] + '"]');
if (!el) return false;
const imgs = Array.from(el.querySelectorAll('img'))
    .filter(im => !(im.classList && im.classList.contains('avatarImage')));
const loaded = imgs.filter(im => {
    const s = im.currentSrc || im.src || '';
    return s.startsWith('http') && (im.naturalWidth || 0) >= 64;
});
const videos = Array.from(el.querySelectorAll('video'))
    .filter(v => (v.currentSrc || v.src || '').startsWith('http'));
return loaded.length === imgs.length && (loaded.length > 0 || videos.length > 0);
"""


# JS that finds a download button matching a filename and clicks it.
# Returns true when a matching button was found and clicked, false otherwise.
_CLICK_FILE_JS = r"""
const target = (arguments[0] || '').trim();
if (!target) return false;
const buttons = document.querySelectorAll(
    '.attaches button[aria-label="Скачать"]'
);
for (const btn of buttons) {
    const titleEl = btn.querySelector('.title');
    const name = titleEl ? (titleEl.innerText || '').trim() : '';
    if (name === target) {
        btn.click();
        return true;
    }
}
return false;
"""

# --- Media-viewer fallback -------------------------------------------------
# Some grids never hydrate inline: the tile keeps a low-res CSS
# background placeholder and no <img> with an http src ever appears, so
# scroll-based hydration times out (observed 2026-09-17 on a forwarded
# post: the photo never reached Telegram). Clicking a tile opens MAX's
# media viewer, whose <img> carries the full-size http URL -- click,
# capture, close, repeat per tile.

_VIEWER_TILES_JS = r"""
const el = document.querySelector('[data-omf-n="' + arguments[0] + '"]');
if (!el) return 0;
return el.querySelectorAll('.media .grid .tile').length;
"""

_VIEWER_CLICK_JS = r"""
const el = document.querySelector('[data-omf-n="' + arguments[0] + '"]');
if (!el) return false;
const tiles = el.querySelectorAll('.media .grid .tile');
const tile = tiles[arguments[1]];
if (!tile) return false;
tile.click();
return true;
"""

_VIEWER_IMG_JS = r"""
const imgs = Array.from(document.querySelectorAll('img'))
    .filter(i => (i.currentSrc || i.src || '').startsWith('http'))
    .filter(i => (i.naturalWidth || 0) > 200);
return imgs.length ? (imgs[0].currentSrc || imgs[0].src) : null;
"""


def _fingerprint(
    text: str,
    timestamp: str | None,
    author: str | None,
    attachments: list[dict[str, Any]],
    quote: dict[str, Any] | None = None,
) -> str:
    """Build a stable id for a MAX message based on its visible content.

    Includes the reply-quote so two identical replies pointing at
    different originals stay distinct.

    Media attachments are represented CANONICALLY: photo / video /
    audio entries carry src that only exists after lazy hydration and
    disappears again when the virtualizer recycles the DOM node, so a
    volatile list would mint a different id for the same message on
    different polls (and re-forward it). Instead, the sibling
    ``media_tile`` descriptor — present in the DOM before hydration and
    stable across runs (``tiles=N size=WxH aria=...``) — carries the
    media identity; file and link attachments are stable as-is.
    """
    attach_parts: list[str] = []
    for att in attachments:
        att_type = att.get("type", "")
        if att_type in ("image", "video", "audio"):
            # Volatile rendering state — see docstring. The media grid's
            # media_tile descriptor (same bubble) already identifies the
            # attachment set stably.
            continue
        url = att.get("url") or ""
        if url.startswith(("blob:", "data:")):
            url = ""
        # For files there is no URL in DOM; fall back to filename + size.
        ident = url or att.get("filename") or ""
        attach_parts.append(
            f"{att_type}|{ident}|{att.get('text', '') or ''}"
        )

    quote_part = ""
    if quote:
        q_author = (quote.get("author") or "").strip()
        q_text = (quote.get("text") or "").strip()
        if q_author or q_text:
            quote_part = f"q={q_author}|{q_text}"

    payload = "\u241F".join(
        [
            (text or "").strip(),
            (timestamp or "").strip(),
            (author or "").strip(),
            quote_part,
            f"n={len(attach_parts)}",
            *attach_parts,
        ]
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]
    return f"max:{digest}"


def _hydration_key(raw: dict[str, Any]) -> str:
    """Stable identity of a raw message for hydration rate-limiting:
    text + timestamp + author. Attachments are deliberately excluded —
    they are exactly what hydration changes."""
    text = (raw.get("text") or "").strip()
    ts = raw.get("timestamp") or ""
    author_raw = raw.get("author")
    author = author_raw.get("name") if isinstance(author_raw, dict) else ""
    payload = f"{text}\x00{ts}\x00{author}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()

# Chromedriver error fragments meaning the browser-side target itself is
# gone: the renderer crashed ("tab crashed"), the window was closed, the
# browser process died, or the WebDriver session was deleted. The
# collector cannot collect its way out of these -- they must propagate to
# the scheduler, whose recovery branch re-opens the channel. Everything
# else (stale elements, detached shadow roots, script timeouts) stays a
# logged warning: the next poll usually recovers without navigation.
_FATAL_DRIVER_ERROR_FRAGMENTS = (
    "tab crashed",
    "chrome not reachable",
    "no such window",
    "invalid session id",
    "session deleted",
)

# These codes describe why a collection yielded no usable rows.  The
# scheduler uses them for operator alerts; keeping them separate from an
# ordinary empty list prevents a temporary WebDriver timeout from being
# misreported as a DOM-selector regression.
COLLECT_FAILURE_SCRIPT_TIMEOUT = "script_timeout"
COLLECT_FAILURE_WEBDRIVER = "webdriver_error"
COLLECT_FAILURE_NO_HISTORY = "no_history_container"
COLLECT_FAILURE_INVALID_RESPONSE = "invalid_collect_response"


def _is_fatal_driver_error(exc: WebDriverException) -> bool:
    """True when ``exc`` means the WebDriver target is dead, not flaky."""
    message = (str(exc) or "").lower()
    return any(fragment in message for fragment in _FATAL_DRIVER_ERROR_FRAGMENTS)


class MessageCollector:
    """Collector for MAX channel messages."""

    def __init__(self, driver: WebDriver) -> None:
        self._driver = driver
        # message key -> monotonic time of the last hydration attempt;
        # rate-limits the hydration pass (see _hydration_allowed)
        self._hydration_limiter: dict[str, float] = {}
        self._last_failure_reason: str | None = None

    @property
    def last_failure_reason(self) -> str | None:
        """Reason the most recent collection could not read the timeline.

        ``None`` means the extraction script completed normally, including
        the (separate) case where it found zero message rows.
        """
        return self._last_failure_reason

    def collect(self, channel: str, limit: int = 50) -> list[Message]:
        data = self._run_collect_js(limit)
        if data is None:
            return []
        items = data.get("items", []) or []

        # MAX lazy-hydrates media: a tile / video only gets its real src
        # once the bubble is (near) the viewport, otherwise the bubble
        # keeps a placeholder and photo / video URLs stay unknown. Scroll
        # such bubbles into view, wait for the media to resolve, then
        # re-read the attachment list while the bubble is still in view.
        pending = [
            raw
            for raw in items
            if raw.get("type") == "message"
            and raw.get("marker")
            and self._has_unresolved_media(raw)
            and self._hydration_allowed(raw)
        ]
        if pending:
            # Newest first: the newest rows stay mounted in the
            # virtualization window while older bubbles are visited.
            pending.sort(
                key=lambda raw: int(raw.get("dataIndex") or 0),
                reverse=True,
            )
            logger.info(
                "Collector: hydrating {} media bubble(s) before extraction",
                len(pending),
            )
            hydrated = self._hydrate_media_bubbles(pending)
            for raw in pending:
                atts = hydrated.get(raw["marker"])
                if atts:
                    raw["attachments"] = atts
        separators = sum(1 for x in items if x.get("type") == "separator")
        message_items = [x for x in items if x.get("type") == "message"]

        # Defensive: re-sort by the virtualized data-index (oldest → newest)
        # so the rest of the pipeline can rely on chronological order even
        # if the JS-side ordering ever regresses. Items without a numeric
        # data-index drop to the end while preserving their relative order.
        def _data_index_key(raw: dict[str, Any]) -> tuple[int, int]:
            try:
                return (0, int(raw.get("dataIndex")))
            except (TypeError, ValueError):
                return (1, 0)

        message_items.sort(key=_data_index_key)

        logger.info(
            "Collector: container={!r} total_rows={} taken={} messages={} separators={}",
            data.get("containerSelector"),
            data.get("totalRows"),
            len(items),
            len(message_items),
            separators,
        )

        return [self._normalize(channel, raw) for raw in message_items]

    def _run_collect_js(self, limit: int) -> dict[str, Any] | None:
        """Run the extraction script once; None when the history is
        unreachable (reason already logged)."""
        try:
            data = self._driver.execute_script(_COLLECT_JS, limit)
        except WebDriverException as e:
            if _is_fatal_driver_error(e):
                # 2026-09-16 incident: a crashed renderer made every poll
                # fail with "tab crashed"; swallowing it here turned a
                # recoverable crash into a 22h silent outage.
                raise
            error_text = str(e).lower()
            self._last_failure_reason = (
                COLLECT_FAILURE_SCRIPT_TIMEOUT
                if "script timeout" in error_text
                else COLLECT_FAILURE_WEBDRIVER
            )
            logger.error("Collection script failed: {}", e)
            return None
        if not data or not data.get("ok"):
            reason = (data or {}).get("reason")
            self._last_failure_reason = (
                COLLECT_FAILURE_NO_HISTORY
                if reason == "no-history-container"
                else COLLECT_FAILURE_INVALID_RESPONSE
            )
            logger.warning(
                "Could not locate history container: reason={}",
                reason,
            )
            return None
        self._last_failure_reason = None
        return data

    @staticmethod
    def _has_unresolved_media(raw: dict[str, Any]) -> bool:
        """True when a media grid is present but no usable src resolved
        yet (the tile is still a placeholder)."""
        atts = raw.get("attachments") or []
        if not any(a.get("type") == "media_tile" for a in atts):
            return False
        for a in atts:
            if a.get("type") in ("image", "video", "audio"):
                if (a.get("url") or "").startswith("http"):
                    return False
        return True

    def _hydration_allowed(self, raw: dict[str, Any]) -> bool:
        """True when this message may be hydrated now. A bubble is
        hydrated at most once per cooldown window: the media URL, once
        captured, has already been forwarded with the message, and
        stubborn placeholders would otherwise cost a scroll plus a
        full timeout on every single poll."""
        last = self._hydration_limiter.get(_hydration_key(raw))
        if last is None:
            return True
        return time.monotonic() - last >= _HYDRATION_RETRY_COOLDOWN

    def _hydrate_media_bubbles(
        self, items: list[dict[str, Any]]
    ) -> dict[str, list]:
        """Scroll each marked media bubble into the viewport, wait for
        its images / video to resolve, and read the attachment list
        while the bubble is still in view (virtualization may recycle
        the DOM node once it scrolls out again). Returns marker →
        attachments for the bubbles that hydrated. Best effort: on
        timeout the message is forwarded without the media, same as
        before this pass existed. Every attempt records into the
        hydration limiter so the same bubble is not re-hydrated on
        every poll."""
        hydrated: dict[str, list] = {}
        for raw in items:
            marker = raw.get("marker") or ""
            self._hydration_limiter[_hydration_key(raw)] = time.monotonic()
            found = False
            try:
                found = bool(
                    self._driver.execute_script(_SCROLL_MARKER_JS, marker)
                )
            except WebDriverException as e:
                if _is_fatal_driver_error(e):
                    raise
                logger.warning(
                    "Hydration scroll failed for bubble marker {}: {}",
                    marker, e,
                )
            if not found:
                logger.warning(
                    "Media bubble marker {} not in DOM, skipping hydration",
                    marker,
                )
                continue
            ready = False
            end = time.monotonic() + _MEDIA_HYDRATION_TIMEOUT
            while time.monotonic() < end:
                try:
                    ready = bool(
                        self._driver.execute_script(_MEDIA_READY_JS, marker)
                    )
                except WebDriverException as e:
                    if _is_fatal_driver_error(e):
                        raise
                    return hydrated  # page / DOM went away
                if ready:
                    break
                time.sleep(0.5)
            # Small settle so trailing images of multi-photo grids finish
            # loading before the targeted re-read.
            time.sleep(1.0)
            logger.info(
                "Collector: media bubble marker {} hydrated: {}",
                marker, ready,
            )
            if not ready:
                # Stubborn placeholder: the grid never resolved inline.
                # Click the tiles open in the media viewer and capture
                # the full-size URLs from there instead.
                viewer_urls = self._capture_viewer_images(marker)
                if not viewer_urls:
                    continue
                atts = self._read_bubble_attachments(marker) or []
                known_urls = {
                    a.get("url") or ""
                    for a in atts
                    if isinstance(a, dict)
                }
                for url in viewer_urls:
                    if url not in known_urls:
                        atts.append({"type": "image", "url": url})
                if atts:
                    hydrated[marker] = atts
                continue
            atts = self._read_bubble_attachments(marker)
            if atts:
                hydrated[marker] = atts
        return hydrated

    def _read_bubble_attachments(self, marker: str) -> list | None:
        try:
            atts = self._driver.execute_script(_READ_ATTACHMENTS_JS, marker)
        except WebDriverException as e:
            if _is_fatal_driver_error(e):
                raise
            logger.warning(
                "Attachment re-read failed for bubble marker {}: {}",
                marker, e,
            )
            return None
        return atts if isinstance(atts, list) else None

    def _capture_viewer_images(self, marker: str) -> list[str]:
        """Last-resort hydration via the MAX media viewer.

        Clicks each media tile of the marked row so MAX opens its media
        viewer, reads the full-size image URL the viewer loaded, then
        closes the viewer again. Returns the captured http URLs; empty
        when the row has no tiles or the viewer never produced an
        image (best effort, same contract as the scroll pass).
        """
        urls: list[str] = []
        try:
            tile_count = int(
                self._driver.execute_script(_VIEWER_TILES_JS, marker) or 0
            )
        except WebDriverException as e:
            if _is_fatal_driver_error(e):
                raise
            logger.warning(
                "Viewer tile count failed for marker {}: {}", marker, e
            )
            return urls
        for tile_index in range(tile_count):
            url: str | None = None
            try:
                opened = bool(
                    self._driver.execute_script(
                        _VIEWER_CLICK_JS, marker, tile_index
                    )
                )
                if not opened:
                    break
                url = self._wait_viewer_image()
            except WebDriverException as e:
                if _is_fatal_driver_error(e):
                    raise
                logger.warning(
                    "Viewer click failed for marker {} tile {}: {}",
                    marker, tile_index, e,
                )
                break
            finally:
                self._close_media_viewer()
            if url and url not in urls:
                urls.append(url)
        if urls:
            logger.info(
                "Collector: marker {} viewer-captured {} image url(s)",
                marker, len(urls),
            )
        return urls

    def _wait_viewer_image(self, timeout: float = 4.0) -> str | None:
        """Poll until the media viewer shows a loaded full-size image."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                url = self._driver.execute_script(_VIEWER_IMG_JS)
            except WebDriverException as e:
                if _is_fatal_driver_error(e):
                    raise
                return None
            if url:
                return str(url)
            time.sleep(0.3)
        return None

    def _close_media_viewer(self) -> None:
        """Dismiss the media viewer opened by a tile click (Escape)."""
        try:
            body = self._driver.find_element("tag name", "body")
            body.send_keys(Keys.ESCAPE)
        except WebDriverException as e:
            if _is_fatal_driver_error(e):
                raise
            logger.debug("Media viewer close failed", exc_info=True)
        time.sleep(0.3)

    def click_file_download(self, filename: str) -> bool:
        """Click the download button for ``filename`` in the visible history.

        Returns True when a matching button was found and clicked. The caller
        is responsible for waiting for the file to appear in Chrome's
        download directory.
        """
        if not filename:
            return False
        try:
            ok = bool(self._driver.execute_script(_CLICK_FILE_JS, filename))
        except WebDriverException as e:
            if _is_fatal_driver_error(e):
                raise
            logger.warning("File click failed for {!r}: {}", filename, e)
            return False
        if not ok:
            logger.warning("Download button not found in DOM for {!r}", filename)
        return ok

    @staticmethod
    def _normalize(channel: str, raw: dict[str, Any]) -> Message:
        text = (raw.get("text") or "").strip()
        timestamp_raw = raw.get("timestamp")
        timestamp = timestamp_raw.strip() if isinstance(timestamp_raw, str) else None
        direction = raw.get("direction")
        author_info = raw.get("author") or {}
        author_name = author_info.get("name") if isinstance(author_info, dict) else None
        quote_raw = raw.get("quote")
        attachments_raw = raw.get("attachments") or []

        attachments = [
            Attachment(
                type=att.get("type", "link"),
                url=att.get("url"),
                filename=att.get("filename"),
                text=att.get("text"),
                file_type=att.get("file_type"),
                size_text=att.get("size_text"),
            )
            for att in attachments_raw
        ]

        quote: Quote | None = None
        if isinstance(quote_raw, dict):
            q_author = (quote_raw.get("author") or "").strip() or None
            q_text = (quote_raw.get("text") or "").strip() or None
            if q_author or q_text:
                quote = Quote(author=q_author, text=q_text)

        message_id = _fingerprint(
            text, timestamp, author_name, attachments_raw, quote_raw
        )

        return Message(
            id=message_id,
            channel=channel,
            text=text,
            author=author_name,
            quote=quote,
            timestamp=timestamp,
            direction=direction,
            attachments=attachments,
            raw_html_sample=raw.get("outerHTMLSample", ""),
        )

    def dump_for_discovery(self, output_dir: str | Path) -> tuple[Path, Path]:
        """Save current page HTML and a screenshot for selector discovery."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        html_path = out / f"page-{ts}.html"
        png_path = out / f"page-{ts}.png"

        try:
            html_path.write_text(self._driver.page_source, encoding="utf-8")
        except Exception:
            logger.exception("Failed to save page HTML")

        try:
            self._driver.save_screenshot(str(png_path))
        except Exception:
            logger.exception("Failed to save screenshot")

        return html_path, png_path
