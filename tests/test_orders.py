"""Tests for 制单 (recording orders) and 交件/入账 (delivery review).

These exercise the requirement checklist:

* whole order resolved with the existing rules from one catalogue snapshot;
* merge by actual language + full composed sentence;
* existing recording numbers marked; unresolvable samples listed with reasons;
* delivery re-reviews only that entry's samples against the latest committed
  catalogue; books only when >=1 sample still has the original lang/sentence;
* first recording number for batch+lang+sentence wins and is reused;
* re-issuing an order marks the reused recording;
* template/firmware commits during review are observed at delivery time.
"""

from __future__ import annotations

from tests.conftest import make_firmware, make_template


def order(client, batch_no, samples, *, note=None, expected=201):
    response = client.post(
        "/api/recording-batches",
        json={"batch_no": batch_no, "note": note, "samples": samples},
    )
    assert response.status_code == expected, response.text
    return response.json()


def deliver(client, batch_no, entry_id, recording_no, *, path=False, expected=201, **extra):
    body = {"entry_id": entry_id, "recording_no": recording_no, **extra}
    if not path:
        body["batch_no"] = batch_no
        response = client.post("/api/deliveries", json=body)
    else:
        response = client.post(
            f"/api/recording-batches/{batch_no}/deliveries", json=body
        )
    assert response.status_code == expected, response.text
    return response


def _entry_by_text(body, text):
    matches = [entry for entry in body["entries"] if entry["text"] == text]
    assert len(matches) == 1, f"entry with text {text!r} not found in {body}"
    return matches[0]


# ---------------------------------------------------------------------------
# 制单
# ---------------------------------------------------------------------------


def test_order_merges_identical_lang_sentence_and_lists_unresolved(client):
    make_template(client, "R1", "en", "Welcome to the studio.")
    make_template(client, "R2", "en", "Welcome to the studio.")
    make_firmware(client, "fw-a", [{"prompt_no": "R1", "lang": "en", "variables": []}])
    make_firmware(client, "fw-b", [{"prompt_no": "R2", "lang": "en", "variables": []}])

    body = order(
        client,
        "B1",
        [
            {"sample_no": 1, "firmware_id": "fw-a", "prompt_no": "R1", "variables": {}},
            {"sample_no": 2, "firmware_id": "fw-b", "prompt_no": "R2", "variables": {}},
            {"sample_no": 3, "firmware_id": "fw-a", "prompt_no": "R-NOPE", "variables": {}},
            {"sample_no": 4, "firmware_id": "fw-ghost", "prompt_no": "R1", "variables": {}},
        ],
    )

    assert body["revision"] == 1
    assert body["reissued"] is False
    assert body["sample_count"] == 4
    assert body["resolved_count"] == 2
    assert body["entry_count"] == 1
    entry = body["entries"][0]
    assert entry["lang"] == "en"
    assert entry["text"] == "Welcome to the studio."
    assert entry["sample_count"] == 2
    assert entry["sample_numbers"] == ["1", "2"]
    assert entry["recorded"] is False

    unresolved = {item["sample_no"]: item for item in body["unresolved"]}
    assert set(unresolved) == {"3", "4"}
    assert unresolved["3"]["reason_code"] == "prompt_not_configured"
    assert unresolved["4"]["reason_code"] == "firmware_not_found"


def test_order_reports_no_playable_template_for_stale_disabled_config(client):
    tpl = make_template(client, "OLD", "en", "Old line.")
    make_firmware(client, "fw-old", [{"prompt_no": "OLD", "lang": "en", "variables": []}])
    # Disabling keeps the prompt configuration, and withdrawing the only
    # template is then allowed (supply guard only covers enabled firmwares).
    client.post("/api/firmwares/fw-old/disable")
    assert client.post(f"/api/templates/{tpl['id']}/withdraw").status_code == 200

    body = order(
        client,
        "B1B",
        [{"firmware_id": "fw-old", "prompt_no": "OLD", "variables": {}}],
    )
    reason = body["unresolved"][0]
    assert reason["reason_code"] == "no_playable_template"
    assert reason["firmware_enabled"] is False


