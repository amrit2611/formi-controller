# Outcome-First Agent Loop — Submission

**Name:** [Amrit Dhandharia] · **Date:** 11 November 2026 · **Hours actually spent (be honest):** [10] · **Language / storage:** Python 3, httpx, SQLite (standard library). No dependencies beyond requirements.txt.

---

## 0. TL;DR
- An hourly loop ingests every event exactly once into SQLite (history first) and runs four deterministic "brains": hallucination detection, a validated prompt fix, caller-ID health, and voice experiments with a feasibility check. Every decision lands in an append-only audit table.
- **public_01:** 3 changes applied, all 3 graded beneficial, 0 harmful. Conclusion rate +1.8% vs baseline.
- **public_02:** 9 changes applied (4 beneficial, 5 neutral, 0 harmful). Conclusion rate +10.8% overall and +32.9% on ks_cancel. The flagged caller ID was rotated out the same day. The supervisor's bad prompt was replaced through an approval in 210 minutes, for 5 credits.
- **Crash-safe:** SIGKILL mid-run, restart, correct finish, nothing applied twice (`reports/crash-test.log`). **Load:** 13.6 to 14.2 s wall per simulated day on public_03 (bar: 600 s), about 60k events/s.
- **Cut:** call-time optimisation (C4), multi-cohort targeting (C2), the supervisor view (C11) and cross-tenant fairness (C15). Section 2 says why.

## 1. Assumptions
1. The HTTP API is the only source of truth. The controller never reads `sim/`, `runs/` or scenario files.
2. A 60-minute advance step is fine-grained enough. Every signal I act on needs a few hours of calls to become statistically visible anyway.
3. Pre-D0 history ran on the business default config, so it is a fair baseline for "normal" hallucination rates and for experiment feasibility.
4. The hallucination flag is noisy (false positives and negatives). Only a population difference counts, never a single call.
5. Validation measures safety, not engagement. So a prompt with real call history that passes validation is preferred over an untested LLM suggestion.
6. Client wishes that are stricter than the signed policy are honoured, because they cost nothing: Kestrel gets at most 2 requests per 24h and none before 11:00 (memo 04).
7. Caller-ID rotation is an operational setting (Compliance memo, renewal and Kestrel policies), so I auto-apply it where the policy allows. Anything a customer hears goes through approval.
8. SQLite is fine for the assignment. In production the same tables move to the Postgres that Platform already runs.
9. If a supervisor edits the config after I propose, the supervisor's version wins: I log the 409 and never force mine.

