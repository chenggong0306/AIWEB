"""Read-only Unity diagnostics and structurally validated Web build discovery.

No Editor is installed, activated, started or modified by this module. Actual
game creation remains the responsibility of DSH and the Unity CLI skills.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CACHE: tuple[float, dict[str, Any]] | None = None
_CACHE_LOCK: asyncio.Lock | None = None
_SKIP_DIRS = {
    ".git", ".venv", ".dsh", "node_modules", "Library", "Temp", "Logs",
    "obj", "UserSettings", "__pycache__", ".vs", ".idea",
}


def find_unity_cli() -> str | None:
    """Find the CLI even when this service predates a Windows PATH update."""
    configured = os.environ.get("UNITY_CLI_PATH")
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    found = shutil.which("unity")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Unity" / "bin" / "unity.exe"
        if candidate.is_file():
            return str(candidate)
    return None


async def _command(cli: str, *args: str, timeout: float = 20) -> dict[str, Any]:
    env = os.environ.copy()
    env.update({"UNITY_NO_UPDATE_CHECK": "1", "UNITY_NO_CONSENT_PROMPT": "1"})
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = await asyncio.create_subprocess_exec(
            cli, *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, **options,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return {"ok": False, "error": "Unity 检测超时", "command": list(args)}
        output = stdout.decode("utf-8", errors="replace").strip()
        try:
            document = json.loads(output)
        except (ValueError, TypeError):
            document = None
        # Some CLI releases report success=true despite a non-zero license
        # status exit. Preserve both values and inspect data.active explicitly.
        return {
            "ok": proc.returncode == 0 and (
                not isinstance(document, dict) or document.get("success") is not False
            ),
            "exit_code": proc.returncode,
            "data": document.get("data") if isinstance(document, dict) else None,
            "errors": document.get("errors", []) if isinstance(document, dict) else [],
            "output": output[:3000] if document is None else "",
            "error": stderr.decode("utf-8", errors="replace").strip()[:1500],
        }
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "command": list(args)}


async def detect_environment(force: bool = False) -> dict[str, Any]:
    """Return CLI, Editor, Web module, license and Pipeline facts; cache 60s."""
    global _CACHE, _CACHE_LOCK
    if _CACHE is not None and not force and time.monotonic() - _CACHE[0] < 60:
        return copy.deepcopy(_CACHE[1])
    if _CACHE_LOCK is None:
        _CACHE_LOCK = asyncio.Lock()
    async with _CACHE_LOCK:
        if _CACHE is not None and not force and time.monotonic() - _CACHE[0] < 60:
            return copy.deepcopy(_CACHE[1])
        cli = find_unity_cli()
        result: dict[str, Any] = {
            "cli_available": cli is not None, "cli_path": cli,
            "cli_version": None, "editors": [], "web_module_available": False,
            "license_active": None, "signed_in": False, "pipeline_instances": [],
            "pipeline_connected": False, "can_build": False, "can_edit_live": False,
            "issues": [], "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if not cli:
            result["issues"].append("未找到 Unity CLI；请安装或设置 UNITY_CLI_PATH。")
        else:
            version, editors, license_info, pipeline = await asyncio.gather(
                _command(cli, "--version"),
                _command(cli, "editors", "--installed", "--format", "json", "--non-interactive"),
                # A cold license service can take longer than ordinary CLI
                # discovery, particularly just after signing in to Unity Hub.
                _command(cli, "license", "status", "--format", "json", "--non-interactive", timeout=50),
                _command(cli, "pipeline", "list", "--format", "json", "--non-interactive"),
            )
            result["cli_version"] = version.get("output") or None
            rows = editors.get("data")
            result["editors"] = rows if isinstance(rows, list) else []
            license_data = license_info.get("data")
            if license_info.get("ok") and isinstance(license_data, dict):
                # Missing, malformed or unsuccessful results are unknown, not
                # proof that an otherwise activated Editor has no license.
                active = license_data.get("active")
                if isinstance(active, bool):
                    result["license_active"] = active
                result["signed_in"] = license_data.get("signedIn") is True
            pipeline_data = pipeline.get("data") or {}
            result["pipeline_instances"] = pipeline_data.get("instances", [])
            summary = pipeline_data.get("summary") or {}
            result["pipeline_connected"] = bool(summary.get("reachableServers", 0))
            # Check actual files rather than trusting localized module labels.
            for editor in result["editors"]:
                location = Path(editor.get("location", ""))
                editor_dir = location.parent if location.suffix.lower() == ".exe" else location
                candidates = [
                    editor_dir / "Data" / "PlaybackEngines" / "WebGLSupport",
                    editor_dir / "Editor" / "Data" / "PlaybackEngines" / "WebGLSupport",
                    editor_dir / "Contents" / "PlaybackEngines" / "WebGLSupport",
                ]
                editor["web_module_available"] = any(path.is_dir() for path in candidates)
            result["web_module_available"] = any(
                row.get("web_module_available") for row in result["editors"]
            )
            for label, check in (("Editor", editors), ("许可证", license_info), ("Pipeline", pipeline)):
                if not check.get("ok") and check.get("errors"):
                    result["issues"].append(f"{label}检测：{check['errors'][0].get('message', '失败')}")
                elif not check.get("ok") and check.get("error"):
                    result["issues"].append(f"{label}检测：{check['error']}")
            if not result["editors"]:
                result["issues"].append("尚未检测到已安装的 Unity Editor。")
            if result["editors"] and not result["web_module_available"]:
                result["issues"].append("Unity Editor 缺少 Web Build Support 模块。")
            if result["license_active"] is None:
                result["issues"].append("Unity 许可证状态尚未确认；检测失败或未返回有效状态，请稍后刷新。")
            elif result["license_active"] is False:
                result["issues"].append("Unity 尚无有效许可证，请先在 Unity Hub 或 CLI 完成激活。")
            if not result["pipeline_connected"]:
                result["issues"].append("尚无连接 Pipeline 的 Unity Editor；实时编辑前需打开作品工程并安装 Pipeline。")
            result["can_build"] = bool(
                result["editors"] and result["web_module_available"] and result["license_active"] is True
            )
            result["can_edit_live"] = result["can_build"] and result["pipeline_connected"]
        _CACHE = (time.monotonic(), result)
        return copy.deepcopy(result)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _walk(root: Path, max_dirs: int = 6000):
    count = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        folder = Path(current)
        dirs[:] = [
            name for name in dirs
            if name not in _SKIP_DIRS and not (folder / name).is_symlink()
            and _within(folder / name, root)
        ]
        count += 1
        if count > max_dirs:
            break
        yield folder, files


def validate_web_build(path: str | Path) -> dict[str, Any] | None:
    """Accept complete Unity Web exports, not arbitrary HTML previews.

    This checks structure, loader identity, references and uncompressed WASM
    magic. It does not claim successful gameplay or replace a browser test.
    """
    root = Path(path).resolve()
    index = root / "index.html"
    if not index.is_file() or index.is_symlink():
        return None
    try:
        # Published output is copied and served later. Do not let a junction or
        # symlink smuggle arbitrary project/host files into that static tree,
        # even if it lives in a directory the discovery walker would skip.
        for current, directories, names in os.walk(root, followlinks=False):
            folder = Path(current)
            for name in directories + names:
                item = folder / name
                if item.is_symlink() or not _within(item, root):
                    return None
        html = index.read_text(encoding="utf-8", errors="replace")
        if "createUnityInstance" not in html:
            return None
        loaders: list[Path] = []
        wasm: list[Path] = []
        data: list[Path] = []
        frameworks: list[Path] = []
        all_files: list[Path] = []
        for directory, names in _walk(root, max_dirs=500):
            for name in names:
                file = directory / name
                if file.is_symlink() or not _within(file, root):
                    return None
                all_files.append(file)
                lower = name.lower()
                if lower.endswith(".loader.js"):
                    loaders.append(file)
                elif re.search(r"\.wasm(?:\.(?:br|gz|unityweb))?$", lower):
                    wasm.append(file)
                elif re.search(r"\.data(?:\.(?:br|gz|unityweb))?$", lower):
                    data.append(file)
                elif re.search(r"\.framework\.js(?:\.(?:br|gz|unityweb))?$", lower):
                    frameworks.append(file)
        if not loaders or not wasm or not data or not frameworks:
            return None
        if not any("createUnityInstance" in f.read_text(encoding="utf-8", errors="replace") for f in loaders):
            return None
        if any(file.stat().st_size == 0 for file in loaders + wasm + data + frameworks):
            return None
        if not any(file.name in html for file in loaders):
            return None
        for file in wasm:
            if file.name.lower().endswith(".wasm"):
                with file.open("rb") as stream:
                    if stream.read(4) != b"\x00asm":
                        return None
        total = sum(file.stat().st_size for file in all_files)
        # Hash streamed file contents so changes do not depend on timestamp
        # granularity; builds are only scanned after an agent task completes.
        digest = hashlib.sha256()
        for file in sorted(all_files):
            digest.update(file.relative_to(root).as_posix().encode("utf-8"))
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        return {
            "path": str(root), "index": "index.html", "size_bytes": total,
            "file_count": len(all_files), "fingerprint": digest.hexdigest(),
            "modified_at": max(file.stat().st_mtime for file in all_files),
            "compressed": any(file.suffix.lower() in {".br", ".gz", ".unityweb"} for file in wasm + data),
            "assets": {
                "loaders": [f.relative_to(root).as_posix() for f in loaders],
                "wasm": [f.relative_to(root).as_posix() for f in wasm],
                "data": [f.relative_to(root).as_posix() for f in data],
                "frameworks": [f.relative_to(root).as_posix() for f in frameworks],
            },
            "validation": "structure_only",
        }
    except (OSError, ValueError):
        return None


def scan_workspace(workspace: str | Path) -> dict[str, Any]:
    """Discover Unity source projects and complete Web export candidates."""
    root = Path(workspace).resolve()
    projects: list[dict[str, Any]] = []
    builds: list[dict[str, Any]] = []
    if not root.is_dir():
        return {"workspace": str(root), "projects": projects, "builds": builds}
    for folder, files in _walk(root):
        version_file = folder / "ProjectSettings" / "ProjectVersion.txt"
        if (folder / "Assets").is_dir() and (folder / "Packages").is_dir() and version_file.is_file():
            version_text = version_file.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"^m_EditorVersion:\s*(.+)$", version_text, re.MULTILINE)
            manifest = folder / "Packages" / "manifest.json"
            dependencies: dict[str, Any] = {}
            try:
                dependencies = json.loads(manifest.read_text(encoding="utf-8")).get("dependencies", {})
            except (OSError, ValueError, AttributeError):
                pass
            projects.append({
                "path": str(folder), "relative_path": folder.relative_to(root).as_posix(),
                "editor_version": match.group(1).strip() if match else None,
                "pipeline_installed": "com.unity.pipeline" in dependencies,
                "packages": dependencies,
            })
        if "index.html" in files:
            build = validate_web_build(folder)
            if build:
                build["relative_path"] = folder.relative_to(root).as_posix()
                builds.append(build)
    builds.sort(key=lambda item: item["modified_at"], reverse=True)
    return {"workspace": str(root), "projects": projects, "builds": builds}


def publish_build(source: str | Path, builds_root: str | Path, build_id: str) -> dict[str, Any]:
    """Copy a validated export into an immutable version directory."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", build_id):
        raise ValueError("Invalid build id")
    validated = validate_web_build(source)
    if validated is None:
        raise ValueError("没有发现完整的 Unity Web 构建，不能作为可玩版本发布。")
    destination_root = Path(builds_root).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / build_id
    if destination.exists():
        raise FileExistsError("构建版本已存在，禁止覆盖。")
    if not _within(destination, destination_root):
        raise ValueError("Build destination escapes builds root")
    shutil.copytree(
        validated["path"], destination,
        ignore=shutil.ignore_patterns(*_SKIP_DIRS),
    )
    result = validate_web_build(destination)
    if result is None or result["fingerprint"] != validated["fingerprint"]:
        # Keep an incomplete copy for diagnosis, but never return a preview URL.
        raise ValueError("复制构建时文件发生变化，请等待构建结束后重试。")
    result["build_id"] = build_id
    return result
