"""HTTP boundary for project authorization. Ownership is never inferred from a name."""
from fastapi import HTTPException, Request
from .auth import require_session
from .projects import current_project, project_store, DEFAULT_PROJECT


def authorize(request: Request):
    path = request.url.path
    if not path.startswith("/api/v1/") or path.startswith(("/api/v1/auth/", "/api/v1/health", "/api/v1/catalog")):
        return None
    user = require_session(request, csrf=request.method not in {"GET", "HEAD", "OPTIONS"})
    if path == "/api/v1/projects":
        return None
    project = request.headers.get("X-Project-ID") or request.query_params.get("project_id") or DEFAULT_PROJECT
    # Image/video tags cannot set custom headers. Resolve their immutable owner,
    # then check membership rather than trusting the browser's active project.
    parts = path.split("/")
    kind_map = {"jobs": "job", "streams": "stream", "hazard-groups": "group", "hazards": "hazard", "video-sources": "source", "stream-sessions": "session"}
    resource = None
    if len(parts) > 4 and parts[3] in kind_map and parts[4] not in {"upload", "camera", "start"}:
        resource = (kind_map[parts[3]], parts[4])
    if len(parts) > 5 and parts[3] == "hikvision":
        resource = ("profile" if parts[4] == "nvr-profiles" else "source", parts[5])
    if len(parts) > 4 and parts[3] == "media":
        resource = ("ticket", parts[4])
    if len(parts) > 5 and parts[3:5] == ["monitor", "playback-tickets"]:
        resource = ("ticket", parts[5])
    if resource and ("/artifact/" in path or parts[3] == "media" or
                     (resource[0] == "ticket" and request.method == "DELETE")):
        project = project_store.owner(*resource)
        if project is None:
            raise HTTPException(404, "资源不存在")
    if not project_store.member(project, user):
        raise HTTPException(403, "当前账号无权访问此项目")
    if resource and project_store.owner(*resource) != project:
        raise HTTPException(404, "当前项目中不存在该资源")
    if path.startswith(("/api/v1/hikvision/nvr-profiles", "/api/v1/video-sources")) and request.method != "GET" and user["role"] != "system_admin":
        raise HTTPException(403, "只有系统管理员可以配置设备")
    return current_project.set(project)
