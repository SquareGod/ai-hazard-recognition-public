"""Keep project ownership tests out of the operator's live databases."""
import sys
import pytest


@pytest.fixture(autouse=True)
def isolated_projects(tmp_path, monkeypatch, request):
    from app.projects import ProjectStore, current_project
    import app.main  # load modules before replacing shared store references
    monkeypatch.setattr("app.main.send_email", lambda **kwargs: [])
    monkeypatch.setattr("app.notifications.send_email", lambda **kwargs: [])
    store = ProjectStore(tmp_path)
    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and hasattr(module, "project_store"):
            monkeypatch.setattr(module, "project_store", store)
    token = current_project.set("demo-construction-project")
    # Existing API unit tests exercise endpoint delegation, using an authenticated
    # synthetic admin. Actual cookies, CSRF and cross-project denial are tested
    # without this fixture branch in test_projects and test_auth_and_groups.
    legacy = {"test_api", "test_stream_api", "test_monitoring_api", "test_media_proxy", "test_workflow_backend"}
    if request.module.__name__ in legacy:
        from app.auth import AuthStore
        from starlette.testclient import TestClient
        auth = AuthStore(tmp_path / "auth.sqlite3")
        user, initial = auth.create_user(name="Test", email="test@example.com", role="system_admin", work_area="全部工区")
        auth.change_password(user["id"], initial, "Only-For-Tests-2026")
        _, session, csrf = auth.authenticate("test@example.com", "Only-For-Tests-2026")
        monkeypatch.setattr("app.auth.auth_store", auth)
        monkeypatch.setattr("app.main.auth_store", auth)
        original = TestClient.__init__
        def authenticated(self, *args, **kwargs):
            kwargs.setdefault("cookies", {"zzy_session": session})
            kwargs.setdefault("headers", {"X-CSRF-Token": csrf})
            original(self, *args, **kwargs)
        monkeypatch.setattr(TestClient, "__init__", authenticated)
        for i in range(1, 7):
            store.bind("source", f"cam-{i}")
        store.bind("job", "job-test")
        if request.module.__name__ == "test_media_proxy":
            store.bind("ticket", "ticket")
    yield store
    current_project.reset(token)
