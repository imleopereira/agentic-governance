-- Console auth tables: users and sessions.
--
-- Users are managed via CLI (governance console add-user) or admin API.
-- Sessions are Postgres-backed, short-lived (default 8h), and cleaned
-- up opportunistically on login.

CREATE TABLE IF NOT EXISTS governance_console_users (
    user_id       UUID         PRIMARY KEY,
    username      VARCHAR(128) NOT NULL UNIQUE,
    password_hash VARCHAR(256) NOT NULL,
    role          VARCHAR(16)  NOT NULL DEFAULT 'viewer'
                  CHECK (role IN ('viewer', 'admin')),
    disabled      BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS governance_console_sessions (
    session_id    UUID         PRIMARY KEY,
    user_id       UUID         NOT NULL REFERENCES governance_console_users(user_id),
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ  NOT NULL,
    revoked       BOOLEAN      NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_sessions_user    ON governance_console_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON governance_console_sessions (expires_at);
