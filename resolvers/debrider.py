"""Real-Debrid API Client Module.

Provides automated link unrestriction via Real-Debrid API
(https://api.real-debrid.com/rest/1.0/unrestrict/link) with pooled connections
and rate-limit backoff.
"""

import asyncio
from typing import Dict, Optional, Tuple
from curl_cffi import requests


class RealDebridClient:
    """Client for unrestricting filehoster links via Real-Debrid."""

    API_URL = "https://api.real-debrid.com/rest/1.0/unrestrict/link"

    def __init__(self, api_token: Optional[str] = None):
        self.api_token = api_token.strip() if api_token else None
        self._sessions: Dict[Optional[str], requests.AsyncSession] = {}

    @property
    def is_configured(self) -> bool:
        """Returns True if a token has been set."""
        return bool(self.api_token)

    def _get_session(self, interface: Optional[str] = None) -> requests.AsyncSession:
        sess = self._sessions.get(interface)
        if sess is None or getattr(sess, "_closed", False):
            kwargs: dict = {"impersonate": "chrome", "max_clients": 16}
            if interface:
                kwargs["interface"] = interface
            sess = requests.AsyncSession(**kwargs)
            self._sessions[interface] = sess
        return sess

    async def close_all(self) -> None:
        for iface in list(self._sessions.keys()):
            sess = self._sessions.pop(iface, None)
            if sess and not getattr(sess, "_closed", False):
                try:
                    await sess.close()
                except Exception:
                    pass

    async def unrestrict(
        self,
        link: str,
        interface: Optional[str] = None,
        timeout: int = 15,
    ) -> Tuple[str, str, int]:
        """Unrestricts a hoster link into a direct high-speed CDN stream."""
        if not self.is_configured:
            raise ValueError("Real-Debrid API token is not configured.")

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "User-Agent": "PartHarvester/1.0",
        }
        data = {"link": link}

        for attempt in range(3):
            session = self._get_session(interface)
            resp = await session.post(
                self.API_URL,
                headers=headers,
                data=data,
                timeout=timeout,
            )

            if resp.status_code == 401:
                raise ValueError("Real-Debrid: Invalid API Token. Check token in settings.")
            if resp.status_code == 403:
                raise ValueError("Real-Debrid: Account expired or hoster traffic quota exceeded.")
            if resp.status_code in (429, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue

            if resp.status_code != 200:
                raise ValueError(
                    f"Real-Debrid error ({resp.status_code}): {resp.text[:200]}"
                )

            payload = resp.json()
            download_url = payload.get("download")
            filename = payload.get("filename", "")
            filesize = int(payload.get("filesize", 0) or 0)

            if not download_url:
                raise ValueError(f"Real-Debrid returned no download URL: {payload}")

            return download_url, filename, filesize

        raise ValueError("Real-Debrid: Rate limit exceeded after retries.")