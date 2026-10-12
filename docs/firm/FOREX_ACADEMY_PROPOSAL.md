# Forex Division + The 537 Academy — Design v2

Status: **design, not implemented.** v2 (2026-10-12) folds in the owner's decisions on the v1
feasibility study. Branch: `firm`.

## P0, simplified (owner, 2026-10-12). This supersedes the P0 row in §7

- **No separate practice system.** One minimal adapter (`src/forex/oanda.py`) serves both the live
  and practice endpoints. It is **read-only by construction**: GET-only transport, and no method
  that writes. OANDA tokens can't be scoped, so the code is the boundary.
- The owner's **live US v20 account** (to be funded with $50) is the integration target.
  **Everything runs on the Pi**, as the `agentic` service account:
  - credentials: `/home/agentic/.agentic-portfolio/forex/oanda.env` (file 0600, folder 0700),
    separate from the equity engine's `/etc/agentic-portfolio/env`. The owner writes it with
    `scripts/oanda_credentials.py set`, which prompts for the token without echo. The adapter
    refuses a credentials file other users could read;
  - read-only probe: `scripts/oanda_probe.py`. Reports go to
    `/opt/agentic-portfolio/state/forex/probe/` (0700).
- **The Academy uses only our virtual engine** ($10,000 per agent). Per-agent OANDA practice
  accounts and the PRACTICE stage are dropped. The CANDIDATE gate is replaced by the supervised
  execution test below, plus probation at small live size.
- Tim stays isolated: `src/firm` never imports `src/forex`, and a test enforces it. Nothing runs on the Pi.
- No autonomous forex trading and no further phases until the owner says so.

### Draft: minimal real-money execution test (finalized only after the read-only probe passes; never run without explicit owner authorization in chat)

| Item | Limit |
|---|---|
| Instrument | `EUR_USD` only, allowlisted in the script |
| Size | ≤ 100 units per order (~$117 notional, ~2.3:1 on $50), ≤ 200 units open at any time |
| Orders | ≤ 10 orders in total for the whole test; every entry carries `stopLossOnFill` (≤ 20 pips) |
| Worst-case loss | ≈ $0.05 (2 trades × 100 units × 20 pips + spread) |
| Guards | `OANDA_ENV=live`, account ID must match, `--authorize <phrase given by the owner>`, aborts on any unexpected state, ends flat with no pending orders, full transcript to `reports/oanda/` |

Steps, each verified against the transaction log:
1. Market buy 100 units with `FOK`, `priceBound`, stop-loss, take-profit and `clientExtensions`.
   Confirm the fill, the attached stop-loss and take-profit orders, the tags, and the half-spread
   cost.
2. Move the stop-loss (replace the dependent order).
3. Submit an opposite 100-unit order with `OPEN_ONLY`. We expect a no-hedging rejection, and the
   **error code** gets recorded.
4. Close the trade through the close endpoint. Confirm realized P&L and financing fields.
5. Open 100 units with a 2-pip take-profit and wait for it to trigger. This checks that a fill made
   by OANDA's own take-profit **carries our tags**, which the reconciler depends on.
6. Place a limit order far from the market, then cancel it, to test the cancel path.
7. Confirm the account is flat and reconcile the balance to the sum of transaction P&L.

## 0. Decisions on record (owner, 2026-10-12)

| # | Decision |
|---|---|
| D1 | **OANDA** (v20 REST and streaming) is the forex broker. |
| D2 | Both **day trading and swing trading**, each with specialized agents. |
| D3 | **$10,000 virtual capital** for every Academy agent. |
| D4 | First live allocation of **about $250–$500** per qualified agent, subject to broker minimums and practical sizing (§5.3). |
| D5 | **5:1 maximum leverage** at first. |
| D6 | **No fixed forex share of the firm.** The CIO recommends allocations from risk and performance. |
| D7 | **Qualified swing agents may hold over weekends** under stricter controls (§5.4). |
| D8 | **Only the owner** authorizes an agent's first live promotion or any increase in its capital authority. |
| D9 | The Academy is a **strategy development environment**, not a leaderboard. AI helps with creation and research; live execution stays deterministic. |
| D10 | Order of work: **OANDA practice integration and the Academy foundation first.** Live forex comes later, and Tim and Core Investments must not be disrupted. |
| D11 | Expand the Firm Space to show forex and Academy agents; redesign floors and divisions as needed. |

