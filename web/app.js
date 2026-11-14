/**
 * PartHarvester Reactive Frontend Application
 * Handles WebSocket telemetry, UI rendering, controls, and settings.
 */

let socket = null;
let reconnectTimer = null;
let currentAdapters = [];
let currentTasks = [];
let searchQuery = '';
let currentSortColumn = '';
let currentSortDirection = 'asc';

// Helpers
function naturalCompare(a, b) {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
}

function formatBytes(bytes, decimals = 2) {
  if (!bytes || bytes <= 0) return '0 B';
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(sizes.length - 1, Math.floor(Math.log(bytes) / Math.log(k)));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

function formatSpeed(bytesPerSec) {
  if (!bytesPerSec || bytesPerSec <= 0) return '0.00 MB/s';
  if (bytesPerSec < 1024 * 1024) {
    return Math.round(bytesPerSec / 1024) + ' KB/s';
  }
  const mbps = bytesPerSec / (1024 * 1024);
  return mbps.toFixed(2) + ' MB/s';
}

function formatEta(seconds) {
  if (!seconds || seconds <= 0 || !Number.isFinite(seconds)) return '--';
  const totalSec = Math.round(seconds);
  if (totalSec < 60) return `${totalSec}s`;
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// WebSocket Connection
function connectWebSocket() {
  const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${wsProtocol}//${window.location.host}/ws`;

  socket = new WebSocket(wsUrl);

  socket.onopen = () => {
    const dot = document.getElementById('ws-dot');
    if (dot) {
      dot.className = 'status-dot connected';
      dot.style.background = '#10b981';
    }
    const statusEl = document.getElementById('ws-status');
    if (statusEl) statusEl.innerText = 'Connected';
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      renderTelemetry(data);
    } catch (e) {
      console.error('Error parsing telemetry payload:', e);
    }
  };

  socket.onclose = () => {
    const dot = document.getElementById('ws-dot');
    if (dot) {
      dot.className = 'status-dot disconnected';
      dot.style.background = '#f43f5e';
    }
    const statusEl = document.getElementById('ws-status');
    if (statusEl) statusEl.innerText = 'Disconnected';
    if (!reconnectTimer) {
      reconnectTimer = setTimeout(connectWebSocket, 2000);
    }
  };

  socket.onerror = (err) => {
    console.error('WebSocket error:', err);
    socket.close();
  };
}

// Render Telemetry
function renderTelemetry(data) {
  const { metrics = {}, adapters = [], tasks = [], extraction_jobs = {}, handoff_events = [] } = data;
  currentAdapters = adapters;
  currentTasks = tasks;

  // Accurate active/queued/completed/failed counts from task list
  let activeFiles = 0;
  let queuedFiles = 0;
  let completedFiles = 0;
  let failedFiles = 0;
  let taskSpeedSum = 0;
  let totalDlBytes = 0;
  let knownSizeBytes = 0;
  let knownCount = 0;

  for (const t of tasks) {
    const sz = Math.max(0, t.file_size || 0);
    const dl = sz > 0 ? Math.min(sz, Math.max(0, t.downloaded_bytes || 0)) : Math.max(0, t.downloaded_bytes || 0);
    totalDlBytes += dl;

    if (sz > 0) {
      knownSizeBytes += sz;
      knownCount += 1;
    }

    if (t.status === 'DOWNLOADING' || t.status === 'RESOLVING') {
      activeFiles += 1;
      if (t.status === 'DOWNLOADING') {
        taskSpeedSum += Math.max(0, t.speed_bytes_sec || 0);
      }
    } else if (t.status === 'QUEUED') {
      queuedFiles += 1;
    } else if (t.status === 'COMPLETED') {
      completedFiles += 1;
    } else if (t.status === 'FAILED') {
      failedFiles += 1;
    }
  }

  const totalStreams = currentAdapters.reduce((sum, a) => sum + (a.active_connections || 0), 0);
  const rawEngineSpeed = Math.max(0, metrics.total_speed_bytes_sec || 0);
  const effectiveSpeed = activeFiles > 0 ? Math.max(rawEngineSpeed, taskSpeedSum) : 0;

  // 1. Top Metrics
  document.getElementById('metric-speed').innerText = formatSpeed(effectiveSpeed);
  document.getElementById('metric-active-count').innerText = totalStreams > 0
    ? `${activeFiles} active · ${totalStreams} streams`
    : `${activeFiles} active`;

  // Estimate total repack size accurately even when queued parts have not resolved file_size yet
  const unknownCount = Math.max(0, tasks.length - knownCount);
  const avgKnownSize = knownCount > 0 ? (knownSizeBytes / knownCount) : 0;
  const estimatedTotalBytes = knownSizeBytes + (unknownCount * avgKnownSize);
  const hasEstimate = unknownCount > 0 && knownCount > 0;

  const totalDlStr = formatBytes(totalDlBytes);
  const totalSzStr = `${hasEstimate ? '~' : ''}${formatBytes(estimatedTotalBytes)}`;
  document.getElementById('metric-downloaded').innerText = `${totalDlStr} / ${totalSzStr}`;

  const pct = estimatedTotalBytes > 0
    ? Math.min(100, (totalDlBytes / estimatedTotalBytes) * 100)
    : 0;
  document.getElementById('metric-progress-fill').style.width = `${pct.toFixed(1)}%`;
  const pctEl = document.getElementById('metric-progress-pct');
  if (pctEl) pctEl.innerText = `${pct.toFixed(1)}%${hasEstimate ? ' EST.' : ''}`;

  document.getElementById('chip-downloading').innerHTML = `<span class="chip-dot"></span>${activeFiles} Active`;
  document.getElementById('chip-queued').innerHTML = `<span class="chip-dot"></span>${queuedFiles} Queued`;
  document.getElementById('chip-completed').innerHTML = `<span class="chip-dot"></span>${completedFiles} Done`;
  document.getElementById('chip-failed').innerHTML = `<span class="chip-dot"></span>${failedFiles} Failed`;

  const queueTotalEl = document.getElementById('total-tasks-count');
  if (queueTotalEl) queueTotalEl.innerText = tasks.length;

  const profileEl = document.getElementById('autotune-profile-badge');
  if (profileEl) {
    if (metrics.auto_tune_profile) {
      profileEl.innerText = metrics.auto_tune_profile;
    } else {
      const activeNets = currentAdapters.filter(a => a.is_enabled && a.is_up);
      const netCount = activeNets.length || 1;
      const totalBwMb = activeNets.reduce((s, a) => s + Math.max(a.ewma_speed_bytes || 0, a.current_speed_bytes || 0), 0) / (1024 * 1024);
      const recFiles = netCount === 1 ? 2 : Math.min(6, netCount * 2);
      const recSegs = netCount === 1 ? 2 : 3;
      const speedTag = totalBwMb > 0.1 ? `${totalBwMb.toFixed(1)} MB/s` : 'probing';
      profileEl.innerText = `Auto: ${recFiles} files × ${recSegs} segs (${netCount} net${netCount > 1 ? 's' : ''}, ${speedTag})`;
    }
  }

  // 2. Hand-off alerts & Bandwidth Share Bar
  renderHandoffAlert(handoff_events);
  renderBandwidthBar(currentAdapters);

  // 3. Adapters List (Combinational Net)
  renderAdapters(currentAdapters);

  // 4. Tasks Table
  renderTasks(tasks, currentAdapters);

  // 5. Extraction Jobs
  renderExtractions(extraction_jobs);
}

function renderHandoffAlert(events) {
  const banner = document.getElementById('handoff-alert-banner');
  const text = document.getElementById('handoff-alert-text');
  if (!banner || !text || !events || events.length === 0) {
    if (banner) banner.style.display = 'none';
    return;
  }

  const latest = events[events.length - 1];
  const now = Date.now() / 1000;
  if (now - latest.timestamp <= 14) {
    banner.style.display = 'flex';
    text.innerHTML = `<strong>TurboBond Hand-Off:</strong> Autonomous takeover of <em>${escapeHtml(latest.filename || 'part')}</em> from <code>${escapeHtml(latest.from_adapter)}</code> ➔ <code>${escapeHtml(latest.to_adapter)}</code>`;
  } else {
    banner.style.display = 'none';
  }
}

function getAdapterIconSvg(friendlyType) {
  const label = String(friendlyType || '');
  if (label.includes('Wi-Fi')) {
    return `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg>`;
  } else if (label.includes('USB') || label.includes('Tether')) {
    return `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>`;
  } else {
    return `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/></svg>`;
  }
}

function renderBandwidthBar(adapters) {
  const container = document.getElementById('bandwidth-share-bar');
  const label = document.getElementById('bandwidth-share-breakdown');
  if (!container || !label) return;

  const active = (adapters || []).filter(a => a.is_enabled && a.is_up);
  if (active.length === 0) {
    container.innerHTML = '<div class="bandwidth-bar-segment" style="width: 100%; background: #374151;"></div>';
    label.innerText = 'No active adapters';
    return;
  }

  const colors = ['#0284c7', '#10b981', '#f59e0b', '#8b5cf6', '#38bdf8'];
  const hasShare = active.some(a => (a.bandwidth_share_pct || 0) > 0);

  let segmentsHtml = '';
  let labelParts = [];
  let allocatedPctSum = 0;

  active.forEach((a, idx) => {
    const color = colors[idx % colors.length];
    let pct;
    if (idx === active.length - 1) {
      pct = Math.max(0, 100 - allocatedPctSum);
    } else {
      pct = hasShare ? Math.round(a.bandwidth_share_pct || 0) : Math.round(100 / active.length);
      allocatedPctSum += pct;
    }
    const slots = a.allocated_slots || 1;
    segmentsHtml += `
      <div class="bandwidth-bar-segment" style="width: ${pct}%; background: ${color};" title="${escapeHtml(a.friendly_type)} (${escapeHtml(a.ip_address)}): ${pct}% share (${slots} slots)"></div>
    `;
    labelParts.push(`
      <span class="legend-badge">
        <span class="legend-dot" style="background: ${color};"></span>
        <span class="legend-name">${escapeHtml(a.friendly_type)}</span>
        <span class="legend-pct tabular" style="color: ${color};">${pct}%</span>
        <span class="legend-slots">(${slots}s)</span>
      </span>
    `);
  });

  container.innerHTML = segmentsHtml;
  label.innerHTML = labelParts.join('');
}

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderAdapters(adapters) {
  const container = document.getElementById('adapters-list');
  if (!container) return;

  if (!adapters || adapters.length === 0) {
    container.innerHTML = '<div class="loading-state">No active network adapters detected.</div>';
    return;
  }

  // Drop any placeholder (loading / empty message) before keyed patching
  container.querySelectorAll(':scope > :not([data-adapter-ip])').forEach(el => el.remove());

  const existing = new Map();
  container.querySelectorAll(':scope > [data-adapter-ip]').forEach(el => {
    existing.set(el.getAttribute('data-adapter-ip'), el);
  });

  const seen = new Set();
  adapters.forEach((a, index) => {
    seen.add(a.ip_address);
    const ip = escapeHtml(a.ip_address);
    const showTypeTag = a.friendly_type && a.friendly_type.toLowerCase() !== String(a.name || '').toLowerCase();
    const curSpeed = a.current_speed_bytes || 0;
    const ewmaSpeed = a.ewma_speed_bytes || 0;
    const showEwma = ewmaSpeed > 0 && Math.abs(ewmaSpeed - curSpeed) > 30 * 1024;

    const html = `
      <div class="adapter-left">
        <label class="toggle-control-mini" title="${a.is_enabled ? 'Disable adapter' : 'Enable adapter'}">
          <input type="checkbox" data-ip="${ip}" ${a.is_enabled ? 'checked' : ''} onchange="toggleAdapter(this.dataset.ip, this.checked)">
          <span class="toggle-slider-mini"></span>
        </label>
        <div class="adapter-icon-frame">
          ${getAdapterIconSvg(a.friendly_type)}
        </div>
        <div class="adapter-meta">
          <div class="adapter-name">
            <span>${escapeHtml(a.name)}</span>
            ${showTypeTag ? `<span class="adapter-type-tag">${escapeHtml(a.friendly_type)}</span>` : ''}
          </div>
          <div class="adapter-ip font-mono">${ip}</div>
        </div>
      </div>
      <div class="adapter-right">
        <div class="adapter-speed-stat tabular font-mono">
          <div class="adapter-speed-val">${formatSpeed(curSpeed)}</div>
          ${showEwma ? `<div class="adapter-speed-ewma">Avg: ${formatSpeed(ewmaSpeed)}</div>` : ''}
        </div>
        <div class="adapter-caps-row font-mono">
          <span class="quota-badge" title="Target stream slots">${a.allocated_slots} Slots</span>
          <span class="conns-badge" title="Active TCP streams">${(a.max_connections || a.max_concurrency_cap) ? `${a.active_connections}/${a.max_connections || a.max_concurrency_cap}` : a.active_connections} Active</span>
        </div>
      </div>`;
    const className = `adapter-item ${a.is_enabled ? 'adapter-enabled' : 'adapter-disabled'}`;
    const sig = className + '|' + html;

    let el = existing.get(a.ip_address);
    if (!el) {
      el = document.createElement('div');
      el.setAttribute('data-adapter-ip', a.ip_address);
    }
    if (el._sig !== sig) {
      el.className = className;
      el.innerHTML = html;
      el._sig = sig;
    }
    if (container.children[index] !== el) {
      container.insertBefore(el, container.children[index] || null);
    }
  });

  existing.forEach((el, ip) => {
    if (!seen.has(ip)) el.remove();
  });
}

function getFilteredAndSortedTasks(tasks) {
  let list = [...(tasks || [])];
  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    list = list.filter(t => (t.filename || '').toLowerCase().includes(q) || (t.status || '').toLowerCase().includes(q));
  }

  const selectVal = document.getElementById('queue-sort-select')?.value || 'natural_part';
  if (currentSortColumn === 'filename' || (!currentSortColumn && selectVal === 'natural_part')) {
    list.sort((a, b) => {
      const res = naturalCompare(a.filename || a.original_url || '', b.filename || b.original_url || '');
      return currentSortDirection === 'desc' ? -res : res;
    });
  } else if (currentSortColumn === 'status' || (!currentSortColumn && selectVal === 'status')) {
    const weights = { DOWNLOADING: 0, RESOLVING: 1, QUEUED: 2, PAUSED: 3, FAILED: 4, COMPLETED: 5 };
    list.sort((a, b) => {
      const res = (weights[a.status] ?? 6) - (weights[b.status] ?? 6);
      return currentSortDirection === 'desc' ? -res : res;
    });
  } else if (currentSortColumn === 'progress' || (!currentSortColumn && selectVal === 'progress')) {
    list.sort((a, b) => {
      const aPct = a.file_size > 0 ? (a.downloaded_bytes / a.file_size) : (a.status === 'COMPLETED' ? 1 : 0);
      const bPct = b.file_size > 0 ? (b.downloaded_bytes / b.file_size) : (b.status === 'COMPLETED' ? 1 : 0);
      return currentSortDirection === 'asc' ? aPct - bPct : bPct - aPct;
    });
  } else if (currentSortColumn === 'size' || (!currentSortColumn && selectVal === 'size')) {
    list.sort((a, b) => currentSortDirection === 'asc' ? (a.file_size || 0) - (b.file_size || 0) : (b.file_size || 0) - (a.file_size || 0));
  } else if (currentSortColumn === 'speed') {
    list.sort((a, b) => currentSortDirection === 'asc' ? (a.speed_bytes_sec || 0) - (b.speed_bytes_sec || 0) : (b.speed_bytes_sec || 0) - (a.speed_bytes_sec || 0));
  }

  return list;
}

