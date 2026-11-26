use aes_gcm::{
    aead::{Aead, KeyInit, Payload},
    Aes256Gcm, Nonce,
};
use anyhow::{anyhow, Result};
use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
use flate2::read::{DeflateDecoder, ZlibDecoder};
use pbkdf2::pbkdf2_hmac;
use regex::Regex;
use serde_json::Value;
use sha2::Sha256;
use std::io::Read;
use url::Url;

pub fn is_privatebin_url(raw: &str) -> bool {
    let Ok(parsed) = Url::parse(raw.trim()) else {
        return false;
    };
    if !matches!(parsed.scheme(), "http" | "https") || parsed.host_str().is_none() {
        return false;
    }
    let query = parsed.query().unwrap_or("").trim();
    let frag = parsed.fragment().unwrap_or("").trim();
    !query.is_empty() && !frag.is_empty() && frag.len() >= 20 && !query.contains('&')
}

pub fn parse_privatebin_url(raw: &str) -> Result<(String, String, String)> {
    let parsed = Url::parse(raw.trim())?;
    let host = parsed
        .host_str()
        .ok_or_else(|| anyhow!("Missing host in PrivateBin URL"))?;
    let port_part = parsed.port().map(|p| format!(":{p}")).unwrap_or_default();
    let base_url = format!("{}://{host}{port_part}{}", parsed.scheme(), parsed.path());
    let paste_id = parsed.query().unwrap_or("").trim().to_string();
    let key = parsed.fragment().unwrap_or("").trim().to_string();
    if paste_id.is_empty() || key.is_empty() {
        return Err(anyhow!("Invalid PrivateBin URL: missing ?id or #key"));
    }
    Ok((base_url, paste_id, key))
}

pub fn decrypt_privatebin_payload(paste_data: &Value, key_base58: &str) -> Result<String> {
    if let Some(status) = paste_data.get("status").and_then(|v| v.as_i64()) {
        if status != 0 {
            let msg = paste_data
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or("Unknown error");
            return Err(anyhow!("PrivateBin server error: {msg}"));
        }
    }

    let adata = paste_data
        .get("adata")
        .ok_or_else(|| anyhow!("Missing 'adata' field"))?;
    let ct_b64 = paste_data
        .get("ct")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("Missing 'ct' field"))?;

    let spec = adata
        .as_array()
        .and_then(|a| a.first())
        .and_then(|v| v.as_array())
        .ok_or_else(|| anyhow!("Invalid 'adata' spec"))?;

    let iv_b64 = spec
        .first()
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("Missing IV"))?;
    let iv = B64.decode(iv_b64)?;
    let raw_ct = B64.decode(ct_b64)?;

    let mut raw_key = bs58::decode(key_base58).into_vec()?;
    if raw_key.len() < 32 {
        let mut padded = vec![0u8; 32 - raw_key.len()];
        padded.extend_from_slice(&raw_key);
        raw_key = padded;
    }

    let aes_key = if spec.len() > 2 {
        let salt_b64 = spec[1].as_str().unwrap_or("");
        let iterations = spec[2].as_u64().unwrap_or(0) as u32;
        if !salt_b64.is_empty() && iterations > 0 {
            let salt = B64.decode(salt_b64)?;
            let key_len = spec
                .get(3)
                .and_then(|v| v.as_u64())
                .map(|bits| (bits / 8) as usize)
                .unwrap_or(32);
            let mut derived = vec![0u8; key_len];
            pbkdf2_hmac::<Sha256>(&raw_key, &salt, iterations, &mut derived);
            derived
        } else {
            raw_key
        }
    } else {
        raw_key
    };

    // Compact JSON AAD matching PrivateBin v2 spec
    let aad = serde_json::to_string(adata)?.into_bytes();

    let cipher = Aes256Gcm::new_from_slice(&aes_key)
        .map_err(|e| anyhow!("Invalid AES-256 key length: {e}"))?;
    let nonce = Nonce::from_slice(&iv);
    let decrypted_compressed = cipher
        .decrypt(
            nonce,
            Payload {
                msg: &raw_ct,
                aad: &aad,
            },
        )
        .map_err(|_| anyhow!("Decryption failed: authentication tag mismatch or invalid key"))?;

    // Decompress: try raw deflate, then zlib, then raw UTF-8
    let mut decompressed = Vec::new();
    let mut raw_deflate = DeflateDecoder::new(&decrypted_compressed[..]);
    if raw_deflate.read_to_end(&mut decompressed).is_err() {
        decompressed.clear();
        let mut zlib_dec = ZlibDecoder::new(&decrypted_compressed[..]);
        if zlib_dec.read_to_end(&mut decompressed).is_err() {
            decompressed = decrypted_compressed;
        }
    }

    let text = String::from_utf8_lossy(&decompressed).to_string();
    if let Ok(container) = serde_json::from_str::<Value>(&text) {
        if let Some(paste) = container.get("paste").and_then(|v| v.as_str()) {
            return Ok(paste.to_string());
        }
        if let Some(first) = container.as_array().and_then(|a| a.first()).and_then(|v| v.as_str()) {
            return Ok(first.to_string());
        }
    }
    Ok(text)
}

