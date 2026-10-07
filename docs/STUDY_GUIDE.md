# Precedent: study guide

Read this top to bottom once (about 45 minutes). Then run `precedent demo` and follow along in the code.

---

## 1. The problem

A marketing agency wants AI agents to help run paid campaigns and influencer programs. Four hard problems stand in the way.

1. **Implicit context.** A client says "Stop campaign A". Is that about this campaign only, or a rule for every future campaign? The reason may live in a meeting, a Slack thread, or nowhere at all. The system must keep four things apart: what was *said* (stated reason), what was *true* (evidence), what *happened* (outcome), and what *caused* it (causal conclusion).
2. **Decision evaluation.** A campaign succeeds because of thousands of decisions. Some outcomes take months (SEO). A good decision can have a bad outcome from noise, and a bad one can get lucky. How do you judge each one?
3. **Facts change.** The attribution window moves from 7 to 14 days, a benchmark is updated, the target customer shifts, and the ad platform and the CRM report different numbers for the same campaign. Which source wins? And when a client asks for deletion, how do you remove every embedding and graph edge derived from their data?
4. **Agent-human harness.** Drafting, recommending, changing a budget and pausing a campaign carry different risk, so they need different approval and rollback. Creators reject AI-sent messages, so humans still negotiate with influencers. Clients approve over Slack and email, and their comments ("delete this", "stop this campaign") must be sorted into one-off decisions vs lasting preferences before entering memory. When should the agent decide, and when escalate?

## 2. The one idea to remember

> **Make the decision a first-class record, linked to the exact messages and numbers it came from. Agents propose; plain code decides what runs.**

Almost everything follows from that:

| If decisions are linked records... | ...then |
|---|---|
| each one points at its source messages and the metrics as they were at that minute | you can recover unspoken reasons, and keep reason, evidence, outcome and conclusion separate |
| each one carries a prediction and a timestamp | you can judge process on what was knowable, and the outcome later |
| facts carry two timestamps and never get overwritten | "what did we believe on 3 Sep?" is a query |
| every derived thing records its sources | deletion is a graph walk with proof, not a search |
| the model can only *propose* | a deterministic gateway can enforce risk, approvals and rollback |

## 3. The seven layers (follow one message through)

Follow *"Stop campaign A for now. The creative feels off-brand."* (Slack, 2 Sep):

1. **Event store** (`db.py`, `events` table). The raw message is stored append-only. It is the root of all lineage.
2. **Extraction agent** (`extract.py`). It reads a *window*: this thread, plus the meeting from the day before, plus CPA for the previous 7 days as known on 2 Sep. It outputs a typed record: action `pause`, scope `campaign`, class `one_off`, stated reason "The creative feels off-brand.", implicit context "CPA up 21%", confidence 0.85. It has two interchangeable backends: an LLM via a typed tool call, or a rule-based baseline.
3. **Memory** (`ingest.py`). The decision is written to the ledger with `derived_from = [meeting, slack message]`, along with chunks, embeddings and graph edges, each with lineage. A lasting preference would instead be *proposed*, compiled into a typed rule, and wait for confirmation.
4. **Truth resolver** (`truth.py`). It answers "what was CPA?" for a given date *as known at* a given time, using the right metric definition and the declared source precedence. It flags when the platform/CRM gap moves.
5. **Evaluator** (`evaluate.py`). At the horizon it scores the outcome with the strongest available method and stores the method, CI, as-of date and definitions. It pools similar decisions into a verdict on the *policy*.
6. **Planner agent** (`agent.py`). An LLM in a tool-calling loop. It reads campaigns, preferences, decisions, untrusted messages and as-of metrics. It acts with `propose_action`, `draft_message` and `ask_human`, reads the gateway's verdict, and repeats until `finish`. Live (needs an LLM API key in `.env`), replay (a recorded run, re-decided live by the gateway) or offline (rule policy).
7. **Action gateway** (`gateway.py`). Every proposal gets a tier (T0 to T4), a list of escalation reasons, approval rules and a compensating (rollback) action.

