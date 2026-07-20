const express = require('express');
const path = require('path');
const fs = require('fs');
const multer = require('multer');
const XLSX = require('xlsx');
const { spawn } = require('child_process');
const cors = require('cors');
const cron = require('node-cron');

const registerAiRoutes = require('./ai_assistant');

const app = express();
const PORT = process.env.PORT || 4000;

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const UPLOADS_DIR = path.join(__dirname, 'uploads');
const HISTORY_DIR = path.join(__dirname, 'history');
const SCHEDULES_FILE = path.join(__dirname, 'schedules.json');

if (!fs.existsSync(UPLOADS_DIR)) fs.mkdirSync(UPLOADS_DIR);
if (!fs.existsSync(HISTORY_DIR)) fs.mkdirSync(HISTORY_DIR);
if (!fs.existsSync(SCHEDULES_FILE)) fs.writeFileSync(SCHEDULES_FILE, JSON.stringify([]));

const upload = multer({ dest: 'uploads/' });

// --- Manual Run Logic ---
let validatorProcess = null;
let validatorLogs = [];
let lastRunMode = 'tealium';
let cancelRequested = false;

function killValidatorProcess(reason = 'cancelled by user') {
    if (!validatorProcess) return false;
    cancelRequested = true;
    try {
        if (process.platform === 'win32') {
            // SIGTERM doesn't kill child processes on Windows reliably; use taskkill /T
            spawn('taskkill', ['/pid', String(validatorProcess.pid), '/f', '/t']);
        } else {
            validatorProcess.kill('SIGTERM');
        }
    } catch (e) {
        try { validatorProcess.kill(); } catch { /* ignore */ }
    }
    validatorLogs.push(`>>> ${reason}`);
    return true;
}

app.post('/api/tag-validator/cancel', (req, res) => {
    if (!validatorProcess) return res.json({ success: false, message: 'Nothing is running' });
    killValidatorProcess('Cancelled by user');
    res.json({ success: true });
});

app.post('/api/tag-validator/upload', upload.single('file'), (req, res) => {
    if (!req.file) return res.status(400).json({ error: 'No file uploaded' });
    const targetPath = path.join(__dirname, 'input_sites.xlsx');
    fs.copyFileSync(req.file.path, targetPath);
    res.json({ success: true, originalName: req.file.originalname });
});

// Single-URL quick run: create a temp Excel with one URL and run the validator
app.post('/api/tag-validator/run-single', (req, res) => {
    if (validatorProcess) return res.status(400).json({ error: 'Running' });
    const { url, mode } = req.body || {};
    if (!url) return res.status(400).json({ error: 'URL required' });

    // Write a single-URL Excel file
    const wb = XLSX.utils.book_new();
    const ws = XLSX.utils.aoa_to_sheet([['URL'], [url.trim()]]);
    XLSX.utils.book_append_sheet(wb, ws, 'Sheet1');
    XLSX.writeFile(wb, path.join(__dirname, 'input_sites.xlsx'));

    const auditMode = mode || 'tealium';
    lastRunMode = auditMode;
    cancelRequested = false;
    validatorLogs = [`Quick Run: ${url} (${auditMode.toUpperCase()} MODE)...`];
    const pyCmd = process.platform === 'win32' ? 'python' : 'python3';
    validatorProcess = spawn(pyCmd, ['-u', 'bulk_tag_validator.py', '--mode', auditMode], { cwd: __dirname });

    validatorProcess.stdout.on('data', d => validatorLogs.push(d.toString().trim()));
    validatorProcess.stderr.on('data', d => validatorLogs.push("ERROR: " + d.toString().trim()));
    validatorProcess.on('close', code => {
        validatorLogs.push(cancelRequested ? 'Run cancelled.' : `Finished with code ${code}`);
        validatorProcess = null;
    });
    res.json({ success: true });
});

