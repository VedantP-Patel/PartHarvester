use crate::models::NetworkAdapter;
use parking_lot::Mutex;
use std::collections::HashMap;
use std::time::{Duration, Instant};

#[derive(Debug, Clone)]
pub struct AutoTuneRecommendation {
    pub max_concurrency: u32,
    pub segments_per_file: u32,
    pub buffer_size_kb: u32,
    pub per_adapter_max_conns: HashMap<String, u32>,
    pub total_socket_budget: u32,
    pub profile_label: String,
}

struct TunerState {
    penalty_by_ip: HashMap<String, u32>,
    last_error_ts: HashMap<String, Instant>,
    clean_completions: HashMap<String, u32>,
    cooldown_until: HashMap<String, Instant>,
    last_rec: Option<AutoTuneRecommendation>,
}

pub struct SmartAutoTuner {
    pub enabled: bool,
    state: Mutex<TunerState>,
}

impl SmartAutoTuner {
    const BURST_MAX_SOCKETS_PER_IP: u32 = 8;
    const MIN_SOCKETS_PER_IP: u32 = 1;

    pub fn new(enabled: bool) -> Self {
        Self {
            enabled,
            state: Mutex::new(TunerState {
                penalty_by_ip: HashMap::new(),
                last_error_ts: HashMap::new(),
                clean_completions: HashMap::new(),
                cooldown_until: HashMap::new(),
                last_rec: None,
            }),
        }
    }

    pub fn register_cooldown(&self, ip: Option<&str>, seconds: f64) {
        let Some(ip) = ip else { return };
        if seconds <= 0.0 {
            return;
        }
        let now = Instant::now();
        let until = now + Duration::from_secs_f64(seconds.min(120.0));
        let mut st = self.state.lock();
        let prev = st.cooldown_until.get(ip).copied().unwrap_or(now);
        st.cooldown_until.insert(ip.to_string(), prev.max(until));
        st.penalty_by_ip.insert(ip.to_string(), 5);
        st.last_error_ts.insert(ip.to_string(), now);
    }

    pub fn get_cooldown_remaining(&self, ip: Option<&str>) -> f64 {
        let Some(ip) = ip else { return 0.0 };
        let st = self.state.lock();
        let Some(&until) = st.cooldown_until.get(ip) else {
            return 0.0;
        };
        let now = Instant::now();
        if until > now {
            (until - now).as_secs_f64()
        } else {
            0.0
        }
    }

    pub fn record_stream_error(&self, ip: Option<&str>, msg: &str) {
        let Some(ip) = ip else { return };
        let lower = msg.to_lowercase();
        let pressure = ["56", "reset", "truncated", "429", "503", "timeout", "stalled", "closed", "http 200"]
            .iter()
            .any(|sig| lower.contains(sig));
        let step = if pressure { 2 } else { 1 };
        let mut st = self.state.lock();
        let cur = *st.penalty_by_ip.get(ip).unwrap_or(&0);
        st.penalty_by_ip.insert(ip.to_string(), (cur + step).min(5));
        st.last_error_ts.insert(ip.to_string(), Instant::now());
        st.clean_completions.insert(ip.to_string(), 0);
    }

    pub fn record_stream_success(&self, ip: Option<&str>) {
        let Some(ip) = ip else { return };
        let mut st = self.state.lock();
        let should_dec = {
            let cnt = st.clean_completions.entry(ip.to_string()).or_insert(0);
            *cnt += 1;
            *cnt >= 3
        };
        if should_dec {
            let mut reset = false;
            if let Some(p) = st.penalty_by_ip.get_mut(ip) {
                if *p > 0 {
                    *p -= 1;
                    reset = true;
                }
            }
            if reset {
                st.clean_completions.insert(ip.to_string(), 0);
            }
        }
    }

    fn adapter_speed_mbps(ad: &NetworkAdapter) -> f64 {
        let best = if ad.ewma_speed_bytes > 65536.0 {
            ad.ewma_speed_bytes
        } else {
            ad.ewma_speed_bytes.max(ad.current_speed_bytes)
        };
        best / (1024.0 * 1024.0)
    }

