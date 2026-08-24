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
            <span class="staged-file-icon"><i class="fas fa-file-csv"></i></span>
            <span class="staged-file-name">${escapeHtml(f.name)}</span>
            <span class="staged-file-size">${(f.size / 1024).toFixed(1)} KB</span>
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
            throw new Error('File too large for the server (limit: 4.5 MB). Please split your data into smaller files and upload them separately.');
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
                onError(new Error('File too large for the server (limit: 4.5 MB). Please split your data into smaller files.'));
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
                <i class="fas fa-file-csv batch-file-icon"></i>
                <span class="batch-file-name">${escapeHtml(f.name)}</span>
            </div>
            <div class="batch-file-stats">
                <span class="batch-stat-pill"><i class="fas fa-database"></i> ${(f.rows || 0).toLocaleString()} records</span>
                <span class="batch-stat-pill"><i class="fas fa-clock"></i> ${fmtDuration(f.start, f.end)}</span>
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
            filters: (currentFilters && (currentFilters.exclusions.length || currentFilters.bound_start || currentFilters.bound_end)) ? currentFilters : null,
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
    if (startEl)    startEl.value       = '';
    if (endEl)      endEl.value         = '';
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
            if (sEl) sEl.value = toLocalDT(data.start);
            if (eEl) eEl.value = toLocalDT(data.end);
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
    if (s) filters.bound_start = s;
    if (e) filters.bound_end   = e;

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

async function loadHealthAssessment() {
    if (!uploadedFilepath) {
        console.warn('No file path for health assessment');
        return;
    }
    
    try {
        const response = await fetch('/api/health-assessment', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filepath: uploadedFilepath })
        });
        
        if (!response.ok) {
            console.error('Health assessment failed:', await response.json());
            return;
        }
        
        const data = await response.json();
        if (data.success) {
            // Backend returns the rollup under `health_assessment` (flat lden/lnight/
            // concern_level/recommendations fields are added server-side).
            const assessment = data.health_assessment || data.assessment;
            renderHealthCards(assessment);
            renderRecommendations(assessment);
        }
    } catch (error) {
        console.error('Error loading health assessment:', error);
    }
}

function renderHealthCards(assessment) {
    if (!assessment) return;
    
    const container = document.getElementById('healthStatusCards');
    if (!container) return;
    
    let html = '';
    
    // Lden card — WHO 2018 road-traffic guideline is 53 dB(A); cardiovascular risk rises above 55 dB.
    if (Number.isFinite(Number(assessment.lden))) {
        const lden = Number(assessment.lden);
        const ldenStatus = lden > 58 ? 'danger' : (lden > 53 ? 'warning' : 'success');
        html += `
            <div class="health-card ${ldenStatus}">
                <div class="health-card-header">
                    <div class="health-card-icon">🌍</div>
                    <div class="health-card-title">Lden (Day-Evening-Night)</div>
                </div>
                <div class="health-card-value">${safeToFixed(lden, 1)}</div>
                <div class="health-card-desc">24-hour noise exposure with evening/night penalties</div>
                <div class="health-card-status">WHO guideline: 53 dB</div>
            </div>
        `;
    }

    // Lnight card — WHO 2018 sleep guideline is 45 dB(A); LOAEL (first effects) is 40 dB.
    if (Number.isFinite(Number(assessment.lnight))) {
        const lnight = Number(assessment.lnight);
        const lnightStatus = lnight > 45 ? 'danger' : (lnight > 40 ? 'warning' : 'success');
        html += `
            <div class="health-card ${lnightStatus}">
                <div class="health-card-header">
                    <div class="health-card-icon">😴</div>
                    <div class="health-card-title">Lnight (Night Sleep)</div>
                </div>
                <div class="health-card-value">${safeToFixed(lnight, 1)}</div>
                <div class="health-card-desc">Night-time noise level affecting sleep quality</div>
                <div class="health-card-status">WHO guideline: 45 dB</div>
            </div>
        `;
    }

    // Overall concern level
    if (assessment.concern_level) {
        const cl = String(assessment.concern_level).toLowerCase();
        const concernStatus = (cl.includes('critical') || cl.includes('high')) ? 'danger'
                            : (cl.includes('moderate')) ? 'warning'
                            : 'success';
        html += `
            <div class="health-card ${concernStatus}">
                <div class="health-card-header">
                    <div class="health-card-icon">⚠️</div>
                    <div class="health-card-title">Health Concern Level</div>
                </div>
                <div class="health-card-value">${assessment.concern_level}</div>
                <div class="health-card-desc">Overall health impact assessment based on WHO thresholds</div>
                <div class="health-card-status">Assessment complete</div>
            </div>
        `;
    }
    
    container.innerHTML = html;
}

// Render three core metric widgets (LAeq, LAmax, LAmin)
function renderCoreMetricWidgets() {
    if (!currentAnalysis || !currentAnalysis.statistics) return;

    const stats    = currentAnalysis.statistics;
    const colNames = Object.keys(stats);
    if (colNames.length === 0) return;

    // Prefer dedicated columns; fall back to first column for any not found
    const leqCol  = colNames.find(c => /^(l[_\-]?eq|laeq)$/i.test(c.trim()))  || colNames[0];
    const lmaxCol = colNames.find(c => /^(l[_\-]?max|lmax)$/i.test(c.trim())) || leqCol;
    const lminCol = colNames.find(c => /^(l[_\-]?min|lmin)$/i.test(c.trim())) || leqCol;

    const leqStat  = stats[leqCol]  || {};
    const lmaxStat = stats[lmaxCol] || leqStat;
    const lminStat = stats[lminCol] || leqStat;

    // Widget 1: Overall LAeq  (energy-average, not arithmetic mean)
    const elLaeqVal   = document.getElementById('widget-laeq-value');
    const elLaeqRange = document.getElementById('widget-laeq-range');
    if (elLaeqVal)   elLaeqVal.textContent   = safeToFixed(leqStat.laeq_db ?? leqStat.mean, 1);
    if (elLaeqRange) elLaeqRange.textContent =
        `Range: ${safeToFixed(leqStat.min, 1)} – ${safeToFixed(leqStat.max, 1)} dB(A)`;

    // Widget 2: Absolute Peak  (highest single reading from L-MAX column, or max of LEQ col)
    const elLamaxVal = document.getElementById('widget-lamax-value');
    if (elLamaxVal) elLamaxVal.textContent = safeToFixed(lmaxStat.max, 1);

    // Widget 3: Quiet Baseline  (lowest reading from L-MIN column, or min of LEQ col)
    const elLaminVal = document.getElementById('widget-lamin-value');
    if (elLaminVal) elLaminVal.textContent = safeToFixed(lminStat.min, 1);
}

// At-a-glance key findings — the headline numbers a reader cares about,
// shown nowhere else. Detailed per-channel statistics live in the Statistics tab.
function renderMetricsPanel() {
    const container = document.getElementById('metricsGrid');
    if (!container) return;

    const kf = window.keyFindings || {};
    const timeValid = !(window.timestampIntegrity && window.timestampIntegrity.time_metrics_valid === false);

    const avg = Number(kf.avg_laeq);
    const fmt = (v, d = 0) => Number.isFinite(Number(v)) ? Number(v).toFixed(d) : '—';

    function levelWord(v) {
        if (!Number.isFinite(v)) return ['—', 'metric-ok'];
        if (v < 45) return ['quiet', 'metric-ok'];
        if (v < 55) return ['moderate', 'metric-ok'];
        if (v < 65) return ['elevated', 'metric-warn'];
        if (v < 75) return ['high', 'metric-danger'];
        return ['very high', 'metric-danger'];
    }
    const [word, avgCls] = levelWord(avg);

    function card(value, unit, label, note, cls) {
        return `
        <div class="metric-item ${cls || ''}">
            <div class="metric-value">${escapeHtml(value)}<span style="font-size:14px;font-weight:600;color:#94a3b8;margin-left:3px">${escapeHtml(unit || '')}</span></div>
            <div class="metric-label" style="margin-top:6px">${escapeHtml(label)}</div>
            <div class="metric-note">${escapeHtml(note)}</div>
        </div>`;
    }

    const cards = [];
    cards.push(card(fmt(avg, 1), 'dB(A)', 'Average noise level',
        `${word} — the steady level with the same energy as the real noise`, avgCls));

    // Guideline comparison must use Lden, the metric the WHO 53 dB guideline is
    // defined on. This card previously showed the share of individual samples at
    // or below 53 dB under the label "Time within health guideline" — a category
    // error that read as high compliance ("73%") for homes whose Lden actually
    // exceeded the guideline.
    const ldenVal = Number(kf.lden);
    if (timeValid && Number.isFinite(ldenVal)) {
        const excess = ldenVal - 53;
        cards.push(card(
            (excess > 0 ? '+' : '') + fmt(excess, 1), 'dB',
            excess > 0 ? 'Above health guideline' : 'Below health guideline',
            `24-hour weighted average (Lden) ${fmt(ldenVal, 1)} dB vs the WHO guideline of 53 dB`,
            excess <= 0 ? 'metric-ok' : (excess < 5 ? 'metric-warn' : 'metric-danger')));
    }

    if (timeValid && Number.isFinite(Number(kf.quietest_hour))) {
        cards.push(card(`${String(kf.quietest_hour).padStart(2, '0')}:00`, '', 'Quietest time of day',
            `around ${fmt(kf.quietest_hour_db, 0)} dB — best for outdoor time`, 'metric-ok'));
    }
    if (timeValid && Number.isFinite(Number(kf.loudest_hour))) {
        cards.push(card(`${String(kf.loudest_hour).padStart(2, '0')}:00`, '', 'Loudest time of day',
            `around ${fmt(kf.loudest_hour_db, 0)} dB`, 'metric-warn'));
    }
    if (Number.isFinite(Number(kf.peak))) {
        // Label matches the stream the value came from.
        const peakLabel = kf.peak_is_lmax ? 'Loudest single moment (L-Max)'
                                          : 'Loudest interval average (LEQ)';
        cards.push(card(fmt(kf.peak, 0), 'dB(A)', peakLabel,
            kf.peak_is_lmax ? 'the highest level the meter recorded at any instant'
                            : 'the loudest averaging interval; instantaneous peaks were higher',
            ''));
    }

    container.innerHTML = cards.length
        ? `<div class="keyfindings-grid">${cards.join('')}</div>`
        : '<p style="color:#6b7280;">Key findings unavailable.</p>';
}

