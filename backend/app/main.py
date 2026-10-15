from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import ROOT, InstanceLock, Settings, confined
from .database import Database, new_id, now
from .dsh_service import AgentService, TERMINAL
from .unity import detect_environment

HIDDEN = {".git", ".venv", ".dsh", "Library", "Temp", "Logs", "obj", "node_modules", "__pycache__"}
TEXT_EXTENSIONS = {".cs", ".json", ".txt", ".md", ".yml", ".yaml", ".uxml", ".uss", ".shader",
                   ".hlsl", ".cginc", ".asmdef", ".html", ".js", ".css", ".xml", ".py", ".toml", ".meta"}
MAX_UPLOAD = 25 * 1024 * 1024


def create_app(settings: Settings | None = None, *, start_runtime: bool = True) -> FastAPI:
    settings = settings or Settings()
    db = Database(settings.data_dir / "aiweb.sqlite3")
    service = AgentService(settings, db)
    submission_locks: dict[str, asyncio.Lock] = {}

    async def submission_guard(project_id: str):
        async with submission_locks.setdefault(project_id, asyncio.Lock()):
            yield

    @asynccontextmanager
    async def lifespan(app):
        owner = InstanceLock(settings.data_dir / "service.lock")
        try:
            if start_runtime:
                owner.acquire()
                await service.start()
            yield
        finally:
            if start_runtime and owner.stream:
                await service.stop()
                owner.release()

    app = FastAPI(title="AIWEB · DSH", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings, app.state.db, app.state.service = settings, db, service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        # Generated game scripts run inside an opaque-origin iframe. They may
        # fetch build assets, but cannot read or mutate application control APIs.
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and (origin == "null" or urlsplit(origin).netloc != request.headers.get("host")):
                return JSONResponse({"detail": "请从本机 AIWEB 页面操作。"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "不接受跨站请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def require_project(project_id: str) -> dict:
        project = db.project(project_id)
        if not project:
            raise HTTPException(404, "作品不存在。")
        return project

    def require_task(task_id: str) -> dict:
        task = db.task(task_id)
        if not task:
            raise HTTPException(404, "任务不存在。")
        return task

    def project_public(project: dict) -> dict:
        return {key: value for key, value in project.items() if key != "session_id"} | {
            "latest_build": next(iter(db.builds(project["id"])), None)}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "service": "AIWEB", "framework": "DeepSeek Harness SDK",
                "runtime_ready": service.probe_result["runtime_ready"]}

    @app.get("/api/settings")
    async def get_settings():
        return settings.public()

    @app.put("/api/settings")
    async def put_settings(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("设置内容应是一个对象。")
        return settings.update(body)

    @app.get("/api/environment")
    async def environment(refresh: bool = False):
        unity = await detect_environment(force=refresh)
        config = settings.public()
        names = [item["name"] for item in service.skills["skills"]]
        warnings = list(unity["issues"])
        if not config["has_api_key"]:
            warnings.insert(0, "请在模型设置中填写 DeepSeek 官方 API Key。")
        if not service.probe_result["runtime_ready"]:
            warnings.insert(0, service.probe_result["detail"])
        return {
            "dsh": dict(service.probe_result),
            "unity": {"installed": unity["cli_available"], "version": unity["cli_version"],
                      "editors": unity["editors"], "web_module": unity["web_module_available"],
                      "connected": unity["pipeline_connected"], "licensed": unity["license_active"],
                      "detail": "；".join(unity["issues"]) or "Unity 环境已就绪", "can_build": unity["can_build"]},
            "skills": {"installed": bool(names) and not service.skills["missing"], "count": len(names),
                       "names": names, "path": service.skills["root"],
                       "detail": "由 DSH 官方 skill 插件加载"},
            "model": {"configured": config["has_api_key"], "model": config["model"]},
            "ready": service.probe_result["runtime_ready"] and config["has_api_key"] and unity["can_build"],
            "warnings": warnings,
        }

    @app.get("/api/projects")
    async def list_projects():
        return {"projects": [project_public(p) for p in db.rows("SELECT * FROM projects ORDER BY updated_at DESC")]}

    @app.post("/api/projects")
    async def new_project(request: Request):
        data = await request.json()
        title = str(data.get("title", "新游戏")).strip()[:100]
        project = db.create_project(title)
        service.workspace(project["id"])
        return project_public(project)

    @app.get("/api/projects/{project_id}")
    async def get_project(project_id: str):
        project = require_project(project_id)
        return {"project": project_public(project), "messages": db.messages(project_id),
                "tasks": db.rows("SELECT id,project_id,status,created_at,updated_at,error,build_id FROM tasks WHERE project_id=? ORDER BY created_at DESC", (project_id,)),
                "builds": db.builds(project_id)}

    @app.post("/api/projects/{project_id}/messages", dependencies=[Depends(submission_guard)])
    async def send_message(project_id: str, request: Request):
        project = require_project(project_id)
        if not settings.key():
            raise HTTPException(409, "请先点击“模型设置”，填写 DeepSeek 官方 API Key。")
        if not service.probe_result["runtime_ready"]:
            raise HTTPException(503, "DSH 运行检查未通过：" + service.probe_result["detail"])
        if db.one("SELECT id FROM tasks WHERE project_id=? AND status IN ('queued','running')", (project_id,)):
            raise HTTPException(409, "这个作品仍有任务执行中，请等待完成或先停止。")
        length = request.headers.get("content-length")
        if length and int(length) > MAX_UPLOAD + 1024 * 1024:
            raise HTTPException(413, "附件总大小不能超过 25 MB。")
        async with request.form(max_files=10, max_fields=10, max_part_size=MAX_UPLOAD) as form:
            text = str(form.get("text", "")).strip()
            uploads = form.getlist("files")
            if len(text) > 20000:
                raise HTTPException(413, "单条需求不能超过 20000 字。")
            if not text and not uploads:
                raise ValueError("请输入需求或选择附件。")
            attachments, total = [], 0
            upload_root = service.workspace(project_id) / "attachments"
            upload_root.mkdir(exist_ok=True)
            for upload in uploads:
                if not hasattr(upload, "read"):
                    continue
                original = Path(str(upload.filename or "附件").replace("\\", "/")).name[:180]
                suffix = Path(original).suffix[:15]
                stored_name = new_id() + suffix if re.fullmatch(r"\.[a-zA-Z0-9_-]{1,14}", suffix) else new_id()
                destination = confined(upload_root, stored_name)
                with destination.open("wb") as target:
                    while chunk := await upload.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_UPLOAD:
                            target.close()
                            destination.unlink(missing_ok=True)
                            raise HTTPException(413, "附件总大小不能超过 25 MB。")
                        target.write(chunk)
                attachments.append({"name": original, "path": "attachments/" + stored_name,
                                    "size": destination.stat().st_size})
        # Keep paths in the task prompt; DSH uses its own filesystem/shell tools.
        prompt = text or "请查看附件，并说明可以如何用于当前游戏。"
        if attachments:
            prompt += "\n\n用户附件（当前作品工作目录内，请按需查看）：\n" + "\n".join(
                f'{item["name"]}: {item["path"]}' for item in attachments)
        db.message(project_id, "user", text, attachments)
        if project["title"] == "新游戏" and text:
            db.execute("UPDATE projects SET title=? WHERE id=?", (text[:40], project_id))
        task = db.create_task(project_id, prompt)
        await service.submit(task["id"])
        return {"project_id": project_id, "task_id": task["id"]}

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        task = require_task(task_id)
        task.pop("prompt", None)
        return task

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        require_task(task_id)
        await service.cancel(task_id)
        return {"ok": True}

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0):
        require_task(task_id)
        try:
            after = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            pass

        async def stream():
            cursor, heartbeat = after, time.monotonic()
            while not await request.is_disconnected():
                rows = db.events(task_id, cursor)
                for row in rows:
                    cursor = row["seq"]
                    yield f'id: {cursor}\nevent: message\ndata: {json.dumps(row, ensure_ascii=False)}\n\n'
                task = db.task(task_id)
                if not task:
                    return
                if task["status"] in TERMINAL and len(rows) < 100:
                    ending = db.one("SELECT seq FROM events WHERE task_id=? AND type='completed' ORDER BY seq DESC LIMIT 1", (task_id,))
                    # Status and event use separate short transactions. Do not
                    # close in the interval before the final event is committed.
                    if ending and ending["seq"] <= cursor:
                        return
                if time.monotonic() - heartbeat >= 10:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.3)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    async def remove_project(project_id: str):
        require_project(project_id)
        running = db.rows("SELECT id FROM tasks WHERE project_id=? AND status IN ('queued','running')", (project_id,))
        for task in running:
            await service.cancel(task["id"])
        deadline = time.monotonic() + 10
        while any(db.task(t["id"])["status"] not in TERMINAL for t in running):
            if time.monotonic() > deadline:
                raise HTTPException(409, "任务正在停止，请稍后再删除记录。")
            await asyncio.sleep(0.1)
        with service.lock:
            runtime = service.runtimes.pop(project_id, None)
        if runtime:
            await asyncio.to_thread(runtime.harness.close)
        with db.connect() as connection:
            connection.execute("DELETE FROM events WHERE task_id IN (SELECT id FROM tasks WHERE project_id=?)", (project_id,))
            for table in ("messages", "tasks", "builds"):
                connection.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
        # User game source is retained on disk. Clearing chat history never
        # recursively deletes an entire Unity project without a separate action.

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str):
        await remove_project(project_id)
        return {"ok": True, "files_retained": True}

    @app.delete("/api/projects")
    async def clear_projects():
        for project in db.rows("SELECT id FROM projects"):
            await remove_project(project["id"])
        return {"ok": True, "files_retained": True}

    def source_path(project_id: str, relative: str) -> Path:
        require_project(project_id)
        if any(part in HIDDEN or part.startswith(".") for part in Path(relative).parts):
            raise HTTPException(403, "该目录不在代码查看范围内。")
        return confined(service.workspace(project_id), relative)

    @app.get("/api/projects/{project_id}/files")
    async def list_files(project_id: str, path: str = ""):
        folder = source_path(project_id, path)
        if not folder.is_dir():
            raise HTTPException(404, "目录不存在。")
        workspace = service.workspace(project_id)
        values = []
        for child in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name in HIDDEN or child.name.startswith(".") or child.is_symlink():
                continue
            if not child.resolve().is_relative_to(workspace):
                continue
            values.append({"name": child.name, "path": child.relative_to(workspace).as_posix(),
                           "type": "directory" if child.is_dir() else "file", "size": child.stat().st_size if child.is_file() else 0})
            if len(values) >= 500:
                break
        return {"path": path, "files": values}

    @app.get("/api/projects/{project_id}/file")
    async def read_file(project_id: str, path: str):
        file = source_path(project_id, path)
        if not file.is_file():
            raise HTTPException(404, "文件不存在。")
        if file.suffix.lower() not in TEXT_EXTENSIONS or file.stat().st_size > 2 * 1024 * 1024:
            raise HTTPException(415, "此文件是素材、二进制或超过 2 MB，请在本地工程中查看。")
        return {"path": path, "content": file.read_text(encoding="utf-8", errors="replace")}

    @app.post("/api/projects/{project_id}/publish")
    async def publish(project_id: str, request: Request):
        require_project(project_id)
        body = await request.json()
        builds = db.builds(project_id)
        build_id = body.get("build_id") or (builds[0]["id"] if builds else None)
        build = db.build(build_id) if build_id else None
        if not build or build["project_id"] != project_id:
            raise HTTPException(409, "请先生成一个完整的 Unity Web 构建再发布。")
        db.execute("UPDATE projects SET published_build=? WHERE id=?", (build_id, project_id))
        return {"url": f'/play/{build_id}/index.html', "build": build,
                "scope": "local", "detail": "发布到本机服务；其他电脑需要后续部署后访问。"}

    async def build_file(build_id: str, path: str, *, published: bool = False):
        build = db.build(build_id)
        if not build:
            raise HTTPException(404, "构建不存在。")
        if published and not db.one("SELECT id FROM projects WHERE published_build=?", (build_id,)):
            raise HTTPException(404, "该版本尚未发布。")
        folder = settings.data_dir / "projects" / build["project_id"] / "builds" / build_id
        file = confined(folder, path or "index.html")
        if not file.is_file() or any(part.startswith(".") for part in Path(path).parts):
            raise HTTPException(404, "构建文件不存在。")
        suffix = file.suffix.lower()
        headers = {"Access-Control-Allow-Origin": "*", "Cross-Origin-Resource-Policy": "cross-origin",
                   "Cache-Control": "public, max-age=3600",
                   # Also isolate top-level published HTML, SVG and other documents.
                   "Content-Security-Policy": "sandbox allow-scripts allow-pointer-lock allow-forms allow-downloads"}
        plain = file.name
        if suffix in {".br", ".gz"}:
            headers["Content-Encoding"] = "br" if suffix == ".br" else "gzip"
            plain = file.stem
        media = "application/wasm" if plain.endswith(".wasm") else mimetypes.guess_type(plain)[0] or "application/octet-stream"
        return FileResponse(file, media_type=media, headers=headers)

    @app.get("/builds/{build_id}/{path:path}")
    async def preview_file(build_id: str, path: str):
        return await build_file(build_id, path)

    @app.get("/play/{build_id}/{path:path}")
    async def published_file(build_id: str, path: str):
        return await build_file(build_id, path, published=True)

    @app.get("/")
    @app.get("/ai.html")
    async def frontend():
        return FileResponse(ROOT / "ai.html", media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/favicon.ico")
    async def favicon():
        return FileResponse(ROOT / "image" / "logo.png", media_type="image/png")

    @app.get("/image/{path:path}")
    async def image(path: str):
        file = confined(ROOT / "image", path)
        if not file.is_file():
            raise HTTPException(404)
        return FileResponse(file)

    @app.get("/{asset}")
    async def frontend_asset(asset: str):
        if asset not in {"ai.css", "ai.js", "client-api.js"}:
            raise HTTPException(404)
        return FileResponse(ROOT / asset, headers={"Cache-Control": "no-store"})

    return app


app = create_app()
