use crate::autotune::SmartAutoTuner;
use crate::journal::IntervalJournal;
use crate::models::{
    AppSettings, DownloadTask, ExtractionJob, HandoffEvent, Segment, TaskStatus, TelemetryMetrics,
    TelemetrySnapshot,
};
use crate::network::NetworkManager;
use crate::platform::{self, PositionalWriter};
use crate::resolvers::LinkManager;
use crate::store::SqliteStore;
use crate::unpacker::ArchiveExtractor;
use crate::utils::{extract_candidate_filename, natural_cmp, origin_referer, sanitize_filename};
use futures_util::StreamExt;
use http_body_util::BodyExt;
use parking_lot::Mutex;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::{Notify, Semaphore};
use tokio::task::{AbortHandle, JoinHandle};
use wreq_util::Profile;

struct ConnGuard<'a> {
    net_mgr: &'a NetworkManager,
    ip: String,
}

impl Drop for ConnGuard<'_> {
    fn drop(&mut self) {
        self.net_mgr.decrement_connections(&self.ip);
    }
}

fn delete_task_files_bg(files: Vec<(String, String)>) {
    if files.is_empty() {
        return;
    }
    std::thread::spawn(move || {
        // Brief pause so aborted segment tasks finish dropping file handles on Windows
        std::thread::sleep(Duration::from_millis(120));
        for (dir, name) in files {
            if dir.is_empty() || name.is_empty() {
                continue;
            }
            let base = PathBuf::from(&dir);
            let part_path = base.join(format!("{name}.part"));
            let final_path = base.join(&name);
            for p in [part_path, final_path] {
                if p.exists() {
                    if std::fs::remove_file(&p).is_err() {
                        std::thread::sleep(Duration::from_millis(150));
                        let _ = std::fs::remove_file(&p);
                    }
                }
            }
        }
    });
}

pub struct TokenBucketLimiter {
    rate_bytes_sec: Mutex<f64>,
    next_free: tokio::sync::Mutex<Instant>,
}

impl TokenBucketLimiter {
    pub fn new(rate_mbps: f64) -> Self {
        Self {
            rate_bytes_sec: Mutex::new(if rate_mbps > 0.0 {
                rate_mbps * 1024.0 * 1024.0
            } else {
                0.0
            }),
            next_free: tokio::sync::Mutex::new(Instant::now()),
        }
    }

    pub fn set_rate(&self, rate_mbps: f64) {
        *self.rate_bytes_sec.lock() = if rate_mbps > 0.0 {
            rate_mbps * 1024.0 * 1024.0
        } else {
            0.0
        };
    }

    pub async fn consume(&self, amount: usize) {
        let rate = *self.rate_bytes_sec.lock();
        if rate <= 0.0 || amount == 0 {
            return;
        }
        let wait_dur = {
            let mut cursor = self.next_free.lock().await;
            let now = Instant::now();
            let min_cursor = now - Duration::from_millis(250);
            if *cursor < min_cursor {
                *cursor = min_cursor;
            }
            *cursor += Duration::from_secs_f64(amount as f64 / rate);
            cursor.saturating_duration_since(now)
        };
        if wait_dur > Duration::from_millis(5) {
            tokio::time::sleep(wait_dur.min(Duration::from_secs(2))).await;
        }
    }
}

pub struct DownloadEngine {
    pub store: Arc<SqliteStore>,
    pub link_mgr: Arc<LinkManager>,
    pub net_mgr: Arc<NetworkManager>,
    pub auto_tuner: Arc<SmartAutoTuner>,
    pub settings: Arc<Mutex<AppSettings>>,
    pub tasks: Arc<Mutex<Vec<DownloadTask>>>,
    pub journals: Arc<Mutex<HashMap<String, IntervalJournal>>>,
    pub extraction_jobs: Arc<Mutex<HashMap<String, ExtractionJob>>>,
    pub handoff_events: Arc<Mutex<Vec<HandoffEvent>>>,
    active_handles: Arc<Mutex<HashMap<String, JoinHandle<()>>>>,
    segment_handles: Arc<Mutex<HashMap<String, Vec<AbortHandle>>>>,
    task_assigned_ip: Arc<Mutex<HashMap<String, String>>>,
    pinned_handoff_ip: Arc<Mutex<HashMap<String, String>>>,
    limiter: Arc<TokenBucketLimiter>,
    queue_notify: Arc<Notify>,
}

