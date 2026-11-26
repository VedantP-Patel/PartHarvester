<script lang="ts">
  export interface AppSettings {
    download_dir: string;
    auto_tune: boolean;
    max_concurrency: number;
    segments_per_file: number;
    buffer_size_kb: number;
    speed_limit_mbps: number;
    max_retries: number;
    min_segment_file_size_mb: number;
    bonding_strategy: string;
    work_stealing_enabled: boolean;
    auto_extract: boolean;
    delete_archive_after_extract: boolean;
    auto_resume_on_start: boolean;
    auto_retry_failed: boolean;
    rd_token: string;
  }

  interface Props {
    open: boolean;
    settings: AppSettings;
    onClose: () => void;
    onSave: (s: AppSettings) => void;
    onBrowseFolder: () => Promise<string | null>;
  }

  import { untrack } from 'svelte';
  let { open, settings, onClose, onSave, onBrowseFolder }: Props = $props();
  let draft = $state<AppSettings>(untrack(() => ({ ...settings })));
  let activeTab = $state<'accel' | 'bonding' | 'extract' | 'accounts'>('accel');

  $effect(() => {
    if (open) {
      draft = { ...settings };
    }
  });

  function handleKeydown(e: KeyboardEvent) {
    if (open && e.key === 'Escape') {
      onClose();
    }
  }

  async function browse() {
    const picked = await onBrowseFolder();
    if (picked) draft.download_dir = picked;
  }
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
  <div class="modal-backdrop" role="presentation" onclick={(e) => e.target === e.currentTarget && onClose()}>
    <div class="modal-window" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <div class="modal-top">
        <h3 id="settings-title">PartHarvester Engine Settings</h3>
        <button class="modal-close" onclick={onClose} aria-label="Close settings">&times;</button>
      </div>

      <div class="tab-bar" role="tablist">
        <button role="tab" aria-selected={activeTab === 'accel'} class:active={activeTab === 'accel'} onclick={() => activeTab = 'accel'}>
          Acceleration
        </button>
        <button role="tab" aria-selected={activeTab === 'bonding'} class:active={activeTab === 'bonding'} onclick={() => activeTab = 'bonding'}>
          Combinational Net
        </button>
        <button role="tab" aria-selected={activeTab === 'extract'} class:active={activeTab === 'extract'} onclick={() => activeTab = 'extract'}>
          Extraction &amp; Files
        </button>
        <button role="tab" aria-selected={activeTab === 'accounts'} class:active={activeTab === 'accounts'} onclick={() => activeTab = 'accounts'}>
          Accounts
        </button>
      </div>

      <div class="modal-body">
        {#if activeTab === 'accel'}
          <label class="toggle-row">
            <input type="checkbox" bind:checked={draft.auto_tune} />
            <span>Smart Auto-Tune (Recommended)</span>
          </label>
          <div class="grid-2">
            <label class="field">
              <span>Max Simultaneous Parts (1–12)</span>
              <input type="number" min="1" max="12" bind:value={draft.max_concurrency} disabled={draft.auto_tune} />
            </label>
            <label class="field">
              <span>Segments Per File (1–16)</span>
              <input type="number" min="1" max="16" bind:value={draft.segments_per_file} disabled={draft.auto_tune} />
            </label>
            <label class="field">
              <span>I/O Buffer Size (KB)</span>
              <select bind:value={draft.buffer_size_kb} disabled={draft.auto_tune}>
                <option value={64}>64 KB</option>
                <option value={256}>256 KB</option>
                <option value={512}>512 KB</option>
                <option value={1024}>1024 KB</option>
              </select>
            </label>
            <label class="field">
              <span>Global Speed Cap (MB/s, 0 = Unlimited)</span>
              <input type="number" min="0" max="1000" step="0.5" bind:value={draft.speed_limit_mbps} />
            </label>
            <label class="field">
              <span>Max Retries</span>
              <input type="number" min="1" max="10" bind:value={draft.max_retries} />
            </label>
            <label class="field">
              <span>Min Size For Slicing (MB)</span>
              <input type="number" min="1" max="100" bind:value={draft.min_segment_file_size_mb} />
            </label>
          </div>
        {:else if activeTab === 'bonding'}
          <label class="field">
            <span>Adapter Allocation Strategy</span>
            <select bind:value={draft.bonding_strategy}>
              <option value="ewma">Adaptive EWMA Dynamic Throughput</option>
              <option value="equal">Equal Round-Robin</option>
              <option value="primary">Primary Adapter Only</option>
            </select>
          </label>
          <label class="toggle-row">
            <input type="checkbox" bind:checked={draft.work_stealing_enabled} />
            <span>Autonomous Straggler Work-Stealing / Hand-Off</span>
          </label>
        {:else if activeTab === 'extract'}
          <div class="field">
            <span>Default Download Directory</span>
            <div class="dir-row">
              <input type="text" bind:value={draft.download_dir} />
              <button class="btn btn-secondary btn-sm" onclick={browse}>Browse...</button>
            </div>
          </div>
          <label class="toggle-row">
            <input type="checkbox" bind:checked={draft.auto_extract} />
            <span>Auto-Extract Multi-Part Archives (7-Zip)</span>
          </label>
          <label class="toggle-row">
            <input type="checkbox" bind:checked={draft.delete_archive_after_extract} />
            <span>Delete Archives After Successful Extraction</span>
          </label>
          <label class="toggle-row">
            <input type="checkbox" bind:checked={draft.auto_resume_on_start} />
            <span>Auto-Resume Downloads on Startup</span>
          </label>
          <label class="toggle-row">
            <input type="checkbox" bind:checked={draft.auto_retry_failed} />
            <span>Auto-Retry Failed Parts</span>
          </label>
        {:else}
          <label class="field">
            <span>Real-Debrid API Token (Optional)</span>
            <input type="password" bind:value={draft.rd_token} placeholder="Paste Real-Debrid API token..." />
          </label>
        {/if}
      </div>

      <div class="modal-bottom">
        <button class="btn btn-secondary" onclick={onClose}>Cancel</button>
        <button class="btn btn-primary" onclick={() => { onSave(draft); onClose(); }}>Save Settings</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(4, 7, 14, 0.85);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 20px;
  }
  .modal-window {
    background: var(--bg-surface);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-lg);
    width: 100%;
    max-width: 600px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .modal-top {
    padding: 12px 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border-default);
  }
  .modal-top h3 { font-size: 14px; font-weight: 600; }
  .modal-close {
    background: none;
    border: none;
    color: var(--text-secondary);
    font-size: 20px;
    cursor: pointer;
    width: 28px;
    height: 28px;
  }
  .tab-bar {
    display: flex;
    gap: 4px;
    padding: 8px 16px 0;
    border-bottom: 1px solid var(--border-default);
    background: var(--bg-elevated);
  }
  .tab-bar button {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-dim);
    font-size: 12px;
    font-weight: 600;
    padding: 6px 10px;
    cursor: pointer;
    font-family: inherit;
  }
  .tab-bar button.active {
    color: var(--accent-cyan-light);
    border-bottom-color: var(--accent-cyan-light);
  }
  .modal-body {
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-height: 240px;
  }
  .grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .field span {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
  }
  .field input, .field select {
    height: 32px;
    padding: 4px 10px;
    width: 100%;
  }
  .dir-row {
    display: flex;
    gap: 6px;
  }
  .dir-row input { flex: 1; }
  .toggle-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12.5px;
    cursor: pointer;
  }
  .modal-bottom {
    padding: 12px 16px;
    border-top: 1px solid var(--border-default);
    background: var(--bg-elevated);
    display: flex;
    justify-content: flex-end;
    gap: 8px;
  }
</style>
