use crate::models::NetworkAdapter;
use crate::platform;
use parking_lot::Mutex;
use std::collections::{HashMap, VecDeque};
use std::time::Instant;

const VIRTUAL_KEYWORDS: &[&str] = &[
    "wsl",
    "vethernet",
    "loopback",
    "vmware",
    "vmnet",
    "virtualbox",
    "vbox",
    "hyper-v",
    "docker",
    "npcap",
    "bluetooth",
    "tailscale",
    "zerotier",
    "wireguard",
    "wintun",
    "tap-windows",
    "br-",
    "veth",
    "virbr",
    "tun",
    "tap",
    "wg",
    "warp",
    "nordlynx",
    "proton",
    "mullvad",
    "openvpn",
    "radmin",
    "hamachi",
    "anyconnect",
    "fortinet",
    "pangps",
    "checkpoint",
];

struct NetState {
    enabled_adapters: HashMap<String, bool>,
    per_adapter_caps: HashMap<String, u32>,
    bytes_history: HashMap<String, VecDeque<(Instant, u64)>>,
    rolling_bytes: HashMap<String, u64>,
    ewma_speeds: HashMap<String, f64>,
    last_ewma_update: HashMap<String, Instant>,
    active_connections: HashMap<String, u32>,
    cached_hw: Vec<(String, String, bool, bool, String)>,
    last_hw_scan: Option<Instant>,
}

pub struct NetworkManager {
    ewma_alpha: f64,
    default_per_adapter_cap: u32,
    state: Mutex<NetState>,
}

impl NetworkManager {
    pub fn new(ewma_alpha: f64, default_per_adapter_cap: u32) -> Self {
        Self {
            ewma_alpha,
            default_per_adapter_cap,
            state: Mutex::new(NetState {
                enabled_adapters: HashMap::new(),
                per_adapter_caps: HashMap::new(),
                bytes_history: HashMap::new(),
                rolling_bytes: HashMap::new(),
                ewma_speeds: HashMap::new(),
                last_ewma_update: HashMap::new(),
                active_connections: HashMap::new(),
                cached_hw: Vec::new(),
                last_hw_scan: None,
            }),
        }
    }

    pub fn should_bind_interface(&self) -> bool {
        self.get_available_adapters()
            .into_iter()
            .filter(|a| a.is_enabled && a.is_up)
            .count()
            > 1
    }

    pub fn set_adapter_cap(&self, ip: &str, cap: u32) {
        if !ip.is_empty() {
            self.state
                .lock()
                .per_adapter_caps
                .insert(ip.to_string(), cap.max(1));
        }
    }

    pub fn set_adapter_enabled(&self, ip: &str, enabled: bool) {
        self.state
            .lock()
            .enabled_adapters
            .insert(ip.to_string(), enabled);
    }

    pub fn increment_connections(&self, ip: &str) {
        if ip.is_empty() {
            return;
        }
        let mut st = self.state.lock();
        *st.active_connections.entry(ip.to_string()).or_insert(0) += 1;
    }

    pub fn decrement_connections(&self, ip: &str) {
        if ip.is_empty() {
            return;
        }
        let mut st = self.state.lock();
        if let Some(c) = st.active_connections.get_mut(ip) {
            *c = c.saturating_sub(1);
        }
    }

    pub fn record_chunk_transfer(&self, ip: &str, bytes_count: u64) {
        if ip.is_empty() {
            return;
        }
        let now = Instant::now();
        let mut st = self.state.lock();
        let hist = st.bytes_history.entry(ip.to_string()).or_default();
        hist.push_back((now, bytes_count));
        *st.rolling_bytes.entry(ip.to_string()).or_insert(0) += bytes_count;
        Self::prune_locked(&mut st, ip, now);

        let instant_speed = Self::speed_locked(&st, ip, now);
        let prev = *st.ewma_speeds.get(ip).unwrap_or(&instant_speed);
        let next = self.ewma_alpha * instant_speed + (1.0 - self.ewma_alpha) * prev;
        st.ewma_speeds.insert(ip.to_string(), next);
        st.last_ewma_update.insert(ip.to_string(), now);
    }