def test_order_merges_on_actual_fallback_language(client):
    # English-request firmware falls back to the Chinese template.
    make_template(client, "P1", "zh", "你好{name}")
    make_firmware(
        client, "fw-en", [{"prompt_no": "P1", "lang": "en", "variables": ["name"]}]
    )
    make_firmware(
        client, "fw-zh", [{"prompt_no": "P1", "lang": "zh", "variables": ["name"]}]
    )

    body = order(
        client,
        "B2",
        [
            {"sample_no": "en-case", "firmware_id": "fw-en", "prompt_no": "P1",
             "variables": {"name": "阿明"}},
            {"sample_no": "zh-case", "firmware_id": "fw-zh", "prompt_no": "P1",
             "variables": {"name": "阿明"}},
        ],
    )
    assert body["entry_count"] == 1
    entry = body["entries"][0]
    assert entry["lang"] == "zh"
    assert entry["text"] == "你好阿明"
    assert entry["sample_numbers"] == ["en-case", "zh-case"]
    assert entry["samples"][0]["fallback"] is True


def test_order_reports_missing_variables_as_reason(client):
    make_template(client, "P1", "en", "At {gate} and {time}")
    make_firmware(
        client,
        "fw-a",
        [{"prompt_no": "P1", "lang": "en", "variables": ["gate", "time"]}],
    )
    body = order(
        client,
        "B3",
        [{"firmware_id": "fw-a", "prompt_no": "P1", "variables": {"gate": "A"}}],
    )
    assert body["resolved_count"] == 0
    reason = body["unresolved"][0]
    assert reason["reason_code"] == "missing_variables"
    assert reason["missing_variables"] == ["time"]
    # omitted sample_no falls back to 1-based position
    assert reason["sample_no"] == "1"


def test_order_uses_only_published_templates(client):
    published = make_template(client, "P1", "en", "Published sentence.")
    make_firmware(client, "fw-a", [{"prompt_no": "P1", "lang": "en", "variables": []}])
    make_template(client, "P1", "en", "Draft different sentence.", publish=False)

    body = order(
        client,
        "B4",
        [{"firmware_id": "fw-a", "prompt_no": "P1", "variables": {}}],
    )
    assert body["entry_count"] == 1
    entry = body["entries"][0]
    assert entry["text"] == "Published sentence."
    assert entry["template_id"] == published["id"]


# ---------------------------------------------------------------------------
# 交件 + 逐例复核 + 入账
# ---------------------------------------------------------------------------


def _two_firmwares_one_sentence(client):
    tpl_a = make_template(client, "R1", "en", "Shared sentence.")
    tpl_b = make_template(client, "R2", "en", "Shared sentence.")
    make_firmware(client, "fw-a", [{"prompt_no": "R1", "lang": "en", "variables": []}])
    make_firmware(client, "fw-b", [{"prompt_no": "R2", "lang": "en", "variables": []}])
    body = order(
        client,
        "B10",
        [
            {"sample_no": 1, "firmware_id": "fw-a", "prompt_no": "R1", "variables": {}},
            {"sample_no": 2, "firmware_id": "fw-b", "prompt_no": "R2", "variables": {}},
        ],
    )
    return tpl_a, tpl_b, body


def test_delivery_books_with_one_match_and_lists_mismatched(client):
    tpl_a, _tpl_b, body = _two_firmwares_one_sentence(client)
    entry_id = body["entries"][0]["entry_id"]

    # Change only the sentence behind sample 1 after 制单.
    patched = client.patch(
        f"/api/templates/{tpl_a['id']}", json={"content": "Changed sentence."}
    )
    assert patched.status_code == 200, patched.text

    response = deliver(client, "B10", entry_id, "REC-001", path=True)
    d = response.json()
    assert d["status"] == "booked"
    assert d["booked"] is True
    assert d["effective_recording_no"] == "REC-001"
    assert d["reviewed_count"] == 2
    assert d["matched_count"] == 1
    assert d["mismatched_count"] == 1
    bad = d["mismatched"][0]
    assert bad["sample_no"] == "1"
    assert bad["reason_code"] == "sentence_changed"
    assert bad["text"] == "Changed sentence."
    assert bad["original_text"] == "Shared sentence."
    assert d["matched"][0]["sample_no"] == "2"

    recs = client.get("/api/recording-batches/B10/recordings").json()
    assert len(recs) == 1
    assert recs[0]["recording_no"] == "REC-001"
    assert recs[0]["text"] == "Shared sentence."


