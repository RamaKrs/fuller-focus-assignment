"""Nonprofit Data Scraper Agent — Fuller Focus take-home, V1.

Input:  an organisation name or a website URL.
Output: one structured JSON profile per org in `output/`, plus a combined CSV.

Everything lives in this single file on purpose (see README): the sections below
are self-contained enough to be split into modules later without rewriting.

Sections
    1. Config & constants
    2. Schema
    3. HTTP helpers
    4. Resolve input
    5. Link discovery
    6. Parsing
    7. LLM calls
    8. ProPublica
    9. Post-processing
   10. Output
   11. main()
"""

from __future__ import annotations

import argparse
import gzip
import logging
from html import unescape
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib import robotparser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import pymupdf
import httpx
import trafilatura
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 1. CONFIG & CONSTANTS
# ---------------------------------------------------------------------------

# Model IDs. Verified 2026-09-22 against claude.com/docs — note Haiku 4.5 takes
# no date suffix; current SDK model IDs are undated.
LINK_MODEL = "claude-haiku-4-5"
EXTRACT_MODEL = "claude-sonnet-5"

# USD per million tokens, (input, output).
# Source: https://claude.com/pricing, checked 2026-09-22.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
}

# Crawl limits.
MAX_PAGES = 6                # pages fetched per org, on top of the homepage
RESERVED_REPORT_SLOTS = 2    # of MAX_PAGES, held for financial documents
MAX_REPORT_PDFS = 2          # report PDFs followed one level below a page
MAX_PDF_PAGES = 16           # pages read from any single PDF
MAX_PDF_HEAD_PAGES = 6       # always read this many from the front
MAX_CHARS_PER_DOC = 15_000   # per-document cap before the extraction prompt
MAX_TOTAL_CHARS = 60_000     # total cap across all documents (~15k tokens)
MAX_SITEMAP_URLS = 200       # sitemap URLs kept as link candidates
MAX_LINK_CANDIDATES = 150    # candidates shown to the link-picker model
MAX_RSS_ITEMS = 10

# HTTP.
HTTP_TIMEOUT = 20.0          # seconds
BROWSER_TIMEOUT = 20.0       # seconds, Playwright
RETRIES = 2                  # retries after the first attempt
BACKOFF_SECONDS = (1, 3)
USER_AGENT = (
    "FullerFocusScraper/0.1 (+nonprofit research bot; contact: hello@example.com)"
)
MIN_TEXT_CHARS = 500         # below this, try the Playwright fallback
MAX_DOWNLOAD_BYTES = 10_000_000   # hard cap on any single response body
MAX_SITEMAPS = 5                  # child sitemaps read from a sitemap index
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# Query parameters stripped during normalisation, so one page isn't fetched
# (or shown to the model) twice under different tracking tags.
TRACKING_PARAM_PREFIXES = ("utm_",)
TRACKING_PARAMS = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "_ga"})

# Suffixes needing three labels to reach the registrable domain
# (example.co.uk), used to decide whether a link is "same site".
MULTI_LABEL_SUFFIXES = frozenset(
    {"co", "com", "org", "net", "ac", "gov", "edu", "or", "ne"}
)

# Extensions we can't read, so never worth a candidate slot.
NON_PAGE_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".zip", ".mp4", ".mp3", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)

# ProPublica Nonprofit Explorer (US filings).
PROPUBLICA_BASE = "https://projects.propublica.org/nonprofits/api/v2/"
PROPUBLICA_MATCH_THRESHOLD = 90   # fuzzy score needed to accept a name match
URL_VERIFY_THRESHOLD = 80         # fuzzy score needed to accept a guessed URL
MAX_FILING_YEARS = 5

OUTPUT_DIR = Path("output")

# NTEE major groups, keyed by the first letter of the NTEE code (spec §7).
NTEE_MAJOR_GROUPS: dict[str, str] = {
    "A": "Arts, Culture & Humanities",
    "B": "Education",
    "C": "Environment & Animals",
    "D": "Environment & Animals",
    "E": "Health",
    "F": "Health",
    "G": "Health",
    "H": "Health",
    "I": "Human Services",
    "J": "Human Services",
    "K": "Human Services",
    "L": "Human Services",
    "M": "Human Services",
    "N": "Human Services",
    "O": "Human Services",
    "P": "Human Services",
    "Q": "International, Foreign Affairs",
    "R": "Public, Societal Benefit",
    "S": "Public, Societal Benefit",
    "T": "Public, Societal Benefit",
    "U": "Public, Societal Benefit",
    "V": "Public, Societal Benefit",
    "W": "Public, Societal Benefit",
    "X": "Religion Related",
    "Y": "Mutual/Membership Benefit",
    "Z": "Unknown",
}
CAUSE_AREAS = sorted(set(NTEE_MAJOR_GROUPS.values()))

# Revenue thresholds for the computed size bucket, ordered low to high.
SIZE_BUCKETS: list[tuple[float, str]] = [
    (1_000_000, "<1M"),
    (10_000_000, "1M-10M"),
    (100_000_000, "10M-100M"),
]
SIZE_BUCKET_TOP = "100M+"

# Link candidates we never bother sending to the model.
LINK_JUNK_PATTERNS = (
    "mailto:", "tel:", "javascript:", "/login", "/signin", "/cart", "/checkout",
    "/donate", "/privacy", "/cookie", "/terms", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "linkedin.com", "youtube.com", "tiktok.com",
)

