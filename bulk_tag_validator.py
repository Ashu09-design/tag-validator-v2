import warnings
warnings.filterwarnings("ignore")
import asyncio
import pandas as pd
from playwright.async_api import async_playwright
# playwright-stealth changed its public API between 1.x and 2.x. 1.x exposes a
# module-level `stealth_async(page)`; 2.x exposes a `Stealth` class with
# `apply_stealth_async(page)`. Pin-free support for both, and degrade to a
# no-op if the package is missing entirely, so an environment mismatch can
# never take the whole validator down at import time.
try:
    from playwright_stealth import Stealth as _StealthCls
except ImportError:
    _StealthCls = None
try:
    from playwright_stealth import stealth_async as _stealth_async_fn
except ImportError:
    _stealth_async_fn = None
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

class _StealthCompat:
    """Uniform `apply_stealth_async(page)` across playwright-stealth versions.

    IMPORTANT: playwright-stealth 1.x is deliberately NOT used. Its injected
    evasion scripts reference helpers that do not exist in the page context
    ("utils is not defined"), which throws on every page and takes the site's
    tag manager down with it — GTM stops initialising, dataLayer freezes and
    no analytics tag ever fires. A click audit run under 1.x therefore reports
    "no tracking" for a page that is in fact fully tracked. Silently wrong
    output is far worse than skipping bot-evasion, so 1.x is skipped unless
    the operator explicitly forces it with TV_FORCE_STEALTH=1.
    """

    def __init__(self):
        self._impl = None
        self._legacy = None
        self._warned = False
        if _StealthCls is not None:
            try:
                self._impl = _StealthCls()          # 2.x — safe
            except Exception:
                self._impl = None
        if self._impl is None and _stealth_async_fn is not None:
            if os.environ.get("TV_FORCE_STEALTH") == "1":
                self._legacy = _stealth_async_fn

    async def apply_stealth_async(self, page):
        try:
            if self._impl is not None:
                await self._impl.apply_stealth_async(page)
            elif self._legacy is not None:
                await self._legacy(page)
            elif _stealth_async_fn is not None and not self._warned:
                self._warned = True
                sys.stderr.write(
                    "[WARN] playwright-stealth 1.x detected and skipped: it breaks "
                    "page JS (GTM/dataLayer) and would corrupt the audit. "
                    "Install playwright-stealth>=2.0.0 for stealth support, or set "
                    "TV_FORCE_STEALTH=1 to use 1.x anyway." + os.linesep)
                sys.stderr.flush()
        except Exception:
            # Stealth is a nice-to-have for bot detection, never a hard
            # dependency of the audit itself.
            pass


stealth_obj = _StealthCompat()
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
    # --- Social / Ad-network pixels (Targeting category) ---
    "Meta / Facebook Pixel": ["facebook.com/tr", "connect.facebook.net/en_us/fbevents",
                              "connect.facebook.net/signals", "facebook.com/signals",
                              "/fbevents.js"],
    "Google Ads": ["googleads.g.doubleclick.net", "googleadservices.com",
                   "/pagead/1p-conversion", "/pagead/viewthroughconversion",
                   "/pagead/conversion", "google.com/ads/ga-audiences",
                   "google.com/pagead"],
    "Floodlight (DV360)": ["fls.doubleclick.net", "ad.doubleclick.net/activity",
                           "ad.doubleclick.net/ddm/activity"],
    "Google DV360 / Display": ["stats.g.doubleclick.net/g/collect",
                                "stats.g.doubleclick.net/r/collect",
                                "stats.g.doubleclick.net/j/collect"],
    "LinkedIn Insight": ["px.ads.linkedin.com", "snap.licdn.com/li.lms-analytics",
                         "/li/track", "/collect/?pid="],
    "TikTok Pixel": ["analytics.tiktok.com", "tiktok.com/i18n/pixel",
                     "tiktok.com/api/v2/pixel", "business-api.tiktok.com/track"],
    "X / Twitter Pixel": ["static.ads-twitter.com", "analytics.twitter.com",
                          "t.co/i/adsct", "ads-api.twitter.com"],
    "Pinterest Tag": ["ct.pinterest.com", "s.pinimg.com/ct", "/v3/?event="],
    "Snapchat Pixel": ["tr.snapchat.com", "sc-static.net/scevent"],
    "Microsoft / Bing UET": ["bat.bing.com", "bat.bing.net"],
    "Criteo": ["criteo.com/", "criteo.net/", "static.criteo.net"],
    "Reddit Pixel": ["pixel.reddit.com", "alb.reddit.com", "redditstatic.com/ads"],
    "Quora Pixel": ["q.quora.com"],
    "Taboola": ["taboola.com/libtrc", "trc.taboola.com"],
    "Outbrain": ["outbrain.com/utils", "tr.outbrain.com", "amplify.outbrain.com"],
    "Amazon Ads": ["amazon-adsystem.com", "aax.amazon-adsystem.com"],
    "Yahoo / Verizon": ["analytics.yahoo.com", "sp.analytics.yahoo.com"],
    "Yandex Metrica": ["mc.yandex.ru/metrika", "mc.yandex.com/metrika",
                       "mc.yandex.ru/watch"],
    "VK Pixel": ["vk.com/rtrg", "top-fwz1.mail.ru/counter"],
    "AdRoll": ["d.adroll.com", "s.adroll.com", "pubads.adroll.com"],
    "Spotify Pixel": ["ads.spotify.com/pixel", "ads-pixel.spotify.com"],
    "Adform": ["track.adform.net", "a1.adform.net", "s1.adform.net"],

    # --- Marketing automation / B2B ---
    "Marketo / Munchkin": ["munchkin.marketo.net", "/munchkin.js",
                           ".mktoresp.com/webevents", ".mktoresp.com/ajax",
                           "pages.marketo.com", "app-sjqe.marketo.com",
                           ".marketo.com/index.php/", "/munchkin/"],
    "Eloqua (Oracle)": ["secure.eloqua.com", "img.en25.com",
                         ".t.eloqua.com", ".en25.com/e/",
                         "elqcfg.min.js", "elqimg.com"],
    "Pardot / Salesforce MC": ["pi.pardot.com", "go.pardot.com",
                                "pardot.com/pd.js", "pi.demandbase.com"],
    "HubSpot": ["js.hs-analytics.net", "js.hs-scripts.com", "track.hubspot.com",
                 "forms.hsforms.com", "api.hubapi.com",
                 "js.hsadspixel.net", "js.hs-banner.com"],
    "6sense": ["epsilon.6sense.com", "j.6sc.co", "b.6sc.co",
                "company.6sense.com"],
    "Demandbase": ["api.company-target.com", "tag.demandbase.com",
                    "secure.demandbase.com", "ipv6.company-target.com"],
    "Klaviyo": ["a.klaviyo.com", "static.klaviyo.com", "static-tracking.klaviyo.com",
                 "fivetran.klaviyo.com"],
    "Mailchimp": ["chimpstatic.com", "list-manage.com/track",
                   "mailchi.mp"],
    "Iterable": ["api.iterable.com/api/embedded",
                  "links.iterable.com"],
    "Braze": ["braze.com/api/v3/data", ".braze.com/api/v3",
               "sondheim.iad-01.braze.com", "sdk.iad-01.braze.com"],
    "ActiveCampaign": ["trackcmp.net", ".activehosted.com"],
    "Marketo Measure (Bizible)": ["cdn.bizible.com", "bizibly.com",
                                    "ipv6.bizible.com"],
    "Adobe Marketo Engage": [".marketo.com/rs/",
                              "tracker.marketo.com"],
    "RollWorks": ["d.adroll.com/pixel", "s.adroll.com/r/",
                   "getrollworks.com"],
}

