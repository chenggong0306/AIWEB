"""Start with: uv run --project backend python backend/run.py (from AIWEB)."""
import argparse
import os
import shutil

import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIWEB 本机开发服务")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    if os.name == "nt" and shutil.which("pwsh") is None:
        parser.error(
            "未找到 PowerShell 7（pwsh），DSH 无法执行 Windows 工具。"
            "请先安装 PowerShell 7，安装后新开终端并运行 pwsh --version 确认，再启动 AIWEB。"
        )
    uvicorn.run("app.main:app", host="127.0.0.1", port=arguments.port)