# Fallback keywords if the link-picker LLM call fails (spec §6 step 3).
LINK_FALLBACK_KEYWORDS = (
    "about", "mission", "program", "team", "leadership", "staff", "board",
    "annual-report", "annual_report", "report", "financial", "news", "press",
    "event", "career", "job", "rfp", "campaign", "partner",
)

# What we ask the link picker to cover, in plain words.
# Financial documents are the point of the exercise, so they are found by
# keyword in code and given reserved slots — never left to the model's choice.
STRONG_REPORT_TERMS = (
    "990", "annual report", "annualreport", "annual-report", "annual_report",
    "financial statement", "audited financial", "form 990", "form-990",
)
WEAK_REPORT_TERMS = (
    "financial", "audit", "impact report", "impact-report", "annual", "irs",
    "tax return", "tax-return", "transparency", "accountability", "report",
)

# Phrases that mark the pages of a PDF actually worth reading. Annual reports
# put the numbers at the back, well past any fixed page cap.
PDF_FINANCIAL_MARKERS = (
    "total revenue", "total expenses", "statement of activities",
    "statement of financial position", "independent auditor", "net assets",
    "total assets", "functional expenses", "balance sheet", "total support",
)

# Keyword -> schema group, for the deterministic link picker (the fallback
# when the model call fails, and the selector used before it exists).
GROUP_KEYWORDS: dict[str, tuple[str, ...]] = {
    "financials": STRONG_REPORT_TERMS + ("financials", "finances"),
    "about": ("about", "mission", "who-we-are", "our-story", "history"),
    "programs": ("program", "what-we-do", "our-work", "initiative", "services"),
    "leadership": ("leadership", "our-team", "meet-the-team", "staff", "board",
                   "governance", "executive", "trustees"),
    "careers": ("career", "/jobs", "job-openings", "employment", "vacanc",
                "work-with-us", "join-us", "join-our-team"),
    "news": ("news", "press", "blog", "media", "stories"),
    "events": ("event", "gala", "fundrais", "walk", "conference"),
    "rfps": ("rfp", "procurement", "tender", "request-for-proposal"),
    "campaign": ("campaign", "appeal"),
    "partners": ("partner", "funder", "supporter", "sponsor", "corporate"),
    "contact": ("contact",),
}

# Order the extraction prompt is filled in when the character budget is tight.
GROUP_PRIORITY = (
    "financials", "about", "programs", "leadership", "careers", "news",
    "events", "campaign", "rfps", "partners", "contact",
)

SCHEMA_GROUPS = (
    "about/mission", "programs", "leadership/team", "annual report or financials PDF",
    "news/press", "events", "careers/jobs", "RFPs/procurement", "campaign",
    "partners/funders", "memberships",
)

log = logging.getLogger("scraper")

# Token usage accumulated across the whole run, keyed by model.
USAGE: dict[str, dict[str, int]] = {}


def record_usage(model: str, input_tokens: int, output_tokens: int) -> None:
    """Add one API call's token counts to the run total."""
    bucket = USAGE.setdefault(model, {"input": 0, "output": 0})
    bucket["input"] += input_tokens
    bucket["output"] += output_tokens


def usage_cost_usd(usage: dict[str, dict[str, int]]) -> float:
    """Convert a token-usage dict into USD using PRICES."""
    total = 0.0
    for model, counts in usage.items():
        price_in, price_out = PRICES.get(model, (0.0, 0.0))
        total += counts["input"] / 1_000_000 * price_in
        total += counts["output"] / 1_000_000 * price_out
    return round(total, 6)


