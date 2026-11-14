import json

from fastapi.testclient import TestClient

from .conftest import make_config


def _client(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(make_config().model_dump_json())
    monkeypatch.setenv("CONFIG_PATH", str(path))
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    from app.main import app
    return TestClient(app)


def test_auth_and_csrf(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/healthz").status_code == 200
        # without login — API is closed
        assert client.get("/api/state").status_code == 401
        # wrong login
        assert client.post("/api/login", json={"user": "admin", "password": "wrong"}).status_code == 401
        # correct login — sets a session cookie
        assert client.post("/api/login", json={"user": "admin", "password": "secret"}).status_code == 200
        state = client.get("/api/state")
        assert state.status_code == 200
        assert "secret" not in json.dumps(state.json())
        # CSRF: POST without the X-Requested-With header
        assert client.post("/api/stack/restart").status_code == 403
        r = client.post("/api/hosts/nope/restart", headers={"X-Requested-With": "restart-ui"})
        assert r.status_code == 404
        # logout — the session is reset
        assert client.post("/api/logout").status_code == 200
        assert client.get("/api/state").status_code == 401


def test_roles(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(make_config().model_dump_json())
    monkeypatch.setenv("CONFIG_PATH", str(path))
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "apass")
    monkeypatch.setenv("SE_USER", "SE")
    monkeypatch.setenv("SE_PASSWORD", "spass")
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    from app.main import app
    with TestClient(app) as client:
        # SE logged in — role se, cannot edit
        assert client.post("/api/login", json={"user": "SE", "password": "spass"}).status_code == 200
        assert client.get("/api/state").json()["role"] == "se"
        r = client.post("/api/hosts", json={"id": "x", "name": "X", "hostname": "10.0.0.9",
                                            "os": "windows", "user": "u", "password": "p", "group": "g"},
                        headers={"X-Requested-With": "restart-ui"})
        assert r.status_code == 403  # SE cannot add
        client.post("/api/logout")
        # admin logged in — role admin, editing allowed
        assert client.post("/api/login", json={"user": "admin", "password": "apass"}).status_code == 200
        assert client.get("/api/state").json()["role"] == "admin"
        r = client.post("/api/hosts", json={"id": "newx", "name": "X", "hostname": "10.0.0.9",
                                            "os": "windows", "user": "u", "password": "p", "group": "g"},
                        headers={"X-Requested-With": "restart-ui"})
        assert r.status_code == 201  # admin can
