import sqlite3, pathlib, json, requests, sys

DB_NAME = "paie_project.db"
SCHEMA_NAME = "schema.sql"
MODEL_NAME = "llama3:8b"          # <- the model I run with Ollama
OLLAMA_URL = "http://localhost:11434/api/chat"
DB_PATH_GLOBAL = None

DB_PATH_GLOBAL = None  # set once in __main__ so db() uses the same absolute path

def set_db_path(db_path):
    """Remember the absolute DB path for all future connections."""
    global DB_PATH_GLOBAL
    DB_PATH_GLOBAL = db_path

def get_conversation(session_id, limit=200):
    """Fetch messages in order for debugging or showing history."""
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT id, role, content, reply_to_message_id, turn_index, created_at
        FROM messages
        WHERE session_id = ?
        ORDER BY turn_index ASC, id ASC
        LIMIT ?
    """, (session_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

def resolve_paths(): #Makes sure the paths to the database and schema are correct
    here = pathlib.Path(__file__).resolve().parent
    db_path = (here / DB_NAME).resolve()
    schema_path = (here / SCHEMA_NAME).resolve()
    return db_path, schema_path

def init_db(db_path, schema_path):
    sql = pathlib.Path(schema_path).read_text(encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(sql)
    conn.commit()
    conn.close()

def db():
    # Always connect to the resolved absolute path set in __main__
    global DB_PATH_GLOBAL
    if DB_PATH_GLOBAL is None:
        # Fallback to local file name if __main__ didn't set it (e.g., interactive use)
        DB_PATH_GLOBAL = pathlib.Path("paie_project.db").resolve()
    conn = sqlite3.connect(str(DB_PATH_GLOBAL))
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def create_session(title="New chat"): # Inserts a new session into the database
    conn = db(); cur = conn.cursor()
    cur.execute("INSERT INTO sessions (title) VALUES (?)", (title,))
    sid = cur.lastrowid
    conn.commit(); conn.close()
    return sid

def next_turn_index(session_id): # Creates an index for the next turn in the session to make keeping order easier
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(turn_index), -1) + 1 FROM messages WHERE session_id = ?", (session_id,))
    t = cur.fetchone()[0]
    conn.close()
    return t


def add_user_message(session_id, content): # Adds a user message to the session
    t = next_turn_index(session_id)
    conn = db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (session_id, role, content, turn_index, meta_json)
        VALUES (?, 'user', ?, ?, ?)
    """, (session_id, content, t, json.dumps({"source":"ui"})))
    mid = cur.lastrowid
    conn.commit(); conn.close()
    return mid, t

def add_assistant_reply(session_id, content, reply_to_message_id, turn_index, meta=None): #Inserts a new message with the role assistant, keeping the turn index at +1
    conn = db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (session_id, role, content, reply_to_message_id, turn_index, meta_json)
        VALUES (?, 'assistant', ?, ?, ?, ?)
    """, (session_id, content, reply_to_message_id, turn_index + 1, json.dumps(meta or {"model": MODEL_NAME})))
    mid = cur.lastrowid
    conn.commit(); conn.close()
    return mid

def get_session_messages_as_chatml(session_id, max_messages=30): # For fetchning the conversations so far in the session
    conn = db(); cur = conn.cursor()
    cur.execute("""
        SELECT role, content
        FROM messages
        WHERE session_id = ?
        ORDER BY turn_index ASC, id ASC
        LIMIT ?
    """, (session_id, max_messages))
    rows = cur.fetchall(); conn.close()
    return [{"role": r, "content": c} for (r, c) in rows]

def ask_ollama(session_id, user_text, system_prompt=None): # Function to ask Ollama for a response based on the user input and session context
    # 1) store user message
    user_msg_id, t = add_user_message(session_id, user_text)

    # 2) build context (optional: include a system message at the top)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages += get_session_messages_as_chatml(session_id, max_messages=50)

    # 3) call Ollama
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": False
    }
    resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    assistant_text = data["message"]["content"]

    # 4) store assistant reply, linked to that user message
    add_assistant_reply(
        session_id,
        assistant_text,
        reply_to_message_id=user_msg_id,
        turn_index=t,
        meta={"model": MODEL_NAME}
    )

    return assistant_text

if __name__ == "__main__":  # Interactive chat loop Test
    import time

    # 0) Resolve absolute paths and bind DB to them
    db_path, schema_path = resolve_paths()
    set_db_path(db_path)  # <- critical so db() always uses the same file
    print("DB:", db_path)
    print("Schema:", schema_path)

    # 1) Initialize DB schema (safe to run every time)
    try:
        init_db(db_path, schema_path)
        print("Schema applied ✅")
    except Exception as e:
        print("Failed to init DB:", e)
        sys.exit(1)

    # 2) Quick connectivity check to Ollama
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=5)
        r.raise_for_status()
        print("Ollama reachable ✅")
        print("Available models:", [t.get("name") for t in r.json().get("models", [])])
    except Exception as e:
        print("Cannot reach Ollama at http://localhost:11434 — is it running?\n", e)
        sys.exit(1)

    # 3) Start (or restart) a chat session
    def start_session():
        title = input("\nGive this chat a title (or press Enter for 'New chat'): ").strip() or "New chat"
        sid = create_session(title)
        print(f"Session started: {sid} — {title}")
        return sid

    sid = start_session()

    print(
        "\nType your message and press Enter.\n"
        "Commands: /new (new session), /history (show last turns), /title (rename), /exit (quit)\n"
    )

    # Optional: a default system prompt (persona & behavior). Leave None to skip.
    system_prompt = "You are a concise, helpful assistant. Be direct and give practical steps."

    try:
        while True:
            user_text = input("You: ").strip()
            if not user_text:
                continue

            # Commands
            if user_text.lower() == "/exit":
                print("Bye!")
                break

            if user_text.lower() == "/new":
                sid = start_session()
                continue

            if user_text.lower() == "/history":
                rows = get_conversation(sid, limit=200)
                print("\n--- History (latest session) ---")
                for (_id, role, content, reply_to, tix, ts) in rows[-20:]:  # show last 20
                    who = "You" if role == "user" else ("Assistant" if role == "assistant" else "System")
                    print(f"[{tix:>3}] {who}: {content[:200] + ('...' if len(content) > 200 else '')}")
                print("--- End ---\n")
                continue

            if user_text.lower() == "/title":
                new_title = input("New title: ").strip()
                if new_title:
                    conn = db(); cur = conn.cursor()
                    cur.execute("UPDATE sessions SET title=? WHERE id=?", (new_title, sid))
                    conn.commit(); conn.close()
                    print("Title updated")
                continue

            # Normal chat turn: ask model & store reply
            try:
                assistant = ask_ollama(sid, user_text, system_prompt=system_prompt)
                print(f"Assistant: {assistant}\n")
            except requests.HTTPError as http_err:
                print("Ollama HTTP error:", getattr(http_err.response, "text", http_err))
            except Exception as e:
                print("Chat error:", e)
                # small backoff; keeps the loop alive on transient issues
                time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nInterrupted. Bye! 👋")
