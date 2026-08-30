"""AI-driven Deep Research. Interprets evidence; does not trade."""

from agentic_portfolio.research.comparison import build_comparison
from agentic_portfolio.research.engine import compare_reports, request_refresh, run_research
from agentic_portfolio.research.packet import ResearchPayload, build_packet
from agentic_portfolio.research.reasoner import CallableResearchReasoner, ScriptedResearchReasoner
from agentic_portfolio.research.safety import (
    RESEARCH_FORBIDDEN_TOOLS,
    RESEARCH_READ_TOOLS,
    ResearchSafetyError,
    as_proposed_action,
    research_cannot_become_buy,
)
from agentic_portfolio.research.store import ResearchStore
from agentic_portfolio.research.types import (
    ResearchConclusion,
    ResearchEvidencePacket,
    ResearchReport,
    ResearchStatus,
    ResearchSubjectKind,
)

__all__ = [
    "CallableResearchReasoner",
    "RESEARCH_FORBIDDEN_TOOLS",
    "RESEARCH_READ_TOOLS",
    "ResearchConclusion",
    "ResearchEvidencePacket",
    "ResearchPayload",
    "ResearchReport",
    "ResearchSafetyError",
    "ResearchStatus",
    "ResearchStore",
    "ResearchSubjectKind",
    "ScriptedResearchReasoner",
    "as_proposed_action",
    "build_comparison",
    "build_packet",
    "compare_reports",
    "request_refresh",
    "research_cannot_become_buy",
    "run_research",
]
