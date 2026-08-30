let currentAuditMode = 'tealium';
let cachedResults = [];
let scheduleFile = null;
let clickResults = [];                // per-element click tracking data
let dcClickResults = [];              // domain crawl click tracking data

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function colorizeLogLines(logs) {
    return logs.map(l => {
        const text = String(l);
        let colorStyle = '';
        if (text.includes('ERROR') || text.includes('failed') || text.includes('failed with code') || text.includes('rejected')) {
            colorStyle = 'color: var(--c-bad); text-shadow: 0 0 6px rgba(248, 113, 113, 0.4);';
        } else if (text.includes('WARNING') || text.includes('WARN')) {
            colorStyle = 'color: var(--c-warn); text-shadow: 0 0 6px rgba(251, 191, 36, 0.4);';
        } else if (text.includes('SUCCESS') || text.includes('Finished') || text.includes('Completed') || text.includes('Saved report')) {
            colorStyle = 'color: var(--c-ok); text-shadow: 0 0 6px rgba(52, 211, 153, 0.4);';
        } else if (text.includes('[Crawl]') || text.includes('CRAWLING') || text.includes('Discovering')) {
            colorStyle = 'color: var(--c-teal); text-shadow: 0 0 6px rgba(34, 211, 238, 0.4);';
        } else if (text.includes('[Audit]') || text.includes('Auditing') || text.includes('Quick Run:')) {
            colorStyle = 'color: var(--c-purple); text-shadow: 0 0 6px rgba(192, 132, 252, 0.4);';
        }
        
        const styleAttr = colorStyle ? ` style="${colorStyle}"` : '';
        return `<div${styleAttr}>${escapeHtml(text)}</div>`;
    }).join('');
}

function formatSingleGa4Event(e) {
    if (!e) return '<span style="color:var(--muted)">--</span>';
    // One event can be sent to several GA4 properties at once (dual tagging).
    // Show each property id as its own chip so a single click doesn't look
    // like it fired the same event two or three separate times.
    const ids = (e.measurement_ids && e.measurement_ids.length)
        ? e.measurement_ids
        : (e.measurement_id ? [e.measurement_id] : []);
    const chip = (id) => {
        const isApi = String(id).startsWith('(');
        const bg = isApi ? 'var(--t-purple)' : 'var(--t-info)';
        const fg = isApi ? 'var(--c-purple)' : 'var(--c-info)';
        const bd = isApi ? 'var(--e-purple)' : 'var(--e-info)';
        return `<span class="badge" style="background:${bg};color:${fg};border:1px solid ${bd};margin:0 3px 4px 0;display:inline-block;font-family:monospace;font-size:0.65rem;">${escapeHtml(id)}</span>`;
    };
    const measId = ids.map(chip).join('');
    const hits = (e.hit_count && e.hit_count > 1)
        ? `<span style="color:var(--muted);font-size:0.65rem"> ×${e.hit_count}</span>` : '';
    let s = `<div style="margin-bottom:4px;">${measId}<br><b style="color:var(--c-ok)">${escapeHtml(e.event)}</b>${hits}</div>`;
    const pkeys = Object.keys(e.params || {});
    if (pkeys.length) {
        s += `<div class="event-params">` +
             pkeys.map(k => `<div style="margin-bottom:2px; word-break:break-all;"><span style="color:var(--muted)">${escapeHtml(k)}</span>: <span style="color:var(--c-info)">${escapeHtml(String(e.params[k]))}</span></div>`).join('') +
             `</div>`;
    }
    return s;
}

function switchTab(tabId) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`[onclick="switchTab('${tabId}')"]`).classList.add('active');

    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    document.getElementById(`tab-${tabId}`).classList.add('active');

    if (tabId === 'scheduler') {
        loadSchedules();
        loadHistory();
    }
}

// Smart element type display for click tracking
function formatElementType(tag) {
    const t = (tag || '').toUpperCase();
    const typeMap = {
        'A': { label: 'Link', bg: 'var(--t-info)', color: 'var(--c-info)', border: 'var(--e-info)' },
        'BUTTON': { label: 'Button', bg: 'rgba(34,197,94,0.12)', color: 'var(--c-ok)', border: 'rgba(34,197,94,0.35)' },
        'INPUT': { label: 'Input', bg: 'var(--t-warn)', color: 'var(--c-warn)', border: 'var(--e-warn)' },
        'SELECT': { label: 'Select', bg: 'var(--t-warn)', color: 'var(--c-warn)', border: 'var(--e-warn)' },
        'TEXTAREA': { label: 'Textarea', bg: 'var(--t-warn)', color: 'var(--c-warn)', border: 'var(--e-warn)' },
        'FORM': { label: 'Form', bg: 'var(--t-purple)', color: 'var(--c-purple)', border: 'var(--e-purple)' },
        'VIDEO': { label: 'Video', bg: 'var(--t-pink)', color: 'var(--c-pink)', border: 'var(--e-pink)' },
        'AUDIO': { label: 'Audio', bg: 'var(--t-pink)', color: 'var(--c-pink)', border: 'var(--e-pink)' },
        'SUMMARY': { label: 'Toggle', bg: 'rgba(139,92,246,0.12)', color: 'var(--c-purple)', border: 'rgba(139,92,246,0.35)' },
        'AREA': { label: 'Area', bg: 'var(--t-info)', color: 'var(--c-info)', border: 'var(--e-info)' },
        'LABEL': { label: 'Label', bg: 'var(--t-warn)', color: 'var(--c-warn)', border: 'var(--e-warn)' },
    };
    const info = typeMap[t] || { label: t, bg: 'rgba(139,92,246,0.1)', color: 'var(--c-purple)', border: 'rgba(139,92,246,0.3)' };
    return `<span class="badge" style="background:${info.bg};color:${info.color};border:1px solid ${info.border}">${info.label}</span>`;
}

function setAuditMode(mode) {
    currentAuditMode = mode;
    document.getElementById('modeTealium').classList.toggle('active', mode === 'tealium');
    document.getElementById('modeGA4').classList.toggle('active', mode === 'ga4');
    document.getElementById('modeClicks').classList.toggle('active', mode === 'clicks');
    if (mode === 'clicks') {
        loadClickResults();
    }
    renderTable();
}


// ===== CLICK RESULTS =====
async function loadClickResults() {
    try {
        const d = await (await fetch('/api/tag-validator/click-results')).json();
        clickResults = d.results || [];
        if (currentAuditMode === 'clicks') renderTable();
    } catch { clickResults = []; }
}

// --- THEME TOGGLE ---
function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('tv-theme', theme);
    document.querySelectorAll('#themeToggle button').forEach(b =>
        b.classList.toggle('active', b.dataset.themeSet === theme));
}
document.addEventListener('DOMContentLoaded', () => {
    applyTheme(localStorage.getItem('tv-theme') || 'dark');
    const toggle = document.getElementById('themeToggle');
    if (toggle) toggle.querySelectorAll('button').forEach(b =>
        b.onclick = () => applyTheme(b.dataset.themeSet));
});