function computeTaskEta(t) {
  if (t.status !== 'DOWNLOADING') return '--';
  if (t.eta_seconds && t.eta_seconds > 0) return formatEta(t.eta_seconds);
  if (t.file_size > 0 && t.speed_bytes_sec > 0 && t.file_size > t.downloaded_bytes) {
    return formatEta((t.file_size - t.downloaded_bytes) / t.speed_bytes_sec);
  }
  return '--';
}

function renderTasks(tasks, adapters) {
  const tbody = document.getElementById('tasks-table-body');
  if (!tbody) return;
  const displayTasks = getFilteredAndSortedTasks(tasks);

  if (!displayTasks || displayTasks.length === 0) {
    const emptyKey = searchQuery ? 'search' : 'queue';
    if (tbody.dataset.empty !== emptyKey || !tbody.querySelector('tr:not([data-task-id])')) {
      tbody.dataset.empty = emptyKey;
      tbody.innerHTML = `
      <tr id="empty-queue-row">
        <td colspan="8">
          <div class="empty-state-frame">
            <svg class="empty-svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/>
              <path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>
            </svg>
            <div class="empty-title">${searchQuery ? 'No matching downloads found' : 'Queue is Empty'}</div>
            <div class="empty-subtitle">${searchQuery ? 'Try adjusting your search filter keywords.' : 'Paste a <code>paste.fitgirl-repacks.site</code> URL or download links above to begin multi-part downloading.'}</div>
          </div>
        </td>
      </tr>`;
    }
    return;
  }

  // Always remove any empty-state placeholder rows before rendering active rows
  delete tbody.dataset.empty;
  tbody.querySelectorAll(':scope > tr:not([data-task-id])').forEach(el => el.remove());

  const activeAdapters = (adapters || []).filter(a => a.is_enabled && a.is_up);

  const existingRows = new Map();
  tbody.querySelectorAll(':scope > tr[data-task-id]').forEach(row => {
    existingRows.set(row.getAttribute('data-task-id'), row);
  });

  const wanted = new Set(displayTasks.map(t => String(t.id)));
  existingRows.forEach((row, id) => {
    if (!wanted.has(id)) {
      row.remove();
      existingRows.delete(id);
    }
  });

  displayTasks.forEach((t, index) => {
    const safeDownloaded = t.file_size > 0
      ? Math.min(t.file_size, Math.max(0, t.downloaded_bytes || 0))
      : Math.max(0, t.downloaded_bytes || 0);
    const rawPct = t.file_size > 0
      ? Math.min(100, (safeDownloaded / t.file_size) * 100)
      : (t.status === 'COMPLETED' ? 100 : 0);
    const pct = rawPct.toFixed(1);

    const isDead = t.error_message && (t.error_message.includes('404') || t.error_message.includes('Dead link'));
    const statusLabel = isDead ? 'DEAD LINK' : t.status;
    const id = escapeHtml(t.id);
    const sizeTotalText = t.file_size > 0 ? formatBytes(t.file_size) : 'Pending';
    const etaText = computeTaskEta(t);

    let row = existingRows.get(String(t.id));

    const segCount = t.segments ? t.segments.length : 0;
    const adapterListKey = t.status === 'DOWNLOADING' && activeAdapters.length > 1
      ? activeAdapters.map(a => a.ip_address).join(',')
      : '';
    const structSig = `${t.filename}|${t.status}|${t.assigned_adapter || ''}|${t.error_message || ''}|${segCount}|${adapterListKey}`;
    const activeSelect = row ? row.querySelector('.adapter-hand-select') : null;
    const isSelectFocused = activeSelect && document.activeElement === activeSelect;

    if (row && row._refs && (row._structSig === structSig || isSelectFocused)) {
      row._refs.fill.style.width = `${pct}%`;
      row._refs.pct.textContent = `${pct}%`;
      row._refs.sizeDl.textContent = formatBytes(safeDownloaded);
      row._refs.sizeTotal.textContent = sizeTotalText;
      row._refs.speed.textContent = t.status === 'DOWNLOADING' ? formatSpeed(t.speed_bytes_sec) : '--';
      row._refs.eta.textContent = etaText;
      if (row._refs.segChunks && t.segments) {
        t.segments.forEach((seg, si) => {
          const chunkEl = row._refs.segChunks[si];
          if (!chunkEl) return;
          const segPct = seg.total_bytes > 0 ? Math.min(100, Math.round((seg.downloaded_bytes / seg.total_bytes) * 100)) : (seg.status === 'COMPLETED' ? 100 : 0);
          const color = seg.assigned_adapter && seg.assigned_adapter.includes('Wi-Fi') ? 'var(--accent-cyan)' : 'var(--status-success)';
          chunkEl.style.background = color;
          chunkEl.style.opacity = seg.status === 'COMPLETED' ? '1' : '0.65';
          chunkEl.title = `Segment ${seg.index + 1}: ${segPct}% (${seg.assigned_adapter || 'Auto'})`;
        });
      }
      if (tbody.children[index] !== row) {
        tbody.insertBefore(row, tbody.children[index] || null);
      }
      return;
    }

    let segmentsHtml = '';
    if (t.segments && t.segments.length > 0) {
      segmentsHtml = `
        <div class="segment-bar-container" title="Multi-Segment Parallel Streams (${t.segments.length} segments)">
          ${t.segments.map(seg => {
        const segPct = seg.total_bytes > 0 ? Math.min(100, Math.round((seg.downloaded_bytes / seg.total_bytes) * 100)) : (seg.status === 'COMPLETED' ? 100 : 0);
        const color = seg.assigned_adapter && seg.assigned_adapter.includes('Wi-Fi') ? 'var(--accent-cyan)' : 'var(--status-success)';
        return `<div class="segment-bar-chunk" style="width: ${100 / t.segments.length}%; background: ${color}; opacity: ${seg.status === 'COMPLETED' ? 1 : 0.65};" title="Segment ${seg.index + 1}: ${segPct}% (${escapeHtml(seg.assigned_adapter || 'Auto')})"></div>`;
      }).join('')}
        </div>`;
    }

    const rowHtml = `
        <td class="filename-cell" title="${escapeHtml(t.filename)}">
          <div class="file-name-container">
            <svg class="file-svg-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
            </svg>
            <span class="file-title">${escapeHtml(t.filename)}</span>
          </div>
          ${t.error_message ? `<div class="file-diag-error font-mono" title="${escapeHtml(t.error_message)}">${escapeHtml(t.error_message)}</div>` : ''}
          ${segmentsHtml}
        </td>
        <td class="adapter-select-cell">
          <div class="adapter-bound-label font-mono">${escapeHtml(t.assigned_adapter || 'Auto-Assigned')}</div>
          ${t.status === 'DOWNLOADING' && activeAdapters.length > 1 ? `
            <select class="adapter-hand-select font-mono" onchange="manualHandoff('${id}', this.value); this.value='';">
              <option value="">Switch Net...</option>
              ${activeAdapters.map(ad => `<option value="${escapeHtml(ad.ip_address)}">${escapeHtml(ad.friendly_type)} (${escapeHtml(ad.ip_address)})</option>`).join('')}
            </select>
          ` : ''}
        </td>
        <td>
          <div class="mini-progress-track">
            <div class="mini-progress-fill" style="width: ${pct}%"></div>
          </div>
          <div class="mini-progress-label tabular font-mono">${pct}%</div>
        </td>
        <td class="tabular font-mono size-cell"><span class="size-dl">${formatBytes(safeDownloaded)}</span> <span class="text-dim">/</span> <span class="size-total">${sizeTotalText}</span></td>
        <td class="tabular font-mono speed-cell">${t.status === 'DOWNLOADING' ? formatSpeed(t.speed_bytes_sec) : '--'}</td>
        <td class="tabular font-mono eta-cell text-dim">${etaText}</td>
        <td>
          <span class="chip chip-${escapeHtml(String(t.status).toLowerCase())}">
            <span class="chip-dot"></span>
            ${escapeHtml(statusLabel)}
          </span>
        </td>
        <td class="actions-cell">
          <div class="row-actions-group">
            ${t.status === 'DOWNLOADING' ? `
              <button class="btn-row-action" title="Pause Download" onclick="pauseTask('${id}')">
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
              </button>` : ''}
            ${t.status === 'PAUSED' || t.status === 'FAILED' ? `
              <button class="btn-row-action btn-accent" title="Resume Download" onclick="resumeTask('${id}')">
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"/></svg>
              </button>` : ''}
            <button class="btn-row-action btn-accent" title="Prioritize / Move to Top" onclick="reorderTask('${id}', 'top')">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="18 15 12 9 6 15"/><line x1="6" y1="4" x2="18" y2="4"/></svg>
            </button>
            <button class="btn-row-action" title="Move Up" onclick="reorderTask('${id}', 'up')">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="18 15 12 9 6 15"/></svg>
            </button>
            <button class="btn-row-action" title="Move Down" onclick="reorderTask('${id}', 'down')">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
            </button>
            <button class="btn-row-action" title="Reveal in Windows Explorer" onclick="openTaskFile('${id}')">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
            </button>
            <button class="btn-row-action" title="Copy Download URL" data-url="${escapeHtml(t.original_url)}" onclick="copyLink(this.getAttribute('data-url'))">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            </button>
            <button class="btn-row-action btn-danger" title="Remove Task" onclick="removeTask('${id}')">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>
        </td>`;

    if (!row) {
      row = document.createElement('tr');
      row.setAttribute('data-task-id', t.id);
    }
    row.innerHTML = rowHtml;
    row._structSig = structSig;
    row._refs = {
      fill: row.querySelector('.mini-progress-fill'),
      pct: row.querySelector('.mini-progress-label'),
      sizeDl: row.querySelector('.size-dl'),
      sizeTotal: row.querySelector('.size-total'),
      speed: row.querySelector('.speed-cell'),
      eta: row.querySelector('.eta-cell'),
      segChunks: row.querySelectorAll('.segment-bar-chunk'),
    };
    if (tbody.children[index] !== row) {
      tbody.insertBefore(row, tbody.children[index] || null);
    }
  });
}

