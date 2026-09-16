"""Provider-neutral URL identity helpers."""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


def canonical_product_url(value: str, *, sort_query: bool = True) -> str:
    """Return one stable browser identity for redirects and default documents."""
    parsed = urlsplit(str(value).strip())
    scheme = parsed.scheme.casefold()
    if scheme in {"http", "https"}:
        scheme = "https"
    path = unquote(parsed.path).replace("//", "/").rstrip("/") or "/"
    if path.casefold().endswith(("/index.html", "/index.htm")):
        path = path.rsplit("/", 1)[0] or "/"
    query = parsed.query
    if sort_query and query:
        query = urlencode(sorted(parse_qsl(query, keep_blank_values=True)), doseq=True)
    return urlunsplit((scheme, parsed.netloc.casefold(), path, query, ""))


__all__ = ["canonical_product_url"]
