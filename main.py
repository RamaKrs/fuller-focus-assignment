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
import csv
import datetime as dt
import json
import gzip
import logging
import os
from html import unescape
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib import robotparser
from urllib.parse import (
    parse_qsl, quote_plus, urlencode, urljoin, urlsplit, urlunsplit,
)

import anthropic
import httpx
import pymupdf
import trafilatura
from bs4 import BeautifulSoup
from rapidfuzz import fuzz
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

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
RESERVE_MIN_SCORE = 4        # a reserved slot needs an unambiguous signal
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
MIN_HOME_LINKS = 10          # below this, the homepage is JS-rendered
MAX_DOWNLOAD_BYTES = 10_000_000   # hard cap on any single response body
MAX_SITEMAPS = 5                  # child sitemaps read from a sitemap index
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
# Statuses that usually mean bot protection rather than a missing page. A
# real browser often gets through where a plain HTTP client does not.
BLOCKED_STATUS = frozenset({401, 403, 406, 429, 503})

# Legal suffixes stripped before searching ProPublica. Its search endpoint
# returns 404 for some queries carrying them: "Code for America Labs, Inc."
# and "Code for America Labs Inc" both fail where "Code for America Labs"
# returns the right organisation.
LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "corp",
    "corporation", "co", "plc", "pbc",
})

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

# URL path segments that mark a genuine financial page. Checked as whole
# segments, so "/about/financials" qualifies but "/financial-literacy" — a
# course Khan Academy teaches — does not.
FINANCIAL_PATH_SEGMENTS = frozenset({
    "financials", "financial", "finances", "financial-information",
    "financial-statements", "financial-reports", "annual-report",
    "annual-reports", "annualreport", "annual-reports-and-financials",
    "990", "990s", "990-forms", "form-990", "tax-documents", "tax-returns",
    "reports-and-financials", "accounts",
})

# The subset that means actual tax filings, which outrank a financials hub.
TAX_FORM_SEGMENTS = frozenset({
    "990", "990s", "990-forms", "form-990", "form-990s", "tax-documents",
    "tax-returns", "irs-form-990",
})

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


MajorGroup = Literal[
    "Arts, Culture & Humanities", "Education", "Environment & Animals",
    "Health", "Human Services", "International, Foreign Affairs",
    "Public, Societal Benefit", "Religion Related",
    "Mutual/Membership Benefit", "Unknown",
]


# --- what the extraction model is asked for --------------------------------
# These mirror the schema above minus every computed field. The model is never
# shown size_bucket, revenue_growth_pct, executive_search_open or the NTEE
# code, so it cannot guess at values that code owns.


class OrganizationExtract(BaseModel):
    name: str
    country: str | None = None
    hq_city: str | None = None
    ein: str | None = None
    year_founded: int | None = None


class FitExtract(BaseModel):
    mission: str | None = None
    programs: list[Program] = Field(default_factory=list)
    cause_area: MajorGroup = "Unknown"
    geographic_scope: GeographicScope | None = None


class FinancialsExtract(BaseModel):
    years: list[FinancialYear] = Field(default_factory=list)
    employee_count: int | None = None
    auditor_firm: str | None = None


class BuyingSignalsExtract(BaseModel):
    open_roles: list[OpenRole] = Field(default_factory=list)
    rfps: list[RFP] = Field(default_factory=list)
    leadership_changes: list[LeadershipChange] = Field(default_factory=list)
    capital_projects: list[CapitalProject] = Field(default_factory=list)
    campaign: Campaign | None = None


class FieldSource(BaseModel):
    """Which pages a group of fields was read from."""

    group: str
    urls: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """The subset of the schema the extraction model is asked to fill in."""

    organization: OrganizationExtract
    fit: FitExtract = Field(default_factory=FitExtract)
    financials: FinancialsExtract = Field(default_factory=FinancialsExtract)
    contacts: Contacts = Field(default_factory=Contacts)
    buying_signals: BuyingSignalsExtract = Field(default_factory=BuyingSignalsExtract)
    timing: Timing = Field(default_factory=Timing)
    network: Network = Field(default_factory=Network)
    field_sources: list[FieldSource] = Field(default_factory=list)


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


def fetch_or_render(url: str, use_browser: bool = True) -> dict[str, Any]:
    """fetch(), falling back to a real browser when a page looks bot-blocked.

    codeforamerica.org answers a plain HTTP client with 403 on every page,
    homepage included, but serves headless Chromium normally.
    """
    result = fetch(url)
    if result["ok"]:
        return result
    blocked = any(result["error"] == f"HTTP {status}" for status in BLOCKED_STATUS)
    is_pdf = urlsplit(url.lower()).path.endswith(".pdf")
    if not (blocked and use_browser) or is_pdf:
        return result

    log.info("%s on %s, retrying with a browser", result["error"], url)
    try:
        html = render_with_browser(url)
    except Exception as exc:
        return {
            "ok": False, "url": url,
            "error": f"{result['error']} and browser retry failed: {exc}",
        }
    return {
        "ok": True, "url": url, "content": html.encode("utf-8"),
        "content_type": "text/html", "truncated": False, "rendered": True,
    }


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


class UrlGuess(BaseModel):
    """The cheap model's guess at an organisation's official website."""

    url: str | None
    confidence: Literal["high", "medium", "low"]


GUESS_URL_SYSTEM = """\
Give the official website of the nonprofit organisation named by the user.

- Return the organisation's own homepage, not a directory listing, a news
  article, a social media profile, a donation platform or a Wikipedia page.
- If you are not confident the organisation exists, or you do not know its
  website, return null for the url. A null is more useful than a guess.
- confidence: 'high' if you are sure, 'medium' if the name is ambiguous or
  several organisations share it, 'low' if you are mostly guessing.
"""