function renderExtractions(jobs) {
  const sec = document.getElementById('extraction-section');
  const container = document.getElementById('groups-container');
  if (!sec || !container) return;

  if (!jobs || Object.keys(jobs).length === 0) {
    sec.style.display = 'none';
    return;
  }

  sec.style.display = 'block';
  container.innerHTML = Object.entries(jobs).map(([group, j]) => `
    <div class="extraction-card">
      <div class="extraction-info">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/>
          <path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>
        </svg>
        <div>
          <div class="extraction-group font-mono">${escapeHtml(group)}</div>
          <div class="extraction-dir">${escapeHtml(j.output_dir)}</div>
        </div>
      </div>
      <div>
        <span class="chip chip-${j.status === 'SUCCESS' ? 'completed' : (j.status === 'FAILED' ? 'failed' : 'downloading')}">
          <span class="chip-dot"></span>
          ${j.status === 'EXTRACTING' ? '7-Zip Unpacking...' : escapeHtml(j.status)}
        </span>
      </div>
    </div>
  `).join('');
}

// Toast Notifications System
function showToast(message, type = 'info') {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span class="toast-dot"></span>
    <span class="toast-text">${escapeHtml(message)}</span>
  `;
  container.appendChild(toast);
  requestAnimationFrame(() => {
    toast.classList.add('toast-show');
  });
  setTimeout(() => {
    toast.classList.remove('toast-show');
    setTimeout(() => toast.remove(), 300);
  }, 4200);
}

// API Actions
async function toggleAdapter(ip, enabled) {
  try {
    await fetch('/api/adapters/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ip_address: ip, enabled })
    });
    showToast(`Adapter ${ip} ${enabled ? 'enabled' : 'disabled'}.`, 'info');
  } catch (e) {
    showToast('Failed to toggle adapter: ' + e.message, 'error');
  }
}

async function pauseTask(taskId) {
  await fetch(`/api/pause/${taskId}`, { method: 'POST' });
}

async function resumeTask(taskId) {
  await fetch(`/api/resume/${taskId}`, { method: 'POST' });
}

async function removeTask(taskId) {
  await fetch(`/api/task/${taskId}`, { method: 'DELETE' });
}

async function manualHandoff(taskId, targetIp) {
  if (!targetIp) return;
  try {
    const res = await fetch(`/api/task/${taskId}/handoff`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_ip: targetIp })
    });
    const data = await res.json();
    if (data.status !== 'ok') {
      showToast('Could not switch network adapter: ' + (data.message || 'Adapter unavailable'), 'error');
    } else {
      showToast(`Handoff requested to ${targetIp}`, 'info');
    }
  } catch (e) {
    showToast('Handoff error: ' + e.message, 'error');
  }
}

async function reorderTask(taskId, direction) {
  try {
    currentSortColumn = '';
    document.querySelectorAll('.th-sort-arrow').forEach(a => a.innerText = '');
    await fetch(`/api/queue/reorder/${taskId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction })
    });
    fetchInitialStatus();
  } catch (e) {
    showToast('Reorder failed: ' + e.message, 'error');
  }
}

