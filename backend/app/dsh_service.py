from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from deepseek_harness import DeepSeekHarness

from .config import OFFICIAL_BASE, Settings
from .database import Database, new_id, now
from .skill_setup import prepare_skills, unity_task_instructions
from .unity import find_unity_cli, publish_build, scan_workspace, validate_web_build

TERMINAL = {"completed", "failed", "cancelled", "blocked"}


@dataclass
class Runtime:
    harness: DeepSeekHarness
    session_id: str
    fingerprint: str


class AgentService:
    """Task queue and SDK adapter. DSH owns every model call and tool decision."""

    def __init__(self, settings: Settings, db: Database):
        self.settings, self.db = settings, db
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.runtimes: dict[str, Runtime] = {}
        self.cancel_flags: dict[str, threading.Event] = {}
        self.lock = threading.RLock()
        self.skills = prepare_skills(settings.dsh_home)
        self.probe_result = {"installed": True, "version": version("deepseek-harness-sdk"),
                             "profile": "sdk-minimal", "runtime_ready": False, "detail": "等待运行检查"}
        self.patch = settings.dsh_home / "aiweb.patch.yml"
        self.patch.write_text(
            "# Keep diagnostic logs on this computer. Model calls still use the official API.\n"
            "- id: session-log-deepseek\n  disabled: true\n"
            "- id: plugin-package-inventory-deepseek\n  disabled: true\n"
            "# A first Unity Web build can exceed the DSH shell's five-minute default.\n"
            "- id: persistent-pwsh\n  config:\n    timeoutMs: 1200000\n"
            # A DSH config overlay replaces this entire object, not only timeoutMs.
            "- id: terminal-pwsh\n  config:\n    shellDialect: pwsh\n    timeoutMs: 1200000\n"
            "- id: persistent-bash\n  config:\n    timeoutMs: 1200000\n"
            "- id: terminal-bash\n  config:\n    timeoutMs: 1200000\n", encoding="utf-8")

    def workspace(self, project_id: str) -> Path:
        path = self.settings.data_dir / "projects" / project_id / "workspace"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def patches(self) -> tuple[str, ...]:
        return (str(self.patch), self.skills["patch_path"])

    async def start(self):
        # Interrupted runs must not silently look as though they are still executing.
        for task in self.db.rows("SELECT id FROM tasks WHERE status IN ('queued','running')"):
            self.db.status(task["id"], "failed", "后台已重启，请重新发送需求继续。")
            self.db.event(task["id"], "error", {"message": "后台已重启，上一任务已停止。"})
            self.db.event(task["id"], "completed", {"status": "failed"})
        self.worker = asyncio.create_task(self._worker())
        await asyncio.to_thread(self.probe)

    def probe(self):
        harness = None
        try:
            probe_dir = self.settings.data_dir / "runtime-check"
            probe_dir.mkdir(exist_ok=True)
            # initialize performs no generation; loopback URL additionally prevents API traffic.
            harness = DeepSeekHarness(profile="sdk-minimal", cwd=str(probe_dir),
                dsh_home=str(self.settings.dsh_home), patches=self.patches(),
                api_key="", base_url="http://127.0.0.1:1", initialize_timeout_seconds=20)
            harness.start()
            self.probe_result.update(runtime_ready=True, detail="DSH SDK 与 Unity Skills 插件启动成功（未调用模型）")
        except Exception as exc:
            self.probe_result.update(runtime_ready=False, detail=self.safe_error(exc))
        finally:
            if harness:
                harness.close()

    async def stop(self):
        with self.lock:
            for flag in self.cancel_flags.values():
                flag.set()
            harnesses = [entry.harness for entry in self.runtimes.values()]
        for harness in harnesses:
            await asyncio.to_thread(harness.close)
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass

    def safe_error(self, exc: Exception, *extra_secrets: str) -> str:
        text = str(exc)
        for key in (self.settings.key(), *extra_secrets):
            if key:
                text = text.replace(key, "[已隐藏密钥]")
        if "401" in text or "Authentication" in text:
            return "DeepSeek 身份验证失败，请检查设置中的 API Key。"
        if "402" in text or "Insufficient Balance" in text:
            return "DeepSeek 账户余额不足，请检查官方账户。"
        return text[:2000] or type(exc).__name__

    async def submit(self, task_id: str):
        self.cancel_flags[task_id] = threading.Event()
        self.db.event(task_id, "status", {"stage": "queued", "label": "等待执行"})
        await self.queue.put(task_id)

    async def cancel(self, task_id: str):
        with self.lock:
            task = self.db.task(task_id)
            if not task or task["status"] in TERMINAL:
                return
            flag = self.cancel_flags.setdefault(task_id, threading.Event())
            flag.set()
            entry = self.runtimes.get(task["project_id"]) if task["status"] != "queued" else None
            if task["status"] == "queued":
                self.db.status(task_id, "cancelled")
                self.db.event(task_id, "completed", {"status": "cancelled", "message": "已取消排队"})
        if entry:
            # This SDK release has no session/cancel method. Close its owned runtime.
            await asyncio.to_thread(entry.harness.close)

    async def _worker(self):
        while True:
            task_id = await self.queue.get()
            try:
                task = self.db.task(task_id)
                if task and task["status"] == "queued":
                    await asyncio.to_thread(self._run, task)
            finally:
                self.cancel_flags.pop(task_id, None)
                self.queue.task_done()

    def _run(self, task: dict):
        task_id, project_id = task["id"], task["project_id"]
        flag = self.cancel_flags.setdefault(task_id, threading.Event())
        timeout_flag = threading.Event()
        timer = None
        entry = None
        key = ""
        try:
            with self.lock:
                if flag.is_set() or self.db.task(task_id)["status"] != "queued":
                    return
                self.db.status(task_id, "running")
            self.db.event(task_id, "status", {"stage": "running", "label": "DSH 正在处理需求"})
            key = self.settings.key()
            if not key:
                raise ValueError("请先在模型设置中填写 DeepSeek 官方 API Key。")
            if not self.probe_result["runtime_ready"]:
                raise RuntimeError(self.probe_result["detail"])
            config = self.settings.public()
            fingerprint = hashlib.sha256((config["model"] + key).encode()).hexdigest()
            with self.lock:
                entry = self.runtimes.get(project_id)
            if entry and entry.fingerprint != fingerprint:
                entry.harness.close()
                with self.lock:
                    self.runtimes.pop(project_id, None)
                entry = None
            new_session = entry is None
            if new_session:
                workspace = self.workspace(project_id)
                cli = find_unity_cli()
                env = {"DSH_SYSTEM_PROMPT": (
                    "You are the game development assistant inside AIWEB. Reply in Chinese. "
                    + unity_task_instructions(workspace, self.skills["catalog_path"])
                    + " Read the matching SKILL.md before acting. Do not access other projects, "
                      "server configuration, API keys or DSH runtime files. Only edit this workspace. "
                      "Never inspect or print process CommandLine fields or environment variables: "
                      "Unity can put temporary access tokens in launch arguments. For process checks "
                      "use IDs, executable paths and the current project's own logs only. "
                      "When using Start-Process for helpers use -WindowStyle Hidden. "
                      "Use a single-threaded Unity Web build without compression for browser preview. "
                      "Be precise about errors and never claim an unbuilt game is playable. "
                      "Do not start background build processes: wait for the actual build result."
                )}
                if cli:
                    env["PATH"] = str(Path(cli).parent) + os.pathsep + os.environ.get("PATH", "")
                harness = DeepSeekHarness(profile="sdk-minimal", cwd=str(workspace),
                    dsh_home=str(self.settings.dsh_home), patches=self.patches(), env=env,
                    model=config["model"], api_key=key, base_url=OFFICIAL_BASE,
                    initialize_timeout_seconds=30, request_timeout_seconds=120)
                entry = Runtime(harness, "aiweb-" + new_id(), fingerprint)
                with self.lock:
                    self.runtimes[project_id] = entry
                old_session = self.db.project(project_id).get("session_id")
                self.db.execute("UPDATE projects SET session_id=? WHERE id=?", (entry.session_id, project_id))
                if old_session:
                    self.db.event(task_id, "status", {"stage": "running", "label": "已根据作品文件和最近对话重建上下文",
                        "detail": "当前 DSH 版本不支持跨进程恢复会话；作品文件和网页记录仍保留。"})

            def expire():
                with self.lock:
                    if self.db.task(task_id)["status"] in TERMINAL:
                        return
                    timeout_flag.set()
                entry.harness.close()

            timer = threading.Timer(1800, expire)
            timer.daemon = True
            timer.start()
            if flag.is_set():
                raise RuntimeError("任务已取消。")
            prompt = task["prompt"]
            if new_session:
                # A bounded transcript is context for a new official session, not a custom agent loop.
                history = self.db.messages(project_id)[:-1][-10:]
                if history:
                    context = "\n\n".join(f'{m["role"]}: {m["content"][:3500]}' for m in history)
                    prompt = "以下是网页保留的最近对话，供新会话继续处理，先检查现有工程文件：\n" + context + "\n\n当前需求：\n" + prompt
            seen: list[str] = []

            def notification(item):
                if flag.is_set():
                    raise RuntimeError("任务已取消。")
                if item.method != "session.event" or item.payload.get("sessionId") != entry.session_id:
                    return
                event = item.payload.get("event", {})
                data = event.get("data", {})
                kind = event.get("type")
                if kind == "assistant/message":
                    message = data.get("message", data)
                    text = "".join(str(block.get("text", "")) for block in message.get("content", [])
                                   if block.get("type") == "text")
                    if text.strip():
                        # Only committed assistant text reaches the web UI, never raw request logs.
                        text = text.replace(key, "[已隐藏密钥]")
                        seen.append(text)
                        self.db.message(project_id, "assistant", text)
                        self.db.event(task_id, "assistant", {"text": text})
                elif kind == "tool/call":
                    self.db.event(task_id, "tool", {"name": data.get("name", "工具"), "phase": "started"})
                elif kind == "tool/result":
                    error = bool(data.get("error")) or any(
                        block.get("isError") for block in data.get("message", {}).get("content", [])
                        if block.get("type") == "tool-result")
                    self.db.event(task_id, "tool", {"name": "工具", "phase": "failed" if error else "completed"})

            result = entry.harness.run(prompt, session_id=entry.session_id, on_notification=notification)
            if flag.is_set():
                raise RuntimeError("任务已取消。")
            if timeout_flag.is_set():
                raise TimeoutError("任务超过 30 分钟，已停止。请缩小需求后重试。")
            if result.finish_reason != "completed":
                raise RuntimeError(f"DSH 本轮未正常完成（{result.finish_reason or '无结束状态'}），请检查模型配置或缩小任务重试。")
            final = result.final_response.replace(key, "[已隐藏密钥]")
            if final.strip() and final not in seen:
                self.db.message(project_id, "assistant", final)
                self.db.event(task_id, "assistant", {"text": final})
            self.db.event(task_id, "status", {"stage": "running", "label": "检查 Unity Web 构建结果"})
            scanned = scan_workspace(self.workspace(project_id))
            build = None
            new_build = None
            if flag.is_set() or timeout_flag.is_set():
                raise RuntimeError("任务已停止。")
            if scanned["builds"]:
                candidate = scanned["builds"][0]
                latest = self.db.builds(project_id)
                previous = None
                if latest:
                    previous = validate_web_build(self.settings.data_dir / "projects" / project_id / "builds" / latest[0]["id"])
                if not previous or previous["fingerprint"] != candidate["fingerprint"]:
                    build_id = new_id()
                    published = publish_build(candidate["path"],
                        self.settings.data_dir / "projects" / project_id / "builds", build_id)
                    new_build = (build_id, project_id, task_id, now(), published["index"], candidate["path"])
                elif latest:
                    build = latest[0]
            # Cancellation and the final commit share a lock; cancelled builds
            # can never be registered as a successful playable version.
            with self.lock:
                if flag.is_set() or timeout_flag.is_set():
                    raise RuntimeError("任务已停止。")
                if new_build:
                    self.db.execute("INSERT INTO builds VALUES(?,?,?,?,?,?)", new_build)
                    build = self.db.build(new_build[0])
                self.db.status(task_id, "completed", build_id=build["id"] if build else None)
                self.db.event(task_id, "completed", {"status": "completed", "build": build,
                    "message": "DSH 本轮已完成，发现 Unity Web 构建。" if build else "DSH 本轮已结束；尚未生成可预览的 Unity Web 构建。"})
        except Exception as exc:
            cancelled = flag.is_set()
            detail = "任务已取消；下次发送将根据现有文件和最近对话重建上下文。" if cancelled else self.safe_error(exc, key)
            if timeout_flag.is_set():
                detail = "任务超过 30 分钟，已停止。请缩小需求后重试。"
            self.db.status(task_id, "cancelled" if cancelled else "failed", detail)
            self.db.event(task_id, "error", {"message": detail})
            self.db.event(task_id, "completed", {"status": "cancelled" if cancelled else "failed", "message": detail})
            if entry:
                entry.harness.close()
            with self.lock:
                self.runtimes.pop(project_id, None)
        finally:
            if timer:
                timer.cancel()
