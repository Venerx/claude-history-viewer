#!/usr/bin/env python3
"""
Parse source/conversations.json → SQLite with FTS5.
Supports both ChatGPT and Claude export formats.

Schema
------
conversations(id, title, create_time, update_time, message_count, preview)
messages(id, conversation_id, role, content, create_time, seq)
search_index [fts5](conversation_id UNINDEXED, title, body)

Can also be run directly:
    python3 build_db.py [--source source/conversations.json] [--db history.db]
"""
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# ── Content-type handling ──────────────────────────────────────────────────────

# Roles whose messages we never show
SKIP_ROLES = frozenset({"system", "tool"})

# Content types we silently ignore
SKIP_CONTENT_TYPES = frozenset({
    "thoughts",
    "reasoning_recap",
    "user_editable_context",
    "tether_browsing_display",
    "tether_quote",
    "code",           # tool invocation JSON (search, browser, etc.) — not user-facing
})


def message_text(content: dict) -> str:
    """Return displayable markdown text from a message content dict."""
    ct = (content or {}).get("content_type", "")

    if ct == "text":
        parts = content.get("parts", [])
        chunks = []
        for p in parts:
            if isinstance(p, str):
                # Canvas artifact parts arrive as a JSON string:
                # '{"name":"...", "type":"document", "content":"# markdown..."}'
                # Unwrap the inner markdown so it renders properly.
                stripped = p.strip()
                if stripped.startswith('{') and '"type"' in stripped and '"content"' in stripped:
                    try:
                        obj = json.loads(stripped)
                        if obj.get("type") in ("document", "canvas", "artifact") and isinstance(obj.get("content"), str):
                            chunks.append(obj["content"].strip())
                            continue
                    except (json.JSONDecodeError, AttributeError):
                        pass
                chunks.append(p)
            elif isinstance(p, dict):
                # Canvas / document artifacts embedded as dict parts
                doc = p.get("content") or p.get("text") or ""
                if isinstance(doc, str) and doc.strip():
                    chunks.append(doc.strip())
        return "\n\n".join(chunks).strip()

    if ct == "code":
        # Code interpreter / tool output — stored in content["text"] directly
        body = (content.get("text") or "").strip()
        lang = (content.get("language") or "").strip()
        return f"```{lang}\n{body}\n```" if body else ""

    if ct == "multimodal_text":
        chunks = []
        for p in content.get("parts", []):
            if isinstance(p, str):
                chunks.append(p)
            elif isinstance(p, dict):
                if p.get("content_type") == "image_asset_pointer":
                    ptr = p.get("asset_pointer", "")
                    # Strip URI schemes: sediment://file_XXX or file-service://file-XXX
                    file_id = (
                        ptr.replace("file-service://", "")
                           .replace("sediment://", "")
                           .split("#")[0]
                    )
                    if file_id:
                        chunks.append(f"![image](/source/{file_id})")
                elif p.get("type") in ("document", "canvas", "artifact"):
                    # Explicit canvas/document parts
                    doc = p.get("content") or p.get("text") or ""
                    if isinstance(doc, str) and doc.strip():
                        chunks.append(doc.strip())
                else:
                    text = p.get("text") or p.get("content") or ""
                    if isinstance(text, str) and text:
                        chunks.append(text)
        return "\n".join(c for c in chunks if c).strip()

    # Generic fallback: unknown content types that carry text directly
    # (e.g. content_type="document" or missing content_type)
    for key in ("text", "content"):
        val = content.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    return ""


# ── Format detection ──────────────────────────────────────────────────────────


def detect_format(data: list) -> str:
    """Return 'claude' or 'chatgpt' based on the first conversation's keys."""
    if not data:
        return "chatgpt"
    first = data[0]
    if "chat_messages" in first or ("uuid" in first and "name" in first):
        return "claude"
    return "chatgpt"


# ── Claude format helpers ─────────────────────────────────────────────────────

# Claude content block types to skip
CLAUDE_SKIP_TYPES = frozenset({"thinking", "tool_use", "tool_result", "token_budget"})


def _iso_to_ts(s: str) -> float:
    """Convert ISO 8601 string (with optional Z) to a unix timestamp float."""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


_IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "heif"})