async function openTaskFile(taskId) {
  try {
    const res = await fetch(`/api/open-file/${taskId}`, { method: 'POST' });
    const data = await res.json();
    if (data.status === 'ok') {
      showToast('Opened in Windows Explorer.', 'info');
    } else {
      showToast('File not downloaded yet or not found on disk.', 'warning');
    }
  } catch (e) {
    showToast('Explorer error: ' + e.message, 'error');
  }
}

async function copyLink(url) {
  try {
    await navigator.clipboard.writeText(url);
    showToast('Link copied to clipboard!', 'success');
  } catch (e) {
    prompt('Download Link:', url);
  }
}

// Queue Toolbar Event Listeners
document.getElementById('btn-open-folder').addEventListener('click', async () => {
  try {
    const res = await fetch('/api/open-folder', { method: 'POST' });
    const data = await res.json();
    if (data.status === 'ok') {
      showToast('Opened download directory in Windows Explorer.', 'info');
    }
  } catch (e) {
    showToast('Could not open folder: ' + e.message, 'error');
  }
});

document.getElementById('btn-clear-completed').addEventListener('click', async () => {
  try {
    const res = await fetch('/api/queue/clear-completed', { method: 'POST' });
    const data = await res.json();
    showToast(`Removed ${data.removed_count} completed tasks from view.`, 'info');
    fetchInitialStatus();
  } catch (e) {
    showToast('Clear failed: ' + e.message, 'error');
  }
});

