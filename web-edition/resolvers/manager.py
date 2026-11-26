"""Universal Link Ingestion and Resolver Manager with Per-Interface Parallel Pre-Resolution."""

import asyncio
import time
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
    is_privatebin_url,
    parse_privatebin_url,
)


class LinkManager:
    """Central manager for link extraction, decryption, and per-interface hoster resolution."""

    _CACHE_TTL_SECONDS = 900.0

    def __init__(self, rd_client: Optional[RealDebridClient] = None):
        self.rd_client = rd_client or RealDebridClient()
        self._pre_resolution_cache: Dict[Tuple[str, Optional[str]], Tuple[float, Tuple[str, str, int]]] = {}
        self._pre_resolving_set: Set[Tuple[str, Optional[str]]] = set()
        self._bg_tasks: Set[asyncio.Task] = set()
        self._domain_semaphores: Dict[Tuple[str, Optional[str]], asyncio.Semaphore] = {}
        self._domain_last_request_time: Dict[Tuple[str, Optional[str]], float] = {}
        self._domain_lock = asyncio.Lock()

    async def _get_domain_semaphore(self, netloc: str, interface: Optional[str]) -> asyncio.Semaphore:
        key = (netloc, interface)
        async with self._domain_lock:
            if key not in self._domain_semaphores:
                self._domain_semaphores[key] = asyncio.Semaphore(3)
            return self._domain_semaphores[key]

    async def _fetch_privatebin_links(
        self, session: "requests.AsyncSession", pb_url: str
    ) -> List[str]:
        """Fetch, decrypt and extract links from a single PrivateBin URL."""
        try:
            base_url, paste_id, key = parse_privatebin_url(pb_url)
            json_endpoint = f"{base_url}?{paste_id}"
            resp = await session.get(
                json_endpoint,
                headers={"X-Requested-With": "JSONHttpRequest"},
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            payload = resp.json()
            decrypted_text = await asyncio.to_thread(
                decrypt_privatebin_payload, payload, key
            )
            return extract_links_from_text(decrypted_text)
        except Exception:
            return []

    async def ingest_input(self, raw_input: str) -> List[str]:
        raw_input = raw_input.strip()
        if not raw_input:
            return []

        tokens = extract_links_from_text(raw_input)
        privatebin_urls = [u for u in tokens if is_privatebin_url(u)]
        direct_urls = [u for u in tokens if not is_privatebin_url(u)]

        if not privatebin_urls:
            return list(dict.fromkeys(direct_urls))

        collected_links: List[str] = list(direct_urls)
        async with requests.AsyncSession(impersonate="chrome") as session:
            # Fetch all PrivateBin pastes concurrently; gather preserves input order.
            results = await asyncio.gather(
                *(self._fetch_privatebin_links(session, u) for u in privatebin_urls)
            )
        for links in results:
            collected_links.extend(links)

        return list(dict.fromkeys(collected_links))

    async def close(self) -> None:
        for t in list(self._bg_tasks):
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        self._pre_resolving_set.clear()

        await self.rd_client.close_all()
        await DataNodesResolver.close_all()
        await GenericResolver.close_all()
        if hasattr(FuckingFastResolver, "close_all"):
            await FuckingFastResolver.close_all()

    def invalidate_cache(self, url: str, interface: Optional[str] = None) -> None:
        if interface is None:
            for key in [k for k in self._pre_resolution_cache if k[0] == url]:
                self._pre_resolution_cache.pop(key, None)
        else:
            self._pre_resolution_cache.pop((url, interface), None)

    def _get_cached(self, cache_key: Tuple[str, Optional[str]]) -> Optional[Tuple[str, str, int]]:
        entry = self._pre_resolution_cache.get(cache_key)
        if not entry:
            return None
        ts, value = entry
        if (time.monotonic() - ts) > self._CACHE_TTL_SECONDS:
            self._pre_resolution_cache.pop(cache_key, None)
            return None
        return value

    async def pre_resolve_batch(
        self,
        urls: List[str],
        interface: Optional[str] = None,
        max_ahead: int = 6,
    ) -> None:
        candidates = [
            u for u in urls
            if self._get_cached((u, interface)) is None
            and (u, interface) not in self._pre_resolving_set
        ]
        for url in candidates[:max_ahead]:
            self._pre_resolving_set.add((url, interface))
            t = asyncio.create_task(self._resolve_and_cache(url, interface))
            self._bg_tasks.add(t)
            t.add_done_callback(self._bg_tasks.discard)

    async def _resolve_and_cache(self, url: str, interface: Optional[str]) -> None:
        try:
            res = await self.resolve_link(url, interface=interface, use_cache=False)
            self._pre_resolution_cache[(url, interface)] = (time.monotonic(), res)
        except Exception:
            pass
        finally:
            self._pre_resolving_set.discard((url, interface))

    async def resolve_link(
        self,
        url: str,
        interface: Optional[str] = None,
        use_cache: bool = True,
    ) -> Tuple[str, str, int]:
        cache_key = (url, interface)
        if use_cache:
            cached = self._get_cached(cache_key)
            if cached is not None:
                return cached
            # Also reuse any fresh resolution for this URL from another local interface
            for (c_url, _), (ts, val) in list(self._pre_resolution_cache.items()):
                if c_url == url and (time.monotonic() - ts) <= self._CACHE_TTL_SECONDS:
                    return val

        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()

        sem = await self._get_domain_semaphore(netloc, interface)
        async with sem:
            loop = asyncio.get_running_loop()
            pace_key = (netloc, interface)
            now = loop.time()
            last_time = self._domain_last_request_time.get(pace_key, 0.0)
            elapsed = now - last_time
            if elapsed < 0.35:
                await asyncio.sleep(0.35 - elapsed)
            self._domain_last_request_time[pace_key] = loop.time()

            if self.rd_client.is_configured and any(
                h in netloc for h in ["1fichier.com", "rapidgator.net"]
            ):
                try:
                    res = await self.rd_client.unrestrict(url, interface=interface)
                    self._pre_resolution_cache[cache_key] = (time.monotonic(), res)
                    return res
                except Exception:
                    pass

            if "fuckingfast.co" in netloc:
                res = await FuckingFastResolver.resolve(url, interface=interface)
                self._pre_resolution_cache[cache_key] = (time.monotonic(), res)
                return res

            if "datanodes.to" in netloc:
                res = await DataNodesResolver.resolve(
                    url, debrider=self.rd_client, interface=interface
                )
                self._pre_resolution_cache[cache_key] = (time.monotonic(), res)
                return res

            # GenericResolver now returns (direct_url, filename, filesize) directly.
            res = await GenericResolver.resolve(url, interface=interface)
            self._pre_resolution_cache[cache_key] = (time.monotonic(), res)
            return res