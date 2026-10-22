#!/usr/bin/env python3
"""canonical-clash: find conflicting canonical signals on a website.

Compares three signals for every URL you give it:
    1. the <link rel="canonical"> tag in the HTML
    2. the XML sitemap (what you tell search engines to crawl)
    3. the redirect chain (what the server actually does)

Standard library only. Run it with:  python3 check.py --sitemap URL
Exit codes: 0 = clean, 1 = conflicts found, 2 = bad input / usage error.
"""

import argparse
import gzip
import json
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

__version__ = "1.0.0"

USER_AGENT = "canonical-clash/%s (open-source SEO checker; polite, sequential requests)" % __version__
REDIRECT_CODES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 10
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_SITEMAP_BYTES = 60 * 1024 * 1024
MAX_SITEMAP_DEPTH = 5

CONFLICT_TYPES = [
    "CANONICAL_MISMATCH",
    "CANONICAL_REDIRECTS",
    "SITEMAP_REDIRECT",
    "MULTIPLE_CANONICALS",
    "MISSING_CANONICAL",
    "SITEMAP_NON200",
    "FETCH_ERROR",
]

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# --------------------------------------------------------------------------
# HTTP layer (manual redirect tracking)
# --------------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects so 3xx responses surface as HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def describe_error(exc):
    if isinstance(exc, urllib.error.URLError):
        return "URLError: %s" % (exc.reason,)
    return "%s: %s" % (type(exc).__name__, exc)


def fetch_once(url, timeout, read_body):
    """One HTTP GET, no redirect following. Returns (status, headers, body_bytes)."""
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        resp = OPENER.open(req, timeout=timeout)
        try:
            status = resp.getcode()
            headers = resp.headers
            body = resp.read(MAX_PAGE_BYTES) if read_body else b""
        finally:
            resp.close()
        return status, headers, body
    except urllib.error.HTTPError as err:
        body = b""
        try:
            if read_body and err.code not in REDIRECT_CODES:
                body = err.read(MAX_PAGE_BYTES)
        except Exception:
            body = b""
        finally:
            err.close()
        return err.code, err.headers, body


def follow(url, timeout, read_body):
    """Follow redirects by hand and record every hop.

    Returns a dict with: chain, final_url, final_status, error, headers, body.
    Each chain entry: {"url", "status", "location", "resolved"}; "location" is the
    raw Location header (None on the last hop) and "resolved" is the absolute URL.
    """
    chain = []
    seen = set()
    current = url
    status = None
    headers = None
    body = b""
    error = None

    for _ in range(MAX_REDIRECTS + 1):
        if current in seen:
            error = "REDIRECT_LOOP: %s was visited twice" % current
            status = None
            break
        seen.add(current)
        try:
            status, headers, body = fetch_once(current, timeout, read_body)
        except Exception as exc:  # never let one URL crash the run
            error = describe_error(exc)
            status = None
            break
        location = headers.get("Location") if headers is not None else None
        if status in REDIRECT_CODES and location:
            resolved = urljoin(current, location.strip())
            chain.append({"url": current, "status": status,
                          "location": location.strip(), "resolved": resolved})
            current = resolved
            continue
        chain.append({"url": current, "status": status, "location": None, "resolved": None})
        break
    else:
        error = "TOO_MANY_REDIRECTS: more than %d hops" % MAX_REDIRECTS
        status = None

    return {
        "chain": chain,
        "final_url": current,
        "final_status": status,
        "error": error,
        "headers": headers,
        "body": body,
    }


def count_redirects(chain):
    return sum(1 for hop in chain if hop.get("location") is not None)


def fmt_chain(chain, final_url=None, error=None):
    parts = ["%s [%s]" % (hop["url"], hop["status"]) for hop in chain]
    if error and final_url and (not chain or chain[-1].get("location") is not None):
        parts.append("%s [error]" % final_url)
    return " -> ".join(parts)


# --------------------------------------------------------------------------
# HTML + URL helpers
# --------------------------------------------------------------------------

class CanonicalParser(HTMLParser):
    """Collects the href of every <link rel="canonical"> tag."""

    def __init__(self):
        super().__init__()
        self.canonicals = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        attr = {k.lower(): (v or "") for k, v in attrs}
        rels = attr.get("rel", "").lower().split()
        if "canonical" in rels:
            href = attr.get("href", "").strip()
            if href:
                self.canonicals.append(href)


def decode_body(body, headers):
    charset = None
    try:
        charset = headers.get_content_charset()
    except Exception:
        charset = None
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return body.decode(enc, errors="replace")
        except LookupError:
            continue
    return body.decode("latin-1", errors="replace")


def looks_like_html(headers):
    ctype = (headers.get("Content-Type", "") if headers is not None else "").lower()
    if not ctype:
        return True
    return "html" in ctype or "xml" in ctype