document.getElementById('btn-clear-all').addEventListener('click', async () => {
  if (!confirm('Are you sure you want to cancel and clear all tasks in the queue?')) return;
  try {
    const res = await fetch('/api/queue/clear-all', { method: 'POST' });
    const data = await res.json();
    showToast(`Cleared entire queue (${data.removed_count} tasks).`, 'info');
    fetchInitialStatus();
  } catch (e) {
    showToast('Clear failed: ' + e.message, 'error');
  }
});

// Search & Filter Input Listeners
const searchInput = document.getElementById('queue-search-input');
const searchClear = document.getElementById('btn-search-clear');
if (searchInput) {
  searchInput.addEventListener('input', (e) => {
    searchQuery = e.target.value.trim();
    if (searchClear) searchClear.style.display = searchQuery ? 'block' : 'none';
    renderTasks(currentTasks, currentAdapters);
  });
}
if (searchClear) {
  searchClear.addEventListener('click', () => {
    if (searchInput) searchInput.value = '';
    searchQuery = '';
    searchClear.style.display = 'none';
    renderTasks(currentTasks, currentAdapters);
  });
}

// Queue Sort Dropdown Listener
const sortSelect = document.getElementById('queue-sort-select');
if (sortSelect) {
  sortSelect.addEventListener('change', async (e) => {
    const val = e.target.value;
    currentSortColumn = '';
    document.querySelectorAll('.th-sort-arrow').forEach(a => a.innerText = '');
    try {
      await fetch('/api/queue/sort', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ criterion: val })
      });
    } catch (err) { }
    renderTasks(currentTasks, currentAdapters);
  });
}

