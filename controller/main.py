"""The controller loop: ingest -> detect -> experiment -> propose -> apply -> log.

    python3 controller/main.py --url http://127.0.0.1:8700 --db controller.db
"""
import argparse
import json
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx

from stats import min_sample, rate, z_critical, z_score
from store import Store

DIMS = ("welcome_message", "voice", "caller_id", "prompt_version")
STEP_MINUTES = 60          # how far each POST /sim/advance moves the world
ALPHA = 0.05               # family-wise false-winner rate we accept per decision
MIN_EFFECT = 0.06          # smallest engagement lift worth detecting (6 points)
EXPLORE_CONTROL = 0.4      # share the current voice keeps while challengers are tested
MAX_EXPERIMENT_DAYS = 5    # skip an experiment that cannot reach its sample size in this many days
INCIDENT_Z = 3.0           # hallucination alarm: today's rate vs history, one-sided
INCIDENT_MIN_CONNECTED = 150
DID_Z = 3.0                # caller-ID alarm: this DID's pickup vs the other live DIDs today
DID_MIN_DIALS = 300
CREDIT_RESERVE = 20        # never spend below this, so an incident later still has budget
VALIDATION_N = 500         # costs 5 credits per validation run
# Client wishes that are STRICTER than the signed policy (from stakeholder memo 04).
CLIENT_PREFS = {"ks_cancel": {"earliest_hour": 11, "max_requests_per_24h": 2}}


# ---------------------------------------------------------------- ingestion
def count_chunk(lines):
    counts = defaultdict(lambda: [0, 0, 0, 0])  # dials, connected, engaged, hallucinated
    observed = []
    for line in lines:
        ev = json.loads(line)
        if ev["type"] == "call":
            day = ev["ts"][:10]
            flagged = bool(ev["qa"] and ev["qa"]["hallucination_flag"])
            for dim in DIMS:
                c = counts[(ev["workflow_id"], day, dim, ev["variant"][dim])]
                c[0] += 1
                c[1] += ev["connected"]
                c[2] += ev["engaged"]
                c[3] += flagged
        elif ev["type"] == "config_changed":
            summary = f"config v{ev['version']} by {ev['actor']}: {', '.join(ev['changed_dims'])}"
            observed.append((ev["ts"], ev["workflow_id"], "observed_config_change", summary, ev))
        elif ev["type"] == "approval_decided":
            summary = f"{ev['approval_id']} {ev['status']} {ev.get('reasons') or ''}".strip()
            observed.append((ev["ts"], ev["workflow_id"], "observed_approval", summary, ev))
    return counts, observed


def ingest(client, store, chunk_no):
    lines = client.get(f"/events/chunks/{chunk_no}").text.splitlines()
    counts, observed = count_chunk(lines)
    store.save_chunk(chunk_no, counts, observed)
    return len(lines)


def catch_up(client, store):
    """On start and after a crash: ingest every chunk the store has not seen yet."""
    chunks = []
    for kind in ("history", "events"):
        chunks += client.get("/events/chunks", params={"kind": kind}).json()
    for ch in sorted(chunks, key=lambda c: c["chunk"]):
        if ch["chunk"] > store.last_chunk():
            ingest(client, store, ch["chunk"])
    history = [c for c in chunks if c["kind"] == "history"]
    return history[-1]["until_ts"][:10]  # D0, the first day the controller owns


# ---------------------------------------------------------------- governance
def config_diff(old, new):
    """Only what changed, in a shape a supervisor can read."""
    diff = {}
    for key in ("cohorts", "prompt_version", "schedule"):
        if old.get(key) != new.get(key):
            diff[key] = new.get(key)
    for cohort, dims in new["allocations"].items():
        for dim, dist in dims.items():
            if old["allocations"].get(cohort, {}).get(dim) != dist:
                diff[f"allocations.{cohort}.{dim}"] = dist
    return diff


def minus_24h(ts):
    return (datetime.fromisoformat(ts) - timedelta(hours=24)).isoformat()