app.post('/api/tag-validator/run', (req, res) => {
    if (validatorProcess) return res.status(400).json({ error: 'Running' });
    const mode = req.body.mode || 'tealium'; // Default to tealium if not specified
    lastRunMode = mode;
    cancelRequested = false;
    validatorLogs = [`Starting Manual Run (${mode.toUpperCase()} MODE)...` ];
    const pyCmd = process.platform === 'win32' ? 'python' : 'python3';
    validatorProcess = spawn(pyCmd, ['-u', 'bulk_tag_validator.py', '--mode', mode], { cwd: __dirname });

    validatorProcess.stdout.on('data', d => validatorLogs.push(d.toString().trim()));
    validatorProcess.stderr.on('data', d => validatorLogs.push("ERROR: " + d.toString().trim()));
    validatorProcess.on('close', code => {
        validatorLogs.push(cancelRequested ? 'Run cancelled.' : `Finished with code ${code}`);
        validatorProcess = null;
    });
    res.json({ success: true });
});

app.get('/api/tag-validator/status', (req, res) => {
    res.json({ running: !!validatorProcess, cancelled: cancelRequested && !validatorProcess, logs: validatorLogs.slice(-20) });
});

app.get('/api/tag-validator/results', (req, res) => {
    const p = path.join(__dirname, 'validation_results.xlsx');
    if (!fs.existsSync(p)) return res.json({ results: [] });
    const wb = XLSX.readFile(p);
    res.json({ results: XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]]) });
});

app.get('/api/tag-validator/download', (req, res) => {
    const p = path.join(__dirname, 'validation_results.xlsx');
    if (!fs.existsSync(p))
        return res.status(404).send('No report yet — run a validation first.');
    const label = { tealium: 'Tealium-Adobe', ga4: 'GA4-GTM', pixels: 'Marketing-Pixels', clicks: 'Click-Tracking' }[lastRunMode] || lastRunMode;
    res.download(p, `Report-${label}.xlsx`);
});

// === DOMAIN CRAWL: discover same-domain URLs ===
app.post('/api/tag-validator/crawl', (req, res) => {
    if (validatorProcess) return res.status(400).json({ error: 'Running' });
    const { url, maxPages } = req.body || {};
    if (!url) return res.status(400).json({ error: 'URL required' });
    // 0 or missing = unlimited (crawl every reachable same-domain page)
    const rawMax = parseInt(maxPages, 10);
    const max = (Number.isFinite(rawMax) && rawMax > 0) ? rawMax : 0;

    ['crawled_urls.xlsx', 'validation_results.xlsx', 'validation_results.json'].forEach(f => {
        const p = path.join(__dirname, f);
        if (fs.existsSync(p)) fs.unlinkSync(p);
    });

    cancelRequested = false;
    validatorLogs = [`Crawling ${url} (${max === 0 ? 'unlimited' : 'max ' + max} pages)...`];
    const pyCmd = process.platform === 'win32' ? 'python' : 'python3';
    validatorProcess = spawn(pyCmd, ['-u', 'domain_crawler.py', url, String(max)], { cwd: __dirname });
    validatorProcess.stdout.on('data', d => validatorLogs.push(d.toString().trim()));
    validatorProcess.stderr.on('data', d => validatorLogs.push("ERROR: " + d.toString().trim()));
    validatorProcess.on('close', code => {
        validatorLogs.push(cancelRequested ? 'Crawl cancelled.' : `Crawl finished with code ${code}`);
        validatorProcess = null;
    });
    res.json({ success: true });
});

