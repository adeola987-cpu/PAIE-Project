PRAGMA foreign_keys = ON;

-- One row per conversation/session
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- One row per message (user or assistant)
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL,
  role TEXT CHECK(role IN ('user','assistant','system')) NOT NULL,
  content TEXT NOT NULL,
  reply_to_message_id INTEGER,                 -- link reply to the exact message
  turn_index INTEGER,                          -- strict ordering within a session
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  meta_json TEXT,                              -- extras: model, tokens, tools, etc.
  FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
  FOREIGN KEY (reply_to_message_id) REFERENCES messages(id) ON DELETE SET NULL
);