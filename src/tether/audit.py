"""Audit trail: per-session directory with prompts, logs, and report.json."""
from __future__ import annotations

import hashlib
import json
import re
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


def redact_secrets(obj: Any, *,
                   denylist: Iterable[str] = (),
                   allowlist: Iterable[str] = ()) -> Any:
    """Recursively replace values of obviously sensitive keys with a marker.
    Structure is preserved; adapter ``env`` mappings keep their keys but have
    every value redacted.

    ``denylist``: keys whose values are always redacted (exact, lower-case-
    insensitive name match); wins over ``allowlist`` and the built-in
    markers. ``allowlist``: keys never redacted even when their name contains
    a secret marker. Both default to empty, which keeps the built-in marker
    behavior exactly unchanged."""
    deny = {str(k).lower() for k in denylist}
    allow = {str(k).lower() for k in allowlist}

    def _walk(o: Any) -> Any:
        if isinstance(o, dict):
            out: dict = {}
            for k, v in o.items():
                lowered = str(k).lower()
                if lowered == "env" and isinstance(v, dict):
                    out[k] = {ek: (REDACTED if ev is not None else None)
                              for ek, ev in v.items()}
                elif lowered in deny and v not in (None, {}, []):
                    out[k] = REDACTED
                elif (_is_secret_key(str(k)) and lowered not in allow
                        and v not in (None, {}, [])):
                    out[k] = REDACTED
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(o, list):
            return [_walk(x) for x in o]
        return o

    return _walk(obj)


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


# Post-session scrub heuristics (dogfood-19): high-confidence secret VALUE
# shapes, extending the SECRET_KEY_MARKERS key heuristics above. Assignment
# forms anchor on a marker-bearing key; the remaining patterns match common
# provider credential token shapes outright. Best-effort by design.
SCRUB_VALUE_PATTERNS: Tuple[re.Pattern, ...] = (
    # key containing a secret marker, then "key = value" / '"key": "value"'
    re.compile(
        r"(?i)[\w.\-]*(?:"
        + "|".join(re.escape(m) for m in SECRET_KEY_MARKERS)
        + r")[\w.\-]*['\"]?\s*[:=]\s*['\"]?"
        r"([A-Za-z0-9_+/=\-]{12,})"),
    # common credential token shapes
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Authorization-style bearer credentials ("Bearer <token>")
    re.compile(r"(?i)\bbearer\s+['\"]?([A-Za-z0-9_+/=\-.]{12,})"),
)


def find_secret_spans(text: str) -> list[Tuple[int, int]]:
    """Sorted, merged character spans of secret-looking substrings in text.

    Purely textual best-effort detection for ``sessions scrub``: matches
    assignment values anchored by :data:`SECRET_KEY_MARKERS` and common
    provider token shapes. Overlapping hits are merged so each secret
    substring is reported once.
    """
    spans: list[Tuple[int, int]] = []
    for pattern in SCRUB_VALUE_PATTERNS:
        for m in pattern.finditer(text):
            start, end = (m.span(1) if m.lastindex else m.span())
            if end > start:
                spans.append((start, end))
    if not spans:
        return []
    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def redact_secret_value(value: str) -> str:
    """redact_body()-style marker for one scrubbed secret substring.

    Keeps the sha256/length auditability but — unlike redact_body() —
    echoes no excerpt of the secret itself.
    """
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"[REDACTED sha256={digest} len={len(value)}]"


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


def append_event_to_log(session_dir: Path, kind: str,
                        data: Dict[str, Any]) -> None:
    """Append an event to an existing session's events.jsonl (dogfood-19).

    Extends the tamper-evident chain: the new event's ``prev`` is the hash
    of the last existing event, so ``tether logs <id> --verify`` stays
    intact after a scrub. Best-effort: a missing or unparseable log simply
    starts (or restarts) the chain segment.
    """
    events = session_dir / "events.jsonl"
    prev_hash = ""
    if events.exists():
        lines = [ln for ln in events.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
        if lines:
            try:
                prev_hash = event_hash(json.loads(lines[-1]))
            except json.JSONDecodeError:
                pass  # unparseable tail: continue from an empty anchor
    event: Dict[str, Any] = {"ts": utcnow(), "kind": kind, **data}
    event["prev"] = prev_hash
    with events.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


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