def test_delivery_rejected_when_no_sample_matches_and_nothing_booked(client):
    tpl_a, tpl_b, body = _two_firmwares_one_sentence(client)
    entry_id = body["entries"][0]["entry_id"]
    client.patch(f"/api/templates/{tpl_a['id']}", json={"content": "Now A."})
    client.patch(f"/api/templates/{tpl_b['id']}", json={"content": "Now B."})

    response = deliver(
        client, "B10", entry_id, "REC-X", expected=409
    )
    payload = response.json()
    assert payload["code"] == "delivery_not_booked"
    assert payload["booked"] is False
    assert payload["matched_count"] == 0
    assert {item["sample_no"] for item in payload["mismatched"]} == {"1", "2"}

    assert client.get("/api/recording-batches/B10/recordings").json() == []
    assert client.get("/api/recording-batches/B10/deliveries").json() == []
    # Re-issued order carries no recording number.
    again = order(
        client,
        "B10",
        [
            {"sample_no": 1, "firmware_id": "fw-a", "prompt_no": "R1", "variables": {}},
            {"sample_no": 2, "firmware_id": "fw-b", "prompt_no": "R2", "variables": {}},
        ],
    )
    assert all(e["recording_no"] is None for e in again["entries"])


def test_first_recording_number_in_batch_is_kept(client):
    make_template(client, "R1", "en", "Only sentence.")
    make_firmware(client, "fw-a", [{"prompt_no": "R1", "lang": "en", "variables": []}])
    body = order(client, "B20", [{"firmware_id": "fw-a", "prompt_no": "R1"}])
    entry_id = body["entries"][0]["entry_id"]

    first = deliver(client, "B20", entry_id, "REC-FIRST").json()
    assert first["status"] == "booked"
    second = deliver(client, "B20", entry_id, "REC-SECOND").json()
    assert second["status"] == "reused"
    assert second["effective_recording_no"] == "REC-FIRST"

    recs = client.get("/api/recording-batches/B20/recordings").json()
    assert [(r["text"], r["recording_no"]) for r in recs] == [
        ("Only sentence.", "REC-FIRST")
    ]
    deliveries = client.get("/api/recording-batches/B20/deliveries").json()
    assert [d["status"] for d in deliveries] == ["booked", "reused"]


def test_reissue_marks_existing_recording_for_reuse(client):
    _tpl_a, _tpl_b, body = _two_firmwares_one_sentence(client)
    entry_id = body["entries"][0]["entry_id"]
    deliver(client, "B10", entry_id, "REC-7")

    again = order(
        client,
        "B10",
        [
            {"sample_no": 1, "firmware_id": "fw-a", "prompt_no": "R1", "variables": {}},
            {"sample_no": 2, "firmware_id": "fw-b", "prompt_no": "R2", "variables": {}},
        ],
    )
    assert again["revision"] == 2
    assert again["reissued"] is True
    entry = again["entries"][0]
    assert entry["recording_no"] == "REC-7"
    assert entry["batch_recording_no"] == "REC-7"
    assert entry["recording_scope"] == "batch"
    assert entry["recorded"] is True

    revisions = client.get("/api/recording-batches/B10/orders").json()
    assert [r["revision"] for r in revisions] == [1, 2]
    rev1 = client.get("/api/recording-batches/B10/orders/1").json()
    assert rev1["revision"] == 1


def test_change_sentence_then_reissue_splits_entries_and_reuses(client):
    """The required self-test arc: 改句 -> delivery (one still matches) ->
    再次出单: unchanged sentence reuses the recording, changed one is new."""
    tpl_a, _tpl_b, body = _two_firmwares_one_sentence(client)
    old_entry_id = body["entries"][0]["entry_id"]

    client.patch(f"/api/templates/{tpl_a['id']}", json={"content": "Studio A line."})
    d = deliver(client, "B10", old_entry_id, "REC-9").json()
    assert d["matched_count"] == 1
    assert d["mismatched_count"] == 1
    assert d["effective_recording_no"] == "REC-9"

    again = order(
        client,
        "B10",
        [
            {"sample_no": 1, "firmware_id": "fw-a", "prompt_no": "R1", "variables": {}},
            {"sample_no": 2, "firmware_id": "fw-b", "prompt_no": "R2", "variables": {}},
        ],
    )
    assert again["entry_count"] == 2
    unchanged = _entry_by_text(again, "Shared sentence.")
    changed = _entry_by_text(again, "Studio A line.")
    assert unchanged["recording_no"] == "REC-9"
    assert changed["recording_no"] is None
    # The new entry can itself be delivered and booked.
    d2 = deliver(client, "B10", changed["entry_id"], "REC-10").json()
    assert d2["status"] == "booked"
    assert d2["matched_count"] == 1