---

## 1. OANDA facts this design relies on

| Fact | Confidence | To confirm in P0 |
|---|---|---|
| US accounts (OANDA Corporation, an NFA member) can use the v20 REST and streaming APIs | Official docs | Token works on a US practice account |
| Practice environment: `api-fxpractice` / `stream-fxpractice`, with its own token | Official | — |
| Per-IP limits: 120 REST req/s, **20 active streams**, 2 new connections/s | Official | — |
| Up to **19 extra sub-accounts** per client; hedging only for ECP accounts | Official (US help center) | Whether practice accounts allow sub-accounts too |
| **FIFO and no-hedging apply per account** | Official article and NFA rules | Exact reject codes when an order breaks them |
| Leverage cap 50:1 on majors and 20:1 on other pairs (CFTC). **Our cap is 5:1** | Regulatory | — |
| Currency pairs only for US clients (about 68); no CFDs | Secondary sources | The instrument list our account actually gets |
| Smallest order is 1 unit, no minimum deposit | Secondary sources | Smallest order size; how the API rounds units |
| Moving money between sub-accounts happens in the HUB, not the API | Believed true | **Whether the API can do transfers** (affects §5.2) |
| `clientExtensions` tags carry through to fills triggered by server-side stop-loss and take-profit orders | Believed true | **Must confirm** (the reconciler depends on it) |
| Candles come in mid, bid and ask, with up to 5,000 per request, and can be aligned to a 17:00 New York day (`dailyAlignment`, `alignmentTimezone`) | From the v20 docs as I know them | Confirm |
| Owned by FTMO since 2025-12-01; fined $600k by the NFA in May 2025 | Press reports | Check NFA BASIC before funding |

---

## 2. Structure: three engines, one firm

Tim's safety comes first. Rather than adding forex to the process that trades Tim's real money,
the design runs forex and the Academy in a **separate engine with its own databases**. The two
engines share library code, governance, and HQ.

```
                         Investment HQ (one UI, one login)  :3100
                                   │  proxies Forex/Academy actions (local, authenticated)
        ┌──────────────────────────┴──────────────────────────────┐
        ▼                                                         ▼
 EQUITY ENGINE  (agentic-portfolio.service — unchanged role)  FOREX & ACADEMY ENGINE  (agentic-forex.service — new)
  Tim · Alex · Max                                              OANDA gateway (practice now, live later)
  Robinhood desk, ledger, reconciler                            Academy: registry · Lab · validation · paper books
  CIO (Graham), proposals, capital pool                         Forex desk (practice → live), risk engine
  firm.db  ◄── read-only ──────────────────────────────────►    academy.db · forex.db · marketdata.db
        │       firm controls (kill switch), approvals              control API 127.0.0.1:3101 (HQ only)
        └──────── consolidated firm NAV, events, Firm Space ◄── read-only
```

**Why separate processes:**
- A crash, memory leak, or busy backtest in forex can't stall Tim's trading or HQ.
- Forex runs 24 hours a day, 5 days a week. Deploying forex updates on a weekday must not restart
  Tim during equity market hours. A separate systemd unit restarts on its own. This one-time Pi
  change needs the owner (a new unit plus a sudoers line).
- Each process writes only to its own databases. That preserves today's rule of a single writer per
  database.

**Databases:**

| DB | Owner | Contents | Backup |
|---|---|---|---|
| `firm.db` | Equity engine | Unchanged, plus governance: owner approvals of promotions and capital authority (as `proposals`), consolidated firm marks | As today |
| `forex.db` | Forex engine | Chairs, real-money forex ledger (`PRACTICE` and later `LIVE` books), OANDA transaction cursor, orders, `fx_trades` | Encrypted nightly, same as `firm.db` |
| `academy.db` | Forex engine | Agents, versions, templates and specs, experiments, validation runs, Academy paper books (`ACADEMY` book), Academy events | Encrypted nightly |
| `marketdata.db` | Forex engine | Bid/ask candles, instrument metadata | **Not** backed up; it can be downloaded again |