def decision_hour(meta):
    start = meta["governance"]["approver_hours"][0]
    return max(start, CLIENT_PREFS.get(meta["workflow_id"], {}).get("earliest_hour", start))


def may_request(store, ts, meta):
    """Inside approver hours (and the client's own hours), and under the daily request limit."""
    wf = meta["workflow_id"]
    gov = meta["governance"]
    hour = int(ts[11:13])
    if not (decision_hour(meta) <= hour < gov["approver_hours"][1]):
        return False
    limit = min(gov["max_requests_per_24h"], CLIENT_PREFS.get(wf, {}).get("max_requests_per_24h", 99))
    return store.count_since(wf, "proposed", minus_24h(ts)) < limit


def propose(client, store, ts, wf, current, new_config, tag, summary, evidence, affected, impact, extra=None):
    proposal_id = f"{wf}:v{current['version']}:{tag}"
    body = {
        "proposal_id": proposal_id,
        "workflow_id": wf,
        "base_version": current["version"],
        "proposed_config": new_config,
        "change_summary": summary,
        "diff": config_diff(current["config"], new_config),
        "affected_leads": affected,
        "dry_run": evidence,
        "impact_score": round(impact, 3),
        "rationale": evidence["rationale"],
    }
    body.update(extra or {})
    store.save_proposal(proposal_id, wf, "intent", body)  # write-ahead: we remember before we send
    send(client, store, ts, proposal_id, wf, body)


def send(client, store, ts, proposal_id, wf, body):
    r = client.post("/approvals", json=body)
    if r.status_code == 200:
        aid = r.json()["approval_id"]
        store.save_proposal(proposal_id, wf, "pending", body, aid)
        store.log(ts, wf, "proposed", body["change_summary"], body["dry_run"], proposal_id)
    elif r.status_code == 409 and "duplicate" in r.text:
        reconcile(client, store, proposal_id, wf, body)  # we sent it before a crash
    else:
        store.save_proposal(proposal_id, wf, "refused", body)
        store.log(ts, wf, "proposal_refused", f"{r.status_code} {r.text[:200]}", body["dry_run"], proposal_id)


def reconcile(client, store, proposal_id, wf, body):
    for a in client.get("/approvals", params={"workflow_id": wf}).json():
        if a["proposal_id"] == proposal_id:
            store.save_proposal(proposal_id, wf, a["status"], body, a["approval_id"])
            return True
    return False


def poll(client, store, ts):
    for proposal_id, wf, aid, status in store.open_proposals():
        if status == "intent":  # crashed between write-ahead and POST
            body = json.loads(store.db.execute(
                "SELECT body FROM proposals WHERE proposal_id = ?", (proposal_id,)).fetchone()[0])
            if not reconcile(client, store, proposal_id, wf, body):
                send(client, store, ts, proposal_id, wf, body)
            continue
        a = client.get(f"/approvals/{aid}").json()
        if a["status"] == "approved":
            r = client.post(f"/workflows/{wf}/apply", json={"approval_id": aid})
            new_status = "applied" if r.status_code == 200 else f"apply_failed_{r.status_code}"
            store.save_proposal(proposal_id, wf, new_status, {})
            store.log(ts, wf, new_status, f"{aid}: {r.text[:200]}", {"approval_id": aid}, proposal_id)
        elif a["status"] != "pending":  # applied, rejected, expired, lapsed
            store.save_proposal(proposal_id, wf, a["status"], {})
            if a["status"] != "applied":
                store.log(ts, wf, a["status"], f"{aid}: {a.get('reasons')}", {"approval_id": aid}, proposal_id)


def auto_apply(client, store, ts, wf, current, new_config, tag, rationale, evidence):
    """PUT /workflows/{id}/config. Only for dims and shifts the policy allows. Returns True if applied."""
    proposal_id = f"{wf}:v{current['version']}:{tag}"
    body = {"proposal_id": proposal_id, "base_version": current["version"],
            "proposed_config": new_config, "rationale": rationale, "evidence": evidence}
    r = client.put(f"/workflows/{wf}/config", json=body)
    kind = "auto_applied" if r.status_code == 200 else f"auto_apply_failed_{r.status_code}"
    store.log(ts, wf, kind, f"{tag}: {r.text[:200]}", {**evidence, "rationale": rationale}, proposal_id)
    return r.status_code == 200


