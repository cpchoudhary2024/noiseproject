// Global variables
let uploadedFilepath = null;
let currentAnalysis = null;
let currentStandards = null;

// Sensor placement — controls whether WHO indoor bedroom limits are evaluated.
// 'outdoor' (default) omits them; 'indoor' includes them.
let deploymentEnvironment = 'outdoor';

// Temporal filtration state
let currentFilters = { exclusions: [], bound_start: null, bound_end: null };

// Staged files list (for multi-file workflow)
let stagedFiles = [];

// Metadata for all files in the current batch (name, rows, start, end)
let batchFileMeta = [];

// DOM Elements
let dropzone = null;
let fileInput = null;
let errorMessage = null;
let proceedBtn = null;
let uploadAnotherBtn = null;
let newAnalysisBtn = null;
let statusText = null;
let statusIndicator = null;

function initApp() {
    dropzone = document.getElementById('dropzone');
    fileInput = document.getElementById('fileInput');
    errorMessage = document.getElementById('error-message');
    proceedBtn = document.getElementById('proceedBtn');
    uploadAnotherBtn = document.getElementById('uploadAnotherBtn');
    newAnalysisBtn = document.getElementById('newAnalysisBtn');
    statusText = document.getElementById('status-text');
    statusIndicator = document.getElementById('status-indicator');

    // Event Listeners (guarded so the page never hard-crashes on load)
    if (dropzone && fileInput) {
        dropzone.addEventListener('click', () => fileInput.click());
        dropzone.addEventListener('dragover', handleDragOver);
        dropzone.addEventListener('dragleave', handleDragLeave);
        dropzone.addEventListener('drop', handleDrop);
    }

    if (fileInput) fileInput.addEventListener('change', handleFileSelect);
    if (proceedBtn) proceedBtn.addEventListener('click', showFilterPanel);
    if (uploadAnotherBtn) uploadAnotherBtn.addEventListener('click', resetToUpload);
    if (newAnalysisBtn) newAnalysisBtn.addEventListener('click', resetToUpload);

    // "Add More Files" input — available from preview and filter sections
    const addMoreInput = document.getElementById('addMoreInput');
    if (addMoreInput) addMoreInput.addEventListener('change', handleAddMoreFiles);

    // Tab switching (updated for new HTML structure)
    document.querySelectorAll('.tab-btn').forEach(button => {
        button.addEventListener('click', () => switchTab(button));
    });

    // Advanced visualization buttons
    document.querySelectorAll('.viz-button').forEach(button => {
        button.addEventListener('click', () => handleVizButtonClick(button));
    });

    setStatus('idle', 'Ready');
}

// Global error handlers: surface the real problem instead of "nothing loads".
window.addEventListener('error', (e) => {
    console.error('Uncaught error:', e.error || e.message);
    showError('Frontend error: ' + (e.message || 'Unknown error'));
    setStatus('error', 'Frontend error');
});

window.addEventListener('unhandledrejection', (e) => {
    console.error('Unhandled rejection:', e.reason);
    showError('Frontend error: ' + (e.reason?.message || String(e.reason || 'Unknown error')));
    setStatus('error', 'Frontend error');
});

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initApp);
} else {
    initApp();
}

// File Upload Handlers
function handleDragOver(e) {
    e.preventDefault();
    if (dropzone) dropzone.classList.add('dragover');
}

function handleDragLeave(e) {
    e.preventDefault();
    if (dropzone) dropzone.classList.remove('dragover');
}

function handleDrop(e) {
    e.preventDefault();
    if (dropzone) dropzone.classList.remove('dragover');
    const files = e.dataTransfer.files;
    if (files.length > 1) {
        _stageFiles(files);
    } else if (files.length === 1) {
        _doSingleUpload(files[0]);
    }
}

function handleFileSelect(e) {
    // Convert to Array BEFORE resetting value — FileList is a live reference
    // and some browsers clear it in-place when input.value is set to ''
    const allFiles = Array.from(e.target.files || []);
    e.target.value = '';   // allow re-selecting the same file later

    if (allFiles.length === 0) return;

    if (allFiles.length > 1) {
        _stageFiles(allFiles);
    } else {
        _doSingleUpload(allFiles[0]);
    }
}

// ── File Staging (multi-file only) ───────────────────────────────────────────

function _stageFiles(fileList) {
    if (!fileList || fileList.length === 0) return;
    if (errorMessage) errorMessage.style.display = 'none';

    // Deduplicate by name+size
    Array.from(fileList).forEach(f => {
        const isDuplicate = stagedFiles.some(s => s.name === f.name && s.size === f.size);
        if (!isDuplicate) stagedFiles.push(f);
    });

    _renderStagedFileList();
}

function _renderStagedFileList() {
    const area  = document.getElementById('stagedFilesArea');
    const list  = document.getElementById('stagedFileList');
    const badge = document.getElementById('stagedCountBadge');
    const label = document.getElementById('uploadStagedLabel');
    if (!area || !list || !badge || !label) return;

    if (stagedFiles.length === 0) {
        area.style.display = 'none';
        return;
    }

    area.style.display = 'block';
    badge.textContent  = stagedFiles.length;
    label.textContent  = `Upload & Merge ${stagedFiles.length} Files`;

    list.innerHTML = stagedFiles.map((f, i) => `
        <li class="staged-file-item">
            <span class="staged-file-name">${escapeHtml(f.name)}</span>
            <span class="staged-file-size">${formatBytes(f.size)}</span>
            <button class="staged-file-remove" onclick="removeStagedFile(${i})" title="Remove">✕</button>
        </li>`).join('');
}

function removeStagedFile(index) {
    stagedFiles.splice(index, 1);
    _renderStagedFileList();
}

function clearStagedFiles() {
    stagedFiles = [];
    _renderStagedFileList();
}

function uploadStagedFiles() {
    if (stagedFiles.length === 0) { showError('No files staged for merge.'); return; }
    _doMultiUpload(stagedFiles);
}

// ── Actual Upload Network Calls ───────────────────────────────────────────────

async function _safeJson(response) {
    const text = await response.text();
    try {
        return JSON.parse(text);
    } catch {
        if (response.status === 413 || text.toLowerCase().includes('request entity too large') || text.toLowerCase().includes('payload too large')) {
            throw new Error('The file is larger than the hosted server accepts in one request (32 MB). Convert it to Parquet with tools/convert_to_parquet.py, or run the platform locally.');
        }
        throw new Error(`Server returned an unexpected response (HTTP ${response.status}). Check your connection and try again.`);
    }
}

// Real progress poller — hits /api/progress/<jobId> every 400 ms and shows
// the actual percentage the backend has reported at real code checkpoints.
function _pollProgress(jobId, label) {
    let stopped = false;
    const iv = setInterval(async () => {
        if (stopped) return;
        try {
            const r = await fetch(`/api/progress/${jobId}`);
            const d = await r.json();
            if (!stopped) setStatus('processing', `${label} ${d.pct}%`);
        } catch { /* network blip — just wait for next tick */ }
    }, 400);
    return { done() { stopped = true; clearInterval(iv); } };
}

// XHR-based upload with real byte-level progress events.
function _xhrUpload(url, formData, { onProgress, onSuccess, onError }) {
    const xhr = new XMLHttpRequest();
    xhr.upload.addEventListener('progress', e => {
        if (e.lengthComputable) onProgress(Math.round(e.loaded / e.total * 100));
    });
    xhr.addEventListener('load', () => {
        const text = xhr.responseText;
        try {
            onSuccess(JSON.parse(text));
        } catch {
            if (xhr.status === 413 || text.toLowerCase().includes('request entity too large') || text.toLowerCase().includes('payload too large')) {
                onError(new Error('The file is larger than the hosted server accepts in one request (32 MB). Convert it to Parquet with tools/convert_to_parquet.py, or run the platform locally.'));
            } else {
                onError(new Error(`Server returned an unexpected response (HTTP ${xhr.status}). Check your connection and try again.`));
            }
        }
    });
    xhr.addEventListener('error', () => onError(new Error('Network error during upload')));
    xhr.open('POST', url);
    xhr.send(formData);
}

function _doSingleUpload(file) {
    if (errorMessage) errorMessage.style.display = 'none';
    window.mergeGapReport = null;
    setStatus('processing', 'Uploading file… 0%');

    const formData = new FormData();
    formData.append('file', file);

    _xhrUpload('/api/upload', formData, {
        onProgress: pct => setStatus('processing', `Uploading file… ${pct}%`),
        onSuccess: data => {
            if (data.success) {
                uploadedFilepath = data.filepath;
                batchFileMeta = [{
                    name: file.name,
                    rows: data.rows,
                    start: data.start_date || null,
                    end: data.end_date || null,
                }];
                displayPreview(data);
                renderBatchFilesPanel();
                showSection('preview-section');
                setStatus('idle', 'File uploaded — ' + data.rows.toLocaleString() + ' records');
            } else {
                showError(data.error || 'Upload failed');
                setStatus('error', 'Upload failed');
            }
        },
        onError: err => {
            showError('Error uploading file: ' + err.message);
            setStatus('error', 'Upload error');
        }
    });
}

function _doMultiUpload(files) {
    if (errorMessage) errorMessage.style.display = 'none';
    setStatus('processing', `Merging ${files.length} files… 0%`);

    const formData = new FormData();
    files.forEach(f => formData.append('files', f));

    _xhrUpload('/api/upload-multi', formData, {
        onProgress: pct => setStatus('processing', `Merging ${files.length} files… ${pct}%`),
        onSuccess: data => {
            if (data.success) {
                uploadedFilepath      = data.filepath;
                window.mergeGapReport = data.gap_analysis || null;
                stagedFiles = [];
                _renderStagedFileList();
                batchFileMeta = (data.file_details || []).map(f => ({
                    name: f.name,
                    rows: f.rows,
                    start: f.start || null,
                    end: f.end || null,
                }));
                displayPreview(data);
                renderBatchFilesPanel();
                showSection('preview-section');
                const gapCount = data.gap_analysis?.gap_count || 0;
                const uptime   = data.gap_analysis?.uptime_pct ?? 100;
                setStatus('idle',
                    `${data.file_count} files merged — ${data.rows.toLocaleString()} records · uptime ${uptime}%` +
                    (gapCount ? ` · ${gapCount} gap(s)` : '')
                );
            } else {
                showError(data.error || 'Multi-file merge failed');
                setStatus('error', 'Merge failed');
            }
        },
        onError: err => {
            showError('Error merging files: ' + err.message);
            setStatus('error', 'Upload error');
        }
    });
}

// Aliases for backward compatibility
function handleFileUpload(file) { _doSingleUpload(file); }
function handleMultiFileUpload(files) { _doMultiUpload(Array.from(files)); }

// ── Add More Files (from preview / filter sections) ───────────────────────────

function addMoreFiles() {
    const input = document.getElementById('addMoreInput');
    if (input) input.click();
}

function handleAddMoreFiles(e) {
    const newFiles = Array.from(e.target.files || []);
    e.target.value = '';
    if (!newFiles.length) return;

    if (errorMessage) errorMessage.style.display = 'none';
    setStatus('processing', `Adding ${newFiles.length} file(s)… 0%`);

    const formData = new FormData();
    newFiles.forEach(f => formData.append('files', f));
    if (uploadedFilepath) formData.append('existing_filepath', uploadedFilepath);

    _xhrUpload('/api/upload-multi', formData, {
        onProgress: pct => setStatus('processing', `Adding ${newFiles.length} file(s)… ${pct}%`),
        onSuccess: data => {
            if (data.success) {
                uploadedFilepath      = data.filepath;
                window.mergeGapReport = data.gap_analysis || null;
                batchFileMeta = (data.file_details || []).map(f => ({
                    name: f.name,
                    rows: f.rows,
                    start: f.start || null,
                    end: f.end || null,
                }));
                displayPreview(data);
                renderBatchFilesPanel();
                showSection('preview-section');
                const gapCount = data.gap_analysis?.gap_count || 0;
                const uptime   = data.gap_analysis?.uptime_pct ?? 100;
                setStatus('idle',
                    `Batch updated — ${data.rows.toLocaleString()} total records across ${data.file_count} file(s)` +
                    (gapCount ? ` · ${gapCount} gap(s) detected` : ` · uptime ${uptime}%`)
                );
            } else {
                showError(data.error || 'Failed to add files to batch');
                setStatus('error', 'Merge failed');
            }
        },
        onError: err => {
            showError('Error adding files: ' + err.message);
            setStatus('error', 'Upload error');
        }
    });
}