**Communication** stays narrow and auditable:
- HQ reads forex and Academy state through **read-only** SQLite connections, the way the Firm Space
  reads today.
- Owner actions (create an agent, run a validation, approve a promotion) are posted from HQ to the
  forex engine's **local control API** on 127.0.0.1:3101, which accepts calls from HQ only (shared
  secret).
- The forex engine reads `firm.db` read-only every cycle for the **firm kill switch** and owner
  approvals. A firm-wide kill stops both engines.
- Live promotion and capital-authority changes are signed in `firm.db` by the owner's HQ session.
  The forex engine acts only on a matching approval row there. It can't approve itself.

The equity engine is the Firm, the forex engine is the Forex Division and Academy, and Graham sits
above both. The CIO's read tools gain the forex and Academy views. His writes stay limited to
proposals.

---

## 3. Organization and Firm Space redesign

### 3.1 Divisions

```
Owner
└── CIO — Graham  (allocation recommendations, evaluation memos, risk oversight; never promotes)
    ├── Core Investments Division      Tim · Alex · Max                     (equity engine)
    ├── Forex Division                 Day Desk  ·  Swing Desk               (live chairs only)
    ├── The 537 Academy                Strategy Lab · Trading Hall · Practice Range · Alumni
    │     ├── Research Lead (AI)       helps write strategies; cannot evaluate or promote
    │     └── The Examiner (no AI)     automated validation and qualification gates
    └── Risk & Operations (no AI)      desks, reconcilers, risk engine, kill switches
```

**Separation of duties.** The AI that helps *write* a strategy (Research Lead) must not be the one
that *judges* it (the Examiner's deterministic gates, then Graham's memo, then the owner). This
keeps a strategy's author from also being its grader. The owner names the Research Lead, as they
named Graham.

### 3.2 Firm Space: from one building to a campus

Today `Building.tsx` hard-codes four rooms (`ROOMS`: cio, tim, alex, max). With 30+ agents the space
has to be **built from data**: divisions, chairs, and agents come from `/api/space`, and the layout
is generated.

