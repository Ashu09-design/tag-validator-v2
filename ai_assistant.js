// === AI Assistant (Groq-powered chatbot) ===
// Conversational layer over the Tag Validator. The LLM calls tools that run
// the same Python validators / crawler used by the UI — but against its own
// set of files (ai_*.xlsx) so it never clobbers a manual UI run.
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const XLSX = require('xlsx');

const GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions';
// gpt-oss-120b produces reliable structured tool calls on Groq; the Llama
// models frequently leak malformed function-call text that the API rejects.
const GROQ_MODEL = 'openai/gpt-oss-120b';

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

    // ---- Groq API key ----
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

    app.get('/api/ai/config', (req, res) => {
        res.json({ configured: !!loadGroqKey() });
    });

    app.post('/api/ai/config', (req, res) => {
        const { apiKey } = req.body || {};
        if (typeof apiKey !== 'string' || !apiKey.trim())
            return res.status(400).json({ error: 'Groq API key required' });
        const c = loadConfig();
        c.apiKey = apiKey.trim();
        fs.writeFileSync(AI_CONFIG_FILE, JSON.stringify(c, null, 2));
        res.json({ success: true, configured: !!loadGroqKey() });
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

        const m = ['tealium', 'ga4', 'pixels'].includes(mode) ? mode : 'tealium';
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
                description: 'Run a live browser tag audit on one or more web page URLs. '
                    + 'mode "tealium" checks Tealium + Adobe Analytics (report suite, page-view tag). '
                    + 'mode "ga4" checks Google Tag Manager (GTM ID) + GA4 (measurement ID, page-view event). '
                    + 'mode "pixels" checks marketing/advertising pixels across 5 OneTrust consent scenarios '
                    + '(Accept All, Reject All, Performance, Functional, Targeting) with fire counts, pixel IDs and source. '
                    + 'Slow: ~30-60s per URL for tealium/ga4, ~2-3 min per URL for pixels.',
                parameters: {
                    type: 'object',
                    properties: {
                        urls: { type: 'array', items: { type: 'string' }, description: 'Page URLs to audit.' },
                        mode: { type: 'string', enum: ['tealium', 'ga4', 'pixels'], description: 'Audit type.' },
                    },
                    required: ['urls', 'mode'],
                },
            },
        },
        {
            type: 'function',
            function: {
                name: 'crawl_domain',
                description: 'Discover all reachable same-domain page URLs of a website via sitemap + BFS crawl. '
                    + 'Returns the list of page URLs and a downloadable Excel link. '
                    + 'Use this when the user gives a domain and wants every page URL, or before validating a whole site.',
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

    const SYSTEM_PROMPT = `You are "Tagly", the friendly AI assistant inside Tag Validator Pro — an expert web-analytics consultant (GA4, Google Tag Manager, Adobe Analytics, Tealium iQ, consent/CMPs, marketing pixels).

STYLE — talk like a real human in a chat, not a formal assistant:
- Answer ONLY what was asked. Don't over-explain, don't add sections the user didn't ask for, don't dump extra advice. If they ask a yes/no, lead with the yes/no.
- Match reply length to the question. Short question -> short 1-3 line answer. Most replies should be short. Only go long when the question genuinely needs it.
- Sound natural and casual, like a helpful colleague texting back. Use contractions, a little warmth and personality. No robotic phrasing, no "Certainly!", no forced enthusiasm, no corporate tone.
- Mirror the user's exact language and vibe — Hinglish gets Hinglish, English gets English, casual gets casual, short gets short.
- Default to plain sentences. Only use bullets/tables/code blocks when they truly make the answer clearer (like multi-URL results) — not for every reply.
- It's fine to ask a quick follow-up instead of guessing. Don't tack on "let me know if you need anything else" every time.
- Never invent data. If you don't know, just say so plainly.

TOOLS:
- validate_tags(urls, mode): live browser audit. mode "ga4" = GTM ID + GA4 (Measurement ID, page_view); mode "tealium" = Tealium account/profile/env + Adobe (Report Suite, page-view); mode "pixels" = marketing pixels across 5 consent scenarios (Accept All, Reject All, Performance, Functional, Targeting) with fire counts/IDs/source.
- crawl_domain(url, max_pages): discover every same-domain page URL.

TOOL ROUTING:
- "Is GA4/GTM here?" / Measurement ID -> validate_tags "ga4". "Is Adobe/Tealium here?" / Report Suite -> validate_tags "tealium". Page_view question -> validate_tags (call twice, ga4+tealium, to cover both). Pixels/consent/compliance -> validate_tags "pixels", read pixel_scenarios.
- "All page URLs of this site" -> crawl_domain, then share [Download Excel](/api/ai/download/crawled). After validate_tags offer [Download Report](/api/ai/download/results).
- Pure how-to / concept questions -> answer from your own expertise, no tool needed.

RESULTS: "PASS" = detected/firing, "FAIL" = not detected. Always quote concrete values (G-XXXX, GTM-XXXX, report suite, fire counts). pixels Compliance "FAIL" = pixels fired even after Reject All (consent violation).

TROUBLESHOOTING: When asked "how do I fix X", give the most likely fix first in plain language, with a quick concrete example. Add more steps only if the problem needs them — don't list every possible cause by default. Useful knowledge to draw on: GA4 tag in GTM with the right Measurement ID (e.g. G-ABC123XYZ); trigger "Initialization - All Pages"; container published; snippet in <head>; verify in GA4 DebugView / the network call to google-analytics.com/g/collect; consent mode (a CMP blocking analytics storage stops hits); SPAs need a History Change trigger for page_view; Adobe report suite lives in s.account / the Launch Analytics extension.

HARD RULES (accuracy is critical):
- Use tools only via the proper tool-calling mechanism — never write raw tool-call syntax or JSON in your reply.
- Never audit/crawl placeholder URLs (example.com). If you need a real URL and don't have one, ASK.
- Never fabricate audit data, IDs or numbers — report only what validate_tags / crawl_domain actually return.
- You have no live internet access. If asked for current news or very recent updates, say so honestly instead of guessing.

Be helpful, accurate, and human.`;

    // Llama models occasionally leak a tool call as plain text. Strip such
    // artefacts so the user never sees raw function-call syntax.
    function cleanReply(t) {
        return String(t || '')
            .replace(/<function\s*=?[\s\S]*?<\/function>/gi, '')
            .replace(/<tool_call>[\s\S]*?<\/tool_call>/gi, '')
            .replace(/<\|python_tag\|>[\s\S]*$/i, '')
            .replace(/<function[\s\S]*$/i, '')
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

    async function callGroq(apiKey, messages, signal) {
        const resp = await fetch(GROQ_URL, {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + apiKey, 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model: GROQ_MODEL,
                messages,
                tools: TOOLS,
                tool_choice: 'auto',
                temperature: 0.4,
                max_tokens: 2500,
            }),
            signal,
        });
        if (!resp.ok) {
            const body = await resp.text().catch(() => '');
            if (resp.status === 401) throw new Error('Groq rejected the API key — check it in AI Settings.');
            if (resp.status === 429) throw new Error('Groq rate limit hit — wait a moment and retry.');
            // 'tool_use_failed': the model emitted a malformed function call.
            // Recover the intended call(s) from failed_generation.
            if (resp.status === 400) {
                let parsed = {};
                try { parsed = JSON.parse(body); } catch { /* ignore */ }
                const fg = parsed && parsed.error && parsed.error.failed_generation;
                if (fg) {
                    const calls = parseLeakedToolCalls(fg);
                    if (calls.length) {
                        return { choices: [{ message: { role: 'assistant', content: '', tool_calls: calls } }] };
                    }
                    // No parseable call — treat the text as the reply.
                    const clean = cleanReply(fg);
                    if (clean) return { choices: [{ message: { role: 'assistant', content: clean } }] };
                }
            }
            throw new Error(`Groq API error ${resp.status}: ${body.slice(0, 300)}`);
        }
        return resp.json();
    }

    app.post('/api/ai/chat', async (req, res) => {
        const apiKey = loadGroqKey();
        if (!apiKey)
            return res.status(400).json({ error: 'Groq API key not set. Open AI Settings and add your key.' });
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

        // If the user cancels (client disconnects), abort the Groq call and
        // bail out of the loop so the assistant is free again immediately.
        // NOTE: listen on `res` — `req` 'close' fires as soon as the body is
        // consumed by express.json(), which is not a client disconnect.
        let clientGone = false;
        const groqAbort = new AbortController();
        res.on('close', () => {
            if (!res.writableEnded) { clientGone = true; groqAbort.abort(); }
        });

        aiBusy = true;
        try {
            for (let turn = 0; turn < 6; turn++) {
                if (clientGone) return;
                const data = await callGroq(apiKey, messages, groqAbort.signal);
                if (clientGone) return;
                const msg = data.choices && data.choices[0] && data.choices[0].message;
                if (!msg) throw new Error('Empty response from Groq.');
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
                        content: JSON.stringify(result).slice(0, 12000),
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
