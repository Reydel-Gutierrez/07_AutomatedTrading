# Risk Rules

Deterministic gates. **Canonical numbers:** `config/portfolio_policy.json` (explained in `PORTFOLIO_POLICY.md`).

Account identity and execution flags: `config/account_rules.json`.

The agent may tighten. It may **never** loosen a hard ceiling. Proposals must use **current Agentic NAV** in the policy formulas — never a hardcoded portfolio size.

Buying power from `get_portfolio` remains a spendable cap (you cannot buy more cash-like BP than exists). That is not a dollar *policy budget*.

---

## SUPERSEDED (not current policy)

`SUPERSEDED_HISTORICAL_RULE` — do not enforce:

- $500 account budget  
- $50 max position  
- $250 max invested  
- max 5 positions  
- max 3 new trades/day  
- $15 daily realized loss  
- $5 minimum share price  
- universal 10% individual-equity cap  
- Speculative 2% per name (replaced by **3%**)  
- pending/null Other-ETF cap (replaced by **25%**)  
- averaging-down **blanket ban** (replaced by thesis + risk review on adds)  
- “tighter of testing dollars ∩ policy” intersection  
- any NAV-tier strategy ($500 / $5k / $40k)

---

## Concentration (hard ceilings, % of **total NAV**)

Not targets. Not default sizes.

| Class + sleeve | Max % NAV |
|---|---|
| `BROAD_MARKET_INDEX_ETF` | 40 |
| `OTHER_DIVERSIFIED_ETF` | 25 |
| `CORE_GROWTH` + `INDIVIDUAL_EQUITY` | 20 |
| `OPPORTUNISTIC` + `INDIVIDUAL_EQUITY` | 15 |
| `TACTICAL` + `INDIVIDUAL_EQUITY` | 10 |
| `SPECULATIVE` per security | 3 |
| `SPECULATIVE` sleeve total | 5 |

Unreliable classification → fail closed (no 40%/25%). See policy.

**> 10% NAV** any individual security → `ENHANCED_CONCENTRATION_REVIEW`.  
**> 15% NAV** `INDIVIDUAL_EQUITY` → `CONCENTRATION_LEVEL = HIGH`.  
Hard max still wins. Enhanced review ≠ automatic human approval in a future auto-exec mode.

**Sector:** review above **30%** NAV (`SECTOR_CONCENTRATION_REVIEW`); hard ceiling **45%** NAV. Drift above a cap: no further increase, review, no forced liquidation.

**Liquidity:** not share price. Normal: planned notional ≤ **1%** of 20-session median daily dollar volume. Speculative: `SPECULATIVE_LIQUIDITY_REVIEW` and ≤ **2%** of that ADV (configurable). Missing data → fail closed for new risk.

Position **count**: observed, not a hard max.

---

## Daily halt

If `(NAV - start_of_day_NAV) / start_of_day_NAV ≤ −2%` → `DAILY_RISK_HALT`.

Rest of session: no risk-increasing BUY; no adds. SELL/REDUCE/HOLD/WATCH/REJECT/NO_ACTION/analysis OK. No auto-liquidation of Core. Resets next session unless HWM state still blocks new risk.

---

## Drawdown vs HWM

`drawdown = (NAV / cash_flow_adjusted_HWM) - 1`

Deposits/withdrawals **scale** HWM so they are not treated as performance. Formula in `config/portfolio_policy.json` → `hwm`. Manual HWM reset is human-only.

| State | At | New risk |
|---|---|---|
| NORMAL | 0 to &lt; −5% | Ordinary |
| WARNING | −5% | Enhanced review; no auto-liq |
| RISK_REDUCTION | −10% | No new Spec/Tactical; Core OK with validation; Opp enhanced review; SELL/REDUCE OK |
| DEFENSIVE | −15% | No new Spec/Tactical; Opp add only if risk-reducing; new Core = validated broad-market ETF only |
| HALTED | −20% | `auto_execution` false. May **recommend** SELL/REDUCE/HOLD/NO_ACTION. May **not** execute until explicit human authorization. No self-resume. No invented liquidation. |

---

## Adds

Increase = new `INVESTMENT_THESIS_REVIEW` + `RISK_REVIEW`. Not “because price fell.” One logical position per symbol.

---

## Product bans (unchanged)

Agentic account only. Long US equity/ETF. No options, crypto, shorts, margin borrow, leveraged ETFs, extended hours. **No capital transfers.**

---

## Execution (unchanged)

`HUMAN_APPROVAL` · `auto_execution=false` · `live_trade_actions_allowed=false` · `require_human_approval=true` · stop after `review_equity_order` (does not place).
