# Adapters

## The interface

Every adapter implements `tether.adapters.base.AgentAdapter`:

| Method | Purpose |
|---|---|
| `is_available()` | `(bool, reason)` — binary/config check |
| `start_session(project_dir, session_id)` | begin a session |
| `send(prompt, session)` | send a prompt, return `AgentState` |
| `cancel(session)` | best-effort stop |
| `plan_prompt` / `execute_prompt` / `repair_prompt` | default prompt builders (overridable) |

`AgentState` carries: `status` (pending/running/needs_input/completed/failed/cancelled/unavailable), `logs`, structured `result`, `changed_files`, `error`, optional `usage`.

Dry-run is enforced by the orchestration core before any adapter call, so every adapter supports it for free.

## Capability metadata

Every adapter declares static capability flags as class attributes on
`AgentAdapter` (safe defaults; subclasses override only what is true):

| Attribute | Meaning | Default |
|---|---|---|
| `supports_cancel` | `cancel(session)` terminates actively running work | `False` |
| `supports_process_tree_kill` | termination covers the whole child process tree | `False` |
| `supports_usage` | parses token/cost usage from agent output | `False` |
| `supports_streaming` | incremental / interactive session support | `False` |
| `one_shot` | each `send()` is a full prompt→result round trip | `True` |

Built-ins (`tether adapters list` shows these as the CAPABILITIES column):

| Adapter | cancel | tree-kill | usage | streaming | one-shot | maturity |
|---|---|---|---|---|---|---|
| mock | no | no | no | no | yes | verified |
| command | yes | yes | no | opt-in | yes | verified |
| opencode | yes* | yes* | no | opt-in* | yes | verified |
| pi | yes* | yes* | no | opt-in* | yes | experimental |

\* inherited from the generic command plumbing; it is the presets'
*agent-level* behavior that remains unverified. Streaming is **opt-in**: it
means the adapter CAN deliver output chunks to an installed
`stream_callback`, not that sends became interactive (see CommandAdapter
below).

## The conformance harness

`tether adapters conformance <name>` certifies adapter *behavior* rather than
mere existence: it runs a battery of deterministic checks and prints a
per-check report plus an overall verdict. The exit code is 0 only on PASS;
skipped checks are allowed and shown with their reason.

```bash
tether adapters conformance mock      # passes out of the box
tether adapters conformance command   # stub-driven; works without config too
```

Checks:

- `availability` — `is_available()` returns `(bool, str)` and reports a working setup
- `success_completes` — a trivial send reaches `completed`
- `logs_capture_output` — stdout **and** stderr land in `AgentState.logs` (distinct markers)
- `failure_maps_failed` — a forced nonzero-exit run maps to `failed`
- `timeout_fails_and_terminates_tree` — a hanging command times out to `failed` and the spawned process tree dies
- `cancel_terminates_active` — only when `supports_cancel`; cancelling mid-run returns a terminal state and kills the tree
- `spawn_failure_unavailable` — a nonexistent binary maps to `unavailable`
- `runs_in_project_dir` — the command executes with `project_dir` as its working directory

Notes:

- Command-family adapters are driven by throwaway stub executables written to
  a temp directory, so fault injection (failure / timeout / cancel / missing
  binary) is fully deterministic.
- When no command is configured for `command`, the battery certifies the
  generic plumbing via stubs and says so in the check detail.
- The harness (`tether.conformance.run_conformance`) accepts ANY
  `AgentAdapter` instance, so new adapters can self-certify; checks needing
  fault injection a class cannot express are skipped, never silently passed.

## Certification (`tether adapters certify <name>`)

Conformance alone proves behavior against stubs; certification adds a live
probe of the real CLI. One command runs three stages, in order:

1. **Availability** — `is_available()` reports a working setup.
2. **Conformance** — the full battery above must print `Verdict: PASS`.
3. **Live probe** — the exact `tether adapters smoke` behavior (a trivial
   send inside a throwaway directory) against the adapter's REAL configured
   command — never stubs.

It then prints a combined verdict:

- `CERTIFIED (experimental): conformance passed + live probe passed; promote
  candidate once real-mission behavior is demonstrated.` — exit 0.
