"""Audit trail: per-session directory with prompts, logs, and report.json."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditTrail:
    def __init__(self, project_dir: Path, audit_dir: str, mission_name: str,
                 session_id: str) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        short = session_id[:8]
        safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in mission_name)
        self.dir = project_dir / audit_dir / f"{stamp}-{safe_name}-{short}"
        self.session_id = session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "prompts").mkdir(exist_ok=True)
        (self.dir / "responses").mkdir(exist_ok=True)
        (self.dir / "verification").mkdir(exist_ok=True)
        self._counter = 0
        self.events: list[Dict[str, Any]] = []

    def log_event(self, kind: str, data: Dict[str, Any]) -> None:
        event = {"ts": utcnow(), "kind": kind, **data}
        self.events.append(event)
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def save_prompt(self, label: str, prompt: str) -> Path:
        self._counter += 1
        path = self.dir / "prompts" / f"{self._counter:03d}-{label}.txt"
        path.write_text(prompt, encoding="utf-8")
        return path

    def save_response(self, label: str, state_json: Dict[str, Any]) -> Path:
        self._counter += 1
        path = self.dir / "responses" / f"{self._counter:03d}-{label}.json"
        path.write_text(json.dumps(state_json, indent=2, default=str), encoding="utf-8")
        return path

    def save_verification(self, attempt: int, results: list[Any]) -> None:
        path = self.dir / "verification" / f"attempt-{attempt:02d}.json"
        payload = [r.model_dump() for r in results]
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def save_json(self, filename: str, payload: Dict[str, Any]) -> None:
        (self.dir / filename).write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )

    def write_report(self, report: Dict[str, Any]) -> Path:
        path = self.dir / "report.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return path


def find_session_dir(project_dir: Path, audit_dir: str,
                     session_id: str) -> Optional[Path]:
    """Locate a session directory by full or prefix session id."""
    root = project_dir / audit_dir
    if not root.exists():
        return None
    exact = root / session_id
    if exact.is_dir():
        return exact
    matches = [d for d in sorted(root.iterdir()) if d.is_dir() and d.name.endswith(f"-{session_id[:8]}")]
    if len(matches) == 1:
        return matches[0]
    return None