function _metricCell(label, value, unit, note, cssClass) {
    const display = Number.isFinite(Number(value)) ? safeToFixed(value, 1) : '—';
    return `
        <div class="metric-item ${cssClass || ''}">
            <div class="metric-label">${escapeHtml(label)}</div>
            <div class="metric-value">${display}</div>
            <div class="metric-unit">${escapeHtml(unit)}</div>
            <div class="metric-note">${escapeHtml(note)}</div>
        </div>`;
}

function renderRecommendations(assessment) {
    if (!assessment || !assessment.recommendations) return;
    
    const container = document.getElementById('recommendationsContainer');
    if (!container) return;
    
    let html = '';
    
    assessment.recommendations.forEach(rec => {
        html += `<div class="recommendation-item"><p>• ${escapeHtml(rec)}</p></div>`;
    });
    
    container.innerHTML = html;
}

function renderStandardsReference() {
    const container = document.querySelector('.standards-reference');
    if (!container) return;
    
    // Fetch standards reference data
    fetch('/api/standards-reference', { method: 'GET' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                let html = '';
                
                if (data.standards && data.standards.guidelines) {
                    let refHtml = '<div class="reference-grid">';
                    for (const [key, guideline] of Object.entries(data.standards.guidelines)) {
                        if (typeof guideline === 'object' && guideline.level) {
                            refHtml += `
                                <div class="reference-item">
                                    <strong>${escapeHtml(key)}</strong>
                                    <div>${escapeHtml(guideline.level)} dB</div>
                                </div>
                            `;
                        }
                    }
                    refHtml += '</div>';
                    container.querySelector('.reference-grid') ? 
                        container.querySelector('.reference-grid').innerHTML = refHtml : 
                        container.innerHTML += refHtml;
                }
            }
        })
        .catch(error => console.error('Error loading standards reference:', error));
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
            <i class="fas fa-check-circle"></i>
            <span>Continuous temporal alignment verified. No gaps detected.</span>
        </div>`;
    } else {
        html += `<div class="gap-log-status disrupted">
            <i class="fas fa-exclamation-triangle"></i>
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
        body: JSON.stringify({ filepath: uploadedFilepath, environment: deploymentEnvironment }),
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
        container.innerHTML = `<p style="color:#6b7280;font-size:13px;">
            Compliance matrix could not be computed — timestamps or sufficient data may be missing.
            Ensure the dataset has a DateTime column to derive Lden/Lnight.
        </p>`;
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
        <p style="color:#9ca3af;">Delta = Measured − Limit. Negative = below limit (compliant). Positive = exceedance.
        A legal PASS under Maryland COMAR does not imply absence of health risk — WHO limits are stricter.
        Hover the ⓘ icon for clinical basis.</p>
    </div>`;

    container.innerHTML = html;
}

function renderDataInsights(statistics) {
    // This function generates key insights from the data
    // Called after analysis completes to highlight important findings
    if (!statistics || Object.keys(statistics).length === 0) return;
    
    const colName = Object.keys(statistics)[0];
    const stats = statistics[colName];
    if (!stats) return;
    
    const mean = Number(stats.mean);
    const max = Number(stats.max);
    const min = Number(stats.min);
    const std = Number(stats.std_dev);
    
    // Log insights (could be displayed in UI if needed)
    console.log('=== DATA INSIGHTS ===');
    console.log(`Average Noise Level (LAeq): ${safeToFixed(mean, 1)} dB(A)`);
    console.log(`Maximum Level: ${safeToFixed(max, 1)} dB(A) - Peak exposure`);
    console.log(`Minimum Level: ${safeToFixed(min, 1)} dB(A) - Baseline background`);
    console.log(`Range: ${safeToFixed(max - min, 1)} dB(A) - Variability in noise`);
    console.log(`Std Deviation: ${safeToFixed(std, 1)} dB(A) - Noise fluctuation`);
    
    if (mean < 50) {
        console.log('✓ ASSESSMENT: Noise levels generally within WHO guidelines');
    } else if (mean < 55) {
        console.log('⚠ ASSESSMENT: Noise levels approaching WHO thresholds');
    } else if (mean < 70) {
        console.log('⚠ CAUTION: Elevated noise levels - health effects possible');
    } else {
        console.log('🚨 ALERT: High noise levels - health effects likely');
    }
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

    // Render core metric widgets FIRST (time-independent)
    renderCoreMetricWidgets();

    // MODULE 6: Data Continuity Log — prefer gap from multi-file merge, fall back to analyze response
    const gapData = window.mergeGapReport || analysis.gap_analysis || null;
    renderGapAnalysis(gapData);

    renderMetricsPanel();
    renderDataInsights(statistics);

    // Executive Summary + Statistics (time-independent)
    displayExecutiveSummary(statistics);
    displayStatistics(statistics, analysis.percentiles);

    // Standards (time-independent)
    displayStandards(currentStandards);

    if (timeValid) {
        loadHealthAssessment();
        loadComputedSummaries();
        renderComplianceMatrix(analysis.compliance_matrix || null);
        displayCharts(analysis);
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
        <i class="fas fa-clock"></i> ${msg}</div>`;

    // Hide the plain-English summary (it leads with Lden/Lnight).
    const pe = document.getElementById('plainEnglishSummaryCard');
    if (pe) pe.style.display = 'none';

    const setHTML = (id, msg) => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = notice(msg);
    };
    const m = 'Unavailable — requires valid timestamps.';
    setHTML('healthStatusCards', m);
    setHTML('recommendationsContainer', m);
    setHTML('complianceContainer', m);
    setHTML('rollingMedianChart', m);
    setHTML('boxWhiskerChart', m);
    setHTML('heatmapChart', m);
    setHTML('timeSeriesChart', m);
    setHTML('thresholdChart', '');  // percentiles still valid; rendered separately
}

// ============================================================================
// Load Computed Summaries (Daily/Hourly from API)
// ============================================================================

async function loadComputedSummaries() {
    if (!uploadedFilepath) {
        console.warn('No file path for computed summaries');
        return;
    }
    
    try {
        const response = await fetch('/api/get-computed-summaries', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                filepath: uploadedFilepath,
                filters: (currentFilters && (currentFilters.exclusions.length || currentFilters.bound_start || currentFilters.bound_end)) ? currentFilters : null,
            })
        });
        
        if (!response.ok) {
            console.error('Computed summaries failed:', await response.json());
            return;
        }
        
        const data = await response.json();
        if (data.success || data.status === 'success') {
            // Store for visualization rendering
            window.computedDailySummary = data.daily_summary;
            window.computedHourlySummary = data.hourly_summary;
            
            console.log(`Loaded ${data.daily_count} daily summaries, ${data.hourly_count} hourly summaries`);
            
            // Render advanced visualizations.
            displayAdvancedVisualizations();

            // Re-render basic charts to ensure correct sizing (first draw may have been on a hidden tab).
            if (currentAnalysis) {
                displayCharts(currentAnalysis);
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
        console.error('Error loading computed summaries:', error);
    }
}