- `FAILED at <stage>: <reason>` — exit 1, naming availability, conformance,
  or the live probe.

Notes:

- Mock's live probe is its normal send, so `tether adapters certify mock`
  certifies out of the box.
- An unavailable real command fails at the **live-probe stage** with the
  underlying reason (missing binary, unconfigured `command`, ...). When no
  command is configured for generic command plumbing, conformance still
  certifies via stubs, but certification then correctly gates on the live
  probe.
- A configured command that misbehaves fails earlier, at the stage that
  observed the problem (conformance checks drive fault-injection variants,
  while `success_completes` exercises the instance itself).

```bash
tether adapters certify mock      # CERTIFIED out of the box
tether adapters certify opencode  # live probe requires a real `opencode` binary
```

## Reviewer sessions (review gate)

The optional mission review gate opens a fresh session on the **mission's own
adapter instance** and sends an adversarial review prompt over the captured
change; the reviewer is just another `send()` — no extra interface. Setting
`review.adapter` in the contract routes that session through a **different
adapter** (resolved from the same adapters config; its availability is checked
before the run), so independent review needs no core-loop changes — when
unset, the gate remains self-review by the mission adapter. Multi-reviewer
consensus (`review.reviewers`, dogfood-32) consults EVERY named adapter —
each resolved via the registry on its own fresh session — and aggregates the
verdict per `review.consensus` (`"all"` = unanimous, `"majority"` = strictly
more approvals than rejections, ties fail safe); per-reviewer outcomes are
recorded in `report["review"]["reviewers"]`.

## Experimental vs verified (promotion criteria)

`verified` is earned, not declared. An adapter may claim `verified=true` only
when all of the following hold:

1. **Certification**: `tether adapters certify <name>` passes — behavioral
   conformance **plus** a live probe of the real CLI (see above).
2. **Demonstrated real-mission behavior**: at least one recorded Tether
   mission ran end-to-end through the adapter against a real project
   (`completed` agent state, verification passed), showing behavior beyond
   trivial probes and stub executables.
3. **Honest metadata**: `name`, `verified`, and the five capability flags
   match observed behavior.

Until then an adapter stays `experimental`. Passing conformance alone is not
sufficient evidence anymore. pi remains `experimental` today: its command
shape was checked against `--help` output, but neither a certify live probe
nor a real mission has been recorded for it. opencode met all three criteria
on 2026-08-22 and is promoted accordingly (record below).

### Promotion record: opencode verified=true (2026-08-22)

1. **Certification**: `tether adapters certify opencode` PASSED — behavioral
   conformance plus a live probe of the real CLI. Certificate:
   `.tether/certificates/opencode-20260822T142025Z.json`.
2. **Demonstrated real-mission behavior**: mission
   `dogfood-14-real-adapter-and-operational-intelligence` ran end-to-end
   through the real `opencode` CLI against this very repository as Tether
   session `7dd812e7b0e1` (agent completed, verification passed) — this
   document update itself ships in that session's change set.
3. **Metadata**: the earned status is recorded here per the promotion
   criteria. The preset's *static* class tag (`OpencodeAdapter.verified`,
   which `tether adapters list` prints) was flipped to `true` in a direct
   follow-up (2026-08-22) together with the test that pins it, after three
   further end-to-end real missions through the real CLI
   (`dogfood-16-review-gate-live`, `dogfood-17-independent-reviewer-and-routing`,
   `dogfood-18-review-telemetry`) satisfied the promotion criteria.

## MockAdapter (verified)

Fully local and deterministic. Scenarios (via config `adapters.mock.scenario`):

- `success` — always completes
- `fail_then_succeed` — first execution send fails, subsequent sends succeed (planning always succeeds)
- `always_fail` — never succeeds

## CommandAdapter (verified, generic)

The main real-world integration point. Settings:

```yaml
adapters:
  myagent:
    command: ["myagent", "--prompt", "{prompt}"]   # required, list of argv strings
    prompt_via_stdin: false   # true = pipe prompt to stdin instead of {prompt}
    env: {}                   # extra environment variables
    timeout_seconds: 1800
```

