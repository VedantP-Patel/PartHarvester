use std::path::Path;
use url::Url;

/// Sanitizes an untrusted filename from URLs, fragments, or Content-Disposition headers.
/// Prevents path traversal (`..`) and strips Windows/POSIX illegal characters.
pub fn sanitize_filename(raw: &str) -> String {
    let base = Path::new(raw)
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or(raw);

    let mut cleaned: String = base
        .chars()
        .filter(|&c| !c.is_control())
        .map(|c| match c {
            '<' | '>' | ':' | '"' | '/' | '\\' | '|' | '?' | '*' => '_',
            _ => c,
        })
        .collect();

    cleaned = cleaned.trim_matches(|c| c == ' ' || c == '.').to_string();
    while cleaned.contains("..") {
        cleaned = cleaned.replace("..", "_");
    }

    if cleaned.is_empty() {
        "download.bin".to_string()
    } else {
        cleaned
    }
}

/// Extracts a candidate filename from a URL's fragment or last path segment.
pub fn extract_candidate_filename(raw_url: &str) -> String {
    if let Ok(parsed) = Url::parse(raw_url) {
        if let Some(frag) = parsed.fragment() {
            if !frag.trim().is_empty() {
                let decoded = urlencoding_decode(frag);
                return sanitize_filename(&decoded);
            }
        }
        if let Some(mut segs) = parsed.path_segments() {
            if let Some(last) = segs.rfind(|s| !s.is_empty()) {
                let decoded = urlencoding_decode(last);
                return sanitize_filename(&decoded);
            }
        }
    }
    "download.bin".to_string()
}

/// Derives an appropriate HTTP Referer from a URL's origin rather than hardcoding a single hoster.
pub fn origin_referer(raw_url: &str) -> Option<String> {
    Url::parse(raw_url).ok().and_then(|u| {
        u.host_str().map(|host| {
            if let Some(port) = u.port() {
                format!("{}://{}:{}/", u.scheme(), host, port)
            } else {
                format!("{}://{}/", u.scheme(), host)
            }
        })
    })
}

pub fn urlencoding_decode(input: &str) -> String {
    let mut out = Vec::with_capacity(input.len());
    let bytes = input.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Some(h), Some(l)) = (
                (bytes[i + 1] as char).to_digit(16),
                (bytes[i + 2] as char).to_digit(16),
            ) {
                out.push(((h << 4) | l) as u8);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).to_string()
}

/// Natural sort key comparing numeric chunks by integer value and text case-insensitively.
pub fn natural_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    let mut ia = 0usize;
    let mut ib = 0usize;
    let ba = a.as_bytes();
    let bb = b.as_bytes();

    while ia < ba.len() && ib < bb.len() {
        let ca = ba[ia];
        let cb = bb[ib];
        if ca.is_ascii_digit() && cb.is_ascii_digit() {
            let start_a = ia;
            while ia < ba.len() && ba[ia].is_ascii_digit() {
                ia += 1;
            }
            let start_b = ib;
            while ib < bb.len() && bb[ib].is_ascii_digit() {
                ib += 1;
            }
            let na: u128 = std::str::from_utf8(&ba[start_a..ia])
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            let nb: u128 = std::str::from_utf8(&bb[start_b..ib])
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            match na.cmp(&nb) {
                std::cmp::Ordering::Equal => {}
                ord => return ord,
            }
        } else {
            let la = ca.to_ascii_lowercase();
            let lb = cb.to_ascii_lowercase();
            match la.cmp(&lb) {
                std::cmp::Ordering::Equal => {
                    ia += 1;
                    ib += 1;
                }
                ord => return ord,
            }
        }
    }
    ba.len().cmp(&bb.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sanitize_filename_blocks_traversal_and_illegal_chars() {
        assert_eq!(sanitize_filename("../../etc/passwd"), "passwd");
        assert_eq!(sanitize_filename("game:part<01>?.rar"), "game_part_01__.rar");
        assert_eq!(sanitize_filename("..."), "download.bin");
        assert_eq!(
            sanitize_filename("Phantom_Blade_Zero_part01.rar"),
            "Phantom_Blade_Zero_part01.rar"
        );
    }

    #[test]
    fn test_natural_cmp() {
        let mut names = vec!["part10.rar", "part2.rar", "part01.rar"];
        names.sort_by(|a, b| natural_cmp(a, b));
        assert_eq!(names, vec!["part01.rar", "part2.rar", "part10.rar"]);
    }

    #[test]
    fn test_origin_referer() {
        assert_eq!(
            origin_referer("https://datanodes.to/abc/file.rar"),
            Some("https://datanodes.to/".to_string())
        );
        assert_eq!(
            origin_referer("https://fuckingfast.co/xyz#part01.rar"),
            Some("https://fuckingfast.co/".to_string())
        );
    }
}
