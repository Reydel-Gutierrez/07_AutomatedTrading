"""Start the localhost dashboard. Default bind 127.0.0.1:3100."""

from __future__ import annotations

from agentic_portfolio.dashboard.app import create_app
from agentic_portfolio.dashboard.settings import resolve_bind


def main() -> None:
    from agentic_portfolio.autonomy.policy import resolve_state_root
    from agentic_portfolio.paths import project_root
    from agentic_portfolio.runtime import get_active_runtime

    bind = resolve_bind()
    # Same state root as scripts/run_service.py (LIVE: project root; PAPER: var/paper or AGENTIC_STATE_ROOT).
    root = resolve_state_root(project_root(), runtime_mode=get_active_runtime())
    app = create_app(root)
    print(
        f"Agentic dashboard http://{bind['host']}:{bind['port']} "
        f"(localhost bind; runtime={get_active_runtime().value}; state_root={root}; login required)"
    )
    app.run(host=bind["host"], port=bind["port"], debug=False, threaded=True)


if __name__ == "__main__":
    main()
