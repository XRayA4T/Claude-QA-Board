#!/usr/bin/env python3
"""
QA Board - a tiny shared question & answer board and document store for a small team and their Claudes.

One file, Python 3.8+ standard library only (no pip installs).

  Run the board (on one machine on the LAN):
      python3 qaboard.py serve --port 8787 [--db qaboard.db] [--key SECRET] [--max-doc-mb 20]

  Things it serves:
      http://HOST:8787/                 web page for humans (questions + documents)
      http://HOST:8787/mcp?who=NAME     MCP endpoint for Claude Code (HTTP transport)
      http://HOST:8787/api/digest?to=N  plain-text "what's waiting for N" (for hooks)
      http://HOST:8787/api/docs         document store (POST raw bytes to upload, see below)

  Upload a file from a shell (e.g. Claude Code running curl):
      curl --data-binary @report.pdf "http://HOST:8787/api/docs?filename=report.pdf&author=dev-claude&key=KEY"

  Documents are stored next to the database in docs/<id>/; every upload of a new version is kept.

  Connect Claude Desktop (stdio -> HTTP bridge, no Node needed):
      python3 qaboard.py bridge "http://HOST:8787/mcp?who=NAME"
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.9.8"
SUPPORTED_PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
STATUSES = ("open", "answered", "resolved")

TEXT_MIMES = {
    "application/json", "application/xml", "application/javascript", "application/x-javascript",
    "application/x-yaml", "application/yaml", "application/x-sh", "application/sql",
    "application/x-python", "application/toml", "image/svg+xml",
}
EXTRA_TYPES = {".md": "text/markdown", ".yaml": "application/yaml", ".yml": "application/yaml",
               ".toml": "application/toml", ".ts": "text/plain", ".tsx": "text/plain",
               ".jsx": "text/plain", ".sql": "application/sql", ".log": "text/plain",
               ".env": "text/plain", ".csv": "text/csv", ".py": "text/x-python"}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
INLINE_SAFE = IMAGE_MIMES | {"image/svg+xml", "application/pdf"}   # served inline (under a sandbox CSP: an SVG
#                                                                  can't run scripts); text/* is served as text/plain


# Built-in icon: a speech bubble with a "Q" (original artwork). To use your own, put favicon.svg,
# favicon.png or favicon.ico in the board's data folder (next to qaboard.db); no restart needed.
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="{ACCENT}"/>
<path d="M14 18a6 6 0 0 1 6-6h24a6 6 0 0 1 6 6v18a6 6 0 0 1-6 6H30l-10 9v-9a6 6 0 0 1-6-6z" fill="#fff"/>
<circle cx="32" cy="27" r="8.5" fill="none" stroke="{ACCENT}" stroke-width="4"/>
<path d="M35 31l6 6" stroke="{ACCENT}" stroke-width="4" stroke-linecap="round"/>
</svg>"""
BRAND_TYPES = {".css": "text/css", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
               ".otf": "font/otf", ".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg",
               ".webp": "image/webp"}
FAVICON_FILES = (("favicon.svg", "image/svg+xml"), ("favicon.png", "image/png"), ("favicon.ico", "image/x-icon"))


def is_text_mime(mime):
    return mime.startswith("text/") or mime in TEXT_MIMES


def guess_mime(filename, head=b""):
    ext = os.path.splitext(filename)[1].lower()
    if ext in EXTRA_TYPES:
        return EXTRA_TYPES[ext]
    mime = mimetypes.guess_type(filename)[0]
    if mime:
        return mime
    try:
        head.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def safe_filename(name):
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return (name or "file")[:120]


# --------------------------------------------------------------------------- storage