document.addEventListener('DOMContentLoaded', () => {
    // --- MANUAL TAB LOGIC ---
    const uploadBoxM = document.getElementById('uploadBoxManual');
    const fileInputM = document.getElementById('fileInputManual');
    const uploadTextM = document.getElementById('uploadTextManual');
    const runBtn = document.getElementById('runBtn');
    const logBox = document.getElementById('logBox');
    const downloadBtn = document.getElementById('downloadBtn');

    uploadBoxM.onclick = () => fileInputM.click();
    fileInputM.onchange = async (e) => {
        if (!e.target.files.length) return;
        const f = e.target.files[0];
        uploadTextM.innerText = 'Uploading...';
        const fd = new FormData();
        fd.append('file', f);
        const r = await fetch('/api/tag-validator/upload', { method: 'POST', body: fd });
        if (r.ok) {
            uploadTextM.innerHTML = '<span class="ready">' + f.name + '</span>';
            runBtn.disabled = false;
        }
    };

    const cancelBtn = document.getElementById('cancelBtn');

    runBtn.onclick = async () => {
        runBtn.disabled = true;
        runBtn.innerText = 'Validating...';
        cancelBtn.classList.remove('hidden');
        cancelBtn.disabled = false;
        logBox.classList.remove('hidden');
        document.getElementById('progressSection').classList.remove('hidden');

        // Pass mode to the run command
        await fetch('/api/tag-validator/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: currentAuditMode })
        });

        const poll = setInterval(async () => {
            const r = await fetch('/api/tag-validator/status');
            const d = await r.json();
            document.body.classList.toggle('audit-running', d.running);
            const logs = d.logs.filter(l => !l.includes('DeprecationWarning') && !l.includes('Pyarrow') && l.trim());
            logBox.innerHTML = colorizeLogLines(logs);
            logBox.scrollTop = logBox.scrollHeight;

            const last = [...logs].reverse().find(l => l.match(/\[\d+\/\d+\]/));
            if (last) {
                const m = last.match(/\[(\d+)\/(\d+)\]/);
                if (m) {
                    const c = +m[1], t = +m[2], pct = Math.round(c / t * 100);
                    document.getElementById('progressLabel').innerText = c + '/' + t;
                    document.getElementById('progressBar').style.width = pct + '%';
                }
            }

            if (!d.running) {
                document.body.classList.remove('audit-running');
                clearInterval(poll);
                runBtn.disabled = false;
                runBtn.innerText = d.cancelled ? 'Cancelled — Run Again' : 'Run Again';
                cancelBtn.classList.add('hidden');
                loadResults();
            }
        }, 800);
    };

    cancelBtn.onclick = async () => {
        if (!confirm('Cancel the running validation? Partial results may not be saved.')) return;
        cancelBtn.disabled = true;
        cancelBtn.innerText = 'Cancelling...';
        await fetch('/api/tag-validator/cancel', { method: 'POST' });
        // Polling loop will detect !running and clean up UI state
    };

    downloadBtn.onclick = () => window.location.href = '/api/tag-validator/download';

    // --- QUICK RUN (single URL) ---
    const quickRunBtn = document.getElementById('quickRunBtn');
    const singleUrlInput = document.getElementById('singleUrlInput');

    quickRunBtn.onclick = async () => {
        const url = singleUrlInput.value.trim();
        if (!url) { alert('Paste a URL first (e.g. https://example.com)'); return; }

        quickRunBtn.disabled = true;
        quickRunBtn.innerText = '⚡ Running...';
        runBtn.disabled = true;
        cancelBtn.classList.remove('hidden');
        cancelBtn.disabled = false;
        cancelBtn.innerText = 'Cancel';
        logBox.classList.remove('hidden');
        document.getElementById('progressSection').classList.remove('hidden');

        await fetch('/api/tag-validator/run-single', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, mode: currentAuditMode })
        });

        const poll = setInterval(async () => {
            const r = await fetch('/api/tag-validator/status');
            const d = await r.json();
            document.body.classList.toggle('audit-running', d.running);
            const logs = d.logs.filter(l => !l.includes('DeprecationWarning') && !l.includes('Pyarrow') && l.trim());
            logBox.innerHTML = colorizeLogLines(logs);
            logBox.scrollTop = logBox.scrollHeight;

            const last = [...logs].reverse().find(l => l.match(/\[\d+\/\d+\]/));
            if (last) {
                const m = last.match(/\[(\d+)\/(\d+)\]/);
                if (m) {
                    const c = +m[1], t = +m[2], pct = Math.round(c / t * 100);
                    document.getElementById('progressLabel').innerText = c + '/' + t;
                    document.getElementById('progressBar').style.width = pct + '%';
                }
            }

            if (!d.running) {
                document.body.classList.remove('audit-running');
                clearInterval(poll);
                quickRunBtn.disabled = false;
                quickRunBtn.innerText = '⚡ Quick Run';
                runBtn.disabled = false;
                cancelBtn.classList.add('hidden');
                loadResults();
            }
        }, 800);
    };

    // Allow pressing Enter in the URL input to trigger Quick Run
    singleUrlInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') quickRunBtn.click();
    });

    renderTable();

    // --- SCHEDULER TAB LOGIC ---
    const uploadBoxS = document.getElementById('uploadBoxSchedule');
    const fileInputS = document.getElementById('fileInputSchedule');
    const uploadTextS = document.getElementById('uploadTextSchedule');
    const scheduleBtn = document.getElementById('scheduleBtn');
    const scheduleFreq = document.getElementById('scheduleFreq');

    uploadBoxS.onclick = () => fileInputS.click();
    fileInputS.onchange = (e) => {
        if (!e.target.files.length) return;
        scheduleFile = e.target.files[0];
        uploadTextS.innerHTML = '<span class="ready">' + scheduleFile.name + '</span>';
        scheduleBtn.disabled = false;
    };

    scheduleBtn.onclick = async () => {
        if (!scheduleFile) return;
        scheduleBtn.disabled = true;
        scheduleBtn.innerText = 'Creating...';
        
        const fd = new FormData();
        fd.append('file', scheduleFile);
        fd.append('frequency', scheduleFreq.value);
        fd.append('email', document.getElementById('scheduleEmail').value.trim());

        const r = await fetch('/api/schedule/add', { method: 'POST', body: fd });
        if (r.ok) {
            scheduleFile = null;
            uploadTextS.innerText = 'Select Excel File for Automation';
            scheduleBtn.innerText = 'Create Schedule';
            loadSchedules();
        } else {
            scheduleBtn.disabled = false;
            scheduleBtn.innerText = 'Create Schedule';
            alert("Error creating schedule");
        }
    };

    document.getElementById('testEmailBtn').onclick = async () => {
        const flash = document.getElementById('schFlash');
        flash.style.color = 'var(--c-teal)';
        flash.innerText = 'Sending test email...';
        const r = await fetch('/api/test-email', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: document.getElementById('scheduleEmail').value.trim() }),
        });
        const j = await r.json();
        flash.style.color = r.ok ? 'var(--c-teal)' : 'var(--c-bad)';
        flash.innerText = r.ok ? j.message : ('Error: ' + j.error);
    };

    async function refreshMailState() {
        try {
            const d = await (await fetch('/api/mail-config')).json();
            const el = document.getElementById('mailState');
            if (d.configured) {
                el.innerHTML = `✅ Sending as <b style="color:var(--c-teal)">${d.sender}</b>`;
                document.getElementById('brevoSender').value = d.sender || '';
            } else {
                el.innerHTML = '⚠ Not configured — alerts will be skipped';
            }
        } catch { /* ignore */ }
    }

    document.getElementById('saveMailBtn').onclick = async () => {
        const sender = document.getElementById('brevoSender').value.trim();
        const apiKey = document.getElementById('brevoKey').value.trim();
        const flash = document.getElementById('schFlash');
        if (!sender || !apiKey) { flash.style.color = 'var(--c-bad)'; flash.innerText = 'Enter sender email + Brevo API key'; return; }
        const r = await fetch('/api/mail-config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sender, apiKey }),
        });
        const j = await r.json();
        flash.style.color = r.ok ? 'var(--c-teal)' : 'var(--c-bad)';
        flash.innerText = r.ok ? 'Email settings saved' : ('Error: ' + j.error);
        document.getElementById('brevoKey').value = '';
        refreshMailState();
    };

    document.getElementById('clearMailBtn').onclick = async () => {
        await fetch('/api/mail-config', { method: 'DELETE' });
        document.getElementById('brevoSender').value = '';
        document.getElementById('brevoKey').value = '';
        refreshMailState();
    };

    refreshMailState();
});

const B = v => v === 'PASS' ? '<span class="badge b-pass">PASS</span>' : '<span class="badge b-fail">FAIL</span>';
const ID = v => v ? '<span class="mono">' + v + '</span>' : '<span style="color:var(--muted)">--</span>';

async function loadResults() {
    const r = await fetch('/api/tag-validator/results');
    const d = await r.json();
    if (!d.results || !d.results.length) return;
    cachedResults = d.results;
    document.getElementById('downloadBtn').classList.remove('hidden');
    if (currentAuditMode === 'clicks') await loadClickResults();
    renderTable();
}

