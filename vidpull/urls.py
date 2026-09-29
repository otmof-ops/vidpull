"""Finding links in whatever people paste, type or hand over on the command line."""

from __future__ import annotations

import re

_URL_IN_TEXT = re.compile(r"(?:https?://|www\.)[^\s<>\"'`]+", re.IGNORECASE)
_BARE_HOST = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?:[/:?#].*)?$")
_TRAILING = ".,;:!?)]}>'\""
_OPENED_BY = {")": "(", "]": "[", "}": "{"}


def looks_like_url(token: str) -> bool:
    token = token.strip()
    if not token or any(ch.isspace() or not ch.isprintable() for ch in token):
        return False  # a link is one word: no space, no line break, no control character
    lowered = token.lower()
    if lowered.startswith(("http://", "https://", "www.")):
        return True
    return bool(_BARE_HOST.match(token))


def normalize_url(token: str) -> str:
    """``token`` as a link: with a scheme, and without what the sentence around it put
    after it. A bracket that closes one the link opened is the link's own:
    ``…/wiki/Song_(album)``."""
    token = token.strip()
    while token and token[-1] in _TRAILING:
        last = token[-1]
        opener = _OPENED_BY.get(last)
        if opener is not None and token.count(opener) >= token.count(last):
            break
        token = token[:-1]
    return token if "://" in token else f"https://{token}"


def extract_urls(text: str | None) -> list[str]:
    """Every link in ``text``, in order, without duplicates; bare hosts count when alone."""
    if not text:
        return []
    found: list[str] = []
    for token in text.split():
        matches = [m.group(0) for m in _URL_IN_TEXT.finditer(token)]
        if matches:
            found.extend(normalize_url(m) for m in matches)
        elif looks_like_url(token):
            found.append(normalize_url(token))
    seen: set[str] = set()
    unique: list[str] = []
    for url in found:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique
