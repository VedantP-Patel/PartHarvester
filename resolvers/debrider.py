"""Real-Debrid API Client Module.

Provides automated link unrestriction via Real-Debrid API
(https://api.real-debrid.com/rest/1.0/unrestrict/link) to bypass countdown timers,
captchas, and speed throttles for hosters like 1fichier, Rapidgator, and DataNodes.
"""

from typing import Optional, Tuple
from curl_cffi import requests


class RealDebridClient:
    """Client for unrestricting filehoster links via Real-Debrid."""

    API_URL = "https://api.real-debrid.com/rest/1.0/unrestrict/link"

    def __init__(self, api_token: Optional[str] = None):
        self.api_token = api_token.strip() if api_token else None

    @property
    def is_configured(self) -> bool:
        """Returns True if a token has been set."""
        return bool(self.api_token)

    async def unrestrict(
        self,
        link: str,
        interface: Optional[str] = None,
        timeout: int = 15,
    ) -> Tuple[str, str, int]:
        """Unrestricts a hoster link into a direct high-speed CDN stream.

        Args:
            link: The original hoster URL (1fichier, Rapidgator, DataNodes, etc.).
            interface: Optional local IP address for socket binding.
            timeout: Request timeout in seconds.

        Returns:
            Tuple of (direct_download_url, filename, filesize_bytes).

        Raises:
            ValueError: If the token is invalid or the hoster is unsupported.
        """
        if not self.is_configured:
            raise ValueError("Real-Debrid API token is not configured.")

        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "User-Agent": "PartHarvester/1.0",
        }
        data = {"link": link}

        session_kwargs = {"impersonate": "chrome"}
        if interface:
            session_kwargs["interface"] = interface

        async with requests.AsyncSession(**session_kwargs) as session:
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

            if resp.status_code != 200:
                raise ValueError(
                    f"Real-Debrid error ({resp.status_code}): {resp.text[:200]}"
                )

            payload = resp.json()
            download_url = payload.get("download")
            filename = payload.get("filename", "")
            filesize = payload.get("filesize", 0)

            if not download_url:
                raise ValueError(f"Real-Debrid returned no download URL: {payload}")

            return download_url, filename, filesize

