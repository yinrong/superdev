# bigdev Field Problems and superdev Fixes

bigdev already runs in production (several projects are live), and the skeleton — "AI only at the two ends, programs guarding the middle" — is proven viable. But usage exposed five problems; this document covers each one: the symptom, the root cause, superdev's fix, and the fix's industry origin.

---

## P1 Regressions never converge (the core pain)

**Symptom**: the AI's programs fail the full regression. Two manifestations: repeated failures with zero progress, or a permanent deadlock nothing solves.

**Two root causes, compounding each other.**
First, workers keep editing broken code: the previous round's damage is never rolled back, changes stack on a failed state, and every edit drifts further.
Second, retries are unconstrained: the same idea gets reused, so failure number five carries as little information as failure number one — pure token burn.

**superdev fix** (details in the detailed design §1, §1.4):
- Red-green discipline: start from the green point, roll back entirely on red, feed the failure log back.
- Research discipline brackets the loop: research before starting, sized by the task (budget set by programs); after the second red, research is forced before any direction change, and the new direction must cite the findings.
- Regression failures are split into micro-tasks with repro scripts instead of snowballing inside the original task.
- The boundary is acknowledged: when a task exceeds model capability or the requirement itself is contradictory, no technical design conjures an answer — you can only detect early and escalate early. Cutting the supervision threshold from 5 to 2 serves exactly this purpose.

**Industry origin**: Aider's "auto-run tests + commit per edit + one-key rollback" (red-green discipline); the troykelly skill set's pre-work-research (size-tiered pre-work research) and research-after-failure (research after two consecutive failures); Kiro's recon phase (survey before acting); Superpowers' TDD gate (test-first as a per-task checkpoint). All discipline-level transplants, not whole-tool adoptions.

---

## P2 The manager AI breaks the rules and burns tokens

**Symptom**: the manager is an AI session that skips steps, writes the wrong ledger format, and implements out of bounds; every loop round re-reads the entire ledger and idles even with nothing to do, so token cost stays high.

**Root cause**: the rules live in prompts, so compliance is a probability event; state is re-derived from files every time, so it reads more than it does.

**superdev fix** (details in the detailed design §2):
- Intercept before file writes: out-of-bounds writes and commits are blocked outright — violations move from probability events to impossible events.
- Checks run automatically on every ledger write; errors are fed back into the session, with no reliance on "remembering".
- State moves into the SQLite projection; the manager reads summaries instead of full files and is woken only when something changes.
- The unavoidable part is acknowledged: task splitting, dependency analysis, and acceptance judgment are necessary cognitive costs, not waste.

**Industry origin**: Bernstein's "scheduling without AI", pushed into the manager's behavior layer — AI proposes, programs adjudicate.

---

## P3 Polling latency, decisions scattered across Feishu chats

**Symptom**: patrol every 2 minutes, intake every 10, the manager loop every 15, supervision every 30 — every state advance waits half a cycle on average. Supervision reports go to Feishu, the expert replies in chat, and the decision then waits for a human to write it back to the ledger; the whole chain is measured in hours.

**Root cause**: the daemons are timer-driven, and decisions are recorded in a chat tool — never persisted, triggering nothing.

**superdev fix** (details in the detailed design §3, §5):
- Every state change triggers the next step on the spot (dispatch, recovery, gate); timers are demoted to reconciliation.
- Decisions follow the one-click web flow: read the report → decide → persist → trigger within seconds; Feishu only carries the link.

**Industry origin**: this project's own deduction — the real value of a database is not fast queries but event triggers; bigdev's web page already had "N decisions awaiting you", and the missing piece was persist-and-trigger.

---

## P4 Supervision escalates too late, and reports carry too little

**Symptom**: it takes 5 failures to pause a project and call an expert, with no strategy constraint between retries. By escalation time, piles of tokens and worker-hours are already burnt.

**Root cause**: a conservative threshold (fear of false kills), plus no distinction between "failing with progress" and "spinning in place".

**superdev fix** (details in the detailed design §6):
- The threshold drops from 5 to 2, triggered instantly by the "stuck" event instead of the 30-minute cycle.
- The second failure forces a "strategy change description" — turning "spinning in place" from permitted behavior into a violation.
- Escalation reports are structured with five elements: the failing cases, the repro commands, the evidence for every direction tried, the cost, and suggested options.
- Automatic recovery stays (metrics rebounding resumes the work), which is what makes a false kill cheap enough to dare lowering the threshold.

**Industry origin**: bigdev's own supervision mechanism with parameters tightened and made event-driven. There is no external counterpart — this part is bigdev-unique, strengthened by superdev.

---

## P5 Environment coupling, fragile operations

**Symptom**: in bigdev's own words: the ZCode desktop app must stay open and logged in, and the Mac must not sleep. Manager and workers are tied to specific clients, and a health check means "recover on the spot with a command".

**Root cause**: engines are hard-wired to session lifecycles, so one stuck client halts the whole pipeline.

**superdev fix** (see the architecture doc's core components):
- Engine adapters are thin, and the discipline script is decoupled from the engine, so any engine can be wrapped.
- Heartbeats become events: an engine going silent is an event that switches to the backup engine automatically (the existing "ZCode quota exhausted → switch to Codex" practice, institutionalized).
- The resource guard is kept as is: kill above 8G, clean up orphans, stop opening new sessions under overload.

**Industry origin**: bigdev's existing multi-engine runner system plus its established degradation practice, institutionalized.

---

## Summary

| Problem | Root cause | superdev mechanism | Origin |
|---|---|---|---|
| P1 regressions never converge | changes stacked on broken code + blind retries | red-green discipline + pre-work research budget + forced research after two failures + micro-task isolation | Aider; two troykelly skills; Kiro recon; Superpowers TDD gate |
| P2 rule violations + token waste | rules by self-restraint + read amplification | hard hook gates + projection summaries | Bernstein philosophy |
| P3 cycle latency + scattered decisions | timer-driven + decisions never persisted | event state machine + decision board | this project's deduction |
| P4 late escalation, thin reports | conservative threshold, no strategy constraint | escalate on two failures + structured reports | bigdev mechanism tightened |
| P5 engine coupling | hard-wired session lifecycles | thin adapters + event-driven degradation | bigdev practice institutionalized |

Of the five problems, P1 is the main throughput bottleneck; P3 and P4 are coordination latency; P2 and P5 are reliability and cost. superdev's three new weapons — red-green discipline, hook adjudication, and the event state machine — map onto them one to one. Nothing was changed for change's sake.