## 2. Scope decisions
| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion | Built | Exactly-once (cursor and counts in one transaction), restartable, measured at about 60k events/s on public_03. | Batch inserts and sharding for 10x (section 8). |
| C2 | Shared cohort object | Partial | Everything uses the `default` cohort, with one definition everywhere. Splitting traffic further would make every experiment slower (see C6 feasibility). | A cohort table referenced by detection, experiments and the view. |
| C3 | Bootstrapping from history | Partial | History is ingested first. It is the hallucination baseline and the input to experiment feasibility. It is not used as a prior for arms. | History priors per arm. |
| C4 | Call-time optimisation | Cut | Needs the most data and the most careful exploration. A wrong hour table silently wastes capacity, because omitted hours get weight 0. | Per-cohort hour tables from history, then per-person preferred hours for returning people. |
| C5 | Caller-ID health | Built | Pickup per live DID vs the other live DIDs, same day. Rotation is auto-applied in max_shift steps where allowed, otherwise proposed. | Re-test rotated-out DIDs later (they stay flagged for the run). |
| C6 | Variant experiments | Partial | Voice only, default cohort. Bonferroni across arms, minimum sample per arm, pickup guardrail, and a feasibility check that skips tests that can't finish within 5 days. | Welcome message as a second, sequential dimension. |
| C7 | Promotion, decay, rollback | Partial | A winner is promoted to 90% with a 10% holdout of the old control, and losers are dropped. The holdout makes decay measurable, but there is no automatic decay rollback yet. | A trailing-window test of winner vs holdout, then a rollback proposal. |
| C8 | Hallucination incident response | Built | Detect (z ≥ 3.0, 150+ connected calls) → candidates (earlier prompts with history first, LLM only if none) → validate → propose with `validation_run_id` → apply. | Automatic post-apply engagement check (section 13). |
| C9 | Governance client | Built | Write-ahead proposals, deterministic ids, reconcile after crash, approver hours, rolling 24h limits, one open proposal per workflow, stale/lapsed/expired paths, auto-apply only within policy. | Batching several dims into one request. |
| C10 | Impact, affected leads, dry run | Partial | The dry run carries the real evidence (z-scores, sample sizes, validation results). Impact is the total-variation size of the change, and affected leads is the daily batch size: honest upper bounds, not forecasts. | Replay against historical cohorts. |
| C11 | Supervisor view | Cut | The audit query (section 10) is the interim single place to look. | A read-only endpoint over the same tables. |
| C12 | Audit store | Built | Every observed config change, approval decision, incident, validation, proposal, auto-apply, apply and skip is a row in `decisions`. | Indexes and retention at scale. |
| C13 | Crash recovery | Built | `scripts/crash_test.sh` and `reports/crash-test.log`. `run.sh` keeps one state db per scenario, so restarting it resumes. | Random-point kill tests in CI. |
| C14 | Loop observability | Partial | A daily `loop_health` row: events, wall seconds, events/s, credits, open proposals. No alerting. | Metrics export and the pages in section 10. |
| C15 | Fair credits and attention across tenants | Cut | Only a global reserve (never spend below 20 credits) and per-workflow request limits. | Per-tenant budgets. |

**The principle:** constraints and explainability first, then the capabilities where doing nothing clearly hurts customers (bad caller ID, a hallucinating prompt), then lift. A false winner hurts a cohort, while a missing experiment only leaves upside unclaimed. So every brain has a minimum sample, and the experiment brain refuses to start a test it can't finish.

## 3. Stakeholders

### 01 Sales (Nikhil Rao)
- **Accepted:** a lift number per workflow on its primary KPI, shown next to its guardrail. On public_02, ks_cancel's conclusion rate went up 28%, and the fix was explainable.
- **Declined:** a blended score (it breaks the rule that pickup and engagement stay separate); "live for everyone by evening" (minimum samples come first); rolling the persuasive prompt out wherever it exists.
- **Wrong claims:** Kestrel's auto-apply is not "for everything": `ks_cancel` allows schedule, caller_id, welcome_message and voice only, with max_shift 0.2, and never prompt or cohorts. Northwind KYC has auto-apply disabled. Another account's demo is not evidence: prompt changes need a validation run on this workflow, and Product says winners stay in their workflow.
- **Reply:**  "Nikhil, I can give you one honest lift number per workflow on its main KPI, with the guardrail beside it. On the incident scenario that was +28% conclusions on Kestrel, and every change has a reason a compliance head can read. I won't blend the KPIs into one score, because it hides the cases where we win one and lose the other. Two corrections: Kestrel's auto-apply covers operational and small voice/opening-line moves only, never scripts, and Northwind KYC has auto-apply switched off. The persuasive prompt needs its own validation on each workflow before it goes anywhere."

### 02 Client Compliance (Meera Iyer, Northwind)
- **Accepted:** no customer-facing change without a named approval (voice, welcome and prompt always go through `/approvals`); validation evidence on every script change (the `validation_run_id` is attached); the 24-hour "why did this customer hear this" answer (every call records config version and variant, and every change is in `decisions`); requests only within approver hours.
- **Correction:** the operational latitude she describes (timing, caller-ID rotation) is in the renewal policy only. On KYC auto-apply is off entirely, so caller-ID changes there would also go through approval. My controller follows the policy per workflow.
- **Reply:**  "Meera, nothing a customer hears changes without one of your approvers saying yes, and script changes always carry a validation run id. Requests only go out in your approvers' hours. For the auditors: every call records the config version and variant it used, and every change sits in our decision log with its evidence, approval and time, so the 24-hour answer is one query. Caller-ID rotation on renewal is auto-applied as your policy allows and will be listed in the weekly summary. On KYC, where auto-apply is off, it goes through approval like everything else."

