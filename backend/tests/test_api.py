from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    settings = Settings(tmp_path / "data", tmp_path / "dsh")
    app = create_app(settings, start_runtime=False)
    with TestClient(app) as browser:
        yield browser


def project(client):
    response = client.post("/api/projects", json={"title": "测试作品"})
    assert response.status_code == 200
    return response.json()["id"]


def configure(client):
    client.put("/api/settings", json={"api_key": "test-private-key"})
    client.app.state.service.probe_result["runtime_ready"] = True


def test_settings_are_redacted_and_persisted(client):
    assert client.get("/api/settings").json()["has_api_key"] is False
    configure(client)
    response = client.get("/api/settings")
    assert response.json()["has_api_key"] is True
    assert "test-private-key" not in response.text
    settings = client.app.state.settings
    assert Settings(settings.data_dir, settings.dsh_home).key() == "test-private-key"
    import os
    if os.name == "nt":
        assert "test-private-key" not in settings.path.read_text()
    client.put("/api/settings", json={"api_key": "", "model": "deepseek-v4-flash"})
    assert settings.key() == "test-private-key"
    assert client.put("/api/settings", json={"base_url": "http://evil.invalid"}).status_code == 400


def test_missing_key_does_not_create_fake_task(client):
    pid = project(client)
    response = client.post(f"/api/projects/{pid}/messages", data={"text": "制作游戏"})
    assert response.status_code == 409
    loaded = client.get(f"/api/projects/{pid}").json()
    assert loaded["messages"] == [] and loaded["tasks"] == []


def test_upload_and_queue_and_cancel(client):
    configure(client)
    pid = project(client)
    response = client.post(f"/api/projects/{pid}/messages", data={"text": "使用这个素材"},
                           files={"files": ("../../sprite.txt", b"test-asset", "text/plain")})
    assert response.status_code == 200
    tid = response.json()["task_id"]
    loaded = client.get(f"/api/projects/{pid}").json()
    attachment = loaded["messages"][0]["attachments"][0]
    assert attachment["name"] == "sprite.txt"
    workspace = client.app.state.service.workspace(pid)
    assert (workspace / attachment["path"]).read_bytes() == b"test-asset"
    assert client.post(f"/api/projects/{pid}/messages", data={"text": "重复"}).status_code == 409
    assert client.post(f"/api/tasks/{tid}/cancel").status_code == 200
    events = client.get(f"/api/tasks/{tid}/events").text
    assert "event: message" in events and '"status": "cancelled"' in events
    assert client.get(f"/api/tasks/{tid}").json()["status"] == "cancelled"
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert (workspace / attachment["path"]).is_file()


def test_file_confinement_and_no_backend_static(client):
    pid = project(client)
    workspace = client.app.state.service.workspace(pid)
    (workspace / "Player.cs").write_text("public class Player {}")
    assert client.get(f"/api/projects/{pid}/file", params={"path": "Player.cs"}).json()["content"] == "public class Player {}"
    assert client.get(f"/api/projects/{pid}/file", params={"path": "../settings.json"}).status_code in {400, 403}
    assert client.get(f"/api/projects/{pid}/file", params={"path": ".env"}).status_code == 403
    assert client.get("/backend/data/settings.json").status_code == 404
    assert client.get("/ai.js").status_code == 200


def test_cross_origin_and_host_rejected(client):
    assert client.put("/api/settings", json={"model": "bad"}, headers={"Origin": "null"}).status_code == 403
    assert client.get("/api/settings", headers={"Origin": "http://evil.invalid"}).status_code == 403
    assert client.get("/api/settings", headers={"Host": "evil.invalid"}).status_code == 400
    assert client.get("/api/settings", headers={"Origin": "http://testserver"}).status_code == 200