Placeholders in any argv part: `{prompt}`, `{project_dir}`, `{session_id}`.

When `prompt_via_stdin`: true, the prompt is piped to the command's stdin and `{prompt}` renders as an **empty string** in argv (so a trailing `"{prompt}"` part stays present but empty). The prompt never appears in the process argv, keeping it out of `ps` output.

### Injected environment variables

Every CommandAdapter child process receives standard Tether context variables
(added automatically; user `env` entries win on conflicts):

- `TETHER_SESSION_ID` — the tether session id
- `TETHER_PROJECT_DIR` — absolute path of the target project
- `TETHER_MISSION` — mission name, when known

Behavior: runs once per `send`, captures stdout/stderr as logs, exit 0 => completed, nonzero => failed, spawn failure => unavailable, timeout => failed. The child is spawned via `subprocess.Popen` with `shell=False`. All of this is covered by tests using stub executables (`tests/test_adapter_harness.py`) — no real agent binaries required. The orchestrator also calls `is_available()` itself before starting a run and fails fast when the adapter is unavailable.

### Opt-in output streaming (dogfood-32)

`CommandAdapter.supports_streaming = True`, but streaming never happens
unless a caller installs a callback on the instance:

```python
adapter.stream_callback = lambda chunk: print(chunk, end="")  # chunk: str
```

With a callback installed, `send()` drains stdout and stderr with background
reader threads and hands every chunk to the callback as it arrives — live
progress without changing the adapter contract. Guarantees:

- The `send(prompt, session)` signature and one-shot semantics are untouched.
- Without a `stream_callback` (the default `None`) behavior is identical to
  before.
- Logs still accumulate the FULL combined output for audit, and
  `usage_patterns` extraction still runs over the complete output.
- Process-tree containment is preserved: timeouts and `cancel()` terminate
  the whole group (SIGTERM then SIGKILL on POSIX), and reader threads end
  when the pipes close.
- Callback exceptions are swallowed — streaming is best-effort observability,
  never a way to fail a send.

Current limitations (accepted-by-design): when the agent leaves a surviving
descendant holding its stdout/stderr, `send()` still returns promptly, but
the daemon reader threads remain blocked on the inherited pipes and the
fds stay open until that descendant exits. This is bounded in practice
because the readers are daemon threads — they can never keep Tether itself
alive — but captured logs for that send will not be complete until the
straggler exits or is killed.

## OpencodeAdapter / PiAdapter — thin CommandAdapter presets

Thin presets over CommandAdapter. The command shapes were checked against the
`--help` output of locally installed CLIs (2026-08):

- opencode: `["opencode", "run", "-m", "opencode/x-preview-f-free", "{prompt}"]`
  (`opencode run [message..]`; the `-m` model pin avoids a server error some
  installations hit on bare `opencode run`)
- pi: `["pi", "--print", "{prompt}"]` (`--print` = non-interactive mode)

End-to-end behavior (provider/model setup, exit codes, session handling) is
**not** exercised by Tether's tests. opencode's real-world behavior has
additionally been demonstrated outside the test suite — certification plus a
full real mission (see the promotion record above); pi remains unverified.
Override the command template in `tether.yaml` if your version differs:

```yaml
adapters:
  opencode:
    command: ["opencode", "run", "--your-actual-flag", "{prompt}"]
```

Both fail cleanly when the binary is missing; the maturity each one reports in
`tether adapters list` reflects its static class tag (see capability table and
promotion record above).

## Adding a new adapter

1. Subclass `AgentAdapter` (or `CommandAdapter` if a command template suffices).
2. Set `name`, `verified` and the five capability flags honestly.
3. Register it: `tether.adapters.register("myname", MyClass)` (or add to `_REGISTRY` in `src/tether/adapters/__init__.py`).
4. Optionally add a preset class in `experimental.py` with documented assumptions.
5. Self-certify: `tether adapters certify myname` must pass (and a real
   mission must demonstrate behavior) before `verified` may be claimed
   (see promotion criteria above).

No changes to the core loop are needed or allowed for agent-specific behavior.