function renderTable() {
    const head = document.getElementById('tableHead');
    const body = document.getElementById('resultsBody');
    const statsBar = document.getElementById('statsBar');

    if (currentAuditMode === 'clicks') {
        head.innerHTML = `
            <tr>
                <th>#</th><th>URL</th>
                <th style="text-align:center">Elements</th>
                <th class="h-teal" style="text-align:center">With Tracking</th>
                <th style="text-align:center; color:var(--c-bad)">No Tracking</th>
                <th>Details</th>
            </tr>
        `;
    } else if (currentAuditMode === 'tealium') {
        head.innerHTML = `
            <tr>
                <th rowspan="2">#</th><th rowspan="2">URL</th>
                <th colspan="4" class="h-teal" style="text-align:center;">TEALIUM ANALYTICS</th>
                <th colspan="3" class="h-adobe" style="text-align:center;">ADOBE ANALYTICS</th>
            </tr>
            <tr>
                <th class="h-teal">Loaded</th><th class="h-teal">Account</th><th class="h-teal">Profile</th><th class="h-teal">Env</th>
                <th class="h-adobe">Loaded</th><th class="h-adobe">Report Suite</th><th class="h-adobe">Page View</th>
            </tr>
        `;
    } else {
        head.innerHTML = `
            <tr>
                <th rowspan="2">#</th><th rowspan="2">URL</th>
                <th colspan="2" class="h-adobe" style="text-align:center; background:rgba(255,255,255,0.05)">GTM</th>
                <th colspan="3" class="h-adobe" style="text-align:center; background: var(--t-info); color: var(--c-info);">GA4</th>
            </tr>
            <tr>
                <th style="background:var(--surface-3)">Loaded</th><th style="background:var(--surface-3)">GTM ID</th>
                <th class="h-adobe" style="background: var(--t-info); color: var(--c-info);">Fired</th>
                <th class="h-adobe" style="background: var(--t-info); color: var(--c-info);">Measurement ID</th>
                <th class="h-adobe" style="background: var(--t-info); color: var(--c-info);">Page View</th>
            </tr>
        `;
    }

    if (!cachedResults.length) {
        const ec = currentAuditMode === 'clicks' ? 6 : (currentAuditMode === 'tealium' ? 9 : 7);
        body.innerHTML = `<tr><td colspan="${ec}" class="empty-msg">Upload a file and run validation</td></tr>`;
        statsBar.classList.add('hidden');
        return;
    }

    let st = { teal: 0, adobe: 0, ga4: 0, compliant: 0, violations: 0 };
    cachedResults.forEach(r => {
        if (r.Tealium_Loaded === 'PASS') st.teal++;
        if (r.Adobe_Loaded === 'PASS') st.adobe++;
        if (r.GA4_Fired === 'PASS') st.ga4++;
        if (r.Compliance === 'PASS') st.compliant++;
        if (r.Compliance === 'FAIL') st.violations++;
    });

    statsBar.classList.remove('hidden');
    if (currentAuditMode === 'clicks') {
        const totalEl = cachedResults.reduce((a, r) => a + (Number(r.Total_Elements) || 0), 0);
        const tracked = cachedResults.reduce((a, r) => a + (Number(r.With_Tracking) || 0), 0);
        const untracked = cachedResults.reduce((a, r) => a + (Number(r.Without_Tracking) || 0), 0);
        const accuracy = totalEl ? Math.round((tracked / totalEl) * 100) : 100;
        statsBar.innerHTML = `
            <div class="stat"><div class="stat-dot" style="background:var(--c-info);color:var(--c-info);"></div><div><div class="stat-val" style="color:var(--c-info-2);">${totalEl}</div><div class="stat-lbl">Total Elements Clicked</div></div></div>
            <div class="stat"><div class="stat-dot dot-teal"></div><div><div class="stat-val val-teal">${tracked}</div><div class="stat-lbl">With Analytics Tracking</div></div></div>
            <div class="stat"><div class="stat-dot" style="background:var(--c-bad);color:var(--c-bad);"></div><div><div class="stat-val" style="color:var(--c-bad);">${untracked}</div><div class="stat-lbl">No Tracking Detected</div></div></div>
            <div class="stat"><div class="stat-dot" style="background:var(--c-purple);color:var(--c-purple);"></div><div><div class="stat-val" style="color:var(--c-purple);">${accuracy}%</div><div class="stat-lbl">Tracking Accuracy</div></div></div>
        `;
    } else if (currentAuditMode === 'tealium') {
        statsBar.innerHTML = `<div class="stat"><div class="stat-dot dot-teal"></div><div><div class="stat-val val-teal">${st.teal}/${cachedResults.length}</div><div class="stat-lbl">Tealium Detected</div></div></div>`;
    } else {
        statsBar.innerHTML = `
            <div class="stat"><div class="stat-dot dot-adobe"></div><div><div class="stat-val val-adobe">${st.adobe}/${cachedResults.length}</div><div class="stat-lbl">Adobe Detected</div></div></div>
            <div class="stat"><div class="stat-dot" style="background:var(--c-info); color:var(--c-info);"></div><div><div class="stat-val" style="color:var(--c-info-2);">${st.ga4}/${cachedResults.length}</div><div class="stat-lbl">GA4 Detected</div></div></div>
        `;
    }

    const PIX = v => (!v || v === 'None')
        ? '<span style="color:var(--muted)">None</span>'
        : '<span class="mono" style="white-space:normal">' + v + '</span>';

    if (currentAuditMode === 'clicks') {
        const clickByUrl = {};
        clickResults.forEach(x => { clickByUrl[x.URL] = x; });
        let html = '';
        cachedResults.forEach((r, i) => {
            const rich = clickByUrl[r.URL];
            const elems = (rich && rich.elements) || [];
            const detailId = 'click-detail-' + i;
            html += `<tr style="cursor:pointer" onclick="document.getElementById('${detailId}').classList.toggle('hidden')">
                <td>${i + 1}</td>
                <td class="url-col" title="${r.URL}">${r.URL}</td>
                <td style="text-align:center"><span class="badge b-count">${r.Total_Elements || 0}</span></td>
                <td style="text-align:center"><span class="badge b-pass">${r.With_Tracking || 0}</span></td>
                <td style="text-align:center">${(r.Without_Tracking || 0) > 0 ? '<span class="badge b-fail">' + r.Without_Tracking + '</span>' : '<span style="color:var(--muted)">0</span>'}</td>
                <td><span style="font-size:0.7rem;color:var(--accent);cursor:pointer">▶ Expand</span></td>
            </tr>`;
            // Expandable detail row
            html += `<tr id="${detailId}" class="hidden"><td colspan="6" style="padding:0;background:var(--inset-bg)">`;
            if (!elems.length) {
                html += '<div style="padding:16px;color:var(--muted)">No clickable elements found</div>';
            } else {
                html += '<table style="width:100%;margin:0;font-size:0.75rem"><thead><tr>' +
                    '<th style="width:30px">#</th><th>Element</th><th>Text</th><th>Type</th><th>Zone</th>' +
                    '<th class="h-teal">GA4 Event 1</th><th class="h-teal">GA4 Event 2</th><th class="h-teal">GA4 Event 3</th>' +
                    '<th class="h-adobe">Adobe / Other Calls</th><th>Network</th></tr></thead><tbody>';
                elems.forEach((el, j) => {
                    const ga4_1 = formatSingleGa4Event(el.ga4_events[0]);
                    const ga4_2 = formatSingleGa4Event(el.ga4_events[1]);
                    let ga4_3 = formatSingleGa4Event(el.ga4_events[2]);
                    if (el.ga4_events.length > 3) {
                        for (let k = 3; k < el.ga4_events.length; k++) {
                            ga4_3 += '<hr style="border-color:var(--border);margin:8px 0">' + formatSingleGa4Event(el.ga4_events[k]);
                        }
                    }
                        
                    const aaHtml = el.adobe_calls.length
                        ? el.adobe_calls.map(c => {
                            const displayName = c.link_name || c.events || c.link_type || 'adobe_call';
                            const rsid = c.report_suite ? '<span class="badge" style="background:var(--t-orange);color:var(--c-orange);border:1px solid var(--e-orange);margin-bottom:4px;display:inline-block;font-family:monospace;font-size:0.6rem;">' + c.report_suite + '</span><br>' : '';
                            let s = '<div style="margin-bottom:6px;">' + rsid + '<b style="color:var(--c-orange)">' + displayName + '</b> <span style="color:var(--muted);font-size:0.7rem">(' + c.link_type + ')</span>';
                            
                            const items = [];
                            if (c.events) items.push({ k: 'events', v: c.events });
                            Object.keys(c.evars || {}).forEach(k => { items.push({ k, v: c.evars[k] }); });
                            Object.keys(c.props || {}).forEach(k => { items.push({ k, v: c.props[k] }); });
                            
                            if (items.length) {
                                s += `<div class="event-params">` + 
                                     items.map(item => `<div style="margin-bottom:2px; word-break:break-all;"><span style="color:var(--muted)">${item.k}</span>: <span style="color:var(--c-orange)">${item.v}</span></div>`).join('') + 
                                     `</div>`;
                            }
                            s += '</div>';
                            return s;
                        }).join('<hr style="border-color:var(--border);margin:8px 0">')
                        : (el.adobe_websdk && el.adobe_websdk.length
                            ? el.adobe_websdk.map(w => '<b style="color:var(--c-orange)">' + w.event_type + '</b>').join('<br>')
                            : '');

                    const otherHtml = el.other_analytics && el.other_analytics.length
                        ? el.other_analytics.map(o => `<div style="margin-bottom:4px;"><span class="badge" style="background:var(--t-purple);color:var(--c-purple);border:1px solid var(--e-purple);">${o.vendor}</span></div>`).join('')
                        : '';
                    
                    let combinedAdobeOther = aaHtml;
                    if (otherHtml) {
                        if (combinedAdobeOther) combinedAdobeOther += '<hr style="border-color:var(--border);margin:8px 0">';
                        combinedAdobeOther += otherHtml;
                    }
                    if (!combinedAdobeOther) combinedAdobeOther = '<span style="color:var(--muted)">--</span>';

                    const elLabel = el.element.id ? '#' + el.element.id : el.element.selector.substring(0, 40);
                    const trackIcon = el.skipped ? '⏭️' : (el.has_tracking ? '✅' : '⚠️');
                    const zone = el.element.zone || 'body';
                    // Trust indicators: a result is only meaningful if the
                    // click actually reached the intended element.
                    const badges = [];
                    if (!el.skipped && el.click_verified === false) {
                        badges.push('<span class="badge" title="The click could not be confirmed on this exact element — treat its events with caution" style="background:var(--t-orange);color:var(--c-orange);border:1px solid var(--e-orange);font-size:0.55rem">unverified</span>');
                    }
                    if (el.element.is_download) {
                        badges.push('<span class="badge" title="Download link" style="background:rgba(56,189,248,0.12);color:var(--c-info-2);border:1px solid rgba(56,189,248,0.35);font-size:0.55rem">download</span>');
                    }
                    if (el.blocked_by) {
                        badges.push(`<span class="badge" title="Covered by ${escapeHtml(el.blocked_by)} — the audit clicked through it" style="background:rgba(148,163,184,0.12);color:var(--text-2);border:1px solid rgba(148,163,184,0.35);font-size:0.55rem">overlaid</span>`);
                    }
                    const badgeHtml = badges.length ? '<div style="margin-top:3px">' + badges.join(' ') + '</div>' : '';
                    const netReqs = el.network_requests || [];
                    const netCount = el.network_count || netReqs.length;
                    const netId = detailId + '-net-' + j;
                    const escUrl = (u) => String(u).replace(/&/g, '&amp;').replace(/</g, '&lt;');
                    const netCell = netCount
                        ? `<span class="badge b-count" style="cursor:pointer" onclick="event.stopPropagation();document.getElementById('${netId}').classList.toggle('hidden')">${netCount} req ▾</span>` +
                          `<div id="${netId}" class="hidden" style="max-height:220px;overflow:auto;margin-top:6px;text-align:left">` +
                          netReqs.map(nr => `<div class="mono" style="font-size:0.62rem;word-break:break-all;color:var(--muted);margin-bottom:3px">${escUrl(nr.url)}</div>`).join('') +
                          `</div>`
                        : (el.skipped ? '<span style="color:var(--muted)">skipped</span>' : '<span style="color:var(--muted)">0</span>');
                    html += `<tr>
                        <td>${j + 1}</td>
                        <td title="${el.element.selector}">${trackIcon} <span class="mono">${elLabel}</span>${badgeHtml}</td>
                        <td style="max-width:150px;overflow:hidden;text-overflow:ellipsis">${el.element.text || '--'}</td>
                        <td>${formatElementType(el.element.tag)}</td>
                        <td style="text-align:center"><span class="badge" style="font-size:0.6rem">${zone}</span></td>
                        <td style="white-space:normal;max-width:240px"><div class="event-cell-container">${ga4_1}</div></td>
                        <td style="white-space:normal;max-width:240px"><div class="event-cell-container">${ga4_2}</div></td>
                        <td style="white-space:normal;max-width:240px"><div class="event-cell-container">${ga4_3}</div></td>
                        <td style="white-space:normal;max-width:240px"><div class="event-cell-container">${combinedAdobeOther}</div></td>
                        <td style="white-space:normal;max-width:280px">${netCell}</td>
                    </tr>`;
                });
                html += '</tbody></table>';
            }
            html += '</td></tr>';
        });
        body.innerHTML = html;
        return;
    }

    body.innerHTML = cachedResults.map((r, i) => {
        if (currentAuditMode === 'tealium') {
            return `<tr>
                <td>${i + 1}</td>
                <td class="url-col" title="${r.URL}">${r.URL}</td>
                <td>${B(r.Tealium_Loaded)}</td>
                <td>${ID(r.Tealium_Account)}</td>
                <td>${ID(r.Tealium_Profile)}</td>
                <td>${ID(r.Tealium_Env)}</td>
                <td>${B(r.Adobe_Loaded)}</td>
                <td>${ID(r.Adobe_ReportSuite)}</td>
                <td>${B(r.Adobe_PageView)}</td>
            </tr>`;
        } else {
            return `<tr>
                <td>${i + 1}</td>
                <td class="url-col" title="${r.URL}">${r.URL}</td>
                <td>${B(r.GTM_Loaded)}</td>
                <td>${ID(r.GTM_ID)}</td>
                <td>${B(r.GA4_Fired)}</td>
                <td>${ID(r.GA4_Measurement_ID)}</td>
                <td>${B(r.GA4_PageView)}</td>
            </tr>`;
        }
    }).join('');
}