    pub fn evaluate(&self, adapters: &[NetworkAdapter]) -> AutoTuneRecommendation {
        let active: Vec<&NetworkAdapter> = adapters
            .iter()
            .filter(|a| a.is_enabled && a.is_up)
            .collect();

        let mut st = self.state.lock();
        if active.is_empty() {
            let rec = AutoTuneRecommendation {
                max_concurrency: 1,
                segments_per_file: 1,
                buffer_size_kb: 256,
                per_adapter_max_conns: HashMap::new(),
                total_socket_budget: 1,
                profile_label: "Standby (No Active Adapter)".to_string(),
            };
            st.last_rec = Some(rec.clone());
            return rec;
        }

        let now = Instant::now();
        let mut caps = HashMap::new();
        let mut total_sockets = 0u32;
        let mut total_speed = 0.0f64;

        for ad in &active {
            let ip = &ad.ip_address;
            let cooling = st
                .cooldown_until
                .get(ip)
                .map(|&u| u > now)
                .unwrap_or(false);
            let cap = if cooling {
                Self::MIN_SOCKETS_PER_IP
            } else {
                let speed = Self::adapter_speed_mbps(ad);
                let target = if speed <= 0.1 {
                    if active.len() == 1 { 4 } else { 3 }
                } else if speed < 2.0 {
                    2
                } else if speed < 8.0 {
                    if active.len() == 1 { 4 } else { 3 }
                } else if speed < 20.0 {
                    if active.len() == 1 { 6 } else { 4 }
                } else if speed < 45.0 {
                    if active.len() == 1 { 7 } else { 5 }
                } else {
                    Self::BURST_MAX_SOCKETS_PER_IP
                };

                let mut penalty = *st.penalty_by_ip.get(ip).unwrap_or(&0);
                if penalty > 0 {
                    if let Some(&last_err) = st.last_error_ts.get(ip) {
                        if now.duration_since(last_err).as_secs_f64() > 20.0 {
                            penalty -= 1;
                            st.penalty_by_ip.insert(ip.clone(), penalty);
                            st.last_error_ts.insert(ip.clone(), now);
                        }
                    }
                }
                target.saturating_sub(penalty).max(Self::MIN_SOCKETS_PER_IP)
            };

            caps.insert(ip.clone(), cap);
            total_sockets += cap;
            total_speed += Self::adapter_speed_mbps(ad);
        }

        let (concurrency, segments) = if total_sockets <= 2 {
            (1, total_sockets.max(1))
        } else if total_sockets <= 4 {
            (2, 2)
        } else if total_sockets <= 6 {
            (2, 3)
        } else if total_sockets <= 9 {
            (3, 3)
        } else {
            let c = (total_sockets / 3).clamp(3, 6);
            let s = (total_sockets / c).clamp(2, 6);
            (c, s)
        };

        let buffer_kb = if total_speed >= 25.0 {
            1024
        } else if total_speed >= 8.0 {
            512
        } else {
            256
        };

        let has_penalties = st.penalty_by_ip.values().any(|&v| v > 0);
        let net_desc = format!("{} net{}", active.len(), if active.len() > 1 { "s" } else { "" });
        let speed_desc = if total_speed > 0.1 {
            format!("{total_speed:.1} MB/s")
        } else {
            "probing".to_string()
        };
        let guard = if has_penalties { " · Reset-Guard" } else { "" };

        let rec = AutoTuneRecommendation {
            max_concurrency: concurrency,
            segments_per_file: segments,
            buffer_size_kb: buffer_kb,
            per_adapter_max_conns: caps,
            total_socket_budget: total_sockets,
            profile_label: format!(
                "Auto: {concurrency} files × {segments} segs ({net_desc}, {speed_desc}){guard}"
            ),
        };
        st.last_rec = Some(rec.clone());
        rec
    }

    pub fn last_profile_label(&self) -> Option<String> {
        self.state
            .lock()
            .last_rec
            .as_ref()
            .map(|r| r.profile_label.clone())
    }
}
