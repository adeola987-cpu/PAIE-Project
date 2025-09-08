# app.py
"""
PAIE — Minimal FastAPI server that:
  1) Initializes your SQLite DB using schema.sql
  2) Exposes API routes that call your existing functions in PAIE.py
  3) Serves a browser UI (index.html) that can use HTMX or fetch() to talk to the API

How you'll run it (after saving this file):
  uvicorn app:app --reload
Then open: http://127.0.0.1:8000
"""

from typing import List, Optional
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Import ONLY what we need from your existing PAIE.py
# (All these functions already exist in your uploaded PAIE.py.)
from PAIE import (
    resolve_paths,          # figures out absolute paths to DB and schema
    set_db_path,            # remembers the DB path globally so every connection goes to the same file
    init_db,                # runs schema.sql (safe to run multiple times)
    db,                     # returns a sqlite3 connection bound to the chosen DB path
    create_session,         # inserts a new session and returns its ID
    get_conversation,       # reads messages for a given session (ordered by turn_index)
    ask_ollama,             # stores user message, calls Ollama, stores assistant reply, returns text
    set_system_prompt,      # stores a 'system' message for the session (session-wide instruction)
    get_latest_system_prompt,
    clear_system_prompt,
)

# -----------------------------------------------------------------------------
# FastAPI app setup
# -----------------------------------------------------------------------------

app = FastAPI(
    title="PAIE Local Server",
    description="Local API + UI for your PAIE project (Ollama + SQLite).",
    version="0.1.0",
)

# We will serve:
#   - templates/index.html (the main page)
#   - static/* (your CSS/JS if you decide to separate them)
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Create folders if they don't exist (harmless; makes first run smoother)
TEMPLATES_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# Tell FastAPI where to find templates and static files
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# -----------------------------------------------------------------------------
# App startup: initialize DB (and make sure Ollama URL in PAIE.py is reachable during chat)
# -----------------------------------------------------------------------------
@app.on_event("startup")
def _startup():
    """
    Runs once when the server starts:
      - Resolve absolute paths to the DB and schema.sql
      - Initialize the DB schema (safe if tables already exist)
    """
    db_path, schema_path = resolve_paths()
    set_db_path(db_path)        # critical so every db() call uses the same absolute path
    init_db(db_path, schema_path)
    # We do NOT call Ollama here; we only hit Ollama when /api/chat is invoked.
    # That way, the UI can still load even if Ollama isn't started yet.


# -----------------------------------------------------------------------------
# Helper utilities for sessions (pure Python, uses your existing db() helper)
# -----------------------------------------------------------------------------
def list_sessions() -> List[dict]:
    """
    Return all sessions as a list of dicts.
    Uses your existing db() connection helper (so it's bound to the right file).
    """
    conn = db(); cur = conn.cursor()
    cur.execute("SELECT id, title, created_at FROM sessions ORDER BY created_at DESC, id DESC")
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "created_at": r[2]} for r in rows]


def ensure_session() -> int:
    """
    Make sure there's at least one session to show on the home page.
    If none exist, create one titled 'New chat' and return its ID.
    """
    sessions = list_sessions()
    if sessions:
        return sessions[0]["id"]
    return create_session("New chat")


# -----------------------------------------------------------------------------
# Page routes (server-rendered HTML)
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, session_id: Optional[int] = None):
    """
    Render the main page (index.html).
    We pass a 'session_id' into the template so the page can load that session's messages.
    If no session_id query param is provided, we pick (or create) a default.
    """
    sid = session_id or ensure_session()
    sessions = list_sessions()      # show in a sidebar or dropdown if you like
    system_prompt = get_latest_system_prompt(sid)
    # find the current session's title so the header can show it
    current = next((s for s in sessions if s["id"] == sid), None)
    current_title = current["title"] if current else f"Session {sid}"

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "session_id": sid,
            "session_title": current_title,   # <-- new
            "sessions": sessions,
            "system_prompt": system_prompt or "",
        },
    )


