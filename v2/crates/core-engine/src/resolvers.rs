use crate::privatebin::{
    decrypt_privatebin_payload, extract_links_from_text, is_privatebin_url, parse_privatebin_url,
};
use crate::utils::{sanitize_filename, urlencoding_decode};
use anyhow::{anyhow, Result};
use futures_util::future::join_all;
use parking_lot::Mutex;
use regex::Regex;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::net::{IpAddr, Ipv4Addr};
use std::time::{Duration, Instant};
use tokio::sync::Semaphore;
use url::Url;
use wreq::redirect::Policy;
use wreq::Client;
use wreq_util::{Emulation, Platform, Profile};

#[derive(Debug, Clone)]
pub struct ResolvedLink {
    pub direct_url: String,
    pub filename: String,
    pub file_size: u64,
}

pub struct ClientPool {
    clients: Mutex<HashMap<(Option<String>, Profile, bool), Client>>,
}

impl Default for ClientPool {
    fn default() -> Self {
        Self::new()
    }
}

impl ClientPool {
    pub fn new() -> Self {
        Self {
            clients: Mutex::new(HashMap::new()),
        }
    }

    pub fn get_client(
        &self,
        interface_ip: Option<&str>,
        profile: Profile,
        follow_redirects: bool,
    ) -> Result<Client> {
        let key = (interface_ip.map(|s| s.to_string()), profile, follow_redirects);
        {
            let guard = self.clients.lock();
            if let Some(c) = guard.get(&key) {
                return Ok(c.clone());
            }
        }

        let platform = match profile {
            Profile::Safari17_0 => Platform::MacOS,
            _ => Platform::Windows,
        };
        let emu = Emulation::builder()
            .profile(profile)
            .platform(platform)
            .build();
        let mut builder = Client::builder()
            .emulation(emu)
            .cookie_store(true)
            .connect_timeout(Duration::from_secs(8));

        if !follow_redirects {
            builder = builder.redirect(Policy::none());
        }

        if let Some(ip_str) = interface_ip {
            if let Ok(v4) = ip_str.parse::<Ipv4Addr>() {
                builder = builder.local_address(IpAddr::V4(v4));
            }
        }

        let client = builder.build()?;
        self.clients.lock().insert(key, client.clone());
        Ok(client)
    }

    pub fn evict(&self, interface_ip: Option<&str>, profile: Profile) {
        let mut guard = self.clients.lock();
        let ip_opt = interface_ip.map(|s| s.to_string());
        guard.remove(&(ip_opt.clone(), profile, true));
        guard.remove(&(ip_opt, profile, false));
    }
}

pub struct LinkManager {
    pub pool: ClientPool,
    rd_token: Mutex<Option<String>>,
    cache: Mutex<HashMap<(String, Option<String>), (Instant, ResolvedLink)>>,
    pre_resolving: Mutex<HashSet<(String, Option<String>)>>,
    domain_sems: tokio::sync::Mutex<HashMap<(String, Option<String>), std::sync::Arc<Semaphore>>>,
}

impl LinkManager {
    const CACHE_TTL: Duration = Duration::from_secs(900);