def test_publish_and_artifact_http_isolation(client):
    pid = project(client)
    assert client.post(f"/api/projects/{pid}/publish", json={}).status_code == 409
    db, settings = client.app.state.db, client.app.state.settings
    build_dir = settings.data_dir / "projects" / pid / "builds" / "build-test"
    build_dir.mkdir(parents=True)
    (build_dir / "index.html").write_text("<html>artifact fixture</html>")
    (build_dir / "game.wasm").write_bytes(b"\x00asmtest")
    db.execute("INSERT INTO builds VALUES(?,?,?,?,?,?)", ("build-test", pid, None, "2026-01-01", "index.html", "fixture"))
    response = client.post(f"/api/projects/{pid}/publish", json={"build_id": "build-test"})
    assert response.json()["scope"] == "local"
    artifact = client.get(response.json()["url"])
    assert "sandbox" in artifact.headers["Content-Security-Policy"]
    assert "allow-same-origin" not in artifact.headers["Content-Security-Policy"]
    assert artifact.headers["Access-Control-Allow-Origin"] == "*"
    wasm = client.get("/builds/build-test/game.wasm")
    assert wasm.headers["Content-Type"] == "application/wasm"


def test_adapter_uses_sdk_and_records_whitelisted_events(client, monkeypatch):
    from app import dsh_service

    calls = []

    class TestHarness:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def run(self, prompt, *, session_id, on_notification):
            calls.append((prompt, session_id))
            def emit(kind, data):
                on_notification(SimpleNamespace(method="session.event", payload={
                    "sessionId": session_id, "event": {"type": kind, "data": data}}))
            emit("request/header", {"secret": "do-not-save-raw-notifications"})
            emit("tool/call", {"name": "pwsh", "arguments": "do-not-save-commands"})
            emit("tool/result", {"message": {"content": [{"type": "tool-result", "isError": False}]}})
            emit("assistant/message", {"message": {"content": [{"type": "text", "text": "已检查工程，尚无构建。"}]}})
            return SimpleNamespace(final_response="已检查工程，尚无构建。", finish_reason="completed")

        def close(self):
            pass

    monkeypatch.setattr(dsh_service, "DeepSeekHarness", TestHarness)
    configure(client)
    pid = project(client)
    db, service = client.app.state.db, client.app.state.service
    for prompt in ["检查工程", "继续检查"]:
        db.message(pid, "user", prompt)
        task = db.create_task(pid, prompt)
        service._run(task)
        assert db.task(task["id"])["status"] == "completed"
        assert db.builds(pid) == []
        event_text = json.dumps(db.events(task["id"]), ensure_ascii=False)
        assert "do-not-save" not in event_text
        assert '"build": null' in event_text
    # One SDK instance and one DSH session across both turns, not a home-made model loop.
    assert len([call for call in calls if isinstance(call, dict)]) == 1
    turns = [call for call in calls if isinstance(call, tuple)]
    assert turns[0][1] == turns[1][1]
    assert len(db.messages(pid)) == 4


def test_cancellation_during_build_scan_cannot_finish_successfully(client, monkeypatch):
    import threading
    from app import dsh_service

    class TestHarness:
        def __init__(self, **kwargs):
            pass

        def run(self, *args, **kwargs):
            return SimpleNamespace(final_response="", finish_reason="completed")

        def close(self):
            pass

    configure(client)
    pid = project(client)
    db, service = client.app.state.db, client.app.state.service
    task = db.create_task(pid, "取消检查")
    flag = threading.Event()
    service.cancel_flags[task["id"]] = flag

    def scan(_workspace):
        flag.set()
        return {"builds": []}

    monkeypatch.setattr(dsh_service, "DeepSeekHarness", TestHarness)
    monkeypatch.setattr(dsh_service, "scan_workspace", scan)
    service._run(task)
    assert db.task(task["id"])["status"] == "cancelled"
    assert not any(e["type"] == "completed" and e["data"]["status"] == "completed" for e in db.events(task["id"]))


def test_old_key_also_redacted_if_settings_changed(client):
    configure(client)
    service = client.app.state.service
    client.put("/api/settings", json={"api_key": "new-test-key"})
    error = service.safe_error(ValueError("old test-private-key and new new-test-key"), "test-private-key")
    assert "test-private-key" not in error and "new-test-key" not in error


def test_only_one_service_can_own_data_directory(tmp_path):
    from app.config import InstanceLock
    first, second = InstanceLock(tmp_path / "lock"), InstanceLock(tmp_path / "lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="已有 AIWEB"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_extended_unity_build_timeout_preserves_powershell_dialect(client):
    patch = client.app.state.service.patch.read_text(encoding="utf-8")
    windows_terminal = patch.split("- id: terminal-pwsh", 1)[1].split("- id:", 1)[0]
    assert "shellDialect: pwsh" in windows_terminal
    assert "timeoutMs: 1200000" in windows_terminal