def slugify(text: str) -> str:
    """Filesystem-safe slug used for `output/<slug>.json`."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "org"


# ---------------------------------------------------------------------------
# 2. SCHEMA
# ---------------------------------------------------------------------------
# Justification lives in the README: every field answers one of four sales
# questions — Is this a good fit? Can they afford it? Who do I contact?
# Why reach out now?

Seniority = Literal["executive", "senior", "mid", "entry", "unknown"]
GeographicScope = Literal["local", "national", "international"]
EventType = Literal["gala", "walk_run_ride", "auction", "conference", "community", "other"]
CampaignStatus = Literal["active", "completed", "announced", "unknown"]
ProjectStatus = Literal["planned", "in_progress", "completed", "unknown"]
SizeBucket = Literal["<1M", "1M-10M", "10M-100M", "100M+", "unknown"]
FinancialsSource = Literal["propublica", "annual_report", "none"]
CauseSource = Literal["irs_ntee", "llm"]
ResolutionMethod = Literal["url_given", "llm_guess_verified"]
PageType = Literal["html", "pdf", "rss", "sitemap"]
FetchMethod = Literal["http", "browser"]


class Organization(BaseModel):
    name: str
    website: str
    country: str | None = None
    hq_city: str | None = None
    ein: str | None = None
    year_founded: int | None = None


class Program(BaseModel):
    name: str
    description: str | None = None


class CauseArea(BaseModel):
    ntee_code: str | None = None
    major_group: str = "Unknown"
    source: CauseSource | None = None


class Fit(BaseModel):
    mission: str | None = None
    programs: list[Program] = Field(default_factory=list)
    cause_area: CauseArea = Field(default_factory=CauseArea)
    geographic_scope: GeographicScope | None = None


class FinancialYear(BaseModel):
    fiscal_year: int
    revenue: float | None = None
    expenses: float | None = None
    total_assets: float | None = None


class ProPublicaMatch(BaseModel):
    name: str
    ein: str
    match_score: float


class Financials(BaseModel):
    years: list[FinancialYear] = Field(default_factory=list)
    revenue_growth_pct: float | None = None       # computed
    size_bucket: SizeBucket = "unknown"           # computed
    employee_count: int | None = None
    auditor_firm: str | None = None
    source: FinancialsSource = "none"
    propublica_match: ProPublicaMatch | None = None


class Leader(BaseModel):
    name: str
    title: str | None = None


class Contacts(BaseModel):
    leaders: list[Leader] = Field(default_factory=list)
    email: str | None = None
    phone: str | None = None
    contact_url: str | None = None


class OpenRole(BaseModel):
    title: str
    seniority: Seniority = "unknown"
    url: str | None = None


class RFP(BaseModel):
    title: str
    deadline: str | None = None
    url: str | None = None


class LeadershipChange(BaseModel):
    description: str
    date: str | None = None
    source_url: str | None = None


class CapitalProject(BaseModel):
    description: str
    status: ProjectStatus = "unknown"
    source_url: str | None = None


class Campaign(BaseModel):
    name: str
    goal: float | None = None
    raised: float | None = None
    status: CampaignStatus = "unknown"
    source_url: str | None = None


class BuyingSignals(BaseModel):
    open_roles: list[OpenRole] = Field(default_factory=list)
    executive_search_open: bool = False           # computed
    rfps: list[RFP] = Field(default_factory=list)
    leadership_changes: list[LeadershipChange] = Field(default_factory=list)
    capital_projects: list[CapitalProject] = Field(default_factory=list)
    campaign: Campaign | None = None


class NewsItem(BaseModel):
    title: str
    date: str | None = None
    url: str | None = None


class Event(BaseModel):
    name: str
    type: EventType = "other"
    date: str | None = None
    url: str | None = None


class Timing(BaseModel):
    recent_news: list[NewsItem] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)


class Funder(BaseModel):
    name: str
    amount: float | None = None


class Network(BaseModel):
    funders: list[Funder] = Field(default_factory=list)
    memberships: list[str] = Field(default_factory=list)


class CrawledPage(BaseModel):
    url: str
    type: PageType
    method: FetchMethod = "http"
    chars: int = 0


class FailedPage(BaseModel):
    url: str
    error: str


class Meta(BaseModel):
    input: str
    resolved_url: str | None = None
    resolution_method: ResolutionMethod | None = None
    crawled_at: str | None = None
    pages_crawled: list[CrawledPage] = Field(default_factory=list)
    failed_pages: list[FailedPage] = Field(default_factory=list)
    field_sources: dict[str, list[str]] = Field(default_factory=dict)
    tokens: dict[str, dict[str, int]] = Field(default_factory=dict)
    cost_usd: float = 0.0
    warnings: list[str] = Field(default_factory=list)


class NonprofitProfile(BaseModel):
    """The full output document written to `output/<slug>.json`."""

    organization: Organization
    fit: Fit = Field(default_factory=Fit)
    financials: Financials = Field(default_factory=Financials)
    contacts: Contacts = Field(default_factory=Contacts)
    buying_signals: BuyingSignals = Field(default_factory=BuyingSignals)
    timing: Timing = Field(default_factory=Timing)
    network: Network = Field(default_factory=Network)
    meta: Meta


class ExtractionResult(BaseModel):
    """The subset of the schema the extraction model is asked to fill in.

    Computed fields (size bucket, growth, executive flag, NTEE mapping) are
    deliberately absent — code owns those, not the LLM.
    """

    organization: Organization
    fit: Fit = Field(default_factory=Fit)
    financials: Financials = Field(default_factory=Financials)
    contacts: Contacts = Field(default_factory=Contacts)
    buying_signals: BuyingSignals = Field(default_factory=BuyingSignals)
    timing: Timing = Field(default_factory=Timing)
    network: Network = Field(default_factory=Network)
    field_sources: dict[str, list[str]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 3. HTTP HELPERS
# ---------------------------------------------------------------------------

_HTTP: httpx.Client | None = None


def http_client() -> httpx.Client:
    """One shared client, so connections are reused across a run."""
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        )
    return _HTTP


def origin_of(url: str) -> str:
    """https://www.example.org/a/b?c -> https://www.example.org"""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def fetch(url: str) -> dict[str, Any]:
    """GET `url` with timeout, user agent and retries.

    Retries only what's worth retrying: network errors, timeouts, 429 and 5xx.
    A 404 comes straight back as a failure.

    Returns {"ok": True, "url": final_url, "content": bytes, "content_type": str,
    "truncated": bool} or {"ok": False, "url": url, "error": str}.
    """
    last_error = "unknown error"
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
        try:
            response = http_client().get(url)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.debug("fetch error %s (attempt %d): %s", url, attempt + 1, last_error)
            continue
        if response.status_code in RETRYABLE_STATUS:
            last_error = f"HTTP {response.status_code}"
            log.debug("fetch %s (attempt %d): %s", url, attempt + 1, last_error)
            continue
        if response.status_code >= 400:
            return {"ok": False, "url": url, "error": f"HTTP {response.status_code}"}
        body = response.content
        return {
            "ok": True,
            "url": str(response.url),
            "content": body[:MAX_DOWNLOAD_BYTES],
            "content_type": response.headers.get("content-type", "")
            .split(";")[0]
            .strip()
            .lower(),
            "truncated": len(body) > MAX_DOWNLOAD_BYTES,
        }
    return {"ok": False, "url": url, "error": last_error}


