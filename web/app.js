/* ============================================================
   LedgerLens AI — Frontend Application Logic
   Talks to the real FastAPI backend (api.py), which wraps the same
   build_graph() pipeline approval_cli.py uses. Every action here —
   upload, approve, reject, edit — maps 1:1 to a graph invoke/resume,
   not a simulation.
   ============================================================ */

// Point this at wherever `uvicorn api:app` is running. Override by
// setting `window.LEDGERLENS_API_BASE = 'https://your-host'` before
// this script loads if it's not on localhost:8000.
const API_BASE = (window.LEDGERLENS_API_BASE || 'http://localhost:8000') + '/api';

// ============================================================
// AUTH — email/password via Supabase Auth (through api.py)
// ============================================================
// Every request scoped to a user's data now carries a real Supabase
// session token instead of an anonymous per-browser UUID.
const AUTH_STORAGE_KEY = 'ledgerlens_auth';
// Endpoints that don't require a signed-in user (sample invoices and
// the 5 built-in vendor contracts stay available to everybody).
const PUBLIC_API_PATHS = [
    '/auth/signup', '/auth/login',
    '/sample-invoices', '/contracts', '/health',
];

function getAuth() {
    try { return JSON.parse(localStorage.getItem(AUTH_STORAGE_KEY) || 'null'); }
    catch { return null; }
}
function setAuth(auth) {
    if (auth) localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(auth));
    else localStorage.removeItem(AUTH_STORAGE_KEY);
}
function isPublicApiPath(url) {
    const path = url.slice(API_BASE.length);
    return PUBLIC_API_PATHS.some(p => path === p || path.startsWith(p + '?') || path.startsWith(p + '/'));
}

const nativeFetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    if (!url.startsWith(API_BASE)) return nativeFetch(input, init);

    const headers = new Headers(init.headers || (typeof input === 'object' ? input.headers : undefined));
    const auth = getAuth();
    if (auth && auth.access_token) headers.set('Authorization', `Bearer ${auth.access_token}`);

    const res = await nativeFetch(input, { ...init, headers });
    if (res.status === 401 && !isPublicApiPath(url)) {
        setAuth(null);
        showAuthScreen('Your session has expired — please log in again.');
    }
    return res;
};

function showAuthScreen(message) {
    document.getElementById('appShell').style.display = 'none';
    document.getElementById('authScreen').style.display = 'flex';
    if (message) {
        const el = document.getElementById('authMessage');
        el.textContent = message;
        el.style.display = 'block';
    }
}

function showApp() {
    document.getElementById('authScreen').style.display = 'none';
    document.getElementById('appShell').style.display = '';
    const auth = getAuth();
    const emailEl = document.getElementById('sidebarUserEmail');
    if (emailEl) emailEl.textContent = auth ? auth.email : '';
}

function switchAuthTab(tab) {
    document.getElementById('loginForm').style.display = tab === 'login' ? 'block' : 'none';
    document.getElementById('signupForm').style.display = tab === 'signup' ? 'block' : 'none';
    document.getElementById('authTabLogin').classList.toggle('active', tab === 'login');
    document.getElementById('authTabSignup').classList.toggle('active', tab === 'signup');
    document.getElementById('authMessage').style.display = 'none';
}

async function handleLogin(event) {
    event.preventDefault();
    const email = document.getElementById('loginEmail').value.trim();
    const password = document.getElementById('loginPassword').value;
    const btn = document.getElementById('loginSubmitBtn');
    btn.disabled = true;
    btn.textContent = 'Logging in…';
    try {
        const res = await fetch(`${API_BASE}/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Login failed');
        setAuth({ access_token: data.access_token, refresh_token: data.refresh_token, email: data.email, user_id: data.user_id });
        showApp();
        bootApp();
    } catch (err) {
        const el = document.getElementById('authMessage');
        el.textContent = err.message;
        el.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.textContent = 'Log In';
    }
}

async function handleSignup(event) {
    event.preventDefault();
    const email = document.getElementById('signupEmail').value.trim();
    const password = document.getElementById('signupPassword').value;
    const btn = document.getElementById('signupSubmitBtn');
    btn.disabled = true;
    btn.textContent = 'Creating account…';
    try {
        const res = await fetch(`${API_BASE}/auth/signup`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, password }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Signup failed');
        if (data.confirmation_required) {
            const el = document.getElementById('authMessage');
            el.textContent = data.message || 'Check your email to confirm your account, then log in.';
            el.style.display = 'block';
            switchAuthTab('login');
            return;
        }
        setAuth({ access_token: data.access_token, refresh_token: data.refresh_token, email: data.email, user_id: data.user_id });
        showApp();
        bootApp();
    } catch (err) {
        const el = document.getElementById('authMessage');
        el.textContent = err.message;
        el.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.textContent = 'Sign Up';
    }
}

async function handleLogout() {
    try { await fetch(`${API_BASE}/auth/logout`, { method: 'POST' }); } catch { /* best-effort */ }
    setAuth(null);
    ALL_INVOICES = [];
    STAGED_INVOICES = [];
    showAuthScreen();
}

/** Everything that used to run unconditionally at the bottom of this
 * file now only runs once we know there's a signed-in user. */
function bootApp() {
    refreshInvoices();
    loadTempContracts();
    loadStagedInvoices();
}

// Maps extraction_agent.py's _method_label() outputs to routing-page labels.
// Gemini is primary for every tier now (paid key); Groq only appears when
// Gemini failed and the tier had raw text to fall back with.
const EXTRACTION_METHOD_META = {
    gemini_text: { tier: 'text_pdf', provider: 'gemini', fallback: false },
    gemini_ocr: { tier: 'ocr', provider: 'gemini', fallback: false },
    gemini_vision: { tier: 'low_confidence_ocr', provider: 'gemini', fallback: false },
    groq_text_fallback: { tier: 'text_pdf (fallback)', provider: 'groq', fallback: true },
    groq_ocr_fallback: { tier: 'ocr (fallback)', provider: 'groq', fallback: true },
};

// ============================================================
// STATE
// ============================================================

let ALL_INVOICES = [];       // every snapshot from GET /api/invoices
let currentPage = 'overview';
let currentDecisionThreadId = null;  // which invoice the reject/edit modal targets
let currentPipelineThreadId = null;  // which invoice #pipelineView is currently showing
let pipelineHideTimer = null;        // pending "auto-hide after done" timeout
let STAGED_INVOICE = null;           // selected uploaded PDF waiting for its contract and Run click
let STAGED_INVOICES = [];            // browser uploads only; samples never enter this list
let SELECTED_BATCH_IDS = new Set();

function pendingApprovals() {
    return ALL_INVOICES.filter(inv => inv.pending_approval);
}

function findInvoice(threadId) {
    return ALL_INVOICES.find(inv => inv.thread_id === threadId) || null;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str ?? '';
    return div.innerHTML;
}


// ============================================================
// NAVIGATION
// ============================================================

function navigateTo(page) {
    document.querySelectorAll('.page-view').forEach(p => p.classList.remove('active'));
    const target = document.getElementById(`page-${page}`);
    if (target) target.classList.add('active');

    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    const navItem = document.querySelector(`.nav-item[data-page="${page}"]`);
    if (navItem) navItem.classList.add('active');

    currentPage = page;
    document.getElementById('sidebar').classList.remove('open');

    renderCurrentPage();
}

function renderCurrentPage() {
    if (currentPage === 'overview') { updateStatCards(); populateDashboard(); }
    if (currentPage === 'dashboard') populateDashboard();
    if (currentPage === 'invoices') populateAllInvoices();
    if (currentPage === 'approval') populateApprovalQueue();
    if (currentPage === 'reports') populateReports();
    if (currentPage === 'routing') populateRouting();
    if (currentPage === 'contracts') populateContracts();
    if (currentPage === 'added-contracts') renderAddedContractCards();
    if (currentPage === 'new-invoices') renderUploadedInvoices();
    if (currentPage === 'samples') loadSampleInvoicesGrid();
    if (currentPage === 'evaluations') populateEvaluationsInvoiceList();
}


// ============================================================
// DATA LOADING
// ============================================================

async function refreshInvoices() {
    try {
        const res = await fetch(`${API_BASE}/invoices`);
        if (!res.ok) throw new Error(await res.text());
        ALL_INVOICES = await res.json();
    } catch (err) {
        console.error('Failed to load invoices from API', err);
        showToast(`Couldn't reach the backend at ${API_BASE} — is uvicorn running?`, 'error');
    }
    document.getElementById('approvalBadge').textContent = pendingApprovals().length;
    updateStatCards();
    renderCurrentPage();
}


// ============================================================
// MOBILE SIDEBAR
// ============================================================

document.getElementById('mobileToggle').addEventListener('click', () => {
    document.getElementById('sidebar').classList.toggle('open');
});


// ============================================================
// FILE UPLOAD & PIPELINE
// ============================================================

const uploadZone = document.getElementById('uploadZone');
const fileInput = document.getElementById('fileInput');
const MAX_INVOICE_UPLOAD_BYTES = 5 * 1024 * 1024;

function validInvoiceFiles(files) {
    return files.filter(file => {
        if (!file.name.toLowerCase().endsWith('.pdf')) {
            showToast(`${file.name} is not a PDF`, 'warning');
            return false;
        }
        if (file.size > MAX_INVOICE_UPLOAD_BYTES) {
            showToast(`${file.name} is larger than the 5 MB limit`, 'warning');
            return false;
        }
        return true;
    });
}

uploadZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadZone.classList.add('dragover');
});

uploadZone.addEventListener('dragleave', () => {
    uploadZone.classList.remove('dragover');
});

uploadZone.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadZone.classList.remove('dragover');
    const files = validInvoiceFiles(Array.from(e.dataTransfer.files));
    if (files.length) stageInvoiceFiles(files);
});

fileInput.addEventListener('change', (e) => {
    const files = validInvoiceFiles(Array.from(e.target.files));
    if (files.length) stageInvoiceFiles(files);
    fileInput.value = '';
});

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

const PIPELINE_STEP_ORDER = ['extraction', 'guardrail', 'compliance', 'risk', 'supervisor', 'approval', 'export'];

function pipelineStepEl(name) {
    return document.querySelector(`.pipeline-step[data-step="${name}"]`);
}

function resetPipelineSteps() {
    PIPELINE_STEP_ORDER.forEach(name => {
        const el = pipelineStepEl(name);
        el.classList.remove('active', 'done', 'failed');
        el.querySelector('[data-duration]').textContent = '';
    });
}

