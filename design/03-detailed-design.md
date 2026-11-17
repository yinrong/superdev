# superdev Detailed Design

This document is written for implementers: for each component it gives the rules, scripts, table schemas, and state transitions at a level you can act on. Terms follow the glossary in the README.

## 1. Red-green discipline (the core of the worker contract)

Every worker step is bound by four rules: start from the green point, research within budget before starting, roll back entirely when broken, and research before switching direction after two failures.

### 1.1 Contract terms (written into `superdev-work.md`, loaded when a worker session starts)

1. **Baseline rule**: every round opens a new branch from the green point of the task's module, named `superdev/<task-id>`.
2. **Definition of green**: the module's full regression suite plus lint pass, judged by the discipline script. A worker's own "I already tested it" does not count.
3. **Red means rollback**: when the regression fails, the discipline script runs `git reset --hard <green point>`; the failure log (truncated to 200 lines) is fed back to the worker as the only new context for the next round.
4. **Two reds turn into research**: after two consecutive reds on the same task, the next round must first pass the research gate (see 1.4) and begin with `strategy change: <one-line new direction>`. The new direction must cite the research findings; pure guesses are rejected by the program and trigger the "stuck" event directly. (Origin: troykelly's research-after-failure — after two consecutive failures, stop guessing and look things up.)
5. **Regression isolation**: when the second red still points at the same failing case, the manager splits that case into a standalone micro-task `repro:<case-id>` that must attach a repro script runnable with one command; the original task is marked "blocked by it".
6. **Evidence return**: after green, commit, push, open a merge request, and return three things: the link, the raw regression output, and this round's diff stats.

### 1.2 Discipline script (shared by all engines, pseudocode)

```python
# red_green_loop.py <task-id> — the outer wrapper around an engine's round
def round(task):
    base = projection.last_green(task.module)
    git(f"checkout -B superdev/{task.id} {base}")

    for attempt in 1..2:
        run_engine_round(task)              # the engine (ZCode/Codex/...) implements
        result = run_regression(task.module)
        log_result(result)                   # write to the projection: regression_events
        if result.green:
            projection.set_last_green(task.module, result.commit)
            return emit("task_evidence", task)
        if attempt == 1:
            git(f"reset --hard {base}")      # roll back entirely, feed the log back
            feed_back(result.log_tail(200))
        else:
            run_research(task)               # post-mortem research (§1.4), budget tiered by the ledger
            if not strategy_cites_research(task):
                return emit("task_stuck", task, reason="strategy change cited no research")
            emit("task_retry_with_strategy", task)

    emit("task_stuck", task, reason="two failures")
```

### 1.3 How the green point is maintained

When a module is onboarded, the full regression runs once, and the passing commit becomes the initial green point. Afterwards it updates in two places: when the gate merges into main, and when the discipline script judges green. The green point is recorded in the ledger `agents/<project>/status.yml` (source of truth); the projection keeps a cache.

### 1.4 Research discipline (pre-work + post-mortem)

**Pre-work research: done before every task starts; the budget is fixed by the dispatcher from the task-size field in the ledger, not left to the AI to size itself.**

| Task size (ledger field) | Research budget | What to research |
|---|---|---|
| trivial (typo, config change) | skip | go straight to implementation |
| small (single-file micro-task) | at most 2 tool calls | read the target file, search for similar implementations |
| medium (feature-level) | 5–10 tool calls | similar implementations, existing patterns, reusable utilities, how to write the tests |
| large (system-level) | separate approval | full recon plus a proof of concept; its appearance raises an alarm — the manager should have split it smaller |

Findings are capped at 30 lines, written into the ledger's `research_notes`, and injected into the worker's opening context when the task is dispatched. New findings after a red are appended, and escalation reports cite them directly. External lookups (API docs and the like) are off by default — private networks enable them as needed.

**Post-mortem research: triggered by the second red; direct retries are no longer allowed.** The budget follows the small/medium tiers above. The research focuses on three things: the failing cases, the previous round's diff, and the directions already tried (assembled from the projection's regression events). Research and the strategy change are one action: the `strategy change` field must cite `research_notes`, or the dispatcher rejects it.

## 2. Hook inventory (assembled for the manager session)

Configuration lives in `agents/<project>/hooks.json`, takes effect with the manager's clone, and never touches the client's global configuration.

