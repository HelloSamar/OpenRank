-- OpenRank: multi-group data model for Cloudflare D1 (D1 is SQLite).
--
-- This file is the canonical copy. worker/migrations/0001_init.sql must be identical
-- (a test checks it). It holds no personal data: Telegram user IDs are stored only as a
-- keyed hash ("user hash"), never raw. It is applied with:  wrangler d1 migrations apply
--
-- Times are unix seconds (UTC). "day" columns are text YYYY-MM-DD.

-- Every chat the bot knows: a private chat, a group, a supergroup or a channel.
CREATE TABLE IF NOT EXISTS chats (
  chat_id     INTEGER PRIMARY KEY,          -- Telegram chat id (negative for groups/channels)
  chat_type   TEXT    NOT NULL CHECK (chat_type IN ('private', 'group', 'supergroup', 'channel')),
  title       TEXT,
  active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),  -- 0 once the bot is removed
  added_by    TEXT,                         -- user hash of whoever added the bot
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

-- Paid plans. 'free' is simply the absence of a row (or an expired one).
CREATE TABLE IF NOT EXISTS plans (
  chat_id      INTEGER PRIMARY KEY REFERENCES chats (chat_id),
  plan         TEXT    NOT NULL CHECK (plan IN ('individual', 'group')),
  expires_at   INTEGER NOT NULL,
  granted_by   TEXT,                        -- user hash of the admin who granted it
  note         TEXT,                        -- payment reference, for the owner's records
  reminded_7d  INTEGER NOT NULL DEFAULT 0 CHECK (reminded_7d IN (0, 1)),
  reminded_1d  INTEGER NOT NULL DEFAULT 0 CHECK (reminded_1d IN (0, 1)),
  updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS plans_by_expiry ON plans (expires_at);

-- Questions started per chat per day (day = Asia/Kolkata). Drives the daily cap.
CREATE TABLE IF NOT EXISTS usage_daily (
  chat_id    INTEGER NOT NULL,
  day        TEXT    NOT NULL,
  questions  INTEGER NOT NULL DEFAULT 0 CHECK (questions >= 0),
  PRIMARY KEY (chat_id, day)
) WITHOUT ROWID;

-- Approximate number of Worker requests per day (day = UTC, because Cloudflare's free limits
-- reset at 00:00 UTC). Lets the bot refuse new quizzes before a free limit is hit.
CREATE TABLE IF NOT EXISTS budget_daily (
  day     TEXT    PRIMARY KEY,
  events  INTEGER NOT NULL DEFAULT 0 CHECK (events >= 0)
) WITHOUT ROWID;

-- Quiz runs. Phase 2 drives them; this table also guarantees one active run per chat.
CREATE TABLE IF NOT EXISTS runs (
  run_id       TEXT    PRIMARY KEY,
  chat_id      INTEGER NOT NULL REFERENCES chats (chat_id),
  quiz_id      TEXT    NOT NULL,
  total        INTEGER NOT NULL CHECK (total > 0),
  gap_seconds  INTEGER NOT NULL CHECK (gap_seconds BETWEEN 5 AND 600),
  next_index   INTEGER NOT NULL DEFAULT 0 CHECK (next_index >= 0),
  state        TEXT    NOT NULL DEFAULT 'running'
               CHECK (state IN ('running', 'paused', 'done', 'stopped', 'failed')),
  next_due_at  INTEGER,
  started_by   TEXT,                        -- user hash
  created_at   INTEGER NOT NULL,
  updated_at   INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_chat
  ON runs (chat_id) WHERE state IN ('running', 'paused');

-- Weekly scores, keyed by (chat, user hash, week). 'name' is the display name shown on the
-- leaderboard (optional); the Telegram user id itself is never stored.
CREATE TABLE IF NOT EXISTS scores (
  chat_id    INTEGER NOT NULL,
  user_hash  TEXT    NOT NULL,
  week       TEXT    NOT NULL,              -- first day of the week, YYYY-MM-DD (Asia/Kolkata)
  name       TEXT,
  points     REAL    NOT NULL DEFAULT 0,
  correct    INTEGER NOT NULL DEFAULT 0,
  wrong      INTEGER NOT NULL DEFAULT 0,
  skipped    INTEGER NOT NULL DEFAULT 0,
  seconds    REAL    NOT NULL DEFAULT 0,
  quizzes    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (chat_id, user_hash, week)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS scores_leaderboard ON scores (chat_id, week, points DESC);
