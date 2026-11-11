# 📡 PartHarvester API & WebSocket Protocol Reference

PartHarvester exposes a RESTful API and a real-time WebSocket telemetry stream for local web clients and automation scripts.

Base URL: `http://localhost:5000`

---

## 1. REST API Endpoints

### 1.1 Ingest & Add Links
**`POST /api/add`**

Adds a PrivateBin URL or raw list of links to the download queue.

- **Request Body**:
  ```json
  {
    "input_text": "https://paste.fitgirl-repacks.site/?99e460e2438a1459#36CYUjqDBywuhLVUUoEbSyT6RaWpC7bLkE2qDHTuKrpFV",
    "download_dir": "D:\\Downloads\\PartHarvester",
    "auto_extract": true
  }
  ```
- **Response**:
  ```json
  {
    "status": "ok",
    "count": 10,
    "tasks": ["a1b2c3d4", "e5f6g7h8", ...]
  }
  ```

---

### 1.2 Get Application Status
**`GET /api/status`**

Returns overall metrics, task queue, active adapters, and extraction jobs.

- **Response**:
  ```json
  {
    "tasks": [
      {
        "id": "a1b2c3d4",
        "original_url": "https://fuckingfast.co/part01.rar",
        "filename": "game_part01.rar",
        "file_size": 2147483648,
        "downloaded_bytes": 1073741824,
        "status": "DOWNLOADING",
        "assigned_adapter": "Wi-Fi (10.225.0.236)",
        "speed_bytes_sec": 5242880.0,
        "eta_seconds": 204
      }
    ],
    "adapters": [
      {
        "name": "Wi-Fi",
        "ip_address": "10.225.0.236",
        "friendly_type": "Wi-Fi",
        "is_enabled": true,
        "current_speed_bytes": 5242880.0
      }
    ],
    "metrics": {
      "total_speed_bytes_sec": 15728640.0,
      "total_downloaded_bytes": 5368709120,
      "total_size_bytes": 21474836480,
      "active_downloads": 3,
      "queued_downloads": 7,
      "completed_downloads": 0,
      "failed_downloads": 0
    }
  }
  ```

---

### 1.3 Queue Controls
- **`POST /api/pause/{task_id}`**: Pauses a specific download task.
- **`POST /api/resume/{task_id}`**: Resumes a paused or failed task.
- **`POST /api/pause-all`**: Pauses all active downloads.
- **`POST /api/resume-all`**: Resumes all paused downloads.
- **`POST /api/retry-failed`**: Re-queues all failed downloads.
- **`DELETE /api/task/{task_id}`**: Cancels and deletes a download item.

---

### 1.4 Network Adapter Toggling & Dynamic Hand-off
**`POST /api/adapters/toggle`**

Enables or disables an interface for Combinational Net downloading.

- **Request Body**:
  ```json
  {
    "ip_address": "192.168.42.129",
    "enabled": true
  }
  ```

**`POST /api/task/{task_id}/handoff`**

Manually switches an active download to a specific network adapter with seamless HTTP Range resumption.

- **Request Body**:
  ```json
  {
    "target_ip": "192.168.42.129"
  }
  ```
- **Response**:
  ```json
  {
    "status": "ok"
  }
  ```

---

### 1.5 Application Settings
- **`GET /api/settings`**: Retrieves current settings.
- **`POST /api/settings`**: Updates settings.
  - **Request Body**:
    ```json
    {
      "download_dir": "D:\\Downloads\\Games",
      "max_concurrency": 5,
      "auto_extract": true,
      "rd_token": "YOUR_REAL_DEBRID_TOKEN"
    }
    ```

---

### 1.6 Manual Extraction Trigger
**`POST /api/extract/{group_prefix}`**

Triggers 7-Zip unpacking for a completed multi-part archive group.

---

## 2. WebSocket Telemetry Stream

**Endpoint:** `ws://localhost:5000/ws`

The server broadcasts live telemetry packets every **300ms** to all connected clients.

### Broadcast Message Schema:
```json
{
  "tasks": [...],
  "adapters": [
    {
      "name": "Wi-Fi",
      "ip_address": "10.225.0.236",
      "friendly_type": "Wi-Fi",
      "is_enabled": true,
      "current_speed_bytes": 5242880.0,
      "ewma_speed_bytes": 5500000.0,
      "active_connections": 2,
      "max_concurrency_cap": 3,
      "allocated_slots": 2,
      "bandwidth_share_pct": 35.5
    }
  ],
  "handoff_events": [
    {
      "timestamp": 1793963400.12,
      "task_id": "a1b2c3d4",
      "filename": "Phantom_Blade_Zero.part05.rar",
      "from_adapter": "Wi-Fi (10.225.0.236)",
      "to_adapter": "USB Tethering (192.168.42.129)"
    }
  ],
  "metrics": {
    "total_speed_bytes_sec": 24500000.0,
    "total_downloaded_bytes": 12884901888,
    "total_size_bytes": 25769803776,
    "active_downloads": 4,
    "queued_downloads": 6,
    "completed_downloads": 5,
    "failed_downloads": 0
  },
  "extraction_jobs": {
    "Phantom_Blade_Zero_-_part": {
      "status": "EXTRACTING",
      "archive_path": "D:\\Downloads\\part01.rar",
      "output_dir": "D:\\Downloads\\Phantom_Blade_Zero"
    }
  }
}
```