def claude_message_parts(msg: dict) -> tuple[str, list]:
    """
    Extract (text, attachments) from a Claude chat message.

    Returns:
        text        – plain message text only (no attachment content embedded)
        attachments – list of {name, type, content} dicts for chips/side-panel
    """
    is_assistant = msg.get("sender", msg.get("role", "")) == "assistant"
    text_chunks = []
    for block in (msg.get("content") or []):
        if not isinstance(block, dict):
            continue
        if block.get("type") in CLAUDE_SKIP_TYPES:
            continue
        if block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                text_chunks.append(text)
    # NOTE: Never fall back to top-level msg["text"] for assistant messages.
    # That field contains Claude's internal verbalized thinking/planning notes,
    # not the user-facing response. For human messages it's also redundant
    # (human content blocks always have the text).  Drop the fallback entirely.

    # Build attachment list (each has name, type, content for side-panel).
    att_bag: Counter = Counter()
    attachments = []
    for att in (msg.get("attachments") or []):
        extracted = (att.get("extracted_content") or "").strip()
        name  = (att.get("file_name") or "").strip()
        ftype = (att.get("file_type") or "").strip()
        if extracted:
            # Use a friendly display name: prefer file_name, else "Pasted text", else type
            display_name = name or ("Pasted text" if ftype in ("txt", "text") else ftype or "file")
            attachments.append({"name": display_name, "type": ftype, "content": extracted})
            att_bag[name] += 1

    # Images / files without extracted content: chip referencing file by uuid.
    for fi in (msg.get("files") or []):
        fn   = (fi.get("file_name") or "").strip()
        uuid = (fi.get("file_uuid") or "").strip()
        ext  = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
        if att_bag[fn] > 0:
            att_bag[fn] -= 1
            continue
        display = fn or "Image"
        attachments.append({
            "name": display,
            "type": ext or "image",
            "content": "",
            "uuid": uuid,   # kept so frontend can request /source/files/<uuid>
        })

    # For assistant messages: extract output files from present_files tool_result
    # These are Claude-generated files (docx, etc.) referenced via local_resource blocks.
    if is_assistant:
        for block in (msg.get("content") or []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            rc = block.get("content") or []
            if isinstance(rc, str):
                continue
            for item in (rc if isinstance(rc, list) else []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "local_resource":
                    fp = item.get("file_path") or ""
                    ext = fp.rsplit(".", 1)[-1].lower() if "." in fp else ""
                    uuid = (item.get("uuid") or "").strip()
                    # Use the human-readable display name from the export
                    display_name = (item.get("name") or "").strip()
                    if not display_name:
                        display_name = fp.split("/")[-1] if fp else ""
                    if display_name:
                        # Append extension if not already present
                        if ext and not display_name.lower().endswith("." + ext):
                            display_name = f"{display_name}.{ext}"
                        attachments.append({
                            "name": display_name,
                            "type": ext,
                            "content": "",
                            "uuid": uuid,
                        })

    return "\n\n".join(text_chunks), attachments


def claude_artifact_ids(msg: dict) -> list[str]:
    """Return list of artifact IDs created in this assistant message."""
    ids = []
    for block in (msg.get("content") or []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "artifacts":
            inp = block.get("input") or {}
            if inp.get("command") == "create" and inp.get("id"):
                ids.append(inp["id"])
    return ids


def _apply_artifact_ops(msg: dict, msg_seq: int, state: dict):
    """
    Apply artifact operations from one assistant message to the running state dict.
    state: {artifact_id: {title, type, lang, content, msg_seq}}
    Artifacts are created once and updated/rewritten across subsequent messages.
    """
    for block in (msg.get("content") or []):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "artifacts":
            inp = block.get("input") or {}
            aid = inp.get("id")
            if not aid:
                continue
            c = inp.get("command", "")
            if c == "create":
                state[aid] = {
                    "content": inp.get("content") or "",
                    "title":   inp.get("title") or "",
                    "type":    inp.get("type") or "",
                    "lang":    inp.get("language") or "",
                    "msg_seq": msg_seq,
                }
            elif aid in state:
                if c == "rewrite":
                    new_c = inp.get("content") or ""
                    if new_c:
                        state[aid]["content"] = new_c
                    if inp.get("title"):
                        state[aid]["title"] = inp["title"]
                    state[aid]["msg_seq"] = msg_seq
                elif c == "update":
                    old_s = inp.get("old_str", "")
                    new_s = inp.get("new_str", "")
                    content = state[aid]["content"]
                    if old_s in content:
                        state[aid]["content"] = content.replace(old_s, new_s, 1)
                    state[aid]["msg_seq"] = msg_seq


def parse_claude_conversation(conv: dict):
    """
    Parse a single Claude conversation dict using tree traversal.

    Claude's export stores all branch variants flat.  parent_message_uuid forms
    a tree; messages sharing the same UUID are human/assistant pairs in the same
    "turn slot".  Multiple children with the same (parent, sender) are retries.
    We walk the active path (always taking the LAST sibling = most recent edit)
    and record all siblings so the UI can show ← / → navigation.

    Returns (meta, msgs, artifacts) where artifacts is a list of artifact rows.
    """
    cid = conv.get("uuid")
    if not cid:
        return None, [], []

    raw_msgs = conv.get("chat_messages") or []
    if not raw_msgs:
        return None, [], []

    # All message UUIDs in this conversation.
    msg_uuid_set = {m.get("uuid") for m in raw_msgs}

    # Group by (parent_uuid, sender) to detect sibling branches.
    # Messages whose parent is NOT in msg_uuid_set are root-level entries.
    VIRTUAL_ROOT = "__ROOT__"
    by_ps: dict[tuple, list] = {}
    for m in raw_msgs:
        p = m.get("parent_message_uuid") or ""
        p_key = p if p in msg_uuid_set else VIRTUAL_ROOT
        s = m.get("sender", "")
        by_ps.setdefault((p_key, s), []).append(m)

    def get_last(parent_key: str, sender: str):
        group = by_ps.get((parent_key, sender), [])
        return group[-1] if group else None

    def get_all(parent_key: str, sender: str):
        return by_ps.get((parent_key, sender), [])

    # ── Walk the active path ───────────────────────────────────────────────────
    result: list[dict] = []
    artifacts_out: list[dict] = []
    artifact_state: dict = {}   # {artifact_id: {title, type, lang, content, msg_seq}}
    seen: set[str] = set()
    seq = 0

    cur_human = get_last(VIRTUAL_ROOT, "human")
    if not cur_human:
        return None, [], []

    for _ in range(300):          # hard iteration cap
        h_uuid = cur_human.get("uuid", "")
        if h_uuid in seen:
            break
        seen.add(h_uuid)

        # Siblings at this human-message slot (same parent, sender=human)
        p_of_h = cur_human.get("parent_message_uuid") or ""
        p_key_of_h = p_of_h if p_of_h in msg_uuid_set else VIRTUAL_ROOT
        siblings_h = get_all(p_key_of_h, "human")
        b_idx_h = siblings_h.index(cur_human) + 1
        b_cnt_h = len(siblings_h)

        h_text, h_atts = claude_message_parts(cur_human)

        siblings_h_data = None
        if b_cnt_h > 1:
            sibs = []
            for sib in siblings_h:
                st, sa = claude_message_parts(sib)
                # Include the assistant response that goes with this user message version
                sib_uuid = sib.get("uuid", "")
                sib_asst = get_last(sib_uuid, "assistant")
                asst_text, asst_atts = claude_message_parts(sib_asst) if sib_asst else ("", [])
                asst_artifact_ids = claude_artifact_ids(sib_asst) if sib_asst else []
                sibs.append({
                    "content":          st,
                    "attachments":      sa,
                    "asst_content":     asst_text,
                    "asst_attachments": asst_atts,
                    "asst_artifact_ids": asst_artifact_ids,
                })
            siblings_h_data = json.dumps(sibs, ensure_ascii=False)

        if h_text or h_atts:
            result.append({
                "conversation_id": cid,
                "role":            "user",
                "content":         h_text,
                "attachments":     json.dumps(h_atts, ensure_ascii=False) if h_atts else None,
                "siblings":        siblings_h_data,
                "branch_index":    b_idx_h,
                "create_time":     _iso_to_ts(cur_human.get("created_at") or ""),
                "seq":             seq,
            })
            seq += 1

        # Assistant response to this human message
        # Claude compute-tool chains produce multiple consecutive assistant messages:
        #   human → assistant_1(bash_tool only) → assistant_2(bash_tool + final text)
        # Also, some siblings (retries) are dead-ends; we must find the sibling
        # whose chain actually leads to the next human turn.
        all_direct_siblings = get_all(h_uuid, "assistant")
        first_asst = None
        asst_chain: list = []
        nxt_from_chain = None

        if all_direct_siblings:
            # Try siblings in reverse (most recent first); pick the first one
            # whose chain connects to a human continuation.
            for cand in reversed(all_direct_siblings):
                chain: list = [cand]
                for _d in range(100):  # safety cap on chain depth
                    chain_next = get_last(chain[-1].get("uuid", ""), "assistant")
                    if chain_next is None:
                        break
                    chain.append(chain_next)
                lid = chain[-1].get("uuid", "")
                nxt = get_last(lid, "human")
                if nxt is not None:
                    first_asst = cand
                    asst_chain = chain
                    nxt_from_chain = nxt
                    break
            if first_asst is None:
                # No sibling leads to a continuation — use the last sibling
                first_asst = all_direct_siblings[-1]
                asst_chain = [first_asst]
                for _d in range(100):
                    chain_next = get_last(asst_chain[-1].get("uuid", ""), "assistant")
                    if chain_next is None:
                        break
                    asst_chain.append(chain_next)

        if first_asst:
            leaf_uuid = asst_chain[-1].get("uuid", "")

            # Siblings/retries are always at the first level (direct children of human)
            siblings_a = all_direct_siblings
            b_idx_a = siblings_a.index(first_asst) + 1
            b_cnt_a = len(siblings_a)

            # Collect text, attachments, and artifact IDs from the whole chain
            all_text_chunks: list[str] = []
            all_atts: list = []
            all_artifact_ids: list = []
            for node in asst_chain:
                node_text, node_atts = claude_message_parts(node)
                if node_text:
                    all_text_chunks.append(node_text)
                all_atts.extend(node_atts)
                all_artifact_ids.extend(claude_artifact_ids(node))
                _apply_artifact_ops(node, seq, artifact_state)

            a_text = "\n\n".join(all_text_chunks)
            a_atts = all_atts
            a_artifact_ids = list(dict.fromkeys(all_artifact_ids))  # dedupe, preserve order

            siblings_a_data = None
            if b_cnt_a > 1:
                sibs = []
                for sib in siblings_a:
                    st, sa = claude_message_parts(sib)
                    sibs.append({"content": st, "attachments": sa,
                                 "artifact_ids": claude_artifact_ids(sib)})
                siblings_a_data = json.dumps(sibs, ensure_ascii=False)

            if a_text or a_atts or a_artifact_ids:
                result.append({
                    "conversation_id": cid,
                    "role":            "assistant",
                    "content":         a_text,
                    "attachments":     json.dumps(a_atts, ensure_ascii=False) if a_atts else None,
                    "artifact_ids":    json.dumps(a_artifact_ids, ensure_ascii=False) if a_artifact_ids else None,
                    "siblings":        siblings_a_data,
                    "branch_index":    b_idx_a,
                    "create_time":     _iso_to_ts(first_asst.get("created_at") or ""),
                    "seq":             seq,
                })
                seq += 1

            # Next human: from the leaf's continuation (found during sibling search)
            nxt = nxt_from_chain if nxt_from_chain else get_last(leaf_uuid, "human")
            if nxt is None:
                nxt = get_last(h_uuid, "human")
            cur_human = nxt
        else:
            # No assistant yet; check for chained human (rare)
            cur_human = get_last(h_uuid, "human")

        if cur_human is None:
            break

    # Convert accumulated artifact state → output rows
    for aid, a in artifact_state.items():
        artifacts_out.append({
            "id":      aid,
            "conv_id": cid,
            "msg_seq": a["msg_seq"],
            "title":   a["title"],
            "type":    a["type"],
            "lang":    a["lang"],
            "content": a["content"],
        })

    if not result:
        return None, [], []

    # Skip conversations that have no actual text content
    if not any(m["content"] for m in result):
        return None, [], []

    preview = next((m["content"][:300] for m in result if m["role"] == "user" and m["content"]), "")
    raw_name = (conv.get("name") or "").strip()
    if not raw_name and preview:
        raw_name = preview[:60].replace("\n", " ").strip()
    if not raw_name:
        raw_name = "Untitled"
    meta = {
        "id":            cid,
        "title":         raw_name,
        "create_time":   _iso_to_ts(conv.get("created_at") or ""),
        "update_time":   _iso_to_ts(conv.get("updated_at") or ""),
        "message_count": len(result),
        "preview":       preview,
    }
    return meta, result, artifacts_out


# ── Thread extraction ─────────────────────────────────────────────────────────


def extract_thread(mapping: dict, current_node: str) -> list:
    """
    Walk from current_node back to root via parent pointers.
    Returns messages in chronological order (root → leaf).
    Handles the branching case: current_node always points to the
    last message of the active branch.
    """
    path, seen = [], set()
    node_id = current_node
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        if node.get("message"):
            path.append(node["message"])
        node_id = node.get("parent")
    path.reverse()
    return path


# ── Per-conversation parser ───────────────────────────────────────────────────


def parse_conversation(conv: dict):
    """
    Returns (meta_dict, message_list) or (None, []) when the
    conversation has no usable messages.
    """
    cid = conv.get("id") or conv.get("conversation_id")
    if not cid:
        return None, []

    mapping     = conv.get("mapping") or {}
    current_node = conv.get("current_node")
    if not current_node:
        return None, []

    thread = extract_thread(mapping, current_node)

    msgs = []
    for seq, msg in enumerate(thread):
        role = (msg.get("author") or {}).get("role", "")
        if role in SKIP_ROLES:
            continue
        content = msg.get("content") or {}
        if content.get("content_type") in SKIP_CONTENT_TYPES:
            continue
        text = message_text(content)
        if not text:
            continue
        msgs.append({
            "conversation_id": cid,
            "role":            role,
            "content":         text,
            "attachments":     None,
            "siblings":        None,
            "branch_index":    1,
            "create_time":     msg.get("create_time") or 0,
            "seq":             seq,
        })

    if not msgs:
        return None, []

    preview = next(
        (m["content"][:300] for m in msgs if m["role"] == "user"), ""
    )
    meta = {
        "id":            cid,
        "title":         (conv.get("title") or "Untitled").strip(),
        "create_time":   conv.get("create_time") or 0,
        "update_time":   conv.get("update_time") or 0,
        "message_count": len(msgs),
        "preview":       preview,
    }
    return meta, msgs


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """\
DROP TABLE IF EXISTS messages;
DROP TABLE IF EXISTS conversations;
DROP TABLE IF EXISTS search_index;
DROP TABLE IF EXISTS memories;
DROP TABLE IF EXISTS project_docs;
DROP TABLE IF EXISTS projects;
DROP TABLE IF EXISTS conversation_meta;
DROP TABLE IF EXISTS pinned_conversations;
DROP TABLE IF EXISTS ui_preferences;
DROP TABLE IF EXISTS workspace_tabs;

CREATE TABLE conversations (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    create_time   REAL,
    update_time   REAL,
    message_count INTEGER,
    preview       TEXT
);

CREATE TABLE conversation_meta (
    conversation_id TEXT PRIMARY KEY,
    custom_title    TEXT,
    archived        INTEGER DEFAULT 0,
    deleted         INTEGER DEFAULT 0,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
);

CREATE TABLE pinned_conversations (
    conversation_id TEXT PRIMARY KEY,
    pinned_at       REAL,
    order_index     INTEGER,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
);

CREATE TABLE ui_preferences (
    pref_key   TEXT PRIMARY KEY,
    pref_value TEXT
);

CREATE TABLE workspace_tabs (
    id             TEXT PRIMARY KEY,
    tab_type       TEXT NOT NULL,
    conversation_id TEXT,
    artifact_id    TEXT,
    title          TEXT,
    pinned         INTEGER DEFAULT 0,
    sort_index     INTEGER DEFAULT 0,
    last_active_at REAL,
    closed         INTEGER DEFAULT 0
);

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role            TEXT,
    content         TEXT,
    attachments     TEXT,
    artifact_ids    TEXT,
    siblings        TEXT,
    branch_index    INTEGER DEFAULT 1,
    create_time     REAL,
    seq             INTEGER
);

CREATE INDEX idx_msg_conv ON messages (conversation_id, seq);

CREATE VIRTUAL TABLE search_index USING fts5(
    conversation_id UNINDEXED,
    title,
    body,
    tokenize = 'trigram'
);

CREATE TABLE memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_uuid TEXT,
    content      TEXT
);

CREATE TABLE projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    create_time  REAL,
    update_time  REAL
);

CREATE TABLE project_docs (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    filename   TEXT,
    content    TEXT,
    create_time REAL
);

DROP TABLE IF EXISTS artifacts;

CREATE TABLE artifacts (
    id       TEXT PRIMARY KEY,
    conv_id  TEXT NOT NULL,
    msg_seq  INTEGER,
    title    TEXT,
    type     TEXT,
    lang     TEXT,
    content  TEXT
);
"""


# ── Build ─────────────────────────────────────────────────────────────────────


def build(source: Path, db_path: Path) -> None:
    print(f"Loading {source} …", flush=True)
    with open(source, encoding="utf-8") as f:
        data = json.load(f)

    fmt   = detect_format(data)
    total = len(data)
    print(f"{total} conversations found ({fmt} format). Indexing…", flush=True)

    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)

    conv_rows, msg_rows, fts_rows = [], [], []
    artifact_rows: list[dict] = []

    for i, conv in enumerate(data):
        if i % 100 == 0:
            sys.stderr.write(f"\r  {i:>5}/{total}")
            sys.stderr.flush()

        if fmt == "claude":
            meta, msgs, artifacts = parse_claude_conversation(conv)
        else:
            meta, msgs = parse_conversation(conv)
            artifacts = []
        if meta is None:
            continue

        conv_rows.append(meta)
        msg_rows.extend(msgs)
        artifact_rows.extend(artifacts)
        fts_rows.append((
            meta["id"],
            meta["title"],
            "\n".join(m["content"] for m in msgs),
        ))

    sys.stderr.write(f"\r  {total}/{total}\n")

    db.executemany(
        "INSERT OR REPLACE INTO conversations "
        "VALUES (:id, :title, :create_time, :update_time, :message_count, :preview)",
        conv_rows,
    )
    db.executemany(
        "INSERT INTO messages (conversation_id, role, content, attachments, artifact_ids, siblings, branch_index, create_time, seq) "
        "VALUES (:conversation_id, :role, :content, :attachments, :artifact_ids, :siblings, :branch_index, :create_time, :seq)",
        [{**m, "artifact_ids": m.get("artifact_ids")} for m in msg_rows],
    )
    db.executemany("INSERT INTO search_index VALUES (?, ?, ?)", fts_rows)

    # Deduplicate artifacts by id (keep last version seen)
    seen_aids: set[str] = set()
    unique_artifacts = []
    for art in reversed(artifact_rows):
        if art["id"] not in seen_aids:
            seen_aids.add(art["id"])
            unique_artifacts.append(art)
    db.executemany(
        "INSERT OR REPLACE INTO artifacts (id, conv_id, msg_seq, title, type, lang, content) "
        "VALUES (:id, :conv_id, :msg_seq, :title, :type, :lang, :content)",
        unique_artifacts,
    )
    print(f"Artifacts indexed: {len(unique_artifacts)}")

    # ── Memories ──────────────────────────────────────────────────────────────
    memories_path = source.parent / "memories.json"
    if memories_path.exists():
        with open(memories_path, encoding="utf-8") as f:
            memories_data = json.load(f)
        if isinstance(memories_data, list):
            for entry in memories_data:
                content = (entry.get("conversations_memory") or "").strip()
                if content:
                    db.execute(
                        "INSERT INTO memories (account_uuid, content) VALUES (?, ?)",
                        (entry.get("account_uuid", ""), content),
                    )
        print(f"Memories indexed.")

    # ── Projects ──────────────────────────────────────────────────────────────
    projects_dir = source.parent / "projects"
    proj_count = 0
    if projects_dir.exists():
        for proj_file in sorted(projects_dir.glob("*.json")):
            with open(proj_file, encoding="utf-8") as f:
                proj = json.load(f)
            pid = proj.get("uuid")
            if not pid:
                continue
            db.execute(
                "INSERT OR REPLACE INTO projects VALUES (?, ?, ?, ?, ?)",
                (
                    pid,
                    (proj.get("name") or "Untitled Project").strip(),
                    (proj.get("description") or "").strip(),
                    _iso_to_ts(proj.get("created_at") or ""),
                    _iso_to_ts(proj.get("updated_at") or ""),
                ),
            )
            for doc in (proj.get("docs") or []):
                doc_id = doc.get("uuid")
                if not doc_id:
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO project_docs VALUES (?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        pid,
                        (doc.get("filename") or "").strip(),
                        (doc.get("content") or "").strip(),
                        _iso_to_ts(doc.get("created_at") or ""),
                    ),
                )
            proj_count += 1
        print(f"{proj_count} project(s) indexed.")

    # ── File index (removed — now handled inline above) ───────────────────────

    db.commit()
    db.close()
    print(f"Done — {len(conv_rows)} conversations indexed → {db_path}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Build ChatGPT history search database")
    ap.add_argument("--source", default="source/conversations.json",
                    help="Path to conversations.json  (default: source/conversations.json)")
    ap.add_argument("--db",     default="history.db",
                    help="Output SQLite database path  (default: history.db)")
    args = ap.parse_args()
    build(Path(args.source), Path(args.db))