function displayExecutiveSummary(statistics) {
    const container = document.getElementById('healthAssessmentContainer');
    if (!container) return;

    // Pull the primary noise column stats
    const firstCol   = Object.keys(statistics)[0] || '';
    const firstStats = statistics[firstCol] || {};
    const laeq = Number(firstStats?.laeq_db ?? firstStats?.mean);

    // Derive environmental metrics (Lden, Lnight) from analysis
    const env        = currentAnalysis?.environmental_metrics || {};
    const envFirst   = env[firstCol] || env[Object.keys(env)[0]] || {};
    const lden       = Number(envFirst?.Lden  ?? envFirst?.lden  ?? NaN);
    const lnight     = Number(envFirst?.Lnight ?? envFirst?.lnight ?? NaN);
    const laeqDay    = Number(envFirst?.LAeq_day   ?? NaN);
    const laeqNight  = Number(envFirst?.LAeq_night ?? NaN);

    // Concern level from WHO 2018 thresholds, graduated to match the report.
    //
    // 'LOW' must NOT be the default. Lden and Lnight are absent whenever the
    // timestamps could not be read (the server strips them rather than publish a
    // fabricated timeline), and the previous default rendered a reassuring "LOW"
    // verdict in exactly that case — a conclusion drawn from no evidence, and a
    // false negative in the direction that matters most.
    const THEME = {
        SERIOUS:  { bg: '#fef2f2', fg: '#7f1d1d', bdr: '#f87171' },
        HIGH:     { bg: '#fef2f2', fg: '#991b1b', bdr: '#fca5a5' },
        MODERATE: { bg: '#fffbeb', fg: '#92400e', bdr: '#fcd34d' },
        LOW:      { bg: '#f0fdf4', fg: '#166534', bdr: '#86efac' },
        'NOT ASSESSABLE': { bg: '#f1f5f9', fg: '#475569', bdr: '#cbd5e1' },
    };

    let level;
    const haveGuidelineMetrics = Number.isFinite(lden) || Number.isFinite(lnight);
    if (!haveGuidelineMetrics) {
        // LAeq cannot stand in: Lden applies +5 dB evening and +10 dB night
        // penalties, so it is always higher — judging by LAeq produces false passes.
        level = 'NOT ASSESSABLE';
    } else {
        const ldenExcess   = Number.isFinite(lden)   ? lden - 53   : -Infinity;
        const lnightExcess = Number.isFinite(lnight) ? lnight - 45 : -Infinity;
        const worst = Math.max(ldenExcess, lnightExcess);
        if (worst >= 10)      level = 'SERIOUS';
        else if (worst >= 5)  level = 'HIGH';
        else if (worst > 0)   level = 'HIGH';
        else if (Number.isFinite(lnight) && lnight > 40) level = 'MODERATE';  // WHO NNG 2009 LOAEL
        else level = 'LOW';
    }
    const _t = THEME[level] || THEME.LOW;
    const levelBg = _t.bg, levelFg = _t.fg, levelBdr = _t.bdr;

    function metricRow(label, val, limit, unit = 'dB(A)') {
        if (!Number.isFinite(val)) return '';
        const over  = Number.isFinite(limit) && val > limit;
        const badge = over
            ? `<span style="font-size:11px;padding:1px 6px;border-radius:4px;background:#fee2e2;color:#991b1b;font-weight:600;margin-left:8px">EXCEEDS by ${(val - limit).toFixed(1)} dB</span>`
            : (Number.isFinite(limit)
                ? `<span style="font-size:11px;padding:1px 6px;border-radius:4px;background:#dcfce7;color:#166534;font-weight:600;margin-left:8px">WITHIN LIMIT</span>`
                : '');
        const limitNote = Number.isFinite(limit) ? ` <span style="color:#9ca3af;font-size:12px">WHO limit: ${limit} ${unit}</span>` : '';
        return `<tr>
            <td style="padding:9px 12px;border-bottom:1px solid #f1f5f9;color:#374151">${label}${limitNote}</td>
            <td style="padding:9px 12px;border-bottom:1px solid #f1f5f9;text-align:right;font-weight:700;font-size:15px;color:#1e3a5f">${val.toFixed(1)} ${unit}</td>
            <td style="padding:9px 12px;border-bottom:1px solid #f1f5f9">${badge}</td>
        </tr>`;
    }

    let html = `
        <div style="background:${levelBg};border:1.5px solid ${levelBdr};border-radius:10px;padding:16px 20px;margin-bottom:18px;display:flex;align-items:center;gap:14px">
            <div style="font-size:13px;font-weight:700;color:${levelFg};white-space:nowrap;letter-spacing:.05em">
                CONCERN LEVEL: ${level}
            </div>
            <div style="width:1px;height:28px;background:${levelBdr};flex-shrink:0"></div>
            <div style="font-size:13px;color:#374151;line-height:1.5">
                ${level === 'HIGH'
                    ? 'WHO 2018 health-based noise guidelines are exceeded. Prolonged exposure is associated with cardiovascular and sleep health risks.'
                    : level === 'MODERATE'
                    ? 'Noise levels approach WHO guidelines. Continued monitoring recommended, especially for sensitive occupants.'
                    : 'Noise levels are within WHO 2018 health-based guidelines. Periodic monitoring is advisable.'}
            </div>
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:13px;background:white;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.06)">
            <thead>
                <tr style="background:#1e3a5f;color:#fff">
                    <th style="padding:9px 12px;text-align:left;font-weight:600">Acoustic Metric</th>
                    <th style="padding:9px 12px;text-align:right;font-weight:600">Measured</th>
                    <th style="padding:9px 12px;font-weight:600">WHO Assessment</th>
                </tr>
            </thead>
            <tbody>
                ${metricRow('LAeq — Whole-record Energy Average', laeq, NaN)}
                ${metricRow('Lden — Day-Evening-Night Weighted', lden, 53)}
                ${metricRow('Lnight — Nighttime (23:00–07:00)', lnight, 45)}
                ${metricRow('LAeq Day (07:00–22:00)', laeqDay, NaN)}
                ${metricRow('LAeq Night (22:00–07:00)', laeqNight, NaN)}
            </tbody>
        </table>`;

    // Interpretations (if returned by the old health API)
    const interpretations = currentAnalysis?.interpretations || [];
    if (interpretations.length > 0) {
        html += `<div style="margin-top:14px;padding:14px 16px;border-left:4px solid #3D5A80;background:#f8fafc;border-radius:0 6px 6px 0">`;
        interpretations.forEach(i => {
            html += `<p style="margin:6px 0;font-size:13px;color:#374151;line-height:1.55">${escapeHtml(i)}</p>`;
        });
        html += `</div>`;
    }

    container.innerHTML = html;
}

function displayStatistics(statistics, percentiles) {
    const container = document.getElementById('statisticsContainer');

    function levelBg(v) {
        if (!Number.isFinite(v)) return { bg: '#f8fafc', color: '#64748b', bar: '#cbd5e1' };
        if (v > 75) return { bg: '#fff1f2', color: '#be123c', bar: '#f43f5e' };
        if (v > 65) return { bg: '#fff7ed', color: '#c2410c', bar: '#f97316' };
        if (v > 55) return { bg: '#fffbeb', color: '#b45309', bar: '#f59e0b' };
        if (v > 45) return { bg: '#fefce8', color: '#854d0e', bar: '#eab308' };
        return { bg: '#f0fdf4', color: '#166534', bar: '#22c55e' };
    }

    function statCard(label, sublabel, value, unit, showBar) {
        const num = Number(value);
        const display = Number.isFinite(num) ? num.toFixed(1) : '—';
        const theme = levelBg(num);
        const barPct = (Number.isFinite(num) && showBar) ? Math.min(100, Math.max(0, ((num - 30) / (100 - 30)) * 100)) : null;
        return `
        <div style="background:${theme.bg};border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:4px;border:1px solid rgba(0,0,0,0.06);">
            <div style="font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em;">${escapeHtml(label)}</div>
            ${sublabel ? `<div style="font-size:10px;color:#cbd5e1;">${escapeHtml(sublabel)}</div>` : ''}
            <div style="font-size:22px;font-weight:700;color:${theme.color};line-height:1.2;">${display} <span style="font-size:12px;font-weight:500;color:#94a3b8;">${escapeHtml(unit || 'dB(A)')}</span></div>
            ${barPct !== null ? `<div style="height:3px;border-radius:2px;background:#e2e8f0;margin-top:4px;"><div style="height:100%;width:${barPct.toFixed(0)}%;background:${theme.bar};border-radius:2px;"></div></div>` : ''}
        </div>`;
    }

    function pctBar(label, value, maxVal) {
        const num = Number(value);
        const display = Number.isFinite(num) ? num.toFixed(1) : '—';
        const theme = levelBg(num);
        const pct = (Number.isFinite(num) && maxVal) ? Math.min(100, Math.max(2, ((num - 20) / (maxVal - 20)) * 100)) : 0;
        return `
        <div style="display:flex;align-items:center;gap:12px;padding:7px 0;border-bottom:1px solid #f1f5f9;">
            <div style="font-weight:700;color:#334155;font-size:13px;min-width:36px;">${escapeHtml(label)}</div>
            <div style="flex:1;background:#f1f5f9;border-radius:4px;height:8px;overflow:hidden;">
                <div style="height:100%;width:${pct.toFixed(0)}%;background:${theme.bar};border-radius:4px;transition:width 0.6s ease;"></div>
            </div>
            <div style="font-weight:600;color:${theme.color};font-size:14px;min-width:60px;text-align:right;">${display} <span style="font-size:10px;font-weight:400;color:#94a3b8;">dB(A)</span></div>
        </div>`;
    }

    let html = '';

    for (const [colName, stats] of Object.entries(statistics)) {
        const pcts = (percentiles && percentiles[colName]) ? percentiles[colName] : {};
        const laeq = stats.laeq_db ?? stats.mean;
        const spread = (Number.isFinite(Number(pcts.L10)) && Number.isFinite(Number(pcts.L90)))
            ? (Number(pcts.L10) - Number(pcts.L90)).toFixed(1) : null;
        const pctVals = [pcts.L5, pcts.L10, pcts.L50, pcts.L90, pcts.L95].map(Number).filter(Number.isFinite);
        const pctMax = pctVals.length ? Math.max(...pctVals) + 4 : 100;

        html += `
        <div style="margin-bottom:32px;">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
            <div style="width:4px;height:22px;background:linear-gradient(180deg,#3D5A80,#7C3AED);border-radius:2px;"></div>
            <h3 style="margin:0;font-size:15px;font-weight:700;color:#1e293b;">Channel: ${escapeHtml(colName)}</h3>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;">

            <div>
              <div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.07em;margin-bottom:10px;">Core Acoustic Metrics</div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
                ${statCard('LAeq', 'Energy-averaged level', laeq, 'dB(A)', true)}
                ${statCard('Arithmetic Mean', 'Linear average (reference)', stats.mean_arithmetic_db, 'dB(A)', true)}
                ${statCard('Maximum', 'Peak instantaneous level', stats.max, 'dB(A)', true)}
                ${statCard('Minimum', 'Lowest recorded level', stats.min, 'dB(A)', false)}
                ${statCard('Range', 'Max − Min spread', stats.range, 'dB', false)}
                ${statCard('L10–L90 Spread', 'Dynamic range indicator', spread, 'dB', false)}
              </div>
              <div style="margin-top:12px;">
                <div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.07em;margin-bottom:10px;">Variability</div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
                  ${statCard('Std Deviation', 'Fluctuation intensity', stats.std_dev, 'dB', false)}
                  ${statCard('Coeff. of Variation', 'Relative variability', stats.cv, '%', false)}
                </div>
              </div>
            </div>

            <div>
              <div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.07em;margin-bottom:10px;">Exceedance Percentile Levels</div>
              <div style="background:#f8fafc;border-radius:10px;padding:14px 16px;border:1px solid rgba(0,0,0,0.06);">
                <div style="font-size:11px;color:#94a3b8;margin-bottom:10px;line-height:1.5;">Each Lx level is exceeded <em>x%</em> of the total monitoring time. Lower values indicate a quieter acoustic environment.</div>
                ${pctBar('L5',  pcts.L5,  pctMax)}
                ${pctBar('L10', pcts.L10, pctMax)}
                ${pctBar('L50', pcts.L50, pctMax)}
                ${pctBar('L90', pcts.L90, pctMax)}
                ${pctBar('L95', pcts.L95, pctMax)}
                <div style="margin-top:12px;display:flex;gap:16px;flex-wrap:wrap;font-size:10px;color:#94a3b8;">
                  <span style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:10px;height:10px;background:#22c55e;border-radius:2px;"></span>≤ 45 dB</span>
                  <span style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:10px;height:10px;background:#eab308;border-radius:2px;"></span>45–55 dB</span>
                  <span style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:10px;height:10px;background:#f59e0b;border-radius:2px;"></span>55–65 dB</span>
                  <span style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:10px;height:10px;background:#f97316;border-radius:2px;"></span>65–75 dB</span>
                  <span style="display:flex;align-items:center;gap:4px;"><span style="display:inline-block;width:10px;height:10px;background:#f43f5e;border-radius:2px;"></span>&gt; 75 dB</span>
                </div>
              </div>
            </div>

          </div>
        </div>`;
    }

    container.innerHTML = html || '<p style="color:#6b7280;padding:20px;">No statistics available.</p>';
}

