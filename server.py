#!/usr/bin/env python3
"""Minimal HTTP server for Claude History Viewer."""
from __future__ import annotations
import html, json, mimetypes, os, re, sqlite3, time, urllib.parse, uuid
import importlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    _docx_module = importlib.import_module("docx")
    _DocxDocument = getattr(_docx_module, "Document", None)
    _DOCX_OK = callable(_DocxDocument)
except Exception:
    _DocxDocument = None
    _DOCX_OK = False

def open_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_runtime_schema(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversation_meta (
                conversation_id TEXT PRIMARY KEY,
                custom_title    TEXT,
                archived        INTEGER DEFAULT 0,
                deleted         INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS pinned_conversations (
                conversation_id TEXT PRIMARY KEY,
                pinned_at       REAL,
                order_index     INTEGER
            );
            CREATE TABLE IF NOT EXISTS ui_preferences (
                pref_key   TEXT PRIMARY KEY,
                pref_value TEXT
            );
            CREATE TABLE IF NOT EXISTS workspace_tabs (
                id              TEXT PRIMARY KEY,
                tab_type        TEXT NOT NULL,
                conversation_id TEXT,
                artifact_id     TEXT,
                title           TEXT,
                pinned          INTEGER DEFAULT 0,
                sort_index      INTEGER DEFAULT 0,
                last_active_at  REAL,
                closed          INTEGER DEFAULT 0
            );
            """
        )
        conn.execute(
            "UPDATE conversation_meta SET custom_title = NULL WHERE TRIM(COALESCE(custom_title, '')) = ''"
        )
        conn.execute(
            """
            UPDATE workspace_tabs
               SET title = (
                   SELECT COALESCE(NULLIF(cm.custom_title, ''), c.title)
                     FROM conversations c
                     LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id
                    WHERE c.id = workspace_tabs.conversation_id
               )
             WHERE tab_type = 'conversation'
               AND conversation_id IS NOT NULL
               AND TRIM(COALESCE(title, '')) = ''
            """
        )
        conn.commit()
    finally:
        conn.close()

def _norm_filename(s: str) -> str:
    """Normalize for matching: lowercase + spaces→underscores."""
    return s.lower().replace(" ", "_")

def _extract_snippet(text: str, q: str, radius: int = 100) -> str:
    """Return a plain-text snippet of `text` centered around query `q`."""
    if not text:
        return ""
    idx = text.lower().find(q.lower())
    if idx == -1:
        return text[:radius * 2] + ("…" if len(text) > radius * 2 else "")
    start = max(0, idx - radius // 2)
    end   = min(len(text), idx + len(q) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix

_SOURCE_INDEX: dict[str, list[Path]] = {}


def _source_index_add(key: str, path: Path) -> None:
    if not key:
        return
    bucket = _SOURCE_INDEX.setdefault(key, [])
    if path not in bucket:
        bucket.append(path)

def _build_source_index(source_dir):
    _SOURCE_INDEX.clear()
    if not source_dir.exists():
        return
    for root, _dirs, files in os.walk(source_dir):
        for fname in files:
            p = Path(root) / fname
            rel = str(p.relative_to(source_dir)).replace("\\", "/")
            rel_stem = str(Path(rel).with_suffix(""))
            _source_index_add(fname, p)
            _source_index_add(p.stem, p)
            _source_index_add(rel, p)
            _source_index_add(rel_stem, p)

class Handler(BaseHTTPRequestHandler):
    db_path = Path("history.db")

    def log_message(self, fmt, *args): pass

    def _request_target(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        return parsed, path

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        _, path = self._request_target()
        if path == "/api/upload-file":
            self._api_upload_file()
        elif path == "/api/pinned":
            self._api_pinned_add()
        elif path == "/api/pinned/reorder":
            self._api_pinned_reorder()
        elif path == "/api/tabs":
            self._api_tabs_create()
        else:
            self.send_error(404)

    def do_PATCH(self):
        _, path = self._request_target()
        if path == "/api/preferences":
            self._api_preferences_update()
        elif path.startswith("/api/tabs/"):
            self._api_tab_update(urllib.parse.unquote(path[len("/api/tabs/"):]))
        elif path.startswith("/api/conversation/"):
            self._api_conversation_update(urllib.parse.unquote(path[len("/api/conversation/"):]))
        else:
            self.send_error(404)

    def do_DELETE(self):
        _, path = self._request_target()
        if path.startswith("/api/pinned/"):
            self._api_pinned_remove(urllib.parse.unquote(path[len("/api/pinned/"):]))
        elif path.startswith("/api/tabs/"):
            self._api_tab_remove(urllib.parse.unquote(path[len("/api/tabs/"):]))
        else:
            self.send_error(404)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        body = self.rfile.read(length)
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            return {}

    def _parse_multipart_form(self, body: bytes, boundary: bytes) -> dict[str, list[dict]]:
        fields: dict[str, list[dict]] = {}
        marker = b"--" + boundary
        for chunk in body.split(marker):
            part = chunk.strip(b"\r\n")
            if not part or part == b"--":
                continue
            if part.endswith(b"--"):
                part = part[:-2]
            if b"\r\n\r\n" not in part:
                continue
            head, data = part.split(b"\r\n\r\n", 1)
            data = data.rstrip(b"\r\n")
            headers = head.decode("utf-8", errors="replace").split("\r\n")
            disp = next((h for h in headers if h.lower().startswith("content-disposition:")), "")
            if not disp:
                continue
            params = dict(re.findall(r'([a-zA-Z0-9_-]+)="([^"]*)"', disp))
            name = params.get("name", "")
            if not name:
                continue
            fields.setdefault(name, []).append({
                "filename": params.get("filename", ""),
                "data": data,
            })
        return fields

    def _api_upload_file(self):
        length = int(self.headers.get("Content-Length", 0))
        ctype = self.headers.get("Content-Type", "")
        m = re.match(r"multipart/form-data\s*;\s*boundary=(.+)", ctype, re.IGNORECASE)
        if not m:
            self.send_json({"error": "Expected multipart/form-data"}, 400); return
        boundary = m.group(1).strip().strip('"').encode("utf-8")
        body = self.rfile.read(length)
        fields = self._parse_multipart_form(body, boundary)
        files  = fields.get("file", [])
        names  = fields.get("name", [])
        if not files or not names:
            self.send_json({"error": "Missing file or name"}, 400); return
        file_data = files[0].get("data", b"")
        file_name = names[0].get("data", b"").decode("utf-8", errors="replace")
        # Sanitise: strip path separators
        safe_name = Path(file_name).name
        if not safe_name:
            self.send_json({"error": "Invalid filename"}, 400); return
        dest_dir = Path(__file__).parent / "source" / "files"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / safe_name
        dest.write_bytes(file_data)
        # Re-index so next /files/ request can find the new file
        _build_source_index(Path(__file__).parent / "source")
        self.send_json({"ok": True, "saved": safe_name})

    def do_GET(self):
        parsed, path = self._request_target()
        qs     = urllib.parse.parse_qs(parsed.query)
        static_dir = Path(__file__).parent / "static"

        if path in ("/", "/index.html"):
            self._serve_file(static_dir / "index.html")
        elif path in ("/app.js", "/static/app.js"):
            self._serve_file(static_dir / "app.js")
        elif path in ("/style.css", "/static/style.css"):
            self._serve_file(static_dir / "style.css")
        elif path == "/api/conversations":
            self._api_conversations(qs)
        elif path.startswith("/api/conversation/"):
            self._api_detail(urllib.parse.unquote(path[len("/api/conversation/"):]))
        elif path == "/api/search":
            self._api_search(qs)
        elif path == "/api/gallery":
            self._api_gallery()
        elif path == "/api/attachment-report":
            self._api_attachment_report()
        elif path == "/api/memories":
            self._api_memories()
        elif path == "/api/projects":
            self._api_projects()
        elif path.startswith("/api/project/"):
            self._api_project(urllib.parse.unquote(path[len("/api/project/"):]))
        elif path.startswith("/api/artifact/"):
            self._api_artifact(urllib.parse.unquote(path[len("/api/artifact/"):]))
        elif path == "/api/preferences":
            self._api_preferences()
        elif path == "/api/pinned":
            self._api_pinned_list()
        elif path == "/api/tabs":
            self._api_tabs()
        elif path == "/api/files-manifest":
            self._api_files_manifest()
        elif path == "/api/file-content":
            self._api_file_content(qs)
        elif path == "/api/search-in-conversation":
            self._api_search_in_conversation(qs)
        elif path.startswith("/files/"):
            self._serve_user_file(urllib.parse.unquote(path[len("/files/"):]))
        elif path.startswith("/chatfiles/"):
            self._serve_named_file(urllib.parse.unquote(path[len("/chatfiles/"):]),
                                   Path(__file__).parent / "source" / "chat_files")
        elif path.startswith("/source/"):
            self._serve_source(urllib.parse.unquote(path[len("/source/"):]))
        else:
            self.send_error(404)

    def _serve_file(self, p):
        if not p.is_file():
            self.send_error(404); return
        data = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_source(self, file_id):
        if not re.match(r'^[\w\-./]+$', file_id):
            self.send_error(400); return
        key = file_id.lstrip("/")
        candidates = _SOURCE_INDEX.get(key, [])
        if not candidates:
            self.send_error(404); return
        if len(candidates) > 1:
            self.send_error(409, "Ambiguous source id; use a relative path"); return
        p = candidates[0]
        if p.is_file():
            data = p.read_bytes()
            mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def _serve_named_file(self, filename, base_dir):
        if re.search(r'[\x00-\x1f]', filename) or '..' in filename:
            self.send_error(400); return
        target = (base_dir / filename).resolve()
        if not str(target).startswith(str(base_dir.resolve())):
            self.send_error(403); return
        if not target.is_file():
            self.send_error(404); return
        data = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f'inline; filename="{urllib.parse.quote(target.name)}"')
        self.end_headers()
        self.wfile.write(data)

    def _serve_user_file(self, key: str):
        """Serve a file from source/files/ by filename (case-insensitive, space↔underscore)."""
        if re.search(r'[\x00-\x1f]', key) or '..' in key:
            self.send_error(400); return
        files_dir = Path(__file__).parent / "source" / "files"
        p = files_dir / key
        if not p.is_file():
            # Try case-insensitive + space↔underscore normalization
            key_norm = _norm_filename(key)
            p = None
            if files_dir.exists():
                for f in files_dir.iterdir():
                    if _norm_filename(f.name) == key_norm:
                        p = f
                        break
        if not p or not p.is_file():
            self.send_error(404); return
        data = p.read_bytes()
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{urllib.parse.quote(p.name)}"')
        self.end_headers()
        self.wfile.write(data)

    def _api_files_manifest(self):
        """Return list of available files in source/files/ with name→uuid mapping."""
        files_dir = Path(__file__).parent / "source" / "files"
        result = {}
        if files_dir.exists():
            for f in files_dir.iterdir():
                if f.is_file() and not f.name.startswith('.'):
                    result[f.name] = f.name  # name → name (uuid lookup in frontend)
        self.send_json({"files": list(result.keys())})

    def _api_file_content(self, qs):
        """Return text content of a file in source/files/ (DOCX → plain text, image → url)."""
        name = ((qs.get("name") or [""])[0]).strip()
        if not name:
            self.send_json({"error": "missing name"}); return
        files_dir = Path(__file__).parent / "source" / "files"
        # Normalize lookup
        norm = _norm_filename(name)
        real_path = None
        if files_dir.exists():
            for f in files_dir.iterdir():
                if _norm_filename(f.name) == norm:
                    real_path = f
                    break
        if not real_path or not real_path.is_file():
            self.send_json({"error": f"File not found: {name}"}); return
        ext = real_path.suffix.lower()
        # Image
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".heic", ".heif"}
        if ext in image_exts:
            self.send_json({"name": name, "content": "", "type": "image",
                            "url": "/files/" + urllib.parse.quote(real_path.name)}); return
        # DOCX
        if ext == ".docx":
            docx_document = _DocxDocument
            if not _DOCX_OK or not callable(docx_document):
                self.send_json({"error": "python-docx not installed"}); return
            try:
                doc = docx_document(str(real_path))
                paragraphs = []
                for para in getattr(doc, "paragraphs", []):
                    text = getattr(para, "text", "")
                    if text.strip():
                        paragraphs.append(text)
                content = "\n\n".join(paragraphs)
                self.send_json({"name": name, "content": content, "type": "docx"}); return
            except Exception as e:
                self.send_json({"error": f"Failed to read DOCX: {e}"}); return
        # PDF or other text-like file: try reading as UTF-8 text
        if ext == ".pdf":
            self.send_json({"error": "PDF preview not supported; download the file instead"}); return
        try:
            content = real_path.read_text(encoding="utf-8", errors="replace")
            self.send_json({"name": name, "content": content, "type": ext.lstrip(".")}); return
        except Exception as e:
            self.send_json({"error": f"Cannot read file: {e}"}); return

    def _api_search_in_conversation(self, qs):
        conv_id = ((qs.get("conv_id") or [""])[0]).strip()
        q = ((qs.get("q") or [""])[0]).strip()
        if not conv_id or not q:
            self.send_json({"matches": []}); return
        conn = open_db(self.db_path)
        try:
            # Messages whose content contains the term
            content_rows = conn.execute(
                "SELECT seq, role FROM messages WHERE conversation_id = ? AND content LIKE ? ORDER BY seq",
                (conv_id, f"%{q}%")
            ).fetchall()

            # Messages linked to artifacts whose content contains the term
            try:
                artifact_rows = conn.execute("""
                    SELECT DISTINCT m.seq, m.role
                    FROM messages m
                    JOIN json_each(m.artifact_ids) j ON json_valid(m.artifact_ids)
                    JOIN artifacts a ON a.conv_id = m.conversation_id AND a.id = j.value
                    WHERE m.conversation_id = ?
                      AND a.content LIKE ?
                    ORDER BY m.seq
                """, (conv_id, f"%{q}%")).fetchall()
            except Exception:
                artifact_rows = []

            # Merge and deduplicate by seq, preserving order
            seen = {}
            for r in list(content_rows) + list(artifact_rows):
                if r["seq"] not in seen:
                    seen[r["seq"]] = r["role"]
            matches = [{"seq": k, "role": v} for k, v in sorted(seen.items())]
            self.send_json({"matches": matches, "total": len(matches)})
        finally:
            conn.close()

    def _api_conversations(self, qs):
        limit  = int((qs.get("limit") or ["50"])[0])
        offset = int((qs.get("offset") or ["0"])[0])
        q      = ((qs.get("q") or [""])[0]).strip()
        view   = ((qs.get("view") or ["recent"])[0]).strip().lower()
        pinned_first = ((qs.get("pinned_first") or ["0"])[0]).strip() in ("1", "true", "yes")
        conn = open_db(self.db_path)
        try:
            if view == "pinned":
                rows = conn.execute(
                    "SELECT c.id, COALESCE(NULLIF(cm.custom_title, ''), c.title) AS title, c.create_time, c.update_time, c.message_count, c.preview "
                    "FROM pinned_conversations p "
                    "JOIN conversations c ON c.id = p.conversation_id "
                    "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                    "WHERE COALESCE(cm.deleted, 0) = 0 "
                    "ORDER BY p.order_index ASC, p.pinned_at DESC "
                    "LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM pinned_conversations p "
                    "LEFT JOIN conversation_meta cm ON cm.conversation_id = p.conversation_id "
                    "WHERE COALESCE(cm.deleted, 0) = 0"
                ).fetchone()[0]
                self.send_json({"conversations": [dict(r) for r in rows],
                                "total": total, "offset": offset, "limit": limit})
                return

            where_clauses = []
            if view == "deleted":
                where_clauses.append("COALESCE(cm.deleted, 0) = 1")
            else:
                where_clauses.append("COALESCE(cm.deleted, 0) = 0")

            if view == "archived":
                where_clauses.append("COALESCE(cm.archived, 0) = 1")
            elif view == "all":
                pass
            elif view == "deleted":
                pass
            else:
                where_clauses.append("COALESCE(cm.archived, 0) = 0")

            if view == "recent":
                where_clauses.append(
                    "c.id NOT IN (SELECT conversation_id FROM pinned_conversations)"
                )
            where_sql = " AND ".join(where_clauses)

            if q:
                like = f"%{q}%"
                if len(q) >= 3:
                    try:
                        order_sql = (
                            "ORDER BY CASE WHEN p.conversation_id IS NULL THEN 1 ELSE 0 END, "
                            "p.order_index ASC, c.update_time DESC, c.create_time DESC"
                        ) if pinned_first else "ORDER BY c.update_time DESC, c.create_time DESC"
                        rows = conn.execute(
                            "SELECT c.id, COALESCE(NULLIF(cm.custom_title, ''), c.title) AS title, c.create_time, c.update_time, c.message_count, c.preview "
                            "FROM conversations c "
                            "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                            "LEFT JOIN pinned_conversations p ON p.conversation_id = c.id "
                            "WHERE " + where_sql + " AND c.id IN ("
                            "  SELECT conversation_id FROM search_index WHERE search_index MATCH ? "
                            "  UNION "
                            "  SELECT c2.id FROM conversations c2 "
                            "  LEFT JOIN conversation_meta cm2 ON cm2.conversation_id = c2.id "
                            "  WHERE COALESCE(cm2.deleted, 0) = 0 AND COALESCE(NULLIF(cm2.custom_title, ''), c2.title) LIKE ?"
                            ") "
                            + order_sql + " "
                            "LIMIT ? OFFSET ?",
                            (q, like, limit, offset),
                        ).fetchall()
                        total = conn.execute(
                            "SELECT COUNT(*) FROM conversations c "
                            "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                            "WHERE " + where_sql + " AND c.id IN ("
                            "  SELECT conversation_id FROM search_index WHERE search_index MATCH ? "
                            "  UNION "
                            "  SELECT c2.id FROM conversations c2 "
                            "  LEFT JOIN conversation_meta cm2 ON cm2.conversation_id = c2.id "
                            "  WHERE COALESCE(cm2.deleted, 0) = 0 AND COALESCE(NULLIF(cm2.custom_title, ''), c2.title) LIKE ?"
                            ")",
                            (q, like),
                        ).fetchone()[0]
                    except Exception:
                        order_sql = (
                            "ORDER BY CASE WHEN p.conversation_id IS NULL THEN 1 ELSE 0 END, "
                            "p.order_index ASC, c.update_time DESC, c.create_time DESC"
                        ) if pinned_first else "ORDER BY c.update_time DESC, c.create_time DESC"
                        rows = conn.execute(
                            "SELECT c.id, COALESCE(NULLIF(cm.custom_title, ''), c.title) AS title, c.create_time, c.update_time, c.message_count, c.preview "
                            "FROM conversations c "
                            "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                            "LEFT JOIN pinned_conversations p ON p.conversation_id = c.id "
                            "WHERE " + where_sql + " AND COALESCE(NULLIF(cm.custom_title, ''), c.title) LIKE ? "
                            + order_sql + " LIMIT ? OFFSET ?",
                            (like, limit, offset),
                        ).fetchall()
                        total = conn.execute(
                            "SELECT COUNT(*) FROM conversations c "
                            "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                            "WHERE " + where_sql + " AND COALESCE(NULLIF(cm.custom_title, ''), c.title) LIKE ?",
                            (like,)
                        ).fetchone()[0]
                else:
                    order_sql = (
                        "ORDER BY CASE WHEN p.conversation_id IS NULL THEN 1 ELSE 0 END, "
                        "p.order_index ASC, c.update_time DESC, c.create_time DESC"
                    ) if pinned_first else "ORDER BY c.update_time DESC, c.create_time DESC"
                    rows = conn.execute(
                        "SELECT c.id, COALESCE(NULLIF(cm.custom_title, ''), c.title) AS title, c.create_time, c.update_time, c.message_count, c.preview "
                        "FROM conversations c "
                        "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                        "LEFT JOIN pinned_conversations p ON p.conversation_id = c.id "
                        "WHERE " + where_sql + " AND COALESCE(NULLIF(cm.custom_title, ''), c.title) LIKE ? "
                        + order_sql + " LIMIT ? OFFSET ?",
                        (like, limit, offset),
                    ).fetchall()
                    total = conn.execute(
                        "SELECT COUNT(*) FROM conversations c "
                        "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                        "WHERE " + where_sql + " AND COALESCE(NULLIF(cm.custom_title, ''), c.title) LIKE ?",
                        (like,)
                    ).fetchone()[0]
            else:
                order_sql = (
                    "ORDER BY CASE WHEN p.conversation_id IS NULL THEN 1 ELSE 0 END, "
                    "p.order_index ASC, c.update_time DESC, c.create_time DESC"
                ) if pinned_first else "ORDER BY c.update_time DESC, c.create_time DESC"
                rows = conn.execute(
                    "SELECT c.id, COALESCE(NULLIF(cm.custom_title, ''), c.title) AS title, c.create_time, c.update_time, c.message_count, c.preview "
                    "FROM conversations c "
                    "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                    "LEFT JOIN pinned_conversations p ON p.conversation_id = c.id "
                    "WHERE " + where_sql + " "
                    + order_sql + " "
                    "LIMIT ? OFFSET ?", (limit, offset),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM conversations c "
                    "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                    "WHERE " + where_sql
                ).fetchone()[0]
            self.send_json({"conversations": [dict(r) for r in rows],
                            "total": total, "offset": offset, "limit": limit})
        finally:
            conn.close()

    def _api_detail(self, conv_id):
        conn = open_db(self.db_path)
        try:
            conv = conn.execute(
                "SELECT c.id, COALESCE(NULLIF(cm.custom_title, ''), c.title) AS title, c.create_time, c.update_time, c.message_count, c.preview "
                "FROM conversations c LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                "WHERE c.id = ? AND COALESCE(cm.deleted, 0) = 0", (conv_id,)
            ).fetchone()
            if not conv:
                self.send_error(404); return
            msgs = conn.execute(
                "SELECT seq, role, content, attachments, artifact_ids, siblings, branch_index, create_time "
                "FROM messages WHERE conversation_id = ? ORDER BY seq", (conv_id,),
            ).fetchall()
            def parse_msg(m):
                d = dict(m)
                for field in ("attachments", "siblings", "artifact_ids"):
                    raw = d.get(field)
                    if raw:
                        try: d[field] = json.loads(raw)
                        except: d[field] = [] if field != "siblings" else None
                    else:
                        d[field] = [] if field != "siblings" else None
                return d
            try:
                art_rows = conn.execute(
                    "SELECT id, title, type, lang FROM artifacts WHERE conv_id = ?", (conv_id,),
                ).fetchall()
                artifacts_meta = {r["id"]: dict(r) for r in art_rows}
            except Exception:
                artifacts_meta = {}
            self.send_json({"conversation": dict(conv),
                            "messages": [parse_msg(m) for m in msgs],
                            "artifacts": artifacts_meta})
        finally:
            conn.close()

    def _api_search(self, qs):
        q     = ((qs.get("q") or [""])[0]).strip()
        limit = int((qs.get("limit") or ["40"])[0])
        if not q:
            self.send_json({"results": [], "q": q}); return
        conn = open_db(self.db_path)
        try:
            # Step 1: find matching conversations via FTS or title LIKE
            conv_ids = []
            conv_ids_set = set()
            if len(q) >= 3:
                try:
                    for r in conn.execute(
                        "SELECT DISTINCT conversation_id FROM search_index WHERE search_index MATCH ? LIMIT 30",
                        (q,)
                    ).fetchall():
                        if r[0] not in conv_ids_set:
                            conv_ids.append(r[0]); conv_ids_set.add(r[0])
                except Exception:
                    pass
            # Always add title LIKE matches
            for r in conn.execute(
                "SELECT id FROM conversations WHERE title LIKE ? LIMIT 15",
                (f"%{q}%",)
            ).fetchall():
                if r[0] not in conv_ids_set:
                    conv_ids.append(r[0]); conv_ids_set.add(r[0])

            # Step 2: for each conv, find messages matching query
            results = []
            for conv_id in conv_ids[:25]:
                conv = conn.execute(
                    "SELECT title FROM conversations WHERE id = ?", (conv_id,)
                ).fetchone()
                if not conv: continue
                conv_title = conv["title"]
                msg_rows = conn.execute(
                    "SELECT seq, role, content FROM messages "
                    "WHERE conversation_id = ? AND content LIKE ? ORDER BY seq LIMIT 3",
                    (conv_id, f"%{q}%")
                ).fetchall()
                if msg_rows:
                    for m in msg_rows:
                        snippet = _extract_snippet(m["content"] or "", q, 100)
                        results.append({
                            "conv_id":    conv_id,
                            "conv_title": conv_title,
                            "seq":        m["seq"],
                            "role":       m["role"],
                            "snippet":    snippet,
                        })
                else:
                    # Title matched but no message body match → add title-level result
                    results.append({
                        "conv_id":    conv_id,
                        "conv_title": conv_title,
                        "seq":        None,
                        "role":       None,
                        "snippet":    "",
                    })
            self.send_json({"results": results[:limit], "q": q})
        finally:
            conn.close()

    def _api_gallery(self):
        image_exts = frozenset({"png", "jpg", "jpeg", "webp", "gif", "bmp", "svg", "heic", "heif"})

        def is_image_like(name: str, file_type: str) -> bool:
            file_type = (file_type or "").strip().lower().lstrip(".")
            name_ext = Path(name or "").suffix.lower().lstrip(".")
            return file_type == "image" or file_type in image_exts or name_ext in image_exts

        def ensure_group(groups: dict, conv_id: str, title: str, update_time, message_count, preview: str):
            group = groups.get(conv_id)
            if group is None:
                group = {
                    "conv_id": conv_id,
                    "conv_title": title,
                    "update_time": update_time,
                    "message_count": message_count,
                    "preview": preview or "",
                    "items": [],
                }
                groups[conv_id] = group
            return group

        def finalize_groups(groups: dict) -> list[dict]:
            out = list(groups.values())
            for group in out:
                group["items"].sort(
                    key=lambda item: (
                        item.get("seq") is None,
                        item.get("seq") or 0,
                        item.get("name") or "",
                    )
                )
            out.sort(key=lambda group: group.get("update_time") or 0, reverse=True)
            return out

        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT m.conversation_id AS conv_id, m.seq, m.role, m.content, m.attachments, "
                "c.title AS conv_title, c.update_time, c.message_count, c.preview, "
                "COALESCE(NULLIF(cm.custom_title, ''), c.title) AS display_title "
                "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                "WHERE COALESCE(cm.deleted, 0) = 0 "
                "ORDER BY c.update_time DESC, m.seq"
            ).fetchall()

            image_groups = {}
            attachment_groups = {}
            linked_image_names = set()

            for r in rows:
                conv_id = r["conv_id"]
                title = r["display_title"] or r["conv_title"] or conv_id
                base_item = {
                    "conv_id": conv_id,
                    "conv_title": title,
                    "update_time": r["update_time"],
                    "message_count": r["message_count"],
                    "preview": r["preview"] or "",
                    "seq": r["seq"],
                    "role": r["role"],
                    "context": (r["content"] or "").strip()[:120],
                }
                try:
                    attachments = json.loads(r["attachments"] or "[]")
                except Exception:
                    attachments = []
                for att in attachments:
                    if not isinstance(att, dict):
                        continue
                    name = (att.get("name") or att.get("file_name") or "").strip()
                    file_type = (att.get("type") or att.get("file_type") or "").strip()
                    content = (att.get("content") or "").strip()
                    item = {
                        **base_item,
                        "name": name or "Attachment",
                        "type": file_type,
                        "has_content": bool(content),
                    }
                    if is_image_like(name, file_type):
                        item["kind"] = "image"
                        linked_image_names.add(_norm_filename(name or item["name"]))
                        ensure_group(image_groups, conv_id, title, r["update_time"], r["message_count"], r["preview"])["items"].append(item)
                    else:
                        item["kind"] = "attachment"
                        ensure_group(attachment_groups, conv_id, title, r["update_time"], r["message_count"], r["preview"])["items"].append(item)

            artifact_groups = {}
            art_rows = conn.execute(
                "SELECT a.id, a.conv_id, a.msg_seq, a.title, a.type, a.lang, a.content, "
                "c.title AS conv_title, c.update_time, c.message_count, c.preview, "
                "COALESCE(NULLIF(cm.custom_title, ''), c.title) AS display_title "
                "FROM artifacts a JOIN conversations c ON c.id = a.conv_id "
                "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                "WHERE COALESCE(cm.deleted, 0) = 0 "
                "ORDER BY c.update_time DESC, a.msg_seq, a.id"
            ).fetchall()
            for r in art_rows:
                title = r["display_title"] or r["conv_title"] or r["conv_id"]
                ensure_group(artifact_groups, r["conv_id"], title, r["update_time"], r["message_count"], r["preview"])["items"].append({
                    "conv_id": r["conv_id"],
                    "conv_title": title,
                    "update_time": r["update_time"],
                    "message_count": r["message_count"],
                    "preview": r["preview"] or "",
                    "artifact_id": r["id"],
                    "msg_seq": r["msg_seq"],
                    "seq": r["msg_seq"],
                    "name": r["title"] or r["id"],
                    "type": r["type"] or "artifact",
                    "lang": r["lang"] or "",
                    "context": (r["content"] or "").strip()[:120],
                    "kind": "artifact",
                })

            unlinked_images = []
            source_dirs = [
                Path(__file__).parent / "source" / "dalle-generations",
                Path(__file__).parent / "source" / "files",
            ]
            for src_dir in source_dirs:
                if not src_dir.exists():
                    continue
                for f in sorted(src_dir.iterdir()):
                    if not f.is_file() or f.name.startswith('.'):
                        continue
                    if f.suffix.lower().lstrip(".") not in image_exts:
                        continue
                    if src_dir.name == "files" and _norm_filename(f.name) in linked_image_names:
                        continue
                    unlinked_images.append({
                        "filename": f.name,
                        "url": f"/source/{f.name}" if src_dir.name == "dalle-generations" else "/files/" + urllib.parse.quote(f.name),
                    })

            self.send_json({
                "sections": [
                    {
                        "key": "images",
                        "title": "Conversation Images",
                        "meta": "Images that can jump back to their source conversation.",
                        "count": sum(len(group["items"]) for group in image_groups.values()),
                        "groups": finalize_groups(image_groups),
                    },
                    {
                        "key": "attachments",
                        "title": "Attachments",
                        "meta": "All attachment chips grouped by conversation.",
                        "count": sum(len(group["items"]) for group in attachment_groups.values()),
                        "groups": finalize_groups(attachment_groups),
                    },
                    {
                        "key": "artifacts",
                        "title": "Artifacts",
                        "meta": "Claude-generated artifacts grouped by the conversation that created them.",
                        "count": sum(len(group["items"]) for group in artifact_groups.values()),
                        "groups": finalize_groups(artifact_groups),
                    },
                ],
                "unlinked_images": unlinked_images,
            })
        finally:
            conn.close()

    def _api_attachment_report(self):
        """Return all attachments that couldn't be displayed (no content + not in source/files/)."""
        files_dir = Path(__file__).parent / "source" / "files"
        local_norms = set()
        if files_dir.exists():
            for f in files_dir.iterdir():
                if f.is_file() and not f.name.startswith('.'):
                    local_norms.add(_norm_filename(f.name))
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT m.seq, m.role, m.content, m.attachments, c.id AS conv_id, c.title AS conv_title "
                "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                "WHERE m.attachments IS NOT NULL AND m.attachments != '[]' AND m.attachments != '' "
                "ORDER BY c.update_time DESC, m.seq"
            ).fetchall()
        finally:
            conn.close()

        missing = []
        seen = set()
        for r in rows:
            try:
                atts = json.loads(r["attachments"] or "[]")
            except Exception:
                continue
            for a in atts:
                name = a.get("name") or ""
                if not name:
                    continue
                has_content = bool(a.get("content"))
                norm = _norm_filename(name)
                in_local = norm in local_norms
                if not has_content and not in_local:
                    key = (r["conv_id"], name)
                    if key in seen:
                        continue
                    seen.add(key)
                    ctx = (r["content"] or "").strip()[:80]
                    missing.append({
                        "conv_id":    r["conv_id"],
                        "conv_title": r["conv_title"],
                        "seq":        r["seq"],
                        "role":       r["role"],
                        "file_name":  name,
                        "file_type":  a.get("type") or "",
                        "context":    ctx,
                    })
        self.send_json({"missing": missing, "total": len(missing)})

    def _api_memories(self):
        conn = open_db(self.db_path)
        try:
            rows = conn.execute("SELECT id, content FROM memories ORDER BY id").fetchall()
            self.send_json({"memories": [dict(r) for r in rows]})
        except Exception:
            self.send_json({"memories": []})
        finally:
            conn.close()

    def _api_projects(self):
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT p.id, p.name, p.description, p.create_time, p.update_time, "
                "COUNT(d.id) AS doc_count "
                "FROM projects p LEFT JOIN project_docs d ON d.project_id = p.id "
                "GROUP BY p.id "
                "ORDER BY p.update_time DESC"
            ).fetchall()
            self.send_json({"projects": [dict(r) for r in rows]})
        except Exception:
            self.send_json({"projects": []})
        finally:
            conn.close()

    def _api_project(self, project_id):
        conn = open_db(self.db_path)
        try:
            proj = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not proj:
                self.send_error(404); return
            docs = conn.execute(
                "SELECT id, filename, content, create_time FROM project_docs "
                "WHERE project_id = ? ORDER BY create_time", (project_id,),
            ).fetchall()
            self.send_json({"project": dict(proj), "docs": [dict(d) for d in docs]})
        finally:
            conn.close()

    def _api_artifact(self, artifact_id):
        conn = open_db(self.db_path)
        try:
            row = conn.execute(
                "SELECT id, conv_id, msg_seq, title, type, lang, content "
                "FROM artifacts WHERE id = ?", (artifact_id,),
            ).fetchone()
            if row:
                self.send_json(dict(row))
            else:
                self.send_error(404)
        finally:
            conn.close()

    def _api_preferences(self):
        conn = open_db(self.db_path)
        try:
            rows = conn.execute("SELECT pref_key, pref_value FROM ui_preferences").fetchall()
            prefs = {}
            for r in rows:
                try:
                    prefs[r["pref_key"]] = json.loads(r["pref_value"])
                except Exception:
                    prefs[r["pref_key"]] = r["pref_value"]
            self.send_json({"preferences": prefs})
        finally:
            conn.close()

    def _api_preferences_update(self):
        payload = self._read_json_body()
        prefs = payload.get("preferences") if isinstance(payload.get("preferences"), dict) else payload
        if not isinstance(prefs, dict):
            self.send_json({"error": "Invalid preferences payload"}, 400); return
        conn = open_db(self.db_path)
        try:
            for k, v in prefs.items():
                conn.execute(
                    "INSERT OR REPLACE INTO ui_preferences(pref_key, pref_value) VALUES (?, ?)",
                    (str(k), json.dumps(v, ensure_ascii=False)),
                )
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_pinned_list(self):
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT p.conversation_id, p.pinned_at, p.order_index, "
                "COALESCE(NULLIF(cm.custom_title, ''), c.title) AS title, c.update_time, c.message_count, c.preview "
                "FROM pinned_conversations p "
                "JOIN conversations c ON c.id = p.conversation_id "
                "LEFT JOIN conversation_meta cm ON cm.conversation_id = c.id "
                "WHERE COALESCE(cm.deleted, 0) = 0 "
                "ORDER BY p.order_index ASC, p.pinned_at DESC"
            ).fetchall()
            self.send_json({"pinned": [dict(r) for r in rows]})
        finally:
            conn.close()

    def _api_pinned_add(self):
        payload = self._read_json_body()
        conv_id = (payload.get("conversation_id") or "").strip()
        if not conv_id:
            self.send_json({"error": "missing conversation_id"}, 400); return
        conn = open_db(self.db_path)
        try:
            exists = conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            if not exists:
                self.send_json({"error": "conversation not found"}, 404); return
            next_idx = conn.execute("SELECT COALESCE(MAX(order_index), -1) + 1 FROM pinned_conversations").fetchone()[0]
            conn.execute(
                "INSERT OR REPLACE INTO pinned_conversations(conversation_id, pinned_at, order_index) VALUES (?, ?, ?)",
                (conv_id, time.time(), int(next_idx)),
            )
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_pinned_remove(self, conv_id):
        conn = open_db(self.db_path)
        try:
            conn.execute("DELETE FROM pinned_conversations WHERE conversation_id = ?", (conv_id,))
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_pinned_reorder(self):
        payload = self._read_json_body()
        ids = payload.get("ids") or []
        if not isinstance(ids, list):
            self.send_json({"error": "ids must be a list"}, 400); return
        conn = open_db(self.db_path)
        try:
            for i, cid in enumerate(ids):
                conn.execute(
                    "UPDATE pinned_conversations SET order_index = ? WHERE conversation_id = ?",
                    (i, str(cid)),
                )
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_tabs(self):
        conn = open_db(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, tab_type, conversation_id, artifact_id, title, pinned, sort_index, last_active_at "
                "FROM workspace_tabs WHERE closed = 0 ORDER BY pinned DESC, sort_index ASC, last_active_at DESC"
            ).fetchall()
            self.send_json({"tabs": [dict(r) for r in rows]})
        finally:
            conn.close()

    def _api_tabs_create(self):
        payload = self._read_json_body()
        tab_id = (payload.get("id") or str(uuid.uuid4())).strip()
        tab_type = (payload.get("tab_type") or "conversation").strip()
        conn = open_db(self.db_path)
        try:
            next_idx = conn.execute("SELECT COALESCE(MAX(sort_index), -1) + 1 FROM workspace_tabs WHERE closed = 0").fetchone()[0]
            conn.execute(
                "INSERT OR REPLACE INTO workspace_tabs(id, tab_type, conversation_id, artifact_id, title, pinned, sort_index, last_active_at, closed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    tab_id,
                    tab_type,
                    payload.get("conversation_id"),
                    payload.get("artifact_id"),
                    payload.get("title") or "",
                    1 if payload.get("pinned") else 0,
                    int(payload.get("sort_index", next_idx)),
                    float(payload.get("last_active_at", time.time())),
                ),
            )
            conn.commit()
            self.send_json({"ok": True, "id": tab_id})
        finally:
            conn.close()

    def _api_tab_update(self, tab_id):
        payload = self._read_json_body()
        if not tab_id:
            self.send_json({"error": "missing tab id"}, 400); return
        fields = []
        values = []
        for k in ("title", "conversation_id", "artifact_id", "sort_index", "tab_type"):
            if k in payload:
                fields.append(f"{k} = ?")
                values.append(payload[k])
        if "pinned" in payload:
            fields.append("pinned = ?")
            values.append(1 if payload.get("pinned") else 0)
        fields.append("last_active_at = ?")
        values.append(time.time())
        values.append(tab_id)
        conn = open_db(self.db_path)
        try:
            conn.execute(f"UPDATE workspace_tabs SET {', '.join(fields)} WHERE id = ?", tuple(values))
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_tab_remove(self, tab_id):
        conn = open_db(self.db_path)
        try:
            conn.execute("UPDATE workspace_tabs SET closed = 1 WHERE id = ?", (tab_id,))
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()

    def _api_conversation_update(self, conv_id):
        payload = self._read_json_body()
        if not conv_id:
            self.send_json({"error": "missing conversation id"}, 400); return
        rename = payload.get("title")
        archive = payload.get("archived")
        deleted = payload.get("deleted")
        conn = open_db(self.db_path)
        try:
            exists = conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            if not exists:
                self.send_json({"error": "conversation not found"}, 404); return
            conn.execute(
                "INSERT OR IGNORE INTO conversation_meta(conversation_id, custom_title, archived, deleted) VALUES (?, NULL, 0, 0)",
                (conv_id,),
            )
            if isinstance(rename, str):
                cleaned_title = rename.strip() or None
                conn.execute(
                    "UPDATE conversation_meta SET custom_title = ? WHERE conversation_id = ?",
                    (cleaned_title, conv_id),
                )
            if archive is not None:
                conn.execute(
                    "UPDATE conversation_meta SET archived = ? WHERE conversation_id = ?",
                    (1 if archive else 0, conv_id),
                )
            if deleted is not None:
                conn.execute(
                    "UPDATE conversation_meta SET deleted = ? WHERE conversation_id = ?",
                    (1 if deleted else 0, conv_id),
                )
            conn.commit()
            self.send_json({"ok": True})
        finally:
            conn.close()


def serve(port=8000, db_path=Path("history.db"), source_dir=Path("source")):
    _ensure_runtime_schema(db_path)
    _build_source_index(source_dir)
    Handler.db_path = db_path
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    serve()