async function loadSchedules() {
    const r = await fetch('/api/schedule/list');
    const d = await r.json();
    const tbody = document.getElementById('schedulesBody');
    if (!d.schedules || !d.schedules.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="empty-msg">No active schedules</td></tr>';
        return;
    }

    tbody.innerHTML = d.schedules.map(s => `
        <tr>
            <td class="mono">${s.id.substring(0,8)}</td>
            <td>${s.filename}</td>
            <td><span class="badge b-pass" style="background:rgba(139,92,246,0.1);color:var(--c-purple);border-color:rgba(139,92,246,0.3);">${s.frequency}</span></td>
            <td>${s.email ? s.email : '<span style="color:var(--muted)">—</span>'}</td>
            <td>${new Date(s.createdAt).toLocaleString()}</td>
            <td>${s.lastRun ? new Date(s.lastRun).toLocaleString() : 'Never'}</td>
            <td style="white-space:normal;max-width:220px;font-size:0.7rem;color:var(--muted)">${s.lastStatus || '—'}</td>
            <td><button class="btn btn-danger" onclick="cancelSchedule('${s.id}')">Cancel</button></td>
        </tr>
    `).join('');
}

async function cancelSchedule(id) {
    if (!confirm('Are you sure you want to cancel this schedule?')) return;
    await fetch('/api/schedule/cancel/' + id, { method: 'DELETE' });
    loadSchedules();
}

async function loadHistory() {
    const r = await fetch('/api/schedule/history');
    const d = await r.json();
    const tbody = document.getElementById('historyBody');
    if (!d.history || !d.history.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty-msg">No automated runs yet</td></tr>';
        return;
    }
    
    tbody.innerHTML = d.history.map(h => `
        <tr>
            <td>${h.filename}</td>
            <td>${new Date(h.date).toLocaleString()}</td>
            <td>${(h.size / 1024).toFixed(1)} KB</td>
            <td><button class="btn btn-download" style="padding:6px 12px" onclick="window.location.href='/api/schedule/download/${h.filename}'">Download</button></td>
        </tr>
    `).join('');
}

// =============== DOMAIN CRAWL ===============
let dcMode = 'tealium';
let dcPollHandle = null;
let dcCachedResults = [];

function setDomainMode(mode) {
    dcMode = mode;
    document.getElementById('dcModeTealium').classList.toggle('active', mode === 'tealium');
    document.getElementById('dcModeGA4').classList.toggle('active', mode === 'ga4');
    document.getElementById('dcModeClicks').classList.toggle('active', mode === 'clicks');
    renderDcTable();
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

document.addEventListener('DOMContentLoaded', () => {
    const discoverBtn = document.getElementById('discoverBtn');
    const crawlValidateBtn = document.getElementById('crawlValidateBtn');
    const validateDiscoveredBtn = document.getElementById('validateDiscoveredBtn');
    const downloadUrlsBtn = document.getElementById('downloadUrlsBtn');
    const dcDownloadBtn = document.getElementById('dcDownloadBtn');

    if (discoverBtn) discoverBtn.onclick = () => startDomainRun(false);
    if (crawlValidateBtn) crawlValidateBtn.onclick = () => startDomainRun(true);

    if (validateDiscoveredBtn) validateDiscoveredBtn.onclick = async () => {
        validateDiscoveredBtn.disabled = true;
        validateDiscoveredBtn.innerText = 'Validating...';
        document.getElementById('dcLogBox').classList.remove('hidden');
        document.getElementById('dcProgressSection').classList.remove('hidden');
        const dcCancelBtn = document.getElementById('dcCancelBtn');
        if (dcCancelBtn) {
            dcCancelBtn.classList.remove('hidden');
            dcCancelBtn.disabled = false;
            dcCancelBtn.innerText = 'Cancel';
        }
        await fetch('/api/tag-validator/run', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: dcMode }),
        });
        pollDomainStatus(true);
    };

    if (downloadUrlsBtn) downloadUrlsBtn.onclick = () => window.location.href = '/api/tag-validator/crawled-urls/download';
    if (dcDownloadBtn) dcDownloadBtn.onclick = () => window.location.href = '/api/tag-validator/download';

    const dcCancelBtn = document.getElementById('dcCancelBtn');
    if (dcCancelBtn) dcCancelBtn.onclick = async () => {
        if (!confirm('Cancel the crawl/validation? Partial discovered URLs will still be saved.')) return;
        dcCancelBtn.disabled = true;
        dcCancelBtn.innerText = 'Cancelling...';
        await fetch('/api/tag-validator/cancel', { method: 'POST' });
        // pollDomainStatus loop detects !running and cleans up
    };
});

async function startDomainRun(alsoValidate) {
    const url = document.getElementById('domainInput').value.trim();
    if (!url) { alert('Please enter a domain URL (e.g. https://example.com)'); return; }
    // 0 (or empty) = unlimited - crawl every reachable page on the site
    const rawVal = document.getElementById('maxPagesInput').value;
    const parsed = parseInt(rawVal, 10);
    const maxPages = (Number.isFinite(parsed) && parsed > 0) ? parsed : 0;

    const discoverBtn = document.getElementById('discoverBtn');
    const crawlValidateBtn = document.getElementById('crawlValidateBtn');
    const dcCancelBtn = document.getElementById('dcCancelBtn');
    discoverBtn.disabled = true;
    crawlValidateBtn.disabled = true;
    discoverBtn.innerText = alsoValidate ? 'Working...' : 'Crawling...';
    if (alsoValidate) crawlValidateBtn.innerText = 'Working...';
    if (dcCancelBtn) {
        dcCancelBtn.classList.remove('hidden');
        dcCancelBtn.disabled = false;
        dcCancelBtn.innerText = 'Cancel';
    }

    document.getElementById('dcLogBox').classList.remove('hidden');
    document.getElementById('dcLogBox').innerHTML = '';
    document.getElementById('dcProgressSection').classList.remove('hidden');
    document.getElementById('dcUrlListBody').innerHTML = '<tr><td colspan="2" class="empty-msg">Crawling...</td></tr>';
    document.getElementById('dcUrlCount').innerText = '';
    document.getElementById('downloadUrlsBtn').classList.add('hidden');
    document.getElementById('dcDownloadBtn').classList.add('hidden');
    document.getElementById('validateDiscoveredBtn').classList.add('hidden');
    dcCachedResults = [];
    renderDcTable();

    const endpoint = alsoValidate ? '/api/tag-validator/crawl-and-validate' : '/api/tag-validator/crawl';
    await fetch(endpoint, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, maxPages, mode: dcMode }),
    });

    pollDomainStatus(alsoValidate);
}

function pollDomainStatus(expectValidation) {
    const logBox = document.getElementById('dcLogBox');
    if (dcPollHandle) clearInterval(dcPollHandle);

    let urlsLoaded = false;

    dcPollHandle = setInterval(async () => {
        const r = await fetch('/api/tag-validator/status');
        const d = await r.json();
        document.body.classList.toggle('audit-running', d.running);
        const logs = (d.logs || []).filter(l => !l.includes('DeprecationWarning') && !l.includes('Pyarrow') && l.trim());
        logBox.innerHTML = colorizeLogLines(logs);
        logBox.scrollTop = logBox.scrollHeight;

        const last = [...logs].reverse().find(l => l.match(/\[\d+\/\d+\]/));
        if (last) {
            const m = last.match(/\[(\d+)\/(\d+)\]/);
            if (m) {
                const c = +m[1], t = +m[2], pct = Math.round(c / t * 100);
                document.getElementById('dcProgressLabel').innerText = c + '/' + t;
                document.getElementById('dcProgressBar').style.width = pct + '%';
            }
        }

        // Load discovered URLs as soon as the crawl phase finishes (even mid-pipeline)
        if (!urlsLoaded && logs.some(l => l.includes('Crawl finished'))) {
            urlsLoaded = true;
            await loadDcCrawledUrls();
        }

        if (!d.running) {
            document.body.classList.remove('audit-running');
            clearInterval(dcPollHandle);
            dcPollHandle = null;

            const discoverBtn = document.getElementById('discoverBtn');
            const crawlValidateBtn = document.getElementById('crawlValidateBtn');
            const validateDiscoveredBtn = document.getElementById('validateDiscoveredBtn');
            const dcCancelBtn = document.getElementById('dcCancelBtn');
            discoverBtn.disabled = false;
            crawlValidateBtn.disabled = false;
            discoverBtn.innerText = d.cancelled ? 'Cancelled — Discover URLs' : 'Discover URLs';
            crawlValidateBtn.innerText = d.cancelled ? 'Cancelled — Crawl + Validate' : 'Crawl + Validate';
            validateDiscoveredBtn.disabled = false;
            validateDiscoveredBtn.innerText = 'Validate These URLs';
            if (dcCancelBtn) dcCancelBtn.classList.add('hidden');

            if (!urlsLoaded) await loadDcCrawledUrls();
            await loadDcResults();
        }
    }, 800);
}

async function loadDcCrawledUrls() {
    const r = await fetch('/api/tag-validator/crawled-urls');
    const d = await r.json();
    const urls = d.urls || [];
    const body = document.getElementById('dcUrlListBody');
    const countEl = document.getElementById('dcUrlCount');
    countEl.innerText = urls.length ? `· ${urls.length} pages` : '';
    if (!urls.length) {
        body.innerHTML = '<tr><td colspan="2" class="empty-msg">No URLs discovered. Check the URL or try again.</td></tr>';
        return;
    }
    body.innerHTML = urls.map((u, i) =>
        `<tr><td>${i + 1}</td><td class="url-col" title="${escapeHtml(u)}"><a href="${escapeHtml(u)}" style="color:var(--c-purple); text-decoration:none;">${escapeHtml(u)}</a></td></tr>`
    ).join('');
    document.getElementById('downloadUrlsBtn').classList.remove('hidden');
    document.getElementById('validateDiscoveredBtn').classList.remove('hidden');
}

