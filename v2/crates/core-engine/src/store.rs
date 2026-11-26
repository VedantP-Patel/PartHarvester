use crate::models::{AppSettings, DownloadTask, ExtractionJob, TaskStatus};
use parking_lot::Mutex;
use rusqlite::{params, Connection};
use std::collections::HashMap;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

pub struct SqliteStore {
    conn: Mutex<Connection>,
}

impl SqliteStore {
    pub fn open(db_path: &Path) -> rusqlite::Result<Self> {
        if let Some(parent) = db_path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let conn = Connection::open(db_path)?;
        conn.execute_batch(
            "PRAGMA journal_mode = WAL;
             PRAGMA synchronous = NORMAL;
             PRAGMA foreign_keys = ON;
             CREATE TABLE IF NOT EXISTS settings (
                 key TEXT PRIMARY KEY,
                 value_json TEXT NOT NULL
             );
             CREATE TABLE IF NOT EXISTS tasks (
                 id TEXT PRIMARY KEY,
                 original_url TEXT NOT NULL,
                 direct_url TEXT,
                 filename TEXT NOT NULL,
                 file_size INTEGER NOT NULL DEFAULT 0,
                 downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                 status TEXT NOT NULL,
                 destination_dir TEXT NOT NULL,
                 priority INTEGER NOT NULL DEFAULT 0,
                 retries INTEGER NOT NULL DEFAULT 0,
                 supports_range INTEGER NOT NULL DEFAULT 1,
                 error_message TEXT,
                 assigned_adapter TEXT,
                 sort_order INTEGER NOT NULL DEFAULT 0,
                 updated_at REAL NOT NULL
             );
             CREATE TABLE IF NOT EXISTS task_intervals (
                 task_id TEXT NOT NULL,
                 start_byte INTEGER NOT NULL,
                 end_byte INTEGER NOT NULL,
                 PRIMARY KEY (task_id, start_byte),
                 FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
             );
             CREATE TABLE IF NOT EXISTS extraction_jobs (
                 group_key TEXT PRIMARY KEY,
                 status TEXT NOT NULL,
                 progress_pct REAL NOT NULL DEFAULT 0.0,
                 archive_path TEXT NOT NULL,
                 output_dir TEXT NOT NULL,
                 error TEXT
             );",
        )?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    pub fn migrate_legacy_json_if_needed(&self, legacy_json_path: &Path) {
        if !legacy_json_path.exists() {
            return;
        }
        let Ok(raw) = std::fs::read_to_string(legacy_json_path) else {
            return;
        };
        let Ok(val) = serde_json::from_str::<serde_json::Value>(&raw) else {
            return;
        };

        if let Some(s_obj) = val.get("settings") {
            if let Ok(settings) = serde_json::from_value::<AppSettings>(s_obj.clone()) {
                let _ = self.save_settings(&settings);
            }
        }
        if let Some(t_arr) = val.get("tasks").and_then(|v| v.as_array()) {
            let mut tasks = Vec::new();
            for item in t_arr {
                if let Ok(mut t) = serde_json::from_value::<DownloadTask>(item.clone()) {
                    t.segments.clear();
                    t.speed_bytes_sec = 0.0;
                    t.eta_seconds = None;
                    tasks.push(t);
                }
            }
            if !tasks.is_empty() {
                let _ = self.save_tasks(&tasks);
            }
        }

        let migrated_path = legacy_json_path.with_extension("json.migrated");
        let _ = std::fs::rename(legacy_json_path, migrated_path);
    }

    pub fn load_settings(&self) -> AppSettings {
        let conn = self.conn.lock();
        let res: rusqlite::Result<String> = conn.query_row(
            "SELECT value_json FROM settings WHERE key = 'app_settings'",
            [],
            |row| row.get(0),
        );
        match res {
            Ok(json_str) => serde_json::from_str(&json_str).unwrap_or_default(),
            Err(_) => AppSettings::default(),
        }
    }

    pub fn save_settings(&self, settings: &AppSettings) -> rusqlite::Result<()> {
        let json_str = serde_json::to_string(settings).unwrap_or_default();
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO settings (key, value_json) VALUES ('app_settings', ?1)
             ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            params![json_str],
        )?;
        Ok(())
    }

    pub fn load_tasks(&self) -> Vec<DownloadTask> {
        let conn = self.conn.lock();
        let Ok(mut stmt) = conn.prepare(
            "SELECT id, original_url, direct_url, filename, file_size, downloaded_bytes,
                    status, destination_dir, priority, retries, supports_range,
                    error_message, assigned_adapter
             FROM tasks ORDER BY sort_order ASC",
        ) else {
            return Vec::new();
        };
        let rows = stmt.query_map([], |row| {
            let status_str: String = row.get(6)?;
            let status = serde_json::from_str::<TaskStatus>(&format!("\"{status_str}\""))
                .unwrap_or(TaskStatus::Queued);
            Ok(DownloadTask {
                id: row.get(0)?,
                original_url: row.get(1)?,
                direct_url: row.get(2)?,
                filename: row.get(3)?,
                file_size: row.get::<_, i64>(4)? as u64,
                downloaded_bytes: row.get::<_, i64>(5)? as u64,
                status,
                destination_dir: row.get(7)?,
                priority: row.get(8)?,
                retries: row.get::<_, i64>(9)? as u32,
                supports_range: row.get::<_, i64>(10)? != 0,
                error_message: row.get(11)?,
                assigned_adapter: row.get(12)?,
                speed_bytes_sec: 0.0,
                eta_seconds: None,
                segments: Vec::new(),
            })
        });
        match rows {
            Ok(iter) => iter.filter_map(|r| r.ok()).collect(),
            Err(_) => Vec::new(),
        }
    }

