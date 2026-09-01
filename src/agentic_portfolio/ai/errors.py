"""AI gateway errors. Fail closed. Never place an order to recover."""

from __future__ import annotations


class AIError(RuntimeError):
    """Base AI-layer error."""


class ProviderOutage(AIError):
    """Configured provider is unavailable."""


class ProviderTimeout(AIError):
    """Provider call exceeded the configured timeout."""


class MalformedResponse(AIError):
    """Provider returned non-JSON or otherwise unusable payload."""


def is_incomplete_max_output_tokens(exc: BaseException) -> bool:
    """True only for OpenAI incomplete responses caused by max_output_tokens."""
    if not isinstance(exc, MalformedResponse):
        return False
    text = str(exc).lower()
    return "incomplete" in text and "max_output_tokens" in text


class SchemaViolation(AIError):
    """Structured output did not match the required schema."""


class BudgetDenied(AIError):
    """Request was not authorized by the global budget manager."""


class BudgetExhausted(BudgetDenied):
    """Monthly $10 cap reached. No external AI calls until next calendar month."""


class PlacementForbidden(AIError):
    """LIVE AI pipeline attempted broker placement. Fail closed."""


class StaleSnapshotError(AIError):
    """LIVE snapshot is missing or too old to decide or propose."""


class MissingBrokerFacts(AIError):
    """Authoritative NAV/cash/buying power/positions are unavailable."""


class PaperContaminationError(AIError):
    """PAPER AI artifact leaked into LIVE decision/proposal path."""


class DuplicateJobError(AIError):
    """Scheduler refused a job that is already running."""
