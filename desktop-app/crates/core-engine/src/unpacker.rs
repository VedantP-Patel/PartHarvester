use crate::models::{DownloadTask, ExtractionJob, SequenceGroup, TaskStatus};
use crate::platform;
use anyhow::{anyhow, Result};
use regex::Regex;
use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use tokio::io::AsyncReadExt;
use tokio::process::Command;

pub struct ArchiveExtractor;

impl ArchiveExtractor {
    pub fn match_part_sequence(filename: &str) -> Option<(String, usize, String)> {
        let patterns = [
            Regex::new(r"(?i)^(?P<prefix>.+?)[_.\-]part(?P<idx>\d+)\.(?P<ext>rar|exe)$").unwrap(),
            Regex::new(r"(?i)^(?P<prefix>.+?)\.(?P<ext>7z|rar)\.(?P<idx>\d{3})$").unwrap(),
            Regex::new(r"(?i)^(?P<prefix>.+?)\.r(?P<idx>\d{2})$").unwrap(),
            Regex::new(r"(?i)^(?P<prefix>fg)[_.\-](?P<idx>\d+)\.(?P<ext>bin)$").unwrap(),
        ];
        for re in patterns {
            if let Some(caps) = re.captures(filename) {
                let prefix = caps.name("prefix")?.as_str().to_string();
                let idx: usize = caps.name("idx")?.as_str().parse().ok()?;
                let ext = caps
                    .name("ext")
                    .map(|m| m.as_str().to_ascii_lowercase())
                    .unwrap_or_else(|| "rar".to_string());
                return Some((prefix, idx, ext));
            }
        }
        None
    }

    pub fn group_tasks_by_sequence(tasks: &[DownloadTask]) -> HashMap<String, SequenceGroup> {
        struct Acc<'a> {
            prefix: String,
            extension: String,
            destination_dir: String,
            parts: BTreeMap<usize, &'a DownloadTask>,
        }

        let mut accs: HashMap<String, Acc<'_>> = HashMap::new();
        for t in tasks {
            if let Some((prefix, idx, ext)) = Self::match_part_sequence(&t.filename) {
                let norm_dir = Path::new(&t.destination_dir)
                    .to_string_lossy()
                    .to_string();
                let key = format!("{norm_dir}::{prefix}");
                let entry = accs.entry(key).or_insert_with(|| Acc {
                    prefix,
                    extension: ext,
                    destination_dir: t.destination_dir.clone(),
                    parts: BTreeMap::new(),
                });
                entry.parts.insert(idx, t);
            }
        }

        let mut groups = HashMap::new();
        for (key, acc) in accs {
            if acc.parts.is_empty() {
                continue;
            }
            let min_idx = *acc.parts.keys().next().unwrap();
            let max_idx = *acc.parts.keys().next_back().unwrap();
            let expected_count = max_idx - min_idx + 1;
            let all_present = (min_idx == 0 || min_idx == 1) && acc.parts.len() == expected_count;
            let completed_parts = acc
                .parts
                .values()
                .filter(|t| t.status == TaskStatus::Completed)
                .count();
            let is_complete = all_present && completed_parts == acc.parts.len();
            let first_part = acc.parts.get(&min_idx).map(|t| t.filename.clone());
            let task_ids = acc.parts.values().map(|t| t.id.clone()).collect();

            groups.insert(
                key.clone(),
                SequenceGroup {
                    group_key: key,
                    prefix: acc.prefix,
                    extension: acc.extension,
                    destination_dir: acc.destination_dir,
                    total_parts: expected_count,
                    completed_parts,
                    is_complete,
                    first_part,
                    task_ids,
                },
            );
        }
        groups
    }

    pub fn parse_7z_progress(chunk: &str) -> Option<f32> {
        let re = Regex::new(r"(\d{1,3})%").unwrap();
        let mut last = None;
        for cap in re.captures_iter(chunk) {
            if let Ok(pct) = cap[1].parse::<f32>() {
                if pct <= 100.0 {
                    last = Some(pct);
                }
            }
        }
        last
    }

    pub async fn extract_archive<F>(
        archive_path: PathBuf,
        output_dir: PathBuf,
        group_id: String,
        part_files: Vec<PathBuf>,
        delete_after_extract: bool,
        mut on_progress: F,
    ) -> Result<ExtractionJob>
    where
        F: FnMut(ExtractionJob) + Send,
    {
        let seven_zip = platform::find_7zip()
            .ok_or_else(|| anyhow!("7-Zip (7z/7zz) not found on system."))?;
        if !archive_path.exists() {
            return Err(anyhow!(
                "Archive first part does not exist: {}",
                archive_path.display()
            ));
        }

        tokio::fs::create_dir_all(&output_dir).await?;

        let mut job = ExtractionJob {
            group_key: group_id.clone(),
            status: "EXTRACTING".to_string(),
            progress_pct: 0.0,
            archive_path: archive_path.to_string_lossy().to_string(),
            output_dir: output_dir.to_string_lossy().to_string(),
            error: None,
        };
        on_progress(job.clone());

        let mut child = Command::new(&seven_zip)
            .arg("x")
            .arg("-y")
            .arg("-p-")
            .arg("-mmt=on")
            .arg("-bsp1")
            .arg(format!("-o{}", output_dir.display()))
            .arg(&archive_path)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;

        let mut stdout = child.stdout.take();
        let mut stderr = child.stderr.take();

        let mut err_buf = Vec::new();
        let stderr_handle = tokio::spawn(async move {
            if let Some(ref mut se) = stderr {
                let _ = se.read_to_end(&mut err_buf).await;
            }
            String::from_utf8_lossy(&err_buf).to_string()
        });

        if let Some(ref mut so) = stdout {
            let mut buf = [0u8; 512];
            loop {
                let n = so.read(&mut buf).await.unwrap_or(0);
                if n == 0 {
                    break;
                }
                let text = String::from_utf8_lossy(&buf[..n]);
                if let Some(pct) = Self::parse_7z_progress(&text) {
                    if (pct - job.progress_pct).abs() >= 1.0 || pct >= 100.0 {
                        job.progress_pct = pct;
                        on_progress(job.clone());
                    }
                }
            }
        }

        let status = child.wait().await?;
        let stderr_text = stderr_handle.await.unwrap_or_default();

        if status.success() {
            job.status = "SUCCESS".to_string();
            job.progress_pct = 100.0;
            on_progress(job.clone());
            if delete_after_extract {
                for pf in part_files {
                    if pf.exists() {
                        let _ = tokio::fs::remove_file(pf).await;
                    }
                }
            }
            Ok(job)
        } else {
            job.status = "FAILED".to_string();
            job.error = Some(stderr_text.chars().rev().take(500).collect::<String>().chars().rev().collect());
            on_progress(job.clone());
            Err(anyhow!(
                "7-Zip extraction failed: {}",
                job.error.as_deref().unwrap_or("Unknown error")
            ))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_7z_progress_parser() {
        assert_eq!(ArchiveExtractor::parse_7z_progress("  14% 2 - setup.bin"), Some(14.0));
        assert_eq!(ArchiveExtractor::parse_7z_progress(" 42% \r 89% "), Some(89.0));
        assert_eq!(ArchiveExtractor::parse_7z_progress("Everything is Ok"), None);
    }
}
