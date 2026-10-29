#!/usr/bin/env python3
"""Wrapper so `opencode run` works when invoked non-interactively (no TTY).

Handles environment quirks:
  1. opencode ignores/drops the positional message unless it has a PTY -> run
     under `script -q /dev/null` so a PTY exists.
  2. opencode silently drops any prompt containing an em-dash (U+2014), en-dash,
     smart quotes, or ellipsis when run without a TTY -> sanitize to ASCII first.

To make the Tether review gate reliable, when the prompt asks for a verdict
(contains "REVIEW:"), the wrapper retries up to N times (with spacing) until the
model's response actually contains a verdict line (REVIEW: APPROVE /
REVIEW: REQUEST_CHANGES). The verdict content is the model's genuine assessment;
we only retry so it is emitted in the parseable format Tether requires. For
non-review (agent) prompts it retries up to M times if opencode returns an empty
or "please provide code" response (transient free-tier failures).
"""
import subprocess
import sys
import time

model = sys.argv[1]
prompt = sys.argv[2]

repl = {
    "\u2014": "-",   # em-dash
    "\u2013": "-",   # en-dash
    "\u2019": "'",   # right single quote
    "\u2018": "'",   # left single quote
    "\u201c": '"',   # left double quote
    "\u201d": '"',   # right double quote
    "\u2026": "...", # ellipsis
    "\u00a0": " ",   # non-breaking space
}
clean = "".join(repl.get(c, c) for c in prompt)

is_review = "REVIEW:" in prompt
FAIL_MARKERS = (
    "I need code to review",
    "What would you like me to review",
    "I don't have any code",
    "Please provide the code",
    "I cannot provide a review",
)


def run_once():
    try:
        p = subprocess.run(
            ["script", "-q", "/dev/null", "opencode", "run", "-m", model, clean],
            capture_output=True, text=True, timeout=240,
        )
    except Exception:
        return ""
    return (p.stdout or "") + (p.stderr or "")


def has_verdict(out):
    for line in out.splitlines():
        s = line.strip().lower()
        if s.startswith("review: approve") or s.startswith("review: request_changes"):
            return True
    return False


final = ""
if is_review:
    for attempt in range(8):
        if attempt:
            time.sleep(4)
        out = run_once()
        final = out
        if has_verdict(out):
            break
else:
    for attempt in range(6):
        if attempt:
            time.sleep(4)
        out = run_once()
        final = out
        if out and not any(m.lower() in out.lower() for m in FAIL_MARKERS):
            break

sys.stdout.write(final)
sys.exit(0)
