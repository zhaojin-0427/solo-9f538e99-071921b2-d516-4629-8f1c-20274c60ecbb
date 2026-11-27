from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    app = create_app(str(db))
    with TestClient(app) as test_client:
        yield test_client


def make_template(client, prompt_no, lang, content, *, publish=True):
    response = client.post(
        "/api/templates",
        json={"prompt_no": prompt_no, "lang": lang, "content": content},
    )
    assert response.status_code == 201, response.text
    data = response.json()
    if publish:
        published = client.post(f"/api/templates/{data['id']}/publish")
        assert published.status_code == 200, published.text
        data = published.json()
    return data


def make_firmware(client, firmware_id, prompts, *, enable=True):
    created = client.post(
        "/api/firmwares", json={"firmware_id": firmware_id, "name": firmware_id}
    )
    assert created.status_code == 201, created.text
    if not enable:
        return created.json()
    enabled = client.put(
        f"/api/firmwares/{firmware_id}/enable", json={"prompts": prompts}
    )
    assert enabled.status_code == 200, enabled.text
    return enabled.json()


def play(client, firmware_id, prompt_no, variables=None, **kwargs):
    return client.post(
        f"/api/firmwares/{firmware_id}/prompts/{prompt_no}/play",
        json={"variables": variables or {}},
        **kwargs,
    )