### 03 Platform/SRE (Kabir Menon)
- **Accepted:** survive being killed (`scripts/crash_test.sh`, log in reports); idempotency (deterministic proposal ids, write-ahead, cursor in the same transaction as counts); loop health without reading logs (the `loop_health` rows).
- **Measured:** one simulated day of public_03 in 13.6 to 14.2 s wall (bar: 600 s), about 60k events/s, peak RSS about 2.4 GB on one process.
- **Pushed back / honest:** SQLite is a file inside the controller, not a service, and nobody operates it at 2 a.m. In production I'd use tables in the existing Postgres. Alerts are designed (section 10) but not wired.
- **Reply:**  "Kabir, the kill/restart test is in the repo: it SIGKILLs the controller mid-run, restarts it, and the run finishes with nothing applied twice. Proposals are written to disk before they're sent and have deterministic ids, so retries can't duplicate them. On public_03 one simulated day takes about 14 seconds on one process, about 60k events a second, with peak memory around 2.4 GB. Memory is the first thing I'd fix before 10x. State is one SQLite file today; in production it becomes tables in your Postgres, not new infrastructure."

### 04 Client Ops Supervisor (Anita Desai, Kestrel)
- **Accepted:** her edits are never undone silently (when her prompt edit caused a hallucination spike on public_02, the controller proposed a validated alternative and waited for approval); at most 2 requests a day and none before 11:00, stricter than the policy's 4 and 10:00; no "safe but empty" fixes (prompts with real engagement history are tried first, and on public_02 the fix lifted engagement by 0.10).
- **Tension named:** her policy lets a controller auto-apply voice and opening-line changes. I chose to auto-apply only caller ID on her line and to send voice changes as requests, so she knows before customers hear something new.
- **Not built:** the one screen. The audit query is the interim.
- **Reply:**  "Anita, you'll get at most two requests a day, never before 11:00, and nothing your customers hear changes without your yes. That includes voice, even though your policy would let us auto-apply it. If one of your prompt edits starts causing hallucinations, we don't revert it behind your back: you get one request with the numbers and a validated alternative, preferring a script that has already kept people on calls. The one-screen view isn't built yet. Until then I can send a daily list of what changed and why."

### 05 Product (Tara Kulkarni)
- **Accepted as rules:** KPIs stay separate; one cohort definition (`default`); expired means rejected and nothing stale is resent; every proposal carries impact, diff, affected leads and a dry run; only real calls and real validation runs count as evidence; changes stay in their own workflow.
- **Her open question, answered:** about 1,100 connected calls per arm to see a 6-point engagement lift (16·p(1-p)/d²), with Bonferroni across arms. **What it costs:** on nw_renewal that would take about 5.7 days per decision, so the controller now skips that experiment rather than run one that can't conclude. In my first run it ran anyway, and the grader marked that exploration harmful. Incidents use a separate, faster rule: 150 connected calls and z ≥ 3.0.
- **Her three P0s:** caller-ID health is built; per-person call time and the supervisor view are cut.
- **Reply:**  "Tara, I held all your definitions. On minimum size: about 1,100 connected calls per arm to see a 6-point engagement lift, corrected for multiple arms. The cost is real: on Northwind renewal that's nearly 6 days per decision, so the controller declines to start that test, because my first run showed an unfinished test only costs engagement. Of your three P0s, caller-ID health is built and rotated a bad number out on the same day. Per-person call time and the supervisor view are cut: call time needs the most data and care, and the audit query covers the view for now."

