<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen } from '@tauri-apps/api/event';
  import { open as openDialog } from '@tauri-apps/plugin-dialog';
  import { isPermissionGranted, requestPermission, sendNotification } from '@tauri-apps/plugin-notification';
  import Sparkline from './lib/Sparkline.svelte';
  import SettingsModal, { type AppSettings } from './lib/SettingsModal.svelte';

  interface Segment {
    index: number;
    start_byte: number;
    end_byte: number;
    downloaded_bytes: number;
    status: string;
    assigned_adapter?: string | null;
    speed_bytes_sec: number;
  }

  interface DownloadTask {
    id: string;
    original_url: string;
    direct_url?: string | null;
    filename: string;
    file_size: number;
    downloaded_bytes: number;
    status: string;
    assigned_adapter?: string | null;
    speed_bytes_sec: number;
    eta_seconds?: number | null;
    error_message?: string | null;
    retries: number;
    destination_dir: string;
    segments: Segment[];
  }

  interface NetworkAdapter {
    name: string;
    ip_address: string;
    is_up: boolean;
    has_gateway: boolean;
    friendly_type: string;
    is_enabled: boolean;
    current_speed_bytes: number;
    ewma_speed_bytes: number;
    active_connections: number;
    max_concurrency_cap: number;
    allocated_slots: number;
    bandwidth_share_pct: number;
  }

  interface ExtractionJob {
    group_key: string;
    status: string;
    progress_pct: number;
    archive_path: string;
    output_dir: string;
    error?: string | null;
  }

  interface HandoffEvent {
    timestamp: number;
    task_id: string;
    filename: string;
    from_adapter: string;
    to_adapter: string;
  }

  interface SequenceGroup {
    group_key: string;
    prefix: string;
    extension: string;
    destination_dir: string;
    total_parts: number;
    completed_parts: number;
    is_complete: boolean;
    first_part?: string | null;
    task_ids: string[];
  }

  interface TelemetrySnapshot {
    tasks: DownloadTask[];
    adapters: NetworkAdapter[];
    handoff_events: HandoffEvent[];
    groups: Record<string, SequenceGroup>;
    extraction_jobs: Record<string, ExtractionJob>;
    metrics: {
      total_speed_bytes_sec: number;
      total_downloaded_bytes: number;
      total_size_bytes: number;
      active_downloads: number;
      queued_downloads: number;
      completed_downloads: number;
      failed_downloads: number;
      auto_tune_enabled: boolean;
      auto_tune_profile: string;
    };
    settings: AppSettings;
  }

  const ADAPTER_COLORS = ['#0284c7', '#10b981', '#f59e0b', '#8b5cf6', '#38bdf8', '#f43f5e'];

  let snap = $state<TelemetrySnapshot>({
    tasks: [],
    adapters: [],
    handoff_events: [],
    groups: {},
    extraction_jobs: {},
    metrics: {
      total_speed_bytes_sec: 0,
      total_downloaded_bytes: 0,
      total_size_bytes: 0,
      active_downloads: 0,
      queued_downloads: 0,
      completed_downloads: 0,
      failed_downloads: 0,
      auto_tune_enabled: true,
      auto_tune_profile: 'Auto: Probing...',
    },
    settings: {
      download_dir: '',
      auto_tune: true,
      max_concurrency: 4,
      segments_per_file: 4,
      buffer_size_kb: 256,
      speed_limit_mbps: 0,
      max_retries: 5,
      min_segment_file_size_mb: 10,
      bonding_strategy: 'ewma',
      work_stealing_enabled: true,
      auto_extract: true,
      delete_archive_after_extract: false,
      auto_resume_on_start: true,
      auto_retry_failed: true,
      rd_token: '',
    },
  });

  let linksInput = $state('');
  let downloadDir = $state('');
  let searchQuery = $state('');
  let sortMode = $state('natural_part');
  let sortCol = $state('');
  let sortDir = $state<'asc' | 'desc'>('asc');
  let settingsOpen = $state(false);
  let confirmClearAll = $state(false);
  let confirmDeleteId = $state<string | null>(null);
  let selectedIds = $state<Set<string>>(new Set());
  let collapsedGroups = $state<Set<string>>(new Set());
  let speedHistory = $state<number[]>([]);
  let toasts = $state<Array<{ id: number; msg: string; type: string }>>([]);
  let toastSeq = 0;

  function addToast(msg: string, type = 'info') {
    const id = ++toastSeq;
    toasts = [...toasts, { id, msg, type }];
    setTimeout(() => {
      toasts = toasts.filter((t) => t.id !== id);
    }, 4000);
  }

  function formatBytes(bytes: number): string {
    if (!bytes || bytes <= 0) return '0 B';
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.min(sizes.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    return `${(bytes / 1024 ** i).toFixed(2)} ${sizes[i]}`;
  }

  function formatSpeed(bps: number): string {
    if (!bps || bps <= 0) return '0.00 MB/s';
    if (bps < 1024 * 1024) return `${Math.round(bps / 1024)} KB/s`;
    return `${(bps / (1024 * 1024)).toFixed(2)} MB/s`;
  }

  function formatEta(t: DownloadTask): string {
    if (t.status !== 'DOWNLOADING') return '--';
    let sec = t.eta_seconds ?? 0;
    if ((!sec || sec <= 0) && t.file_size > 0 && t.speed_bytes_sec > 0 && t.file_size > t.downloaded_bytes) {
      sec = (t.file_size - t.downloaded_bytes) / t.speed_bytes_sec;
    }
    if (!sec || sec <= 0 || !Number.isFinite(sec)) return '--';
    const s = Math.round(sec);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    return `${Math.floor(m / 60)}h ${m % 60}m`;
  }

  function adapterColor(ipOrLabel: string | null | undefined): string {
    if (!ipOrLabel) return ADAPTER_COLORS[0];
    const idx = snap.adapters.findIndex((a) => ipOrLabel.includes(a.ip_address));
    if (idx >= 0) return ADAPTER_COLORS[idx % ADAPTER_COLORS.length];
    let hash = 0;
    for (let i = 0; i < ipOrLabel.length; i++) hash = (hash * 31 + ipOrLabel.charCodeAt(i)) | 0;
    return ADAPTER_COLORS[Math.abs(hash) % ADAPTER_COLORS.length];
  }

  const activeAdapters = $derived(snap.adapters.filter((a) => a.is_enabled && a.is_up));
  const totalStreams = $derived(snap.adapters.reduce((sum, a) => sum + (a.active_connections || 0), 0));

  const latestHandoff = $derived.by(() => {
    if (!snap.handoff_events || snap.handoff_events.length === 0) return null;
    const last = snap.handoff_events[snap.handoff_events.length - 1];
    const ageSec = Date.now() / 1000 - last.timestamp;
    return ageSec <= 14 ? last : null;
  });

  const filteredTasks = $derived.by(() => {
    const q = searchQuery.trim().toLowerCase();
    let list = snap.tasks.filter(
      (t) => !q || t.filename.toLowerCase().includes(q) || t.status.toLowerCase().includes(q)
    );
    if (sortCol) {
      list = [...list].sort((a, b) => {
        let cmp = 0;
        if (sortCol === 'filename') cmp = a.filename.localeCompare(b.filename, undefined, { numeric: true });
        else if (sortCol === 'progress') {
          const pa = a.file_size > 0 ? a.downloaded_bytes / a.file_size : a.status === 'COMPLETED' ? 1 : 0;
          const pb = b.file_size > 0 ? b.downloaded_bytes / b.file_size : b.status === 'COMPLETED' ? 1 : 0;
          cmp = pa - pb;
        } else if (sortCol === 'size') cmp = (a.file_size || 0) - (b.file_size || 0);
        else if (sortCol === 'speed') cmp = (a.speed_bytes_sec || 0) - (b.speed_bytes_sec || 0);
        else if (sortCol === 'status') cmp = a.status.localeCompare(b.status);
        return sortDir === 'asc' ? cmp : -cmp;
      });
    }
    return list;
  });

  const groupedTasks = $derived.by(() => {
    const groups = new Map<string, { items: DownloadTask[]; groupKey: string | null; isComplete: boolean }>();
    const seqValues = Object.values(snap.groups || {});
    for (const t of filteredTasks) {
      const m = t.filename.match(/^(.+?)[_.\-]part\d+\./i);
      const label = m ? m[1] : 'Individual Files';
      const seq = seqValues.find((g) => g.task_ids.includes(t.id));
      const existing = groups.get(label) ?? { items: [], groupKey: seq?.group_key ?? null, isComplete: seq?.is_complete ?? false };
      existing.items.push(t);
      groups.set(label, existing);
    }
    return Array.from(groups.entries());
  });

  const sizeStats = $derived.by(() => {
    let knownBytes = 0;
    let knownCount = 0;
    let dlBytes = 0;
    for (const t of snap.tasks) {
      const sz = Math.max(0, t.file_size || 0);
      const dl = sz > 0 ? Math.min(sz, Math.max(0, t.downloaded_bytes || 0)) : Math.max(0, t.downloaded_bytes || 0);
      dlBytes += dl;
      if (sz > 0) {
        knownBytes += sz;
        knownCount += 1;
      }
    }
    const unknownCount = Math.max(0, snap.tasks.length - knownCount);
    const hasEstimate = unknownCount > 0 && knownCount > 0;
    const avgKnown = knownCount > 0 ? knownBytes / knownCount : 0;
    const estTotal = knownBytes + unknownCount * avgKnown;
    const pct = estTotal > 0 ? Math.min(100, (dlBytes / estTotal) * 100) : 0;
    return { dlBytes, estTotal, hasEstimate, pct };
  });

  function toggleSortCol(col: string) {
    if (sortCol === col) {
      sortDir = sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      sortCol = col;
      sortDir = 'asc';
    }
  }

  async function refreshStatus() {
    try {
      const next = await invoke<TelemetrySnapshot>('get_status');
      snap = next;
      speedHistory = [...speedHistory.slice(-59), next.metrics.total_speed_bytes_sec];
    } catch {}
  }

  async function browseFolder(): Promise<string | null> {
    const picked = await openDialog({ directory: true, multiple: false });
    return typeof picked === 'string' ? picked : null;
  }

  async function handleIngest() {
    const text = linksInput.trim();
    if (!text) {
      addToast('Paste a PrivateBin URL or download links first.', 'warning');
      return;
    }
    const count = await invoke<number>('add_links', {
      inputText: text,
      downloadDir: downloadDir.trim() || null,
    });
    if (count > 0) {
      linksInput = '';
      addToast(`Queued ${count} download parts.`, 'success');
      await refreshStatus();
    } else {
      addToast('No valid links found.', 'warning');
    }
  }

  async function toggleAutoExtractInline(checked: boolean) {
    const next = { ...snap.settings, auto_extract: checked };
    await invoke('update_settings', { settings: next });
    await refreshStatus();
  }

  async function copyLink(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      addToast('Download URL copied to clipboard.', 'success');
    } catch {
      addToast('Clipboard copy failed.', 'error');
    }
  }

  async function verifySfvPrompt() {
    const picked = await openDialog({
      multiple: false,
      filters: [{ name: 'Checksum Files', extensions: ['sfv'] }],
    });
    if (typeof picked === 'string') {
      try {
        const res = await invoke<Array<{ filename: string; ok: boolean }>>('verify_sfv', { sfvPath: picked });
        const passed = res.filter((r) => r.ok).length;
        addToast(`SFV Check: ${passed}/${res.length} files OK`, passed === res.length ? 'success' : 'warning');
      } catch (e) {
        addToast(`SFV error: ${e}`, 'error');
      }
    }
  }

  async function bulkAction(action: 'pause' | 'resume' | 'remove' | 'delete_files') {
    for (const id of selectedIds) {
      if (action === 'delete_files') {
        await invoke('remove_task', { taskId: id, deleteFile: true });
      } else {
        await invoke(`${action}_task`, { taskId: id });
      }
    }
    selectedIds = new Set();
    await refreshStatus();
  }

  function toggleSelect(id: string) {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    selectedIds = next;
  }

  function toggleGroup(key: string) {
    const next = new Set(collapsedGroups);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    collapsedGroups = next;
  }

  onMount(() => {
    let prevActive = 0;
    let unlisten: (() => void) | null = null;

    (async () => {
      await refreshStatus();
      if (!downloadDir && snap.settings.download_dir) {
        downloadDir = snap.settings.download_dir;
      }

      try {
        unlisten = await listen<TelemetrySnapshot>('telemetry', async (event) => {
          const next = event.payload;
          if (prevActive > 0 && next.metrics.active_downloads === 0 && next.metrics.completed_downloads > 0) {
            try {
              let granted = await isPermissionGranted();
              if (!granted) {
                const perm = await requestPermission();
                granted = perm === 'granted';
              }
              if (granted) {
                sendNotification({
                  title: 'PartHarvester Complete',
                  body: `Finished ${next.metrics.completed_downloads} parts.`,
                });
              }
            } catch {}
          }
          prevActive = next.metrics.active_downloads;
          snap = next;
          speedHistory = [...speedHistory.slice(-59), next.metrics.total_speed_bytes_sec];
        });
      } catch {}
    })();

    const pollTimer = setInterval(refreshStatus, 400);
    return () => {
      clearInterval(pollTimer);
      if (unlisten) unlisten();
    };
  });
