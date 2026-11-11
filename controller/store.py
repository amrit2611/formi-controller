"""Everything the controller remembers lives in one SQLite file, so a restart loses nothing."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_chunk INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS stats (
    workflow_id TEXT NOT NULL,
    day TEXT NOT NULL,
    dim TEXT NOT NULL,
    option TEXT NOT NULL,
    dials INTEGER NOT NULL,
    connected INTEGER NOT NULL,
    engaged INTEGER NOT NULL,
    hallucinated INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, day, dim, option)
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    approval_id TEXT,
    status TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_ts TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence TEXT NOT NULL,
    proposal_id TEXT,
    wall_ts REAL NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ---- ingestion cursor -------------------------------------------------
    def last_chunk(self):
        row = self.db.execute("SELECT last_chunk FROM cursor WHERE id = 1").fetchone()
        return row[0] if row else -1

    def save_chunk(self, chunk_no, counts, observed):
        with self.db:  # one transaction: counts, observed events and cursor move together, or not at all
            self.db.executemany(
                """INSERT INTO stats VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (workflow_id, day, dim, option) DO UPDATE SET
                     dials = dials + excluded.dials,
                     connected = connected + excluded.connected,
                     engaged = engaged + excluded.engaged,
                     hallucinated = hallucinated + excluded.hallucinated""",
                [(*key, *val) for key, val in counts.items()],
            )
            self.db.execute(
                "INSERT INTO cursor VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET last_chunk = excluded.last_chunk",
                (chunk_no,),
            )
            self.db.executemany(
                """INSERT INTO decisions (sim_ts, workflow_id, kind, summary, evidence, proposal_id, wall_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(ts, wf, kind, summary, json.dumps(ev), ev.get("proposal_id"), time.time())
                 for ts, wf, kind, summary, ev in observed],
            )

    def totals(self, wf, dim, since_day, until_day="9999"):
        rows = self.db.execute(
            """SELECT option, SUM(dials), SUM(connected), SUM(engaged), SUM(hallucinated)
               FROM stats WHERE workflow_id = ? AND dim = ? AND day >= ? AND day < ?
               GROUP BY option""",
            (wf, dim, since_day, until_day),
        )
        return {opt: {"dials": d, "connected": c, "engaged": e, "hallucinated": h} for opt, d, c, e, h in rows}

    # ---- audit log (append only) -------------------------------------------
    def log(self, sim_ts, wf, kind, summary, evidence, proposal_id=None):
        with self.db:
            self.db.execute(
                """INSERT INTO decisions (sim_ts, workflow_id, kind, summary, evidence, proposal_id, wall_ts)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (sim_ts, wf, kind, summary, json.dumps(evidence), proposal_id, time.time()),
            )

    def already_logged(self, wf, kind, summary_like="%"):
        return self.db.execute(
            "SELECT 1 FROM decisions WHERE workflow_id = ? AND kind = ? AND summary LIKE ? LIMIT 1",
            (wf, kind, summary_like),
        ).fetchone() is not None

    # ---- proposals ---------------------------------------------------------
    def save_proposal(self, proposal_id, wf, status, body, approval_id=None):
        with self.db:
            self.db.execute(
                """INSERT INTO proposals VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (proposal_id) DO UPDATE SET status = excluded.status,
                     approval_id = COALESCE(excluded.approval_id, proposals.approval_id)""",
                (proposal_id, wf, approval_id, status, json.dumps(body)),
            )

    def open_proposals(self):
        return self.db.execute(
            "SELECT proposal_id, workflow_id, approval_id, status FROM proposals WHERE status IN ('intent', 'pending', 'approved')"
        ).fetchall()

    def has_open_proposal(self, wf):
        return any(row[1] == wf for row in self.open_proposals())

    # ---- added in Part 7 (strengthen) ------------------------------------
    def count_since(self, wf, kind, since_ts):
        """How many audit rows of one kind a workflow has had since a timestamp."""
        return self.db.execute(
            "SELECT COUNT(*) FROM decisions WHERE workflow_id = ? AND kind = ? AND sim_ts >= ?",
            (wf, kind, since_ts),
        ).fetchone()[0]

    def last_change_ts(self, wf, dim):
        """When was this dimension last changed, by anyone? Read from our own audit log."""
        row = self.db.execute(
            """SELECT sim_ts FROM decisions
               WHERE workflow_id = ? AND kind = 'observed_config_change' AND summary LIKE ?
               ORDER BY id DESC LIMIT 1""",
            (wf, f"%{dim}%"),
        ).fetchone()
        return row[0] if row else None

    def flagged_dids(self, wf):
        rows = self.db.execute(
            "SELECT DISTINCT summary FROM decisions WHERE workflow_id = ? AND kind = 'did_flagged'", (wf,))
        return {r[0] for r in rows}

    def latest(self, wf, kind):
        """The newest audit row of one kind: (sim_ts, summary, evidence) or None."""
        return self.db.execute(
            "SELECT sim_ts, summary, evidence FROM decisions WHERE workflow_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (wf, kind),
        ).fetchone()
