"""DataNodes Link Resolver Module.

Handles resolution of datanodes.to links into direct downloadable streams.
Supports unthrottled maximum speed via Real-Debrid API when configured,
with pooled session form submission (op=download2) and 302 redirect capture.
"""

import asyncio
import re
import urllib.parse
from typing import Dict, Optional, Tuple
from curl_cffi import requests
from resolvers.debrider import RealDebridClient


class DataNodesResolver:
    """Resolver for datanodes.to links with per-interface session pooling."""

    DIRECT_URL_REGEX = re.compile(
        r'https?://[a-zA-Z0-9\.\-]+/d/[a-zA-Z0-9_\-]+/[^\s\'"<>]+'
    )
    DOWNLOAD_BTN_REGEX = re.compile(
        r'<a[^>]*href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*download[^"\']*["\']',
        re.IGNORECASE,
    )
    HIDDEN_INPUT_REGEX = re.compile(
        r'<input[^>]*type=["\']hidden["\'][^>]*>',
        re.IGNORECASE,
    )
    ATTR_NAME_REGEX = re.compile(r'name=["\']([^"\']+)["\']', re.IGNORECASE)
    ATTR_VALUE_REGEX = re.compile(r'value=["\']([^"\']*)["\']', re.IGNORECASE)

    _sessions: Dict[Optional[str], requests.AsyncSession] = {}

    @classmethod
    def _get_session(cls, interface: Optional[str] = None) -> requests.AsyncSession:
        sess = cls._sessions.get(interface)
        if sess is None or getattr(sess, "_closed", False):
            kwargs: dict = {"impersonate": "chrome", "max_clients": 12}
            if interface:
                kwargs["interface"] = interface
            sess = requests.AsyncSession(**kwargs)
            cls._sessions[interface] = sess
        return sess

    @classmethod
    def _evict_session(cls, interface: Optional[str] = None) -> None:
        sess = cls._sessions.pop(interface, None)
        if sess and not getattr(sess, "_closed", False):
            try:
                asyncio.create_task(sess.close())
            except Exception:
                pass

    @classmethod
    async def close_all(cls) -> None:
        for iface in list(cls._sessions.keys()):
            sess = cls._sessions.pop(iface, None)
            if sess and not getattr(sess, "_closed", False):
                try:
                    await sess.close()
                except Exception:
                    pass

    @classmethod
    def _extract_hidden_inputs(cls, html: str) -> Dict[str, str]:
        fields: Dict[str, str] = {}
        for tag in cls.HIDDEN_INPUT_REGEX.findall(html):
            name_m = cls.ATTR_NAME_REGEX.search(tag)
            val_m = cls.ATTR_VALUE_REGEX.search(tag)
            if name_m:
                fields[name_m.group(1)] = val_m.group(1) if val_m else ""
        return fields

    @classmethod
    async def resolve(
        cls,
        url: str,
        debrider: Optional[RealDebridClient] = None,
        interface: Optional[str] = None,
        timeout: int = 20,
    ) -> Tuple[str, str, int]:
        """Resolves a datanodes.to URL to (direct_url, filename, filesize)."""
        parsed = urllib.parse.urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        file_id = path_parts[0] if path_parts else ""
        expected_filename = urllib.parse.unquote(path_parts[1]) if len(path_parts) > 1 else ""

        # 1. Real-Debrid unrestriction if configured
        if debrider and debrider.is_configured:
            try:
                direct_url, filename, size = await debrider.unrestrict(
                    url, interface=interface, timeout=timeout
                )
                return direct_url, filename or expected_filename, size
            except Exception:
                pass

        # 2. Free Resolver with pooled session & non-redirecting POST capture
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://paste.fitgirl-repacks.site/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        last_err: Optional[Exception] = None
        for attempt in range(2):
            session = cls._get_session(interface)
            try:
                resp = await session.get(url, headers=headers, timeout=timeout)
                if resp.status_code == 404:
                    raise ValueError("Dead link (HTTP 404): File removed from DataNodes.")
                if resp.status_code != 200:
                    raise ValueError(f"DataNodes returned HTTP {resp.status_code}")

                html = resp.text
                if "File Not Found" in html or "file was deleted" in html.lower():
                    raise ValueError("Dead link: File deleted on DataNodes.")

                payload = cls._extract_hidden_inputs(html)
                payload.setdefault("op", "download2")
                if file_id:
                    payload.setdefault("id", file_id)
                payload.setdefault("method_free", "Free Download >>")

                # Do NOT follow redirects automatically so a 302 to the binary file
                # is captured from Location instead of downloading gigabytes into memory.
                post_resp = await session.post(
                    url,
                    data=payload,
                    headers={
                        **headers,
                        "Referer": url,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=timeout,
                    allow_redirects=False,
                )

                if post_resp.status_code in (301, 302, 303, 307, 308):
                    location = post_resp.headers.get("location", "").strip()
                    if location:
                        direct_url = urllib.parse.urljoin(url, location)
                        filename = expected_filename or urllib.parse.unquote(
                            urllib.parse.urlparse(direct_url).path.split("/")[-1]
                        )
                        return direct_url, filename or "part.rar", 0

                post_html = post_resp.text
                direct_match = cls.DIRECT_URL_REGEX.search(post_html)
                if direct_match:
                    direct_url = direct_match.group(0)
                    filename = expected_filename or urllib.parse.unquote(
                        direct_url.split("/")[-1]
                    )
                    return direct_url, filename, 0

                btn_match = cls.DOWNLOAD_BTN_REGEX.search(post_html)
                if btn_match:
                    direct_url = urllib.parse.urljoin(url, btn_match.group(1))
                    filename = expected_filename or urllib.parse.unquote(
                        direct_url.split("/")[-1]
                    )
                    return direct_url, filename, 0

                raise ValueError("DataNodes direct link not found in POST response.")
            except ValueError as ve:
                if "Dead link" in str(ve):
                    raise
                last_err = ve
                cls._evict_session(interface)
            except Exception as exc:
                last_err = exc
                cls._evict_session(interface)
                if attempt == 0:
                    await asyncio.sleep(0.5)

        raise last_err or ValueError("DataNodes resolution failed.")