# ---------------------------------------------------------------- detection
def detect_hallucinations(store, ts, wf, d0):
    today = ts[:10]
    baseline = store.totals(wf, "prompt_version", "0000", d0)
    base_conn = sum(v["connected"] for v in baseline.values())
    base_flag = sum(v["hallucinated"] for v in baseline.values())
    for prompt, t in store.totals(wf, "prompt_version", today).items():
        if t["connected"] < INCIDENT_MIN_CONNECTED:
            continue
        z = z_score(base_flag, base_conn, t["hallucinated"], t["connected"])
        if z >= INCIDENT_Z and not store.already_logged(wf, "incident_detected", f"{prompt} {today}%"):
            store.log(ts, wf, "incident_detected", f"{prompt} {today} hallucination spike",
                      {"prompt": prompt, "today_rate": rate(t["hallucinated"], t["connected"]),
                       "today_connected": t["connected"], "history_rate": rate(base_flag, base_conn),
                       "z": round(z, 2)})


def tv_distance(a, b):
    sa, sb = sum(a.values()), sum(b.values())
    keys = set(a) | set(b)
    return 0.5 * sum(abs(a.get(k, 0) / sa - b.get(k, 0) / sb) for k in keys)


def step_toward(old, target, max_shift):
    """Move from old toward target by at most max_shift (total-variation distance)."""
    tv = tv_distance(old, target)
    if tv <= max_shift:
        return target
    f = 0.999 * max_shift / tv
    so, st = sum(old.values()), sum(target.values())
    mixed = {k: old.get(k, 0) / so + f * (target.get(k, 0) / st - old.get(k, 0) / so)
             for k in set(old) | set(target)}
    return {k: round(v, 4) for k, v in mixed.items() if v > 0.0001}


# ---------------------------------------------------------------- caller-ID health (C5)
def check_caller_ids(client, store, ts, meta):
    wf = meta["workflow_id"]
    current = client.get(f"/workflows/{wf}/config").json()
    alloc = current["config"]["allocations"]["default"]["caller_id"]
    live = {k: v for k, v in alloc.items() if v > 0}
    flagged = store.flagged_dids(wf)
    today = ts[:10]
    data = store.totals(wf, "caller_id", today)

    # 1. detect: compare each live DID's pickup with the other live DIDs, same day, same hours
    for did in live:
        t = data.get(did)
        if did in flagged or not t or t["dials"] < DID_MIN_DIALS:
            continue
        others = [data[k] for k in live if k != did and k in data]
        o_dials = sum(o["dials"] for o in others)
        o_conn = sum(o["connected"] for o in others)
        z = z_score(o_conn, o_dials, t["connected"], t["dials"])
        if z <= -DID_Z:
            store.log(ts, wf, "did_flagged", did,
                      {"pickup": round(rate(t["connected"], t["dials"]), 4), "dials": t["dials"],
                       "others_pickup": round(rate(o_conn, o_dials), 4), "z": round(z, 2)})
            flagged.add(did)

    # 2. act: move traffic off flagged DIDs onto a fresh, unflagged option
    bad = [d for d in live if d in flagged]
    if not bad or store.has_open_proposal(wf):
        return
    healthy = [d for d in live if d not in flagged]
    spare = [d for d in meta["options"]["caller_id"] if d not in live and d not in flagged]
    pool = healthy + spare[:1] or healthy
    if not pool:
        return
    target = {d: 1 / len(pool) for d in pool}
    auto = meta["governance"]["auto_apply"]
    if auto["enabled"] and "caller_id" in auto["allowed_dims"]:
        new_alloc = step_toward(alloc, target, auto["max_shift"])
        new_config = json.loads(json.dumps(current["config"]))
        new_config["allocations"]["default"]["caller_id"] = new_alloc
        auto_apply(client, store, ts, wf, current, new_config, f"did-rotate-{ts[:13]}",
                   f"caller ID {', '.join(bad)} lost pickup; rotating toward {', '.join(pool)}",
                   {"flagged": bad, "target": target})
    elif may_request(store, ts, meta):
        new_config = json.loads(json.dumps(current["config"]))
        new_config["allocations"]["default"]["caller_id"] = target
        evidence = {"rationale": f"caller ID {', '.join(bad)} pickup fell significantly below the other DIDs today",
                    "flagged": bad, "target": target}
        propose(client, store, ts, wf, current, new_config, f"did-{'-'.join(bad)}-{today}",
                f"stop dialling from {', '.join(bad)}", evidence, meta["daily_batch"]["size"],
                tv_distance(alloc, target))