@lru_cache(maxsize=64)
def _robots(origin: str) -> robotparser.RobotFileParser | None:
    """Parsed robots.txt for one origin, or None if there isn't a usable one."""
    result = fetch(urljoin(origin, "/robots.txt"))
    if not result["ok"]:
        return None
    parser = robotparser.RobotFileParser()
    parser.parse(result["content"].decode("utf-8", "replace").splitlines())
    return parser


def robots_allows(base_url: str, url: str) -> bool:
    """True if the site's robots.txt permits fetching `url`.

    A missing or unparseable robots.txt is treated as "allowed", which is the
    conventional reading.
    """
    parser = _robots(origin_of(base_url))
    if parser is None:
        return True
    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:  # a malformed robots.txt shouldn't stop the crawl
        return True


def robots_sitemaps(base_url: str) -> list[str]:
    """Sitemap: lines declared in robots.txt (often better than guessing)."""
    parser = _robots(origin_of(base_url))
    if parser is None:
        return []
    return list(parser.site_maps() or [])


# ---------------------------------------------------------------------------
# 4. RESOLVE INPUT
# ---------------------------------------------------------------------------

def looks_like_url(value: str) -> bool:
    """Heuristic: a URL has a dot and no spaces, or starts with http.

    "Feeding America" and "charity: water" are names; "khanacademy.org" and
    "www.example.org/about" are URLs.
    """
    value = value.strip()
    if value.lower().startswith(("http://", "https://")):
        return True
    return "." in value and " " not in value


def normalise_url(value: str) -> str:
    """Add https:// if missing, strip tracking params and the fragment."""
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")):
        value = "https://" + value.lstrip("/")
    parts = urlsplit(value)
    kept = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(TRACKING_PARAM_PREFIXES)
        and key.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path or "/", urlencode(kept), "")
    )


def guess_url_from_name(name: str) -> dict[str, Any]:
    """Ask the cheap model for the org's official URL and a confidence level."""
    raise NotImplementedError


def verify_url_matches_name(name: str, url: str, html: str) -> bool:
    """Fuzzy-match the org name against the page title and leading text."""
    raise NotImplementedError


def resolve_input(value: str) -> dict[str, Any]:
    """Turn a name or URL into {"url", "resolution_method"} or raise."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 5. LINK DISCOVERY
# ---------------------------------------------------------------------------

def registrable_domain(host: str) -> str:
    """example.org from www.example.org; example.co.uk from give.example.co.uk."""
    host = host.lower().split(":")[0].removeprefix("www.")
    labels = [label for label in host.split(".") if label]
    if (
        len(labels) >= 3
        and labels[-2] in MULTI_LABEL_SUFFIXES
        and len(labels[-1]) == 2
    ):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def extract_links(html: str, base_url: str) -> list[dict[str, str]]:
    """Same-site anchors (plus PDFs anywhere) as {"text", "url", "source"}.

    Subdomains count as the same site — annual reports often sit on
    `give.` or `reports.` hosts — and PDFs are kept whatever the domain,
    because reports are frequently served from a CDN.
    """
    soup = BeautifulSoup(html, "lxml")
    home_domain = registrable_domain(urlsplit(base_url).netloc)
    links: list[dict[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith("#"):
            continue
        url = urljoin(base_url, href)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            continue
        is_pdf = parts.path.lower().endswith(".pdf")
        if registrable_domain(parts.netloc) != home_domain and not is_pdf:
            continue
        # A second unescape pass: CMS exports often double-encode, leaving
        # literal "&#x27;" in the text BeautifulSoup already decoded once.
        text = unescape(" ".join(anchor.get_text(" ", strip=True).split()))[:120]
        if not text:
            text = unescape(
                (anchor.get("title") or anchor.get("aria-label") or "")
            ).strip()[:120]
        links.append({"text": text, "url": normalise_url(url), "source": "homepage"})
    return links


def _sitemap_locs(xml: bytes) -> tuple[list[str], bool]:
    """(<loc> values, is this a sitemap index?) — handles .xml.gz too."""
    if xml[:2] == b"\x1f\x8b":
        try:
            xml = gzip.decompress(xml)
        except OSError:
            return [], False
    soup = BeautifulSoup(xml, "xml")
    is_index = soup.find("sitemapindex") is not None
    return [loc.get_text(strip=True) for loc in soup.find_all("loc")], is_index


def discover_sitemap_urls(base_url: str) -> list[str]:
    """URLs from robots.txt's Sitemap: lines, else /sitemap.xml.

    A sitemap index is followed one level down. Big sites blow past the cap
    easily, so keyword-matching URLs are kept first — 200 arbitrary event
    pages would crowd out the annual report.
    """
    origin = origin_of(base_url)
    queue = robots_sitemaps(base_url) or [urljoin(origin, "/sitemap.xml")]
    queue = queue[:MAX_SITEMAPS]
    seen: set[str] = set()
    found: list[str] = []
    while queue and len(found) < MAX_SITEMAP_URLS * 10:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen:
            continue
        seen.add(sitemap_url)
        result = fetch(sitemap_url)
        if not result["ok"]:
            log.debug("sitemap %s: %s", sitemap_url, result["error"])
            continue
        locs, is_index = _sitemap_locs(result["content"])
        if is_index:
            queue.extend(loc for loc in locs[:MAX_SITEMAPS] if loc not in seen)
        else:
            found.extend(locs)
    interesting, rest = [], []
    for url in found:
        low = url.lower()
        bucket = interesting if any(k in low for k in LINK_FALLBACK_KEYWORDS) else rest
        bucket.append(url)
    return (interesting + rest)[:MAX_SITEMAP_URLS]


def discover_feed(html: str, base_url: str) -> str | None:
    """RSS/Atom feed from <link rel=alternate>, else try /feed and /rss."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("link", href=True):
        rel = " ".join(tag.get("rel") or []).lower()
        type_ = (tag.get("type") or "").lower()
        if "alternate" in rel and ("rss+xml" in type_ or "atom+xml" in type_):
            return normalise_url(urljoin(base_url, tag["href"]))
    for path in ("/feed", "/rss"):
        candidate = urljoin(origin_of(base_url), path)
        result = fetch(candidate)
        if result["ok"] and (
            "xml" in result["content_type"]
            or result["content"].lstrip()[:5] in (b"<?xml", b"<rss")
        ):
            return candidate
    return None