// Interactive Column Header Click Listeners
document.querySelectorAll('.sortable-th').forEach(th => {
  th.addEventListener('click', () => {
    const col = th.getAttribute('data-sort');
    if (currentSortColumn === col) {
      currentSortDirection = currentSortDirection === 'asc' ? 'desc' : 'asc';
    } else {
      currentSortColumn = col;
      currentSortDirection = 'asc';
    }
    document.querySelectorAll('.th-sort-arrow').forEach(a => a.innerText = '');
    const arrowEl = document.getElementById(`sort-arrow-${col}`);
    if (arrowEl) arrowEl.innerText = currentSortDirection === 'asc' ? '▲' : '▼';
    renderTasks(currentTasks, currentAdapters);
  });
});

// Ingest Links Button
document.getElementById('btn-add-links').addEventListener('click', async () => {
  const text = document.getElementById('links-input').value.trim();
  if (!text) {
    showToast('Please paste a paste.fitgirl-repacks.site link or download URL list.', 'warning');
    return;
  }

  const downloadDir = document.getElementById('download-dir').value.trim();
  const autoExtract = document.getElementById('auto-extract-toggle').checked;
  const btn = document.getElementById('btn-add-links');
  const btnSpan = btn.querySelector('span');

  btn.disabled = true;
  if (btnSpan) btnSpan.innerText = 'Ingesting & Resolving...';

  try {
    const res = await fetch('/api/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input_text: text,
        download_dir: downloadDir || undefined,
        auto_extract: autoExtract
      })
    });
    const data = await res.json();
    if (data.count === 0) {
      showToast('No valid download links found in the input. Please verify the URL.', 'warning');
    } else {
      document.getElementById('links-input').value = '';
      showToast(`Successfully queued ${data.count} download parts!`, 'success');
      fetchInitialStatus();
    }
  } catch (e) {
    showToast('Failed to add links: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    if (btnSpan) btnSpan.innerText = 'Ingest Links & Start Multi-Part Download';
  }
});

