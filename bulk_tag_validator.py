import warnings
warnings.filterwarnings("ignore")
import asyncio
import pandas as pd
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import os
import time
import re
import sys
import json
import argparse
import datetime
from urllib.parse import unquote, parse_qs, urlparse

# Force UTF-8 on stdout/stderr so log lines with Unicode (->, arrows, em-dashes,
# accented site names) don't crash on Windows' default cp1252 console.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

stealth_obj = Stealth()
CONCURRENCY = 3

COOKIE_SELECTORS = [
    '#onetrust-accept-btn-handler',
    '#accept-recommended-btn-handler',
    'button[title="Accept All"]',
    'button[title="Accept"]',
    '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
    '#CybotCookiebotDialogBodyButtonAccept',
    '#truste-consent-button',
    '#didomi-notice-agree-button',
    '.cc-accept', '.cc-btn.cc-allow',
    '#cookie-accept', '#accept-cookies',
    '[data-action="accept"]',
    'button[aria-label="Accept all cookies"]',
    'button[aria-label="Accept cookies"]',
    'button[aria-label="accept and close"]',
]

COOKIE_TEXT_PATTERNS = [
    "Accept All", "Accept all", "ACCEPT ALL",
    "Accept Cookies", "Accept cookies",
    "Allow All", "Allow all",
    "I Accept", "I agree",
    "Agree", "OK", "Got it",
    "Accept & Close", "Accept and close",
    "Consent", "Continue",
]

REJECT_SELECTORS = [
    '#onetrust-reject-all-handler',
    '#CybotCookiebotDialogBodyButtonDecline',
    '#CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll',
    'button[title="Reject All"]',
    'button[title="Reject"]',
    '#truste-consent-required',
    '.cc-deny', '.cc-btn.cc-deny',
    '#cookie-reject', '#reject-cookies',
    '[data-action="reject"]',
    'button[aria-label="Reject all cookies"]',
    'button[aria-label="Reject cookies"]',
    'button[aria-label="Deny"]',
]

REJECT_TEXT_PATTERNS = [
    "Reject All", "Reject all", "REJECT ALL",
    "Reject Cookies", "Reject cookies", "Reject",
    "Decline All", "Decline all", "Decline",
    "Deny All", "Deny all", "Deny",
    "Necessary only", "Only necessary", "Use necessary cookies only",
    "Essential only", "Continue without accepting",
]

# Marketing / advertising pixels keyed by domain fragments found in network requests
MARKETING_PIXELS = {
    "Meta / Facebook Pixel": ["facebook.com/tr", "connect.facebook.net", "facebook.com/signals"],
    "Google Ads": ["googleads.g.doubleclick.net", "googleadservices.com", "/pagead/", "google.com/ads", "google.com/pagead"],
    "Floodlight (DV360)": ["fls.doubleclick.net", "ad.doubleclick.net"],
    "LinkedIn Insight": ["px.ads.linkedin.com", "snap.licdn.com"],
    "TikTok Pixel": ["analytics.tiktok.com", "tiktok.com/i18n/pixel", "tiktok.com/api/v2/pixel"],
    "X / Twitter Pixel": ["static.ads-twitter.com", "analytics.twitter.com", "t.co/i/adsct"],
    "Pinterest Tag": ["ct.pinterest.com", "s.pinimg.com/ct"],
    "Snapchat Pixel": ["tr.snapchat.com", "sc-static.net/scevent"],
    "Microsoft / Bing UET": ["bat.bing.com"],
    "Criteo": ["criteo.com", "criteo.net"],
    "Reddit Pixel": ["pixel.reddit.com", "alb.reddit.com", "redditstatic.com/ads"],
    "Quora Pixel": ["q.quora.com"],
    "Taboola": ["taboola.com"],
    "Outbrain": ["outbrain.com"],
    "Amazon Ads": ["amazon-adsystem.com"],
    "Yahoo / Verizon": ["analytics.yahoo.com", "sp.analytics.yahoo.com"],
}


def detect_marketing_pixels(url):
    """Return the set of marketing pixel names matched by a request URL."""
    low = (url or "").lower()
    found = set()
    for name, fragments in MARKETING_PIXELS.items():
        if any(f in low for f in fragments):
            found.add(name)
    return found


# ===== CONSENT SCENARIOS (OneTrust category model) =====
# C0001 Strictly Necessary | C0002 Performance | C0003 Functional
# C0004 Targeting | C0005 Social Media
SCENARIOS = ["Accept All", "Reject All", "Performance", "Functional", "Targeting"]
SCENARIO_GROUPS = {
    "Accept All":  "C0001:1,C0002:1,C0003:1,C0004:1,C0005:1",
    "Reject All":  "C0001:1,C0002:0,C0003:0,C0004:0,C0005:0",
    "Performance": "C0001:1,C0002:1,C0003:0,C0004:0,C0005:0",
    "Functional":  "C0001:1,C0002:0,C0003:1,C0004:0,C0005:0",
    "Targeting":   "C0001:1,C0002:0,C0003:0,C0004:1,C0005:1",
}
# Banner action per scenario:
#   Accept All -> click Accept-All  (full consent)
#   Reject All -> click Reject-All  (no consent)
#   Performance / Functional / Targeting -> DO NOT click any button.
#     Clicking "Accept All" would grant FULL consent and destroy the
#     partial-category meaning. The injected OneTrust OptanonConsent cookie
#     (set per category below) is what governs these scenarios.
SCENARIO_ACTION = {
    "Accept All": "accept",
    "Reject All": "reject",
    "Performance": "none",
    "Functional": "none",
    "Targeting": "none",
}

# Initiator-script signatures -> who fired the request
SOURCE_SIGNATURES = [
    ("Tealium", ["tiqcdn.com", "tiqcdn.net", "tags.tiqcdn", "/utag/", "utag.js",
                 "utag.sync", "tealium"]),
    ("Adobe",   ["assets.adobedtm.com", "/satellite-", "/launch-", "launch.min.js",
                 "launch-ensighten", "appmeasurement", "s_code", "demdex.net",
                 "adobedc.net", "/at.js", "omtrdc", "adobe.com/launch"]),
    ("GTM / gtag", ["googletagmanager.com/gtm", "googletagmanager.com/gtag",
                    "/gtm.js", "/gtag/js", "googletagmanager.com/a"]),
]


def _match_signature(url):
    low = (url or "").lower()
    for name, sigs in SOURCE_SIGNATURES:
        if any(s in low for s in sigs):
            return name
    return None


def classify_source(initiator_urls, page_host):
    """Plain (non-recursive) check of a single initiator frontier."""
    for u in initiator_urls:
        m = _match_signature(u)
        if m:
            return m
    return None


def resolve_source(seed_urls, init_map, page_host, max_depth=8):
    """Walk the request-initiator graph upward to find the true source.

    A pixel beacon's *direct* initiator is usually the pixel vendor's own
    library (e.g. fbevents.js). To know who really deployed it we follow
    'who loaded that script?' until we hit a tag manager (Tealium / Adobe /
    GTM) or run out of parents (=> the tag is hardcoded on the site)."""
    visited = set()
    frontier = [u for u in (seed_urls or []) if u]
    depth = 0
    while frontier and depth < max_depth:
        # Does anything in the current frontier belong to a tag manager?
        for u in frontier:
            m = _match_signature(u)
            if m:
                return m
        # Climb to the scripts that loaded the current frontier scripts
        nxt = []
        for u in frontier:
            if u in visited:
                continue
            visited.add(u)
            for parent in init_map.get(u, []):
                if parent and parent not in visited:
                    nxt.append(parent)
        frontier = nxt
        depth += 1
    return "Hardcoded"


# Pixel-ID extraction: regex applied to the beacon URL (and POST body)
PIXEL_ID_PATTERNS = {
    "Meta / Facebook Pixel": [r'facebook\.com/tr/?\?(?:[^&]*&)*id=(\d{6,})',
                              r'[?&]id=(\d{8,})'],
    "Google Ads": [r'/(?:viewthroughconversion|conversion|1p-conversion)/(\d{6,})',
                    r'[?&]tid=(AW-\d+)', r'[?&]label=([\w-]+)'],
    "Floodlight (DV360)": [r'[;?&]src=(\d+)', r'/activity[i]?;src=(\d+)'],
    "LinkedIn Insight": [r'[?&]pid=(\d+)', r'/px/li/track\?pid=(\d+)'],
    "TikTok Pixel": [r'[?&]sdkid=([A-Za-z0-9]+)', r'[?&]pixel_code=([A-Za-z0-9]+)'],
    "X / Twitter Pixel": [r'[?&]txn_id=([A-Za-z0-9]+)', r'[?&]p_id=([A-Za-z0-9]+)'],
    "Pinterest Tag": [r'[?&]tid=(\d+)'],
    "Snapchat Pixel": [r'[?&]pid=([A-Za-z0-9-]+)', r'[?&]id=([A-Za-z0-9-]{6,})'],
    "Microsoft / Bing UET": [r'[?&]ti=(\d+)'],
    "Criteo": [r'[?&]a=(\d+)'],
    "Reddit Pixel": [r'pixel\.reddit\.com/.*?[?&]id=([A-Za-z0-9_]+)', r'/(t2_[a-z0-9]+)/'],
    "Quora Pixel": [r'q\.quora\.com/_/ad/([0-9a-f]+)/pixel'],
    "Taboola": [r'/libtrc/(?:unip/)?(\d+)/', r'[?&]tim=.*?(\d{4,})'],
    "Outbrain": [r'[?&]marketerId=([A-Za-z0-9]+)'],
    "Amazon Ads": [r'amazon-adsystem\.com/[^?]*[?&](?:cb|pid)=([A-Za-z0-9-]+)'],
    "Yahoo / Verizon": [r'[?&]a=(\d+)'],
}


def pick_source(source_counts):
    """Return the EXACT source the pixel actually fired from — the source
    responsible for the most beacon fires (dominant origin), not a fixed
    priority guess. `source_counts` is {source: number_of_fires}.

    Tie-break: a real tag manager (Tealium/Adobe/GTM) beats 'Hardcoded',
    because a tied 'Hardcoded' is almost always just the vendor library
    script load, while the manager is what actually deployed the tag."""
    if not source_counts:
        return "Hardcoded"
    mx = max(source_counts.values())
    top = [s for s, c in source_counts.items() if c == mx]
    if len(top) == 1:
        return top[0]
    for s in ("Tealium", "Adobe", "GTM / gtag"):
        if s in top:
            return s
    return top[0]


def extract_pixel_id(name, url, post_data=""):
    """Best-effort extraction of the advertiser/pixel ID from a beacon."""
    blob = url + (("&" + post_data) if post_data else "")
    for pat in PIXEL_ID_PATTERNS.get(name, []):
        m = re.search(pat, blob, re.I)
        if m:
            return m.group(1)
    return ""


def _host_of(url):
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _cookie_domain_for(url):
    h = _host_of(url)
    if not h:
        return None
    if h.startswith("www."):
        h = h[4:]
    return "." + h


async def accept_cookies(page):
    # German / multilingual fall-back accept phrases — many EU sites surface
    # German first regardless of the URL locale.
    extra_text = COOKIE_TEXT_PATTERNS + [
        "Alle akzeptieren", "Alle Akzeptieren", "Akzeptieren", "Annehmen",
        "Zustimmen", "Auswahl bestätigen", "Einverstanden", "Erlauben",
        "Tout accepter", "Accepter", "Aceptar todas", "Aceptar",
        "Aceitar tudo", "Aceitar", "Accetta tutto",
    ]
    accepted = False
    # Try selectors twice — CMPs can stack a second dialog after the first.
    for attempt in range(2):
        clicked_this_round = False
        for sel in COOKIE_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=200):
                    await el.click(timeout=2000, force=True)
                    accepted = True
                    clicked_this_round = True
                    await asyncio.sleep(0.4)
                    break
            except: pass
        if not clicked_this_round:
            for text in extra_text:
                try:
                    btn = page.get_by_role("button", name=text, exact=False).first
                    if await btn.is_visible(timeout=200):
                        await btn.click(timeout=2000, force=True)
                        accepted = True
                        clicked_this_round = True
                        await asyncio.sleep(0.4)
                        break
                except: pass
        if not clicked_this_round:
            break
    return accepted