def clean_candidates(
    links: list[dict[str, str]], home_url: str | None = None
) -> list[dict[str, str]]:
    """Deduplicate and drop junk (social, login, cart, donate, legal, assets).

    When the same URL appears twice, the one with the longer anchor text wins
    — that's the link the picker can actually reason about.
    """
    home_key = normalise_url(home_url).lower().rstrip("/") if home_url else None
    best: dict[str, dict[str, str]] = {}
    for link in links:
        url = link["url"]
        low = url.lower()
        path = urlsplit(low).path
        if any(pattern in low for pattern in LINK_JUNK_PATTERNS):
            continue
        if path.endswith(NON_PAGE_EXTENSIONS):
            continue
        key = low.rstrip("/")
        if home_key is not None and key == home_key:
            continue  # the homepage is already downloaded
        current = best.get(key)
        if current is None or len(link["text"]) > len(current["text"]):
            best[key] = link
    return list(best.values())


def discover_candidates(home_url: str, home_html: str) -> dict[str, Any]:
    """Everything link-related for one site: candidates, feed, feed items."""
    links = extract_links(home_html, home_url)
    sitemap_urls = discover_sitemap_urls(home_url)
    links += [
        {"text": "", "url": normalise_url(url), "source": "sitemap"}
        for url in sitemap_urls
    ]
    candidates = clean_candidates(links, home_url=home_url)

    feed_url = discover_feed(home_html, home_url)
    feed_items: list[dict[str, str | None]] = []
    if feed_url:
        result = fetch(feed_url)
        if result["ok"]:
            feed_items = parse_feed(result["content"])
        else:
            log.debug("feed %s: %s", feed_url, result["error"])
    return {
        "candidates": candidates,
        "sitemap_count": len(sitemap_urls),
        "feed_url": feed_url,
        "feed_items": feed_items,
    }


# ---------------------------------------------------------------------------
# 6. PARSING
# ---------------------------------------------------------------------------

def _footer_text(html: str) -> str:
    """Footer text, which is where the EIN, phone and address usually live.

    trafilatura strips footers on purpose, so this is pulled out separately
    and appended to the homepage only — the same footer on six pages would
    just burn tokens.
    """
    soup = BeautifulSoup(html, "lxml")
    parts = []
    for tag in soup.find_all(["footer"]) + soup.find_all(
        attrs={"class": re.compile(r"footer", re.I)}
    ):
        parts.append(" ".join(tag.get_text(" ", strip=True).split()))
    seen, out = set(), []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return unescape(" ".join(out))[:3000]


def html_to_text(html: str, url: str, keep_footer: bool = False) -> str:
    """Main text via trafilatura, falling back to a stripped BeautifulSoup."""
    text = ""
    try:
        text = (
            trafilatura.extract(
                html, url=url, include_comments=False, include_tables=True,
                favor_recall=True,
            )
            or ""
        )
    except Exception as exc:  # trafilatura is strict about malformed markup
        log.debug("trafilatura failed on %s: %s", url, exc)
    if len(text) < MIN_TEXT_CHARS:
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        fallback = " ".join(soup.get_text(" ", strip=True).split())
        if len(fallback) > len(text):
            text = fallback
    if keep_footer:
        footer = _footer_text(html)
        if footer and footer not in text:
            text = f"{text}\n\n--- page footer ---\n{footer}"
    return text.strip()


def pdf_to_text(data: bytes) -> str:
    """Text from a PDF: the opening pages plus any page carrying financials.

    A fixed "first N pages" cap reads the glossy introduction and misses the
    statements at the back, so pages are scanned locally (no tokens) for the
    markers in PDF_FINANCIAL_MARKERS and pulled in as well. Page numbers are
    labelled so the model can see where the gaps are.
    """
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"unreadable PDF: {exc}") from exc

    with document:
        total = document.page_count
        chosen = list(range(min(MAX_PDF_HEAD_PAGES, total)))
        for index in range(len(chosen), total):
            if len(chosen) >= MAX_PDF_PAGES:
                break
            try:
                page_text = document[index].get_text()
            except Exception:
                continue
            low = page_text.lower()
            if any(marker in low for marker in PDF_FINANCIAL_MARKERS):
                chosen.append(index)

        parts = []
        for index in chosen:
            try:
                page_text = " ".join(document[index].get_text().split())
            except Exception as exc:
                log.debug("pdf page %d failed: %s", index, exc)
                continue
            if page_text:
                parts.append(f"[page {index + 1} of {total}] {page_text}")

    if not parts:
        raise ValueError("PDF contained no extractable text (likely scanned)")
    return "\n\n".join(parts)