function displayCompliance(compliance) {
    const container = document.getElementById('complianceContainer');
    let html = '';

    if (!compliance || typeof compliance !== 'object') {
        container.textContent = 'No compliance data available.';
        return;
    }
    
    for (const [colName, standards] of Object.entries(compliance)) {
        html += `
            <div style="margin-bottom: 30px;">
                <h3>${escapeHtml(colName)}</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Area Type</th>
                            <th>Measured (dB)</th>
                            <th>Limit (dB)</th>
                            <th>Status</th>
                            <th>Exceeded By (dB)</th>
                        </tr>
                    </thead>
                    <tbody>
        `;

        const currentLeq = Number(standards?.current_leq);
        if (Number.isFinite(currentLeq)) {
            html += `
                <tr>
                    <td>current_leq (LAeq)</td>
                    <td>${safeToFixed(currentLeq, 2)}</td>
                    <td>-</td>
                    <td class="pass">${safeToFixed(currentLeq, 2)}</td>
                    <td>-</td>
                </tr>
            `;
        }

        const currentLden = Number(standards?.current_Lden);
        if (Number.isFinite(currentLden)) {
            html += `
                <tr>
                    <td>current_Lden</td>
                    <td>${safeToFixed(currentLden, 2)}</td>
                    <td>-</td>
                    <td class="pass">${safeToFixed(currentLden, 2)}</td>
                    <td>-</td>
                </tr>
            `;
        }

        const currentLnight = Number(standards?.current_Lnight);
        if (Number.isFinite(currentLnight)) {
            html += `
                <tr>
                    <td>current_Lnight</td>
                    <td>${safeToFixed(currentLnight, 2)}</td>
                    <td>-</td>
                    <td class="pass">${safeToFixed(currentLnight, 2)}</td>
                    <td>-</td>
                </tr>
            `;
        }

        const currentLaeq24h = Number(standards?.current_LAeq_24h);
        if (Number.isFinite(currentLaeq24h)) {
            html += `
                <tr>
                    <td>current_LAeq_24h</td>
                    <td>${safeToFixed(currentLaeq24h, 2)}</td>
                    <td>-</td>
                    <td class="pass">${safeToFixed(currentLaeq24h, 2)}</td>
                    <td>-</td>
                </tr>
            `;
        }
        
        for (const [areaType, info] of Object.entries(standards)) {
            if (areaType === 'current_leq' || areaType === 'current_Lden' || areaType === 'current_Lnight' || areaType === 'current_LAeq_24h') continue;

            const isScalar = (typeof info === 'string' || typeof info === 'number' || typeof info === 'boolean');
            const statusText = isScalar ? String(info) : (info?.status || 'N/A');
            const statusClass = statusText === 'PASS' ? 'pass' : (statusText === 'FAIL' ? 'fail' : '');
            const limit = isScalar ? 'N/A' : (info?.limit_db ?? 'N/A');
            const measured = isScalar ? 'N/A' : (info?.value_db ?? 'N/A');
            const exceeded = isScalar ? 'N/A' : (info?.exceeded_by_db ?? 'N/A');
            
            html += `
                <tr>
                    <td>${escapeHtml(areaType)}</td>
                    <td>${measured}</td>
                    <td>${limit}</td>
                    <td class="${statusClass}">${statusText}</td>
                    <td>${Number.isFinite(exceeded) ? safeToFixed(exceeded, 2) : exceeded}</td>
                </tr>
            `;
        }
        
        html += `
                    </tbody>
                </table>
            </div>
        `;
    }
    
    container.innerHTML = html;
}