def guess_url_from_name(name: str) -> dict[str, Any]:
    """Ask the cheap model for the org's official URL and a confidence level."""
    try:
        response = client().messages.parse(
            model=LINK_MODEL,
            max_tokens=200,
            system=GUESS_URL_SYSTEM,
            messages=[{"role": "user", "content": name}],
            output_format=UrlGuess,
        )
        record_usage(
            LINK_MODEL, response.usage.input_tokens, response.usage.output_tokens
        )
        guess = response.parsed_output
        if guess is None:
            return {"url": None, "confidence": "low"}
        return {"url": guess.url, "confidence": guess.confidence}
    except Exception as exc:
        log.warning("URL guess failed: %s", exc)
        return {"url": None, "confidence": "low"}


def verify_url_matches_name(
    name: str, url: str, html: str, also_check: tuple[str, ...] = ()
) -> float:
    """How well a page looks like it belongs to `name`, 0-100.

    Checked three ways because any one of them can be unhelpful on its own:
    the <title>, the start of the page text, and the domain itself (a site
    whose title is just "Home" often still has the name in the URL).

    A verification step is the whole point of letting a model guess a URL -
    without it a confident wrong answer becomes a confident wrong profile.
    """
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text_head = html_to_text(html, url)[:2000]

    name_compact = re.sub(r"[^a-z0-9]", "", name.lower())
    scores = [
        fuzz.token_set_ratio(name.lower(), title.lower()) if title else 0.0,
        fuzz.token_set_ratio(name.lower(), text_head.lower()) if text_head else 0.0,
    ]
    # Every domain in the chain counts, not just the final one. A rebrand
    # redirects an old name-matching domain to a new one: trusselltrust.org
    # now serves trussell.org.uk, and only the original domain still carries
    # the name we were given.
    for candidate in (url, *also_check):
        domain = registrable_domain(urlsplit(candidate).netloc).rsplit(".", 1)[0]
        domain_compact = re.sub(r"[^a-z0-9]", "", domain)
        if name_compact and domain_compact:
            scores.append(fuzz.ratio(name_compact, domain_compact))
            # Also allow the name to contain the domain ("the trussell trust"
            # against "trusselltrust"), which a plain ratio penalises.
            scores.append(fuzz.partial_ratio(domain_compact, name_compact))
    log.debug("verification scores for %s: title/text/domain = %s", url, scores)
    return max(scores)


class ResolutionError(RuntimeError):
    """The input could not be turned into a verified organisation website."""


def resolve_input(value: str, use_browser: bool = True) -> dict[str, Any]:
    """Turn a name or URL into a verified homepage.

    Returns {"url", "resolution_method", "home"} where `home` is an already
    fetched homepage when resolution needed one, so a verified name lookup
    does not pay for a second download.
    """
    value = value.strip()
    if looks_like_url(value):
        return {
            "url": normalise_url(value),
            "resolution_method": "url_given",
            "home": None,
        }

    guess = guess_url_from_name(value)
    if not guess["url"]:
        raise ResolutionError(
            f"Could not find a website for {value!r}. Please pass the URL directly."
        )

    url = normalise_url(guess["url"])
    log.info("guessed %s for %r (confidence %s)", url, value, guess["confidence"])
    home = fetch_homepage(url, use_browser=use_browser)
    if not home["ok"]:
        raise ResolutionError(
            f"Could not verify website for {value!r}: {url} did not load "
            f"({home['error']}). Please pass the URL directly."
        )

    score = verify_url_matches_name(
        value, home["url"], home["html"], also_check=(url,)
    )
    if score < URL_VERIFY_THRESHOLD:
        raise ResolutionError(
            f"Could not verify website for {value!r}: {home['url']} does not "
            f"look like it (match {score:.0f} of {URL_VERIFY_THRESHOLD} needed). "
            "Please pass the URL directly."
        )

    log.info("verified %s for %r (match %.0f)", home["url"], value, score)
    return {
        "url": home["url"],
        "resolution_method": "llm_guess_verified",
        "home": home,
    }


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
    segments = {seg for seg in urlsplit(url.lower()).path.split("/") if seg}
    strong = any(term in blob for term in STRONG_REPORT_TERMS) or bool(
        segments & FINANCIAL_PATH_SEGMENTS
    )
    # Presence, not count: "990" and "form 990" are the same signal seen twice.
    score = 4 if strong else 0
    score += 1 if any(term in blob for term in WEAK_REPORT_TERMS) else 0
    if not score:
        return 0
    # A 990 or tax-filing page beats a general financials page.
    if segments & TAX_FORM_SEGMENTS or "990" in blob:
        score += 2
    if not urlsplit(url.lower()).path.endswith(".pdf"):
        return score
    score += 2
    # Recency outweighs wording, but only for documents: the current year's
    # filing is the one worth reading, even when an older one happens to be
    # better labelled. On an HTML page a year means the opposite — an
    # archived edition — so no bonus applies there.
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
    strong = [
        link
        for link in rank_reports(candidates)
        if report_score(link["text"], link["url"]) >= RESERVE_MIN_SCORE
    ]
    for link in strong[:RESERVED_REPORT_SLOTS]:
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
    method = "browser" if result.get("rendered") else "http"
    text = html_to_text(html, url, keep_footer=picked.get("is_home", False))
    if len(text) < MIN_TEXT_CHARS and use_browser and method == "http":
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


def fetch_homepage(url: str, use_browser: bool = True) -> dict[str, Any]:
    """Fetch the homepage, rendering it when the raw HTML is unusable.

    This has to happen before link discovery, not after: khanacademy.org's
    homepage is 219k chars of JavaScript with zero anchors in the raw HTML
    and 232 once rendered. Discovering links from the un-rendered page finds
    nothing worth reading.
    """
    result = fetch_or_render(url, use_browser=use_browser)
    if not result["ok"]:
        return {"ok": False, "url": url, "error": result["error"]}

    final_url = result["url"]
    html = result["content"].decode("utf-8", "replace")
    method = "browser" if result.get("rendered") else "http"
    if method == "browser":
        return {"ok": True, "url": final_url, "html": html, "method": method}
    link_count = len(extract_links(html, final_url))
    if use_browser and (
        link_count < MIN_HOME_LINKS
        or len(html_to_text(html, final_url)) < MIN_TEXT_CHARS
    ):
        log.info(
            "homepage looks JS-rendered (%d links in raw html), using browser",
            link_count,
        )
        try:
            html = render_with_browser(final_url)
            method = "browser"
        except Exception as exc:
            log.warning("browser render failed for %s: %s", final_url, exc)
    return {"ok": True, "url": final_url, "html": html, "method": method}