# ---------------------------------------------------------------- hallucination fix (C8)
def credits_left(client):
    return client.get("/sim/state").json()["credits_remaining"]


def fix_hallucinations(client, store, ts, meta, d0):
    wf = meta["workflow_id"]
    incident = store.latest(wf, "incident_detected")
    if not incident or incident[0][:10] != ts[:10]:
        return  # nothing detected today
    bad_prompt = json.loads(incident[2])["prompt"]
    current = client.get(f"/workflows/{wf}/config").json()
    if current["config"]["prompt_version"] != bad_prompt:
        return  # already moved off it
    if store.has_open_proposal(wf) or not may_request(store, ts, meta):
        return
    if store.already_logged(wf, "fix_validated", f"{bad_prompt} {ts[:10]}%"):
        return  # one attempt per bad prompt per day

    # Candidates: prompts we have real engagement history for, best first. LLM ideas only if we have none.
    seen = store.totals(wf, "prompt_version", "0000")
    known = sorted((p for p in seen if p != bad_prompt and seen[p]["connected"] >= INCIDENT_MIN_CONNECTED),
                   key=lambda p: -rate(seen[p]["engaged"], seen[p]["connected"]))
    candidates = known
    if not candidates and credits_left(client) > CREDIT_RESERVE + 3:
        r = client.post("/llm/suggest-fixes", json={"workflow_id": wf, "problem": f"hallucination spike on {bad_prompt}"})
        body = r.json() if r.status_code == 200 else {}
        items = body if isinstance(body, list) else body.get("suggestions") or body.get("fixes") or []
        candidates = [i["prompt_version"] if isinstance(i, dict) else i for i in items]
        store.log(ts, wf, "llm_suggested", f"{bad_prompt}: {candidates}", {"response": body})

    min_pass = meta["governance"]["min_validation_pass_rate"]
    for cand in candidates[:2]:
        if credits_left(client) < CREDIT_RESERVE + VALIDATION_N / 100:
            store.log(ts, wf, "fix_skipped_credits", bad_prompt, {"credits": credits_left(client)})
            return
        r = client.post("/validation/runs", json={"workflow_id": wf, "prompt_version": cand, "n": VALIDATION_N})
        v = r.json() if r.status_code == 200 else {}
        vid = v.get("validation_run_id") or v.get("run_id") or v.get("id")
        passed = v.get("pass_rate", 0) >= min_pass
        store.log(ts, wf, "fix_validated", f"{bad_prompt} {ts[:10]} -> {cand} pass={v.get('pass_rate')}",
                  {"candidate": cand, "validation": v, "passed": passed})
        if passed and vid:
            new_config = json.loads(json.dumps(current["config"]))
            new_config["prompt_version"] = cand
            hist = seen.get(cand)
            evidence = {"rationale": f"{bad_prompt} hallucinates far above history; {cand} passed validation "
                                     f"({v.get('pass_rate')}) and has real engagement history",
                        "incident": json.loads(incident[2]), "validation": v,
                        "candidate_history": hist}
            propose(client, store, ts, wf, current, new_config, f"prompt-{cand}-{ts[:10]}",
                    f"switch prompt {bad_prompt} -> {cand} (hallucination incident)", evidence,
                    meta["daily_batch"]["size"], 1.0, extra={"evidence": {"validation_run_id": vid}})
            return