async function loadDcResults() {
    const r = await fetch('/api/tag-validator/results');
    const d = await r.json();
    if (!d.results || !d.results.length) { renderDcTable(); return; }
    dcCachedResults = d.results;
    document.getElementById('dcDownloadBtn').classList.remove('hidden');
    if (dcMode === 'clicks') {
        try {
            const cr = await (await fetch('/api/tag-validator/click-results')).json();
            dcClickResults = cr.results || [];
        } catch { dcClickResults = []; }
    }
    renderDcTable();
}

function renderDcTable() {
    const head = document.getElementById('dcTableHead');
    const body = document.getElementById('dcResultsBody');
    const statsBar = document.getElementById('dcStatsBar');
    if (!head) return;

    if (dcMode === 'clicks') {
        head.innerHTML = `
            <tr>
                <th>#</th><th>URL</th>
                <th style="text-align:center">Elements</th>
                <th class="h-teal" style="text-align:center">With Tracking</th>
                <th style="text-align:center; color:var(--c-bad)">No Tracking</th>
                <th>Details</th>
            </tr>`;
    } else if (dcMode === 'tealium') {
        head.innerHTML = `
            <tr>
                <th rowspan="2">#</th><th rowspan="2">URL</th>
                <th colspan="4" class="h-teal" style="text-align:center;">TEALIUM ANALYTICS</th>
                <th colspan="3" class="h-adobe" style="text-align:center;">ADOBE ANALYTICS</th>
            </tr>
            <tr>
                <th class="h-teal">Loaded</th><th class="h-teal">Account</th><th class="h-teal">Profile</th><th class="h-teal">Env</th>
                <th class="h-adobe">Loaded</th><th class="h-adobe">Report Suite</th><th class="h-adobe">Page View</th>
            </tr>`;
    } else {
        head.innerHTML = `
            <tr>
                <th rowspan="2">#</th><th rowspan="2">URL</th>
                <th colspan="2" class="h-adobe" style="text-align:center; background:rgba(255,255,255,0.05)">GTM</th>
                <th colspan="3" class="h-adobe" style="text-align:center; background: var(--t-info); color: var(--c-info);">GA4</th>
            </tr>
            <tr>
                <th style="background:var(--surface-3)">Loaded</th><th style="background:var(--surface-3)">GTM ID</th>
                <th class="h-adobe" style="background: var(--t-info); color: var(--c-info);">Fired</th>
                <th class="h-adobe" style="background: var(--t-info); color: var(--c-info);">Measurement ID</th>
                <th class="h-adobe" style="background: var(--t-info); color: var(--c-info);">Page View</th>
            </tr>`;
    }

    if (!dcCachedResults.length) {
        const ec = dcMode === 'clicks' ? 6 : (dcMode === 'tealium' ? 9 : 7);
        body.innerHTML = `<tr><td colspan="${ec}" class="empty-msg">Run Crawl + Validate to see tag audit per page</td></tr>`;
        statsBar.classList.add('hidden');
        return;
    }

    let st = { teal: 0, adobe: 0, ga4: 0, compliant: 0, violations: 0 };
    dcCachedResults.forEach(r => {
        if (r.Tealium_Loaded === 'PASS') st.teal++;
        if (r.Adobe_Loaded === 'PASS') st.adobe++;
        if (r.GA4_Fired === 'PASS') st.ga4++;
        if (r.Compliance === 'PASS') st.compliant++;
        if (r.Compliance === 'FAIL') st.violations++;
    });

    statsBar.classList.remove('hidden');
    if (dcMode === 'clicks') {
        const totalEl = dcCachedResults.reduce((a, r) => a + (Number(r.Total_Elements) || 0), 0);
        const tracked = dcCachedResults.reduce((a, r) => a + (Number(r.With_Tracking) || 0), 0);
        const untracked = dcCachedResults.reduce((a, r) => a + (Number(r.Without_Tracking) || 0), 0);
        statsBar.innerHTML = `
            <div class="stat"><div class="stat-dot" style="background:var(--c-info);color:var(--c-info);"></div><div><div class="stat-val" style="color:var(--c-info-2);">${totalEl}</div><div class="stat-lbl">Total Elements Clicked</div></div></div>
            <div class="stat"><div class="stat-dot dot-teal"></div><div><div class="stat-val val-teal">${tracked}</div><div class="stat-lbl">With Analytics Tracking</div></div></div>
            <div class="stat"><div class="stat-dot" style="background:var(--c-bad);color:var(--c-bad);"></div><div><div class="stat-val" style="color:var(--c-bad);">${untracked}</div><div class="stat-lbl">No Tracking Detected</div></div></div>
        `;
    } else if (dcMode === 'tealium') {
        statsBar.innerHTML = `<div class="stat"><div class="stat-dot dot-teal"></div><div><div class="stat-val val-teal">${st.teal}/${dcCachedResults.length}</div><div class="stat-lbl">Tealium Detected</div></div></div>`;
    } else {
        statsBar.innerHTML = `
            <div class="stat"><div class="stat-dot dot-adobe"></div><div><div class="stat-val val-adobe">${st.adobe}/${dcCachedResults.length}</div><div class="stat-lbl">Adobe Detected</div></div></div>
            <div class="stat"><div class="stat-dot" style="background:var(--c-info); color:var(--c-info);"></div><div><div class="stat-val" style="color:var(--c-info-2);">${st.ga4}/${dcCachedResults.length}</div><div class="stat-lbl">GA4 Detected</div></div></div>`;
    }

    if (dcMode === 'clicks') {
        const clickByUrl = {};
        dcClickResults.forEach(x => { clickByUrl[x.URL] = x; });
        let html = '';
        dcCachedResults.forEach((r, i) => {
            const rich = clickByUrl[r.URL];
            const elems = (rich && rich.elements) || [];
            const detailId = 'dc-click-detail-' + i;
            html += `<tr style="cursor:pointer" onclick="document.getElementById('${detailId}').classList.toggle('hidden')">
                <td>${i + 1}</td>
                <td class="url-col" title="${r.URL}">${r.URL}</td>
                <td style="text-align:center"><span class="badge b-count">${r.Total_Elements || 0}</span></td>
                <td style="text-align:center"><span class="badge b-pass">${r.With_Tracking || 0}</span></td>
                <td style="text-align:center">${(r.Without_Tracking || 0) > 0 ? '<span class="badge b-fail">' + r.Without_Tracking + '</span>' : '<span style="color:var(--muted)">0</span>'}</td>
                <td><span style="font-size:0.7rem;color:var(--accent);cursor:pointer">▶ Expand</span></td>
            </tr>`;
            html += `<tr id="${detailId}" class="hidden"><td colspan="6" style="padding:0;background:var(--inset-bg)">`;
            if (!elems.length) {
                html += '<div style="padding:16px;color:var(--muted)">No clickable elements found</div>';
            } else {
                html += '<table style="width:100%;margin:0;font-size:0.75rem"><thead><tr>' +
                    '<th style="width:30px">#</th><th>Element</th><th>Text</th><th>Type</th><th>Zone</th>' +
                    '<th class="h-teal">GA4 Event 1</th><th class="h-teal">GA4 Event 2</th><th class="h-teal">GA4 Event 3</th>' +
                    '<th class="h-adobe">Adobe / Other Calls</th><th>Network</th></tr></thead><tbody>';
                elems.forEach((el, j) => {
                    const ga4_1 = formatSingleGa4Event(el.ga4_events[0]);
                    const ga4_2 = formatSingleGa4Event(el.ga4_events[1]);
                    let ga4_3 = formatSingleGa4Event(el.ga4_events[2]);
                    if (el.ga4_events.length > 3) {
                        for (let k = 3; k < el.ga4_events.length; k++) {
                            ga4_3 += '<hr style="border-color:var(--border);margin:8px 0">' + formatSingleGa4Event(el.ga4_events[k]);
                        }
                    }
                        
                    const aaHtml = el.adobe_calls.length
                        ? el.adobe_calls.map(c => {
                            const displayName = c.link_name || c.events || c.link_type || 'adobe_call';
                            const rsid = c.report_suite ? '<span class="badge" style="background:var(--t-orange);color:var(--c-orange);border:1px solid var(--e-orange);margin-bottom:4px;display:inline-block;font-family:monospace;font-size:0.6rem;">' + c.report_suite + '</span><br>' : '';
                            let s = '<div style="margin-bottom:6px;">' + rsid + '<b style="color:var(--c-orange)">' + displayName + '</b> <span style="color:var(--muted);font-size:0.7rem">(' + c.link_type + ')</span>';
                            
                            const items = [];
                            if (c.events) items.push({ k: 'events', v: c.events });
                            Object.keys(c.evars || {}).forEach(k => { items.push({ k, v: c.evars[k] }); });
                            Object.keys(c.props || {}).forEach(k => { items.push({ k, v: c.props[k] }); });
                            
                            if (items.length) {
                                s += `<div class="event-params">` + 
                                     items.map(item => `<div style="margin-bottom:2px; word-break:break-all;"><span style="color:var(--muted)">${item.k}</span>: <span style="color:var(--c-orange)">${item.v}</span></div>`).join('') + 
                                     `</div>`;
                            }
                            s += '</div>';
                            return s;
                        }).join('<hr style="border-color:var(--border);margin:8px 0">')
                        : (el.adobe_websdk && el.adobe_websdk.length
                            ? el.adobe_websdk.map(w => '<b style="color:var(--c-orange)">' + w.event_type + '</b>').join('<br>')
                            : '');

                    const otherHtml = el.other_analytics && el.other_analytics.length
                        ? el.other_analytics.map(o => `<div style="margin-bottom:4px;"><span class="badge" style="background:var(--t-purple);color:var(--c-purple);border:1px solid var(--e-purple);">${o.vendor}</span></div>`).join('')
                        : '';
                    
                    let combinedAdobeOther = aaHtml;
                    if (otherHtml) {
                        if (combinedAdobeOther) combinedAdobeOther += '<hr style="border-color:var(--border);margin:8px 0">';
                        combinedAdobeOther += otherHtml;
                    }
                    if (!combinedAdobeOther) combinedAdobeOther = '<span style="color:var(--muted)">--</span>';

                    const elLabel = el.element.id ? '#' + el.element.id : el.element.selector.substring(0, 40);
                    const trackIcon = el.skipped ? '⏭️' : (el.has_tracking ? '✅' : '⚠️');
                    const zone = el.element.zone || 'body';
                    // Trust indicators: a result is only meaningful if the
                    // click actually reached the intended element.
                    const badges = [];
                    if (!el.skipped && el.click_verified === false) {
                        badges.push('<span class="badge" title="The click could not be confirmed on this exact element — treat its events with caution" style="background:var(--t-orange);color:var(--c-orange);border:1px solid var(--e-orange);font-size:0.55rem">unverified</span>');
                    }
                    if (el.element.is_download) {
                        badges.push('<span class="badge" title="Download link" style="background:rgba(56,189,248,0.12);color:var(--c-info-2);border:1px solid rgba(56,189,248,0.35);font-size:0.55rem">download</span>');
                    }
                    if (el.blocked_by) {
                        badges.push(`<span class="badge" title="Covered by ${escapeHtml(el.blocked_by)} — the audit clicked through it" style="background:rgba(148,163,184,0.12);color:var(--text-2);border:1px solid rgba(148,163,184,0.35);font-size:0.55rem">overlaid</span>`);
                    }
                    const badgeHtml = badges.length ? '<div style="margin-top:3px">' + badges.join(' ') + '</div>' : '';
                    const netReqs = el.network_requests || [];
                    const netCount = el.network_count || netReqs.length;
                    const netId = detailId + '-net-' + j;
                    const escUrl = (u) => String(u).replace(/&/g, '&amp;').replace(/</g, '&lt;');
                    const netCell = netCount
                        ? `<span class="badge b-count" style="cursor:pointer" onclick="event.stopPropagation();document.getElementById('${netId}').classList.toggle('hidden')">${netCount} req ▾</span>` +
                          `<div id="${netId}" class="hidden" style="max-height:220px;overflow:auto;margin-top:6px;text-align:left">` +
                          netReqs.map(nr => `<div class="mono" style="font-size:0.62rem;word-break:break-all;color:var(--muted);margin-bottom:3px">${escUrl(nr.url)}</div>`).join('') +
                          `</div>`
                        : (el.skipped ? '<span style="color:var(--muted)">skipped</span>' : '<span style="color:var(--muted)">0</span>');
                    html += `<tr>
                        <td>${j + 1}</td>
                        <td title="${el.element.selector}">${trackIcon} <span class="mono">${elLabel}</span>${badgeHtml}</td>
                        <td style="max-width:150px;overflow:hidden;text-overflow:ellipsis">${el.element.text || '--'}</td>
                        <td>${formatElementType(el.element.tag)}</td>
                        <td style="text-align:center"><span class="badge" style="font-size:0.6rem">${zone}</span></td>
                        <td style="white-space:normal;max-width:240px"><div class="event-cell-container">${ga4_1}</div></td>
                        <td style="white-space:normal;max-width:240px"><div class="event-cell-container">${ga4_2}</div></td>
                        <td style="white-space:normal;max-width:240px"><div class="event-cell-container">${ga4_3}</div></td>
                        <td style="white-space:normal;max-width:240px"><div class="event-cell-container">${combinedAdobeOther}</div></td>
                        <td style="white-space:normal;max-width:280px">${netCell}</td>
                    </tr>`;
                });
                html += '</tbody></table>';
            }
            html += '</td></tr>';
        });
        body.innerHTML = html;
        return;
    }

    body.innerHTML = dcCachedResults.map((r, i) => {
        if (dcMode === 'tealium') {
            return `<tr>
                <td>${i + 1}</td>
                <td class="url-col" title="${r.URL}">${r.URL}</td>
                <td>${B(r.Tealium_Loaded)}</td>
                <td>${ID(r.Tealium_Account)}</td>
                <td>${ID(r.Tealium_Profile)}</td>
                <td>${ID(r.Tealium_Env)}</td>
                <td>${B(r.Adobe_Loaded)}</td>
                <td>${ID(r.Adobe_ReportSuite)}</td>
                <td>${B(r.Adobe_PageView)}</td>
            </tr>`;
        } else {
            return `<tr>
                <td>${i + 1}</td>
                <td class="url-col" title="${r.URL}">${r.URL}</td>
                <td>${B(r.GTM_Loaded)}</td>
                <td>${ID(r.GTM_ID)}</td>
                <td>${B(r.GA4_Fired)}</td>
                <td>${ID(r.GA4_Measurement_ID)}</td>
                <td>${B(r.GA4_PageView)}</td>
            </tr>`;
        }
    }).join('');
}