def collect_documents(
    home: dict[str, Any],
    candidates: list[dict[str, str]],
    picked: list[dict[str, Any]],
    use_browser: bool = True,
) -> dict[str, Any]:
    """Fetch and parse the chosen pages, following report PDFs one level down.

    Reserved financial slots are queued first, then the picker's choices. When
    a fetched HTML page links to an annual report or 990 — which is where they
    almost always live, not on the homepage — the best one is pulled in too.
    """
    home_url = home["url"]
    documents: list[dict[str, Any]] = [
        {
            "url": home_url,
            "type": "html",
            "method": home["method"],
            "text": html_to_text(home["html"], home_url, keep_footer=True),
            "covers": ["about"],
            "html": home["html"],  # kept so report PDFs on it can be followed
        }
    ]
    failed: list[dict[str, str]] = []

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
        result = fetch_or_render(url, use_browser=use_browser)
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

_CLIENT: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    """The Anthropic client, built once from ANTHROPIC_API_KEY in .env."""
    global _CLIENT
    if _CLIENT is None:
        load_dotenv(Path(__file__).with_name(".env"))
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and "
                "add your key."
            )
        _CLIENT = anthropic.Anthropic()
    return _CLIENT


SchemaGroup = Literal[
    "about", "programs", "leadership", "careers", "news", "events", "rfps",
    "campaign", "partners", "contact", "financials",
]


class LinkChoice(BaseModel):
    """One chosen link, referenced by its number in the list we sent."""

    index: int
    covers: list[SchemaGroup]
    reason: str


class LinkSelection(BaseModel):
    picks: list[LinkChoice]


PICK_LINKS_SYSTEM = """\
You choose which pages of a nonprofit's website are worth reading in full.

The pages you pick are read by a later step that fills in a structured profile
for a company selling services to this nonprofit. It needs: mission, programs,
leadership and staff, contact details, open roles, RFPs or procurement notices,
leadership changes, capital projects, fundraising campaigns, recent news,
events, funders and partners, and memberships.

Annual reports, 990s and financial statements are already collected separately.
Do not spend a pick on them.

Rules:
- Pick at most {limit} links, fewer if the rest are not worth reading.
- Prefer one page that covers several groups over several narrow pages.
- Prefer hub and overview pages ("Our team", "Newsroom") over a single blog
  post or one person's profile.
- Skip pages that only serve the public (donation forms, shops, find-help
  tools, course content) - they say nothing about the organisation.
- Return each pick's index number exactly as given.
"""


def shortlist_candidates(
    candidates: list[dict[str, str]], limit: int = MAX_LINK_CANDIDATES
) -> list[dict[str, str]]:
    """Trim the candidate list, keeping keyword-relevant links first.

    A big sitemap can push the list past the prompt cap, and the useful pages
    are rarely the first ones alphabetically.
    """
    if len(candidates) <= limit:
        return candidates
    relevant, rest = [], []
    for link in candidates:
        bucket = relevant if groups_for(link["text"], link["url"]) else rest
        bucket.append(link)
    return (relevant + rest)[:limit]


def pick_links(
    candidates: list[dict[str, str]], org_hint: str, limit: int = MAX_PAGES
) -> list[dict[str, Any]]:
    """LLM call #1 (cheap): choose up to `limit` URLs worth reading.

    The model returns list indices rather than URLs: it cannot invent a page
    that way, and the reply is a fraction of the output tokens.

    Falls back to the keyword picker if the call fails twice.
    """
    shortlist = shortlist_candidates(candidates)
    if not shortlist:
        return []
    listing = "\n".join(
        f"{i}. {link['text'] or '(no text)'} | {link['url']}"
        for i, link in enumerate(shortlist)
    )
    prompt = (
        f"Organisation: {org_hint}\n\nCandidate links:\n{listing}\n\n"
        f"Choose at most {limit}."
    )

    for attempt in range(2):
        try:
            response = client().messages.parse(
                model=LINK_MODEL,
                max_tokens=1500,
                system=PICK_LINKS_SYSTEM.format(limit=limit),
                messages=[{"role": "user", "content": prompt}],
                output_format=LinkSelection,
            )
            record_usage(
                LINK_MODEL, response.usage.input_tokens, response.usage.output_tokens
            )
            selection = response.parsed_output
            if selection is None:
                raise ValueError("no parsed output")
            picks = []
            for choice in selection.picks[:limit]:
                if not 0 <= choice.index < len(shortlist):
                    log.debug("picker returned out-of-range index %d", choice.index)
                    continue
                link = shortlist[choice.index]
                picks.append(
                    {
                        "url": link["url"],
                        "covers": list(choice.covers),
                        "reason": choice.reason,
                    }
                )
            if picks:
                return picks
            if not selection.picks:
                # A deliberate "nothing here worth reading" is a valid answer
                # — falling back to keywords would only pick noise.
                log.info("link picker found nothing worth reading")
                return []
            log.warning("link picker returned only invalid indices (attempt %d)",
                        attempt + 1)
        except Exception as exc:
            log.warning("link picker failed (attempt %d): %s", attempt + 1, exc)

    log.info("falling back to keyword link picking")
    return pick_links_by_keyword(candidates)[:limit]