</script>

<div class="app-container">
  <header class="card app-header">
    <div class="header-left">
      <div class="brand-badge" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" />
        </svg>
      </div>
      <div>
        <div class="brand-row">
          <h1>PartHarvester</h1>
          <span class="version-tag font-mono">v2 Native</span>
        </div>
        <p class="subtitle">Cross-Platform Multi-Part Downloader &amp; Combinational Net</p>
      </div>
    </div>

    <div class="header-right">
      <div class="live-speed tabular font-mono">
        <span class="speed-val">{formatSpeed(snap.metrics.total_speed_bytes_sec)}</span>
        <span class="text-dim">|</span>
        <span>{snap.metrics.active_downloads} active{totalStreams > 0 ? ` · ${totalStreams} streams` : ''}</span>
      </div>
      <div class="conn-pill font-mono">
        <span class="conn-dot"></span>
        <span>Engine Live</span>
      </div>
      <button class="btn btn-secondary" onclick={() => settingsOpen = true}>Settings</button>
    </div>
  </header>

  {#if latestHandoff}
    <div class="handoff-banner">
      <strong>TurboBond Hand-Off:</strong>
      <span>{latestHandoff.filename}</span>
      <code>{latestHandoff.from_adapter}</code> ➔ <code>{latestHandoff.to_adapter}</code>
    </div>
  {/if}

  <div class="top-grid">
    <section class="card bonding-card">
      <div class="section-head">
        <h2>Combinational Net &amp; Telemetry</h2>
        <span class="profile-tag font-mono">{snap.metrics.auto_tune_profile}</span>
      </div>

      <div class="meter-box">
        <div class="meter-top">
          <span>Total Progress</span>
          <span class="tabular font-mono">{formatBytes(sizeStats.dlBytes)} / {sizeStats.hasEstimate ? '~' : ''}{formatBytes(sizeStats.estTotal)} ({sizeStats.pct.toFixed(1)}%{sizeStats.hasEstimate ? ' EST.' : ''})</span>
        </div>
        <div class="progress-track">
          <div class="progress-fill" style="width: {sizeStats.pct}%"></div>
        </div>
      </div>

      <div class="meter-box">
        <div class="meter-top">
          <span>Bandwidth Quota Allocation</span>
          <div class="share-legend font-mono">
            {#if activeAdapters.length === 0}
              <span class="text-dim">No active adapters</span>
            {:else}
              {#each activeAdapters as ad, i (ad.ip_address)}
                {@const pct = ad.bandwidth_share_pct > 0 ? Math.round(ad.bandwidth_share_pct) : Math.round(100 / activeAdapters.length)}
                <span class="legend-item">
                  <span class="ad-color" style="background: {ADAPTER_COLORS[i % ADAPTER_COLORS.length]}"></span>
                  {ad.friendly_type} {pct}% ({ad.allocated_slots}s)
                </span>
              {/each}
            {/if}
          </div>
        </div>
        <div class="progress-track bar-flex">
          {#each activeAdapters as ad, i (ad.ip_address)}
            {@const pct = ad.bandwidth_share_pct > 0 ? ad.bandwidth_share_pct : 100 / activeAdapters.length}
            <div style="width: {pct}%; background: {ADAPTER_COLORS[i % ADAPTER_COLORS.length]}; height: 100%;"></div>
          {/each}
        </div>
      </div>

      <div class="sparkline-box">
        <div class="meter-top">
          <span class="spark-label">60s Combined Throughput</span>
          <span class="font-mono text-dim">Peak: {formatSpeed(Math.max(0, ...speedHistory))}</span>
        </div>
        <Sparkline points={speedHistory} color="#38bdf8" height={44} />
      </div>

      <div class="adapters-list">
        {#each snap.adapters as ad, idx (ad.ip_address)}
          <div class="adapter-row" class:disabled={!ad.is_enabled}>
            <div class="ad-left">
              <input
                type="checkbox"
                checked={ad.is_enabled}
                onchange={(e) => invoke('toggle_adapter', { ipAddress: ad.ip_address, enabled: (e.currentTarget as HTMLInputElement).checked })}
              />
              <span class="ad-color" style="background: {ADAPTER_COLORS[idx % ADAPTER_COLORS.length]}"></span>
              <div>
                <div class="ad-name">{ad.name} <span class="ad-type">{ad.friendly_type}</span></div>
                <div class="ad-ip font-mono text-dim">{ad.ip_address} {ad.has_gateway ? '' : '(No Gateway)'}</div>
              </div>
            </div>
            <div class="ad-right tabular font-mono">
              <div class="ad-speed">{formatSpeed(ad.current_speed_bytes)}</div>
              <div class="ad-ewma text-dim">
                {ad.ewma_speed_bytes > 0 ? `Avg: ${formatSpeed(ad.ewma_speed_bytes)}` : '\u00A0'}
              </div>
              <div class="ad-badges">
                <span class="slot-badge">{ad.allocated_slots} Slots</span>
                <span class="conn-badge">{ad.active_connections}/{ad.max_concurrency_cap} Active</span>
              </div>
            </div>
          </div>
        {/each}
      </div>
    </section>

    <section class="card ingest-card">
      <h2>Add Download Links or PrivateBin Paste</h2>
      <textarea
        bind:value={linksInput}
        rows="4"
        placeholder="Paste paste.fitgirl-repacks.site URL or multi-part link list..."
      ></textarea>
      <div class="dir-extract-row">
        <div class="dir-row flex-1">
          <input type="text" bind:value={downloadDir} placeholder="Download destination..." />
          <button class="btn btn-secondary" onclick={async () => { const p = await browseFolder(); if (p) downloadDir = p; }}>Browse</button>
        </div>
        <label class="inline-toggle">
          <input type="checkbox" checked={snap.settings.auto_extract} onchange={(e) => toggleAutoExtractInline((e.currentTarget as HTMLInputElement).checked)} />
          <span>Auto-Extract (7z)</span>
        </label>
      </div>
      <button class="btn btn-primary ingest-btn" onclick={handleIngest}>
        Ingest Links &amp; Start Multi-Part Download
      </button>
    </section>
  </div>

  {#if Object.keys(snap.extraction_jobs).length > 0}
    <section class="card">
      <h2>Archive Unpacking</h2>
      <div class="extract-list">
        {#each Object.values(snap.extraction_jobs) as job (job.group_key)}
          <div class="extract-row">
            <div>
              <div class="font-mono">{job.group_key}</div>
              <div class="text-dim">{job.output_dir}</div>
            </div>
            <div class="extract-right">
              <div class="progress-track small-track">
                <div class="progress-fill" style="width: {job.progress_pct}%"></div>
              </div>
              <span class="chip chip-{job.status === 'SUCCESS' ? 'completed' : job.status === 'FAILED' ? 'failed' : 'downloading'}">
                <span class="chip-dot"></span>
                {job.status} {job.status === 'EXTRACTING' ? `${job.progress_pct.toFixed(0)}%` : ''}
              </span>
            </div>
          </div>
        {/each}
      </div>
    </section>
  {/if}

  <section class="card queue-card">
    <div class="queue-header-bar">
      <h2>Downloads Queue (<span class="tabular">{snap.tasks.length}</span> parts)</h2>
      <div class="queue-chips">
        <span class="chip chip-downloading"><span class="chip-dot"></span>{snap.metrics.active_downloads} Active</span>
        <span class="chip chip-queued"><span class="chip-dot"></span>{snap.metrics.queued_downloads} Queued</span>
        <span class="chip chip-completed"><span class="chip-dot"></span>{snap.metrics.completed_downloads} Done</span>
        <span class="chip chip-failed"><span class="chip-dot"></span>{snap.metrics.failed_downloads} Failed</span>
      </div>
    </div>

    <div class="queue-toolbar">
      <div class="toolbar-left">
        <input type="text" bind:value={searchQuery} placeholder="Filter parts..." class="search-input" />
        <select bind:value={sortMode} onchange={() => { sortCol = ''; invoke('sort_queue', { criterion: sortMode }); }}>
          <option value="natural_part">Part # (01 → 99)</option>
          <option value="status">Status</option>
          <option value="progress">Progress %</option>
          <option value="size">File Size</option>
        </select>
        {#if selectedIds.size > 0}
          <span class="chip chip-downloading">{selectedIds.size} selected</span>
          <button class="btn btn-secondary btn-sm" onclick={() => bulkAction('pause')}>Pause</button>
          <button class="btn btn-secondary btn-sm" onclick={() => bulkAction('resume')}>Resume</button>
          <button class="btn btn-secondary btn-sm btn-danger" onclick={() => bulkAction('remove')}>Remove</button>
          <button class="btn btn-secondary btn-sm btn-danger" title="Remove selected and delete local files" onclick={() => bulkAction('delete_files')}>Del Files</button>
        {/if}
      </div>
      <div class="toolbar-right">
        <button class="btn btn-secondary btn-sm" onclick={() => invoke('pause_all').then(refreshStatus)}>Pause All</button>
        <button class="btn btn-secondary btn-sm" onclick={() => invoke('resume_all').then(refreshStatus)}>Resume All</button>
        <button class="btn btn-secondary btn-sm" onclick={() => invoke('retry_failed').then(refreshStatus)}>Retry Failed</button>
        <button class="btn btn-secondary btn-sm" onclick={verifySfvPrompt}>Verify SFV</button>
        <button class="btn btn-secondary btn-sm" onclick={() => invoke('open_folder', { folderPath: null })}>Open Folder</button>
        <button class="btn btn-secondary btn-sm" onclick={() => invoke('clear_completed').then(refreshStatus)}>Clear Done</button>
        {#if confirmClearAll}
          <button class="btn btn-secondary btn-sm" onclick={() => { invoke('clear_all', { deleteFiles: false }).then(refreshStatus); confirmClearAll = false; }}>Queue Only</button>
          <button class="btn btn-secondary btn-sm btn-danger" onclick={() => { invoke('clear_all', { deleteFiles: true }).then(() => { refreshStatus(); addToast('Cleared queue and deleted local files.', 'info'); }); confirmClearAll = false; }}>Clear + Del Files</button>
          <button class="btn btn-secondary btn-sm" onclick={() => confirmClearAll = false}>Cancel</button>
        {:else}
          <button class="btn btn-secondary btn-sm btn-danger" onclick={() => confirmClearAll = true}>Clear All</button>
        {/if}
      </div>
    </div>

    <div class="table-wrap">
      <table class="tasks-table">
        <thead>
          <tr>
            <th class="col-chk"></th>
            <th class="sortable" onclick={() => toggleSortCol('filename')}>Part / File {sortCol === 'filename' ? (sortDir === 'asc' ? '▲' : '▼') : ''}</th>
            <th class="col-ad">Adapter / Streams</th>
            <th class="col-prog sortable" onclick={() => toggleSortCol('progress')}>Progress {sortCol === 'progress' ? (sortDir === 'asc' ? '▲' : '▼') : ''}</th>
            <th class="col-sz sortable" onclick={() => toggleSortCol('size')}>Size {sortCol === 'size' ? (sortDir === 'asc' ? '▲' : '▼') : ''}</th>
            <th class="col-spd sortable" onclick={() => toggleSortCol('speed')}>Speed {sortCol === 'speed' ? (sortDir === 'asc' ? '▲' : '▼') : ''}</th>
            <th class="col-eta">ETA</th>
            <th class="col-st sortable" onclick={() => toggleSortCol('status')}>Status {sortCol === 'status' ? (sortDir === 'asc' ? '▲' : '▼') : ''}</th>
            <th class="col-act">Actions</th>
          </tr>
        </thead>
        <tbody>
          {#each groupedTasks as [groupName, groupData] (groupName)}
            <tr class="group-row">
              <td colspan="9">
                <div class="group-header">
                  <span role="button" tabindex="0" onclick={() => toggleGroup(groupName)} onkeydown={(e) => e.key === 'Enter' && toggleGroup(groupName)}>
                    {collapsedGroups.has(groupName) ? '▸' : '▾'} {groupName} ({groupData.items.length} parts)
                  </span>
                  <div class="group-actions">
                    <span class="font-mono text-dim">{groupData.items.filter((t) => t.status === 'COMPLETED').length}/{groupData.items.length} done</span>
                    {#if groupData.groupKey && groupData.isComplete}
                      <button class="btn btn-secondary btn-sm" onclick={() => invoke('manual_extract', { groupKey: groupData.groupKey }).then(() => addToast('Extraction started.', 'info'))}>Extract</button>
                    {/if}
                  </div>
                </div>
              </td>
            </tr>
            {#if !collapsedGroups.has(groupName)}
              {#each groupData.items as t (t.id)}
                {@const pct = t.file_size > 0 ? Math.min(100, (t.downloaded_bytes / t.file_size) * 100) : (t.status === 'COMPLETED' ? 100 : 0)}
                {@const isDead = t.error_message && (t.error_message.includes('404') || t.error_message.includes('Dead link'))}
                <tr>
                  <td class="col-chk"><input type="checkbox" checked={selectedIds.has(t.id)} onchange={() => toggleSelect(t.id)} /></td>
                  <td class="col-file">
                    <div class="file-name" title={t.filename}>{t.filename}</div>
                    {#if t.segments.length > 0}
                      <div class="seg-bar">
                        {#each t.segments as seg (seg.index)}
                          <div class="seg-chunk" style="width: {100 / t.segments.length}%; background: {adapterColor(seg.assigned_adapter)}; opacity: {seg.status === 'COMPLETED' ? 1 : 0.65}"></div>
                        {/each}
                      </div>
                    {/if}
                    {#if t.error_message}
                      <div class="err-msg font-mono" title={t.error_message}>{t.error_message}</div>
                    {/if}
                  </td>
                  <td class="col-ad font-mono text-dim">
                    <div>{t.assigned_adapter ?? 'Queued'}</div>
                    {#if t.status === 'DOWNLOADING' && activeAdapters.length > 1}
                      <select class="handoff-select" onchange={(e) => { const v = (e.currentTarget as HTMLSelectElement).value; if (v) { invoke('handoff_task', { taskId: t.id, targetIp: v }).then(refreshStatus); (e.currentTarget as HTMLSelectElement).value = ''; } }}>
                        <option value="">Switch Net...</option>
                        {#each activeAdapters as ad (ad.ip_address)}
                          <option value={ad.ip_address}>{ad.friendly_type} ({ad.ip_address})</option>
                        {/each}
                      </select>
                    {/if}
                  </td>
                  <td class="col-prog tabular font-mono">{pct.toFixed(1)}%</td>
                  <td class="col-sz tabular font-mono">{formatBytes(t.downloaded_bytes)} / {t.file_size > 0 ? formatBytes(t.file_size) : 'Pending'}</td>
                  <td class="col-spd tabular font-mono">{t.status === 'DOWNLOADING' ? formatSpeed(t.speed_bytes_sec) : '--'}</td>
                  <td class="col-eta tabular font-mono text-dim">{formatEta(t)}</td>
                  <td class="col-st">
                    <span class="chip chip-{t.status.toLowerCase()}"><span class="chip-dot"></span>{isDead ? 'DEAD LINK' : t.status}</span>
                  </td>
                  <td class="col-act">
                    {#if t.status === 'DOWNLOADING'}
                      <button class="btn btn-secondary btn-row" title="Pause" onclick={() => invoke('pause_task', { taskId: t.id }).then(refreshStatus)}>Pause</button>
                    {:else if t.status === 'PAUSED' || t.status === 'FAILED'}
                      <button class="btn btn-secondary btn-row" title="Resume" onclick={() => invoke('resume_task', { taskId: t.id }).then(refreshStatus)}>Resume</button>
                    {/if}
                    <button class="btn btn-secondary btn-row" title="Move to Top" onclick={() => invoke('reorder_task', { taskId: t.id, direction: 'top' }).then(refreshStatus)}>⤒</button>
                    <button class="btn btn-secondary btn-row" title="Move Up" onclick={() => invoke('reorder_task', { taskId: t.id, direction: 'up' }).then(refreshStatus)}>↑</button>
                    <button class="btn btn-secondary btn-row" title="Move Down" onclick={() => invoke('reorder_task', { taskId: t.id, direction: 'down' }).then(refreshStatus)}>↓</button>
                    <button class="btn btn-secondary btn-row" title="Copy Link" onclick={() => copyLink(t.original_url)}>URL</button>
                    <button class="btn btn-secondary btn-row" title="Reveal File" onclick={() => invoke('reveal_task_file', { taskId: t.id })}>File</button>
                    {#if confirmDeleteId === t.id}
                      <button class="btn btn-secondary btn-row btn-danger" title="Confirm delete file from disk" onclick={() => { invoke('remove_task', { taskId: t.id, deleteFile: true }).then(() => { refreshStatus(); addToast(`Deleted ${t.filename}`, 'info'); }); confirmDeleteId = null; }}>Del?</button>
                      <button class="btn btn-secondary btn-row" title="Cancel" onclick={() => confirmDeleteId = null}>No</button>
                    {:else}
                      <button class="btn btn-secondary btn-row btn-danger" title="Remove from queue (keep file)" onclick={() => invoke('remove_task', { taskId: t.id, deleteFile: false }).then(refreshStatus)}>&times;</button>
                      <button class="btn btn-secondary btn-row btn-danger" title="Delete task &amp; file from disk" onclick={() => confirmDeleteId = t.id}>Del</button>
                    {/if}
                  </td>
                </tr>
              {/each}
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
  </section>
</div>

<SettingsModal
  open={settingsOpen}
  settings={snap.settings}
  onClose={() => settingsOpen = false}
  onSave={(s) => invoke('update_settings', { settings: s }).then(refreshStatus)}
  onBrowseFolder={browseFolder}
/>

<div class="toast-stack">
  {#each toasts as toast (toast.id)}
    <div class="toast">{toast.msg}</div>
  {/each}
</div>

<style>
  .app-header { display: flex; justify-content: space-between; align-items: center; }
  .header-left { display: flex; align-items: center; gap: 10px; }
  .brand-badge { width: 32px; height: 32px; display: flex; align-items: center; justify-content: center; border-radius: 8px; background: var(--accent-cyan-tint); color: var(--accent-cyan-light); border: 1px solid var(--border-accent); font-weight: 700; }
  .brand-row { display: flex; align-items: center; gap: 8px; }
  h1 { font-size: 17px; }
  h2 { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
  .version-tag { font-size: 10.5px; color: var(--accent-cyan-light); background: var(--accent-cyan-tint); border: 1px solid var(--border-accent); padding: 1px 6px; border-radius: 4px; }
  .subtitle { font-size: 11.5px; color: var(--text-dim); }
  .header-right { display: flex; align-items: center; gap: 8px; }
  .live-speed { background: var(--bg-elevated); border: 1px solid var(--border-default); border-radius: 9999px; padding: 4px 12px; display: flex; gap: 8px; font-size: 12px; min-width: 245px; justify-content: center; }
  .speed-val { color: var(--accent-cyan-light); font-weight: 700; min-width: 78px; text-align: right; }
  .conn-pill { display: flex; align-items: center; gap: 6px; font-size: 11px; background: var(--bg-elevated); border: 1px solid var(--border-default); padding: 4px 10px; border-radius: 9999px; }
  .conn-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent-emerald-light); }
  .handoff-banner { background: var(--bg-elevated); border: 1px solid var(--accent-amber); border-radius: var(--radius-md); padding: 6px 12px; display: flex; align-items: center; gap: 8px; font-size: 12px; }
  .top-grid { display: grid; grid-template-columns: 55fr 45fr; gap: 10px; }
  .bonding-card, .ingest-card { display: flex; flex-direction: column; gap: 8px; }
  .section-head { display: flex; justify-content: space-between; align-items: center; }
  .profile-tag { font-size: 11px; color: var(--accent-cyan-light); }
  .meter-box, .sparkline-box { background: var(--bg-elevated); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 7px 10px; }
  .meter-top { display: flex; justify-content: space-between; align-items: center; font-size: 11px; margin-bottom: 5px; flex-wrap: wrap; gap: 6px; }
  .share-legend { display: flex; gap: 8px; flex-wrap: wrap; font-size: 11px; }
  .legend-item { display: inline-flex; align-items: center; gap: 4px; }
  .bar-flex { display: flex; }
  .spark-label { font-size: 10.5px; color: var(--text-dim); text-transform: uppercase; }
  .progress-track { height: 5px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; }
  .progress-fill { height: 100%; background: var(--accent-cyan); transition: width 0.2s ease; }
  .adapters-list { display: flex; flex-direction: column; gap: 6px; }
  .adapter-row { display: flex; justify-content: space-between; align-items: center; background: var(--bg-elevated); border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: 6px 10px; }
  .adapter-row.disabled { opacity: 0.45; }
  .ad-left { display: flex; align-items: center; gap: 8px; }
  .ad-color { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .ad-name { font-weight: 600; font-size: 12px; }
  .ad-type { font-size: 10.5px; background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
  .ad-ip { font-size: 11px; }
  .ad-right { text-align: right; font-size: 11px; display: flex; flex-direction: column; align-items: flex-end; gap: 2px; min-width: 120px; }
  .ad-speed { color: var(--accent-cyan-light); font-weight: 700; font-size: 12px; }
  .ad-ewma { font-size: 10.5px; line-height: 14px; min-height: 14px; }
  .ad-badges { display: flex; gap: 4px; }
  .slot-badge { background: var(--accent-emerald-tint); color: var(--accent-emerald-light); padding: 1px 5px; border-radius: 3px; font-size: 10px; }
  .conn-badge { background: var(--accent-cyan-tint); color: var(--accent-cyan-light); padding: 1px 5px; border-radius: 3px; font-size: 10px; }
  textarea { padding: 8px 10px; font-family: var(--font-mono); resize: vertical; flex: 1; }
  .dir-extract-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .flex-1 { flex: 1; }
  .dir-row { display: flex; gap: 6px; }
  .dir-row input { flex: 1; height: 32px; padding: 0 10px; }
  .inline-toggle { display: flex; align-items: center; gap: 6px; font-size: 11.5px; font-weight: 600; cursor: pointer; white-space: nowrap; }
  .ingest-btn { width: 100%; justify-content: center; height: 34px; }
  .queue-card { display: flex; flex-direction: column; }
  .queue-header-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; flex-wrap: wrap; gap: 8px; }
  .queue-chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .queue-toolbar { display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
  .toolbar-left, .toolbar-right { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .search-input, select { height: 28px; padding: 0 8px; }
  .table-wrap { max-height: calc(100vh - 480px); min-height: 220px; overflow-y: auto; overflow-x: hidden; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); }
  .tasks-table { width: 100%; table-layout: fixed; border-collapse: separate; border-spacing: 0; font-size: 12px; }
  .tasks-table th { position: sticky; top: 0; background: var(--bg-elevated); padding: 6px 8px; text-align: left; font-size: 11px; text-transform: uppercase; color: var(--text-secondary); border-bottom: 1px solid var(--border-default); z-index: 5; white-space: nowrap; overflow: hidden; }
  .sortable { cursor: pointer; user-select: none; }
  .sortable:hover { color: var(--accent-cyan-light); }
  .tasks-table td { padding: 5px 8px; border-bottom: 1px solid var(--border-subtle); vertical-align: middle; }
  .group-row td { padding: 0; }
  .group-header { display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; background: var(--bg-elevated); border-bottom: 1px solid var(--border-default); font-weight: 600; font-size: 12px; }
  .group-header span[role="button"] { cursor: pointer; }
  .group-actions { display: flex; align-items: center; gap: 8px; }
  .col-chk { width: 28px; }
  .col-ad { width: 150px; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .handoff-select { width: 100%; height: 22px; font-size: 10.5px; margin-top: 2px; padding: 0 4px; }
  .col-prog { width: 64px; }
  .col-sz { width: 140px; font-size: 11px; white-space: nowrap; }
  .col-spd { width: 82px; color: var(--accent-cyan-light); white-space: nowrap; }
  .col-eta { width: 58px; white-space: nowrap; }
  .col-st { width: 108px; }
  .col-act { width: 298px; text-align: right; white-space: nowrap; }
  .btn-row { height: 24px; padding: 2px 6px; font-size: 11px; border-radius: var(--radius-sm); }
  .file-name { font-weight: 600; width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .seg-bar { display: flex; height: 3px; gap: 2px; margin-top: 3px; }
  .seg-chunk { height: 100%; border-radius: 1px; }
  .err-msg { color: var(--accent-rose-light); font-size: 11px; margin-top: 2px; }
  .extract-list { display: flex; flex-direction: column; gap: 6px; }
  .extract-row { display: flex; justify-content: space-between; align-items: center; background: var(--bg-elevated); padding: 8px 10px; border-radius: var(--radius-md); }
  .extract-right { display: flex; align-items: center; gap: 10px; }
  .small-track { width: 120px; }
  .toast-stack { position: fixed; bottom: 16px; right: 16px; display: flex; flex-direction: column; gap: 6px; z-index: 2000; }
  .toast { background: var(--bg-elevated); border: 1px solid var(--border-default); padding: 8px 12px; border-radius: var(--radius-md); font-size: 12px; }
</style>