// =============== AI ASSISTANT CHAT ===============
let chatHistory = [];          // [{role:'user'|'assistant', content}]
let chatSending = false;
let chatAbort = null;          // AbortController for the in-flight request

function setChatSendMode(sending) {
    const btn = document.getElementById('chatSend');
    if (sending) {
        btn.classList.add('stop');
        btn.innerHTML = '■';
        btn.title = 'Cancel response';
    } else {
        btn.classList.remove('stop');
        btn.innerHTML = '➤';
        btn.title = 'Send';
    }
}

function cancelChat() {
    if (chatAbort) chatAbort.abort();
}

// Minimal, safe markdown -> HTML (escapes everything first).
function renderMarkdown(src) {
    let s = String(src);
    s = s.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
    s = s.replace(/```[\w]*\n?([\s\S]*?)```/g, (m, c) => `<pre>${c.replace(/\n$/, '')}</pre>`);
    s = s.replace(/(^\|.+\|[ \t]*\n\|[-:\s|]+\|[ \t]*\n(?:\|.*\|[ \t]*\n?)*)/gm, block => {
        const lines = block.trim().split('\n');
        const cells = l => l.replace(/^\||\|$/g, '').split('|').map(x => x.trim());
        const head = cells(lines[0]);
        const rows = lines.slice(2).map(cells);
        let h = '<table><thead><tr>' + head.map(c => `<th>${c}</th>`).join('') + '</tr></thead><tbody>';
        h += rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('');
        return h + '</tbody></table>';
    });
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]+)\)/g,
        '<a href="$2" rel="noopener">$1</a>');
    // auto-linkify bare assistant download paths
    s = s.replace(/(?<!["'>= ])(\/api\/ai\/download\/(?:results|crawled))/g,
        '<a href="$1" rel="noopener">Download file</a>');
    s = s.replace(/(?:^|\n)((?:[-*] .+(?:\n|$))+)/g, (m, blk) => {
        const items = blk.trim().split('\n').map(l => `<li>${l.replace(/^[-*]\s+/, '')}</li>`).join('');
        return `<ul style="margin:6px 0 6px 18px">${items}</ul>`;
    });
    s = s.replace(/\n/g, '<br>');
    s = s.replace(/<br>(\s*<(?:table|thead|tbody|tr|ul|pre|\/))/g, '$1');
    return s;
}

function addChatMessage(role, content, type) {
    const body = document.getElementById('chatBody');
    const div = document.createElement('div');
    div.className = 'chat-msg ' + (type || role);
    div.innerHTML = role === 'bot' ? renderMarkdown(content) : escapeHtml(content);
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
    return div;
}

function showChatTyping() {
    const panel = document.getElementById('chatPanel');
    if (panel) panel.classList.add('agent-talking');
    const body = document.getElementById('chatBody');
    const div = document.createElement('div');
    div.className = 'chat-typing';
    div.id = 'chatTyping';
    div.innerHTML = '<span></span><span></span><span></span>';
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
}
function hideChatTyping() {
    const panel = document.getElementById('chatPanel');
    if (panel) panel.classList.remove('agent-talking');
    const t = document.getElementById('chatTyping');
    if (t) t.remove();
}

async function sendChatMessage(text) {
    text = (text || '').trim();
    if (!text || chatSending) return;
    chatSending = true;
    setChatSendMode(true);

    addChatMessage('user', text);
    chatHistory.push({ role: 'user', content: text });
    const input = document.getElementById('chatInput');
    input.value = '';
    input.style.height = '42px';
    showChatTyping();

    chatAbort = new AbortController();
    try {
        const r = await fetch('/api/ai/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ messages: chatHistory }),
            signal: chatAbort.signal,
        });
        const d = await r.json();
        hideChatTyping();
        if (!r.ok) {
            addChatMessage('bot', '⚠ ' + (d.error || 'Something went wrong.'), 'note');
        } else {
            addChatMessage('bot', d.reply);
            chatHistory.push({ role: 'assistant', content: d.reply });
        }
    } catch (e) {
        hideChatTyping();
        if (e.name === 'AbortError') {
            addChatMessage('bot', 'Okay, cancelled that one. 👍 Ask me whenever you\u2019re ready.', 'note');
        } else {
            addChatMessage('bot', '⚠ Network error: ' + e.message, 'note');
        }
    } finally {
        chatSending = false;
        chatAbort = null;
        setChatSendMode(false);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const fab = document.getElementById('chatFab');
    const panel = document.getElementById('chatPanel');
    const input = document.getElementById('chatInput');
    if (!fab) return;

    async function refreshGroqState() {
        try {
            const d = await (await fetch('/api/ai/config')).json();
            const el = document.getElementById('groqState');
            if (!d.configured) {
                el.innerHTML = '⚠ No API key yet — add any AI key below.';
            } else {
                const parts = [];
                if (d.groq) parts.push('Groq ✅');
                if (d.gemini) parts.push('Gemini ✅');
                if (d.openrouter) parts.push('OpenRouter ✅');
                el.innerHTML = 'Assistant ready — ' + parts.join(' · ');
            }
        } catch { /* ignore */ }
    }

    function greet() {
        if (document.getElementById('chatBody').children.length) return;
        addChatMessage('bot', "Hey! I'm **Tagly** 👋 your web-analytics assistant. I can:\n\n"
            + "- Audit any URL — **GTM, GA4, Adobe, Tealium**, IDs, report suites, page-view tags\n"
            + "- **Crawl a whole domain** into an Excel of page URLs\n"
            + "- Explain **how to fix** tagging issues with examples\n\n"
            + "What are we looking at today?");
    }

    const bubble = document.getElementById('chatBubble');
    function hideBubble() { if (bubble) bubble.classList.add('hidden'); }

    function openChat() {
        panel.classList.add('open');
        fab.classList.add('hidden');
        hideBubble();
        greet();
        refreshGroqState();
        input.focus();
    }
    fab.onclick = openChat;
    if (bubble) {
        bubble.onclick = openChat;
        const bc = document.getElementById('chatBubbleClose');
        if (bc) bc.onclick = (e) => { e.stopPropagation(); hideBubble(); };
    }
    document.getElementById('chatCloseBtn').onclick = () => {
        panel.classList.remove('open');
        fab.classList.remove('hidden');
    };
    document.getElementById('chatClearBtn').onclick = () => {
        chatHistory = [];
        document.getElementById('chatBody').innerHTML = '';
        greet();
    };
    document.getElementById('chatSettingsBtn').onclick = () => {
        document.getElementById('chatSettings').classList.toggle('open');
        refreshGroqState();
    };

    document.getElementById('saveGroqBtn').onclick = async () => {
        const apiKey = document.getElementById('groqKeyInput').value.trim();
        const geminiKey = document.getElementById('geminiKeyInput').value.trim();
        const openrouterKey = document.getElementById('openrouterKeyInput').value.trim();
        if (!apiKey && !geminiKey && !openrouterKey) return;
        const body = {};
        if (apiKey) body.apiKey = apiKey;
        if (geminiKey) body.geminiKey = geminiKey;
        if (openrouterKey) body.openrouterKey = openrouterKey;
        const r = await fetch('/api/ai/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        document.getElementById('groqKeyInput').value = '';
        document.getElementById('geminiKeyInput').value = '';
        document.getElementById('openrouterKeyInput').value = '';
        if (r.ok) document.getElementById('chatSettings').classList.remove('open');
        refreshGroqState();
    };
    document.getElementById('clearGroqBtn').onclick = async () => {
        await fetch('/api/ai/config', { method: 'DELETE' });
        document.getElementById('groqKeyInput').value = '';
        document.getElementById('geminiKeyInput').value = '';
        document.getElementById('openrouterKeyInput').value = '';
        refreshGroqState();
    };

    document.getElementById('chatSend').onclick = () => {
        if (chatSending) cancelChat();
        else sendChatMessage(input.value);
    };
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (!chatSending) sendChatMessage(input.value);
        }
    });
    input.addEventListener('input', () => {
        input.style.height = '42px';
        input.style.height = Math.min(input.scrollHeight, 100) + 'px';
    });
});

