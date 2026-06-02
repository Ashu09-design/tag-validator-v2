"""
Domain crawler for Tag Validator Pro v3.1.
Discovers every reachable same-domain page via BFS + sitemap.xml.
URL normalization treats http/https and www/non-www as the same page,
so duplicates never appear in the output.

Usage: python domain_crawler.py <start_url> [max_pages]
  max_pages = 0 (or omitted) -> unlimited (crawl every page on the site)
"""
import sys
import re
import time
import urllib.request
import urllib.parse
import gzip
from collections import deque
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
import pandas as pd

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

SKIP_EXTENSIONS = (
    '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico', '.bmp',
    '.pdf', '.zip', '.rar', '.tar', '.gz', '.7z',
    '.mp4', '.mp3', '.avi', '.mov', '.wmv', '.flv', '.webm',
    '.css', '.js', '.json', '.rss',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.woff', '.woff2', '.ttf', '.eot', '.otf',
)


# Index filenames that mean the same as the parent directory
INDEX_FILES = ('index.html', 'index.htm', 'index.php', 'index.aspx',
               'default.html', 'default.htm', 'default.aspx', 'home.html')


def normalize_url(url: str) -> str:
    """Aggressive canonicalization so the same logical page never appears twice:
    - force https + lowercase host + strip www
    - drop fragment
    - drop query string entirely (kills the ?utm_source=, ?sort=, ?page=N variant
      explosion that makes 'unlimited' crawls never terminate)
    - strip trailing slash and /index.html-style files
    - collapse duplicate slashes
    """
    try:
        url = url.strip()
        if not url:
            return ""
        if url.startswith("//"):
            url = "https:" + url
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        p = urllib.parse.urlparse(url)
        host = p.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host.endswith(":80") or host.endswith(":443"):
            host = host.rsplit(":", 1)[0]
        path = p.path or "/"
        path = re.sub(r'/+', '/', path)
        # /foo/index.html -> /foo
        lower_path = path.lower()
        for idx in INDEX_FILES:
            if lower_path.endswith("/" + idx):
                path = path[: -(len(idx) + 1)] or "/"
                break
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        # Query string and fragment dropped — only the canonical page identity matters
        return urllib.parse.urlunparse(("https", host, path, "", "", ""))
    except Exception:
        return ""