// === DOMAIN CRAWL + VALIDATE chained ===
app.post('/api/tag-validator/crawl-and-validate', (req, res) => {
    if (validatorProcess) return res.status(400).json({ error: 'Running' });
    const { url, maxPages, mode } = req.body || {};
    if (!url) return res.status(400).json({ error: 'URL required' });
    const rawMax = parseInt(maxPages, 10);
    const max = (Number.isFinite(rawMax) && rawMax > 0) ? rawMax : 0;
    const auditMode = mode || 'tealium';
    lastRunMode = auditMode;

    ['crawled_urls.xlsx', 'validation_results.xlsx', 'validation_results.json'].forEach(f => {
        const p = path.join(__dirname, f);
        if (fs.existsSync(p)) fs.unlinkSync(p);
    });

    cancelRequested = false;
    validatorLogs = [`Crawling ${url} (${max === 0 ? 'unlimited' : 'max ' + max} pages)...`];
    const pyCmd = process.platform === 'win32' ? 'python' : 'python3';
    validatorProcess = spawn(pyCmd, ['-u', 'domain_crawler.py', url, String(max)], { cwd: __dirname });
    validatorProcess.stdout.on('data', d => validatorLogs.push(d.toString().trim()));
    validatorProcess.stderr.on('data', d => validatorLogs.push("ERROR: " + d.toString().trim()));
    validatorProcess.on('close', code => {
        if (cancelRequested) {
            validatorLogs.push('Crawl cancelled. Validation phase skipped.');
            validatorProcess = null;
            return;
        }
        validatorLogs.push(`Crawl finished (code ${code}). Starting ${auditMode.toUpperCase()} validation...`);
        if (code !== 0) { validatorProcess = null; return; }
        validatorProcess = spawn(pyCmd, ['-u', 'bulk_tag_validator.py', '--mode', auditMode], { cwd: __dirname });
        validatorProcess.stdout.on('data', d => validatorLogs.push(d.toString().trim()));
        validatorProcess.stderr.on('data', d => validatorLogs.push("ERROR: " + d.toString().trim()));
        validatorProcess.on('close', c2 => {
            validatorLogs.push(cancelRequested ? 'Validation cancelled.' : `Validation finished with code ${c2}`);
            validatorProcess = null;
        });
    });
    res.json({ success: true });
});

app.get('/api/tag-validator/crawled-urls', (req, res) => {
    const p = path.join(__dirname, 'crawled_urls.xlsx');
    if (!fs.existsSync(p)) return res.json({ urls: [] });
    const wb = XLSX.readFile(p);
    const rows = XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]]);
    res.json({ urls: rows.map(r => r.URL).filter(Boolean) });
});

app.get('/api/tag-validator/crawled-urls/download', (req, res) => {
    const p = path.join(__dirname, 'crawled_urls.xlsx');
    if (!fs.existsSync(p)) return res.status(404).send('No crawled URL list — run a crawl first.');
    res.download(p, 'Crawled_URLs.xlsx');
});

// Rich per-scenario pixel data (source attribution) for the Pixels view
app.get('/api/tag-validator/results-rich', (req, res) => {
    const p = path.join(__dirname, 'validation_results.json');
    if (!fs.existsSync(p)) return res.json({ results: [], scenarios: [] });
    try {
        const d = JSON.parse(fs.readFileSync(p, 'utf8'));
        res.json({ results: d.results || [], scenarios: d.scenarios || [] });
    } catch {
        res.json({ results: [], scenarios: [] });
    }
});

// Rich per-element click tracking data (GA4 events + Adobe calls per click)
app.get('/api/tag-validator/click-results', (req, res) => {
    const p = path.join(__dirname, 'click_results.json');
    if (!fs.existsSync(p)) return res.json({ results: [] });
    try {
        const d = JSON.parse(fs.readFileSync(p, 'utf8'));
        res.json({ results: d.results || [] });
    } catch {
        res.json({ results: [] });
    }
});



// --- Email alerts (Brevo HTTP API) ---
// Sent over HTTPS:443, which cloud hosts allow — unlike SMTP ports 465/587
// that Hugging Face Spaces block outright.
const MAIL_CONFIG_FILE = path.join(__dirname, 'mail_config.json');

function loadMailCreds() {
    // In-app config takes precedence; env vars are a fallback.
    if (fs.existsSync(MAIL_CONFIG_FILE)) {
        try {
            const c = JSON.parse(fs.readFileSync(MAIL_CONFIG_FILE, 'utf8'));
            if (c.apiKey && c.sender)
                return { apiKey: String(c.apiKey).trim(), sender: String(c.sender).trim() };
        } catch { /* ignore */ }
    }
    if (process.env.BREVO_API_KEY && process.env.BREVO_SENDER)
        return { apiKey: process.env.BREVO_API_KEY.trim(), sender: process.env.BREVO_SENDER.trim() };
    return null;
}

