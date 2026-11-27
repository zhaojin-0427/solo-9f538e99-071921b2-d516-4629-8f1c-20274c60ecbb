"""Business logic.

Invariant: every prompt configured on an *enabled* firmware resolves to at
least one published template whose variables the firmware declares.

The invariant is maintained by mutating and then verifying inside a single
``BEGIN IMMEDIATE`` transaction: if verification fails the transaction is
rolled back, so a refused request changes nothing at all.  Because SQLite
serialises write transactions and readers use WAL snapshots, playback never
observes a half-applied enable/withdraw, even when the two interleave.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from . import domain
from .db import read_tx, write_tx
from .errors import ApiError
from .schemas import FirmwareEnable, FirmwarePromptIn, TemplateCreate, TemplateUpdate

LANG_LABEL = {"zh": "Chinese", "en": "English"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _template_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "prompt_no": row["prompt_no"],
        "lang": row["lang"],
        "content": row["content"],
        "variables": json.loads(row["variables_json"]),
        "status": row["status"],
        "publish_seq": row["publish_seq"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "published_at": row["published_at"],
    }


def _firmware_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    prompts = conn.execute(
        """
        SELECT prompt_no, lang, variables_json
          FROM firmware_prompts
         WHERE firmware_id = ?
         ORDER BY prompt_no
        """,
        (row["firmware_id"],),
    ).fetchall()
    return {
        "firmware_id": row["firmware_id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "prompts": [
            {
                "prompt_no": prompt["prompt_no"],
                "lang": prompt["lang"],
                "variables": json.loads(prompt["variables_json"]),
            }
            for prompt in prompts
        ],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "enabled_at": row["enabled_at"],
    }


def _fetch_firmware_row(conn: sqlite3.Connection, firmware_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM firmwares WHERE firmware_id = ?", (firmware_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "firmware_not_found", f"firmware {firmware_id!r} not found")
    return row


def _fetch_template_row(conn: sqlite3.Connection, template_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM templates WHERE id = ?", (template_id,)
    ).fetchone()
    if row is None:
        raise ApiError(404, "template_not_found", f"template {template_id} not found")
    return row


# ---------------------------------------------------------------------------
# Selection helpers (always read inside the caller's transaction)
# ---------------------------------------------------------------------------


def _candidates(conn: sqlite3.Connection, prompt_no: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, lang, content, variables_json, publish_seq
          FROM templates
         WHERE prompt_no = ? AND status = 'published'
        """,
        (prompt_no,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "lang": row["lang"],
            "content": row["content"],
            "variables": json.loads(row["variables_json"]),
            "publish_seq": row["publish_seq"],
        }
        for row in rows
    ]


def _missing_in_firmware(
    conn: sqlite3.Connection, firmware_id: str
) -> list[dict[str, str]]:
    """Prompts configured on *firmware_id* that currently cannot be played."""
    rows = conn.execute(
        """
        SELECT prompt_no, lang, variables_json
          FROM firmware_prompts
         WHERE firmware_id = ?
         ORDER BY prompt_no
        """,
        (firmware_id,),
    ).fetchall()
    missing = []
    for row in rows:
        variables = json.loads(row["variables_json"])
        if domain.choose(_candidates(conn, row["prompt_no"]), row["lang"], variables) is None:
            missing.append(
                {
                    "firmware_id": firmware_id,
                    "prompt_no": row["prompt_no"],
                    "lang": row["lang"],
                }
            )
    return missing


def _missing_for_prompt(
    conn: sqlite3.Connection, firmware_ids: Iterable[str], prompt_no: str
) -> list[dict[str, str]]:
    """Playability of a single prompt number on the given firmwares."""
    missing = []
    for firmware_id in firmware_ids:
        rows = conn.execute(
            """
            SELECT prompt_no, lang, variables_json
              FROM firmware_prompts
             WHERE firmware_id = ? AND prompt_no = ?
            """,
            (firmware_id, prompt_no),
        ).fetchall()
        for row in rows:
            variables = json.loads(row["variables_json"])
            if (
                domain.choose(_candidates(conn, row["prompt_no"]), row["lang"], variables)
                is None
            ):
                missing.append(
                    {
                        "firmware_id": firmware_id,
                        "prompt_no": row["prompt_no"],
                        "lang": row["lang"],
                    }
                )
    return missing