def get_host(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host.rsplit(":", 1)[0]
    except Exception:
        return ""


def get_path(url: str) -> str:
    """Return the normalized URL's path (e.g. '/de' or '/' )."""
    try:
        return urllib.parse.urlparse(url).path or "/"
    except Exception:
        return "/"


def derive_path_prefix(start_norm: str) -> str:
    """If user pasted https://site.com/de/, restrict the crawl to URLs whose path
    starts with /de (with a slash boundary). Returns '' to mean 'no restriction'."""
    path = get_path(start_norm)
    if not path or path == "/":
        return ""
    # path is already normalized (no trailing slash unless root)
    return path


def matches_prefix(url: str, prefix: str) -> bool:
    """True if the URL's path is at or under the prefix.
    Empty prefix = match everything on the host."""
    if not prefix:
        return True
    p = get_path(url)
    return p == prefix or p.startswith(prefix + "/")


class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        for k, v in attrs:
            if k.lower() == "href" and v:
                self.links.append(v)


def fetch(url: str, timeout: int = 20, want_html_only: bool = True):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,text/xml",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        low_ctype = ctype.lower()
        if want_html_only and "html" not in low_ctype:
            return None, resp.geturl(), ctype
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        charset = "utf-8"
        m = re.search(r'charset=([\w-]+)', ctype, re.I)
        if m:
            charset = m.group(1)
        try:
            text = data.decode(charset, errors="ignore")
        except Exception:
            text = data.decode("utf-8", errors="ignore")
        return text, resp.geturl(), ctype


def parse_sitemap(xml_text: str, base_url: str):
    """Return (page_urls, child_sitemaps) extracted from a sitemap or sitemap-index."""
    pages = []
    sitemaps = []
    try:
        # Strip namespaces to make tag matching tolerant
        clean = re.sub(r'\sxmlns="[^"]+"', '', xml_text, count=1)
        root = ET.fromstring(clean)
    except Exception:
        return pages, sitemaps
    tag = root.tag.lower()
    if tag.endswith("sitemapindex"):
        for sm in root.findall(".//sitemap/loc"):
            if sm.text:
                sitemaps.append(sm.text.strip())
    else:
        for u in root.findall(".//url/loc"):
            if u.text:
                pages.append(u.text.strip())
    return pages, sitemaps


def load_sitemaps(start_url: str, target_host: str, path_prefix: str = ""):
    """Try /sitemap.xml and /robots.txt → Sitemap entries.
    Returns normalized page URLs that are on the target host AND under path_prefix."""
    discovered = []
    seen = set()
    queue = deque()
    base = f"https://{target_host}"

    # robots.txt first
    try:
        txt, _, _ = fetch(base + "/robots.txt", timeout=10, want_html_only=False)
        if txt:
            for line in txt.splitlines():
                m = re.match(r'\s*Sitemap:\s*(\S+)', line, re.I)
                if m:
                    queue.append(m.group(1).strip())
    except Exception:
        pass

    # common locations
    for guess in [base + "/sitemap.xml", base + "/sitemap_index.xml", base + "/sitemap-index.xml"]:
        if guess not in queue:
            queue.append(guess)

    visited_sitemaps = set()
    while queue:
        sm_url = queue.popleft()
        if sm_url in visited_sitemaps:
            continue
        visited_sitemaps.add(sm_url)
        try:
            text, _, _ = fetch(sm_url, timeout=15, want_html_only=False)
            if not text:
                continue
            pages, children = parse_sitemap(text, sm_url)
            for p in pages:
                n = normalize_url(p)
                if (n and get_host(n) == target_host
                        and matches_prefix(n, path_prefix)
                        and n not in seen):
                    seen.add(n)
                    discovered.append(n)
            for c in children:
                if c not in visited_sitemaps:
                    queue.append(c)
        except Exception:
            continue

    return discovered


def crawl(start_url: str, max_pages: int = 0):
    """max_pages=0 means unlimited."""
    start_norm = normalize_url(start_url)
    if not start_norm:
        print(f"Invalid start URL: {start_url}")
        return []

    target_host = get_host(start_norm)
    path_prefix = derive_path_prefix(start_norm)  # e.g. "/de" when user pastes /de/
    unlimited = (max_pages <= 0)
    # Hard safety cap even in "unlimited" mode — prevents accidental runaway on
    # sites with crawler traps. 50000 is way bigger than any real site needs.
    HARD_CAP = 50000
    limit_str = "unlimited (safety cap 50000)" if unlimited else f"max {max_pages}"
    scope = f"{target_host}{path_prefix}" if path_prefix else target_host
    print(f"Crawling: {scope} ({limit_str})")
    if path_prefix:
        print(f"Path filter active: only URLs under '{path_prefix}/' will be kept "
              f"(other regions/locales like /tr-tr/, /en-us/, etc. excluded).")
    sys.stdout.flush()

    # Step 1: seed from sitemap.xml / robots.txt sitemaps (cheap, fast, exhaustive)
    print("Looking up sitemap.xml / robots.txt...")
    sys.stdout.flush()
    sitemap_urls = load_sitemaps(start_norm, target_host, path_prefix)
    if sitemap_urls:
        print(f"Sitemap gave {len(sitemap_urls)} in-scope URLs (will still BFS-crawl to catch unlinked pages).")
        sys.stdout.flush()

    visited = set()         # URLs we've already fetched
    queued = set()          # URLs ever placed on the queue (prevents re-queue)
    discovered = []         # ordered, deduped output list
    discovered_set = set()  # O(1) membership check for discovered
    queue = deque()

    # Seed: start URL first, then sitemap URLs
    queue.append(start_norm)
    queued.add(start_norm)
    for u in sitemap_urls:
        if u not in queued:
            queued.add(u)
            queue.append(u)

    # Step 2: BFS crawl. Loop terminates when:
    #   - queue empties (every link has been followed)
    #   - max_pages reached (when not unlimited)
    #   - HARD_CAP reached (safety net even in unlimited mode)
    while queue:
        if not unlimited and len(discovered) >= max_pages:
            break
        if len(discovered) >= HARD_CAP:
            print(f"Hard safety cap ({HARD_CAP}) reached — stopping. "
                  f"This site is larger than expected or has a crawler trap.")
            sys.stdout.flush()
            break
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        html = None
        final_norm = url
        try:
            html, final_url, _ = fetch(url)
            if final_url:
                final_norm = normalize_url(final_url) or url
        except Exception as e:
            print(f"[skip] {url} ({str(e)[:60]})")
            sys.stdout.flush()
            continue

        # Record the page only if it's on the target host AND inside the path prefix.
        # The prefix check is what prevents /tr-tr/, /en-us/ etc. from leaking in
        # when the user started from /de/.
        if (final_norm
                and get_host(final_norm) == target_host
                and matches_prefix(final_norm, path_prefix)
                and final_norm not in discovered_set):
            discovered.append(final_norm)
            discovered_set.add(final_norm)
            progress_label = f"[{len(discovered)}/{'inf' if unlimited else max_pages}]"
            print(f"{progress_label} Found: {final_norm}")
            sys.stdout.flush()

        if html is None:
            continue
        if not unlimited and len(discovered) >= max_pages:
            break

        parser = LinkExtractor()
        try:
            parser.feed(html)
        except Exception:
            continue

        for href in parser.links:
            href = href.strip()
            if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                continue
            abs_url = urllib.parse.urljoin(url, href)
            low = abs_url.lower().split("?")[0].split("#")[0]
            if any(low.endswith(ext) for ext in SKIP_EXTENSIONS):
                continue
            if low.endswith(".xml") or "/sitemap" in low:
                continue
            n = normalize_url(abs_url)
            if not n or get_host(n) != target_host:
                continue
            # Don't even queue out-of-scope URLs (saves HTTP fetches + prevents the
            # crawl from drifting into other locales via cross-region links).
            if not matches_prefix(n, path_prefix):
                continue
            if n not in queued:
                queued.add(n)
                queue.append(n)

        time.sleep(0.05)

    return discovered


def main():
    if len(sys.argv) < 2:
        print("Usage: python domain_crawler.py <start_url> [max_pages]")
        sys.exit(1)
    start_url = sys.argv[1]
    max_pages = int(sys.argv[2]) if len(sys.argv) > 2 else 0  # 0 = unlimited

    t0 = time.time()
    urls = crawl(start_url, max_pages)
    elapsed = round(time.time() - t0, 1)

    # Final dedupe pass (paranoia — should already be unique)
    final = []
    seen_final = set()
    for u in urls:
        if u not in seen_final:
            seen_final.add(u)
            final.append(u)

    print(f"\nDiscovered {len(final)} unique pages in {elapsed}s")

    df = pd.DataFrame({"URL": final})
    df.to_excel("crawled_urls.xlsx", index=False)
    df.to_excel("input_sites.xlsx", index=False)
    print("Saved: crawled_urls.xlsx (and input_sites.xlsx for validator)")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
