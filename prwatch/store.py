"""SQLite state for the PR watcher: one row per PR, one row per model call (the cost ledger)."""

import datetime
import sqlite3

from prwatch.decide import Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
  number INTEGER PRIMARY KEY,
  role TEXT NOT NULL DEFAULT '',
  last_reviewed_head TEXT NOT NULL DEFAULT '',
  last_rerequest_head TEXT NOT NULL DEFAULT '',
  last_fix_review_id INTEGER NOT NULL DEFAULT 0,
  skipped_head TEXT NOT NULL DEFAULT '',
  last_note TEXT NOT NULL DEFAULT '',
  head_seen TEXT NOT NULL DEFAULT '',
  head_seen_at TEXT NOT NULL DEFAULT '',
  cap_commented INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  pr INTEGER NOT NULL,
  kind TEXT NOT NULL,
  model TEXT NOT NULL,
  head TEXT NOT NULL,
  reason TEXT NOT NULL,
  outcome TEXT,         -- NULL while running; a crash mid-call still counts against the caps
  in_tokens INTEGER,    -- tokens and cost come from claude's JSON output (every model, subagents
  out_tokens INTEGER,   -- included); NULL when the call gave no JSON. cost_usd is claude's list-price
  cache_read INTEGER,   -- figure, not what the subscription token is billed
  cache_write INTEGER,
  cost_usd REAL,
  reviewer TEXT,        -- the reviewer subagent's model for a review (opus, or the small model)
  account TEXT          -- which token paid: primary, second or desktop
);
-- One review per (PR, head): key review:<pr>:<head>. state: model (needs a verdict), post (verdict
-- stored, not confirmed on GitHub), posted, dropped, stuck. The verdict is kept, so a retried post
-- never re-runs the model.
CREATE TABLE IF NOT EXISTS jobs (
  key TEXT PRIMARY KEY,
  pr INTEGER NOT NULL,
  head TEXT NOT NULL,
  kind TEXT NOT NULL,
  base_prev TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL,
  model_tries INTEGER NOT NULL DEFAULT 0,
  post_tries INTEGER NOT NULL DEFAULT 0,
  next_try_at TEXT NOT NULL DEFAULT '',
  verdict_json TEXT NOT NULL DEFAULT '',
  review_id INTEGER,
  last_error TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  verdict_at TEXT NOT NULL DEFAULT '',   -- when the verdict was stored (caps the wait for CI)
  posted_event TEXT NOT NULL DEFAULT '', -- APPROVE, COMMENT or REQUEST_CHANGES as posted
  withheld TEXT NOT NULL DEFAULT ''      -- why an approval was withheld; "CI ..." is retried by approve_later
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
-- One row per reference leaf the brain refresh follows. sync_sha: the master commit the leaf was last
-- checked against. state: ok, due (its named files changed since), stuck (needs a person).
CREATE TABLE IF NOT EXISTS brain_leaves (
  slug TEXT PRIMARY KEY,
  path TEXT NOT NULL DEFAULT '',
  sync_sha TEXT NOT NULL DEFAULT '',
  synced_at TEXT NOT NULL DEFAULT '',
  touched_at TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'ok',
  tries INTEGER NOT NULL DEFAULT 0,
  next_try_at TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT ''
);
"""


USAGE_COLUMNS = (("in_tokens", "INTEGER"), ("out_tokens", "INTEGER"), ("cache_read", "INTEGER"),
                 ("cache_write", "INTEGER"), ("cost_usd", "REAL"))


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(calls)")}
        for col, typ in USAGE_COLUMNS:
            if col not in have:  # databases created before the usage columns
                self.db.execute(f"ALTER TABLE calls ADD COLUMN {col} {typ}")
        have_jobs = {r[1] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for col in ("verdict_at", "posted_event", "withheld"):
            if col not in have_jobs:  # databases from before these columns
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        have_calls = {r[1] for r in self.db.execute("PRAGMA table_info(calls)")}
        for col in ("reviewer", "account"):
            if col not in have_calls:  # databases from before these columns
                self.db.execute(f"ALTER TABLE calls ADD COLUMN {col} TEXT")
        have = {r[1] for r in self.db.execute("PRAGMA table_info(prs)")}
        for col, typ in (("head_seen", "TEXT NOT NULL DEFAULT ''"), ("head_seen_at", "TEXT NOT NULL DEFAULT ''"),
                         ("cap_commented", "INTEGER NOT NULL DEFAULT 0")):
            if col not in have:  # databases created before these columns
                self.db.execute(f"ALTER TABLE prs ADD COLUMN {col} {typ}")
        self.db.commit()

    def row(self, n):
        r = self.db.execute("SELECT * FROM prs WHERE number=?", (n,)).fetchone()
        if not r:
            return Row(), ""
        return Row(r["role"], r["last_reviewed_head"], r["last_rerequest_head"],
                   r["last_fix_review_id"], r["skipped_head"], r["head_seen"], r["head_seen_at"], r["cap_commented"]), r["last_note"]

    def save(self, n, row, note):
        self.db.execute(
            "INSERT INTO prs (number, role, last_reviewed_head, last_rerequest_head, last_fix_review_id, "
            "skipped_head, last_note, head_seen, head_seen_at, cap_commented) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(number) DO UPDATE SET "
            "role=excluded.role, last_reviewed_head=excluded.last_reviewed_head, "
            "last_rerequest_head=excluded.last_rerequest_head, "
            "last_fix_review_id=excluded.last_fix_review_id, skipped_head=excluded.skipped_head, "
            "last_note=excluded.last_note, head_seen=excluded.head_seen, head_seen_at=excluded.head_seen_at, "
            "cap_commented=excluded.cap_commented",
            (n, row.role, row.last_reviewed_head, row.last_rerequest_head,
             row.last_fix_review_id, row.skipped_head, note, row.head_seen, row.head_seen_at, row.cap_commented))
        self.db.commit()

    def calls_today(self, pr=None):
        day = utcnow().strftime("%Y-%m-%d")
        q, args = "SELECT COUNT(*) FROM calls WHERE substr(ts,1,10)=?", [day]
        if pr is not None:
            q, args = q + " AND pr=?", args + [pr]
        return self.db.execute(q, args).fetchone()[0]

    def start_call(self, pr, kind, model, head, reason):
        cur = self.db.execute(
            "INSERT INTO calls (ts, pr, kind, model, head, reason) VALUES (?,?,?,?,?,?)",
            (stamp(), pr, kind, model, head, reason))
        self.db.commit()
        return cur.lastrowid

    def end_call(self, call_id, outcome, usage=None):
        u = usage or {}
        self.db.execute("UPDATE calls SET outcome=?, reviewer=?, account=?, "
                        + ",".join(f"{c}=?" for c, _ in USAGE_COLUMNS) + " WHERE id=?",
                        (outcome[:300], u.get("reviewer"), u.get("account"), *(u.get(c) for c, _ in USAGE_COLUMNS),
                         call_id))
        self.db.commit()

    def reviewers_today(self):
        """{reviewer model: reviews} over today's calls that ran a reviewer."""
        day = utcnow().strftime("%Y-%m-%d")
        return dict(self.db.execute("SELECT reviewer, COUNT(*) FROM calls WHERE substr(ts,1,10)=? AND reviewer IS NOT "
                                    "NULL GROUP BY reviewer", (day,)).fetchall())

    def usage_today(self):
        """Sums over today's calls: in_tokens, out_tokens, cache_read, cache_write, cost_usd."""
        day = utcnow().strftime("%Y-%m-%d")
        r = self.db.execute("SELECT " + ",".join(f"COALESCE(SUM({c}),0)" for c, _ in USAGE_COLUMNS)
                            + " FROM calls WHERE substr(ts,1,10)=?", (day,)).fetchone()
        return {c: r[i] for i, (c, _) in enumerate(USAGE_COLUMNS)}

    # ---- jobs

    def job(self, key):
        return self.db.execute("SELECT * FROM jobs WHERE key=?", (key,)).fetchone()

    def upsert_job(self, key, **fields):
        fields["updated_at"] = stamp()
        if self.job(key) is None:
            cols = ["key", *fields]
            self.db.execute(f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [key, *fields.values()])
        else:
            self.db.execute(f"UPDATE jobs SET {','.join(k + '=?' for k in fields)} WHERE key=?",
                            [*fields.values(), key])
        self.db.commit()

    def jobs_in(self, *states):
        return self.db.execute(f"SELECT * FROM jobs WHERE state IN ({','.join('?' * len(states))}) ORDER BY updated_at",
                               states).fetchall()

    def recent_posted(self, limit=10):
        return self.db.execute("SELECT * FROM jobs WHERE state IN ('posted','dropped','stuck') "
                               "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()

    # ---- brain leaves

    def brain_rows(self):
        return self.db.execute("SELECT * FROM brain_leaves ORDER BY slug").fetchall()

    def brain_row(self, slug):
        return self.db.execute("SELECT * FROM brain_leaves WHERE slug=?", (slug,)).fetchone()

    def brain_upsert(self, slug, **fields):
        if self.brain_row(slug) is None:
            cols = ["slug", *fields]
            self.db.execute(f"INSERT INTO brain_leaves ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            [slug, *fields.values()])
        elif fields:
            self.db.execute(f"UPDATE brain_leaves SET {','.join(k + '=?' for k in fields)} WHERE slug=?",
                            [*fields.values(), slug])
        self.db.commit()

    # ---- meta (auth pause)

    def get_meta(self, k, default=""):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.db.commit()


def stamp(dt=None):
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def due(next_try_at):
    return not next_try_at or next_try_at <= stamp()


def later(minutes):
    return stamp(utcnow() + datetime.timedelta(minutes=minutes))
