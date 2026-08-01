# HotDealBot Data Platform — Deep Plan

> Goal: evolve the bot into the data source for a public price-analytics site
> (think PriceEmpire, but for Korean hot deals): price history per product,
> average prices, how often products go on sale, per-site/per-category stats —
> with special focus on popular items like PC components.
>
> Written 2026-08-01. Phase A is IMPLEMENTED in the bot (ver 2.2+); B–E are design.

---

## The core insight that drives everything

**Product matching can be re-run retroactively; raw capture cannot.**
Titles, prices, and timestamps must be recorded from day one. Any matching or
analytics pipeline can be rebuilt later over the stored raw rows — so Phase A
(capture) ships immediately inside the bot, and everything else can iterate
offline without touching bot uptime.

---

## Phase A — Raw capture inside the bot ✅ IMPLEMENTED

Every NEW deal (post-dedup — including deals suppressed by the burst cap or
with no recipients) is appended to a `deal_history` table in `bot_state.db`:

| column        | notes                                                        |
|---------------|--------------------------------------------------------------|
| deal_key      | PK — same canonical key as dedup (`qr-qb_saleinfo-123`)      |
| site_code     | quasarzone / arcalive / fmkorea / ppomppu / eomisae          |
| title         | raw crawled title (never overwritten — matching input)       |
| category      | normalized bot category (식품/PC/...)                         |
| price_raw     | raw price text as crawled ("22,900원", "$120", "무배")        |
| price_amount  | parsed numeric (REAL, NULL when unparseable)                 |
| currency      | KRW / USD / EUR / JPY / '' when unknown                      |
| url           | article URL                                                  |
| image_url     | thumbnail URL if any                                         |
| first_seen_at | UTC timestamp (CURRENT_TIMESTAMP)                            |

- Parsing lives in `src/price_parse.py` (만원 units, comma thousands, $/€/¥;
  deliberately does NOT guess on "무료/무배" — that usually means shipping).
- Volume estimate: ~500–1,500 deals/day ≈ 15–45MB/year in SQLite. Trivial.
  `deal_history` is intentionally EXEMPT from the 60-day purge that trims the
  dedup table — it grows forever (that's the point).
- Enrichment caveat: eomisae detail-page prices arrive after plan time, so a
  few eomisae rows have empty price_raw. Fine — titles carry most prices, and
  ETL can re-parse titles anytime.

## Phase B — Product identity (the hard problem, PriceEmpire's moat)

Mapping messy Korean deal titles → canonical products. Run OFFLINE (separate
ETL script/repo), never inside the bot.

**Matching ladder (precision-first):**
1. **Deterministic SKU extraction** — PC components have strong model tokens:
   `RTX 5080`, `RX 9070 XT`, `9800X3D`, `SN850X 2TB`, `DDR5-6000 32GB`,
   `990 PRO`, `AW2725Q`. Regex/token rules give near-100% precision exactly on
   the "popular PC parts" target. Start here; this alone powers a useful site.
2. **Brand + alias dictionary** — repo-maintained tables mapping Korean/English
   variants (삼성↔Samsung, 조텍↔Zotac, 에펨 store names to strip, etc.).
3. **Fuzzy match** (rapidfuzz token_set_ratio ≥ threshold) against the product
   catalog, writing (deal_key, product_id, confidence, method); below-threshold
   goes to a human review queue (simple web page or CSV).
4. Optional: **LLM batch classification** for the ambiguous tail (cheap model,
   suggestions only, never auto-committed).

**Product catalog sources (external DBs) — legality-ranked:**
- ✅ **Naver 검색 OpenAPI (shop.json)** — official, free tier (25k calls/day),
  returns product name/category/maker/price for a query. Best KR-legal way to
  resolve a title → canonical product + current market price context.
- ✅ **TechPowerUp GPU/CPU databases** — authoritative spec sheets for the GPU/
  CPU seed catalog (manual/scripted seed, small and static).
- ✅ **Open Icecat** — free official product-content catalog (brand/MPN keyed)
  for monitors/peripherals/appliances.
- ⚠️ **다나와(Danawa)** — the ideal Korean catalog but NO public API; scraping
  is against ToS. Do not build on it; at most manual cross-checking.
- ⚠️ PCPartPicker/Geizhals — same story, no official API. Avoid.

**Schema (analytics DB):**
```
products(id, canonical_name, brand, model_code, category, specs_json, image_url)
product_aliases(product_id, alias_norm)            -- matching accelerators
deal_product_map(deal_key, product_id, confidence, method, matched_at)
```

## Phase C — ETL + analytics store

- A **separate scheduled job** (Windows Task Scheduler / cron; later a
  Claude Code routine) that: reads new `deal_history` rows (WAL mode = safe
  concurrent reads while the bot runs) → runs the matching ladder → upserts
  into the analytics DB → refreshes aggregates.
- **Storage choice:** start with the same SQLite file, move to hosted
  **Postgres (Supabase/Neon free tier)** the moment the site goes public —
  concurrent web readers + the bot's writer don't belong on one SQLite file.
- **Precomputed aggregates** (tables or materialized views), because the site
  should never scan raw history per request:
  - `product_price_stats(product_id, week, min, avg, median, deal_count)`
  - `product_site_stats(product_id, site_code, deal_count, last_seen)`
  - `category_trends(category, week, deal_count, avg_discount)`
  - "deal score" = current price vs trailing 90-day average → powers a
    "historic low!" badge, the single most compelling site feature.

## Phase D — The site

- **Stack recommendation:** Next.js on Vercel + Supabase Postgres (all free
  tier to start), read-only API routes over the aggregate tables.
- **Pages:** Home (today's best deals vs historical average, trending),
  Product page (price-history chart, every past deal, sale frequency,
  per-site breakdown), Category browse (PC부품 first), Search, Stats
  (deals/day per site — the bot already tracks fetch metrics).
- Charts: build-time guidance exists (dataviz skill) — decide when building.
- SEO angle: product pages with real price history are exactly what ranks.

## Phase E — Close the loop back into Discord

- `/가격 <제품>` command: price history sparkline + historic low + last deals,
  straight from the analytics DB.
- Keyword alerts upgraded to **price-aware alerts**: "RTX 5080 이 90일 평균보다
  10% 이상 쌀 때만 DM" — this is the killer feature no Korean deal bot has.

## Sequencing & effort

| step | what | effort | status |
|------|------|--------|--------|
| A | capture in bot | small | ✅ done |
| B1 | SKU extractor + seed GPU/CPU/SSD catalog | medium | next |
| C1 | ETL script + aggregates (SQLite) | medium | after B1 |
| E1 | `/가격` command (validates the data end-to-end) | small | after C1 |
| C2 | move analytics to Supabase Postgres | small | when site starts |
| D | Next.js site MVP (product pages + home) | large | after C2 |
| B2–B4 | aliases, fuzzy, review queue, LLM tail | ongoing | iterate |

Guiding rule: **the bot stays a dumb, reliable recorder** — all intelligence
(matching, aggregation, serving) lives outside it, so analytics work can never
break deal delivery.
