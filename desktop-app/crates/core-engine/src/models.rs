use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum TaskStatus {
    Queued,
    Resolving,
    Downloading,
    Paused,
    Completed,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct Segment {
    pub index: usize,
    pub start_byte: u64,
    pub end_byte: u64,
    pub downloaded_bytes: u64,
    pub status: String,
    pub assigned_adapter: Option<String>,
    pub speed_bytes_sec: f64,
}

impl Segment {
    pub fn total_bytes(&self) -> u64 {
        if self.end_byte >= self.start_byte {
            self.end_byte - self.start_byte + 1
        } else {
            0
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct DownloadTask {
    pub id: String,
    pub original_url: String,
    pub direct_url: Option<String>,
    pub filename: String,
    pub file_size: u64,
    pub downloaded_bytes: u64,
    pub status: TaskStatus,
    pub assigned_adapter: Option<String>,
    pub speed_bytes_sec: f64,
    pub eta_seconds: Option<u64>,
    pub error_message: Option<String>,
    pub retries: u32,
    pub destination_dir: String,
    pub segments: Vec<Segment>,
    pub supports_range: bool,
    pub priority: i32,
}

impl Default for DownloadTask {
    fn default() -> Self {
        Self::new(String::new(), String::new(), "download.bin".to_string())
    }
}

impl DownloadTask {
    pub fn new(original_url: String, destination_dir: String, filename: String) -> Self {
        Self {
            id: Uuid::new_v4().to_string()[..8].to_string(),
            original_url,
            direct_url: None,
            filename,
            file_size: 0,
            downloaded_bytes: 0,
            status: TaskStatus::Queued,
            assigned_adapter: None,
            speed_bytes_sec: 0.0,
            eta_seconds: None,
            error_message: None,
            retries: 0,
            destination_dir,
            segments: Vec::new(),
            supports_range: true,
            priority: 0,
        }
    }

    pub fn progress_pct(&self) -> f64 {
        if self.file_size == 0 {
            return if self.status == TaskStatus::Completed {
                100.0
            } else {
                0.0
            };
        }
        ((self.downloaded_bytes as f64 / self.file_size as f64) * 100.0).min(100.0)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NetworkAdapter {
    pub name: String,
    pub ip_address: String,
    pub is_up: bool,
    pub has_gateway: bool,
    pub speed_mbps: u64,
    pub friendly_type: String,
    pub is_enabled: bool,
    pub current_speed_bytes: f64,
    pub ewma_speed_bytes: f64,
    pub active_connections: u32,
    pub max_concurrency_cap: u32,
    pub allocated_slots: u32,
    pub bandwidth_share_pct: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct AppSettings {
    pub download_dir: String,
    pub auto_tune: bool,
    pub max_concurrency: u32,
    pub segments_per_file: u32,
    pub buffer_size_kb: u32,
    pub speed_limit_mbps: f64,
    pub max_retries: u32,
    pub min_segment_file_size_mb: u64,
    pub bonding_strategy: String,
    pub work_stealing_enabled: bool,
    pub auto_extract: bool,
    pub delete_archive_after_extract: bool,
    pub auto_resume_on_start: bool,
    pub auto_retry_failed: bool,
    pub rd_token: String,
}

impl Default for AppSettings {
    fn default() -> Self {
        let dl_dir = dirs::download_dir()
            .or_else(|| {
                std::env::var("USERPROFILE")
                    .or_else(|_| std::env::var("HOME"))
                    .ok()
                    .map(|h| std::path::PathBuf::from(h).join("Downloads"))
            })
            .map(|d| d.join("PartHarvester").to_string_lossy().to_string())
            .unwrap_or_else(|| "Downloads/PartHarvester".to_string());
        Self {
            download_dir: dl_dir,
            auto_tune: true,
            max_concurrency: 4,
            segments_per_file: 4,
            buffer_size_kb: 256,
            speed_limit_mbps: 0.0,
            max_retries: 5,
            min_segment_file_size_mb: 10,
            bonding_strategy: "ewma".to_string(),
            work_stealing_enabled: true,
            auto_extract: true,
            delete_archive_after_extract: false,
            auto_resume_on_start: true,
            auto_retry_failed: true,
            rd_token: String::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HandoffEvent {
    pub timestamp: f64,
    pub task_id: String,
    pub filename: String,
    pub from_adapter: String,
    pub to_adapter: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExtractionJob {
    pub group_key: String,
    pub status: String, // QUEUED, EXTRACTING, SUCCESS, FAILED
    pub progress_pct: f32,
    pub archive_path: String,
    pub output_dir: String,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SequenceGroup {
    pub group_key: String,
    pub prefix: String,
    pub extension: String,
    pub destination_dir: String,
    pub total_parts: usize,
    pub completed_parts: usize,
    pub is_complete: bool,
    pub first_part: Option<String>,
    pub task_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetryMetrics {
    pub total_speed_bytes_sec: f64,
    pub total_downloaded_bytes: u64,
    pub total_size_bytes: u64,
    pub active_downloads: usize,
    pub queued_downloads: usize,
    pub completed_downloads: usize,
    pub failed_downloads: usize,
    pub auto_tune_enabled: bool,
    pub auto_tune_profile: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetrySnapshot {
    pub tasks: Vec<DownloadTask>,
    pub adapters: Vec<NetworkAdapter>,
    pub handoff_events: Vec<HandoffEvent>,
    pub groups: HashMap<String, SequenceGroup>,
    pub extraction_jobs: HashMap<String, ExtractionJob>,
    pub metrics: TelemetryMetrics,
    pub settings: AppSettings,
}
