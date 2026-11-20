"""Web pages for answers: their text, to read and to check quotes against, as the library
does with files. A page on the user's own network (the router, the Pi's own pages) is never
fetched: each address is looked up once, checked, and that address is the one fetched."""

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from alfred.library import normal

AGENT = "Mozilla/5.0 (compatible; Alfred/1.2; +https://github.com/vincedevs/alfred)"


async def quote_on_page(url: str, quote: str, pages: dict) -> str | None:
    """None if the quote is on the page, else why not. `pages`: the texts fetched for this
    task, so each page is fetched once."""
    if len(normal(quote)) < 8:
        return "the quote is too short to check: quote a full phrase"
    text = await page(url, pages)
    if isinstance(text, Exception):
        return f"couldn't open that page ({text}): cite another source or drop the claim"
    return None if normal(quote) in normal(text) else "the quote isn't on that page"


async def page(url: str, pages: dict) -> str | Exception:
    """A page's text (or why it couldn't be had), fetched once per task."""
    if url not in pages:
        try:
            pages[url] = await asyncio.wait_for(fetch_text(url), 15)
        except Exception as error:
            pages[url] = error if str(error) else ValueError("it took too long")
    return pages[url]


async def fetch_text(url: str) -> str:
    """A page's text, at most 5 MB, following up to five redirects, each checked."""
    async with httpx.AsyncClient(trust_env=False) as client:  # straight to the checked address
        for _ in range(6):
            host, address = await public_address(url)
            parts = urlsplit(url)
            netloc = f"[{address}]" if ":" in address else address
            pinned = parts._replace(netloc=f"{netloc}:{parts.port}" if parts.port else netloc)
            async with client.stream(
                "GET", pinned.geturl(), headers={"Host": parts.netloc, "User-Agent": AGENT},
                extensions={"sni_hostname": host},
            ) as reply:  # fmt: skip
                if reply.is_redirect:
                    url = urljoin(url, reply.headers.get("location", ""))
                    continue
                reply.raise_for_status()
                body = bytearray()
                async for block in reply.aiter_bytes():
                    body += block
                    if len(body) > 5 << 20:
                        raise ValueError("the page is larger than 5 MB")
                kind = reply.headers.get("content-type", "text/html").split(";")[0]
                return await asyncio.to_thread(page_text, bytes(body), kind)
    raise ValueError("too many redirects")


async def public_address(url: str) -> tuple[str, str]:
    """The host and the one address it is fetched from, if every address it has is public."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http and https addresses")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    infos = await asyncio.to_thread(socket.getaddrinfo, parts.hostname, port)
    addresses = [info[4][0].split("%")[0] for info in infos]
    if not addresses or not all(ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError("that address is on a private network")
    return parts.hostname, addresses[0]


def page_text(body: bytes, kind: str) -> str:
    if kind == "text/plain":
        return body.decode("utf-8", errors="replace")
    if kind == "application/pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(body)
        try:
            return "\n".join(pdf[i].get_textpage().get_text_bounded() for i in range(len(pdf)))
        finally:
            pdf.close()
    from io import BytesIO

    from markitdown import MarkItDown, StreamInfo

    info = StreamInfo(mimetype=kind, extension=".html")
    return (
        MarkItDown(enable_plugins=False)
        .convert_stream(BytesIO(body), stream_info=info)
        .text_content
    )