async function sendViaBrevo(creds, message) {
    const recipients = String(message.to || '')
        .split(',').map(s => s.trim()).filter(Boolean)
        .map(email => ({ email }));
    if (!recipients.length) throw new Error('No recipient email configured');

    const payload = {
        sender: { email: creds.sender, name: 'Tag Validator' },
        to: recipients,
        subject: message.subject,
        htmlContent: message.html,
    };
    if (message.attachments && message.attachments.length) {
        payload.attachment = message.attachments
            .filter(a => a.path && fs.existsSync(a.path))
            .map(a => ({ name: a.filename, content: fs.readFileSync(a.path).toString('base64') }));
    }

    let resp;
    try {
        resp = await fetch('https://api.brevo.com/v3/smtp/email', {
            method: 'POST',
            headers: {
                'api-key': creds.apiKey,
                'content-type': 'application/json',
                'accept': 'application/json',
            },
            body: JSON.stringify(payload),
            signal: AbortSignal.timeout(15000),
        });
    } catch (e) {
        throw new Error('Could not reach Brevo API: ' + ((e && e.message) || e));
    }
    if (!resp.ok) {
        const body = await resp.text().catch(() => '');
        if (resp.status === 401)
            throw new Error('Brevo rejected the API key — check it in Email Settings.');
        if (resp.status === 400 && /sender/i.test(body))
            throw new Error('Brevo: sender email is not a verified sender on your account. ' + body);
        throw new Error(`Brevo API error ${resp.status}: ${body}`);
    }
}

function mailerReady() {
    return !!loadMailCreds();
}

app.get('/api/mail-config', (req, res) => {
    const c = loadMailCreds();
    res.json({ configured: !!c, sender: c ? c.sender : '' });
});

app.post('/api/mail-config', (req, res) => {
    const { apiKey, sender } = req.body || {};
    if (!apiKey || !sender)
        return res.status(400).json({ error: 'Brevo API key and sender email required' });
    fs.writeFileSync(MAIL_CONFIG_FILE,
        JSON.stringify({ apiKey: String(apiKey).trim(), sender: String(sender).trim() }, null, 2));
    res.json({ success: true, sender: String(sender).trim() });
});

app.delete('/api/mail-config', (req, res) => {
    if (fs.existsSync(MAIL_CONFIG_FILE)) fs.unlinkSync(MAIL_CONFIG_FILE);
    res.json({ success: true });
});

function analyzeFailures() {
    const p = path.join(__dirname, 'validation_results.xlsx');
    if (!fs.existsSync(p)) return { failed: [], total: 0 };
    const wb = XLSX.readFile(p);
    const rows = XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]]);
    const failed = [];
    for (const r of rows) {
        const reasons = [];
        if (r.Error) reasons.push(String(r.Error));
        const passKeys = ['Tealium_Loaded', 'Adobe_Loaded', 'GTM_Loaded', 'GA4_Fired'];
        const present = passKeys.filter(k => k in r);
        if (present.length && !present.some(k => r[k] === 'PASS'))
            reasons.push('No analytics tag detected');
        if (r.Compliance === 'FAIL')
            reasons.push('Consent violation: pixels fired without consent');
        if (reasons.length) failed.push({ url: r.URL, reasons });
    }
    return { failed, total: rows.length };
}