### 06 CTO (Aryamann)
- **Accepted:** explainable over bigger (every change has a z-score or a validation run behind it); nothing that can hurt a cohort ships untested (the feasibility check); credits only where they change a decision (5 credits used on public_02, a 20-credit reserve); docs for the on-call engineer.
- **Wrong claims caught:** Sales on Kestrel and on Northwind KYC auto-apply; the Compliance latitude does not extend to KYC.
- **Reply:**  "I built depth on four decisions: incident response, caller-ID health, voice experiments that refuse to start if they can't finish, and a crash-safe governance client, all explained from one audit table. On the incident scenario that gave zero harmful changes and a validated prompt fix in 210 minutes. I cut call time, multi-cohort targeting and the supervisor view, and the write-up says who I said no to. Two memo claims didn't match the policies: Kestrel auto-apply isn't 'everything', and Northwind KYC auto-apply is off. What breaks first at scale is single-process memory and ingestion. The fix is sharding by workflow."

## 4. Architecture
```
            ┌──────────────── simulator (HTTP only) ───────────────┐
            │ /sim/advance  /events/chunks  /workflows  .../config  │
            │ /approvals  .../apply  /validation/runs  /llm/...     │
            └────▲──────────────┬───────────────────────▲──────────┘
                 │ advance 60m   │ chunks (NDJSON)       │ propose / poll / apply / PUT
┌────────────────┴──────────────▼───────────────────────┴──────────────┐
│ controller/main.py  (one turn per simulated hour)                    │
│  catch_up/ingest → poll → per workflow:                              │
│     detect_hallucinations → check_caller_ids → fix_hallucinations    │
│     → run_voice_experiment (at decision hour)  → daily loop_health   │
└────────────────┬───────────────────────────────────────▲──────────────┘
                 │ one transaction per chunk              │ reads
┌────────────────▼───────────────────────────────────────┴──────────────┐
│ state/<scenario>.db  (SQLite, WAL)                                    │
│  cursor │ stats(wf, day, dim, option → dials, connected, engaged,     │
│         │       hallucinated) │ proposals │ decisions (audit)         │
└───────────────────────────────────────────────────────────────────────┘
```
- **State:** all of it is in the db. Nothing important lives only in memory.
- **On restart:** `run.sh` picks `state/<scenario>.db` and `catch_up` ingests only chunks above the cursor. `poll` re-checks every open proposal. A proposal stuck in `intent` (crashed between saving and sending) is reconciled through `GET /approvals` or resent with the same id. If the db's cursor is beyond the simulator's chunk count, the db is from an older world and is moved aside.

## 5. The learning loop
- **Cohorts:** `default` only. Every split halves the traffic per arm, and the feasibility numbers in section 3 (memo 05) show renewal can't even afford the default-cohort voice test. Cohorts come after per-arm traffic allows them.
- **Experiment design:**
  - Voice only. The current voice keeps 40% as control and the other voices share 60%.
  - Welcome message would run sequentially after voice, never crossed with it, so there are no interaction effects to untangle.
  - Before exploring, days needed = min sample ÷ (historical connected per day × challenger share). If that's over 5, the test is skipped and logged.
- **Deciding a winner:**
  - A two-proportion z-test of each challenger vs control on engagement, over the window since the voice allocation last changed (read from my own log, so supervisor edits restart the window).
  - Bonferroni line: z_crit = Φ⁻¹(1 − 0.05 / (2k)) for k challengers (2.39 for 3).
  - An arm counts only once both it and the control have the minimum sample.
  - Family-wise false-winner rate of about 5% per decision.
- **Promotion and decay:** the winner goes to 90% and the old control keeps 10% as a holdout, so decay stays measurable. Arms significantly worse than control are dropped. There is no automatic decay rollback yet (section 13).
- **Caller ID:**
  - Each live DID's pickup vs the pooled other live DIDs, same day, at least 300 dials, flagged at z ≤ −3.0.
  - Traffic moves to the healthy DIDs plus one unused spare, in max_shift steps (auto-apply), or as a proposal where auto-apply is off.
  - On public_02 the flagged DID was rotated out within the day: the flagged-DID share went from 11.5% to 0 from the next day on.
