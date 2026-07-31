# HotDealBot — Master Fix & Enhancement Plan

> Compiled 2026-07-31 from a full line-by-line audit + performance review.
> **Executed 2026-07-31 (ver 2.2): Phases 0–5 complete** (all items). Phase 6 deferred by design.
> Verified: full compile sweep, app import check, and 40 offline smoke tests (all passing),
> including an end-to-end lock-takeover test with a live child process.

---

## Phase 0 — Critical safety fix

- [x] **0.1 🔴 Safe, identity-verified takeover in startup lock** — `src/startup_lock.py`
  Rewritten. `os.kill(pid, 0)` probe is gone; owner identity is proven by **PID + process
  creation time** (psutil) before any terminate — recycled PIDs of unrelated processes are
  never touched. Graceful terminate → wait → lock-poll with 25s deadline; metadata cleared
  on clean release; same takeover semantics on Windows and POSIX.
  *Implementation note:* Windows `msvcrt` locks are mandatory (readers blocked too), so the
  lock now claims a byte at offset 2^30 — keeping metadata at offset 0 readable by the
  second instance. Covered by smoke tests: unrelated-PID safety + live takeover (0.3s).

## Phase 1 — Crawler correctness + pipeline restructure

- [x] **1.1 Detail pages fetched only for NEW deals** — parsing no longer fetches details;
  `enrich_deal_fmkorea` / `enrich_deal_eomisae` run post-dedup via `service.enrich_new_deals`
  (15s timeout each, best-effort). ~95% of detail HTTP traffic eliminated.
- [x] **1.2 Persistent crawler singletons** — all 5 crawlers are module-level singletons;
  sessions/cookies (fmkorea anti-bot, ppomppu warmup) and detail caches (bounded, 500
  entries) survive across cycles. fmkorea warms up once per process, not per cycle.
- [x] **1.3 Per-site crawl budgets** — `CRAWLER_TIMEOUT_SECONDS_BY_NAME`: fmkorea 60s,
  eomisae 30s, others 20s.
- [x] **1.4 Regex fixes** — cookie hint now `escape\('…'\)` (matches real JS);
  id extraction now `[?&]document_srl=` (no empty-string match, rejects prefixed params).
- [x] **1.5 Per-article `encode_deal_key` guards** — one bad row logs and skips instead of
  aborting the whole site's cycle (all 5 fetchers).
- [x] **1.6 gzip enabled** — base urllib crawlers accept+decompress gzip; requests-based
  crawlers already negotiated compression.
- [x] **1.7 lxml parser** — via `make_soup()` helper with graceful `html.parser` fallback.
- [x] **1.8 Page-hash short-circuit** — unchanged list pages skip parsing entirely
  (cache in `BaseCrawler.get`).
- [x] **1.9 Playwright cooldown** — failed fallback sets a 5-min cooldown; no more
  Chromium cold-launch every cycle while blocked. (Persistent browser deliberately NOT
  used: sync Playwright objects are not safe across executor threads.)

## Phase 2 — Delivery loop restructure

- [x] **2.1 Three-phase cycle** — `build_delivery_plan` (lock) → `enrich_new_deals` +
  `deliver_plan` (no lock) → `finalize_deliveries` (lock). Category/site tokens computed
  once per deal (absorbs old 6.2).
- [x] **2.2 Batched embeds** — up to 10 embeds per message per channel; on a batch-level
  400 the chunk falls back to individual sends to isolate the bad embed.
- [x] **2.3 Concurrent delivery** — channels + DM users delivered via `asyncio.gather`;
  per-channel ordering preserved.
- [x] **2.4 Retry give-up caps** — 3 cycles of purely-permanent failures (or 10 of any)
  → deal marked seen + one error log. No more infinite retry loops.
- [x] **2.5 Embed truncation** — title/field-name/author clamped to 256 via `_clamp_text`.
- [x] **2.6 Price fallback fixed** — `deal.get('price') or '가격 정보 없음'`; no more `> ****`.
- [x] **2.7 Dead placeholder.com removed** — thumbnail/icon simply omitted when missing.
- [x] **2.8 Local-logo attachments removed** — ppomppu/fmkorea logos now hosted URLs
  (Google s2 favicons, consistent with the other 3 sites); no file I/O per message;
  `discord.File` machinery deleted.
- [x] **2.9 `embed.timestamp`** replaces formatted KST footer (viewer-local rendering).
- [x] **2.10 Non-messageable channel guard** — treated as stale + unregistered.