// ── Batch Files Panel ─────────────────────────────────────────────────────────

function renderBatchFilesPanel() {
    const panel  = document.getElementById('batchFilesPanel');
    const list   = document.getElementById('batchFilesList');
    const badge  = document.getElementById('batchTotalBadge');
    const addBtn = document.getElementById('previewAddMoreBtn');
    if (!panel || !list) return;

    if (!batchFileMeta || batchFileMeta.length === 0) {
        panel.style.display = 'none';
        if (addBtn) addBtn.style.display = 'none';
        return;
    }

    panel.style.display = 'block';
    if (addBtn) addBtn.style.display = 'inline-flex';

    const totalRows = batchFileMeta.reduce((s, f) => s + (f.rows || 0), 0);
    if (badge) badge.textContent = `${batchFileMeta.length} file${batchFileMeta.length > 1 ? 's' : ''} · ${totalRows.toLocaleString()} total records`;

    const fmtDate = iso => {
        if (!iso) return '—';
        const d = new Date(iso);
        return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) +
               ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    };

    const fmtDuration = (start, end) => {
        if (!start || !end) return '—';
        const ms = new Date(end) - new Date(start);
        if (ms <= 0) return '—';
        const days  = Math.floor(ms / 86400000);
        const hours = Math.floor((ms % 86400000) / 3600000);
        const mins  = Math.floor((ms % 3600000) / 60000);
        if (days > 0)  return `${days}d ${hours}h ${mins}m`;
        if (hours > 0) return `${hours}h ${mins}m`;
        return `${mins}m`;
    };

    list.innerHTML = batchFileMeta.map((f, i) => `
        <div class="batch-file-row">
            <div class="batch-file-left">
                <span class="batch-file-index">${i + 1}</span>
                <span class="batch-file-name">${escapeHtml(f.name)}</span>
            </div>
            <div class="batch-file-stats">
                <span class="batch-stat-pill">${(f.rows || 0).toLocaleString()} readings</span>
                <span class="batch-stat-pill">${fmtDuration(f.start, f.end)}</span>
                <span class="batch-stat-range">${fmtDate(f.start)} → ${fmtDate(f.end)}</span>
            </div>
        </div>`).join('');
}

function displayPreview(data) {
    document.getElementById('preview-rows').textContent = data.rows;
    document.getElementById('preview-cols').textContent = data.columns.length;
    
    // Display table
    const tableContainer = document.getElementById('previewTable');
    const table = document.getElementById('dataPreviewTable');
    
    // Create header
    let html = '<thead><tr>';
    data.columns.forEach(col => {
        html += `<th>${escapeHtml(col)}</th>`;
    });
    html += '</tr></thead><tbody>';
    
    // Create rows
    data.preview.forEach(row => {
        html += '<tr>';
        data.columns.forEach(col => {
            const value = row[col] !== null ? row[col] : 'N/A';
            html += `<td>${escapeHtml(String(value))}</td>`;
        });
        html += '</tr>';
    });
    html += '</tbody>';
    
    table.innerHTML = html;
    tableContainer.style.display = 'block';
}

function runAnalysis() {
    if (!uploadedFilepath) {
        showError('No file uploaded');
        return;
    }
    
    const _analysisJobId = Math.random().toString(36).slice(2, 10);
    setStatus('processing', 'Running analysis… 0%');
    showSection('analysis-section');
    const _analysisTicker = _pollProgress(_analysisJobId, 'Running analysis…');

    fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            filepath: uploadedFilepath,
            job_id: _analysisJobId,
            environment: deploymentEnvironment,
            filters: _activeFilters(),
        })
    })
    .then(response => response.json())
    .then(data => {
        _analysisTicker.done();
        if (data.success) {
            currentAnalysis = data.analysis;
            currentStandards = data.standards;
            window.timestampIntegrity = data.timestamp_integrity || { status: 'ok', time_metrics_valid: true };
            window.filterSummary = data.filter_summary || null;
            window.keyFindings = data.key_findings || {};
            window.complianceMatrix = data.compliance_matrix || [];
            // Display plain-English summary if returned by server
            displayPlainEnglishSummary(data.plain_english_summary || '');
            try {
                displayResults();
                showSection('results-section');
                setStatus('idle', 'Analysis complete');
            } catch (e) {
                console.error('Results rendering failed:', e);
                showError('Analysis succeeded, but results could not be displayed: ' + e.message);
                setStatus('error', 'Display error');
                showSection('preview-section');
            }
        } else if (data.error_kind === 'temporal_filter') {
            // The date range could not be honoured. Send the user back to the
            // filter screen with the reason, rather than analysing the whole
            // file and letting them believe the filter worked.
            showError(data.error);
            setStatus('error', 'Date range not applied');
            showSection('preview-section');
        } else {
            const details = data.traceback || data.hint || '';
            const msg = 'Analysis error: ' + (data.error || 'Unknown error') + (details ? '\n\n' + details : '');
            showError(msg);
            setStatus('error', 'Analysis failed');
            console.error('Analysis failed payload:', data);
        }
    })
    .catch(error => {
        _analysisTicker.done();
        showError('Error running analysis: ' + error.message);
        setStatus('error', 'Analysis error');
        console.error('Analysis request failed:', error);
    });
}

// ============================================================================
// Temporal Filtration Panel
// ============================================================================

function showFilterPanel() {
    if (!uploadedFilepath) { showError('No file uploaded'); return; }

    // Reset filter state (null-guard every element — section may not yet be in DOM on first call)
    currentFilters = { exclusions: [], bound_start: null, bound_end: null };
    const startEl   = document.getElementById('filter-bound-start');
    const endEl     = document.getElementById('filter-bound-end');
    const listEl    = document.getElementById('exclusionList');
    const previewBar = document.getElementById('filterPreviewBar');
    if (startEl)    startEl.value = startEl.dataset.extent = '';
    if (endEl)      endEl.value   = endEl.dataset.extent   = '';
    if (listEl)     listEl.innerHTML    = '';
    if (previewBar) previewBar.style.display = 'none';

    showSection('filter-section');

    // Fetch and display the dataset date range
    fetch('/api/get-data-date-range', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filepath: uploadedFilepath }),
    })
    .then(r => r.json())
    .then(data => {
        const bar  = document.getElementById('filterInfoBar');
        const text = document.getElementById('filterInfoText');
        const rows = document.getElementById('filterInfoRows');
        if (!bar || !text || !rows) return;
        if (data.success) {
            const fmt = iso => new Date(iso).toLocaleString();
            text.textContent = `Dataset spans: ${fmt(data.start)} → ${fmt(data.end)}`;
            rows.textContent = `${data.total_rows.toLocaleString()} rows`;
            bar.style.display = 'flex';

            // Pre-fill bounds with actual data extents (user can narrow them)
            const toLocalDT = iso => {
                const d = new Date(iso);
                const pad = n => String(n).padStart(2, '0');
                return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
            };
            const sEl = document.getElementById('filter-bound-start');
            const eEl = document.getElementById('filter-bound-end');
            // Remember the pre-filled extents: a bound left at the dataset's own
            // edge restricts nothing and must not be sent as a filter.
            if (sEl) sEl.value = sEl.dataset.extent = toLocalDT(data.start);
            if (eEl) eEl.value = eEl.dataset.extent = toLocalDT(data.end);
        }
    })
    .catch(err => console.warn('Date range fetch failed:', err));
}

function addExclusionRow() {
    const list = document.getElementById('exclusionList');
    if (!list) return;
    const idx  = list.children.length;
    const row  = document.createElement('div');
    row.className = 'exclusion-row';
    row.dataset.idx = idx;
    row.innerHTML = `
        <div class="filter-field">
            <label>Exclude From</label>
            <input type="datetime-local" class="filter-input excl-start" step="1"
                   placeholder="Start of bad window">
        </div>
        <div class="filter-field">
            <label>Exclude Until</label>
            <input type="datetime-local" class="filter-input excl-end" step="1"
                   placeholder="End of bad window">
        </div>
        <button class="btn-remove-exclusion" onclick="this.closest('.exclusion-row').remove()">✕ Remove</button>
    `;
    list.appendChild(row);
}

function _collectFilters() {
    const filters = { exclusions: [], bound_start: null, bound_end: null };
    const startEl = document.getElementById('filter-bound-start');
    const endEl   = document.getElementById('filter-bound-end');
    const s = startEl ? startEl.value : '';
    const e = endEl   ? endEl.value   : '';
    if (s && s !== startEl.dataset.extent) filters.bound_start = s;
    if (e && e !== endEl.dataset.extent)   filters.bound_end   = e;

    document.querySelectorAll('.exclusion-row').forEach(row => {
        const start = row.querySelector('.excl-start')?.value;
        const end   = row.querySelector('.excl-end')?.value;
        if (start && end) filters.exclusions.push({ start, end });
    });
    return filters;
}

async function previewFilters() {
    if (!uploadedFilepath) return;
    const filters = _collectFilters();
    const btn = document.getElementById('previewFilterBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Checking…'; }

    try {
        const resp = await fetch('/api/validate-filters', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filepath: uploadedFilepath, filters }),
        });
        const data = await resp.json();
        const bar  = document.getElementById('filterPreviewBar');
        const text = document.getElementById('filterPreviewText');
        if (!bar || !text) return;
        if (data.success) {
            text.textContent = `Filter preview: ${data.retained_rows.toLocaleString()} of ${data.total_rows.toLocaleString()} rows retained (${data.retained_pct}%). ${data.dropped_rows.toLocaleString()} rows will be dropped.`;
            bar.style.display = 'block';
        }
    } catch (err) {
        console.error('Filter preview failed:', err);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Preview Row Count'; }
    }
}

// The filters to send with a request, or null when none were set.
function _activeFilters() {
    const f = currentFilters;
    return (f && (f.exclusions.length || f.bound_start || f.bound_end)) ? f : null;
}

function applyFiltersAndAnalyze() {
    currentFilters = _collectFilters();
    runAnalysis();
}

function skipFilters() {
    currentFilters = { exclusions: [], bound_start: null, bound_end: null };
    runAnalysis();
}

function safeToFixed(value, decimals = 2, fallback = 'N/A') {
    const num = Number(value);
    if (!Number.isFinite(num)) return fallback;
    return num.toFixed(decimals);
}

// ============================================================================
// New Health Assessment Rendering Functions
// ============================================================================

// At-a-glance key findings — the headline numbers a reader cares about,
// shown nowhere else. Detailed per-channel statistics live in the Statistics tab.
function renderMetricsPanel() {
    const container = document.getElementById('metricsGrid');
    if (!container) return;

    const kf = window.keyFindings || {};
    const timeValid = !(window.timestampIntegrity && window.timestampIntegrity.time_metrics_valid === false);
    const num = v => Number.isFinite(Number(v));
    const fmt = v => num(v) ? Number(v).toFixed(1) : '—';
    const hh = h => `${String(h).padStart(2, '0')}:00–${String((Number(h) + 1) % 24).padStart(2, '0')}:00`;

    // [label, value, unit, qualifier]. Guideline values are stated as numbers
    // for context; the verdicts themselves are in the Compliance matrix.
    const items = [['LAeq, whole record', fmt(kf.avg_laeq), 'dB(A)', 'Energy average of all LEQ readings']];
    if (timeValid && num(kf.lden)) {
        items.push(['Lden', fmt(kf.lden), 'dB(A)', 'WHO 2018 road-traffic guideline value: 53']);
    }
    if (timeValid && num(kf.lnight)) {
        items.push(['Lnight', fmt(kf.lnight), 'dB(A)', 'WHO 2018 road-traffic guideline value: 45']);
    }
    if (timeValid && num(kf.loudest_hour)) {
        items.push(['Highest hourly LAeq', fmt(kf.loudest_hour_db), 'dB(A)',
                    `${hh(kf.loudest_hour)}, all days pooled`]);
    }
    if (timeValid && num(kf.quietest_hour)) {
        items.push(['Lowest hourly LAeq', fmt(kf.quietest_hour_db), 'dB(A)',
                    `${hh(kf.quietest_hour)}, all days pooled`]);
    }
    if (num(kf.peak)) {
        items.push(kf.peak_is_lmax
            ? ['LAmax', fmt(kf.peak), 'dB(A)', 'Highest reading on the L-Max channel']
            : ['Highest LEQ reading', fmt(kf.peak), 'dB(A)', 'No L-Max channel in this file']);
    }

    container.innerHTML = `<dl class="headline-grid">${items.map(([label, value, unit, note]) => `
        <div class="headline-item">
            <dt>${escapeHtml(label)}</dt>
            <dd><span class="headline-value">${escapeHtml(value)}</span> <span class="headline-unit">${escapeHtml(unit)}</span></dd>
            <dd class="headline-note">${escapeHtml(note)}</dd>
        </div>`).join('')}</dl>`;
}