async def reject_cookies(page):
    for sel in REJECT_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=200):
                await el.click(timeout=2000)
                return True
        except: pass
    for text in REJECT_TEXT_PATTERNS:
        try:
            btn = page.get_by_role("button", name=text, exact=False).first
            if await btn.is_visible(timeout=200):
                await btn.click(timeout=2000)
                return True
        except: pass
    return False

def parse_analytics_payload(url, post_data=""):
    combined = url
    if post_data:
        combined = url + "&" + post_data if "?" in url else url + "?" + post_data
    low = combined.lower()
    result = {
        "is_adobe": False, "adobe_pv": False, "adobe_rsid": "",
        "is_ga4": False, "ga4_pv": False, "ga4_mid": "",
        "is_gtm": False, "gtm_id": "",
        "is_tealium_js": False, "tealium_account": "", "tealium_profile": "", "tealium_env": "",
    }

    if "/b/ss/" in low:
        result["is_adobe"] = True
        m = re.search(r'/b/ss/([^/]+)/', url)
        if m: result["adobe_rsid"] = m.group(1)
        if "pe=" not in low: result["adobe_pv"] = True

    if any(x in low for x in [".omtrdc.net", ".2o7.net", "appmeasurement", "s_code", "satellite-", "launch-"]):
        result["is_adobe"] = True

    if any(x in low for x in ["adobedc.net", "adobedc.demdex", "/ee/v", "/interact"]):
        result["is_adobe"] = True
        if post_data:
            try:
                body = json.loads(post_data)
                events = body.get("events", [])
                for ev in events:
                    xdm = ev.get("xdm", {})
                    if xdm.get("eventType") == "web.webpagedetails.pageViews": result["adobe_pv"] = True
                    web = xdm.get("web", {})
                    if web.get("webPageDetails", {}).get("pageViews", {}).get("value"): result["adobe_pv"] = True
            except: pass

    if "tiqcdn.com" in low and "utag" in low:
        result["is_tealium_js"] = True
        m = re.search(r'tiqcdn\.com/utag/([^/]+)/([^/]+)/([^/]+)/', url, re.I)
        if m:
            result["tealium_account"] = m.group(1)
            result["tealium_profile"] = m.group(2)
            result["tealium_env"] = m.group(3)

    if "googletagmanager.com/gtm.js" in low:
        result["is_gtm"] = True
        m = re.search(r'[?&]id=(GTM-[A-Z0-9]+)', url, re.I)
        if m: result["gtm_id"] = m.group(1).upper()

    if "googletagmanager.com/gtag/js" in low:
        m = re.search(r'[?&]id=(G-[A-Z0-9]+)', url, re.I)
        if m:
            result["ga4_mid"] = m.group(1).upper()
            result["is_ga4"] = True

    if "/g/collect" in low or (("google-analytics.com" in low or "analytics.google.com" in low) and "collect" in low):
        result["is_ga4"] = True
        m = re.search(r'[?&]tid=(G-[A-Z0-9]+)', combined, re.I)
        if m: result["ga4_mid"] = m.group(1).upper()
        if "en=page_view" in low: result["ga4_pv"] = True

    if result["is_ga4"] and post_data and not result["ga4_pv"]:
        if "page_view" in post_data.lower(): result["ga4_pv"] = True

    return result

async def validate_tags(browser, url, index, total):
    results = {
        "URL": url,
        "Tealium_Loaded": "FAIL", "Tealium_Account": "", "Tealium_Profile": "", "Tealium_Env": "",
        "GTM_Loaded": "FAIL", "GTM_ID": "",
        "GA4_Fired": "FAIL", "GA4_Measurement_ID": "", "GA4_PageView": "FAIL",
        "Adobe_Loaded": "FAIL", "Adobe_ReportSuite": "", "Adobe_PageView": "FAIL",
        "Error": ""
    }

    gtm_ids, ga4_ids, adobe_rsids = set(), set(), set()
    tealium_accounts = []
    flags = {"tealium_js": False, "gtm": False, "ga4": False, "ga4_pv": False, "adobe": False, "adobe_pv": False}

    context = None
    try:
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/125.0.0.0"
        )
        page = await context.new_page()
        await stealth_obj.apply_stealth_async(page)

        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Network.enable")

        def on_cdp_request(params):
            req = params.get("request", {})
            parsed = parse_analytics_payload(req.get("url", ""), req.get("postData", ""))
            if parsed["is_adobe"]: flags["adobe"] = True
            if parsed["adobe_pv"]: flags["adobe_pv"] = True
            if parsed["adobe_rsid"]: adobe_rsids.add(parsed["adobe_rsid"])
            if parsed["is_tealium_js"]:
                flags["tealium_js"] = True
                if parsed["tealium_account"]:
                    tealium_accounts.append({"account": parsed["tealium_account"], "profile": parsed["tealium_profile"], "env": parsed["tealium_env"]})
            if parsed["is_gtm"]:
                flags["gtm"] = True
                if parsed["gtm_id"]: gtm_ids.add(parsed["gtm_id"])
            if parsed["is_ga4"]:
                flags["ga4"] = True
                if parsed["ga4_mid"]: ga4_ids.add(parsed["ga4_mid"])
            if parsed["ga4_pv"]: flags["ga4_pv"] = True

        cdp.on("Network.requestWillBeSent", on_cdp_request)

        def handle_request(request):
            parsed = parse_analytics_payload(request.url, "")
            if parsed["is_adobe"]: flags["adobe"] = True
            if parsed["adobe_pv"]: flags["adobe_pv"] = True
            if parsed["adobe_rsid"]: adobe_rsids.add(parsed["adobe_rsid"])
            if parsed["is_tealium_js"]:
                flags["tealium_js"] = True
                if parsed["tealium_account"] and not tealium_accounts:
                    tealium_accounts.append({"account": parsed["tealium_account"], "profile": parsed["tealium_profile"], "env": parsed["tealium_env"]})
            if parsed["is_gtm"]:
                flags["gtm"] = True
                if parsed["gtm_id"]: gtm_ids.add(parsed["gtm_id"])
            if parsed["is_ga4"]:
                flags["ga4"] = True
                if parsed["ga4_mid"]: ga4_ids.add(parsed["ga4_mid"])
            if parsed["ga4_pv"]: flags["ga4_pv"] = True

        page.on("request", handle_request)

        sys.stdout.write(f"[{index}/{total}] Checking: {url}\n")
        sys.stdout.flush()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            results["Error"] = "Timeout" if "Timeout" in str(e) else str(e)[:80]

        await asyncio.sleep(2)
        await accept_cookies(page)
        try: await page.wait_for_load_state("networkidle", timeout=12000)
        except: pass
        await asyncio.sleep(8) # Robust wait for late tags

        # --- FALLBACK: Performance API ---
        try:
            perf_urls = await page.evaluate("performance.getEntriesByType('resource').map(r => r.name)")
            for u in (perf_urls or []):
                p = parse_analytics_payload(u, "")
                if p["is_adobe"]: flags["adobe"] = True
                if p["adobe_pv"]: flags["adobe_pv"] = True
                if p["adobe_rsid"]: adobe_rsids.add(p["adobe_rsid"])
                if p["is_ga4"]: flags["ga4"] = True
                if p["ga4_mid"]: ga4_ids.add(p["ga4_mid"])
                if p["ga4_pv"]: flags["ga4_pv"] = True
                if p["is_tealium_js"]: flags["tealium_js"] = True
        except: pass

        # --- FALLBACK: JS Objects ---
        try:
            js_data = await page.evaluate("""
                (() => {
                    let res = { utag: !!window.utag, gtm: !!window.google_tag_manager, s: !!window.s, alloy: !!window.alloy };
                    if (window.utag && window.utag.cfg) {
                        res.teal_acc = window.utag.cfg.account;
                        res.teal_prof = window.utag.cfg.profile;
                    }
                    if (window.google_tag_manager) {
                        res.gtm_ids = Object.keys(window.google_tag_manager).filter(k => k.startsWith('GTM-') || k.startsWith('G-'));
                    }
                    if (window.dataLayer) {
                        res.dl_pv = window.dataLayer.some(e => e.event === 'page_view' || e.event === 'gtm.js');
                    }
                    return res;
                })()
            """)
            if js_data.get("utag"): flags["tealium_js"] = True
            if js_data.get("gtm"): flags["gtm"] = True
            if js_data.get("s") or js_data.get("alloy"):
                flags["adobe"] = True
                flags["adobe_pv"] = True
            if js_data.get("gtm_ids"):
                for k in js_data["gtm_ids"]:
                    if k.startswith("GTM-"): gtm_ids.add(k)
                    if k.startswith("G-"): ga4_ids.add(k); flags["ga4"] = True
            if js_data.get("dl_pv"): flags["ga4_pv"] = True
        except: pass

        # BUILD RESULTS
        results["Tealium_Loaded"] = "PASS" if flags["tealium_js"] else "FAIL"
        if tealium_accounts:
            results["Tealium_Account"] = tealium_accounts[0]["account"]
            results["Tealium_Profile"] = tealium_accounts[0]["profile"]
            results["Tealium_Env"] = tealium_accounts[0]["env"]
        results["GTM_Loaded"] = "PASS" if flags["gtm"] else "FAIL"
        results["GTM_ID"] = ", ".join(sorted(gtm_ids)) if gtm_ids else ""
        results["GA4_Fired"] = "PASS" if flags["ga4"] else "FAIL"
        results["GA4_Measurement_ID"] = ", ".join(sorted(ga4_ids)) if ga4_ids else ""
        results["GA4_PageView"] = "PASS" if flags["ga4_pv"] else "FAIL"
        results["Adobe_Loaded"] = "PASS" if flags["adobe"] else "FAIL"
        results["Adobe_ReportSuite"] = ", ".join(sorted(adobe_rsids)) if adobe_rsids else ""
        results["Adobe_PageView"] = "PASS" if flags["adobe_pv"] else "FAIL"

        sys.stdout.write(f"[{index}/{total}] Done: {url} | PageView: {'YES' if flags['adobe_pv'] or flags['ga4_pv'] else 'NO'}\n")
        sys.stdout.flush()
        await page.close()
    except Exception as e:
        results["Error"] = f"Fatal: {str(e)[:80]}"
    finally:
        if context: await context.close()
    return results

