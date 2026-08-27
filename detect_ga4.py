"""Detect which GA4 properties (and GTM containers) a page actually sends to.

The SDR audit has to be told which GA4 property counts as the source of truth,
because a site commonly tags to more than one and an event can legitimately be
configured for only one of them. Rather than making someone recall the ID, load
the page once, accept consent, nudge the page into firing, and report the
measurement IDs that really appear on the wire.

Usage:  python detect_ga4.py <url>
Prints JSON: {"ids": [...], "gtm": [...], "url": "..."}
"""
import asyncio
import json
import re
import sys
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright

import bulk_tag_validator as B

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


async def detect(url):
    ids, gtm = set(), set()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
        ctx = await browser.new_context(viewport={'width': 1280, 'height': 800},
                                        user_agent=UA)

        def on_req(req):
            u = req.url
            if '/g/collect' in u or ('google-analytics.com' in u and 'collect' in u):
                tid = (parse_qs(urlparse(u).query).get('tid') or [''])[0]
                if tid:
                    ids.add(tid)
            m = re.search(r'gtm\.js\?id=(GTM-[A-Z0-9]+)', u)
            if m:
                gtm.add(m.group(1))

        ctx.on("request", on_req)
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        await asyncio.sleep(4)
        try:
            await B.accept_cookies(page)
        except Exception:
            pass
        await asyncio.sleep(5)

        # Some properties only reveal themselves on an interaction, not on the
        # page view, so scroll and click one harmless link with navigation
        # blocked before deciding what the site tags to.
        try:
            await page.evaluate(B.BLOCK_NAVIGATION_JS)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
            await asyncio.sleep(1.5)
            link = page.locator('a[href]').first
            if await link.count() > 0:
                await link.evaluate("e => e.click()")
        except Exception:
            pass
        await asyncio.sleep(8)

        # Anything the tag manager declared but has not yet sent to.
        try:
            declared = await page.evaluate(
                """() => {
                    const out = [];
                    try {
                        const g = window.google_tag_data && window.google_tag_data.tidr
                                  && window.google_tag_data.tidr.destination;
                        if (g) out.push(...Object.keys(g));
                    } catch (e) {}
                    try {
                        for (const k of Object.keys(window.google_tag_manager || {})) {
                            if (/^G-/.test(k) || /^GTM-/.test(k)) out.push(k);
                        }
                    } catch (e) {}
                    return out;
                }""")
            for d in declared or []:
                if str(d).startswith('G-'):
                    ids.add(str(d))
                elif str(d).startswith('GTM-'):
                    gtm.add(str(d))
        except Exception:
            pass

        await browser.close()
    return {"url": url, "ids": sorted(ids), "gtm": sorted(gtm)}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ids": [], "gtm": [], "error": "no url"}))
        return
    url = sys.argv[1].strip()
    if not url.startswith("http"):
        url = "https://" + url
    try:
        print(json.dumps(asyncio.run(detect(url))))
    except Exception as e:
        print(json.dumps({"ids": [], "gtm": [], "error": str(e)[:200]}))


if __name__ == "__main__":
    main()