def render_with_browser(url: str) -> str:
    """Playwright fallback for JS-rendered pages; returns HTML."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle",
                      timeout=int(BROWSER_TIMEOUT * 1000))
            return page.content()
        finally:
            browser.close()


def _feed_field(item: Any, names: tuple[str, ...]) -> Any:
    """First child tag matching any of `names`, case-insensitively."""
    lowered = {name.lower() for name in names}
    return item.find(lambda tag: tag.name and tag.name.lower() in lowered)


def parse_feed(xml: bytes) -> list[dict[str, str | None]]:
    """RSS/Atom items as {"title", "date", "link"}.

    Feeds are the cheapest good data on a site: real publication dates, no
    LLM needed, no date guessing from prose.
    """
    soup = BeautifulSoup(xml, "xml")
    entries = soup.find_all("item") or soup.find_all("entry")
    items: list[dict[str, str | None]] = []
    for entry in entries[:MAX_RSS_ITEMS]:
        title = _feed_field(entry, ("title",))
        date = _feed_field(entry, ("pubDate", "published", "updated", "date"))
        link = _feed_field(entry, ("link",))
        href = None
        if link is not None:
            href = link.get("href") or link.get_text(strip=True) or None
        items.append(
            {
                "title": title.get_text(" ", strip=True) if title else None,
                "date": date.get_text(strip=True) if date else None,
                "link": href,
            }
        )
    return items


def report_score(text: str, url: str) -> int:
    """How much a link looks like an annual report, 990 or financial statement.

    Anchor text and URL are both scored; PDFs and recent years get a bump, so
    "2025 Annual Report (PDF)" outranks a 2016 one.
    """
    blob = f"{text} {url}".lower().replace("%20", " ")
    # Presence, not count: "990" and "form 990" are the same signal seen twice.
    score = 4 if any(term in blob for term in STRONG_REPORT_TERMS) else 0
    score += 1 if any(term in blob for term in WEAK_REPORT_TERMS) else 0
    if not score:
        return 0
    if urlsplit(url.lower()).path.endswith(".pdf"):
        score += 2
    # Recency outweighs wording: the current year's filing is the one worth
    # reading, even when an older one happens to be better labelled.
    years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", blob)]
    if years:
        this_year = int(time.strftime("%Y"))
        newest = min(max(years), this_year)
        score += max(0, 8 - (this_year - newest))
    return score


def rank_reports(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Report-ish links, best first."""
    scored = [(report_score(link["text"], link["url"]), link) for link in links]
    return [link for score, link in sorted(scored, key=lambda p: -p[0]) if score > 0]


def groups_for(text: str, url: str) -> list[str]:
    """Which schema groups a link's text and URL suggest."""
    blob = f"{text} {url}".lower()
    return [
        group
        for group, keywords in GROUP_KEYWORDS.items()
        if any(keyword in blob for keyword in keywords)
    ]


