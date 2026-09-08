from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.auth import AuthStore
from app.main import app
from app.projects import current_project, DEFAULT_PROJECT
from app.workflow_store import WorkflowStore


def test_project_isolation_rename_membership_and_images(tmp_path, isolated_projects):
    store = isolated_projects
    other = store.save("第二项目")
    auth = AuthStore(tmp_path / "auth.sqlite3")
    user, initial = auth.create_user(name="安全员", email="member@example.com", role="safety_officer", work_area="A1")
    auth.change_password(user["id"], initial, "Test-Password-2026")
    store.set_member(DEFAULT_PROJECT, user["id"], True)
    workflow = WorkflowStore(tmp_path / "workflow.sqlite3")
    finding = {"label_id": "H003", "name": "未佩戴安全帽", "final_status": "confirmed_hazard", "severity": "general", "evidence": ["头部可见"], "source_frame_ids": ["f1"]}
    store.bind("job", "own-job")
    own = workflow.create_from_findings(job_id="own-job", work_area="A1", findings=[finding])[0]
    token = current_project.set(other["id"])
    store.bind("job", "foreign-job")
    foreign = workflow.create_from_findings(job_id="foreign-job", work_area="A1", findings=[finding])[0]
    current_project.reset(token)
    with patch("app.auth.auth_store", auth), patch("app.main.auth_store", auth), patch("app.main.workflow_store", workflow), TestClient(app, client=("127.0.0.1", 50001)) as client:
        assert client.get("/api/v1/jobs").status_code == 401
        login = client.post("/api/v1/auth/login", json={"email": "member@example.com", "password": "Test-Password-2026"})
        csrf = login.json()["csrf_token"]
        assert [g["id"] for g in client.get("/api/v1/hazard-groups").json()] == [own["group_id"]]
        assert client.get(f"/api/v1/hazard-groups/{foreign['group_id']}").status_code == 404
        assert client.get("/api/v1/jobs/foreign-job/artifact/frames/f1.jpg").status_code == 403
        assert client.get("/api/v1/hazard-groups", headers={"X-Project-ID": other["id"]}).status_code == 403
        assert client.post("/api/v1/projects", json={"name": "unauthorized"}, headers={"X-CSRF-Token": csrf}).status_code == 403
        store.save("示例建设项目（测试改名）", DEFAULT_PROJECT)
        assert store.owner("job", "own-job") == DEFAULT_PROJECT
        assert len(client.get("/api/v1/notifications").json()["items"]) == 1
        store.set_member(other["id"], user["id"], True)
        groups = client.get("/api/v1/hazard-groups", headers={"X-Project-ID": other["id"]}).json()
        assert [g["id"] for g in groups] == [foreign["group_id"]]
        store.set_member(other["id"], user["id"], False)
        assert client.get("/api/v1/hazard-groups", headers={"X-Project-ID": other["id"]}).status_code == 403


def test_worker_results_keep_submitted_project(tmp_path, isolated_projects):
    store = isolated_projects
    other = store.save("项目二")
    store.bind("job", "in-flight", other["id"])
    workflow = WorkflowStore(tmp_path / "workflow.sqlite3")
    result = workflow.create_from_findings(job_id="in-flight", work_area="A1", findings=[{"label_id": "H003", "final_status": "confirmed_hazard", "evidence": ["test"]}])[0]
    assert store.owner("hazard", result["id"]) == other["id"]
    assert store.owner("group", result["group_id"]) == other["id"]
    assert store.filter("hazard", [result]) == []
