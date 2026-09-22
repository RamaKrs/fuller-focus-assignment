# Nonprofit Data Scraper Agent

Turns a nonprofit's scattered online footprint — web pages, annual-report PDFs,
990 filings, news feeds — into one structured, validated profile per
organisation.

```bash
python main.py "Feeding America"
python main.py https://www.charitywater.org
python main.py --batch orgs.txt
```

Input is an organisation **name or URL**. Output is `output/<slug>.json` (the
full profile) plus `output/combined.csv` (one row per organisation, built for a
CRM or a spreadsheet).

---

## 1. Problem statement

Useful information about a nonprofit is spread across its website, its annual
report PDF, its IRS filings, its news posts and its jobs page. Anyone who sells
to nonprofits — a consultancy, an events supplier, a software vendor, an
executive search firm — needs that information to decide whether an
organisation is worth approaching, and when. Collecting it by hand takes a
researcher roughly half an hour per organisation, and it is stale within
months.

The task is not just extraction. It is deciding *what is worth extracting*,
getting it consistently across organisations that structure their sites
completely differently, and doing it cheaply enough that the answer can be
refreshed rather than collected once.

## 2. Value

The schema is built around four questions a seller actually asks. Every field
earns its place by answering one of them:

| Question | Fields |
|---|---|
| **Is this a good fit?** | mission, programs, cause area, geographic scope |
| **Can they afford it?** | multi-year revenue/expenses/assets, growth rate, size bucket, employee count, auditor firm |
| **Who do I contact?** | named leaders with titles, phone |
| **Why reach out now?** | leadership changes, active campaign, recent news |

The schema was **cut down after measuring it**, not designed once and left. The
first version had forty fields; six of them came back empty on every
organisation in the test set and were costing tokens and a page slot each. §8
has the numbers and §7 the reasoning. What survives is what the test set showed
actually arrives.

**Positioning.** Fuller Focus's own database is built largely from *external*
sources — IRS filings, executive-search firms, building permits, foundation
grant feeds. This agent deliberately covers the complementary half: what only
appears on the organisation's **own website** (campaigns, events, programs,
open roles, leadership news, memberships), and then merges IRS financials from
ProPublica so the two halves sit in one record.

## 3. Why this approach

The assignment allows either an LLM tool-calling loop that navigates the site,
or a crawler with LLM extraction. This is the second, with one LLM routing
step in the middle:

```
input → resolve URL → crawl homepage → discover links (anchors + sitemap + RSS)
      → [LLM 1: Haiku] pick which pages to read
      → fetch + parse (HTML, PDF, browser fallback)
      → [LLM 2: Sonnet] extract the schema
      → ProPublica enrichment → computed fields → JSON + CSV
```

**Exactly two model calls per organisation** (three if the input is a name and
needs a URL guess). That was the deciding factor:

- **Predictable cost.** A free-roaming agent's bill depends on how confusing it
  finds a site. At 500,000 organisations, a long tail of agents wandering
  through course catalogues is the difference between a viable product and an
  unbounded invoice. Here the cost per organisation is bounded by construction
  — a hard character budget caps the extraction input no matter how much the
  crawler finds.
- **Debuggable.** Each stage can be run and inspected on its own
  (`--stage links|pick|crawl`), and a bad profile can be traced to the exact
  page that produced it via `meta.field_sources`.
- **Most of the flexibility anyway.** The genuinely hard judgement — *which of
  these 198 links describe the organisation rather than its product?* — is
  still made by a model. Everything around it is deterministic.

**Anything code can compute, code computes.** Growth rates, size buckets, the
NTEE-to-cause-area mapping, the executive-search flag, the CSV row, the cost
accounting. The model is only asked to do reading comprehension. This is also
what keeps categories consistent at scale (§9).

## 4. MVP — install and run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # for JavaScript-rendered sites

cp .env.example .env                 # add your ANTHROPIC_API_KEY
python main.py "Feeding America"
```

```
usage: main.py [-h] [--batch FILE] [--no-browser] [--stage {links,pick,crawl}]
               [--no-llm] [--verbose] [org]
