# canonical-clash

A tiny, zero-dependency Python CLI that finds **conflicting canonical signals** on a website by cross-checking three things:

1. the `<link rel="canonical">` tag in your HTML
2. your XML sitemap
3. the redirect chains your server actually serves

It is one file (`check.py`), uses only the Python 3 standard library, and runs with a bare `python3 check.py`. It exits with code `1` when it finds conflicts, so it drops straight into CI.

## Why canonical conflicts hurt SEO

A canonical tag is a hint that tells search engines which URL is the "master" copy of a page. Sitemaps, redirects and canonical tags are three separate ways of saying the same thing, and when they disagree, search engines have to guess:

- **Wasted crawl budget.** Crawlers spend requests on redirecting or dead URLs instead of your real pages.
- **Diluted ranking signals.** Links and engagement get split across URL variants instead of consolidating on one.
- **Wrong URL in the index.** When your signals clash, Google may ignore your canonical and pick its own, often not the one you wanted.
- **Silent regressions.** These problems appear after migrations, CMS upgrades, HTTPS/www changes and trailing-slash rule changes, and nothing visibly breaks.

canonical-clash turns that audit into a single command.

## Quickstart

No install, no `pip`. Copy `check.py` anywhere and run:

```bash
python3 check.py --urls urls.txt
python3 check.py --sitemap https://example.com/sitemap.xml --limit 100
cat canonical-report.md
```

By default it writes `canonical-report.json` and `canonical-report.md` to the current directory and prints a short summary.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--sitemap URL` | | XML sitemap or sitemap index (`.xml` or `.xml.gz`). Child sitemaps are followed. |
| `--urls FILE` | | Text file with one URL per line (blank lines and `#` comments are ignored). |
| `--limit N` | `50` | Maximum URLs to check. `0` means no limit. |
| `--timeout SEC` | `15` | Per-request timeout in seconds. |
| `--json FILE` | `canonical-report.json` | Where to write the structured JSON report. |
| `--md FILE` | `canonical-report.md` | Where to write the Markdown report. |

Exactly one of `--sitemap` or `--urls` is required.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | No conflicts found. |
| `1` | One or more conflicts found. |
| `2` | Bad input (unreadable sitemap or file, no URLs, invalid options). |

## The conflict types

| Type | What it means | How to fix it |
| --- | --- | --- |
| `CANONICAL_MISMATCH` | The canonical `href` differs from the page's final URL. Trailing slashes, host case and default ports are ignored when comparing, but the raw values are reported. | Point the canonical at the URL the page is actually served from, or redirect the page to the canonical. |
| `CANONICAL_REDIRECTS` | The canonical target itself redirects. A canonical must be the final destination, not a redirecting URL. | Update the canonical to the target's final URL. |
| `SITEMAP_REDIRECT` | A URL listed in the sitemap redirects somewhere else. Sitemaps should list final URLs only. | Replace the sitemap entry with the final destination URL. |
| `MULTIPLE_CANONICALS` | More than one canonical tag on the page. Search engines may ignore all of them. | Emit exactly one canonical tag (check themes, plugins and SEO modules for duplicates). |
| `MISSING_CANONICAL` | A page that returns 200 HTML has no canonical tag. | Add a self-referencing canonical. |
| `SITEMAP_NON200` | A sitemap URL ends in a non-200 status (404, 410, 5xx, ...). | Remove it from the sitemap or fix the page. |

A seventh type, `FETCH_ERROR`, is recorded when a URL (or a canonical target) cannot be fetched at all: timeouts, DNS or TLS failures, redirect loops, or more than 10 redirect hops. It never crashes the run, and it counts as a conflict for the exit code.

Notes on scope:

- `SITEMAP_REDIRECT` and `SITEMAP_NON200` only apply with `--sitemap`, because only then do we know the URLs came from a sitemap.
- Canonical checks only run on final `200` responses that look like HTML.
- Each canonical target is requested once per run, even if many pages share it.

## How it behaves

- **Manual redirect tracking.** Redirects are never auto-followed. Every hop's status code and `Location` header is recorded, up to 10 hops, with loop detection.
- **Polite.** Requests are sequential and send a `User-Agent` that identifies the tool. All network errors are caught per URL.
- **Relative canonicals** are resolved against the final page URL.

## GitHub Action

Use it as a composite action in any workflow. Replace `your-org/canonical-clash@v1` with wherever you host this repository (or use `uses: ./` if the action lives in the same repo).

```yaml
name: SEO canonical check
on:
  schedule:
    - cron: "0 6 * * 1"
  workflow_dispatch:

jobs:
  canonical-clash:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: your-org/canonical-clash@v1
        with:
          sitemap-or-urls-file: https://example.com/sitemap.xml
          limit: "200"
```

| Input | Default | Description |
| --- | --- | --- |
| `sitemap-or-urls-file` | (required) | A sitemap URL (`http://` or `https://`) or the path to a URL list file in your repo. |
| `limit` | `50` | Maximum URLs to check (`0` = no limit). |
| `timeout` | `15` | Per-request timeout in seconds. |

The job **fails when conflicts are found** (exit code `1`). The reports are always uploaded as the `canonical-clash-report` artifact (`canonical-report.json` and `canonical-report.md`), and the Markdown report is also added to the job summary.

To check a URL list committed to your repo, pass its path:

```yaml
- uses: your-org/canonical-clash@v1
  with:
    sitemap-or-urls-file: urls.txt
```

## JSON report shape

```json
{
  "tool": "canonical-clash",
  "version": "1.0.0",
  "generated_at": "2026-01-01T00:00:00Z",
  "input": { "mode": "sitemap", "source": "https://example.com/sitemap.xml", "limit": 50 },
  "summary": {
    "urls_checked": 50,
    "urls_with_conflicts": 3,
    "total_conflicts": 4,
    "by_type": { "CANONICAL_MISMATCH": 1, "SITEMAP_REDIRECT": 3 }
  },
  "results": [
    {
      "url": "https://example.com/old",
      "chain": [
        { "url": "https://example.com/old", "status": 301, "location": "/new", "resolved": "https://example.com/new" },
        { "url": "https://example.com/new", "status": 200, "location": null, "resolved": null }
      ],
      "final_url": "https://example.com/new",
      "final_status": 200,
      "canonicals": [{ "raw": "/new", "resolved": "https://example.com/new" }],
      "conflicts": [{ "type": "SITEMAP_REDIRECT", "details": "..." }]
    }
  ]
}
```

(`by_type` always lists all seven types in the real report; the example is shortened. Each result also contains `redirect_count`, `error`, `html_checked` and `canonical_target_checks`.)

## License

MIT — see [LICENSE](LICENSE).