async function sendAlertEmail(recipients, label) {
    const creds = loadMailCreds();
    if (!creds) throw new Error('Email not configured — set your Brevo API key in the app (Email Settings)');
    if (!recipients) throw new Error('No recipient email configured');
    const { failed, total } = analyzeFailures();
    const ok = total - failed.length;
    const when = new Date().toLocaleString();
    const rows = failed.length
        ? failed.map(f => `<tr><td style="padding:6px 10px;border:1px solid #ddd">${f.url}</td>` +
            `<td style="padding:6px 10px;border:1px solid #ddd;color:#b91c1c">${f.reasons.join('<br>')}</td></tr>`).join('')
        : `<tr><td colspan="2" style="padding:10px;color:#16a34a">All sites passed.</td></tr>`;
    const html = `
      <div style="font-family:Arial,sans-serif;color:#1e293b">
        <h2 style="margin:0 0 4px">Tag Validation — Scheduled Run Complete</h2>
        <p style="color:#64748b;margin:0 0 6px">${label || ''} · ${when}</p>
        <p><b>Total:</b> ${total} &nbsp;|&nbsp; <b style="color:#16a34a">Passed:</b> ${ok}
           &nbsp;|&nbsp; <b style="color:#b91c1c">Failed:</b> ${failed.length}</p>
        <h3 style="margin:14px 0 6px">Failed Websites</h3>
        <table style="border-collapse:collapse;font-size:13px">
          <tr style="background:#f1f5f9">
            <th style="padding:6px 10px;border:1px solid #ddd;text-align:left">Website</th>
            <th style="padding:6px 10px;border:1px solid #ddd;text-align:left">Reason</th></tr>
          ${rows}
        </table>
        <p style="color:#94a3b8;font-size:12px;margin-top:16px">Full report attached.</p>
      </div>`;
    const xlsxPath = path.join(__dirname, 'validation_results.xlsx');
    await sendViaBrevo(creds, {
        to: recipients,
        subject: `[Tag Validator] Run complete — ${failed.length} failed of ${total}`,
        html,
        attachments: fs.existsSync(xlsxPath)
            ? [{ filename: 'Tag-Validation-Report.xlsx', path: xlsxPath }] : [],
    });
    return { total, failed: failed.length };
}

app.get('/api/mailer-status', (req, res) => res.json({ mailerReady: mailerReady() }));

app.post('/api/test-email', async (req, res) => {
    try {
        const r = await sendAlertEmail((req.body && req.body.email), 'Manual test');
        res.json({ success: true, message: `Test email sent (${r.failed} failed / ${r.total})` });
    } catch (e) {
        res.status(400).json({ error: e.message });
    }
});

// --- Scheduling Logic ---
const activeCronJobs = {};

function getSchedules() {
    try {
        return JSON.parse(fs.readFileSync(SCHEDULES_FILE, 'utf8'));
    } catch {
        return [];
    }
}

function saveSchedules(data) {
    fs.writeFileSync(SCHEDULES_FILE, JSON.stringify(data, null, 2));
}

function getCronExpression(frequency) {
    switch (frequency) {
        case 'hourly': return '0 * * * *';
        case 'daily': return '0 0 * * *';
        case 'weekly': return '0 0 * * 0';
        case 'monthly': return '0 0 1 * *';
        case 'every_minute': return '* * * * *'; // For testing
        default: return '0 0 * * *';
    }
}

function executeScheduledJob(schedule) {
    console.log(`[Scheduler] Executing job ${schedule.id} (${schedule.filename})`);
    
    // Copy the scheduled input file to the main input file location
    const inputPath = path.join(__dirname, 'input_sites.xlsx');
    if (fs.existsSync(schedule.filePath)) {
        fs.copyFileSync(schedule.filePath, inputPath);
    } else {
        console.log(`[Scheduler] File missing for job ${schedule.id}`);
        return;
    }

    const pyCmd = process.platform === 'win32' ? 'python' : 'python3';
    const proc = spawn(pyCmd, ['-u', 'bulk_tag_validator.py'], { cwd: __dirname });
    
    proc.on('close', code => {
        console.log(`[Scheduler] Job ${schedule.id} finished with code ${code}`);
        const resultFile = path.join(__dirname, 'validation_results.xlsx');
        if (fs.existsSync(resultFile)) {
            const dateStr = new Date().toISOString().replace(/[:.]/g, '-');
            const historyName = `Schedule_${schedule.id.substring(0, 4)}_${dateStr}.xlsx`;
            const historyPath = path.join(HISTORY_DIR, historyName);
            fs.copyFileSync(resultFile, historyPath);
            
            // Update last run time + send the alert email
            const schedules = getSchedules();
            const idx = schedules.findIndex(s => s.id === schedule.id);
            const recipient = (idx !== -1 ? schedules[idx].email : schedule.email) || '';
            if (recipient && mailerReady()) {
                sendAlertEmail(recipient, `Schedule ${schedule.id.substring(0, 4)} (${schedule.filename})`)
                    .then(r => {
                        if (idx !== -1) schedules[idx].lastStatus =
                            `Emailed ${recipient} — ${r.failed} failed / ${r.total}`;
                        if (idx !== -1) saveSchedules(schedules);
                    })
                    .catch(e => {
                        if (idx !== -1) { schedules[idx].lastStatus = 'Email failed: ' + e.message; saveSchedules(schedules); }
                        console.log('[Scheduler] Email error:', e.message);
                    });
            } else if (recipient) {
                if (idx !== -1) schedules[idx].lastStatus = 'Email skipped: Brevo not configured';
                console.log('[Scheduler] Email skipped: Brevo API key / sender not configured');
            }
            if (idx !== -1) {
                schedules[idx].lastRun = new Date().toISOString();
                saveSchedules(schedules);
            }
        }
    });
}

