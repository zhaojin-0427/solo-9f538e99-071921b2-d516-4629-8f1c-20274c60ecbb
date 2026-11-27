"""Recording-studio workflows: 制单 (issue a recording order) and
交件/入账 (accept a voice-actor delivery and book its recording number).

Sentence resolution deliberately reuses the catalogue's existing rules
(:func:`app.domain.choose` + :func:`app.domain.render`, identical language /
fallback / variable / freshness logic to playback).  Every order is resolved
inside one transaction against a single committed catalogue snapshot, and a
delivery is re-reviewed inside a fresh transaction against the **latest
committed** catalogue, so template and firmware-configuration commits made
during recording are picked up at booking time.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import domain
from .db import read_tx, write_tx
from .errors import ApiError
from .schemas import DeliveryIn, OrderCreate, ReviewRequest
from .service import LANG_LABEL, utcnow

# ---------------------------------------------------------------------------
# Sample resolution (the existing "取句" rules)
# ---------------------------------------------------------------------------


def _coerce_value(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise _Unresolved(
        "invalid_variable_value",
        f"variable {name!r} must be a string, number or boolean",
    )


class _Unresolved(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def resolve_sample(
    conn: sqlite3.Connection,
    firmware_id: str,
    prompt_no: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Resolve one sample against *conn*'s catalogue snapshot.

    Mirrors :func:`app.service.play`: firmware prompt configuration supplies
    the requested language and declared variables; only published templates
    whose variables are all declared are eligible; English falls back to
    Chinese; most variables then latest publish win.  Never raises for a
    business-level inability to take the sentence — the returned dict carries
    ``ok=False`` and a stable ``reason_code`` instead.
    """
    result: dict[str, Any] = {
        "ok": False,
        "reason_code": None,
        "reason_message": None,
        "missing_variables": [],
        "firmware_enabled": None,
        "requested_lang": None,
        "used_lang": None,
        "fallback": False,
        "template_id": None,
        "text": None,
    }

    firmware = conn.execute(
        "SELECT enabled FROM firmwares WHERE firmware_id = ?", (firmware_id,)
    ).fetchone()
    if firmware is None:
        result.update(
            reason_code="firmware_not_found",
            reason_message=f"firmware {firmware_id!r} is not registered",
        )
        return result
    result["firmware_enabled"] = bool(firmware["enabled"])

    config = conn.execute(
        """
        SELECT lang, variables_json
          FROM firmware_prompts
         WHERE firmware_id = ? AND prompt_no = ?
        """,
        (firmware_id, prompt_no),
    ).fetchone()
    if config is None:
        result.update(
            reason_code="prompt_not_configured",
            reason_message=(
                f"prompt {prompt_no!r} is not configured for firmware "
                f"{firmware_id!r}"
            ),
        )
        return result

    requested_lang = config["lang"]
    declared = json.loads(config["variables_json"])
    result["requested_lang"] = requested_lang

    absent = sorted(set(declared) - set(values or {}))
    if absent:
        result.update(
            reason_code="missing_variables",
            reason_message="missing variable values: " + ", ".join(absent),
            missing_variables=absent,
        )
        return result

    try:
        rendered_values = {
            name: _coerce_value(name, values[name]) for name in declared
        }
    except _Unresolved as exc:
        result.update(
            reason_code=exc.code, reason_message=exc.message, **exc.extra
        )
        return result

    rows = conn.execute(
        """
        SELECT id, lang, content, variables_json, publish_seq
          FROM templates
         WHERE prompt_no = ? AND status = 'published'
        """,
        (prompt_no,),
    ).fetchall()
    candidates = [
        {
            "id": row["id"],
            "lang": row["lang"],
            "content": row["content"],
            "variables": json.loads(row["variables_json"]),
            "publish_seq": row["publish_seq"],
        }
        for row in rows
    ]
    chosen = domain.choose(candidates, requested_lang, declared)
    if chosen is None:
        result.update(
            reason_code="no_playable_template",
            reason_message=(
                f"no published template playable for prompt {prompt_no!r} "
                f"({LANG_LABEL[requested_lang]}) with the firmware's variables"
            ),
        )
        return result

    try:
        text = domain.render(chosen["content"], rendered_values)
    except KeyError as exc:  # defensive: eligibility should make this impossible
        result.update(
            reason_code="missing_variables",
            reason_message=f"missing variable value: {exc.args[0]!s}",
            missing_variables=[str(exc.args[0])],
        )
        return result

    result.update(
        ok=True,
        used_lang=chosen["lang"],
        fallback=chosen["lang"] != requested_lang,
        template_id=chosen["id"],
        text=text,
    )
    return result