## Phase 3 — Storage layer

- [x] **3.1 Per-cycle `refresh_runtime_config()` removed** — startup-only (bot is the sole
  writer).
- [x] **3.2 Batch marks** — `mark_deals_sent()` (one transaction per cycle) used by the
  planner, finalizer, and burst-cap paths.
- [x] **3.3 Persistent connection** — one module-level connection; `close_db()` on shutdown
  (in `main()`'s finally).
- [x] **3.4 Hygiene** — `PRAGMA optimize` on close; `VACUUM` after startup purge/compaction.
  (`created_at` stays TEXT — cosmetic, skipped deliberately.)
  Also added: `bot_meta` key/value table (used by 5.3).

## Phase 4 — Commands & UX

- [x] **4.1 `/알림 모드:삭제` skips add-validation** — only normalizes; legacy/now-invalid
  keywords are removable.
- [x] **4.2 `ALERT_KEYWORD_MIN_LEN = 3`** — matches the abuse check; error text shows 3~15자.
- [x] **4.3 Atomic cooldown** — `try_alert_update()` checks+consumes in one step, after
  validation (failed validation doesn't burn the cooldown).
- [x] **4.4 조회 no longer logs `*.success` audit events** (both filter commands).
- [x] **4.5 `/채널등록` instant + robust** — no inline crawl/priming (the background loop
  keeps `posted_deal_ids` current every minute); the new per-site burst cap
  (`MAX_NEW_DEALS_PER_SITE_PER_CYCLE = 10`) prevents floods after outages/first runs.

## Phase 5 — Runtime & operational robustness

- [x] **5.1 `from __future__ import annotations`** in all modules (fixes the
  `discord.abc.MessageableChannel` import crash on Python 3.10–3.13; annotation now removed
  anyway with the delivery rewrite).
- [x] **5.2 Queue-based logging** — `QueueHandler`/`QueueListener` for error/audit/runtime
  loggers; all `print()` calls replaced with `runtime_logger` (console + rotating
  `runtime_log.txt`). Event loop never blocks on console I/O.
- [x] **5.3 Conditional `tree.sync()`** — SHA-256 of command definitions stored in
  `bot_meta`; sync skipped when unchanged.
- [x] **5.4 Startup stale-channel prune dropped** — delivery-time detection covers it;
  the prune (still used on `guild_remove`) now checks channels concurrently.
- [x] **5.5 pytz → stdlib `zoneinfo`** (+`tzdata` dep for Windows); pytz removed.
- [x] **5.6 requirements.txt** — added lxml/psutil/tzdata, playwright marked optional with
  the `playwright install chromium` note, minimum versions pinned.
- [x] **5.7 Startup error handling** — `StartupLockError` ordered before `RuntimeError`
  (was dead code), plus `discord.LoginFailure` and `sqlite3.Error` with friendly messages.
- [x] **5.8 Error dumps anchored** to project `error/` dir, capped at 20 files.
- [x] **5.9 Transient per-user dicts pruned** hourly (cooldowns, DM-suppress markers).
- [x] **5.10 5 MB response cap** in base crawler.

## Phase 6 — Scale-readiness (deferred until usage grows)

- [x] **6.2 Canonicalize deal category/site once per cycle** — absorbed into 2.1.
- [ ] **6.1 Inverted keyword index / Aho-Corasick for alerts** — do when alert users grow
  into the hundreds.
- [ ] **6.3 Async HTTP (httpx/aiohttp) crawler rewrite** — only if executor-thread issues
  persist after Phase 1 (none observed in testing).

### Explicit non-goals (decided, don't revisit without new evidence)
- Keep SQLite (no Postgres/Redis at this scale).
- Keep `posted_deal_ids` as an in-memory set.
- Keep the circuit-breaker/retry-classification design.
- Keep the 1-minute poll interval.

### Deployment notes (ver 2.2)
- `pip install -r requirements.txt` (new deps: **lxml, psutil, tzdata**).
- First start after upgrade will run one slash sync (new hash), then skip on later boots.
- `source/fmkorea.png` and `source/logo_ppomppu.jpg` are no longer referenced by code.
- Upgrade edge: if an OLD-version bot is still running during the first NEW-version start,
  the old lock byte (offset 0) differs from the new one (offset 2^30) — stop the old bot
  manually once during the upgrade.
