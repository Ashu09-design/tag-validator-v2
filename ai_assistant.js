// === AI Assistant (Multi-provider chatbot) ===
// Conversational layer over the Tag Validator. The LLM calls tools that run
// the same Python validators / crawler used by the UI — but against its own
// set of files (ai_*.xlsx) so it never clobbers a manual UI run.
// Supports 2 free AI providers in a fallback chain: Groq → Gemini
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const XLSX = require('xlsx');

// --- Provider 1: Groq (primary) ---
// llama-3.3-70b-versatile: reliable NATIVE tool-calling (no JSON leaking) and a
// generous free tier — best free option for this assistant.
const GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions';
const GROQ_MODEL = 'llama-3.3-70b-versatile';

// --- Provider 2: Google Gemini (fallback) ---
const GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions';
const GEMINI_MODEL = 'gemini-2.0-flash';

module.exports = function registerAiRoutes(app, ctx) {
    const ROOT = ctx.rootDir;
    const AI_CONFIG_FILE = path.join(ROOT, 'ai_config.json');
    const AI_INPUT = path.join(ROOT, 'ai_input.xlsx');
    const AI_RESULTS = path.join(ROOT, 'ai_results.xlsx');
    const AI_RESULTS_JSON = path.join(ROOT, 'ai_results.json');
    const AI_CRAWLED = path.join(ROOT, 'ai_crawled.xlsx');
    const pyCmd = process.platform === 'win32' ? 'python' : 'python3';

    // Only one AI-triggered Python job at a time.
    let aiBusy = false;

    // ---- API keys (2-provider fallback chain) ----
    function loadConfig() {
        if (fs.existsSync(AI_CONFIG_FILE)) {
            try { return JSON.parse(fs.readFileSync(AI_CONFIG_FILE, 'utf8')) || {}; }
            catch { /* ignore */ }
        }
        return {};
    }
    function loadGroqKey() {
        const c = loadConfig();
        return (String(c.apiKey || process.env.GROQ_API_KEY || '').trim()) || null;
    }
    function loadGeminiKey() {
        const c = loadConfig();
        return (String(c.geminiKey || process.env.GEMINI_API_KEY || '').trim()) || null;
    }

    function anyKeyConfigured() {
        return !!(loadGroqKey() || loadGeminiKey());
    }

    app.get('/api/ai/config', (req, res) => {
        res.json({
            configured: anyKeyConfigured(),
            groq: !!loadGroqKey(),
            gemini: !!loadGeminiKey(),
        });
    });

    app.post('/api/ai/config', (req, res) => {
        const { apiKey, geminiKey } = req.body || {};
        const keys = [apiKey, geminiKey];
        if (!keys.some(k => typeof k === 'string' && k.trim()))
            return res.status(400).json({ error: 'Provide at least one API key' });
        const c = loadConfig();
        if (typeof apiKey === 'string' && apiKey.trim()) c.apiKey = apiKey.trim();
        if (typeof geminiKey === 'string' && geminiKey.trim()) c.geminiKey = geminiKey.trim();
        fs.writeFileSync(AI_CONFIG_FILE, JSON.stringify(c, null, 2));
        res.json({
            success: true,
            groq: !!loadGroqKey(), gemini: !!loadGeminiKey(),
        });
    });

    app.delete('/api/ai/config', (req, res) => {
        if (fs.existsSync(AI_CONFIG_FILE)) fs.unlinkSync(AI_CONFIG_FILE);
        res.json({ success: true });
    });

    // ---- Downloads for files the assistant produced ----
    app.get('/api/ai/download/results', (req, res) => {
        if (!fs.existsSync(AI_RESULTS))
            return res.status(404).send('No assistant report yet.');
        res.download(AI_RESULTS, 'AI-Tag-Report.xlsx');
    });
    app.get('/api/ai/download/crawled', (req, res) => {
        if (!fs.existsSync(AI_CRAWLED))
            return res.status(404).send('No crawled URL list yet.');
        res.download(AI_CRAWLED, 'AI-Crawled-URLs.xlsx');
    });

    // ---- Python runners ----
    function runPython(args) {
        return new Promise((resolve) => {
            const proc = spawn(pyCmd, ['-u', ...args], { cwd: ROOT });
            let out = '';
            proc.stdout.on('data', d => { out += d.toString(); });
            proc.stderr.on('data', d => { out += d.toString(); });
            proc.on('close', code => resolve({ code, out }));
            proc.on('error', err => resolve({ code: -1, out: String(err) }));
        });
    }

    function normalizeUrl(u) {
        u = String(u || '').trim();
        if (!u) return '';
        return /^https?:\/\//i.test(u) ? u : 'https://' + u;
    }

    async function crawlDomain(url, maxPages) {
        const max = Number.isFinite(+maxPages) && +maxPages > 0 ? Math.floor(+maxPages) : 20;
        const { code, out } = await runPython(
            ['domain_crawler.py', normalizeUrl(url), String(max), AI_CRAWLED]);
        if (!fs.existsSync(AI_CRAWLED))
            return { error: 'Crawl produced no pages. ' + out.slice(-300) };
        const wb = XLSX.readFile(AI_CRAWLED);
        const rows = XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]]);
        const urls = rows.map(r => r.URL).filter(Boolean);
        return { code, urls };
    }

    async function validateUrls(urls, mode) {
        const list = (urls || []).map(normalizeUrl).filter(Boolean);
        if (!list.length) return { error: 'No valid URLs given.' };
        const ws = XLSX.utils.json_to_sheet(list.map(u => ({ URL: u })));
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, 'Sheet1');
        XLSX.writeFile(wb, AI_INPUT);

        [AI_RESULTS, AI_RESULTS_JSON].forEach(f => { if (fs.existsSync(f)) fs.unlinkSync(f); });

        const m = ['tealium', 'ga4', 'pixels', 'full'].includes(mode) ? mode : 'full';
        const { code, out } = await runPython([
            'bulk_tag_validator.py', '--mode', m,
            '--input', AI_INPUT, '--output', AI_RESULTS, '--json-out', AI_RESULTS_JSON,
        ]);
        if (!fs.existsSync(AI_RESULTS))
            return { error: 'Validation produced no report. ' + out.slice(-300) };
        const wb2 = XLSX.readFile(AI_RESULTS);
        const rows = XLSX.utils.sheet_to_json(wb2.Sheets[wb2.SheetNames[0]]);
        let rich = null;
        if (m === 'pixels' && fs.existsSync(AI_RESULTS_JSON)) {
            try { rich = JSON.parse(fs.readFileSync(AI_RESULTS_JSON, 'utf8')); } catch { /* ignore */ }
        }
        return { code, mode: m, rows, rich };
    }

    // ---- Tool definitions exposed to the LLM ----
    const TOOLS = [
        {
            type: 'function',
            function: {
                name: 'validate_tags',
                description: 'Live browser tag audit of page URL(s). '
                    + 'mode "full" (DEFAULT for any general URL question): one pass detects HTTP status, CMS, '
                    + 'Tealium, Adobe (report suite, page-view), GTM (ID), GA4 (Measurement ID, page_view). '
                    + 'mode "pixels": marketing pixels across 5 consent scenarios with fire counts/IDs/source. '
                    + 'tealium/ga4 are legacy narrow modes — prefer full.',
                parameters: {
                    type: 'object',
                    properties: {
                        urls: { type: 'array', items: { type: 'string' }, description: 'Page URLs to audit.' },
                        mode: { type: 'string', enum: ['full', 'pixels', 'tealium', 'ga4'],
                            description: 'Use "full" unless user specifically wants marketing pixels.' },
                    },
                    required: ['urls', 'mode'],
                },
            },
        },
        {
            type: 'function',
            function: {
                name: 'crawl_domain',
                description: 'Discover all same-domain page URLs (sitemap + BFS crawl). Returns the URL list and '
                    + 'an Excel link. Use when user wants every page URL of a site, or before auditing a whole site.',
                parameters: {
                    type: 'object',
                    properties: {
                        url: { type: 'string', description: 'Starting domain URL, e.g. https://example.com' },
                        max_pages: { type: 'integer', description: 'Max pages to discover (default 20, 0 = unlimited).' },
                    },
                    required: ['url'],
                },
            },
        },
    ];

    async function execTool(name, args) {
        if (name === 'crawl_domain') {
            const r = await crawlDomain(args.url, args.max_pages);
            if (r.error) return r;
            return {
                total_pages: r.urls.length,
                urls: r.urls.slice(0, 80),
                truncated: r.urls.length > 80,
                excel_download: '/api/ai/download/crawled',
            };
        }
        if (name === 'validate_tags') {
            const r = await validateUrls(args.urls, args.mode);
            if (r.error) return r;
            const payload = { mode: r.mode, count: r.rows.length, results: r.rows,
                excel_download: '/api/ai/download/results' };
            if (r.rich) payload.pixel_scenarios = r.rich;
            return payload;
        }
        return { error: 'Unknown tool: ' + name };
    }

    function pruneToolResult(name, result) {
        if (!result) return result;
        if (name === 'validate_tags') {
            const pruned = {
                mode: result.mode,
                count: result.count,
                excel_download: result.excel_download
            };
            if (result.error) pruned.error = result.error;
            if (Array.isArray(result.results)) {
                pruned.results = result.results.map(row => {
                    const cleanRow = {};
                    for (const [k, v] of Object.entries(row)) {
                        if (k === 'URL' || k === 'Error') {
                            if (v) cleanRow[k] = v;
                            continue;
                        }
                        if (v !== 'FAIL' && v !== '--' && v !== null && v !== undefined && String(v).trim() !== '') {
                            cleanRow[k] = v;
                        }
                    }
                    return cleanRow;
                });
            }
            if (result.pixel_scenarios) {
                const cleanScenarios = {};
                for (const [scenario, pixels] of Object.entries(result.pixel_scenarios.scenarios || {})) {
                    if (Array.isArray(pixels)) {
                        const activePixels = pixels
                            .filter(p => p.count > 0)
                            .map(p => ({
                                name: p.name,
                                id: p.id,
                                count: p.count,
                                source: p.source
                            }));
                        if (activePixels.length > 0) {
                            cleanScenarios[scenario] = activePixels;
                        }
                    }
                }
                pruned.pixel_scenarios = {
                    compliance: result.pixel_scenarios.compliance || {},
                    active_fires: cleanScenarios
                };
            }
            return pruned;
        }
        return result;
    }

    const SYSTEM_PROMPT = `You are "Tagly", the AI assistant inside Tag Validator Pro — an expert web-analytics consultant (GA4, GTM, Adobe Analytics, Tealium iQ, consent/CMPs, marketing pixels).

STYLE: Talk like a real human texting a colleague. Answer ONLY what was asked, lead with the yes/no. Keep it short (1-3 lines) unless the question needs more. Casual, contractions, no corporate tone, no "Certainly!", no "let me know if you need anything else". Mirror the user's language (Hinglish->Hinglish). Plain sentences; bullets/tables only when they truly help. Never invent data — if unsure, say so.

TOOL ROUTING (auto-detect, never guess):
- URL + ANY general question (what analytics, is GA4/GTM/Adobe/Tealium there, Measurement ID, Report Suite, page_view, CMS, HTTP status) -> validate_tags mode "full". ONE call detects everything, so check before saying "GA4 isn't here" — the result tells you if it's Tealium/Adobe instead.
- Marketing pixels / consent / compliance -> validate_tags mode "pixels", read pixel_scenarios.
- "All page URLs of this site" -> crawl_domain, then share [Download Excel](/api/ai/download/crawled). After validate_tags offer [Download Report](/api/ai/download/results).
- Pure how-to / concept -> answer from expertise, no tool.

ANSWERING: Give exactly what was asked from the result. "page_view firing?" -> page-view status only (GA4_PageView/Adobe_PageView). "GA4 here?" -> answer GA4, but if absent and site uses Tealium/Adobe, note that in one line. Flag a 404/500 or mention CMS when relevant. For a pixels question about one scenario, report only that scenario.

RESULTS: "PASS" = detected/firing, "FAIL" = not detected. Quote concrete values (G-XXXX, GTM-XXXX, report suite, fire counts, HTTP status, CMS). HTTP 200 = OK; 404/500 = flag it. pixels Compliance "FAIL" = pixels fired even after Reject All (consent violation).

TROUBLESHOOTING ("how do I fix X"): most likely fix first, plain language, quick example. Knowledge: GA4 tag in GTM with right Measurement ID; trigger "Initialization - All Pages"; container published; snippet in <head>; verify in GA4 DebugView / google-analytics.com/g/collect; consent mode (CMP blocking analytics storage stops hits); SPAs need a History Change trigger for page_view; Adobe report suite in s.account / Launch Analytics extension.

HARD RULES: Use tools only via the proper tool-calling mechanism — never write raw tool-call JSON in your reply. Never audit placeholder URLs (example.com) — ASK for a real one. Never fabricate data/IDs/numbers. No live internet — if asked for current news, say so honestly.

Be helpful, accurate, and human.`;

    // Llama models occasionally leak a tool call as plain text. Strip such
    // artefacts so the user never sees raw function-call syntax.
    function cleanReply(t) {
        return String(t || '')
            .replace(/<function\s*=?[\s\S]*?<\/function>/gi, '')
            .replace(/<tool_call>[\s\S]*?<\/tool_call>/gi, '')
            .replace(/<\|python_tag\|>[\s\S]*$/i, '')
            .replace(/<function[\s\S]*$/i, '')
            // Last-resort: strip a leaked raw-JSON tool call (```json fenced or
            // bare) so the user never sees {"name":...,"arguments":...}.
            .replace(/```(?:json)?\s*\{[\s\S]*?"arguments"[\s\S]*?\}\s*```/gi, '')
            .replace(/\{[^{}]*"name"\s*:\s*"[a-zA-Z_]+"[\s\S]*?"arguments"\s*:\s*\{[\s\S]*?\}\s*\}/g, '')
            .trim() || "Sure — could you share the URL you'd like me to check?";
    }

    // Salvage tool calls that Llama emitted in the broken "<function=name{json}>"
    // text format (Groq returns these as a 400 'tool_use_failed' error).
    function parseLeakedToolCalls(text) {
        const calls = [];
        const re = /<function=([a-zA-Z_]+)>?\s*(\{[\s\S]*?\})\s*<\/function>/g;
        let m;
        while ((m = re.exec(text)) !== null) {
            try {
                JSON.parse(m[2]); // validate
                calls.push({
                    id: 'call_' + Math.random().toString(36).slice(2, 11),
                    type: 'function',
                    function: { name: m[1], arguments: m[2] },
                });
            } catch { /* skip malformed */ }
        }
        return calls;
    }

    const KNOWN_TOOLS = new Set(TOOLS.map(t => t.function.name));

    // Some models (esp. via OpenAI-compat shims) leak a tool call as a raw JSON
    // blob in the *content* — e.g. {"name":"validate_tags","arguments":{...}} —
    // instead of using the proper tool_calls field. Detect and convert those so
    // the audit actually runs instead of the JSON showing up in the chat.
    function parseJsonToolCalls(text) {
        const s = String(text || '').trim();
        if (!s || !s.includes('"name"') || !s.includes('"arguments"')) return [];
        const calls = [];
        // Match {...,"name":"tool",...,"arguments":{...}...} objects anywhere in
        // the text (handles ```json fences and plain JSON alike).
        const re = /\{[^{}]*"name"\s*:\s*"([a-zA-Z_]+)"[\s\S]*?"arguments"\s*:\s*(\{[\s\S]*?\})\s*\}/g;
        let m;
        while ((m = re.exec(s)) !== null) {
            if (!KNOWN_TOOLS.has(m[1])) continue;
            try {
                JSON.parse(m[2]); // validate arguments object
                calls.push({
                    id: 'call_' + Math.random().toString(36).slice(2, 11),
                    type: 'function',
                    function: { name: m[1], arguments: m[2] },
                });
            } catch { /* skip malformed */ }
        }
        return calls;
    }

    // One LLM call against a single provider (OpenAI-compatible endpoint).
    async function callProvider(provider, messages, signal) {
        const resp = await fetch(provider.url, {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + provider.key, 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model: provider.model,
                messages,
                tools: TOOLS,
                tool_choice: 'auto',
                temperature: 0.4,
                max_tokens: 1200,
            }),
            signal,
        });
        if (!resp.ok) {
            const body = await resp.text().catch(() => '');
            // 'tool_use_failed' (Groq/Llama): model emitted a malformed function
            // call — recover the intended call(s) from failed_generation.
            if (resp.status === 400) {
                let parsed = {};
                try { parsed = JSON.parse(body); } catch { /* ignore */ }
                const fg = parsed && parsed.error && parsed.error.failed_generation;
                if (fg) {
                    const calls = parseLeakedToolCalls(fg);
                    if (calls.length)
                        return { choices: [{ message: { role: 'assistant', content: '', tool_calls: calls } }] };
                    const clean = cleanReply(fg);
                    if (clean) return { choices: [{ message: { role: 'assistant', content: clean } }] };
                }
            }
            if (resp.status === 401)
                throw new Error(`${provider.name} rejected the API key — check it in AI Settings.`);
            if (resp.status === 429) {
                // Surface the provider's actual limit reason (per-minute vs
                // per-day quota) so the user knows whether to just wait a moment
                // or has exhausted the daily free allowance.
                let detail = '';
                try {
                    const p = JSON.parse(body);
                    detail = (p && p.error && (p.error.message || p.error)) || '';
                } catch { detail = body.slice(0, 200); }
                const perDay = /per day|daily|TPD|RPD|quota/i.test(detail);
                const err = new Error(
                    `${provider.name} rate limit hit${detail ? ' — ' + String(detail).slice(0, 180) : '.'}`);
                err.retryable = !perDay;   // retry only for momentary per-minute bursts
                throw err;
            }
            throw new Error(`${provider.name} API error ${resp.status}: ${body.slice(0, 200)}`);
        }
        return resp.json();
    }

    // Try providers in chain: Groq → Gemini.
    // Automatically falls back when any provider is rate-limited or failing.
    async function callLLM(messages, signal) {
        const providers = [];
        const gk = loadGroqKey();
        if (gk) providers.push({ name: 'Groq', url: GROQ_URL, model: GROQ_MODEL, key: gk });
        const gm = loadGeminiKey();
        if (gm) providers.push({ name: 'Gemini', url: GEMINI_URL, model: GEMINI_MODEL, key: gm });
        if (!providers.length) throw new Error('No LLM API key set.');

        const sleep = (ms) => new Promise(r => setTimeout(r, ms));
        let lastErr;
        for (const provider of providers) {
            // Up to 2 attempts per provider: a momentary 429 (free-tier RPM
            // burst) usually clears after a short wait, so retry once before
            // falling through to the next provider.
            for (let attempt = 0; attempt < 2; attempt++) {
                try {
                    console.log(`[AI] Trying provider: ${provider.name}${attempt ? ' (retry)' : ''}`);
                    const result = await callProvider(provider, messages, signal);
                    console.log(`[AI] ✅ ${provider.name} succeeded`);
                    return result;
                } catch (e) {
                    if (e && e.name === 'AbortError') throw e;   // user cancelled
                    console.log(`[AI] ❌ ${provider.name} failed: ${e.message}`);
                    lastErr = e;
                    if (e && e.retryable && attempt === 0) {
                        await sleep(2500);   // brief backoff, then retry same provider
                        continue;
                    }
                    break;   // non-retryable or already retried — next provider
                }
            }
        }
        throw lastErr || new Error('All LLM providers failed.');
    }

    app.post('/api/ai/chat', async (req, res) => {
        if (!anyKeyConfigured())
            return res.status(400).json({ error: 'No API key set. Open AI Settings and add at least one API key (Groq or Gemini).' });
        if (aiBusy)
            return res.status(409).json({ error: 'The assistant is already running an audit. Please wait for it to finish.' });

        const history = Array.isArray(req.body && req.body.messages) ? req.body.messages : [];
        const clean = history
            .filter(m => m && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string')
            .slice(-20)
            .map(m => ({ role: m.role, content: m.content }));
        if (!clean.length || clean[clean.length - 1].role !== 'user')
            return res.status(400).json({ error: 'No user message provided.' });

        const messages = [{ role: 'system', content: SYSTEM_PROMPT }, ...clean];

        // If the user cancels (client disconnects), abort the LLM call and
        // bail out of the loop so the assistant is free again immediately.
        // NOTE: listen on `res` — `req` 'close' fires as soon as the body is
        // consumed by express.json(), which is not a client disconnect.
        let clientGone = false;
        const llmAbort = new AbortController();
        res.on('close', () => {
            if (!res.writableEnded) { clientGone = true; llmAbort.abort(); }
        });

        aiBusy = true;
        try {
            for (let turn = 0; turn < 6; turn++) {
                if (clientGone) return;
                const data = await callLLM(messages, llmAbort.signal);
                if (clientGone) return;
                const msg = data.choices && data.choices[0] && data.choices[0].message;
                if (!msg) throw new Error('Empty response from the model.');

                // Recover tool calls the model leaked as raw JSON / <function=>
                // text in the content instead of the proper tool_calls field.
                if ((!msg.tool_calls || !msg.tool_calls.length) && typeof msg.content === 'string') {
                    const leaked = parseJsonToolCalls(msg.content);
                    if (leaked.length) {
                        msg.tool_calls = leaked;
                        msg.content = '';   // don't show the raw JSON to the user
                    }
                }
                messages.push(msg);

                if (!msg.tool_calls || !msg.tool_calls.length) {
                    return res.json({ reply: cleanReply(msg.content) });
                }
                for (const tc of msg.tool_calls) {
                    let argsObj = {};
                    try { argsObj = JSON.parse(tc.function.arguments || '{}'); } catch { /* ignore */ }
                    let result;
                    try {
                        result = await execTool(tc.function.name, argsObj);
                    } catch (e) {
                        result = { error: String((e && e.message) || e) };
                    }
                    messages.push({
                        role: 'tool',
                        tool_call_id: tc.id,
                        content: JSON.stringify(pruneToolResult(tc.function.name, result)).slice(0, 2000),
                    });
                }
            }
            if (!clientGone)
                res.json({ reply: "I ran several steps but couldn't finish — please narrow the request and try again." });
        } catch (e) {
            // A client-cancel aborts the Groq fetch — that's expected, not an error.
            if (clientGone || (e && e.name === 'AbortError')) return;
            res.status(500).json({ error: String((e && e.message) || e) });
        } finally {
            aiBusy = false;
        }
    });
};