# ---------------------------------------------------------------- experiments (C6, C7)
def run_voice_experiment(client, store, ts, meta, d0):
    wf = meta["workflow_id"]
    if meta["objective"]["primary"] != "engagement_rate" or store.has_open_proposal(wf):
        return
    if not may_request(store, ts, meta):
        return
    max_drop = meta["objective"]["guardrail"].get("pickup_rate_max_drop", 0.02)
    current = client.get(f"/workflows/{wf}/config").json()
    alloc = current["config"]["allocations"]["default"]["voice"]
    arms = {k: v for k, v in alloc.items() if v > 0}
    control = max(arms, key=arms.get)
    today = ts[:10]
    affected = meta["daily_batch"]["size"]

    if len(arms) == 1:  # not experimenting yet: explore once
        if store.already_logged(wf, "proposed", "explore%"):
            return
        others = [v for v in meta["options"]["voice"] if v != control]
        new_alloc = {control: EXPLORE_CONTROL, **{v: (1 - EXPLORE_CONTROL) / len(others) for v in others}}
        hist = store.totals(wf, "voice", "0000", d0)
        # Feasibility: only explore if every challenger can reach the minimum sample within MAX_EXPERIMENT_DAYS.
        days = store.db.execute("SELECT COUNT(DISTINCT day) FROM stats WHERE workflow_id = ? AND day < ?",
                                (wf, d0)).fetchone()[0] or 1
        conn_per_day = sum(t["connected"] for t in hist.values()) / days
        eng = rate(sum(t["engaged"] for t in hist.values()), sum(t["connected"] for t in hist.values()))
        need = min_sample(eng or 0.3, MIN_EFFECT)
        days_needed = need / max(1, conn_per_day * (1 - EXPLORE_CONTROL) / len(others))
        if days_needed > MAX_EXPERIMENT_DAYS:
            if not store.already_logged(wf, "experiment_skipped"):
                store.log(ts, wf, "experiment_skipped",
                          f"voice test would need {days_needed:.1f} days per decision (max {MAX_EXPERIMENT_DAYS})",
                          {"connected_per_day": round(conn_per_day), "min_connected_per_arm": need,
                           "challengers": others})
            return
        evidence = {"rationale": f"history only ever used {control}; test {len(others)} other voices on "
                                 f"{1 - EXPLORE_CONTROL:.0%} of traffic, keep {control} as control",
                    "history_by_voice": hist}
        new_config = json.loads(json.dumps(current["config"]))
        new_config["allocations"]["default"]["voice"] = new_alloc
        propose(client, store, ts, wf, current, new_config, f"explore-{today}",
                f"explore voices: keep {control} at {EXPLORE_CONTROL:.0%}, split the rest",
                evidence, affected, tv_distance(alloc, new_alloc))
        return

    # Window: from the day after the voice allocation last changed (by anyone), read from our own log.
    changed = store.last_change_ts(wf, "voice") or current["applied_at"] or d0
    since = (date.fromisoformat(changed[:10]) + timedelta(days=1)).isoformat()
    data = store.totals(wf, "voice", since)
    ctl = data.get(control)
    if not ctl:
        return
    challengers = [v for v in arms if v != control and v in data]
    need = min_sample(rate(ctl["engaged"], ctl["connected"]) or 0.3, MIN_EFFECT)
    z_crit = z_critical(ALPHA, max(1, len(challengers)))
    results = {}
    for v in challengers:
        t = data[v]
        z = z_score(ctl["engaged"], ctl["connected"], t["engaged"], t["connected"])
        pickup_drop = rate(ctl["connected"], ctl["dials"]) - rate(t["connected"], t["dials"])
        results[v] = {"z": round(z, 2), "connected": t["connected"],
                      "engagement": round(rate(t["engaged"], t["connected"]), 4),
                      "pickup_drop": round(pickup_drop, 4)}
    evidence_base = {"control": control, "control_connected": ctl["connected"],
                     "control_engagement": round(rate(ctl["engaged"], ctl["connected"]), 4),
                     "min_connected_per_arm": need, "z_critical": round(z_crit, 2),
                     "window": [since, today], "arms": results}

    ready = {v: r for v, r in results.items() if r["connected"] >= need and ctl["connected"] >= need}
    losers = [v for v, r in ready.items() if r["z"] <= -z_crit]
    winners = [v for v, r in ready.items() if r["z"] >= z_crit and r["pickup_drop"] <= max_drop]
    new_alloc = None
    if winners:
        best = max(winners, key=lambda v: ready[v]["z"])
        new_alloc = {best: 0.9, control: 0.1}  # keep a 10% holdout to catch decay
        tag, summary = f"promote-{best}-{today}", f"promote voice {best} over {control} (z={ready[best]['z']})"
        rationale = f"{best} beat {control} on engagement past the Bonferroni line with the pickup guardrail held"
    elif losers:
        new_alloc = {k: v for k, v in arms.items() if k not in losers}
        tag, summary = f"drop-{'-'.join(losers)}-{today}", f"stop testing voice {', '.join(losers)}: worse than {control}"
        rationale = f"{', '.join(losers)} significantly worse than {control}; move their traffic back"
    if new_alloc is None:
        store.log(ts, wf, "experiment_checked", f"no change: {len(ready)} of {len(results)} arms have enough data",
                  evidence_base)
        return
    evidence = {**evidence_base, "rationale": rationale}
    new_config = json.loads(json.dumps(current["config"]))
    new_config["allocations"]["default"]["voice"] = new_alloc
    propose(client, store, ts, wf, current, new_config, tag, summary, evidence, affected, tv_distance(alloc, new_alloc))


