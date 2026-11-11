"""PrivateBin v2 Decryption Module.

Handles automated client-side decryption of encrypted pastes from
https://paste.fitgirl-repacks.site and any standard PrivateBin v2 instance.
Decodes Base58 keys, executes PBKDF2-HMAC-SHA256 key derivation, processes
Additional Authenticated Data (AAD), and performs AES-256-GCM authenticated
decryption with zlib/rawdeflate decompression.
"""

import base64
import json
import re
import urllib.parse
import zlib
from typing import List, Tuple

import base58
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def parse_privatebin_url(url: str) -> Tuple[str, str, str]:
    """Parses a PrivateBin URL into base domain, paste ID, and decryption key.

    Example URL:
    https://paste.fitgirl-repacks.site/?99e460e2438a1459#36CYUqDBywuhLVUuoebSyT6RaWpC7bUkF2qDHTuKrpFV

    Returns:
        (base_url, paste_id, key)
    """
    parsed = urllib.parse.urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    paste_id = parsed.query.strip()
    key = parsed.fragment.strip()

    if not paste_id or not key:
        raise ValueError(
            f"Invalid PrivateBin URL format. Must include paste query '?id' and fragment '#key': {url}"
        )

    return base_url, paste_id, key


def decrypt_privatebin_payload(paste_data: dict, key_base58: str) -> str:
    """Decrypts a PrivateBin v2 JSON payload using the given Base58 key.

    Args:
        paste_data: Dictionary returned by the PrivateBin server JSON endpoint.
        key_base58: The symmetric key string from the URL fragment (#key).

    Returns:
        Plaintext decrypted content string.
    """
    adata = paste_data.get("adata")
    ct_b64 = paste_data.get("ct")

    if not adata or not ct_b64:
        raise ValueError("Invalid payload: Missing 'adata' or 'ct' fields.")

    spec = adata[0]
    iv = base64.b64decode(spec[0])
    raw_ct = base64.b64decode(ct_b64)

    # 1. Decode Base58 key with 32-byte zero-padding preservation
    raw_key = base58.b58decode(key_base58)
    if len(raw_key) < 32:
        raw_key = raw_key.rjust(32, b"\x00")

    # 2. Derive AES Key using PBKDF2-HMAC-SHA256 if spec specifies iterations and salt
    if len(spec) > 2 and spec[1] and spec[2]:
        salt = base64.b64decode(spec[1])
        iterations = int(spec[2])
        key_bytes_len = int(spec[3]) // 8 if len(spec) > 3 else 32
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=key_bytes_len,
            salt=salt,
            iterations=iterations,
        )
        aes_key = kdf.derive(raw_key)
    else:
        aes_key = raw_key

    # 3. Compact JSON serialization of Additional Authenticated Data (AAD)
    aad = json.dumps(adata, separators=(",", ":")).encode("utf-8")

    # 4. Decrypt via AES-GCM
    aesgcm = AESGCM(aes_key)
    try:
        decrypted_compressed = aesgcm.decrypt(iv, raw_ct, aad)
    except Exception as exc:
        raise ValueError("Decryption failed: Authentication tag mismatch or invalid key.") from exc

    # 5. Decompress payload (handles raw deflate, zlib, or uncompressed)
    try:
        decompressed = zlib.decompress(decrypted_compressed, -zlib.MAX_WBITS)
    except zlib.error:
        try:
            decompressed = zlib.decompress(decrypted_compressed)
        except zlib.error:
            decompressed = decrypted_compressed

    # 6. Extract message text
    text_content = decompressed.decode("utf-8", errors="replace")
    try:
        message_container = json.loads(text_content)
        if isinstance(message_container, dict) and "paste" in message_container:
            return message_container["paste"]
        elif isinstance(message_container, list) and message_container:
            return str(message_container[0])
    except json.JSONDecodeError:
        pass

    return text_content


def extract_links_from_text(text: str) -> List[str]:
    """Finds all URLs inside decrypted or raw text, stripping markup and trailing punctuation."""
    pattern = r"https?://[^\s'\"<>\)]+"
    candidates = re.findall(pattern, text)
    cleaned = []
    for link in candidates:
        link = link.rstrip(".,;)>]")
        if link and link not in cleaned:
            cleaned.append(link)
    return cleaned
