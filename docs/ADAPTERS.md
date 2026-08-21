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
2. Set `name` and `verified` honestly.
3. Register it: `tether.adapters.register("myname", MyClass)` (or add to `_REGISTRY` in `src/tether/adapters/__init__.py`).
4. Optionally add a preset class in `experimental.py` with documented assumptions.

No changes to the core loop are needed or allowed for agent-specific behavior.