def test_delivery_judges_latest_committed_firmware_config(client):
    make_template(client, "P1", "en", "Hello.")
    make_template(client, "P1", "zh", "你好。")
    make_firmware(client, "fw-a", [{"prompt_no": "P1", "lang": "en", "variables": []}])
    body = order(client, "B30", [{"firmware_id": "fw-a", "prompt_no": "P1"}])
    entry_id = body["entries"][0]["entry_id"]
    assert body["entries"][0]["lang"] == "en"

    # Commit a firmware configuration change during review: now Chinese.
    changed = client.put(
        "/api/firmwares/fw-a/enable",
        json={"prompts": [{"prompt_no": "P1", "lang": "zh", "variables": []}]},
    )
    assert changed.status_code == 200, changed.text

    resp = deliver(client, "B30", entry_id, "REC-Z", expected=409)
    bad = resp.json()["mismatched"][0]
    assert bad["used_lang"] == "zh"
    assert bad["text"] == "你好。"
    assert bad["reason_code"] in {"language_changed", "lang_and_sentence_changed"}


def test_delivery_detects_newly_missing_variables_from_config(client):
    make_template(client, "P1", "en", "Hello.")
    make_firmware(client, "fw-a", [{"prompt_no": "P1", "lang": "en", "variables": []}])
    body = order(
        client,
        "B31",
        [{"firmware_id": "fw-a", "prompt_no": "P1", "variables": {}}],
    )
    entry_id = body["entries"][0]["entry_id"]

    client.put(
        "/api/firmwares/fw-a/enable",
        json={"prompts": [{"prompt_no": "P1", "lang": "en", "variables": ["extra"]}]},
    )
    resp = deliver(client, "B31", entry_id, "REC-Z", expected=409)
    bad = resp.json()["mismatched"][0]
    assert bad["reason_code"] == "missing_variables"
    assert bad["missing_variables"] == ["extra"]


def test_review_endpoint_is_read_only(client):
    _tpl_a, _tpl_b, body = _two_firmwares_one_sentence(client)
    entry_id = body["entries"][0]["entry_id"]
    response = client.post(
        "/api/recording-batches/B10/review", json={"entry_id": entry_id}
    )
    assert response.status_code == 200, response.text
    review = response.json()
    assert review["reviewed_count"] == 2
    assert review["matched_count"] == 2
    assert client.get("/api/recording-batches/B10/deliveries").json() == []
    assert client.get("/api/recording-batches/B10/recordings").json() == []