EXTRACT_SYSTEM = """\
You read pages from a nonprofit's website and fill in a structured profile.
The profile is used by a company deciding whether to sell services to this
nonprofit, so accuracy matters more than completeness.

Rules:
- Use ONLY information present in the provided text. Never guess, and never
  use anything you know about this organisation from elsewhere.
- If a field is not in the text, return null, or an empty list. An empty
  field is correct; an invented one is not.
- Dates: ISO format (YYYY-MM-DD, or YYYY-MM, or YYYY) when the text makes
  that unambiguous. Otherwise keep the original wording.
- Money: plain numbers, no currency symbols or thousands separators. Take
  figures from the most recent fiscal year you can identify.
- leadership_changes: only explicit statements - a new chief executive
  appointed, a director retiring, a search under way. Do not infer a change
  from a staff list.
- campaign: a named fundraising campaign with a goal or a total raised. One
  campaign object, or null. Do not treat a general donate button as one.
- open_roles: only actual job postings. seniority 'executive' means C-suite,
  chief officer, executive director, president or vice-president.
- recent_news: dated news or press items the organisation published. Prefer
  the news feed and news pages. Do not turn annual-report highlights or
  undated achievements into news items.
- contact_url: a page whose purpose is contacting the organisation. A careers
  or donate page is not a contact page - use null instead.
- cause_area: choose the single closest major group from the allowed values.
- field_sources: for each group you filled in, list the source URLs the
  information came from, copying each URL exactly as it appears after
  '=== SOURCE (type): ' in the headers. URLs only, no type suffix.

Limits: programs 8, leaders 10, open_roles 15, recent_news 5 (most recent),
events 8, funders 10, memberships 10.

Reply with a single JSON object matching this schema and nothing else - no
prose, no explanation, no markdown fences:

{schema}
"""


# Extraction is validated in code rather than with the API's structured
# outputs. The full profile schema exceeds the grammar compiler's size limit
# ("The compiled grammar is too large"), so the schema goes in the prompt and
# Pydantic enforces it on the way back. The link picker's schema is small
# enough that it still uses structured outputs.
SECTION_MODELS: dict[str, type[BaseModel]] = {
    "organization": OrganizationExtract,
    "fit": FitExtract,
    "financials": FinancialsExtract,
    "contacts": Contacts,
    "buying_signals": BuyingSignalsExtract,
    "timing": Timing,
    "network": Network,
}


def extraction_schema_text() -> str:
    """The schema sent to the model, generated from the Pydantic models.

    Generated rather than hand-written so the prompt cannot drift out of step
    with what validation will actually accept.
    """
    return json.dumps(ExtractionResult.model_json_schema(), separators=(",", ":"))


