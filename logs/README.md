# Logs

Pipeline traces. Decisions live in `JOURNAL.md`.

## Include

- Stage enter/exit
- Read-only MCP summaries
- Classification validate/fail-closed
- Risk: matrix rule ids, daily halt, HWM state
- Hard-stop before review
- Transfer refusals

## Exclude

- Review/place/cancel (must not happen)
- Full account numbers; auth payloads

Append-only. If a BUY dies at a ceiling, log the matrix key. If the pipeline tries to pass order plan, halt.
