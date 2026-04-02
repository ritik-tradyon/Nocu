"""Incident Memory — learns from past Nocu analyses.

Stores every analysis interaction and enables:
1. Similar incident lookup by error codes + error classes + service
2. Known fix retrieval (what fixed it last time?)
3. Recurring error detection (is this pattern getting worse?)
4. Feedback loop (was the analysis useful? what was the actual fix?)

Storage: SQLite (zero config, stdlib, single file)
Retention: auto-prunes incidents older than configured window (default 60 days)
"""

import os
import json
import sqlite3
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("nocu.memory")


class IncidentMemory:
    """SQLite-backed incident memory with multi-signal similarity matching."""

    def __init__(self, db_path: str = ".nocu_memory/incidents.db", retention_days: int = 60):
        self.db_path = db_path
        self.retention_days = retention_days
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        self._prune_old()

    def _init_db(self):
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,

                    -- What was asked
                    question TEXT,
                    query_type TEXT,
                    service_name TEXT,
                    time_range TEXT,
                    search_terms TEXT,

                    -- What was found
                    error_codes TEXT,
                    error_classes TEXT,
                    nr_data_summary TEXT,
                    analyzer_used TEXT,
                    analysis TEXT,
                    code_references TEXT,
                    sources_used TEXT,

                    -- Matching
                    error_fingerprint TEXT,

                    -- Feedback
                    was_useful INTEGER,
                    actual_root_cause TEXT,
                    actual_fix TEXT,
                    resolution_notes TEXT,
                    resolved_at TEXT
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_service
                ON incidents(service_name)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_fingerprint
                ON incidents(error_fingerprint)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp
                ON incidents(timestamp)
            """)

            # User conversation history
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    incident_id TEXT,
                    service_name TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_messages_user
                ON user_messages(user_id, timestamp)
            """)

    def _prune_old(self):
        """Delete incidents and messages older than retention window."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        ).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            deleted_incidents = conn.execute(
                "DELETE FROM incidents WHERE timestamp < ?", (cutoff,)
            ).rowcount
            deleted_messages = conn.execute(
                "DELETE FROM user_messages WHERE timestamp < ?", (cutoff,)
            ).rowcount

        total = deleted_incidents + deleted_messages
        if total > 0:
            logger.info(
                f"[memory] Pruned {deleted_incidents} incidents, "
                f"{deleted_messages} messages older than {self.retention_days} days"
            )

    # ──────────────────────────────────────────────
    # Store
    # ──────────────────────────────────────────────

    def store_incident(
        self,
        question: str,
        query_type: str,
        service_name: str,
        time_range: str,
        search_terms: list[str],
        error_codes: list[str],
        error_classes: list[str],
        nr_data_summary: str,
        analyzer_used: str,
        analysis: str,
        code_references: list[str] = None,
        sources_used: list[str] = None,
    ) -> str:
        """Store a new incident from a completed analysis.

        Returns the incident ID for feedback commands.
        """
        now = datetime.now(timezone.utc).isoformat()
        incident_id = hashlib.sha256(
            f"{service_name}:{now}:{question[:100]}".encode()
        ).hexdigest()[:8]

        fingerprint = self._compute_fingerprint(
            service_name, error_codes, error_classes
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO incidents (
                    id, timestamp, question, query_type, service_name,
                    time_range, search_terms, error_codes, error_classes,
                    nr_data_summary, analyzer_used, analysis,
                    code_references, sources_used, error_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                incident_id, now, question, query_type, service_name,
                time_range,
                json.dumps(search_terms),
                json.dumps(error_codes),
                json.dumps(error_classes),
                nr_data_summary[:5000],
                analyzer_used,
                analysis[:10000],
                json.dumps(code_references or []),
                json.dumps(sources_used or []),
                fingerprint,
            ))

        logger.info(f"[memory] Stored incident {incident_id} for {service_name}")
        return incident_id

    # ──────────────────────────────────────────────
    # Feedback
    # ──────────────────────────────────────────────

    def record_feedback(
        self,
        incident_id: str,
        was_useful: bool,
        actual_root_cause: str = "",
        actual_fix: str = "",
        resolution_notes: str = "",
    ) -> bool:
        """Record feedback for an incident. Returns True if incident was found."""
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                UPDATE incidents SET
                    was_useful = ?,
                    actual_root_cause = ?,
                    actual_fix = ?,
                    resolution_notes = ?,
                    resolved_at = ?
                WHERE id = ?
            """, (
                1 if was_useful else 0,
                actual_root_cause, actual_fix,
                resolution_notes, now, incident_id,
            ))
            return cursor.rowcount > 0

    # ──────────────────────────────────────────────
    # Recall — finding similar past incidents
    # ──────────────────────────────────────────────

    def find_similar(
        self,
        service_name: str,
        error_codes: list[str],
        error_classes: list[str],
        query_type: str = "",
        max_results: int = 3,
    ) -> list[dict]:
        """Find past incidents with similar error patterns.

        Scoring hierarchy:
          1. Exact fingerprint match (same codes+classes in same service) → 10 pts
          2. Shared error codes → 5 pts each
          3. Shared error classes → 3 pts each
          4. Same query type → 1 pt
          5. Has known fix → 2 pts bonus
          6. Was marked useful → 1 pt bonus
        """
        fingerprint = self._compute_fingerprint(
            service_name, error_codes, error_classes
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT * FROM incidents
                WHERE service_name = ?
                ORDER BY timestamp DESC
                LIMIT 100
            """, (service_name,)).fetchall()

        if not rows:
            return []

        code_set = set(c.strip() for c in error_codes if c.strip())
        class_set = set(c.strip().lower() for c in error_classes if c.strip())

        scored = []
        for row in rows:
            score = 0
            reasons = []

            # Signal 1: exact fingerprint
            if row["error_fingerprint"] == fingerprint and fingerprint:
                score += 10
                reasons.append("exact error pattern match")

            # Signal 2: shared error codes
            try:
                past_codes = set(json.loads(row["error_codes"] or "[]"))
            except json.JSONDecodeError:
                past_codes = set()

            shared_codes = code_set & past_codes
            if shared_codes:
                score += len(shared_codes) * 5
                reasons.append(f"same error codes: {', '.join(sorted(shared_codes))}")

            # Signal 3: shared error classes
            try:
                past_classes = set(
                    c.lower() for c in json.loads(row["error_classes"] or "[]")
                )
            except json.JSONDecodeError:
                past_classes = set()

            shared_classes = class_set & past_classes
            if shared_classes:
                score += len(shared_classes) * 3
                reasons.append(f"same error classes: {', '.join(sorted(shared_classes))}")

            # Signal 4: same query type
            if query_type and row["query_type"] == query_type:
                score += 1

            # Signal 5: has known fix (more actionable)
            if row["actual_fix"]:
                score += 2
                reasons.append("has known fix")

            # Signal 6: was useful
            if row["was_useful"] == 1:
                score += 1

            if score > 0:
                scored.append({
                    "incident_id": row["id"],
                    "timestamp": row["timestamp"],
                    "question": row["question"],
                    "error_codes": row["error_codes"],
                    "error_classes": row["error_classes"],
                    "analysis_preview": (row["analysis"] or "")[:300],
                    "actual_root_cause": row["actual_root_cause"] or "",
                    "actual_fix": row["actual_fix"] or "",
                    "was_useful": row["was_useful"],
                    "score": score,
                    "match_reasons": reasons,
                })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:max_results]

    def get_recurring_errors(
        self,
        service_name: str,
        min_occurrences: int = 2,
    ) -> list[dict]:
        """Find error patterns that keep recurring in a service."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT error_fingerprint, error_codes, error_classes,
                       COUNT(*) as occurrence_count,
                       MIN(timestamp) as first_seen,
                       MAX(timestamp) as last_seen,
                       GROUP_CONCAT(DISTINCT actual_fix) as fixes_applied
                FROM incidents
                WHERE service_name = ?
                AND error_fingerprint != ''
                GROUP BY error_fingerprint
                HAVING COUNT(*) >= ?
                ORDER BY occurrence_count DESC
            """, (service_name, min_occurrences)).fetchall()

        return [dict(r) for r in rows]

    def get_service_history(
        self,
        service_name: str,
        limit: int = 10,
    ) -> list[dict]:
        """Get recent incident history for a service."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT id, timestamp, question, query_type,
                       error_codes, error_classes, analyzer_used,
                       was_useful, actual_root_cause, actual_fix
                FROM incidents
                WHERE service_name = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (service_name, limit)).fetchall()

        return [dict(r) for r in rows]

    # ──────────────────────────────────────────────
    # LLM Context Generation
    # ──────────────────────────────────────────────

    def build_memory_context(
        self,
        service_name: str,
        error_codes: list[str],
        error_classes: list[str],
        query_type: str = "",
        max_chars: int = 3000,
    ) -> str:
        """Build a context string from memory for LLM prompt injection.

        Returns a formatted block describing similar past incidents,
        known fixes, and recurring patterns. Returns empty string
        if no relevant memory exists.
        """
        parts = []

        # 1. Similar past incidents
        similar = self.find_similar(
            service_name, error_codes, error_classes, query_type
        )
        if similar:
            parts.append("## Prior Incidents (from Nocu memory)\n")
            for i, inc in enumerate(similar, 1):
                ts = inc["timestamp"][:10]  # just the date
                parts.append(f"### Match #{i} ({ts}, score: {inc['score']})")
                parts.append(f"Question: {inc['question']}")
                parts.append(f"Matched on: {', '.join(inc['match_reasons'])}")

                if inc["actual_root_cause"]:
                    parts.append(f"Root cause (confirmed): {inc['actual_root_cause']}")
                if inc["actual_fix"]:
                    parts.append(f"Fix applied: {inc['actual_fix']}")

                if not inc["actual_root_cause"] and not inc["actual_fix"]:
                    parts.append(f"Prior analysis: {inc['analysis_preview']}")

                useful_tag = ""
                if inc["was_useful"] == 1:
                    useful_tag = " (marked useful)"
                elif inc["was_useful"] == 0:
                    useful_tag = " (marked not useful — prior analysis may be wrong)"
                if useful_tag:
                    parts.append(f"Feedback:{useful_tag}")

                parts.append("")

        # 2. Recurring patterns
        recurring = self.get_recurring_errors(service_name, min_occurrences=2)
        if recurring:
            parts.append("## Recurring Patterns\n")
            for rec in recurring[:3]:
                codes = rec["error_codes"] or "[]"
                classes = rec["error_classes"] or "[]"
                parts.append(
                    f"- Seen {rec['occurrence_count']}x "
                    f"(first: {rec['first_seen'][:10]}, last: {rec['last_seen'][:10]}): "
                    f"codes={codes}, classes={classes}"
                )
                if rec["fixes_applied"]:
                    parts.append(f"  Fixes tried: {rec['fixes_applied']}")
            parts.append("")

        context = "\n".join(parts)

        if len(context) > max_chars:
            context = context[:max_chars] + "\n... (memory truncated)"

        return context

    def get_stats(self) -> dict:
        """Get overall memory statistics."""
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM incidents"
            ).fetchone()[0]

            with_feedback = conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE was_useful IS NOT NULL"
            ).fetchone()[0]

            useful = conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE was_useful = 1"
            ).fetchone()[0]

            services = conn.execute(
                "SELECT DISTINCT service_name FROM incidents"
            ).fetchall()

            recent_7d = conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE timestamp > ?",
                ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),)
            ).fetchone()[0]

            with_fixes = conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE actual_fix != '' AND actual_fix IS NOT NULL"
            ).fetchone()[0]

        return {
            "total_incidents": total,
            "with_feedback": with_feedback,
            "useful_count": useful,
            "accuracy_rate": f"{useful}/{with_feedback} ({useful/with_feedback*100:.0f}%)"
                if with_feedback > 0 else "no feedback yet",
            "known_fixes": with_fixes,
            "services_tracked": [s[0] for s in services],
            "incidents_last_7d": recent_7d,
            "retention_days": self.retention_days,
        }

    # ──────────────────────────────────────────────
    # User Conversation History
    # ──────────────────────────────────────────────

    def store_user_message(
        self,
        user_id: str,
        role: str,
        content: str,
        incident_id: str = "",
        service_name: str = "",
    ):
        """Store a user message or Nocu response.

        Args:
            user_id: Telegram user ID.
            role: "user" for questions, "nocu" for responses.
            content: The message text.
            incident_id: Link to incident if this is a response.
            service_name: Service being discussed.
        """
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO user_messages
                    (user_id, timestamp, role, content, incident_id, service_name)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                str(user_id), now, role,
                content[:5000],  # cap storage
                incident_id, service_name,
            ))

    def get_user_history(self, user_id: str, limit: int = 10) -> list[dict]:
        """Get the last N messages for a user (both questions and responses).

        Returns list of {role, content, service_name, timestamp} dicts,
        ordered oldest-first so the conversation reads naturally.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT role, content, service_name, timestamp, incident_id
                FROM user_messages
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (str(user_id), limit)).fetchall()

        # Reverse so oldest is first (natural reading order)
        return [dict(r) for r in reversed(rows)]

    def build_conversation_context(
        self, user_id: str, limit: int = 10, max_chars: int = 4000
    ) -> str:
        """Build a conversation history string for LLM context.

        Formats the last N messages as a conversation the LLM can reference
        to understand follow-up questions and ongoing investigations.
        """
        history = self.get_user_history(user_id, limit)

        if not history:
            return ""

        parts = ["## Recent Conversation History\n"]
        total_chars = 0

        for msg in history:
            ts = msg["timestamp"][11:16]  # just HH:MM
            service_tag = f" [{msg['service_name']}]" if msg["service_name"] else ""

            if msg["role"] == "user":
                line = f"[{ts}] You{service_tag}: {msg['content']}"
            else:
                # Truncate Nocu responses to keep context manageable
                preview = msg["content"][:300]
                if len(msg["content"]) > 300:
                    preview += "..."
                line = f"[{ts}] Nocu{service_tag}: {preview}"

            if total_chars + len(line) > max_chars:
                break

            parts.append(line)
            total_chars += len(line)

        if len(parts) <= 1:  # only header, no messages fit
            return ""

        parts.append("")  # trailing newline
        return "\n".join(parts)

    def prune_user_messages(self, retention_days: int = None):
        """Clean up old user messages."""
        days = retention_days or self.retention_days
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM user_messages WHERE timestamp < ?", (cutoff,)
            )

    # ──────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────

    def _compute_fingerprint(
        self,
        service_name: str,
        error_codes: list[str],
        error_classes: list[str],
    ) -> str:
        """Compute a stable fingerprint for error pattern deduplication.

        Sorts codes and classes so order doesn't matter.
        """
        codes_str = ",".join(sorted(set(str(c) for c in error_codes if c)))
        classes_str = ",".join(sorted(set(
            c.strip().lower() for c in error_classes if c and c.strip()
        )))
        raw = f"{service_name}|{codes_str}|{classes_str}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