    fn prune_locked(st: &mut NetState, ip: &str, now: Instant) {
        let Some(hist) = st.bytes_history.get_mut(ip) else {
            return;
        };
        let mut rolling = *st.rolling_bytes.get(ip).unwrap_or(&0);
        while let Some(&(ts, b)) = hist.front() {
            if now.duration_since(ts).as_secs_f64() > 2.0 {
                hist.pop_front();
                rolling = rolling.saturating_sub(b);
            } else {
                break;
            }
        }
        st.rolling_bytes.insert(ip.to_string(), rolling);
    }

    fn speed_locked(st: &NetState, ip: &str, now: Instant) -> f64 {
        let Some(hist) = st.bytes_history.get(ip) else {
            return 0.0;
        };
        let Some(&(earliest, _)) = hist.front() else {
            return 0.0;
        };
        let total = *st.rolling_bytes.get(ip).unwrap_or(&0);
        let dur = now.duration_since(earliest).as_secs_f64().max(0.5);
        total as f64 / dur
    }

    pub fn get_available_adapters(&self) -> Vec<NetworkAdapter> {
        let now = Instant::now();
        let mut st = self.state.lock();
        let refresh = st
            .last_hw_scan
            .map(|t| now.duration_since(t).as_secs_f64() >= 2.0)
            .unwrap_or(true);

        if refresh {
            st.cached_hw = Self::scan_hardware();
            st.last_hw_scan = Some(now);
        }

        let hw = st.cached_hw.clone();
        let mut adapters = Vec::with_capacity(hw.len());

        for (name, ip, is_up, has_gateway, friendly) in hw {
            let is_enabled = *st.enabled_adapters.get(&ip).unwrap_or(&(is_up && has_gateway));
            Self::prune_locked(&mut st, &ip, now);
            let cur_speed = Self::speed_locked(&st, &ip, now);
            let active_conns = *st.active_connections.get(&ip).unwrap_or(&0);
            let mut ewma = *st.ewma_speeds.get(&ip).unwrap_or(&0.0);
            if ewma > 0.0 {
                if let Some(&last_upd) = st.last_ewma_update.get(&ip) {
                    let idle = now.duration_since(last_upd).as_secs_f64();
                    if idle > 1.0 {
                        let half_life = if active_conns == 0 || cur_speed == 0.0 { 1.0 } else { 3.0 };
                        ewma *= 0.5f64.powf((idle - 1.0) / half_life);
                        if ewma <= 4096.0 {
                            ewma = 0.0;
                        }
                        st.ewma_speeds.insert(ip.clone(), ewma);
                    }
                }
            }
            let cap = *st
                .per_adapter_caps
                .get(&ip)
                .unwrap_or(&self.default_per_adapter_cap);

            adapters.push(NetworkAdapter {
                name,
                ip_address: ip,
                is_up,
                has_gateway,
                speed_mbps: 0,
                friendly_type: friendly,
                is_enabled,
                current_speed_bytes: cur_speed,
                ewma_speed_bytes: ewma,
                active_connections: active_conns,
                max_concurrency_cap: cap,
                allocated_slots: if is_enabled && is_up { cap } else { 0 },
                bandwidth_share_pct: 0.0,
            });
        }

        let total_ewma: f64 = adapters
            .iter()
            .filter(|a| a.is_enabled && a.is_up)
            .map(|a| a.ewma_speed_bytes)
            .sum();

        for a in &mut adapters {
            if total_ewma > 0.0 && a.is_enabled && a.is_up {
                a.bandwidth_share_pct = ((a.ewma_speed_bytes / total_ewma) * 1000.0).round() / 10.0;
            }
        }

        adapters
    }

    fn classify_adapter(name: &str, ip: &str) -> String {
        let lower = name.to_lowercase();
        if lower.contains("wi-fi")
            || lower.contains("wifi")
            || lower.contains("wireless")
            || lower.starts_with("wlan")
            || lower.starts_with("wlp")
        {
            "Wi-Fi".to_string()
        } else if lower.contains("tether")
            || lower.contains("rndis")
            || lower.contains("ncm")
            || lower.contains("usb")
            || lower.contains("wwan")
            || lower.contains("mobile")
            || lower.contains("android")
            || lower.contains("apple")
            || ip.starts_with("172.20.10.")
            || ip.starts_with("192.168.42.")
        {
            "USB Tethering".to_string()
        } else {
            "Ethernet / LAN".to_string()
        }
    }

