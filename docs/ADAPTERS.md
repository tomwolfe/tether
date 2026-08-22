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
| command | yes | yes | no | no | yes | verified |
| opencode | yes* | yes* | no | no | yes | experimental |
| pi | yes* | yes* | no | no | yes | experimental |

\* inherited from the generic command plumbing; it is the presets'
*agent-level* behavior that remains unverified.

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

## Experimental vs verified (promotion criteria)

`verified` is earned, not declared. An adapter may claim `verified` only when
all of the following hold:

1. **Conformance**: `tether adapters conformance <name>` prints `Verdict: PASS`.
2. **Demonstrated real CLI behavior**: at least one recorded
   `tether adapters smoke <name>` against an actually installed binary,
   showing a completed trivial run with real exit codes and logs — evidence
   beyond stub executables.
3. **Honest metadata**: `name`, `verified`, and the five capability flags
   match observed behavior.

Until then an adapter stays `experimental`. opencode and pi are intentionally
experimental today: their command shapes were checked against `--help`
output, but end-to-end runs are not exercised by Tether's tests.

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

Behavior: runs once per `send`, captures stdout/stderr as logs, exit 0 => completed, nonzero => failed, spawn failure => unavailable, timeout => failed. Uses `subprocess.run(shell=False)`. All of this is covered by tests using stub executables (`tests/test_adapter_harness.py`) — no real agent binaries required. The orchestrator also calls `is_available()` itself before starting a run and fails fast when the adapter is unavailable.

## OpencodeAdapter / PiAdapter — EXPERIMENTAL

Thin presets over CommandAdapter. The command shapes were checked against the
`--help` output of locally installed CLIs (2026-08):

- opencode: `["opencode", "run", "{prompt}"]` (`opencode run [message..]`)
- pi: `["pi", "--print", "{prompt}"]` (`--print` = non-interactive mode)

End-to-end behavior (provider/model setup, exit codes, session handling) is
**not** exercised by Tether's tests, so both remain marked `experimental`.
Override the command template in `tether.yaml` if your version differs:

```yaml
adapters:
  opencode:
    command: ["opencode", "run", "--your-actual-flag", "{prompt}"]
```

Both report `experimental` maturity in `tether adapters list` and fail cleanly when the binary is missing.

## Adding a new adapter

1. Subclass `AgentAdapter` (or `CommandAdapter` if a command template suffices).
2. Set `name`, `verified` and the five capability flags honestly.
3. Register it: `tether.adapters.register("myname", MyClass)` (or add to `_REGISTRY` in `src/tether/adapters/__init__.py`).
4. Optionally add a preset class in `experimental.py` with documented assumptions.
5. Self-certify: `tether adapters conformance myname` must print PASS before
   `verified` may be claimed (see promotion criteria above).

No changes to the core loop are needed or allowed for agent-specific behavior.
