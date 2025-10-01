-- Schema for optional GPT-Review orchestration service storage
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    instructions_path TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('browser', 'api')),
    model TEXT,
    run_command TEXT,
    branch_prefix TEXT DEFAULT 'iteration',
    remote TEXT DEFAULT 'origin',
    status TEXT NOT NULL DEFAULT 'pending',
    current_iteration INTEGER DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE patches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    op TEXT NOT NULL,
    file TEXT NOT NULL,
    body TEXT,
    body_b64 TEXT,
    target TEXT,
    mode TEXT,
    status TEXT NOT NULL,
    commit_sha TEXT,
    inserted_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE command_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    command TEXT NOT NULL,
    exit_code INTEGER,
    duration_seconds REAL,
    output_tail TEXT,
    triggered_by_patch INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    context JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_patches_session ON patches(session_id);
CREATE INDEX idx_command_runs_session ON command_runs(session_id);
CREATE INDEX idx_logs_session ON logs(session_id);
