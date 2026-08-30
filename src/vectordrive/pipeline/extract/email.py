"""Email (.eml) extraction: parse MIME structure, index the identifying
headers (Subject/From/To/Date) plus the human-readable body text, and
drop attachment/binary payloads entirely.

Registered as a real extractor (not the plain-text sniff fallback) after
diagnosing a real-corpus bug (2026-08-23): .eml wasn't in
_SUPPORTED_EXTENSIONS, so it fell through to TextExtractor via
_looks_like_text sniffing (raw RFC822 email source is technically valid
UTF-8), which read the *entire* raw MIME source — headers, ARC-Seal/DKIM
signatures, and base64-encoded attachment payloads — as literal document
text. One file alone produced 1,822 chunks of base64 noise (roughly a
third of the whole corpus's chunks at the time), fully present in the
lexical (FTS) index and competing in every keyword search against real
content, with none of it ever being genuine document text.
"""
from __future__ import annotations

from email import message_from_bytes, policy
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path

from vectordrive.pipeline.extract.base import ExtractedDocument, ExtractedPage

_HEADER_FIELDS = ("Subject", "From", "To", "Date")


class _HTMLTextExtractor(HTMLParser):
    """Minimal, dependency-free HTML-to-text: collects visible text,
    dropping script/style content and all markup. Used only as a fallback
    when a message has no text/plain part at all (get_body() preferred
    'plain' but the message only offered 'html').
    """

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.text()


def _extract_body(msg: EmailMessage) -> str:
    """The message's readable body, via EmailMessage.get_body() — the
    stdlib API that already walks multipart/alternative and multipart/
    mixed correctly and excludes attachments (any part with
    Content-Disposition: attachment) by construction, so there is no
    separate "drop attachments" step to get wrong. Prefers text/plain;
    falls back to text/html, stripped to visible text. Returns "" for a
    message with no textual body part at all (e.g. attachment-only).
    """
    body_part = msg.get_body(preferencelist=("plain", "html"))
    if body_part is None:
        return ""
    content = body_part.get_content()
    if body_part.get_content_type() == "text/html":
        content = _html_to_text(content)
    return content


class EmailExtractor:
    NAME = "email"
    VERSION = "1"

    def extract(self, path: Path) -> ExtractedDocument:
        with open(path, "rb") as f:
            raw = f.read()
        msg: EmailMessage = message_from_bytes(raw, policy=policy.default)

        header_lines = [f"{field}: {msg[field]}" for field in _HEADER_FIELDS if msg[field]]
        body = _extract_body(msg).strip()

        text = "\n".join(header_lines)
        if body:
            text = f"{text}\n\n{body}" if text else body

        page = ExtractedPage(page_number=1, text=text, text_source="native", needs_ocr=False)
        return ExtractedDocument(
            extractor_name=self.NAME, extractor_version=self.VERSION, page_count=1, pages=[page]
        )
