"""FuckingFast Link Resolver Module.

Resolves fuckingfast.co landing pages into direct, high-speed binary streams
(https://dl.fuckingfast.co/dl/...) using the HTMX /f/{id}/go endpoint and curl_cffi
Chrome / Safari TLS/JA3 impersonation with multi-profile fallback and cookie persistence
to reliably navigate Cloudflare bot challenges and rate limits.
"""

import asyncio
import re
import urllib.parse
from typing import Dict, Optional, Tuple
from curl_cffi import requests


class FuckingFastResolver:
    """Resolver for extracting direct binary links from fuckingfast.co."""

    _sessions: Dict[Tuple[Optional[str], str], requests.AsyncSession] = {}
    _PROFILES = ["chrome", "safari17_0", "chrome124"]

    @classmethod
    def _get_session(cls, interface: Optional[str], impersonate: str) -> requests.AsyncSession:
        key = (interface, impersonate)
        session = cls._sessions.get(key)
        if session is None or getattr(session, "_closed", False):
            s_kwargs = {"impersonate": impersonate}
            if interface:
                s_kwargs["interface"] = interface
            session = requests.AsyncSession(**s_kwargs)
            cls._sessions[key] = session
        return session

    @classmethod
    def _evict_session(cls, interface: Optional[str], impersonate: str) -> None:
        key = (interface, impersonate)
        s = cls._sessions.pop(key, None)
        if s and not getattr(s, "_closed", False):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(s.close())
            except RuntimeError:
                try:
                    asyncio.run(s.close())
                except Exception:
                    pass

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

        last_error = None

        for attempt, profile in enumerate(cls._PROFILES):
            try:
                session = cls._get_session(interface, profile)

                # Fetch landing page to establish CF cookies & extract hx-post
                landing_headers = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://paste.fitgirl-repacks.site/",
                }

                resp = await session.get(clean_url, headers=landing_headers, timeout=timeout)
                if resp.status_code in (403, 429):
                    cls._evict_session(interface, profile)
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue

                if resp.status_code == 404 or "Not Found" in resp.text[:500]:
                    raise ValueError("Dead link (HTTP 404 Not Found on FuckingFast). File was removed or DMCA'd.")

                if resp.status_code != 200:
                    cls._evict_session(interface, profile)
                    await asyncio.sleep(1.0)
                    continue

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
                }

                post_resp = await session.post(go_url, headers=htmx_headers, timeout=timeout, allow_redirects=False)

                direct_url = post_resp.headers.get("hx-redirect")
                if not direct_url:
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

                try:
                    content_range = probe_resp.headers.get("content-range", "")
                    if "/" in content_range:
                        try:
                            filesize = int(content_range.split("/")[-1])
                        except ValueError:
                            pass

                    cd = probe_resp.headers.get("content-disposition", "")
                    if "filename*=" in cd:
                        cd_name = cd.split("filename*=")[-1].strip().strip('"\'')
                        if "''" in cd_name:
                            cd_name = cd_name.split("''")[-1]
                        filename = urllib.parse.unquote(cd_name)
                    elif "filename=" in cd and not filename:
                        filename = cd.split("filename=")[-1].strip().strip('"\'')
                finally:
                    # Release the streamed connection back to the pooled session
                    try:
                        await probe_resp.aclose()
                    except Exception:
                        pass

                if not filename:
                    filename = f"{file_id}.rar"

                return direct_url, filename, filesize

            except ValueError as ve:
                if "Dead link" in str(ve):
                    raise
                last_error = ve
            except Exception as e:
                cls._evict_session(interface, profile)
                last_error = e
                await asyncio.sleep(1.0)

        raise ValueError(f"FuckingFast landing page returned HTTP 403 or failed after fallback attempts: {last_error}")