class Store:
    def __init__(self, path, max_doc_bytes=20 * 1024 * 1024):
        self.lock = threading.Lock()
        self.max_doc_bytes = max_doc_bytes
        self.attention = None   # {"reason", "by", "ts"} while the session is waiting for a person
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.docdir = os.path.join(os.path.dirname(os.path.abspath(path)), "docs")
        self.tmpdir = os.path.join(self.docdir, ".tmp")
        os.makedirs(self.tmpdir, exist_ok=True)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS questions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL,
                to_whom TEXT NOT NULL DEFAULT 'anyone',
                status TEXT NOT NULL DEFAULT 'open',
                created REAL NOT NULL,
                updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS answers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_id INTEGER NOT NULL REFERENCES questions(id),
                body TEXT NOT NULL,
                author TEXT NOT NULL,
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                question_id INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,
                updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS users(
                name TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'person',
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS document_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER NOT NULL REFERENCES documents(id),
                version INTEGER NOT NULL,
                filename TEXT NOT NULL,
                mime TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                author TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL,
                created REAL NOT NULL);
            """
        )
        self.db.commit()
        self._seed_users()
        # Leftovers from interrupted uploads
        for f in os.listdir(self.tmpdir):
            try:
                os.remove(os.path.join(self.tmpdir, f))
            except OSError:
                pass

    # ---------------------------------------------------------------- questions
    def _answers(self, qid):
        rows = self.db.execute(
            "SELECT id, body, author, created FROM answers WHERE question_id=? ORDER BY id", (qid,)
        ).fetchall()
        return [dict(r) for r in rows]

    def _question(self, row):
        q = dict(row)
        q["to"] = q.pop("to_whom")
        q["answers"] = self._answers(q["id"])
        q["documents"] = [
            {"id": r["id"], "title": r["title"]}
            for r in self.db.execute(
                "SELECT id, title FROM documents WHERE question_id=? AND archived=0 ORDER BY id", (q["id"],))
        ]
        return q

    def post(self, title, body, author, to):
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        now = time.time()
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO questions(title, body, author, to_whom, status, created, updated) "
                "VALUES(?,?,?,?, 'open', ?, ?)",
                (title, (body or "").strip(), clean_name(author), clean_name(to or "anyone"), now, now),
            )
            self.db.commit()
            return self.get(cur.lastrowid)

    def answer(self, qid, body, author):
        body = (body or "").strip()
        if not body:
            raise ValueError("answer text is required")
        now = time.time()
        with self.lock:
            q = self.db.execute("SELECT status FROM questions WHERE id=?", (qid,)).fetchone()
            if not q:
                raise KeyError("no question #%s" % qid)
            self.db.execute(
                "INSERT INTO answers(question_id, body, author, created) VALUES(?,?,?,?)",
                (qid, body, clean_name(author), now),
            )
            new_status = "answered" if q["status"] == "open" else q["status"]
            self.db.execute("UPDATE questions SET status=?, updated=? WHERE id=?", (new_status, now, qid))
            self.db.commit()
            return self.get(qid)

    def set_status(self, qid, status):
        if status not in STATUSES:
            raise ValueError("status must be one of %s" % ", ".join(STATUSES))
        with self.lock:
            cur = self.db.execute(
                "UPDATE questions SET status=?, updated=? WHERE id=?", (status, time.time(), qid)
            )
            self.db.commit()
            if cur.rowcount == 0:
                raise KeyError("no question #%s" % qid)
            return self.get(qid)

    def get(self, qid):
        row = self.db.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if not row:
            raise KeyError("no question #%s" % qid)
        return self._question(row)

    def list(self, status="unresolved", to=None, limit=50):
        sql, args = "SELECT * FROM questions WHERE 1=1", []
        if status == "unresolved":
            sql += " AND status IN ('open','answered')"
        elif status in STATUSES:
            sql += " AND status=?"
            args.append(status)
        elif status not in (None, "", "all"):
            raise ValueError("status must be open, answered, resolved, unresolved or all")
        if to:
            sql += " AND to_whom IN (?, 'anyone')"
            args.append(clean_name(to))
        sql += " ORDER BY updated DESC LIMIT ?"
        args.append(max(1, min(int(limit or 50), 500)))
        with self.lock:
            return [self._question(r) for r in self.db.execute(sql, args).fetchall()]

    # ---------------------------------------------------------------- users (the team list)
    def _seed_users(self):
        """First start with a users table: take the names from people.txt if there is one, and always
        include dev-claude (the Claude Code on the server)."""
        if self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        names = ["dev-claude"]
        roster = os.path.join(os.path.dirname(self.docdir), "people.txt")
        if os.path.isfile(roster):
            with open(roster, encoding="utf-8", errors="replace") as f:
                names += [clean_name(l.split("#", 1)[0]) for l in f if l.split("#", 1)[0].strip()]
        now = time.time()
        for n in dict.fromkeys(names):
            if n not in ("anyone", "unknown"):
                self.db.execute("INSERT OR IGNORE INTO users(name, kind, created) VALUES(?,?,?)",
                                (n, "claude" if n.endswith("claude") else "person", now))
        self.db.commit()

    def users(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT name, kind FROM users ORDER BY name")]

    def add_user(self, name, kind):
        name = clean_name(name)
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}", name) or name in ("anyone", "unknown"):
            raise ValueError("use a short name with letters, digits, - or _ (not 'anyone')")
        if kind not in ("person", "claude"):
            raise ValueError("kind must be person or claude")
        with self.lock:
            self.db.execute("INSERT INTO users(name, kind, created) VALUES(?,?,?) "
                            "ON CONFLICT(name) DO UPDATE SET kind=excluded.kind", (name, kind, time.time()))
            self.db.commit()
        return {"name": name, "kind": kind}

    def remove_user(self, name):
        with self.lock:
            cur = self.db.execute("DELETE FROM users WHERE name=?", (clean_name(name),))
            self.db.commit()
        if not cur.rowcount:
            raise KeyError("no user %s" % name)

    def people(self):
        users = self.users()
        if users:
            return sorted({u["name"] for u in users} | {"anyone"})
        # Without a users list: people.txt, or every name the board has seen.
        roster = os.path.join(os.path.dirname(self.docdir), "people.txt")
        if os.path.isfile(roster):
            names = set()
            with open(roster, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        names.add(clean_name(line))
            return sorted(names | {"anyone"})
        with self.lock:
            rows = self.db.execute(
                "SELECT author AS n FROM questions UNION SELECT to_whom FROM questions "
                "UNION SELECT author FROM answers UNION SELECT author FROM document_versions"
            ).fetchall()
        return sorted({r["n"] for r in rows} | {"anyone"})

    def digest(self, who):
        who = clean_name(who)
        with self.lock:
            waiting = self.db.execute(
                "SELECT id, title, author FROM questions WHERE status='open' "
                "AND to_whom IN (?, 'anyone') AND author<>? ORDER BY id",
                (who, who),
            ).fetchall()
            answered = self.db.execute(
                "SELECT id, title FROM questions WHERE status='answered' AND author=? ORDER BY id",
                (who,),
            ).fetchall()
            recent_docs = self.db.execute(
                "SELECT d.id, d.title, v.author FROM documents d JOIN document_versions v "
                "ON v.doc_id=d.id AND v.version=(SELECT MAX(version) FROM document_versions WHERE doc_id=d.id) "
                "WHERE d.archived=0 AND d.updated>? AND v.author<>? ORDER BY d.updated DESC LIMIT 5",
                (time.time() - 86400, who),
            ).fetchall()
        lines = []
        if waiting:
            lines.append("[QA board] %d open question(s) waiting for %s:" % (len(waiting), who))
            lines += ["  #%d from %s: %s" % (r["id"], r["author"], r["title"]) for r in waiting]
        if answered:
            lines.append("[QA board] %d of %s's questions have answers to review:" % (len(answered), who))
            lines += ["  #%d %s" % (r["id"], r["title"]) for r in answered]
        if lines and recent_docs:
            lines.append("[QA board] documents added or updated in the last day:")
            lines += ["  doc %d by %s: %s" % (r["id"], r["author"], r["title"]) for r in recent_docs]
        if lines:
            lines.append("(Use the qaboard tools: get_question / answer_question / resolve_question / get_document.)")
        return "\n".join(lines)

    # ---------------------------------------------------------------- documents
    def spool(self, chunks):
        """Write an upload to a temp file. Returns (tmp_path, size, sha256, first_bytes)."""
        fd, tmp = tempfile.mkstemp(dir=self.tmpdir)
        h, size, head = hashlib.sha256(), 0, b""
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    size += len(chunk)
                    if size > self.max_doc_bytes:
                        raise ValueError("file is larger than the %d MB limit" % (self.max_doc_bytes // 1048576))
                    if len(head) < 4096:
                        head += chunk[:4096 - len(head)]
                    h.update(chunk)
                    f.write(chunk)
            if size == 0:
                raise ValueError("the file is empty")
            return tmp, size, h.hexdigest(), head
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def add_document(self, spooled, filename, author, title=None, description=None,
                     question_id=None, replace_id=None, note=""):
        tmp, size, sha, head = spooled
        try:
            filename = safe_filename(filename)
            mime = guess_mime(filename, head)
            author = clean_name(author)
            now = time.time()
            with self.lock:
                if question_id not in (None, ""):
                    question_id = int(question_id)
                    if not self.db.execute("SELECT 1 FROM questions WHERE id=?", (question_id,)).fetchone():
                        raise KeyError("no question #%s" % question_id)
                else:
                    question_id = None
                if replace_id not in (None, ""):
                    doc_id = int(replace_id)
                    d = self.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
                    if not d:
                        raise KeyError("no document %s" % doc_id)
                    version = self.db.execute(
                        "SELECT COALESCE(MAX(version),0)+1 FROM document_versions WHERE doc_id=?", (doc_id,)
                    ).fetchone()[0]
                    self.db.execute(
                        "UPDATE documents SET title=?, description=?, question_id=COALESCE(?, question_id), "
                        "archived=0, updated=? WHERE id=?",
                        ((title or "").strip() or d["title"],
                         d["description"] if description is None else description.strip(),
                         question_id, now, doc_id))
                else:
                    cur = self.db.execute(
                        "INSERT INTO documents(title, description, question_id, created, updated) VALUES(?,?,?,?,?)",
                        ((title or "").strip() or filename, (description or "").strip(), question_id, now, now))
                    doc_id, version = cur.lastrowid, 1
                rel = os.path.join(str(doc_id), "v%d-%s" % (version, filename))
                os.makedirs(os.path.join(self.docdir, str(doc_id)), exist_ok=True)
                os.replace(tmp, os.path.join(self.docdir, rel))
                tmp = None
                self.db.execute(
                    "INSERT INTO document_versions(doc_id, version, filename, mime, size, sha256, author, note, path, created) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, version, filename, mime, size, sha, author, (note or "").strip(), rel, now))
                self.db.commit()
                return self._document(doc_id)
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    def _document(self, doc_id, with_versions=True):
        d = self.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not d:
            raise KeyError("no document %s" % doc_id)
        doc = dict(d)
        doc["archived"] = bool(doc["archived"])
        versions = [dict(r) for r in self.db.execute(
            "SELECT version, filename, mime, size, sha256, author, note, created FROM document_versions "
            "WHERE doc_id=? ORDER BY version DESC", (doc_id,))]
        cur = versions[0] if versions else {}
        doc.update({k: cur.get(k) for k in ("version", "filename", "mime", "size", "author")})
        doc["download"] = "/api/docs/%d/download" % doc_id
        doc["view"] = "/api/docs/%d/view" % doc_id
        if with_versions:
            doc["versions"] = versions
        else:
            doc["version_count"] = len(versions)
        return doc

    def get_document(self, doc_id):
        with self.lock:
            return self._document(int(doc_id))

    def list_documents(self, query=None, question_id=None, include_archived=False, limit=100):
        sql, args = "SELECT d.id FROM documents d WHERE 1=1", []
        if not include_archived:
            sql += " AND d.archived=0"
        if question_id not in (None, ""):
            sql += " AND d.question_id=?"
            args.append(int(question_id))
        if query:
            like = "%" + query.strip() + "%"
            sql += (" AND (d.title LIKE ? OR d.description LIKE ? OR EXISTS (SELECT 1 FROM document_versions v "
                    "WHERE v.doc_id=d.id AND (v.filename LIKE ? OR v.author LIKE ?)))")
            args += [like, like, like, like]
        sql += " ORDER BY d.updated DESC LIMIT ?"
        args.append(max(1, min(int(limit or 100), 500)))
        with self.lock:
            return [self._document(r["id"], with_versions=False) for r in self.db.execute(sql, args).fetchall()]

    def doc_file(self, doc_id, version=None):
        with self.lock:
            if version in (None, ""):
                r = self.db.execute(
                    "SELECT * FROM document_versions WHERE doc_id=? ORDER BY version DESC LIMIT 1", (int(doc_id),)
                ).fetchone()
            else:
                r = self.db.execute(
                    "SELECT * FROM document_versions WHERE doc_id=? AND version=?", (int(doc_id), int(version))
                ).fetchone()
        if not r:
            raise KeyError("no document %s%s" % (doc_id, "" if version in (None, "") else " version %s" % version))
        path = os.path.join(self.docdir, r["path"])
        if not os.path.isfile(path):
            raise KeyError("the file for document %s is missing on the server" % doc_id)
        return path, dict(r)

    def set_archived(self, doc_id, archived):
        with self.lock:
            cur = self.db.execute("UPDATE documents SET archived=?, updated=? WHERE id=?",
                                  (1 if archived else 0, time.time(), int(doc_id)))
            self.db.commit()
            if cur.rowcount == 0:
                raise KeyError("no document %s" % doc_id)
            return self._document(int(doc_id))


def clean_attention(reason, url=None, code=None, by="someone"):
    """Validate a needs-attention signal: one-line reason, an optional http(s) link, an optional short code."""
    reason = " ".join(str(reason or "").split())[:200]
    if not reason:
        raise ValueError("give a short reason, e.g. 'OK to deploy to www-dev?'")
    url = str(url or "").strip()
    if url and not re.fullmatch(r"https?://[^\s<>\"']{3,1000}", url):
        raise ValueError("url must be a plain http(s) link")
    code = str(code or "").strip()
    if code and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{1,39}", code):
        raise ValueError("code must be short letters/digits, e.g. ABCD-EFGH")
    att = {"reason": reason, "by": clean_name(by), "ts": time.time()}
    if url:
        att["url"] = url
    if code:
        att["code"] = code
    return att


def clean_name(name):
    name = (name or "").strip().lower().replace(" ", "-")
    return name[:40] or "unknown"


# --------------------------------------------------------------------------- MCP

TOOLS = [
    {
        "name": "check_inbox",
        "description": "Show open questions addressed to you (or to anyone), and your own questions that "
                       "have received answers. Call this at the start of a task and whenever asked "
                       "about the team board.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "post_question",
        "description": "Post a question to the shared team Q&A board. `to` is required: the person or Claude "
                       "who should answer (e.g. 'alice', 'bob', 'dev-claude'). Only use 'anyone' when the user "
                       "has said the question is for anyone; if they haven't said who it's for, ask them before "
                       "posting. Include enough context that the reader can answer without asking back.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "One-line question"},
                "body": {"type": "string", "description": "Details, context, code, file paths"},
                "to": {"type": "string", "description": "Who should answer, e.g. 'dev-claude'. 'anyone' only if the user said so"},
            },
            "required": ["title", "to"],
        },
    },
    {
        "name": "list_questions",
        "description": "List questions on the board, newest activity first, with their answers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string",
                           "enum": ["unresolved", "open", "answered", "resolved", "all"],
                           "description": "Default 'unresolved' (open + answered)"},
                "to": {"type": "string", "description": "Only questions addressed to this name (or anyone)"},
                "limit": {"type": "integer", "description": "Default 20"},
            },
        },
    },
    {
        "name": "get_question",
        "description": "Read one question, all of its answers and the documents attached to it.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
    },
    {
        "name": "answer_question",
        "description": "Post an answer to a question. Be specific; include commands, file paths and "
                       "code where useful.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "answer": {"type": "string"}},
            "required": ["id", "answer"],
        },
    },
    {
        "name": "resolve_question",
        "description": "Mark a question resolved (usually done by whoever asked it, once satisfied). "
                       "Use reopen=true to reopen it.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "reopen": {"type": "boolean"}},
            "required": ["id"],
        },
    },
    {
        "name": "upload_document",
        "description": "Store a document in the team's shared document store, where everyone on the board "
                       "can open it. Pass text files (markdown, code, CSV, logs, JSON) as `content`; small binary "
                       "files as `content_base64`. For large or binary files that exist on disk, uploading with "
                       "curl is cheaper (see the server instructions). Give replace_id to upload a new version of "
                       "an existing document (old versions are kept). Optionally attach it to a question.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "File name with extension, e.g. 'api-notes.md'"},
                "content": {"type": "string", "description": "Text content (UTF-8)"},
                "content_base64": {"type": "string", "description": "Binary content, base64-encoded"},
                "title": {"type": "string", "description": "Human-friendly title. Default: the file name"},
                "description": {"type": "string", "description": "One or two lines on what it is"},
                "question_id": {"type": "integer", "description": "Attach to this question"},
                "replace_id": {"type": "integer", "description": "Upload as a new version of this document"},
                "note": {"type": "string", "description": "What changed in this version"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "list_documents",
        "description": "List documents in the shared store, most recently updated first. Search by words in "
                       "the title, description, file name or author.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "question_id": {"type": "integer", "description": "Only documents attached to this question"},
                "include_archived": {"type": "boolean"},
                "limit": {"type": "integer", "description": "Default 30"},
            },
        },
    },
    {
        "name": "get_document",
        "description": "Read a document: its details and version history, plus the content of text files "
                       "(up to max_chars) or the picture for PNG/JPEG/GIF/WebP images. Other binary files "
                       "(PDF, Office, zip) come back as details and a download path unless as_base64=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "version": {"type": "integer", "description": "Default: latest"},
                "max_chars": {"type": "integer", "description": "Text limit, default 100000"},
                "as_base64": {"type": "boolean", "description": "Return binary content as base64 (max 5 MB)"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "archive_document",
        "description": "Hide a document from the normal list (nothing is deleted). restore=true brings it back.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "restore": {"type": "boolean"}},
            "required": ["id"],
        },
    },
    {
        "name": "needs_attention",
        "description": "Signal that you are blocked until a person does something in your session: e.g. give the "
                       "go-ahead to merge/deploy, sign in (aws sso login, gh auth login: include the sign-in url and code), "
                       "approve a risky step, "
                       "or answer a question you've asked in the terminal. The board shows it to the team (the "
                       "console preview's border pulses red, with your reason). Call it right after you ask, and "
                       "call attention_cleared once you have what you need.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string",
                           "description": "One short line: what you need, e.g. 'OK to deploy to www-dev?'"},
                "url": {"type": "string",
                        "description": "A link the person should open, e.g. the AWS SSO or GitHub device sign-in page"},
                "code": {"type": "string",
                         "description": "A code they must enter or check there, e.g. the AWS SSO / GitHub one-time code"},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "attention_cleared",
        "description": "Clear the needs_attention signal once the person has responded and you can carry on.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

INSTRUCTIONS = (
    "Shared Q&A board and document store for the team (people and their Claude assistants). "
    "Your identity on the board is set by the connection. Use check_inbox at the start of work. "
    "When you need a decision, fact or context only a teammate has, post_question instead of guessing. "
    "Answer questions addressed to you with concrete, verified information. "
    "Put documents the team should be able to read (specs, reports, notes, screenshots) in the document store "
    "with upload_document, and link them from questions or answers as 'doc <id>'. "
    "Posts are Markdown; to show an image (PNG, JPEG, GIF, WebP or SVG) from the document store in a post, "
    "write ![caption](/api/docs/<id>/view) (add ?v=<n> for an older version). "
    "Whenever you stop to wait for a person in your session (a go-ahead to merge or deploy, a sign-in such as "
    "aws sso login, an approval, or an answer to a question you asked in the terminal), call needs_attention "
    "with a one-line reason so the team sees it on the board - for a sign-in, include its url and code - and "
    "call attention_cleared when you can carry on."
)


def _text(s):
    return {"type": "text", "text": s}


def call_tool(store, who, name, args, base_url=""):
    """Returns a list of MCP content blocks."""
    args = args or {}
    if name == "check_inbox":
        text = store.digest(who)
        return [_text(text or "Nothing waiting for %s on the QA board." % who)]
    if name == "post_question":
        to = (args.get("to") or "").strip()
        if not to:
            raise ValueError("not posted: no recipient. Ask the user who this question is for (a person, "
                             "a Claude such as dev-claude, or 'anyone'), then call post_question again with `to`.")
        q = store.post(args.get("title"), args.get("body"), who, to)
        note = ""
        if q["to"] == "anyone":
            note = (" Note: it is addressed to ANYONE, so no one in particular is asked to answer it. If the user "
                    "didn't mean that, tell them; they can ask for it to be re-posted to the right person.")
        return [_text("Posted question #%d to %s.%s\n%s" % (q["id"], q["to"], note, json.dumps(q, indent=1)))]
    if name == "list_questions":
        qs = store.list(args.get("status") or "unresolved", args.get("to"), args.get("limit") or 20)
        return [_text(json.dumps(qs, indent=1) if qs else "No matching questions.")]
    if name == "get_question":
        return [_text(json.dumps(store.get(int(args["id"])), indent=1))]
    if name == "answer_question":
        q = store.answer(int(args["id"]), args.get("answer"), who)
        return [_text("Answer added to #%d.\n%s" % (q["id"], json.dumps(q, indent=1)))]
    if name == "resolve_question":
        q = store.set_status(int(args["id"]), "open" if args.get("reopen") else "resolved")
        return [_text("Question #%d is now %s." % (q["id"], q["status"]))]

    if name == "upload_document":
        if args.get("content_base64"):
            try:
                data = base64.b64decode(args["content_base64"], validate=False)
            except (ValueError, TypeError):
                raise ValueError("content_base64 isn't valid base64")
        elif args.get("content") is not None:
            data = str(args["content"]).encode("utf-8")
        else:
            raise ValueError("give the file as content (text) or content_base64 (binary)")
        spooled = store.spool([data])
        d = store.add_document(spooled, args.get("filename"), who, args.get("title"), args.get("description"),
                               args.get("question_id"), args.get("replace_id"), args.get("note") or "")
        return [_text("Stored doc %d version %d (%s, %d bytes). Everyone on the board can open it at %s%s\n%s" % (
            d["id"], d["version"], d["filename"], d["size"], base_url, d["view"], json.dumps(d, indent=1)))]
    if name == "list_documents":
        docs = store.list_documents(args.get("query"), args.get("question_id"),
                                    bool(args.get("include_archived")), args.get("limit") or 30)
        return [_text(json.dumps(docs, indent=1) if docs else "No matching documents.")]
    if name == "get_document":
        doc = store.get_document(int(args["id"]))
        path, ver = store.doc_file(doc["id"], args.get("version"))
        meta = dict(doc)
        meta["shown_version"] = ver["version"]
        out = [_text(json.dumps(meta, indent=1))]
        mime = ver["mime"]
        if is_text_mime(mime):
            limit = max(1000, min(int(args.get("max_chars") or 100000), 1000000))
            with open(path, "rb") as f:
                raw = f.read(limit * 4 + 4)
            text = raw.decode("utf-8", errors="replace")
            cut = len(text) > limit or ver["size"] > len(raw)
            out.append(_text(text[:limit] + ("\n\n[... truncated; raise max_chars or download %s%s]"
                                             % (base_url, doc["download"]) if cut else "")))
        elif mime in IMAGE_MIMES and ver["size"] <= 5 * 1024 * 1024 and not args.get("as_base64"):
            with open(path, "rb") as f:
                out.append({"type": "image", "mimeType": mime, "data": base64.b64encode(f.read()).decode()})
        elif args.get("as_base64"):
            if ver["size"] > 5 * 1024 * 1024:
                raise ValueError("file is over 5 MB; download it from %s%s" % (base_url, doc["download"]))
            with open(path, "rb") as f:
                out.append(_text(base64.b64encode(f.read()).decode()))
        else:
            out.append(_text("Binary file (%s, %d bytes). Download: %s%s  (or call again with as_base64=true)"
                             % (mime, ver["size"], base_url, doc["download"])))
        return out
    if name == "archive_document":
        d = store.set_archived(int(args["id"]), not args.get("restore"))
        return [_text("Document %d is now %s." % (d["id"], "archived" if d["archived"] else "active"))]
    if name == "needs_attention":
        att = clean_attention(args.get("reason"), args.get("url"), args.get("code"), who)
        store.attention = att
        extra = (" with the link" if att.get("url") else "") + (" and code" if att.get("code") else "")
        return [_text("The board now shows the team that you need someone%s: %s. Call attention_cleared once "
                      "they've responded." % (extra, att["reason"]))]
    if name == "attention_cleared":
        had = store.attention
        store.attention = None
        return [_text("Cleared." if had else "Nothing was set.")]
    raise LookupError("unknown tool: %s" % name)


def handle_rpc(store, who, msg, base_url="", upload_hint=""):
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notification (e.g. notifications/initialized) or a response: nothing to send
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({
            "protocolVersion": asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "qaboard", "version": VERSION},
            "instructions": INSTRUCTIONS + " You are connected as '%s'." % who + upload_hint,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        try:
            content = call_tool(store, who, params.get("name"), params.get("arguments"), base_url)
            return ok({"content": content, "isError": False})
        except LookupError as e:
            if "unknown tool" in str(e):
                return err(-32602, str(e))
            msg = e.args[0] if isinstance(e, KeyError) and e.args else e
            return ok({"content": [_text("Error: %s" % msg)], "isError": True})
        except (ValueError, TypeError, OSError) as e:
            return ok({"content": [_text("Error: %s" % e)], "isError": True})
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return err(-32601, "Method not found: %s" % method)


# --------------------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = "qaboard/" + VERSION
    protocol_version = "HTTP/1.1"

    # -- helpers
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        if body:
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "text" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj))

    def _length(self):
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return -1

    def _body(self, limit):
        n = self._length()
        if n < 0 or n > limit:
            self.close_connection = True
            raise OverflowError("request body too large")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8")) if raw.strip() else {}

    def _chunks(self, n):
        left = n
        while left > 0:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                raise ValueError("upload was cut off")
            left -= len(chunk)
            yield chunk

    def _parsed(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        return u.path.rstrip("/") or "/", q

    def _authorized(self, q):
        key = self.server.key
        return not key or q.get("key") == key or self.headers.get("X-QA-Key") == key

    def _who(self, q):
        return clean_name(q.get("who") or self.headers.get("X-QA-Who") or "claude")

    def _base_url(self):
        host = self.headers.get("Host") or "localhost"
        return "http://%s" % host

    def _favicon(self):
        base = os.path.dirname(self.server.store.docdir)
        for name, mime in FAVICON_FILES:
            f = os.path.join(base, name)
            if os.path.isfile(f) and os.path.getsize(f) < 512 * 1024:
                with open(f, "rb") as fh:
                    return self._send(200, fh.read(), mime, {"Cache-Control": "public, max-age=3600"})
        return self._send(200, FAVICON_SVG.replace("{ACCENT}", self.server.accent), "image/svg+xml",
                          {"Cache-Control": "public, max-age=3600"})

    def _brand_dir(self):
        return os.path.join(os.path.dirname(self.server.store.docdir), "brand")

    def _brand_file(self, rel):
        base = os.path.realpath(self._brand_dir())
        f = os.path.realpath(os.path.join(base, urllib.parse.unquote(rel)))
        ctype = BRAND_TYPES.get(os.path.splitext(f)[1].lower())
        if not ctype or not f.startswith(base + os.sep) or not os.path.isfile(f):
            return self._json(404, {"error": "not found"})
        with open(f, "rb") as fh:
            return self._send(200, fh.read(), ctype, {"Cache-Control": "public, max-age=3600",
                                                      "X-Content-Type-Options": "nosniff"})

    def _page(self):
        page = self.server.page
        if os.path.isfile(os.path.join(self._brand_dir(), "brand.css")):
            v = int(os.path.getmtime(os.path.join(self._brand_dir(), "brand.css")))
            page = page.replace("</style>", '</style>\n<link rel="stylesheet" href="/brand/brand.css?v=%d">' % v, 1)
        return page

    def _send_file(self, path, ver, inline, q):
        mime = ver["mime"]
        headers = {"X-Content-Type-Options": "nosniff"}
        fname = ver["filename"]
        quoted = urllib.parse.quote(fname)
        if inline and (mime in INLINE_SAFE or is_text_mime(mime)):
            ctype = mime if mime in INLINE_SAFE else "text/plain; charset=utf-8"
            headers["Content-Disposition"] = "inline; filename*=UTF-8''%s" % quoted
            if mime != "application/pdf":   # Chrome's PDF viewer doesn't run in a sandboxed response
                headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
        else:
            ctype = mime if not is_text_mime(mime) else "application/octet-stream"
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''%s" % quoted
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, max-age=0")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # -- verbs
    def do_GET(self):
        path, q = self._parsed()
        if path in ("/favicon.ico", "/favicon.svg", "/favicon.png"):   # browsers ask without the key
            return self._favicon()
        if path.startswith("/brand/"):   # the board's look: stylesheet, fonts, logo (no key needed)
            return self._brand_file(path[len("/brand/"):])
        if not self._authorized(q):
            return self._send(401, "Missing or wrong key. Add ?key=... to the URL.", "text/plain")
        store = self.server.store
        try:
            if path == "/":
                return self._send(200, self._page(), "text/html")
            if path == "/health":
                return self._json(200, {"ok": True, "version": VERSION})
            if path == "/api/questions":
                return self._json(200, {"questions": store.list(q.get("status", "unresolved"), q.get("to"), q.get("limit", 100))})
            if path.startswith("/api/questions/"):
                return self._json(200, store.get(int(path.split("/")[3])))
            if path == "/api/people":
                return self._json(200, {"people": store.people()})
            if path == "/api/users":
                return self._json(200, {"users": store.users()})
            if path == "/api/live":
                live = dict(getattr(self.server, "live", None) or {"ts": 0})
                live["attention"] = store.attention
                if live.get("text") and q.get("lines", "").isdigit():
                    n = int(q["lines"])
                    live["text"] = "\n".join(live["text"].split("\n")[-n:]) if n else ""
                return self._json(200, live)
            if path == "/api/digest":
                return self._send(200, store.digest(q.get("to") or self._who(q)), "text/plain")
            if path == "/api/docs":
                return self._json(200, {
                    "documents": store.list_documents(q.get("q"), q.get("question"), q.get("archived") == "1",
                                                      q.get("limit", 200)),
                    "max_mb": store.max_doc_bytes // 1048576})
            parts = path.split("/")
            if len(parts) >= 4 and parts[1:3] == ["api", "docs"]:
                doc_id = int(parts[3])
                if len(parts) == 4:
                    return self._json(200, store.get_document(doc_id))
                if len(parts) == 5 and parts[4] in ("download", "view"):
                    fpath, ver = store.doc_file(doc_id, q.get("v"))
                    return self._send_file(fpath, ver, parts[4] == "view", q)
            if path == "/mcp":
                return self._send(405, "", extra={"Allow": "POST"})
            return self._json(404, {"error": "not found"})
        except KeyError as e:
            return self._json(404, {"error": str(e).strip("'\"")})
        except ValueError as e:
            return self._json(400, {"error": str(e)})

    def do_POST(self):
        path, q = self._parsed()
        if not self._authorized(q):
            self.close_connection = True
            return self._json(401, {"error": "missing or wrong key"})
        store = self.server.store
        try:
            if path == "/api/live":   # console snapshot from the board watcher
                d = self._body(2 * 1024 * 1024)
                text = str(d.get("text") or "")[-400000:]
                prev = getattr(self.server, "live", None) or {}
                now = time.time()
                self.server.live = {"ts": now, "changed": prev.get("changed", now) if prev.get("text") == text else now,
                                    "text": text, "busy": bool(d.get("busy")), "prompt": bool(d.get("prompt")),
                                    "title": str(d.get("title") or "")[:80]}
                return self._json(200, {"ok": True})
            if path == "/api/attention":   # set or clear "the session needs someone" (page, watcher, scripts)
                d = self._body(64 * 1024)
                if d.get("clear"):
                    store.attention = None
                else:
                    store.attention = clean_attention(d.get("reason"), d.get("url"), d.get("code"),
                                                      d.get("by") or "someone")
                return self._json(200, {"attention": store.attention})
            if path == "/api/docs":   # raw upload: body = file bytes, details in the query string
                n = self._length()
                if n <= 0:
                    return self._json(400, {"error": "send the file as the request body"})
                if n > store.max_doc_bytes:
                    self.close_connection = True
                    return self._json(413, {"error": "file is larger than the %d MB limit" % (store.max_doc_bytes // 1048576)})
                spooled = store.spool(self._chunks(n))
                author = q.get("author") or q.get("who") or self.headers.get("X-QA-Who") or "unknown"
                d = store.add_document(spooled, q.get("filename") or "upload", author, q.get("title"),
                                       q.get("description"), q.get("question"), q.get("replace"), q.get("note", ""))
                return self._json(201, d)
            data = self._body(store.max_doc_bytes * 4 // 3 + 1024 * 1024 if path == "/mcp" else 1024 * 1024)
        except OverflowError as e:
            return self._json(413, {"error": str(e)})
        except KeyError as e:
            return self._json(404, {"error": str(e).strip("'\"")})
        except (ValueError, UnicodeDecodeError) as e:
            if path == "/mcp":
                return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            return self._json(400, {"error": str(e)})
        try:
            if path == "/mcp":
                who = self._who(q)
                base = self._base_url()
                keyq = ("?key=" + urllib.parse.quote(q["key"])) if q.get("key") else ""
                hint = (" This is the %s board." % self.server.brand +
                        " To upload a file from disk without pasting it, run: curl --data-binary @FILE "
                        "'%s/api/docs%s%sfilename=NAME&author=%s' (add &title=..., &question=ID or &replace=DOC_ID)."
                        % (base, keyq, "&" if keyq else "?", who))
                if isinstance(data, list):
                    out = [r for r in (handle_rpc(store, who, m, base, hint) for m in data) if r]
                else:
                    out = handle_rpc(store, who, data, base, hint)
                if not out:
                    return self._send(202)
                return self._json(200, out)
            if path == "/api/questions":
                return self._json(201, store.post(data.get("title"), data.get("body"), data.get("author"), data.get("to")))
            parts = path.split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "questions"]:
                qid = int(parts[3])
                if parts[4] == "answers":
                    return self._json(201, store.answer(qid, data.get("body"), data.get("author")))
                if parts[4] == "status":
                    return self._json(200, store.set_status(qid, data.get("status")))
            if path == "/api/users":
                return self._json(201, store.add_user(data.get("name"), data.get("kind") or "person"))
            if len(parts) == 5 and parts[1:3] == ["api", "users"] and parts[4] == "delete":
                store.remove_user(urllib.parse.unquote(parts[3]))
                return self._json(200, {"ok": True})
            if len(parts) == 5 and parts[1:3] == ["api", "docs"] and parts[4] == "archive":
                return self._json(200, store.set_archived(int(parts[3]), data.get("archived", True)))
            return self._json(404, {"error": "not found"})
        except KeyError as e:
            return self._json(404, {"error": str(e).strip("'\"")})
        except ValueError as e:
            return self._json(400, {"error": str(e)})

    def do_DELETE(self):
        self._send(405, "", extra={"Allow": "GET, POST"})


def lighten(hex_colour, amount=0.45):
    """A lighter shade of the accent for dark mode."""
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    r, g, b = (round(c + (255 - c) * amount) for c in (r, g, b))
    return "#%02x%02x%02x" % (r, g, b)


def render_page(brand, accent, terminal_url=""):
    import html
    if terminal_url and not re.fullmatch(r"https?://[^\s<>\"']+", terminal_url):
        terminal_url = ""
    return (PAGE.replace("{VERSION}", VERSION).replace("{BRAND_HTML}", html.escape(brand))
                .replace("{TERM_JSON}", json.dumps(terminal_url).replace("<", "\\u003c"))
                .replace("{BRAND_JSON}", json.dumps(brand))
                .replace("{ACCENT_DARK}", lighten(accent))
                .replace("{ACCENT}", accent))


def serve(args):
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    srv.store = Store(args.db, int(args.max_doc_mb * 1024 * 1024))
    srv.key = args.key
    srv.brand = args.name
    srv.accent = args.accent if re.fullmatch(r"#[0-9a-fA-F]{6}", args.accent or "") else "#3949ab"
    srv.page = render_page(srv.brand, srv.accent, args.terminal_url)
    sys.stderr.write("QA board v%s on http://%s:%d  (db: %s, docs: %s, max %s MB%s)\n" % (
        VERSION, args.host, args.port, args.db, srv.store.docdir, args.max_doc_mb,
        ", key required" if args.key else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------- stdio bridge

def bridge(args):
    """Relay newline-delimited JSON-RPC between stdin/stdout (Claude Desktop) and the board's /mcp."""
    url = args.url
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # LAN: never use a proxy
    session = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(url, data=line.encode("utf-8"), headers=headers, method="POST")
        try:
            with opener.open(req, timeout=120) as r:
                session = r.headers.get("Mcp-Session-Id") or session
                body = r.read().decode("utf-8").strip()
            if body:
                sys.stdout.write(json.dumps(json.loads(body), separators=(",", ":")) + "\n")
                sys.stdout.flush()
        except Exception as e:  # board down / unreachable: answer requests so the client doesn't hang
            sys.stderr.write("qaboard bridge: %s\n" % e)
            if isinstance(msg, dict) and msg.get("id") is not None:
                sys.stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32000, "message": "QA board unreachable at %s: %s" % (url, e)},
                }) + "\n")
                sys.stdout.flush()