# ---------------------------------------------------------------------------
# Recording-number lookup (batch first-wins, then studio library)
# ---------------------------------------------------------------------------


def _recording_numbers(
    conn: sqlite3.Connection, batch_no: str, lang: str, text: str
) -> dict[str, Any]:
    batch_row = conn.execute(
        """
        SELECT recording_no FROM batch_recordings
         WHERE batch_no = ? AND lang = ? AND text = ?
        """,
        (batch_no, lang, text),
    ).fetchone()
    library_row = conn.execute(
        "SELECT recording_no FROM recordings WHERE lang = ? AND text = ?",
        (lang, text),
    ).fetchone()
    batch_no_value = batch_row["recording_no"] if batch_row else None
    library_no_value = library_row["recording_no"] if library_row else None
    effective = batch_no_value or library_no_value
    if batch_no_value:
        scope = "batch"
    elif library_no_value:
        scope = "library"
    else:
        scope = None
    return {
        "recording_no": effective,
        "batch_recording_no": batch_no_value,
        "library_recording_no": library_no_value,
        "recording_scope": scope,
        "recorded": effective is not None,
    }


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _sample_out(row: sqlite3.Row) -> dict[str, Any]:
    missing = json.loads(row["missing_json"]) if row["missing_json"] else None
    return {
        "ordinal": row["ordinal"],
        "sample_no": row["sample_no"],
        "firmware_id": row["firmware_id"],
        "prompt_no": row["prompt_no"],
        "variables": json.loads(row["variables_json"]),
        "status": row["status"],
        "requested_lang": row["requested_lang"],
        "used_lang": row["used_lang"],
        "fallback": bool(row["fallback"]),
        "template_id": row["template_id"],
        "text": row["text"],
        "firmware_enabled": (
            bool(row["firmware_enabled"])
            if row["firmware_enabled"] is not None
            else None
        ),
        "reason_code": row["reason_code"],
        "reason_message": row["reason_message"],
        "missing_variables": missing,
    }


def _serialize_order(
    conn: sqlite3.Connection, order_row: sqlite3.Row, *, reissued: bool
) -> dict[str, Any]:
    order_id = order_row["id"]
    entry_rows = conn.execute(
        "SELECT * FROM order_entries WHERE order_id = ? ORDER BY entry_no",
        (order_id,),
    ).fetchall()
    sample_rows = conn.execute(
        "SELECT * FROM order_samples WHERE order_id = ? ORDER BY ordinal",
        (order_id,),
    ).fetchall()

    samples_by_entry: dict[int, list[sqlite3.Row]] = {}
    for sample in sample_rows:
        if sample["entry_id"] is not None:
            samples_by_entry.setdefault(sample["entry_id"], []).append(sample)

    entries: list[dict[str, Any]] = []
    for entry in entry_rows:
        rows = samples_by_entry.get(entry["id"], [])
        numbers = [row["sample_no"] for row in rows]
        rec = _recording_numbers(
            conn, order_row["batch_no"], entry["lang"], entry["text"]
        )
        entries.append(
            {
                "entry_id": entry["id"],
                "order_id": order_id,
                "entry_no": entry["entry_no"],
                "lang": entry["lang"],
                "text": entry["text"],
                "template_id": entry["template_id"],
                "fallback": bool(entry["fallback"]),
                **rec,
                "sample_count": len(rows),
                "sample_numbers": numbers,
                "samples": [_sample_out(row) for row in rows],
            }
        )

    unresolved = [
        _sample_out(row) for row in sample_rows if row["status"] == "unresolved"
    ]
    resolved_count = len(sample_rows) - len(unresolved)
    return {
        "order_id": order_id,
        "batch_no": order_row["batch_no"],
        "revision": order_row["revision"],
        "reissued": reissued,
        "note": order_row["note"],
        "created_at": order_row["created_at"],
        "sample_count": len(sample_rows),
        "resolved_count": resolved_count,
        "unresolved_count": len(unresolved),
        "entry_count": len(entry_rows),
        "entries": entries,
        "unresolved": unresolved,
    }


