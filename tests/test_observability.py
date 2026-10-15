"""HW2 Part D: authentication tests for the session endpoints.

Offline: no Langfuse, Docker, or model key. The TestClient is used without a
`with` block, so the server lifespan (tracing setup) never runs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import app as server_app


@pytest.fixture
def client(world: dict) -> TestClient:
    server_app._SESSIONS.clear()
    return TestClient(server_app.app)


def test_create_session_rejects_role_mismatch(client: TestClient) -> None:
    # User 1 is a shopper in the database; claiming support must fail.
    response = client.post("/sessions", json={"user_id": 1, "role": "support"})

    assert response.status_code == 403
    assert "token" not in response.json()
    assert server_app._SESSIONS == {}


def test_token_cannot_authorize_another_session(client: TestClient) -> None:
    first = client.post("/sessions", json={"user_id": 1, "role": "shopper"}).json()
    second = client.post("/sessions", json={"user_id": 9002, "role": "merchant"}).json()

    # Shopper 1's token presented for merchant 9002's session.
    response = client.post(
        f"/sessions/{second['session_id']}/messages",
        headers={"Authorization": f"Bearer {first['token']}"},
        json={"message": "Show me order 4127."},
    )

    assert response.status_code == 403