def normalize(url):
    """Comparison form: lowercase scheme/host, default ports and fragment dropped,
    trailing slash removed. Reported values always stay raw."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = "[%s]" % host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = "%s:%d" % (host, port)
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, host, path, parts.query, ""))


# --------------------------------------------------------------------------
# Input loading
# --------------------------------------------------------------------------

def http_get(url, timeout, max_bytes):
    req = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(max_bytes)
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data


def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def child_loc(element):
    for child in element:
        if local_name(child.tag) == "loc" and child.text:
            return child.text.strip()
    return None


def load_sitemap(url, timeout, limit, depth=0, visited=None):
    """Return a de-duplicated list of page URLs. Supports <sitemapindex> and .gz."""
    if visited is None:
        visited = set()
    visited.add(url)
    root = ET.fromstring(http_get(url, timeout, MAX_SITEMAP_BYTES))
    kind = local_name(root.tag)
    urls = []
    seen = set()

    def room():
        return limit <= 0 or len(urls) < limit

    if kind == "sitemapindex":
        for entry in root:
            if local_name(entry.tag) != "sitemap" or not room():
                continue
            loc = child_loc(entry)
            if not loc or loc in visited or depth >= MAX_SITEMAP_DEPTH:
                continue
            try:
                sub = load_sitemap(loc, timeout, 0 if limit <= 0 else limit - len(urls),
                                   depth + 1, visited)
            except Exception as exc:
                print("warning: could not read child sitemap %s (%s)" % (loc, describe_error(exc)),
                      file=sys.stderr)
                continue
            for item in sub:
                if item not in seen and room():
                    seen.add(item)
                    urls.append(item)
    elif kind == "urlset":
        for entry in root:
            if local_name(entry.tag) != "url" or not room():
                continue
            loc = child_loc(entry)
            if loc and loc not in seen:
                seen.add(loc)
                urls.append(loc)
    else:
        raise ValueError("%s is not a sitemap (root element is <%s>)" % (url, kind))
    return urls


def load_url_file(path):
    urls = []
    seen = set()
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line not in seen:
                seen.add(line)
                urls.append(line)
    return urls


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def check_target(target, timeout, cache):
    """Fetch a canonical target once (cached) and summarise its redirect behaviour."""
    if target not in cache:
        r = follow(target, timeout, False)
        cache[target] = {
            "url": target,
            "final_url": r["final_url"],
            "final_status": r["final_status"],
            "redirects": count_redirects(r["chain"]),
            "chain": r["chain"],
            "error": r["error"],
        }
    return cache[target]


def analyze(url, from_sitemap, timeout, target_cache):
    r = follow(url, timeout, True)
    chain = r["chain"]
    final_url = r["final_url"]
    final_status = r["final_status"]
    error = r["error"]
    redirects = count_redirects(chain)
    conflicts = []
    canonicals = []
    target_checks = []
    html_checked = False

    def add(kind, details):
        conflicts.append({"type": kind, "details": details})

    if error:
        detail = error
        if chain:
            detail += " | chain: " + fmt_chain(chain, final_url, error)
        add("FETCH_ERROR", detail)

    if from_sitemap:
        if redirects > 0:
            add("SITEMAP_REDIRECT",
                "Sitemap lists a URL that redirects: %s" % fmt_chain(chain, final_url, error))
        if not error and final_status != 200:
            add("SITEMAP_NON200", "Final status %s at %s" % (final_status, final_url))

    if not error and final_status == 200 and looks_like_html(r["headers"]):
        html_checked = True
        parser = CanonicalParser()
        try:
            parser.feed(decode_body(r["body"], r["headers"]))
            parser.close()
        except Exception:
            pass  # keep whatever was parsed before the markup broke
        raws = parser.canonicals
        canonicals = [{"raw": raw, "resolved": urljoin(final_url, raw)} for raw in raws]

        if not raws:
            add("MISSING_CANONICAL", "No <link rel=\"canonical\"> found on a 200 page (%s)" % final_url)
        else:
            if len(raws) > 1:
                add("MULTIPLE_CANONICALS",
                    "%d canonical tags found: %s" % (len(raws), ", ".join(raws)))
            done = set()
            for item in canonicals:
                resolved = item["resolved"]
                if resolved in done:
                    continue
                done.add(resolved)
                if resolved == final_url:
                    continue  # identical to the page we just fetched: nothing to compare
                if normalize(resolved) != normalize(final_url):
                    shown = item["raw"]
                    if shown != resolved:
                        shown = "%s (resolves to %s)" % (item["raw"], resolved)
                    add("CANONICAL_MISMATCH",
                        "canonical = %s | final page URL = %s" % (shown, final_url))
                # The target itself must not redirect (also catches trailing-slash hops).
                check = check_target(resolved, timeout, target_cache)
                target_checks.append(check)
                if check["error"]:
                    add("FETCH_ERROR",
                        "canonical target %s could not be fetched: %s" % (resolved, check["error"]))
                elif check["redirects"] > 0:
                    add("CANONICAL_REDIRECTS",
                        "canonical target redirects: %s" % fmt_chain(check["chain"]))

    return {
        "url": url,
        "chain": chain,
        "redirect_count": redirects,
        "final_url": final_url,
        "final_status": final_status,
        "error": error,
        "html_checked": html_checked,
        "canonicals": canonicals,
        "canonical_target_checks": target_checks,
        "conflicts": conflicts,
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def md_cell(text):
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def build_markdown(report):
    summary = report["summary"]
    lines = [
        "# canonical-clash report",
        "",
        "- Generated: %s" % report["generated_at"],
        "- Input: %s" % report["input"]["description"],
        "- URLs checked: %d" % summary["urls_checked"],
        "- URLs with conflicts: %d" % summary["urls_with_conflicts"],
        "- Total conflicts: %d" % summary["total_conflicts"],
        "",
        "## Summary",
        "",
        "| Conflict type | Count |",
        "| --- | ---: |",
    ]
    for kind in CONFLICT_TYPES:
        lines.append("| %s | %d |" % (kind, summary["by_type"][kind]))
    lines += ["", "## Conflicts", ""]
    rows = [(res["url"], c["type"], c["details"])
            for res in report["results"] for c in res["conflicts"]]
    if not rows:
        lines.append("No conflicts found.")
    else:
        lines += ["| URL | Type | Details |", "| --- | --- | --- |"]
        for url, kind, details in rows:
            lines.append("| %s | %s | %s |" % (md_cell(url), kind, md_cell(details)))
    lines.append("")
    return "\n".join(lines)


def build_report(results, mode, source, limit):
    by_type = {kind: 0 for kind in CONFLICT_TYPES}
    total = 0
    with_conflicts = 0
    for res in results:
        if res["conflicts"]:
            with_conflicts += 1
        for c in res["conflicts"]:
            by_type[c["type"]] += 1
            total += 1
    return {
        "tool": "canonical-clash",
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input": {
            "mode": mode,
            "source": source,
            "limit": limit,
            "description": "%s %s (limit: %s)" % (
                "sitemap" if mode == "sitemap" else "URL file", source,
                limit if limit > 0 else "none"),
        },
        "summary": {
            "urls_checked": len(results),
            "urls_with_conflicts": with_conflicts,
            "total_conflicts": total,
            "by_type": by_type,
        },
        "results": results,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="check.py",
        description="Find conflicting canonical signals: canonical tag vs XML sitemap vs redirects.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sitemap", metavar="URL",
                        help="XML sitemap or sitemap index URL (.xml or .xml.gz)")
    source.add_argument("--urls", metavar="FILE", help="text file with one URL per line")
    parser.add_argument("--limit", type=int, default=50, metavar="N",
                        help="max URLs to check (default 50, 0 = no limit)")
    parser.add_argument("--timeout", type=float, default=15, metavar="SEC",
                        help="per-request timeout in seconds (default 15)")
    parser.add_argument("--json", default="canonical-report.json", metavar="FILE",
                        help="JSON report path (default canonical-report.json)")
    parser.add_argument("--md", default="canonical-report.md", metavar="FILE",
                        help="Markdown report path (default canonical-report.md)")
    parser.add_argument("--version", action="version", version="canonical-clash " + __version__)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.limit < 0:
        print("error: --limit must be 0 or greater", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("error: --timeout must be greater than 0", file=sys.stderr)
        return 2

    try:
        if args.sitemap:
            mode, src = "sitemap", args.sitemap
            urls = load_sitemap(args.sitemap, args.timeout, args.limit)
        else:
            mode, src = "urls", args.urls
            urls = load_url_file(args.urls)
        if args.limit > 0:
            urls = urls[:args.limit]
    except Exception as exc:
        print("error: could not load input (%s)" % describe_error(exc), file=sys.stderr)
        return 2

    if not urls:
        print("error: no URLs to check", file=sys.stderr)
        return 2

    from_sitemap = mode == "sitemap"
    target_cache = {}
    results = []
    total = len(urls)
    for index, url in enumerate(urls, 1):
        res = analyze(url, from_sitemap, args.timeout, target_cache)
        results.append(res)
        kinds = sorted({c["type"] for c in res["conflicts"]})
        status = res["final_status"] if res["final_status"] is not None else "ERR"
        print("[%d/%d] %s %s%s" % (index, total, status, url,
              "  -> " + ", ".join(kinds) if kinds else ""),
              file=sys.stderr)

    report = build_report(results, mode, src, args.limit)
    try:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        with open(args.md, "w", encoding="utf-8") as handle:
            handle.write(build_markdown(report))
    except OSError as exc:
        print("error: could not write report (%s)" % exc, file=sys.stderr)
        return 2

    summary = report["summary"]
    print("")
    print("canonical-clash: checked %d URL(s), %d conflict(s) on %d URL(s)" % (
        summary["urls_checked"], summary["total_conflicts"], summary["urls_with_conflicts"]))
    for kind in CONFLICT_TYPES:
        if summary["by_type"][kind]:
            print("  %-20s %d" % (kind, summary["by_type"][kind]))
    print("reports: %s, %s" % (args.json, args.md))
    return 1 if summary["total_conflicts"] else 0


if __name__ == "__main__":
    sys.exit(main())