// ===================== SDR QA =====================
// Automates the manual SDR pass: upload the sheet, pick which GA4 property is
// the source of truth, then every row is clicked for real and compared.

let sdrSheets = [];
let sdrPollTimer = null;

function sdrEl(id) { return document.getElementById(id); }

function sdrSetLog(lines) {
    const box = sdrEl('sdrLog');
    if (!box) return;
    box.classList.remove('hidden');
    box.innerHTML = (lines || []).map(l => escapeHtml(l)).join('<br>');
    box.scrollTop = box.scrollHeight;
}

function sdrFillSheets(sheets) {
    sdrSheets = sheets || [];
    const sel = sdrEl('sdrSheet');
    if (!sel) return;
    // Only sheets that actually contain test cases are worth offering.
    const usable = sdrSheets.filter(s => (s.testable_rows || 0) > 0);
    if (!usable.length) {
        sel.innerHTML = '<option value="">No sheet with testable rows found</option>';
        sel.disabled = true;
        return;
    }
    usable.sort((a, b) => (b.testable_rows || 0) - (a.testable_rows || 0));
    sel.innerHTML = usable.map(s =>
        '<option value="' + escapeHtml(s.name) + '">' + escapeHtml(s.name) +
        ' — ' + s.testable_rows + ' rows, ' + (s.page_urls || []).length + ' pages</option>'
    ).join('');
    sel.disabled = false;
    sdrOnSheetChange();
}

function sdrOnSheetChange() {
    const sel = sdrEl('sdrSheet');
    const info = sdrEl('sdrSheetInfo');
    const qa = sdrEl('sdrQaColumn');
    const meta = sdrSheets.find(s => s.name === sel.value);
    if (!meta) return;
    const urls = meta.page_urls || [];
    const real = urls.filter(u => /^https?:/i.test(u));
    if (info) {
        let html = meta.testable_rows + ' testable rows across ' + real.length + ' real page URL(s)';
        if (urls.length > real.length) {
            html += ' <span style="color:var(--c-warn)">· ' + (urls.length - real.length) +
                    ' placeholder URL(s) will be marked "Not Tested"</span>';
        }
        if (real.length) {
            html += '<br><span style="opacity:.75">' +
                    escapeHtml(real.slice(0, 3).join(' · ')) +
                    (real.length > 3 ? ' …' : '') + '</span>';
        }
        info.innerHTML = html;
    }
    if (qa) {
        const cols = meta.qa_columns || [];
        qa.innerHTML = '<option value="">Auto (prefers Live/Prod)</option>' +
            cols.map(c => '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + '</option>').join('');
        qa.disabled = false;
        const live = cols.find(c => /live|prod/i.test(c));
        if (live) qa.value = live;
    }
    // Point the detect box at this sheet's first real page. It is only left
    // alone when it already holds one of THIS sheet's URLs — otherwise it is
    // a leftover from a different SDR and would scan the wrong site.
    const durl = sdrEl('sdrDetectUrl');
    if (durl && real.length) {
        const cur = (durl.value || '').trim().replace(/\/$/, '');
        const belongs = real.some(u => u.replace(/\/$/, '') === cur);
        if (!belongs) durl.value = real[0];
    }
    sdrUpdateRunState();
}

function sdrUpdateRunState() {
    const btn = sdrEl('sdrRunBtn');
    if (!btn) return;
    const sheetSel = sdrEl('sdrSheet');
    const idSel = sdrEl('sdrGa4Id');
    const sheetOk = sheetSel && !sheetSel.disabled && sheetSel.value;
    const ga4Ok = idSel && idSel.value;
    btn.disabled = !(sheetOk && ga4Ok);
    btn.title = ga4Ok ? '' : 'Choose which GA4 property to validate against first';
}

// Everything downstream of the file belongs to THAT file: its sheets, its
// page URLs, the GA4 property those pages tag to, and any results. A team
// running SDRs for several sites must never see one client's sheets or a
// previous site's GA4 id sitting under a newly chosen file.
function sdrClearDownstream() {
    const sheet = sdrEl('sdrSheet');
    if (sheet) { sheet.innerHTML = '<option value="">Reading sheets…</option>'; sheet.disabled = true; }
    const info = sdrEl('sdrSheetInfo');
    if (info) info.innerHTML = '';
    const qa = sdrEl('sdrQaColumn');
    if (qa) { qa.innerHTML = '<option value="">Auto (prefers Live/Prod)</option>'; qa.disabled = true; }
    const idSel = sdrEl('sdrGa4Id');
    if (idSel) { idSel.innerHTML = '<option value="">Detect IDs, then choose one…</option>'; idSel.disabled = true; }
    const ga4info = sdrEl('sdrGa4Info');
    if (ga4info) ga4info.innerHTML = 'Sites often tag to more than one GA4 property. Pick the one this SDR is written for — an event that fires only on the other property will be reported as a failure.';
    const durl = sdrEl('sdrDetectUrl');
    if (durl) durl.value = '';
    const sum = sdrEl('sdrSummary');
    if (sum) { sum.classList.add('hidden'); sum.innerHTML = ''; }
    const card = sdrEl('sdrResultsCard');
    if (card) card.classList.add('hidden');
    const body = sdrEl('sdrBody');
    if (body) body.innerHTML = '';
    ['sdrDownloadFilled', 'sdrDownloadReport', 'sdrResumeBtn'].forEach(id => {
        const el = sdrEl(id);
        if (el) el.classList.add('hidden');
    });
    sdrSheets = [];
    sdrUpdateRunState();
}

async function sdrUpload() {
    const f = sdrEl('sdrFile');
    const state = sdrEl('sdrFileState');
    if (!f || !f.files.length) { alert('Choose an SDR .xlsx file first'); return; }
    sdrClearDownstream();
    const fd = new FormData();
    fd.append('file', f.files[0]);
    state.textContent = 'Uploading and reading sheets…';
    try {
        const r = await fetch('/api/sdr/upload', { method: 'POST', body: fd });
        const d = await r.json();
        if (!r.ok || d.error) { state.textContent = 'Upload failed: ' + (d.error || r.status); return; }
        state.innerHTML = '✅ Loaded <b>' + escapeHtml(d.originalName || 'SDR') + '</b> — ' +
                          (d.sheets || []).length + ' sheet(s) found';
        sdrFillSheets(d.sheets);
    } catch (e) {
        state.textContent = 'Upload failed: ' + e.message;
    }
}

async function sdrDetectGa4() {
    const btn = sdrEl('sdrDetectBtn');
    const info = sdrEl('sdrGa4Info');
    const sel = sdrEl('sdrGa4Id');
    let url = (sdrEl('sdrDetectUrl').value || '').trim();
    if (!url) {
        const meta = sdrSheets.find(s => s.name === sdrEl('sdrSheet').value);
        const real = ((meta && meta.page_urls) || []).filter(u => /^https?:/i.test(u));
        url = real[0] || '';
    }
    if (!url) { alert('Enter a page URL to scan for GA4 IDs'); return; }
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = 'Scanning…';
    info.textContent = 'Loading ' + url + ' and watching which GA4 properties receive hits…';
    try {
        const r = await fetch('/api/sdr/detect-ga4', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url })
        });
        const d = await r.json();
        const ids = d.ids || [];
        if (!ids.length) {
            sel.innerHTML = '<option value="">No GA4 property detected</option>';
            sel.disabled = true;
            info.innerHTML = '<span style="color:var(--c-warn)">No GA4 hits seen on that page.</span> ' +
                (d.error ? escapeHtml(String(d.error).slice(0, 120))
                         : 'Check the URL, or consent may be blocking tags.');
        } else {
            sel.innerHTML = '<option value="">— choose a property —</option>' +
                ids.map(i => '<option value="' + escapeHtml(i) + '">' + escapeHtml(i) + '</option>').join('');
            sel.disabled = false;
            info.innerHTML = 'Found <b>' + ids.length + '</b> GA4 propert' +
                (ids.length === 1 ? 'y' : 'ies') + ': ' +
                ids.map(i => '<code>' + escapeHtml(i) + '</code>').join(', ') +
                (d.gtm && d.gtm.length ? ' · GTM ' + d.gtm.map(g => escapeHtml(g)).join(', ') : '') +
                '<br>Pick the one this SDR was written for.';
            if (ids.length === 1) sel.value = ids[0];
        }
    } catch (e) {
        info.textContent = 'Detection failed: ' + e.message;
    }
    btn.disabled = false;
    btn.textContent = oldText;
    sdrUpdateRunState();
}