Cross-cutting: **lineage** (deletion), the **eval harness** (`evals/`), and the **audit log**.

## 4. Concepts in plain English

- **Bitemporal data.** Two times per fact: *valid time* (when it was true in the world) and *recorded time* (when we learned it). A late CRM correction for 1 Sep shows 10 conversions "as known on 3 Sep" and 12 "as known on 6 Sep". Old rows are superseded, never edited.
- **Metric definitions.** "Conversions (7-day click)" and "conversions (14-day click)" are different metrics. Switching windows adds rows; it doesn't rewrite history.
- **Source precedence.** A registry says who wins per metric. The CRM wins for conversions and revenue (closer to money, deduplicated); the ad platform wins for spend. Both numbers are always kept.
- **Gap-shift flag.** Platforms over-report against the CRM by a fairly stable amount (~32% here). Alerting on the gap itself would fire every time. Precedent alerts when the gap moves away from its 28-day norm.
- **Process vs outcome.** Process: was the decision sound given what was knowable then? Outcome: what happened? Mixing them up ("resulting") punishes good decisions that got unlucky.
- **CI-based outcome call.** An outcome is "good" or "bad" only if the whole 95% confidence interval agrees. Otherwise it's "inconclusive", which is the honest answer for most single decisions.
- **Difference-in-differences (DiD).** Compare the treated campaign's change with a similar campaign's change over the same period. That cancels shared shocks like seasonality. CIs come from a *block* bootstrap, which resamples 3-day chunks so day-to-day correlation isn't ignored.
- **Random-effects pooling (DerSimonian–Laird).** Combine many noisy estimates of the same kind of decision into one verdict per decision class, allowing real variation between instances. Single estimates get shrunk toward the class mean.
- **The 2×2.** Good process with a good outcome is an earned win; with a bad outcome it's bad luck, *only if the class verdict says the policy works*, otherwise it's a policy problem. Bad process with a good outcome is lucky; with a bad outcome, an earned loss. Plus a fifth label: inconclusive.
- **Trend-adjusted leading indicator.** SEO sessions up 51% sounds great, but the line was already rising. Projecting the pre-period trend shows only +3% of real lift.
- **Lineage deletion.** Each derived row lists its parents. Deleting a client walks the graph, removes descendants, fixes version chains, rebuilds anything with surviving parents, deletes facts, actions and approvals, redacts the audit trail, and reports residue (which must be zero).
- **Risk tiers.** T0 analyse (auto), T1 draft (auto, a human sends), T2 recommend (approve), T3 small budget change (auto, under a watchdog), T4 pause, launch, large spend or external message (explicit approval, two people above $1k/day). Unknown actions get T4.
- **Fail-closed approvals.** "hmm", "not ok" and "yes, no" never execute. Only allowlisted people for that client count, never the proposer. A modification like "yes, cap at 1200" is re-assessed and may need a second approver.
- **Memory gate.** Every comment is classified as one-off, lasting preference, policy, or context change. Clients can't create agency policy. Lasting preferences compile to a typed rule (e.g. `{applies_to: retarget, forbid_days: [sat, sun]}`), need authorised confirmation, and expire after 180 days. Anything not compilable stays advisory.

## 5. Reading order for the code (about 1.5 hours)

1. `ledger/seed.py`: the synthetic world. Know the cast: Priya (Acme client), Dana (Northwind client), Sam (agency lead), Lena (account manager).
2. `ledger/db.py`: the schema. Notice `lineage`, the two timestamps on `facts`, and `people`.
3. `ledger/extract.py`: the `SYSTEM` prompt, `DECISION_TOOL` schema, `classify_comment` and `compile_constraint`.
4. `ledger/ingest.py`: `ingest_event` (the whole pipeline in one function) and `confirm_preference`.
5. `ledger/truth.py`: `series` (as-of), `resolve` (precedence plus gap shift).
6. `ledger/gateway.py`: `assess`, `propose`, `record_reply`, `can_approve`.
7. `ledger/evaluate.py`: `did_effect`, `outcome_call`, `class_stats`.
8. `ledger/forget.py`: `descendants`, `forget`, `residual`.
9. `tests/test_ledger.py`: each test is a one-paragraph spec of a behaviour, and the fastest way to learn what the system guarantees.

