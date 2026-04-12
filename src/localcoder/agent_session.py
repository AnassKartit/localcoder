"""Append-only JSONL session persistence.

Each session is a .jsonl file under ~/.localcoder/sessions/.
Every message, compaction event, and model change is persisted as it happens.
Sessions can be resumed with -c, listed, or inspected.
"""

import json
import os
import time
from pathlib import Path

SESSIONS_DIR = Path.home() / ".localcoder" / "sessions"


def _ensure_dir():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def new_session_id():
    """Generate a timestamp-based session ID."""
    return time.strftime("%Y%m%d_%H%M%S")


class Session:
    """Append-only JSONL session file."""

    def __init__(self, session_id=None, cwd=None, model=None):
        _ensure_dir()
        self.session_id = session_id or new_session_id()
        self.path = SESSIONS_DIR / f"{self.session_id}.jsonl"
        self._compacted_at = 0  # line index where compaction occurred
        self._messages = []  # in-memory cache

        # Write session header if new
        if not self.path.exists():
            self._append({
                "type": "session_info",
                "session_id": self.session_id,
                "cwd": cwd or os.getcwd(),
                "model": model or "",
                "started_at": time.time(),
                "started_at_human": time.strftime("%Y-%m-%d %H:%M:%S"),
            })

    def _append(self, entry):
        """Append a single JSON entry to the session file."""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def add_message(self, message):
        """Persist a message (system/user/assistant/tool) to the session."""
        self._messages.append(message)
        self._append({
            "type": "message",
            "ts": time.time(),
            "message": _serialize_message(message),
        })

    def add_compaction(self, summary_text, compacted_count):
        """Record a compaction event. The summary replaces old messages."""
        self._compacted_at = len(self._messages)
        self._append({
            "type": "compaction",
            "ts": time.time(),
            "summary": summary_text,
            "compacted_count": compacted_count,
        })

    def add_model_change(self, old_model, new_model):
        """Record a model switch."""
        self._append({
            "type": "model_change",
            "ts": time.time(),
            "old": old_model,
            "new": new_model,
        })

    def add_event(self, event_type, data=None):
        """Record a generic event (error, interrupt, etc.)."""
        self._append({
            "type": event_type,
            "ts": time.time(),
            "data": data or {},
        })

    @property
    def messages(self):
        """Return the current message list (post-compaction view)."""
        return list(self._messages)

    @classmethod
    def load(cls, session_id):
        """Load a session from disk, reconstructing message history."""
        path = SESSIONS_DIR / f"{session_id}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"Session {session_id} not found")

        session = cls.__new__(cls)
        session.session_id = session_id
        session.path = path
        session._messages = []
        session._compacted_at = 0

        cwd = None
        model = None
        compaction_summary = None

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = entry.get("type")
                if etype == "session_info":
                    cwd = entry.get("cwd")
                    model = entry.get("model")
                elif etype == "message":
                    msg = entry.get("message", {})
                    session._messages.append(msg)
                elif etype == "compaction":
                    # After compaction, we rebuild from the summary
                    compaction_summary = entry.get("summary", "")
                    compacted_count = entry.get("compacted_count", 0)
                    session._compacted_at = len(session._messages)
                elif etype == "model_change":
                    model = entry.get("new")

        return session, cwd, model

    def get_messages_for_continuation(self):
        """Get messages suitable for continuing a session.

        If compaction occurred, returns:
          [system_msg, compaction_summary_as_user, assistant_ack, ...post_compaction_msgs]
        Otherwise returns all messages.
        """
        return list(self._messages)


def get_latest_session_id():
    """Return the most recent session ID, or None."""
    _ensure_dir()
    sessions = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sessions:
        return None
    return sessions[0].stem


def list_sessions(limit=10):
    """List recent sessions with metadata."""
    _ensure_dir()
    sessions = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    results = []
    for path in sessions[:limit]:
        try:
            with open(path, "r") as f:
                first_line = f.readline().strip()
                info = json.loads(first_line) if first_line else {}

            # Count messages
            msg_count = 0
            with open(path, "r") as f:
                for line in f:
                    if '"type": "message"' in line or '"type":"message"' in line:
                        msg_count += 1

            results.append({
                "id": path.stem,
                "cwd": info.get("cwd", "?"),
                "model": info.get("model", "?"),
                "started": info.get("started_at_human", "?"),
                "messages": msg_count,
                "size_kb": path.stat().st_size // 1024,
            })
        except Exception:
            continue
    return results


def _serialize_message(msg):
    """Serialize a message dict, handling non-JSON-safe content."""
    if isinstance(msg, dict):
        result = {}
        for k, v in msg.items():
            if k == "content" and isinstance(v, list):
                # Multi-part content (text + images) — keep text, drop base64
                parts = []
                for part in v:
                    if isinstance(part, dict):
                        if part.get("type") == "image_url":
                            parts.append({"type": "image_url", "image_url": {"url": "[base64 image omitted]"}})
                        else:
                            parts.append(part)
                    else:
                        parts.append(part)
                result[k] = parts
            else:
                result[k] = v
        return result
    return msg