# --------------------------------------------------------------------------- web page

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{BRAND_HTML} · Q&amp;A Board</title>
<link rel="icon" href="/favicon.svg">
<style>
:root{--bg:#f6f6f3;--card:#fff;--fg:#1c1c1a;--muted:#6b6b66;--line:#e2e2dc;--accent:{ACCENT};
--open:#c2410c;--ans:#15803d;--res:#6b7280;--field:#fff;--drop:#eef0fb}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--card:#1f1f1d;--fg:#ececE8;--muted:#9a9a93;
--line:#34342f;--accent:{ACCENT_DARK};--open:#fb923c;--ans:#4ade80;--res:#9ca3af;--field:#191917;--drop:#23253a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,Ubuntu,sans-serif}
main{max-width:880px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;margin-bottom:12px}
h1{font-size:20px;margin:0;line-height:1.15}
h1 span{display:block;font-size:13px;font-weight:500;color:var(--muted);letter-spacing:.02em}
.brand{display:flex;gap:10px;align-items:center}
.brand img{border-radius:8px;flex:0 0 auto}
h1 .ver{font-size:11px;font-weight:500;letter-spacing:0;text-transform:none;opacity:.75;margin-left:4px}
header{border-top:4px solid var(--accent);padding-top:12px}
.me{display:flex;gap:6px;align-items:center;color:var(--muted);font-size:14px}
input,textarea,select{font:inherit;color:var(--fg);background:var(--field);border:1px solid var(--line);
border-radius:8px;padding:8px 10px;width:100%}
textarea{min-height:80px;resize:vertical}
.me input,.me select{width:160px;padding:5px 8px}
.forwhom{align-items:center}.forlabel{flex:0 0 auto!important}
.topills{display:flex;flex-wrap:wrap;gap:6px;flex:1 1 300px}
.topill{border-radius:99px;padding:4px 12px;font-size:14px}
.topill.on{background:var(--accent);border-color:var(--accent);color:#fff}
.topill .cl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;background:var(--drop);color:var(--accent);border-radius:99px;padding:1px 6px;margin-right:6px;vertical-align:1px}
.topill.on .cl{background:rgba(255,255,255,.25);color:#fff}
.topill.any{border-style:dashed}
.urow{display:flex;align-items:center;gap:12px}.urow .main{flex:1;min-width:0}
.av{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;font-weight:700;background:var(--drop);color:var(--accent);flex:0 0 auto}
.av.cl{background:var(--accent);color:#fff;font-size:18px}
.ubadge{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;border:1px solid var(--line);border-radius:99px;padding:1px 8px;color:var(--muted);margin-left:4px}
.ubadge.cl{border-color:var(--accent);color:var(--accent)}
.rmuser.danger{border-color:#e5484d;color:#e5484d}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;
padding:7px 14px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
@media (prefers-color-scheme:dark){button.primary{color:#111}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px}
details.new summary{cursor:pointer;font-weight:600}
.row{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}.row>*{flex:1 1 200px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 10px}
.tabs button{padding:4px 12px;font-size:14px}.tabs button.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.views{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:14px}
.views button{border:0;border-bottom:3px solid transparent;border-radius:0;background:none;padding:8px 14px;font-weight:600;color:var(--muted)}
.views button.on{color:var(--fg);border-bottom-color:var(--accent)}
.q h3{margin:0;font-size:16px;cursor:pointer}
.meta{color:var(--muted);font-size:13px;margin-top:2px}
.pill{display:inline-block;font-size:12px;font-weight:600;border-radius:99px;padding:0 8px;margin-right:6px;border:1px solid}
.open{color:var(--open)}.answered{color:var(--ans)}.resolved{color:var(--res)}
.body{white-space:pre-wrap;margin:10px 0 0;overflow-wrap:anywhere}
.md{white-space:normal;line-height:1.55}
.md p{margin:0 0 8px}
.md ul,.md ol{margin:2px 0 8px;padding-left:22px}
.md li{margin:2px 0}
.md li.sub{margin-left:20px;list-style-type:circle}
.md h4{margin:12px 0 4px;font-size:15px}
.md h4.h1{font-size:17px}
.md code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em;background:var(--drop);padding:1px 5px;border-radius:5px;overflow-wrap:anywhere}
.md pre{background:var(--drop);padding:8px 10px;border-radius:8px;overflow-x:auto;margin:4px 0 8px}
.md pre code{background:none;padding:0;white-space:pre}
.md a.mdimg{display:block;margin:6px 0 8px;width:max-content;max-width:100%}
.md a.mdimg img{display:block;max-width:100%;max-height:480px;border:1px solid var(--line);border-radius:8px;background:#fff}
.md>:last-child{margin-bottom:0}
.md a{overflow-wrap:anywhere}
.ans{border-left:3px solid var(--line);padding:4px 0 4px 12px;margin-top:12px}
.ans.folded{cursor:pointer;margin-top:6px;padding:2px 0 2px 12px}
.ans.folded .meta{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ans.folded:hover{border-left-color:var(--accent)}
.ans.folded .peek{color:var(--fg);opacity:.75;margin-left:4px}
.ans .chev{display:inline-block;width:12px;text-decoration:none;color:var(--muted)}
.foldbar{font-size:13px;color:var(--muted);margin-top:10px}
.empty{color:var(--muted);text-align:center;padding:30px}
.err{color:var(--open);font-size:14px}
.ok{color:var(--ans);font-size:14px}
.forme{outline:2px solid var(--open);outline-offset:-1px}
.qfiles{align-items:center;gap:10px;border:1px dashed var(--line);border-radius:8px;padding:6px 10px}
.qfiles>*{flex:0 1 auto}.qfiles.over{background:var(--drop);border-color:var(--accent)}
.attachbtn{cursor:pointer;border:1px solid var(--line);border-radius:8px;padding:4px 12px;background:var(--card);font-size:14px;white-space:nowrap}
.livebox{border-radius:10px;overflow:hidden;border:1px solid var(--line)}
#livepre{margin:0;max-height:70vh;overflow:auto;background:#16141a;color:#e8e6ea;padding:12px 14px;
 font:12.5px/1.32 ui-monospace,SFMono-Regular,Menlo,"DejaVu Sans Mono",monospace;white-space:pre}
.topbar{position:fixed;top:0;left:0;right:0;margin:0 auto;max-width:880px;z-index:20;background:var(--bg);padding:12px 16px 0}
/* the header and tabs are pinned (position:fixed, so nothing on the page can push them off the top);
   the page starts below them: --toph is the pinned bar's measured height */
main{padding-top:calc(var(--toph,160px) + 8px)}
html{scroll-padding-top:calc(var(--toph,160px) + 8px)}
.views{margin-bottom:0;background:var(--bg)}
main>section{padding-top:14px}
.brand{margin-right:auto}
.qdropzone{margin-top:8px;padding:12px}
.led{display:inline-block;width:10px;height:10px;border-radius:50%;background:#0e0e0e;border:1px solid rgba(160,160,160,.4)}
.led.flash{animation:ledflash .25s ease-out}
@keyframes ledflash{from{background:#39d96b;box-shadow:0 0 9px 2px rgba(57,217,107,.9)}to{background:#0e0e0e;box-shadow:none}}
.mini.alert{animation:attnpulse 1.2s ease-in-out infinite;border-color:#e5484d}
@keyframes attnpulse{0%,100%{box-shadow:0 0 0 1px #e5484d,0 0 4px 1px rgba(229,72,77,.35)}50%{box-shadow:0 0 0 3px #e5484d,0 0 14px 4px rgba(229,72,77,.75)}}
@media (prefers-reduced-motion:reduce){.mini.alert{animation:none;box-shadow:0 0 0 3px #e5484d}.led.flash{animation-duration:.01s}}
.livehead{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin:0 0 14px}
#liveattn{margin-bottom:14px}
.livehead #livestate{margin:0;line-height:28px}
.livehead #openterm{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;height:28px;padding:0 12px;
 font-size:13px;font-weight:600;line-height:1;letter-spacing:0}
@media (prefers-color-scheme:dark){.livehead #openterm{color:#16121b}}
.termshade{position:fixed;inset:0;z-index:99;background:rgba(20,20,24,.55);backdrop-filter:blur(1px)}
body.termopen{overflow:hidden}
.termwin{position:fixed;z-index:100;display:flex;flex-direction:column;background:#202124;border-radius:10px;overflow:hidden;
 box-shadow:0 18px 50px rgba(0,0,0,.45),0 0 0 1px rgba(255,255,255,.08);min-width:360px;min-height:220px}
.termwin.max{left:8px!important;top:8px!important;width:calc(100vw - 16px)!important;height:calc(100vh - 16px)!important}
.termbar{display:flex;align-items:center;gap:8px;height:38px;padding:0 6px 0 10px;background:#2b2c30;cursor:move;user-select:none;flex:0 0 auto}
.termtab{display:flex;align-items:center;gap:7px;background:#3c3d42;color:#e8eaed;font:13px/1 system-ui,sans-serif;
 padding:7px 12px;border-radius:8px 8px 0 0;margin-top:8px;align-self:stretch;max-width:46%;overflow:hidden;white-space:nowrap}
.termtab span{overflow:hidden;text-overflow:ellipsis}
.termurl{flex:1;min-width:60px;background:#18191b;color:#bdc1c6;border:0;border-radius:99px;padding:5px 12px;font:12px/1 system-ui,sans-serif;
 text-align:left;cursor:text;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.termbtns{display:flex;gap:2px;flex:0 0 auto}
.termbtns a,.termbtns button{width:32px;height:28px;display:grid;place-items:center;border:0;border-radius:6px;background:transparent;
 color:#e8eaed;font:15px/1 system-ui,sans-serif;text-decoration:none;cursor:pointer;padding:0}
.termbtns a:hover,.termbtns button:hover{background:#3c3d42}
.termbtns .x:hover{background:#e5484d;color:#fff}
#termframe{flex:1;border:0;width:100%;background:#16141a}
.termwin.dragging #termframe{pointer-events:none}
.termresize{position:absolute;right:0;bottom:0;width:18px;height:18px;cursor:nwse-resize;
 background:linear-gradient(135deg,transparent 50%,rgba(255,255,255,.35) 50%,rgba(255,255,255,.35) 60%,transparent 60%,transparent 70%,rgba(255,255,255,.35) 70%,rgba(255,255,255,.35) 80%,transparent 80%)}
.termwin.max .termresize{display:none}
.attnacts{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.attnlink{background:#e5484d;color:#fff!important;text-decoration:none;font-weight:600;border-radius:8px;padding:6px 12px}
.attncode b{font-family:ui-monospace,Menlo,monospace;letter-spacing:.06em;font-size:15px;user-select:all}
.attn{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:10px;padding:10px 12px;border-radius:8px;border:1px solid #e5484d;background:color-mix(in srgb,#e5484d 12%,var(--card));font-size:14px}
.mini{position:relative;width:210px;height:62px;padding:0;overflow:hidden;background:#16141a;border:1px solid var(--line);border-radius:8px;cursor:pointer;flex:0 0 auto}
.mini:hover{border-color:var(--accent)}
.mini pre{position:absolute;left:6px;right:6px;bottom:4px;margin:0;font:5.5px/1.25 ui-monospace,Menlo,monospace;color:#e8e6ea;white-space:pre;overflow:hidden;text-align:left;pointer-events:none}
.mini .led{position:absolute;top:5px;right:5px}
@media (max-width:640px){.mini{display:none}}
.warnbox{margin-top:10px;border-left:3px solid var(--open);background:var(--drop);padding:10px 12px;border-radius:0 8px 8px 0;font-size:14px}
.att{margin-top:8px;font-size:14px}.att a{margin-right:10px}
a{color:var(--accent)}
.drop{border:2px dashed var(--line);border-radius:12px;padding:16px;text-align:center;color:var(--muted)}
.drop.over{background:var(--drop);border-color:var(--accent)}
.doc{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
.doc .main{flex:1 1 300px;min-width:0}
.doc h3{margin:0;font-size:16px;overflow-wrap:anywhere}
.doc .acts{display:flex;gap:6px;flex-wrap:wrap}
.doc .acts a,.doc .acts button{font-size:13px;padding:4px 10px;border:1px solid var(--line);border-radius:8px;text-decoration:none;background:var(--card);color:var(--fg)}
.badge{font-size:12px;border:1px solid var(--line);border-radius:6px;padding:0 6px;color:var(--muted);margin-left:6px}
.vers{font-size:13px;color:var(--muted);margin-top:6px}
[hidden]{display:none!important}
</style></head>
<body><main>
<div class="topbar">
<header><div class="brand"><img src="/favicon.svg" alt="" width="34" height="34"><h1>{BRAND_HTML}<span>Q&amp;A Board <small class="ver">v{VERSION}</small></span></h1></div>
<button class="mini" id="mini" hidden title="Claude's console - open the Live view" aria-label="Open the Live view"><pre id="minipre"></pre><span class="led"></span></button>
<label class="me">I am <select id="me"><option value="">choose…</option></select></label></header>
<nav class="views" id="views"><button data-v="questions">Questions</button><button data-v="docs">Documents</button><button data-v="live">Live</button><button data-v="users">Users</button></nav>
</div>

<section id="v-questions">
<details class="card new" id="newbox"><summary>Ask a question</summary>
<div class="row"><input id="title" placeholder="Question (one line)"></div>
<div class="row"><textarea id="qbody" placeholder="Details, context, links, code"></textarea></div>
<div class="drop qdropzone" id="qdrop">Drop files here, or <label style="color:var(--accent);cursor:pointer;text-decoration:underline">choose files<input type="file" id="qfile" multiple hidden></label>
<div class="meta" id="qfilelist">They're attached to the question when you post it.</div></div>
<div class="row forwhom"><span class="meta forlabel">For whom?</span><div class="topills" id="topills"></div><input type="hidden" id="to">
<button class="primary" id="post" style="flex:0 0 auto">Post</button></div>
<div class="warnbox" id="anywarn" hidden>
  <b>This question is for anyone.</b> Nobody in particular will be asked to answer it, and Claudes only pick it up if they choose to. Is that right?
  <div class="row" style="justify-content:flex-start"><button class="primary" id="postany" style="flex:0 0 auto">Post to anyone</button>
  <button id="chooseto" style="flex:0 0 auto">Choose someone</button></div>
</div>
<div class="err" id="perr"></div></details>
<div class="tabs" id="tabs"></div>
<div id="list"></div>
</section>

<section id="v-users" hidden>
<div class="card"><div class="row" style="margin-top:0"><input id="newuser" placeholder="Name, e.g. alice or bob-claude" autocomplete="off">
<select id="newkind" style="flex:0 0 130px"><option value="person">Person</option><option value="claude">Claude</option></select>
<button class="primary" id="adduser" style="flex:0 0 auto">Add user</button></div>
<div class="meta" id="usermsg" style="margin-top:6px">People choose themselves next to "I am". Claudes are listed first under "For whom?".</div></div>
<div id="userlist"></div>
</section>

<section id="v-live" hidden>
<div class="attn" id="liveattn" hidden><span id="liveattntext"></span>
  <span class="attnacts"><a class="attnlink" id="liveopen" target="_blank" rel="noopener noreferrer" hidden>Open link &#8599;</a>
  <span class="attncode" id="livecode" hidden>Code <b id="livecodetext"></b> <button id="livecopy">Copy</button></span>
  <button id="liveclear" hidden>Mark as handled</button></span></div>
<div class="livehead"><div class="meta" id="livestate">Loading…</div>
<button id="openterm" class="primary" title="Open the Claude Code web terminal in a window over the board">Open terminal</button></div>
<div class="livebox" id="livebox" hidden><pre id="livepre"></pre></div>
<div class="meta" style="margin-top:8px">Read-only copy of the Claude Code console on the server, refreshed every 2 seconds. To type to Claude, use the web terminal.</div>
</section>

<section id="v-docs" hidden>
<div class="card">
  <div class="drop" id="drop">Drop files here, or <label style="color:var(--accent);cursor:pointer;text-decoration:underline">choose files<input type="file" id="file" multiple hidden></label>
    <div class="meta" id="maxmb"></div></div>
  <div class="row"><input id="dtitle" placeholder="Title (optional; default: file name)">
  <input id="dq" placeholder="Attach to question # (optional)" inputmode="numeric" style="flex:0 1 220px"></div>
  <div class="row"><input id="ddesc" placeholder="Description (optional)"></div>
  <div id="uploadmsg" class="meta"></div>
</div>
<div class="row" style="margin-bottom:10px"><input id="dsearch" placeholder="Search documents" type="search">
<label class="meta" style="flex:0 0 auto;display:flex;gap:6px;align-items:center"><input type="checkbox" id="darch" style="width:auto"> show archived</label></div>
<div id="docs"></div>
<input type="file" id="verfile" hidden>
</section>
<datalist id="people"></datalist>
<div class="termshade" id="termshade" hidden title="Close the terminal window to use the board"></div>
<div class="termwin" id="termwin" hidden role="dialog" aria-label="Claude Code terminal">
 <div class="termbar" id="termbar">
  <div class="termtab"><img src="/favicon.svg" alt="" width="14" height="14"><span>{BRAND_HTML} &middot; Claude Code</span></div>
  <button class="termurl" id="termurl" title="Change the terminal's address"></button>
  <span class="termbtns"><a id="termnew" target="_blank" rel="noopener" title="Open in its own browser tab">&#8599;</a>
  <button id="termmax" title="Maximise">&#9634;</button><button id="termclose" class="x" title="Close (Claude keeps running)">&#10005;</button></span>
 </div>
 <iframe id="termframe" title="Claude Code terminal" src="about:blank" allow="clipboard-read; clipboard-write"></iframe>
 <div class="termresize" id="termresize" title="Drag to resize"></div>
</div>
</main>
<script>
// keep the page clear of the pinned header, whatever its height (brand, mini console, narrow windows)
(()=>{const t=document.querySelector('.topbar'),set=()=>document.documentElement.style.setProperty('--toph',t.offsetHeight+'px');set();if(window.ResizeObserver)new ResizeObserver(set).observe(t);addEventListener('resize',set)})();
const KEY=new URLSearchParams(location.search).get('key');
const BRAND={BRAND_JSON};
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function store(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){return null}}
function url(path){const u=new URL(path,location.origin);if(KEY)u.searchParams.set('key',KEY);return u}
async function api(path,body){
 const r=await fetch(url(path),body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
 const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.status);return j}
function ago(t){const s=Date.now()/1000-t;if(s<60)return'just now';if(s<3600)return Math.floor(s/60)+'m ago';
 if(s<86400)return Math.floor(s/3600)+'h ago';return new Date(t*1000).toLocaleDateString()}
function size(n){return n<1024?n+' B':n<1048576?(n/1024).toFixed(0)+' KB':(n/1048576).toFixed(1)+' MB'}
const me=()=>($('#me').value.trim().toLowerCase().replace(/\s+/g,'-')||'');
$('#me').onchange=()=>{store('qa-me',$('#me').value);refresh()};

// ---- views
let view=['#docs','#live','#users'].includes(location.hash)?location.hash.slice(1):(store('qa-view')||'questions');
function showView(v){view=v;store('qa-view',v);
 document.querySelectorAll('#views button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));
 $('#v-questions').hidden=v!=='questions';$('#v-docs').hidden=v!=='docs';$('#v-live').hidden=v!=='live';$('#v-users').hidden=v!=='users';refresh()}
$('#views').onclick=e=>{const b=e.target.closest('button');if(b&&b.dataset.v)showView(b.dataset.v)};
$('#mini').onclick=()=>{showView('live');window.scrollTo(0,0)};
function refresh(){loadUsers();view==='docs'?loadDocs():view==='live'?loadLive():view==='users'?null:load()}
setInterval(()=>{if(!document.hidden)loadLive()},2000);

// ---- live console (sent by the board watcher on the server, when it runs with --live)
const ANSI16=['#1e1e1e','#e5534b','#57ab5a','#c69026','#539bf5','#b083f0','#39c5cf','#d0d0d0',
 '#636e7b','#ff938a','#6bc46d','#daaa3f','#6cb6ff','#dcbdfb','#56d4dd','#ffffff'];
function ansi256(n){if(n<16)return ANSI16[n];if(n>=232){const v=8+(n-232)*10;return `rgb(${v},${v},${v})`}
 n-=16;const c=[0,95,135,175,215,255];return `rgb(${c[Math.floor(n/36)]},${c[Math.floor(n/6)%6]},${c[n%6]})`}
function ansiToHtml(text){
 let st={},out='',open=false;const rx=/\x1b\[([0-9;]*)m/g;let last=0,m;
 const style=()=>{const s=[];if(st.fg)s.push('color:'+st.fg);if(st.bg)s.push('background:'+st.bg+';padding:.08em 0');if(st.b)s.push('font-weight:700');
  if(st.d)s.push('opacity:.6');if(st.i)s.push('font-style:italic');if(st.u)s.push('text-decoration:underline');if(st.inv)s.push('filter:invert(1)');return s.join(';')};
 const emit=t=>{if(!t)return;t=esc(t.replace(/\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b./g,''));const s=style();out+=s?`<span style="${s}">${t}</span>`:t};
 while((m=rx.exec(text))){emit(text.slice(last,m.index));last=rx.lastIndex;
  const p=m[1]===''?[0]:m[1].split(';').map(Number);
  for(let i=0;i<p.length;i++){const c=p[i];
   if(c===0)st={};else if(c===1)st.b=1;else if(c===2)st.d=1;else if(c===3)st.i=1;else if(c===4)st.u=1;else if(c===7)st.inv=1;
   else if(c===22){st.b=0;st.d=0}else if(c===23)st.i=0;else if(c===24)st.u=0;else if(c===27)st.inv=0;
   else if(c>=30&&c<=37)st.fg=ANSI16[c-30];else if(c>=90&&c<=97)st.fg=ANSI16[c-82];else if(c===39)st.fg=null;
   else if(c>=40&&c<=47)st.bg=ANSI16[c-40];else if(c>=100&&c<=107)st.bg=ANSI16[c-92];else if(c===49)st.bg=null;
   else if((c===38||c===48)&&p[i+1]===5){st[c===38?'fg':'bg']=ansi256(p[i+2]);i+=2}
   else if((c===38||c===48)&&p[i+1]===2){st[c===38?'fg':'bg']=`rgb(${p[i+2]},${p[i+3]},${p[i+4]})`;i+=4}}}
 emit(text.slice(last));return out}
let liveTs=0,liveChanged=0,lastFlash=0;
function agoShort(t){const a=Date.now()/1000-t;return a<5?'just now':a<90?Math.round(a)+'s ago':ago(t)}
// The mini box's light flashes green whenever the screen changes and fades to black in 1/4 s,
// so steady activity shows as a pulse. Its border pulses red when the session needs someone.
function flashLed(){const el=$('#mini .led');if(!el)return;el.classList.remove('flash');void el.offsetWidth;el.classList.add('flash')}
function attentionText(j){
 if(j.attention)return esc(j.attention.reason)+' <span class="meta">('+esc(j.attention.by)+', '+agoShort(j.attention.ts)+')</span>';
 if(j.prompt)return 'Claude is waiting at a question or permission prompt on its screen';
 return ''}
// Polled on every tab: feeds the header's mini screen; the Live tab gets the full text.
async function loadLive(){
 const full=view==='live';
 try{const j=await api('/api/live'+(full?'':'?lines=40'));
  const fresh=j.ts&&Date.now()/1000-j.ts<90;
  const changed=j.ts?(j.changed||j.ts):0;
  if(changed&&changed!==lastFlash){if(lastFlash)flashLed();lastFlash=changed}
  liveChanged=changed;
  const mini=$('#mini');mini.hidden=!j.ts;
  const need=!!(j.attention||(fresh&&j.prompt));
  mini.classList.toggle('alert',need);
  mini.title=need?'Needs you: '+(j.attention?j.attention.reason+(j.attention.url?' (link on the Live view)':''):'Claude is waiting at a prompt on its screen')+' - click to open the Live view':'Claude\'s console (click to open the Live view)';
  if(j.ts)$('#minipre').innerHTML=ansiToHtml((j.text||'').split('\n').slice(-16).join('\n'));
  if(!full)return;
  const box=$('#livebox'),st=$('#livestate'),att=$('#liveattn');
  att.hidden=!need;
  if(need){$('#liveattntext').innerHTML='<b>Needs you:</b> '+attentionText(j);$('#liveclear').hidden=!j.attention;
   const a=j.attention||{},lk=$('#liveopen'),cd=$('#livecode');
   lk.hidden=!a.url;if(a.url){lk.href=a.url;lk.title=a.url}
   cd.hidden=!a.code;if(a.code)$('#livecodetext').textContent=a.code}
  if(!j.ts){st.textContent='Nothing yet. The live view appears when the board watcher on the server runs with --live.';box.hidden=true;return}
  st.innerHTML=(!fresh?'<b class="err">Not updating</b> (last update '+ago(j.ts)+'). The watcher may be stopped.'
   :(j.busy?'<b>Claude is working</b>':'Claude is idle')+' · screen last changed '+agoShort(changed))+(j.title?' · '+esc(j.title):'');
  if(changed!==liveTs){liveTs=changed;const pre=$('#livepre');const atBottom=box.hidden||pre.scrollTop+pre.clientHeight>=pre.scrollHeight-30;
   box.hidden=false;pre.innerHTML=ansiToHtml(j.text||'');if(atBottom)pre.scrollTop=pre.scrollHeight}
 }catch(e){if(full)$('#livestate').textContent='Could not reach the board: '+e.message}}
$('#livecopy').onclick=()=>{const t=$('#livecodetext').textContent;const b=$('#livecopy');
 const done=m=>{b.textContent=m;setTimeout(()=>b.textContent='Copy',1500)};
 try{navigator.clipboard.writeText(t).then(()=>done('Copied'),()=>done('Select it'))}catch(e){done('Select it')}};
$('#liveclear').onclick=async()=>{try{await api('/api/attention',{clear:true,by:me()||'someone'});loadLive()}catch(e){$('#livestate').textContent=e.message}};


// ---- users: the board's team list. People pick themselves under "I am"; everyone appears in "For whom?"
let USERS=[],usersSig='';
const isClaude=u=>u.kind==='claude';
function sortUsers(list){return [...list].sort((a,b)=>(a.name==='dev-claude'?-1:b.name==='dev-claude'?1:0)||(isClaude(b)-isClaude(a))||a.name.localeCompare(b.name))}
async function loadUsers(){
 try{const j=await api('/api/users');const sig=JSON.stringify(j.users);if(sig===usersSig&&view!=='users')return;usersSig=sig;
  USERS=sortUsers(j.users);renderMe();renderToPills();if(view==='users')renderUsers()}catch(e){}}
function renderMe(){
 const sel=$('#me'),cur=sel.value||store('qa-me')||'';const people=USERS.filter(u=>!isClaude(u));
 sel.innerHTML='<option value="">choose…</option>'+people.map(u=>`<option value="${esc(u.name)}">${esc(u.name)}</option>`).join('');
 sel.value=people.some(u=>u.name===cur)?cur:''}
function renderToPills(){
 const cur=$('#to').value,box=$('#topills');
 box.innerHTML=USERS.map(u=>`<button type="button" class="topill${u.name===cur?' on':''}" data-to="${esc(u.name)}" aria-pressed="${u.name===cur}">${isClaude(u)?'<span class="cl">Claude</span>':''}${esc(u.name)}</button>`).join('')
  +`<button type="button" class="topill any${cur==='anyone'?' on':''}" data-to="anyone" aria-pressed="${cur==='anyone'}">anyone</button>`
  +(USERS.length?'':'<span class="meta">Add people in the Users tab.</span>')}
$('#topills').onclick=e=>{const b=e.target.closest('button');if(!b)return;
 $('#to').value=b.dataset.to===$('#to').value?'':b.dataset.to;$('#anywarn').hidden=true;renderToPills()};
function renderUsers(){
 const list=$('#userlist');
 if(!USERS.length){list.innerHTML='<div class="empty">No users yet. Add the team above.</div>';return}
 list.innerHTML=USERS.map(u=>{const cl=isClaude(u),dev=u.name==='dev-claude';
  return `<div class="card urow"><div class="av${cl?' cl':''}" aria-hidden="true">${cl?'&#10035;':esc(u.name[0].toUpperCase())}</div>
  <div class="main"><b>${esc(u.name)}</b> <span class="ubadge${cl?' cl':''}">${cl?'Claude':'Person'}</span>
  <div class="meta">${dev?'Claude Code on the server. It answers questions on this board and is listed first.':cl?'A Claude assistant (e.g. in someone\'s Claude Desktop).':'Picks themselves next to "I am".'}</div></div>
  <button class="rmuser" data-rm="${esc(u.name)}">Remove</button></div>`}).join('')}
$('#userlist').onclick=async e=>{const b=e.target.closest('button[data-rm]');if(!b)return;
 if(!b.dataset.confirm){b.dataset.confirm='1';b.textContent='Remove '+b.dataset.rm+'?';b.classList.add('danger');
  setTimeout(()=>{if(b.isConnected){delete b.dataset.confirm;b.textContent='Remove';b.classList.remove('danger')}},4000);return}
 try{await api('/api/users/'+encodeURIComponent(b.dataset.rm)+'/delete',{});usermsg('Removed '+b.dataset.rm+'. Their questions and answers stay on the board.');usersSig='';loadUsers()}
 catch(err){usermsg(err.message,true)}};
function usermsg(t,bad){const m=$('#usermsg');m.className=bad?'err':'ok';m.textContent=t}
$('#adduser').onclick=async()=>{const n=$('#newuser').value.trim();if(!n)return usermsg('Type a name first.',true);
 try{const u=await api('/api/users',{name:n,kind:$('#newkind').value});$('#newuser').value='';usermsg('Added '+u.name+'.');usersSig='';loadUsers()}
 catch(err){usermsg(err.message,true)}};
$('#newuser').onkeydown=e=>{if(e.key==='Enter')$('#adduser').click()};

// ---- floating terminal window: the Claude Code web terminal (ttyd) in an iframe
const TERM_DEFAULT={TERM_JSON};
const termUrl=()=>store('qa-term')||TERM_DEFAULT;
function termGeom(){try{return JSON.parse(store('qa-termbox')||'null')}catch(e){return null}}
function saveGeom(){const w=$('#termwin');if(w.classList.contains('max'))return;
 store('qa-termbox',JSON.stringify({l:w.offsetLeft,t:w.offsetTop,w:w.offsetWidth,h:w.offsetHeight}))}
function placeTerm(){const w=$('#termwin'),g=termGeom();
 const W=Math.min(innerWidth-24,g?g.w:Math.round(innerWidth*.8)),H=Math.min(innerHeight-24,g?g.h:Math.round(innerHeight*.72));
 w.style.width=W+'px';w.style.height=H+'px';
 w.style.left=Math.max(8,Math.min(innerWidth-W-8,g?g.l:Math.round((innerWidth-W)/2)))+'px';
 w.style.top=Math.max(8,Math.min(innerHeight-60,g?g.t:Math.round((innerHeight-H)/2)))+'px'}
function openTerm(){const w=$('#termwin');
 if(!termUrl()){changeTermUrl();if(!termUrl())return}
 if(w.hidden){placeTerm();$('#termframe').src=termUrl();$('#termurl').textContent=termUrl().replace(/^https?:\/\//,'');
  $('#termnew').href=termUrl();w.hidden=false;$('#termshade').hidden=false;document.body.classList.add('termopen')}
 $('#termframe').focus()}
function closeTerm(){$('#termframe').src='about:blank';$('#termwin').hidden=true;$('#termshade').hidden=true;document.body.classList.remove('termopen')}
function changeTermUrl(){const v=prompt('Address of this board\'s Claude Code terminal (as your browser reaches it, e.g. through the SSH tunnel):',termUrl());
 if(v===null)return;const t=v.trim();
 if(t&&!/^https?:\/\/[^\s]+$/.test(t)){alert('Use an address like http://127.0.0.1:17681');return}
 store('qa-term',t&&t!==TERM_DEFAULT?t:'');if(!$('#termwin').hidden){$('#termframe').src=termUrl();$('#termurl').textContent=termUrl().replace(/^https?:\/\//,'');$('#termnew').href=termUrl()}}
$('#openterm').onclick=openTerm;
$('#termclose').onclick=closeTerm;
$('#termmax').onclick=()=>{const w=$('#termwin');w.classList.toggle('max');$('#termmax').title=w.classList.contains('max')?'Restore':'Maximise';if(!w.classList.contains('max'))placeTerm()};
$('#termurl').onclick=changeTermUrl;
$('#termbar').ondblclick=e=>{if(!e.target.closest('button,a,#termurl'))$('#termmax').click()};
// drag by the title bar, resize from the corner (the iframe ignores the pointer meanwhile)
let tdrag=null;
function startDrag(e,kind){const w=$('#termwin');if(w.classList.contains('max'))return;
 tdrag={kind,x:e.clientX,y:e.clientY,l:w.offsetLeft,t:w.offsetTop,w:w.offsetWidth,h:w.offsetHeight};
 e.currentTarget.setPointerCapture(e.pointerId);w.classList.add('dragging');e.preventDefault()}
function moveDrag(e){if(!tdrag)return;const w=$('#termwin'),dx=e.clientX-tdrag.x,dy=e.clientY-tdrag.y;
 if(tdrag.kind==='move'){w.style.left=Math.max(-tdrag.w+120,Math.min(innerWidth-120,tdrag.l+dx))+'px';w.style.top=Math.max(0,Math.min(innerHeight-40,tdrag.t+dy))+'px'}
 else{w.style.width=Math.max(360,tdrag.w+dx)+'px';w.style.height=Math.max(220,tdrag.h+dy)+'px'}}
function endDrag(){if(!tdrag)return;tdrag=null;$('#termwin').classList.remove('dragging');saveGeom()}
$('#termbar').addEventListener('pointerdown',e=>{if(!e.target.closest('button,a,#termurl'))startDrag(e,'move')});
$('#termresize').addEventListener('pointerdown',e=>startDrag(e,'size'));
for(const el of [$('#termbar'),$('#termresize')]){el.addEventListener('pointermove',moveDrag);el.addEventListener('pointerup',endDrag);el.addEventListener('pointercancel',endDrag)}
addEventListener('resize',()=>{if(!$('#termwin').hidden&&!$('#termwin').classList.contains('max'))placeTerm()});

// ---- questions
let filter=store('qa-filter')||'unresolved';const expanded=new Set();
// Long threads: only the latest SHOW_LAST answers are open; earlier ones fold to a one-line preview.
const SHOW_LAST=2,openAns=new Set(),closedAns=new Set();
function peek(t){t=String(t||'').replace(/!\[([^\]]*)\]\([^)]*\)/g,'[image: $1]').replace(/\[([^\]]+)\]\([^)]*\)/g,'$1')
 .replace(/```[\s\S]*?(```|$)/g,' [code] ').replace(/^\s*#+\s+/gm,'').replace(/[`*]/g,'').replace(/^\s*&gt;\s?|^\s*>\s?/gm,'').replace(/\s+/g,' ').trim();return t.length>140?t.slice(0,137)+'…':t}
const FILTERS=[['unresolved','Unresolved'],['open','Open'],['answered','Answered'],['resolved','Resolved'],['all','All']];
function tabs(){$('#tabs').innerHTML=FILTERS.map(([k,l])=>`<button data-f="${k}" class="${k===filter?'on':''}">${l}</button>`).join('');}
$('#tabs').onclick=e=>{const f=e.target.dataset.f;if(f){filter=f;store('qa-filter',f);tabs();load()}};
function card(q){const open=expanded.has(q.id);const forMe=me()&&q.status==='open'&&(q.to===me()||q.to==='anyone')&&q.author!==me();
 const docs=q.documents||[];
 let h=`<div class="card q ${forMe?'forme':''}" data-id="${q.id}"><h3 data-toggle="${q.id}"><span class="pill ${q.status}">${q.status}</span>#${q.id} ${esc(q.title)}</h3>
 <div class="meta">${esc(q.author)} → ${esc(q.to)} · ${ago(q.created)} · ${q.answers.length} answer${q.answers.length===1?'':'s'}${docs.length?` · ${docs.length} document${docs.length===1?'':'s'}`:''}</div>`;
 if(open){if(q.body)h+=`<div class="body md">${md(q.body)}</div>`;
  if(docs.length)h+=`<div class="att">Attached: ${docs.map(d=>`<a href="${url('/api/docs/'+d.id+'/view')}" target="_blank" rel="noopener">${esc(d.title)}</a>`).join('')}</div>`;
  const n=q.answers.length,early=a=>q.answers.indexOf(a)<n-SHOW_LAST;
  const folded=q.answers.filter(a=>early(a)?!openAns.has(a.id):closedAns.has(a.id));
  const bar=[];
  if(folded.length>1)bar.push(`${folded.length} replies folded · <a href="#" data-unfoldall="${q.id}">show all</a>`);
  if(q.answers.some(a=>early(a)&&!folded.includes(a)))bar.push(`<a href="#" data-foldall="${q.id}">fold earlier replies</a>`);
  if(bar.length)h+=`<div class="foldbar">${bar.join(' · ')}</div>`;
  for(const a of q.answers){
   if(folded.includes(a))h+=`<div class="ans folded" data-unfold="${a.id}" title="Show this reply"><div class="meta"><span class="chev">▸</span> ${esc(a.author)} · ${ago(a.created)} <span class="peek">${esc(peek(a.body))}</span></div></div>`;
   else h+=`<div class="ans"><div class="meta"><a href="#" class="chev" data-fold="${a.id}" title="Fold this reply">▾</a> ${esc(a.author)} · ${ago(a.created)}</div><div class="body md" style="margin-top:4px">${md(a.body)}</div></div>`}
  h+=`<div class="row"><textarea data-draft="${q.id}" placeholder="Write an answer"></textarea></div>
  <div class="row" style="justify-content:flex-end"><button data-attach="${q.id}" style="flex:0 0 auto">Attach file</button><button class="primary" data-answer="${q.id}" style="flex:0 0 auto">Answer</button>
  ${q.status==='resolved'?`<button data-status="${q.id}:open" style="flex:0 0 auto">Reopen</button>`:`<button data-status="${q.id}:resolved" style="flex:0 0 auto">Mark resolved</button>`}</div>`}
 return h+'</div>'}
function linkDocs(html){return html.replace(/\bdoc (\d+)\b/gi,(m,id)=>`<a href="${url('/api/docs/'+id+'/view')}" target="_blank" rel="noopener">${m}</a>`)}
// Markdown for posts (what Claude writes): **bold**, *italic*, `code`, ``` blocks, lists, # headings, links,
// and images from the board's own document store: ![caption](/api/docs/12/view).
// The text is HTML-escaped first, so a post can never add its own HTML.
function docUrl(u,kind){ // a link to one of this board's documents (relative, or written for any host/port)
 const m=String(u).replace(/&amp;/g,'&').match(/^(?:https?:\/\/[^\/\s]+)?\/api\/docs\/(\d+)\/(view|download)(?:\?(\S*))?$/);
 if(!m)return null;
 const v=new URLSearchParams(m[3]||'').get('v');
 return url('/api/docs/'+m[1]+'/'+(kind||m[2])+(v&&/^\d+$/.test(v)?'?v='+v:'')).href;   // with this viewer's key
}
function mdInline(t){
 const codes=[],keep=[],hold=h=>{keep.push(h);return '\u0001'+(keep.length-1)+'\u0001'};
 t=t.replace(/`([^`\n]+)`/g,(m,c)=>{codes.push(c);return '\u0000'+(codes.length-1)+'\u0000'});
 t=t.replace(/!\[([^\]\n]*)\]\(([^\s)]+)\)/g,(m,alt,src)=>{const d=docUrl(src,'view');
  if(d)return hold(`<a class="mdimg" href="${esc(d)}" target="_blank" rel="noopener"><img src="${esc(d)}" alt="${alt}" title="${alt}" loading="lazy"></a>`);
  return /^https?:\/\//.test(src)?hold(`<a href="${src}" target="_blank" rel="noopener">${alt||src}</a>`):m});
 t=t.replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|\/api\/docs\/)[^\s)]+)\)/g,(m,txt,href)=>{const d=docUrl(href);
  return hold(`<a href="${d?esc(d):href}" target="_blank" rel="noopener">${txt}</a>`)});
 t=t.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?])/g,(m,pre,href)=>{const d=docUrl(href);
  return pre+hold(`<a href="${d?esc(d):href}" target="_blank" rel="noopener">${href}</a>`)});
 t=t.replace(/\*\*(?=\S)([^\n]*?\S)\*\*/g,'<strong>$1</strong>');
 t=t.replace(/(^|[^*\w])\*(?=\S)([^*\n]*?\S)\*(?![*\w])/g,'$1<em>$2</em>');
 t=linkDocs(t);
 t=t.replace(/\u0001(\d+)\u0001/g,(m,i)=>keep[+i]);
 return t.replace(/\u0000(\d+)\u0000/g,(m,i)=>'<code>'+codes[+i]+'</code>');
}
function md(src){
 const out=[];let para=[],list=null,code=null;
 const flushP=()=>{if(para.length){out.push('<p>'+para.map(mdInline).join('<br>')+'</p>');para=[]}};
 const flushL=()=>{if(list){out.push((list.t==='ol'&&list.start!=='1'?`<ol start="${list.start}">`:`<${list.t}>`)+
   list.items.map(i=>`<li${i.sub?' class="sub"':''}>${mdInline(i.text)}</li>`).join('')+`</${list.t}>`);list=null}};
 for(const line of esc(src||'').split('\n')){
  if(code!==null){if(/^\s*```/.test(line)){out.push('<pre><code>'+code.join('\n')+'</code></pre>');code=null}else code.push(line);continue}
  let m;
  if(/^\s*```/.test(line)){flushP();flushL();code=[]}
  else if((m=line.match(/^\s{0,3}(#{1,4})\s+(.*)$/))){flushP();flushL();out.push('<h4 class="h'+m[1].length+'">'+mdInline(m[2])+'</h4>')}
  else if((m=line.match(/^(\s*)[-*•]\s+(.*)$/))){flushP();if(!list||list.t!=='ul'){flushL();list={t:'ul',items:[]}}list.items.push({text:m[2],sub:m[1].length>=2})}
  else if((m=line.match(/^(\s*)(\d+)[.)]\s+(.*)$/))){flushP();if(!list||list.t!=='ol'){flushL();list={t:'ol',items:[],start:m[2]}}list.items.push({text:m[3],sub:m[1].length>=2})}
  else if(/^\s*$/.test(line)){flushP();flushL()}
  else if(list&&/^\s{2,}\S/.test(line)){list.items[list.items.length-1].text+=' '+line.trim()}
  else{flushL();para.push(line)}
 }
 if(code!==null)out.push('<pre><code>'+code.join('\n')+'</code></pre>');
 flushP();flushL();return out.join('');
}
async function load(){
 // While someone is in an answer box, hold the refresh: always if they've typed something, and for
 // 20s of no activity if it's empty. After that the list refreshes and the box gets its focus back.
 const a=document.activeElement;let refocus=null;
 if(a&&a.closest&&a.closest('#list')&&/^(TEXTAREA|INPUT)$/.test(a.tagName)){
  if(a.value||Date.now()-lastActivity<20000)return;
  refocus=a.dataset.draft;
 }
 const drafts={};document.querySelectorAll('[data-draft]').forEach(t=>{if(t.value)drafts[t.dataset.draft]=t.value});
 try{const [{questions},{people}]=await Promise.all([api('/api/questions?status='+filter),api('/api/people')]);
  $('#people').innerHTML=people.map(p=>`<option value="${esc(p)}">`).join('');
  QUESTIONS=questions;
  $('#list').innerHTML=questions.length?questions.map(card).join(''):'<div class="empty">No questions here.</div>';
  for(const[id,v]of Object.entries(drafts)){const t=document.querySelector(`[data-draft="${id}"]`);if(t)t.value=v}
  if(refocus){const t=document.querySelector(`[data-draft="${refocus}"]`);if(t)t.focus()}
  const n=questions.filter(q=>q.status==='open').length;document.title=(n?'('+n+') ':'')+BRAND+' · Q&A Board';
 }catch(e){$('#list').innerHTML=`<div class="empty err">Could not reach the board: ${esc(e.message)}</div>`}}
let attachTo=null,lastActivity=0,QUESTIONS=[];
['focusin','input','keydown'].forEach(ev=>$('#list').addEventListener(ev,()=>{lastActivity=Date.now()}));
$('#list').onclick=async e=>{const t=e.target;
 if(t.dataset.toggle){const id=+t.dataset.toggle;expanded.has(id)?expanded.delete(id):expanded.add(id);load();return}
 const uf=t.closest&&t.closest('[data-unfold]');
 if(uf&&!t.closest('a[href]:not([data-unfold])')){const id=+uf.dataset.unfold;openAns.add(id);closedAns.delete(id);load();return}
 if(t.dataset.fold){e.preventDefault();const id=+t.dataset.fold;openAns.delete(id);closedAns.add(id);load();return}
 if(t.dataset.unfoldall||t.dataset.foldall){e.preventDefault();const q=QUESTIONS.find(x=>x.id===+(t.dataset.unfoldall||t.dataset.foldall));
  if(q)for(const a of q.answers){if(t.dataset.unfoldall){openAns.add(a.id);closedAns.delete(a.id)}else{openAns.delete(a.id);if(q.answers.indexOf(a)>=q.answers.length-SHOW_LAST)closedAns.delete(a.id)}}
  load();return}
 if(!me()){alertBox('Choose who you are next to "I am" first.');return}
 if(t.dataset.attach){attachTo=+t.dataset.attach;$('#verfile').dataset.mode='attach';$('#verfile').click();return}
 if(t.dataset.answer){const ta=document.querySelector(`[data-draft="${t.dataset.answer}"]`);if(!ta.value.trim())return;
  try{await api(`/api/questions/${t.dataset.answer}/answers`,{body:ta.value,author:me()});ta.value='';ta.blur();load()}catch(err){alertBox(err.message)}}
 if(t.dataset.status){const[id,s]=t.dataset.status.split(':');try{await api(`/api/questions/${id}/status`,{status:s});load()}catch(err){alertBox(err.message)}}};
function alertBox(m){$('#perr').textContent=m;$('#newbox').open=true;setTimeout(()=>$('#perr').textContent='',4000)}
let qFiles=[];
function showQFiles(){$('#qfilelist').innerHTML=qFiles.length?qFiles.map((f,i)=>`${esc(f.name)} (${size(f.size)}) <a href="#" data-rmq="${i}">remove</a>`).join(' · '):'They\'re attached to the question when you post it.'}
function addQFiles(list){for(const f of list){if(f.size>maxMB*1048576){alertBox(`${f.name} is larger than the ${maxMB} MB limit.`);continue}qFiles.push(f)}showQFiles()}
$('#qfile').onchange=e=>{addQFiles([...e.target.files]);e.target.value=''};
$('#qfilelist').onclick=e=>{if(e.target.dataset.rmq){e.preventDefault();qFiles.splice(+e.target.dataset.rmq,1);showQFiles()}};
const qdrop=$('#newbox');
['dragenter','dragover'].forEach(ev=>qdrop.addEventListener(ev,e=>{if(e.dataTransfer&&[...e.dataTransfer.types].includes('Files')){e.preventDefault();$('#qdrop').classList.add('over')}}));
['dragleave','drop'].forEach(ev=>qdrop.addEventListener(ev,()=>$('#qdrop').classList.remove('over')));
qdrop.addEventListener('drop',e=>{if(e.dataTransfer&&e.dataTransfer.files.length){e.preventDefault();qdrop.open=true;addQFiles([...e.dataTransfer.files])}});
async function postQuestion(to){
 const btns=[$('#post'),$('#postany')];btns.forEach(b=>b.disabled=true);
 try{const q=await api('/api/questions',{title:$('#title').value,body:$('#qbody').value,to,author:me()});
  const failed=[];
  for(const [i,f] of qFiles.entries()){$('#qfilelist').textContent=`Attaching ${f.name} (${i+1} of ${qFiles.length})…`;
   try{await upload(f,{question:q.id})}catch(err){failed.push(f.name+': '+err.message)}}
  qFiles=[];showQFiles();
  $('#title').value=$('#qbody').value=$('#to').value='';$('#anywarn').hidden=true;renderToPills();
  if(failed.length)alertBox(`Question #${q.id} was posted, but these files weren't attached: ${failed.join('; ')}`);else $('#newbox').open=false;
  expanded.add(q.id);load()}catch(e){alertBox(e.message)}
 finally{btns.forEach(b=>b.disabled=false)}}
$('#post').onclick=()=>{if(!me())return alertBox('Choose who you are next to "I am" first.');
 if(!$('#title').value.trim())return alertBox('Write the question first.');
 const to=$('#to').value.trim().toLowerCase();
 if(!to||to==='anyone'){$('#anywarn').hidden=false;$('#postany').focus();return}
 postQuestion(to)};
$('#postany').onclick=()=>postQuestion('anyone');
$('#chooseto').onclick=()=>{$('#anywarn').hidden=true;$('#to').value='';renderToPills();const b=$('#topills button');if(b)b.focus()};

// ---- documents
let maxMB=20;
async function upload(file,opts={}){
 const p=new URLSearchParams({filename:file.name,author:me()||'unknown'});
 if(opts.title)p.set('title',opts.title);if(opts.description)p.set('description',opts.description);
 if(opts.question)p.set('question',opts.question);if(opts.replace)p.set('replace',opts.replace);
 const r=await fetch(url('/api/docs?'+p),{method:'POST',body:file});
 const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.status);return j}
async function uploadMany(files){
 if(!me()){$('#uploadmsg').className='err';$('#uploadmsg').textContent='Choose who you are next to "I am" first.';return}
 const many=files.length>1;
 for(const f of files){
  if(f.size>maxMB*1048576){msg(`${f.name}: larger than the ${maxMB} MB limit.`,true);continue}
  msg(`Uploading ${f.name}…`);
  try{const d=await upload(f,{title:many?'':$('#dtitle').value.trim(),description:$('#ddesc').value.trim(),question:$('#dq').value.trim()});
   msg(`Stored ${d.filename} as doc ${d.id}.`)}catch(e){msg(`${f.name}: ${e.message}`,true)}}
 $('#dtitle').value=$('#ddesc').value='';loadDocs()}
function msg(t,bad){const m=$('#uploadmsg');m.className=bad?'err':'ok';m.textContent=t}
$('#file').onchange=e=>{uploadMany([...e.target.files]);e.target.value=''};
const drop=$('#drop');
['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('over')}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('over')}));
drop.addEventListener('drop',e=>uploadMany([...e.dataTransfer.files]));
let verFor=null;
$('#verfile').onchange=async e=>{const f=e.target.files[0];e.target.value='';if(!f)return;
 try{
  if($('#verfile').dataset.mode==='attach'){await upload(f,{question:attachTo});load()}
  else{await upload(f,{replace:verFor});msg(`New version of doc ${verFor} stored.`);loadDocs()}
 }catch(err){view==='docs'?msg(err.message,true):alertBox(err.message)}};
function docRow(d){
 const v=url('/api/docs/'+d.id+'/view'),dl=url('/api/docs/'+d.id+'/download');
 return `<div class="card doc"><div class="main"><h3>${esc(d.title)}${d.archived?'<span class="badge">archived</span>':''}</h3>
 <div class="meta">doc ${d.id} · ${esc(d.filename)} · ${size(d.size)} · v${d.version}${d.version_count>1?` (${d.version_count} versions)`:''} · ${esc(d.author)} · ${ago(d.updated)}${d.question_id?` · <a href="#" data-goq="${d.question_id}">question #${d.question_id}</a>`:''}</div>
 ${d.description?`<div class="body md" style="margin-top:4px">${md(d.description)}</div>`:''}
 <div class="vers" data-vers="${d.id}"></div></div>
 <div class="acts"><a href="${v}" target="_blank" rel="noopener">Open</a><a href="${dl}">Download</a>
 <button data-newver="${d.id}">New version</button>${d.version_count>1?`<button data-showvers="${d.id}">History</button>`:''}
 <button data-arch="${d.id}" data-on="${d.archived?0:1}">${d.archived?'Restore':'Archive'}</button></div></div>`}
let dsearchT=null;
$('#dsearch').oninput=()=>{clearTimeout(dsearchT);dsearchT=setTimeout(loadDocs,250)};
$('#darch').onchange=loadDocs;
async function loadDocs(){
 const a=document.activeElement;if(a&&a.closest&&a.closest('#v-docs')&&a.id!=='dsearch'&&a.value&&a.type!=='checkbox')return;
 try{const p=new URLSearchParams();const s=$('#dsearch').value.trim();if(s)p.set('q',s);if($('#darch').checked)p.set('archived','1');
  const j=await api('/api/docs?'+p);maxMB=j.max_mb||maxMB;$('#maxmb').textContent=`Up to ${maxMB} MB per file. Everyone with access to the board can open these.`;
  $('#docs').innerHTML=j.documents.length?j.documents.map(docRow).join(''):'<div class="empty">No documents yet. Drop a file above to add one.</div>';
 }catch(e){$('#docs').innerHTML=`<div class="empty err">Could not reach the board: ${esc(e.message)}</div>`}}
$('#docs').onclick=async e=>{const t=e.target;
 if(t.dataset.goq){e.preventDefault();expanded.add(+t.dataset.goq);filter='all';store('qa-filter','all');tabs();showView('questions');return}
 if(t.dataset.newver){if(!me()){msg('Choose who you are next to "I am" first.',true);return}verFor=+t.dataset.newver;$('#verfile').dataset.mode='version';$('#verfile').click()}
 if(t.dataset.showvers){const box=document.querySelector(`[data-vers="${t.dataset.showvers}"]`);
  if(box.innerHTML){box.innerHTML='';return}
  const d=await api('/api/docs/'+t.dataset.showvers);
  box.innerHTML=d.versions.map(v=>`v${v.version} · <a href="${url('/api/docs/'+d.id+'/download')}&v=${v.version}">${esc(v.filename)}</a> · ${size(v.size)} · ${esc(v.author)} · ${ago(v.created)}${v.note?' · '+esc(v.note):''}`).join('<br>')}
 if(t.dataset.arch){try{await api(`/api/docs/${t.dataset.arch}/archive`,{archived:t.dataset.on==='1'});loadDocs()}catch(err){msg(err.message,true)}}};

api('/api/docs?limit=1').then(j=>{maxMB=j.max_mb||maxMB}).catch(()=>{});
tabs();showView(view);setInterval(refresh,8000);if(view!=='live')loadLive();
</script></body></html>
"""


# --------------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="Tiny shared Q&A board and document store with an MCP endpoint for Claude.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the board")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--db", default="qaboard.db")
    s.add_argument("--key", default=None, help="optional shared secret; clients add ?key=... or X-QA-Key")
    s.add_argument("--max-doc-mb", type=float, default=20, help="largest document accepted (default 20)")
    s.add_argument("--name", default="Team", help='board name shown on the page, e.g. "Acme Web App"')
    s.add_argument("--accent", default="#3949ab", help="accent colour, e.g. #7a4cab")
    s.add_argument("--terminal-url", default="", help="where browsers reach this board's Claude Code web terminal, "
                                                      "e.g. http://127.0.0.1:17681 (the SSH tunnel)")
    b = sub.add_parser("bridge", help="stdio<->HTTP MCP bridge for Claude Desktop")
    b.add_argument("url", help='e.g. "http://server.example:8787/mcp?who=alice-claude&key=KEY"')
    args = p.parse_args()
    serve(args) if args.cmd == "serve" else bridge(args)


if __name__ == "__main__":
    main()