document.getElementById('btn-pause-all').addEventListener('click', async () => {
  await fetch('/api/pause-all', { method: 'POST' });
  showToast('All downloads paused.', 'info');
});

document.getElementById('btn-resume-all').addEventListener('click', async () => {
  await fetch('/api/resume-all', { method: 'POST' });
  showToast('Resuming all queued downloads.', 'info');
});

document.getElementById('btn-retry-failed').addEventListener('click', async () => {
  await fetch('/api/retry-failed', { method: 'POST' });
  showToast('Retrying failed parts...', 'info');
});

// Comprehensive Settings Modal
const settingsModal = document.getElementById('settings-modal');
document.getElementById('btn-settings-toggle').addEventListener('click', async () => {
  try {
    const res = await fetch('/api/settings');
    const s = await res.json();
    document.getElementById('setting-rd-token').value = s.rd_token || '';
    const autoTuneEl = document.getElementById('setting-auto-tune');
    if (autoTuneEl) {
      autoTuneEl.checked = s.auto_tune !== false;
      const syncManualLock = () => {
        const locked = autoTuneEl.checked;
        ['setting-concurrency', 'setting-segments', 'setting-buffer-size'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.disabled = locked;
        });
      };
      autoTuneEl.onchange = syncManualLock;
      syncManualLock();
    }
    document.getElementById('setting-concurrency').value = s.max_concurrency || 2;
    document.getElementById('setting-segments').value = s.segments_per_file || 2;
    document.getElementById('setting-buffer-size').value = s.buffer_size_kb || 256;
    document.getElementById('setting-speed-limit').value = s.speed_limit_mbps || 0;
    document.getElementById('setting-max-retries').value = s.max_retries || 3;
    document.getElementById('setting-min-segment-size').value = s.min_segment_file_size_mb || 10;
    document.getElementById('setting-bonding-strategy').value = s.bonding_strategy || 'ewma';
    document.getElementById('setting-work-stealing').checked = s.work_stealing_enabled !== false;
    document.getElementById('setting-auto-extract').checked = s.auto_extract !== false;
    document.getElementById('setting-delete-archive').checked = !!s.delete_archive_after_extract;
    document.getElementById('setting-auto-resume').checked = s.auto_resume_on_start !== false;
    document.getElementById('setting-auto-retry').checked = s.auto_retry_failed !== false;
    document.getElementById('setting-default-dir').value = s.download_dir || '';
    settingsModal.style.display = 'flex';
  } catch (e) {
    showToast('Could not load settings: ' + e.message, 'error');
  }
});