# -----------------------------------------------------------------------------
# API routes (called by your frontend via HTMX or fetch())
# -----------------------------------------------------------------------------

@app.get("/api/sessions")
def api_list_sessions():
    """
    JSON list of sessions. Useful for building a sessions sidebar/dropdown.
    """
    return {"sessions": list_sessions()}


@app.post("/api/sessions")
def api_create_session(title: str = Form("New chat")):
    """
    Create a new session. Returns its ID.
    Using Form() keeps it simple to call from an HTML <form> (HTMX-friendly).
    """
    sid = create_session(title)
    return {"id": sid, "title": title}

@app.post("/api/sessions/rename")
def api_rename_session(session_id: int = Form(...), title: str = Form(...)):
    conn = db(); cur = conn.cursor()
    cur.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
    conn.commit(); conn.close()
    return {"ok": True, "id": session_id, "title": title}


@app.get("/api/messages")
def api_list_messages(session_id: int):
    """
    Return messages in a given session as JSON.
    Good for a React/Vue client OR debugging.
    """
    rows = get_conversation(session_id, limit=500)
    items = [
        {
            "id": r[0],
            "role": r[1],
            "content": r[2],
            "reply_to": r[3],
            "turn_index": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]
    return {"messages": items}


@app.get("/fragment/messages", response_class=HTMLResponse)
def fragment_messages(session_id: int):
    """
    Return a SMALL HTML fragment for the chat area (HTMX will swap this into the page).
    This is useful if you don't want to write custom JS — the server simply returns ready-to-insert HTML.
    """
    rows = get_conversation(session_id, limit=200)

    # Build minimal "bubbles" HTML. You can style these via /static/styles.css
    # role is 'user', 'assistant', or 'system'
    def bubble(role: str, text: str) -> str:
        css = f"msg {role}"
        # Simple HTML-escape: very basic; for production, consider proper escaping/template partials
        safe = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<div class="{css}"><div class="bubble">{safe}</div></div>'

    html = []
    for (_id, role, content, reply_to, tix, created_at) in rows:
        html.append(bubble(role, content))

    return HTMLResponse("".join(html))


@app.post("/api/chat")
def api_chat(session_id: int = Form(...), text: str = Form(...)):
    """
    Core chat endpoint:
      - Stores the user message
      - Calls Ollama using your ask_ollama()
      - Stores the assistant reply
      - Returns the assistant's text as JSON (good for fetch) or you can ignore the body if you're using HTMX+fragment reload.
    """
    # ask_ollama will:
    #   * add the user message (role='user')
    #   * build context (including latest system prompt)
    #   * POST to Ollama (http://localhost:11434/api/chat in your PAIE.py)
    #   * add assistant reply (role='assistant')
    #   * return the assistant text
    try:
        assistant_text = ask_ollama(session_id, text)
        return {"reply": assistant_text}
    except Exception as e:
        # If Ollama isn't running or timeouts happen, we surface a controlled 500 with a helpful message.
        raise HTTPException(status_code=500, detail=f"Ollama call failed: {e}")


@app.post("/api/system")
def api_set_system_prompt(session_id: int = Form(...), prompt: str = Form(...)):
    """
    Set/update the session-wide system prompt ("persona" or instruction).
    It is stored as a 'system' message and automatically included by ask_ollama().
    """
    set_system_prompt(session_id, prompt)
    return {"ok": True}


@app.delete("/api/system")
def api_clear_system_prompt(session_id: int):
    """
    Remove ALL system messages for this session (i.e., reset persona).
    """
    clear_system_prompt(session_id)
    return {"ok": True}


# -----------------------------------------------------------------------------
# Optional: tiny redirect helpers (handy for HTMX forms that expect a page refresh)
# -----------------------------------------------------------------------------

@app.post("/goto")
def goto(session_id: int = Form(...)):
    """
    Small helper that redirects the user to '/' pre-selecting a given session.
    This is useful if you submit a form (e.g., 'New Session') and then want to land on that session's page.
    """
    return RedirectResponse(url=f"/?session_id={session_id}", status_code=303)
