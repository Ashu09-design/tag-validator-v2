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
    for sel in COOKIE_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=200):
                await el.click(timeout=2000)
                return True
        except: pass
    for text in COOKIE_TEXT_PATTERNS:
        try:
            btn = page.get_by_role("button", name=text, exact=False).first
            if await btn.is_visible(timeout=200):
                await btn.click(timeout=2000)
                return True
        except: pass
    return False


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
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
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


async def run_batch(browser, urls_batch, start_index, total, mode=None):
    fn = validate_pixels if mode == 'pixels' else validate_tags
    tasks = [fn(browser, url, start_index + i, total) for i, url in enumerate(urls_batch)]
    return await asyncio.gather(*tasks)

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", help="Mode of audit: 'tealium', 'ga4' or 'pixels'")
    parser.add_argument("--input", default="input_sites.xlsx", help="Input Excel of URLs")
    parser.add_argument("--output", default="validation_results.xlsx", help="Output Excel path")
    parser.add_argument("--json-out", dest="json_out", default="validation_results.json",
                        help="Output path for rich per-scenario pixel JSON")
    args = parser.parse_args()

    input_file, output_file = args.input, args.output
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

    # Persist rich per-scenario pixel data (used by the UI for source attribution)
    if args.mode == 'pixels':
        rich = [r.pop("_rich") for r in all_results if "_rich" in r]
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"generated": datetime.datetime.now().isoformat(),
                       "scenarios": SCENARIOS, "results": rich}, f, indent=2)
    else:
        for r in all_results:
            r.pop("_rich", None)

    res_df = pd.DataFrame(all_results)
    if args.mode == 'tealium':
        cols = ['URL', 'Tealium_Loaded', 'Tealium_Account', 'Tealium_Profile', 'Tealium_Env', 'Adobe_Loaded', 'Adobe_ReportSuite', 'Adobe_PageView', 'Error']
    elif args.mode == 'ga4':
        cols = ['URL', 'GTM_Loaded', 'GTM_ID', 'GA4_Fired', 'GA4_Measurement_ID', 'GA4_PageView', 'Error']
    elif args.mode == 'pixels':
        cols = ['URL']
        for sc in SCENARIOS:
            cols += [f'{sc}_Count', f'{sc}_Pixels']
        cols += ['Compliance', 'Error']
    else: cols = res_df.columns
    res_df[[c for c in cols if c in res_df.columns]].to_excel(output_file, index=False)

if __name__ == "__main__":
    asyncio.run(main())