async def _capture_pixels_for_scenario(browser, url, scenario):
    """Load the URL once under a single OneTrust consent `scenario` and return
    {pixel_name: {"count": int, "sources": set()}} for every marketing pixel
    that fired. Source is attributed from the JS initiator stack via CDP."""
    page_host = _host_of(url)
    cdp_records = []           # list of {"url", "init":[...], "type"}
    context = None
    try:
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )

        # Force the consent scenario via OneTrust cookies (no-op on non-OT sites)
        dom = _cookie_domain_for(url)
        if dom:
            now = datetime.datetime.utcnow()
            ts = now.strftime("%a+%b+%d+%Y+%H:%M:%S+GMT+0000")
            optanon = (
                f"isGpcEnabled=0&datestamp={ts}&version=202401.1.0&isIABGlobal=false"
                f"&hosts=&consentId=00000000-0000-0000-0000-000000000000"
                f"&interactionCount=1&landingPath=NotLandingPage"
                f"&groups={SCENARIO_GROUPS[scenario]}&AwaitingReconsent=false"
            )
            try:
                await context.add_cookies([
                    {"name": "OptanonConsent", "value": optanon, "domain": dom, "path": "/"},
                    {"name": "OptanonAlertBoxClosed",
                     "value": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                     "domain": dom, "path": "/"},
                ])
            except Exception:
                pass

        page = await context.new_page()
        await stealth_obj.apply_stealth_async(page)

        # CDP: capture each request URL with its JS initiator stack
        try:
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")

            def on_will_be_sent(params):
                try:
                    req = params.get("request", {}) or {}
                    req_url = req.get("url", "")
                    if not req_url:
                        return
                    init = params.get("initiator", {}) or {}
                    urls = []
                    if init.get("url"):
                        urls.append(init["url"])
                    cur = init.get("stack") or {}
                    depth = 0
                    while cur and depth < 6:
                        for fr in (cur.get("callFrames") or []):
                            if fr.get("url"):
                                urls.append(fr["url"])
                        cur = cur.get("parent")
                        depth += 1
                    cdp_records.append({"url": req_url, "init": urls,
                                        "type": init.get("type", ""),
                                        "post": req.get("postData", "") or ""})
                except Exception:
                    pass

            cdp.on("Network.requestWillBeSent", on_will_be_sent)
        except Exception:
            pass

        # ---- PASS 1: open the page and APPLY the consent choice ----
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        await asyncio.sleep(2)
        action = SCENARIO_ACTION.get(scenario, "accept")
        if action == "accept":
            await accept_cookies(page)
        elif action == "reject":
            await reject_cookies(page)
        # 'none' -> leave the banner untouched (pure baseline)

        # Give the CMP a moment to persist the consent (cookie / localStorage)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except: pass
        await asyncio.sleep(3)

        # ---- PASS 2: RELOAD with the consent state now in effect ----
        # Only requests from here on are counted. This is what a real user
        # sees on their next page view after choosing consent, so a site
        # that respects consent will NOT fire marketing pixels after Reject.
        cdp_records.clear()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

        await asyncio.sleep(2)
        # Re-apply the banner choice if the CMP shows it again on reload
        if action == "accept":
            await accept_cookies(page)
        elif action == "reject":
            await reject_cookies(page)

        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except: pass
        await asyncio.sleep(7)

        # Build the request->initiator graph (first load of each script wins)
        init_map = {}
        for rec in cdp_records:
            if rec["url"] not in init_map:
                init_map[rec["url"]] = rec["init"]

        pixels = {}
        for rec in cdp_records:
            for pname in detect_marketing_pixels(rec["url"]):
                bucket = pixels.setdefault(
                    pname, {"count": 0, "sources": {}, "ids": set()})
                bucket["count"] += 1

                pid = extract_pixel_id(pname, rec["url"], rec.get("post", ""))
                if pid:
                    bucket["ids"].add(pid)

                if rec.get("type") == "parser":
                    # Beacon hardcoded directly in the page HTML markup
                    src = "Hardcoded"
                else:
                    src = resolve_source(rec["init"], init_map, page_host)
                bucket["sources"][src] = bucket["sources"].get(src, 0) + 1

        await page.close()
    except Exception:
        pixels = locals().get("pixels", {})
    finally:
        if context:
            try: await context.close()
            except: pass
    return pixels


async def validate_pixels(browser, url, index, total):
    results = {"URL": url, "Compliance": "PASS", "Error": ""}
    rich = {"URL": url, "scenarios": {}}
    try:
        sys.stdout.write(f"[{index}/{total}] Checking pixels: {url}\n")
        sys.stdout.flush()

        for sc in SCENARIOS:
            px = await _capture_pixels_for_scenario(browser, url, sc)
            total_fires = sum(b["count"] for b in px.values())
            summary = "; ".join(
                f"{n}{(' #' + '/'.join(sorted(b['ids']))) if b['ids'] else ''}"
                f" x{b['count']} [{pick_source(b['sources'])}]"
                for n, b in sorted(px.items())
            ) or "None"
            results[f"{sc}_Pixels"] = summary
            results[f"{sc}_Count"] = total_fires
            rich["scenarios"][sc] = [
                {"name": n, "count": b["count"],
                 "id": "/".join(sorted(b["ids"])),
                 "source": pick_source(b["sources"])}
                for n, b in sorted(px.items())
            ]

        # Compliance fails if any marketing pixel fires after an explicit
        # Reject All (consent violation).
        results["Compliance"] = "FAIL" if results.get("Reject All_Count", 0) > 0 else "PASS"

        sys.stdout.write(
            f"[{index}/{total}] Done: {url} | "
            + " ".join(f"{s}:{results[f'{s}_Count']}" for s in SCENARIOS)
            + f" | {results['Compliance']}\n"
        )
        sys.stdout.flush()
    except Exception as e:
        results["Error"] = f"Fatal: {str(e)[:80]}"
        sys.stdout.write(f"[{index}/{total}] [ERROR] {url}\n")
        sys.stdout.flush()
    results["_rich"] = rich
    return results


def parse_ga4_event(url, post_data=""):
    """Extract a list of GA4/UA events and their parameters from a collect hit."""
    url_parts = url.split("?", 1)
    url_qs = url_parts[-1] if len(url_parts) > 1 else ""
    url_params = parse_qs(url_qs)

    lines = []
    if post_data:
        # Split post body into lines (each line represents a separate event in a batch)
        lines = [line.strip() for line in post_data.splitlines() if line.strip()]

    # If no post data lines, fall back to parsing parameters from the URL itself
    if not lines:
        lines = [url_qs]

    events = []
    for line in lines:
        line_params = parse_qs(line)
        # Combine URL common params with line-specific params (line-specific overrides)
        params = {**url_params, **line_params}

        event_name = params.get("en", [""])[0]
        is_ua = False
        if not event_name:
            if params.get("t", [""])[0] == "event":
                event_name = params.get("ea", [""])[0] or "UA Event"
                is_ua = True

        if not event_name:
            continue

        result = {
            "event": unquote(event_name),
            "measurement_id": params.get("tid", [""])[0],
            "params": {}
        }

        if is_ua:
            if params.get("ec"): result["params"]["category"] = unquote(params["ec"][0])
            if params.get("ea"): result["params"]["action"] = unquote(params["ea"][0])
            if params.get("el"): result["params"]["label"] = unquote(params["el"][0])
            if params.get("ev"): result["params"]["value"] = params["ev"][0]
        else:
            # Extract event parameters (ep.*, epn.* for numeric, up.* for user properties)
            for k, v in params.items():
                if k.startswith("ep."):
                    result["params"][k[3:]] = unquote(v[0])
                elif k.startswith("epn."):
                    result["params"][k[4:]] = v[0]  # numeric param
                elif k.startswith("up."):
                    result["params"]["[user] " + k[3:]] = unquote(v[0])

        events.append(result)

    return events


def parse_adobe_link_call(url, post_data=""):
    """Extract Adobe Analytics tracking details from a /b/ss/ hit.

    Handles THREE beacon kinds, all of which a QA engineer sees in Omnibug:
      * classic link tracking  (pe=lnk_o / lnk_d / lnk_e)
      * custom event beacons   (events=event1,event2 with no pe=)
      * page views             (s.t(): no pe=, no events=, has pageName) —
                                these fire on navigation and are the primary
                                signal on page-view-tracked sites."""
    blob = url + ("&" + post_data if post_data else "")
    params = parse_qs(blob.split("?", 1)[-1] if "?" in blob else "")

    pe = params.get("pe", [""])[0]
    events_param = params.get("events", [""])[0]
    page_name = unquote(params.get("pageName", [""])[0])
    g_url = unquote(params.get("g", [""])[0])   # the page URL the beacon was for

    if pe:
        link_type = pe                  # lnk_o / lnk_d / lnk_e
    elif events_param:
        link_type = "event"
    else:
        link_type = "page_view"

    # Human-readable name: link name (pev2) for link hits, else the pageName.
    link_name = unquote(params.get("pev2", [""])[0]) or (page_name if link_type == "page_view" else "")

    result = {
        "link_type": link_type,
        "link_name": link_name,
        "link_url": unquote(params.get("pev1", [""])[0]) or g_url,
        "page_name": page_name,
        "report_suite": "",
        "events": events_param,
        "evars": {},
        "props": {},
    }

    # Extract report suite from URL path: /b/ss/{rsid}/
    m = re.search(r'/b/ss/([^/]+)/', url)
    if m:
        result["report_suite"] = m.group(1)

    # Extract eVars (v1-v255) and props (c1-c75)
    for k, v in params.items():
        if re.match(r'^v\d+$', k):
            result["evars"][f"eVar{k[1:]}"] = unquote(v[0])
        elif re.match(r'^c\d+$', k):
            result["props"][f"prop{k[1:]}"] = unquote(v[0])

    return result

def parse_adobe_websdk_event(post_data):
    """Parse Adobe Web SDK interaction events from POST JSON payload.
    Extracts eventType, webInteraction name, URL, and type."""
    events = []
    if not post_data:
        return events
    try:
        data = json.loads(post_data)
        ev_list = data.get("events", [])
        if not isinstance(ev_list, list):
            ev_list = [ev_list] if isinstance(ev_list, dict) else []
            
        for ev in ev_list:
            if not isinstance(ev, dict):
                continue
            xdm = ev.get("xdm", {})
            if not isinstance(xdm, dict):
                continue
            
            event_type = xdm.get("eventType", "")
            web = xdm.get("web", {})
            if not isinstance(web, dict):
                web = {}
            web_interaction = web.get("webInteraction", {})
            if not isinstance(web_interaction, dict):
                web_interaction = {}
                
            interaction_name = web_interaction.get("name", "")
            interaction_url = web_interaction.get("URL", "")
            interaction_type = web_interaction.get("type", "")
            
            if event_type or interaction_name or interaction_url:
                events.append({
                    "event_type": event_type or "adobe_websdk_event",
                    "interaction_name": interaction_name,
                    "interaction_url": interaction_url,
                    "interaction_type": interaction_type,
                })
    except Exception:
        pass
    return events


# ===== CLICK TRACKING AUDIT =====