// ============================================================================
// MODULE 6 — Data Continuity Log
// ============================================================================

function renderGapAnalysis(gapData) {
    const section = document.getElementById('gapLogSection');
    const content = document.getElementById('gapLogContent');
    if (!section || !content) return;

    if (!gapData) {
        section.style.display = 'none';
        return;
    }

    section.style.display = 'block';

    const uptime     = Number(gapData.uptime_pct ?? 100).toFixed(1);
    const gapCount   = gapData.gap_count || 0;
    const minorCount = gapData.minor_gap_count || 0;
    const majorCount = gapData.major_gap_count || 0;
    const missingMin = ((gapData.missing_seconds || 0) / 60).toFixed(1);
    const isContinuous = gapData.continuous !== false && gapCount === 0;

    let html = '';

    // Status banner
    if (isContinuous) {
        html += `<div class="gap-log-status continuous">
            <span>Continuous temporal alignment verified. No gaps detected.</span>
        </div>`;
    } else {
        html += `<div class="gap-log-status disrupted">
            <span><strong>${gapCount} disruption(s) detected</strong> — ${minorCount} Minor, ${majorCount} Major.
            Total missing data: ${missingMin} min.</span>
        </div>`;
    }

    // Uptime progress bar
    const fillWidth = Math.min(100, Math.max(0, Number(uptime)));
    html += `<div class="gap-uptime-bar">
        <div class="gap-uptime-track">
            <div class="gap-uptime-fill" style="width:${fillWidth}%;"></div>
        </div>
        <span class="gap-uptime-label">Uptime: ${uptime}%</span>
    </div>`;

    // Gaps list
    if (gapData.gaps && gapData.gaps.length > 0) {
        html += `<ul class="gap-items-list">`;
        gapData.gaps.forEach(g => {
            const cls = g.category === 'Minor' ? 'minor' : 'major';
            html += `<li class="gap-item ${cls}">
                <span class="gap-badge ${cls}">${escapeHtml(g.category)}</span>
                <span>${escapeHtml(g.label)}</span>
            </li>`;
        });
        html += `</ul>`;
    }

    content.innerHTML = html;
}

// ============================================================================
// Sensor placement (Outdoor / Indoor) toggle
// ============================================================================

function setDeploymentEnvironment(env) {
    const next = (env === 'indoor') ? 'indoor' : 'outdoor';
    deploymentEnvironment = next;

    // Reflect active state on the toggle buttons
    document.querySelectorAll('#envToggle .env-toggle-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.env === next);
    });

    const hint = document.getElementById('envHint');
    if (hint) {
        hint.textContent = next === 'indoor'
            ? 'Indoor: WHO bedroom limits (30 dB LAeq / 45 dB LAmax) are included in the compliance matrix.'
            : 'Outdoor: WHO indoor bedroom limits are omitted (not comparable to an outdoor mic).';
    }

    // If results are already on screen, refresh compliance immediately.
    if (uploadedFilepath && currentAnalysis) {
        refreshComplianceForEnvironment();
    }
}

function refreshComplianceForEnvironment() {
    fetch('/api/compliance-check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filepath: uploadedFilepath, environment: deploymentEnvironment, filters: _activeFilters() }),
    })
        .then(r => r.json())
        .then(data => {
            if (data && data.success) {
                renderComplianceMatrix(data.compliance_matrix || []);
            }
        })
        .catch(err => console.error('Compliance refresh failed:', err));
}

// ============================================================================
// MODULE 7 — Compliance Matrix (WHO 2018 + Maryland COMAR)
// ============================================================================

function renderComplianceMatrix(matrixRows) {
    const container = document.getElementById('complianceContainer');
    if (!container) return;

    if (!matrixRows || matrixRows.length === 0) {
        container.innerHTML = `<p class="empty-note">The compliance matrix could not be computed: Lden and Lnight
            need a readable date and time column.</p>`;
        return;
    }

    // Group by category for section dividers
    const categories = {
        who_env:    { label: 'WHO 2018 — Environmental (Road Traffic, Aircraft & Railway)', rows: [] },
        who_indoor: { label: 'WHO 1999 / 2018 — Indoor (Bedroom)', rows: [] },
        maryland:   { label: 'Maryland COMAR 26.02.03 — Legal Limits &amp; State Goals', rows: [] },
    };

    matrixRows.forEach(r => {
        const cat = r.category || 'who_env';
        if (categories[cat]) categories[cat].rows.push(r);
        else categories.who_env.rows.push(r);
    });

    let html = `<table class="cm-table">
        <thead>
            <tr>
                <th>Regulatory Standard</th>
                <th>Metric</th>
                <th>Measured</th>
                <th>Limit</th>
                <th>Status</th>
                <th>Delta</th>
            </tr>
        </thead>
        <tbody>`;

    for (const [catKey, cat] of Object.entries(categories)) {
        if (cat.rows.length === 0) continue;

        html += `<tr class="cm-category-header"><td colspan="6">${escapeHtml(cat.label)}</td></tr>`;

        cat.rows.forEach(r => {
            const indicative = r.kind === 'indicative';
            const pass   = r.status === 'PASS';
            const delta  = Number(r.delta_db);
            const deltaStr = Number.isFinite(delta)
                ? (delta >= 0 ? `+${delta.toFixed(1)}` : delta.toFixed(1))
                : '—';
            const deltaCls = indicative ? 'zero' : (delta > 0 ? 'positive' : (delta < 0 ? 'negative' : 'zero'));

            // Source-specific (aircraft/railway): show a neutral "indicative reference"
            // pill instead of PASS/FAIL — a source-blind meter cannot attribute the source.
            let pillCls, pillIcon, pillText;
            if (indicative) {
                pillCls  = 'indicative';
                pillIcon = 'ⓘ';
                pillText = 'Indicative';
            } else {
                pillCls  = pass ? 'pass' : 'fail';
                pillIcon = pass ? '✓' : '✕';
                pillText = r.status;
            }

            const tooltip = r.tooltip ? `
                <span class="cm-tooltip-icon">ⓘ</span>
                <span class="cm-tooltip-text">${escapeHtml(r.tooltip)}</span>` : '';

            html += `<tr${indicative ? ' class="cm-indicative-row"' : ''}>
                <td class="cm-standard-name cm-tooltip-cell">
                    ${escapeHtml(r.standard)}${tooltip}
                </td>
                <td class="cm-metric">${escapeHtml(r.metric)}</td>
                <td class="cm-measured">${safeToFixed(r.measured_db, 1)} dB(A)</td>
                <td class="cm-limit">${safeToFixed(r.limit_db, 1)} dB(A)${indicative ? ' <span class="cm-ref-tag">ref</span>' : ''}</td>
                <td><span class="status-pill ${pillCls}">${pillIcon} ${pillText}</span></td>
                <td class="cm-delta ${deltaCls}">${deltaStr}</td>
            </tr>`;
        });
    }

    html += `</tbody></table>
    <div class="cm-caveats">
        <p><strong>Indicative reference rows (aircraft, railway):</strong> the sound level meter measures
        total combined acoustic energy and cannot confirm the source, so these source-specific WHO guidelines
        are shown as reference comparisons only — not pass/fail verdicts.</p>
        <p><strong>Measurement window:</strong> WHO 2018 intends Lden/Lnight as long-term <em>annual average</em>
        exposure. A monitoring period of days or weeks is indicative of conditions during that window only.</p>
        <p class="table-note">Delta = Measured − Limit. Negative = below limit (compliant). Positive = exceedance.
        A legal PASS under Maryland COMAR does not imply absence of health risk — WHO limits are stricter.
        Hover the ⓘ icon for clinical basis.</p>
    </div>`;

    container.innerHTML = html;
}

function renderFilterBanner() {
    const bar = document.getElementById('filterActiveBanner');
    const txt = document.getElementById('filterActiveText');
    if (!bar || !txt) return;
    const f = window.filterSummary;
    if (!f || !f.applied) { bar.style.display = 'none'; return; }
    const fmt = (iso) => {
        if (!iso) return '\u2014';
        const d = new Date(iso);
        return Number.isNaN(d.getTime()) ? '\u2014'
            : d.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric' });
    };
    const pct = f.rows_before ? Math.round(1000 * f.rows_after / f.rows_before) / 10 : 0;
    txt.textContent = ' Everything below \u2014 and every report you download \u2014 covers '
        + fmt(f.range_start) + ' to ' + fmt(f.range_end) + ' only: '
        + Number(f.rows_after).toLocaleString() + ' of '
        + Number(f.rows_before).toLocaleString() + ' readings (' + pct + '%).';
    bar.style.display = 'block';
}

function displayResults() {
    renderFilterBanner();
    if (!currentAnalysis) return;

    const analysis = currentAnalysis;
    const statistics = analysis.statistics || {};

    // Timestamp integrity — gate all time-dependent outputs.
    const timeValid = applyTimestampIntegrity();

    // MODULE 6: Data Continuity Log — prefer gap from multi-file merge, fall back to analyze response
    const gapData = window.mergeGapReport || analysis.gap_analysis || null;
    renderGapAnalysis(gapData);

    renderMetricsPanel();

    // Executive Summary + Statistics (time-independent)
    displayExecutiveSummary(statistics);
    displayStatistics(statistics, analysis.percentiles);

    // Standards (time-independent)
    displayStandards(currentStandards);

    if (timeValid) {
        loadComputedSummaries();
        renderComplianceMatrix(window.complianceMatrix || []);
        displayCharts();
    } else {
        suppressTimeDependentOutputs();
    }
}

// ── Timestamp integrity gate ────────────────────────────────────────────────

function applyTimestampIntegrity() {
    const ti = window.timestampIntegrity || { status: 'ok', time_metrics_valid: true };
    const valid = ti.time_metrics_valid !== false;
    const banner = document.getElementById('timestampWarning');
    const txt = document.getElementById('timestampWarningText');
    const soft = valid && ti.status === 'recovered';

    if (banner) {
        if (!valid || soft) {
            banner.style.display = 'flex';
            banner.classList.toggle('ts-warning-soft', soft);
            const title = banner.querySelector('strong');
            if (title) title.textContent = soft
                ? 'Timestamps recovered — please verify'
                : 'Timestamps could not be read reliably';
            if (txt) txt.textContent = ti.message ||
                'Timestamps are unreliable; time-based metrics are suppressed.';
        } else {
            banner.style.display = 'none';
        }
    }
    return valid;
}

function suppressTimeDependentOutputs() {
    const notice = (msg) => `<div class="ts-suppressed-note">
        ${msg}</div>`;

    // Hide the plain-English summary (it leads with Lden/Lnight).
    const pe = document.getElementById('plainEnglishSummaryCard');
    if (pe) pe.style.display = 'none';

    const setHTML = (id, msg) => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = notice(msg);
    };
    const m = 'Unavailable — requires valid timestamps.';
    setHTML('complianceContainer', m);
    setHTML('rollingMedianChart', m);
    setHTML('boxWhiskerChart', m);
    setHTML('heatmapChart', m);
    setHTML('timeSeriesChart', m);
}

