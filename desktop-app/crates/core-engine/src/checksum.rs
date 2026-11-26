use anyhow::Result;
use crc32fast::Hasher as Crc32Hasher;
use md5::{Digest as Md5Digest, Md5};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::Path;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChecksumResult {
    pub filename: String,
    pub expected: String,
    pub actual: String,
    pub ok: bool,
}

pub fn verify_sfv_file(sfv_path: &Path) -> Result<Vec<ChecksumResult>> {
    let dir = sfv_path.parent().unwrap_or_else(|| Path::new("."));
    let content = std::fs::read_to_string(sfv_path)?;
    let mut results = Vec::new();

    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with(';') || trimmed.starts_with('#') {
            continue;
        }
        let mut parts = trimmed.rsplitn(2, char::is_whitespace);
        let Some(expected_crc) = parts.next() else { continue };
        let Some(fname) = parts.next() else { continue };
        let fname = fname.trim();
        let expected = expected_crc.trim().to_ascii_lowercase();
        let target = dir.join(fname);
        if !target.exists() {
            results.push(ChecksumResult {
                filename: fname.to_string(),
                expected,
                actual: "MISSING".to_string(),
                ok: false,
            });
            continue;
        }
        let actual = compute_crc32(&target)?;
        results.push(ChecksumResult {
            filename: fname.to_string(),
            ok: actual == expected,
            expected,
            actual,
        });
    }
    Ok(results)
}

pub fn compute_crc32(path: &Path) -> Result<String> {
    let file = File::open(path)?;
    let mut reader = BufReader::with_capacity(256 * 1024, file);
    let mut hasher = Crc32Hasher::new();
    let mut buf = vec![0u8; 256 * 1024];
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(format!("{:08x}", hasher.finalize()))
}

pub fn compute_sha256(path: &Path) -> Result<String> {
    let file = File::open(path)?;
    let mut reader = BufReader::with_capacity(256 * 1024, file);
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 256 * 1024];
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

pub fn compute_md5(path: &Path) -> Result<String> {
    let file = File::open(path)?;
    let mut reader = BufReader::with_capacity(256 * 1024, file);
    let mut hasher = Md5::new();
    let mut buf = vec![0u8; 256 * 1024];
    loop {
        let n = reader.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_sfv_verification() {
        let dir = tempdir().unwrap();
        let data_file = dir.path().join("part01.rar");
        std::fs::write(&data_file, b"PartHarvesterTestPayload").unwrap();
        let crc = compute_crc32(&data_file).unwrap();

        let sfv = dir.path().join("release.sfv");
        std::fs::write(&sfv, format!("; comment\npart01.rar {crc}\n")).unwrap();

        let res = verify_sfv_file(&sfv).unwrap();
        assert_eq!(res.len(), 1);
        assert!(res[0].ok);
    }
}