# JS global objects created by marketing pixel libraries.
# Used as a fallback: if CDP/Performance missed the beacon but the vendor
# library loaded and initialised, we detect it via these globals.
PIXEL_JS_GLOBALS = {
    "Meta / Facebook Pixel": ["fbq", "_fbq"],
    "Google Ads": ["gtag", "google_trackConversion"],
    "LinkedIn Insight": ["_linkedin_data_partner_ids", "lintrk"],
    "TikTok Pixel": ["ttq"],
    "X / Twitter Pixel": ["twq"],
    "Pinterest Tag": ["pintrk"],
    "Snapchat Pixel": ["snaptr"],
    "Microsoft / Bing UET": ["UET", "uetq"],
    "Criteo": ["criteo_q"],
    "Reddit Pixel": ["rdt"],
    "Quora Pixel": ["qp"],
    "Taboola": ["_tfa"],
    "Outbrain": ["obApi"],
    "Amazon Ads": ["amzn"],
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
    # Marketo Munchkin ID is "###-XXX-###" (e.g. 165-AEK-754); shows up both in
    # the loader URL (munchkin.marketo.net/{id}/munchkin.js) and as the
    # subdomain of the tracking endpoint ({id}.mktoresp.com).
    "Marketo / Munchkin": [r'munchkin\.marketo\.net/(\d{3}-[A-Z]{3}-\d{3})',
                            r'/(\d{3}-[A-Z]{3}-\d{3})/munchkin\.js',
                            r'(\d{3}-[A-Z]{3}-\d{3})\.mktoresp\.com',
                            r'[?&]munchkinId=(\d{3}-[A-Z]{3}-\d{3})'],
    "Eloqua (Oracle)": [r'secure\.eloqua\.com/visitor/v200/svrGP\?.*?[?&]elqSiteId=(\d+)',
                         r'[?&]elqSiteID=(\d+)',
                         r'/sites/(\d+)/'],
    "Pardot / Salesforce MC": [r'pi\.pardot\.com/(?:visitor|prospect)/.*?[?&]ver=(\d+)',
                                r'[?&]pi_id=(\d+)',
                                r'[?&]account_id=(\d+)'],
    "HubSpot": [r'js\.hs-analytics\.net/analytics/\d+/(\d+)\.js',
                 r'js\.hs-scripts\.com/(\d+)\.js',
                 r'[?&]portalId=(\d+)'],
    "6sense": [r'[?&]cid=([A-Za-z0-9_-]+)'],
    "Demandbase": [r'[?&]key=([A-Za-z0-9]+)'],
    "Drift": [r'/(?:\d+)/([a-z0-9]{20,})/'],
    "Klaviyo": [r'[?&]c=([A-Za-z0-9]+)', r'/onsite/track/.*?company_id=([A-Za-z0-9]+)'],
    "Optimizely": [r'cdn\.optimizely\.com/(?:js|datafiles)/(\d+)\.js'],
    "Yandex Metrica": [r'/watch/(\d+)', r'mc\.yandex\.[a-z]+/.*?[?&]tid=(\d+)'],
    "Adform": [r'[?&]mid=(\d+)', r'[?&]pm=(\d+)'],
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


# Well-known multi-part TLDs where the registrable domain is 3+ parts.
_MULTI_TLDS = {
    "co.uk", "co.jp", "co.kr", "co.in", "co.za", "co.nz", "co.il",
    "com.au", "com.br", "com.cn", "com.mx", "com.sg", "com.hk", "com.tw",
    "com.ar", "com.tr", "com.my", "com.ph", "com.vn", "com.co", "com.pe",
    "org.uk", "org.au", "net.au", "ac.uk", "gov.uk",
    "ne.jp", "or.jp", "ac.jp",
}


def _cookie_domain_for(url):
    """Extract the registrable domain (eTLD+1) for setting cookies.

    OneTrust reads its consent cookie from the root domain. If the page is on
    shop.example.com but we set the cookie on .shop.example.com, the CMP never
    sees it and all pixels fire regardless of the chosen scenario.
    """
    h = _host_of(url)
    if not h:
        return None
    if h.startswith("www."):
        h = h[4:]
    if h.replace(".", "").isdigit() or ":" in h:
        return "." + h
    parts = h.split(".")
    if len(parts) <= 2:
        return "." + h
    for n in (3, 2):
        tld_candidate = ".".join(parts[-(n - 1):])
        if tld_candidate in _MULTI_TLDS and len(parts) > n:
            return "." + ".".join(parts[-(n + 1):])
    return "." + ".".join(parts[-2:])


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
    """Load the URL under a single OneTrust consent `scenario` and return
    {pixel_name: {"count": int, "sources": {}, "ids": set()}} for every
    marketing pixel that fired.

    Captures via THREE independent paths (CDP + Playwright + Performance API)
    and uses JS global detection as a last-resort fallback.
    Source is attributed from the JS initiator stack via CDP."""
    page_host = _host_of(url)
    cdp_records = []           # list of {"url", "init":[...], "type", "post"}
    pw_seen_urls = set()       # Playwright backup: dedup set
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

        # ---- CDP: capture each request URL with its JS initiator stack ----
        cdp = None
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

        # ---- Playwright-level backup capture (catches iframes/workers) ----
        def on_pw_request(request):
            try:
                req_url = request.url
                if req_url and req_url not in pw_seen_urls:
                    pw_seen_urls.add(req_url)
                    pd_str = ""
                    try: pd_str = request.post_data or ""
                    except: pass
                    if not any(r["url"] == req_url for r in cdp_records[-50:]):
                        cdp_records.append({"url": req_url, "init": [],
                                            "type": "pw_backup",
                                            "post": pd_str})
            except Exception:
                pass
        try: page.on("request", on_pw_request)
        except Exception: pass

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

        # ---- Mark the boundary between PASS 1 and PASS 2 ----
        # We keep PASS 1 records (some pixels only fire on first visit) and
        # also capture PASS 2 (return visit with consent state baked in).
        pass1_count = len(cdp_records)

        # ---- PASS 2: RELOAD with the consent state now in effect ----
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
        await asyncio.sleep(10)    # extended wait for late-loading deferred pixels

        # One more networkidle attempt to catch very late beacons
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except: pass

        # ---- FALLBACK: Performance API ----
        # Image beacons and CSS-loaded tracking pixels may not appear in CDP
        # but ARE visible via the Performance Resource Timing API.
        try:
            perf_urls = await page.evaluate(
                "performance.getEntriesByType('resource').map(r => r.name)")
            for pu in (perf_urls or []):
                if pu and pu not in pw_seen_urls:
                    pw_seen_urls.add(pu)
                    if detect_marketing_pixels(pu):
                        if not any(r["url"] == pu for r in cdp_records[-100:]):
                            cdp_records.append({"url": pu, "init": [],
                                                "type": "perf_api",
                                                "post": ""})
        except Exception:
            pass

        # ---- FALLBACK: JS global pixel objects ----
        # Some pixels load entirely via inline scripts (createElement('img'),
        # sendBeacon) which may bypass CDP. If the vendor library initialised,
        # its global object (fbq, ttq, snaptr…) will exist on window.
        try:
            js_code = f"""(() => {{
                const found = [];
                {'; '.join(
                    f"if (typeof window.{g} !== 'undefined') found.push('{pname}')"
                    for pname, globals_list in PIXEL_JS_GLOBALS.items()
                    for g in globals_list
                )};
                return [...new Set(found)];
            }})()"""
            js_detected = await page.evaluate(js_code)
            for pname in (js_detected or []):
                cdp_records.append({
                    "url": f"__js_global_detected__/{pname.replace(' ', '_').replace('/', '_')}",
                    "init": [], "type": "js_global", "post": ""
                })
        except Exception:
            pass

        # Build the request->initiator graph (first load of each script wins)
        init_map = {}
        for rec in cdp_records:
            if rec["url"] not in init_map:
                init_map[rec["url"]] = rec["init"]

        pixels = {}
        for rec in cdp_records:
            req_url = rec["url"]
            # Handle JS global detection synthetic records
            if req_url.startswith("__js_global_detected__/"):
                raw_name = req_url.split("/", 1)[1].replace("_", " ")
                matched_name = None
                for pname in PIXEL_JS_GLOBALS:
                    norm = pname.replace(" ", " ").replace("/", " ")
                    if raw_name.replace("_", " ") in norm or norm in raw_name.replace("_", " "):
                        matched_name = pname
                        break
                if not matched_name:
                    for pname in PIXEL_JS_GLOBALS:
                        if pname.replace(" ", "_").replace("/", "_") == raw_name:
                            matched_name = pname
                            break
                if matched_name:
                    bucket = pixels.setdefault(
                        matched_name, {"count": 0, "sources": {}, "ids": set()})
                    if bucket["count"] == 0:
                        bucket["count"] = 1
                        bucket["sources"]["Detected (JS)"] = 1
                continue

            for pname in detect_marketing_pixels(req_url):
                bucket = pixels.setdefault(
                    pname, {"count": 0, "sources": {}, "ids": set()})
                bucket["count"] += 1

                pid = extract_pixel_id(pname, req_url, rec.get("post", ""))
                if pid:
                    bucket["ids"].add(pid)

                if rec.get("type") == "parser":
                    # Beacon hardcoded directly in the page HTML markup
                    src = "Hardcoded"
                elif rec.get("type") in ("pw_backup", "perf_api"):
                    # Fallback captures don't have initiator stacks
                    src = "Hardcoded"
                else:
                    src = resolve_source(rec["init"], init_map, page_host)
                bucket["sources"][src] = bucket["sources"].get(src, 0) + 1

        try: page.remove_listener("request", on_pw_request)
        except: pass
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

    // Deep element collection — walks the light DOM AND every open shadow
    // root so web-component menus (common on enterprise sites) are scanned.
    function collectAll(root, acc) {
        try {
            root.querySelectorAll('*').forEach(n => {
                acc.push(n);
                if (n.shadowRoot) collectAll(n.shadowRoot, acc);
            });
        } catch(e) {}
        return acc;
    }
    const ALL_ELEMENTS = collectAll(document, []);
    function queryDeep(sel) {
        const out = [];
        for (const el of ALL_ELEMENTS) {
            try { if (el.matches && el.matches(sel)) out.push(el); } catch(e) {}
        }
        return out;
    }

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

    // 1. Gather directly interactive elements (deep: pierces shadow DOM).
    //    CRUCIAL: hidden-but-real links are kept too. Mega-menu / dropdown
    //    links are display:none until their parent is hovered, so a
    //    visible-only scan misses most of the nav tree (observed: 224 of
    //    279 links hidden on an enterprise homepage). The click loop
    //    re-expands menus before clicking, so these are fully testable.
    for (const sel of directSelectors) {
        try {
            queryDeep(sel).forEach(el => {
                if (isVisible(el)) {
                    elementsToScan.add(el);
                } else if (
                    (el.tagName === 'A' && el.getAttribute('href')) ||
                    el.tagName === 'BUTTON'
                ) {
                    elementsToScan.add(el);
                }
            });
        } catch(e) {}
    }

    // 2. Gather from class/id-based selectors — resolve containers to their
    //    interactive children to avoid reporting DIVs/SPANs/LIs as clickables.
    for (const sel of containerSelectors) {
        try {
            queryDeep(sel).forEach(el => {
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
        ALL_ELEMENTS.forEach(el => {
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

            // Prefer visible innerText; fall back to textContent (innerText
            // is EMPTY for display:none elements — hidden menu links need
            // this), then value, placeholder, aria-label, title, alt — so
            // icon-only buttons / image links still get a meaningful label.
            // Icon-only and image links are common in headers/footers (logo,
            // social icons, back-to-top arrows). A bare "[a]" label makes the
            // report unreadable AND unmatchable during beacon attribution,
            // so dig for any human-readable name the markup offers.
            const imgAlt = (sel) => {
                try {
                    const n = el.querySelector && el.querySelector(sel);
                    if (!n) return '';
                    return (n.getAttribute('alt') || n.getAttribute('aria-label')
                            || n.getAttribute('title') || n.textContent || '').trim();
                } catch (e) { return ''; }
            };
            // Look for any of `names` on the element, then on up to 4
            // ancestors — AEM puts the authored title on the wrapping
            // component div, not on the <a> itself.
            // Each attribute name is searched across the whole ancestor chain
            // before moving to the next name, so a descriptive authored title
            // on a wrapper ("Skinvive by Juvederm") beats a generic component
            // type sitting closer on the element itself ("imageComponent").
            const attrUp = (names) => {
                for (const a of names) {
                    try {
                        let n = el, hops = 0;
                        while (n && n !== document.body && hops < 5) {
                            const v = n.getAttribute && n.getAttribute(a);
                            if (v && v.trim() && v.trim() !== 'true' && v.trim() !== 'false') {
                                return v.trim();
                            }
                            n = n.parentElement; hops++;
                        }
                    } catch (e) {}
                }
                return '';
            };
            // "/content/dam/.../close-modal.svg" -> "close modal"
            const fileLabel = (p) => {
                try {
                    if (!p) return '';
                    let base = String(p).split('?')[0].split('/').pop() || '';
                    base = base.replace(/\.(svg|png|jpe?g|gif|webp|coreimg.*)$/i, '');
                    base = base.replace(/[-_.]+/g, ' ').replace(/\s+/g, ' ').trim();
                    return base.length > 1 ? base : '';
                } catch (e) { return ''; }
            };
            // Last resort before "[button]": a readable form of the id, minus
            // the random AEM hash suffix (button-fd5bb9814f -> "button").
            const idLabel = (n) => {
                try {
                    let s = (n.id || '').replace(/-[0-9a-f]{6,}$/i, '');
                    s = s.replace(/[-_]+/g, ' ').trim();
                    return (s && s.toLowerCase() !== 'button' && s.length > 2) ? s : '';
                } catch (e) { return ''; }
            };
            const text = (el.innerText
                || (el.textContent || '').replace(/\s+/g, ' ').trim()
                || el.value
                || el.placeholder
                || el.getAttribute('aria-label')
                || el.getAttribute('title')
                || el.getAttribute('alt')
                || imgAlt('img')
                || imgAlt('picture img')
                || imgAlt('svg title')
                || imgAlt('svg')
                || imgAlt('use')
                || imgAlt('[aria-label]')
                || imgAlt('[alt]')
                // --- AEM / Adobe EMU component metadata ---
                // Enterprise AEM sites render icon buttons and SVG logos with
                // no text, no alt and no aria-label; the human name lives in
                // authoring attributes instead. Without these the report is a
                // wall of "[button]" rows, and — worse — beacon attribution
                // loses its text key and can bind a hit to the wrong element.
                || attrUp(['data-selector-text', 'data-title', 'data-analytics-id',
                           'data-gtm-attribute', 'aria-label', 'title'])
                || fileLabel(attrUp(['data-icon-reference', 'data-asset', 'data-cmp-src']))
                || idLabel(el)
                || '').replace(/\s+/g, ' ').trim().substring(0, 80);
            const href = (el.href || el.getAttribute('href') || '').trim();

            // Tag each element with its page zone so the audit can click
            // header -> footer -> body in a clean order. Many enterprise
            // sites (AEM/Experience Fragments etc.) don't use semantic
            // <footer>/<header> tags at all — just divs with classes like
            // "cmp-experiencefragment--footer". So walk ancestors checking
            // id/class SUBSTRINGS, not a fixed tag/role selector list.
            // Footer is checked first: footer link lists often sit inside
            // a <nav>, so a header check done first would wrongly claim them.
            let zone = 'body';
            try {
                let cur = el;
                while (cur && cur !== document.body && cur !== document.documentElement) {
                    const id = (cur.id || '').toLowerCase();
                    const cls = (typeof cur.className === 'string' ? cur.className : '').toLowerCase();
                    const role = (cur.getAttribute && cur.getAttribute('role') || '').toLowerCase();
                    if (cur.tagName === 'FOOTER' || role === 'contentinfo' || id.includes('footer') || cls.includes('footer')) {
                        zone = 'footer';
                        break;
                    }
                    if (cur.tagName === 'HEADER' || cur.tagName === 'NAV' || role === 'banner'
                        || id.includes('header') || cls.includes('header')
                        || id.includes('navbar') || cls.includes('navbar')
                        || cls.includes('top-nav') || cls.includes('main-nav')) {
                        zone = 'header';
                        break;
                    }
                    cur = cur.parentElement;
                }
            } catch(e) {}

            // Dedup by normalised text + href + ZONE (case-insensitive,
            // whitespace-collapsed). Zone is part of the key so the same
            // link in header AND footer is clicked in BOTH places — link
            // position often changes the analytics payload, so QA needs
            // each occurrence. Empty-text icons keep DOM identity.
            const textKey = text.toLowerCase().replace(/\s+/g, ' ');
            const hrefKey = href.toLowerCase();
            const key = (textKey
                ? 'T:' + textKey + '|H:' + hrefKey
                : (el.tagName + '|H:' + hrefKey + '|I:' + (el.id || ''))) + '|Z:' + zone;
            if (seen.has(key)) return;
            seen.add(key);

            // STAMP a unique, stable handle straight onto the node. The click
            // loop locates elements by [data-tvuid="..."] instead of a CSS
            // path, which removes the whole class of "clicked the wrong
            // element" bugs: non-unique selectors resolving via .first, and
            // header/footer duplicates that share text+href. Idempotent —
            // discovery runs many times (hover/expand passes) and an already
            // stamped node keeps its original uid.
            let uid = '';
            try {
                uid = el.getAttribute('data-tvuid') || '';
                if (!uid) {
                    window.__tvSeq = (window.__tvSeq || 0) + 1;
                    uid = 'tv' + window.__tvSeq;
                    el.setAttribute('data-tvuid', uid);
                }
            } catch(e) {}

            // Download intent: GA4 file_download / download events only fire
            // for these, and they need the download-interception path so the
            // browser doesn't stall on a real file transfer.
            const dlAttr = el.getAttribute && el.getAttribute('download');
            const isDownload = (dlAttr !== null && dlAttr !== undefined)
                || /\.(pdf|docx?|xlsx?|pptx?|zip|csv|txt|rtf|dmg|exe|pkg|mp4|mp3|wav|avi|mov)(\?|#|$)/i.test(href);

            results.push({
                selector: buildSelector(el),
                uid: uid,
                tag: el.tagName,
                text: text || `[${el.tagName.toLowerCase()}]`,
                href: href,
                id: el.id || '',
                zone: zone,
                hidden: !isVisible(el),   // needs menu re-expansion before click
                is_download: !!isDownload,
                target: (el.getAttribute && el.getAttribute('target')) || '',
                className: (typeof el.className === 'string') ? el.className.substring(0, 100) : '',
            });
        } catch(e) {}
    });

    // QA-style: scan up to 2000 elements per page (effectively unlimited for
    // any normal site). Hard cap is just a safety net for runaway lists.
    return results.slice(0, 2000);
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
    for (let round = 0; round < 6; round++) {
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
                if (actedThisRound > 150) break;   // don't blow up huge pages in one round
            }
            if (actedThisRound > 150) break;
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


# Close any consent UI that is sitting on top of the page.
#
# The menu-discovery pass clicks anything that looks like a menu opener, and
# "Cookies Settings" matches `button[aria-expanded="false"]`. That opens the
# OneTrust Preference Center, whose full-screen dark filter then covers the
# entire header for the rest of the audit: every header link hit-tests as
# obscured, gets clicked by a fallback that the site's own handlers ignore,
# and is reported as untracked. Consent has already been granted explicitly
# before this runs, so dismissing the leftover dialog changes no consent
# state — it just gets the overlay out of the way.
# Make a covered element reachable by a REAL mouse click.
#
# Some clicks open a panel that stays on top of other elements for the rest of
# the run. Falling back to a JS .click() reaches the right node but produces an
# untrusted event, and plenty of analytics handlers (Adobe ActivityMap, and
# this site's own site_link/exit_link handlers) simply ignore those — the
# element then gets reported as untracked even though it is tagged. Instead,
# walk the stack of elements sitting over the target and switch off their
# pointer-events just long enough to deliver a trusted click to the real
# element. Nothing is hidden or removed, and it is put back immediately after.
PIERCE_OVERLAY_JS = r"""
(e) => {
    // Always undo a previous pierce first. If an earlier one never got its
    // restore (an exception between click and cleanup), overwriting the
    // bookkeeping list would strand those nodes with pointer-events:none for
    // the rest of the audit and quietly break every later element.
    try {
        for (const [n, val, pri] of (window.__tvPierced || [])) {
            if (val) n.style.setProperty('pointer-events', val, pri || '');
            else n.style.removeProperty('pointer-events');
        }
    } catch (err) {}
    window.__tvPierced = [];

    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) return {ok: false, reason: 'no-box'};
    const pts = [
        [r.x + r.width / 2,    r.y + r.height / 2],
        [r.x + r.width * 0.25, r.y + r.height / 2],
        [r.x + r.width * 0.75, r.y + r.height / 2],
    ].filter(([x, y]) => x >= 0 && y >= 0 && x <= innerWidth && y <= innerHeight);
    if (!pts.length) return {ok: false, reason: 'offscreen'};

    const touched = [];
    const hits = (n) => n && (n === e || e.contains(n) || n.contains(e));
    for (const [x, y] of pts) {
        for (let guard = 0; guard < 10; guard++) {
            const top = document.elementFromPoint(x, y);
            if (hits(top)) {
                window.__tvPierced = touched;
                return {ok: true, x: x, y: y, pierced: touched.length};
            }
            if (!top || top === document.documentElement || top === document.body) break;
            touched.push([top, top.style.getPropertyValue('pointer-events'),
                          top.style.getPropertyPriority('pointer-events')]);
            top.style.setProperty('pointer-events', 'none', 'important');
        }
    }
    window.__tvPierced = touched;
    return {ok: false, reason: 'unreachable', pierced: touched.length};
}
"""

RESTORE_OVERLAY_JS = r"""
(() => {
    const t = window.__tvPierced || [];
    for (const [n, val, pri] of t) {
        try {
            if (val) n.style.setProperty('pointer-events', val, pri || '');
            else n.style.removeProperty('pointer-events');
        } catch (e) {}
    }
    window.__tvPierced = [];
    return t.length;
})()
"""

CLOSE_CONSENT_UI_JS = r"""
(() => {
    let closed = 0;
    try {
        if (window.OneTrust && typeof window.OneTrust.Close === 'function') {
            window.OneTrust.Close();
            closed++;
        }
    } catch (e) {}
    const overlays = [
        '#onetrust-pc-sdk', '.onetrust-pc-dark-filter', '.ot-pc-dark-filter',
        '#onetrust-banner-sdk', '#onetrust-consent-sdk .onetrust-pc-dark-filter',
        '.ot-fade-in.onetrust-pc-dark-filter',
        '#truste-consent-track', '#cookie-consent-overlay',
        '[id*="cookie"][class*="overlay"]', '[class*="cmp-overlay"]',
    ];
    for (const sel of overlays) {
        try {
            document.querySelectorAll(sel).forEach(n => {
                const st = window.getComputedStyle(n);
                if (st && st.display !== 'none' && st.visibility !== 'hidden') {
                    n.style.setProperty('display', 'none', 'important');
                    closed++;
                }
            });
        } catch (e) {}
    }
    return closed;
})()
"""


# Instrument the page's tracking APIs so click-tracking is detected even when
# the network beacon is deferred to the NEXT page view (Adobe ActivityMap),
# batched by a TMS, or suppressed by consent state. Sites like Fresenius track
# every link via ActivityMap — the click stores an s_sq cookie and the beacon
# only rides on the next page load, so pure network capture reports
# "no tracking" for links that ARE tracked. Wrapping the APIs catches the
# call itself, at the moment of click:
#   s.tl(...)                    -> Adobe custom link tracking
#   utag.link(...)               -> Tealium click events
#   gtag('event', ...)           -> GA4 API events
#   dataLayer.push({event:...})  -> GTM/GA4 click triggers
INSTRUMENT_TRACKING_JS = r"""
(() => {
    if (window.__qaTrackInstalled) return true;
    window.__qaTrackInstalled = true;
    window.__qaTrackCaptures = [];
    const push = (type, detail) => {
        try {
            window.__qaTrackCaptures.push({type: type, detail: detail, ts: Date.now()});
            if (window.__qaTrackCaptures.length > 500) window.__qaTrackCaptures.shift();
        } catch(e) {}
    };
    const safeJson = (o) => {
        try { return JSON.parse(JSON.stringify(o)); } catch(e) { return String(o); }
    };

    // Adobe AppMeasurement s.tl — re-check periodically, s is often defined
    // late by the tag manager (Tealium/Launch).
    const wrapS = () => {
        try {
            const s = window.s;
            if (s && typeof s.tl === 'function' && !s.__qaWrapped) {
                const orig = s.tl.bind(s);
                s.__qaWrapped = true;
                s.tl = function(obj, linkType, linkName, vars, doneAction) {
                    push('s.tl', {link_type: String(linkType || ''), link_name: String(linkName || '')});
                    return orig(obj, linkType, linkName, vars, doneAction);
                };
            }
        } catch(e) {}
    };
    // Tealium utag.link
    const wrapU = () => {
        try {
            const u = window.utag;
            if (u && typeof u.link === 'function' && !u.__qaWrapped) {
                const orig = u.link.bind(u);
                u.__qaWrapped = true;
                u.link = function(data, cb, uids) {
                    push('utag.link', safeJson(data || {}));
                    return orig(data, cb, uids);
                };
            }
        } catch(e) {}
    };
    // GA4 gtag
    const wrapG = () => {
        try {
            if (typeof window.gtag === 'function' && !window.gtag.__qaWrapped) {
                const orig = window.gtag;
                const wrapped = function() {
                    try {
                        if (arguments[0] === 'event') {
                            push('gtag', {event: String(arguments[1] || ''), params: safeJson(arguments[2] || {})});
                        }
                    } catch(e) {}
                    return orig.apply(this, arguments);
                };
                wrapped.__qaWrapped = true;
                window.gtag = wrapped;
            }
        } catch(e) {}
    };
    // GTM dataLayer.push — only capture entries with an `event` key
    const wrapDL = () => {
        try {
            const dl = window.dataLayer;
            if (dl && typeof dl.push === 'function' && !dl.__qaWrapped) {
                const orig = dl.push.bind(dl);
                dl.__qaWrapped = true;
                dl.push = function() {
                    try {
                        const a = arguments[0];
                        if (a && typeof a === 'object' && !Array.isArray(a) && a.event) {
                            push('dataLayer', safeJson(a));
                        }
                    } catch(e) {}
                    return orig.apply(null, arguments);
                };
            }
        } catch(e) {}
    };

    // Adobe Client Data Layer (adobeDataLayer) — the event bus AEM Core
    // Components / EMU sites push `cmp:click` and custom events into. It is
    // NOT window.dataLayer, so the GTM wrap above never saw it. On AEM sites
    // this is often the only click signal that fires at click time.
    const wrapACDL = () => {
        try {
            const adl = window.adobeDataLayer;
            if (adl && typeof adl.push === 'function' && !adl.__qaWrapped) {
                const orig = adl.push.bind(adl);
                adl.__qaWrapped = true;
                adl.push = function() {
                    try {
                        for (const a of arguments) {
                            if (a && typeof a === 'object' && !Array.isArray(a)) {
                                if (a.event || a['xdm:eventType']) push('adobeDataLayer', safeJson(a));
                            }
                        }
                    } catch(e) {}
                    return orig.apply(null, arguments);
                };
            }
        } catch(e) {}
    };

    // CLICK WITNESS — records what the browser actually delivered the click
    // to. Coordinate-based CDP clicks can land on an overlay / sticky header
    // instead of the intended node; without this the audit would happily
    // report another element's beacons under this element's row. Capture
    // phase + composedPath so shadow-DOM targets are seen too.
    try {
        if (!window.__qaWitnessInstalled) {
            window.__qaWitnessInstalled = true;
            window.__qaLastClick = null;
            document.addEventListener('click', (e) => {
                try {
                    const path = (e.composedPath && e.composedPath()) || [];
                    const uids = [];
                    for (const n of path) {
                        if (n && n.getAttribute) {
                            const u = n.getAttribute('data-tvuid');
                            if (u) uids.push(u);
                        }
                    }
                    const t = e.target;
                    window.__qaLastClick = {
                        uids: uids,
                        tag: (t && t.tagName) || '',
                        text: ((t && (t.innerText || t.textContent)) || '').replace(/\s+/g, ' ').trim().slice(0, 60),
                        ts: Date.now()
                    };
                } catch(err) {}
            }, true);
        }
    } catch(e) {}

    const wrapAll = () => { wrapS(); wrapU(); wrapG(); wrapDL(); wrapACDL(); };
    wrapAll();
    window.__qaTrackTimer = setInterval(wrapAll, 2000);
    return true;
})()
"""

# Read + clear the click witness. Returns what the last real click actually
# hit, so the audit can prove the intended element received the event.
READ_CLEAR_WITNESS_JS = r"""
(() => {
    const w = window.__qaLastClick || null;
    window.__qaLastClick = null;
    return w;
})()
"""

HARVEST_TRACKING_JS = r"""
(() => {
    const c = window.__qaTrackCaptures || [];
    window.__qaTrackCaptures = [];
    return c;
})()
"""

# Look at what the click pushed WITHOUT consuming it. Used right after a click
# to decide whether a network beacon is still coming (and therefore whether to
# keep the capture window open) before the real harvest runs.
PEEK_TRACKING_JS = r"""
(() => (window.__qaTrackCaptures || []).slice())()
"""

# How long a click's capture window stays open.
#   IDLE  — nothing fired at the API level; close fast, most elements are untracked.
#   MAX   — the API said an event fired, so a network beacon is on its way.
#           Tag managers commonly defer /g/collect by 5s+, and a window that
#           closes earlier pushes the beacon into the NEXT element's results.
CLICK_IDLE_WAIT = 1.8
CLICK_BEACON_MAX_WAIT = 8.0
# Drain at the very end of the page so the last elements' deferred beacons
# still land before reconciliation runs.
FINAL_DRAIN_WAIT = 9.0

# Read AND clear Adobe ActivityMap's s_sq cookie. ActivityMap records the
# clicked link into s_sq at click time and only transmits it with the NEXT
# page view — since the audit blocks navigation, the cookie itself is the
# proof that the click was tracked. Cleared after reading so each click
# starts with a clean slate.
READ_CLEAR_SSQ_JS = r"""
(() => {
    const m = document.cookie.match(/(?:^|;\s*)s_sq=([^;]*)/);
    if (!m || !m[1]) return '';
    let val = '';
    try { val = decodeURIComponent(m[1]); } catch(e) { val = m[1]; }
    const host = location.hostname;
    const parts = host.split('.');
    const domains = [host, '.' + host];
    if (parts.length > 2) {
        domains.push('.' + parts.slice(-2).join('.'));
        domains.push('.' + parts.slice(-3).join('.'));
    }
    for (const d of domains) {
        document.cookie = 's_sq=; path=/; domain=' + d + '; expires=Thu, 01 Jan 1970 00:00:00 GMT';
    }
    document.cookie = 's_sq=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT';
    return val;
})()
"""


# ===== CLICK ATTRIBUTION SUPPORT =====
# Page-level events that fire on a timer, on scroll, or on tag-manager
# bootstrap. They are NOT caused by the element under test, but they land in
# whatever capture window happens to be open — which is how a scroll event
# ends up printed under some innocent button. Filtered out of click results.
NOISE_EVENTS = {
    'scroll', 'spa_scroll', 'gtm.scrolldepth', 'user_engagement', 'page_view',
    'session_start', 'first_visit', 'gtm.js', 'gtm.dom', 'gtm.load', 'gtm.init',
    'gtm.triggergroup', 'gtm.historychange', 'gtm.timer', 'onetrustloaded',
    'optanonloaded', 'onetrustgroupsupdated', 'optanonconsentupdated',
    'gtm.scrollDepth'.lower(), 'spa_pageview', 'virtual_page_view',
    'cmp:show', 'cmp:loaded', 'consent_update',
}


def _is_noise_event(name):
    return str(name or '').strip().lower() in NOISE_EVENTS


# Parameter keys whose value identifies WHICH element a beacon belongs to.
# Every GA4 click beacon carries the clicked link's own url/text, so a beacon
# can be bound to its true element by content instead of by arrival time —
# which is what makes attribution correct even when a tag manager defers the
# beacon by several seconds into a later element's window.
_LINK_URL_KEYS = ('ep.link_url', 'ep.file_name', 'ep.outbound_url', 'link_url',
                  'gtm.elementurl', 'linkhref', 'ep.page_location')
_LINK_TEXT_KEYS = ('ep.link_text', 'ep.download_file_name', 'ep.file_name',
                   'link_text', 'gtm.elementtext', 'linktext')


def beacon_identity(ev):
    """Extract (link_url, link_text) that identify the element a tracking
    event describes. Works on our parsed GA4 event dicts and on raw
    dataLayer payloads. Returns ('','') when the event carries no element
    identity (e.g. a bare `click` outbound hit)."""
    if not isinstance(ev, dict):
        return '', ''
    params = ev.get('params') if isinstance(ev.get('params'), dict) else {}
    merged = {}
    for src in (params, ev):
        if isinstance(src, dict):
            for k, v in src.items():
                if isinstance(v, (str, int, float)):
                    merged[str(k).lower()] = str(v)
    url = ''
    for k in _LINK_URL_KEYS:
        if merged.get(k):
            url = merged[k]
            break
    txt = ''
    for k in _LINK_TEXT_KEYS:
        if merged.get(k):
            txt = merged[k]
            break
    return url.strip(), txt.strip()


def _attr_key_url(u):
    """Normalise a url for element<->beacon matching. Keeps the fragment,
    because anchor links (#isi) are distinct QA targets, but drops scheme,
    www and trailing slash so relative/absolute forms unify."""
    s = str(u or '').strip().lower()
    if not s:
        return ''
    frag = ''
    if '#' in s:
        s, frag = s.split('#', 1)
        frag = '#' + frag
    s = re.sub(r'^https?://', '', s)
    s = re.sub(r'^www\.', '', s)
    s = s.split('?')[0].rstrip('/')
    return s + frag


def _url_keys(u, base=""):
    """All the normalised forms a url might be written as, for matching.

    A beacon reports the link exactly as the markup wrote it — "#isi",
    "/science", "//cdn/x" — while discovery stored the browser-resolved
    absolute href. Comparing one form against the other silently fails, which
    leaves a genuinely tracked element looking untracked and lets its beacon
    drift onto a neighbour. Emitting both the absolute key and the
    path+fragment key lets either form match.
    """
    s = str(u or '').strip()
    if not s:
        return set()
    keys = set()
    abs_form = s
    if base:
        try:
            from urllib.parse import urljoin
            abs_form = urljoin(base, s)
        except Exception:
            abs_form = s
    for form in (s, abs_form):
        k = _attr_key_url(form)
        if k:
            keys.add(k)
            # host-less form: "host.com/path#frag" -> "/path#frag"
            m = re.match(r'^[^/#?]+\.[^/#?]+(/.*|#.*)?$', k)
            if m:
                tail = m.group(1) or '/'
                keys.add(tail if tail.startswith(('/', '#')) else '/' + tail)
            elif k.startswith(('/', '#')):
                keys.add(k)
    return {k for k in keys if k and k not in ('/', '')}


def _attr_key_text(t):
    return re.sub(r'\s+', ' ', str(t or '')).strip().lower()


_ZONE_KEYS = ('ep.link_location', 'link_location', 'ep.file_location',
              'file_location', 'linklocation', 'downloadlocation')


def beacon_zone(ev):
    """The page region a beacon says the click happened in ('header' /
    'footer' / ''). Sites that tag link_location give us a free tie-breaker
    between the same link duplicated in the header and the footer — without
    it, one twin absorbs the other's events."""
    if not isinstance(ev, dict):
        return ''
    params = ev.get('params') if isinstance(ev.get('params'), dict) else {}
    for src in (params, ev):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            if str(k).lower() in _ZONE_KEYS and isinstance(v, str) and v.strip():
                z = v.strip().lower()
                if 'header' in z or 'nav' in z:
                    return 'header'
                if 'footer' in z:
                    return 'footer'
                if 'body' in z or 'main' in z or 'content' in z:
                    return 'body'
    return ''


# Params that change on every hit and say nothing about the event itself.
# Excluded when deciding whether two hits are "the same event".
_VOLATILE_PARAMS = {'hit_timestamp', 'ep.hit_timestamp', '_et', 'et',
                    'client_id_config', 'ep.client_id_config', '_p', 'seg',
                    '_s', 'sid', 'sct', 'ep.page_url', 'page_url'}


def consolidate_ga4_events(events):
    """Collapse hits that are the same event sent to several GA4 properties.

    Sites commonly dual-tag: one click fires `exit_link` to two measurement
    IDs, so a raw list reads "exit_link, exit_link, exit_link" and looks like
    a bug. Group by event name + meaningful params, and report the property
    IDs together, so each row shows what actually happened once.
    """
    grouped = []
    index = {}
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        name = str(ev.get("event") or "")
        params = ev.get("params") if isinstance(ev.get("params"), dict) else {}
        sig_items = tuple(sorted(
            (str(k), str(v)) for k, v in params.items()
            if str(k).lower() not in _VOLATILE_PARAMS
        ))
        mid = str(ev.get("measurement_id") or "")
        # API-level captures (dataLayer/ACDL/gtag) are a different kind of
        # evidence than a network beacon — never merge the two together.
        is_api = mid.startswith("(")
        key = (name, sig_items, is_api)
        if key in index:
            slot = grouped[index[key]]
            if mid and mid not in slot["_ids"]:
                slot["_ids"].append(mid)
            slot["hit_count"] += 1
            continue
        index[key] = len(grouped)
        grouped.append({
            "event": name,
            "_ids": [mid] if mid else [],
            "params": dict(params),
            "hit_count": 1,
        })

    out = []
    for g in grouped:
        ids = g.pop("_ids")
        g["measurement_ids"] = ids
        # Keep `measurement_id` a plain string so existing report/Excel
        # rendering keeps working unchanged.
        g["measurement_id"] = " + ".join(ids) if ids else ""
        out.append(g)
    return out


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

        # Which element's click window is open right now. Every captured
        # request is stamped with it, so attribution is an explicit label
        # rather than an array slice that a late beacon can slide out of.
        # -1 means "no click in progress" (page load, menu expansion, idle).
        current_cid = {"v": -1}

        def _push_req(req_url, post, ts):
            if not req_url:
                return
            key = (req_url, round(ts, 2))
            if key in seen_req_keys:
                return
            seen_req_keys.add(key)
            all_requests.append({"url": req_url, "post": post or "", "ts": ts,
                                 "cid": current_cid["v"]})

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
                # Zone + frame are part of identity: the same link in header
                # AND footer (or inside an iframe) is a separate QA case.
                key += "|Z:" + (el.get("zone") or "body") + "|F:" + (el.get("frame_url") or "")
                el["fp"] = key          # stable fingerprint, survives re-stamping
                if key not in elements_by_key:
                    elements_by_key[key] = el
                else:
                    # Keep the richest record: a later pass may see the element
                    # while it is visible (real uid + accurate zone) where the
                    # first pass caught it collapsed.
                    prev = elements_by_key[key]
                    if not prev.get("uid") and el.get("uid"):
                        prev["uid"] = el["uid"]

        # Pass A: initial discovery (default page state, no special hovers).
        try: _add_batch(await page.evaluate(DISCOVER_CLICKABLES_JS))
        except Exception: pass

        # Pass B: hover each top-nav item ONE AT A TIME, brief settle, then
        # discover what's now visible — sub-menus revealed under that item.
        # While that menu is open, DRILL DEEPER: hover any submenu items that
        # look like parents themselves so nested flyouts (sub-sub-menus)
        # render and get discovered too. Time-budgeted so a pathological
        # mega-menu can't stall the whole audit.
        try:
            nav_locator = page.locator(
                'header > * a, header > * button, header nav a, header nav button, '
                'nav > a, nav > button, nav > ul > li, nav > ul > li > a, nav > ul > li > button, '
                '[role="menuitem"], [class*="has-submenu"], [class*="has-children"]'
            )
            sub_parent_locator = page.locator(
                '[aria-haspopup="true"], [class*="has-submenu"], [class*="has-children"], '
                'li:has(> ul) > a, li:has(> div ul) > a, [class*="dropdown"] > a, '
                '[class*="flyout"] > a, li[class*="parent"] > a'
            )
            nav_count = min(await nav_locator.count(), 80)
            passb_deadline = time.time() + 150   # hard budget for pass B
            for ni in range(nav_count):
                if time.time() > passb_deadline:
                    break
                try:
                    await nav_locator.nth(ni).hover(timeout=400, force=True, no_wait_after=True)
                    await asyncio.sleep(0.35)        # let sub-menu render
                    before = len(elements_by_key)
                    _add_batch(await page.evaluate(DISCOVER_CLICKABLES_JS))
                    # Only drill deeper when this hover actually revealed
                    # something new — otherwise it's not a menu parent.
                    if len(elements_by_key) > before:
                        try:
                            sub_count = min(await sub_parent_locator.count(), 20)
                            for si in range(sub_count):
                                if time.time() > passb_deadline:
                                    break
                                try:
                                    s_el = sub_parent_locator.nth(si)
                                    if not await s_el.is_visible(timeout=120):
                                        continue
                                    await s_el.hover(timeout=300, force=True, no_wait_after=True)
                                    await asyncio.sleep(0.25)   # let flyout render
                                    _add_batch(await page.evaluate(DISCOVER_CLICKABLES_JS))
                                except Exception:
                                    pass
                        except Exception:
                            pass
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
                count = min(await page.locator(sel).count(), 12)
            except Exception:
                count = 0
            for i in range(count):
                try:
                    el = page.locator(sel).nth(i)
                    if not await el.is_visible(timeout=300):
                        continue
                    # Never open the cookie/consent UI while hunting for
                    # menus: "Cookies Settings" matches these opener
                    # selectors, and the preference center it opens covers
                    # the header for the rest of the audit.
                    try:
                        if await el.evaluate("""e => {
                            let n = e;
                            while (n && n !== document.body) {
                                const id = (n.id || '').toLowerCase();
                                const cls = (typeof n.className === 'string' ? n.className : '').toLowerCase();
                                if (/cookie|consent|onetrust|optanon|^ot-|privacy|truste/.test(id)
                                    || /cookie|consent|onetrust|optanon|ot-pc|ot-sdk|privacy|truste/.test(cls)) {
                                    return true;
                                }
                                n = n.parentElement;
                            }
                            const t = (e.innerText || e.getAttribute('aria-label') || '').toLowerCase();
                            return /cookie|consent|privacy choices|manage choices/.test(t);
                        }"""):
                            continue
                    except Exception:
                        pass
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
                    for _round in range(8):
                        before = len(elements_by_key)
                        round_clicked = 0
                        for ssel in sub_selectors:
                            try:
                                sc = min(await page.locator(ssel).count(), 40)
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

        # Same-origin iframe discovery — video embeds, form widgets and CMP
        # iframes contain clickables too. Cross-origin frames can't be read
        # (browser security), those throw and are skipped harmlessly.
        try:
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                try:
                    batch = await fr.evaluate(DISCOVER_CLICKABLES_JS)
                    for b in batch:
                        b["frame_url"] = fr.url
                    if batch:
                        sys.stdout.write(f"[{index}/{total}] Found {len(batch)} clickable(s) inside iframe {fr.url[:60]}\n")
                        sys.stdout.flush()
                    _add_batch(batch)
                except Exception:
                    pass
        except Exception:
            pass

        # KEEP navigation blocked during the click loop. preventDefault only
        # stops the browser-default navigation — GA4 / GTM / Adobe click
        # handlers still run in the same event tick (s.tl fires on the click
        # itself, not on the navigation). Staying on the page keeps expanded
        # menus intact, skips a full page reload per link (3-4x faster), and
        # prevents the failure where one bad navigation kills every element
        # after it (observed: 255/302 skipped after one nav-back failed).
        # JS-driven redirects (window.location=...) can still escape the
        # blocker — the per-element health check below recovers from those.
        try:
            await page.evaluate(BLOCK_NAVIGATION_JS)
        except Exception:
            pass

        # Instrument tracking APIs (s.tl / utag.link / gtag / dataLayer) and
        # clear any ActivityMap s_sq left over from the discovery clicks so
        # the first audited element starts clean.
        try:
            await page.evaluate(INSTRUMENT_TRACKING_JS)
            await page.evaluate(READ_CLEAR_SSQ_JS)
            await page.evaluate(HARVEST_TRACKING_JS)
        except Exception:
            pass

        # Discovery may have left a consent dialog open on top of the page.
        try:
            _cc = await page.evaluate(CLOSE_CONSENT_UI_JS)
            if _cc:
                sys.stdout.write(f"[{index}/{total}] Dismissed {_cc} consent overlay(s) left open by discovery\n")
                sys.stdout.flush()
                await asyncio.sleep(0.4)
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

        async def _ensure_on_page():
            """Recover the audit page if a click escaped the nav blocker.
            Re-creates the page (with CDP + listeners) if it crashed/closed,
            re-navigates with retries if the URL drifted. Returns True if a
            reload happened (menus will be collapsed again)."""
            nonlocal page, cdp
            try:
                if page.is_closed():
                    page = await context.new_page()
                    try: page.on("request", on_pw_request)
                    except Exception: pass
                    cdp = await context.new_cdp_session(page)
                    await cdp.send("Network.enable")
                    cdp.on("Network.requestWillBeSent", on_req_persistent)
            except Exception:
                pass
            cur = ""
            try:
                cur = page.url
            except Exception:
                pass
            if _norm_url(cur) == _norm_url(original_url):
                return False
            for attempt in range(3):
                try:
                    await page.goto(original_url, wait_until="domcontentloaded", timeout=25000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=6000)
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
                    break
                except Exception:
                    await asyncio.sleep(2.0 * (attempt + 1))
            try:
                await page.evaluate(BLOCK_NAVIGATION_JS)
            except Exception:
                pass
            # A reload wipes the API wraps — re-instrument.
            try:
                await page.evaluate(INSTRUMENT_TRACKING_JS)
                await page.evaluate(READ_CLEAR_SSQ_JS)
                await page.evaluate(HARVEST_TRACKING_JS)
            except Exception:
                pass
            # A reload also wipes the data-tvuid stamps, which are how the
            # click loop guarantees it clicks the intended node. Re-run
            # discovery to re-stamp, then re-point each pending element at
            # its NEW uid via the fingerprint (which is reload-stable).
            try:
                fresh = await page.evaluate(DISCOVER_CLICKABLES_JS)
                fp_to_uid = {}
                for f in fresh or []:
                    t = re.sub(r'\s+', ' ', (f.get("text") or "").strip().lower())
                    h = (f.get("href") or "").strip().lower()
                    if t:
                        k = "T:" + t + "|H:" + h
                    else:
                        k = (f.get("tag", "") + "|H:" + h + "|I:" + (f.get("id") or ""))
                    k += "|Z:" + (f.get("zone") or "body") + "|F:" + (f.get("frame_url") or "")
                    if f.get("uid"):
                        fp_to_uid[k] = f["uid"]
                for pending in elements:
                    nu = fp_to_uid.get(pending.get("fp"))
                    if nu:
                        pending["uid"] = nu
            except Exception:
                pass
            return True

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
                    "zone": elem.get("zone", "body"),
                    "frame_url": elem.get("frame_url", ""),
                    "uid": elem.get("uid", ""),
                    "is_download": bool(elem.get("is_download")),
                },
                "ga4_events": [],
                "adobe_calls": [],
                "adobe_websdk": [],
                "other_analytics": [],
                "datalayer_events": [],   # instant, click-time ground truth
                "network_requests": [],   # RAW capture: every request in this click's window
                "network_count": 0,
                "has_tracking": False,
                "skipped": False,
                "skip_reason": "",
                "click_method": "",
                "click_verified": False,  # witness confirmed the intended node was hit
                "blocked_by": "",         # element covering this one, if any
                "click_method_note": "",
                "attribution": "",        # how the events were bound: witness/content/window
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
                        # Drop page-level noise (scroll depth, engagement pings,
                        # page_view). These fire on timers and would otherwise be
                        # printed under whichever button happened to be under test.
                        if ev.get("event") and not _is_noise_event(ev["event"]):
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

            # HEALTH CHECK: if a previous click escaped the nav blocker
            # (JS redirect / window.open), recover before touching locators —
            # otherwise every remaining element fails on a dead page.
            try:
                await _ensure_on_page()
            except Exception:
                pass

            # Resolve the target frame — elements discovered inside a
            # same-origin iframe must be located through that frame.
            target = page
            if elem.get("frame_url"):
                try:
                    for fr in page.frames:
                        if fr.url == elem["frame_url"]:
                            target = fr
                            break
                except Exception:
                    pass

            # PRIMARY: the uid stamped onto the node at discovery time. This
            # is a 1:1 handle — no ambiguity, no .first picking a different
            # node, and header/footer twins that share text+href stay
            # distinct. Everything below is fallback for when a reload wiped
            # the stamps before re-stamping could run.
            uid = elem.get("uid") or ""
            if uid:
                try:
                    cand = target.locator(f'[data-tvuid="{uid}"]')
                    if await cand.count() > 0:
                        el = cand.first
                except Exception:
                    pass

            # FALLBACK 0: the discovery selector.
            if el is None:
                try:
                    cand = target.locator(elem["selector"]).first
                    if await cand.count() > 0:
                        el = cand
                except Exception:
                    pass
            # FALLBACK 1: locate by href — selectors can go stale, but the
            # href is stable. NOTE: el.href (what discovery stored) is the
            # ABSOLUTE url while the DOM attribute is often RELATIVE, and
            # CSS [href=...] matches the raw attribute — so try the absolute
            # form, the path-only form, and an ends-with match.
            if el is None and (elem.get("href") or "").startswith("http"):
                try:
                    from urllib.parse import urlparse as _hp
                    full = elem["href"]
                    path = _hp(full).path or ""
                    cand_sels = [f'a[href="{full.replace(chr(34), "")}"]']
                    if path and path != "/":
                        cand_sels.append(f'a[href="{path}"]')
                        cand_sels.append(f'a[href$="{path}"]')
                    for cs in cand_sels:
                        try:
                            cand = target.locator(cs).first
                            if await cand.count() > 0:
                                el = cand
                                break
                        except Exception:
                            pass
                except Exception:
                    pass
            # FALLBACK 2: locate by exact visible text.
            if el is None:
                txt = (elem.get("text") or "").strip()
                if txt and not txt.startswith("["):
                    try:
                        cand = target.get_by_text(txt, exact=True).first
                        if await cand.count() > 0:
                            el = cand
                    except Exception:
                        pass

            if el is not None:
                # Check if element is visible — after navigating back,
                # submenus will be collapsed. Expand parent menus first.
                try:
                    is_visible = await el.is_visible(timeout=500)
                except Exception:
                    is_visible = False

                if not is_visible:
                    # Re-open collapsed menus/accordions first (they re-collapse
                    # after every navigate-back), then walk up the DOM tree.
                    try:
                        await page.evaluate(EXPOSE_HIDDEN_JS)
                        await asyncio.sleep(0.4)
                    except Exception:
                        pass
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
                await asyncio.sleep(0.35)

                # ---- DRAIN before the click ----
                # Everything above (menu re-expansion, parent hovers, JS
                # .click() on collapsed parents, scrolling) fires the site's
                # OWN tracking. Previously the capture window opened before
                # all that, so a parent menu item's beacons were reported
                # under its child. Open the window only now, and throw away
                # anything the expansion produced.
                try:
                    await page.evaluate(HARVEST_TRACKING_JS)     # discard
                    await page.evaluate(READ_CLEAR_WITNESS_JS)   # discard
                    await page.evaluate(READ_CLEAR_SSQ_JS)       # discard
                except Exception:
                    pass
                await asyncio.sleep(0.15)
                req_bookmark = len(all_requests)
                current_cid["v"] = ei

                # Resolve a click point that actually HITS this element.
                # A centre-point CDP click is the highest-fidelity input
                # (isTrusted:true, which Adobe ActivityMap and some GTM
                # triggers require) but it is coordinate-based: a sticky
                # header, cookie bar or overlay sitting on top means the
                # click lands on a DIFFERENT element while the audit records
                # it as a success. Hit-test first, and only take the trusted
                # path when the topmost node at that point really is ours.
                hit = None
                try:
                    hit = await el.evaluate("""(e) => {
                        const r = e.getBoundingClientRect();
                        if (!r.width || !r.height) return {ok: false, reason: 'no-box'};
                        const cands = [
                            [r.x + r.width / 2,   r.y + r.height / 2],
                            [r.x + r.width * 0.25, r.y + r.height / 2],
                            [r.x + r.width * 0.75, r.y + r.height / 2],
                            [r.x + Math.min(6, r.width / 2), r.y + Math.min(6, r.height / 2)],
                        ];
                        for (const [x, y] of cands) {
                            if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
                            const top = document.elementFromPoint(x, y);
                            if (top && (top === e || e.contains(top) || top.contains(e))) {
                                return {ok: true, x: x, y: y};
                            }
                        }
                        return {ok: false, reason: 'obscured'};
                    }""")
                except Exception:
                    hit = None

                # If something is covering the element, try to clear it before
                # falling back. A previous element in this very loop is the
                # usual culprit: clicking a "More from ..." style toggle
                # leaves its panel open on top of the links that come next.
                if not (hit and hit.get("ok")):
                    try:
                        await page.evaluate(CLOSE_CONSENT_UI_JS)
                        await page.keyboard.press("Escape")
                        await asyncio.sleep(0.25)
                        await el.scroll_into_view_if_needed(timeout=1200)
                        await asyncio.sleep(0.2)
                        hit = await el.evaluate("""(e) => {
                            const r = e.getBoundingClientRect();
                            if (!r.width || !r.height) return {ok: false, reason: 'no-box'};
                            const cands = [
                                [r.x + r.width / 2,   r.y + r.height / 2],
                                [r.x + r.width * 0.25, r.y + r.height / 2],
                                [r.x + r.width * 0.75, r.y + r.height / 2],
                            ];
                            let blocker = '';
                            for (const [x, y] of cands) {
                                if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
                                const top = document.elementFromPoint(x, y);
                                if (top && (top === e || e.contains(top) || top.contains(e))) {
                                    return {ok: true, x: x, y: y};
                                }
                                if (top && !blocker) {
                                    blocker = top.tagName + (top.id ? '#' + top.id : '')
                                            + '.' + String(top.className || '').slice(0, 50);
                                }
                            }
                            return {ok: false, reason: 'obscured', blocker: blocker};
                        }""")
                    except Exception:
                        pass
                    if hit and not hit.get("ok") and hit.get("blocker"):
                        el_res["blocked_by"] = hit["blocker"]

                # Still covered: neutralise the covering elements so the
                # trusted click can be delivered to the real target.
                pierced = False
                if not (hit and hit.get("ok")):
                    try:
                        hit = await el.evaluate(PIERCE_OVERLAY_JS)
                        pierced = True
                        if hit and hit.get("ok") and hit.get("pierced"):
                            el_res["click_method_note"] = f"pierced {hit['pierced']} overlay(s)"
                    except Exception:
                        pass

                if hit and hit.get("ok"):
                    try:
                        await cdp.send("Input.dispatchMouseEvent", {
                            "type": "mouseMoved", "x": hit["x"], "y": hit["y"]})
                        await cdp.send("Input.dispatchMouseEvent", {
                            "type": "mousePressed", "x": hit["x"], "y": hit["y"],
                            "button": "left", "clickCount": 1})
                        await asyncio.sleep(0.05)
                        await cdp.send("Input.dispatchMouseEvent", {
                            "type": "mouseReleased", "x": hit["x"], "y": hit["y"],
                            "button": "left", "clickCount": 1})
                        clicked = True
                        click_method = "cdp-click"
                    except Exception:
                        pass

                # STILL obscured. Playwright's force-click is NOT a safe
                # fallback here: force only skips actionability checks, it
                # still dispatches at the element's coordinates, so it lands
                # on whatever is covering the element — producing a confident
                # "clicked" with another element's beacons, or none at all.
                # Dispatch on the node itself instead: untrusted, but it is
                # guaranteed to reach the element under test.
                if not clicked:
                    try:
                        await el.evaluate("e => e.click()")
                        clicked = True
                        click_method = "js-click"
                    except Exception:
                        pass

                # Only now try a coordinate click, as a last resort for nodes
                # where .click() is a no-op (e.g. label/summary handling).
                if not clicked:
                    try:
                        await el.click(force=True, timeout=2500, no_wait_after=True)
                        clicked = True
                        click_method = "force-click"
                    except Exception:
                        pass

                # Put any neutralised overlays back before anything else runs.
                if pierced:
                    try:
                        await page.evaluate(RESTORE_OVERLAY_JS)
                    except Exception:
                        pass

                # ---- VERIFY the intended node actually received the click ----
                if clicked:
                    await asyncio.sleep(0.25)
                    witness = None
                    try:
                        witness = await page.evaluate(READ_CLEAR_WITNESS_JS)
                    except Exception:
                        witness = None
                    w_uids = (witness or {}).get("uids") or []
                    if uid and w_uids:
                        if uid in w_uids:
                            el_res["click_verified"] = True
                        else:
                            # A different element swallowed the click. Retry by
                            # dispatching directly on our node rather than
                            # recording the other element's beacons here.
                            # JS dispatch, not a coordinate click — the whole
                            # reason we got here is that coordinates resolve
                            # to somebody else.
                            try:
                                await el.evaluate("e => e.click()")
                                click_method += "+retarget-js"
                            except Exception:
                                pass
                            await asyncio.sleep(0.25)
                            try:
                                w2 = await page.evaluate(READ_CLEAR_WITNESS_JS)
                                if uid in ((w2 or {}).get("uids") or []):
                                    el_res["click_verified"] = True
                            except Exception:
                                pass
                    elif uid and not w_uids:
                        # No witness at all (handler stopped propagation before
                        # our capture listener, or click never landed).
                        el_res["click_verified"] = False

            if clicked:
                # ---- ADAPTIVE WAIT ----
                # Fixed 3s was the single biggest source of wrong data on this
                # stack: GTM defers /g/collect by ~5s, so every tracked click's
                # beacon arrived AFTER the window closed and got recorded under
                # the NEXT element. Read the instant API-level signal first —
                # dataLayer/ACDL push synchronously at click time — and only
                # pay the long wait when we know a beacon is actually coming.
                await asyncio.sleep(0.6)
                try:
                    early_caps = await page.evaluate(PEEK_TRACKING_JS)
                except Exception:
                    early_caps = []
                expect_beacon = any(
                    not _is_noise_event((c.get("detail") or {}).get("event")
                                        if isinstance(c.get("detail"), dict) else "")
                    for c in (early_caps or [])
                ) or bool(elem.get("is_download"))

                if os.environ.get("TV_DEBUG"):
                    try:
                        _dbg = await page.evaluate("""()=>({dl:!!(window.dataLayer&&window.dataLayer.__qaWrapped),
                            inst:!!window.__qaTrackInstalled, caps:(window.__qaTrackCaptures||[]).length,
                            dllen:Array.isArray(window.dataLayer)?window.dataLayer.length:-1,
                            url:location.href.slice(0,60)})""")
                    except Exception as _e:
                        _dbg = {"err": str(_e)[:60]}
                    sys.stdout.write("      DBG ei=%s reqs=%s bm=%s caps=%s %s\n" % (
                        ei, len(all_requests), req_bookmark, len(early_caps or []), _dbg))
                    sys.stdout.flush()
                deadline = time.time() + (CLICK_BEACON_MAX_WAIT if expect_beacon
                                          else CLICK_IDLE_WAIT)
                quiet_needed = 1.2 if expect_beacon else 0.6
                last_seen = len(all_requests)
                last_change = time.time()
                while time.time() < deadline:
                    await asyncio.sleep(0.25)
                    if len(all_requests) != last_seen:
                        last_seen = len(all_requests)
                        last_change = time.time()
                    elif time.time() - last_change >= quiet_needed:
                        break

            label = elem.get("text", "")[:30] or elem.get("selector", "")[:30]
            if not clicked:
                current_cid["v"] = -1     # never leave a window open on a skip
                el_res["skipped"] = True
                el_res["skip_reason"] = "not-found" if el is None else "unclickable"
                sys.stdout.write(
                    f"[{index}/{total}]   [{ei+1}/{len(elements)}] [{elem.get('zone', 'body').upper()[:6]}] "
                    f"\"{label}\" -> [SKIPPED: {el_res['skip_reason']}]\n")
                sys.stdout.flush()
                continue

            # Close this element's window before parsing so nothing that
            # arrives during the (async) parse gets stamped with this cid.
            current_cid["v"] = -1

            # Capture: grab all NEW requests since the bookmark
            click_requests = all_requests[req_bookmark:]
            # RAW network log for this click — full QA visibility, nothing
            # silently dropped. Capped per element as a safety net only.
            el_res["network_requests"] = [
                {"url": r["url"][:300]} for r in click_requests[:200]
            ]
            el_res["network_count"] = len(click_requests)
            el_res["click_method"] = click_method
            el_res["attribution"] = "window"
            _parse_click_requests(click_requests, el_res)

            # Harvest API-LEVEL tracking fired by this click — catches Adobe
            # s.tl, Tealium utag.link, gtag events and GTM dataLayer pushes
            # even when their network beacon is deferred, batched or blocked.
            try:
                api_caps = await page.evaluate(HARVEST_TRACKING_JS)
            except Exception:
                api_caps = []
            for cap in api_caps or []:
                ctype = cap.get("type")
                det = cap.get("detail") or {}
                if ctype == "s.tl":
                    el_res["adobe_calls"].append({
                        "link_type": "s.tl:" + ((det.get("link_type") or "o") if isinstance(det, dict) else "o"),
                        "link_name": (det.get("link_name") or "") if isinstance(det, dict) else "",
                        "evars": {}, "props": {},
                    })
                    el_res["has_tracking"] = True
                elif ctype == "utag.link":
                    detail_str = json.dumps(det)[:180] if isinstance(det, (dict, list)) else str(det)[:180]
                    el_res["other_analytics"].append({"vendor": "Tealium utag.link", "url": detail_str})
                    el_res["has_tracking"] = True
                elif ctype == "gtag":
                    el_res["ga4_events"].append({
                        "event": (det.get("event") or "") if isinstance(det, dict) else str(det),
                        "measurement_id": "(gtag api)",
                        "params": (det.get("params") or {}) if isinstance(det, dict) else {},
                    })
                    el_res["has_tracking"] = True
                elif ctype in ("dataLayer", "adobeDataLayer") and isinstance(det, dict):
                    ev_name = str(det.get("event") or det.get("xdm:eventType") or "")
                    # Scroll-depth / GTM bootstrap / consent events fire on
                    # timers, not on this click. Recording them here is what
                    # made unrelated buttons look "tracked".
                    if _is_noise_event(ev_name):
                        continue
                    params = {k: v for k, v in det.items()
                              if k != "event" and isinstance(v, (str, int, float, bool))}
                    src = "(dataLayer)" if ctype == "dataLayer" else "(adobeDataLayer)"
                    el_res["datalayer_events"].append({
                        "event": ev_name, "source": src, "params": params,
                    })
                    el_res["ga4_events"].append({
                        "event": ev_name,
                        "measurement_id": src,
                        "params": params,
                    })
                    el_res["has_tracking"] = True

            # ActivityMap: the click stores its link record in the s_sq
            # cookie (sent with the NEXT page view, which we block) — the
            # cookie itself is proof the click is Adobe-tracked.
            try:
                s_sq = await page.evaluate(READ_CLEAR_SSQ_JS)
            except Exception:
                s_sq = ""
            if s_sq:
                oid = ""
                m_oid = re.search(r'oid%3D(.*?)%26', s_sq, re.I) or re.search(r'oid=([^&;]*)', s_sq, re.I)
                if m_oid:
                    oid = unquote(unquote(m_oid.group(1))).strip()[:120]
                el_res["adobe_calls"].append({
                    "link_type": "ActivityMap",
                    "link_name": oid or s_sq[:120],
                    "evars": {}, "props": {},
                })
                el_res["has_tracking"] = True

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
            vtag = "" if el_res.get("click_verified") else " [UNVERIFIED]"
            sys.stdout.write(
                f"[{index}/{total}]   [{ei+1}/{len(elements)}] [{zone_tag}] \"{label}\" -> [{click_method}]{vtag} "
                f"GA4:[{ga_str}] Adobe:[{aa_str}]{extra}\n")
            sys.stdout.flush()

            # Leave the page in a neutral state for the next element. Toggles,
            # accordions and "expand" panels stay open after being clicked and
            # will physically cover the elements that come after them, which
            # shows up as a run of unverified clicks with no tracking. Escape
            # closes most overlays; anything this reopens is discarded by the
            # drain at the start of the next element.
            try:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.15)
            except Exception:
                pass

            # Close any popup tabs the click opened (target=_blank links).
            # Their requests were already captured by the context-level
            # listener; leaving them open would pollute later clicks.
            try:
                for extra_pg in context.pages:
                    if extra_pg != page:
                        try: await extra_pg.close()
                        except Exception: pass
            except Exception:
                pass

            # The nav blocker keeps most clicks on-page; if this one escaped
            # (JS redirect), recover now with retries + page re-create.
            try:
                await _ensure_on_page()
            except Exception:
                pass

        # ---- FINAL DRAIN ----
        # The last elements clicked may still have beacons in flight. Without
        # this they'd simply never be captured.
        if elements_result:
            current_cid["v"] = -1
            await asyncio.sleep(FINAL_DRAIN_WAIT)

        # ================= CONTENT-BASED RECONCILIATION =================
        # Timing alone cannot attribute a beacon that a tag manager defers by
        # several seconds — by the time it lands, a later element's window is
        # open, which is exactly how "button 1's parameters" end up displayed
        # under button 2. But every click beacon carries the clicked link's
        # OWN identity (ep.link_url / ep.link_text / gtm.elementUrl), so the
        # beacon can be bound to its true element by content instead.
        #
        # Pass 1: index every audited element by url and by text.
        # Pass 2: re-read every analytics request, work out which element it
        #         describes, and move it there — overriding the time-window
        #         guess whenever the payload names an element unambiguously.
        try:
            by_url = {}
            by_text = {}
            for i, r in enumerate(elements_result):
                if r.get("skipped"):
                    continue
                for uk in _url_keys(r["element"].get("href"), original_url):
                    by_url.setdefault(uk, []).append(i)
                tk = _attr_key_text(r["element"].get("text"))
                if tk:
                    by_text.setdefault(tk, []).append(i)

            def _pick(cands, fallback_cid, zone):
                """Narrow a candidate list down to one element."""
                if not cands:
                    return None
                if len(cands) == 1:
                    return cands[0]
                # The beacon often names the region it fired in; prefer the
                # candidate sitting in that region. This is what keeps a
                # header logo and a footer logo — identical href, identical
                # text — from absorbing each other's events.
                if zone:
                    zoned = [i for i in cands
                             if elements_result[i]["element"].get("zone") == zone]
                    if len(zoned) == 1:
                        return zoned[0]
                    if zoned:
                        return fallback_cid if fallback_cid in zoned else zoned[0]
                # Otherwise the click window that was open is the best tie-break.
                return fallback_cid if fallback_cid in cands else cands[0]

            def _resolve_owner(link_url, link_text, fallback_cid, zone=''):
                """Which element does this beacon describe? Returns an index
                into elements_result, or None to leave it where it is."""
                tk = _attr_key_text(link_text)
                u_hits = []
                for uk in _url_keys(link_url, original_url):
                    for i in by_url.get(uk, []):
                        if i not in u_hits:
                            u_hits.append(i)
                t_hits = by_text.get(tk, []) if tk else []

                # Strongest: url AND text agree.
                both = [i for i in u_hits if i in t_hits]
                if both:
                    return _pick(both, fallback_cid, zone)
                if u_hits:
                    return _pick(u_hits, fallback_cid, zone)
                if t_hits:
                    return _pick(t_hits, fallback_cid, zone)
                return None

            # Wipe the network-derived events and rebuild them from scratch
            # with correct ownership. API-level captures (dataLayer/ACDL/s.tl)
            # are NOT touched: those are synchronous at click time and were
            # already attributed correctly.
            api_only = []
            for r in elements_result:
                keep_ga4 = [e for e in r["ga4_events"]
                            if str(e.get("measurement_id", "")).startswith("(")]
                api_only.append(keep_ga4)
                r["ga4_events"] = []
                r["adobe_calls"] = [c for c in r["adobe_calls"]
                                    if str(c.get("link_type", "")).startswith("s.tl")
                                    or c.get("link_type") == "ActivityMap"]
                r["adobe_websdk"] = []
                r["other_analytics"] = []
                r["total_requests"] = 0

            moved = 0
            page_level_events = []
            for req in all_requests:
                req_low = req["url"].lower()
                cid = req.get("cid", -1)
                is_ga4 = ("/g/collect" in req_low or (
                    ("google-analytics.com" in req_low or "analytics.google.com" in req_low)
                    and "collect" in req_low))

                if is_ga4:
                    for ev in parse_ga4_event(req["url"], req["post"]):
                        name = ev.get("event") or ""
                        if not name or _is_noise_event(name):
                            continue
                        lu, lt = beacon_identity(ev)
                        owner = _resolve_owner(lu, lt, cid, beacon_zone(ev))
                        if owner is None:
                            # No element identity in the payload. Such hits are
                            # usually page-level (consent state, engagement,
                            # visibility) and merely happened to land inside a
                            # click's window — attributing them would print the
                            # same phantom event under a dozen unrelated
                            # buttons. Keep one only when the element's own
                            # click-time dataLayer capture named that same
                            # event, which proves the click caused it.
                            if not (lu or lt) and 0 <= cid < len(elements_result):
                                api_names = {
                                    str(x.get("event") or "").lower()
                                    for x in elements_result[cid].get("datalayer_events", [])
                                }
                                if name.lower() not in api_names:
                                    page_level_events.append(name)
                                    continue
                            owner = cid if 0 <= cid < len(elements_result) else None
                        if owner is None:
                            continue
                        if owner != cid:
                            moved += 1
                        tgt = elements_result[owner]
                        tgt["ga4_events"].append(ev)
                        tgt["has_tracking"] = True
                        tgt["total_requests"] += 1
                        if tgt["attribution"] != "content":
                            tgt["attribution"] = "content"
                    continue

                # Non-GA4 vendors carry no element identity, so they stay with
                # the click window they arrived in.
                if 0 <= cid < len(elements_result):
                    _parse_click_requests([req], elements_result[cid])

            # Put the API-level GA4 events back.
            for r, keep in zip(elements_result, api_only):
                r["ga4_events"] = keep + r["ga4_events"]
                if r["ga4_events"] or r["adobe_calls"]:
                    r["has_tracking"] = True

            # Collapse the same event sent to multiple GA4 properties.
            for r in elements_result:
                r["ga4_events"] = consolidate_ga4_events(r["ga4_events"])

            # Recompute has_tracking honestly after the rebuild.
            for r in elements_result:
                r["has_tracking"] = bool(r["ga4_events"] or r["adobe_calls"]
                                         or r["adobe_websdk"] or r["other_analytics"])

            sys.stdout.write(f"[{index}/{total}] Reconciled beacons by payload identity "
                             f"({moved} re-attributed to their true element)\n")
            if page_level_events:
                from collections import Counter as _C
                _pl = ", ".join(f"{k}({v})" for k, v in _C(page_level_events).most_common(6))
                sys.stdout.write(f"[{index}/{total}] Excluded page-level (non-click) events: {_pl}\n")
            # The per-element lines above were printed BEFORE reconciliation,
            # so reprint the final, corrected picture — that is what the
            # report and the UI actually contain.
            sys.stdout.write(f"[{index}/{total}] ---- FINAL (post-reconciliation) ----\n")
            for _i, _r in enumerate(elements_result):
                if _r.get("skipped"):
                    continue
                _names = []
                for _e in _r["ga4_events"]:
                    _n = _e.get("event") or ""
                    _ids = _e.get("measurement_ids") or []
                    _real = [x for x in _ids if not str(x).startswith("(")]
                    _names.append(f"{_n}x{len(_real)}" if len(_real) > 1 else _n)
                _lbl = (_r["element"].get("text") or "")[:30]
                _zt = _r["element"].get("zone", "body").upper()[:6]
                sys.stdout.write(
                    f"[{index}/{total}]   [{_i+1}] [{_zt}] \"{_lbl}\" -> "
                    f"{', '.join(_names) if _names else '(no tracking)'}\n")
            sys.stdout.flush()
        except Exception as _rec_err:
            sys.stdout.write(f"[{index}/{total}] [WARN] reconciliation skipped: {str(_rec_err)[:120]}\n")
            sys.stdout.flush()

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
    skipped = sum(1 for r in elements_result if r.get("skipped"))
    sys.stdout.write(f"[{index}/{total}] Done: {url} | {tracked}/{len(elements_result)} elements have tracking"
                     f"{f' | {skipped} skipped' if skipped else ''}\n")
    sys.stdout.flush()

    return {
        "URL": url,
        "Total_Elements": len(elements_result),
        "With_Tracking": tracked,
        "Without_Tracking": len(elements_result) - tracked,
        "Skipped": skipped,
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
        cols = ['URL', 'Total_Elements', 'With_Tracking', 'Without_Tracking', 'Skipped', 'Error']
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
                
                # Raw request URLs for full QA visibility — capped so the
                # cell stays inside Excel's 32k character limit.
                net_urls = "\n".join(nr.get("url", "") for nr in el_res.get("network_requests", [])[:60])

                detail_rows.append({
                    "Page URL": page_url,
                    "Element Tag": el.get("tag", ""),
                    "Element Text": el.get("text", ""),
                    "Element ID": el.get("id", ""),
                    "Element Href": el.get("href", ""),
                    "Element Selector": el.get("selector", ""),
                    "Zone": el.get("zone", ""),
                    "Frame": el.get("frame_url", ""),
                    "Is Download": "Yes" if el.get("is_download") else "No",
                    "Click Status": "Skipped" if el_res.get("skipped") else (el_res.get("click_method") or "clicked"),
                    "Click Verified": ("--" if el_res.get("skipped")
                                       else ("Yes" if el_res.get("click_verified") else "No")),
                    "Skip Reason": el_res.get("skip_reason", ""),
                    "Blocked By": el_res.get("blocked_by", ""),
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
                    "Analytics Requests": el_res.get("total_requests", 0),
                    "All Network Requests": el_res.get("network_count", 0),
                    "Network Request URLs": net_urls,
                })
        detail_df = pd.DataFrame(detail_rows)
        with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
            summary_df.to_excel(writer, sheet_name='Summary', index=False)
            detail_df.to_excel(writer, sheet_name='Clicked_Elements_Detail', index=False)
    else:
        res_df.to_excel(output_file, index=False)


if __name__ == "__main__":
    asyncio.run(main())