def _fetch_batch(conn: sqlite3.Connection, batch_no: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM recording_batches WHERE batch_no = ?", (batch_no,)
    ).fetchone()
    if row is None:
        raise ApiError(
            404, "batch_not_found", f"recording batch {batch_no!r} not found"
        )
    return row


def _latest_order_row(conn: sqlite3.Connection, batch_no: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM recording_orders
         WHERE batch_no = ?
         ORDER BY revision DESC LIMIT 1
        """,
        (batch_no,),
    ).fetchone()
    if row is None:  # should be impossible (batch and order created together)
        raise ApiError(404, "order_not_found", f"batch {batch_no!r} has no order")
    return row


# ---------------------------------------------------------------------------
# 制单
# ---------------------------------------------------------------------------


def create_order(db_path: str, payload: OrderCreate) -> dict[str, Any]:
    now = utcnow()
    with write_tx(db_path) as conn:
        existing = conn.execute(
            "SELECT * FROM recording_batches WHERE batch_no = ?",
            (payload.batch_no,),
        ).fetchone()
        reissued = existing is not None
        if existing is None:
            conn.execute(
                """
                INSERT INTO recording_batches
                       (batch_no, note, revision, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (payload.batch_no, payload.note, now, now),
            )
            revision = 1
        else:
            revision = int(existing["revision"]) + 1
            conn.execute(
                """
                UPDATE recording_batches
                   SET revision = ?, note = COALESCE(?, note), updated_at = ?
                 WHERE batch_no = ?
                """,
                (revision, payload.note, now, payload.batch_no),
            )

        order_cursor = conn.execute(
            """
            INSERT INTO recording_orders (batch_no, revision, note, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (payload.batch_no, revision, payload.note, now),
        )
        order_id = int(order_cursor.lastrowid)

        # Resolve every sample against this same transaction snapshot.
        resolved: list[tuple[Any, str, dict[str, Any]]] = []
        for ordinal, sample in enumerate(payload.samples):
            values = sample.variables or {}
            result = resolve_sample(
                conn, sample.firmware_id, sample.prompt_no, values
            )
            sample_no = (
                str(sample.sample_no)
                if sample.sample_no is not None
                else str(ordinal + 1)
            )
            resolved.append((sample, sample_no, result))

        # Merge resolved samples by actual language + full composed sentence,
        # numbering entries by first appearance.
        entry_ids: dict[tuple[str, str], int] = {}
        entry_meta: dict[tuple[str, str], dict[str, Any]] = {}
        for _sample, _sample_no, result in resolved:
            if not result["ok"]:
                continue
            key = (result["used_lang"], result["text"])
            if key not in entry_ids:
                rec = _recording_numbers(conn, payload.batch_no, key[0], key[1])
                cursor = conn.execute(
                    """
                    INSERT INTO order_entries
                           (order_id, entry_no, lang, text, template_id,
                            fallback, recording_no, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        len(entry_ids) + 1,
                        key[0],
                        key[1],
                        result["template_id"],
                        1 if result["fallback"] else 0,
                        rec["recording_no"],
                        now,
                    ),
                )
                entry_ids[key] = int(cursor.lastrowid)
                entry_meta[key] = {"fallback": result["fallback"]}

        for ordinal, (sample, sample_no, result) in enumerate(resolved):
            key = (
                (result["used_lang"], result["text"]) if result["ok"] else None
            )
            entry_id = entry_ids.get(key) if key is not None else None
            conn.execute(
                """
                INSERT INTO order_samples
                       (order_id, entry_id, ordinal, sample_no, firmware_id,
                        prompt_no, variables_json, status, requested_lang,
                        used_lang, fallback, template_id, text,
                        firmware_enabled, reason_code, reason_message,
                        missing_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    entry_id,
                    ordinal,
                    sample_no,
                    sample.firmware_id,
                    sample.prompt_no,
                    json.dumps(sample.variables or {}, ensure_ascii=False),
                    "resolved" if result["ok"] else "unresolved",
                    result["requested_lang"],
                    result["used_lang"],
                    1 if result["fallback"] else 0,
                    result["template_id"],
                    result["text"],
                    result["firmware_enabled"],
                    result["reason_code"],
                    result["reason_message"],
                    json.dumps(result["missing_variables"], ensure_ascii=False)
                    if result["missing_variables"]
                    else None,
                ),
            )

        order_row = conn.execute(
            "SELECT * FROM recording_orders WHERE id = ?", (order_id,)
        ).fetchone()
        return _serialize_order(conn, order_row, reissued=reissued)


def list_batches(db_path: str) -> list[dict[str, Any]]:
    with read_tx(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM recording_batches ORDER BY batch_no"
        ).fetchall()
        summaries = []
        for row in rows:
            order_count = conn.execute(
                "SELECT COUNT(*) AS n FROM recording_orders WHERE batch_no = ?",
                (row["batch_no"],),
            ).fetchone()["n"]
            recording_count = conn.execute(
                "SELECT COUNT(*) AS n FROM batch_recordings WHERE batch_no = ?",
                (row["batch_no"],),
            ).fetchone()["n"]
            summaries.append(
                {
                    "batch_no": row["batch_no"],
                    "note": row["note"],
                    "revision": row["revision"],
                    "order_count": order_count,
                    "recording_count": recording_count,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return summaries


def get_latest_order(db_path: str, batch_no: str) -> dict[str, Any]:
    with read_tx(db_path) as conn:
        _fetch_batch(conn, batch_no)
        return _serialize_order(
            conn, _latest_order_row(conn, batch_no), reissued=False
        )


def list_order_revisions(db_path: str, batch_no: str) -> list[dict[str, Any]]:
    with read_tx(db_path) as conn:
        _fetch_batch(conn, batch_no)
        rows = conn.execute(
            "SELECT * FROM recording_orders WHERE batch_no = ? ORDER BY revision",
            (batch_no,),
        ).fetchall()
        result = []
        for row in rows:
            counts = conn.execute(
                """
                SELECT
                  COUNT(*) AS sample_count,
                  SUM(CASE WHEN status = 'unresolved' THEN 1 ELSE 0 END)
                    AS unresolved_count,
                  (SELECT COUNT(*) FROM order_entries WHERE order_id = ?)
                    AS entry_count
                FROM order_samples WHERE order_id = ?
                """,
                (row["id"], row["id"]),
            ).fetchone()
            result.append(
                {
                    "order_id": row["id"],
                    "batch_no": row["batch_no"],
                    "revision": row["revision"],
                    "note": row["note"],
                    "created_at": row["created_at"],
                    "sample_count": counts["sample_count"],
                    "unresolved_count": counts["unresolved_count"] or 0,
                    "entry_count": counts["entry_count"],
                }
            )
        return result


def get_order_revision(
    db_path: str, batch_no: str, revision: int
) -> dict[str, Any]:
    with read_tx(db_path) as conn:
        _fetch_batch(conn, batch_no)
        row = conn.execute(
            "SELECT * FROM recording_orders WHERE batch_no = ? AND revision = ?",
            (batch_no, revision),
        ).fetchone()
        if row is None:
            raise ApiError(
                404,
                "order_not_found",
                f"batch {batch_no!r} has no order revision {revision}",
            )
        return _serialize_order(conn, row, reissued=revision > 1)


# ---------------------------------------------------------------------------
# Entry lookup + per-sample re-review at 交件 time
# ---------------------------------------------------------------------------


_ENTRY_SELECT = """
    SELECT oe.*, o.batch_no AS batch_no, o.revision AS revision
      FROM order_entries oe
      JOIN recording_orders o ON o.id = oe.order_id
"""


def _resolve_entry_row(
    conn: sqlite3.Connection,
    batch_no: str,
    *,
    entry_id: int | None = None,
    order_id: int | None = None,
    revision: int | None = None,
    entry_no: int | None = None,
) -> sqlite3.Row:
    if entry_id is not None:
        row = conn.execute(
            _ENTRY_SELECT + " WHERE oe.id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "entry_not_found", f"entry {entry_id} not found")
        if row["batch_no"] != batch_no:
            raise ApiError(
                404,
                "entry_not_found",
                f"entry {entry_id} does not belong to batch {batch_no!r}",
            )
        return row

    # Locate the order that owns the numbered entry.
    if order_id is not None:
        order = conn.execute(
            "SELECT * FROM recording_orders WHERE id = ? AND batch_no = ?",
            (order_id, batch_no),
        ).fetchone()
        if order is None:
            raise ApiError(
                404,
                "order_not_found",
                f"order {order_id} not found in batch {batch_no!r}",
            )
    elif revision is not None:
        order = conn.execute(
            "SELECT * FROM recording_orders WHERE batch_no = ? AND revision = ?",
            (batch_no, revision),
        ).fetchone()
        if order is None:
            raise ApiError(
                404,
                "order_not_found",
                f"batch {batch_no!r} has no order revision {revision}",
            )
    else:
        order = _latest_order_row(conn, batch_no)

    row = conn.execute(
        _ENTRY_SELECT + " WHERE oe.order_id = ? AND oe.entry_no = ?",
        (order["id"], entry_no),
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "entry_not_found",
            f"entry_no {entry_no} not found in order {order['id']} "
            f"(revision {order['revision']}) of batch {batch_no!r}",
        )
    return row


def _review_entry_samples(
    conn: sqlite3.Connection, entry: sqlite3.Row
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Re-resolve exactly the samples merged into *entry*.

    The catalogue reads happen in the caller's transaction, so a delivery
    sees the latest committed templates and firmware configuration.
    """
    rows = conn.execute(
        "SELECT * FROM order_samples WHERE entry_id = ? ORDER BY ordinal",
        (entry["id"],),
    ).fetchall()
    original_lang = entry["lang"]
    original_text = entry["text"]
    items: list[dict[str, Any]] = []
    for row in rows:
        values = json.loads(row["variables_json"])
        current = resolve_sample(
            conn, row["firmware_id"], row["prompt_no"], values
        )
        item: dict[str, Any] = {
            "ordinal": row["ordinal"],
            "sample_no": row["sample_no"],
            "firmware_id": row["firmware_id"],
            "prompt_no": row["prompt_no"],
            "variables": values,
            "matches": False,
            "reason_code": None,
            "reason_message": None,
            "requested_lang": current["requested_lang"],
            "used_lang": current["used_lang"],
            "fallback": current["fallback"],
            "template_id": current["template_id"],
            "text": current["text"],
            "original_lang": original_lang,
            "original_text": original_text,
            "missing_variables": current["missing_variables"] or None,
            "current_recording_no": None,
            "current_recording_scope": None,
        }
        if current["ok"]:
            # If the sample now lands on a sentence that already has a
            # recording in this batch (or the studio library), surface that
            # number so the later sample can reuse it.
            current_rec = _recording_numbers(
                conn,
                entry["batch_no"],
                current["used_lang"],
                current["text"],
            )
            item["current_recording_no"] = current_rec["recording_no"]
            item["current_recording_scope"] = current_rec["recording_scope"]
            same_lang = current["used_lang"] == original_lang
            same_text = current["text"] == original_text
            if same_lang and same_text:
                item["matches"] = True
            else:
                if not same_lang and not same_text:
                    code = "lang_and_sentence_changed"
                elif not same_lang:
                    code = "language_changed"
                else:
                    code = "sentence_changed"
                item["reason_code"] = code
                item["reason_message"] = (
                    "sample now resolves to "
                    f"{current['used_lang']} {current['text']!r}, not the "
                    f"original {original_lang} {original_text!r}"
                )
        else:
            item["reason_code"] = current["reason_code"]
            item["reason_message"] = current["reason_message"]
        items.append(item)

    matched = [item for item in items if item["matches"]]
    mismatched = [item for item in items if not item["matches"]]
    return items, matched, mismatched


def _review_payload(
    conn: sqlite3.Connection, batch_no: str, entry: sqlite3.Row
) -> dict[str, Any]:
    items, matched, mismatched = _review_entry_samples(conn, entry)
    return {
        "batch_no": batch_no,
        "order_id": entry["order_id"],
        "entry_id": entry["id"],
        "entry_no": entry["entry_no"],
        "revision": entry["revision"],
        "original_lang": entry["lang"],
        "original_text": entry["text"],
        "reviewed_count": len(items),
        "matched_count": len(matched),
        "mismatched_count": len(mismatched),
        "matched": matched,
        "mismatched": mismatched,
    }


def review_entry(
    db_path: str, batch_no: str, payload: ReviewRequest
) -> dict[str, Any]:
    with read_tx(db_path) as conn:
        _fetch_batch(conn, batch_no)
        entry = _resolve_entry_row(
            conn,
            batch_no,
            entry_id=payload.entry_id,
            order_id=payload.order_id,
            revision=payload.revision,
            entry_no=payload.entry_no,
        )
        return _review_payload(conn, batch_no, entry)


# ---------------------------------------------------------------------------
# 交件 / 入账
# ---------------------------------------------------------------------------


def submit_delivery(
    db_path: str, payload: DeliveryIn, path_batch_no: str | None = None
) -> dict[str, Any]:
    batch_no = path_batch_no or payload.batch_no
    if not batch_no:
        raise ApiError(
            422,
            "batch_no_required",
            "batch_no is required (path or request body)",
        )
    if payload.batch_no and path_batch_no and payload.batch_no != path_batch_no:
        raise ApiError(
            400,
            "batch_mismatch",
            f"batch_no in path {path_batch_no!r} does not match body "
            f"{payload.batch_no!r}",
        )

    now = utcnow()
    with write_tx(db_path) as conn:
        _fetch_batch(conn, batch_no)
        entry = _resolve_entry_row(
            conn,
            batch_no,
            entry_id=payload.entry_id,
            order_id=payload.order_id,
            revision=payload.revision,
            entry_no=payload.entry_no,
        )
        review = _review_payload(conn, batch_no, entry)
        submitted_recording_no = payload.recording_no

        base_delivery = {
            "batch_no": batch_no,
            "order_id": entry["order_id"],
            "entry_id": entry["id"],
            "entry_no": entry["entry_no"],
            "revision": entry["revision"],
            "recording_no": submitted_recording_no,
            "original_lang": review["original_lang"],
            "original_text": review["original_text"],
            "reviewed_count": review["reviewed_count"],
            "matched_count": review["matched_count"],
            "mismatched_count": review["mismatched_count"],
            "matched": review["matched"],
            "mismatched": review["mismatched"],
        }

        if review["matched_count"] < 1:
            # 入账条件不满足：不写任何登记，按目录服务的冲突信封返回全部
            # 已不匹配样例及原因。
            raise ApiError(
                409,
                "delivery_not_booked",
                "delivery not booked: no sample still resolves to the original "
                f"{entry['lang']} sentence {entry['text']!r}; "
                f"{review['mismatched_count']} sample(s) no longer match",
                status="rejected",
                booked=False,
                effective_recording_no=None,
                **base_delivery,
            )

        existing = conn.execute(
            """
            SELECT recording_no FROM batch_recordings
             WHERE batch_no = ? AND lang = ? AND text = ?
            """,
            (batch_no, entry["lang"], entry["text"]),
        ).fetchone()

        if existing is not None:
            # 同批号下，同语种同句保留首个录音号。
            status = "reused"
            effective = existing["recording_no"]
        else:
            status = "booked"
            effective = submitted_recording_no

        review_json = json.dumps(
            {
                "matched": review["matched"],
                "mismatched": review["mismatched"],
            },
            ensure_ascii=False,
        )
        cursor = conn.execute(
            """
            INSERT INTO deliveries
                   (batch_no, order_id, entry_id, submitted_recording_no,
                    status, effective_recording_no, reviewed_count,
                    matched_count, review_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_no,
                entry["order_id"],
                entry["id"],
                submitted_recording_no,
                status,
                effective,
                review["reviewed_count"],
                review["matched_count"],
                review_json,
                now,
            ),
        )
        delivery_id = int(cursor.lastrowid)

        if status == "booked":
            conn.execute(
                """
                INSERT INTO batch_recordings
                       (batch_no, lang, text, recording_no, entry_id,
                        delivery_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_no,
                    entry["lang"],
                    entry["text"],
                    submitted_recording_no,
                    entry["id"],
                    delivery_id,
                    now,
                ),
            )
            # Studio-wide library: first recording number for the sentence wins.
            conn.execute(
                """
                INSERT OR IGNORE INTO recordings
                       (lang, text, recording_no, source_batch_no, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entry["lang"],
                    entry["text"],
                    submitted_recording_no,
                    batch_no,
                    now,
                ),
            )
            conn.execute(
                "UPDATE order_entries SET recording_no = ? WHERE id = ?",
                (submitted_recording_no, entry["id"]),
            )

        return {
            "delivery_id": delivery_id,
            **base_delivery,
            "status": status,
            "booked": True,
            "effective_recording_no": effective,
            "created_at": now,
        }


def list_deliveries(db_path: str, batch_no: str) -> list[dict[str, Any]]:
    with read_tx(db_path) as conn:
        _fetch_batch(conn, batch_no)
        rows = conn.execute(
            "SELECT * FROM deliveries WHERE batch_no = ? ORDER BY id",
            (batch_no,),
        ).fetchall()
        result = []
        for row in rows:
            review = json.loads(row["review_json"] or "{}")
            result.append(
                {
                    "delivery_id": row["id"],
                    "batch_no": row["batch_no"],
                    "order_id": row["order_id"],
                    "entry_id": row["entry_id"],
                    "entry_no": None,  # filled below
                    "revision": None,
                    "recording_no": row["submitted_recording_no"],
                    "status": row["status"],
                    "booked": True,
                    "effective_recording_no": row["effective_recording_no"],
                    "original_lang": None,
                    "original_text": None,
                    "reviewed_count": row["reviewed_count"],
                    "matched_count": row["matched_count"],
                    "mismatched_count": row["reviewed_count"]
                    - row["matched_count"],
                    "matched": review.get("matched", []),
                    "mismatched": review.get("mismatched", []),
                    "created_at": row["created_at"],
                }
            )
            entry = conn.execute(
                _ENTRY_SELECT + " WHERE oe.id = ?", (row["entry_id"],)
            ).fetchone()
            if entry is not None:
                result[-1]["entry_no"] = entry["entry_no"]
                result[-1]["revision"] = entry["revision"]
                result[-1]["original_lang"] = entry["lang"]
                result[-1]["original_text"] = entry["text"]
        return result


def list_batch_recordings(db_path: str, batch_no: str) -> list[dict[str, Any]]:
    with read_tx(db_path) as conn:
        _fetch_batch(conn, batch_no)
        rows = conn.execute(
            "SELECT * FROM batch_recordings WHERE batch_no = ? ORDER BY id",
            (batch_no,),
        ).fetchall()
        return [
            {
                "lang": row["lang"],
                "text": row["text"],
                "recording_no": row["recording_no"],
                "source_batch_no": row["batch_no"],
                "scope": "batch",
            }
            for row in rows
        ]


def list_library_recordings(
    db_path: str, lang: str | None = None, text: str | None = None
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if lang is not None:
        clauses.append("lang = ?")
        params.append(lang)
    if text is not None:
        clauses.append("text = ?")
        params.append(text)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with read_tx(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM recordings {where} ORDER BY id", params
        ).fetchall()
        return [
            {
                "lang": row["lang"],
                "text": row["text"],
                "recording_no": row["recording_no"],
                "source_batch_no": row["source_batch_no"],
                "scope": "library",
            }
            for row in rows
        ]