| Timing | Match | Action | On failure |
|---|---|---|---|
| Before the AI writes a file | target path outside this project's ledger directory | intercept, with the hint "the manager writes the ledger only" | blocked |
| Before the AI runs a command | command contains `git commit` or `git push` | intercept, with the hint "the manager does not deliver" | blocked |
| After a ledger write | matches `*.yml` | run `superdev.py check <file>` | error messages fed back into the session |
| Before the session ends | — | full check plus projection consistency audit | list the unclosed items and block the wrap-up |

`superdev.py check` validates eight items: task count, grouping, dependency acyclicity, the parallel-ready count, engine and worker membership, responsibility boundaries, whether strategy changes cite research_notes, whether the repro script exists and is executable, and whether the research budget is within the tier table.

## 3. State machine

### 3.1 Task state diagram

```
draft → ready → assigned → in_progress → evidence_wait → gating
                                  │             │
                                  ↓(two reds)   ↓(evidence rejected)
                               stuck ──→ repro_split → ready
                                  │
                                  ↓(manager judges it beyond capability)
                              escalated →(expert decision)→ ready (with a new strategy)
gating →(all green + evidence complete)→ merged → done
any state →(human pause)→ paused →(resume)→ back to the original state
```

### 3.2 Transition trigger table (held by programs; the AI can only propose)

| Event | Transition | Triggered action |
|---|---|---|
| task_created | draft→ready | refresh the projection after the checks pass |
| worker_idle | ready→assigned | the dispatcher matches an engine and opens a branch |
| round_green | in_progress→evidence_wait | collect evidence, notify the gate |
| round_red_1 | no transition | roll back, feed the log back |
| round_red_2 | in_progress→stuck | trigger post-mortem research; escalate if the strategy cites no research |
| repro_split | stuck→ready (new micro-task) | generate the repro script contract |
| expert_decided | escalated→ready | write the new strategy back to the ledger, dispatch |
| all_green | gating→merged | merge into main, sync the ledger back to the module repo, update the green point |
| metrics_stall (30 min) | project-level counter | twice → pause dispatching for the project and escalate |

### 3.3 Daemon duties redefined (from polling doers to reconcilers)

| Daemon | What it did in bigdev | What it does in superdev |
|---|---|---|
| patrol | polled dispatch every 2 minutes | fallback: backfill missed dispatches, watch engine session health |
| intake | collected evidence, merged, synced every 10 minutes | fallback: gate reconciliation, projection-vs-ledger consistency |
| supervisor | compared progress every 30 minutes, escalated after 5 failures | metric comparison kept; escalation threshold 2; stuck events fire immediately |
| resource guard | resource disposal every 60 seconds | kept as is |

## 4. SQLite projection schema

```sql
CREATE TABLE tasks (            -- copy of the ledger's task plan yml
  task_id TEXT PRIMARY KEY, project TEXT, module TEXT,
  status TEXT, engine TEXT, worker TEXT,
  red_count INTEGER DEFAULT 0, strategy_changes INTEGER DEFAULT 0,
  depends_on TEXT, mr_url TEXT, updated_at TEXT
);
CREATE TABLE regression_events (-- written by the discipline script
  task_id TEXT, attempt INTEGER, green INTEGER,
  commit_sha TEXT, log_tail TEXT, created_at TEXT
);
CREATE TABLE decisions (        -- copy of the decision table
  decision_id TEXT PRIMARY KEY, project TEXT, question TEXT,
  context_report TEXT,          -- program-assembled "directions already tried"
  status TEXT,                  -- waiting/decided/escalated
  decided_by TEXT, decided_at TEXT
);
CREATE TABLE events (           -- event bus (with a fired flag)
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, payload TEXT, fired INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE metrics (          -- supervisor metrics (definitions inherited from bigdev)
  project TEXT, ts TEXT, done_count INTEGER,
  worker_hours REAL, cost_by_model TEXT
);
```

There is exactly one write path: programs change the ledger → refresh the projection → insert events → the dispatcher consumes the unfired events.

## 5. Web decision board

Inherits bigdev's web console (127.0.0.1:6010) and adds one decision pipeline:

1. **Awaiting your decision** (pinned at the top of the page): lists all pending decisions. Each opens a structured report assembled by programs — the failing cases, every direction tried (description, diff stats, last 20 lines of the failure log), and the cost spent so far.
2. **One-click decision**: pick a direction (change strategy / split into micro-tasks / switch engine / abandon), optionally with a note. The decision is written to the database, the program writes it back to the ledger in sync, and events trigger dispatch or recovery. No AI participates anywhere.
3. **Remote access**: add token or reverse-proxy authentication; by default it still listens on localhost only.
4. **Feishu push**: when a decision is waiting, send one message carrying a page anchor, so an expert can open it from a phone and decide.

## 6. Supervision rules (upgraded)

- Metric comparison stays at once per 30 minutes, but events take priority: the second time a task gets stuck escalates immediately, without waiting for the cycle.
- Escalation reports carry five elements: the failing cases, the repro commands, the directions already tried (with evidence), the cost spent (broken down by model and thinking effort), and suggested options.
- Two recovery paths: automatic recovery when metrics rebound (inherited), and immediate recovery on an expert decision (event-triggered).

## 7. Migrating from bigdev

- The ledger format is backward compatible: bigdev project directories are read as-is, and green points initialize on first run.
- Engine adapters reuse bigdev's runner system, wrapped in `red_green_loop.py`.
- Hook configuration lands with the manager's clone; the client's global configuration is untouched.
- The `live: false` rollout flag is kept as is, so new superdev projects and existing bigdev projects can coexist on the same platform.

## 8. Landing the absorbed items (competitor strengths, engineered)

This section comes from the absorption list in the competitive analysis (doc 05): each item is a competitor-proven strength, shown here in its concrete superdev form.

### 8.1 spec-kit format compatibility

New ledger import command: `superdev.py import-spec <spec-kit dir>`. spec-kit's plan/tasks files convert directly into the task plan; a plan whose granularity violates the splitting rules (outside the 10–50 micro-task range) is automatically bounced back to the manager for re-splitting. Direction: spec-kit owns writing the spec, superdev owns landing it; the two can be chained.

### 8.2 The gate upgraded to a quality pipeline

The "green" in "merge into main only when all green" is upgraded from a single regression suite to a per-module configurable quality pipeline: `gate: [lint, typecheck, test, build]`, written in the module's ledger config. Any red step in the pipeline equals a regression red and follows the same rollback-and-feed-back flow. Origin: Bernstein's Janitor.

### 8.3 Event log with a tamper-evident chain

Every record in the `events` table carries a chained signature (this record's hash includes the previous record's hash). Anyone can verify offline, with a public command, that the full decision chain "from task creation to merge" has not been altered. Origin: Bernstein's HMAC chained log.

### 8.4 CI integration: automatic feedback after a merge request

Once a merge request is open, the CI adapters (one thin adapter each for GitHub Actions / GitLab CI / Jenkins) watch the pipeline result. On CI failure: the log is fed back to the worker automatically for one round of "roll back to the green point → fix → push again"; a second failure enters the two-reds research flow. Local regressions and remote CI share one failure-handling path.

### 8.5 Kanban view on the decision board

Tasks are laid out by status on a kanban board (to do / in progress / awaiting evidence / stuck / merged); drag-and-drop proposes a state transition (programs still adjudicate). Cloud sync (multi-user collaboration) is on the roadmap, not in the first release.

### 8.6 A requirements-clarification step in the manager loop

Before splitting tasks, the manager runs "requirements clarification": one question at a time, with options; the clarification record goes into the ledger and becomes part of the task's acceptance criteria. Origin: Superpowers' brainstorming skill.

### 8.7 Reviewer worker (second pair of eyes)

A new worker role: the reviewer. After a regular worker opens a merge request, the reviewer walks the diff first (test coverage, naming, obvious defects) and produces a structured review; the review is fed back to the original worker for one fix round before the gate. The reviewer runs a different engine from the manager to avoid same-model blind spots. Origin: Superpowers' code review skill.

### 8.8 Module knowledge base (cross-task memory)

research_notes grows from a per-task note into a module-level knowledge base: each module accumulates "which fixes worked, which pits were hit", distilled automatically by programs from historical regression events and merged tasks. The research phase of a new task consults the knowledge base first — proven fixes inject their conclusions directly, and known pits inject warnings. Origin: troykelly's memory-integration. This is the compound interest of research discipline: research once, the whole project benefits.