def pick_links_by_keyword(candidates: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Deterministic picker: the best link for each schema group, in priority
    order. Used as the fallback when the model call fails."""
    best: dict[str, tuple[int, dict[str, str]]] = {}
    for link in candidates:
        covers = groups_for(link["text"], link["url"])
        score = report_score(link["text"], link["url"]) + len(covers)
        for group in covers:
            current = best.get(group)
            if current is None or score > current[0]:
                best[group] = (score, link)

    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in GROUP_PRIORITY:
        if group not in best or len(chosen) >= MAX_PAGES:
            continue
        link = best[group][1]
        if link["url"] in seen:
            continue
        seen.add(link["url"])
        chosen.append(
            {
                "url": link["url"],
                "covers": groups_for(link["text"], link["url"]),
                "reason": f"keyword match for {group}",
            }
        )
    return chosen


def reserve_report_links(candidates: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Financial documents, chosen in code and never left to the model."""
    reserved: list[dict[str, Any]] = []
    for link in rank_reports(candidates)[:RESERVED_REPORT_SLOTS]:
        reserved.append(
            {
                "url": link["url"],
                "covers": ["financials"],
                "reason": "reserved slot: looks like an annual report or 990",
            }
        )
    return reserved


def _parse_document(
    result: dict[str, Any], picked: dict[str, Any], use_browser: bool
) -> dict[str, Any]:
    """Turn one fetched response into a text document record."""
    url = result["url"]
    content_type = result["content_type"]
    is_pdf = content_type == "application/pdf" or urlsplit(
        url.lower()
    ).path.endswith(".pdf")

    if is_pdf:
        text = pdf_to_text(result["content"])
        return {
            "url": url, "type": "pdf", "method": "http", "text": text,
            "covers": picked.get("covers", []), "html": None,
        }

    html = result["content"].decode("utf-8", "replace")
    method = "http"
    text = html_to_text(html, url, keep_footer=picked.get("is_home", False))
    if len(text) < MIN_TEXT_CHARS and use_browser:
        log.info("thin page (%d chars), rendering with browser: %s", len(text), url)
        try:
            html = render_with_browser(url)
            text = html_to_text(html, url, keep_footer=picked.get("is_home", False))
            method = "browser"
        except Exception as exc:
            log.warning("browser render failed for %s: %s", url, exc)
    return {
        "url": url, "type": "html", "method": method, "text": text,
        "covers": picked.get("covers", []), "html": html,
    }


def collect_documents(
    home_url: str,
    home_result: dict[str, Any],
    candidates: list[dict[str, str]],
    picked: list[dict[str, Any]],
    use_browser: bool = True,
) -> dict[str, Any]:
    """Fetch and parse the chosen pages, following report PDFs one level down.

    Reserved financial slots are queued first, then the picker's choices. When
    a fetched HTML page links to an annual report or 990 — which is where they
    almost always live, not on the homepage — the best one is pulled in too.
    """
    documents: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    home_doc = _parse_document(
        home_result, {"covers": ["about"], "is_home": True}, use_browser
    )
    documents.append(home_doc)

    queue = reserve_report_links(candidates)
    seen = {normalise_url(home_url).rstrip("/")}
    seen.update(item["url"].rstrip("/") for item in queue)
    for item in picked:
        if item["url"].rstrip("/") not in seen:
            seen.add(item["url"].rstrip("/"))
            queue.append(item)

    # A report PDF found on a fetched page jumps the queue.
    pdfs_followed = 0
    fetched = 0
    while queue and fetched < MAX_PAGES:
        item = queue.pop(0)
        url = item["url"]
        if not robots_allows(home_url, url):
            log.info("robots.txt disallows %s", url)
            failed.append({"url": url, "error": "disallowed by robots.txt"})
            continue
        result = fetch(url)
        fetched += 1
        if not result["ok"]:
            log.warning("skipping %s: %s", url, result["error"])
            failed.append({"url": url, "error": result["error"]})
            continue
        try:
            document = _parse_document(result, item, use_browser)
        except Exception as exc:
            log.warning("could not parse %s: %s", url, exc)
            failed.append({"url": url, "error": str(exc)})
            continue
        documents.append(document)

        if (
            document["type"] == "html"
            and document["html"]
            and pdfs_followed < MAX_REPORT_PDFS
        ):
            deeper = rank_reports(
                [
                    link
                    for link in extract_links(document["html"], document["url"])
                    if link["url"].lower().endswith(".pdf")
                    and link["url"].rstrip("/") not in seen
                ]
            )
            for link in deeper:
                if pdfs_followed >= MAX_REPORT_PDFS:
                    break
                # The same PDF is often linked twice under different anchor
                # text; without this it would eat both report slots.
                key = link["url"].rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                pdfs_followed += 1
                queue.insert(
                    0,
                    {
                        "url": link["url"],
                        "covers": ["financials"],
                        "reason": f"report PDF linked from {document['url']}",
                    },
                )

    return {"documents": apply_budget(documents), "failed_pages": failed}


def apply_budget(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cap each document and the total, spending the budget on what matters.

    The homepage stays first; everything else is ordered by GROUP_PRIORITY so
    an annual report is never the thing that gets cut.
    """
    def rank(document: dict[str, Any]) -> int:
        covers = document.get("covers") or []
        ranks = [GROUP_PRIORITY.index(c) for c in covers if c in GROUP_PRIORITY]
        return min(ranks) if ranks else len(GROUP_PRIORITY)

    ordered = documents[:1] + sorted(documents[1:], key=rank)
    budget = MAX_TOTAL_CHARS
    kept: list[dict[str, Any]] = []
    for document in ordered:
        if budget <= 0:
            log.info("character budget spent, dropping %s", document["url"])
            continue
        text = document["text"][:MAX_CHARS_PER_DOC][:budget]
        budget -= len(text)
        document = {**document, "text": text, "chars": len(text)}
        document.pop("html", None)
        kept.append(document)
    return kept


# ---------------------------------------------------------------------------
# 7. LLM CALLS
# ---------------------------------------------------------------------------

def pick_links(
    candidates: list[dict[str, str]], org_hint: str
) -> list[dict[str, Any]]:
    """LLM call #1 (cheap): choose up to MAX_PAGES URLs worth reading."""
    raise NotImplementedError


def extract(documents: list[dict[str, Any]], feed_items: list[dict[str, Any]]) -> ExtractionResult:
    """LLM call #2: read the documents and fill in the schema."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 8. PROPUBLICA
# ---------------------------------------------------------------------------

# DEFERRED DECISION (2026-09-22): we currently fetch both the 990 PDF and
# ProPublica's structured filings, which overlap. PDFs are ~45% of the
# extraction payload (~$0.005-0.010 per org, ~$2.5-5k across 500k). Once this
# enrichment works, measure how far ProPublica's revenue/expenses/assets agree
# with the PDF-derived figures, then decide whether to stop fetching 990 PDFs
# for US orgs. Annual reports stay either way - campaigns, auditor firm and
# programme detail are not in IRS data. Do not drop them before measuring.


def propublica_search(name: str) -> list[dict[str, Any]]:
    """Search the Nonprofit Explorer by organisation name."""
    raise NotImplementedError


def propublica_organization(ein: str) -> dict[str, Any] | None:
    """Fetch one organisation, including its filings."""
    raise NotImplementedError


def enrich_financials(profile: NonprofitProfile) -> None:
    """Attach multi-year IRS financials and the NTEE code, in place."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 9. POST-PROCESSING
# ---------------------------------------------------------------------------

def revenue_growth_pct(years: list[FinancialYear]) -> float | None:
    """CAGR between the oldest and newest year with revenue; None if < 2."""
    raise NotImplementedError


def size_bucket_for(revenue: float | None) -> str:
    """Map the latest revenue onto a fixed size bucket."""
    raise NotImplementedError


def major_group_for_ntee(ntee_code: str | None) -> str | None:
    """First letter of the NTEE code -> major group name."""
    raise NotImplementedError


def post_process(profile: NonprofitProfile) -> None:
    """Fill every computed field, in place."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 10. OUTPUT
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "name", "website", "country", "hq_city", "ein", "cause_area",
    "geographic_scope", "size_bucket", "latest_revenue", "latest_fiscal_year",
    "revenue_growth_pct", "mission", "num_programs", "top_leader_name",
    "top_leader_title", "email", "phone", "num_open_roles",
    "executive_search_open", "num_rfps", "has_leadership_change",
    "has_active_campaign", "campaign_name", "num_events", "latest_news_title",
    "latest_news_date", "funders", "memberships", "auditor_firm", "crawled_at",
    "cost_usd", "num_warnings",
]


def write_json(profile: NonprofitProfile) -> Path:
    """Write output/<slug>.json and return the path."""
    raise NotImplementedError


def profile_to_row(profile: NonprofitProfile) -> dict[str, Any]:
    """Flatten one profile into a CSV row (lists become counts or joins)."""
    raise NotImplementedError


def rebuild_combined_csv() -> Path:
    """Rebuild output/combined.csv from every JSON file in output/."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 11. MAIN
# ---------------------------------------------------------------------------

def process_org(value: str, use_browser: bool = True) -> NonprofitProfile:
    """Run the whole pipeline for one organisation name or URL."""
    raise NotImplementedError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Turn a nonprofit's website into a structured profile."
    )
    parser.add_argument(
        "org", nargs="?", help="organisation name or website URL"
    )
    parser.add_argument(
        "--batch", metavar="FILE", help="file with one name or URL per line"
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="disable the Playwright fallback for JavaScript-heavy sites",
    )
    parser.add_argument(
        "--links-only", action="store_true",
        help="stop after link discovery and print the candidates (no LLM calls)",
    )
    parser.add_argument(
        "--crawl-only", action="store_true",
        help="crawl and parse pages with the keyword picker, printing chars "
             "per page (no LLM calls)",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="debug logging"
    )
    args = parser.parse_args(argv)
    if not args.org and not args.batch:
        parser.error("provide an organisation name/URL or --batch FILE")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    if args.links_only or args.crawl_only:
        if not args.org:
            log.error("debug modes take a single organisation, not --batch")
            return 2
        return show_candidates(
            args.org, crawl=args.crawl_only, use_browser=not args.no_browser
        )

    raise NotImplementedError("full pipeline lands in a later milestone")


def show_candidates(
    value: str, crawl: bool = False, use_browser: bool = True
) -> int:
    """Debug view for the crawl half of the pipeline — no LLM, no cost."""
    if not looks_like_url(value):
        log.error("--links-only needs a URL for now; name resolution is next.")
        return 2
    home_url = normalise_url(value)
    log.info("fetching %s", home_url)
    result = fetch(home_url)
    if not result["ok"]:
        log.error("could not fetch homepage: %s", result["error"])
        return 1

    home_url = result["url"]
    home_html = result["content"].decode("utf-8", "replace")
    found = discover_candidates(home_url, home_html)

    print(f"\nhomepage      {home_url}  ({len(home_html):,} chars html)")
    print(f"robots.txt    {'present' if _robots(origin_of(home_url)) else 'none'}")
    print(f"sitemap urls  {found['sitemap_count']}")
    print(f"feed          {found['feed_url'] or 'none'}")

    candidates = found["candidates"]
    print(f"\ncandidates    {len(candidates)}\n")
    for candidate in candidates[:MAX_LINK_CANDIDATES]:
        text = (candidate["text"] or "-")[:55]
        print(f"  {candidate['source']:9} {text:<55}  {candidate['url']}")
    if len(candidates) > MAX_LINK_CANDIDATES:
        print(f"  ... {len(candidates) - MAX_LINK_CANDIDATES} more")

    if found["feed_items"]:
        print(f"\nfeed items    {len(found['feed_items'])}\n")
        for item in found["feed_items"]:
            print(f"  {(item['date'] or '?')[:31]:<31}  {(item['title'] or '-')[:70]}")

    if not crawl:
        print()
        return 0

    reserved = reserve_report_links(candidates)
    print(f"\nreserved financial slots  {len(reserved)}")
    for item in reserved:
        print(f"  {item['url']}")

    picked = pick_links_by_keyword(candidates)
    print(f"\nkeyword picks             {len(picked)}")
    for item in picked:
        print(f"  {','.join(item['covers'])[:40]:<40}  {item['url']}")

    collected = collect_documents(
        home_url, result, candidates, picked, use_browser=use_browser
    )
    print(f"\ndocuments fetched         {len(collected['documents'])}\n")
    total = 0
    for document in collected["documents"]:
        total += document["chars"]
        print(
            f"  {document['type']:<5} {document['method']:<8} "
            f"{document['chars']:>7,} chars  {document['url'][:88]}"
        )
    print(f"\n  total                   {total:>7,} chars "
          f"(budget {MAX_TOTAL_CHARS:,})")
    if collected["failed_pages"]:
        print(f"\nfailed                    {len(collected['failed_pages'])}")
        for failure in collected["failed_pages"]:
            print(f"  {failure['error'][:40]:<40}  {failure['url'][:70]}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