- **Call time:** cut (C4).
- **Guardrail:** a voice winner must not drop pickup by more than the workflow's `pickup_rate_max_drop`, otherwise it isn't promoted.

## 6. Governance
```
intent ──POST──► pending ──► approved ──apply 200──► applied
  │ (crash: reconcile, else resend same id)  └─ 409 stale / 410 lapsed ──► logged, never forced
  └─ POST refused (4xx) ──► refused (logged)
pending ──► rejected | expired ──► logged; never resent. A new proposal = fresh data + current version in its id
PUT /config (auto-apply, policy-permitted dims only, ≤ max_shift) ──► auto_applied | auto_apply_failed_<code>
```
- **Timing and limits:**
  - No request outside approver hours (Kestrel not before 11:00).
  - At most min(policy limit, client preference) requests per rolling 24h, counted from my own log.
  - One open proposal per workflow.
  - `poll` runs every simulated hour, inside the 2-hour apply window.
  - On public_02: 2 requests, 2 approved, 0 expired, 0 lapsed, 0 apply conflicts.
- **Auto-apply:**
  - Used only for caller-ID rotation, and only where the policy lists `caller_id` (renewal, ks_cancel).
  - It moves in max_shift steps, which is why one rotation shows as several versions.
  - Voice and welcome are never auto-applied, even on ks_cancel where policy permits it (memos 02 and 04).
- **Impact, affected leads and dry run, honestly:**
  - The dry run holds the real evidence: z, sample sizes, the validation result, the candidate's history.
  - `impact_score` is the total-variation distance of the change (1.0 for a prompt swap). It ranks size, not expected benefit.
  - `affected_leads` is the daily batch, an upper bound.
- **Supervisor edits mid-flight:** observed as `config_changed` events and logged. The experiment window restarts from the latest voice change. An approved change that no longer applies (409 stale) is logged and dropped, so the supervisor's version stands.

## 7. Incidents
- **Hallucination detection:**
  - Per workflow, per prompt seen today: today's flagged rate vs the workflow's whole pre-D0 history.
  - Triggers at 150+ connected calls and one-sided z ≥ 3.0, about a 0.13% false-alarm chance per check.
  - Logged once per prompt per day.
- **Choosing a fix:**
  - Earlier prompts with real history, best observed engagement first. LLM suggestions (3 credits) only if there are none.
  - Each candidate is validated at n = 500 (5 credits), at most 2 per incident, never below the 20-credit reserve.
  - The first candidate at or above `min_validation_pass_rate` is proposed with its `validation_run_id`.
- **Credits:** public_02 used 5 (one validation, no LLM call). public_01 used 8 (one LLM call plus one validation, on nw_kyc, see section 11).
- **Verification:** detection keeps running on the new prompt. Post-apply engagement is in `stats` but not checked automatically (section 13).
- **Timeline from public_02** (from `reports/audit-public_02.txt`):

| Sim time | Workflow | What happened |
|---|---|---|
| D2 12:00 | ks_cancel | `did_flagged D2`: its pickup significantly below the other live DIDs. Auto-rotation starts (v3 to v5, max_shift 0.2 steps). |
| D3 11:30 | ks_cancel | Supervisor edit v6: prompt changed to P2. |
| D3 13:00 | ks_cancel | `incident_detected`: P2 hallucination spike. P1 validated (pass 0.986). Proposal "switch prompt P2 → P1". |
| D3 15:00 | ks_cancel | Approved and applied (v7). The grader: time to fix 210 min, fixed by controller_approved, engagement +0.102. |
| D4 14:00 | ks_cancel | `did_flagged D1`, rotated the same way (v8 to v10). |
| D5 15:00 | nw_renewal | Supervisor voice edit (v2), observed and logged. No action, because no experiment was running there (skipped as infeasible). |

## 8. Scale
Measured on public_03_load (12 workflows, about 840k events per simulated day), with one controller process on this laptop:
- **Wall time per simulated day:** 13.6 to 14.2 s (bar: 600 s). The whole 5-day scenario including the history catch-up: 1 min 46 s.
- **Ingest:** about 59k to 62k events/s.
- **Peak memory (max RSS):** about 2.4 GB.
- **Credits:** 0 used.