DISCOVER_CLICKABLES_JS = r"""
(() => {
    const seen = new Set();
    const results = [];

    // Tags that are inherently interactive — clicks on these are meaningful.
    const INTERACTIVE_TAGS = new Set([
        'A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'VIDEO', 'AUDIO',
        'SUMMARY', 'AREA', 'DETAILS', 'LABEL', 'OPTION',
    ]);

    // Selectors that target genuinely interactive elements directly.
    const directSelectors = [
        'a', 'area[href]', 'button', 'input[type="submit"]', 'input[type="button"]', 'input[type="image"]',
        'input', 'select', 'textarea',
        'video', 'audio', 'summary',
        '[role="button"]', '[role="link"]', '[role="tab"]', '[role="checkbox"]', '[role="radio"]',
        '[onclick]', '[data-action]', '[data-analytics]', '[data-tracking]', '[data-click]', '[data-event]', '[data-cta]', '[data-ga]',
        '[aria-expanded]', '[data-emu]', '[data-emu-track]', '[data-cmp-clickable]', '[data-track-click]', '[data-click-track]', '[data-analytics-id]',
        '.emu-button', '.emu-link', '.emu-image__link', '.cmp-button', '.cmp-link', '.cmp-image__link',
        '[class*="click-track"]', '[class*="track-click"]', '[class*="emu-button"]', '[class*="emu-link"]'
    ];

    // Class/id-based selectors that often match CONTAINER elements (div, li, span)
    // instead of the actual interactive child. We scan these separately and
    // resolve the real clickable inside them.
    const containerSelectors = [
        '.cta', '.btn', '[class*="btn"]', '[class*="button"]', '[class*="toggle"]', '[class*="accordion"]',
        '[class*="tab"]', '[class*="expand"]', '[class*="collapse"]', '[class*="menu"]', '[class*="video"]',
        '[class*="player"]', '[id*="video"]', '[id*="player"]', '[class*="click"]', '[class*="link"]',
        'form',
    ];
    
    function buildSelector(el) {
        if (el.id) return '#' + CSS.escape(el.id);
        const path = [];
        let cur = el;
        while (cur && cur !== document.body && cur !== document.documentElement) {
            let seg = cur.tagName.toLowerCase();
            if (cur.id) { path.unshift('#' + CSS.escape(cur.id)); break; }
            const parent = cur.parentElement;
            if (parent) {
                const siblings = [...parent.children].filter(c => c.tagName === cur.tagName);
                if (siblings.length > 1) seg += ':nth-of-type(' + (siblings.indexOf(cur) + 1) + ')';
            }
            path.unshift(seg);
            cur = cur.parentElement;
        }
        return path.join(' > ');
    }
    
    function isVisible(el) {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width < 3 || rect.height < 3) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        return true;
    }

    // Is this element itself interactive (not just a container)?
    function isInteractive(el) {
        if (INTERACTIVE_TAGS.has(el.tagName)) return true;
        // Elements with explicit roles are interactive
        const role = el.getAttribute('role');
        if (role && ['button','link','tab','checkbox','radio','menuitem','switch','option'].includes(role)) return true;
        // Elements with inline event handlers are interactive
        if (el.onclick || el.onmousedown || el.getAttribute('onclick')) return true;
        // data-action or similar click-intent attributes
        if (el.getAttribute('data-action') || el.getAttribute('data-click') || el.getAttribute('data-event')) return true;
        return false;
    }

    // For a container element, try to find the best interactive child.
    // Returns the child element, or null if none found.
    function findInteractiveChild(container) {
        // Prefer direct child links/buttons first
        const direct = container.querySelector('a, button, input[type="submit"], input[type="button"], [role="button"], [role="link"]');
        if (direct && isVisible(direct)) return direct;
        return null;
    }

    // Walk UP to find the nearest interactive ancestor (e.g. a div inside an a).
    // Stops at body. Returns the ancestor or null.
    function findInteractiveAncestor(el) {
        let cur = el.parentElement;
        while (cur && cur !== document.body && cur !== document.documentElement) {
            if (INTERACTIVE_TAGS.has(cur.tagName) || isInteractive(cur)) {
                return cur;
            }
            cur = cur.parentElement;
        }
        return null;
    }

    const elementsToScan = new Set();

    // 1. Gather directly interactive elements
    for (const sel of directSelectors) {
        try {
            document.querySelectorAll(sel).forEach(el => {
                if (isVisible(el)) {
                    elementsToScan.add(el);
                }
            });
        } catch(e) {}
    }

    // 2. Gather from class/id-based selectors — resolve containers to their
    //    interactive children to avoid reporting DIVs/SPANs/LIs as clickables.
    for (const sel of containerSelectors) {
        try {
            document.querySelectorAll(sel).forEach(el => {
                if (!isVisible(el)) return;
                if (isInteractive(el)) {
                    // The matched element itself is interactive
                    elementsToScan.add(el);
                } else {
                    // First check: is this container INSIDE an interactive ancestor?
                    // e.g. a div[class*="btn"] inside an <a> — prefer the <a>.
                    const ancestor = findInteractiveAncestor(el);
                    if (ancestor) {
                        elementsToScan.add(ancestor);
                        return;
                    }
                    // Container (div, li, span, etc.) — find the real interactive child
                    const child = findInteractiveChild(el);
                    if (child) {
                        elementsToScan.add(child);
                    }
                    // If no interactive child AND the container has cursor:pointer +
                    // an explicit click handler, keep it (custom JS widget).
                    else if (el.onclick || el.getAttribute('onclick') || el.getAttribute('data-action')) {
                        elementsToScan.add(el);
                    }
                    // Otherwise skip — it's just a container with a class like "tab-panel"
                }
            });
        } catch(e) {}
    }

    // 3. Gather other elements with inline events or cursor: pointer style
    try {
        document.querySelectorAll('*').forEach(el => {
            if (elementsToScan.has(el)) return;
            try {
                if (el.onclick || el.onmousedown || el.getAttribute('onclick')) {
                    if (isVisible(el)) elementsToScan.add(el);
                    return;
                }
                // Only add cursor:pointer elements if they are themselves interactive
                // or are a small leaf element (not a big container).
                const style = window.getComputedStyle(el);
                if (style.cursor === 'pointer') {
                    if (!isVisible(el)) return;
                    // If this element is inside an interactive ancestor (e.g. div inside a),
                    // prefer the ancestor — it's already discovered or will be.
                    if (!INTERACTIVE_TAGS.has(el.tagName) && !isInteractive(el)) {
                        const ancestor = findInteractiveAncestor(el);
                        if (ancestor) {
                            elementsToScan.add(ancestor);
                            return;
                        }
                    }
                    // Skip large container divs that just inherit pointer from children
                    const rect = el.getBoundingClientRect();
                    const isLargeContainer = !INTERACTIVE_TAGS.has(el.tagName)
                        && (rect.width > 600 && rect.height > 200);
                    if (!isLargeContainer) {
                        // Check if it's a container with an interactive child
                        if (INTERACTIVE_TAGS.has(el.tagName) || isInteractive(el)) {
                            elementsToScan.add(el);
                        } else {
                            const child = findInteractiveChild(el);
                            if (child) {
                                elementsToScan.add(child);
                            } else {
                                // Small pointer element with no interactive child — keep it
                                elementsToScan.add(el);
                            }
                        }
                    }
                }
            } catch(e) {}
        });
    } catch(e) {}

    function shouldIgnoreElement(el) {
        let cur = el;
        while (cur && cur !== document.body && cur !== document.documentElement) {
            const id = (cur.id || '').toLowerCase();
            const className = (typeof cur.className === 'string' ? cur.className : '').toLowerCase();
            
            if (id.includes('cookie') || id.includes('consent') || id.includes('onetrust') || id.includes('ot-') || id.includes('privacy') || id.includes('emu-consent') || id.includes('chat-') || id.includes('chatbot') || id.includes('ai-assistant')) {
                return true;
            }
            if (className.includes('cookie') || className.includes('consent') || className.includes('onetrust') || className.includes('ot-') || className.includes('privacy') || className.includes('chat-') || className.includes('chatbot') || className.includes('assistant-chat')) {
                return true;
            }
            cur = cur.parentElement;
        }
        
        const ownText = (el.innerText || el.value || '').toLowerCase().trim();
        if (ownText.includes('cookie settings') || ownText.includes('cookies settings') || ownText.includes('cookie preferences') || ownText.includes('manage cookies') || ownText.includes('accept cookies') || ownText.includes('reject cookies') || ownText.includes('accept all') || ownText.includes('reject all')) {
            return true;
        }
        
        return false;
    }

    // Dedup ONLY by logical identity (tag + text + href + id) — never by
    // pixel coordinates. Tiles and cards often contain multiple distinct
    // links at near-identical positions; coordinate-dedup was skipping them.
    elementsToScan.forEach(el => {
        try {
            if (shouldIgnoreElement(el)) return;

            // Prefer visible innerText; fall back to value, placeholder,
            // aria-label, title, alt — so icon-only buttons / image links
            // still get a meaningful label instead of "[a]".
            const text = (el.innerText
                || el.value
                || el.placeholder
                || el.getAttribute('aria-label')
                || el.getAttribute('title')
                || el.getAttribute('alt')
                || (el.querySelector && (el.querySelector('img') || {}).alt)
                || '').trim().substring(0, 80);
            const href = (el.href || el.getAttribute('href') || '').trim();
            // Dedup by normalised text + href (case-insensitive, whitespace-
            // collapsed). Same logical button across tag/id variants -> ONE
            // entry. Empty-text icons keep DOM identity so they don't merge.
            const textKey = text.toLowerCase().replace(/\s+/g, ' ');
            const hrefKey = href.toLowerCase();
            const key = textKey
                ? 'T:' + textKey + '|H:' + hrefKey
                : (el.tagName + '|H:' + hrefKey + '|I:' + (el.id || ''));
            if (seen.has(key)) return;
            seen.add(key);

            // Tag each element with its page zone so the audit can click
            // header -> footer -> body in a clean order.
            let zone = 'body';
            try {
                if (el.closest('header, [role="banner"], .site-header, #header, .header, .navbar, nav, .top-nav')) {
                    zone = 'header';
                } else if (el.closest('footer, [role="contentinfo"], .site-footer, #footer, .footer, .bottom-nav')) {
                    zone = 'footer';
                }
            } catch(e) {}

            results.push({
                selector: buildSelector(el),
                tag: el.tagName,
                text: text || `[${el.tagName.toLowerCase()}]`,
                href: href,
                id: el.id || '',
                zone: zone,
                className: (typeof el.className === 'string') ? el.className.substring(0, 100) : '',
            });
        } catch(e) {}
    });

    // QA-style: scan up to 1000 elements per page (effectively unlimited for
    // any normal site). Hard cap is just a safety net for runaway lists.
    return results.slice(0, 1000);
})()
"""


# Pre-discovery: open every collapsible / dropdown / accordion / hover-menu so
# the link-discovery pass sees the items inside. Without this, sub-menu links
# never get clicked because they aren't in the DOM (or are display:none) at
# discovery time. We do several passes because expanding one menu sometimes
# reveals more collapsibles inside it.
EXPOSE_HIDDEN_JS = r"""
(async () => {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    function fireMouseEnter(el) {
        try {
            ['pointerover','mouseover','pointerenter','mouseenter'].forEach(type => {
                el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
            });
        } catch(e) {}
    }

    function isVisible(el) {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) return false;
        const s = window.getComputedStyle(el);
        return !(s.display === 'none' || s.visibility === 'hidden');
    }

    let totalRevealed = 0;
    // Several rounds — opening one menu can reveal nested collapsibles.
    for (let round = 0; round < 4; round++) {
        let actedThisRound = 0;

        // 1. Open native <details>
        document.querySelectorAll('details:not([open])').forEach(d => {
            try { d.open = true; actedThisRound++; } catch(e) {}
        });

        // 2. Click items that explicitly mark themselves collapsed.
        const collapsedSelectors = [
            '[aria-expanded="false"]',
            '[data-toggle="collapse"]', '[data-bs-toggle="collapse"]',
            '[data-toggle="dropdown"]', '[data-bs-toggle="dropdown"]',
            '[aria-haspopup="true"]:not([aria-expanded="true"])',
            'button[class*="accordion"]', 'button[class*="expand"]',
            'button[class*="toggle"]:not([aria-expanded="true"])',
        ];
        for (const sel of collapsedSelectors) {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                if (!isVisible(el)) continue;
                try { el.click(); actedThisRound++; } catch(e) {}
                if (actedThisRound > 40) break;   // don't blow up huge pages in one round
            }
            if (actedThisRound > 40) break;
        }

        // 3. Hover-style menus (top nav). Dispatch mouse events on every
        // candidate parent so CSS :hover / JS mouseenter handlers reveal
        // their sub-menus.
        const hoverSelectors = [
            'nav li', 'nav a', '[role="menuitem"]', '[role="menubar"] > *',
            'header li', '.menu li', '.nav li', '[class*="dropdown"] > a',
            '[class*="has-submenu"]', '[class*="has-children"]',
        ];
        for (const sel of hoverSelectors) {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                if (!isVisible(el)) continue;
                fireMouseEnter(el);
            }
        }

        if (actedThisRound === 0) break;   // nothing left to open
        totalRevealed += actedThisRound;
        await sleep(250);
    }

    return totalRevealed;
})()
"""