```

| Flag | Effect |
|---|---|
| `--batch FILE` | one name or URL per line; a failure doesn't stop the run |
| `--stage links\|pick\|crawl` | stop early and print that stage, for debugging |
| `--no-llm` | keyword picker instead of the model — crawl with zero API cost |
| `--no-browser` | disable the Playwright fallback |

Worked examples for five organisations are committed in [`examples/`](examples/),
including the combined CSV.

**A note on the code layout.** Everything is in a single `main.py`, organised
into eleven numbered sections with small functions. That is a deliberate V1
choice for a project of this size — it keeps the whole pipeline readable
top-to-bottom — and the sections map one-to-one onto modules if it grows.

## 5. Methodology

**Resolve the input.** A URL is normalised (scheme added, tracking parameters
and fragments stripped). A *name* is sent to Haiku for a URL guess, which is
then **verified** before anything else happens: the organisation name is fuzzy
matched against the page title, the leading page text, and every domain in the
redirect chain. Below threshold, the organisation is rejected with a message
asking for the URL. An unverified guess would turn a confident wrong answer
into a confident wrong profile.

**Discover links.** Homepage anchors (subdomains count as same-site; PDFs are
kept from any domain, because reports are often on a CDN), plus `sitemap.xml`
— following one level of sitemap index, and preferring URLs that match schema
keywords so a large site's URL budget isn't spent on arbitrary pages — plus an
RSS/Atom feed if one exists. Feeds are the cheapest good data on a site: real
publication dates, no model needed.

**Pick pages.** Haiku is given the candidate list and returns **indices, not
URLs** — it cannot invent a page that way, and the reply costs a fraction of
the output tokens. Two failures fall back to a deterministic keyword picker.

**Financial documents are not left to the model.** One of the four page slots
is reserved for annual reports, 990s and financial statements, selected by
keyword scoring in code. Report PDFs linked from a fetched page are followed
one level down — which is the only way to reach them in practice:
`feedingamerica.org`'s homepage has **zero** PDF links, while its financials
page has **thirteen**.

**Parse.** `trafilatura` for HTML with a BeautifulSoup fallback; the homepage
footer is extracted separately, because that is where the EIN, phone and
address live and trafilatura strips footers by design. PyMuPDF for PDFs, with
pages selected by scanning locally for financial markers rather than taking a
fixed prefix — Feeding America's 990 is **91 pages with the statements at page
86**, so a first-ten-pages cap would read the cover and capture no numbers.

RSS/Atom feeds are probed on the news or blog host as well as the main one,
because organisations routinely run the blog on a separate subdomain and
declare the feed only there. `khanacademy.org` advertises no feed;
`blog.khanacademy.org` has one with ten dated posts.

**Fallbacks.** A page that yields too little text, or whose homepage has almost
no anchors, is re-fetched with headless Chromium. So is any resource answering
401/403/406/429/503, which usually means bot protection rather than a missing
page — HTML is re-rendered, while feeds and PDFs go through the browser's HTTP
stack instead, since *rendering* a feed returns Chromium's XML viewer markup
rather than the feed. Requests retry twice with backoff on network errors, 429
and 5xx; a 404 fails immediately. Every failure is recorded in
`meta.failed_pages`.

**Extract.** All documents are labelled with their source URL and sent to
Sonnet with the schema, with instructions to use only the supplied text and to
return null rather than guess. The reply is validated with Pydantic; validation
errors are sent back for one retry, and anything still invalid is salvaged
section by section so a bad enum in an events list can't cost the mission
statement.

**Enrich and compute.** ProPublica supplies multi-year IRS figures and the NTEE
code. Then code fills in everything derivable (§9).

## 6. Tools and tech

| Tool | Why |
|---|---|
| **Claude Haiku 4.5** | Link picking and URL guessing. Routing decisions don't need a frontier model, and this is ~5% of the per-organisation bill. |
| **Claude Sonnet 5** | Extraction. This is the step where accuracy matters — reading a 990 and a leadership page and returning consistent structured fields. |
| `httpx` | HTTP with timeouts |
| `beautifulsoup4` + `lxml` | anchors, sitemap and RSS parsing |
| `trafilatura` | main-text extraction that drops navigation and boilerplate |
| `pymupdf` | PDF text, with per-page access for marker-based selection |
| `playwright` | headless Chromium, for JS-rendered and bot-protected pages |
| `pydantic` | the schema *is* the validation — one definition drives the prompt, the validation and the output |
| `rapidfuzz` | URL verification and ProPublica name matching |
| **ProPublica Nonprofit Explorer** | IRS 990 data, free, structured. Parsing numbers out of a PDF is strictly worse than reading the filing the numbers came from. |

**Structured outputs, where they fit.** The link picker uses the API's
structured outputs, so its `covers` values cannot come back outside the fixed
enum. The full profile schema **exceeds the grammar compiler's size limit**
(`The compiled grammar is too large`), so extraction sends the schema in the
prompt and validates in code instead. The schema text is generated from the
Pydantic models, so the prompt cannot drift from what validation will accept.

## 7. Schema and justification

Full schema: [`examples/feedingamerica-org.json`](examples/feedingamerica-org.json).
Grouped by the
question each field answers — see §2 for the table.

Design decisions worth calling out:

- **Multi-year financials, not a single number.** One revenue figure says how
  big an organisation is; three or five say whether it's growing. A nonprofit
  growing 26% a year is a very different prospect from one shrinking at the
  same size.
- **`field_sources` on every group.** Each block of fields carries the URLs it
  came from, so any claim in the profile can be checked against the page that
  produced it. For a sales dataset this is the difference between usable and
  merely plausible. The model returns *source numbers*, not URLs — echoing a
  137-character CDN link back ten times per profile was about a fifth of the
  reply — and code expands them to URLs in the saved file.
- **Computed fields are separated from extracted ones.** The extraction model
  is never shown `size_bucket`, `revenue_growth_pct` or the NTEE code —
  they're absent from its schema, so it cannot guess at values code owns.
- **`meta` records how the profile was built** — pages crawled with method and
  size, pages that failed with the reason, tokens, cost, warnings. A profile
  that's thin because a site blocked us is a different thing from one that's
  thin because the organisation publishes nothing, and the record says which.

**Excluded by design:** full board rosters, donation-page mechanics, impact
statistics, social media follower counts, tech stack — each is either low value
to a seller or cheaper from a dedicated source than a page slot here.

**Excluded after measuring**, which is the more interesting list. These were in
the first version and were cut because the test set showed them arriving empty:

| Field | Filled | Why it was cut |
|---|---|---|
| `open_roles` | 0/5 | jobs live in JS job-board APIs on another domain |
| `rfps` | 0/5 | rarely on a nonprofit's own site; procurement portals have them |
| `email` | 0/5 | obfuscated against scrapers almost everywhere |
| `contact_url` | 0/5 | low value once you have named leaders and a phone number |
| `memberships` | 0/5 | mentioned in prose, never structured |
| `events` | 1/5 | usually a JS calendar widget |
| `funders` | 1/5 | logo walls, i.e. images, not text |
| `capital_projects` | 1/5 | only ever appeared inside annual-report narrative |

Cutting these removed two page slots as well as the fields themselves, since
careers and partners pages no longer needed crawling. That is most of the 57%
saving in §8 — **the cheapest token is the one you never had a reason to
spend.**

## 8. Cost, scale and feasibility

All figures below are **measured**, from the five-organisation run committed in
`examples/` — taken from each profile's own `meta.tokens` and `meta.cost_usd`.

| Organisation | Cost | Haiku in/out | Sonnet in/out | Pages | PDFs |
|---|---:|---:|---:|---:|---:|
| charity: water | $0.0367 | 4,422 / 115 | 12,591 / 656 | 5 | 1 |
| Code for America | $0.0365 | 1,630 / 138 | 13,194 / 780 | 6 | 1 |
| Feeding America | $0.0384 | 5,875 / 132 | 13,082 / 571 | 5 | 1 |
| Khan Academy | $0.0269 | 4,984 / 136 | 7,311 / 661 | 6 | 0 |
| The Trussell Trust | $0.0396 | 6,371 / 123 | 12,214 / 817 | 5 | 1 |
| **Mean** | **$0.0356** | 4,656 / 128 | 11,678 / 697 | 5.4 | 0.8 |

**Where the money goes now:** Sonnet input 66%, Haiku 15%, Sonnet output 20%.
Input dominates, so the remaining levers are about sending fewer and smaller
documents — not about the reply.

**Non-token costs per organisation:** ~5.4 HTTP requests plus robots/sitemap/
feed probes, 0.8 PDFs, and **19% of pages rendered in headless Chromium** — a
second or two of real CPU each, and the most expensive thing here that isn't
tokens.

### How it got here: $0.0837 → $0.0356

The first working version cost **$0.0837** per organisation. Two changes cut it
by **57%** with no measurable loss — leaders, programmes and fiscal years all
came back identical across the test set (20, 20 and 22 respectively).

| Change | Effect |
|---|---|
| **`effort: low` on extraction** | Sonnet 5 thinks by default and thinking is billed as output. It was **76% of output tokens** and made results no better — on charity: water, 5,197 output tokens against 1,266, where the *cheaper* run found one more fiscal year. |
| **Cutting eight empty fields** (§7) | Removed two page slots as well as the fields, taking `MAX_PAGES` from 6 to 4. Sonnet input fell 18,273 → 11,678. |
| **Source numbers instead of URLs** in `field_sources` | ~20% of the reply was echoed CDN links. |
| **Terser field wording** | Programme descriptions capped at 15 words. |

Output tokens per organisation fell from 4,161 to 697 — **83%** — and the
schema sent in the prompt shrank from 7,381 to 4,972 characters.

One lesson worth stating plainly: **the default settings were the problem, not
the architecture.** Nobody asks for thinking tokens on a JSON extraction task,
and nobody notices paying for them either.

### At 500,000 organisations

| Scenario | Cost |
|---|---|
| One full pass, as built | **~$17,800** |
| Full pass via the Batch API (50%) | **~$8,900** |
| Quarterly refresh of everything | ~$71,000/yr |
| Tiered refresh (below) | **~$12,000/yr** |

Compute: 500k × 5.4 ≈ **2.7M HTTP requests** and ~**500k browser renders** per
pass. At that volume the crawl needs per-domain rate limiting and a scheduler
more than it needs a cheaper model.

### What I'd change next

Now that input dominates, the order has changed:

1. **Batch API — ~50% off, no quality cost.** This work is not
   latency-sensitive. Nothing else is as close to free.
2. **Tiered refresh, ~80% off steady state.** The fields have different
   half-lives: mission, programmes and cause area change yearly; financials
   annually on a predictable filing schedule; news weekly. Re-extracting a
   whole profile to learn one headline is the biggest remaining waste. Re-crawl
   on content hash, `Last-Modified` and sitemap `lastmod`.
3. **Prompt caching** — ~1,400 of 11,678 input tokens (12%) are now the fixed
   system prompt and schema, identical on every call.
4. **Trim the documents, not just the schema.** 11,678 input tokens is still
   the bulk of the bill, and a 990 PDF contributes text we mostly discard.
   Extracting only the marked financial pages, rather than head pages plus
   markers, is the obvious next measurement.
5. **Haiku for organisations with no PDF.** Khan Academy has none and already
   costs $0.0269; it may not need Sonnet at all. Route on whether a financial
   document was retrieved.
6. **IRS bulk data instead of per-organisation API calls** — no tokens either
   way, but it removes a round trip and a third-party dependency.

## 9. Categorisation (bonus)

Three categories, each assigned the cheapest reliable way:

| Category | How | Consistency mechanism |
|---|---|---|
| **Cause area** | IRS NTEE code's first letter → major group. Falls back to the model choosing from the same fixed list. | A 10-value fixed taxonomy. The IRS code is authoritative where it exists, so most organisations are classified without a model opinion at all. |
| **Size bucket** | Computed from latest revenue | Pure arithmetic — identical inputs always give identical output |
| **Geographic scope** | Model, constrained to `local` / `national` / `international` | Three values, validated on the way in |

**How this stays consistent across 500,000 organisations:**

1. **Fixed taxonomies, never free text.** Every category is an enum. The model
   picks from a list; it never invents a label.
2. **Validation rejects, it doesn't coerce.** Anything outside the list becomes
   `Unknown` rather than something plausible-looking. `Unknown` is auditable;
   a silently invented category is not.
3. **Prefer the deterministic source.** Where the IRS has classified an
   organisation, that classification wins, and identical inputs give identical
   outputs forever. The model only fills gaps.
4. **The source is recorded.** `cause_area.source` is `irs_ntee` or `llm`, so
   the model-assigned subset can be sampled and reviewed separately — you can
   audit the 20% that needed judgement instead of all 100%.

**A caveat worth stating.** IRS NTEE codes are self-selected at registration
and go stale. charity: water is coded `P80` (Human Services) and the agent
therefore labels it Human Services, while the model — reading the actual site —
said "International, Foreign Affairs", which is the better description of an
organisation drilling wells in Ethiopia. Deterministic isn't the same as
correct. At scale I'd keep the IRS code as the stable key but surface the
model's disagreement as a flag for review, which is exactly the kind of signal
sampling in point 4 would surface.

## 10. Limitations

- **Name resolution depends on the model knowing the organisation.** It handles
  misspellings ("Feading America" resolves correctly) and rejects fake and
  ambiguous names cleanly, but a small local nonprofit it has never seen will
  fail. A search API is the upgrade.
- **ProPublica is US-only**, and it lags: both US test organisations' most
  recent filings exist there only as PDFs with no structured figures. Non-US
  organisations get financials only from what's on their site.
- **Name matching against ProPublica can mis-match**, and did. An early version
  scored names with `token_set_ratio` alone, which returns 100 whenever one
  name's tokens are a subset of the other's; it matched the UK charity Trussell
  to "Robert And Martha Trussell Familyfoundation" at 100 and attached that
  foundation's finances to the profile. Matching now requires two scorers to
  agree, which also rejects Feeding America's independently-run member food
  banks. It is deliberately strict because a wrong EIN is worse than no EIN — it
  fails silently and looks plausible. An EIN printed on the site is used
  directly when available and is far more reliable: charity: water's legal name
  is "Charity Global Inc", which no name match would ever have found.
- **Four pages per organisation is a real constraint**, and a deliberate one —
  it is most of why a profile costs 3.6 cents. When a site has several
  financial documents they take slots from leadership and news pages.
- **`open_roles` was removed from the schema**, not because job postings don't
  matter — they are the strongest buying signal there is — but because they
  came back empty on 5/5 organisations. Jobs live in JavaScript-rendered
  third-party boards (Greenhouse, Lever, Workday) on another domain, so the
  field cost a page slot and returned nothing. Reading those boards' JSON APIs
  directly would restore it at no token cost, and is the first thing I'd
  build next (§11).
- **`campaign` is currently 0/5** and on probation. It survived the cut on the
  argument that a named campaign is a strong buying signal, but the one
  instance the old schema found came from an annual report that the tighter
  page budget no longer fetches. Either it earns its place on a larger sample
  or it follows `open_roles` out.
- **Extraction varies between runs.** The same PDFs produced FY2025 revenue of
  $82,267,852 on one run and $90,800,000 on another for charity: water. This is
  why IRS data is preferred where it exists, and why `field_sources` matters.
- **Scanned PDFs yield nothing.** No OCR; the page is recorded as failed.
- **Freshness is inherited.** If an organisation's site is a year out of date,
  so is its profile. `meta.crawled_at` records when we looked.
- **`robots.txt` is respected**, so some sites will legitimately yield less.
- **Sites rate-limit you, and the effect is cumulative.** After a day of
  development runs, `codeforamerica.org` began returning 403 to requests that
  had succeeded earlier the same day; full page rendering still passes their
  challenge but the raw request path does not. At 500,000 organisations this
  stops being an inconvenience and becomes a design constraint: per-domain rate
  limiting, a real contact address in the user agent, and spreading a refresh
  cycle over time rather than crawling in bursts.

## 11. What I'd improve with more time

- **Prompt caching** on the fixed system prompt and schema — a stable prefix on
  every call, and the single biggest cost lever available (§8).
- **Concurrency.** The V1 is deliberately synchronous. Page fetches within an
  organisation, and organisations within a batch, are embarrassingly parallel.
- **Incremental refresh.** Re-crawl on content hash / `Last-Modified` /
  sitemap `lastmod`, and split the schema by volatility: mission and programs
  change yearly, news and jobs weekly.
- **A real search step** for URL resolution instead of model recall.
- **Jobs boards as a special case** — detecting a Greenhouse/Lever/Workday
  board and reading its JSON API would fix `open_roles` properly.
- **Tech-stack detection**, which is free: response headers, script sources and
  HTML patterns, no tokens at all.
- **Per-domain rate limiting and polite scheduling**, which the blocking above
  showed is not optional at scale.
- **A small labelled eval set.** With ~30 hand-checked organisations, prompt
  and model changes could be measured rather than eyeballed — and the
  run-to-run extraction variance above could be quantified instead of noted.

## 12. What I spent

**About $2.10 of Claude API usage in total**, summed from the per-run cost that
every run prints. Roughly half is committed test-set runs (four full passes as
the schema was measured and cut); the rest is development.

Build time was roughly six hours.

### What the test set actually produced

Field coverage across the five organisations, after the cut:

| Field | Coverage | |
|---|---|---|
| mission | 5/5 | |
| financials (≥2 years) | 5/5 | |
| named leaders | 5/5 | |
| cause area from IRS NTEE | 4/5 | the fifth is UK, so no IRS record |
| EIN | 3/5 | |
| phone | 3/5 | |
| auditor firm | 3/5 | from the PDFs — not available anywhere else |
| recent news | 2/5 | |
| campaign | 0/5 | on probation, see limitations |

No failed pages across 27 fetched, and no failed organisations.

The cut cost nothing measurable: leaders (20), programmes (20) and fiscal years
(22) are identical to the pre-cut run. Recent news fell from 10 to 6 items,
which is the per-organisation cap moving from 5 to 3, not a retrieval loss.

Two results worth flagging as the payoff for forcing financial documents:

- **Feeding America**: 5 fiscal years, from ProPublica's IRS filings merged
  with the 2024 Form 990 published on their own site — the year ProPublica
  holds only as a PDF — plus auditor RSM US LLP and 421 employees, neither of
  which is in the IRS structured data at all.
- **The Trussell Trust**: a UK charity with no IRS record at all, yet two years
  of financials (£62.8M and £54.1M) read out of a **150-page** annual report
  whose accounts begin on page 101.
