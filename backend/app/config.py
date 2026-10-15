from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
OFFICIAL_BASE = "https://api.deepseek.com"


def private_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)


def _dpapi(value: bytes, decrypt: bool = False) -> bytes:
    """Windows encrypts the key for the current Windows account, without dependencies."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise OSError("Windows 无法读取或保存模型密钥，请在设置中重新填写。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


class Settings:
    def __init__(self, data_dir: Path | None = None, dsh_home: Path | None = None):
        self.data_dir = (data_dir or Path(os.environ.get("AIWEB_DATA_DIR", BACKEND / "data"))).resolve()
        self.dsh_home = (dsh_home or Path(os.environ.get("AIWEB_DSH_HOME", BACKEND / ".dsh"))).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.dsh_home.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "settings.json"
        self.lock = threading.RLock()
        self.values = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def key(self) -> str:
        with self.lock:
            saved = self.values.get("api_key", "")
            if not saved:
                return os.environ.get("DEEPSEEK_API_KEY", "").strip()
            if saved.startswith("dpapi:"):
                return _dpapi(base64.b64decode(saved[6:]), decrypt=True).decode()
            return saved[6:] if saved.startswith("plain:") else ""

    def public(self) -> dict:
        return {
            "provider": "deepseek-official",
            "model": self.values.get("model", "deepseek-v4-flash"),
            "base_url": OFFICIAL_BASE,
            "has_api_key": bool(self.key()),
            "key_storage": "Windows 当前账户加密" if os.name == "nt" else "本机私有配置文件",
        }

    def update(self, changes: dict) -> dict:
        with self.lock:
            base = changes.get("base_url")
            if base and base.rstrip("/") not in {OFFICIAL_BASE, OFFICIAL_BASE + "/v1"}:
                raise ValueError("当前只连接 DeepSeek 官方 API。")
            model = changes.get("model")
            if model is not None:
                model = str(model).strip()
                if not model or len(model) > 120 or not all(c.isalnum() or c in "-_.:/" for c in model):
                    raise ValueError("请填写有效的模型名称。")
                self.values["model"] = model
            key = changes.get("api_key")
            if isinstance(key, str) and key.strip():
                key = key.strip()
                if len(key) > 4096 or any(c.isspace() for c in key):
                    raise ValueError("API Key 格式不正确。")
                self.values["api_key"] = (
                    "dpapi:" + base64.b64encode(_dpapi(key.encode())).decode()
                    if os.name == "nt" else "plain:" + key
                )
            private_write(self.path, self.values)
            return self.public()


def confined(root: Path, relative: str = "") -> Path:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise ValueError("文件路径超出作品目录。")
    # A junction/symlink must never let generated files escape their workspace.
    return candidate


class InstanceLock:
    """Only one local service may own a given project's DSH/SQLite state."""

    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def acquire(self):
        stream = self.path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError("已有 AIWEB 后台在使用这个数据目录，请访问已启动的页面，或先停止原来的后台。") from exc
        self.stream = stream

    def release(self):
        if self.stream:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None