function showPipelineRunning(fileName, fileSize) {
    // A fresh run starting cancels any pending auto-hide from the last one
    // and detaches the view from whatever invoice it was tracking before.
    if (pipelineHideTimer) {
        clearTimeout(pipelineHideTimer);
        pipelineHideTimer = null;
    }
    currentPipelineThreadId = null;
    document.getElementById('pipelineFileName').textContent = fileName;
    document.getElementById('pipelineFileSize').textContent = fileSize || 'PDF';
    document.getElementById('pipelineView').classList.add('visible');
    resetPipelineSteps();
    // We can't time individual backend nodes over one HTTP round trip,
    // so every step lights up as "running" together and we resolve them
    // against the real result once the response comes back.
    PIPELINE_STEP_ORDER.forEach(name => pipelineStepEl(name).classList.add('active'));
    pipelineStepEl('extraction').querySelector('[data-duration]').textContent = 'running…';
}

/** True once a snapshot has reached a final state — nothing left for the
 * pipeline view to wait on (still-pending human review is NOT done). */
function isPipelineDone(data) {
    if (data.pending_approval) return false;
    return !!(data.extraction_error || data.guardrail_passed === false || data.decision);
}

/** Hides #pipelineView a few seconds after an invoice reaches a final
 * state, so it doesn't sit on screen forever — and clears the "results
 * empty state" placeholder back into view once it does. Approving/
 * rejecting from the Approval Queue (or anywhere else) calls this too,
 * since the pipeline view lives in the DOM regardless of which page is
 * currently active. */
function maybeScheduleAutoHide(data) {
    if (pipelineHideTimer) {
        clearTimeout(pipelineHideTimer);
        pipelineHideTimer = null;
    }
    if (!isPipelineDone(data)) return;
    pipelineHideTimer = setTimeout(() => {
        document.getElementById('pipelineView').classList.remove('visible');
        currentPipelineThreadId = null;
        pipelineHideTimer = null;
    }, 3000);
}

/** Reconciles the pipeline UI against a real snapshot from the backend. */
function applyPipelineResult(data) {
    resetPipelineSteps();
    const mark = (name, cls, label) => {
        const el = pipelineStepEl(name);
        el.classList.add(cls);
        el.querySelector('[data-duration]').textContent = label;
    };

    if (data.extraction_error) {
        mark('extraction', 'failed', 'failed');
        return;
    }
    mark('extraction', 'done', data.invoice ? data.invoice.extraction_method || 'done' : 'done');

    if (data.guardrail_passed === false) {
        mark('guardrail', 'failed', 'blocked');
        return;
    }
    mark('guardrail', 'done', 'passed');
    mark('compliance', 'done', data.compliance ? data.compliance.overall_status : 'done');
    mark('risk', 'done', data.risk ? data.risk.risk_score : 'done');
    mark('supervisor', 'done', 'done');

    if (data.pending_approval) {
        pipelineStepEl('approval').classList.add('active');
        pipelineStepEl('approval').querySelector('[data-duration]').textContent = 'awaiting review';
        pipelineStepEl('export').querySelector('[data-duration]').textContent = 'pending';
        return;
    }

    mark('approval', 'done', 'skipped');

    const status = data.decision ? data.decision.status : null;
    if (status === 'approved_exported') {
        mark('export', 'done', 'exported');
    } else if (status === 'rejected') {
        mark('export', 'failed', 'rejected');
    } else {
        mark('export', 'failed', status || 'stopped');
    }
}

/** Human-readable toast for whatever a snapshot's current state is. */
function describeOutcome(data) {
    if (data.extraction_error) {
        return { type: 'error', message: `Extraction failed: ${data.extraction_error}` };
    }
    if (data.guardrail_passed === false) {
        const detail = (data.guardrail_violations[0] || [])[1] || 'guardrail check failed';
        return { type: 'error', message: `Blocked by guardrail — ${detail}` };
    }
    if (data.pending_approval) {
        const reason = (data.pending_approval.reasons || [])[0] || 'needs review';
        return { type: 'warning', message: `Escalated to human review — ${reason}` };
    }
    if (data.decision) {
        const reason = (data.decision.reasons || [])[0] || '';
        if (data.decision.status === 'approved_exported') return { type: 'success', message: `Approved & exported — ${reason}` };
        if (data.decision.status === 'rejected') return { type: 'error', message: `Rejected — ${reason}` };
        if (data.decision.status === 'blocked_by_guardrail') return { type: 'error', message: `Blocked by guardrail — ${reason}` };
        if (data.decision.status === 'extraction_failed') return { type: 'error', message: reason };
    }
    return { type: 'info', message: 'Invoice updated' };
}

async function uploadInvoice(file) {
    const formData = new FormData();
    formData.append('file', file);
    const res = await fetch(`${API_BASE}/invoices`, { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Upload failed');
    return data;
}

function setUploadWorkflow(step) {
    const steps = [
        document.getElementById('uploadStepFile'),
        document.getElementById('uploadStepContract'),
        document.getElementById('uploadStepRun'),
    ];
    steps.forEach((el, index) => el.classList.toggle('complete', index < step));
    steps.forEach((el, index) => el.classList.toggle('current', index === step));
}

async function loadStagedInvoices() {
    try {
        const res = await fetch(`${API_BASE}/staged-invoices`);
        const invoices = await res.json();
        if (!res.ok) throw new Error(invoices.detail || 'Could not load uploaded invoices');
        const localFiles = new Map(STAGED_INVOICES.map(item => [item.upload_id, item.file]));
        STAGED_INVOICES = invoices.map(item => ({ ...item, file: localFiles.get(item.upload_id) }));
        if (STAGED_INVOICE) {
            STAGED_INVOICE = STAGED_INVOICES.find(item => item.upload_id === STAGED_INVOICE.upload_id) || null;
        }
        renderUploadedInvoices();
    } catch (err) {
        showToast(`Could not load uploaded invoices: ${err.message}`, 'error');
    }
}

function updateSelectedUploadWorkflow() {
    const addContractBtn = document.getElementById('addContractBtn');
    const runBtn = document.getElementById('runStagedInvoiceBtn');
    if (!STAGED_INVOICE) {
        addContractBtn.disabled = true;
        runBtn.disabled = true;
        document.getElementById('stagedFileName').textContent = 'Choose an invoice to begin';
        document.getElementById('stagedContractStatus').textContent = 'Available after the PDF is uploaded';
        setUploadWorkflow(-1);
        return;
    }
    document.getElementById('stagedFileName').textContent = `${STAGED_INVOICE.filename}${STAGED_INVOICE.file ? ` · ${formatFileSize(STAGED_INVOICE.file.size)}` : ''}`;
    if (STAGED_INVOICE.contract) {
        document.getElementById('stagedContractStatus').textContent = `Contract saved for ${STAGED_INVOICE.contract.vendor_name}`;
        addContractBtn.disabled = false;
        runBtn.disabled = false;
        setUploadWorkflow(2);
    } else {
        document.getElementById('stagedContractStatus').textContent = 'Fill in the contract required for this invoice';
        addContractBtn.disabled = false;
        runBtn.disabled = true;
        setUploadWorkflow(1);
    }
}

function renderUploadedInvoices() {
    const list = document.getElementById('uploadedInvoicesList');
    const newInvoicesList = document.getElementById('newInvoicesList');
    const batchBtn = document.getElementById('runUploadedBatchBtn');
    if (!list || !newInvoicesList || !batchBtn) return;

    const availableIds = new Set(STAGED_INVOICES.map(item => item.upload_id));
    SELECTED_BATCH_IDS = new Set([...SELECTED_BATCH_IDS].filter(id => availableIds.has(id)));
    const selectedInvoices = STAGED_INVOICES.filter(item => SELECTED_BATCH_IDS.has(item.upload_id));
    list.innerHTML = selectedInvoices.length
        ? selectedInvoices.map(item => `
            <div class="uploaded-invoice-row">
                <div class="uploaded-invoice-info">
                    <strong>${escapeHtml(item.filename)}</strong>
                    <small>Contract ready: ${escapeHtml(item.contract.vendor_name)}</small>
                </div>
            </div>`).join('')
        : '<div class="uploaded-invoices-empty">No invoices selected. Select 2–5 from New Invoices.</div>';

    newInvoicesList.innerHTML = STAGED_INVOICES.length
        ? STAGED_INVOICES.map(item => {
            const ready = !!item.contract;
            const selected = SELECTED_BATCH_IDS.has(item.upload_id);
            return `
                <div class="uploaded-invoice-row ${selected ? 'active' : ''}">
                    <div class="uploaded-invoice-info">
                        <strong>${escapeHtml(item.filename)}</strong>
                        <small>${ready ? `Contract ready: ${escapeHtml(item.contract.vendor_name)}` : 'Vendor contract required'}</small>
                    </div>
                    <button type="button" class="btn btn-ghost btn-sm" data-upload-id="${item.upload_id}" onclick="viewNewInvoice(this.dataset.uploadId)">View</button>
                    <button type="button" class="btn btn-ghost btn-sm" data-upload-id="${item.upload_id}" onclick="selectNewInvoice(this.dataset.uploadId)">${ready ? (selected ? 'Remove' : 'Select') : 'Add Contract'}</button>
                </div>`;
        }).join('')
        : '<div class="uploaded-invoices-empty">No new invoices uploaded yet.</div>';

    const count = SELECTED_BATCH_IDS.size;
    batchBtn.disabled = count < 2 || count > 5;
    batchBtn.textContent = count ? `Run Selected Batch (${count})` : 'Run Selected Batch';
}

async function viewNewInvoice(uploadId) {
    // Goes through the same wrapped `fetch` used everywhere else, so the
    // current user's Bearer token is attached and the backend's
    // per-user ownership check (_get_owned_staged) applies — one user
    // can never view another user's staged invoice. A plain
    // `window.open(apiUrl)` or `<a href>` wouldn't carry that header.
    try {
        const res = await fetch(`${API_BASE}/invoices/${encodeURIComponent(uploadId)}/file`);
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.detail || 'Could not load invoice PDF');
        }
        const blob = await res.blob();
        // Opening a Blob object URL (typed application/pdf) makes the
        // browser render it in its built-in PDF viewer tab instead of
        // downloading it, since there's no Content-Disposition on this
        // client-side URL for the browser to treat as a download hint.
        const blobUrl = URL.createObjectURL(blob);
        const viewerWindow = window.open(blobUrl, '_blank');
        if (!viewerWindow) showToast('Please allow pop-ups to view the invoice', 'warning');
        setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
    } catch (err) {
        showToast(`Could not open invoice: ${err.message}`, 'error');
    }
}

function selectNewInvoice(uploadId) {
    const invoice = STAGED_INVOICES.find(item => item.upload_id === uploadId);
    if (!invoice) return;
    STAGED_INVOICE = invoice;
    updateSelectedUploadWorkflow();
    if (!invoice.contract) {
        navigateTo('upload');
        openContractModal();
    } else if (SELECTED_BATCH_IDS.has(uploadId)) {
        SELECTED_BATCH_IDS.delete(uploadId);
    } else {
        if (SELECTED_BATCH_IDS.size >= 5) {
            showToast('A batch can contain at most 5 uploaded invoices', 'warning');
            return;
        }
        SELECTED_BATCH_IDS.add(uploadId);
    }
    renderUploadedInvoices();
}