// ============================================================================
// Load Computed Summaries (Daily/Hourly from API)
// ============================================================================

async function loadComputedSummaries() {
    if (!uploadedFilepath) {
        console.warn('No file path for computed summaries');
        return;
    }
    
    window.computedSummaryError = null;
    try {
        const response = await fetch('/api/get-computed-summaries', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filepath: uploadedFilepath,
                filters: _activeFilters(),
            })
        });

        const data = await response.json();
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        if (data.success || data.status === 'success') {
            // Store for visualization rendering
            window.computedDailySummary = data.daily_summary;
            window.computedHourlySummary = data.hourly_summary;
            
            // Render advanced visualizations.
            displayAdvancedVisualizations();

            // Re-render basic charts to ensure correct sizing (first draw may have been on a hidden tab).
            if (currentAnalysis) {
                displayCharts();
            }

            // Resize any Plotly charts already visible in the active tab.
            setTimeout(() => {
                if (typeof Plotly !== 'undefined') {
                    document.querySelectorAll('.tab-content.active .js-plotly-plot').forEach(el => {
                        try { Plotly.Plots.resize(el); } catch (_) {}
                    });
                }
            }, 150);
        }
    } catch (error) {
        // Record the reason so the charts that depend on these summaries say
        // why they are empty instead of silently showing nothing.
        console.error('Error loading computed summaries:', error);
        window.computedSummaryError = error.message || String(error);
        window.computedDailySummary = [];
        window.computedHourlySummary = [];
        displayAdvancedVisualizations();
        displayCharts();
    }
}

function displayExecutiveSummary(statistics) {
    const container = document.getElementById('healthAssessmentContainer');
    if (!container) return;

    // Every value comes from the LEQ channel. Taking the first column made the
    // "whole-record LAeq" the energy mean of the L-Max stream on NSRT exports.
    const col   = getPrimaryNoiseColumn();
    const stats = statistics[col] || {};
    const env   = (currentAnalysis?.environmental_metrics || {})[col] || {};
    const fmt   = v => Number.isFinite(Number(v)) ? Number(v).toFixed(1) : '—';

    const rows = [
        ['LAeq, whole record',  'All readings',                                   stats.laeq_db],
        ['LAeq, day',           '07:00–19:00 (Lden day period)',                 env.LAeq_day_lden],
        ['LAeq, evening',       '19:00–23:00 (Lden evening period)',             env.LAeq_evening_lden],
        ['Lnight',              '23:00–07:00; no penalty',                        env.Lnight],
        ['Lden',                'Day + evening (+5 dB) + night (+10 dB), 24 h',   env.Lden],
        ['LAeq, COMAR day',     '07:00–22:00 (COMAR 26.02.03.01B(5))',           env.LAeq_day_ldn],
        ['LAeq, COMAR night',   '22:00–07:00 (COMAR 26.02.03.01B(15))',          env.LAeq_night_ldn],
        ['Ldn',                 'Day + night (+10 dB, 22:00–07:00), 24 h',        env.Ldn],
    ];

    const body = rows.map(([metric, period, v]) => `
        <tr><th scope="row">${metric}</th><td>${period}</td><td class="num">${fmt(v)}</td></tr>`).join('');

    container.innerHTML = `
        <p class="section-note">Energy averages of the <strong>${escapeHtml(col.trim())}</strong> channel over each
        averaging period, computed across the whole record. Guideline comparisons are in the Compliance matrix.</p>
        <table class="data-table">
            <thead><tr><th scope="col">Metric</th><th scope="col">Averaging period</th><th scope="col" class="num">dB(A)</th></tr></thead>
            <tbody>${body}</tbody>
        </table>
        <p class="table-note">Lden and Lnight are defined in EU Directive 2002/49/EC, Annex I; Ldn and the day/night hours
        in COMAR 26.02.03.01. WHO defines Lden and Lnight as annual averages; over a shorter record they describe the
        measured period only.</p>`;
}

function displayStatistics(statistics, percentiles) {
    const container = document.getElementById('statisticsContainer');
    if (!container) return;

    // LEQ channel first, then the remaining channels in file order.
    const primary = getPrimaryNoiseColumn();
    const cols = Object.keys(statistics || {}).sort((a, b) => (b === primary) - (a === primary));
    if (!cols.length) {
        container.innerHTML = '<p class="empty-note">No statistics available.</p>';
        return;
    }
    const fmt = v => Number.isFinite(Number(v)) ? Number(v).toFixed(1) : '—';
    const pct = (c, k) => ((percentiles || {})[c] || {})[k];
    const spread = c => {
        const hi = Number(pct(c, 'L10')), lo = Number(pct(c, 'L90'));
        return Number.isFinite(hi) && Number.isFinite(lo) ? hi - lo : NaN;
    };

    const rows = [
        ['Energy average', c => statistics[c].laeq_db, 'dB(A)'],
        ['Arithmetic mean', c => statistics[c].mean_arithmetic_db, 'dB(A)'],
        ['Standard deviation', c => statistics[c].std_dev, 'dB'],
        ['Minimum', c => statistics[c].min, 'dB(A)'],
        ['Maximum', c => statistics[c].max, 'dB(A)'],
        ['L5', c => pct(c, 'L5'), 'dB(A)'],
        ['L10', c => pct(c, 'L10'), 'dB(A)'],
        ['L50 (median)', c => pct(c, 'L50'), 'dB(A)'],
        ['L90', c => pct(c, 'L90'), 'dB(A)'],
        ['L95', c => pct(c, 'L95'), 'dB(A)'],
        ['L10 − L90', spread, 'dB'],
    ];

    const head = cols.map(c => `<th scope="col" class="num">${escapeHtml(c.trim())}</th>`).join('');
    const body = rows.map(([label, get, unit]) => `
        <tr><th scope="row">${label}</th><td class="unit">${unit}</td>${cols.map(c => `<td class="num">${fmt(get(c))}</td>`).join('')}</tr>`).join('');

    container.innerHTML = `
        <p class="section-note">Computed over every reading in the analysed record, per logger channel.</p>
        <div class="table-scroll">
        <table class="data-table">
            <thead><tr><th scope="col">Statistic</th><th scope="col">Unit</th>${head}</tr></thead>
            <tbody>${body}</tbody>
        </table>
        </div>
        <p class="table-note">Energy average: 10·log10 of the mean of 10^(L/10) over all readings; on the LEQ channel this is the
        LAeq of the record. On the L-Max and L-Min channels it is shown for completeness and is not an LAeq.
        Lx is the level exceeded for x% of readings. Standard deviation is of the dB values.</p>`;
}

