"""LIVE-safe AI artifact persistence. PAPER and LIVE never share a directory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from agentic_portfolio.ai.errors import PaperContaminationError
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import LIVE_SOURCE_OF_TRUTH, RuntimeMode
from agentic_portfolio.schemas import to_dict


def ai_dir(root: Path | None, runtime_mode: RuntimeMode | str) -> Path:
    mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
    if mode == RuntimeMode.LIVE.value:
        return (root or project_root()) / "state" / "live_ai"
    return (root or project_root()) / "state" / "paper_ai"


class AIArtifactStore:
    def __init__(self, root: Path | None = None, *, runtime_mode: RuntimeMode | str = RuntimeMode.PAPER) -> None:
        self.base = root or project_root()
        self.runtime_mode = runtime_mode.value if isinstance(runtime_mode, RuntimeMode) else str(runtime_mode).upper()
        self.root = ai_dir(self.base, self.runtime_mode)
        for name in ("screenings", "research", "decisions", "proposals", "scans"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> dict[str, Any]:
        if self._index_path.exists():
            return json.loads(self._index_path.read_text(encoding="utf-8"))
        return {"screenings": [], "research": [], "decisions": [], "proposals": [], "scans": []}

    def _save_index(self) -> None:
        self._index_path.write_text(json.dumps(self._index, indent=2, default=str), encoding="utf-8")

    def _stamp(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = to_dict(record)
        payload["runtime_mode"] = self.runtime_mode
        payload["source_of_truth"] = LIVE_SOURCE_OF_TRUTH if self.runtime_mode == RuntimeMode.LIVE.value else "isolated_paper_book"
        payload["paper_environment"] = self.runtime_mode != RuntimeMode.LIVE.value
        payload["live_order_placement"] = False
        return payload

    def _write(self, kind: str, artifact_id: str, record: dict[str, Any]) -> dict[str, Any]:
        payload = self._stamp(record)
        if self.runtime_mode == RuntimeMode.LIVE.value and str(payload.get("runtime_mode")) != RuntimeMode.LIVE.value:
            raise PaperContaminationError("refusing to persist a non-LIVE artifact in live_ai")
        path = self.root / kind / f"{artifact_id}.json"
        if path.exists():
            raise FileExistsError(f"{kind} artifact already exists: {artifact_id}")
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        meta = {
            "id": artifact_id,
            "ticker": payload.get("ticker"),
            "created_at": payload.get("created_at") or payload.get("timestamp"),
            "runtime_mode": payload["runtime_mode"],
            "path": str(path.relative_to(self.root)),
            "status": payload.get("status"),
            "recommended_action": payload.get("recommended_action") or payload.get("action"),
        }
        self._index.setdefault(kind, []).append(meta)
        self._save_index()
        return payload

    def save_screening(self, screening_id: str, record: dict[str, Any]) -> dict[str, Any]:
        return self._write("screenings", screening_id, record)

    def save_research(self, research_id: str, record: dict[str, Any]) -> dict[str, Any]:
        return self._write("research", research_id, record)

    def save_decision(self, decision_id: str, record: dict[str, Any]) -> dict[str, Any]:
        return self._write("decisions", decision_id, record)

    def save_proposal(self, proposal_id: str, record: dict[str, Any]) -> dict[str, Any]:
        return self._write("proposals", proposal_id, record)

    def save_scan(self, scan_id: str, record: dict[str, Any]) -> dict[str, Any]:
        return self._write("scans", scan_id, record)

    def _read_kind(self, kind: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        folder = self.root / kind
        if not folder.exists():
            return rows
        for path in sorted(folder.glob("*.json")):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue
        return [row for row in rows if self._belongs(row)]

    def _belongs(self, row: Mapping[str, Any]) -> bool:
        mode = str(row.get("runtime_mode") or "").upper()
        if mode and mode != self.runtime_mode:
            return False
        if self.runtime_mode == RuntimeMode.LIVE.value and row.get("paper_environment") is True:
            return False
        return True

    def screenings(self) -> list[dict[str, Any]]:
        return self._read_kind("screenings")

    def research_reports(self) -> list[dict[str, Any]]:
        return self._read_kind("research")

    def decisions(self) -> list[dict[str, Any]]:
        return self._read_kind("decisions")

    def proposals(self) -> list[dict[str, Any]]:
        return self._read_kind("proposals")

    def scans(self) -> list[dict[str, Any]]:
        return self._read_kind("scans")

    def latest_for_ticker(self, kind: str, ticker: str) -> dict[str, Any] | None:
        rows = [row for row in self._read_kind(kind) if str(row.get("ticker") or "").upper() == ticker.upper()]
        if not rows:
            return None
        rows.sort(key=lambda r: str(r.get("created_at") or r.get("timestamp") or ""), reverse=True)
        return rows[0]

    def has_open_proposal(self, ticker: str, *, session_date: str | None = None) -> bool:
        for row in self.proposals():
            if str(row.get("ticker") or "").upper() != ticker.upper():
                continue
            if session_date and str(row.get("session_date") or "") != session_date:
                continue
            if str(row.get("status") or "") in {"PROPOSED", "DRAFT"}:
                return True
        return False