async function stageInvoiceFiles(files) {
    for (const file of files) await stageInvoiceFile(file);
}

async function stageInvoiceFile(file) {
    document.getElementById('stagedFileName').textContent = `Uploading ${file.name}…`;
    document.getElementById('stagedContractStatus').textContent = 'Waiting for the PDF upload';
    document.getElementById('addContractBtn').disabled = true;
    document.getElementById('runStagedInvoiceBtn').disabled = true;
    setUploadWorkflow(0);

    try {
        const staged = await uploadInvoice(file);
        STAGED_INVOICE = { ...staged, file };
        STAGED_INVOICES = [STAGED_INVOICE, ...STAGED_INVOICES];
        updateSelectedUploadWorkflow();
        renderUploadedInvoices();
        showToast('PDF uploaded. Add its vendor contract to continue.', 'info');
    } catch (err) {
        updateSelectedUploadWorkflow();
        showToast(`Could not upload ${file.name}: ${err.message}`, 'error');
    }
}

async function runStagedInvoice() {
    if (!STAGED_INVOICE || !STAGED_INVOICE.contract) {
        showToast('Upload a PDF and save its vendor contract before running it', 'warning');
        return;
    }
    const runBtn = document.getElementById('runStagedInvoiceBtn');
    runBtn.disabled = true;
    runBtn.textContent = 'Running…';
    navigateTo('results');
    showPipelineRunning(STAGED_INVOICE.filename, STAGED_INVOICE.file ? formatFileSize(STAGED_INVOICE.file.size) : 'PDF');
    try {
        const res = await fetch(`${API_BASE}/invoices/${encodeURIComponent(STAGED_INVOICE.upload_id)}/run`, { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Could not run invoice');
        applyPipelineResult(data);
        currentPipelineThreadId = data.thread_id;
        maybeScheduleAutoHide(data);
        const outcome = describeOutcome(data);
        showToast(outcome.message, outcome.type);
        STAGED_INVOICES = STAGED_INVOICES.filter(item => item.upload_id !== STAGED_INVOICE.upload_id);
        SELECTED_BATCH_IDS.delete(STAGED_INVOICE.upload_id);
        STAGED_INVOICE = STAGED_INVOICES[0] || null;
        updateSelectedUploadWorkflow();
        renderUploadedInvoices();
    } catch (err) {
        resetPipelineSteps();
        pipelineStepEl('extraction').classList.add('failed');
        pipelineStepEl('extraction').querySelector('[data-duration]').textContent = 'error';
        showToast(`Could not run invoice: ${err.message}`, 'error');
    } finally {
        runBtn.disabled = !STAGED_INVOICE || !STAGED_INVOICE.contract;
        runBtn.textContent = 'Run Invoice';
        await refreshInvoices();
    }
}

async function runUploadedBatch() {
    const upload_ids = [...SELECTED_BATCH_IDS];
    if (upload_ids.length < 2 || upload_ids.length > 5) {
        showToast('Select at least 2 and at most 5 uploaded invoices', 'warning');
        return;
    }
    const batchBtn = document.getElementById('runUploadedBatchBtn');
    batchBtn.disabled = true;
    batchBtn.textContent = 'Running batch…';
    navigateTo('results');
    showPipelineRunning(`${upload_ids.length} uploaded invoices`, 'Batch');
    try {
        const res = await fetch(`${API_BASE}/invoices/batch-run`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ upload_ids }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Could not run uploaded batch');
        const lastResult = data.results[data.results.length - 1];
        if (lastResult) {
            applyPipelineResult(lastResult);
            currentPipelineThreadId = lastResult.thread_id;
            maybeScheduleAutoHide(lastResult);
        }
        showToast(`Batch completed for ${data.results.length} uploaded invoices`, 'success');
        SELECTED_BATCH_IDS.clear();
        STAGED_INVOICE = null;
        await loadStagedInvoices();
        updateSelectedUploadWorkflow();
    } catch (err) {
        resetPipelineSteps();
        pipelineStepEl('extraction').classList.add('failed');
        pipelineStepEl('extraction').querySelector('[data-duration]').textContent = 'error';
        showToast(`Batch mode failed: ${err.message}`, 'error');
    } finally {
        await refreshInvoices();
        renderUploadedInvoices();
    }
}

async function processFiles(files) {
    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const label = files.length > 1 ? `${file.name} (${i + 1}/${files.length})` : file.name;
        showPipelineRunning(label, formatFileSize(file.size));
        try {
            const data = await uploadInvoice(file);
            applyPipelineResult(data);
            currentPipelineThreadId = data.thread_id;
            maybeScheduleAutoHide(data);
            const outcome = describeOutcome(data);
            showToast(outcome.message, outcome.type);
        } catch (err) {
            resetPipelineSteps();
            pipelineStepEl('extraction').classList.add('failed');
            pipelineStepEl('extraction').querySelector('[data-duration]').textContent = 'error';
            showToast(`Upload failed for ${file.name}: ${err.message}`, 'error');
        }
        await refreshInvoices();
    }
}

async function uploadSampleInvoice(samplePath) {
    const res = await fetch(`${API_BASE}/invoices/sample`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: samplePath }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Failed to run sample invoice');
    return data;
}

/** Same idea as processFiles(), but for PDFs already sitting in
 * sample_invoices/ on the server instead of a browser File object. */
async function processSamplePaths(samples) {
    for (let i = 0; i < samples.length; i++) {
        const sample = samples[i];
        const label = samples.length > 1 ? `${sample.filename} (${i + 1}/${samples.length})` : sample.filename;
        showPipelineRunning(label, sample.category === 'scanned' ? 'Scanned PDF' : 'PDF');
        try {
            const data = await uploadSampleInvoice(sample.path);
            applyPipelineResult(data);
            currentPipelineThreadId = data.thread_id;
            maybeScheduleAutoHide(data);
            const outcome = describeOutcome(data);
            showToast(outcome.message, outcome.type);
        } catch (err) {
            resetPipelineSteps();
            pipelineStepEl('extraction').classList.add('failed');
            pipelineStepEl('extraction').querySelector('[data-duration]').textContent = 'error';
            showToast(`${sample.filename} failed: ${err.message}`, 'error');
        }
        await refreshInvoices();
    }
}

async function loadSampleInvoicesGrid() {
    const grid = document.getElementById('sampleInvoicesGrid');
    if (!grid) return;
    try {
        const res = await fetch(`${API_BASE}/sample-invoices`);
        const samples = await res.json();
        if (!res.ok) throw new Error(samples.detail || 'Could not list sample invoices');
        grid.innerHTML = samples.map(s => `
            <div class="sample-invoice-row">
                <div class="sample-invoice-row-info">
                    <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/></svg>
                    <div>
                        <div class="sample-invoice-row-name">${escapeHtml(s.filename)}</div>
                        <div class="sample-invoice-row-category">${escapeHtml(s.category)}</div>
                    </div>
                </div>
                <div class="sample-invoice-row-actions">
                    <button type="button" class="btn btn-ghost btn-sm" data-path="${escapeHtml(s.path)}" onclick="viewSamplePdf(this)">View PDF</button>
                    <button type="button" class="btn btn-secondary btn-sm" data-path="${escapeHtml(s.path)}" onclick="runSingleSample(this)">Run</button>
                </div>
            </div>
        `).join('');
    } catch (err) {
        grid.innerHTML = '';  // falls back to the ":empty" placeholder text in CSS
    }
}

/** Opens a sample PDF straight from the server in a new tab, so it opens
 * in whatever the browser/OS is configured to view PDFs with (built-in
 * viewer, or Adobe Acrobat if that's the default handler). Reads the
 * path from a data-attribute rather than interpolating it into the
 * onclick string — a raw file path (which can contain backslashes on
 * Windows) embedded straight into inline JS gets silently mangled by
 * the parser, which was causing "unknown sample invoice path" errors. */
function viewSamplePdf(btnEl) {
    const path = btnEl.dataset.path;
    const url = `${API_BASE}/sample-invoices/file?path=${encodeURIComponent(path)}`;
    window.open(url, '_blank');
}

async function runSingleSample(btnEl) {
    const row = btnEl.closest('.sample-invoice-row');
    const path = btnEl.dataset.path;
    const filename = row.querySelector('.sample-invoice-row-name').textContent;
    const category = row.querySelector('.sample-invoice-row-category').textContent;
    const originalLabel = btnEl.textContent;
    btnEl.disabled = true;
    btnEl.textContent = 'Running…';
    navigateTo('results'); // pipeline progress view lives on the Results page
    try {
        await processSamplePaths([{ path, filename, category }]);
    } finally {
        btnEl.disabled = false;
        btnEl.textContent = originalLabel;
    }
}


// ============================================================
// OVERVIEW / STAT CARDS
// ============================================================

function isToday(isoString) {
    if (!isoString) return false;
    const d = new Date(isoString);
    const now = new Date();
    return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
}

function isAutoApproved(inv) {
    return !!(inv.decision && inv.decision.status === 'approved_exported'
        && (inv.decision.reasons || []).some(r => r.toLowerCase().includes('auto-approved')));
}

function isHumanApproved(inv) {
    return !!(inv.decision && inv.decision.status === 'approved_exported' && !isAutoApproved(inv));
}

function setStatCard(valueId, value, changeText) {
    const valueEl = document.getElementById(valueId);
    if (!valueEl) return;
    valueEl.textContent = value;
    const changeEl = valueEl.parentElement.querySelector('.stat-card-change');
    if (changeEl && changeText !== undefined) changeEl.textContent = changeText;
}

function updateStatCards() {
    const total = ALL_INVOICES.length;
    const autoApproved = ALL_INVOICES.filter(isAutoApproved).length;
    const humanApproved = ALL_INVOICES.filter(isHumanApproved).length;
    const rejected = ALL_INVOICES.filter(inv => inv.decision && inv.decision.status === 'rejected').length;
    const totalApprovedAmount = ALL_INVOICES
        .filter(inv => inv.decision && inv.decision.status === 'approved_exported' && inv.invoice)
        .reduce((sum, inv) => sum + inv.invoice.amount, 0);
    const todayCount = ALL_INVOICES.filter(inv => isToday(inv.created_at)).length;
    const todayApprovedAmount = ALL_INVOICES
        .filter(inv => isToday(inv.created_at) && inv.decision && inv.decision.status === 'approved_exported' && inv.invoice)
        .reduce((sum, inv) => sum + inv.invoice.amount, 0);

    const pct = (n) => total ? ((n / total) * 100).toFixed(1) + '% rate' : '—';

    setStatCard('statTotal', total, `${todayCount} today`);
    setStatCard('statAutoApproved', autoApproved, pct(autoApproved));
    setStatCard('statHumanApproved', humanApproved, pct(humanApproved));
    setStatCard('statRejected', rejected, pct(rejected));
    setStatCard('statAmount', formatCompactAmount(totalApprovedAmount), `₹${formatAmount(todayApprovedAmount)} today`);
}

function formatCompactAmount(n) {
    if (n >= 1e7) return '₹' + (n / 1e7).toFixed(2) + 'Cr';
    if (n >= 1e5) return '₹' + (n / 1e5).toFixed(2) + 'L';
    if (n >= 1e3) return '₹' + (n / 1e3).toFixed(1) + 'K';
    return '₹' + formatAmount(n);
}


// ============================================================
// DASHBOARD / ALL INVOICES TABLES
// ============================================================

function decidedBy(inv) {
    if (!inv.decision) return inv.pending_approval ? 'pending' : 'system';
    if (inv.decision.status === 'approved_exported') return isAutoApproved(inv) ? 'auto' : 'human';
    if (inv.decision.status === 'rejected') return 'human';
    return 'system';
}

function statusOf(inv) {
    if (inv.decision) return inv.decision.status;
    if (inv.pending_approval) return 'escalate_to_human';
    return 'processing';
}

function invoiceTableRow(inv, vendorTruncateLen) {
    const number = inv.invoice ? inv.invoice.invoice_number : (inv.filename || inv.thread_id.slice(0, 8));
    const vendor = inv.invoice ? truncate(inv.invoice.vendor, vendorTruncateLen) : '—';
    const amount = inv.invoice ? '₹' + formatAmount(inv.invoice.amount) : '—';
    const date = inv.invoice && inv.invoice.invoice_date ? inv.invoice.invoice_date : '—';
    return { number, vendor, amount, date };
}

function populateDashboard() {
    const tbody = document.getElementById('recentInvoicesBody');
    tbody.innerHTML = '';

    ALL_INVOICES.slice(0, 8).forEach(inv => {
        const r = invoiceTableRow(inv, 28);
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><code style="font-family:var(--font-mono);font-size:12px;color:var(--accent-secondary)">${r.number}</code></td>
            <td>${r.vendor}</td>
            <td style="font-family:var(--font-mono)">${r.amount}</td>
            <td style="color:var(--text-secondary)">${r.date}</td>
            <td>${statusBadge(statusOf(inv), decidedBy(inv))}</td>
            <td><code style="font-size:11px;color:var(--text-muted)">${inv.invoice ? inv.invoice.extraction_method || '—' : '—'}</code></td>
        `;
        tr.style.cursor = 'pointer';
        tr.addEventListener('click', () => openDetailModalByThreadId(inv.thread_id));
        tbody.appendChild(tr);
    });
}

function populateAllInvoices() {
    const tbody = document.getElementById('allInvoicesBody');
    tbody.innerHTML = '';

    ALL_INVOICES.forEach(inv => {
        const r = invoiceTableRow(inv, 24);
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><code style="font-family:var(--font-mono);font-size:12px;color:var(--accent-secondary)">${r.number}</code></td>
            <td>${r.vendor}</td>
            <td style="font-family:var(--font-mono)">${r.amount}</td>
            <td style="color:var(--text-secondary)">${r.date}</td>
            <td>${complianceBadge(inv.compliance ? inv.compliance.overall_status : null)}</td>
            <td>${riskBadge(inv.risk ? inv.risk.risk_score : null)}</td>
            <td>${statusBadge(statusOf(inv), decidedBy(inv))}</td>
            <td><button class="btn btn-ghost btn-sm" onclick="openDetailModalByThreadId('${inv.thread_id}')">View</button></td>
        `;
        tbody.appendChild(tr);
    });
}


// ============================================================
// APPROVAL QUEUE
// ============================================================

function populateApprovalQueue() {
    const container = document.getElementById('approvalQueue');
    const emptyState = document.getElementById('approvalEmpty');
    const pending = pendingApprovals();

    if (pending.length === 0) {
        container.innerHTML = '';
        emptyState.style.display = 'block';
        document.getElementById('approvalBadge').textContent = 0;
        return;
    }

    emptyState.style.display = 'none';
    container.innerHTML = '';

    pending.forEach(item => {
        const p = item.pending_approval;
        const invoiceDate = item.invoice && item.invoice.invoice_date ? item.invoice.invoice_date : '—';
        const dueDate = item.invoice && item.invoice.due_date ? item.invoice.due_date : '—';

        const card = document.createElement('div');
        card.className = 'approval-card';
        card.id = `approval-${item.thread_id}`;
        card.innerHTML = `
            <div class="approval-card-header">
                <div>
                    <div class="approval-vendor">${p.vendor}</div>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:2px;">
                        <code style="font-family:var(--font-mono)">${p.invoice_number}</code>
                    </div>
                </div>
                <div class="approval-amount">₹${formatAmount(p.amount)}</div>
            </div>
            <div class="approval-card-body">
                <div class="approval-meta">
                    <div class="approval-meta-item">
                        <span class="approval-meta-label">Invoice Date</span>
                        <span class="approval-meta-value">${invoiceDate}</span>
                    </div>
                    <div class="approval-meta-item">
                        <span class="approval-meta-label">Due Date</span>
                        <span class="approval-meta-value">${dueDate}</span>
                    </div>
                    <div class="approval-meta-item">
                        <span class="approval-meta-label">Status</span>
                        <span class="approval-meta-value">${statusBadge('escalate_to_human', 'human')}</span>
                    </div>
                </div>
                ${item.compliance && item.compliance.relevant_clause ? `
                <div class="approval-reasons" style="border-left-color:var(--accent-secondary);margin-bottom:12px;">
                    <h5 style="color:var(--accent-secondary)"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 12h-5"/><path d="M15 8h-5"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/></svg> Relevant Contract Clause${item.compliance.relevant_clause_score != null ? ` (match ${(item.compliance.relevant_clause_score * 100).toFixed(0)}%)` : ''}</h5>
                    <ul><li>${item.compliance.relevant_clause}</li></ul>
                </div>` : ''}
                <div class="approval-reasons">
                    <h5><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg> Reasons Flagged</h5>
                    <ul>
                        ${p.reasons.map(r => `<li>${r}</li>`).join('')}
                    </ul>
                </div>
            </div>
            <div class="approval-card-actions">
                <button class="btn btn-primary" onclick="approveInvoice('${item.thread_id}')">
                    <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg> Approve
                </button>
                <button class="btn btn-danger" onclick="openRejectModal('${item.thread_id}')">
                    <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg> Reject
                </button>
                <button class="btn btn-secondary" onclick="openEditModal('${item.thread_id}')">
                    <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg> Edit Fields
                </button>
                <button class="btn btn-ghost" onclick="openDetailModalByThreadId('${item.thread_id}')">Details</button>
            </div>
        `;
        container.appendChild(card);
    });

    document.getElementById('approvalBadge').textContent = pending.length;
}


// ============================================================
// APPROVAL ACTIONS — each of these resumes the paused graph on the
// backend via Command(resume=...), same as approval_cli.py.
// ============================================================

/** After an approve/reject/edit action resolves, brings #pipelineView in
 * sync if it's still showing the invoice that action was for — it may not
 * be the active page right now (e.g. the decision came from the Approval
 * Queue), but the elements are still in the DOM and get updated either way. */
function reflectDecisionOnPipeline(action, data) {
    if (action === 'edit') {
        // Editing resumes the graph and re-runs it past the approval node,
        // so show the pipeline running from the start again rather than
        // just snapping to the new end state.
        const fileName = document.getElementById('pipelineFileName').textContent;
        const fileSize = document.getElementById('pipelineFileSize').textContent;
        showPipelineRunning(fileName, fileSize);
        currentPipelineThreadId = data.thread_id;
        setTimeout(() => {
            applyPipelineResult(data);
            maybeScheduleAutoHide(data);
        }, 500);
        return;
    }
    applyPipelineResult(data);
    maybeScheduleAutoHide(data);
}

async function submitDecision(threadId, payload) {
    try {
        const res = await fetch(`${API_BASE}/invoices/${threadId}/decision`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Action failed');

        const card = document.getElementById(`approval-${threadId}`);
        if (card) {
            card.style.opacity = '0';
            card.style.transform = 'translateX(40px)';
            card.style.transition = 'all 0.3s ease';
        }

        const outcome = describeOutcome(data);
        showToast(outcome.message, outcome.type);

        if (data.thread_id === currentPipelineThreadId) {
            reflectDecisionOnPipeline(payload.action, data);
        }

        setTimeout(() => refreshInvoices(), card ? 300 : 0);
        return data;
    } catch (err) {
        showToast(`Action failed: ${err.message}`, 'error');
        throw err;
    }
}

function approveInvoice(threadId) {
    submitDecision(threadId, { action: 'approve' });
}

function openRejectModal(threadId) {
    currentDecisionThreadId = threadId;
    document.getElementById('rejectModal').classList.add('visible');
    document.getElementById('rejectReason').value = '';
    document.getElementById('rejectReason').focus();
}

function closeRejectModal() {
    document.getElementById('rejectModal').classList.remove('visible');
    currentDecisionThreadId = null;
}

function submitReject() {
    if (currentDecisionThreadId === null) return;
    const reason = document.getElementById('rejectReason').value.trim();
    const threadId = currentDecisionThreadId;
    closeRejectModal();
    submitDecision(threadId, { action: 'reject', note: reason });
}

async function openEditModal(threadId) {
    currentDecisionThreadId = threadId;
    const entry = findInvoice(threadId);
    const vendorKey = entry?.invoice?.vendor?.trim().toLowerCase();
    // Prefer the shared, saved contract so edits made from Added Contracts
    // are immediately reflected in the Approval Queue too. Fall back to the
    // built-in vendor_contracts.json entry (e.g. for sample invoices, which
    // are matched against that file rather than a manually-added contract)
    // so editing here still works — saving will create a manually-added
    // contract that overrides the built-in one for this vendor.
    await ensureRealContracts();
    const contract = TEMP_CONTRACTS.find(c => c.vendor_name?.trim().toLowerCase() === vendorKey)
        || entry?.contract
        || REAL_CONTRACTS.find(c => c.vendor_name?.trim().toLowerCase() === vendorKey);
    if (!contract) {
        showToast('The vendor contract for this invoice is unavailable', 'error');
        currentDecisionThreadId = null;
        return;
    }

    document.getElementById('editModal').classList.add('visible');
    document.getElementById('editVendorName').value = contract.vendor_name || '';
    document.getElementById('editGstin').value = contract.gstin || '';
    document.getElementById('editPaymentTerms').value = contract.payment_terms_days ?? '';
    document.getElementById('editMaxAmount').value = contract.max_invoice_amount ?? '';
    document.getElementById('editDiscountPercentage').value = contract.discount_percentage ?? '';

    document.getElementById('editPricingRuleRows').innerHTML = '';
    Object.entries(contract.pricing_rules || {}).forEach(([item, rule]) => {
        addEditPricingRuleRow(item, rule.min, rule.max);
    });
    if (!Object.keys(contract.pricing_rules || {}).length) addEditPricingRuleRow();

    document.getElementById('editClauseRows').innerHTML = '';
    (contract.clauses || []).forEach(clause => addEditClauseRow(clause));
}

function addEditPricingRuleRow(item = '', min = '', max = '') {
    const row = document.createElement('div');
    row.className = 'dynamic-row';
    row.innerHTML = `
        <input class="form-input" type="text" placeholder="Line-item description, e.g. Custom Print Run" value="${escapeHtml(item)}" data-field="item">
        <input class="form-input" type="number" placeholder="Min Rs." style="width:100px" value="${escapeHtml(String(min))}" data-field="min">
        <input class="form-input" type="number" placeholder="Max Rs." style="width:100px" value="${escapeHtml(String(max))}" data-field="max">
        <button type="button" class="dynamic-row-remove" onclick="this.parentElement.remove()">
            <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
        </button>
    `;
    document.getElementById('editPricingRuleRows').appendChild(row);
}

function addEditClauseRow(text = '') {
    const row = document.createElement('div');
    row.className = 'dynamic-row clause-row';
    row.innerHTML = `
        <input class="form-input" type="text" placeholder="e.g. Payment terms are Net 30. A 50% advance is required for custom print runs exceeding Rs. 25,000." value="${escapeHtml(text)}" data-field="clause">
        <button type="button" class="dynamic-row-remove" onclick="this.parentElement.remove()">
            <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
        </button>
    `;
    document.getElementById('editClauseRows').appendChild(row);
}

function closeEditModal() {
    document.getElementById('editModal').classList.remove('visible');
    currentDecisionThreadId = null;
}

async function submitEdit() {
    if (currentDecisionThreadId === null) return;
    const vendor_name = document.getElementById('editVendorName').value.trim();
    const gstin = document.getElementById('editGstin').value.trim();
    const payment_terms_days = document.getElementById('editPaymentTerms').value;
    const max_invoice_amount = document.getElementById('editMaxAmount').value;
    const discount_percentage = document.getElementById('editDiscountPercentage').value;

    if (!vendor_name || !gstin || !payment_terms_days || !max_invoice_amount) {
        showToast('Vendor Name, GSTIN, Payment Terms, and Max Invoice Amount are required', 'warning');
        return;
    }

    const pricing_rules = Array.from(document.querySelectorAll('#editPricingRuleRows .dynamic-row')).map(row => ({
        item: row.querySelector('[data-field="item"]').value.trim(),
        min: parseFloat(row.querySelector('[data-field="min"]').value) || 0,
        max: parseFloat(row.querySelector('[data-field="max"]').value) || 0,
    })).filter(r => r.item);

    if (pricing_rules.length === 0) {
        showToast('Add at least one pricing rule', 'warning');
        return;
    }

    const clauses = Array.from(document.querySelectorAll('#editClauseRows [data-field="clause"]'))
        .map(input => input.value.trim())
        .filter(Boolean);

    const body = {
        vendor_name, gstin,
        payment_terms_days: parseInt(payment_terms_days, 10),
        max_invoice_amount: parseFloat(max_invoice_amount),
        discount_percentage: discount_percentage ? parseFloat(discount_percentage) : null,
        pricing_rules, clauses,
    };
    const threadId = currentDecisionThreadId;
    try {
        const res = await fetch(`${API_BASE}/invoices/${threadId}/contract-edit`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Could not save contract edit');

        closeEditModal();
        showToast(describeOutcome(data).message, describeOutcome(data).type);
        if (data.thread_id === currentPipelineThreadId) reflectDecisionOnPipeline('edit', data);
        await loadTempContracts();
        await refreshInvoices();
    } catch (err) {
        showToast(`Could not save contract edit: ${err.message}`, 'error');
    }
}


// ============================================================
// VENDOR CONTRACTS (real file + manually-added, session-only)
// ============================================================

let TEMP_CONTRACTS = [];  // this browser user's saved contracts, retained for 30 days
let REAL_CONTRACTS = [];  // the 5 built-in vendor_contracts.json entries, shared by everybody
let VIEWING_CONTRACT = false;
let EDITING_CONTRACT_VENDOR = null;
let MATCHED_EXISTING_CONTRACT = null;

function findExistingContract(vendorName) {
    const key = (vendorName || '').trim().toLowerCase();
    if (!key) return null;
    return TEMP_CONTRACTS.find(c => (c.vendor_name || '').trim().toLowerCase() === key) || null;
}

function checkExistingContractMatch() {
    if (VIEWING_CONTRACT || EDITING_CONTRACT_VENDOR) return; // only for brand-new "Add Contract" flow
    const vendorName = document.getElementById('contractVendorName').value;
    const match = findExistingContract(vendorName);
    const banner = document.getElementById('contractExistingBanner');
    if (match) {
        MATCHED_EXISTING_CONTRACT = match;
        document.getElementById('contractExistingBannerText').textContent =
            `Contract already exists for ${match.vendor_name} (GSTIN ${match.gstin || '—'}) — no need to re-enter it.`;
        banner.style.display = '';
    } else {
        MATCHED_EXISTING_CONTRACT = null;
        banner.style.display = 'none';
    }
}

function useExistingContract() {
    if (!MATCHED_EXISTING_CONTRACT) return;
    const c = MATCHED_EXISTING_CONTRACT;
    document.getElementById('contractGstin').value = c.gstin || '';
    document.getElementById('contractPaymentTerms').value = c.payment_terms_days ?? '';
    document.getElementById('contractMaxAmount').value = c.max_invoice_amount ?? '';
    document.getElementById('contractDiscountPercentage').value = c.discount_percentage ?? '';
    document.getElementById('pricingRuleRows').innerHTML = '';
    Object.entries(c.pricing_rules || {}).forEach(([item, rule]) => addPricingRuleRow(item, rule.min, rule.max));
    document.getElementById('clauseRows').innerHTML = '';
    (c.clauses || []).forEach(clause => addClauseRow(clause));
    submitContractForm();
}

function dismissExistingContractMatch() {
    MATCHED_EXISTING_CONTRACT = null;
    document.getElementById('contractExistingBanner').style.display = 'none';
}

// Lazily fetches/caches the built-in contracts so callers other than the
// Contracts page (e.g. the Edit Fields modal) can look a vendor up without
// re-fetching every time. Safe to call repeatedly; only hits the network
// once REAL_CONTRACTS is empty.
async function ensureRealContracts() {
    if (REAL_CONTRACTS.length) return REAL_CONTRACTS;
    try {
        const res = await fetch(`${API_BASE}/contracts`);
        if (res.ok) REAL_CONTRACTS = await res.json();
    } catch (err) {
        // leave REAL_CONTRACTS as-is; caller falls back gracefully
    }
    return REAL_CONTRACTS;
}

async function loadTempContracts() {
    try {
        const res = await fetch(`${API_BASE}/temp-contracts`);
        TEMP_CONTRACTS = await res.json();
    } catch (err) {
        TEMP_CONTRACTS = [];
    }
    renderUploadTempContractChips();
    if (currentPage === 'contracts') renderTempContractCards();
    if (currentPage === 'added-contracts') renderAddedContractCards();
}

function formatRs(n) {
    if (n === null || n === undefined) return '—';
    return 'Rs.' + Number(n).toLocaleString('en-IN');
}

function renderUploadTempContractChips() {
    const el = document.getElementById('uploadTempContractsList');
    if (!el) return;
    el.innerHTML = TEMP_CONTRACTS.map(c => `
        <span class="temp-contract-chip">
            ${escapeHtml(c.vendor_name)}
            <button onclick="deleteTempContract('${escapeHtml(c.vendor_name).replace(/'/g, "\\'")}')" title="Remove">
                <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
            </button>
        </span>
    `).join('');
}

function contractCardHtml(c, removable) {
    // pricing_rules is an object map ({item: {min,max}}) for both the real
    // vendor_contracts.json and manually-added contracts.
    const ruleRows = Object.entries(c.pricing_rules || {});
    const clauses = c.clauses || [];
    const summaryBits = [];
    if (ruleRows.length) summaryBits.push(`${ruleRows.length} pricing rule${ruleRows.length > 1 ? 's' : ''}`);
    if (clauses.length) summaryBits.push(`${clauses.length} clause${clauses.length > 1 ? 's' : ''}`);
    const hasDetails = ruleRows.length > 0 || clauses.length > 0;
    return `
        <div class="contract-card">
            <div class="contract-card-header">
                <div>
                    <div class="contract-card-vendor">${escapeHtml(c.vendor_name)}</div>
                    <div class="contract-card-gstin">${escapeHtml(c.gstin || '—')}</div>
                </div>
                ${removable ? `<button class="contract-card-remove" onclick="deleteTempContract('${escapeHtml(c.vendor_name).replace(/'/g, "\\'")}')" title="Remove">
                    <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>
                </button>` : ''}
            </div>
            <div class="contract-card-meta">
                <span>Net <strong>${c.payment_terms_days ?? '—'}</strong></span>
                <span>Max <strong>${formatRs(c.max_invoice_amount)}</strong></span>
                ${c.discount_percentage ? `<span>Discount <strong>${c.discount_percentage}%</strong></span>` : ''}
            </div>
            ${summaryBits.length ? `
                <button type="button" class="contract-card-toggle" onclick="this.closest('.contract-card').classList.toggle('expanded')" ${hasDetails ? '' : 'disabled'}>
                    <span>${summaryBits.join(' · ')}</span>
                    <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
                </button>
                <div class="contract-card-details">
                    ${ruleRows.length ? `
                        <div class="contract-detail-label">Pricing Rules</div>
                        <table class="contract-detail-table">
                            <thead><tr><th>Item</th><th>Min</th><th>Max</th></tr></thead>
                            <tbody>
                                ${ruleRows.map(([item, r]) => `
                                    <tr>
                                        <td>${escapeHtml(item)}</td>
                                        <td>${formatRs(r.min)}</td>
                                        <td>${formatRs(r.max)}</td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    ` : ''}
                    ${clauses.length ? `
                        <div class="contract-detail-label">Clauses</div>
                        <ul class="contract-detail-clauses">
                            ${clauses.map(cl => `<li>${escapeHtml(cl)}</li>`).join('')}
                        </ul>
                    ` : ''}
                </div>
            ` : ''}
        </div>
    `;
}

async function populateContracts() {
    const realEl = document.getElementById('realContractsList');
    try {
        const res = await fetch(`${API_BASE}/contracts`);
        const real = await res.json();
        if (res.ok) REAL_CONTRACTS = real;
        realEl.innerHTML = res.ok
            ? real.map(c => contractCardHtml(c, false)).join('')
            : `<p style="color:var(--text-muted); font-size:13px;">${escapeHtml(real.detail || 'Could not load vendor_contracts.json')}</p>`;
    } catch (err) {
        realEl.innerHTML = `<p style="color:var(--text-muted); font-size:13px;">Could not reach the backend.</p>`;
    }
    renderTempContractCards();
}

function renderTempContractCards() {
    const el = document.getElementById('tempContractsList');
    if (!el) return;
    el.innerHTML = TEMP_CONTRACTS.map(c => contractCardHtml(c, true)).join('');
}

function renderAddedContractCards() {
    const el = document.getElementById('addedContractsList');
    if (!el) return;
    if (!TEMP_CONTRACTS.length) {
        el.innerHTML = '<div class="uploaded-invoices-empty">No vendor contracts added yet.</div>';
        return;
    }
    el.innerHTML = TEMP_CONTRACTS.map(c => `
        <div class="contract-card">
            <div class="contract-card-header">
                <div>
                    <div class="contract-card-vendor">${escapeHtml(c.vendor_name)}</div>
                    <div class="contract-card-gstin">${escapeHtml(c.gstin || '—')}</div>
                </div>
                <div style="display:flex; gap:8px;">
                    <button class="btn btn-ghost btn-sm" data-vendor="${escapeHtml(c.vendor_name)}" onclick="openAddedContractView(this.dataset.vendor)">View</button>
                    <button class="btn btn-ghost btn-sm" data-vendor="${escapeHtml(c.vendor_name)}" onclick="openAddedContractEdit(this.dataset.vendor)">Edit</button>
                    <button class="btn btn-danger btn-sm" data-vendor="${escapeHtml(c.vendor_name)}" onclick="deleteAddedContract(this.dataset.vendor)">Delete</button>
                </div>
            </div>
            <div class="contract-card-meta">
                <span>Net <strong>${c.payment_terms_days ?? '—'}</strong></span>
                <span>Max <strong>${formatRs(c.max_invoice_amount)}</strong></span>
                ${c.discount_percentage ? `<span>Discount <strong>${c.discount_percentage}%</strong></span>` : ''}
            </div>
        </div>`).join('');
}

async function deleteTempContract(vendorName) {
    try {
        const res = await fetch(`${API_BASE}/temp-contracts/${encodeURIComponent(vendorName)}`, { method: 'DELETE' });
        if (!res.ok) {
            const data = await res.json();
            throw new Error(data.detail || 'Delete failed');
        }
        showToast(`Removed contract for ${vendorName}`, 'info');
    } catch (err) {
        showToast(`Could not remove contract: ${err.message}`, 'error');
    }
    await loadTempContracts();
}

async function deleteAddedContract(vendorName) {
    if (!window.confirm(`Delete the vendor contract for ${vendorName}? This cannot be undone.`)) return;
    await deleteTempContract(vendorName);
}

// ---- Add Vendor Contract modal ----

function openContractModal() {
    if (!STAGED_INVOICE) {
        showToast('Upload a new PDF before adding its contract', 'warning');
        return;
    }
    document.getElementById('contractModal').classList.add('visible');
    VIEWING_CONTRACT = false;
    EDITING_CONTRACT_VENDOR = null;
    document.querySelector('#contractModal .modal-header h3').lastChild.textContent = ' Add Vendor Contract';
    document.querySelectorAll('#contractModal input, #contractModal .dynamic-row-remove, #contractModal .btn-sm').forEach(el => el.disabled = false);
    document.querySelector('#contractModal .btn-primary').style.display = '';
    document.getElementById('contractVendorName').value = '';
    document.getElementById('contractGstin').value = '';
    document.getElementById('contractPaymentTerms').value = '';
    document.getElementById('contractMaxAmount').value = '';
    document.getElementById('contractDiscountPercentage').value = '';
    document.getElementById('pricingRuleRows').innerHTML = '';
    document.getElementById('clauseRows').innerHTML = '';
    addPricingRuleRow();  // start with one empty row — at least one rule is expected
    document.getElementById('contractVendorName').oninput = checkExistingContractMatch;
    document.getElementById('contractExistingBanner').style.display = 'none';
    MATCHED_EXISTING_CONTRACT = null;
}

function closeContractModal() {
    document.getElementById('contractModal').classList.remove('visible');
    VIEWING_CONTRACT = false;
    EDITING_CONTRACT_VENDOR = null;
}

function openAddedContractView(vendorName) {
    const contract = TEMP_CONTRACTS.find(c => c.vendor_name === vendorName);
    if (!contract) return;
    VIEWING_CONTRACT = true;
    document.getElementById('contractModal').classList.add('visible');
    document.querySelector('#contractModal .modal-header h3').lastChild.textContent = ' Vendor Contract';
    document.getElementById('contractVendorName').value = contract.vendor_name || '';
    document.getElementById('contractGstin').value = contract.gstin || '';
    document.getElementById('contractPaymentTerms').value = contract.payment_terms_days ?? '';
    document.getElementById('contractMaxAmount').value = contract.max_invoice_amount ?? '';
    document.getElementById('contractDiscountPercentage').value = contract.discount_percentage ?? '';
    document.getElementById('pricingRuleRows').innerHTML = '';
    Object.entries(contract.pricing_rules || {}).forEach(([item, rule]) => addPricingRuleRow(item, rule.min, rule.max));
    document.getElementById('clauseRows').innerHTML = '';
    (contract.clauses || []).forEach(clause => addClauseRow(clause));
    document.querySelectorAll('#contractModal input, #contractModal .dynamic-row-remove, #contractModal .btn-sm').forEach(el => el.disabled = true);
    document.querySelector('#contractModal .btn-primary').style.display = 'none';
}

function openAddedContractEdit(vendorName) {
    const contract = TEMP_CONTRACTS.find(c => c.vendor_name === vendorName);
    if (!contract) return;
    VIEWING_CONTRACT = false;
    EDITING_CONTRACT_VENDOR = contract.vendor_name;
    document.getElementById('contractModal').classList.add('visible');
    document.querySelector('#contractModal .modal-header h3').lastChild.textContent = ' Edit Vendor Contract';
    document.getElementById('contractVendorName').value = contract.vendor_name || '';
    document.getElementById('contractGstin').value = contract.gstin || '';
    document.getElementById('contractPaymentTerms').value = contract.payment_terms_days ?? '';
    document.getElementById('contractMaxAmount').value = contract.max_invoice_amount ?? '';
    document.getElementById('contractDiscountPercentage').value = contract.discount_percentage ?? '';
    document.getElementById('pricingRuleRows').innerHTML = '';
    Object.entries(contract.pricing_rules || {}).forEach(([item, rule]) => addPricingRuleRow(item, rule.min, rule.max));
    document.getElementById('clauseRows').innerHTML = '';
    (contract.clauses || []).forEach(clause => addClauseRow(clause));
    document.querySelectorAll('#contractModal input, #contractModal .dynamic-row-remove, #contractModal .btn-sm').forEach(el => el.disabled = false);
    document.querySelector('#contractModal .btn-primary').style.display = '';
}

function addPricingRuleRow(item = '', min = '', max = '') {
    const row = document.createElement('div');
    row.className = 'dynamic-row';
    row.innerHTML = `
        <input class="form-input" type="text" placeholder="Line-item description, e.g. Custom Print Run" value="${escapeHtml(item)}" data-field="item">
        <input class="form-input" type="number" placeholder="Min Rs." style="width:100px" value="${escapeHtml(String(min))}" data-field="min">
        <input class="form-input" type="number" placeholder="Max Rs." style="width:100px" value="${escapeHtml(String(max))}" data-field="max">
        <button type="button" class="dynamic-row-remove" onclick="this.parentElement.remove()">
            <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
        </button>
    `;
    document.getElementById('pricingRuleRows').appendChild(row);
}

function addClauseRow(text = '') {
    const row = document.createElement('div');
    row.className = 'dynamic-row clause-row';
    row.innerHTML = `
        <input class="form-input" type="text" placeholder="e.g. Payment terms are Net 30. A 50% advance is required for custom print runs exceeding Rs. 25,000." value="${escapeHtml(text)}" data-field="clause">
        <button type="button" class="dynamic-row-remove" onclick="this.parentElement.remove()">
            <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
        </button>
    `;
    document.getElementById('clauseRows').appendChild(row);
}

async function submitContractForm() {
    if (VIEWING_CONTRACT) return;
    const vendor_name = document.getElementById('contractVendorName').value.trim();
    const gstin = document.getElementById('contractGstin').value.trim();
    const payment_terms_days = document.getElementById('contractPaymentTerms').value;
    const max_invoice_amount = document.getElementById('contractMaxAmount').value;
    const discount_percentage = document.getElementById('contractDiscountPercentage').value;

    if (!vendor_name || !gstin || !payment_terms_days || !max_invoice_amount) {
        showToast('Vendor Name, GSTIN, Payment Terms, and Max Invoice Amount are required', 'warning');
        return;
    }

    const pricing_rules = Array.from(document.querySelectorAll('#pricingRuleRows .dynamic-row')).map(row => ({
        item: row.querySelector('[data-field="item"]').value.trim(),
        min: parseFloat(row.querySelector('[data-field="min"]').value) || 0,
        max: parseFloat(row.querySelector('[data-field="max"]').value) || 0,
    })).filter(r => r.item);

    if (pricing_rules.length === 0) {
        showToast('Add at least one pricing rule', 'warning');
        return;
    }

    const clauses = Array.from(document.querySelectorAll('#clauseRows [data-field="clause"]'))
        .map(input => input.value.trim())
        .filter(Boolean);

    const body = {
        vendor_name, gstin,
        payment_terms_days: parseInt(payment_terms_days, 10),
        max_invoice_amount: parseFloat(max_invoice_amount),
        discount_percentage: discount_percentage ? parseFloat(discount_percentage) : null,
        pricing_rules, clauses,
        previous_vendor_name: EDITING_CONTRACT_VENDOR,
    };

    try {
        if (EDITING_CONTRACT_VENDOR) {
            const res = await fetch(`${API_BASE}/temp-contracts`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Could not update contract');
            closeContractModal();
            await loadTempContracts();
            showToast(`Contract updated for ${data.vendor_name}`, 'success');
            return;
        }
        if (!STAGED_INVOICE) throw new Error('Upload a new PDF before saving its contract');
        const res = await fetch(`${API_BASE}/invoices/${encodeURIComponent(STAGED_INVOICE.upload_id)}/contract`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Could not save contract');
        STAGED_INVOICE.contract = data.contract;
        STAGED_INVOICES = STAGED_INVOICES.map(item => item.upload_id === STAGED_INVOICE.upload_id ? STAGED_INVOICE : item);
        updateSelectedUploadWorkflow();
        renderUploadedInvoices();
        showToast(`Contract saved for ${data.contract.vendor_name}. You can now run the invoice.`, 'success');
        closeContractModal();
        await loadTempContracts();
    } catch (err) {
        showToast(`Could not save contract: ${err.message}`, 'error');
    }
}


// ============================================================
// EVALUATIONS
// ============================================================

/** An invoice can be evaluated once extraction has actually produced
 * an Invoice object — no point offering it while it's still failed/
 * mid-flight. */
function invoiceIsEvaluable(inv) {
    return !!(inv && inv.invoice);
}

function populateEvaluationsInvoiceList() {
    const listEl = document.getElementById('evalInvoiceList');
    if (!listEl) return;

    if (!ALL_INVOICES.length) {
        listEl.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg></div>
                <h3>Nothing to evaluate yet</h3>
                <p>Process an invoice from Upload Invoice or Sample Invoices, then come back here.</p>
            </div>
        `;
        return;
    }

    const invoices = ALL_INVOICES.slice().sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
    listEl.innerHTML = invoices.map(inv => `
        <div class="eval-invoice-row">
            <div class="eval-invoice-row-header">
                <div>
                    <div class="eval-invoice-row-name">${escapeHtml(inv.filename || inv.thread_id)}</div>
                    <div class="eval-invoice-row-meta">${inv.invoice ? escapeHtml(inv.invoice.vendor || '') : 'Not fully processed yet'}</div>
                </div>
                <button class="btn btn-primary btn-sm" id="evalBtn-${inv.thread_id}"
                    ${invoiceIsEvaluable(inv) ? '' : 'disabled'}
                    onclick="runInvoiceEvaluation('${inv.thread_id}')">
                    <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg> Run Evaluation
                </button>
            </div>
            <div class="eval-invoice-row-result" id="evalResult-${inv.thread_id}"></div>
        </div>
    `).join('');
}

function renderInvoiceEvalResult(data) {
    const sourceLabel = data.eval_type === 'ground_truth'
        ? `Scored against ground_truth.json${data.reference ? ' — ' + escapeHtml(data.reference) : ''}`
        : data.eval_type === 'manual_contract'
            ? `Scored against the manually-added vendor contract${data.reference ? ' — ' + escapeHtml(data.reference) : ''}`
            : 'No reference available for this invoice';

    if (!data.checks || !data.checks.length) {
        return `<div class="eval-error-note">${escapeHtml(data.note || 'No checks could be run for this invoice.')}</div>`;
    }

    const rows = data.checks.map(c => `
        <tr>
            <td>${escapeHtml(c.field)}</td>
            <td>${escapeHtml(String(c.expected ?? '—'))}</td>
            <td>${escapeHtml(String(c.actual ?? '—'))}</td>
            <td>${complianceBadge(c.passed ? 'pass' : 'fail')}</td>
        </tr>
    `).join('');

    return `
        <div style="font-size:12px; color:var(--text-muted); margin-bottom:8px;">${sourceLabel}</div>
        <table class="eval-results-table">
            <thead><tr><th>Field</th><th>Expected</th><th>Actual</th><th>Result</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        <div style="margin-top:10px; display:flex; align-items:center; gap:8px;">
            ${complianceBadge(data.passed ? 'pass' : 'fail')}
            <span style="font-size:12px; color:var(--text-muted);">overall</span>
        </div>
    `;
}

async function runInvoiceEvaluation(threadId) {
    const btn = document.getElementById(`evalBtn-${threadId}`);
    const resultEl = document.getElementById(`evalResult-${threadId}`);
    const originalLabel = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = 'Running…';
    resultEl.innerHTML = '';

    try {
        const res = await fetch(`${API_BASE}/invoices/${threadId}/evaluate`, { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Evaluation failed');
        resultEl.innerHTML = renderInvoiceEvalResult(data);
    } catch (err) {
        resultEl.innerHTML = `<div class="eval-error-note">${escapeHtml(err.message)}</div>`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalLabel;
    }
}


// ============================================================
// REPORTS
// Vendor breakdown table stays derived client-side from the live
// invoice list (it's an all-time view, so there's no "window" to get
// from the backend for it). The Daily/Weekly digest cards, however,
// are fetched from the real ReportingAgent via /api/report/daily and
// /api/report/weekly — the same digest run_scheduled_report.py would
// produce for this user, just generated on page load instead of on a
// cron schedule. These used to be recomputed by hand from ALL_INVOICES
// filtered to "created_at within the last 7 days", which is why the
// digest could show 0 even when the vendor table below it had rows —
// it was measuring a different thing than the backend ever computed.
// ============================================================

function _setDigestCard(prefix, { processed, autoApproved, humanApproved, rejected, narrative, isEmpty }) {
    const setStat = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    setStat(`${prefix}StatProcessed`, processed);
    setStat(`${prefix}StatAutoApproved`, autoApproved);
    setStat(`${prefix}StatHumanApproved`, humanApproved);
    setStat(`${prefix}StatRejected`, rejected);

    const narrativeEl = document.getElementById(`${prefix}Narrative`);
    if (!narrativeEl) return;
    narrativeEl.style.cssText = isEmpty ? 'color:var(--text-muted);font-style:italic' : '';
    narrativeEl.textContent = narrative;
}

async function _loadDigestCard(prefix, endpoint, emptyLabel) {
    try {
        const res = await fetch(`${API_BASE}${endpoint}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || `Failed to load ${endpoint}`);

        const processed = (data.total_auto_approved || 0) + (data.total_human_approved || 0) + (data.total_rejected || 0);
        _setDigestCard(prefix, {
            processed,
            autoApproved: data.total_auto_approved || 0,
            humanApproved: data.total_human_approved || 0,
            rejected: data.total_rejected || 0,
            narrative: processed === 0
                ? `No invoices processed ${emptyLabel}. Process an invoice to see this digest here.`
                : (data.narrative || ''),
            isEmpty: processed === 0,
        });
    } catch (err) {
        _setDigestCard(prefix, {
            processed: 0, autoApproved: 0, humanApproved: 0, rejected: 0,
            narrative: `Couldn't load this digest — is the backend reachable at ${API_BASE}?`,
            isEmpty: true,
        });
    }
}

function populateReports() {
    // Fire off both digest fetches; they render independently as they land.
    // _loadDigestCard('dailyReport', '/report/daily', 'in the past day');
    _loadDigestCard('report', '/report/weekly', 'in the past 7 days');

    const tbody = document.getElementById('vendorBreakdownBody');
    tbody.innerHTML = '';

    const byVendor = new Map();
    ALL_INVOICES.filter(inv => inv.invoice).forEach(inv => {
        const vendor = inv.invoice.vendor;
        if (!byVendor.has(vendor)) byVendor.set(vendor, { vendor, invoices: 0, total: 0, risk: 'low' });
        const entry = byVendor.get(vendor);
        entry.invoices += 1;
        entry.total += inv.invoice.amount;
        if (inv.risk) {
            const rank = { low: 0, medium: 1, high: 2 };
            if (rank[inv.risk.risk_score] > rank[entry.risk]) entry.risk = inv.risk.risk_score;
        }
    });

    Array.from(byVendor.values()).sort((a, b) => b.total - a.total).forEach(v => {
        const avg = v.total / v.invoices;
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${v.vendor}</td>
            <td style="text-align:center">${v.invoices}</td>
            <td style="font-family:var(--font-mono)">₹${formatAmount(v.total)}</td>
            <td style="font-family:var(--font-mono);color:var(--text-secondary)">₹${formatAmount(avg)}</td>
            <td>${riskBadge(v.risk)}</td>
        `;
        tbody.appendChild(tr);
    });

    if (byVendor.size === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--text-muted)">No processed invoices yet</td></tr>`;
    }
    // Daily/weekly digest stats are populated by the _loadDigestCard() calls
    // above — see the fetches at the top of this function.
}


// ============================================================
// ROUTING STATS (derived from each invoice's extraction_method)
// ============================================================

function populateRouting() {
    const rows = ALL_INVOICES
        .filter(inv => inv.invoice && inv.invoice.extraction_method)
        .map(inv => {
            const meta = EXTRACTION_METHOD_META[inv.invoice.extraction_method]
                || { tier: inv.invoice.extraction_method, provider: '—', fallback: false };
            return { file: inv.filename || inv.thread_id, ...meta, success: true };
        });

    const failedRows = ALL_INVOICES
        .filter(inv => inv.extraction_error)
        .map(inv => ({ file: inv.filename || inv.thread_id, tier: '—', provider: '—', success: false, fallback: false }));

    const allRows = rows.concat(failedRows);
    const total = rows.length || 1; // percentages are over successful extractions

    const groqCount = rows.filter(r => r.provider === 'groq').length;
    const geminiCount = rows.filter(r => r.provider === 'gemini' && !r.fallback).length;
    const fallbackCount = rows.filter(r => r.fallback).length;

    const groqPct = ((groqCount / total) * 100).toFixed(0);
    const geminiPct = ((geminiCount / total) * 100).toFixed(0);
    const fallbackPct = ((fallbackCount / total) * 100).toFixed(0);

    document.getElementById('routingGroq').style.width = groqPct + '%';
    document.getElementById('routingGemini').style.width = geminiPct + '%';
    document.getElementById('routingFallback').style.width = fallbackPct + '%';
    document.getElementById('routingGroqPct').textContent = groqPct + '%';
    document.getElementById('routingGeminiPct').textContent = geminiPct + '%';
    document.getElementById('routingFallbackPct').textContent = fallbackPct + '%';

    const tbody = document.getElementById('routingLogBody');
    tbody.innerHTML = '';

    if (allRows.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--text-muted)">No processed invoices yet</td></tr>`;
        return;
    }

    allRows.forEach(r => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><code style="font-size:11px">${r.file}</code></td>
            <td><code style="font-size:11px;color:var(--text-muted)">${r.tier}</code></td>
            <td style="color:${r.provider === 'groq' ? 'var(--accent-primary)' : r.provider === 'gemini' ? 'var(--accent-secondary)' : 'var(--text-muted)'};font-weight:600">${r.provider}</td>
            <td>${r.success ? '<span style="color:var(--accent-success)"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg></span>' : '<span style="color:var(--accent-danger)"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></span>'}</td>
            <td>${r.fallback ? '<span style="color:var(--accent-warning)"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14Z"/></svg> Yes</span>' : '<span style="color:var(--text-muted)">—</span>'}</td>
        `;
        tbody.appendChild(tr);
    });
}


// ============================================================
// INVOICE DETAIL MODAL
// ============================================================

function openDetailModalByThreadId(threadId) {
    const inv = findInvoice(threadId);
    if (inv) openDetailModal(inv);
}

function openDetailModal(inv) {
    const number = inv.invoice ? inv.invoice.invoice_number : (inv.filename || inv.thread_id);
    document.getElementById('detailModalTitle').innerHTML =
        `<svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/></svg> ${number}`;

    const body = document.getElementById('detailModalBody');
    const decision = inv.decision;
    const status = statusOf(inv);
    const who = decidedBy(inv);

    body.innerHTML = `
        <div class="detail-grid">
            <div class="detail-panel">
                <div class="detail-panel-title"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18Z"/><path d="M6 12H4a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2"/><path d="M18 9h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-2"/><path d="M10 6h4"/><path d="M10 10h4"/><path d="M10 14h4"/><path d="M10 18h4"/></svg> Invoice Information</div>
                ${inv.invoice ? `
                <div class="detail-row">
                    <span class="detail-row-label">Vendor</span>
                    <span class="detail-row-value">${inv.invoice.vendor}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-row-label">Invoice #</span>
                    <span class="detail-row-value" style="font-family:var(--font-mono)">${inv.invoice.invoice_number}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-row-label">Amount</span>
                    <span class="detail-row-value" style="color:var(--accent-primary);font-family:var(--font-mono)">₹${formatAmount(inv.invoice.amount)}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-row-label">Invoice Date</span>
                    <span class="detail-row-value">${inv.invoice.invoice_date || '—'}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-row-label">Due Date</span>
                    <span class="detail-row-value">${inv.invoice.due_date || '—'}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-row-label">Extraction</span>
                    <span class="detail-row-value"><code style="font-size:11px">${inv.invoice.extraction_method || '—'}</code></span>
                </div>` : `
                <div class="detail-row">
                    <span class="detail-row-label">File</span>
                    <span class="detail-row-value">${inv.filename || '—'}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-row-label">Status</span>
                    <span class="detail-row-value" style="color:var(--accent-danger)">Extraction failed</span>
                </div>`}
            </div>
            <div class="detail-panel">
                <div class="detail-panel-title"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg> Agent Results</div>
                <div class="detail-row">
                    <span class="detail-row-label">Guardrails</span>
                    <span class="detail-row-value">${inv.guardrail_passed === false
                        ? '<span style="color:var(--accent-danger)"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg> Failed</span>'
                        : inv.guardrail_passed === true
                        ? '<span style="color:var(--accent-success)"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg> Passed</span>'
                        : '<span style="color:var(--text-muted)">—</span>'}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-row-label">Compliance</span>
                    <span class="detail-row-value">${complianceBadge(inv.compliance ? inv.compliance.overall_status : null)}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-row-label">Risk Score</span>
                    <span class="detail-row-value">${riskBadge(inv.risk ? inv.risk.risk_score : null)}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-row-label">Decision</span>
                    <span class="detail-row-value">${statusBadge(status, who)}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-row-label">Decided By</span>
                    <span class="detail-row-value">${who === 'auto'
                        ? '<svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg> Auto'
                        : who === 'human'
                        ? '<svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg> Human'
                        : who === 'pending'
                        ? '<svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> Awaiting human'
                        : '<svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1Z"/></svg> System'}</span>
                </div>
                ${inv.risk && inv.risk.vendor_invoice_count ? `
                <div class="detail-row">
                    <span class="detail-row-label">Vendor History</span>
                    <span class="detail-row-value" style="color:var(--text-secondary)">${inv.risk.vendor_invoice_count} prior invoice(s)${inv.risk.vendor_avg_amount != null ? `, avg ₹${formatAmount(inv.risk.vendor_avg_amount)}` : ''}</span>
                </div>` : ''}
            </div>
        </div>

        ${inv.compliance && inv.compliance.checks && inv.compliance.checks.length ? `
        <div class="approval-reasons" style="margin-bottom:16px">
            <h5><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="8" height="4" x="8" y="2" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/></svg> Compliance Checks</h5>
            <ul>${inv.compliance.checks.map(c => `<li><strong style="color:${c.status === 'pass' ? 'var(--accent-success)' : c.status === 'warning' ? 'var(--accent-warning)' : 'var(--accent-danger)'}">[${c.status}]</strong> ${c.field}: ${c.detail}</li>`).join('')}</ul>
        </div>` : ''}

        ${inv.compliance && inv.compliance.relevant_clause ? `
        <div class="approval-reasons" style="margin-bottom:16px;border-left-color:var(--accent-secondary)">
            <h5 style="color:var(--accent-secondary)"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 12h-5"/><path d="M15 8h-5"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/></svg> Relevant Contract Clause (RAG)${inv.compliance.relevant_clause_score != null ? ` — match ${(inv.compliance.relevant_clause_score * 100).toFixed(0)}%` : ''}</h5>
            <ul><li>${inv.compliance.relevant_clause}</li></ul>
        </div>` : ''}

        ${inv.risk && inv.risk.flags && inv.risk.flags.length > 0 ? `
        <div class="approval-reasons" style="margin-bottom:16px">
            <h5><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg> Risk Flags</h5>
            <ul>${inv.risk.flags.map(f => `<li><strong>${f.flag}</strong>: ${f.detail}</li>`).join('')}</ul>
        </div>` : ''}

        ${inv.guardrail_violations && inv.guardrail_violations.length > 0 ? `
        <div class="approval-reasons" style="margin-bottom:16px;border-left-color:var(--accent-danger)">
            <h5 style="color:var(--accent-danger)"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1Z"/></svg> Guardrail Violations</h5>
            <ul>${inv.guardrail_violations.map(v => `<li><strong>[${v[0]}]</strong> ${v[1]}</li>`).join('')}</ul>
        </div>` : ''}

        ${inv.pending_approval ? `
        <div class="approval-reasons" style="margin-bottom:16px;border-left-color:var(--accent-warning)">
            <h5 style="color:var(--accent-warning)"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> Awaiting Human Approval</h5>
            <ul>${inv.pending_approval.reasons.map(r => `<li>${r}</li>`).join('')}</ul>
        </div>` : ''}

        ${decision && decision.reasons && decision.reasons.length ? `
        <div class="approval-reasons" style="${decision.status === 'rejected' || decision.status === 'blocked_by_guardrail' || decision.status === 'extraction_failed' ? 'border-left-color:var(--accent-danger)' : ''}">
            <h5 style="${decision.status === 'rejected' || decision.status === 'blocked_by_guardrail' || decision.status === 'extraction_failed' ? 'color:var(--accent-danger)' : ''}"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg> ${decision.status === 'rejected' ? 'Rejection Reason' : 'Final Decision'}</h5>
            <ul>${decision.reasons.map(r => `<li>${r}</li>`).join('')}</ul>
        </div>` : ''}

        ${inv.invoice && inv.invoice.line_items && inv.invoice.line_items.length > 0 ? `
        <div class="detail-panel" style="margin-top:16px">
            <div class="detail-panel-title"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73Z"/><path d="M12 22V12"/><path d="M3.29 7 12 12l8.71-5"/><path d="m7.5 4.27 9 5.15"/></svg> Line Items</div>
            <table class="line-items-table">
                <thead>
                    <tr>
                        <th>Description</th>
                        <th class="align-right">Qty</th>
                        <th class="align-right">Rate</th>
                        <th class="align-right">Amount</th>
                    </tr>
                </thead>
                <tbody>
                    ${inv.invoice.line_items.map(li => `
                    <tr>
                        <td>${li.description}</td>
                        <td class="align-right">${li.quantity}</td>
                        <td class="align-right">₹${formatAmount(li.rate)}</td>
                        <td class="align-right">₹${formatAmount(li.amount)}</td>
                    </tr>`).join('')}
                </tbody>
            </table>
        </div>` : ''}

        ${inv.pending_approval ? `
        <div class="approval-card-actions" style="margin-top:16px">
            <button class="btn btn-primary" onclick="closeDetailModal(); approveInvoice('${inv.thread_id}')">
                <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg> Approve
            </button>
            <button class="btn btn-danger" onclick="closeDetailModal(); openRejectModal('${inv.thread_id}')">
                <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg> Reject
            </button>
            <button class="btn btn-secondary" onclick="closeDetailModal(); openEditModal('${inv.thread_id}')">
                <svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/><path d="m15 5 4 4"/></svg> Edit Fields
            </button>
        </div>` : ''}
    `;

    document.getElementById('detailModal').classList.add('visible');
}

function closeDetailModal() {
    document.getElementById('detailModal').classList.remove('visible');
}


// ============================================================
// TOAST NOTIFICATIONS
// ============================================================

function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    const icons = { success: '<svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>', error: '<svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/></svg>', warning: '<svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>', info: '<svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>' };
    toast.innerHTML = `<span>${icons[type] || ''}</span><span>${message}</span>`;

    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'all 0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}


// ============================================================
// HELPERS
// ============================================================

function formatAmount(n) {
    return Number(n).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function truncate(str, len) {
    return str.length > len ? str.substring(0, len) + '…' : str;
}

function statusBadge(status, decidedByVal) {
    const map = {
        'approved_exported': decidedByVal === 'auto'
            ? '<span class="status-badge auto"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14Z"/></svg> Auto-Approved</span>'
            : '<span class="status-badge approved"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg> Approved</span>',
        'rejected': '<span class="status-badge rejected"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg> Rejected</span>',
        'escalate_to_human': '<span class="status-badge pending"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> Pending</span>',
        'blocked_by_guardrail': '<span class="status-badge blocked"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1Z"/></svg> Blocked</span>',
        'extraction_failed': '<span class="status-badge rejected"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7.86 2h8.28L22 7.86v8.28L16.14 22H7.86L2 16.14V7.86Z"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/></svg> Failed</span>',
        'processing': '<span class="status-badge pending"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg> Processing</span>',
    };
    return map[status] || `<span class="status-badge">${status}</span>`;
}

function complianceBadge(status) {
    if (status === 'pass') return '<span class="status-badge approved"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg> Pass</span>';
    if (status === 'fail') return '<span class="status-badge rejected"><svg class="icon-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg> Fail</span>';
    return '<span class="status-badge" style="color:var(--text-muted)">—</span>';
}

function riskBadge(score) {
    const map = {
        'low': '<span class="status-badge approved">Low</span>',
        'medium': '<span class="status-badge pending">Medium</span>',
        'high': '<span class="status-badge rejected">High</span>',
    };
    return map[score] || '<span class="status-badge" style="color:var(--text-muted)">—</span>';
}


// ============================================================
// KEYBOARD SHORTCUTS & CLOSE ON OUTSIDE CLICK
// ============================================================

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeEditModal();
        closeRejectModal();
        closeDetailModal();
        closeContractModal();
    }
});

document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) {
            closeEditModal();
            closeRejectModal();
            closeDetailModal();
            closeContractModal();
        }
    });
});


// ============================================================
// INITIAL LOAD
// ============================================================

if (getAuth()) {
    showApp();
    bootApp();
} else {
    showAuthScreen();
}