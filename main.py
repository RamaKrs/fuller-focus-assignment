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
import logging
import re
from pathlib import Path
from typing import Any, Literal

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
MAX_PDF_PAGES = 10           # pages read from any single PDF
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
    "/privacy", "/cookie", "/terms", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "linkedin.com", "youtube.com", "tiktok.com",
)

# Fallback keywords if the link-picker LLM call fails (spec §6 step 3).
LINK_FALLBACK_KEYWORDS = (
    "about", "mission", "program", "team", "leadership", "staff", "board",
    "annual-report", "annual_report", "report", "financial", "news", "press",
    "event", "career", "job", "rfp", "campaign", "partner",
)

# What we ask the link picker to cover, in plain words.
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

def fetch(url: str) -> dict[str, Any]:
    """GET `url` with timeout, user agent and retries.

    Returns {"ok": True, "url": final_url, "content": bytes, "content_type": str}
    or {"ok": False, "url": url, "error": str}.
    """
    raise NotImplementedError


def robots_allows(base_url: str, url: str) -> bool:
    """True if the site's robots.txt permits fetching `url`."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 4. RESOLVE INPUT
# ---------------------------------------------------------------------------

def looks_like_url(value: str) -> bool:
    """Heuristic: a URL has a dot and no spaces, or starts with http."""
    raise NotImplementedError


def normalise_url(value: str) -> str:
    """Add https:// if missing, strip tracking params and fragments."""
    raise NotImplementedError


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

def extract_links(html: str, base_url: str) -> list[dict[str, str]]:
    """Same-domain anchors (plus off-domain PDFs) as {"text", "url"}."""
    raise NotImplementedError


def discover_sitemap_urls(base_url: str) -> list[str]:
    """Read /sitemap.xml (following a sitemap index) for extra candidates."""
    raise NotImplementedError


def discover_feed(html: str, base_url: str) -> str | None:
    """Find an RSS/Atom feed via <link rel=alternate>, then /feed and /rss."""
    raise NotImplementedError


def clean_candidates(links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Deduplicate and drop junk links (social, login, cart, legal pages)."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 6. PARSING
# ---------------------------------------------------------------------------

def html_to_text(html: str, url: str) -> str:
    """Main-text extraction with trafilatura, falling back to BeautifulSoup."""
    raise NotImplementedError


def pdf_to_text(data: bytes) -> str:
    """First MAX_PDF_PAGES pages of a PDF."""
    raise NotImplementedError


def render_with_browser(url: str) -> str:
    """Playwright fallback for JS-rendered pages; returns HTML."""
    raise NotImplementedError


def parse_feed(xml: bytes) -> list[dict[str, str | None]]:
    """RSS/Atom items as {"title", "date", "link"}."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 7. LLM CALLS
# ---------------------------------------------------------------------------

def pick_links(
    candidates: list[dict[str, str]], org_hint: str
) -> list[dict[str, Any]]:
    """LLM call #1 (cheap): choose up to MAX_PAGES URLs worth reading."""
    raise NotImplementedError


def pick_links_by_keyword(candidates: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Deterministic fallback used when pick_links fails."""
    raise NotImplementedError


def extract(documents: list[dict[str, Any]], feed_items: list[dict[str, Any]]) -> ExtractionResult:
    """LLM call #2: read the documents and fill in the schema."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# 8. PROPUBLICA
# ---------------------------------------------------------------------------

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
    raise NotImplementedError("pipeline not implemented yet (milestone 1: skeleton)")


if __name__ == "__main__":
    raise SystemExit(main())
