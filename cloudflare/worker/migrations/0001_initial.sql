-- Quote-only relay storage. No account, position, order, deal, or P&L columns exist.
CREATE TABLE IF NOT EXISTS quote_snapshots (
  snapshot_id TEXT PRIMARY KEY NOT NULL,
  payload_hash TEXT NOT NULL,
  received_at_ms INTEGER NOT NULL,
  source_generated_at TEXT NOT NULL,
  source_collected_at TEXT NOT NULL,
  expected_collection_interval_seconds INTEGER NOT NULL,
  incoming_collection_age_seconds REAL NOT NULL,
  source_stale_after_seconds INTEGER NOT NULL,
  quote_collected_at TEXT NOT NULL,
  tick_at TEXT NOT NULL,
  bid REAL NOT NULL,
  ask REAL NOT NULL,
  last REAL,
  mid REAL NOT NULL,
  spread REAL NOT NULL,
  spread_points REAL,
  digits INTEGER NOT NULL,
  point REAL NOT NULL,
  trade_tick_size REAL NOT NULL,
  mt5_reported_offset_seconds REAL,
  mt5_reported_offset_material INTEGER NOT NULL CHECK (mt5_reported_offset_material IN (0, 1)),
  CHECK (ask >= bid),
  CHECK (bid > 0),
  CHECK (point > 0),
  CHECK (trade_tick_size > 0)
);

CREATE INDEX IF NOT EXISTS quote_snapshots_received_at
  ON quote_snapshots(received_at_ms DESC, snapshot_id DESC);

CREATE TABLE IF NOT EXISTS ingest_rate_windows (
  window_key TEXT PRIMARY KEY NOT NULL,
  hits INTEGER NOT NULL,
  expires_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ingest_rate_windows_expiry
  ON ingest_rate_windows(expires_at_ms);