async function sdrRun(resume) {
    const sheet = sdrEl('sdrSheet').value;
    const ga4Id = sdrEl('sdrGa4Id').value;
    const qaColumn = sdrEl('sdrQaColumn').value;
    const startUrl = (sdrEl('sdrDetectUrl').value || '').trim();
    if (!ga4Id) { alert('Choose which GA4 measurement ID to validate against'); return; }

    sdrEl('sdrRunBtn').disabled = true;
    sdrEl('sdrResumeBtn').classList.add('hidden');
    sdrEl('sdrCancelBtn').classList.remove('hidden');
    sdrEl('sdrDownloadFilled').classList.add('hidden');
    sdrEl('sdrDownloadReport').classList.add('hidden');
    sdrSetLog(['Starting SDR QA…']);

    try {
        const r = await fetch('/api/sdr/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sheet: sheet, ga4Id: ga4Id, qaColumn: qaColumn,
                                   startUrl: startUrl, resume: !!resume })
        });
        const d = await r.json();
        if (!r.ok || d.error) {
            sdrSetLog(['Could not start: ' + (d.error || r.status)]);
            sdrEl('sdrRunBtn').disabled = false;
            sdrEl('sdrCancelBtn').classList.add('hidden');
            return;
        }
    } catch (e) {
        sdrSetLog(['Could not start: ' + e.message]);
        sdrEl('sdrRunBtn').disabled = false;
        return;
    }
    if (sdrPollTimer) clearInterval(sdrPollTimer);
    sdrPollTimer = setInterval(sdrPoll, 1500);
}

let sdrPollTick = 0;

async function sdrPoll() {
    try {
        const d = await (await fetch('/api/tag-validator/status')).json();
        sdrSetLog(d.logs || []);
        // Results are checkpointed after every page, so the table can fill in
        // as the run proceeds instead of staying blank for half an hour.
        if (++sdrPollTick % 6 === 0) await sdrLoadResults();
        if (!d.running) {
            clearInterval(sdrPollTimer);
            sdrPollTimer = null;
            sdrPollTick = 0;
            sdrEl('sdrCancelBtn').classList.add('hidden');
            sdrUpdateRunState();
            await sdrLoadResults();
            await sdrCheckResumable();
        }
    } catch (e) { /* keep polling */ }
}

// If a previous run stopped part-way, offer to continue it rather than
// silently starting over — these runs are long and re-doing them is costly.
async function sdrCheckResumable() {
    try {
        const d = await (await fetch('/api/sdr/results')).json();
        const btn = sdrEl('sdrResumeBtn');
        if (!btn) return;
        const incomplete = d.partial && d.completed && d.total_cases
                           && d.completed < d.total_cases;
        if (incomplete) {
            btn.textContent = `▶ Resume previous run (${d.completed}/${d.total_cases} done)`;
            btn.classList.remove('hidden');
        } else {
            btn.classList.add('hidden');
        }
    } catch (e) { /* ignore */ }
}

async function sdrLoadResults() {
    let d;
    try {
        d = await (await fetch('/api/sdr/results')).json();
    } catch (e) { return; }
    const rows = d.results || [];
    if (!rows.length) return;

    sdrEl('sdrDownloadFilled').classList.remove('hidden');
    sdrEl('sdrDownloadReport').classList.remove('hidden');

    const pass = rows.filter(r => r.status === 'PASS').length;
    const fail = rows.filter(r => r.status === 'FAIL').length;
    const skip = rows.filter(r => r.status === 'SKIPPED').length;
    const tile = (label, val, color) =>
        '<div class="table-card glass" style="padding:12px 18px;min-width:120px">' +
        '<div style="font-size:0.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em">' + label + '</div>' +
        '<div style="font-size:1.5rem;font-weight:700;color:' + color + '">' + val + '</div></div>';
    const sum = sdrEl('sdrSummary');
    sum.classList.remove('hidden');
    const progressTile = (d.partial && d.total_cases && d.completed < d.total_cases)
        ? tile('In progress', d.completed + '/' + d.total_cases, 'var(--c-warn)') : '';
    sum.innerHTML = progressTile + tile('Pass', pass, 'var(--c-ok)') + tile('Fail', fail, 'var(--c-bad)') +
        tile('Not tested', skip, 'var(--c-warn)') + tile('Total', rows.length, 'var(--text)') +
        (d.ga4_id ? tile('GA4 property',
            '<span style="font-size:0.85rem;font-family:monospace">' + escapeHtml(d.ga4_id) + '</span>',
            'var(--accent)') : '');

    // A convention mismatch fails every row, which looks like hundreds of
    // separate bugs. Show the root causes above the table so the reader knows
    // how many actual fixes are involved.
    const pats = d.failure_patterns || [];
    if (pats.length) {
        sum.innerHTML += '<div class="table-card glass" style="padding:12px 18px;flex:1;min-width:280px">' +
            '<div style="font-size:0.68rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:6px">Failure patterns</div>' +
            pats.slice(0, 5).map(p =>
                '<div style="font-size:0.74rem;margin-bottom:4px">' +
                '<span class="mono" style="color:var(--c-bad)">' + escapeHtml(p.param) + '</span>' +
                ' <span style="color:var(--muted)">' + p.failed_rows + ' row(s)</span>' +
                (p.note ? '<br><span style="font-size:0.68rem;color:var(--muted);opacity:.85">' +
                          escapeHtml(p.note) + '</span>' : '') +
                '</div>').join('') +
            '</div>';
    }

    const badge = (s) => {
        const map = {
            PASS: ['var(--c-ok)', 'var(--t-ok)', 'Pass'],
            FAIL: ['var(--c-bad)', 'var(--t-bad)', 'Fail'],
            SKIPPED: ['var(--c-warn)', 'var(--t-warn)', 'Not tested']
        };
        const m = map[s] || ['var(--muted)', 'rgba(148,163,184,.12)', s];
        return '<span class="badge" style="color:' + m[0] + ';background:' + m[1] +
               ';border:1px solid ' + m[0] + '55">' + m[2] + '</span>';
    };

    sdrEl('sdrResultsCard').classList.remove('hidden');
    sdrEl('sdrBody').innerHTML = rows.map(r => {
        const shortPage = String(r.page_url || '').replace(/^https?:\/\/(www\.)?/, '');
        const reasonColor = r.status === 'FAIL' ? 'var(--c-bad)' : 'var(--muted)';
        return '<tr>' +
            '<td>' + r.excel_row + '</td>' +
            '<td class="url-col" title="' + escapeHtml(r.page_url || '') +
                '" style="max-width:170px;overflow:hidden;text-overflow:ellipsis">' + escapeHtml(shortPage) + '</td>' +
            '<td style="max-width:180px">' + escapeHtml(r.button_name || '') + '</td>' +
            '<td><span class="mono" style="color:var(--c-info)">' + escapeHtml(r.expected_event || '') + '</span></td>' +
            '<td><span class="mono" style="color:' + (r.actual_event ? 'var(--c-ok)' : 'var(--muted)') + '">' +
                escapeHtml(r.actual_event || '--') + '</span></td>' +
            '<td style="text-align:center">' + badge(r.status) + '</td>' +
            '<td style="white-space:normal;max-width:340px;font-size:0.72rem;color:' + reasonColor + '">' +
                escapeHtml(r.reason || '') + '</td>' +
            '</tr>';
    }).join('');
}

document.addEventListener('DOMContentLoaded', () => {
    const up = document.getElementById('sdrUploadBtn');
    if (up) up.addEventListener('click', sdrUpload);
    const det = document.getElementById('sdrDetectBtn');
    if (det) det.addEventListener('click', sdrDetectGa4);
    const run = document.getElementById('sdrRunBtn');
    if (run) run.addEventListener('click', () => sdrRun(false));
    const res = document.getElementById('sdrResumeBtn');
    if (res) res.addEventListener('click', () => sdrRun(true));
    const sheetSel = document.getElementById('sdrSheet');
    if (sheetSel) sheetSel.addEventListener('change', sdrOnSheetChange);
    const idSel = document.getElementById('sdrGa4Id');
    if (idSel) idSel.addEventListener('change', sdrUpdateRunState);
    const cancel = document.getElementById('sdrCancelBtn');
    if (cancel) cancel.addEventListener('click', async () => {
        await fetch('/api/tag-validator/cancel', { method: 'POST' });
    });
    // Pick up an SDR that was uploaded in an earlier session.
    // Choosing a file uploads it immediately. Requiring a second click was a
    // trap: the picker kept showing the previous SDR's sheets and page URLs,
    // so a run could be started against the wrong site without any warning.
    const fileInput = document.getElementById('sdrFile');
    if (fileInput) fileInput.addEventListener('change', () => {
        if (fileInput.files && fileInput.files.length) sdrUpload();
    });

    fetch('/api/sdr/sheets').then(r => r.json()).then(d => {
        if (d.sheets && d.sheets.length) {
            sdrFillSheets(d.sheets);
            const st = document.getElementById('sdrFileState');
            if (st) {
                st.innerHTML = 'Currently loaded: <b>' +
                    escapeHtml(d.fileName || 'previously uploaded SDR') + '</b> — ' +
                    d.sheets.length + ' sheet(s). Choose a file above to replace it.';
            }
        }
    }).catch(() => {});
    sdrCheckResumable();
    // A run may still be going from before this page was opened.
    fetch('/api/tag-validator/status').then(r => r.json()).then(d => {
        if (d.running) {
            sdrEl('sdrCancelBtn').classList.remove('hidden');
            sdrEl('sdrRunBtn').disabled = true;
            if (!sdrPollTimer) sdrPollTimer = setInterval(sdrPoll, 1500);
        }
    }).catch(() => {});
});