impl DownloadEngine {
    pub fn new(db_path: &Path, legacy_json_path: Option<&Path>) -> anyhow::Result<Arc<Self>> {
        let store = Arc::new(SqliteStore::open(db_path)?);
        if let Some(leg) = legacy_json_path {
            store.migrate_legacy_json_if_needed(leg);
        }

        let settings = store.load_settings();
        let mut tasks = store.load_tasks();
        let mut journals = HashMap::new();

        for t in &mut tasks {
            if matches!(t.status, TaskStatus::Downloading | TaskStatus::Resolving) {
                t.status = if settings.auto_resume_on_start {
                    TaskStatus::Queued
                } else {
                    TaskStatus::Paused
                };
            }
            t.speed_bytes_sec = 0.0;
            t.eta_seconds = None;
            t.segments.clear();

            if t.status != TaskStatus::Completed && !t.destination_dir.is_empty() && !t.filename.is_empty() {
                let final_path = PathBuf::from(&t.destination_dir).join(&t.filename);
                let part_path = PathBuf::from(&t.destination_dir).join(format!("{}.part", t.filename));
                if final_path.exists() && !part_path.exists() {
                    if let Ok(meta) = std::fs::metadata(&final_path) {
                        if meta.len() > 0 {
                            t.file_size = meta.len();
                            t.downloaded_bytes = meta.len();
                            t.status = TaskStatus::Completed;
                        }
                    }
                } else {
                    let mut ivs = store.load_task_intervals(&t.id);
                    if ivs.is_empty() && part_path.exists() && t.file_size > 0 {
                        ivs = platform::query_allocated_ranges(&part_path, t.file_size);
                    }
                    if !ivs.is_empty() && t.file_size > 0 {
                        let j = IntervalJournal::from_intervals(t.file_size, ivs);
                        t.downloaded_bytes = j.total_downloaded();
                        journals.insert(t.id.clone(), j);
                    }
                }
            }
        }

        let extraction_jobs = store.load_extraction_jobs();
        let link_mgr = Arc::new(LinkManager::new(Some(settings.rd_token.clone())));
        let net_mgr = Arc::new(NetworkManager::new(0.35, 3));
        let auto_tuner = Arc::new(SmartAutoTuner::new(settings.auto_tune));
        let limiter = Arc::new(TokenBucketLimiter::new(settings.speed_limit_mbps));

        let engine = Arc::new(Self {
            store,
            link_mgr,
            net_mgr,
            auto_tuner,
            settings: Arc::new(Mutex::new(settings)),
            tasks: Arc::new(Mutex::new(tasks)),
            journals: Arc::new(Mutex::new(journals)),
            extraction_jobs: Arc::new(Mutex::new(extraction_jobs)),
            handoff_events: Arc::new(Mutex::new(Vec::new())),
            active_handles: Arc::new(Mutex::new(HashMap::new())),
            segment_handles: Arc::new(Mutex::new(HashMap::new())),
            task_assigned_ip: Arc::new(Mutex::new(HashMap::new())),
            pinned_handoff_ip: Arc::new(Mutex::new(HashMap::new())),
            limiter,
            queue_notify: Arc::new(Notify::new()),
        });

        engine.spawn_background_loops();
        Ok(engine)
    }