    fn is_virtual(name: &str, ip: &str) -> bool {
        let lower = name.to_lowercase();
        if VIRTUAL_KEYWORDS.iter().any(|k| lower.contains(k)) || lower == "lo" {
            return true;
        }
        ip.starts_with("127.")
            || ip.starts_with("169.254.")
            || ip.starts_with("172.17.")
            || ip.starts_with("172.18.")
            || ip.starts_with("192.168.56.")
    }

    fn scan_hardware() -> Vec<(String, String, bool, bool, String)> {
        let gw_set = platform::get_default_route_interfaces();
        let mut out = Vec::new();

        #[cfg(unix)]
        {
            if let Ok(ifaces) = if_addrs::get_if_addrs() {
                for iface in ifaces {
                    if let if_addrs::IfAddr::V4(v4) = iface.addr {
                        let ip = v4.ip.to_string();
                        if Self::is_virtual(&iface.name, &ip) {
                            continue;
                        }
                        let has_gw = gw_set.is_empty() || gw_set.contains(&iface.name);
                        let friendly = Self::classify_adapter(&iface.name, &ip);
                        out.push((iface.name, ip, true, has_gw, friendly));
                    }
                }
            }
        }

        #[cfg(windows)]
        {
            use windows::Win32::Foundation::ERROR_BUFFER_OVERFLOW;
            use windows::Win32::NetworkManagement::IpHelper::{
                GetAdaptersAddresses, GAA_FLAG_INCLUDE_GATEWAYS, IP_ADAPTER_ADDRESSES_LH,
            };
            use windows::Win32::Networking::WinSock::{AF_INET, SOCKADDR_IN};

            let mut buf_len: u32 = 16384;
            let mut buf = vec![0u8; buf_len as usize];
            unsafe {
                let mut ret = GetAdaptersAddresses(
                    AF_INET.0 as u32,
                    GAA_FLAG_INCLUDE_GATEWAYS,
                    None,
                    Some(buf.as_mut_ptr() as *mut IP_ADAPTER_ADDRESSES_LH),
                    &mut buf_len,
                );
                if ret == ERROR_BUFFER_OVERFLOW.0 {
                    buf.resize(buf_len as usize, 0);
                    ret = GetAdaptersAddresses(
                        AF_INET.0 as u32,
                        GAA_FLAG_INCLUDE_GATEWAYS,
                        None,
                        Some(buf.as_mut_ptr() as *mut IP_ADAPTER_ADDRESSES_LH),
                        &mut buf_len,
                    );
                }
                if ret == 0 {
                    let mut ptr = buf.as_ptr() as *const IP_ADAPTER_ADDRESSES_LH;
                    while !ptr.is_null() {
                        let ad = &*ptr;
                        let name = if !ad.FriendlyName.is_null() {
                            ad.FriendlyName.to_string().unwrap_or_default()
                        } else {
                            String::new()
                        };
                        // Skip software loopback (24) and virtual tunnel/VPN adapters (131)
                        if ad.IfType == 24 || ad.IfType == 131 {
                            ptr = ad.Next;
                            continue;
                        }
                        let is_up = ad.OperStatus.0 == 1; // IfOperStatusUp
                        let idx_str = ad.Anonymous1.Anonymous.IfIndex.to_string();
                        let has_gw = !ad.FirstGatewayAddress.is_null()
                            || gw_set.is_empty()
                            || gw_set.contains(&idx_str);

                        let mut u_ptr = ad.FirstUnicastAddress;
                        while !u_ptr.is_null() {
                            let u = &*u_ptr;
                            if !u.Address.lpSockaddr.is_null() {
                                let sa = &*(u.Address.lpSockaddr as *const SOCKADDR_IN);
                                let ip = std::net::Ipv4Addr::from(u32::from_be(sa.sin_addr.S_un.S_addr))
                                    .to_string();
                                if !Self::is_virtual(&name, &ip) {
                                    let friendly = Self::classify_adapter(&name, &ip);
                                    out.push((name.clone(), ip, is_up, has_gw, friendly));
                                }
                            }
                            u_ptr = u.Next;
                        }
                        ptr = ad.Next;
                    }
                }
            }
        }

        out
    }

    pub fn calculate_adaptive_quotas(&self, total_slots: u32, adapters: &[NetworkAdapter]) -> HashMap<String, u32> {
        let active: Vec<&NetworkAdapter> = adapters
            .iter()
            .filter(|a| a.is_enabled && a.is_up)
            .collect();
        if active.is_empty() {
            return HashMap::new();
        }
        let total_ewma: f64 = active.iter().map(|a| a.ewma_speed_bytes.max(1024.0)).sum();
        let mut quotas = HashMap::new();
        for a in active {
            let speed = a.ewma_speed_bytes.max(1024.0);
            let raw = ((total_slots as f64) * (speed / total_ewma)).round() as u32;
            quotas.insert(
                a.ip_address.clone(),
                raw.clamp(1, a.max_concurrency_cap.max(1)),
            );
        }
        quotas
    }