**What breaks first at 10x** (about 8.4M events per simulated day):
- Memory. Each chunk is read whole into memory as text plus parsed JSON. At 10x, chunks would push past the 8 GB box. Fix: stream-parse lines and aggregate as you go.
- Then single-core JSON parsing (about 140 s per day at the current rate, still under the bar).
- Then the single SQLite writer.

**Partitioning:** shard workflows across worker processes, each with its own cursor over workflow-filtered events and its own decisions. Tenants are independent, so nothing is shared except the credit budget.

## 9. Reliability
- **Crash test:** `scripts/crash_test.sh` starts the simulator, SIGKILLs the controller 2 seconds in, prints the stored cursor and proposals, restarts, finishes and grades. In my run (`reports/crash-test.log`) the kill landed at D1 14:00 with the cursor at chunk 19 and one proposal already applied. Run 2 caught up and finished. The graded crash run (`reports/crash_public_02_incidents.md`) matched the clean run exactly: 9 changes, 4 beneficial, 5 neutral, 0 harmful, and no proposal applied twice.
- **What is lost on a kill:** nothing committed. A chunk being counted at kill time is re-read in full.
- **Idempotency keys:**
  - Proposals use `workflow:v{base_version}:{tag}`, so a repeat POST gets 409 duplicate, which triggers reconcile.
  - Auto-apply ids are keyed by workflow, version and hour.
- **4xx/5xx:** refused calls are logged with status and body and not retried blindly. A 409 on advance means the scenario finished, so the loop exits. Network errors are not retried (section 13).

## 10. Audit & observability
**Query** (why did ks_cancel change, who approved it, what happened next):
```
sqlite3 -header -column state/public_02_incidents.db "
select substr(sim_ts,1,16) ts, kind, substr(summary,1,70) summary, proposal_id
from decisions where workflow_id = 'ks_cancel'
  and kind not in ('observed_approval','loop_health','experiment_checked')
order by id;"
```
**Output (public_02, excerpt):**
```
ts                wf          kind                    summary                                                               
----------------  ----------  ----------------------  ----------------------------------------------------------------------
2027-01-07T11:30  ks_cancel   observed_config_change  config v6 by supervisor: prompt_version                               
2027-01-07T13:00  ks_cancel   incident_detected       P2 2027-01-07 hallucination spike                                     
2027-01-07T13:00  ks_cancel   fix_validated           P2 2027-01-07 -> P1 pass=0.986                                        
2027-01-07T13:00  ks_cancel   proposed                switch prompt P2 -> P1 (hallucination incident)                       
2027-01-07T15:00  ks_cancel   applied                 apr_00002: {"workflow_id":"ks_cancel","version":7,"changed_dims":["pro
2027-01-07T15:00  ks_cancel   observed_config_change  config v7 by controller_approved: prompt_version                      
```
Each `proposed` row's evidence column holds the full dry run (z, samples, validation id), and `select evidence from decisions where proposal_id = '...'` shows it.

**Would page:**
- credits below the reserve while an incident is open,
- ingest lag above one simulated hour,
- an approved change within 30 minutes of lapsing.

**Would not page (daily summary instead):** rejections, expiries, supervisor edits, skipped experiments, validation failures.

## 11. Results
**public_01** (no incidents):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ |
|---|---|---|---|---|---|---|
| ks_cancel | 0.346 | +4.0% | 0.240 | +0.6% | 0.503 | +2.4% |
| nw_kyc | 0.373 | +2.7% | 0.240 | -0.7% | 0.611 | +3.4% |
| nw_renewal | 0.415 | -1.1% | 0.261 | -0.8% | 0.544 | -0.4% |
| ALL | 0.375 | +1.8% | 0.247 | -0.2% | 0.538 | +1.5% |