| Level | View | What it shows (from real records only) |
|---|---|---|
| **Campus** | Isometric exterior. The **HQ Tower** connects by skybridge to the **Academy Annex** | Lit windows where work is happening, a live/paused badge per floor, firm controls status |
| **Tower 3: Executive** | Graham's office · **Boardroom** · Risk & Operations room | Boardroom: pending promotion files on the table (from proposals). Risk room: kill switches, reconcile status, exposure gauges |
| **Tower 2: Core Investments** | Tim, Alex, Max (today's offices, kept) | As today |
| **Tower 1: Forex trading floor** | **Day Desk** pod (multi-monitor desks, wall clocks for Tokyo, London and New York with the active session lit) · **Swing Desk** pod · **open chairs** shown empty with a "Chair open" placard | Live chairs, their positions, P&L mood, the session, and weekend status |
| **Annex: Strategy Lab** | Workbenches with backtest rigs; the Research Lead's office | Validation runs in progress (a progress bar per rig, from `validation_runs`), Research Lead drafting when a Studio session is active |
| **Annex: Trading Hall** | Open-plan room with desks grouped by department, then by Day or Swing | Every Academy agent in paper trading: trading, waiting for the bar close, paused by the risk engine, degraded |
| **Annex: Practice Range** | A few desks connected to OANDA practice | Candidates in the practice stage, their order flow against the real practice broker |
| **Annex: Alumni wall** | Portrait wall | Graduated agents (linked to their chair) and retired agents (record kept) |

Details:
- Each character's look comes from its stored persona: colors and accessories the owner chose,
  plus a stable variant derived from the agent ID. Personality never affects trading.
- Only the floor in view animates; the others render static. Trading Hall desks are drawn on a
  grid from the agent count, with paging if there are more than about 40 agents.
- A promotion **walks the character** from the Practice Range across the skybridge to its chair on
  the forex floor. This happens only when the promotion record exists.
- The existing rule stays: *states must only come from real records.*

---

## 4. The Academy: a strategy development environment

### 4.1 Core objects

```sql
-- academy.db
strategy_templates (id, version, tier,                 -- 'code' | 'spec'
                    family,                            -- e.g. 'breakout', 'mean_reversion', 'trend' (counts trials, §4.6)
                    spec_json NULL, code_ref NULL,     -- spec: DSL document; code: module + git sha
                    params_schema_json, status,        -- immutable once PUBLISHED
                    created_by, created_at)
agents             (id, display_name, persona_json, department, style,   -- style: 'day' | 'swing'
                    stage, created_by, created_at, retired_at NULL)
agent_versions     (agent_id, version, template_id, template_version,
                    params_json, risk_envelope_json,   -- frozen; any change = new version
                    parent_version NULL, change_note, created_by, created_at)
experiments        (id, agent_id, hypothesis, change_summary, from_version, to_version,
                    outcome NULL, conclusion NULL, created_by, created_at, closed_at)
validation_runs    (id, agent_id, version, kind,      -- backtest | walk_forward | holdout | monte_carlo | baseline | sensitivity | gap_stress
                    data_hash, code_sha, cost_model_json, window_json,
                    metrics_json, passed, report_json, started_at, finished_at)
stage_transitions  (id, agent_id, version, from_stage, to_stage, by, reason, evidence_json, ts)
research_notes     (id, agent_id NULL, author, kind, body, sources_json, ts)   -- AI or owner research, cited
-- books: the ACADEMY book reuses the ledger schema (one sleeve per agent version)
```

**Reproducibility rule.** Every validation run stores the agent version, the data hash, the git sha
of the code, and the cost model. Any result can be re-run exactly. A result whose inputs can't be
reproduced doesn't count.

### 4.2 Three ways to define a strategy

| Tier | How it's made | What runs | Safety |
|---|---|---|---|
| **1. Template + parameters** | Owner (or Research Lead) picks a published template and sets parameters within schema bounds | Reviewed template code | Schema-validated |
| **2. Strategy spec** | Built from a closed library of indicators and conditions, sessions, stops, targets, trailing rules and filters. Written in the **Studio** by hand or from a Research Lead draft | A tested **interpreter** | It's data, not code. A JSON Schema check runs and a stop rule is mandatory. It can't do I/O, look at other agents, or see the future |
| **3. New code template** | When the spec language can't express an idea (e.g. regime models) | Python in the repo | A normal engineering change: tests, a backtest, owner merge, deploy |

The spec language and the indicator library are **the same code in every environment**: backtest,
paper, practice and live. Indicators see **completed bars only**; the interpreter makes it
impossible for a strategy to read bar *t+1* at time *t*.

### 4.3 The development loop

```
Idea ──► Draft (Studio; Research Lead may draft + cite research)
     ──► Quick backtest (seconds; in-sample, for iteration only — never counts as evidence)
     ──► Experiment record (hypothesis written BEFORE results)
     ──► Validation suite (Examiner): walk-forward · hold-out · costs · Monte Carlo · random baseline · sensitivity · [gap stress for weekend holders]
     ──► PAPER (ACADEMY book, $10k, live bar closes)
     ──► Review: Lab shows paper vs. expected band; Research Lead suggests improvements → new VERSION (back to validation)
```

Lab features that make it a development environment rather than a scoreboard:
- **Trade inspector:** chart each trade with entry, stop, target, and the indicator values the rule saw.
- **Breakdowns** by session, pair, weekday, volatility regime and trend/range regime, to see *where*
  the edge is.
- **Parameter sensitivity maps:** a robust strategy shows a plateau; an overfit one shows a spike.
- **Version comparison:** A/B any two versions on the same data and costs.
- **Experiment journal:** hypothesis, change, result, conclusion. Abandoned ideas stay on record, so
  the firm learns from what didn't work.
- **Clone and fork:** start a new agent from any version, and keep the family tree.

### 4.4 The AI's role (assist, never execute)

| Task | Who | Notes |
|---|---|---|
| Draft a spec from plain English | Research Lead (Claude) | The output must pass the schema check; the owner saves it |
| Research a concept (e.g. "London breakout evidence") | Research Lead with web search | Written to `research_notes` with sources |
| Critique a validation report and propose a next experiment | Research Lead | Creates a draft new version and never edits an existing one |
| Explain why a paper agent is under its expected band | Research Lead | Read-only tools over Academy data |
| Promotion memo, allocation recommendation | **Graham** | Evidence-cited; proposals only |
| Pass or fail a gate | **Examiner (no AI)** | Deterministic |
| Any trading decision | **Nobody with AI** | Specs and templates are deterministic |

Budget: the Research Lead gets its own share of the AI budget (`settings.ai_budget`), metered per
call like the CIO's. Studio work is on demand; nothing runs AI on a schedule by default.

### 4.5 Lifecycle and gates

```
DRAFT → VALIDATED → PAPER → QUALIFIED → PRACTICE → CANDIDATE → [owner] → LIVE·PROBATION → LIVE
                                                                             │
Automatic demotion: LIVE → PAPER (breach / decay) · PAPER → DRAFT (validation invalidated)   any → RETIRED
```

| Gate | Requirements (initial; owner may tune) |
|---|---|
| **→ VALIDATED** (Examiner) | Walk-forward out-of-sample over ≥ 3 years · out-of-sample Sharpe > 0.5 after costs · max drawdown inside the risk envelope · beats the **random-entry baseline** (same stops, sizing, sessions) at p < 0.05 · plateau sensitivity · ≥ 100 out-of-sample trades (day) or ≥ 40 (swing) · last 12 months hold-out not used in tuning · weekend holders pass gap stress |
| **→ QUALIFIED** (Examiner) | Paper: day agents ≥ 30 forex days **and** ≥ 100 trades; swing agents ≥ 60 days **and** ≥ 30 trades · paper results inside the validated expected band · **deflated Sharpe** > 0 counting every version and sibling in the strategy family · no risk-engine violations |
| **→ CANDIDATE** (Examiner) | Practice: ≥ 10 forex days on OANDA practice · zero reconcile breaks · no unexpected rejections · paper and practice fills agree within the slippage model |
| **→ LIVE·PROBATION** | Graham's memo (fit, correlation with live chairs, recommended authority) → **owner authorization (D8)** |
| **→ LIVE** (probation ends) | ≥ 30 forex days live inside the expected band. Raising authority above the probation level needs the owner (D8) |

A practical note: the paper gates take one to three months of calendar time. That is a deliberate
feature, not a delay to engineer away.

### 4.6 Anti-overfitting is structural

- **Trial counting.** Every version and every agent in the same `family` counts as a trial. The
  deflated Sharpe ratio and the baseline test account for them, so trying 40 tweaks of a breakout
  raises the bar for the 41st.
- **Hypothesis first.** An experiment's hypothesis is stored before its results exist.
- **Hold-out stays locked.** The most recent 12 months are used for validation only, and the
  Examiner refuses a run that tuned on them. The lock window rolls forward each quarter.
- **Paper must match the backtest.** A strategy that does much *better* on paper than validated is
  flagged too. The likely cause is luck or a bug.

---

## 5. Forex trading, risk, and capital

### 5.1 Desk and broker semantics (shared by paper, practice and live)

One **FX broker emulator** models OANDA's rules: FIFO, no hedging, units, margin, financing,
`priceBound`, server-side stop and take-profit, and stops filling *through* a price gap at the next
available price. Academy paper, backtests, and the risk engine all use it, so a paper agent can
never do something that would fail live.

Orders: entries are `MARKET` + `FOK` + `priceBound` + **`stopLossOnFill` (required)**, with an
optional take-profit or trailing stop. `positionFill` is explicit (`OPEN_ONLY` / `REDUCE_ONLY`). An
agent holds one net position per instrument. Every order and trade is tagged
`firm:<book>:<uuid>` and `agent:<id>@v<n>`.

**Bars are canonical from OANDA.** At each bar close (plus a few seconds), the engine fetches the
completed candle from OANDA's candles endpoint rather than building bars from the stream. Live and
backtest then see identical bars. The stream is used only for gates: freshness, spread, and the
tradeable flag.

### 5.2 Capital authority and CIO allocation (D6, D8)

Two numbers per live chair:

| Number | Set by | Meaning |
|---|---|---|
| **Capital authority** | **Owner only** | The most this agent may manage. Set at promotion ($250–$500). Raising it requires the owner (D8) |
| **Deployable capital** | CIO, within the authority | What the agent's sizing uses: `min(deployable, sub-account NAV)` |

- Graham recommends allocations from risk and performance. Within the owner's authority he may
  **lower** an agent's deployable capital at once (de-risk) and **restore** it later up to the
  authority. He can't raise authority or move money.
- Real money sits in **one OANDA sub-account per chair**, funded to the chair's authority.
  Funding, top-ups and moving money between chairs are owner actions in the OANDA HUB, unless P0
  shows the API supports transfers. Graham's recommendation becomes a checklist the owner executes,
  and the reconciler books the resulting `TRANSFER_FUNDS` transactions.
- Firm-level: forex has no fixed share of the firm (D6). Graham's allocation memo must show total
  forex exposure against firm NAV, and the owner sees it on every authority request.

### 5.3 Sizing at $250–$500 (D4, D5)

`units = (deployable × risk%) / stop distance in USD per unit`, then capped by the 5:1 leverage limit.

| Case | Inputs | Result |
|---|---|---|
| Day, EUR/USD, $250 | 0.5% risk = $1.25 · 15-pip stop | ≈ 833 units ≈ $900 notional → **3.6:1**, margin ≈ $18 |
| Day, tight 8-pip stop, $250 | Risk sizing alone → 6.8:1 | **Capped at 5:1** → ≈ 1,157 units, actual risk $0.93 |
| Swing, EUR/USD, $500 | 1% = $5 · 120-pip stop | ≈ 416 units ≈ $450 notional → **0.9:1** |

OANDA's 1-unit granularity makes these sizes workable. P0 confirms the minimum. P&L will be small
in dollars, which is the point of probation. Results are reported as **percentages**, so a $10k
paper record and a $250 live record compare directly.

### 5.4 Risk rules

| Rule | Day agents | Swing agents |
|---|---|---|
| Risk per trade | 0.5% of deployable | 1.0% |
| Leverage (Σ notional / sleeve NAV) | ≤ 5:1 | ≤ 5:1 |
| Firm-wide forex leverage (all live chairs) | ≤ 5:1 | |
| Margin used after the order | ≤ 25% of NAV | ≤ 25% |
| Daily loss stop (no new entries until 17:00 ET) | −2% | −3% |
| Sleeve drawdown | −10% pause · −15% demote to PAPER | same |
| Spread max · price freshness | Per pair · ≤ 5 s | same |
| Rollover window 16:55–17:10 ET | No entries | No entries |
| Friday | No new entries after 12:00 ET; flat by 16:30 ET | See weekend rules |
| Currency exposure (firm) | Net per currency across live chairs capped (owner sets) | |

**Weekend holding (D7), swing agents only, stricter:**
- The weekend flag is part of the agent's frozen risk envelope, and it must have passed **gap
  stress** in validation (historical weekend gaps per pair, 99th percentile).
- From Friday 12:00 ET, positions held into the weekend must satisfy: notional ≤ **2:1**;
  **gap-adjusted loss** (stop distance + 99th-percentile weekend gap) ≤ **1.5%** of the sleeve; a
  broker-side stop in place.
- At 15:30 ET Friday the engine trims anything still over these limits. Trimming is deterministic:
  reduce the position that's largest relative to its limit first.
- Graham can propose a firm-wide flat-for-the-weekend (e.g. elections, G7). The risk engine
  enforces it only after the owner approves; the owner can also set it directly.

### 5.5 Reconciliation (practice now, live later)

- Each chair's sub-account is reconciled to its book **1:1** using OANDA's ordered transaction IDs.
  Transactions are polled with `sinceid`, which uses no stream slots.
- Expected: fills, firm-tagged stop-loss/take-profit/trailing closes, financing, funds transfers.
  Severe: `MARGIN_CLOSEOUT` pauses the chair and alerts. Foreign (untagged) activity puts **that
  chair only** on hold.
- An ambiguous order outcome marks the order UNKNOWN and engages the **chair's** kill switch, with
  the same pattern as the equity desk.

---

## 6. Remaining architectural issues and how they're resolved

| # | Issue | Resolution |
|---|---|---|
| A1 | Forex running inside Tim's process risks the live equity book | Separate engine, unit and databases (§2) |
| A2 | Deploy windows: forex is open 24/5, equity has market hours | Each unit deploys on its own schedule. Forex deploys require no order in flight and every live position protected by a broker stop; otherwise wait for 17:00 ET or the weekend. `deploy_pi.py` gains a `--engine` flag |
| A3 | Two writers on one SQLite file | One writer per DB; cross-reads are read-only; commands go through the control API |
| A4 | Hard-coded roster and `ROOMS` | Agents and chairs are data; the Firm Space layout is generated (§3.2). Tim, Alex and Max stay in `firm.db` unchanged, and Academy equity agents come later |
| A5 | Differences between paper and live | One FX broker emulator, canonical OANDA bars, the same interpreter, practice stage, SIM shadow of live agents |
| A6 | Cross-venue money isn't fungible | Capital authority and deployable capital per chair; real transfers are owner HUB actions; consolidated NAV is read-only in HQ |
| A7 | Kill-switch reach | The forex engine honors the firm kill switch from `firm.db`, plus a division switch and per-chair switches |
| A8 | Live-trading switch scope | A **separate** owner switch `AGENTIC_FX_LIVE_ORDER_PLACEMENT`. The practice adapter is **hard-pinned to `api-fxpractice`** and can't reach live hosts. Live tokens are added by the owner only at P6 |
| A9 | Stream budget (20 per IP) | One pricing stream per engine; transactions by REST polling |
| A10 | 30 agents × alerts = Telegram noise | Academy activity goes to a daily digest; only gates, demotions, risk breaches and practice/live issues alert immediately |
| A11 | Pi resources | Backtests in a worker pool inside the forex engine at low priority with a CPU cap (systemd `CPUQuota`), queued. Heavy sweeps can run on the laptop with the same code, and results are uploaded with their data hash |
| A12 | SD card wear from candles and polling | **USB SSD** for `/opt/agentic-portfolio/state` before P1 data backfill |
| A13 | Time and DST | Forex day = 17:00 America/New_York; calendar module for forex sessions and holidays; NTP health in `/api/health` |
| A14 | Taxes (§988) | Per-sub-account realized P&L export each year |
| A15 | Counterparty | Funds per chair stay small; check NFA BASIC before live funding |

---

## 7. Phased plan (no disruption to Tim)

**Guardrails for every phase:**
- No change to the equity desk, ledger, reconciler or Tim's code path.
- Changes to the equity engine are limited to read-only HQ views, the Firm Space, a proxy to the
  control API, and governance rows.
- The full existing test suite plus `test_isolation` must pass on every forex/Academy commit.
- Equity-engine deploys happen outside market hours as today; forex engine deploys don't restart
  the equity engine.

| Phase | Builds | Done when | Owner action |
|---|---|---|---|
| **P0 — Practice spike** (on the laptop) | A probe script against OANDA **practice**: account, instruments, candles (BA, alignment), pricing stream, small practice orders to observe FIFO, no-hedge, stop-loss/take-profit on fill, `clientExtensions` on server-side closes, sub-accounts, transfers | Every row in §1 is confirmed or corrected in this doc | Open a US **practice** account and generate a token |
| **P1 — Forex engine foundation** | `agentic-forex.service`, its DBs, control API, OANDA **practice** gateway (read: pricing, candles, account), `marketdata.db` and backfill for ~20 pairs, forex calendar, kill-switch link, `deploy_pi.py --engine`, HQ health tiles, **Firm Space campus shell** (tower + annex; data-driven floors, existing offices unchanged) | Engine runs 2 weeks on the Pi with no effect on Tim; backfill complete; Space shows the new floors (empty) | Install the new unit + sudoers line; practice token in the env file; SSD |
| **P2 — Academy core (offline)** | Registry, versions, templates, spec language and indicator library, FX broker emulator, backtester, Examiner validation suite (walk-forward, hold-out, Monte Carlo, baseline, sensitivity, gap stress), Lab UI v1 (Studio for Tiers 1–2, reports, trade inspector, experiments) | 3–4 seed templates validated end to end; reports reproducible from data hash and sha | — |
| **P3 — Academy paper trading** | Bar-close runtime, ACADEMY book ($10k sleeves), risk engine, Trading Hall populated, leaderboard with confidence bands, Academy digest | A first cohort of about 6 agents (3 day, 3 swing) paper trading for 2 weeks with clean books | Approve seed agents' names and personalities (optional) |
| **P4 — OANDA practice integration** | Practice write path, transaction-cursor reconciler, practice sub-accounts per candidate, Practice Range, QUALIFIED → PRACTICE → CANDIDATE gates, Graham's Academy tools and promotion memos, Boardroom | One agent completes practice with zero breaks (realistically 1–3 months after P3, gated by evidence) | Create practice sub-accounts if P0 shows they must be made in the HUB |
| **P5 — Research Lead** | Claude-assisted Studio: plain English → spec, cited research notes, report critiques, draft next versions; per-role AI budget | The owner can go from idea to validated draft in the UI without code | Name the Research Lead; set its AI budget share |
| **P6 — Live readiness** (owner-gated) | Live gateway behind `AGENTIC_FX_LIVE_ORDER_PLACEMENT`, chair lifecycle, capital authority and deployable capital, weekend trim, forex trading floor live, consolidated firm NAV | Dry run on practice with live code path and switch off; owner-reviewed validation package | Open/fund live account and sub-account, add live token, set the switch, **authorize the specific agent and authority (D8)** |
| **P7 — Scale** | More chairs, decay monitoring, news blackout calendar, Academy for Core Investments (equity agents on Robinhood daily data) | — | Per-agent authorizations |

P0 through P5 involve **no real money**. P0–P3 don't even send orders anywhere except OANDA practice
during the P0 probe.

---

## 8. Owner decisions still open (not needed until noted)

| Before | Decision |
|---|---|
| P0 | Open the OANDA US **practice** account + token |
| P1 | Approve the second systemd unit and its sudoers line; buy the SSD (recommended) |
| P3 | Initial pair universe (proposal: 7 majors + 8 liquid crosses) |
| P5 | Research Lead's name and AI budget share |
| P6 | Firm currency-exposure cap; whether Graham may *lower* deployable capital without asking (recommended: yes, with an alert) |

---

### Sources

- [OANDA Development Guide](https://developer.oanda.com/rest-live-v20/development-guide) · [v20 Getting Started](https://developer.oanda.com/rest-live-v20/introduction/)
- [OANDA US: sub-accounts](https://help.oanda.com/us/en/faqs/create-additional-sub-accounts.htm) · [margin rules](https://help.oanda.com/us/en/faqs/margin-rules.htm) · [position limits](https://help.oanda.com/us/en/faqs/position-limits-or-client-and-trading-account-limits.htm)
- [OANDA US: the NFA FIFO rule](https://www.oanda.com/us-en/trade-tap-blog/trading-knowledge/the-nfas-fifo-rule-challenges-solutions-forex-expert-advisors/)
- [CFTC retail forex rules (Euromoney)](https://www.euromoney.com/article/b12kht4m12njyr/fx-news-cftc-releases-final-rules-covering-retail-fx)
- [FTMO completes OANDA acquisition (ETF.com)](https://www.etf.com/sections/news/ftmo-building-global-trading-powerhouse-completes-acquisition-oanda-cvc) · [NFA fine (Finance Magnates)](https://www.financemagnates.com/forex/oanda-fined-600000-by-us-regulator-over-compliance-failures/)
- [FXEmpire: OANDA US](https://www.fxempire.com/brokers/oandaus) · [QuantConnect: OANDA](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/brokerages/oanda)
