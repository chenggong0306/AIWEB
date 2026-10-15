"""Install reviewed Unity skill bundles into DSH without changing DSH code."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

SKILL_NAMES = (
    "unity-cli", "unity-package-management", "ui", "ui-uitk", "ui-ugui",
    "initialize-ai-navigation", "urp-postprocessing", "optimize-web",
)
_BACKEND = Path(__file__).resolve().parents[1]
_PACKAGED_SKILLS = _BACKEND / "resources" / "skills"


def prepare_skills(
    dsh_home: str | Path,
    source_root: str | Path | None = None,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """Copy bundles including references; preserve existing local edits.

    The official DSH filesystem provider discovers $DSH_HOME/skills/<name>/SKILL.md.
    sdk-minimal does not mount it by default; the generated patch adds the real
    DSH skill registry/provider/tool. Caller must test initialization with the
    patch, and must not silently claim skill loading if startup fails.
    """
    home = Path(dsh_home).resolve()
    destination = home / "skills"
    source = Path(source_root).resolve() if source_root else _PACKAGED_SKILLS
    if not source.is_dir() and source_root is None:
        source = Path.home() / ".agents" / "skills"
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    available: list[dict[str, str]] = []
    missing: list[str] = []
    for name in SKILL_NAMES:
        target = destination / name
        entry = target / "SKILL.md"
        if entry.is_file() and not refresh:
            available.append({"name": name, "path": str(entry)})
            continue
        bundle = source / name
        if not (bundle / "SKILL.md").is_file():
            missing.append(name)
            continue
        shutil.copytree(bundle, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        copied.append(name)
        available.append({"name": name, "path": str(entry)})
    # The complete SDK profile already mounts these ids. This overlay is for
    # sdk-minimal only: add real DSH plugins, not an application agent loop.
    patch = home / "unity-skills.patch.yml"
    patch_text = """# Applies to sdk-minimal only. No DSH source changes or npm install.
- insert:
    - id: unity-skill-registry
      name: '@deepseek-ai/dsh-skill'
    - id: unity-skill-filesystem
      name: '@deepseek-ai/dsh-skill-filesystem'
      config:
        includeDefaultRoots: false
        customSkillDirs:
          - SKILL_DIRECTORY
        watch: false
    - id: unity-skill-tool
      name: '@deepseek-ai/dsh-tool-skill'
""".replace("SKILL_DIRECTORY", json.dumps(str(destination), ensure_ascii=False))
    patch.write_text(patch_text, encoding="utf-8")
    catalog_path = home / "unity-skills.md"
    catalog_lines = [
        "# Unity skills installed for this DSH instance", "",
        "Use the DSH skill tool to load the matching skill before acting.",
        "If this runtime cannot mount its skill plugins, read the actual SKILL.md",
        "with the DSH shell and resolve its references relative to that file.",
        "Do not invent a replacement agent loop or pretend a skill was loaded.", "",
    ]
    catalog_lines.extend(f"- {item['name']}: {item['path']}" for item in available)
    catalog_lines.extend([
        "", "## Project workflow", "",
        "Keep all game source changes inside the current workspace.",
        "Use Unity CLI to create/open the project and operate the Editor.",
        "Read unity-cli and the relevant references before running commands.",
        "Before scene/asset edits inspect unity status; discover available commands.",
        "If an Editor is running but unreachable, inspect Pipeline and Safe Mode.",
        "Do not hand-edit scene/prefab YAML while a live Editor is reachable.",
        "Build a real Unity Web export under workspace/output/web/index.html.",
        "Disable Web build compression for the initial preview, or enable Unity",
        "decompression fallback; do not require unconfigured response encodings.",
        "Never substitute a handwritten HTML mockup for a Unity build.",
        "Do not install an Editor, activate a license, purchase assets or publish",
        "externally without explicit authorization for that action.",
        "If license/editor/module requirements are missing, report the exact blocker.",
        "Report the Unity project path, build path, performed checks and failures.", "",
    ])
    catalog_path.write_text("\n".join(catalog_lines), encoding="utf-8")
    return {
        "root": str(destination), "source": str(source), "copied": copied,
        "skills": available, "missing": missing, "patch_path": str(patch),
        "catalog_path": str(catalog_path),
        "discovery": "$DSH_HOME/skills/<skill-name>/SKILL.md",
        "minimal_requires_patch": True,
    }


def unity_task_instructions(workspace: str | Path, skill_catalog: str | Path) -> str:
    """Small invocation context pointing DSH to the original skill instructions."""
    root = Path(workspace).resolve()
    return (
        "You are building a Unity game for this project. "
        f"The permitted game workspace is {root}. "
        f"First read the installed skill catalog at {Path(skill_catalog).resolve()}, "
        "then load unity-cli and whichever Unity skills the task requires. "
        "Use DSH's existing tools and agent loop. Export a complete Unity Web build "
        f"to {root / 'output' / 'web'}; only a successful Unity export is a playable result. "
        "Keep the previous working game intact if compilation or building fails."
    )