# ---------------------------------------------------------------- loop
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--db", default="controller.db")
    args = ap.parse_args()
    client = httpx.Client(base_url=args.url, timeout=120)
    world_chunks = client.get("/sim/state").json()["chunks"]
    store = Store(args.db)
    if store.last_chunk() >= world_chunks:  # this db remembers a different (older) world
        store.db.close()
        os.rename(args.db, f"{args.db}.old-{int(time.time())}")
        store = Store(args.db)
    workflows = {w["workflow_id"]: w for w in client.get("/workflows").json()}
    d0 = catch_up(client, store)
    print(f"caught up to chunk {store.last_chunk()}, D0 = {d0}", flush=True)

    day_started, day_events = time.time(), 0
    while True:
        r = client.post("/sim/advance", json={"minutes": STEP_MINUTES})
        if r.status_code == 409:  # scenario already finished (e.g. restarted at the very end)
            break
        state = r.json()
        for ch in state["new_chunks"]:
            if ch["chunk"] > store.last_chunk():
                day_events += ingest(client, store, ch["chunk"])
        ts = state["ts"]
        poll(client, store, ts)
        for wf, meta in workflows.items():
            detect_hallucinations(store, ts, wf, d0)
            check_caller_ids(client, store, ts, meta)
            fix_hallucinations(client, store, ts, meta, d0)
            if int(ts[11:13]) == decision_hour(meta):
                run_voice_experiment(client, store, ts, meta, d0)
        if ts[11:16] == "00:00":  # loop health once per simulated day (C14, minimal)
            wall = time.time() - day_started
            health = {"events": day_events, "wall_seconds": round(wall, 2),
                      "events_per_sec": round(day_events / wall) if wall else None,
                      "credits_remaining": state.get("credits_remaining") or credits_left(client),
                      "open_proposals": len(store.open_proposals())}
            store.log(ts, "_loop", "loop_health", f"{day_events} events in {wall:.1f}s", health)
            print("day done", ts[:10], health, flush=True)
            day_started, day_events = time.time(), 0
        if state["finished"]:
            break
    print("finished:", client.get("/sim/state").json(), flush=True)


if __name__ == "__main__":
    main()
