"""FuckingFast Link Resolver Module.

Resolves fuckingfast.co landing pages into direct, high-speed binary streams
(https://dl.fuckingfast.co/dl/...) using the HTMX /f/{id}/go endpoint and curl_cffi
Chrome TLS/JA3 impersonation to bypass Cloudflare and ad redirects cleanly.
"""

import re
import urllib.parse
from typing import Optional, Tuple
from curl_cffi import requests


class FuckingFastResolver:
    """Resolver for extracting direct binary links from fuckingfast.co."""

    @classmethod
    async def resolve(
        cls,
        url: str,
        interface: Optional[str] = None,
        timeout: int = 15,
    ) -> Tuple[str, str, int]:
        """Resolves a fuckingfast.co URL to (direct_download_url, filename, filesize_bytes).

        Args:
            url: The fuckingfast landing page URL (e.g. https://fuckingfast.co/xrhzu2j9ft3u#file.rar)
            interface: Optional local IP address for socket binding (Combinational Net).
            timeout: Request timeout in seconds.

        Returns:
            Tuple of (direct_url, filename, filesize_bytes).

        Raises:
            ValueError: If link is dead (404) or failed to resolve direct CDN link.
        """
        parsed = urllib.parse.urlparse(url)
        expected_filename = urllib.parse.unquote(parsed.fragment) if parsed.fragment else ""
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

        # Extract file ID from path (e.g. /xrhzu2j9ft3u or /f/xrhzu2j9ft3u)
        path_segments = [p for p in parsed.path.split("/") if p]
        if not path_segments:
            raise ValueError(f"Invalid FuckingFast URL: {url}")
        file_id = path_segments[1] if path_segments[0] == "f" and len(path_segments) > 1 else path_segments[0]

        session_kwargs = {"impersonate": "chrome"}
        if interface:
            session_kwargs["interface"] = interface

        async with requests.AsyncSession(**session_kwargs) as session:
            # 1. Fetch initial landing page to establish CF cookies & verify file status
            landing_headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://paste.fitgirl-repacks.site/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            }

            resp = await session.get(clean_url, headers=landing_headers, timeout=timeout)
            if resp.status_code in (403, 429):
                # Back off briefly for Cloudflare rate limiting and retry once
                import asyncio
                await asyncio.sleep(2.0)
                resp = await session.get(clean_url, headers=landing_headers, timeout=timeout)

            if resp.status_code == 404 or "Not Found" in resp.text[:500]:
                raise ValueError(f"Dead link (HTTP 404 Not Found on FuckingFast). File was removed or DMCA'd.")

            if resp.status_code != 200:
                raise ValueError(f"FuckingFast landing page returned HTTP {resp.status_code}")

            # 2. Check for hx-post path or use standard /f/{file_id}/go
            hx_match = re.search(r'hx-post="([^"]+)"', resp.text)
            hx_path = hx_match.group(1) if hx_match else f"/f/{file_id}/go"
            go_url = urllib.parse.urljoin(clean_url, hx_path)

            # 3. Send HTMX POST request to trigger hx-redirect
            htmx_headers = {
                "HX-Request": "true",
                "HX-Current-URL": clean_url,
                "Referer": clean_url,
                "Origin": "https://fuckingfast.co",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            }

            post_resp = await session.post(go_url, headers=htmx_headers, timeout=timeout, allow_redirects=False)

            direct_url = post_resp.headers.get("hx-redirect")
            if not direct_url:
                # Check for direct dl link in response text or body
                dl_match = re.search(r'https?://dl\.fuckingfast\.co/dl/[^\s\'"<>]+', post_resp.text)
                if dl_match:
                    direct_url = dl_match.group(0)

            if not direct_url:
                raise ValueError("Could not extract direct dl.fuckingfast.co link from HTMX response.")

            # 4. Probe the direct CDN stream to get exact filesize and filename
            probe_resp = await session.get(
                direct_url,
                headers={"Referer": clean_url, "Range": "bytes=0-0"},
                stream=True,
                timeout=timeout,
            )

            filesize = 0
            filename = expected_filename

            # Extract size from Content-Range (e.g. bytes 0-2097151999/2097152000)
            content_range = probe_resp.headers.get("content-range", "")
            if "/" in content_range:
                try:
                    filesize = int(content_range.split("/")[-1])
                except ValueError:
                    pass

            # Extract filename from Content-Disposition if not already known
            cd = probe_resp.headers.get("content-disposition", "")
            if "filename*=" in cd:
                cd_name = cd.split("filename*=")[-1].strip().strip('"\'')
                if "''" in cd_name:
                    cd_name = cd_name.split("''")[-1]
                filename = urllib.parse.unquote(cd_name)
            elif "filename=" in cd and not filename:
                filename = cd.split("filename=")[-1].strip().strip('"\'')

            if not filename:
                filename = f"{file_id}.rar"

            return direct_url, filename, filesize