    pub fn save_tasks(&self, tasks: &[DownloadTask]) -> rusqlite::Result<()> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);

        let mut conn = self.conn.lock();
        let tx = conn.transaction()?;
        tx.execute("DELETE FROM tasks", [])?;
        {
            let mut stmt = tx.prepare(
                "INSERT INTO tasks (
                    id, original_url, direct_url, filename, file_size, downloaded_bytes,
                    status, destination_dir, priority, retries, supports_range,
                    error_message, assigned_adapter, sort_order, updated_at
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
            )?;
            for (idx, t) in tasks.iter().enumerate() {
                let status_str = serde_json::to_string(&t.status)
                    .unwrap_or_else(|_| "\"QUEUED\"".to_string())
                    .trim_matches('"')
                    .to_string();
                stmt.execute(params![
                    t.id,
                    t.original_url,
                    t.direct_url,
                    t.filename,
                    t.file_size as i64,
                    t.downloaded_bytes as i64,
                    status_str,
                    t.destination_dir,
                    t.priority,
                    t.retries as i64,
                    if t.supports_range { 1i64 } else { 0i64 },
                    t.error_message,
                    t.assigned_adapter,
                    idx as i64,
                    now,
                ])?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    pub fn save_task_intervals(&self, task_id: &str, intervals: &[(u64, u64)]) -> rusqlite::Result<()> {
        let mut conn = self.conn.lock();
        let tx = conn.transaction()?;
        tx.execute("DELETE FROM task_intervals WHERE task_id = ?1", params![task_id])?;
        {
            let mut stmt = tx.prepare(
                "INSERT INTO task_intervals (task_id, start_byte, end_byte) VALUES (?1, ?2, ?3)",
            )?;
            for &(s, e) in intervals {
                stmt.execute(params![task_id, s as i64, e as i64])?;
            }
        }
        tx.commit()?;
        Ok(())
    }

    pub fn load_task_intervals(&self, task_id: &str) -> Vec<(u64, u64)> {
        let conn = self.conn.lock();
        let Ok(mut stmt) = conn.prepare(
            "SELECT start_byte, end_byte FROM task_intervals WHERE task_id = ?1 ORDER BY start_byte ASC",
        ) else {
            return Vec::new();
        };
        let rows = stmt.query_map(params![task_id], |row| {
            Ok((row.get::<_, i64>(0)? as u64, row.get::<_, i64>(1)? as u64))
        });
        match rows {
            Ok(iter) => iter.filter_map(|r| r.ok()).collect(),
            Err(_) => Vec::new(),
        }
    }

    pub fn delete_task_intervals(&self, task_id: &str) -> rusqlite::Result<()> {
        let conn = self.conn.lock();
        conn.execute("DELETE FROM task_intervals WHERE task_id = ?1", params![task_id])?;
        Ok(())
    }

    pub fn save_extraction_job(&self, job: &ExtractionJob) -> rusqlite::Result<()> {
        let conn = self.conn.lock();
        conn.execute(
            "INSERT INTO extraction_jobs (group_key, status, progress_pct, archive_path, output_dir, error)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT(group_key) DO UPDATE SET
               status = excluded.status,
               progress_pct = excluded.progress_pct,
               error = excluded.error",
            params![
                job.group_key,
                job.status,
                job.progress_pct,
                job.archive_path,
                job.output_dir,
                job.error,
            ],
        )?;
        Ok(())
    }

    pub fn load_extraction_jobs(&self) -> HashMap<String, ExtractionJob> {
        let conn = self.conn.lock();
        let Ok(mut stmt) = conn.prepare(
            "SELECT group_key, status, progress_pct, archive_path, output_dir, error FROM extraction_jobs",
        ) else {
            return HashMap::new();
        };
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                ExtractionJob {
                    group_key: row.get(0)?,
                    status: row.get(1)?,
                    progress_pct: row.get(2)?,
                    archive_path: row.get(3)?,
                    output_dir: row.get(4)?,
                    error: row.get(5)?,
                },
            ))
        });
        match rows {
            Ok(iter) => iter.filter_map(|r| r.ok()).collect(),
            Err(_) => HashMap::new(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_sqlite_store_roundtrip_and_migration() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        let legacy_path = dir.path().join("download_session.json");

        std::fs::write(
            &legacy_path,
            r#"{"settings":{"max_concurrency":6,"auto_tune":false},"tasks":[{"id":"t1","original_url":"https://fuckingfast.co/abc#part01.rar","filename":"part01.rar","file_size":1024,"downloaded_bytes":512,"status":"PAUSED","destination_dir":"D:/Downloads","retries":0,"supports_range":true,"priority":0,"segments":[]}]}"#,
        )
        .unwrap();

        let store = SqliteStore::open(&db_path).unwrap();
        store.migrate_legacy_json_if_needed(&legacy_path);

        assert!(!legacy_path.exists());
        assert!(dir.path().join("download_session.json.migrated").exists());

        let settings = store.load_settings();
        assert_eq!(settings.max_concurrency, 6);
        assert!(!settings.auto_tune);

        let tasks = store.load_tasks();
        assert_eq!(tasks.len(), 1);
        assert_eq!(tasks[0].filename, "part01.rar");

        store.save_task_intervals("t1", &[(0, 511)]).unwrap();
        assert_eq!(store.load_task_intervals("t1"), vec![(0, 511)]);
    }
}