# Block ALL navigation during the click audit so a single anchor click doesn't
# take us off the page and end the loop. preventDefault only stops the
# browser-default navigation; the analytics click listeners (Adobe s.tl, GA4
# gtag, GTM dataLayer pushes) still run as normal because they fire in the
# same event tick.
BLOCK_NAVIGATION_JS = r"""
(() => {
    if (window.__navBlockerInstalled) return true;
    window.__navBlockerInstalled = true;

    window.__navBlockerClickHandler = (e) => {
        try {
            const a = e.target && e.target.closest && e.target.closest('a');
            if (a) e.preventDefault();
        } catch (err) {}
    };
    document.addEventListener('click', window.__navBlockerClickHandler, false);

    window.__navBlockerSubmitHandler = (e) => {
        try { e.preventDefault(); } catch (err) {}
    };
    document.addEventListener('submit', window.__navBlockerSubmitHandler, false);

    return true;
})()
"""


def _norm_url(u):
    u = str(u or '').strip().lower()
    u = re.sub(r'^https?://', '', u)
    u = re.sub(r'^www\.', '', u)
    u = u.split('?')[0].split('#')[0]
    return u.strip('/')

def clean_compare_text(text1, text2):
    t1 = _norm_str(text1)
    t2 = _norm_str(text2)
    if not t1 or not t2:
        return False
    if t1 == t2:
        return True
    if len(t1) > 3 and len(t2) > 3:
        if t1 in t2 or t2 in t1:
            return True
    return False

def clean_compare_url(url1, url2):
    u1 = _norm_url(url1)
    u2 = _norm_url(url2)
    if not u1 or not u2:
        return False
    if u1 == u2:
        return True
    if u1 in u2 or u2 in u1:
        return True
    return False

def extract_event_identifiers(ev, ev_type):
    texts = set()
    urls = set()
    
    if ev_type == "ga4":
        if ev.get("event"):
            texts.add(ev["event"])
        for k, v in ev.get("params", {}).items():
            val_str = str(v).strip()
            if not val_str:
                continue
            if val_str.startswith(("http://", "https://", "/", "www.")):
                urls.add(val_str)
            else:
                texts.add(val_str)
    elif ev_type == "adobe":
        if ev.get("link_name"):
            texts.add(ev["link_name"])
        if ev.get("link_url"):
            urls.add(ev["link_url"])
        for k, v in ev.get("evars", {}).items():
            val_str = str(v).strip()
            if val_str.startswith(("http://", "https://", "/", "www.")):
                urls.add(val_str)
            else:
                texts.add(val_str)
        for k, v in ev.get("props", {}).items():
            val_str = str(v).strip()
            if val_str.startswith(("http://", "https://", "/", "www.")):
                urls.add(val_str)
            else:
                texts.add(val_str)
    elif ev_type == "adobe_websdk":
        if ev.get("interaction_name"):
            texts.add(ev["interaction_name"])
        if ev.get("interaction_url"):
            urls.add(ev["interaction_url"])
            
    return texts, urls