function initCronJobs() {
    const schedules = getSchedules();
    schedules.forEach(schedule => {
        const expr = getCronExpression(schedule.frequency);
        if (cron.validate(expr)) {
            const job = cron.schedule(expr, () => executeScheduledJob(schedule));
            activeCronJobs[schedule.id] = job;
        }
    });
    console.log(`[Scheduler] Initialized ${schedules.length} scheduled jobs.`);
}

app.post('/api/schedule/add', upload.single('file'), (req, res) => {
    if (!req.file) return res.status(400).json({ error: 'No file uploaded' });
    const frequency = req.body.frequency || 'daily';
    const email = (req.body.email || '').trim();

    const scheduleId = uuidv4();
    const storedFilePath = path.join(UPLOADS_DIR, `sched_${scheduleId}.xlsx`);
    fs.renameSync(req.file.path, storedFilePath);

    const newSchedule = {
        id: scheduleId,
        filename: req.file.originalname,
        filePath: storedFilePath,
        frequency: frequency,
        email: email,
        createdAt: new Date().toISOString(),
        lastRun: null,
        lastStatus: ''
    };

    const schedules = getSchedules();
    schedules.push(newSchedule);
    saveSchedules(schedules);

    const expr = getCronExpression(frequency);
    const job = cron.schedule(expr, () => executeScheduledJob(newSchedule));
    activeCronJobs[scheduleId] = job;

    res.json({ success: true, schedule: newSchedule });
});

app.get('/api/schedule/list', (req, res) => {
    res.json({ schedules: getSchedules() });
});

app.delete('/api/schedule/cancel/:id', (req, res) => {
    const id = req.params.id;
    if (activeCronJobs[id]) {
        activeCronJobs[id].stop();
        delete activeCronJobs[id];
    }
    const schedules = getSchedules().filter(s => s.id !== id);
    saveSchedules(schedules);
    res.json({ success: true });
});

app.get('/api/schedule/history', (req, res) => {
    const files = fs.readdirSync(HISTORY_DIR).filter(f => f.endsWith('.xlsx'));
    const history = files.map(f => {
        const stats = fs.statSync(path.join(HISTORY_DIR, f));
        return {
            filename: f,
            date: stats.mtime.toISOString(),
            size: stats.size
        };
    }).sort((a, b) => new Date(b.date) - new Date(a.date));
    res.json({ history });
});

app.get('/api/schedule/download/:filename', (req, res) => {
    const p = path.join(HISTORY_DIR, req.params.filename);
    if (fs.existsSync(p)) res.download(p);
    else res.status(404).json({ error: 'Not found' });
});

initCronJobs();

// --- AI Assistant (Groq chatbot) ---
registerAiRoutes(app, { rootDir: __dirname });

app.listen(PORT, () => console.log(`Tag Validator running at http://localhost:${PORT}`));
