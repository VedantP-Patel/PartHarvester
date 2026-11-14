"""Generic & Public Hoster Resolver Module.

Handles direct file links and open hosters such as PixelDrain and MediaFire.
"""

import re
import urllib.parse
from typing import Dict, Optional, Tuple
from curl_cffi import requests


class GenericResolver:
    """Resolver for direct URLs, PixelDrain, and MediaFire."""

    MEDIAFIRE_BTN_REGEX = re.compile(
        r'<a\b[^>]*\bhref=["\'](https?://download\d*\.mediafire\.com/[^"\']+)["\'][^>]*>',
        re.IGNORECASE,
    )
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
    async def close_all(cls) -> None:
        for iface in list(cls._sessions.keys()):
            sess = cls._sessions.pop(iface, None)
            if sess and not getattr(sess, "_closed", False):
                try:
                    await sess.close()
                except Exception:
                    pass

    @classmethod
    async def resolve(
        cls,
        url: str,
        interface: Optional[str] = None,
        timeout: int = 15,
    ) -> Tuple[str, str, int]:
        """Normalizes and resolves a generic URL to (direct_url, filename, filesize)."""
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()

        # 1. PixelDrain API direct stream + metadata lookup
        if "pixeldrain.com" in netloc:
            parts = [p for p in parsed.path.split("/") if p]
            file_id = parts[-1] if parts else ""
            if file_id and file_id != "file":
                direct_url = f"https://pixeldrain.com/api/file/{file_id}"
                filename = f"pixeldrain_{file_id}"
                filesize = 0
                try:
                    session = cls._get_session(interface)
                    info_resp = await session.get(
                        f"https://pixeldrain.com/api/file/{file_id}/info",
                        timeout=min(timeout, 8),
                    )
                    if info_resp.status_code == 404:
                        raise ValueError("Dead link (HTTP 404): PixelDrain file not found.")
                    if info_resp.status_code == 200:
                        info = info_resp.json()
                        if info.get("name"):
                            filename = info["name"]
                        if info.get("size"):
                            filesize = int(info["size"])
                except ValueError:
                    raise
                except Exception:
                    pass
                return direct_url, filename, filesize

        # 2. MediaFire direct button scraper
        if "mediafire.com" in netloc:
            session = cls._get_session(interface)
            resp = await session.get(url, timeout=timeout, allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location", "")
                if "download" in loc and "mediafire.com" in loc:
                    filename = urllib.parse.unquote(urllib.parse.urlparse(loc).path.split("/")[-1])
                    return loc, filename or "mediafire.bin", 0
            if resp.status_code == 404:
                raise ValueError("Dead link (HTTP 404): MediaFire file removed.")
            match = cls.MEDIAFIRE_BTN_REGEX.search(resp.text)
            if match:
                direct_url = match.group(1)
                filename = urllib.parse.unquote(
                    urllib.parse.urlparse(direct_url).path.split("/")[-1]
                )
                return direct_url, filename or "mediafire.bin", 0

        # 3. Direct binary link pass-through
        path_name = urllib.parse.unquote(parsed.path.split("/")[-1]) if parsed.path else "download.bin"
        fragment_name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else ""
        filename = fragment_name or path_name or "download.bin"
        return url, filename, 0