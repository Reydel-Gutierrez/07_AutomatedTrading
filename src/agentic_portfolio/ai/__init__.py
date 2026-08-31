"""Production AI gateway: structured, budgeted, proposal-only. Never places orders."""

from agentic_portfolio.ai.budget import BudgetManager
from agentic_portfolio.ai.errors import BudgetExhausted, PlacementForbidden
from agentic_portfolio.ai.gateway import AIGateway, build_gateway
from agentic_portfolio.ai.pipeline import run_candidate_pipeline
from agentic_portfolio.ai.safety import LIVE_AI_ALLOWED, LIVE_ORDER_PLACEMENT, LIVE_PROPOSALS_ALLOWED
from agentic_portfolio.ai.types import BudgetMode, ModelRole, RecommendedAction

__all__ = [
    "AIGateway",
    "BudgetExhausted",
    "BudgetManager",
    "BudgetMode",
    "LIVE_AI_ALLOWED",
    "LIVE_ORDER_PLACEMENT",
    "LIVE_PROPOSALS_ALLOWED",
    "ModelRole",
    "PlacementForbidden",
    "RecommendedAction",
    "build_gateway",
    "run_candidate_pipeline",
]
