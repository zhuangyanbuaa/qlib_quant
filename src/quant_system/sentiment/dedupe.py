"""Stable URL, title, and semantic deduplication helpers."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}
_WORD = re.compile(r"[a-z0-9]+")


def canonicalize_url(url: str) -> str:
    """Return a tracking-free URL suitable for deterministic dedupe keys."""
    parsed = urlsplit(url.strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_KEYS
        and not key.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(query_pairs))
    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_text(value: str) -> str:
    """Lowercase text and keep only stable alphanumeric tokens."""
    return " ".join(_WORD.findall(value.lower()))


def title_fingerprint(title: str) -> str:
    """Hash a normalized title to catch common syndicated reposts."""
    return stable_hash(normalize_text(title), prefix="title")


def semantic_fingerprint(title: str, summary: str) -> str:
    """A lightweight semantic key using unique normalized title/summary tokens."""
    tokens = sorted(set(_WORD.findall(f"{title} {summary}".lower())))
    return stable_hash(" ".join(tokens), prefix="semantic")


def article_dedupe_key(*, url: str, title: str) -> str:
    """Prefer canonical URL identity, falling back to title identity."""
    canonical = canonicalize_url(url)
    if canonical:
        return stable_hash(canonical, prefix="url")
    return title_fingerprint(title)


def stable_hash(value: str, *, prefix: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"