def parse_json_reply(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model reply, tolerating stray wrapping."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _normalise_field_sources(raw: Any) -> list[FieldSource]:
    """Accept either [{group, urls}] or {group: [urls]}."""
    sources: list[FieldSource] = []
    if isinstance(raw, dict):
        raw = [{"group": key, "urls": value} for key, value in raw.items()]
    for item in raw or []:
        try:
            source = FieldSource.model_validate(item)
        except ValidationError:
            continue
        # Strip a trailing "(html)"/"(pdf)" if the model copied the header type.
        source.urls = [
            re.sub(r"\s*\((?:html|pdf|rss|sitemap)\)\s*$", "", url)
            for url in source.urls
        ]
        sources.append(source)
    return sources


def salvage_extraction(
    data: dict[str, Any]
) -> tuple[ExtractionResult | None, list[str]]:
    """Validate section by section, keeping whatever is well formed.

    One bad enum value in an events list shouldn't cost us the mission
    statement, so a section that fails validation is dropped on its own and
    noted in the warnings rather than failing the whole profile.
    """
    sections: dict[str, Any] = {}
    warnings: list[str] = []
    for key, model in SECTION_MODELS.items():
        raw = data.get(key)
        if raw is None:
            continue
        try:
            sections[key] = model.model_validate(raw)
        except ValidationError as exc:
            warnings.append(
                f"dropped invalid section '{key}' ({exc.error_count()} errors)"
            )
    if "organization" not in sections:
        return None, warnings
    return (
        ExtractionResult(
            field_sources=_normalise_field_sources(data.get("field_sources")),
            **sections,
        ),
        warnings,
    )


def build_extraction_prompt(
    documents: list[dict[str, Any]], feed_items: list[dict[str, Any]]
) -> str:
    """Label every document with its source URL so citations are possible."""
    parts = [
        f"=== SOURCE ({document['type']}): {document['url']} ===\n"
        f"{document['text']}"
        for document in documents
        if document["text"]
    ]
    if feed_items:
        lines = "\n".join(
            f"- {item.get('date') or 'no date'} | {item.get('title') or ''} "
            f"| {item.get('link') or ''}"
            for item in feed_items
        )
        parts.append(f"=== SOURCE (rss): site news feed ===\n{lines}")
    return "\n\n".join(parts)


def extract(
    documents: list[dict[str, Any]], feed_items: list[dict[str, Any]]
) -> tuple[ExtractionResult | None, list[str]]:
    """LLM call #2: read the documents and fill in the schema.

    On a validation failure the errors are sent back for one retry, which is
    usually enough - the model corrects an enum or a stray string in a number
    field. Anything still invalid is salvaged section by section.
    """
    prompt = build_extraction_prompt(documents, feed_items)
    if not prompt.strip():
        return None, ["no readable text was collected"]

    system = EXTRACT_SYSTEM.format(schema=extraction_schema_text())
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    warnings: list[str] = []

    for attempt in range(2):
        try:
            response = client().messages.create(
                model=EXTRACT_MODEL,
                max_tokens=8000,
                system=system,
                messages=messages,
            )
            record_usage(
                EXTRACT_MODEL, response.usage.input_tokens,
                response.usage.output_tokens,
            )
            reply = "".join(
                block.text for block in response.content if block.type == "text"
            )
            if response.stop_reason == "max_tokens":
                warnings.append("extraction reply hit the token cap")

            data = parse_json_reply(reply)
            try:
                return ExtractionResult.model_validate(data), warnings
            except ValidationError as exc:
                if attempt == 0:
                    log.info("extraction did not validate, retrying with errors")
                    messages += [
                        {"role": "assistant", "content": reply},
                        {
                            "role": "user",
                            "content": (
                                "That did not match the schema. Fix these "
                                f"errors and resend the whole JSON object:\n"
                                f"{exc.errors(include_url=False)}"
                            ),
                        },
                    ]
                    continue
                result, salvage_warnings = salvage_extraction(data)
                return result, warnings + salvage_warnings
        except Exception as exc:
            log.warning("extraction failed (attempt %d): %s", attempt + 1, exc)
            warnings.append(f"extraction error: {type(exc).__name__}")
    return None, warnings


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


def digits_only(ein: str | None) -> str | None:
    """36-3673599 -> 363673599, which is the form the API expects."""
    if not ein:
        return None
    digits = re.sub(r"\D", "", ein)
    return digits if len(digits) == 9 else None


def search_variants(name: str) -> list[str]:
    """Query forms to try against ProPublica, simplest-likely-to-work first."""
    cleaned = " ".join(re.sub(r"[^\w\s&-]", " ", name).split())
    words = cleaned.split()
    while words and words[-1].lower().strip(".") in LEGAL_SUFFIXES:
        words.pop()
    variants = [" ".join(words), cleaned, name]
    if len(words) > 4:
        variants.insert(1, " ".join(words[:4]))
    seen: set[str] = set()
    ordered = []
    for variant in variants:
        key = variant.lower().strip()
        if key and key not in seen:
            seen.add(key)
            ordered.append(variant.strip())
    return ordered


def propublica_search(name: str) -> list[dict[str, Any]]:
    """Search the Nonprofit Explorer by organisation name."""
    last_error = None
    for variant in search_variants(name):
        result = fetch(f"{PROPUBLICA_BASE}search.json?q={quote_plus(variant)}")
        if not result["ok"]:
            last_error = result["error"]
            log.debug("ProPublica search %r: %s", variant, result["error"])
            continue
        try:
            organizations = json.loads(result["content"]).get("organizations") or []
        except (json.JSONDecodeError, AttributeError) as exc:
            log.warning("ProPublica search returned unusable JSON: %s", exc)
            continue
        if organizations:
            if variant != name:
                log.debug("ProPublica matched on simplified query %r", variant)
            return organizations
    if last_error:
        log.warning("ProPublica search failed: %s", last_error)
    return []


def propublica_organization(ein: str) -> dict[str, Any] | None:
    """Fetch one organisation, including its filings."""
    result = fetch(f"{PROPUBLICA_BASE}organizations/{ein}.json")
    if not result["ok"]:
        log.info("ProPublica has no record for EIN %s (%s)", ein, result["error"])
        return None
    try:
        return json.loads(result["content"])
    except json.JSONDecodeError as exc:
        log.warning("ProPublica organization JSON unusable: %s", exc)
        return None


def match_propublica(
    name: str, candidates: list[dict[str, Any]], hq_city: str | None = None
) -> tuple[dict[str, Any] | None, float]:
    """Best name match above the threshold, with a nudge for a matching city.

    Deliberately strict: "Feeding America" has sixteen search hits, most of
    them independent member food banks. A wrong EIN is worse than no EIN,
    because it silently attaches someone else's finances.
    """
    best: dict[str, Any] | None = None
    best_score = 0.0
    city = (hq_city or "").split(",")[0].strip().lower()
    for candidate in candidates:
        score = fuzz.token_set_ratio(name.lower(), (candidate.get("name") or "").lower())
        if city and city == (candidate.get("city") or "").strip().lower():
            score = min(100.0, score + 5)
        if score > best_score:
            best, best_score = candidate, score
    if best is None or best_score < PROPUBLICA_MATCH_THRESHOLD:
        return None, best_score
    return best, best_score


def _filing_years(payload: dict[str, Any]) -> list[FinancialYear]:
    """Filings that actually carry figures, newest first.

    ProPublica also returns `filings_without_data` - filings it holds only as
    a PDF. Those are skipped here; the year they cover is often available
    from the organisation's own copy of the 990, which is one reason the site
    crawl still earns its place.
    """
    years: list[FinancialYear] = []
    for filing in payload.get("filings_with_data") or []:
        fiscal_year = filing.get("tax_prd_yr")
        if not fiscal_year:
            continue
        years.append(
            FinancialYear(
                fiscal_year=int(fiscal_year),
                revenue=filing.get("totrevenue"),
                expenses=filing.get("totfuncexpns"),
                total_assets=filing.get("totassetsend"),
            )
        )
    years.sort(key=lambda year: year.fiscal_year, reverse=True)
    return years[:MAX_FILING_YEARS]


def enrich_financials(profile: NonprofitProfile) -> None:
    """Attach multi-year IRS financials and the NTEE code, in place."""
    name = profile.organization.name
    ein = digits_only(profile.organization.ein)
    payload: dict[str, Any] | None = None
    match_score = 100.0

    if ein:
        # An EIN printed on the site is the most reliable key there is.
        payload = propublica_organization(ein)
        if payload is None:
            log.info("EIN %s from the site did not resolve; trying by name", ein)

    if payload is None:
        candidates = propublica_search(name)
        match, match_score = match_propublica(
            name, candidates, profile.organization.hq_city
        )
        if match is None:
            profile.meta.warnings.append(
                "no confident ProPublica match; financials are from the site only"
                + (f" (best name score {match_score:.0f})" if candidates else "")
            )
            return
        ein = str(match.get("ein") or "").zfill(9)
        payload = propublica_organization(ein)
        if payload is None:
            profile.meta.warnings.append("ProPublica matched but returned no record")
            return

    organization = payload.get("organization") or {}
    irs_years = _filing_years(payload)

    # Where both sources cover a year, compare them. A site figure that
    # disagrees materially with the filed return is worth surfacing: it is
    # usually a different fiscal period or a consolidated group total, and a
    # seller quoting the wrong one looks careless.
    site_by_year = {year.fiscal_year: year for year in profile.financials.years}
    for irs_year in irs_years:
        site_year = site_by_year.get(irs_year.fiscal_year)
        if not (site_year and site_year.revenue and irs_year.revenue):
            continue
        drift = abs(site_year.revenue - irs_year.revenue) / irs_year.revenue
        log.info(
            "FY%d revenue: site %.0f vs IRS %.0f (%.1f%% apart)",
            irs_year.fiscal_year, site_year.revenue, irs_year.revenue, drift * 100,
        )
        if drift > 0.05:
            profile.meta.warnings.append(
                f"FY{irs_year.fiscal_year} revenue on the site differs from the "
                f"IRS filing by {drift:.0%}; the IRS figure is used"
            )

    # Merge rather than replace. IRS figures win for any year both sources
    # cover, but ProPublica lags - Feeding America's FY2024 filing is present
    # there only as a PDF, while the organisation publishes the numbers
    # itself - so a newer year found on the site is kept.
    covered = {year.fiscal_year for year in irs_years}
    kept = [year for year in profile.financials.years if year.fiscal_year not in covered]
    merged = sorted(
        irs_years + kept, key=lambda year: year.fiscal_year, reverse=True
    )[:MAX_FILING_YEARS]

    if kept:
        profile.meta.warnings.append(
            "kept "
            + ", ".join(
                str(year) for year in sorted(
                    (year.fiscal_year for year in kept), reverse=True
                )
            )
            + " from the site: ProPublica has no structured data for those years"
        )

    profile.financials.years = merged
    profile.financials.source = "propublica" if irs_years else "annual_report"
    profile.financials.propublica_match = ProPublicaMatch(
        name=organization.get("name") or name,
        ein=str(organization.get("ein") or ein),
        match_score=round(match_score, 1),
    )

    ntee = organization.get("ntee_code")
    major_group = major_group_for_ntee(ntee)
    if major_group:
        profile.fit.cause_area = CauseArea(
            ntee_code=ntee, major_group=major_group, source="irs_ntee"
        )


# ---------------------------------------------------------------------------
# 9. POST-PROCESSING
# ---------------------------------------------------------------------------

def revenue_growth_pct(years: list[FinancialYear]) -> float | None:
    """Compound annual growth between the oldest and newest year with revenue.

    A compound rate rather than a raw difference, so a three-year and a
    one-year gap are comparable across organisations.
    """
    with_revenue = sorted(
        (year for year in years if year.revenue and year.revenue > 0),
        key=lambda year: year.fiscal_year,
    )
    if len(with_revenue) < 2:
        return None
    oldest, newest = with_revenue[0], with_revenue[-1]
    span = newest.fiscal_year - oldest.fiscal_year
    if span <= 0:
        return None
    growth = (newest.revenue / oldest.revenue) ** (1 / span) - 1
    return round(growth * 100, 1)


def size_bucket_for(revenue: float | None) -> str:
    """Map the latest revenue onto a fixed size bucket."""
    if revenue is None:
        return "unknown"
    for threshold, label in SIZE_BUCKETS:
        if revenue < threshold:
            return label
    return SIZE_BUCKET_TOP


def major_group_for_ntee(ntee_code: str | None) -> str | None:
    """First letter of the NTEE code -> major group name."""
    if not ntee_code:
        return None
    return NTEE_MAJOR_GROUPS.get(ntee_code.strip()[:1].upper())


def latest_year(years: list[FinancialYear]) -> FinancialYear | None:
    """Most recent year that carries a revenue figure."""
    with_revenue = [year for year in years if year.revenue is not None]
    if not with_revenue:
        return None
    return max(with_revenue, key=lambda year: year.fiscal_year)


def post_process(profile: NonprofitProfile) -> None:
    """Fill every computed field, in place.

    Everything here is arithmetic or a lookup, so it is done in code. The
    model is never asked for a value that can be derived - that is what keeps
    categories consistent across organisations.
    """
    profile.buying_signals.executive_search_open = any(
        role.seniority == "executive" for role in profile.buying_signals.open_roles
    )

    years = profile.financials.years
    profile.financials.revenue_growth_pct = revenue_growth_pct(years)
    newest = latest_year(years)
    profile.financials.size_bucket = size_bucket_for(
        newest.revenue if newest else None
    )

    # Reject anything outside the fixed taxonomy, whatever its source.
    if profile.fit.cause_area.major_group not in CAUSE_AREAS:
        profile.fit.cause_area.major_group = "Unknown"


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
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{slugify(profile.organization.name)}.json"
    path.write_text(profile.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def profile_to_row(profile: NonprofitProfile) -> dict[str, Any]:
    """Flatten one profile into a CSV row.

    Lists collapse to counts or short joins: the row is for filtering and
    sorting in a CRM or a spreadsheet, and the JSON keeps everything.
    """
    newest = latest_year(profile.financials.years)
    leader = profile.contacts.leaders[0] if profile.contacts.leaders else None
    news = profile.timing.recent_news[0] if profile.timing.recent_news else None
    campaign = profile.buying_signals.campaign
    return {
        "name": profile.organization.name,
        "website": profile.organization.website,
        "country": profile.organization.country,
        "hq_city": profile.organization.hq_city,
        "ein": profile.organization.ein,
        "cause_area": profile.fit.cause_area.major_group,
        "geographic_scope": profile.fit.geographic_scope,
        "size_bucket": profile.financials.size_bucket,
        "latest_revenue": newest.revenue if newest else None,
        "latest_fiscal_year": newest.fiscal_year if newest else None,
        "revenue_growth_pct": profile.financials.revenue_growth_pct,
        "mission": profile.fit.mission,
        "num_programs": len(profile.fit.programs),
        "top_leader_name": leader.name if leader else None,
        "top_leader_title": leader.title if leader else None,
        "email": profile.contacts.email,
        "phone": profile.contacts.phone,
        "num_open_roles": len(profile.buying_signals.open_roles),
        "executive_search_open": profile.buying_signals.executive_search_open,
        "num_rfps": len(profile.buying_signals.rfps),
        "has_leadership_change": bool(profile.buying_signals.leadership_changes),
        "has_active_campaign": bool(campaign and campaign.status == "active"),
        "campaign_name": campaign.name if campaign else None,
        "num_events": len(profile.timing.events),
        "latest_news_title": news.title if news else None,
        "latest_news_date": news.date if news else None,
        "funders": "; ".join(funder.name for funder in profile.network.funders),
        "memberships": "; ".join(profile.network.memberships),
        "auditor_firm": profile.financials.auditor_firm,
        "crawled_at": profile.meta.crawled_at,
        "cost_usd": profile.meta.cost_usd,
        "num_warnings": len(profile.meta.warnings),
    }


def load_profiles() -> list[NonprofitProfile]:
    """Every profile currently in output/, skipping anything unreadable."""
    profiles: list[NonprofitProfile] = []
    for path in sorted(OUTPUT_DIR.glob("*.json")):
        try:
            profiles.append(
                NonprofitProfile.model_validate_json(path.read_text(encoding="utf-8"))
            )
        except (ValidationError, json.JSONDecodeError, OSError) as exc:
            log.warning("skipping %s: %s", path.name, exc)
    return profiles


def rebuild_combined_csv() -> Path | None:
    """Rebuild output/combined.csv from every JSON file in output/.

    Rebuilt from the JSON rather than appended to, so re-running one
    organisation updates its row instead of duplicating it.
    """
    profiles = load_profiles()
    if not profiles:
        return None
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / "combined.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for profile in sorted(profiles, key=lambda p: p.organization.name.lower()):
            writer.writerow(profile_to_row(profile))
    return path


# ---------------------------------------------------------------------------
# 11. MAIN
# ---------------------------------------------------------------------------

def usage_snapshot() -> dict[str, dict[str, int]]:
    """Copy of the running token totals, for per-org accounting."""
    return {model: dict(counts) for model, counts in USAGE.items()}


def usage_since(before: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    """Tokens spent since `before` — one org's share of a batch run."""
    delta: dict[str, dict[str, int]] = {}
    for model, counts in USAGE.items():
        start = before.get(model, {"input": 0, "output": 0})
        spent = {
            "input": counts["input"] - start["input"],
            "output": counts["output"] - start["output"],
        }
        if spent["input"] or spent["output"]:
            delta[model] = spent
    return delta


def process_org(value: str, use_browser: bool = True) -> NonprofitProfile:
    """Run the whole pipeline for one organisation name or URL."""
    started_usage = usage_snapshot()
    warnings: list[str] = []

    resolved = resolve_input(value, use_browser=use_browser)
    home_url = resolved["url"]

    home = resolved["home"] or fetch_homepage(home_url, use_browser=use_browser)
    if not home["ok"]:
        raise RuntimeError(f"could not fetch {home_url}: {home['error']}")
    home_url = home["url"]

    found = discover_candidates(home_url, home["html"])
    candidates = found["candidates"]
    if not candidates:
        warnings.append("no candidate links found on the homepage")

    reserved = reserve_report_links(candidates)
    budget = max(1, MAX_PAGES - len(reserved))
    picked = pick_links(candidates, home_url, limit=budget)
    if not reserved:
        warnings.append("no annual report, 990 or financials page found")

    collected = collect_documents(home, candidates, picked, use_browser=use_browser)
    documents = collected["documents"]

    extracted, extract_warnings = extract(documents, found["feed_items"])
    warnings += extract_warnings
    if extracted is None:
        warnings.append("extraction failed; only crawl metadata was recorded")
        extracted = ExtractionResult(
            organization=OrganizationExtract(
                name=registrable_domain(urlsplit(home_url).netloc)
            )
        )

    profile = NonprofitProfile(
        organization=Organization(
            name=extracted.organization.name,
            website=home_url,
            country=extracted.organization.country,
            hq_city=extracted.organization.hq_city,
            ein=extracted.organization.ein,
            year_founded=extracted.organization.year_founded,
        ),
        fit=Fit(
            mission=extracted.fit.mission,
            programs=extracted.fit.programs,
            cause_area=CauseArea(
                major_group=extracted.fit.cause_area, source="llm"
            ),
            geographic_scope=extracted.fit.geographic_scope,
        ),
        financials=Financials(
            years=extracted.financials.years,
            employee_count=extracted.financials.employee_count,
            auditor_firm=extracted.financials.auditor_firm,
            source="annual_report" if extracted.financials.years else "none",
        ),
        contacts=extracted.contacts,
        buying_signals=BuyingSignals(
            open_roles=extracted.buying_signals.open_roles,
            rfps=extracted.buying_signals.rfps,
            leadership_changes=extracted.buying_signals.leadership_changes,
            capital_projects=extracted.buying_signals.capital_projects,
            campaign=extracted.buying_signals.campaign,
        ),
        timing=extracted.timing,
        network=extracted.network,
        meta=Meta(
            input=value,
            resolved_url=home_url,
            resolution_method=resolved["resolution_method"],
            crawled_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            pages_crawled=[
                CrawledPage(
                    url=document["url"], type=document["type"],
                    method=document["method"], chars=document["chars"],
                )
                for document in documents
            ],
            failed_pages=[
                FailedPage(url=failure["url"], error=failure["error"])
                for failure in collected["failed_pages"]
            ],
            field_sources={
                source.group: source.urls for source in extracted.field_sources
            },
            warnings=warnings,
        ),
    )

    if found["feed_url"]:
        profile.meta.pages_crawled.append(
            CrawledPage(url=found["feed_url"], type="rss", method="http",
                        chars=len(found["feed_items"]))
        )

    try:
        enrich_financials(profile)
    except Exception as exc:  # enrichment must never lose the crawl
        log.warning("ProPublica enrichment failed: %s", exc)
        profile.meta.warnings.append(f"ProPublica enrichment failed: {exc}")

    post_process(profile)

    profile.meta.tokens = usage_since(started_usage)
    profile.meta.cost_usd = usage_cost_usd(profile.meta.tokens)
    return profile


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
        "--stage", choices=("links", "pick", "crawl"),
        help="stop early and print that stage: 'links' lists candidates, "
             "'pick' adds link selection, 'crawl' adds fetching and parsing",
    )
    parser.add_argument(
        "--no-llm", action="store_true",
        help="use the keyword picker instead of the model (no API cost)",
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
    # The SDK and its HTTP layer log every request at INFO, which drowns ours.
    for noisy in ("httpx", "httpx2", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.stage:
        if not args.org:
            log.error("--stage takes a single organisation, not --batch")
            return 2
        return show_stage(
            args.org, stage=args.stage, use_browser=not args.no_browser,
            use_llm=not args.no_llm,
        )

    targets = read_targets(args)
    if targets is None:
        return 2

    done: list[NonprofitProfile] = []
    failed: list[tuple[str, str]] = []
    for position, target in enumerate(targets, start=1):
        if len(targets) > 1:
            log.info("[%d/%d] %s", position, len(targets), target)
        try:
            profile = process_org(target, use_browser=not args.no_browser)
        except Exception as exc:
            # One bad organisation must not end a batch.
            log.error("%s failed: %s", target, exc)
            failed.append((target, str(exc)))
            continue
        write_json(profile)
        done.append(profile)
        if len(targets) == 1:
            print_profile(profile)

    csv_path = rebuild_combined_csv()
    print_summary(done, failed, csv_path)
    return 0 if done else 1


def read_targets(args: argparse.Namespace) -> list[str] | None:
    """The organisations to process, from --batch or the positional argument."""
    if not args.batch:
        return [args.org]
    try:
        lines = Path(args.batch).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.error("could not read %s: %s", args.batch, exc)
        return None
    targets = [
        line.strip() for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not targets:
        log.error("%s contained no organisations", args.batch)
        return None
    return targets


def print_profile(profile: NonprofitProfile) -> None:
    """One organisation's headline numbers."""
    print(f"\n{profile.organization.name}  ({profile.organization.website})")
    print(f"  mission     {(profile.fit.mission or '-')[:88]}")
    print(f"  cause       {profile.fit.cause_area.major_group} "
          f"({profile.fit.cause_area.ntee_code or 'no NTEE'}, "
          f"{profile.fit.cause_area.source})")
    print(f"  size        {profile.financials.size_bucket}"
          f"   growth {profile.financials.revenue_growth_pct}%"
          f"   ({len(profile.financials.years)} yrs, "
          f"{profile.financials.source})")
    print(f"  programs {len(profile.fit.programs)}   "
          f"leaders {len(profile.contacts.leaders)}   "
          f"roles {len(profile.buying_signals.open_roles)}   "
          f"news {len(profile.timing.recent_news)}   "
          f"events {len(profile.timing.events)}")
    for warning in profile.meta.warnings:
        print(f"  warning     {warning}")


# Fields worth reporting coverage on: the ones a seller acts on.
COVERAGE_FIELDS: dict[str, Any] = {
    "mission": lambda p: bool(p.fit.mission),
    "ein": lambda p: bool(p.organization.ein),
    "financials": lambda p: bool(p.financials.years),
    "cause area": lambda p: p.fit.cause_area.major_group != "Unknown",
    "leaders": lambda p: bool(p.contacts.leaders),
    "phone or email": lambda p: bool(p.contacts.phone or p.contacts.email),
    "open roles": lambda p: bool(p.buying_signals.open_roles),
    "news": lambda p: bool(p.timing.recent_news),
    "events": lambda p: bool(p.timing.events),
}


def print_summary(
    done: list[NonprofitProfile],
    failed: list[tuple[str, str]],
    csv_path: Path | None,
) -> None:
    """Run totals: coverage, failures and what it cost."""
    print(f"\n{'-' * 62}")
    print(f"processed {len(done)}, failed {len(failed)}")
    for target, error in failed:
        print(f"  FAILED  {target}: {error[:90]}")

    if done:
        print("\nfield coverage")
        for label, present in COVERAGE_FIELDS.items():
            count = sum(1 for profile in done if present(profile))
            bar = "#" * count + "." * (len(done) - count)
            print(f"  {label:<16} {count}/{len(done)}  {bar}")

    if csv_path:
        print(f"\ncombined csv  {csv_path}")
    total = usage_cost_usd(USAGE)
    for model, counts in USAGE.items():
        print(f"tokens        {model}: {counts['input']:,} in, "
              f"{counts['output']:,} out")
    print(f"total cost    ${total:.4f}", end="")
    if done:
        print(f"   (${total / len(done):.4f} per organisation)")
    else:
        print()
    print()


def show_stage(
    value: str, stage: str = "links", use_browser: bool = True,
    use_llm: bool = True,
) -> int:
    """Debug view of one pipeline stage, stopping before extraction."""
    if not looks_like_url(value):
        log.error("--stage needs a URL for now; name resolution is next.")
        return 2
    home_url = normalise_url(value)
    log.info("fetching %s", home_url)
    home = fetch_homepage(home_url, use_browser=use_browser)
    if not home["ok"]:
        log.error("could not fetch homepage: %s", home["error"])
        return 1

    home_url, home_html = home["url"], home["html"]
    found = discover_candidates(home_url, home_html)

    print(f"\nhomepage      {home_url}  ({len(home_html):,} chars html, "
          f"via {home['method']})")
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

    if stage == "links":
        print()
        return 0

    reserved = reserve_report_links(candidates)
    print(f"\nreserved financial slots  {len(reserved)}")
    for item in reserved:
        print(f"  {item['url']}")

    budget = max(1, MAX_PAGES - len(reserved))
    if use_llm:
        picked = pick_links(candidates, home_url, limit=budget)
        label = f"model picks ({LINK_MODEL})"
    else:
        picked = pick_links_by_keyword(candidates)[:budget]
        label = "keyword picks"
    print(f"\n{label}  {len(picked)}")
    for item in picked:
        print(f"  {','.join(item['covers'])[:34]:<34}  {item['url'][:78]}")
        print(f"  {'':34}  -> {item['reason'][:78]}")

    if USAGE:
        print(f"\ntokens                    {USAGE}")
        print(f"cost so far               ${usage_cost_usd(USAGE):.5f}")

    if stage == "pick":
        print()
        return 0

    collected = collect_documents(
        home, candidates, picked, use_browser=use_browser
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
