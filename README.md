# Nonprofit Data Scraper Agent
### Loom video: https://www.loom.com/share/064db98555e149f08b073c276a311416

Turns a nonprofit's scattered online footprint — web pages, annual-report PDFs,
990 filings, news feeds — into one structured, validated profile.

```bash
python main.py "Feeding America"
python main.py https://www.charitywater.org
python main.py --batch orgs.txt
```

Setup is in [§4](#4-mvp) — it needs a virtualenv, `playwright install
chromium`, and an `ANTHROPIC_API_KEY` in `.env`.

Input is an organisation **name or URL**. Output is `output/<slug>.json` (the
full profile) and `output/combined.csv` (one row per organisation, ready for a
CRM). Five worked examples are committed in [`examples/`](examples/).

**Measured: $0.0356 per organisation**
#### Disclaimer: A lot of the code is in place just to handle messy data and unexpected errors. So much of it is not worth reviewing. The real pipelines is in process_org().

---
## 1. Problem statement

Useful information about a nonprofit is scattered across its website, its
annual report PDF, its IRS filings and its news posts. Anyone selling to
nonprofits needs that information to decide whether an organisation is worth
approaching, and when. Collecting it by hand takes a researcher around half an
hour per organisation.

## 2. Value

The schema answers four questions a seller asks, in order:

| Question | Fields |
|---|---|
| **Is this a good fit?** | mission, programs, cause area, geographic scope |
| **Can they afford it?** | multi-year revenue / expenses / assets, growth rate, size bucket, employee count, auditor firm |
| **Who do I contact?** | named leaders with titles, phone |

### How it plugs into a sales workflow

1. **Key on EIN.** It is the join key — stable, unique, and it dedupes against
   existing CRM records while merging cleanly with IRS data and third-party
   sources. Without it, every row is a guess at identity.
2. **`combined.csv` imports directly** into HubSpot or Salesforce. Flat
   columns, lists collapsed to counts and short joins. That is the CSV's only
   reason to exist; the JSON keeps everything.
3. **Segment and route** on `size_bucket` + `cause_area` + `geographic_scope`.
4. **Prioritise** on `revenue_growth_pct`. A nonprofit growing 26% a year is a
   different prospect from one shrinking at the same size.

### Why the schema is deliberately generic

A donor-CRM vendor, an audit firm and an executive search firm
want overlapping but different fields. We already extract `auditor_firm`, which
is the single best field for an accounting firm doing competitive displacement
and close to worthless for a caterer.

Choosing one of those buyers is a business decision, not an engineering one,
and getting it wrong bakes a bad assumption into 500,000 rows. So V1 collects a
**common core** that any nonprofit-seller needs — identity, fit, capacity,
contacts — and leaves buyer-specific fields to a layer above.

- **A narrow, expensive profile can only be built for a known target list.**
  That is account research, not prospecting. Prospecting needs breadth first
  and depth second.
- **A cheap generic profile can be built for everyone and deepened on demand.**
  Qualify on the core, then spend real money enriching only the few thousand
  organisations that pass the filter.
- **Buyer-specific extraction is a per-vertical module**, not a rewrite. The
  pipeline already separates crawl, page selection, extraction and computation;
  a vertical adds fields and keywords, not a new system.

The honest version: with a 2–6 hour budget, a defensible generic core plus a
clear extension path is worth more than a specialised schema justified by a
persona I invented.

## 3. Why this approach

The assignment allows an LLM tool-calling loop that navigates the site, or a
crawler with LLM extraction. This is the second, with one cheap routing call in
the middle:

```
input → resolve URL → crawl homepage → discover links (anchors + sitemap + RSS)
      → [LLM 1: Haiku]  pick which pages to read
      → fetch + parse (HTML, PDF, browser fallback)
      → [LLM 2: Sonnet] extract the schema
      → ProPublica enrichment → computed fields → JSON + CSV
```

**Exactly two model calls per organisation** (three if the input is a name and
needs a URL guess). The reasons, in order of how much they mattered:

- **Predictable cost.** A free-roaming agent's bill depends on how confusing it
  finds a site. At 500,000 organisations that approach sounds expensive.
- **Refreshability.** This is the one that matters most and is easiest to miss.
  A pipeline with named stages can be re-entered at any stage. An agent that
  decides its own route cannot be asked to "just re-check the news page",
  because it has no stable notion of which page that was. Section 7 turns this
  into the main cost argument.
- **Debuggable.** Each stage runs alone (`--stage links|pick|crawl`), and a bad
  field traces to the page that produced it through `meta.field_sources`.
- **Most of the flexibility anyway.**

**Anything code can compute, code computes**: growth rates, size buckets, the
NTEE-to-cause-area mapping, the CSV row, the cost accounting. The model only
does reading comprehension. **This is also what keeps categories consistent
across organisations.**

## 4. MVP

```bash
python3 -m venv .venv
source .venv/bin/activate            # every new terminal needs this
pip install -r requirements.txt
playwright install chromium          # ~170MB, for JavaScript-rendered sites

cp .env.example .env                 # then put your key in it
python main.py "Feeding America"
```

**Smoke test without an API key.** This crawls a site and prints what it
found, making no model calls, so it verifies the install on its own:

```bash
python main.py https://www.charitywater.org --stage links
```

Requires Python 3.11+. On most Linux systems the interpreter is `python3`, not
`python` — once the virtualenv is activated, `python` works. If you see
`ModuleNotFoundError: No module named 'anthropic'`, the virtualenv isn't
active: run `source .venv/bin/activate` again.

```
usage: main.py [-h] [--batch FILE] [--no-browser] [--stage {links,pick,crawl}]
               [--verbose] [org]
```

| Flag | Effect |
|---|---|
| `--batch FILE` | one name or URL per line; one failure doesn't stop the run |
| `--stage links\|pick\|crawl` | stop early and print that stage |
| `--no-browser` | disable the Playwright fallback |

What it does on a real run, taken from the committed examples:

- Resolves `"Feeding America"` to a **verified** URL, or rejects the input.
- Finds and reads its **2024 Form 990** — a 91-page PDF whose financial
  statements start at page 86.
- Merges that with five years of IRS filings from ProPublica.
- Returns mission, programmes, named leadership, phone, EIN, auditor
  (RSM US LLP), 421 employees, cause area from the IRS NTEE code, a computed
  9% growth rate and a `100M+` size bucket.
- Writes the JSON, rebuilds the CSV, prints what it cost.

**Code layout.** Everything is in one `main.py`, in eleven numbered sections
with small functions. That is a deliberate V1 choice at this size — the whole
pipeline reads top to bottom — and the sections map one-to-one onto modules
when it grows. `process_org` is the entire pipeline in about a hundred lines;
everything else hangs off it.

**On length.** It is ~2,500 lines, which is more than a V1 of this scope
should need, and most of the excess is failure handling. The brief asks for
that explicitly ("fallbacks, assumptions, retries"), but there is a difference
between fallbacks that earn their place and fallbacks written for imagined
problems. **A fallback you have never seen run is
untested code.**

## 5. Methodology

**Resolve the input.** A URL is normalised (scheme added, tracking parameters
and fragments stripped). A *name* goes to Haiku for a guess which is then
**verified**: the name is fuzzy-matched against the page title, the leading
text, and every domain in the redirect chain. Below threshold the organisation
is rejected with a message asking for the URL. An unverified guess turns a
confident wrong answer into a confident wrong profile.

**Discover links.** Homepage anchors (subdomains count as same-site; PDFs are
kept from any domain, since reports often sit on a CDN), plus `sitemap.xml`
with one level of index following, preferring URLs that match schema keywords.
RSS/Atom feeds are probed on the news or blog host as well as the main one —
`khanacademy.org` advertises no feed while `blog.khanacademy.org` has one with
ten dated posts. Feeds are the cheapest data on a site: real publication dates,
no model involved.

**Pick pages.** Haiku receives the candidate list and returns **indices, not
URLs** — it cannot invent a page that way, and the reply is a fraction of the
tokens. If it fails twice the run continues on the homepage and any reserved
financial pages rather than guessing.

**Financial documents are not left to the model.** One of the four page slots
is reserved for annual reports, 990s and financial statements, chosen by
keyword scoring in code. Report PDFs linked from a fetched page are followed
one level down, which is the only way to reach them in practice:
`feedingamerica.org`'s homepage has **zero** PDF links while its financials
page has **thirteen**.

**Parse.** `trafilatura` for HTML with a BeautifulSoup fallback. The homepage
footer is extracted separately — that is where the EIN, phone and address live,
and trafilatura strips footers by design. PyMuPDF for PDFs, with pages selected
by scanning locally for financial markers and ranking them by marker count and
figure density, strongest first. A fixed page cap does not work: Feeding
America's 990 puts its statements at page 86 of 91, and Trussell's annual
report at page 101 of 150.

**Fallbacks.** A page yielding too little text, or a homepage with almost no
anchors, is re-fetched with headless Chromium. So is anything answering
401/403/406/429/503, which usually means bot protection — HTML is re-rendered,
while feeds and PDFs go through the browser's HTTP stack instead, since
*rendering* a feed returns Chromium's XML viewer markup. Requests retry twice
with backoff on network errors, 429 and 5xx; 404 fails immediately. Every
failure is recorded in `meta.failed_pages`.

**Extract.** Documents are numbered and labelled with their source, then sent
to Sonnet with the schema and instructions to use only the supplied text and
return null rather than guess. The reply is validated with Pydantic; errors go
back for one retry; anything still invalid is recorded as a warning rather
than silently half-saved.

**Enrich and compute.** ProPublica supplies multi-year IRS figures and the NTEE
code, merged rather than substituted (§7). Then code fills in everything
derivable.

## 6. Tools and tech

| Tool | Why |
|---|---|
| **Claude Haiku 4.5** | Link picking and URL guessing. Routing does not need a frontier model; it is 15% of the bill. |
| **Claude Sonnet 5** | Extraction — see the measurement below. |
| `httpx` | HTTP with timeouts |
| `beautifulsoup4` + `lxml` | anchors, sitemap and RSS parsing |
| `trafilatura` | main-text extraction that drops navigation and boilerplate |
| `pymupdf` | PDF text with per-page access, for marker-based selection |
| `playwright` | headless Chromium for JS-rendered and bot-protected pages |
| `pydantic` | the schema *is* the validation — one definition drives the prompt, the validation and the output |
| `rapidfuzz` | URL verification and ProPublica name matching |
| **ProPublica Nonprofit Explorer** | IRS 990 data, free and structured. Reading the filing beats parsing numbers out of a PDF. |

**Sonnet, not Haiku, for extraction — measured, not assumed.** Haiku is 59%
cheaper and gets the numbers wrong:

| Org | Model | Cost | Revenue extracted |
|---|---|---:|---|
| charity: water | Sonnet 5 | $0.0318 | 82,267,852 |
| charity: water | Haiku 4.5 | $0.0130 | **95,010,230** |
| Trussell | Sonnet 5 | $0.0324 | 62,813,000 |
| Trussell | Haiku 4.5 | $0.0134 | **62,813** |

I tried using Haiku for everything to make it cheaper but:

UK charity accounts are printed in **£'000s**: the table reads "62,813" meaning
£62.8M. Sonnet applied the convention, Haiku took it literally and was wrong by
1000×, which silently moves the organisation from `10M-100M` to `<1M`.

**Structured outputs, where they fit.** The link picker uses the API's
structured outputs, so its enum values cannot come back invalid. The full
profile schema **exceeds the grammar compiler's size limit** (`The compiled
grammar is too large`), so extraction sends the schema in the prompt and
validates in code. The schema text is generated from the Pydantic models, so
the prompt cannot drift from what validation accepts.

## 7. Cost, scale and feasibility

Measured from the run committed in `examples/`, via each profile's own
`meta.tokens` and `meta.cost_usd`.

| Organisation | Cost | Haiku in/out | Sonnet in/out | Pages | PDFs |
|---|---:|---:|---:|---:|---:|
| charity: water | $0.0367 | 4,422 / 115 | 12,591 / 656 | 5 | 1 |
| Code for America | $0.0365 | 1,630 / 138 | 13,194 / 780 | 6 | 1 |
| Feeding America | $0.0384 | 5,875 / 132 | 13,082 / 571 | 5 | 1 |
| Khan Academy | $0.0269 | 4,984 / 136 | 7,311 / 661 | 6 | 0 |
| The Trussell Trust | $0.0396 | 6,371 / 123 | 12,214 / 817 | 5 | 1 |
| **Mean** | **$0.0356** | 4,656 / 128 | 11,678 / 697 | 5.4 | 0.8 |

Sonnet input is 66% of the bill, Haiku 15%, Sonnet output 20%. Non-token cost
per organisation: ~5.4 HTTP requests plus robots/sitemap/feed probes, 0.8 PDFs,
and **19% of pages rendered in Chromium** — a second or two of real CPU each,
and the most expensive thing here that is not tokens.

### It started at $0.0837 and that was mostly waste

| Change | Effect |
|---|---|
| **`effort: low` on extraction** | Sonnet 5 thinks by default and thinking bills as output. It was **76% of output tokens** and made results no better — on charity: water, 5,197 output tokens against 1,266, where the cheaper run found one *more* fiscal year. |
| **Cut eight fields that came back empty** | Removed two page slots as well, taking `MAX_PAGES` from 6 to 4. Sonnet input fell 18,273 → 11,678. |
| **Source numbers instead of URLs** in `field_sources` | Echoed CDN links were ~20% of the reply. |
| **Terser wording**, capped programme descriptions | |

**57% cheaper with nothing measurably lost** — leaders (20), programmes (20)
and fiscal years (22) are identical across the test set before and after.
Output per organisation fell 4,161 → 697 tokens.

### At 500,000 organisations

| Scenario | Cost |
|---|---|
| One full pass, as built | **~$17,800** |
| Full pass via the Batch API (50%) | **~$8,900** | 
| Naive quarterly refresh of everything | ~$71,000/yr |
| **Tiered refresh (below)** | **~$20,000/yr** |

Batch API: Claude's non real time alternative for big file requests.

### Tiered refresh: why "cheap and generic" is the strategy

Refreshing everything quarterly costs four times the build and is almost
entirely wasted, because the fields have completely different half-lives:

| Tier | Fields | Changes | How to refresh | Token cost |
|---|---|---|---|---|
| **Static** | name, EIN, founded, cause area | ~never | on demand | none |
| **Slow** | mission, programmes, leaders, geography | yearly | annual full re-extract | full price |
| **Scheduled** | financials | on a filing calendar we can predict | ProPublica API | **zero** |
| **Volatile** | news, campaigns, leadership changes | weekly | RSS feed | **zero where a feed exists** |

Two of those tiers cost no tokens at all. ProPublica is a JSON API. An RSS feed
gives dated headlines with no model involved — and feed discovery already
handles the blog-subdomain case most sites use. In the test set 2 of 5
organisations had a usable feed; the rest need a page fetch, and **Haiku is
good enough for that**, since the measurement above shows its weakness is
numbers, not narrative.

So a realistic steady state is one annual full pass (~$17,800) plus continuous
near-free refresh of the fields that actually decay — roughly **$20,000/year**
rather than $71,000, for data that is *fresher* than quarterly.

**This is what the generic core buys.** A narrow, expensive per-buyer schema
cannot be refreshed this way, because its fields don't separate by volatility —
they separate by customer. Cheap and generic is not the compromise; it is the
thing that makes continuous freshness affordable.

### Other levers, in order

1. **Batch API** — 50% off, no quality cost, and this work is not
   latency-sensitive.
2. **Prompt caching** — ~12% of input tokens are the fixed system prompt and
   schema, identical every call.
3. **Trim the documents, not just the schema.** 11,678 input tokens is the bulk
   of the bill and a 990 contributes text we largely discard.
4. **Haiku for organisations with no financial PDF.** Khan Academy has none and
   already costs $0.0269.
5. **IRS bulk data** instead of per-organisation API calls.

## 8. Limitations

- **Name resolution depends on the model knowing the organisation.** It handles
  misspellings ("Feading America" resolves correctly) and rejects fake and
  ambiguous names cleanly, but a small local nonprofit it has never seen will
  fail. A search API is the upgrade.
- **ProPublica is US-only and lags.** Both US test organisations' most recent
  filings exist there only as PDFs with no structured figures, which is why IRS
  data is *merged* with the site's rather than replacing it. Non-US
  organisations get financials only from their own reports.
- **Name matching can mis-match, and did.** An early version scored with
  `token_set_ratio` alone, which returns 100 whenever one name's tokens are a
  subset of the other's. It matched the UK charity Trussell to "Robert And
  Martha Trussell Familyfoundation" and attached that foundation's finances.
  Matching now requires two scorers to agree, which also rejects Feeding
  America's independently-run member food banks. A wrong EIN is worse than no
  EIN: it fails silently and looks plausible.
- **Four pages per organisation is tight**, and is most of why a profile costs
  3.6 cents. When a site has several financial documents they take slots from
  leadership and news pages.
- **Some high-value fields were cut because they were unreachable, not because
  they don't matter.** `open_roles` and `rfps` came back 0/5: jobs live in
  JavaScript-rendered third-party boards (Greenhouse, Lever, Workday) on
  another domain. The fix is a job-board API reader, not a better prompt.
- **`campaign` is 0/5 and on probation.** It survived the cut because a named
  campaign is a strong signal, but the one instance the old schema found came
  from an annual report the tighter page budget no longer fetches.
- **Extraction varies between runs.** The same PDFs produced FY2025 revenue of
  $82,267,852 on one run and $90,800,000 on another. This is why IRS data is
  preferred where it exists.
- **Scanned PDFs yield nothing** — no OCR; the page is recorded as failed.
- **Freshness is inherited.** If a site is a year out of date, so is the
  profile. `meta.crawled_at` records when we looked.
- **`robots.txt` is respected**, so some sites legitimately yield less.

---

## Bonus: categorisation

Three categories, each assigned the cheapest reliable way:

| Category | How | Consistency mechanism |
|---|---|---|
| **Cause area** | IRS NTEE code's first letter → major group; falls back to the model choosing from the same fixed list | 10-value fixed taxonomy; most organisations are classified with no model opinion at all |
| **Size bucket** | computed from latest revenue | arithmetic — identical inputs always give identical outputs |
| **Geographic scope** | model, constrained to `local` / `national` / `international` | three values, validated on the way in |

Keeping this consistent across 500,000 organisations:

1. **Fixed taxonomies, never free text.** The model picks from a list; it never
   invents a label.
2. **Validation rejects rather than coerces.** Anything outside the list becomes
   `Unknown`, which is auditable. A silently invented category is not.
3. **Prefer the deterministic source.** Where the IRS has classified an
   organisation that wins, and identical inputs give identical outputs forever.
4. **Record the source.** `cause_area.source` is `irs_ntee` or `llm`, so the
   model-assigned subset can be sampled and reviewed separately — audit the
   20% that needed judgement, not all 100%.

**A caveat.** IRS NTEE codes are self-selected at registration and go stale.
charity: water is coded `P80` (Human Services), so the agent labels it Human
Services, while the model — reading the actual site — said "International,
Foreign Affairs", which better describes an organisation drilling wells in
Ethiopia. Deterministic is not the same as correct. At scale I would keep the
IRS code as the stable key and surface the model's disagreement as a review
flag.

## What I'd improve with more time

- **The tiered refresh scheduler** (§7) — the single highest-value addition,
  and the one the architecture is already shaped for.
- **Job-board API readers** for Greenhouse, Lever and Workday.
- **Split the models by field type.** Haiku is reliable on narrative and
  dangerous on numbers; a Haiku pass for news and programmes with Sonnet
  reserved for financial documents would cut cost again.
- **Concurrency.** V1 is deliberately synchronous. Pages within an organisation
  and organisations within a batch are embarrassingly parallel.
- **A real search step** for URL resolution instead of model recall.
- **A labelled eval set.** With ~30 hand-checked organisations, prompt and model
  changes could be measured rather than eyeballed.

## What I spent

**About $2.20 of Claude API usage**, summed from the per-run cost every run
prints. Roughly half is committed test-set runs — several full passes as the
schema was measured and cut — and the rest is development.

Build time was roughly six hours.

### What the test set produced

Five organisations, two by name and three by URL. **5/5 processed, 0 failed, 0
failed pages out of 27 fetched.**

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
| campaign | 0/5 | |

Two results that justify forcing financial documents into the crawl:

- **Feeding America** — five fiscal years, from ProPublica's IRS filings merged
  with the 2024 Form 990 published on their own site (the year ProPublica holds
  only as a PDF), plus auditor RSM US LLP and 421 employees, neither of which
  is in IRS structured data at all.
- **The Trussell Trust** — a UK charity with no IRS record whatsoever, yet two
  years of financials (£62.8M and £54.1M) read out of a **150-page** annual
  report whose accounts begin on page 101.