    fn spawn_background_loops(self: &Arc<Self>) {
        let eng = Arc::clone(self);
        tokio::spawn(async move {
            eng.queue_loop().await;
        });

        let eng_cp = Arc::clone(self);
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(5)).await;
                let snapshot = eng_cp.tasks.lock().clone();
                let _ = eng_cp.store.save_tasks(&snapshot);
            }
        });
    }

    fn effective_bind_ip<'a>(&self, ip: &'a str) -> Option<&'a str> {
        if ip.is_empty() || !self.net_mgr.should_bind_interface() {
            None
        } else {
            Some(ip)
        }
    }

    pub fn apply_auto_tune(&self) {
        let adapters = self.net_mgr.get_available_adapters();
        let rec = self.auto_tuner.evaluate(&adapters);
        for (ip, cap) in &rec.per_adapter_max_conns {
            self.net_mgr.set_adapter_cap(ip, *cap);
        }
        let mut s = self.settings.lock();
        if s.auto_tune {
            s.max_concurrency = rec.max_concurrency;
            s.segments_per_file = rec.segments_per_file;
            s.buffer_size_kb = rec.buffer_size_kb;
        }
    }

    pub async fn add_links(&self, input_text: &str, download_dir: Option<String>) -> Vec<DownloadTask> {
        let target_dir = download_dir
            .filter(|d| !d.trim().is_empty())
            .unwrap_or_else(|| self.settings.lock().download_dir.clone());
        let urls = self.link_mgr.ingest_input(input_text).await;
        let mut created = Vec::new();
        {
            let mut tasks = self.tasks.lock();
            for url in urls {
                if let Some(existing) = tasks
                    .iter_mut()
                    .find(|t| t.original_url == url && t.destination_dir == target_dir)
                {
                    if existing.status == TaskStatus::Failed {
                        existing.status = TaskStatus::Queued;
                        existing.error_message = None;
                        existing.retries = 0;
                    }
                    created.push(existing.clone());
                    continue;
                }
                let fname = extract_candidate_filename(&url);
                let task = DownloadTask::new(url, target_dir.clone(), fname);
                created.push(task.clone());
                tasks.push(task);
            }
            tasks.sort_by(|a, b| natural_cmp(&a.filename, &b.filename));
        }
        self.apply_auto_tune();
        let _ = self.store.save_tasks(&self.tasks.lock());
        self.queue_notify.notify_one();
        created
    }

    pub fn pause_task(&self, task_id: &str) -> bool {
        let mut changed = false;
        {
            let mut tasks = self.tasks.lock();
            if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                if matches!(
                    t.status,
                    TaskStatus::Downloading | TaskStatus::Resolving | TaskStatus::Queued
                ) {
                    t.status = TaskStatus::Paused;
                    t.speed_bytes_sec = 0.0;
                    t.eta_seconds = None;
                    for s in &mut t.segments {
                        s.speed_bytes_sec = 0.0;
                        if s.status == "DOWNLOADING" || s.status == "QUEUED" {
                            s.status = "PAUSED".to_string();
                        }
                    }
                    changed = true;
                }
            }
        }
        if changed {
            if let Some(h) = self.active_handles.lock().remove(task_id) {
                h.abort();
            }
            if let Some(segs) = self.segment_handles.lock().remove(task_id) {
                for ah in segs {
                    ah.abort();
                }
            }
            let _ = self.store.save_tasks(&self.tasks.lock());
        }
        changed
    }

    pub fn resume_task(&self, task_id: &str) -> bool {
        let mut changed = false;
        {
            let mut tasks = self.tasks.lock();
            if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                if matches!(t.status, TaskStatus::Paused | TaskStatus::Failed) {
                    t.status = TaskStatus::Queued;
                    t.error_message = None;
                    t.retries = 0;
                    changed = true;
                }
            }
        }
        if changed {
            let _ = self.store.save_tasks(&self.tasks.lock());
            self.queue_notify.notify_one();
        }
        changed
    }

    pub fn pause_all(&self) {
        let ids: Vec<String> = self.tasks.lock().iter().map(|t| t.id.clone()).collect();
        for id in ids {
            self.pause_task(&id);
        }
    }

    pub fn resume_all(&self) {
        {
            let mut tasks = self.tasks.lock();
            for t in tasks.iter_mut() {
                if matches!(t.status, TaskStatus::Paused | TaskStatus::Failed) {
                    t.status = TaskStatus::Queued;
                    t.error_message = None;
                    t.retries = 0;
                }
            }
        }
        let _ = self.store.save_tasks(&self.tasks.lock());
        self.queue_notify.notify_one();
    }

    pub fn remove_task(&self, task_id: &str, delete_file: bool) -> bool {
        let file_info = if delete_file {
            self.tasks
                .lock()
                .iter()
                .find(|t| t.id == task_id)
                .map(|t| (t.destination_dir.clone(), t.filename.clone()))
        } else {
            None
        };
        self.pause_task(task_id);
        self.journals.lock().remove(task_id);
        let _ = self.store.delete_task_intervals(task_id);
        let mut tasks = self.tasks.lock();
        let before = tasks.len();
        tasks.retain(|t| t.id != task_id);
        let removed = tasks.len() != before;
        if removed {
            let _ = self.store.save_tasks(&tasks);
            if let Some(fi) = file_info {
                delete_task_files_bg(vec![fi]);
            }
        }
        removed
    }

    pub fn retry_failed(&self) {
        self.apply_auto_tune();
        {
            let mut tasks = self.tasks.lock();
            for t in tasks.iter_mut() {
                if t.status == TaskStatus::Failed {
                    t.status = TaskStatus::Queued;
                    t.error_message = None;
                    t.retries = 0;
                }
            }
        }
        let _ = self.store.save_tasks(&self.tasks.lock());
        self.queue_notify.notify_one();
    }

    pub fn reorder_task(&self, task_id: &str, direction: &str) -> bool {
        let mut tasks = self.tasks.lock();
        let Some(idx) = tasks.iter().position(|t| t.id == task_id) else {
            return false;
        };
        match direction {
            "top" => {
                if idx > 0 {
                    let item = tasks.remove(idx);
                    tasks.insert(0, item);
                }
            }
            "up" => {
                if idx > 0 {
                    tasks.swap(idx, idx - 1);
                } else {
                    return false;
                }
            }
            "down" => {
                if idx + 1 < tasks.len() {
                    tasks.swap(idx, idx + 1);
                } else {
                    return false;
                }
            }
            _ => return false,
        }
        let _ = self.store.save_tasks(&tasks);
        self.queue_notify.notify_one();
        true
    }

    pub fn handoff_task(&self, task_id: &str, target_ip: &str) -> bool {
        let target_ad = self
            .net_mgr
            .get_available_adapters()
            .into_iter()
            .find(|a| a.ip_address == target_ip && a.is_enabled && a.is_up);
        let Some(ad) = target_ad else {
            return false;
        };

        let (fname, from_label) = {
            let mut tasks = self.tasks.lock();
            let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) else {
                return false;
            };
            if t.status != TaskStatus::Downloading {
                return false;
            }
            let prev = t.assigned_adapter.clone().unwrap_or_else(|| "Unknown".to_string());
            t.assigned_adapter = Some(format!("{} ({})", ad.friendly_type, ad.ip_address));
            t.status = TaskStatus::Queued;
            t.speed_bytes_sec = 0.0;
            (t.filename.clone(), prev)
        };

        if let Some(h) = self.active_handles.lock().remove(task_id) {
            h.abort();
        }
        self.pinned_handoff_ip
            .lock()
            .insert(task_id.to_string(), target_ip.to_string());
        self.record_handoff_event(
            task_id,
            &fname,
            &from_label,
            &format!("{} ({})", ad.friendly_type, ad.ip_address),
        );
        self.reorder_task(task_id, "top");
        true
    }

    pub fn clear_completed(&self) -> usize {
        let mut tasks = self.tasks.lock();
        let before = tasks.len();
        tasks.retain(|t| t.status != TaskStatus::Completed);
        let removed = before - tasks.len();
        if removed > 0 {
            let _ = self.store.save_tasks(&tasks);
        }
        removed
    }

    pub fn clear_all(&self, delete_files: bool) -> usize {
        let files_to_delete: Vec<(String, String)> = if delete_files {
            self.tasks
                .lock()
                .iter()
                .map(|t| (t.destination_dir.clone(), t.filename.clone()))
                .collect()
        } else {
            Vec::new()
        };
        self.pause_all();
        let mut tasks = self.tasks.lock();
        let cnt = tasks.len();
        tasks.clear();
        self.journals.lock().clear();
        let _ = self.store.save_tasks(&tasks);
        if delete_files {
            delete_task_files_bg(files_to_delete);
        }
        cnt
    }

    pub fn sort_tasks(&self, criterion: &str) {
        let mut tasks = self.tasks.lock();
        match criterion {
            "status" => {
                let weight = |s: TaskStatus| match s {
                    TaskStatus::Downloading => 0,
                    TaskStatus::Resolving => 1,
                    TaskStatus::Queued => 2,
                    TaskStatus::Paused => 3,
                    TaskStatus::Failed => 4,
                    TaskStatus::Completed => 5,
                };
                tasks.sort_by_key(|t| weight(t.status));
            }
            "progress" => tasks.sort_by(|a, b| b.progress_pct().total_cmp(&a.progress_pct())),
            "size" => tasks.sort_by(|a, b| b.file_size.cmp(&a.file_size)),
            _ => tasks.sort_by(|a, b| natural_cmp(&a.filename, &b.filename)),
        }
        let _ = self.store.save_tasks(&tasks);
    }

    pub fn update_settings(&self, new_settings: AppSettings) {
        self.link_mgr.set_rd_token(&new_settings.rd_token);
        self.limiter.set_rate(new_settings.speed_limit_mbps);
        *self.settings.lock() = new_settings.clone();
        let _ = self.store.save_settings(&new_settings);
        self.apply_auto_tune();
        self.queue_notify.notify_one();
    }

    pub fn snapshot(&self) -> TelemetrySnapshot {
        let tasks = self.tasks.lock().clone();
        let settings = self.settings.lock().clone();
        let adapters = self.net_mgr.get_available_adapters();
        let groups = ArchiveExtractor::group_tasks_by_sequence(&tasks);
        let extraction_jobs = self.extraction_jobs.lock().clone();
        let handoff_events = self.handoff_events.lock().clone();

        let total_speed: f64 = tasks
            .iter()
            .filter(|t| t.status == TaskStatus::Downloading)
            .map(|t| t.speed_bytes_sec)
            .sum();
        let total_downloaded: u64 = tasks.iter().map(|t| t.downloaded_bytes).sum();
        let total_size: u64 = tasks.iter().map(|t| t.file_size).sum();
        let active_downloads = tasks
            .iter()
            .filter(|t| t.status == TaskStatus::Downloading)
            .count();
        let queued_downloads = tasks
            .iter()
            .filter(|t| t.status == TaskStatus::Queued)
            .count();
        let completed_downloads = tasks
            .iter()
            .filter(|t| t.status == TaskStatus::Completed)
            .count();
        let failed_downloads = tasks
            .iter()
            .filter(|t| t.status == TaskStatus::Failed)
            .count();

        let profile = if settings.auto_tune {
            self.auto_tuner
                .last_profile_label()
                .unwrap_or_else(|| "Auto: Probing...".to_string())
        } else {
            format!(
                "Manual: {} files × {} segs",
                settings.max_concurrency, settings.segments_per_file
            )
        };

        TelemetrySnapshot {
            tasks,
            adapters,
            handoff_events,
            groups,
            extraction_jobs,
            metrics: TelemetryMetrics {
                total_speed_bytes_sec: total_speed,
                total_downloaded_bytes: total_downloaded,
                total_size_bytes: total_size,
                active_downloads,
                queued_downloads,
                completed_downloads,
                failed_downloads,
                auto_tune_enabled: settings.auto_tune,
                auto_tune_profile: profile,
            },
            settings,
        }
    }

    async fn queue_loop(self: Arc<Self>) {
        loop {
            self.apply_auto_tune();
            let (max_conc, segs_per_file) = {
                let s = self.settings.lock();
                (s.max_concurrency.max(1) as usize, s.segments_per_file.max(1))
            };
            let stream_budget = (max_conc * segs_per_file as usize) as u32;

            let mut to_spawn = Vec::new();
            let mut local_pending: HashMap<String, u32> = HashMap::new();
            {
                let mut tasks = self.tasks.lock();
                let mut active_count = tasks
                    .iter()
                    .filter(|t| matches!(t.status, TaskStatus::Downloading | TaskStatus::Resolving))
                    .count();

                for t in tasks.iter_mut() {
                    if active_count >= max_conc {
                        break;
                    }
                    if t.status != TaskStatus::Queued {
                        continue;
                    }
                    let pinned = self.pinned_handoff_ip.lock().remove(&t.id);
                    let adapter = pinned
                        .and_then(|ip| {
                            self.net_mgr
                                .get_available_adapters()
                                .into_iter()
                                .find(|a| a.ip_address == ip && a.is_enabled && a.is_up)
                        })
                        .or_else(|| {
                            let mut ads: Vec<_> = self
                                .net_mgr
                                .get_available_adapters()
                                .into_iter()
                                .filter(|a| {
                                    a.is_enabled
                                        && a.is_up
                                        && self.auto_tuner.get_cooldown_remaining(Some(&a.ip_address)) <= 0.0
                                })
                                .collect();
                            if ads.is_empty() {
                                return self.net_mgr.get_best_available_adapter(stream_budget);
                            }
                            ads.sort_by(|a, b| {
                                let ca = a.active_connections + *local_pending.get(&a.ip_address).unwrap_or(&0);
                                let cb = b.active_connections + *local_pending.get(&b.ip_address).unwrap_or(&0);
                                ca.cmp(&cb)
                                    .then_with(|| b.ewma_speed_bytes.total_cmp(&a.ewma_speed_bytes))
                            });
                            ads.into_iter().next()
                        });

                    let (ad_label, ad_ip) = match adapter {
                        Some(ad) => (
                            format!("{} ({})", ad.friendly_type, ad.ip_address),
                            ad.ip_address,
                        ),
                        None => ("System Default".to_string(), String::new()),
                    };
                    if !ad_ip.is_empty() {
                        *local_pending.entry(ad_ip.clone()).or_insert(0) += 1;
                    }
                    t.status = TaskStatus::Resolving;
                    t.assigned_adapter = Some(ad_label);
                    self.task_assigned_ip
                        .lock()
                        .insert(t.id.clone(), ad_ip.clone());
                    active_count += 1;
                    to_spawn.push((t.id.clone(), ad_ip));
                }
            }

            for (task_id, ip) in to_spawn {
                let eng = Arc::clone(&self);
                let eng_c = Arc::clone(&eng);
                let tid_c = task_id.clone();
                let handle = tokio::spawn(async move {
                    Arc::clone(&eng_c).run_task_pipeline(tid_c.clone(), ip).await;
                    eng_c.active_handles.lock().remove(&tid_c);
                    eng_c.queue_notify.notify_one();
                });
                self.active_handles.lock().insert(task_id, handle);
            }

            let _ = tokio::time::timeout(Duration::from_millis(250), self.queue_notify.notified()).await;
        }
    }

    async fn run_task_pipeline(self: Arc<Self>, task_id: String, mut interface_ip: String) {
        let Some(orig_url) = self
            .tasks
            .lock()
            .iter()
            .find(|t| t.id == task_id)
            .map(|t| t.original_url.clone())
        else {
            return;
        };

        // Try assigned adapter, failover to other adapters, and finish with System Default (no local bind)
        let mut candidate_ips = vec![interface_ip.clone()];
        for ad in self.net_mgr.get_available_adapters() {
            if ad.is_enabled && ad.is_up && ad.ip_address != interface_ip {
                candidate_ips.push(ad.ip_address);
            }
        }
        if !interface_ip.is_empty() && !candidate_ips.iter().any(|c| c.is_empty()) {
            candidate_ips.push(String::new());
        }

        let mut resolved_opt = None;
        let mut last_err_msg = String::new();
        for cand_ip in candidate_ips {
            let bind_opt = if cand_ip.is_empty() {
                None
            } else {
                self.effective_bind_ip(&cand_ip)
            };
            if !cand_ip.is_empty() {
                self.net_mgr.increment_connections(&cand_ip);
            }
            let res = tokio::time::timeout(
                Duration::from_secs(15),
                self.link_mgr.resolve_link(&orig_url, bind_opt, true),
            )
            .await;
            if !cand_ip.is_empty() {
                self.net_mgr.decrement_connections(&cand_ip);
            }

            match res {
                Ok(Ok(r)) => {
                    if cand_ip != interface_ip {
                        self.record_handoff_event(&task_id, &orig_url, &interface_ip, &cand_ip);
                        interface_ip = cand_ip.clone();
                        let mut tasks = self.tasks.lock();
                        if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                            t.assigned_adapter = Some(if cand_ip.is_empty() {
                                "System Default".to_string()
                            } else {
                                format!("Failover ({cand_ip})")
                            });
                        }
                    }
                    resolved_opt = Some(r);
                    break;
                }
                Ok(Err(e)) => {
                    last_err_msg = e.to_string();
                    if last_err_msg.contains("404") || last_err_msg.contains("Dead link") {
                        break;
                    }
                    if !cand_ip.is_empty() {
                        self.auto_tuner.register_cooldown(Some(&cand_ip), 45.0);
                    }
                }
                Err(_) => {
                    last_err_msg = format!(
                        "Resolve timed out on {}",
                        if cand_ip.is_empty() { "System Default" } else { &cand_ip }
                    );
                    if !cand_ip.is_empty() {
                        self.auto_tuner.register_cooldown(Some(&cand_ip), 60.0);
                    }
                }
            }
        }

        let Some(resolved) = resolved_opt else {
            self.fail_task(&task_id, &last_err_msg);
            return;
        };

        let (direct_url, dest_dir, filename, mut file_size) = {
            let mut tasks = self.tasks.lock();
            let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) else {
                return;
            };
            if t.status != TaskStatus::Resolving {
                return;
            }
            t.direct_url = Some(resolved.direct_url.clone());
            if !resolved.filename.is_empty() && t.filename == "download.bin" {
                t.filename = sanitize_filename(&resolved.filename);
            }
            if resolved.file_size > 0 {
                t.file_size = resolved.file_size;
            }
            t.status = TaskStatus::Downloading;
            t.error_message = None;
            (
                resolved.direct_url,
                t.destination_dir.clone(),
                t.filename.clone(),
                t.file_size,
            )
        };

        let referer = origin_referer(&orig_url).unwrap_or_default();
        let client = match self
            .link_mgr
            .pool
            .get_client(self.effective_bind_ip(&interface_ip), Profile::Chrome131, true)
        {
            Ok(c) => c,
            Err(e) => {
                self.fail_task(&task_id, &e.to_string());
                return;
            }
        };

        let mut supports_range = true;
        if file_size == 0 {
            let mut req = client.get(&direct_url).header("Range", "bytes=0-0");
            if !referer.is_empty() {
                req = req.header("Referer", &referer);
            }
            if let Ok(probe) = req.send().await {
                let code = probe.status().as_u16();
                if code == 206 {
                    if let Some(cr) = probe.headers().get("content-range").and_then(|v| v.to_str().ok()) {
                        if let Some(total_str) = cr.split('/').next_back() {
                            if let Ok(sz) = total_str.parse::<u64>() {
                                file_size = sz;
                            }
                        }
                    }
                    supports_range = true;
                } else if code == 200 {
                    if let Some(cl) = probe.headers().get("content-length").and_then(|v| v.to_str().ok()) {
                        if let Ok(sz) = cl.parse::<u64>() {
                            file_size = sz;
                        }
                    }
                    supports_range = false;
                }
            }
        }

        {
            let mut tasks = self.tasks.lock();
            if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                t.file_size = file_size;
                t.supports_range = supports_range;
            }
        }

        let (segs_per_file, min_seg_mb, buf_kb) = {
            let s = self.settings.lock();
            (
                s.segments_per_file.max(1) as usize,
                s.min_segment_file_size_mb * 1024 * 1024,
                s.buffer_size_kb.max(64) as usize,
            )
        };

        let part_path = PathBuf::from(&dest_dir).join(format!("{filename}.part"));
        let final_path = PathBuf::from(&dest_dir).join(&filename);

        if supports_range && file_size >= min_seg_mb && segs_per_file > 1 {
            if let Err(e) = platform::preallocate_sparse_file(&part_path, file_size) {
                self.fail_task(&task_id, &e.to_string());
                return;
            }
            let ivs = self.store.load_task_intervals(&task_id);
            let journal = IntervalJournal::from_intervals(file_size, ivs);
            let slices = journal.plan_hole_slices(segs_per_file);

            if journal.is_complete() {
                let _ = tokio::fs::rename(&part_path, &final_path).await;
                self.complete_task(&task_id, file_size);
                return;
            }

            self.journals.lock().insert(task_id.clone(), journal);

            let adapters = self
                .net_mgr
                .allocate_segments_to_adapters(slices.len(), &self.settings.lock().bonding_strategy);

            let seg_models: Vec<Segment> = slices
                .iter()
                .enumerate()
                .map(|(i, &(s, e))| Segment {
                    index: i,
                    start_byte: s,
                    end_byte: e,
                    downloaded_bytes: 0,
                    status: "QUEUED".to_string(),
                    assigned_adapter: adapters
                        .get(i)
                        .and_then(|a| a.as_ref())
                        .map(|a| format!("{} ({})", a.friendly_type, a.ip_address)),
                    speed_bytes_sec: 0.0,
                })
                .collect();

            {
                let mut tasks = self.tasks.lock();
                if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                    t.segments = seg_models.clone();
                }
            }

            let writer = match PositionalWriter::open(&part_path) {
                Ok(w) => Arc::new(w),
                Err(e) => {
                    self.fail_task(&task_id, &e.to_string());
                    return;
                }
            };

            let sem = Arc::new(Semaphore::new(segs_per_file));
            self.segment_handles.lock().remove(&task_id);
            let mut seg_handles = Vec::new();
            for (idx, &(s_byte, e_byte)) in slices.iter().enumerate() {
                let seg_ip = adapters
                    .get(idx)
                    .and_then(|a| a.as_ref())
                    .map(|a| a.ip_address.clone())
                    .unwrap_or_else(|| interface_ip.clone());
                let eng = Arc::clone(&self);
                let w = Arc::clone(&writer);
                let url = direct_url.clone();
                let ref_hdr = referer.clone();
                let tid = task_id.clone();
                let sem_c = Arc::clone(&sem);

                let jh = tokio::spawn(async move {
                    let _p = sem_c.acquire().await;
                    eng.download_segment(tid, idx, s_byte, e_byte, url, ref_hdr, seg_ip, w, buf_kb)
                        .await
                });
                self.segment_handles
                    .lock()
                    .entry(task_id.clone())
                    .or_default()
                    .push(jh.abort_handle());
                seg_handles.push(jh);
            }

            let mut all_ok = true;
            for h in seg_handles {
                match h.await {
                    Ok(Ok(())) => {}
                    _ => all_ok = false,
                }
            }
            self.segment_handles.lock().remove(&task_id);
            let _ = writer.sync_all();

            let complete = self
                .journals
                .lock()
                .get(&task_id)
                .map(|j| j.is_complete())
                .unwrap_or(false);

            if all_ok && complete {
                let _ = self.store.delete_task_intervals(&task_id);
                let _ = tokio::fs::rename(&part_path, &final_path).await;
                self.complete_task(&task_id, file_size);
                return;
            } else {
                // Re-queue on transient segment failure so healthy adapter can finish remaining holes
                let mut tasks = self.tasks.lock();
                if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                    if t.status == TaskStatus::Downloading {
                        t.retries += 1;
                        if t.retries < self.settings.lock().max_retries {
                            t.status = TaskStatus::Queued;
                            t.speed_bytes_sec = 0.0;
                            t.error_message = Some(format!("Retrying ({})...", t.retries));
                        } else {
                            t.status = TaskStatus::Failed;
                            t.speed_bytes_sec = 0.0;
                            t.error_message = Some("Max segment retries exceeded".to_string());
                        }
                    }
                }
                let _ = self.store.save_tasks(&tasks);
            }
        } else {
            // Single stream fallback
            if let Err(e) = self
                .download_single(&task_id, &direct_url, &referer, &interface_ip, &part_path, &final_path, buf_kb)
                .await
            {
                self.fail_task(&task_id, &e.to_string());
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn download_segment(
        &self,
        task_id: String,
        seg_idx: usize,
        start: u64,
        end: u64,
        url: String,
        referer: String,
        adapter_ip: String,
        writer: Arc<PositionalWriter>,
        buf_kb: usize,
    ) -> anyhow::Result<()> {
        let client = self
            .link_mgr
            .pool
            .get_client(self.effective_bind_ip(&adapter_ip), Profile::Chrome131, true)?;
        if !adapter_ip.is_empty() {
            self.net_mgr.increment_connections(&adapter_ip);
        }
        let _conn_guard = ConnGuard {
            net_mgr: &self.net_mgr,
            ip: adapter_ip.clone(),
        };

        let res = async {
            let mut req = client
                .get(&url)
                .header("Range", format!("bytes={start}-{end}"));
            if !referer.is_empty() {
                req = req.header("Referer", &referer);
            }
            let resp = req.send().await?;
            let code = resp.status().as_u16();
            if !matches!(code, 200 | 206) {
                return Err(anyhow::anyhow!("Segment {seg_idx} HTTP {code}"));
            }

            let mut stream = resp.into_data_stream();
            let mut offset = start;
            let mut last_check = Instant::now();
            let mut bytes_since = 0u64;
            let mut write_buf: Vec<u8> = Vec::with_capacity(buf_kb * 1024);

            while let Some(chunk_res) =
                tokio::time::timeout(Duration::from_secs(15), stream.next()).await?
            {
                let chunk = chunk_res?;
                if chunk.is_empty() {
                    continue;
                }
                let remaining = (end - offset + 1) as usize;
                let slice = &chunk[..chunk.len().min(remaining)];
                write_buf.extend_from_slice(slice);

                if write_buf.len() >= buf_kb * 1024 || slice.len() == remaining {
                    let data = std::mem::take(&mut write_buf);
                    let w_off = offset;
                    let w_len = data.len() as u64;
                    let w_clone = Arc::clone(&writer);
                    tokio::task::spawn_blocking(move || w_clone.write_at(w_off, &data)).await??;
                    offset += w_len;

                    {
                        let mut journals = self.journals.lock();
                        if let Some(j) = journals.get_mut(&task_id) {
                            j.add_range(w_off, w_off + w_len - 1);
                            let total_dl = j.total_downloaded();
                            let ivs = j.intervals.clone();
                            drop(journals);
                            let _ = self.store.save_task_intervals(&task_id, &ivs);
                            let mut tasks = self.tasks.lock();
                            if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                                t.downloaded_bytes = total_dl;
                                if let Some(s) = t.segments.get_mut(seg_idx) {
                                    s.downloaded_bytes += w_len;
                                    s.status = "DOWNLOADING".to_string();
                                }
                            }
                        }
                    }
                }

                bytes_since += slice.len() as u64;
                self.net_mgr.record_chunk_transfer(&adapter_ip, slice.len() as u64);
                self.limiter.consume(slice.len()).await;

                let elapsed = last_check.elapsed().as_secs_f64();
                if elapsed >= 0.5 {
                    let speed = bytes_since as f64 / elapsed;
                    let mut tasks = self.tasks.lock();
                    if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                        if let Some(s) = t.segments.get_mut(seg_idx) {
                            s.speed_bytes_sec = speed;
                        }
                        t.speed_bytes_sec = t
                            .segments
                            .iter()
                            .filter(|s| s.status == "DOWNLOADING")
                            .map(|s| s.speed_bytes_sec)
                            .sum();
                    }
                    bytes_since = 0;
                    last_check = Instant::now();
                }

                if offset > end {
                    break;
                }
            }

            if !write_buf.is_empty() {
                let w_off = offset;
                let w_len = write_buf.len() as u64;
                let w_clone = Arc::clone(&writer);
                tokio::task::spawn_blocking(move || w_clone.write_at(w_off, &write_buf)).await??;
                let mut journals = self.journals.lock();
                if let Some(j) = journals.get_mut(&task_id) {
                    j.add_range(w_off, w_off + w_len - 1);
                }
            }

            let mut tasks = self.tasks.lock();
            if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                if let Some(s) = t.segments.get_mut(seg_idx) {
                    s.status = "COMPLETED".to_string();
                    s.speed_bytes_sec = 0.0;
                }
            }
            self.auto_tuner.record_stream_success(Some(&adapter_ip));
            Ok(())
        }
        .await;

        if let Err(ref e) = res {
            self.auto_tuner.record_stream_error(Some(&adapter_ip), &e.to_string());
        }
        res
    }

    #[allow(clippy::too_many_arguments)]
    async fn download_single(
        &self,
        task_id: &str,
        url: &str,
        referer: &str,
        adapter_ip: &str,
        part_path: &Path,
        final_path: &Path,
        _buf_kb: usize,
    ) -> anyhow::Result<()> {
        use tokio::io::AsyncWriteExt;

        if let Some(parent) = part_path.parent() {
            tokio::fs::create_dir_all(parent).await?;
        }
        let existing = tokio::fs::metadata(part_path).await.map(|m| m.len()).unwrap_or(0);
        let client = self
            .link_mgr
            .pool
            .get_client(self.effective_bind_ip(adapter_ip), Profile::Chrome131, true)?;
        let mut req = client.get(url);
        if !referer.is_empty() {
            req = req.header("Referer", referer);
        }
        if existing > 0 {
            req = req.header("Range", format!("bytes={existing}-"));
        }

        let resp = req.send().await?;
        let code = resp.status().as_u16();
        let mut file = if code == 206 {
            tokio::fs::OpenOptions::new().create(true).append(true).open(part_path).await?
        } else if code == 200 {
            tokio::fs::File::create(part_path).await?
        } else if code == 416 {
            let _ = tokio::fs::rename(part_path, final_path).await;
            self.complete_task(task_id, existing);
            return Ok(());
        } else {
            return Err(anyhow::anyhow!("HTTP {code}"));
        };

        let mut downloaded = if code == 206 { existing } else { 0 };
        let mut stream = resp.into_data_stream();
        let mut last_check = Instant::now();
        let mut bytes_since = 0u64;

        while let Some(chunk_res) = tokio::time::timeout(Duration::from_secs(15), stream.next()).await? {
            let chunk = chunk_res?;
            if chunk.is_empty() {
                continue;
            }
            file.write_all(&chunk).await?;
            let clen = chunk.len() as u64;
            downloaded += clen;
            bytes_since += clen;
            self.net_mgr.record_chunk_transfer(adapter_ip, clen);
            self.limiter.consume(chunk.len()).await;

            let elapsed = last_check.elapsed().as_secs_f64();
            if elapsed >= 0.5 {
                let speed = bytes_since as f64 / elapsed;
                let mut tasks = self.tasks.lock();
                if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                    t.downloaded_bytes = downloaded;
                    t.speed_bytes_sec = speed;
                }
                bytes_since = 0;
                last_check = Instant::now();
            }
        }
        file.flush().await?;
        let _ = tokio::fs::rename(part_path, final_path).await;
        self.complete_task(task_id, downloaded);
        Ok(())
    }

    fn complete_task(&self, task_id: &str, final_size: u64) {
        let should_extract = {
            let mut tasks = self.tasks.lock();
            if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
                t.status = TaskStatus::Completed;
                t.downloaded_bytes = final_size;
                t.file_size = final_size;
                t.speed_bytes_sec = 0.0;
                t.eta_seconds = Some(0);
            }
            self.settings.lock().auto_extract
        };
        let _ = self.store.save_tasks(&self.tasks.lock());

        if should_extract {
            let tasks_snap = self.tasks.lock().clone();
            let groups = ArchiveExtractor::group_tasks_by_sequence(&tasks_snap);
            let delete_after = self.settings.lock().delete_archive_after_extract;
            for (gkey, g) in groups {
                if g.is_complete && g.extension != "bin" {
                    if let Some(first_part) = g.first_part {
                        let already = self.extraction_jobs.lock().contains_key(&gkey);
                        if !already {
                            let first_file = PathBuf::from(&g.destination_dir).join(&first_part);
                            let clean_prefix = g.prefix.trim_matches(|c| c == '.' || c == '_' || c == '-');
                            let out_dir = PathBuf::from(&g.destination_dir).join(clean_prefix);
                            let part_files: Vec<PathBuf> = tasks_snap
                                .iter()
                                .filter(|t| g.task_ids.contains(&t.id))
                                .map(|t| PathBuf::from(&t.destination_dir).join(&t.filename))
                                .collect();
                            let jobs_arc = Arc::clone(&self.extraction_jobs);
                            let store_arc = Arc::clone(&self.store);
                            tokio::spawn(async move {
                                let _ = ArchiveExtractor::extract_archive(
                                    first_file,
                                    out_dir,
                                    gkey.clone(),
                                    part_files,
                                    delete_after,
                                    |job| {
                                        jobs_arc.lock().insert(gkey.clone(), job.clone());
                                        let _ = store_arc.save_extraction_job(&job);
                                    },
                                )
                                .await;
                            });
                        }
                    }
                }
            }
        }
    }

    fn fail_task(&self, task_id: &str, msg: &str) {
        let mut tasks = self.tasks.lock();
        if let Some(t) = tasks.iter_mut().find(|t| t.id == task_id) {
            t.status = TaskStatus::Failed;
            t.speed_bytes_sec = 0.0;
            t.error_message = Some(msg.to_string());
        }
        let _ = self.store.save_tasks(&tasks);
    }

    pub fn record_handoff_event(&self, task_id: &str, filename: &str, from: &str, to: &str) {
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let mut evs = self.handoff_events.lock();
        evs.push(HandoffEvent {
            timestamp: ts,
            task_id: task_id.to_string(),
            filename: filename.to_string(),
            from_adapter: from.to_string(),
            to_adapter: to.to_string(),
        });
        if evs.len() > 15 {
            evs.remove(0);
        }
    }
}
