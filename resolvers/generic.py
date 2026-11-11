"""Generic & Public Hoster Resolver Module.

Handles direct file links and open hosters such as PixelDrain and MediaFire.
"""

import re
import urllib.parse
from typing import Optional, Tuple
from curl_cffi import requests


class GenericResolver:
    """Resolver for direct URLs, PixelDrain, and MediaFire."""

    MEDIAFIRE_BTN_REGEX = re.compile(r'href=["\'](https?://download\d*\.mediafire\.com/[^"\']+)["\']')

    @classmethod
    async def resolve(
        cls,
        url: str,
        interface: Optional[str] = None,
        timeout: int = 15,
    ) -> Tuple[str, str]:
        """Normalizes and resolves a generic URL.

        Args:
            url: The link to resolve.
            interface: Optional local IP address for socket binding.
            timeout: Timeout in seconds.

        Returns:
            Tuple of (direct_url, filename).
        """
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()

        # 1. PixelDrain API direct stream transformation
        if "pixeldrain.com" in netloc:
            parts = [p for p in parsed.path.split("/") if p]
            file_id = parts[-1] if parts else ""
            if file_id and file_id != "file":
                direct_url = f"https://pixeldrain.com/api/file/{file_id}"
                filename = f"pixeldrain_{file_id}"
                return direct_url, filename

        # 2. MediaFire direct button scraper
        if "mediafire.com" in netloc:
            session_kwargs = {"impersonate": "chrome"}
            if interface:
                session_kwargs["interface"] = interface
            async with requests.AsyncSession(**session_kwargs) as session:
                resp = await session.get(url, timeout=timeout)
                match = cls.MEDIAFIRE_BTN_REGEX.search(resp.text)
                if match:
                    direct_url = match.group(1)
                    filename = urllib.parse.unquote(direct_url.split("/")[-1])
                    return direct_url, filename

        # 3. Direct binary link pass-through
        path_name = urllib.parse.unquote(parsed.path.split("/")[-1]) if parsed.path else "download.bin"
        fragment_name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else ""
        filename = fragment_name or path_name or "download.bin"
        return url, filename