def _enabled_firmwares_for_prompt(conn: sqlite3.Connection, prompt_no: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT f.firmware_id
          FROM firmwares AS f
          JOIN firmware_prompts AS fp ON fp.firmware_id = f.firmware_id
         WHERE f.enabled = 1 AND fp.prompt_no = ?
         ORDER BY f.firmware_id
        """,
        (prompt_no,),
    ).fetchall()
    return [row["firmware_id"] for row in rows]


def _raise_if_missing(missing: Iterable[dict[str, str]], action: str) -> None:
    missing = list(missing)
    if not missing:
        return
    rendered = ", ".join(
        f"{item['firmware_id']}/{item['prompt_no']} ({LANG_LABEL[item['lang']]})"
        for item in missing
    )
    raise ApiError(
        409,
        "prompt_missing",
        f"{action} refused: no playable template for {rendered}; "
        "the request changed nothing",
        missing=missing,
    )


# ---------------------------------------------------------------------------
# Firmwares
# ---------------------------------------------------------------------------


def create_firmware(db_path: str, payload: Any) -> dict[str, Any]:
    now = utcnow()
    with write_tx(db_path) as conn:
        if conn.execute(
            "SELECT 1 FROM firmwares WHERE firmware_id = ?", (payload.firmware_id,)
        ).fetchone():
            raise ApiError(
                409,
                "firmware_exists",
                f"firmware {payload.firmware_id!r} already exists",
            )
        conn.execute(
            """
            INSERT INTO firmwares (firmware_id, name, enabled, created_at, updated_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (payload.firmware_id, payload.name, now, now),
        )
        return _firmware_dict(conn, _fetch_firmware_row(conn, payload.firmware_id))


def list_firmwares(db_path: str, enabled: bool | None = None) -> list[dict[str, Any]]:
    with read_tx(db_path) as conn:
        if enabled is None:
            rows = conn.execute(
                "SELECT * FROM firmwares ORDER BY firmware_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM firmwares WHERE enabled = ? ORDER BY firmware_id",
                (1 if enabled else 0,),
            ).fetchall()
        return [_firmware_dict(conn, row) for row in rows]


def get_firmware(db_path: str, firmware_id: str) -> dict[str, Any]:
    with read_tx(db_path) as conn:
        return _firmware_dict(conn, _fetch_firmware_row(conn, firmware_id))


def enable_firmware(
    db_path: str, firmware_id: str, payload: FirmwareEnable
) -> dict[str, Any]:
    """Replace the prompt configuration and enable the firmware, atomically.

    If any configured prompt would be unplayable, the transaction rolls back:
    a disabled firmware stays disabled, and an already-enabled firmware keeps
    its previous configuration intact.
    """
    if not payload.prompts:
        raise ApiError(
            400,
            "empty_prompt_set",
            "configuring a firmware requires at least one prompt",
        )
    now = utcnow()
    with write_tx(db_path) as conn:
        row = _fetch_firmware_row(conn, firmware_id)

        conn.execute("DELETE FROM firmware_prompts WHERE firmware_id = ?", (firmware_id,))
        for prompt in payload.prompts:
            conn.execute(
                """
                INSERT INTO firmware_prompts
                       (firmware_id, prompt_no, lang, variables_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    firmware_id,
                    prompt.prompt_no,
                    prompt.lang,
                    json.dumps(sorted(set(prompt.variables)), ensure_ascii=False),
                ),
            )
        conn.execute(
            """
            UPDATE firmwares
               SET enabled = 1, updated_at = ?,
                   enabled_at = COALESCE(enabled_at, ?)
             WHERE firmware_id = ?
            """,
            (now, now, firmware_id),
        )

        # Verify the post-change state before committing.
        _raise_if_missing(_missing_in_firmware(conn, firmware_id), "enable")
        return _firmware_dict(conn, _fetch_firmware_row(conn, firmware_id))


def disable_firmware(db_path: str, firmware_id: str) -> dict[str, Any]:
    now = utcnow()
    with write_tx(db_path) as conn:
        _fetch_firmware_row(conn, firmware_id)
        conn.execute(
            "UPDATE firmwares SET enabled = 0, updated_at = ? WHERE firmware_id = ?",
            (now, firmware_id),
        )
        return _firmware_dict(conn, _fetch_firmware_row(conn, firmware_id))


def firmware_missing(db_path: str, firmware_id: str) -> list[dict[str, str]]:
    with read_tx(db_path) as conn:
        _fetch_firmware_row(conn, firmware_id)
        return _missing_in_firmware(conn, firmware_id)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _variables_of(content: str) -> list[str]:
    try:
        return domain.variables(content)
    except domain.TemplateSyntaxError as exc:
        raise ApiError(422, "template_syntax_error", str(exc), position=exc.position)


def create_template(db_path: str, payload: TemplateCreate) -> dict[str, Any]:
    variable_names = _variables_of(payload.content)
    now = utcnow()
    with write_tx(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO templates
                   (prompt_no, lang, content, variables_json, status,
                    publish_seq, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'draft', 0, ?, ?)
            """,
            (
                payload.prompt_no,
                payload.lang,
                payload.content,
                json.dumps(variable_names, ensure_ascii=False),
                now,
                now,
            ),
        )
        return _template_dict(_fetch_template_row(conn, int(cursor.lastrowid)))


def list_templates(
    db_path: str,
    prompt_no: str | None = None,
    lang: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if prompt_no is not None:
        clauses.append("prompt_no = ?")
        params.append(prompt_no)
    if lang is not None:
        clauses.append("lang = ?")
        params.append(lang)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with read_tx(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM templates {where} ORDER BY id", params
        ).fetchall()
        return [_template_dict(row) for row in rows]


def get_template(db_path: str, template_id: int) -> dict[str, Any]:
    with read_tx(db_path) as conn:
        return _template_dict(_fetch_template_row(conn, template_id))


def update_template(
    db_path: str, template_id: int, payload: TemplateUpdate
) -> dict[str, Any]:
    """Edit template content.

    Editing a *published* template is allowed only if every enabled firmware
    that uses the same prompt number still has a playable template afterwards
    (its own other templates may cover it).  Otherwise the edit rolls back.
    """
    variable_names = _variables_of(payload.content)
    now = utcnow()
    with write_tx(db_path) as conn:
        row = _fetch_template_row(conn, template_id)
        conn.execute(
            """
            UPDATE templates
               SET content = ?, variables_json = ?, updated_at = ?
             WHERE id = ?
            """,
            (
                payload.content,
                json.dumps(variable_names, ensure_ascii=False),
                now,
                template_id,
            ),
        )
        if row["status"] == "published":
            affected = _enabled_firmwares_for_prompt(conn, row["prompt_no"])
            _raise_if_missing(
                _missing_for_prompt(conn, affected, row["prompt_no"]),
                "edit",
            )
        return _template_dict(_fetch_template_row(conn, template_id))


def publish_template(db_path: str, template_id: int) -> dict[str, Any]:
    now = utcnow()
    with write_tx(db_path) as conn:
        row = _fetch_template_row(conn, template_id)
        if row["status"] == "published":
            raise ApiError(
                409, "template_is_published", "template is already published"
            )
        seq = conn.execute(
            "SELECT COALESCE(MAX(publish_seq), 0) + 1 AS seq FROM templates"
        ).fetchone()["seq"]
        conn.execute(
            """
            UPDATE templates
               SET status = 'published', publish_seq = ?, published_at = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (seq, now, now, template_id),
        )
        return _template_dict(_fetch_template_row(conn, template_id))


def withdraw_template(db_path: str, template_id: int) -> dict[str, Any]:
    """Take a published template out of service if doing so keeps supply.

    Only enabled firmwares configured with the same prompt number are
    re-checked.  The status change and the check share one transaction, so a
    concurrent enable cannot slip in between them.
    """
    now = utcnow()
    with write_tx(db_path) as conn:
        row = _fetch_template_row(conn, template_id)
        if row["status"] != "published":
            raise ApiError(
                409,
                "template_not_published",
                f"template is {row['status']}, not published",
            )
        conn.execute(
            "UPDATE templates SET status = 'withdrawn', updated_at = ? WHERE id = ?",
            (now, template_id),
        )
        affected = _enabled_firmwares_for_prompt(conn, row["prompt_no"])
        _raise_if_missing(
            _missing_for_prompt(conn, affected, row["prompt_no"]),
            "withdraw",
        )
        return _template_dict(_fetch_template_row(conn, template_id))


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------


def _coerce_value(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise ApiError(
        422,
        "invalid_variable_value",
        f"variable {name!r} must be a string, number or boolean",
    )


def play(
    db_path: str, firmware_id: str, prompt_no: str, values: Mapping[str, Any]
) -> dict[str, Any]:
    with read_tx(db_path) as conn:
        firmware = _fetch_firmware_row(conn, firmware_id)
        if not firmware["enabled"]:
            raise ApiError(
                409, "firmware_disabled", f"firmware {firmware_id!r} is not enabled"
            )
        config = conn.execute(
            """
            SELECT lang, variables_json
              FROM firmware_prompts
             WHERE firmware_id = ? AND prompt_no = ?
            """,
            (firmware_id, prompt_no),
        ).fetchone()
        if config is None:
            raise ApiError(
                404,
                "prompt_not_configured",
                f"prompt {prompt_no!r} is not configured for firmware {firmware_id!r}",
            )

        declared = json.loads(config["variables_json"])
        absent = sorted(set(declared) - set(values))
        if absent:
            raise ApiError(
                422,
                "missing_variables",
                "missing variable values: " + ", ".join(absent),
                missing_variables=absent,
            )
        rendered_values = {name: _coerce_value(name, values[name]) for name in declared}

        chosen = domain.choose(_candidates(conn, prompt_no), config["lang"], declared)
        if chosen is None:
            # The invariant should make this unreachable; report clearly if the
            # catalogue was tampered with out of band.
            raise ApiError(
                409,
                "prompt_unavailable",
                f"no playable template for prompt {prompt_no!r} "
                f"({LANG_LABEL[config['lang']]})",
            )
        return {
            "firmware_id": firmware_id,
            "prompt_no": prompt_no,
            "requested_lang": config["lang"],
            "used_lang": chosen["lang"],
            "fallback": chosen["lang"] != config["lang"],
            "template_id": chosen["id"],
            "text": domain.render(chosen["content"], rendered_values),
        }
