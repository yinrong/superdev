# superdev Architecture

The system splits top-down into four layers: a web page where humans make the calls; a manager AI that produces ideas plus a program layer that enforces the rules; worker AIs that do the work; and a gate that accepts and merges. This document explains who owns what in each part, how data flows, and why the cuts land where they do.

## Overall architecture diagram

```
┌───────────────────────────────────────────────────────────────┐
│  Humans (final decisions, expert fallback)                    │
│    Web decision board (one click → persist → trigger)         │
│    Feishu (link notifications only)                           │
└───────────┬───────────────────────────────────────────────────┘
            │
┌───────────▼───────────────────────────────────────────────────┐
│  Manager session (one per project, AI) — ideas only           │
│    split micro-tasks / dependency analysis / acceptance       │
│    judgments / reassignment decisions                         │
│    Hook interception: ledger writes only; out-of-bounds       │
│    code writes and commits are blocked outright               │
│    Contract checks run automatically on every ledger          │
│    write; failures bounce back                                │
└───────────┬───────────────────────────────────────────────────┘
            │ Proposals (ledger write = single source of truth)
┌───────────────────────────────────────────────────────────────┐
│  Adjudication layer (pure programs, no model calls)           │
│    contract validation · state machine · event bus ·          │
│    dispatcher · supervisor · resource guard                   │
│    Ledger files (source of truth) ⇄ SQLite projection         │
│    (query copy)                                               │
└──────┬────────────────────────┬───────────────────────────────┘
       │                        │ events trigger:
       │ events trigger         │  recovery / merge /
       │ dispatch               │  escalation
┌──────▼────────────────────────────────────────────────────────┐
│  Worker layer (ZCode / Codex / Kimi / Claude — 4 engines,     │
│    one contract)                                              │
│    Red-green discipline: start at a green point → roll        │
│    back on red → feed logs back → research after two          │
│    failures                                                   │
│    implement / test / commit / open merge requests /          │
│    return raw evidence                                        │
└──────┬────────────────────────────────────────────────────────┘
       │ merge request + raw output + regression results
┌──────▼────────────────────────────────────────────────────────┐
│  Gate: merge into main only when all green; the ledger        │
│  syncs back into each module repo                             │
└───────────────────────────────────────────────────────────────┘
```

## Six-layer model: what bigdev kept, what superdev changed

**Ideology layer: unchanged, stated more bluntly.** bigdev's "the manager only thinks, workers only do" stays. One general principle is added: AI proposes, programs adjudicate — the AI produces the ideas, the programs make the call.

**Organization layer: unchanged, but the manager is watched.** The hierarchy is still human → manager → engine → worker. Every manager tool call is checked by a hook.

**Execution layer: timers become events.** bigdev advanced by polling at 2/10/15/30 minutes; superdev triggers on every state change, and polling is demoted to a reconciliation fallback.

**Gate layer: unchanged, but the checks run automatically.** Merge to main only when all green, acceptance judged on evidence — both stay. Contract checks move from "the manager remembers to run them" to "hooks run them".

**Interface layer: the web page is promoted to a decision board, Feishu demoted to notifications.** Decisions happen with one click on the web page and trigger the moment they persist; Feishu only delivers reminders with links.

**Convergence layer: entirely new.** The predecessor had no such layer, and workers spun on broken code. superdev adds four things: a pre-work research budget, red-green discipline, forced research after two failures, and regression problems split into micro-tasks with repro scripts.

## Core components

### Ledger (single source of truth)

Tasks, decisions, and status live in files under `agents/<project>/`, versioned by git. The ledger doubles as the audit record and can be synced into each module repo. The database never replaces it.

### SQLite projection (query copy)

Programs organize the ledger into four database tables (tasks, regression events, decisions, metrics); the web page and the dispatcher read only those. There is exactly one write path: programs change the ledger first, then refresh the projection.

### State machine + event bus

The lifecycle states of tasks and decisions are held by programs; the AI can only propose transitions. The moment a transition persists, the program triggers the corresponding action on the spot (dispatch, recovery supervision, merge check) instead of waiting for a cycle. The four daemons stay, with their duties changed to reconciliation: fix any inconsistency between the projection and the ledger, and replay any missed event.

### Hooks (manager constraints)

| Timing | Rule |
|---|---|
| Before the AI writes a file | Target outside this project's ledger directory → intercept |
| Before the AI runs git commit / git push | Inside a manager session → intercept (the manager does not deliver) |
| After a ledger file is written | Run `superdev.py check` automatically; failure messages are fed back into the session |
| Before the session ends | Full check + projection consistency audit |

### Worker red-green discipline engine

Each engine (ZCode/Codex/Kimi/Claude) gets one thin adapter sharing a single discipline script: run the regression, roll back to the green point on red, feed failure logs back, switch to research after two failures. Details in the detailed design §1.

### Reviewer worker (second pair of eyes)

After a regular worker opens a merge request, a reviewer worker walks the diff first, produces a structured review fed back for fixes, and only then does the request enter the gate. The reviewer runs a different engine from the manager to avoid same-model blind spots.

### CI adapter

Once a merge request is open, the adapter watches the remote pipeline (GitHub Actions / GitLab CI / Jenkins); failure logs follow the same "roll back to the green point → feed back" path as local regressions.

### Module knowledge base

Each module accumulates "which fixes worked, which pits were hit", distilled automatically by programs from history. The research phase of a new task consults it first: proven fixes inject their conclusions directly, and known pits inject warnings.

### Web decision board

Inherits bigdev's web console (127.0.0.1:6010); "awaiting your decision" is upgraded into a one-click decision flow: read the structured report → decide → persist → trigger within seconds. Remote access adds authentication.

## Data flow: the full journey of one task

```
The manager splits micro-task T and writes it into the ledger
(checks pass automatically)
  → the program refreshes the projection → event: task created
  → the dispatcher matches T with an engine that has capacity
  → the worker opens a new branch from T's module green point
  → the implementation round runs the regression:
      pass → commit, open a merge request, return evidence
           → event: evidence ready
      fail → roll back entirely to the green point; the failure
           log is fed to the worker (at most twice)
      second failure → research first; the new direction must
           cite the research findings
      still no direction after research → event: stuck → the
           supervisor splits a repro micro-task or escalates
  → gate: all green and evidence complete → merge into main →
    the ledger syncs back to the module repo
  → the projection refreshes → the completion rate updates on
    the web page
```

## Six key architecture decisions

| Decision | Choice | Reason |
|---|---|---|
| Files or a database | Files are the source of truth; the database is only a copy | Preserves the three lifelines: git history, audit, syncing into module repos |
| Polling or events | Events lead, polling reconciles | Compresses the 2–30 minute cycle latency to seconds |
| Where the rules live | Hooks plus check programs, not prompts | Relying on the AI's self-restraint makes violations a probability event |
| Feishu or the web | The web is the decision board; Feishu only pushes | A decision must persist and trigger; people still live in Feishu, so pushes stay |
| Build or assemble | Discipline is home-built; engine-level parts are swappable | Everything needed is discipline-level; assembling a whole orchestrator creates two-headed command |
| Supervision threshold | 2 (bigdev used 5) | A second retry adds no new information; escalate early and force a direction change |

## Deployment shape (inherited from bigdev)

- One manager session per project, an independent clone, opened manually by a human.
- Worker background tasks are installed once; all projects are picked up automatically; the total is capped by `max_parallel`.
- The resource guard cycles every 60 seconds: kill any single process above 8G, kill the largest process when free memory drops below 10%, clean up orphan sessions, and stop opening new sessions under overload.
