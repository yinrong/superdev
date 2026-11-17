# superdev Selling Points

superdev is a system that lets an AI team deliver software automatically: AI splits the tasks and writes the code; programs enforce the rules, run the flow, and close out the work. One-line positioning: **other tools assign work to AI agents; superdev makes the work land** — it is the execution-reliability layer that fixes AI work failures. Its predecessor bigdev already runs several projects in production; superdev keeps that skeleton and reworks five pain points, each mapped to one concrete mechanism.

## Core selling points

### Selling point 1: rules are enforced by programs — the AI cannot break them even if it wants to

bigdev's rules live in prompts and rely on the manager AI's self-restraint; in practice it skips steps and implements code out of bounds. superdev intercepts with hooks: out-of-bounds code writes are blocked outright, and a ledger written in the wrong format is bounced immediately. Rules move from "probably obeyed" to "impossible to violate".

### Selling point 2: break it and it rolls back; two failures force research first

bigdev's most painful scenario is a worker stacking changes on broken code: the previous round's damage is never rolled back, every edit drifts further, and the same idea gets retried five times with zero progress. superdev fixes three rules per worker: start from the green point; roll back entirely when tests fail; after two consecutive failures, research before switching direction, and the new direction must cite the research findings. The token economics live here: failed rounds are what burn money; research itself does not.

### Selling point 3: the flow does not wait for timers — events advance the moment they happen

bigdev advances on four timers (2/10/15/30 minutes); every action waits half a cycle on average, and human decisions wait hours. superdev puts state into the database, and every state change triggers the next step on the spot (dispatch, recovery, merge checks); timers are demoted to a reconciliation fallback.

### Selling point 4: failures escalate faster, and reports carry more

bigdev needs 5 failures before escalating to an expert, with no constraint between retries. superdev escalates after two. The report carries four things: the failing cases, the repro commands, every direction already tried with its evidence, and the cost spent so far. A wrong escalation is cheap (metrics rebounding auto-resume the work), so the threshold can be cut to 2.

### Selling point 5: it develops itself through the same pipeline

superdev's own code changes are delivered by its own pipeline; every step of self-modification is watched by its own discipline and supervision. The "program written by itself cannot pass its own tests" dead loop is exactly what its own structure catches.

### Selling point 6: four interchangeable execution engines; runs in private networks

The worker engine is not locked to one vendor: ZCode, Codex, Kimi, and Claude all work against the same contract. Private git, Feishu, on-premise deployment — GitHub is never assumed. Feishu only delivers notification links; real decisions happen on the project's own web page.

### Selling point 7: strengths proven by competitors are all absorbed or made compatible

The strongest capabilities of every competitor are in the design: spec-kit plan files import directly (it owns writing specs, we own execution); Bernstein's pre-merge quality pipeline and tamper-evident audit chain; Agent Orchestrator's CI-failure feedback loop; Vibe Kanban's kanban view; Superpowers' requirements clarification and AI review; troykelly's cross-task memory (a fix that worked in one repo is reused across the whole project). Contested dimensions reach parity; vacant dimensions stay exclusive — "surpassing" is then a fact across all dimensions, not positioning talk.

## Who it fits

| Scenario | Example |
|---|---|
| One product split into multiple repos | a 7-repo product: seven repos managed together |
| One repo, many parallel sessions | one repo, parallel sessions: `max_workers` caps the parallelism |
| One refactor spanning multiple projects | a cross-project docker migration: the to-dos fan out into each project |
| Regression-heavy engineering that fears breakage | the home ground of red-green discipline |

## Why not just assemble existing projects

**→ The convergence discipline comes from Aider: copy the rules, do not install the tool.** Aider (~30k★, a pair-programming tool) already has the discipline we want: run tests after every change, roll back with one key at any time. But installing the whole tool into the pipeline would fight the division of labor where workers own their own commits, and the useful part ports over in a 50-line script.

**→ The research discipline comes from the troykelly skill set; likewise, rules only.** Two rules go straight into the contract: research before starting, sized by the task; after two consecutive failures, stop guessing and look things up. The original skill posts its findings to a GitHub issue; we write them into the ledger instead.

**→ The spec-first route aligns with spec-kit; borrow the vocabulary only.** spec-kit (GitHub official, ~120k★) proves that "write the spec first, fresh context per task" is already industry consensus. superdev's ledger plus a new branch per round is the same thing; the template vocabulary aligns with it, but its toolchain is not adopted.

**→ Bernstein's philosophy is absorbed; its orchestrator body is not assembled.** Its "scheduling without AI" idea is pushed by superdev into the manager's behavior layer (hook interception). But assembling the whole thing would amount to junking our own guard programs and creating a two-headed command.

**→ Agent Orchestrator, same story.** Its "feed CI failure logs back and retry" is valuable, but it ships its own supervision loop, which would conflict once assembled.

**→ Vibe Kanban has the most stars (28.7k) but has sunset.** Bloop shut it down and handed it to community maintenance, and it assumes the GitHub ecosystem — not something to bet on.

One principle summarizes it: engine-level hard problems (edit engine, diff tolerance) can be swapped in as parts later; discipline-level mechanisms are all written into our own contract. Everything superdev needs is discipline-level.
