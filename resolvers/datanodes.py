"""DataNodes Link Resolver Module.

Handles resolution of datanodes.to links into direct downloadable streams.
Supports unthrottled maximum speed via Real-Debrid API when configured,
with automated form submission (op=download2) fallback for free tier links.
"""

import re
import urllib.parse
from typing import Optional, Tuple
from curl_cffi import requests
from resolvers.debrider import RealDebridClient


class DataNodesResolver:
    """Resolver for datanodes.to links."""

    DIRECT_URL_REGEX = re.compile(r'https?://[a-zA-Z0-9\.\-]+/d/[a-zA-Z0-9_\-]+/[^\s\'"<>]+')
    DOWNLOAD_BTN_REGEX = re.compile(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*download[^"\']*["\']', re.IGNORECASE)

    @classmethod
    async def resolve(
        cls,
        url: str,
        debrider: Optional[RealDebridClient] = None,
        interface: Optional[str] = None,
        timeout: int = 20,
    ) -> Tuple[str, str, int]:
        """Resolves a datanodes.to URL to (direct_url, filename, filesize).

        Args:
            url: The datanodes.to link.
            debrider: Optional RealDebridClient for 1-click unthrottling.
            interface: Optional local IP address for socket binding.
            timeout: Request timeout.

        Returns:
            Tuple of (direct_url, filename, filesize).
        """
        parsed = urllib.parse.urlparse(url)
        path_parts = [p for p in parsed.path.split("/") if p]
        file_id = path_parts[0] if path_parts else ""
        expected_filename = urllib.parse.unquote(path_parts[1]) if len(path_parts) > 1 else ""

        # 1. If Real-Debrid is configured, unrestrict directly (fastest, unthrottled)
        if debrider and debrider.is_configured:
            try:
                direct_url, filename, size = await debrider.unrestrict(url, interface=interface, timeout=timeout)
                return direct_url, filename or expected_filename, size
            except Exception as e:
                # If debrider fails, fallback to free resolver
                pass

        # 2. Free Resolver: Fetch initial landing page and extract form parameters
        session_kwargs = {"impersonate": "chrome"}
        if interface:
            session_kwargs["interface"] = interface

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            "Referer": "https://paste.fitgirl-repacks.site/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        async with requests.AsyncSession(**session_kwargs) as session:
            resp = await session.get(url, headers=headers, timeout=timeout)
            html = resp.text

            # Look for op=download2 form
            payload = {
                "op": "download2",
                "id": file_id,
                "method_free": "Free Download >>",
            }

            # Check if there is a 'rand' hidden token input
            rand_match = re.search(r'<input[^>]*name=["\']rand["\'][^>]*value=["\']([^"\']+)["\']', html)
            if rand_match:
                payload["rand"] = rand_match.group(1)

            # Submit the form
            post_resp = await session.post(
                url,
                data=payload,
                headers={**headers, "Referer": url, "Content-Type": "application/x-www-form-urlencoded"},
                timeout=timeout,
                allow_redirects=True,
            )

            # Check for direct download link in post response
            post_html = post_resp.text
            direct_match = cls.DIRECT_URL_REGEX.search(post_html)
            if direct_match:
                direct_url = direct_match.group(0)
                filename = expected_filename or direct_url.split("/")[-1]
                return direct_url, filename, 0

            # Check if button href exists
            btn_match = cls.DOWNLOAD_BTN_REGEX.search(post_html)
            if btn_match:
                direct_url = btn_match.group(1)
                filename = expected_filename or direct_url.split("/")[-1]
                return direct_url, filename, 0

        # Fallback to original URL
        return url, expected_filename or "part.rar", 0

