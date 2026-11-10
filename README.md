# Focus

One thing to work on now, picked from what Hermes already knows, and guarded while you do it.

Hermes accumulates open loops: Kanban cards blocked on you, chats where the agent asked a question and you never answered, cron jobs failing quietly, things you said you would do by Friday. Focus reads all of it, picks one item with rules you can read, leases it for 90 minutes, and keeps the agent pointed at it.

```
PROPOSED  Approve the pricing page copy
  next   Answer the blocker so the task can move
  why    someone is waiting on you: Kanban t_a1 is blocked (needs_input); priority 150; unblocks 2 other task(s)
  open   hermes kanban show t_a1

then:
  2. Fix flaky checkout test  [someone is waiting on you]
  3. Fix failing cron: backup to S3  [automation is failing]
  4. Migrate billing webhooks to v2  [work in flight]
```

## What it does that a prompt cannot

- **Reads real state.** Open Kanban tasks (and which ones unblock others), interactive chats whose last agent message asks you something, cron jobs with a failure streak, and commitments you record. Machine sessions (cron, subagents, workers) are ignored.
- **Decides deterministically.** Hard gates first: due within 24 h, then someone waiting on you, then failing automation, then work in flight, then backlog. A weighted score only breaks ties inside a gate. Same state, same answer. No model call.
- **Holds a lease.** Once you accept, routine re-ranking cannot replace the focus. Only something strictly more urgent (a deadline under 24 h, or someone waiting on you when your focus is lower) is offered as an interruption, and you decide.
- **Guards against drift.** A `pre_llm_call` hook notices when a turn has nothing to do with the leased focus and adds one line of context for the agent: help, keep it short, offer to return. At most once per 20 minutes per chat. Never blocks. Off for cron, subagents and other unattended runs.
- **Closes itself.** When the focused Kanban task completes, the `kanban_task_completed` hook ends the lease with the worker's summary as proof.
- **Asks for proof.** `done` needs evidence (a test result, a link, a sent message). `/now review` shows what you finished, how long it took, and how many turns stayed on focus.

## Use

| Surface | How |
|---|---|
| Chat | `/now`, `/now accept [n] [min]`, `/now done <proof>`, `/now defer [n] [hours]`, `/now add <what> [by <when>]`, `/now review` |
| Agent | tools `focus_now`, `focus_accept`, `focus_done`, `focus_defer`, `focus_add` (the agent only accepts after you agree) |
| Terminal | `hermes focus`, `hermes focus accept 2`, `hermes focus --json` |
| Desktop | Focus pane, status-bar chip with lease minutes, Focus page, palette command |

Deadlines: `+3h`, `+2d`, `today`, `tomorrow`, `2026-11-11`, `2026-11-11 17:00`.

## Install

```bash
hermes plugins install acadine-intelligence/hermes-focus
hermes plugins enable focus
```

Desktop: Capabilities → Plugins → enable the Focus desktop half.

Settings (`plugins.entries.focus.settings` in `config.yaml`): `lease_minutes` (90), `session_window_days` (3), `drift_guard` (true).

## Disclosure

- **Reads outside its own data, read-only:** the active profile's `state.db` (session titles, the last user/assistant message of recent interactive sessions), `kanban.db` (open tasks and links), and `cron/jobs.json` (job names, status, last error). SQLite files open in read-only mode.
- **Writes only** `<HERMES_HOME>/plugin-data/focus/` (`focus.json`, a lock file).
- **Injects context:** a system prompt section naming the leased focus, and the one-line drift note described above.
- **No network calls, no LLM calls, no shell commands, no background processes, no telemetry, no credentials.**

## Develop

```bash
uv venv -p 3.11 .venv && uv pip install -p .venv/bin/python pytest
.venv/bin/python -m pytest
hermes plugins validate . && hermes plugins doctor --ci .
python3 demo/seed.py /tmp/focus-demo   # fictional Hermes home for demos
```

`hermes_focus/` is the engine (stdlib only). `__init__.py` wires it into Hermes. `dashboard/plugin_api.py` serves the Desktop pane. `desktop/plugin.js` is the pane.

MIT licence.