function displayCharts(analysis) {
    // Plotly is loaded via CDN in index.html; if it's blocked/offline, don't crash analysis.
    if (typeof Plotly === 'undefined') {
        const timeChart = document.getElementById('timeSeriesChart');
        const threshChart = document.getElementById('thresholdChart');
        const msg = 'Charts unavailable: Plotly failed to load (CDN blocked/offline).';
        if (timeChart) timeChart.textContent = msg;
        if (threshChart) threshChart.textContent = msg;
        return;
    }

    const statistics = analysis.statistics || {};
    const percentiles = analysis.percentiles || {};
    
    const colNames = Object.keys(statistics);

    if (colNames.length === 0) {
        const threshChart = document.getElementById('thresholdChart');
        const timeChart = document.getElementById('timeSeriesChart');
        if (threshChart) threshChart.textContent = 'No statistics available.';
        if (timeChart) timeChart.textContent = 'No statistics available.';
        return;
    }

    // ── Percentile Exceedance Table ──
    const threshChart = document.getElementById('thresholdChart');
    if (threshChart && colNames.length > 0 && percentiles[colNames[0]]) {
        const colName = colNames[0];
        const pcts = percentiles[colName] || {};
        const pctDefs = [
            { key: 'L5',  label: 'L5',  meaning: 'Loudest 5% of events — acute noise peaks' },
            { key: 'L10', label: 'L10', meaning: 'Transient peak zone — dominant traffic maxima' },
            { key: 'L50', label: 'L50', meaning: 'Median acoustic climate — typical conditions' },
            { key: 'L90', label: 'L90', meaning: 'Statistical background floor — residual noise' },
            { key: 'L95', label: 'L95', meaning: 'Near-quietest 5% — persistent ambient floor' },
        ];

        function _badge(text, bg, color) {
            return `<span style="display:inline-block;padding:2px 7px;border-radius:4px;background:${bg};color:${color};font-weight:600;font-size:11px;white-space:nowrap;">${text}</span>`;
        }
        function whoStatusBadge(v) {
            if (!Number.isFinite(v)) return '<span style="color:#9ca3af;">—</span>';
            if (v > 65) return _badge('CRITICAL', '#fee2e2', '#dc2626');
            if (v > 53) return _badge('EXCEEDS Lden', '#fff7ed', '#ea580c');
            if (v > 45) return _badge('ABOVE Lnight', '#fefce8', '#ca8a04');
            return _badge('WITHIN LIMITS', '#f0fdf4', '#16a34a');
        }
        function epaStatusBadge(v) {
            if (!Number.isFinite(v)) return '<span style="color:#9ca3af;">—</span>';
            if (v > 70) return _badge('CRITICAL', '#fee2e2', '#dc2626');
            if (v > 55) return _badge('EXCEEDS REF', '#fff7ed', '#ea580c');
            return _badge('WITHIN REF', '#f0fdf4', '#16a34a');
        }
        function mdStatusBadge(v) {
            if (!Number.isFinite(v)) return '<span style="color:#9ca3af;">—</span>';
            if (v > 75) return _badge('CRITICAL', '#fee2e2', '#dc2626');
            if (v > 65) return _badge('EXCEEDS DAY', '#fff7ed', '#ea580c');
            if (v > 55) return _badge('EXCEEDS NIGHT', '#fefce8', '#ca8a04');
            return _badge('WITHIN LIMITS', '#f0fdf4', '#16a34a');
        }

        const th = (label, sub, w) =>
            `<th style="padding:10px 12px;border-bottom:2px solid #e2e8f0;font-weight:600;color:#374151;${w ? 'width:' + w + ';' : ''}vertical-align:bottom;">
                ${label}<br><span style="font-weight:400;font-size:10px;color:#9ca3af;">${sub}</span>
             </th>`;

        let thtml = `<div style="overflow-x:auto;margin:4px 0 12px;">
            <div style="font-size:12px;color:#6b7280;margin-bottom:8px;">Column: <strong>${escapeHtml(colName)}</strong></div>
            <table style="width:100%;border-collapse:collapse;font-size:13px;line-height:1.5;">
              <thead>
                <tr style="background:#f1f5f9;text-align:left;">
                  ${th('Level', 'Exceedance', '60px')}
                  ${th('Value', 'dB(A)', '95px')}
                  ${th('Acoustic Meaning', '', '')}
                  ${th('WHO 2018', 'Lden ≤53 / Lnight ≤45', '130px')}
                  ${th('EPA 1974', 'Outdoor ≤55 / Safety ≤70', '130px')}
                  ${th('Maryland COMAR', 'Res. Day ≤65 / Night ≤55', '140px')}
                </tr>
              </thead>
              <tbody>`;

        pctDefs.forEach((def, i) => {
            const v = Number(pcts[def.key]);
            const valStr = Number.isFinite(v) ? v.toFixed(1) : '—';
            const rowBg = i % 2 === 0 ? '#ffffff' : '#f9fafb';
            thtml += `<tr style="background:${rowBg};">
                <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;font-weight:700;color:#1e293b;">${escapeHtml(def.label)}</td>
                <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;font-weight:600;color:#0f172a;font-size:14px;">${valStr}</td>
                <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;color:#4b5563;">${escapeHtml(def.meaning)}</td>
                <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;">${whoStatusBadge(v)}</td>
                <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;">${epaStatusBadge(v)}</td>
                <td style="padding:10px 12px;border-bottom:1px solid #f1f5f9;">${mdStatusBadge(v)}</td>
              </tr>`;
        });

        thtml += `</tbody></table>
            <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:16px;font-size:11px;color:#6b7280;line-height:1.6;">
                <span><strong>WHO 2018:</strong> Road traffic Lden ≤ 53 dB(A), Lnight ≤ 45 dB(A) (Environmental Noise Guidelines for the European Region)</span>
                <span><strong>EPA 1974:</strong> Outdoor Leq(24h) ≤ 55 dB(A) to prevent activity interference; ≤ 70 dB(A) to prevent hearing loss</span>
                <span><strong>Maryland COMAR 26.02.03.03 Table 2:</strong> Residential receiving property — Daytime (7am–10pm) ≤ 65 dB(A), Nighttime (10pm–7am) ≤ 55 dB(A)</span>
            </div>
            <div style="margin-top:6px;font-size:11px;color:#94a3b8;font-style:italic;">Note: Lx percentile levels are instantaneous exceedance statistics and differ from time-weighted Leq or Lden metrics required by each standard. Comparison is indicative only.</div>
        </div>`;
        threshChart.innerHTML = thtml;
    }
    
    // Daily Acoustic Trend Chart
    const timeChart = document.getElementById('timeSeriesChart');
    if (!timeChart) return;

    const dailyRecords = window.computedDailySummary || [];

    // Fallback: if no daily data yet, use overall Lden/Lnight for a compliance summary panel
    if (!dailyRecords.length) {
        const env = (currentAnalysis && currentAnalysis.environmental_metrics) || {};
        const envEntry = Object.values(env).find(v => v && typeof v === 'object' && v.Lden !== undefined);
        if (envEntry) {
            const mLden   = Number(envEntry.Lden);
            const mLnight = Number(envEntry.Lnight);
            const ldenOk  = Number.isFinite(mLden);
            const lnightOk = Number.isFinite(mLnight);
            if (ldenOk || lnightOk) {
                const allV = [ldenOk ? mLden : 53, lnightOk ? mLnight : 45, 53, 45].filter(Number.isFinite);
                const yMax = Math.ceil(Math.max(...allV) + 8);
                const labels = [], measured = [], barColors = [], limits = [], annotations = [];
                if (ldenOk) {
                    labels.push('Lden (Day-Evening-Night)');
                    measured.push(mLden);
                    limits.push(53);
                    barColors.push(mLden > 53 ? 'rgba(239,68,68,0.78)' : 'rgba(16,185,129,0.78)');
                    annotations.push({
                        x: 'Lden (Day-Evening-Night)', y: mLden,
                        text: `${mLden.toFixed(1)} dB — ${mLden > 53 ? '⚠ +' + (mLden-53).toFixed(1) + ' dB' : '✓ PASS'}`,
                        xref: 'x', yref: 'y', showarrow: false, yanchor: 'bottom', yshift: 6,
                        font: { size: 11, color: mLden > 53 ? '#dc2626' : '#059669' },
                    });
                }
                if (lnightOk) {
                    labels.push('Lnight (23:00–07:00)');
                    measured.push(mLnight);
                    limits.push(45);
                    barColors.push(mLnight > 45 ? 'rgba(239,68,68,0.78)' : 'rgba(16,185,129,0.78)');
                    annotations.push({
                        x: 'Lnight (23:00–07:00)', y: mLnight,
                        text: `${mLnight.toFixed(1)} dB — ${mLnight > 45 ? '⚠ +' + (mLnight-45).toFixed(1) + ' dB' : '✓ PASS'}`,
                        xref: 'x', yref: 'y', showarrow: false, yanchor: 'bottom', yshift: 6,
                        font: { size: 11, color: mLnight > 45 ? '#dc2626' : '#059669' },
                    });
                }
                try {
                    Plotly.newPlot(timeChart, [
                        { x: labels, y: limits, name: 'WHO 2018 Limit', type: 'bar',
                          marker: { color: 'rgba(100,116,139,0.25)', line: { color: 'rgba(100,116,139,0.7)', width: 1.5 }, pattern: { shape: '/' } },
                          hovertemplate: 'WHO Limit: %{y} dB(A)<extra></extra>' },
                        { x: labels, y: measured, name: 'Measured Level', type: 'bar',
                          marker: { color: barColors }, hovertemplate: 'Measured: %{y:.1f} dB(A)<extra></extra>' },
                    ], {
                        title: { text: 'Overall WHO 2018 Road Traffic Compliance (Monitoring Period)', font: { size: 14, color: '#1e293b' }, x: 0.5, xanchor: 'center' },
                        barmode: 'group', bargap: 0.35,
                        xaxis: { automargin: true, tickfont: { size: 13 } },
                        yaxis: { title: 'Noise Level dB(A)', range: [0, yMax], gridcolor: 'rgba(0,0,0,0.07)', zeroline: false },
                        margin: { t: 55, b: 80, l: 65, r: 40 }, height: 400,
                        legend: { orientation: 'h', y: -0.22, xanchor: 'center', x: 0.5 },
                        plot_bgcolor: '#fafafa', paper_bgcolor: 'white',
                        annotations,
                    }, { responsive: true, displayModeBar: false, displaylogo: false });
                } catch(e) { console.error('Fallback compliance chart failed:', e); }
                return;
            }
        }
        timeChart.innerHTML = '<p style="padding:30px;color:#6b7280;text-align:center;font-size:13px;">Daily trend requires timestamped data covering multiple days. Running analysis with your file will populate this chart.</p>';
        return;
    }

    // Sort by date and extract per-day values
    const sorted = [...dailyRecords]
        .filter(r => r.Date && Number.isFinite(Number(r.Average_L_EQ_dB)))
        .sort((a, b) => new Date(a.Date) - new Date(b.Date));

    if (!sorted.length) {
        timeChart.innerHTML = '<p style="padding:30px;color:#6b7280;text-align:center;font-size:13px;">No valid daily averages found in this dataset.</p>';
        return;
    }

    const dates       = sorted.map(r => String(r.Date).substring(0, 10));
    const laeqDaily   = sorted.map(r => Number(r.Average_L_EQ_dB));
    const nightlyLaeq = sorted.map(r => {
        const v = Number(r.Nighttime_LAeq);
        return Number.isFinite(v) ? v : null;
    });
    const hasNightData = nightlyLaeq.some(v => v !== null);

    // Use spline for smooth curves when ≥3 points, straight lines otherwise
    const lineShape = sorted.length >= 3 ? 'spline' : 'linear';
    // For single-day datasets render markers-only (no connecting line)
    const lineMode  = sorted.length === 1 ? 'markers' : 'lines+markers';

    const allVals = [...laeqDaily, ...nightlyLaeq.filter(v => v !== null), 53, 45];
    const tYMax = Math.ceil(Math.max(...allVals) + 6);
    const tYMin = Math.floor(Math.min(...allVals) - 4);

    const trendData = [
        {
            x: dates, y: laeqDaily,
            name: '24-hr LAeq',
            type: 'scatter', mode: lineMode,
            line: { color: '#3D5A80', width: 2.5, shape: lineShape },
            marker: { size: sorted.length === 1 ? 12 : 7, color: '#3D5A80', symbol: 'circle' },
            connectgaps: false,
            hovertemplate: '<b>%{x}</b><br>24-hr LAeq: %{y:.1f} dB(A)<extra></extra>',
        },
    ];
    if (hasNightData) {
        trendData.push({
            x: dates, y: nightlyLaeq,
            name: 'Nighttime LAeq (22:00–07:00)',
            type: 'scatter', mode: lineMode,
            line: { color: '#7C3AED', width: 2, dash: 'dot', shape: lineShape },
            marker: { size: sorted.length === 1 ? 10 : 6, color: '#7C3AED', symbol: 'diamond' },
            connectgaps: false,
            hovertemplate: '<b>%{x}</b><br>Nighttime LAeq: %{y:.1f} dB(A)<extra></extra>',
        });
    }

    const trendLayout = {
        title: { text: 'Daily Acoustic Trend vs. WHO 2018 Reference Levels', font: { size: 14, color: '#1e293b' }, x: 0.5, xanchor: 'center' },
        xaxis: {
            title: dates.length > 1 ? 'Date' : '', automargin: true,
            tickangle: dates.length > 7 ? -40 : 0,
            tickfont: { size: 11 }, gridcolor: 'rgba(0,0,0,0.06)',
            type: dates.length > 1 ? 'category' : 'category',
        },
        yaxis: { title: 'Noise Level dB(A)', range: [tYMin, tYMax], gridcolor: 'rgba(0,0,0,0.07)', zeroline: false },
        margin: { t: 55, b: dates.length > 7 ? 100 : 70, l: 65, r: 40 },
        height: 460,
        legend: { orientation: 'h', y: -0.22, xanchor: 'center', x: 0.5 },
        plot_bgcolor: '#fafafa',
        paper_bgcolor: 'white',
        shapes: [
            // Shaded "exceedance" bands removed. The traces here are daily LAeq
            // values; the 53 and 45 dB lines are the WHO Lden and Lnight guideline
            // VALUES, which are penalty-weighted long-term averages. Colouring the
            // area above them made each day read as a pass or fail against a
            // guideline that does not apply to a single day's LAeq.
            { type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 53, y1: 53,
              line: { color: 'rgba(239,68,68,0.65)', width: 1.5, dash: 'dash' } },
            { type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 45, y1: 45,
              line: { color: 'rgba(245,158,11,0.65)', width: 1.5, dash: 'dash' } },
        ],
        annotations: [
            { xref: 'paper', x: 0.01, y: 53, text: '53 dB(A) reference', showarrow: false,
              font: { size: 10, color: 'rgba(220,38,38,0.85)' }, xanchor: 'left', yanchor: 'bottom', yshift: 3 },
            { xref: 'paper', x: 0.01, y: 45, text: '45 dB(A) reference', showarrow: false,
              font: { size: 10, color: 'rgba(180,100,0,0.85)' }, xanchor: 'left', yanchor: 'bottom', yshift: 3 },
        ],
    };

    try {
        Plotly.newPlot(timeChart, trendData, trendLayout, { responsive: true, displayModeBar: true, displaylogo: false });
    } catch (e) {
        console.error('Daily trend chart failed:', e);
        if (timeChart) timeChart.textContent = 'Daily trend chart failed to render.';
    }
}