3 changes applied, all beneficial: two ks_cancel voice steps (explore, then promote) and one nw_kyc prompt change. That last one is worth being honest about: public_01 has no scripted incident, but on D3 the detector saw P1's hallucination rate at z ≥ 3.0 against history. With no other prompt in history, it asked the LLM (P3), validated it (pass 0.988) and proposed it. The grader marked it beneficial. It may still have been day-level noise passing the threshold, and that is the false-positive cost of z = 3.0 checked hourly. nw_renewal was left alone (experiment infeasible), so its small dip is noise against a no-op baseline.

**public_02** (incidents):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ |
|---|---|---|---|---|---|---|
| ks_cancel | 0.329 | +32.9% | 0.222 | +20.8% | 0.510 | +7.1% |
| nw_kyc | 0.379 | +1.2% | 0.244 | +0.7% | 0.598 | +1.0% |
| nw_renewal | 0.421 | -1.3% | 0.261 | -0.3% | 0.543 | -1.0% |
| ALL | 0.370 | +10.8% | 0.240 | +8.0% | 0.540 | +1.8% |

9 changes applied: 4 beneficial, 5 neutral, 0 harmful. Hallucinated calls: 163, against 416 in the baseline. nw_renewal ends 1.3% below baseline with no change applied to it, which is run-to-run noise rather than anything the controller did.

**Interpretation:**
- The big wins are the two incidents on ks_cancel: caller-ID rotation (pickup) and the prompt fix (hallucinations down, engagement up).
- Lift from experiments is small by design: one voice test on ks_cancel, and renewal skipped.
- **The harmful change I removed:** before the feasibility check, the controller explored voices on nw_renewal. The test could never reach its sample size, and the grader marked it harmful (engagement −0.015). The check turned that into "skipped, logged".
- nw_kyc's primary is pickup, and only caller ID moves pickup there. With auto-apply off and no DID incident, there was nothing safe to change.

## 12. Trade-offs & rejected alternatives
| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Postgres or Redis now | Platform runs them | A file store keeps the assignment self-contained. The schema ports directly. |
| 15-minute steps | React faster | More round trips, no gain: signals need hours of calls to be significant. |
| Let the LLM pick the fix | Fewer moving parts | Breaks "winner selection is statistical". Validation decides, and history-backed prompts go first. |
| Auto-apply voice on Kestrel | Policy allows it, and it's faster (Sales) | The customer hears it. Anita and the CTO memo both favour asking. |
| Thompson sampling / bandits | Less regret during exploration | Harder to explain to an approver, and "deterministic winner selection" is easier to show with a fixed test. |
| Always run the voice test | More lift opportunities | Measured as harmful on renewal. The feasibility check replaced it. |

## 13. Known weaknesses
- No automatic decay rollback. The 10% holdout makes it measurable, but nothing acts on it yet.
- No automatic post-apply guardrail check after a prompt fix.
- Impact score measures change size, not expected benefit, so a queue ranked by it would be imperfect.
- Peak memory of about 2.4 GB on public_03 comes from reading whole chunks. It is the first thing to fix for 10x.
- One cohort only, no call-time optimisation, and the welcome message is never tested.
- A flagged DID stays flagged for the rest of the run, with no re-test.
- One process, no retry with backoff on network errors, and no alerting.
- The hallucination baseline is all of pre-D0 history, so a workflow whose normal rate drifts could be misjudged.
- Supervisor edits are logged and respected, but not evaluated. The nw_renewal voice edit on D5 was never checked for harm.

## 14. If this went to production at Formi next month
1. Post-apply verification and decay rollback: a trailing-window test of every applied change against its holdout or prior, with an automatic rollback proposal.
2. Streaming ingestion and sharding by workflow. Load test at 10x and a memory cap.
3. Move the store to Postgres, then add a read-only supervisor view (C11) over the same tables.
4. Evaluate supervisor edits with the same detectors, and offer a validated revert proposal, never an auto-revert.
5. Cohorts and call-time optimisation (C2, C4) on the highest-volume workflow first, gated by the same feasibility rule.
6. Paging for the three alerts in section 10.
