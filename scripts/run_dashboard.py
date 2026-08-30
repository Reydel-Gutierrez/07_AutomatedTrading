"""Start the localhost dashboard. Default bind 127.0.0.1:3100."""

from __future__ import annotations

from agentic_portfolio.dashboard.app import create_app
from agentic_portfolio.dashboard.settings import resolve_bind


def main() -> None:
    bind = resolve_bind()
    app = create_app()
    print(
        f"Agentic dashboard http://{bind['host']}:{bind['port']} "
        f"(localhost bind; {bind.get('host')}; PAPER/LIVE banner; no live order placement; approve does not place; "
        "login required)"
    )
    app.run(host=bind["host"], port=bind["port"], debug=False, threaded=True)


if __name__ == "__main__":
    main()