async def validate_clicks(browser, url, index, total):
    """Click-level analytics event audit for a single URL.
    Clicks each interactive element and captures GA4 events + Adobe link calls."""
    sys.stdout.write(f"[{index}/{total}] Click audit: {url}\n")
    sys.stdout.flush()

    context = None
    elements_result = []
    try:
        context = await browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )

        # Force the consent scenario via OneTrust cookies (no-op on non-OT sites)
        dom = _cookie_domain_for(url)
        if dom:
            now = datetime.datetime.utcnow()
            ts = now.strftime("%a+%b+%d+%Y+%H:%M:%S+GMT+0000")
            optanon = (
                f"isGpcEnabled=0&datestamp={ts}&version=202401.1.0&isIABGlobal=false"
                f"&hosts=&consentId=00000000-0000-0000-0000-000000000000"
                f"&interactionCount=1&landingPath=NotLandingPage"
                f"&groups={SCENARIO_GROUPS['Accept All']}&AwaitingReconsent=false"
            )
            try:
                await context.add_cookies([
                    {"name": "OptanonConsent", "value": optanon, "domain": dom, "path": "/"},
                    {"name": "OptanonAlertBoxClosed",
                     "value": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                     "domain": dom, "path": "/"},
                ])
            except Exception:
                pass

        page = await context.new_page()
        await stealth_obj.apply_stealth_async(page)

        # Set up CDP for request capture
        cdp = await context.new_cdp_session(page)
        await cdp.send("Network.enable")

        # PERSISTENT request log attached BEFORE navigation so we never miss a
        # beacon. We capture via THREE independent paths (CDP + Playwright
        # context-level + page-level) and dedup by url+ts. Attribution later
        # uses per-click time windows, so requests fired during page load /
        # discovery simply won't match any click — harmless.
        all_requests = []
        seen_req_keys = set()

        def _push_req(req_url, post, ts):
            if not req_url:
                return
            key = (req_url, round(ts, 2))
            if key in seen_req_keys:
                return
            seen_req_keys.add(key)
            all_requests.append({"url": req_url, "post": post or "", "ts": ts})

        def on_req_persistent(params):
            try:
                req = params.get("request", {}) or {}
                _push_req(req.get("url", ""), req.get("postData", "") or "", time.time())
            except Exception:
                pass
        cdp.on("Network.requestWillBeSent", on_req_persistent)

        def on_pw_request(request):
            try:
                pd = ""
                try: pd = request.post_data or ""
                except: pass
                _push_req(request.url, pd, time.time())
            except Exception:
                pass
        # Context-level catches requests from ALL pages/popups/frames; page
        # level is a belt-and-suspenders backup.
        try: context.on("request", on_pw_request)
        except Exception: pass
        try: page.on("request", on_pw_request)
        except Exception: pass

        # Load page and settle
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(2)
        await accept_cookies(page)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except:
            pass
        await asyncio.sleep(3)

        # Smooth scroll down and up to trigger lazy-loaded elements and dynamic content
        try:
            sys.stdout.write(f"[{index}/{total}] Scrolling page to load lazy content...\n")
            sys.stdout.flush()
            await page.evaluate("""async () => {
                await new Promise((resolve) => {
                    let totalHeight = 0;
                    const distance = 400;
                    const timer = setInterval(() => {
                        const scrollHeight = document.body.scrollHeight;
                        window.scrollBy(0, distance);
                        totalHeight += distance;
                        if(totalHeight >= scrollHeight || totalHeight > 10000){
                            clearInterval(timer);
                            resolve();
                        }
                    }, 80);
                });
                window.scrollTo(0, 0);
            }""")
            await asyncio.sleep(2)
        except Exception:
            pass

        # Block navigation ONLY during menu expansion phase so menu-opener
        # clicks don't accidentally navigate away during discovery.
        try:
            await page.evaluate(BLOCK_NAVIGATION_JS)
        except Exception:
            pass

        # 1) Expose collapsed menus / accordions / dropdowns (JS internal rounds).
        try:
            revealed = await page.evaluate(EXPOSE_HIDDEN_JS)
            if revealed:
                sys.stdout.write(f"[{index}/{total}] Revealed {revealed} hidden menu/collapsed elements\n")
                sys.stdout.flush()
                await asyncio.sleep(0.8)
        except Exception:
            pass

        # 2) Multi-pass discovery with per-nav-item hover. CSS :hover only
        #    keeps ONE menu open at a time (the one mouse is over), so we
        #    hover each top-nav item one by one and discover its submenu
        #    items while it's open. Accumulate across passes with Python-
        #    side dedup so duplicates (footer/header cookie links etc.)
        #    only count once.
        elements_by_key = {}
        def _add_batch(batch):
            for el in batch:
                t = (el.get("text") or "").strip().lower()
                t = re.sub(r'\s+', ' ', t)
                h = (el.get("href") or "").strip().lower()
                if t:
                    key = "T:" + t + "|H:" + h
                else:
                    key = (el.get("tag", "") + "|H:" + h + "|I:" + (el.get("id") or ""))
                if key not in elements_by_key:
                    elements_by_key[key] = el

        # Pass A: initial discovery (default page state, no special hovers).
        try: _add_batch(await page.evaluate(DISCOVER_CLICKABLES_JS))
        except Exception: pass

        # Pass B: hover each top-nav item ONE AT A TIME, brief settle, then
        # discover what's now visible — sub-menus revealed under that item.
        try:
            nav_locator = page.locator(
                'header > * a, header > * button, header nav a, header nav button, '
                'nav > a, nav > button, nav > ul > li, nav > ul > li > a, nav > ul > li > button, '
                '[role="menuitem"], [class*="has-submenu"], [class*="has-children"]'
            )
            nav_count = min(await nav_locator.count(), 30)
            for ni in range(nav_count):
                try:
                    await nav_locator.nth(ni).hover(timeout=400, force=True, no_wait_after=True)
                    await asyncio.sleep(0.35)        # let sub-menu render
                    _add_batch(await page.evaluate(DISCOVER_CLICKABLES_JS))
                except Exception:
                    pass
            # Move mouse off so footer hover-menus get a chance too.
            try: await page.mouse.move(10, 600)
            except: pass
            await asyncio.sleep(0.3)
        except Exception:
            pass

        # Pass C: one more expose (in case hovers triggered new collapsibles).
        try:
            await page.evaluate(EXPOSE_HIDDEN_JS)
            await asyncio.sleep(0.4)
            _add_batch(await page.evaluate(DISCOVER_CLICKABLES_JS))
        except Exception:
            pass

        # Pass D: AGGRESSIVE menu drill. Many sites (Fresenius and other
        # enterprise mega-menus) open their menus on CLICK, not hover. Find
        # every element that looks like a menu opener (hamburger / burger /
        # aria-haspopup / aria-expanded=false), click it, discover what's
        # newly visible, then drill one level deeper into any "has children"
        # parents inside the opened menu.
        opener_selectors = [
            'button[aria-label*="menu" i]',
            'button[aria-label*="navigation" i]',
            'button[aria-label*="open" i]',
            'button[class*="burger" i]',
            'button[class*="hamburger" i]',
            'button[class*="menu-toggle" i]',
            'button[class*="nav-toggle" i]',
            'button[class*="navigation-toggle" i]',
            'button[class*="navbar-toggle" i]',
            '[aria-haspopup="menu"]',
            '[aria-haspopup="true"]',
            '[data-toggle="menu"]',
            '[data-bs-toggle="offcanvas"]',
            'button[aria-expanded="false"]',
            '[role="button"][aria-controls]',
        ]
        sub_selectors = [
            '[class*="has-children"]:not([class*="has-children--hidden"])',
            '[class*="has-submenu"]',
            '[class*="parent"][role="menuitem"]',
            'li[class*="menu-item"] > a',
            'li[class*="parent"] > a',
            'li[class*="parent"] > button',
            '[aria-haspopup="true"]:not([aria-expanded="true"])',
        ]
        opened_count = 0
        for sel in opener_selectors:
            try:
                count = min(await page.locator(sel).count(), 6)
            except Exception:
                count = 0
            for i in range(count):
                try:
                    el = page.locator(sel).nth(i)
                    if not await el.is_visible(timeout=300):
                        continue
                    await el.click(force=True, timeout=900, no_wait_after=True)
                    await asyncio.sleep(0.55)   # let overlay/menu render
                    opened_count += 1
                    # Discover what just became visible
                    try: _add_batch(await page.evaluate(DISCOVER_CLICKABLES_JS))
                    except: pass
                    # RECURSIVE drill into nested submenus. Each round clicks
                    # every visible "has children" parent (which reveals its
                    # children — possibly more parents), then re-discovers.
                    # Repeat until a round reveals no new clickable elements
                    # (or we hit the round cap). This catches 3rd/4th-level
                    # mega-menus where a submenu opens yet another submenu.
                    # We track which sub-parents we've already clicked (by a
                    # cheap signature) so we don't toggle the same one shut.
                    clicked_subs = set()
                    for _round in range(6):
                        before = len(elements_by_key)
                        round_clicked = 0
                        for ssel in sub_selectors:
                            try:
                                sc = min(await page.locator(ssel).count(), 25)
                            except Exception:
                                sc = 0
                            for j in range(sc):
                                try:
                                    s_el = page.locator(ssel).nth(j)
                                    if not await s_el.is_visible(timeout=150):
                                        continue
                                    # Skip parents already expanded / clicked.
                                    try:
                                        sig = await s_el.evaluate(
                                            "e => (e.tagName||'')+'|'+(e.textContent||'').trim().slice(0,40)+'|'+(e.getAttribute('href')||'')")
                                    except Exception:
                                        sig = f"{ssel}#{j}"
                                    if sig in clicked_subs:
                                        continue
                                    try:
                                        if (await s_el.get_attribute("aria-expanded")) == "true":
                                            clicked_subs.add(sig)
                                            continue
                                    except Exception:
                                        pass
                                    clicked_subs.add(sig)
                                    await s_el.click(force=True, timeout=600, no_wait_after=True)
                                    round_clicked += 1
                                    await asyncio.sleep(0.25)
                                    _add_batch(await page.evaluate(DISCOVER_CLICKABLES_JS))
                                except Exception:
                                    pass
                        after = len(elements_by_key)
                        # Stop when a full round added nothing new and clicked
                        # nothing new — the menu tree is fully expanded.
                        if after == before and round_clicked == 0:
                            break
                    # Close any open overlay/dialog so the next opener can
                    # start from a clean state. Escape works for most CMP /
                    # mega-menu implementations.
                    try: await page.keyboard.press("Escape")
                    except: pass
                    await asyncio.sleep(0.2)
                except Exception:
                    pass
        if opened_count:
            sys.stdout.write(f"[{index}/{total}] Drilled into {opened_count} menu opener(s)\n")
            sys.stdout.flush()

        # Final discovery pass after all drilling.
        try: _add_batch(await page.evaluate(DISCOVER_CLICKABLES_JS))
        except Exception: pass

        # REMOVE the navigation blocker before the click loop.
        # We WANT clicks to navigate naturally so Adobe's deferred
        # link tracking (s.tl via beforeunload) fires its beacon.
        try:
            await page.evaluate("""(() => {
                if (window.__navBlockerClickHandler) {
                    document.removeEventListener('click', window.__navBlockerClickHandler, false);
                    window.__navBlockerClickHandler = null;
                }
                if (window.__navBlockerSubmitHandler) {
                    document.removeEventListener('submit', window.__navBlockerSubmitHandler, false);
                    window.__navBlockerSubmitHandler = null;
                }
                window.__navBlockerInstalled = false;
            })()""")
        except Exception:
            pass

        elements = list(elements_by_key.values())

        # Order: header first, then footer, then body.
        zone_rank = {"header": 0, "footer": 1, "body": 2}
        elements.sort(key=lambda e: zone_rank.get(e.get("zone", "body"), 2))

        if elements:
            by_zone = {}
            for e in elements:
                by_zone[e.get("zone", "body")] = by_zone.get(e.get("zone", "body"), 0) + 1
            zone_summary = ", ".join(f"{z}={by_zone[z]}" for z in ("header", "footer", "body") if z in by_zone)
            sys.stdout.write(f"[{index}/{total}] Found {len(elements)} clickable elements ({zone_summary})\n")
        else:
            sys.stdout.write(f"[{index}/{total}] Found 0 clickable elements on {url}\n")
        sys.stdout.flush()

        original_url = page.url

        # Request capture listeners were attached before navigation (see top of
        # this function). Clear out everything captured during page load /
        # discovery so the click loop starts from a clean slate — keeps the
        # DEBUG host dump and attribution focused on click-driven beacons.
        all_requests.clear()
        seen_req_keys.clear()

        elements_result = []
        for elem in elements:
            elements_result.append({
                "element": {
                    "selector": elem.get("selector", ""),
                    "tag": elem.get("tag", ""),
                    "text": elem.get("text", ""),
                    "href": elem.get("href", ""),
                    "id": elem.get("id", ""),
                },
                "ga4_events": [],
                "adobe_calls": [],
                "adobe_websdk": [],
                "other_analytics": [],
                "has_tracking": False,
                "total_requests": 0
            })

        GENERIC_VENDORS = [
            ("Segment",   ["api.segment.io/v1/t", "api.segment.io/v1/i", "cdp.segment.io"]),
            ("Tealium",   ["collect.tealiumiq.com/event", "collect.tealiumiq.com/i"]),
            ("Mixpanel",  ["api.mixpanel.com/track", "api-js.mixpanel.com/track"]),
            ("Heap",      ["heapanalytics.com/api/track", "heapanalytics.com/h"]),
            ("Amplitude", ["api.amplitude.com/2/httpapi", "api2.amplitude.com", "api.eu.amplitude.com"]),
            ("Hotjar",    ["vc.hotjar.io", "in.hotjar.com/api/v"]),
            ("FullStory", ["fullstory.com/rec/event", "rs.fullstory.com"]),
            ("MS Clarity",["clarity.ms/collect", "clarity.ms/eus2"]),
            ("Pendo",     ["data.pendo.io/data"]),
            ("Quantum Metric", ["cdn.quantummetric.com/qtm", "qm-record"]),
            ("Snowplow",  ["/com.snowplowanalytics.snowplow/", "/snowplow/track"]),
        ]

        def _parse_click_requests(reqs, el_res):
            """Parse a list of requests and assign analytics events to el_res."""
            for req in reqs:
                req_low = req["url"].lower()

                # --- GA4 ---
                if "/g/collect" in req_low or (
                    ("google-analytics.com" in req_low or "analytics.google.com" in req_low)
                    and "collect" in req_low
                ):
                    ga4_list = parse_ga4_event(req["url"], req["post"])
                    for ev in ga4_list:
                        if ev.get("event") and ev["event"] != "page_view":
                            el_res["ga4_events"].append(ev)
                            el_res["has_tracking"] = True
                            el_res["total_requests"] += 1

                # --- Adobe Classic (/b/ss/) ---
                # Only keep click-level beacons (lnk_o/lnk_d/lnk_e = s.tl calls)
                # Skip page_view (s.t) — those are destination page noise.
                elif "/b/ss/" in req_low:
                    adobe = parse_adobe_link_call(req["url"], req["post"])
                    if adobe and adobe.get("link_type") != "page_view":
                        el_res["adobe_calls"].append(adobe)
                        el_res["has_tracking"] = True
                        el_res["total_requests"] += 1

                # --- Adobe Web SDK (adobedc / omtrdc / demdex) ---
                elif (("adobedc.net" in req_low or "omtrdc.net" in req_low
                       or "demdex.net" in req_low or "2o7.net" in req_low)
                      and req["post"]):
                    ws_events = parse_adobe_websdk_event(req["post"])
                    for w in ws_events:
                        el_res["adobe_websdk"].append(w)
                        el_res["has_tracking"] = True
                        el_res["total_requests"] += 1

                # --- Other vendors ---
                else:
                    matched_vendor = None
                    for vname, sigs in GENERIC_VENDORS:
                        if any(s in req_low for s in sigs):
                            matched_vendor = vname
                            break
                    if matched_vendor:
                        if not any(o["vendor"] == matched_vendor for o in el_res["other_analytics"]):
                            el_res["other_analytics"].append({"vendor": matched_vendor, "url": req["url"][:200]})
                            el_res["has_tracking"] = True
                            el_res["total_requests"] += 1
                    else:
                        mp_detected = detect_marketing_pixels(req["url"])
                        for mp_name in mp_detected:
                            if not any(o["vendor"] == mp_name for o in el_res["other_analytics"]):
                                el_res["other_analytics"].append({"vendor": mp_name, "url": req["url"][:200]})
                                el_res["has_tracking"] = True
                                el_res["total_requests"] += 1

        # ---- PER-CLICK CAPTURE LOOP ----
        # For each element: bookmark request count → click → wait → diff.
        # Requests that arrive between bookmark and post-wait are directly
        # attributed to this element. No heuristics, no timing guesswork.
        for ei, elem in enumerate(elements):
            el_res = elements_result[ei]
            clicked = False
            click_method = ""
            el = None
            try:
                el = page.locator(elem["selector"]).first
            except Exception:
                pass

            # Bookmark: note how many requests exist BEFORE clicking
            req_bookmark = len(all_requests)

            if el is not None:
                # Check if element is visible — after navigating back,
                # submenus will be collapsed. Expand parent menus first.
                try:
                    is_visible = await el.is_visible(timeout=500)
                except Exception:
                    is_visible = False

                if not is_visible:
                    # JS: walk up DOM tree, expand any collapsed parent menus
                    try:
                        await el.evaluate("""e => {
                            let node = e;
                            const toExpand = [];
                            while (node && node !== document.body) {
                                node = node.parentElement;
                                if (!node) break;
                                const tag = node.tagName || '';
                                const expanded = node.getAttribute('aria-expanded');
                                const hasPopup = node.getAttribute('aria-haspopup');
                                if (tag === 'LI' || tag === 'DIV' || hasPopup) {
                                    if (expanded === 'false' || hasPopup) {
                                        toExpand.unshift(node);
                                    }
                                }
                            }
                            for (const p of toExpand) {
                                const trigger = p.querySelector(':scope > a, :scope > button, :scope > [role="button"]');
                                const t = trigger || p;
                                t.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
                                t.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                                t.click();
                            }
                        }""")
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass

                    # Playwright hover on parent nav items
                    try:
                        parent_sel = elem.get("selector", "")
                        # Extract top-level nav parent (e.g. first li in the selector)
                        parts = parent_sel.split(" > ")
                        for depth in range(2, min(len(parts), 5)):
                            partial = " > ".join(parts[:depth])
                            try:
                                parent_loc = page.locator(partial).first
                                if await parent_loc.is_visible(timeout=300):
                                    await parent_loc.hover(force=True, timeout=500)
                                    await asyncio.sleep(0.3)
                            except Exception:
                                pass
                    except Exception:
                        pass

                    # Re-check visibility
                    try:
                        is_visible = await el.is_visible(timeout=500)
                    except Exception:
                        is_visible = False

                try:
                    await el.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                await asyncio.sleep(0.2)

                # PRIMARY METHOD: CDP Input.dispatchMouseEvent
                # This sends a REAL mouse event through the browser's input
                # pipeline — generating isTrusted:true events. Adobe
                # AppMeasurement / Activity Map ONLY fires s.tl() on trusted
                # click events. Playwright's force:true and JS dispatches
                # create untrusted events which Adobe ignores.
                try:
                    box = await el.bounding_box()
                    if box:
                        cx = box['x'] + box['width'] / 2
                        cy = box['y'] + box['height'] / 2
                        await cdp.send("Input.dispatchMouseEvent", {
                            "type": "mousePressed",
                            "x": cx, "y": cy,
                            "button": "left",
                            "clickCount": 1
                        })
                        await asyncio.sleep(0.05)
                        await cdp.send("Input.dispatchMouseEvent", {
                            "type": "mouseReleased",
                            "x": cx, "y": cy,
                            "button": "left",
                            "clickCount": 1
                        })
                        clicked = True
                        click_method = "cdp-click"
                except Exception:
                    pass

                # Fallback: Playwright click (for elements where bounding box fails)
                if not clicked:
                    try:
                        await el.click(force=True, timeout=2500, no_wait_after=True)
                        clicked = True
                        click_method = "force-click"
                    except Exception:
                        pass

                # Last resort: JS click
                if not clicked:
                    try:
                        await el.evaluate("e => e.click()")
                        clicked = True
                        click_method = "js-click"
                    except Exception:
                        pass

            if clicked:
                await asyncio.sleep(3.0)   # let s.tl() / click events fire

            label = elem.get("text", "")[:30] or elem.get("selector", "")[:30]
            if not clicked:
                sys.stdout.write(
                    f"[{index}/{total}]   [{ei+1}/{len(elements)}] [{elem.get('zone', 'body').upper()[:6]}] \"{label}\" -> [SKIPPED]\n")
                sys.stdout.flush()
                continue

            # Capture: grab all NEW requests since the bookmark
            click_requests = all_requests[req_bookmark:]
            _parse_click_requests(click_requests, el_res)

            # Log result for this element
            ga_str = ", ".join(e["event"] for e in el_res["ga4_events"]) or "--"
            aa_parts = []
            for c in el_res["adobe_calls"]:
                name = c.get("link_name") or c.get("events") or c.get("link_type") or "adobe"
                aa_parts.append(name)
            aa_str = ", ".join(aa_parts) or "--"
            if el_res["adobe_websdk"] and aa_str == "--":
                aa_str = ", ".join(w["event_type"] for w in el_res["adobe_websdk"])
            oa_str = ", ".join(sorted({o["vendor"] for o in el_res["other_analytics"]})) if el_res["other_analytics"] else ""
            zone_tag = elem.get("zone", "body").upper()[:6]
            extra = f" Other:[{oa_str}]" if oa_str else ""
            sys.stdout.write(
                f"[{index}/{total}]   [{ei+1}/{len(elements)}] [{zone_tag}] \"{label}\" -> [{click_method}] GA4:[{ga_str}] Adobe:[{aa_str}]{extra}\n")
            sys.stdout.flush()

            # After click, page may have navigated. Go back to original page.
            try:
                current = page.url
                if current != original_url and not current.startswith("about:"):
                    await page.goto(original_url, wait_until="domcontentloaded", timeout=20000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except:
                        pass
                    await asyncio.sleep(1.5)
            except Exception:
                # If we can't go back, try one more time
                try:
                    await page.goto(original_url, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(1.5)
                except:
                    pass

        # DEBUG: dump unique hosts seen across the whole click loop
        try:
            from collections import Counter
            from urllib.parse import urlparse as _up
            hosts = Counter()
            for _r in all_requests:
                try:
                    h = (_up(_r["url"]).hostname or "").lower()
                    if h: hosts[h] += 1
                except: pass
            total_reqs = len(all_requests)
            tracking_keywords = ("adobedtm", "adobedc", "demdex", "omtrdc", "sc.omtrdc",
                                 "tealium", "tiqcdn", "google-analytics", "googletagmanager",
                                 "/b/ss/", "segment.io", "/g/collect", "2o7.net")
            tracking_hosts = {h: c for h, c in hosts.items()
                              if any(k in h for k in tracking_keywords) or any(k in h for k in ["analytics", "tag", "tracking", "collect"])}
            top10 = ", ".join(f"{h}({c})" for h, c in hosts.most_common(10))
            sys.stdout.write(f"[{index}/{total}] DEBUG total_requests={total_reqs} "
                             f"top_hosts: {top10}\n")
            if tracking_hosts:
                sys.stdout.write(f"[{index}/{total}] DEBUG analytics_hosts_seen: "
                                 f"{', '.join(f'{h}({c})' for h, c in tracking_hosts.items())}\n")
            else:
                sys.stdout.write(f"[{index}/{total}] DEBUG NO analytics hosts in capture — "
                                 f"likely cookie consent not accepted OR site uses 1st-party proxy\n")
            sys.stdout.flush()
        except Exception:
            pass

        try: cdp.remove_listener("Network.requestWillBeSent", on_req_persistent)
        except: pass
        try: page.remove_listener("request", on_pw_request)
        except: pass
        try: context.remove_listener("request", on_pw_request)
        except: pass

        await page.close()
    except Exception as e:
        sys.stdout.write(f"[{index}/{total}] [ERROR] {url}: {str(e)[:80]}\n")
        sys.stdout.flush()
    finally:
        if context:
            try:
                await context.close()
            except:
                pass

    tracked = sum(1 for r in elements_result if r["has_tracking"])
    sys.stdout.write(f"[{index}/{total}] Done: {url} | {tracked}/{len(elements_result)} elements have tracking\n")
    sys.stdout.flush()

    return {
        "URL": url,
        "Total_Elements": len(elements_result),
        "With_Tracking": tracked,
        "Without_Tracking": len(elements_result) - tracked,
        "Error": "",
        "_click_rich": {
            "URL": url,
            "elements": elements_result
        }
    }


# ===== SDR (Solution Design Reference) VALIDATION =====
# Reads the user's SDR Excel — a per-button QA spec — opens each page, finds
# the matching link/button, clicks it, captures the GA4 events that fire,
# and compares against the SDR's expected event name + parameters. Output
# is PASS/FAIL per row with a parameter-level diff.

def parse_sdr_file(sdr_path):
    """Parse an SDR Excel into a list of test cases.

    The user's SDR template has row 0 as human labels like "Site Type (site_type)".
    The lowercase token inside parentheses is the actual GA4 event parameter name,
    and the column's value (from row 1 onwards) is the expected value for that
    parameter on that test case.
    """
    df = pd.read_excel(sdr_path)
    if len(df) < 2:
        return []

    label_row = df.iloc[0]
    col_to_param = {}
    col_to_label = {}
    for col in df.columns:
        v = str(label_row.get(col, '') or '').strip()
        col_to_label[col] = v
        m = re.search(r'\(([a-z_][a-z0-9_]*)\)\s*$', v.lower())
        if m:
            col_to_param[col] = m.group(1).strip()

    def col_with(needle):
        for col, lbl in col_to_label.items():
            if needle in lbl.lower():
                return col
        return None

    url_col       = col_with("page url")
    loc_col       = col_with("location")
    event_col     = col_with("ga4 event name")
    link_text_col = col_with("link text")
    link_url_col  = col_with("link url")

    cases = []
    last_url = ''
    for i in range(1, len(df)):
        row = df.iloc[i]
        url_v = row.get(url_col) if url_col else None
        if pd.isna(url_v) or str(url_v).strip() == '':
            page_url = last_url
        else:
            page_url = str(url_v).strip()
            last_url = page_url

        def cell(col):
            if not col:
                return ''
            v = row.get(col)
            if pd.isna(v):
                return ''
            s = str(v).strip()
            return '' if s in ('-', 'nan', 'None') else s

        link_text = cell(link_text_col)
        link_url  = cell(link_url_col)
        if not link_text and not link_url:
            continue
        expected_event = cell(event_col)
        if not expected_event:
            continue

        expected_params = {}
        for col, pname in col_to_param.items():
            if pname in ('link_text', 'link_url'):
                continue
            sv = cell(col)
            if sv:
                expected_params[pname] = sv

        cases.append({
            "row_index": int(i),
            "page_url": page_url,
            "location": cell(loc_col).lower(),
            "link_text": link_text,
            "link_url": link_url,
            "expected_event": expected_event,
            "expected_params": expected_params,
        })
    return cases


def _norm_str(s):
    """Loose comparison normalisation — case/whitespace insensitive."""
    return re.sub(r'\s+', ' ', str(s or '').strip().lower())


async def _find_sdr_target(page, link_text, link_url):
    """Locate the SDR element by link URL (if given) or visible text.
    Returns a Playwright locator or None."""
    # 1. Exact href match
    if link_url and link_url.startswith('http'):
        try:
            esc = link_url.replace('"', '\\"')
            loc = page.locator(f'a[href="{esc}"]').first
            if await loc.count() > 0:
                return loc
        except Exception:
            pass
        # 2. Match by host+path (drop query/hash)
        try:
            from urllib.parse import urlparse
            p = urlparse(link_url)
            relative = p.path or '/'
            esc = relative.replace('"', '\\"')
            loc = page.locator(f'a[href$="{esc}"]').first
            if await loc.count() > 0:
                return loc
        except Exception:
            pass
    elif link_url and link_url.startswith('/'):
        try:
            esc = link_url.replace('"', '\\"')
            loc = page.locator(f'a[href="{esc}"]').first
            if await loc.count() > 0:
                return loc
        except Exception:
            pass
    # 3. Match by visible text (case-insensitive, partial)
    if link_text:
        try:
            loc = page.get_by_text(link_text, exact=False).first
            if await loc.count() > 0:
                return loc
        except Exception:
            pass
        # 4. Match by aria-label / title
        for attr in ('aria-label', 'title', 'alt'):
            try:
                esc = link_text.replace('"', '\\"')
                loc = page.locator(f'[{attr}*="{esc}" i]').first
                if await loc.count() > 0:
                    return loc
            except Exception:
                pass
    return None


async def validate_sdr(browser, sdr_path, start_url):
    """Run the full SDR audit. Returns one result record per SDR test case."""
    cases = parse_sdr_file(sdr_path)
    sys.stdout.write(f"[SDR] Parsed {len(cases)} test cases from SDR\n")
    sys.stdout.flush()
    if not cases:
        return []

    # Group cases by effective page URL (Global / blank -> start_url).
    by_page = {}
    page_order = []
    for c in cases:
        key = (c["page_url"] or '').strip()
        if not key or key.lower() == 'global':
            key = start_url
        if key not in by_page:
            page_order.append(key)
            by_page[key] = []
        by_page[key].append(c)

    context = await browser.new_context(
        viewport={'width': 1280, 'height': 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    page = await context.new_page()
    await stealth_obj.apply_stealth_async(page)
    cdp = await context.new_cdp_session(page)
    await cdp.send("Network.enable")

    results = []
    for page_url in page_order:
        page_cases = by_page[page_url]
        sys.stdout.write(f"[SDR] === {page_url} ({len(page_cases)} cases) ===\n")
        sys.stdout.flush()

        try:
            await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(2)
        await accept_cookies(page)
        try: await page.wait_for_load_state("networkidle", timeout=10000)
        except: pass
        await asyncio.sleep(2)

        # Block nav, expose menus, hover top nav for CSS-only dropdowns
        try: await page.evaluate(BLOCK_NAVIGATION_JS)
        except: pass
        try: await page.evaluate(EXPOSE_HIDDEN_JS)
        except: pass
        await asyncio.sleep(0.6)
        try:
            nav_loc = page.locator('header a, nav > a, nav > ul > li > a')
            nc = min(await nav_loc.count(), 15)
            for ni in range(nc):
                try:
                    await nav_loc.nth(ni).hover(timeout=350, force=True, no_wait_after=True)
                    await asyncio.sleep(0.12)
                except: pass
        except: pass
        try: await page.evaluate(EXPOSE_HIDDEN_JS)
        except: pass
        try: await page.evaluate(BLOCK_NAVIGATION_JS)
        except: pass
        await asyncio.sleep(0.5)

        for case in page_cases:
            captured = []
            def on_req(params):
                try:
                    req = params.get("request", {}) or {}
                    u = req.get("url", "")
                    if not u:
                        return
                    ul = u.lower()
                    if "/g/collect" in ul or (
                        ("google-analytics.com" in ul or "analytics.google.com" in ul)
                        and "collect" in ul):
                        captured.append({"url": u, "post": req.get("postData", "") or ""})
                except Exception:
                    pass
            cdp.on("Network.requestWillBeSent", on_req)

            target = await _find_sdr_target(page, case["link_text"], case["link_url"])
            element_found = target is not None
            clicked = False
            click_err = ""
            if element_found:
                try: await target.scroll_into_view_if_needed(timeout=1500)
                except: pass
                await asyncio.sleep(0.15)
                for method in ("force", "dispatch", "js"):
                    try:
                        if method == "force":
                            await target.click(force=True, timeout=2500, no_wait_after=True)
                        elif method == "dispatch":
                            await target.dispatch_event("click", timeout=2000)
                        else:
                            await target.evaluate("e => e.click()")
                        clicked = True
                        break
                    except Exception as ce:
                        click_err = str(ce)[:80]

            if clicked:
                await asyncio.sleep(2.5)

            cdp.remove_listener("Network.requestWillBeSent", on_req)

            # Parse captured GA4 hits — pick first matching expected event name.
            matched = None
            all_events = []
            for req in captured:
                ga4_list = parse_ga4_event(req["url"], req["post"])
                for ev in ga4_list:
                    all_events.append(ev)
                    if ev["event"] == case["expected_event"] and matched is None:
                        matched = ev

            event_pass = matched is not None
            param_results = []
            params_passed = 0
            params_failed = 0
            if matched:
                for p_name, p_expected in case["expected_params"].items():
                    actual = matched["params"].get(p_name, "")
                    is_match = _norm_str(actual) == _norm_str(p_expected)
                    if is_match:
                        params_passed += 1
                    else:
                        params_failed += 1
                    param_results.append({
                        "param": p_name,
                        "expected": p_expected,
                        "actual": actual,
                        "match": is_match,
                    })

            if not element_found:
                overall = "FAIL"
                fail_reason = "Element not found on page"
            elif not clicked:
                overall = "FAIL"
                fail_reason = f"Click failed: {click_err}"
            elif not event_pass:
                overall = "FAIL"
                fail_reason = f"Expected event '{case['expected_event']}' not fired"
            elif params_failed > 0:
                overall = "FAIL"
                fail_reason = f"{params_failed} parameter(s) mismatched"
            else:
                overall = "PASS"
                fail_reason = ""

            results.append({
                "row_index": case["row_index"],
                "page_url": page_url,
                "location": case["location"],
                "link_text": case["link_text"],
                "link_url": case["link_url"],
                "expected_event": case["expected_event"],
                "actual_events": [e["event"] for e in all_events],
                "element_found": element_found,
                "clicked": clicked,
                "click_error": click_err,
                "event_match": event_pass,
                "params_passed": params_passed,
                "params_failed": params_failed,
                "param_results": param_results,
                "overall": overall,
                "fail_reason": fail_reason,
            })
            short_label = (case["link_text"] or case["link_url"] or "?")[:38]
            sys.stdout.write(
                f"[SDR] row{case['row_index']:>3} \"{short_label}\" -> "
                f"event={'OK' if event_pass else 'MISS'} "
                f"params {params_passed}P/{params_failed}F  [{overall}]\n"
            )
            sys.stdout.flush()

    await page.close()
    await context.close()
    return results


async def run_batch(browser, urls_batch, start_index, total, mode=None):
    if mode == 'clicks':
        # Click audit is sequential per URL (clicking is inherently serial)
        results = []
        for i, url in enumerate(urls_batch):
            results.append(await validate_clicks(browser, url, start_index + i, total))
        return results
    fn = validate_pixels if mode == 'pixels' else validate_tags
    tasks = [fn(browser, url, start_index + i, total) for i, url in enumerate(urls_batch)]
    return await asyncio.gather(*tasks)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", help="Mode: 'tealium', 'ga4', 'pixels', 'clicks' or 'sdr'")
    parser.add_argument("--sdr", help="Path to SDR Excel file (for --mode sdr)")
    parser.add_argument("--start-url", dest="start_url",
                        help="Start URL for SDR audit (defaults to first http URL found in SDR)")
    args = parser.parse_args()

    # ---- SDR mode runs an entirely different audit pipeline ----
    if args.mode == 'sdr':
        sdr_path = args.sdr or 'sdr_input.xlsx'
        if not os.path.exists(sdr_path):
            sys.stdout.write(f"[SDR] SDR file not found: {sdr_path}\n"); sys.stdout.flush()
            return

        start_url = (args.start_url or '').strip()
        if not start_url:
            cases = parse_sdr_file(sdr_path)
            for c in cases:
                pu = c.get("page_url", "")
                if pu and pu.lower() != 'global' and pu.startswith('http'):
                    start_url = pu
                    break
            if not start_url:
                for c in cases:
                    lu = c.get("link_url", "")
                    if lu.startswith('http'):
                        from urllib.parse import urlparse
                        p = urlparse(lu)
                        start_url = f"{p.scheme}://{p.netloc}/"
                        break
        if not start_url:
            sys.stdout.write("[SDR] Could not determine start URL — pass --start-url\n"); sys.stdout.flush()
            return

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
            sdr_results = await validate_sdr(browser, sdr_path, start_url)
            await browser.close()

        # Persist rich JSON + a flat Excel report.
        with open('sdr_results.json', 'w', encoding='utf-8') as f:
            json.dump({"generated": datetime.datetime.now().isoformat(),
                       "start_url": start_url, "results": sdr_results}, f, indent=2, default=str)

        flat_rows = []
        for r in sdr_results:
            failed = [p for p in r["param_results"] if not p["match"]]
            flat_rows.append({
                "Row": r["row_index"],
                "Page": r["page_url"],
                "Location": r["location"],
                "Link Text": r["link_text"],
                "Link URL": r["link_url"],
                "Expected Event": r["expected_event"],
                "Element Found": "Yes" if r["element_found"] else "No",
                "Clicked": "Yes" if r["clicked"] else "No",
                "Event Fired": "Yes" if r["event_match"] else "No",
                "Actual Events": ", ".join(r["actual_events"]) or "-",
                "Params Passed": r["params_passed"],
                "Params Failed": r["params_failed"],
                "Failed Params": "; ".join(
                    f"{p['param']}: expected '{p['expected']}', got '{p['actual']}'" for p in failed) or "-",
                "Result": r["overall"],
                "Fail Reason": r["fail_reason"],
            })
        pd.DataFrame(flat_rows).to_excel('sdr_results.xlsx', index=False)
        passed = sum(1 for r in sdr_results if r["overall"] == "PASS")
        sys.stdout.write(f"[SDR] Done: {passed}/{len(sdr_results)} test cases passed\n"); sys.stdout.flush()
        return

    input_file, output_file = "input_sites.xlsx", "validation_results.xlsx"
    if not os.path.exists(input_file): return
    df = pd.read_excel(input_file)
    url_col = next((col for col in df.columns if any(n in str(col).lower() for n in ['url', 'link', 'website', 'site', 'address'])), None)
    if not url_col: return
    urls = [("https://" + str(u).strip() if not str(u).strip().startswith("http") else str(u).strip()) for u in df[url_col] if pd.notna(u)]
    total = len(urls)

    all_results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
        for i in range(0, total, CONCURRENCY):
            all_results.extend(await run_batch(browser, urls[i:i + CONCURRENCY], i + 1, total, args.mode))
        await browser.close()

    # Persist rich data for the UI
    if args.mode == 'clicks':
        click_rich = [r.pop("_click_rich") for r in all_results if "_click_rich" in r]
        with open("click_results.json", "w", encoding="utf-8") as f:
            json.dump({"generated": datetime.datetime.now().isoformat(),
                       "results": click_rich}, f, indent=2)
    elif args.mode == 'pixels':
        rich = [r.pop("_rich") for r in all_results if "_rich" in r]
        with open("validation_results.json", "w", encoding="utf-8") as f:
            json.dump({"generated": datetime.datetime.now().isoformat(),
                       "scenarios": SCENARIOS, "results": rich}, f, indent=2)
    else:
        for r in all_results:
            r.pop("_rich", None)
            r.pop("_click_rich", None)

    res_df = pd.DataFrame(all_results)
    if args.mode == 'tealium':
        cols = ['URL', 'Tealium_Loaded', 'Tealium_Account', 'Tealium_Profile', 'Tealium_Env', 'Adobe_Loaded', 'Adobe_ReportSuite', 'Adobe_PageView', 'Error']
        res_df[[c for c in cols if c in res_df.columns]].to_excel(output_file, index=False)
    elif args.mode == 'ga4':
        cols = ['URL', 'GTM_Loaded', 'GTM_ID', 'GA4_Fired', 'GA4_Measurement_ID', 'GA4_PageView', 'Error']
        res_df[[c for c in cols if c in res_df.columns]].to_excel(output_file, index=False)
    elif args.mode == 'pixels':
        cols = ['URL']
        for sc in SCENARIOS:
            cols += [f'{sc}_Count', f'{sc}_Pixels']
        cols += ['Compliance', 'Error']
        res_df[[c for c in cols if c in res_df.columns]].to_excel(output_file, index=False)
    elif args.mode == 'clicks':
        cols = ['URL', 'Total_Elements', 'With_Tracking', 'Without_Tracking', 'Error']
        summary_df = res_df[[c for c in cols if c in res_df.columns]]
        
        detail_rows = []
        for r in click_rich:
            page_url = r.get("URL", "")
            for el_res in r.get("elements", []):
                el = el_res.get("element", {})
                
                ga4_events = el_res.get("ga4_events", [])
                ga4_1_name = ga4_events[0]["event"] if len(ga4_events) > 0 else "--"
                ga4_1_id = ga4_events[0]["measurement_id"] if len(ga4_events) > 0 else "--"
                ga4_1_params = "; ".join([f"{k}={v}" for k, v in ga4_events[0].get("params", {}).items()]) if len(ga4_events) > 0 else "--"
                
                ga4_2_name = ga4_events[1]["event"] if len(ga4_events) > 1 else "--"
                ga4_2_id = ga4_events[1]["measurement_id"] if len(ga4_events) > 1 else "--"
                ga4_2_params = "; ".join([f"{k}={v}" for k, v in ga4_events[1].get("params", {}).items()]) if len(ga4_events) > 1 else "--"
                
                ga4_3_name = ga4_events[2]["event"] if len(ga4_events) > 2 else "--"
                ga4_3_id = ga4_events[2]["measurement_id"] if len(ga4_events) > 2 else "--"
                ga4_3_params = "; ".join([f"{k}={v}" for k, v in ga4_events[2].get("params", {}).items()]) if len(ga4_events) > 2 else "--"
                
                ga4_extra = ", ".join([e["event"] for e in ga4_events[3:]]) if len(ga4_events) > 3 else "--"
                
                adobe_calls = el_res.get("adobe_calls", [])
                adobe_evs = ", ".join([(c.get("link_name") or c.get("link_type")) for c in adobe_calls]) or "--"
                
                sdk_calls = el_res.get("adobe_websdk", [])
                if sdk_calls:
                    sdk_evs = ", ".join([w.get("event_type") for w in sdk_calls])
                    if adobe_evs == "--":
                        adobe_evs = sdk_evs
                    else:
                        adobe_evs += " | " + sdk_evs
                        
                other_evs = ", ".join([o.get("vendor", "") for o in el_res.get("other_analytics", [])]) or "--"
                
                detail_rows.append({
                    "Page URL": page_url,
                    "Element Tag": el.get("tag", ""),
                    "Element Text": el.get("text", ""),
                    "Element ID": el.get("id", ""),
                    "Element Href": el.get("href", ""),
                    "Element Selector": el.get("selector", ""),
                    "Tracking Detected": "Yes" if el_res.get("has_tracking") else "No",
                    "GA4 Event 1 Name": ga4_1_name,
                    "GA4 Event 1 ID": ga4_1_id,
                    "GA4 Event 1 Params": ga4_1_params,
                    "GA4 Event 2 Name": ga4_2_name,
                    "GA4 Event 2 ID": ga4_2_id,
                    "GA4 Event 2 Params": ga4_2_params,
                    "GA4 Event 3 Name": ga4_3_name,
                    "GA4 Event 3 ID": ga4_3_id,
                    "GA4 Event 3 Params": ga4_3_params,
                    "GA4 Extra Events": ga4_extra,
                    "Adobe Calls Fired": adobe_evs,
                    "Other Analytics Fired": other_evs,
                    "Total Network Requests": el_res.get("total_requests", 0)
                })
        detail_df = pd.DataFrame(detail_rows)
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            summary_df.to_excel(writer, sheet_name='Summary', index=False)
            detail_df.to_excel(writer, sheet_name='Clicked_Elements_Detail', index=False)
    else:
        res_df.to_excel(output_file, index=False)


if __name__ == "__main__":
    asyncio.run(main())
