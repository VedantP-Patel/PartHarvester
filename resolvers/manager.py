"""Universal Link Ingestion and Resolver Manager with Pre-Resolution Pipeline.

Features:
- Automated PrivateBin v2 client decryption.
- Pipelined asynchronous background pre-resolving of upcoming links.
- Instant 0ms cache lookups when workers request direct streams.
"""

import asyncio
import urllib.parse
from typing import Dict, List, Optional, Set, Tuple
from curl_cffi import requests

from resolvers.datanodes import DataNodesResolver
from resolvers.debrider import RealDebridClient
from resolvers.fuckingfast import FuckingFastResolver
from resolvers.generic import GenericResolver
from resolvers.privatebin import (
    decrypt_privatebin_payload,
    extract_links_from_text,
    parse_privatebin_url,
)


class LinkManager:
    """Central manager for link extraction, decryption, and hoster resolution."""

    def __init__(self, rd_client: Optional[RealDebridClient] = None):
        self.rd_client = rd_client or RealDebridClient()
        self._pre_resolution_cache: Dict[str, Tuple[str, str, int]] = {}
        self._pre_resolving_set: Set[str] = set()
        self._domain_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._domain_last_request_time: Dict[str, float] = {}
        self._domain_lock = asyncio.Lock()

    async def _get_domain_semaphore(self, netloc: str) -> asyncio.Semaphore:
        """Limits concurrent landing page resolutions to avoid triggering hoster/Cloudflare WAF rate limits."""
        async with self._domain_lock:
            if netloc not in self._domain_semaphores:
                self._domain_semaphores[netloc] = asyncio.Semaphore(2)
            return self._domain_semaphores[netloc]

    async def ingest_input(self, raw_input: str) -> List[str]:
        """Ingests arbitrary user input and extracts all candidate download links."""
        raw_input = raw_input.strip()
        if not raw_input:
            return []

        # Check if input is a PrivateBin URL
        if "paste.fitgirl-repacks.site" in raw_input or (
            "paste" in raw_input and "?" in raw_input and "#" in raw_input
        ):
            lines = [line.strip() for line in raw_input.splitlines() if line.strip()]
            for line in lines:
                if "?" in line and "#" in line:
                    try:
                        base_url, paste_id, key = parse_privatebin_url(line)
                        json_endpoint = f"{base_url}?{paste_id}"

                        async with requests.AsyncSession(impersonate="chrome") as session:
                            resp = await session.get(
                                json_endpoint,
                                headers={"X-Requested-With": "JSONHttpRequest"},
                                timeout=15,
                            )
                            if resp.status_code == 200:
                                payload = resp.json()
                                # Offload CPU-heavy PBKDF2 + AES-GCM + zlib work off the event loop
                                decrypted_text = await asyncio.to_thread(
                                    decrypt_privatebin_payload, payload, key
                                )
                                return extract_links_from_text(decrypted_text)
                    except Exception:
                        pass

        # Standard extraction from multi-line or single text input
        return extract_links_from_text(raw_input)

    def invalidate_cache(self, url: str) -> None:
        """Evicts a stale or expired direct URL from the pre-resolution cache."""
        self._pre_resolution_cache.pop(url, None)

    async def pre_resolve_batch(
        self,
        urls: List[str],
        interface: Optional[str] = None,
        max_ahead: int = 5,
    ) -> None:
        """Pre-resolves upcoming queued URLs in the background to eliminate dispatch latency."""
        candidates = [u for u in urls if u not in self._pre_resolution_cache and u not in self._pre_resolving_set]
        to_process = candidates[:max_ahead]

        for url in to_process:
            self._pre_resolving_set.add(url)
            asyncio.create_task(self._resolve_and_cache(url, interface))

    async def _resolve_and_cache(self, url: str, interface: Optional[str]) -> None:
        """Helper to resolve a link in the background and insert into cache."""
        try:
            res = await self.resolve_link(url, interface=interface, use_cache=False)
            self._pre_resolution_cache[url] = res
        except Exception:
            pass
        finally:
            self._pre_resolving_set.discard(url)

    async def resolve_link(
        self,
        url: str,
        interface: Optional[str] = None,
        use_cache: bool = True,
    ) -> Tuple[str, str, int]:
        """Resolves a candidate link using the appropriate hoster resolver with domain pacing.

        Checks pre-resolution cache first for instantaneous 0ms pickup.
        """
        if use_cache and url in self._pre_resolution_cache:
            return self._pre_resolution_cache[url]

        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()

        # Enforce domain-level concurrency limiting and polite inter-request pacing
        sem = await self._get_domain_semaphore(netloc)
        async with sem:
            loop = asyncio.get_running_loop()
            now = loop.time()
            last_time = self._domain_last_request_time.get(netloc, 0.0)
            elapsed = now - last_time
            if elapsed < 0.6:
                await asyncio.sleep(0.6 - elapsed)
            self._domain_last_request_time[netloc] = loop.time()

            # 1. Real-Debrid unrestrictor (if configured and hoster is supported)
            if self.rd_client.is_configured and any(
                h in netloc for h in ["1fichier.com", "rapidgator.net"]
            ):
                try:
                    res = await self.rd_client.unrestrict(url, interface=interface)
                    self._pre_resolution_cache[url] = res
                    return res
                except Exception:
                    pass

            # 2. FuckingFast Resolver
            if "fuckingfast.co" in netloc:
                res = await FuckingFastResolver.resolve(url, interface=interface)
                self._pre_resolution_cache[url] = res
                return res

            # 3. DataNodes Resolver
            if "datanodes.to" in netloc:
                res = await DataNodesResolver.resolve(
                    url, debrider=self.rd_client, interface=interface
                )
                self._pre_resolution_cache[url] = res
                return res

            # 4. Generic & Public Hosters (PixelDrain, MediaFire, direct)
            direct_url, filename = await GenericResolver.resolve(url, interface=interface)
            res = (direct_url, filename, 0)
            self._pre_resolution_cache[url] = res
            return res
