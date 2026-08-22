"""Audit trail: per-session directory with prompts, logs, and report.json."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def canonical_json(obj: Any) -> str:
    """Canonical JSON serialization used for the event hash chain."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def event_hash(event: Dict[str, Any]) -> str:
    """Hex sha256 of the canonical JSON serialization of an event."""
    return hashlib.sha256(canonical_json(event).encode("utf-8")).hexdigest()


SECRET_KEY_MARKERS = ("secret", "token", "password", "passwd", "api_key",
                      "apikey", "credential", "private_key", "auth")

REDACTED = "[REDACTED]"


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    if any(m in lowered for m in SECRET_KEY_MARKERS):
        return True
    # adapter env blocks: every value is potentially sensitive
    return lowered == "env"


def redact_secrets(obj: Any) -> Any:
    """Recursively replace values of obviously sensitive keys with a marker.
    Structure is preserved; adapter ``env`` mappings keep their keys but have
    every value redacted."""
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            if str(k).lower() == "env" and isinstance(v, dict):
                out[k] = {ek: (REDACTED if ev is not None else None)
                          for ek, ev in v.items()}
            elif _is_secret_key(str(k)) and v not in (None, {}, []):
                out[k] = REDACTED
            else:
                out[k] = redact_secrets(v)
        return out
    if isinstance(obj, list):
        return [redact_secrets(x) for x in obj]
    return obj


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact_body(text: str) -> str:
    """Redacted form of a prompt/response body: sha256 + length + excerpts.

    Keeps the record auditable (identifiable, bounded) without storing the
    full content.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return (f"[REDACTED sha256={digest} len={len(text)} "
            f"head={text[:64]!r} tail={text[-64:]!r}]")


class AuditTrail:
    def __init__(self, project_dir: Path, audit_dir: str, mission_name: str,
                 session_id: str, redact_prompts: bool = False) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        short = session_id[:8]
        safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in mission_name)
        self.dir = project_dir / audit_dir / f"{stamp}-{safe_name}-{short}"
        self.session_id = session_id
        self.redact_prompts = redact_prompts
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "prompts").mkdir(exist_ok=True)
        (self.dir / "responses").mkdir(exist_ok=True)
        (self.dir / "verification").mkdir(exist_ok=True)
        self._counter = 0
        self._prev_hash = ""  # hash chain anchor: empty for the first event
        self.events: list[Dict[str, Any]] = []

    def log_event(self, kind: str, data: Dict[str, Any]) -> None:
        event = {"ts": utcnow(), "kind": kind, **data}
        # Tamper-evident chain: each event records the hash of its predecessor.
        event["prev"] = self._prev_hash
        self._prev_hash = event_hash(event)
        self.events.append(event)
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def save_prompt(self, label: str, prompt: str) -> Path:
        self._counter += 1
        body = redact_body(prompt) if self.redact_prompts else prompt
        path = self.dir / "prompts" / f"{self._counter:03d}-{label}.txt"
        path.write_text(body, encoding="utf-8")
        return path

    def save_response(self, label: str, state_json: Dict[str, Any]) -> Path:
        self._counter += 1
        if self.redact_prompts:
            state_json = dict(state_json)
            for key in ("logs", "error"):
                value = state_json.get(key)
                if isinstance(value, str) and value:
                    state_json[key] = redact_body(value)
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
    """Locate a session directory by full or prefix session id.

    Raises ValueError when the prefix is ambiguous (multiple matches).
    """
    root = project_dir / audit_dir
    if not root.exists():
        return None
    exact = root / session_id
    if exact.is_dir():
        return exact
    matches = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        short = d.name.rsplit("-", 1)[-1]
        # Session dirs store session_id[:8]; accept prefixes of either side.
        if short.startswith(session_id) or session_id.startswith(short):
            matches.append(d)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listing = "\n".join(f"  {m.name}" for m in matches)
        raise ValueError(
            f"Ambiguous session id prefix {session_id!r}; matches:\n{listing}\n"
            "Use a longer prefix."
        )
    return None


def verify_event_chain(lines: Iterable[str]) -> Tuple[bool, str]:
    """Validate the prev-hash chain of events.jsonl lines.

    Returns (ok, message); on failure the message names the first broken
    event (1-based index and kind).
    """
    prev_hash = ""
    for idx, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as e:
            return False, f"event {idx}: invalid JSON ({e})"
        if not isinstance(event, dict):
            return False, f"event {idx}: not a JSON object"
        if event.get("prev") != prev_hash:
            kind = event.get("kind", "?")
            return False, (
                f"event {idx} (kind={kind!r}): 'prev' does not match the "
                f"hash of the previous event"
            )
        prev_hash = event_hash(event)
    return True, ""
