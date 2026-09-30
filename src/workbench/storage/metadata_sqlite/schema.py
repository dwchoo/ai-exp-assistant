"""Versioned SQLite schema for durable task metadata."""

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY CHECK (length(task_id) > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_specs (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, revision),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS session_settings (
    revision INTEGER PRIMARY KEY CHECK (revision > 0),
    settings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY CHECK (length(run_id) > 0),
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    settings_revision INTEGER NOT NULL,
    retry_count_used INTEGER NOT NULL CHECK (retry_count_used >= 0),
    inputs_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, revision) REFERENCES task_specs(task_id, revision),
    FOREIGN KEY (settings_revision) REFERENCES session_settings(revision),
    UNIQUE (task_id, revision, run_id),
    UNIQUE (task_id, run_id)
);

CREATE TABLE IF NOT EXISTS task_decisions (
    decision_id TEXT PRIMARY KEY CHECK (length(decision_id) > 0),
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_id TEXT,
    settings_revision INTEGER NOT NULL,
    retry_count_used INTEGER NOT NULL CHECK (retry_count_used >= 0),
    kind TEXT NOT NULL CHECK (kind IN (
        'scope_approved', 'proceed', 'revoked', 'cancelled',
        'retry', 'continue', 'blocked'
    )),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, revision) REFERENCES task_specs(task_id, revision),
    FOREIGN KEY (task_id, revision, run_id) REFERENCES runs(task_id, revision, run_id),
    FOREIGN KEY (settings_revision) REFERENCES session_settings(revision),
    UNIQUE (decision_id, task_id, revision),
    CHECK (
        (kind IN ('scope_approved', 'proceed') AND run_id IS NULL)
        OR (kind = 'revoked')
        OR (kind IN ('cancelled', 'retry', 'continue', 'blocked') AND run_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS authorization_consumption (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (decision_id, task_id, revision)
        REFERENCES task_decisions(decision_id, task_id, revision),
    FOREIGN KEY (task_id, revision, run_id)
        REFERENCES runs(task_id, revision, run_id)
);

CREATE TABLE IF NOT EXISTS active_runs (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY (task_id, run_id) REFERENCES runs(task_id, run_id)
);

CREATE TABLE IF NOT EXISTS run_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) > 0),
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'started', 'cancelled', 'revoked', 'completed', 'failed'
    )),
    settings_revision INTEGER NOT NULL,
    retry_count_used INTEGER NOT NULL CHECK (retry_count_used >= 0),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (settings_revision) REFERENCES session_settings(revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_terminal_event_per_run
    ON run_events(run_id)
    WHERE kind IN ('cancelled', 'revoked', 'completed', 'failed');

CREATE TABLE IF NOT EXISTS invalidated_revisions (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, revision),
    FOREIGN KEY (task_id, revision) REFERENCES task_specs(task_id, revision),
    FOREIGN KEY (decision_id, task_id, revision)
        REFERENCES task_decisions(decision_id, task_id, revision)
);

CREATE TRIGGER IF NOT EXISTS invalidated_revisions_requires_authority_decision
BEFORE INSERT ON invalidated_revisions
WHEN COALESCE((SELECT kind FROM task_decisions WHERE decision_id = NEW.decision_id), '')
     NOT IN ('cancelled', 'revoked')
BEGIN
    SELECT RAISE(ABORT, 'invalidation requires a cancellation or revocation decision');
END;

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY CHECK (length(message_id) > 0),
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, revision, run_id)
        REFERENCES runs(task_id, revision, run_id)
);

CREATE TABLE IF NOT EXISTS delivery_attempts (
    attempt_id TEXT PRIMARY KEY CHECK (length(attempt_id) > 0),
    message_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages(message_id),
    UNIQUE (message_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS delivery_status_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    status TEXT NOT NULL CHECK (status IN (
        'attempted', 'api_returned', 'omp_processed', 'failed', 'unknown'
    )),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (attempt_id) REFERENCES delivery_attempts(attempt_id),
    UNIQUE (attempt_id, sequence)
);

CREATE TABLE IF NOT EXISTS shell_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) > 0),
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('sent', 'accepted', 'started', 'ended', 'failed')),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS takeover_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) > 0),
    run_id TEXT NOT NULL,
    request_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('requested', 'confirmed')),
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (request_id) REFERENCES takeover_events(event_id),
    CHECK (
        (kind = 'requested' AND request_id IS NULL)
        OR (kind = 'confirmed' AND request_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS one_takeover_confirmation
    ON takeover_events(request_id)
    WHERE kind = 'confirmed';

CREATE TRIGGER IF NOT EXISTS authorization_consumption_requires_proceed
BEFORE INSERT ON authorization_consumption
WHEN (SELECT kind FROM task_decisions WHERE decision_id = NEW.decision_id) != 'proceed'
BEGIN
    SELECT RAISE(ABORT, 'only a proceed decision can authorize a run');
END;

CREATE TRIGGER IF NOT EXISTS takeover_confirmation_matches_request
BEFORE INSERT ON takeover_events
WHEN NEW.kind = 'confirmed' AND (
    (SELECT kind FROM takeover_events WHERE event_id = NEW.request_id) != 'requested'
    OR (SELECT run_id FROM takeover_events WHERE event_id = NEW.request_id) != NEW.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'takeover confirmation must reference a request for the same run');
END;
"""

MIGRATION_1_TO_2 = """
BEGIN IMMEDIATE;
DROP TRIGGER IF EXISTS invalidated_revisions_immutable_update;
DROP TRIGGER IF EXISTS invalidated_revisions_immutable_delete;
DROP TRIGGER IF EXISTS invalidated_revisions_requires_authority_decision;
ALTER TABLE invalidated_revisions RENAME TO invalidated_revisions_schema_v1;
CREATE TABLE invalidated_revisions (
    task_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, revision),
    FOREIGN KEY (task_id, revision) REFERENCES task_specs(task_id, revision),
    FOREIGN KEY (decision_id, task_id, revision)
        REFERENCES task_decisions(decision_id, task_id, revision)
);
CREATE TRIGGER invalidated_revisions_requires_authority_decision
BEFORE INSERT ON invalidated_revisions
WHEN COALESCE((SELECT kind FROM task_decisions WHERE decision_id = NEW.decision_id), '')
     NOT IN ('cancelled', 'revoked')
BEGIN
    SELECT RAISE(ABORT, 'invalidation requires a cancellation or revocation decision');
END;
INSERT INTO invalidated_revisions(task_id, revision, decision_id, reason, created_at)
    SELECT task_id, revision, decision_id, reason, created_at
    FROM invalidated_revisions_schema_v1;
DROP TABLE invalidated_revisions_schema_v1;
PRAGMA user_version = 2;
COMMIT;
"""

APPEND_ONLY_TABLES = (
    "tasks",
    "task_specs",
    "session_settings",
    "runs",
    "task_decisions",
    "authorization_consumption",
    "run_events",
    "invalidated_revisions",
    "messages",
    "delivery_attempts",
    "delivery_status_events",
    "shell_events",
    "takeover_events",
)