    pub fn get_best_available_adapter(&self, total_target_concurrency: u32) -> Option<NetworkAdapter> {
        let all = self.get_available_adapters();
        let mut active: Vec<NetworkAdapter> = all
            .into_iter()
            .filter(|a| a.is_enabled && a.is_up)
            .collect();
        if active.is_empty() {
            return None;
        }
        let quotas = self.calculate_adaptive_quotas(total_target_concurrency, &active);
        active.sort_by(|a, b| {
            let a_head = a.max_concurrency_cap > a.active_connections;
            let b_head = b.max_concurrency_cap > b.active_connections;
            b_head
                .cmp(&a_head)
                .then_with(|| b.ewma_speed_bytes.total_cmp(&a.ewma_speed_bytes))
        });

        for a in &active {
            let q = *quotas.get(&a.ip_address).unwrap_or(&a.max_concurrency_cap);
            if a.active_connections < q {
                return Some(a.clone());
            }
        }

        active
            .into_iter()
            .filter(|a| a.active_connections < a.max_concurrency_cap)
            .max_by(|a, b| {
                (a.max_concurrency_cap - a.active_connections)
                    .cmp(&(b.max_concurrency_cap - b.active_connections))
                    .then_with(|| a.ewma_speed_bytes.total_cmp(&b.ewma_speed_bytes))
            })
    }

    pub fn allocate_segments_to_adapters(&self, segment_count: usize, strategy: &str) -> Vec<Option<NetworkAdapter>> {
        let active: Vec<NetworkAdapter> = self
            .get_available_adapters()
            .into_iter()
            .filter(|a| a.is_enabled && a.is_up)
            .collect();
        if active.is_empty() {
            return vec![None; segment_count];
        }
        if active.len() == 1 || strategy == "primary" {
            return vec![Some(active[0].clone()); segment_count];
        }
        if strategy == "equal" {
            return (0..segment_count)
                .map(|i| Some(active[i % active.len()].clone()))
                .collect();
        }

        let mut headroom: HashMap<String, u32> = active
            .iter()
            .map(|a| {
                (
                    a.ip_address.clone(),
                    a.max_concurrency_cap.saturating_sub(a.active_connections),
                )
            })
            .collect();
        let total_headroom: u32 = headroom.values().sum();
        let total_ewma: f64 = active.iter().map(|a| a.ewma_speed_bytes.max(1024.0)).sum();
        let mut quotas: HashMap<String, usize> = HashMap::new();
        let mut assigned_total = 0usize;

        for a in &active {
            let share = a.ewma_speed_bytes.max(1024.0) / total_ewma;
            let desired = (segment_count as f64 * share).round() as usize;
            let capped = if total_headroom > 0 {
                desired.min(*headroom.get(&a.ip_address).unwrap_or(&0) as usize)
            } else {
                desired
            };
            quotas.insert(a.ip_address.clone(), capped);
            assigned_total += capped;
        }

        while assigned_total < segment_count {
            let best = active
                .iter()
                .filter(|a| {
                    *quotas.get(&a.ip_address).unwrap_or(&0)
                        < *headroom.get(&a.ip_address).unwrap_or(&0) as usize
                })
                .max_by(|a, b| a.ewma_speed_bytes.total_cmp(&b.ewma_speed_bytes))
                .unwrap_or(&active[0]);
            *quotas.entry(best.ip_address.clone()).or_insert(0) += 1;
            assigned_total += 1;
            let _ = &mut headroom;
        }

        let map: HashMap<String, NetworkAdapter> = active
            .iter()
            .map(|a| (a.ip_address.clone(), a.clone()))
            .collect();
        let mut res = Vec::with_capacity(segment_count);
        for (ip, count) in quotas {
            if let Some(ad) = map.get(&ip) {
                for _ in 0..count {
                    res.push(Some(ad.clone()));
                }
            }
        }
        while res.len() < segment_count {
            res.push(Some(active[0].clone()));
        }
        res.truncate(segment_count);
        res
    }
}
