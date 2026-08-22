# Security & Threat Model

This document describes Tether's security posture: what it protects against,
what it explicitly does not, and where its guarantees end. Read it before
running missions against projects you care about.

## Trust boundaries

Tether orchestrates autonomous coding agents. An agent run is **untrusted
output with full user privileges**. Tether adds checkpointing, auditability,
and bounded recovery around that execution — it does not confine it.

## What Tether does NOT protect against

### Untrusted agent output

An agent can write arbitrary files inside `allowed_paths`. The write sandbox
(`allowed_paths` / `forbidden_paths`) is a **detection** mechanism: violations
are caught by post-execution diff inspection and fail the mission. It is not
an OS-level enforcement boundary. A determined or buggy agent can:

- write outside allowed paths via means invisible to path-based diffing
  (symlink tricks, processes spawned during execution),
- execute arbitrary code if your verification commands interpret generated
  files (e.g. `make`, test runners importing code).

`sandbox_mode: enforce` narrows this but is still not a security container.
When you configure `allowed_paths`, run with `enforce` rather than the default
`warn`: warn mode relies only on content-based change detection and can miss
writes invisible to diffing (e.g. gitignored paths), which is exactly why
Tether logs an advisory warning for that combination.
Use OS-level isolation (containers, VMs, separate users) for untrusted agents.

### Prompt injection

Repository content read into agent context can carry instructions that hijack
the agent. Tether cannot prevent this. Mitigations are procedural:

- review `patch.diff` / session reports before trusting a run,
- prefer narrow `allowed_paths`,
- treat verification commands as the primary trust anchor.

### Secret leakage

Prompts, responses, logs, and audit artifacts may contain secrets present in
the project (env files, credentials in config). Tether redacts adapter `env`
settings in resolved config and offers optional prompt redaction, but
**redaction is best-effort pattern matching, not a guarantee**. Assume anything
in `.tether/sessions/` may contain sensitive material; treat that directory as
sensitive and exclude it from backups/syncs you do not control.

`tether sessions scrub <session-id> --confirm` rewrites high-confidence
secret patterns found in one session's records, but scrub is **best-effort
pattern redaction, not cryptographic erasure**: patterns it does not recognize
survive, the sha256-based markers keep a verifiable trace of what was removed,
and copies of scrubbed material can persist in backups, syncs, or earlier
exports of the session directory.

### Verification command risk

Verification commands (`python -m pytest`, etc.) run locally **with your full
user privileges**, on whatever code the agent produced. A malicious agent can
plant code that executes during verification. Only run missions whose
verification commands you would be willing to run on untrusted input.

## Rollback limits

- Git rollback resets tracked files listed in the session report and (with
  `--clean`) removes session-attributable untracked files. Pre-existing
  untracked user files are preserved — which means agent-created files that
  collide with pre-existing names may not be fully reverted.
- Non-git restores replace the tree from a tar backup verified by sha256
  sidecar. Files created *after* the backup but before detection are lost only
  if restore succeeds; a failed checksum refuses restore rather than restoring
  a truncated archive.
- Auto-rollback is opt-in, conservative, and scoped to reported changed files.

## Local lock scope

The single-writer lock (atomic `O_CREAT|O_EXCL`, stale takeover) prevents
accidental concurrent Tether runs against one project directory. It is an
advisory, per-project, local mechanism — not protection against another tool,
another user, or a network filesystem race.

## Audit chain

The session event log is a SHA-256 hash chain: tampering breaks verification
(`tether logs <session-id> --verify`). This makes the audit trail
**tamper-evident, not tamper-proof** — anyone with filesystem write access can
rewrite the entire chain consistently. For stronger guarantees, export session
directories to append-only storage.

## Reporting

Report suspected vulnerabilities privately to the repository maintainer.