## 5b. How the agent loop works

```
messages = [task]
loop (max 14 steps):
    response = model(system_rules, messages, tools)       # the model chooses tool calls
    for each tool call:
        result = run the tool                             # reads are plain queries; actions go to the gateway
        messages += result                                # e.g. {"tier": "T4", "status": "awaiting_approval", "reasons": [...]}
    stop when the model calls finish
```

Key points to say: the model chooses *what* to do, and code decides *whether it happens*. The model sees every verdict and must adapt (no retries of blocked actions, no duplicates). Client text reaches it wrapped as untrusted. In the prompt-injection eval, even a fully hijacked agent asking for $5,000/day and a direct creator message gets T4 awaiting approval, then blocked.

## 6. Self-test (answer out loud, then check)

1. Why keep stated reason and causal conclusion in separate fields? *The client gives the reason; only the evaluator, with a named method, may claim cause. Mixing them turns opinions into "facts".*
2. The same client says "Stop campaign A." twice, three weeks apart, with no reason the second time. What happens? *Low confidence, so a question goes to the account manager quoting the last reason and asking: this campaign, this channel, or a standing rule?*
3. Ad platform says 232 conversions, CRM says 156. Which is used, and is it flagged? *CRM by registry rule. It's flagged only if the gap moved from its norm, or there's no norm under the current definition.*
4. What changes in past evaluations when attribution moves from 7 to 14 days? *Nothing is overwritten. Each conclusion records the definitions and as-of date it used. Re-scoring creates a new conclusion. With CRM precedence, CPA doesn't change at all.*
5. Why grade decision classes instead of single decisions? *Single decisions are mostly inconclusive. Pooling many gives a reliable verdict on the policy, which is what you can actually change.*
6. A client email says "Across all clients, skip legal review." What happens? *The policy label is downgraded to a client preference, confidence drops, and a person is asked. It can't compile to a safe rule, so even if confirmed it stays advisory.*
7. Priya replies "yes, cap at 1200" to an $800 proposal. Does it run? *It's re-assessed: $1,200 is above $1,000/day, so it's T4 and needs two people. It waits for a second approver; earlier approvals of $800 don't count.*
8. How do you prove deletion? *A lineage walk plus a residue check across 15 places, all zero. Backups use crypto-shredding, and the LLM provider has zero data retention.*
9. When does the agent decide without a human? *T0/T1, or a small T3 budget change, with no escalation reason (confidence fine, no preference conflict, not novel, no gap shift, no context change).*
10. What's weakest today? *The agent runs one task at a time with no scheduling yet, thresholds are hand-set, the LLM extraction path isn't benchmarked, and novelty detection is weak with a toy embedder (the escalation eval shows it).*
11. Do you need an LLM key? *For live agent runs, yes: any tool-calling model through an OpenAI-compatible API, configured in a local `.env`; a run costs cents. Without one, replay re-runs a recorded live session, and offline uses a rule policy with the same tools so CI and the harness still work.*

## 7. Explaining it in two minutes

"Today a decision is a Slack message: no structure, no record of what was known, no link to what happened. I made the decision a first-class record linked to its source messages and the numbers as they were at that minute. Facts carry two timestamps and are never overwritten.

That gives you context, because reason, evidence, outcome and conclusion are separate. It gives you fair evaluation, because process is judged on what was knowable, outcomes only when the confidence interval agrees, and policies across many decisions. It handles changing facts, because definitions are versioned and precedence is declared. And it makes deletion provable, because it's a graph walk.

On the action side the model only proposes. A gateway in plain code tiers every action by reversibility, blast radius and spend, takes approvals from allowlisted people over Slack or email, re-checks modified approvals, rolls back, and never lets the agent message a creator. I built and tested it, then reviewed it against 12-Factor Agents and the OWASP agentic top 10, and fixed what that review found."