pub fn extract_links_from_text(text: &str) -> Vec<String> {
    let unescaped = html_escape::decode_html_entities(text).replace(r"\/", "/");
    let re = Regex::new(r#"https?://[^\s'"<>]+"#).unwrap();
    let mut cleaned = Vec::new();

    for m in re.find_iter(&unescaped) {
        let mut link = m
            .as_str()
            .trim_end_matches(&['.', ',', ';', '>', '*', '_', '`', '~', ']'][..])
            .to_string();
        while link.ends_with(')') && link.matches(')').count() > link.matches('(').count() {
            link.pop();
        }
        if !link.is_empty() && !cleaned.contains(&link) {
            cleaned.push(link);
        }
    }
    cleaned
}

#[cfg(test)]
mod tests {
    use super::*;
    use aes_gcm::aead::Aead;
    use flate2::{write::DeflateEncoder, Compression};
    use std::io::Write;

    #[test]
    fn test_privatebin_url_and_decryption_roundtrip() {
        let key_bytes = [7u8; 32];
        let key_b58 = bs58::encode(&key_bytes).into_string();
        let url = format!("https://paste.fitgirl-repacks.site/?99e460e2438a1459#{key_b58}");
        assert!(is_privatebin_url(&url));

        let iv = [3u8; 12];
        let salt = [9u8; 8];
        let adata = serde_json::json!([
            [B64.encode(iv), B64.encode(salt), 1000, 256, 128, "aes", "gcm", "zlib"],
            "plaintext",
            0,
            0
        ]);
        let aad = serde_json::to_string(&adata).unwrap().into_bytes();

        let mut derived = [0u8; 32];
        pbkdf2_hmac::<Sha256>(&key_bytes, &salt, 1000, &mut derived);

        let payload_json = r#"{"paste":"https://fuckingfast.co/abc#part01.rar\nhttps://fuckingfast.co/def#part02.rar"}"#;
        let mut enc = DeflateEncoder::new(Vec::new(), Compression::default());
        enc.write_all(payload_json.as_bytes()).unwrap();
        let compressed = enc.finish().unwrap();

        let cipher = Aes256Gcm::new_from_slice(&derived).unwrap();
        let ct = cipher
            .encrypt(
                Nonce::from_slice(&iv),
                Payload {
                    msg: &compressed,
                    aad: &aad,
                },
            )
            .unwrap();

        let paste_data = serde_json::json!({
            "status": 0,
            "adata": adata,
            "ct": B64.encode(ct),
        });

        let decrypted = decrypt_privatebin_payload(&paste_data, &key_b58).unwrap();
        let links = extract_links_from_text(&decrypted);
        assert_eq!(
            links,
            vec![
                "https://fuckingfast.co/abc#part01.rar",
                "https://fuckingfast.co/def#part02.rar"
            ]
        );
    }
}