// ── Shared chart styling ────────────────────────────────────────────────────
// Okabe–Ito colours (colour-blind safe); reference lines are neutral so a
// guideline value is never styled as a verdict on data it does not apply to.
const CHART_COLORS = {
    primary: '#0072B2',
    secondary: '#D55E00',
    tertiary: '#009E73',
    reference: '#6B7280',
    nightBand: 'rgba(31, 58, 95, 0.07)',
    series: ['#0072B2', '#D55E00', '#009E73', '#CC79A7', '#E69F00', '#56B4E9'],
};
const CHART_CONFIG = { responsive: true, displaylogo: false,
                       modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d'] };

// Deep-merge ``overrides`` onto the common layout (axes are merged, not replaced).
function baseChartLayout(overrides = {}) {
    const axis = { gridcolor: '#E5E7EB', linecolor: '#9CA3AF', zeroline: false, automargin: true,
                   ticks: 'outside', tickcolor: '#9CA3AF', title: { font: { size: 12 } } };
    const base = {
        font: { family: 'IBM Plex Sans, Arial, sans-serif', size: 12, color: '#1F2933' },
        paper_bgcolor: '#FFFFFF', plot_bgcolor: '#FFFFFF',
        margin: { t: 16, r: 24, b: 56, l: 64 },
        legend: { orientation: 'h', x: 0, xanchor: 'left', y: -0.18 },
        hoverlabel: { font: { family: 'IBM Plex Sans, Arial, sans-serif' } },
    };
    const out = { ...base, ...overrides };
    for (const key of ['xaxis', 'yaxis']) {
        const o = overrides[key] || {};
        out[key] = { ...axis, ...o, title: { ...axis.title, ...(o.title || {}) } };
    }
    return out;
}

function referenceLines(levels) {
    return levels.map(y => ({ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: y, y1: y,
                              line: { color: CHART_COLORS.reference, width: 1, dash: 'dash' } }));
}

function referenceLabels(pairs) {
    return pairs.map(([y, text]) => ({ xref: 'paper', x: 1, y, text: `${text} dB(A)`, showarrow: false,
                                       xanchor: 'right', yanchor: 'bottom', yshift: 2,
                                       font: { size: 10, color: CHART_COLORS.reference } }));
}

function displayCharts() {
    const timeChart = document.getElementById('timeSeriesChart');
    if (!timeChart) return;
    // Plotly is loaded via CDN in index.html; if it's blocked/offline, don't crash analysis.
    if (typeof Plotly === 'undefined') {
        timeChart.textContent = 'Charts unavailable: the plotting library did not load.';
        return;
    }

    const dailyRecords = window.computedDailySummary || [];

    if (!dailyRecords.length) {
        timeChart.innerHTML = `<p class="empty-note">${window.computedSummaryError
            ? 'Daily values could not be loaded: ' + escapeHtml(window.computedSummaryError)
            : 'Daily values are not available for this record.'}</p>`;
        return;
    }

    // Sort by date and extract per-day values
    const sorted = [...dailyRecords]
        .filter(r => r.Date && Number.isFinite(Number(r.Average_L_EQ_dB)))
        .sort((a, b) => new Date(a.Date) - new Date(b.Date));

    if (!sorted.length) {
        timeChart.innerHTML = '<p class="empty-note">No daily values could be computed for this record.</p>';
        return;
    }

    const dates       = sorted.map(r => String(r.Date).substring(0, 10));
    const laeqDaily   = sorted.map(r => Number(r.Average_L_EQ_dB));
    const nightlyLaeq = sorted.map(r => {
        const v = Number(r.Nighttime_LAeq);
        return Number.isFinite(v) ? v : null;
    });
    const hasNightData = nightlyLaeq.some(v => v !== null);

    // Straight segments only: a spline would draw values between days that were
    // never measured. A single day is drawn as markers alone.
    const lineMode = sorted.length === 1 ? 'markers' : 'lines+markers';

    const allVals = [...laeqDaily, ...nightlyLaeq.filter(v => v !== null), 53, 45];
    const tYMax = Math.ceil(Math.max(...allVals) + 4);
    const tYMin = Math.floor(Math.min(...allVals) - 4);

    const trendData = [
        {
            x: dates, y: laeqDaily,
            name: 'LAeq, whole day',
            type: 'scatter', mode: lineMode,
            line: { color: CHART_COLORS.primary, width: 2 },
            marker: { size: 6, color: CHART_COLORS.primary, symbol: 'circle' },
            connectgaps: false,
            hovertemplate: '%{x}<br>LAeq: %{y:.1f} dB(A)<extra></extra>',
        },
    ];
    if (hasNightData) {
        trendData.push({
            x: dates, y: nightlyLaeq,
            name: 'LAeq, night (22:00–07:00)',
            type: 'scatter', mode: lineMode,
            line: { color: CHART_COLORS.secondary, width: 2, dash: 'dot' },
            marker: { size: 6, color: CHART_COLORS.secondary, symbol: 'square' },
            connectgaps: false,
            hovertemplate: '%{x}<br>Night LAeq: %{y:.1f} dB(A)<extra></extra>',
        });
    }

    // The traces are daily LAeq values; the 53 and 45 dB lines are the WHO Lden
    // and Lnight guideline VALUES, which are penalty-weighted long-term averages.
    // They are drawn as neutral references so no day reads as a pass or fail.
    const trendLayout = baseChartLayout({
        xaxis: { title: { text: 'Date' }, type: 'category', tickangle: dates.length > 10 ? -45 : 0 },
        yaxis: { title: { text: 'Sound level, LAeq (dB(A))' }, range: [tYMin, tYMax] },
        height: 420,
        shapes: referenceLines([53, 45]),
        annotations: referenceLabels([[53, 'WHO Lden guideline value, 53'], [45, 'WHO Lnight guideline value, 45']]),
    });

    try {
        Plotly.newPlot(timeChart, trendData, trendLayout, CHART_CONFIG);
    } catch (e) {
        console.error('Daily trend chart failed:', e);
        if (timeChart) timeChart.textContent = 'Daily trend chart failed to render.';
    }
}

function displayStandards(_standards) {
    const container = document.getElementById('standardsComparisonContainer');

    // ── Section builder helpers ─────────────────────────────────────────────
    function sectionHeader(title, subtitle) {
        return `<h3 class="ref-title">${title}</h3><p class="ref-subtitle">${subtitle}</p>`;
    }

    function stdTable(headers, rows) {
        const head = headers.map(hd => `<th scope="col">${hd}</th>`).join('');
        const body = rows.map(row => `<tr>${row.map((cell, ci) =>
            ci === 0 ? `<th scope="row">${cell}</th>` : `<td>${cell}</td>`).join('')}</tr>`).join('');
        return `<div class="table-scroll"><table class="data-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
    }

    const limitBadge = val => `<span class="num-strong">${val}</span>`;
    const strengthBadge = strength => `<span class="tag">${strength}</span>`;
    const section = content => `<section class="ref-section">${content}</section>`;

    // ── 1. WHO 2018 Environmental Noise Guidelines ──────────────────────────
    const s1 = section(`
        ${sectionHeader('WHO 2018 Environmental Noise Guidelines', 'Environmental Noise Guidelines for the European Region — World Health Organization, 2018')}
        <p class="ref-text">These are health-based recommendations expressed as annual average outdoor noise levels. They apply to transport noise sources near residential areas.</p>
        ${stdTable(
            ['Noise Source', 'Metric', 'Guideline Level', 'Averaging period', 'Strength'],
            [
                ['Road Traffic', 'Lden', limitBadge('≤ 53 dB(A)'), 'Year', strengthBadge('Strong')],
                ['Road Traffic', 'Lnight', limitBadge('≤ 45 dB(A)'), 'Year, 23:00–07:00', strengthBadge('Strong')],
                ['Railway', 'Lden', limitBadge('≤ 54 dB(A)'), 'Year', strengthBadge('Strong')],
                ['Railway', 'Lnight', limitBadge('≤ 44 dB(A)'), 'Year, 23:00–07:00', strengthBadge('Strong')],
                ['Aircraft', 'Lden', limitBadge('≤ 45 dB(A)'), 'Year', strengthBadge('Strong')],
                ['Aircraft', 'Lnight', limitBadge('≤ 40 dB(A)'), 'Year, 23:00–07:00', strengthBadge('Strong')],
                ['Wind Turbines', 'Lden', limitBadge('≤ 45 dB(A)'), 'Year', strengthBadge('Conditional')],
                ['Leisure / Amplified Music', 'LAeq,24h', limitBadge('≤ 70 dB(A)'), 'Year, all leisure sources combined', strengthBadge('Conditional')],
            ],
        )}
        <div class="ref-note">
          <strong>Strong vs Conditional.</strong> WHO grades a recommendation <em>strong</em> when it is confident the benefits outweigh the harms and it can be adopted as policy in most circumstances; <em>conditional</em> when the evidence base is thinner and a policy-maker needs to weigh local factors. A conditional recommendation is still a recommendation — the difference is in the certainty of the evidence, not in whether the level matters.
          <br><br><strong>These are annual averages</strong>, and they are outdoor levels. Comparing them against a week or a month is indicative only — see &ldquo;What can I report from my measurement?&rdquo; above.
        </div>
        <p class="ref-source">Source: WHO, <em>Environmental Noise Guidelines for the European Region</em> (2018), ISBN 978-92-890-5356-3. Indicator definitions follow EU Directive 2002/49/EC Annex I. Values and gradings verified 5 Aug 2026.</p>
    `);

    // ── 2. WHO 1999 Indoor / Community Guidelines ───────────────────────────
    const s2 = section(`
        ${sectionHeader('WHO 1999 Indoor & Community Noise Guidelines', 'Guidelines for Community Noise — World Health Organization, 1999 (Berglund, Lindvall & Schwela)')}
        <p class="ref-text">These guidelines define maximum indoor noise levels to protect health and well-being. They apply to the indoor acoustic environment of buildings.</p>
        ${stdTable(
            ['Setting', 'Metric', 'Limit', 'Applies To'],
            [
                ['Bedroom — sleep protection', 'LAeq', limitBadge('≤ 30 dB(A)'), 'Night (22:00–07:00)'],
                ['Bedroom — sleep protection', 'LAmax (fast)', limitBadge('≤ 45 dB(A)'), 'Night (single events)'],
                ['Living room / residential', 'LAeq', limitBadge('≤ 35 dB(A)'), 'Day / Evening'],
                ['School classroom', 'LAeq', limitBadge('≤ 35 dB(A)'), 'During class hours'],
                ['School classroom background', 'LAeq', limitBadge('≤ 35 dB(A)'), 'Unoccupied (HVAC + external)'],
                ['Hospital — patient ward', 'LAeq', limitBadge('≤ 35 dB(A)'), 'Day'],
                ['Hospital — patient ward', 'LAeq', limitBadge('≤ 30 dB(A)'), 'Night'],
                ['Outdoor living / garden areas', 'LAeq', limitBadge('≤ 55 dB(A)'), 'Day / Evening (serious annoyance threshold)'],
            ],
        )}
        <p class="ref-source">Source: WHO (1999) Guidelines for Community Noise, Geneva. Edited by Berglund B, Lindvall T, Schwela DH. ISBN 92-4-154553-4.</p>
    `);

    // ── 3. Maryland COMAR 26.02.03 ─────────────────────────────────────────
    // Two different tables in two different regulations, previously merged into
    // one and cited to the wrong section. Table 2 (.03) is the enforceable
    // limit; Table 1 (.02) is the state's goal and is stated on Ldn.
    const s3 = section(`
        ${sectionHeader('Maryland COMAR 26.02.03 — Noise Control', 'Code of Maryland Regulations — Control of Noise Pollution, Maryland Department of the Environment')}
        <p class="ref-text"><strong>Enforceable limits — COMAR 26.02.03.03, Table 2.</strong> &ldquo;A person may not cause or permit noise levels which exceed those specified in Table 2.&rdquo; Measured at or within the property line of the <em>receiving</em> property (.03D(2)).</p>
        ${stdTable(
            ['Receiving Zone', 'Day (7 am – 10 pm)', 'Night (10 pm – 7 am)', 'Averaging period'],
            [
                ['Residential', limitBadge('65 dB(A)'), limitBadge('55 dB(A)'), 'Not stated in the regulation'],
                ['Commercial', limitBadge('67 dB(A)'), limitBadge('62 dB(A)'), 'Not stated in the regulation'],
                ['Industrial', limitBadge('75 dB(A)'), limitBadge('75 dB(A)'), 'Not stated in the regulation'],
            ],
        )}
        <div class="ref-note">
          <strong>On the averaging period.</strong> Table 2 is headed &ldquo;Maximum Allowable Noise Levels (dBA)&rdquo; and gives no averaging time. COMAR defines &ldquo;equivalent sound level&rdquo; (.01B(13)) and says the <em>standards</em> in Table 1 are expressed in equivalent levels, but says nothing of the kind about Table 2. This platform compares Table 2 against the <strong>LAeq of the period</strong>, which is the common reading — a not-to-exceed reading of the same table would be stricter. Prominent discrete tones and periodic noises must be <strong>5 dB(A) below</strong> these levels (.03A(3)).
        </div>
        <p class="ref-text"><strong>State goals — COMAR 26.02.03.02, Table 1.</strong> &ldquo;Goals for the attainment of an adequate environment&rdquo;, which the Table 2 limits above are intended to achieve. These are targets, not levels a person may not exceed.</p>
        ${stdTable(
            ['Zoning District', 'Level', 'Metric', 'Averaging period'],
            [
                ['Residential', limitBadge('55 dB(A)'), 'Ldn', '24 hours, +10 dB applied to 10 pm – 7 am'],
                ['Commercial', limitBadge('64 dB(A)'), 'Ldn', '24 hours, +10 dB applied to 10 pm – 7 am'],
                ['Industrial', limitBadge('70 dB(A)'), 'Leq(24)', '24 hours, no penalty'],
            ],
        )}
        <p class="ref-source">Day and night hours are defined in COMAR 26.02.03.01B(5) and B(15); Ldn in B(4). Verified against the regulation text, 5 Aug 2026. Note that Maryland's night starts at 10 pm, an hour earlier than the 11 pm night used by WHO Lnight — the two cover different windows and are not interchangeable.</p>
    `);

    // ── 4. Occupational Standards ──────────────────────────────────────────
    const s4 = section(`
        ${sectionHeader('Occupational Noise Exposure Standards', 'For workplace noise — not directly applicable to community or environmental monitoring')}
        <p class="ref-text">These apply to workers exposed to noise during an 8-hour workday. Environmental or community data should <em>not</em> be compared directly against these occupational limits without appropriate dose-calculation methodology.</p>
        ${stdTable(
            ['Organization', 'Standard', 'Permissible Limit', 'Exchange Rate', 'What Triggers Action'],
            [
                ['OSHA (USA)', '29 CFR 1910.95', limitBadge('90 dB(A) TWA (8 hr)'), '5 dB', 'Engineering/administrative controls required if exceeded'],
                ['OSHA (USA)', '29 CFR 1910.95', '85 dB(A) TWA (8 hr)', '5 dB', 'Action level — hearing conservation program mandatory'],
                ['OSHA (USA)', '29 CFR 1910.95', limitBadge('140 dB peak'), '—', 'Hard ceiling — never to be exceeded (impulsive/impact)'],
                ['NIOSH (USA)', 'NIOSH REL (1998)', limitBadge('85 dB(A) TWA (8 hr)'), '3 dB', 'Recommended limit — stricter exchange rate (halving at +3 dB)'],
                ['EU', 'Directive 2003/10/EC', limitBadge('87 dB(A) TWA (8 hr)'), '3 dB', 'Exposure limit value (ELV) — hearing protectors included in dose'],
                ['EU', 'Directive 2003/10/EC', '85 dB(A) TWA (8 hr)', '3 dB', 'Upper action value — hearing protectors + HCP required'],
                ['EU', 'Directive 2003/10/EC', '80 dB(A) TWA (8 hr)', '3 dB', 'Lower action value — information, training, hearing protectors available'],
            ],
        )}
        <div class="ref-note">
          <strong>OSHA Table G-16 (duration guide):</strong> 90 dBA → 8 hr · 95 dBA → 4 hr · 100 dBA → 2 hr · 105 dBA → 1 hr · 110 dBA → 30 min · 115 dBA → 15 min.
          At ≥ 115 dBA exposure is impermissible without engineering controls.
        </div>
        <p class="ref-source">NIOSH uses a 3 dB exchange rate (equal-energy principle); OSHA uses a 5 dB rate. The NIOSH 85 dB REL is more protective and is recommended for hearing conservation program design.</p>
    `);

    // ── 5. EPA 1974 Community Reference Levels ────────────────────────────
    const s5 = section(`
        ${sectionHeader('US EPA 1974 — Levels of Environmental Noise', '"Levels Document" — EPA 550/9-74-004 — Informational Reference (not a federal regulation)')}
        <p class="ref-text">The EPA 1974 Levels Document identifies noise levels that protect public health and welfare with an adequate margin of safety. These are reference levels, not enforceable federal noise standards (EPA's noise enforcement authority was transferred to states in 1982).</p>
        ${stdTable(
            ['Purpose / Protection Goal', 'Metric', 'Reference Level', 'Area Type'],
            [
                ['Protect against hearing loss (lifetime exposure)', 'Leq(24h)', limitBadge('≤ 70 dB(A)'), 'All environments'],
                ['Prevent activity interference & annoyance', 'Ldn (day-night avg)', limitBadge('≤ 55 dB(A)'), 'Outdoor residential'],
                ['Prevent activity interference & annoyance', 'Leq (24h)', limitBadge('≤ 45 dB(A)'), 'Indoor residential / schools / hospitals'],
            ],
        )}
        <p class="ref-source">Source: US EPA (1974). Information on Levels of Environmental Noise Requisite to Protect Public Health and Welfare with an Adequate Margin of Safety. EPA/ONAC 550/9-74-004.</p>
    `);

    // ── 0. How to read a noise limit ───────────────────────────────────────
    // Placed first because every table below is unreadable without it: a limit
    // is a number AND an averaging period, and comparing the right number over
    // the wrong period is the most common way these figures get misused.
    const s0 = section(`
        ${sectionHeader('How to read a noise limit', 'Every limit below is a number plus an averaging period. Both matter.')}
        <p class="ref-text">
          A noise limit is never just a decibel value. It is a value <em>attached to a stated period of time</em>.
          45 dB measured over eight hours and 45 dB measured over one second are entirely different claims, and a
          reading compared against the wrong period is meaningless — however carefully it was measured.
        </p>
        ${stdTable(
            ['Metric', 'What it measures', 'Period it is defined over'],
            [
                ['<strong>LAeq,T</strong>', 'The steady level carrying the same sound energy as the real, varying sound. An <em>energy</em> average, not an arithmetic one.', 'Whatever T says — LAeq,1h, LAeq,8h, LAeq,24h'],
                ['<strong>L<sub>night</sub></strong>', 'LAeq across the night only. No penalty applied.', '<strong>23:00–07:00 (8 h)</strong>, averaged over a <strong>year</strong>'],
                ['<strong>L<sub>den</sub></strong>', 'A 24-hour LAeq with +5 dB added to evening and +10 dB to night readings before averaging, because the same sound harms more at those hours.', 'Day 07:00–19:00, evening 19:00–23:00, night 23:00–07:00, averaged over a <strong>year</strong>'],
                ['<strong>L<sub>dn</sub></strong>', 'The US/Maryland equivalent of Lden: 24-hour average, +10 dB at night, no separate evening.', 'Day 07:00–22:00, night 22:00–07:00, over 24 hours'],
                ['<strong>L<sub>Amax</sub></strong>', 'The single loudest moment. Not an average at all.', 'One event'],
                ['<strong>L90 / L50 / L10</strong>', 'Percentiles: the level exceeded 90%, 50% or 10% of the time. L90 is the background; L10 the louder events. Not averages.', 'The whole measurement, computed across every reading'],
            ],
        )}
        <div class="ref-note">
          <strong>Two traps worth knowing.</strong>
          <br>· <strong>Night is not one thing.</strong> WHO L<sub>night</sub> runs 23:00–07:00 (8 h). Maryland's night runs 22:00–07:00 (9 h). A figure computed on one window cannot be checked against the other's limit.
          <br>· <strong>L<sub>den</sub> is penalty-weighted.</strong> Its 53 dB is not on the scale of anything your meter displays, because evening and night readings are raised by 5 and 10 dB before averaging. No single reading can be compared against it.
        </div>
    `);

    // ── 0b. Choosing an averaging period for your own monitoring ───────────
    const s0b = section(`
        ${sectionHeader('What can I report from my measurement?', 'WHO Lden and Lnight are defined as YEARLY averages. Shorter records are still useful — but say what they are.')}
        <p class="ref-text">
          WHO's guideline values are written for a <strong>long-term annual average</strong> (Directive 2002/49/EC, Annex I).
          Almost nobody measures for a year. That does not make a shorter record invalid — it changes what you are
          entitled to claim from it.
        </p>
        ${stdTable(
            ['You measured for', 'You can report', 'Say it like this', 'Do not claim'],
            [
                ['<strong>1–3 days</strong>', 'LAeq per hour and per day, L90/L10, L<sub>Amax</sub>, the day/night split', '&ldquo;Over three days in April, the night-time LAeq was X dB(A).&rdquo;', 'An Lden or Lnight verdict — too few days to represent a typical period'],
                ['<strong>1 week</strong>', 'All of the above, plus per-night L<sub>night</sub> and per-day L<sub>den</sub>, and how many exceeded the guideline', '&ldquo;L<sub>night</sub> exceeded 45 dB(A) on 6 of 7 nights measured.&rdquo;', 'That the site&rsquo;s annual Lnight is X — one week is not a year'],
                ['<strong>1 month</strong>', 'All of the above, plus a stable diurnal pattern and weekday/weekend difference', '&ldquo;Across 28 days in March, the mean of the nightly L<sub>night</sub> values was X dB(A).&rdquo;', 'A seasonal or annual figure — one month carries one season&rsquo;s weather and activity'],
                ['<strong>1 year</strong>', 'L<sub>den</sub> and L<sub>night</sub> as WHO defines them', '&ldquo;The annual L<sub>den</sub> was X dB(A), against the WHO guideline of 53 dB(A).&rdquo;', '—'],
            ],
        )}
        <div class="ref-note">
          <strong>The honest way to use a short record.</strong> Report the measured period as the measured period, and
          compare it to the guideline as an <em>indication</em> rather than a verdict. Reporting how many individual
          nights or days crossed the line is stronger than a single averaged number, because it survives the objection
          that the record was too short: &ldquo;6 of 7 nights above 45 dB(A)&rdquo; is a fact about those seven nights and
          needs no extrapolation.
          <br><br>ISO 1996-2 puts the combined uncertainty of an environmental noise measurement at roughly
          <strong>1–3 dB</strong> once instrument, microphone position, source variability and weather are accounted for.
          Differences smaller than that should not be treated as meaningful, whichever standard you are comparing against.
        </div>
    `);

    container.innerHTML = `
        <div class="ref-wrapper">
          <div class="ref-intro">
            <p class="section-note">Every value below was checked against the primary source document, not a secondary summary. Each limit is shown with the averaging period it is defined over. Start with &ldquo;How to read a noise limit&rdquo; if any of the metrics are unfamiliar.</p>
          </div>
          ${s0}${s0b}${s1}${s2}${s3}${s4}${s5}
        </div>`;
}

function switchTab(button) {
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));

    button.classList.add('active');
    const tabName = button.getAttribute('data-tab');
    const tabElement = document.getElementById(tabName);
    if (!tabElement) return;

    tabElement.classList.add('active');

    // When the Charts tab becomes visible, always re-render all charts.
    // Charts rendered in a hidden (display:none) container have 0×0 dimensions and appear blank.
    // Re-rendering after the container is visible guarantees correct sizing.
    if (tabName === 'visualizations' && uploadedFilepath && currentAnalysis) {
        setTimeout(() => {
            displayCharts();
            displayAdvancedVisualizations();
        }, 30); // 30 ms — enough for CSS display:block to propagate
        return;
    }

    // For other tabs: resize any Plotly charts that were rendered while the tab was hidden.
    if (typeof Plotly !== 'undefined') {
        tabElement.querySelectorAll('.js-plotly-plot').forEach(el => {
            try { Plotly.Plots.resize(el); } catch (_) {}
        });
    }
}

// ============================================================================
// Advanced Visualizations (Rolling Median, Box-and-Whisker, Heatmap)
// ============================================================================

function displayAdvancedVisualizations() {
    if (typeof Plotly === 'undefined') {
        console.error('Plotly not available for visualizations');
        return;
    }
    
    updateAnalysisFileNote();
    displayRollingMedianChart();
    displayBoxWhiskerChart();
    displayHeatmapChart();
}

function updateAnalysisFileNote() {
    const note = document.getElementById('analysisFileNote');
    if (!note) return;

    const fileName = uploadedFilepath ? uploadedFilepath.split('/').pop() : '';
    note.textContent = fileName ? `Analyzing file: ${fileName}` : '';
}

function getPrimaryNoiseColumn() {
    const statistics = currentAnalysis?.statistics || {};
    const availableColumns = Object.keys(statistics);
    // Column names from the CSV often include spaces/units (e.g. ' LEQ dB -A '),
    // so use includes() rather than exact === to match the preferred metric.
    const preferredOrder = ['leq', 'laeq', 'l-min', 'lmin', 'l-max', 'lmax'];

    for (const preferred of preferredOrder) {
        const match = availableColumns.find((column) => column.toLowerCase().includes(preferred));
        if (match) return match;
    }

    return availableColumns[0] || 'leq';
}

function displayRollingMedianChart() {
    const container = document.getElementById('rollingMedianChart');
    if (!container) return;

    // Figure 1 is by hour of day only; a daily series belongs in Figure 4.
    const hourlySummary = window.computedHourlySummary;
    if (!hourlySummary || !hourlySummary.length) {
        container.innerHTML = `<p class="empty-note">${window.computedSummaryError
            ? 'Hourly values could not be loaded: ' + escapeHtml(window.computedSummaryError)
            : 'Loading hourly values…'}</p>`;
        return;
    }

    try {
        const sorted = [...hourlySummary].sort((a, b) => Number(a.Hour) - Number(b.Hour));
        const hourValues = sorted.map(h => Number(h.Hour));
        const xLabels = sorted.map(h => `${String(Number(h.Hour)).padStart(2, '0')}:00`);
        const yLaeq   = sorted.map(h => Number(h.Average_L_EQ_dB));
        const xTitle  = 'Hour of day (start of hour)';

        const validY = yLaeq.filter(Number.isFinite);
        if (validY.length === 0) {
            container.innerHTML = '<p class="empty-note">No hourly LAeq values could be computed.</p>';
            return;
        }

        const rawTrace = {
            x: xLabels, y: yLaeq,
            name: 'Hourly LAeq',
            type: 'scatter', mode: 'lines+markers',
            line: { color: CHART_COLORS.primary, width: 2 },
            marker: { size: 6, color: CHART_COLORS.primary },
            hovertemplate: '%{x}<br>LAeq: %{y:.1f} dB(A)<extra></extra>',
            showlegend: false,
        };

        const allVals = validY.concat([53, 45]);
        const yMin = Math.floor(Math.min(...allVals) - 4);
        const yMax = Math.ceil(Math.max(...allVals) + 4);

        const shapes = referenceLines([53, 45]);
        const annotations = referenceLabels([[53, 'WHO Lden guideline value, 53'],
                                             [45, 'WHO Lnight guideline value, 45']]);

        // Shade the WHO Lnight window (23:00–07:00) on the categorical hour axis.
        // Plotly maps categories to integer positions 0..23, so we shade by index.
        {
            const idxOf = (hr) => hourValues.indexOf(hr);
            const nightBands = [];
            const preDawn = idxOf(0);   // 00:00
            const dawnEnd = idxOf(6);   // through 06:59 (07:00 boundary)
            const lateNight = idxOf(23);
            if (preDawn !== -1 && dawnEnd !== -1) nightBands.push([preDawn - 0.5, dawnEnd + 0.5]);
            if (lateNight !== -1) nightBands.push([lateNight - 0.5, lateNight + 0.5]);
            nightBands.forEach(([x0, x1]) => {
                shapes.unshift({
                    type: 'rect', xref: 'x', yref: 'paper',
                    x0, x1, y0: 0, y1: 1,
                    fillcolor: CHART_COLORS.nightBand,
                    line: { width: 0 }, layer: 'below',
                });
            });
            annotations.push({
                xref: 'paper', yref: 'paper', x: 0, y: 1, text: 'Shaded: WHO night period, 23:00–07:00',
                showarrow: false, font: { size: 10, color: CHART_COLORS.reference },
                xanchor: 'left', yanchor: 'bottom',
            });
        }

        const layout = baseChartLayout({
            xaxis: { title: { text: xTitle } },
            yaxis: { title: { text: 'Sound level, LAeq (dB(A))' }, range: [yMin, yMax] },
            margin: { t: 28, r: 24, b: 56, l: 64 },
            height: 420,
            hovermode: 'x unified',
            shapes, annotations,
        });

        Plotly.newPlot(container, [rawTrace], layout, CHART_CONFIG);
    } catch (e) {
        console.error('Hourly trend chart failed:', e);
        container.textContent = 'Hourly trend chart failed to render.';
    }
}

async function displayBoxWhiskerChart() {
    const container = document.getElementById('boxWhiskerChart');
    if (!container || !uploadedFilepath) return;

    try {
        const noiseColumn = getPrimaryNoiseColumn();
        const response = await fetch('/api/diurnal-boxplot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filepath: uploadedFilepath,
                noise_col: noiseColumn,
                filters: _activeFilters(),
            })
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || 'Failed to load box-and-whisker chart');
        }

        const data = await response.json();
        const chart = data.chart || {};
        if (!chart.data || !chart.layout) {
            throw new Error('Invalid box plot chart data returned by server');
        }

        await Plotly.newPlot(container, chart.data, chart.layout, { responsive: true });
    } catch (e) {
        console.error('Box plot failed:', e);
        container.textContent = 'Box-and-whisker chart failed to render.';
    }
}

async function displayHeatmapChart() {
    const container = document.getElementById('heatmapChart');
    if (!container || !uploadedFilepath) return;
    
    try {
        const noiseColumn = getPrimaryNoiseColumn();
        const response = await fetch('/api/temporal-heatmap', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filepath: uploadedFilepath,
                noise_col: noiseColumn,
                resolution: 'hourly',
                filters: _activeFilters(),
            })
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || 'Failed to load temporal heatmap');
        }

        const data = await response.json();
        const chart = data.chart || {};
        if (!chart.data || !chart.layout) {
            throw new Error('Invalid heatmap chart data returned by server');
        }

        await Plotly.newPlot(container, chart.data, chart.layout, { responsive: true });
    } catch (e) {
        console.error('Heatmap failed:', e);
        container.textContent = 'Heatmap chart failed to render.';
    }
}

// ============================================================================
// Plain-English Summary Display
// ============================================================================

function displayPlainEnglishSummary(text) {
    const card = document.getElementById('plainEnglishSummaryCard');
    const textEl = document.getElementById('plainEnglishSummaryText');
    if (!card || !textEl) return;

    if (!text) {
        card.style.display = 'none';
        return;
    }

    // Render paragraphs and bullets from the \n\n-separated server text.
    const htmlParts = [];
    for (const para of text.split('\n\n')) {
        const lines = para.split('\n');
        const bulletLines = lines.filter(l => l.trim().startsWith('•'));
        const headerText  = lines.filter(l => !l.trim().startsWith('•')).join(' ').trim();
        if (headerText) {
            htmlParts.push(headerText.startsWith('WHO ')
                ? `<p class="summary-subhead">${escapeHtml(headerText)}</p>`
                : `<p>${escapeHtml(headerText)}</p>`);
        }
        if (bulletLines.length) {
            const items = bulletLines.map(l => `<li>${escapeHtml(l.trim().replace(/^•\s*/, ''))}</li>`).join('');
            htmlParts.push(`<ul>${items}</ul>`);
        }
    }
    textEl.innerHTML = htmlParts.join('\n');
    card.style.display = 'block';
}

function generateReport(reportType, format = 'html') {
    if (!uploadedFilepath) {
        showError('No file to generate report for');
        return;
    }

    const deviceId             = (document.getElementById('deviceIdInput')?.value || '').trim();
    const sourceFiles          = batchFileMeta.length > 0 ? batchFileMeta.map(f => f.name) : [];
    const customSectionHeading = (document.getElementById('customSectionHeading')?.value || '').trim();
    const customSectionBody    = (document.getElementById('customSectionBody')?.value || '').trim();

    const _reportJobId = Math.random().toString(36).slice(2, 10);
    setStatus('processing', `Generating ${format.toUpperCase()} report… 0%`);
    const _reportTicker = _pollProgress(_reportJobId, `Generating ${format.toUpperCase()} report…`);

    fetch('/api/generate-report', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            filepath: uploadedFilepath,
            report_type: reportType,
            format: format,
            job_id: _reportJobId,
            filters: _activeFilters(),
            device_id: deviceId,
            source_files: sourceFiles,
            environment: deploymentEnvironment,
            merge_gap_report: window.mergeGapReport || null,
            custom_section_heading: customSectionHeading,
            custom_section_body: customSectionBody,
        })
    })
    .then(response => {
        _reportTicker.done();
        if (response.ok) {
            const contentDisposition = response.headers.get('content-disposition');
            let filename = `noise_report_${reportType}_${new Date().toISOString().split('T')[0]}.${format === 'docx' ? 'docx' : format}`;
            if (contentDisposition) {
                const filenameMatch = contentDisposition.match(/filename="?([^"]+)"?/i);
                if (filenameMatch && filenameMatch[1]) {
                    filename = filenameMatch[1];
                }
            }
            return response.blob().then(blob => ({ blob, filename }));
        }
        return response.json().then(data => { throw new Error(data.error); });
    })
    .then(({ blob, filename }) => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
        setStatus('idle', 'Report generated');
        showNotification(`${format.toUpperCase()} report generated successfully!`);
    })
    .catch(error => {
        _reportTicker.done();
        showError('Error generating report: ' + error.message);
        setStatus('error', 'Report generation failed');
    });
}

function openReportFormatModal(reportType) {
    const modal = document.getElementById('reportFormatModal');
    if (!modal) {
        generateReport(reportType, 'pdf');
        return;
    }

    modal.dataset.reportType = reportType;
    modal.style.display = 'flex';
    modal.setAttribute('aria-hidden', 'false');
}

function closeReportFormatModal() {
    const modal = document.getElementById('reportFormatModal');
    if (!modal) return;

    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
    delete modal.dataset.reportType;
}

function chooseReportFormat(format) {
    const modal = document.getElementById('reportFormatModal');
    const reportType = modal?.dataset?.reportType || 'comprehensive';
    closeReportFormatModal();
    generateReport(reportType, format);
}

window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
        closeReportFormatModal();
    }
});

// ============================================================================
// Chart Image Download Functions
// ============================================================================

async function downloadChartImage(chartId, chartName) {
    if (!uploadedFilepath) {
        showError('No file selected');
        return;
    }

    const chartElement = document.getElementById(chartId);
    if (!chartElement) {
        showError(`Chart element "${chartId}" not found on page.`);
        return;
    }

    // Plotly renders into an SVG inside the container — if it's absent the chart hasn't loaded yet
    if (!chartElement.querySelector('svg.main-svg')) {
        showError(`"${chartName}" has not rendered yet. Open the Charts tab to load all charts first, then download.`);
        return;
    }

    if (typeof Plotly === 'undefined') {
        showError('Plotly is not available — chart download requires the Plotly library.');
        return;
    }

    setStatus('processing', `Downloading ${chartName}…`);
    try {
        await Plotly.downloadImage(chartElement, {
            format: 'png',
            width: 1400,
            height: 720,
            filename: `${chartName}_${new Date().toISOString().split('T')[0]}`
        });
        setStatus('idle', 'Chart downloaded');
        showNotification(`${chartName} downloaded as PNG (1400 × 720)`);
    } catch (e) {
        console.error('Chart image download failed:', e);
        showError(`Download failed for "${chartName}": ${e.message || 'unknown error'}. Try switching to the Charts tab first.`);
        setStatus('error', 'Download failed');
    }
}

// Download all chart images as a batch
function downloadAllCharts() {
    if (!uploadedFilepath) {
        showError('No file selected');
        return;
    }
    
    setStatus('processing', 'Preparing all charts for download...');
    
    const charts = [
        { id: 'rollingMedianChart', name: 'Rolling_Median' },
        { id: 'boxWhiskerChart', name: 'Hourly_BoxWhisker' },
        { id: 'heatmapChart', name: 'Temporal_Heatmap' },
        { id: 'timeSeriesChart', name: 'Daily_Acoustic_Trend' }
    ];
    
    let downloadCount = 0;
    charts.forEach((chart, index) => {
        setTimeout(() => {
            const element = document.getElementById(chart.id);
            if (element) {
                try {
                    Plotly.downloadImage(element, {
                        format: 'png',
                        width: 1200,
                        height: 600,
                        filename: `${chart.name}_${new Date().toISOString().split('T')[0]}.png`
                    });
                    downloadCount++;
                } catch (e) {
                    console.error(`Failed to download ${chart.name}:`, e);
                }
            }
        }, index * 500); // Stagger downloads by 500ms
    });
    
    setTimeout(() => {
        setStatus('idle', 'Charts downloaded');
        showNotification(`Downloaded ${downloadCount} chart images`);
    }, charts.length * 500 + 1000);
}

// ============================================================================
// CSV Download Functions
// ============================================================================

function downloadDailySummaryCSV() {
    if (!uploadedFilepath) {
        showError('No file uploaded');
        return;
    }
    setStatus('processing', 'Generating daily summary CSV…');

    fetch('/api/export-daily-summary-csv', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filepath: uploadedFilepath, filters: _activeFilters() }),
    })
    .then(response => {
        if (response.ok) return response.blob();
        return response.json().then(d => { throw new Error(d.error || 'Server error'); });
    })
    .then(blob => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `DAILY_SUMMARY_${new Date().toISOString().split('T')[0]}.csv`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
        setStatus('idle', 'Daily summary downloaded');
        showNotification('Daily summary downloaded as CSV (energy-averaged LAeq)');
    })
    .catch(error => {
        showError('Error generating daily summary: ' + error.message);
        setStatus('error', 'Daily summary failed');
    });
}

function downloadHourlySummaryCSV() {
    if (!uploadedFilepath) {
        showError('No file uploaded');
        return;
    }
    setStatus('processing', 'Generating hourly summary CSV…');

    fetch('/api/export-hourly-summary-csv', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filepath: uploadedFilepath, filters: _activeFilters() }),
    })
    .then(response => {
        if (response.ok) return response.blob();
        return response.json().then(d => { throw new Error(d.error || 'Server error'); });
    })
    .then(blob => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `HOURLY_SUMMARY_${new Date().toISOString().split('T')[0]}.csv`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
        setStatus('idle', 'Hourly summary downloaded');
        showNotification('Hourly summary downloaded as CSV (energy-averaged LAeq)');
    })
    .catch(error => {
        showError('Error generating hourly summary: ' + error.message);
        setStatus('error', 'Hourly summary failed');
    });
}

function exportData() {
    if (!uploadedFilepath) {
        showError('No file to export');
        return;
    }
    
    setStatus('processing', 'Exporting data...');
    
    fetch('/api/export-data', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ filepath: uploadedFilepath, filters: _activeFilters() })
    })
    .then(response => {
        if (response.ok) {
            return response.blob();
        }
        return response.json().then(data => { throw new Error(data.error); });
    })
    .then(blob => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `noise_analysis_export_${new Date().toISOString().split('T')[0]}.xlsx`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
        setStatus('idle', 'Data exported');
        showNotification('Data exported successfully!');
    })
    .catch(error => {
        showError('Error exporting data: ' + error.message);
        setStatus('error', 'Export failed');
    });
}

// Utility Functions
function showSection(sectionId) {
    // Sections live inside div.main-container (not inside <main>), so use .section class selector
    document.querySelectorAll('.section').forEach(section => {
        section.classList.remove('section-active');
        section.classList.add('section-hidden');
    });

    const section = document.getElementById(sectionId);
    if (section) {
        section.classList.remove('section-hidden');
        section.classList.add('section-active');
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }
}

function resetToUpload() {
    uploadedFilepath = null;
    currentAnalysis = null;
    currentStandards = null;
    currentFilters = { exclusions: [], bound_start: null, bound_end: null };
    window.computedDailySummary  = null;
    window.computedHourlySummary = null;
    window.mergeGapReport        = null;
    stagedFiles = [];
    batchFileMeta = [];
    if (fileInput) fileInput.value = '';
    _renderStagedFileList();
    renderBatchFilesPanel();
    showSection('upload-section');
    setStatus('idle', 'Ready');
}

function showError(message) {
    if (!errorMessage) {
        console.error('Error:', message);
        return;
    }
    errorMessage.textContent = message;
    errorMessage.style.display = 'block';
}

function setStatus(status, text) {
    if (statusIndicator) statusIndicator.className = `status-dot ${status}`;
    if (statusText) statusText.textContent = text;
}

function formatBytes(n) {
    if (!Number.isFinite(n)) return '';
    return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return String(text ?? '').replace(/[&<>"']/g, m => map[m]);
}

function showNotification(message) {
    const notification = document.createElement('div');
    notification.className = 'toast';
    notification.setAttribute('role', 'status');
    notification.textContent = message;
    document.body.appendChild(notification);
    setTimeout(() => notification.remove(), 3500);
}

// CSS for animations
const style = document.createElement('style');
style.textContent = `
    @keyframes slideIn {
        from {
            transform: translateX(400px);
            opacity: 0;
        }
        to {
            transform: translateX(0);
            opacity: 1;
        }
    }
    
    @keyframes slideOut {
        from {
            transform: translateX(0);
            opacity: 1;
        }
        to {
            transform: translateX(400px);
            opacity: 0;
        }
    }
`;
document.head.appendChild(style);

// ============================================================
// MULTI-FILE COMPARISON FEATURE
// ============================================================

let compareFilesList = [];
let compareDatasets = null;

function showCompareSection() {
    showSection('compare-section');
    _initCompareDropzone();
}

function _initCompareDropzone() {
    const dz = document.getElementById('compareDropzone');
    const fi = document.getElementById('compareFileInput');
    if (!dz || dz._compareInitialized) return;
    dz._compareInitialized = true;

    dz.addEventListener('click', () => fi.click());
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('drag-over'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
    dz.addEventListener('drop', (e) => {
        e.preventDefault();
        dz.classList.remove('drag-over');
        _addCompareFiles(Array.from(e.dataTransfer.files));
    });
    fi.addEventListener('change', () => {
        _addCompareFiles(Array.from(fi.files));
        fi.value = '';
    });
}

function _addCompareFiles(files) {
    files.forEach(f => {
        if (!compareFilesList.find(x => x.name === f.name && x.size === f.size)) {
            // Default label = filename without extension; user can edit it below.
            f._label = f.name.replace(/\.[^.]+$/, '');
            compareFilesList.push(f);
        }
    });
    _renderCompareFileList();
}

// Update a file's custom label as the user types (used in the comparison legend/report).
function setCompareLabel(index, value) {
    if (compareFilesList[index]) compareFilesList[index]._label = value;
}

function _renderCompareFileList() {
    const badge = document.getElementById('compareFileBadge');
    const listEl = document.getElementById('compareFileListItems');
    const dz = document.getElementById('compareDropzone');
    const listContainer = document.getElementById('compareFileList');
    if (!badge || !listEl || !dz || !listContainer) return;

    badge.textContent = compareFilesList.length;

    if (compareFilesList.length > 0) {
        dz.style.display = 'none';
        listContainer.style.display = 'block';
    } else {
        dz.style.display = '';
        listContainer.style.display = 'none';
    }

    listEl.innerHTML = compareFilesList.map((f, i) => `
        <li class="staged-file-item compare-file-item">
            <input type="text" class="compare-label-input" value="${escapeHtml(f._label || '')}"
                   placeholder="Label for this location"
                   oninput="setCompareLabel(${i}, this.value)" title="This name appears in the comparison charts and report">
            <span class="staged-file-name compare-file-orig" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
            <span class="staged-file-size">${formatBytes(f.size)}</span>
            <button class="staged-remove-btn" onclick="removeCompareFile(${i})" title="Remove">✕</button>
        </li>
    `).join('');
}

function removeCompareFile(index) {
    compareFilesList.splice(index, 1);
    _renderCompareFileList();
}

function clearCompareFiles() {
    compareFilesList = [];
    compareDatasets = null;
    _renderCompareFileList();
    const results = document.getElementById('compareResults');
    if (results) results.style.display = 'none';
    const loading = document.getElementById('compareLoading');
    if (loading) loading.style.display = 'none';
}

async function runComparison() {
    if (compareFilesList.length < 2) {
        alert('Please add at least 2 files to compare.');
        return;
    }
    if (compareFilesList.length > 6) {
        alert('Maximum 6 files can be compared at once.');
        return;
    }

    const btn = document.getElementById('compareAnalyzeBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Comparing…'; }
    document.getElementById('compareLoading').style.display = 'block';
    document.getElementById('compareResults').style.display = 'none';

    const formData = new FormData();
    compareFilesList.forEach(f => formData.append('files[]', f));
    // Custom labels (default = filename without extension) — used as the location names.
    formData.append('labels', JSON.stringify(
        compareFilesList.map(f => (f._label || '').trim() || f.name)));

    try {
        const res = await fetch('/api/compare', { method: 'POST', body: formData });
        const data = await res.json();
        if (!res.ok || data.error) throw new Error(data.error || 'Server error');

        compareDatasets = data.datasets;
        window.comparisonSummary = data.comparison_summary || null;
        document.getElementById('compareLoading').style.display = 'none';
        document.getElementById('compareResults').style.display = 'block';
        _renderComparisonResults(compareDatasets);
    } catch (err) {
        document.getElementById('compareLoading').style.display = 'none';
        alert('Comparison failed: ' + err.message);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Compare'; }
    }
}

function _fmt(v, decimals = 1) {
    return (v !== null && v !== undefined) ? Number(v).toFixed(decimals) : 'N/A';
}

function _renderComparisonResults(datasets) {
    _renderCompareVerdict(window.comparisonSummary);
    _renderCompareStatusCards(datasets);
    _renderCompareDiurnalChart(datasets);
    _renderCompareMetricsTable(datasets);
}

// Per-location status cards — plain numbers + within/above-guideline badges,
// sorted loudest to quietest. No axes, legends, or plots to interpret.
function _renderCompareStatusCards(datasets) {
    const el = document.getElementById('compareStatusCards');
    if (!el) return;

    const ds = [...datasets].sort((a, b) => (b.lden ?? b.laeq ?? 0) - (a.lden ?? a.laeq ?? 0)); // loudest first

    const badge = (v, limit) => {
        if (v == null) return `<span class="cmp-badge na">—</span>`;
        if (v <= limit) return `<span class="cmp-badge ok">At or below guideline value</span>`;
        return `<span class="cmp-badge bad">Above by ${(v - limit).toFixed(1)} dB</span>`;
    };
    const val = v => v != null ? `${Number(v).toFixed(1)}<span class="cmp-unit"> dB</span>` : '—';

    el.innerHTML = ds.map((d, i) => {
        const exceeds = (d.lden != null && d.lden > 53) || (d.lnight != null && d.lnight > 45);
        return `<div class="cmp-card ${exceeds ? 'bad' : 'ok'}">
            <div class="cmp-card-head">
                <span class="cmp-rank">#${i + 1}</span>
                <span class="cmp-card-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</span>
            </div>
            <div class="cmp-metric">
                <div class="cmp-metric-top"><span class="cmp-metric-label">Lden</span><span class="cmp-metric-val">${val(d.lden)}</span></div>
                ${badge(d.lden, 53)}
            </div>
            <div class="cmp-metric">
                <div class="cmp-metric-top"><span class="cmp-metric-label">Lnight</span><span class="cmp-metric-val">${val(d.lnight)}</span></div>
                ${badge(d.lnight, 45)}
            </div>
        </div>`;
    }).join('');
}

// Plain-language ranking / verdict banner
function _renderCompareVerdict(summary) {
    const el = document.getElementById('compareVerdict');
    if (!el) return;
    if (!summary || !summary.verdict) { el.style.display = 'none'; return; }
    const anyExceed = (summary.n_exceed_day || 0) > 0 || (summary.n_exceed_night || 0) > 0;
    el.className = 'compare-verdict ' + (anyExceed ? 'warn' : 'ok');
    el.style.display = 'block';
    el.innerHTML = `<div class="cv-title">What the comparison shows</div>
        <p>${escapeHtml(summary.verdict)}</p>`;
}

// Dot plot: each location's Lden (circle) and Lnight (diamond) vs the WHO lines.
function _renderCompareMetricsTable(datasets) {
    const container = document.getElementById('compareMetricsTable');
    if (!container) return;

    const WHO_DAY = 53, WHO_NIGHT = 45;

    const metrics = [
        { label: 'Monitoring period',        key: 'date_range',     fmt: v => v || 'N/A', who: null },
        { label: 'Duration',                 key: 'duration_label', fmt: v => v || 'N/A', who: null },
        { label: 'LAeq, whole record',       key: 'laeq',           fmt: v => v != null ? `${_fmt(v)} dB` : 'N/A', who: null },
        { label: 'Lden',                     key: 'lden',           fmt: v => v != null ? `${_fmt(v)} dB` : 'N/A', who: { limit: WHO_DAY,   higher_is_bad: true } },
        { label: 'Lnight',                   key: 'lnight',         fmt: v => v != null ? `${_fmt(v)} dB` : 'N/A', who: { limit: WHO_NIGHT, higher_is_bad: true } },
        { label: 'LAmax',                    key: 'lmax',           fmt: v => v != null ? `${_fmt(v)} dB` : 'N/A', who: null },
        { label: 'L90',                      key: 'l90',            fmt: v => v != null ? `${_fmt(v)} dB` : 'N/A', who: null },
    ];

    const headerRow = `<tr><th class="metric-label">Metric</th>${datasets.map(d => `<th>${escapeHtml(d.name)}</th>`).join('')}</tr>`;
    const bodyRows = metrics.map(m => {
        const cells = datasets.map(d => {
            const raw = d[m.key];
            const display = m.fmt(raw);
            let cls = '';
            if (m.who && raw != null) {
                cls = Number(raw) > m.who.limit ? 'who-fail' : 'who-pass';
            }
            return `<td class="${cls}">${display}</td>`;
        }).join('');
        return `<tr><td class="metric-label">${m.label}</td>${cells}</tr>`;
    }).join('');

    container.innerHTML = `<table class="compare-metrics-table"><thead>${headerRow}</thead><tbody>${bodyRows}</tbody></table>`;
}

function _renderCompareDiurnalChart(datasets) {
    const el = document.getElementById('compareDiurnalChart');
    if (!el || typeof Plotly === 'undefined') return;

    const hours = Array.from({ length: 24 }, (_, i) => `${String(i).padStart(2, '0')}:00`);

    const traces = datasets.map((d, i) => ({
        type: 'scatter', mode: 'lines+markers', name: d.name,
        x: hours, y: d.diurnal || Array(24).fill(null),
        line: { color: CHART_COLORS.series[i % CHART_COLORS.series.length], width: 2 },
        marker: { size: 5 }, connectgaps: false,
        hovertemplate: `${escapeHtml(d.name)}<br>%{x}: %{y:.1f} dB(A)<extra></extra>`,
    }));

    const layout = baseChartLayout({
        xaxis: { title: { text: 'Hour of day (start of hour)' }, tickangle: -45 },
        yaxis: { title: { text: 'Sound level, LAeq (dB(A))' } },
        margin: { t: 16, r: 24, b: 72, l: 64 },
        legend: { orientation: 'h', x: 0, y: -0.28 },
        height: 440,
        shapes: referenceLines([53, 45]),
        annotations: referenceLabels([[53, 'WHO Lden guideline value, 53'], [45, 'WHO Lnight guideline value, 45']]),
    });

    Plotly.newPlot(el, traces, layout, CHART_CONFIG);
}

async function downloadCompareReport() {
    if (!compareDatasets || compareDatasets.length < 2) {
        alert('Run a comparison first before downloading the report.');
        return;
    }
    const btn = document.getElementById('compareReportBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Generating…'; }

    try {
        const res = await fetch('/api/compare-report', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ datasets: compareDatasets }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.error || `HTTP ${res.status}`);
        }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `noise_comparison_${new Date().toISOString().slice(0,10)}.pdf`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    } catch (err) {
        alert('Report generation failed: ' + err.message);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Comparison report (PDF)'; }
    }
}
