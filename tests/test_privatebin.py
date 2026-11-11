"""Unit Tests for PrivateBin Decryption Module."""

import base64
import json
import os
import zlib
import base58
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from resolvers.privatebin import (
    decrypt_privatebin_payload,
    extract_links_from_text,
    parse_privatebin_url,
)


def test_parse_privatebin_url():
    url = "https://paste.fitgirl-repacks.site/?99e460e2438a1459#36CYUqDBywuhLVUuoebSyT6RaWpC7bUkF2qDHTuKrpFV"
    base_url, paste_id, key = parse_privatebin_url(url)
    assert base_url == "https://paste.fitgirl-repacks.site/"
    assert paste_id == "99e460e2438a1459"
    assert key == "36CYUqDBywuhLVUuoebSyT6RaWpC7bUkF2qDHTuKrpFV"


def test_extract_links_from_text():
    sample_text = """
    # Phantom Blade Zero Download Links
    - https://fuckingfast.co/part01.rar#file.rar
    - https://datanodes.to/part02.rar
    Some other text here...
    https://pixeldrain.com/u/abc12345
    """
    links = extract_links_from_text(sample_text)
    assert len(links) == 3
    assert "https://fuckingfast.co/part01.rar#file.rar" in links
    assert "https://datanodes.to/part02.rar" in links
    assert "https://pixeldrain.com/u/abc12345" in links


def test_privatebin_encryption_decryption_roundtrip():
    # 1. Generate 32-byte (256-bit) key
    raw_key = os.urandom(32)
    key_b58 = base58.b58encode(raw_key).decode("utf-8")

    # 2. Plaintext payload
    secret_text = "https://fuckingfast.co/part01.rar\nhttps://fuckingfast.co/part02.rar"
    message_json = json.dumps({"paste": secret_text}).encode("utf-8")
    compressed = zlib.compress(message_json)

    # 3. Create AAD spec with salt and iterations
    iv = os.urandom(16)
    salt = os.urandom(8)
    iterations = 1000
    adata = [
        [
            base64.b64encode(iv).decode("utf-8"),
            base64.b64encode(salt).decode("utf-8"),
            iterations,
            256,
            128,
            "aes",
            "gcm",
            "zlib",
        ]
    ]
    aad = json.dumps(adata, separators=(",", ":")).encode("utf-8")

    # Derive key via PBKDF2
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    derived_aes_key = kdf.derive(raw_key)

    # 4. Encrypt using AES-GCM
    aesgcm = AESGCM(derived_aes_key)
    ciphertext_with_tag = aesgcm.encrypt(iv, compressed, aad)
    ct_b64 = base64.b64encode(ciphertext_with_tag).decode("utf-8")

    mock_paste_response = {
        "status": 0,
        "id": "test_paste",
        "v": 2,
        "adata": adata,
        "ct": ct_b64,
    }

    # 5. Decrypt using our module
    decrypted = decrypt_privatebin_payload(mock_paste_response, key_b58)
    assert decrypted == secret_text
    extracted = extract_links_from_text(decrypted)
    assert len(extracted) == 2