function displayStandards(_standards) {
    const container = document.getElementById('standardsComparisonContainer');

    // ── Section builder helpers ─────────────────────────────────────────────
    function sectionHeader(icon, title, subtitle, accentColor) {
        return `
        <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:14px;">
          <div style="font-size:22px;line-height:1;">${icon}</div>
          <div>
            <div style="font-size:15px;font-weight:700;color:#1e293b;">${title}</div>
            <div style="font-size:12px;color:#94a3b8;margin-top:2px;">${subtitle}</div>
          </div>
        </div>`;
    }

    function stdTable(headers, rows, accent) {
        const thStyle = `padding:9px 14px;border-bottom:2px solid ${accent}22;font-weight:600;color:#475569;font-size:12px;text-transform:uppercase;letter-spacing:0.04em;background:#f8fafc;`;
        const tdStyle = 'padding:9px 14px;border-bottom:1px solid #f1f5f9;font-size:13px;color:#334155;';
        let h = `<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.07);">`;
        h += `<thead><tr>${headers.map(hd => `<th style="${thStyle}">${hd}</th>`).join('')}</tr></thead><tbody>`;
        rows.forEach((row, i) => {
            const bg = i % 2 === 0 ? '#fff' : '#fafbfc';
            h += `<tr style="background:${bg};">${row.map((cell, ci) => `<td style="${tdStyle}${ci === 0 ? 'font-weight:600;' : ''}">${cell}</td>`).join('')}</tr>`;
        });
        h += `</tbody></table></div>`;
        return h;
    }

    function limitBadge(val) {
        return `<span style="font-weight:700;color:#1e293b;font-size:13px;">${val}</span>`;
    }
    function strengthBadge(strength) {
        const map = {
            'Strong': ['#dcfce7', '#166534'],
            'Conditional': ['#fef9c3', '#854d0e'],
            'WHO 2018': ['#eff6ff', '#1d4ed8'],
            'WHO 1999': ['#f5f3ff', '#6d28d9'],
            'Maryland': ['#fff7ed', '#c2410c'],
            'OSHA': ['#fef2f2', '#991b1b'],
            'NIOSH': ['#fdf4ff', '#86198f'],
            'EU Directive': ['#f0fdf4', '#15803d'],
            'EPA 1974': ['#ecfeff', '#0e7490'],
        };
        const [bg, color] = map[strength] || ['#f1f5f9', '#475569'];
        return `<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:${bg};color:${color};font-weight:600;font-size:11px;">${strength}</span>`;
    }

    const section = (content) => `<div style="background:#fff;border-radius:12px;padding:22px 24px;margin-bottom:20px;border:1px solid #e2e8f0;box-shadow:0 1px 4px rgba(0,0,0,0.06);">${content}</div>`;

    // ── 1. WHO 2018 Environmental Noise Guidelines ──────────────────────────
    const s1 = section(`
        ${sectionHeader('🌍', 'WHO 2018 Environmental Noise Guidelines', 'Environmental Noise Guidelines for the European Region — World Health Organization, 2018', '#3b82f6')}
        <p style="font-size:12px;color:#64748b;margin:0 0 14px;">These are health-based recommendations expressed as annual average outdoor noise levels. They apply to transport noise sources near residential areas.</p>
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
            '#3b82f6'
        )}
        <div style="margin-top:10px;background:#eff6ff;border-radius:8px;padding:11px 13px;font-size:12px;color:#1e3a8a;line-height:1.6;">
          <strong>Strong vs Conditional.</strong> WHO grades a recommendation <em>strong</em> when it is confident the benefits outweigh the harms and it can be adopted as policy in most circumstances; <em>conditional</em> when the evidence base is thinner and a policy-maker needs to weigh local factors. A conditional recommendation is still a recommendation — the difference is in the certainty of the evidence, not in whether the level matters.
          <br><br><strong>These are annual averages</strong>, and they are outdoor levels. Comparing them against a week or a month is indicative only — see &ldquo;What can I report from my measurement?&rdquo; above.
        </div>
        <p style="font-size:11px;color:#94a3b8;margin:10px 0 0;">Source: WHO, <em>Environmental Noise Guidelines for the European Region</em> (2018), ISBN 978-92-890-5356-3. Indicator definitions follow EU Directive 2002/49/EC Annex I. Values and gradings verified 5 Aug 2026.</p>
    `);

    // ── 2. WHO 1999 Indoor / Community Guidelines ───────────────────────────
    const s2 = section(`
        ${sectionHeader('🏠', 'WHO 1999 Indoor & Community Noise Guidelines', 'Guidelines for Community Noise — World Health Organization, 1999 (Berglund, Lindvall & Schwela)', '#8b5cf6')}
        <p style="font-size:12px;color:#64748b;margin:0 0 14px;">These guidelines define maximum indoor noise levels to protect health and well-being. They apply to the indoor acoustic environment of buildings.</p>
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
            '#8b5cf6'
        )}
        <p style="font-size:11px;color:#94a3b8;margin:10px 0 0;">Source: WHO (1999) Guidelines for Community Noise, Geneva. Edited by Berglund B, Lindvall T, Schwela DH. ISBN 92-4-154553-4.</p>
    `);

    // ── 3. Maryland COMAR 26.02.03 ─────────────────────────────────────────
    // Two different tables in two different regulations, previously merged into
    // one and cited to the wrong section. Table 2 (.03) is the enforceable
    // limit; Table 1 (.02) is the state's goal and is stated on Ldn.
    const s3 = section(`
        ${sectionHeader('⚖️', 'Maryland COMAR 26.02.03 — Noise Control', 'Code of Maryland Regulations — Control of Noise Pollution, Maryland Department of the Environment', '#f97316')}
        <p style="font-size:12px;color:#64748b;margin:0 0 6px;"><strong>Enforceable limits — COMAR 26.02.03.03, Table 2.</strong> &ldquo;A person may not cause or permit noise levels which exceed those specified in Table 2.&rdquo; Measured at or within the property line of the <em>receiving</em> property (.03D(2)).</p>
        ${stdTable(
            ['Receiving Zone', 'Day (7 am – 10 pm)', 'Night (10 pm – 7 am)', 'Averaging period'],
            [
                ['Residential', limitBadge('65 dB(A)'), limitBadge('55 dB(A)'), 'Not stated in the regulation'],
                ['Commercial', limitBadge('67 dB(A)'), limitBadge('62 dB(A)'), 'Not stated in the regulation'],
                ['Industrial', limitBadge('75 dB(A)'), limitBadge('75 dB(A)'), 'Not stated in the regulation'],
            ],
            '#f97316'
        )}
        <div style="margin-top:10px;background:#fff7ed;border-radius:8px;padding:11px 13px;font-size:12px;color:#7c2d12;line-height:1.6;">
          <strong>On the averaging period.</strong> Table 2 is headed &ldquo;Maximum Allowable Noise Levels (dBA)&rdquo; and gives no averaging time. COMAR defines &ldquo;equivalent sound level&rdquo; (.01B(13)) and says the <em>standards</em> in Table 1 are expressed in equivalent levels, but says nothing of the kind about Table 2. This platform compares Table 2 against the <strong>LAeq of the period</strong>, which is the common reading — a not-to-exceed reading of the same table would be stricter. Prominent discrete tones and periodic noises must be <strong>5 dB(A) below</strong> these levels (.03A(3)).
        </div>
        <p style="font-size:12px;color:#64748b;margin:16px 0 6px;"><strong>State goals — COMAR 26.02.03.02, Table 1.</strong> &ldquo;Goals for the attainment of an adequate environment&rdquo;, which the Table 2 limits above are intended to achieve. These are targets, not levels a person may not exceed.</p>
        ${stdTable(
            ['Zoning District', 'Level', 'Metric', 'Averaging period'],
            [
                ['Residential', limitBadge('55 dB(A)'), 'Ldn', '24 hours, +10 dB applied to 10 pm – 7 am'],
                ['Commercial', limitBadge('64 dB(A)'), 'Ldn', '24 hours, +10 dB applied to 10 pm – 7 am'],
                ['Industrial', limitBadge('70 dB(A)'), 'Leq(24)', '24 hours, no penalty'],
            ],
            '#f97316'
        )}
        <p style="font-size:11px;color:#94a3b8;margin:10px 0 0;">Day and night hours are defined in COMAR 26.02.03.01B(5) and B(15); Ldn in B(4). Verified against the regulation text, 5 Aug 2026. Note that Maryland's night starts at 10 pm, an hour earlier than the 11 pm night used by WHO Lnight — the two cover different windows and are not interchangeable.</p>
    `);

    // ── 4. Occupational Standards ──────────────────────────────────────────
    const s4 = section(`
        ${sectionHeader('🏭', 'Occupational Noise Exposure Standards', 'For workplace noise — not directly applicable to community or environmental monitoring', '#ef4444')}
        <p style="font-size:12px;color:#64748b;margin:0 0 14px;">These apply to workers exposed to noise during an 8-hour workday. Environmental or community data should <em>not</em> be compared directly against these occupational limits without appropriate dose-calculation methodology.</p>
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
            '#ef4444'
        )}
        <div style="margin-top:12px;background:#fef2f2;border-radius:8px;padding:12px 14px;font-size:12px;color:#7f1d1d;line-height:1.6;">
          <strong>OSHA Table G-16 (duration guide):</strong> 90 dBA → 8 hr · 95 dBA → 4 hr · 100 dBA → 2 hr · 105 dBA → 1 hr · 110 dBA → 30 min · 115 dBA → 15 min.
          At ≥ 115 dBA exposure is impermissible without engineering controls.
        </div>
        <p style="font-size:11px;color:#94a3b8;margin:10px 0 0;">NIOSH uses a 3 dB exchange rate (equal-energy principle); OSHA uses a 5 dB rate. The NIOSH 85 dB REL is more protective and is recommended for hearing conservation program design.</p>
    `);

    // ── 5. EPA 1974 Community Reference Levels ────────────────────────────
    const s5 = section(`
        ${sectionHeader('📋', 'US EPA 1974 — Levels of Environmental Noise', '"Levels Document" — EPA 550/9-74-004 — Informational Reference (not a federal regulation)', '#0ea5e9')}
        <p style="font-size:12px;color:#64748b;margin:0 0 14px;">The EPA 1974 Levels Document identifies noise levels that protect public health and welfare with an adequate margin of safety. These are reference levels, not enforceable federal noise standards (EPA's noise enforcement authority was transferred to states in 1982).</p>
        ${stdTable(
            ['Purpose / Protection Goal', 'Metric', 'Reference Level', 'Area Type'],
            [
                ['Protect against hearing loss (lifetime exposure)', 'Leq(24h)', limitBadge('≤ 70 dB(A)'), 'All environments'],
                ['Prevent activity interference & annoyance', 'Ldn (day-night avg)', limitBadge('≤ 55 dB(A)'), 'Outdoor residential'],
                ['Prevent activity interference & annoyance', 'Leq (24h)', limitBadge('≤ 45 dB(A)'), 'Indoor residential / schools / hospitals'],
            ],
            '#0ea5e9'
        )}
        <p style="font-size:11px;color:#94a3b8;margin:10px 0 0;">Source: US EPA (1974). Information on Levels of Environmental Noise Requisite to Protect Public Health and Welfare with an Adequate Margin of Safety. EPA/ONAC 550/9-74-004.</p>
    `);

    // ── 0. How to read a noise limit ───────────────────────────────────────
    // Placed first because every table below is unreadable without it: a limit
    // is a number AND an averaging period, and comparing the right number over
    // the wrong period is the most common way these figures get misused.
    const s0 = section(`
        ${sectionHeader('📖', 'How to read a noise limit', 'Every limit below is a number plus an averaging period. Both matter.', '#0f766e')}
        <p style="font-size:13px;color:#334155;margin:0 0 12px;line-height:1.7;">
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
            '#0f766e'
        )}
        <div style="margin-top:12px;background:#f0fdfa;border-radius:8px;padding:12px 14px;font-size:12px;color:#134e4a;line-height:1.65;">
          <strong>Two traps worth knowing.</strong>
          <br>· <strong>Night is not one thing.</strong> WHO L<sub>night</sub> runs 23:00–07:00 (8 h). Maryland's night runs 22:00–07:00 (9 h). A figure computed on one window cannot be checked against the other's limit.
          <br>· <strong>L<sub>den</sub> is penalty-weighted.</strong> Its 53 dB is not on the scale of anything your meter displays, because evening and night readings are raised by 5 and 10 dB before averaging. No single reading can be compared against it.
        </div>
    `);

    // ── 0b. Choosing an averaging period for your own monitoring ───────────
    const s0b = section(`
        ${sectionHeader('📅', 'What can I report from my measurement?', 'WHO Lden and Lnight are defined as YEARLY averages. Shorter records are still useful — but say what they are.', '#7c3aed')}
        <p style="font-size:13px;color:#334155;margin:0 0 12px;line-height:1.7;">
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
            '#7c3aed'
        )}
        <div style="margin-top:12px;background:#faf5ff;border-radius:8px;padding:12px 14px;font-size:12px;color:#581c87;line-height:1.65;">
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
        <div style="max-width:900px;">
          <div style="margin-bottom:20px;">
            <h3 style="margin:0 0 4px;font-size:17px;font-weight:700;color:#1e293b;">Noise Standards Reference</h3>
            <p style="margin:0;font-size:12px;color:#94a3b8;">Every value below was checked against the primary source document, not a secondary summary. Each limit is shown with the averaging period it is defined over. Start with &ldquo;How to read a noise limit&rdquo; if any of the metrics are unfamiliar.</p>
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
            displayCharts(currentAnalysis);
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

    // Use hourly summary data (Hour 0-23) for an hourly diurnal trend.
    // Fall back to daily summary if hourly is not yet available.
    const hourlySummary = window.computedHourlySummary;
    const dailySummary  = window.computedDailySummary;

    if (!hourlySummary && !dailySummary) {
        container.innerHTML = '<p style="padding:20px;color:#666;text-align:center;">Hourly data not yet loaded.</p>';
        return;
    }

    try {
        let xLabels, yLaeq, xTitle, chartTitle;

        let hourValues = null;  // numeric hour per x point (hourly mode only)
        if (hourlySummary && hourlySummary.length > 0) {
            // ── HOURLY mode: Hour 0-23 averaged across all monitoring days ──
            const sorted = [...hourlySummary].sort((a, b) => Number(a.Hour) - Number(b.Hour));
            hourValues = sorted.map(h => Number(h.Hour));
            xLabels = sorted.map(h => `${String(Number(h.Hour)).padStart(2, '0')}:00`);
            yLaeq   = sorted.map(h => Number(h.Average_L_EQ_dB));
            xTitle      = 'Hour of Day';
            chartTitle  = '24-Hour Diurnal LAeq Profile';
        } else {
            // ── DAILY fallback ──
            const validPairs = dailySummary
                .filter(d => d.Date && Number.isFinite(Number(d.Average_L_EQ_dB)))
                .map(d => ({ date: d.Date, laeq: Number(d.Average_L_EQ_dB) }));
            if (validPairs.length === 0) {
                container.innerHTML = '<p style="padding:20px;color:#666;text-align:center;">Not enough data to plot.</p>';
                return;
            }
            xLabels = validPairs.map(p => p.date);
            yLaeq   = validPairs.map(p => p.laeq);
            xTitle      = 'Date';
            chartTitle  = 'Daily LAeq Trend with Rolling Median';
        }

        const validY = yLaeq.filter(Number.isFinite);
        if (validY.length === 0) {
            container.innerHTML = '<p style="padding:20px;color:#666;text-align:center;">No valid LAeq values found.</p>';
            return;
        }

        // Fill area under line for better readability
        const areaTrace = {
            x: xLabels, y: yLaeq,
            type: 'scatter', mode: 'none',
            fill: 'tozeroy',
            fillcolor: 'rgba(61, 90, 128, 0.08)',
            showlegend: false,
            hoverinfo: 'skip',
        };

        const rawTrace = {
            x: xLabels, y: yLaeq,
            name: 'Hourly LAeq',
            type: 'scatter', mode: 'lines+markers',
            line: { color: '#3D5A80', width: 2.5 },
            marker: { size: 7, color: '#3D5A80', symbol: 'circle' },
            hovertemplate: '%{x}<br>LAeq: %{y:.1f} dB(A)<extra></extra>',
        };

        const allVals = validY.concat([53, 45]);
        const yMin = Math.floor(Math.min(...allVals) - 4);
        const yMax = Math.ceil(Math.max(...allVals) + 6);

        const shapes = [
            { type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 53, y1: 53,
              line: { color: 'rgba(239,68,68,0.55)', width: 1.5, dash: 'dot' } },
            { type: 'line', xref: 'paper', x0: 0, x1: 1, y0: 45, y1: 45,
              line: { color: 'rgba(200,120,0,0.55)', width: 1.5, dash: 'dot' } },
        ];

        const annotations = [
            { xref: 'paper', x: 0.99, y: 53, text: '53 dB reference',
              showarrow: false, font: { size: 10, color: 'rgba(220,38,38,0.85)' },
              xanchor: 'right', yanchor: 'bottom', yshift: 3 },
            { xref: 'paper', x: 0.99, y: 45, text: '45 dB reference',
              showarrow: false, font: { size: 10, color: 'rgba(180,100,0,0.85)' },
              xanchor: 'right', yanchor: 'bottom', yshift: 3 },
        ];

        // Shade the WHO Lnight window (23:00–07:00) on the categorical hour axis.
        // Plotly maps categories to integer positions 0..23, so we shade by index.
        if (hourValues && hourValues.length) {
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
                    fillcolor: 'rgba(30, 58, 95, 0.07)',
                    line: { width: 0 }, layer: 'below',
                });
            });
            annotations.push({
                xref: 'paper', yref: 'paper', x: 0.01, y: 0.98,
                text: '▮ Shaded = WHO night window (23:00–07:00)',
                showarrow: false, font: { size: 10, color: 'rgba(30,58,95,0.65)' },
                xanchor: 'left', yanchor: 'top',
            });
        }

        const layout = {
            title: { text: chartTitle, font: { size: 15, color: '#1e293b' }, x: 0.5, xanchor: 'center' },
            xaxis: { title: xTitle, automargin: true, showgrid: true, gridcolor: 'rgba(0,0,0,0.06)' },
            yaxis: { title: 'LAeq dB(A)', range: [yMin, yMax], gridcolor: 'rgba(0,0,0,0.06)', zeroline: false },
            margin: { t: 60, b: 70, l: 65, r: 40 },
            height: 450,
            hovermode: 'x unified',
            shapes, annotations,
            legend: { orientation: 'h', y: -0.2, xanchor: 'center', x: 0.5 },
            plot_bgcolor: '#fafafa',
            paper_bgcolor: 'white',
        };

        Plotly.newPlot(container, [areaTrace, rawTrace], layout,
                       { responsive: true, displayModeBar: true, displaylogo: false });
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
                filters: currentFilters?.exclusions?.length || currentFilters?.bound_start ? currentFilters : null,
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
                filters: currentFilters?.exclusions?.length || currentFilters?.bound_start ? currentFilters : null,
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

    // Colour the card border/background by concern level
    const isHigh    = /concern level:\s*HIGH|concern level:\s*SERIOUS/i.test(text);
    const isMod     = /concern level:\s*MODERATE/i.test(text);
    if (isHigh) {
        card.style.borderLeftColor = '#991b1b';
        card.style.background      = '#FEF2F2';
    } else if (isMod) {
        card.style.borderLeftColor = '#b45309';
        card.style.background      = '#FFFBEB';
    } else {
        card.style.borderLeftColor = '#166534';
        card.style.background      = '#F0FDF4';
    }

    // Render structured paragraphs and bullets from \n\n-separated text
    const htmlParts = [];
    for (const para of text.split('\n\n')) {
        const lines = para.split('\n');
        const bulletLines = lines.filter(l => l.trim().startsWith('•'));
        const headerLines = lines.filter(l => !l.trim().startsWith('•'));
        const headerText  = headerLines.join(' ').trim();
        if (headerText) {
            const isStrong = headerText.startsWith('WHO ') || headerText.startsWith('Overall Concern');
            const escaped  = headerText.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
            htmlParts.push(isStrong
                ? `<p style="margin:8px 0 4px"><strong>${escaped}</strong></p>`
                : `<p style="margin:8px 0 4px">${escaped}</p>`);
        }
        if (bulletLines.length) {
            const items = bulletLines.map(l => {
                const content = l.trim().replace(/^•\s*/, '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                return `<li style="margin-bottom:4px">${content}</li>`;
            }).join('');
            htmlParts.push(`<ul style="margin:4px 0 8px 20px;padding:0">${items}</ul>`);
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
            filters: (currentFilters && (currentFilters.exclusions.length || currentFilters.bound_start || currentFilters.bound_end)) ? currentFilters : null,
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
        body: JSON.stringify({ filepath: uploadedFilepath }),
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
        body: JSON.stringify({ filepath: uploadedFilepath }),
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
        body: JSON.stringify({
            filepath: uploadedFilepath
        })
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
    if (statusIndicator) statusIndicator.className = `status ${status}`;
    if (statusText) statusText.textContent = text;
}

function escapeHtml(text) {
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
}

function exportDailySummary() {
    if (!uploadedFilepath) {
        showError('No file to export');
        return;
    }
    
    setStatus('processing', 'Generating daily summary...');
    
    fetch('/api/export-daily-summary-csv', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            filepath: uploadedFilepath
        })
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
        a.download = `DAILY_SUMMARY_${new Date().toISOString().split('T')[0]}.csv`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
        setStatus('idle', 'Daily summary generated');
        showNotification('Daily summary generated successfully!');
    })
    .catch(error => {
        showError('Error generating daily summary: ' + error.message);
        setStatus('error', 'Daily summary failed');
    });
}

function exportHourlySummary() {
    if (!uploadedFilepath) {
        showError('No file to export');
        return;
    }
    
    setStatus('processing', 'Generating hourly summary...');
    
    fetch('/api/export-hourly-summary-csv', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            filepath: uploadedFilepath
        })
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
        a.download = `HOURLY_SUMMARY_${new Date().toISOString().split('T')[0]}.csv`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
        setStatus('idle', 'Hourly summary generated');
        showNotification('Hourly summary generated successfully!');
    })
    .catch(error => {
        showError('Error generating hourly summary: ' + error.message);
        setStatus('error', 'Hourly summary failed');
    });
}

function exportWeeklySummary() {
    if (!uploadedFilepath) {
        showError('No file to export');
        return;
    }
    
    setStatus('processing', 'Generating weekly summary...');
    
    fetch('/api/export-weekly-summary', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            filepath: uploadedFilepath
        })
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
        a.download = `weekly_summary_${new Date().toISOString().split('T')[0]}.xlsx`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
        setStatus('idle', 'Weekly summary generated');
        showNotification('Weekly summary generated successfully!');
    })
    .catch(error => {
        showError('Error generating weekly summary: ' + error.message);
        setStatus('error', 'Weekly summary failed');
    });
}

function generateAdvancedCharts() {
    if (!uploadedFilepath) {
        showError('No file to generate charts for');
        return;
    }
    
    setStatus('processing', 'Generating advanced charts...');
    
    fetch('/api/generate-advanced-charts', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            filepath: uploadedFilepath
        })
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
        a.download = `advanced_charts_${new Date().toISOString().split('T')[0]}.html`;
        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        document.body.removeChild(a);
        setStatus('idle', 'Advanced charts generated');
        showNotification('Advanced charts generated successfully! Opening in a new window...');
        window.open(url, '_blank');
    })
    .catch(error => {
        showError('Error generating advanced charts: ' + error.message);
        setStatus('error', 'Chart generation failed');
    });
}

function showNotification(message) {
    const notification = document.createElement('div');
    notification.style.cssText = `
        background: #27ae60;
        color: white;
        padding: 15px 20px;
        border-radius: 6px;
        position: fixed;
        bottom: 20px;
        right: 20px;
        z-index: 1000;
        animation: slideIn 0.3s ease-out;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
    `;
    notification.textContent = message;
    document.body.appendChild(notification);
    
    setTimeout(() => {
        notification.style.animation = 'slideOut 0.3s ease-out';
        setTimeout(() => {
            document.body.removeChild(notification);
        }, 300);
    }, 3000);
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
            <span class="staged-file-icon">📄</span>
            <input type="text" class="compare-label-input" value="${escapeHtml(f._label || '')}"
                   placeholder="Label for this location"
                   oninput="setCompareLabel(${i}, this.value)" title="This name appears in the comparison charts and report">
            <span class="staged-file-name compare-file-orig" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
            <span class="staged-file-size">${(f.size / 1024).toFixed(1)} KB</span>
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
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Analyzing…'; }
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
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-balance-scale"></i> Compare Files'; }
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
        if (v <= limit) return `<span class="cmp-badge ok"><i class="fas fa-check"></i> Within guideline</span>`;
        return `<span class="cmp-badge bad"><i class="fas fa-triangle-exclamation"></i> Above by ${(v - limit).toFixed(1)} dB</span>`;
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
                <div class="cmp-metric-top"><span class="cmp-metric-label">Day &amp; night (Lden)</span><span class="cmp-metric-val">${val(d.lden)}</span></div>
                ${badge(d.lden, 53)}
            </div>
            <div class="cmp-metric">
                <div class="cmp-metric-top"><span class="cmp-metric-label">Night (Lnight)</span><span class="cmp-metric-val">${val(d.lnight)}</span></div>
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
    el.innerHTML = `<div class="cv-title"><i class="fas fa-circle-info"></i> What the comparison shows</div>
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
        { label: 'Average level (LAeq)',     key: 'laeq',           fmt: v => v != null ? `${_fmt(v)} dB` : 'N/A', who: null },
        { label: 'Day-night (Lden)',         key: 'lden',           fmt: v => v != null ? `${_fmt(v)} dB` : 'N/A', who: { limit: WHO_DAY,   higher_is_bad: true } },
        { label: 'Night (Lnight)',           key: 'lnight',         fmt: v => v != null ? `${_fmt(v)} dB` : 'N/A', who: { limit: WHO_NIGHT, higher_is_bad: true } },
        { label: 'Loudest moment (LAmax)',   key: 'lmax',           fmt: v => v != null ? `${_fmt(v)} dB` : 'N/A', who: null },
        { label: 'Quiet background (L90)',   key: 'l90',            fmt: v => v != null ? `${_fmt(v)} dB` : 'N/A', who: null },
    ];

    const headerRow = `<tr><th class="metric-label">Metric</th>${datasets.map(d => `<th>${d.name}</th>`).join('')}</tr>`;
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
    const COLORS = ['#0066ff', '#e74c3c', '#27ae60', '#f39c12', '#9b59b6', '#1abc9c'];

    const traces = datasets.map((d, i) => ({
        type: 'scatter', mode: 'lines+markers', name: d.name,
        x: hours, y: d.diurnal || Array(24).fill(null),
        line: { color: COLORS[i % COLORS.length], width: 2.5 },
        marker: { size: 5 }, connectgaps: false,
    }));

    // WHO reference bands
    traces.push({
        type: 'scatter', mode: 'lines', name: '45 dB reference',
        x: hours, y: Array(24).fill(45),
        line: { color: '#8e44ad', dash: 'dash', width: 1.5 },
        showlegend: true
    });
    traces.push({
        type: 'scatter', mode: 'lines', name: '53 dB reference',
        x: hours, y: Array(24).fill(53),
        line: { color: '#c0392b', dash: 'dot', width: 1.5 },
        showlegend: true
    });

    const layout = {
        xaxis: { title: 'Hour of Day', tickangle: -45 },
        yaxis: { title: 'LAeq dB(A)' },
        legend: { orientation: 'h', y: -0.25 },
        margin: { l: 55, r: 20, t: 30, b: 80 },
        plot_bgcolor: '#f8fafc', paper_bgcolor: 'white',
        height: 420,
    };

    Plotly.newPlot(el, traces, layout, { responsive: true, displayModeBar: true });
}

async function downloadCompareReport() {
    if (!compareDatasets || compareDatasets.length < 2) {
        alert('Run a comparison first before downloading the report.');
        return;
    }
    const btn = document.getElementById('compareReportBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Generating…'; }

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
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-file-pdf"></i> Download Comparison Report (PDF)'; }
    }
}