    pub fn new(rd_token: Option<String>) -> Self {
        let clean = rd_token
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty());
        Self {
            pool: ClientPool::new(),
            rd_token: Mutex::new(clean),
            cache: Mutex::new(HashMap::new()),
            pre_resolving: Mutex::new(HashSet::new()),
            domain_sems: tokio::sync::Mutex::new(HashMap::new()),
        }
    }

    pub fn set_rd_token(&self, token: &str) {
        let clean = token.trim().to_string();
        *self.rd_token.lock() = if clean.is_empty() { None } else { Some(clean) };
    }

    pub fn rd_configured(&self) -> bool {
        self.rd_token.lock().is_some()
    }

    pub fn invalidate_cache(&self, url: &str) {
        let mut guard = self.cache.lock();
        guard.retain(|(u, _), _| u != url);
    }

    fn get_cached(&self, url: &str, interface: Option<&str>) -> Option<ResolvedLink> {
        let mut guard = self.cache.lock();
        let key = (url.to_string(), interface.map(|s| s.to_string()));
        if let Some((ts, val)) = guard.get(&key) {
            if ts.elapsed() <= Self::CACHE_TTL {
                return Some(val.clone());
            }
            guard.remove(&key);
        }
        for ((u, _), (ts, val)) in guard.iter() {
            if u == url && ts.elapsed() <= Self::CACHE_TTL {
                return Some(val.clone());
            }
        }
        None
    }

    pub async fn ingest_input(&self, raw_input: &str) -> Vec<String> {
        let trimmed = raw_input.trim();
        if trimmed.is_empty() {
            return Vec::new();
        }
        let tokens = extract_links_from_text(trimmed);
        let mut pb_urls = Vec::new();
        let mut direct_urls = Vec::new();
        for t in tokens {
            if is_privatebin_url(&t) {
                pb_urls.push(t);
            } else {
                direct_urls.push(t);
            }
        }

        if pb_urls.is_empty() {
            return direct_urls;
        }

        let client = match self.pool.get_client(None, Profile::Chrome131, true) {
            Ok(c) => c,
            Err(_) => return direct_urls,
        };

        let futs = pb_urls
            .into_iter()
            .map(|u| Self::fetch_privatebin_links(client.clone(), u));
        let results = join_all(futs).await;

        let mut collected = direct_urls;
        for links in results {
            for l in links {
                if !collected.contains(&l) {
                    collected.push(l);
                }
            }
        }
        collected
    }

    async fn fetch_privatebin_links(client: Client, pb_url: String) -> Vec<String> {
        let Ok((base_url, paste_id, key)) = parse_privatebin_url(&pb_url) else {
            return Vec::new();
        };
        let endpoint = format!("{base_url}?{paste_id}");
        let Ok(resp) = client
            .get(&endpoint)
            .header("X-Requested-With", "JSONHttpRequest")
            .send()
            .await
        else {
            return Vec::new();
        };
        if !resp.status().is_success() {
            return Vec::new();
        }
        let Ok(payload) = resp.json::<Value>().await else {
            return Vec::new();
        };
        match decrypt_privatebin_payload(&payload, &key) {
            Ok(text) => extract_links_from_text(&text),
            Err(_) => Vec::new(),
        }
    }

    async fn domain_semaphore(&self, netloc: &str, interface: Option<&str>) -> std::sync::Arc<Semaphore> {
        let mut guard = self.domain_sems.lock().await;
        let key = (netloc.to_string(), interface.map(|s| s.to_string()));
        guard
            .entry(key)
            .or_insert_with(|| std::sync::Arc::new(Semaphore::new(3)))
            .clone()
    }

    pub async fn resolve_link(
        &self,
        url: &str,
        interface: Option<&str>,
        use_cache: bool,
    ) -> Result<ResolvedLink> {
        if use_cache {
            if let Some(cached) = self.get_cached(url, interface) {
                return Ok(cached);
            }
        }

        let parsed = Url::parse(url)?;
        let netloc = parsed.host_str().unwrap_or("").to_ascii_lowercase();
        let sem = self.domain_semaphore(&netloc, interface).await;
        let _permit = sem.acquire().await?;

        let res = if self.rd_configured()
            && (netloc.contains("1fichier.com") || netloc.contains("rapidgator.net"))
        {
            match self.resolve_real_debrid(url, interface).await {
                Ok(r) => r,
                Err(_) => self.resolve_generic(url, interface).await?,
            }
        } else if netloc.contains("fuckingfast.co") {
            self.resolve_fuckingfast(url, interface).await?
        } else if netloc.contains("datanodes.to") {
            self.resolve_datanodes(url, interface).await?
        } else {
            self.resolve_generic(url, interface).await?
        };

        self.cache.lock().insert(
            (url.to_string(), interface.map(|s| s.to_string())),
            (Instant::now(), res.clone()),
        );
        Ok(res)
    }

    pub fn try_mark_preresolving(&self, url: &str, interface: Option<&str>) -> bool {
        if self.get_cached(url, interface).is_some() {
            return false;
        }
        let mut guard = self.pre_resolving.lock();
        guard.insert((url.to_string(), interface.map(|s| s.to_string())))
    }

    pub fn unmark_preresolving(&self, url: &str, interface: Option<&str>) {
        self.pre_resolving
            .lock()
            .remove(&(url.to_string(), interface.map(|s| s.to_string())));
    }

    async fn resolve_real_debrid(&self, link: &str, interface: Option<&str>) -> Result<ResolvedLink> {
        let token = self
            .rd_token
            .lock()
            .clone()
            .ok_or_else(|| anyhow!("Real-Debrid token not configured"))?;
        let client = self.pool.get_client(interface, Profile::Chrome131, true)?;

        for attempt in 0..3 {
            let resp = client
                .post("https://api.real-debrid.com/rest/1.0/unrestrict/link")
                .header("Authorization", format!("Bearer {token}"))
                .header("User-Agent", "PartHarvester/2.0")
                .form(&[("link", link)])
                .send()
                .await?;

            let code = resp.status().as_u16();
            if code == 401 {
                return Err(anyhow!("Real-Debrid: Invalid API Token."));
            }
            if code == 403 {
                return Err(anyhow!("Real-Debrid: Account expired or quota exceeded."));
            }
            if matches!(code, 429 | 502 | 503 | 504) && attempt < 2 {
                tokio::time::sleep(Duration::from_secs(attempt + 1)).await;
                continue;
            }
            if code != 200 {
                let txt = resp.text().await.unwrap_or_default();
                return Err(anyhow!("Real-Debrid error ({code}): {}", &txt[..txt.len().min(200)]));
            }

            let payload: Value = resp.json().await?;
            let download_url = payload
                .get("download")
                .and_then(|v| v.as_str())
                .ok_or_else(|| anyhow!("Real-Debrid returned no download URL"))?
                .to_string();
            let filename = payload
                .get("filename")
                .and_then(|v| v.as_str())
                .map(sanitize_filename)
                .unwrap_or_else(|| "download.bin".to_string());
            let file_size = payload
                .get("filesize")
                .and_then(|v| v.as_u64())
                .unwrap_or(0);
            return Ok(ResolvedLink {
                direct_url: download_url,
                filename,
                file_size,
            });
        }
        Err(anyhow!("Real-Debrid rate limit exceeded"))
    }

    async fn resolve_fuckingfast(&self, url: &str, interface: Option<&str>) -> Result<ResolvedLink> {
        let parsed = Url::parse(url)?;
        let expected_filename = parsed
            .fragment()
            .filter(|f| !f.trim().is_empty())
            .map(|f| sanitize_filename(&urlencoding_decode(f)));
        let clean_url = format!(
            "{}://{}{}",
            parsed.scheme(),
            parsed.host_str().unwrap_or(""),
            parsed.path()
        );
        let segs: Vec<&str> = parsed.path().split('/').filter(|s| !s.is_empty()).collect();
        if segs.is_empty() {
            return Err(anyhow!("Invalid FuckingFast URL: {url}"));
        }
        let file_id = if segs[0] == "f" && segs.len() > 1 {
            segs[1]
        } else {
            segs[0]
        };

        let profiles = [Profile::Safari17_0, Profile::Chrome131, Profile::Chrome124];
        let mut last_err: Option<anyhow::Error> = None;

        for (attempt, &profile) in profiles.iter().enumerate() {
            let client = self.pool.get_client(interface, profile, false)?;
            match tokio::time::timeout(
                Duration::from_secs(10),
                client
                    .get(&clean_url)
                    .header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
                    .header("Referer", "https://paste.fitgirl-repacks.site/")
                    .send(),
            )
            .await
            {
                Ok(Ok(resp)) => {
                    let code = resp.status().as_u16();
                    if code == 404 {
                        return Err(anyhow!("Dead link (HTTP 404 Not Found on FuckingFast)."));
                    }
                    if matches!(code, 403 | 429) {
                        drop(resp);
                        self.pool.evict(interface, profile);
                        last_err = Some(anyhow!("HTTP {code} on {profile:?}"));
                        tokio::time::sleep(Duration::from_millis(400 * (attempt as u64 + 1))).await;
                        continue;
                    }
                    let text = resp.text().await.unwrap_or_default();
                    if text.get(..500).unwrap_or(&text).contains("Not Found") {
                        return Err(anyhow!("Dead link (HTTP 404 Not Found on FuckingFast)."));
                    }
                    let head = text.get(..2500).unwrap_or(&text);
                    if head.contains("cf-browser-verification")
                        || head.contains("challenge-platform")
                        || head.contains("<title>Just a moment...</title>")
                    {
                        self.pool.evict(interface, profile);
                        last_err = Some(anyhow!("Cloudflare challenge on {profile:?}"));
                        tokio::time::sleep(Duration::from_millis(400 * (attempt as u64 + 1))).await;
                        continue;
                    }
                    if code != 200 {
                        self.pool.evict(interface, profile);
                        last_err = Some(anyhow!("HTTP {code}"));
                        tokio::time::sleep(Duration::from_millis(400 * (attempt as u64 + 1))).await;
                        continue;
                    }

                    let hx_re = Regex::new(r#"hx-post="([^"]+)""#).unwrap();
                    let hx_path = hx_re
                        .captures(&text)
                        .and_then(|c| c.get(1))
                        .map(|m| m.as_str().to_string())
                        .unwrap_or_else(|| format!("/f/{file_id}/go"));
                    let go_url = Url::parse(&clean_url)?.join(&hx_path)?.to_string();

                    let post_resp = client
                        .post(&go_url)
                        .header("HX-Request", "true")
                        .header("HX-Current-URL", &clean_url)
                        .header("Referer", &clean_url)
                        .header("Origin", "https://fuckingfast.co")
                        .send()
                        .await?;

                    let hx_redirect = post_resp
                        .headers()
                        .get("hx-redirect")
                        .and_then(|v| v.to_str().ok())
                        .map(|s| s.to_string());
                    let post_text = post_resp.text().await.unwrap_or_default();

                    let direct_url = hx_redirect.or_else(|| {
                        let dl_re = Regex::new(r#"https?://dl\.fuckingfast\.co/dl/[^\s'"<>]+"#).unwrap();
                        dl_re.find(&post_text).map(|m| m.as_str().to_string())
                    });

                    let Some(direct_url) = direct_url else {
                        last_err = Some(anyhow!("Could not extract dl.fuckingfast.co link"));
                        continue;
                    };

                    if let Some(fname) = expected_filename {
                        return Ok(ResolvedLink {
                            direct_url,
                            filename: fname,
                            file_size: 0,
                        });
                    }

                    return Ok(ResolvedLink {
                        direct_url,
                        filename: sanitize_filename(&format!("{file_id}.rar")),
                        file_size: 0,
                    });
                }
                Ok(Err(e)) => {
                    self.pool.evict(interface, profile);
                    last_err = Some(e.into());
                    tokio::time::sleep(Duration::from_millis(400)).await;
                }
                Err(_) => {
                    self.pool.evict(interface, profile);
                    last_err = Some(anyhow!("Timed out on {profile:?}"));
                }
            }
        }
        Err(last_err.unwrap_or_else(|| anyhow!("FuckingFast resolution failed")))
    }

    async fn resolve_datanodes(&self, url: &str, interface: Option<&str>) -> Result<ResolvedLink> {
        if self.rd_configured() {
            if let Ok(r) = self.resolve_real_debrid(url, interface).await {
                return Ok(r);
            }
        }
        let parsed = Url::parse(url)?;
        let parts: Vec<&str> = parsed.path().split('/').filter(|s| !s.is_empty()).collect();
        let file_id = parts.first().copied().unwrap_or("");
        let expected_filename = parts
            .get(1)
            .map(|s| sanitize_filename(&urlencoding_decode(s)))
            .unwrap_or_default();

        let client = self.pool.get_client(interface, Profile::Chrome131, false)?;
        let resp = client
            .get(url)
            .header("Referer", "https://paste.fitgirl-repacks.site/")
            .send()
            .await?;

        let code = resp.status().as_u16();
        if code == 404 {
            return Err(anyhow!("Dead link (HTTP 404): File removed from DataNodes."));
        }
        if code != 200 {
            return Err(anyhow!("DataNodes returned HTTP {code}"));
        }
        let html = resp.text().await.unwrap_or_default();
        if html.contains("File Not Found") || html.to_lowercase().contains("file was deleted") {
            return Err(anyhow!("Dead link: File deleted on DataNodes."));
        }

        let input_re = Regex::new(r#"(?i)<input[^>]*type=["']hidden["'][^>]*>"#).unwrap();
        let name_re = Regex::new(r#"(?i)name=["']([^"']+)["']"#).unwrap();
        let val_re = Regex::new(r#"(?i)value=["']([^"']*)["']"#).unwrap();
        let mut form: HashMap<String, String> = HashMap::new();
        for tag in input_re.find_iter(&html) {
            if let Some(nm) = name_re.captures(tag.as_str()).and_then(|c| c.get(1)) {
                let val = val_re
                    .captures(tag.as_str())
                    .and_then(|c| c.get(1))
                    .map(|m| m.as_str())
                    .unwrap_or("");
                form.insert(nm.as_str().to_string(), val.to_string());
            }
        }
        form.entry("op".to_string()).or_insert_with(|| "download2".to_string());
        if !file_id.is_empty() {
            form.entry("id".to_string()).or_insert_with(|| file_id.to_string());
        }
        form.entry("method_free".to_string())
            .or_insert_with(|| "Free Download >>".to_string());

        let post_resp = client
            .post(url)
            .header("Referer", url)
            .form(&form)
            .send()
            .await?;

        let pcode = post_resp.status().as_u16();
        if matches!(pcode, 301 | 302 | 303 | 307 | 308) {
            if let Some(loc) = post_resp.headers().get("location").and_then(|v| v.to_str().ok()) {
                let direct_url = parsed.join(loc.trim())?.to_string();
                let fname = if !expected_filename.is_empty() {
                    expected_filename
                } else {
                    Url::parse(&direct_url)
                        .ok()
                        .and_then(|u| u.path_segments()?.next_back().map(sanitize_filename))
                        .unwrap_or_else(|| "part.rar".to_string())
                };
                return Ok(ResolvedLink {
                    direct_url,
                    filename: fname,
                    file_size: 0,
                });
            }
        }

        let post_html = post_resp.text().await.unwrap_or_default();
        let direct_re = Regex::new(r#"https?://[a-zA-Z0-9.\-]+/d/[a-zA-Z0-9_\-]+/[^\s'"<>]+"#).unwrap();
        if let Some(m) = direct_re.find(&post_html) {
            let direct_url = m.as_str().to_string();
            let fname = if !expected_filename.is_empty() {
                expected_filename
            } else {
                sanitize_filename(direct_url.split('/').next_back().unwrap_or("part.rar"))
            };
            return Ok(ResolvedLink {
                direct_url,
                filename: fname,
                file_size: 0,
            });
        }
        Err(anyhow!("DataNodes direct link not found in POST response."))
    }

    async fn resolve_generic(&self, url: &str, interface: Option<&str>) -> Result<ResolvedLink> {
        let parsed = Url::parse(url)?;
        let netloc = parsed.host_str().unwrap_or("").to_ascii_lowercase();

        if netloc.contains("pixeldrain.com") {
            let parts: Vec<&str> = parsed.path().split('/').filter(|s| !s.is_empty()).collect();
            if let Some(&file_id) = parts.last() {
                if file_id != "file" {
                    let direct_url = format!("https://pixeldrain.com/api/file/{file_id}");
                    let mut filename = format!("pixeldrain_{file_id}");
                    let mut file_size = 0u64;
                    if let Ok(client) = self.pool.get_client(interface, Profile::Chrome131, true) {
                        if let Ok(resp) = client
                            .get(format!("https://pixeldrain.com/api/file/{file_id}/info"))
                            .send()
                            .await
                        {
                            if resp.status().as_u16() == 404 {
                                return Err(anyhow!("Dead link (HTTP 404): PixelDrain file not found."));
                            }
                            if resp.status().is_success() {
                                if let Ok(info) = resp.json::<Value>().await {
                                    if let Some(n) = info.get("name").and_then(|v| v.as_str()) {
                                        filename = sanitize_filename(n);
                                    }
                                    if let Some(sz) = info.get("size").and_then(|v| v.as_u64()) {
                                        file_size = sz;
                                    }
                                }
                            }
                        }
                    }
                    return Ok(ResolvedLink {
                        direct_url,
                        filename,
                        file_size,
                    });
                }
            }
        }

        if netloc.contains("mediafire.com") {
            let client = self.pool.get_client(interface, Profile::Chrome131, false)?;
            let resp = client.get(url).send().await?;
            let code = resp.status().as_u16();
            if matches!(code, 301 | 302 | 303 | 307 | 308) {
                if let Some(loc) = resp.headers().get("location").and_then(|v| v.to_str().ok()) {
                    if loc.contains("download") && loc.contains("mediafire.com") {
                        let fname = Url::parse(loc)
                            .ok()
                            .and_then(|u| u.path_segments()?.next_back().map(sanitize_filename))
                            .unwrap_or_else(|| "mediafire.bin".to_string());
                        return Ok(ResolvedLink {
                            direct_url: loc.to_string(),
                            filename: fname,
                            file_size: 0,
                        });
                    }
                }
            }
            if code == 404 {
                return Err(anyhow!("Dead link (HTTP 404): MediaFire file removed."));
            }
            let html = resp.text().await.unwrap_or_default();
            let btn_re = Regex::new(r#"(?i)<a\b[^>]*\bhref=["'](https?://download\d*\.mediafire\.com/[^"']+)["'][^>]*>"#).unwrap();
            if let Some(cap) = btn_re.captures(&html) {
                let direct_url = cap[1].to_string();
                let fname = Url::parse(&direct_url)
                    .ok()
                    .and_then(|u| u.path_segments()?.next_back().map(sanitize_filename))
                    .unwrap_or_else(|| "mediafire.bin".to_string());
                return Ok(ResolvedLink {
                    direct_url,
                    filename: fname,
                    file_size: 0,
                });
            }
        }

        let fname = parsed
            .fragment()
            .filter(|f| !f.is_empty())
            .map(|f| sanitize_filename(&urlencoding_decode(f)))
            .or_else(|| {
                parsed
                    .path_segments()?
                    .rfind(|s| !s.is_empty())
                    .map(|s| sanitize_filename(&urlencoding_decode(s)))
            })
            .unwrap_or_else(|| "download.bin".to_string());

        Ok(ResolvedLink {
            direct_url: url.to_string(),
            filename: fname,
            file_size: 0,
        })
    }
}