def test_entry_locators_entry_no_and_nested_route(client):
    make_template(client, "P1", "en", "One.")
    make_template(client, "P2", "en", "Two.")
    make_firmware(client, "fw-a", [
        {"prompt_no": "P1", "lang": "en", "variables": []},
        {"prompt_no": "P2", "lang": "en", "variables": []},
    ])
    body = order(client, "B40", [
        {"firmware_id": "fw-a", "prompt_no": "P1"},
        {"firmware_id": "fw-a", "prompt_no": "P2"},
    ])
    assert [e["entry_no"] for e in body["entries"]] == [1, 2]

    # entry_no + latest order, string form "E2", flat route
    resp = client.post(
        "/api/deliveries",
        json={"batch_no": "B40", "entry": "E2", "recording_no": "REC-E2"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["entry_no"] == 2

    # nested route + entry_no integer
    resp = client.post(
        "/api/recording-batches/B40/deliveries",
        json={"entry_no": 1, "recording_no": "REC-E1"},
    )
    assert resp.status_code == 201, resp.text

    # unknown batch / entry
    assert client.post(
        "/api/deliveries",
        json={"batch_no": "NOPE", "entry_id": 1, "recording_no": "R"},
    ).status_code == 404
    assert client.post(
        "/api/deliveries",
        json={"batch_no": "B40", "entry_id": 99999, "recording_no": "R"},
    ).status_code == 404
    # path/body batch mismatch
    mismatch = client.post(
        "/api/recording-batches/B40/deliveries",
        json={"batch_no": "OTHER", "entry_no": 1, "recording_no": "R"},
    )
    assert mismatch.status_code == 400


def test_global_library_reuse_across_batches(client):
    make_template(client, "P1", "en", "Global sentence.")
    make_firmware(client, "fw-a", [{"prompt_no": "P1", "lang": "en", "variables": []}])

    b1 = order(client, "B50", [{"firmware_id": "fw-a", "prompt_no": "P1"}])
    deliver(client, "B50", b1["entries"][0]["entry_id"], "REC-GLOBAL")

    b2 = order(client, "B51", [{"firmware_id": "fw-a", "prompt_no": "P1"}])
    entry = b2["entries"][0]
    assert entry["batch_recording_no"] is None
    assert entry["library_recording_no"] == "REC-GLOBAL"
    assert entry["recording_no"] == "REC-GLOBAL"
    assert entry["recording_scope"] == "library"

    lib = client.get("/api/recordings", params={"text": "Global sentence."}).json()
    assert len(lib) == 1 and lib[0]["recording_no"] == "REC-GLOBAL"


def test_batch_listing_and_get_latest(client):
    make_template(client, "P1", "en", "X.")
    make_firmware(client, "fw-a", [{"prompt_no": "P1", "lang": "en", "variables": []}])
    order(client, "B60", [{"firmware_id": "fw-a", "prompt_no": "P1"}])
    order(client, "B60", [{"firmware_id": "fw-a", "prompt_no": "P1"}])

    batches = client.get("/api/recording-batches").json()
    row = [b for b in batches if b["batch_no"] == "B60"][0]
    assert row["revision"] == 2
    assert row["order_count"] == 2

    latest = client.get("/api/recording-batches/B60").json()
    assert latest["revision"] == 2
    assert client.get("/api/recording-batches/NOPE").status_code == 404


def test_alias_payload_shapes_are_accepted(client):
    make_template(client, "P1", "en", "Alias {gate}.")
    make_firmware(
        client, "fw-a", [{"prompt_no": "P1", "lang": "en", "variables": ["gate"]}]
    )
    # items/cases aliases, firmware/prompt aliases, values alias, batch alias.
    resp = client.post(
        "/api/orders",
        json={
            "batch": "B70",
            "items": [
                {"no": 7, "firmware": "fw-a", "prompt": "P1",
                 "values": {"gate": "A"}}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["batch_no"] == "B70"
    assert body["entries"][0]["samples"][0]["sample_no"] == "7"

    # flat variables also tolerated
    resp2 = client.post(
        "/api/recording-batches",
        json={
            "batch_no": "B71",
            "cases": [
                {"firmware_id": "fw-a", "prompt_no": "P1", "gate": "North"}
            ],
        },
    )
    assert resp2.status_code == 201, resp2.text
    assert resp2.json()["entries"][0]["text"] == "Alias North."


def test_mismatched_sample_now_hitting_recorded_sentence_suggests_reuse(client):
    make_template(client, "PA", "en", "Sentence A.")
    make_template(client, "PB", "en", "Sentence B.")
    make_firmware(client, "fw-a", [{"prompt_no": "PA", "lang": "en", "variables": []}])
    make_firmware(client, "fw-b", [{"prompt_no": "PB", "lang": "en", "variables": []}])

    body = order(
        client,
        "B80",
        [
            {"sample_no": "A", "firmware_id": "fw-a", "prompt_no": "PA"},
            {"sample_no": "B", "firmware_id": "fw-b", "prompt_no": "PB"},
        ],
    )
    entry_a = _entry_by_text(body, "Sentence A.")
    entry_b = _entry_by_text(body, "Sentence B.")
    deliver(client, "B80", entry_a["entry_id"], "REC-A")

    # During review PB's sentence is changed to the already-recorded A sentence.
    pb_id = next(t["id"] for t in client.get("/api/templates").json()
                 if t["prompt_no"] == "PB")
    assert client.patch(
        f"/api/templates/{pb_id}", json={"content": "Sentence A."}
    ).status_code == 200

    # Delivering B with a new recording number is not booked (B no longer has
    # any sample matching its original 'Sentence B.'); the mismatched sample is
    # flagged with the first recording number it can now reuse.
    resp = deliver(client, "B80", entry_b["entry_id"], "REC-B-NEW", expected=409)
    item = resp.json()["mismatched"][0]
    assert item["reason_code"] == "sentence_changed"
    assert item["text"] == "Sentence A."
    assert item["current_recording_no"] == "REC-A"
    assert item["current_recording_scope"] == "batch"
    assert client.get("/api/recording-batches/B80/recordings").json()[0][
        "recording_no"
    ] == "REC-A"


def test_chinese_named_payload_keys_are_accepted(client):
    make_template(client, "P1", "zh", "你好{name}")
    make_firmware(
        client, "fw-cn", [{"prompt_no": "P1", "lang": "zh", "variables": ["name"]}]
    )
    resp = client.post(
        "/api/recording-batches",
        json={
            "录音批号": "中文批号-1",
            "样例": [
                {"编号": 1, "固件号": "fw-cn", "提示号": "P1",
                 "变量值": {"name": "阿强"}}
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["batch_no"] == "中文批号-1"
    entry_id = body["entries"][0]["entry_id"]

    resp2 = client.post(
        "/api/deliveries",
        json={"批号": "中文批号-1", "原条目": {"entry_id": entry_id}, "录音号": "录音-001"},
    )
    assert resp2.status_code == 201, resp2.text
    assert resp2.json()["effective_recording_no"] == "录音-001"