document.getElementById('btn-close-settings').addEventListener('click', () => settingsModal.style.display = 'none');
document.getElementById('btn-cancel-settings').addEventListener('click', () => settingsModal.style.display = 'none');

settingsModal.addEventListener('click', (e) => {
  if (e.target === settingsModal) settingsModal.style.display = 'none';
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && settingsModal.style.display === 'flex') {
    settingsModal.style.display = 'none';
  }
});

document.getElementById('btn-save-settings').addEventListener('click', async () => {
  const rdToken = document.getElementById('setting-rd-token').value;
  const concurrency = parseInt(document.getElementById('setting-concurrency').value) || 4;
  const segments = parseInt(document.getElementById('setting-segments').value) || 4;
  const bufferSize = parseInt(document.getElementById('setting-buffer-size').value) || 256;
  const speedLimit = parseFloat(document.getElementById('setting-speed-limit').value) || 0.0;
  const maxRetries = parseInt(document.getElementById('setting-max-retries').value) || 3;
  const minSegmentSize = parseInt(document.getElementById('setting-min-segment-size').value) || 10;
  const bondingStrategy = document.getElementById('setting-bonding-strategy').value;
  const workStealing = document.getElementById('setting-work-stealing').checked;
  const autoExtract = document.getElementById('setting-auto-extract').checked;
  const deleteArchive = document.getElementById('setting-delete-archive').checked;
  const autoResume = document.getElementById('setting-auto-resume').checked;
  const autoRetry = document.getElementById('setting-auto-retry').checked;
  const defaultDir = document.getElementById('setting-default-dir').value;

  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        rd_token: rdToken,
        auto_tune: document.getElementById('setting-auto-tune')?.checked !== false,
        max_concurrency: concurrency,
        segments_per_file: segments,
        buffer_size_kb: bufferSize,
        speed_limit_mbps: speedLimit,
        max_retries: maxRetries,
        min_segment_file_size_mb: minSegmentSize,
        bonding_strategy: bondingStrategy,
        work_stealing_enabled: workStealing,
        auto_extract: autoExtract,
        delete_archive_after_extract: deleteArchive,
        auto_resume_on_start: autoResume,
        auto_retry_failed: autoRetry,
        download_dir: defaultDir
      })
    });
    const mainDirInput = document.getElementById('download-dir');
    if (mainDirInput && defaultDir) mainDirInput.value = defaultDir;
    const mainAutoExtract = document.getElementById('auto-extract-toggle');
    if (mainAutoExtract) mainAutoExtract.checked = autoExtract;
    settingsModal.style.display = 'none';
    showToast('Settings saved successfully.', 'success');
  } catch (e) {
    showToast('Failed to save settings: ' + e.message, 'error');
  }
});

// Hydrate Destination Field & Eager Boot Telemetry
async function hydrateDefaults() {
  try {
    const res = await fetch('/api/settings');
    if (res.ok) {
      const s = await res.json();
      const mainDirInput = document.getElementById('download-dir');
      if (mainDirInput && !mainDirInput.value && s.download_dir) {
        mainDirInput.value = s.download_dir;
      }
      const autoExtractEl = document.getElementById('auto-extract-toggle');
      if (autoExtractEl && typeof s.auto_extract === 'boolean') {
        autoExtractEl.checked = s.auto_extract;
      }
    }
  } catch (e) { }
}

async function fetchInitialStatus() {
  try {
    const res = await fetch('/api/status');
    if (res.ok) {
      const data = await res.json();
      renderTelemetry(data);
    }
  } catch (e) {
    console.warn('Initial telemetry fetch deferred to WebSocket:', e);
  }
}

// Initial Boot Sequence
hydrateDefaults();
fetchInitialStatus();
connectWebSocket();