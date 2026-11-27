"""End-to-end API behaviour, including enable/withdraw atomicity."""

from __future__ import annotations

import threading

from tests.conftest import make_firmware, make_template, play


def test_play_renders_requested_language(client):
    make_template(client, "P1", "zh", "请到{地点}集合")
    make_template(client, "P1", "en", "Please gather at {地点}")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": ["地点"]}])

    response = play(client, "fw-A", "P1", {"地点": "north gate"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "Please gather at north gate"
    assert body["used_lang"] == "en"
    assert body["fallback"] is False
    assert "X-Prompt-Fallback" not in response.headers


def test_english_falls_back_to_chinese(client):
    make_template(client, "P1", "zh", "请到{地点}集合")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": ["地点"]}])

    response = play(client, "fw-A", "P1", {"地点": "北门"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "请到北门集合"
    assert body["used_lang"] == "zh"
    assert body["fallback"] is True
    assert response.headers["X-Prompt-Fallback"] == "en->zh"


def test_english_template_needing_extra_variable_is_skipped(client):
    make_template(client, "P1", "zh", "请到{地点}集合")
    make_template(client, "P1", "en", "Meet at {地点} with {guide}")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": ["地点"]}])

    body = play(client, "fw-A", "P1", {"地点": "北门"}).json()
    assert body["used_lang"] == "zh"
    assert body["fallback"] is True


def test_more_variables_then_latest_publish(client):
    first = make_template(client, "P1", "en", "One {a}")
    second = make_template(client, "P1", "en", "Two {a} {b}")
    third = make_template(client, "P1", "en", "Three {a} {b}")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": ["a", "b"]}])

    body = play(client, "fw-A", "P1", {"a": "1", "b": "2"}).json()
    assert body["template_id"] == third["id"]
    assert body["text"] == "Three 1 2"
    assert body["template_id"] not in (first["id"], second["id"])


def test_play_requires_declared_variables(client):
    make_template(client, "P1", "en", "Hello {name}")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": ["name"]}])

    response = play(client, "fw-A", "P1", {})
    assert response.status_code == 422
    assert response.json()["code"] == "missing_variables"


def test_play_rejects_disabled_firmware_and_unknown_prompt(client):
    make_template(client, "P1", "en", "Hello")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": []}])

    other = make_firmware(client, "fw-B", [], enable=False)
    assert other["enabled"] is False
    assert play(client, "fw-B", "P1").status_code == 409
    assert play(client, "fw-A", "P9").status_code == 404


def test_enable_refused_lists_every_missing_prompt_and_changes_nothing(client):
    make_template(client, "P1", "en", "Hello")
    client.post("/api/firmwares", json={"firmware_id": "fw-A"})

    response = client.put(
        "/api/firmwares/fw-A/enable",
        json={
            "prompts": [
                {"prompt_no": "P1", "lang": "en", "variables": []},
                {"prompt_no": "P2", "lang": "zh", "variables": ["who"]},
                {"prompt_no": "P3", "lang": "en", "variables": []},
            ]
        },
    )
    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == "prompt_missing"
    assert {(item["prompt_no"], item["lang"]) for item in payload["missing"]} == {
        ("P2", "zh"),
        ("P3", "en"),
    }

    state = client.get("/api/firmwares/fw-A").json()
    assert state["enabled"] is False
    assert state["prompts"] == []
    assert client.get("/api/firmwares/fw-A/missing").json() == []


def test_failed_reenable_keeps_previous_configuration(client):
    make_template(client, "P1", "en", "Hello")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": []}])

    response = client.put(
        "/api/firmwares/fw-A/enable",
        json={
            "prompts": [
                {"prompt_no": "P1", "lang": "en", "variables": []},
                {"prompt_no": "P2", "lang": "en", "variables": []},
            ]
        },
    )
    assert response.status_code == 409

    state = client.get("/api/firmwares/fw-A").json()
    assert state["enabled"] is True
    assert [item["prompt_no"] for item in state["prompts"]] == ["P1"]
    assert play(client, "fw-A", "P1").status_code == 200


def test_empty_enable_is_rejected_without_touching_state(client):
    make_template(client, "P1", "en", "Hello")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": []}])

    response = client.put("/api/firmwares/fw-A/enable", json={"prompts": []})
    assert response.status_code == 400
    assert client.get("/api/firmwares/fw-A").json()["enabled"] is True


def test_withdraw_only_template_in_use_is_refused(client):
    template = make_template(client, "P1", "en", "Hello")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": []}])

    response = client.post(f"/api/templates/{template['id']}/withdraw")
    assert response.status_code == 409
    payload = response.json()
    assert payload["missing"] == [
        {"firmware_id": "fw-A", "prompt_no": "P1", "lang": "en"}
    ]
    assert client.get(f"/api/templates/{template['id']}").json()["status"] == "published"


def test_withdraw_allowed_when_another_template_covers_the_prompt(client):
    primary = make_template(client, "P1", "en", "Primary")
    backup = make_template(client, "P1", "en", "Backup")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": []}])

    assert client.post(f"/api/templates/{primary['id']}/withdraw").status_code == 200
    assert client.post(f"/api/templates/{backup['id']}/withdraw").status_code == 409
    # New requests select only from what remains.
    assert play(client, "fw-A", "P1").json()["template_id"] == backup["id"]


def test_withdraw_rechecks_only_same_prompt_number(client):
    p1 = make_template(client, "P1", "en", "Hello")
    make_template(client, "P2", "en", "Bye")
    make_firmware(
        client,
        "fw-A",
        [
            {"prompt_no": "P1", "lang": "en", "variables": []},
            {"prompt_no": "P2", "lang": "en", "variables": []},
        ],
    )

    # P1 has a second template so withdrawing the first is fine even though P2
    # has no spare: the check never looks at P2.
    make_template(client, "P1", "en", "Hello again")
    assert client.post(f"/api/templates/{p1['id']}/withdraw").status_code == 200
    assert play(client, "fw-A", "P2").status_code == 200


def test_fallback_keeps_working_after_english_withdrawn(client):
    zh = make_template(client, "P1", "zh", "请到{地点}")
    en = make_template(client, "P1", "en", "Go to {地点}")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": ["地点"]}])

    # Chinese fallback still covers the firmware, so withdrawing the only
    # English template is allowed and playback falls back to Chinese.
    assert client.post(f"/api/templates/{en['id']}/withdraw").status_code == 200
    body = play(client, "fw-A", "P1", {"地点": "大门"}).json()
    assert body["template_id"] == zh["id"]
    assert body["fallback"] is True

    # Chinese is now the only supply, so it cannot be withdrawn.
    assert client.post(f"/api/templates/{zh['id']}/withdraw").status_code == 409


def test_withdraw_visible_to_new_requests_immediately(client):
    first = make_template(client, "P1", "en", "First")
    second = make_template(client, "P1", "en", "Second")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": []}])

    assert play(client, "fw-A", "P1").json()["template_id"] == second["id"]
    client.post(f"/api/templates/{second['id']}/withdraw")
    assert play(client, "fw-A", "P1").json()["template_id"] == first["id"]


def test_edit_published_template_cannot_strand_firmware(client):
    only = make_template(client, "P1", "en", "Plain")
    make_firmware(client, "fw-A", [{"prompt_no": "P1", "lang": "en", "variables": []}])

    response = client.patch(
        f"/api/templates/{only['id']}", json={"content": "Needs {extra}"}
    )
    assert response.status_code == 409
    assert play(client, "fw-A", "P1").json()["text"] == "Plain"

    # With a spare that covers the new variable set, the edit is allowed.
    make_template(client, "P1", "en", "Backup")
    ok = client.patch(f"/api/templates/{only['id']}", json={"content": "Needs {extra}"})
    assert ok.status_code == 200


def test_edit_and_republish_withdrawn_template(client):
    template = make_template(client, "P1", "en", "Old {x}", publish=False)
    edited = client.patch(f"/api/templates/{template['id']}", json={"content": "New {y}"})
    assert edited.status_code == 200
    assert edited.json()["variables"] == ["y"]
    published = client.post(f"/api/templates/{template['id']}/publish")
    assert published.status_code == 200
    assert published.json()["publish_seq"] > 0


def test_bad_template_syntax_is_rejected(client):
    response = client.post(
        "/api/templates",
        json={"prompt_no": "P1", "lang": "en", "content": "Hello {name"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "template_syntax_error"


def test_validation_rejects_bad_input(client):
    assert (
        client.post(
            "/api/templates",
            json={"prompt_no": "P 1", "lang": "en", "content": "x"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/templates",
            json={"prompt_no": "P1", "lang": "fr", "content": "x"},
        ).status_code
        == 422
    )
    client.post("/api/firmwares", json={"firmware_id": "fw-A"})
    dup = client.put(
        "/api/firmwares/fw-A/enable",
        json={
            "prompts": [
                {"prompt_no": "P1", "lang": "en", "variables": []},
                {"prompt_no": "P1", "lang": "zh", "variables": []},
            ]
        },
    )
    assert dup.status_code == 422


def test_concurrent_enable_and_withdraw_never_break_supply(client, tmp_path):
    """Hammer the two operations; every play must succeed and states stay valid."""
    for index in range(6):
        make_template(client, "P1", "en", f"English {index}")
    make_template(client, "P1", "zh", "中文")
    prompt = {"prompt_no": "P1", "lang": "en", "variables": []}
    make_firmware(client, "fw-A", [prompt])

    errors: list[str] = []
    stop = threading.Event()

    def player():
        while not stop.is_set():
            response = play(client, "fw-A", "P1")
            if response.status_code != 200:
                errors.append(response.text)

    def toggler(firmware_id):
        for _ in range(10):
            client.put(f"/api/firmwares/{firmware_id}/enable", json={"prompts": [prompt]})
            for template in client.get("/api/templates", params={"prompt_no": "P1"}).json():
                if template["status"] == "published":
                    client.post(f"/api/templates/{template['id']}/withdraw")

    client.post("/api/firmwares", json={"firmware_id": "fw-B"})
    threads = [threading.Thread(target=player) for _ in range(3)]
    threads += [
        threading.Thread(target=toggler, args=("fw-A",)),
        threading.Thread(target=toggler, args=("fw-B",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads[3:]:
        thread.join()
    stop.set()
    for thread in threads[:3]:
        thread.join()

    assert errors == []
    for firmware_id in ("fw-A", "fw-B"):
        assert client.get(f"/api/firmwares/{firmware_id}/missing").json() == []
    assert play(client, "fw-A", "P1").status_code == 200
