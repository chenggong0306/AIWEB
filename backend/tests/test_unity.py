"""Unity discovery/publishing regression tests; no Editor or model is run."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import unity
from app.skill_setup import SKILL_NAMES, prepare_skills
from app.unity import publish_build, scan_workspace, validate_web_build


class UnityLicenseDetectionTests(unittest.IsolatedAsyncioTestCase):
    """License service failures must not appear as a negative license check."""

    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiweb-license-test-")
        self.addCleanup(self.temporary.cleanup)
        self.editor_dir = Path(self.temporary.name) / "Editor"
        (self.editor_dir / "Data" / "PlaybackEngines" / "WebGLSupport").mkdir(parents=True)
        self.license_result = {"ok": True, "data": {"active": True, "signedIn": True}}
        self.cli_patch = patch.object(unity, "find_unity_cli", return_value="fixture-unity")
        self.cli_patch.start()
        self.addCleanup(self.cli_patch.stop)
        self.cache_patch = patch.object(unity, "_CACHE", None)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.lock_patch = patch.object(unity, "_CACHE_LOCK", None)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)

        async def command(_cli, *args, **kwargs):
            if args[:2] == ("license", "status"):
                return self.license_result
            if args[0] == "editors":
                return {"ok": True, "data": [{
                    "version": "fixture", "location": str(self.editor_dir / "Unity.exe"),
                }]}
            if args[0] == "pipeline":
                return {"ok": True, "data": {"instances": [], "summary": {"reachableServers": 0}}}
            return {"ok": True, "output": "fixture-cli"}

        self.command_mock = AsyncMock(side_effect=command)
        self.command_patch = patch.object(unity, "_command", self.command_mock)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    async def test_active_license_allows_build_and_gets_a_cold_start_timeout(self) -> None:
        result = await unity.detect_environment(force=True)
        self.assertIs(result["license_active"], True)
        self.assertIs(result["signed_in"], True)
        self.assertIs(result["can_build"], True)
        license_call = next(
            call for call in self.command_mock.call_args_list
            if call.args[1:3] == ("license", "status")
        )
        self.assertGreater(license_call.kwargs["timeout"], 20)
        self.assertLessEqual(license_call.kwargs["timeout"], 60)

    async def test_timeout_is_unknown_and_does_not_claim_license_is_missing(self) -> None:
        self.license_result = {"ok": False, "error": "Unity 检测超时"}
        result = await unity.detect_environment(force=True)
        self.assertIsNone(result["license_active"])
        self.assertIs(result["can_build"], False)
        self.assertTrue(any("许可证状态尚未确认" in issue for issue in result["issues"]))
        self.assertFalse(any("尚无有效许可证" in issue for issue in result["issues"]))

    async def test_failed_response_with_inactive_data_is_still_unknown(self) -> None:
        self.license_result = {"ok": False, "data": {"active": False}, "error": "service unavailable"}
        result = await unity.detect_environment(force=True)
        self.assertIsNone(result["license_active"])
        self.assertIs(result["can_build"], False)

    async def test_missing_or_malformed_license_state_is_unknown(self) -> None:
        for data in ({}, {"active": "false"}, [], None):
            with self.subTest(data=data):
                self.license_result = {"ok": True, "data": data}
                result = await unity.detect_environment(force=True)
                self.assertIsNone(result["license_active"])
                self.assertIs(result["can_build"], False)

    async def test_confirmed_inactive_license_is_false(self) -> None:
        self.license_result = {"ok": True, "data": {"active": False, "signedIn": True}}
        result = await unity.detect_environment(force=True)
        self.assertIs(result["license_active"], False)
        self.assertIs(result["can_build"], False)
        self.assertTrue(any("尚无有效许可证" in issue for issue in result["issues"]))


class UnityOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="aiweb-unity-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def make_build(self) -> Path:
        build = self.workspace / "output" / "web"
        (build / "Build").mkdir(parents=True)
        (build / "index.html").write_text(
            '<script>const loaderUrl="Build/game.loader.js";createUnityInstance();</script>',
            encoding="utf-8",
        )
        (build / "Build" / "game.loader.js").write_text(
            "function createUnityInstance(){}", encoding="utf-8"
        )
        (build / "Build" / "game.framework.js").write_text("var Module = {};", encoding="utf-8")
        (build / "Build" / "game.data").write_bytes(b"UnityWebData1.0\x00fixture")
        (build / "Build" / "game.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
        return build

    def test_html_and_incomplete_build_are_not_playable(self) -> None:
        (self.workspace / "index.html").write_text("<h1>Game mockup</h1>", encoding="utf-8")
        self.assertIsNone(validate_web_build(self.workspace))
        build = self.make_build()
        (build / "Build" / "game.data").unlink()
        self.assertIsNone(validate_web_build(build))

    def test_wasm_header_is_required_for_uncompressed_output(self) -> None:
        build = self.make_build()
        (build / "Build" / "game.wasm").write_bytes(b"not-webassembly")
        self.assertIsNone(validate_web_build(build))

    def test_scans_unity_project_and_complete_build(self) -> None:
        project = self.workspace / "Game"
        for name in ("Assets", "Packages", "ProjectSettings"):
            (project / name).mkdir(parents=True)
        (project / "ProjectSettings" / "ProjectVersion.txt").write_text(
            "m_EditorVersion: 6000.5.6f1\n", encoding="utf-8"
        )
        (project / "Packages" / "manifest.json").write_text(
            '{"dependencies":{"com.unity.pipeline":"0.1.0"}}', encoding="utf-8"
        )
        self.make_build()
        result = scan_workspace(self.workspace)
        self.assertEqual(len(result["projects"]), 1)
        self.assertEqual(result["projects"][0]["editor_version"], "6000.5.6f1")
        self.assertTrue(result["projects"][0]["pipeline_installed"])
        self.assertEqual(len(result["builds"]), 1)
        self.assertEqual(result["builds"][0]["validation"], "structure_only")

    def test_publish_is_immutable_and_rejects_path_traversal(self) -> None:
        build = self.make_build()
        previous = validate_web_build(build)
        published = publish_build(build, self.root / "builds", "version-1")
        self.assertEqual(previous["fingerprint"], published["fingerprint"])
        self.assertTrue((Path(published["path"]) / "index.html").is_file())
        with self.assertRaises(FileExistsError):
            publish_build(build, self.root / "builds", "version-1")
        with self.assertRaises(ValueError):
            publish_build(build, self.root / "builds", "../outside")

    def test_fingerprint_changes_when_content_changes(self) -> None:
        build = self.make_build()
        previous = validate_web_build(build)["fingerprint"]
        (build / "Build" / "game.data").write_bytes(b"UnityWebData1.0\x00new-content")
        self.assertNotEqual(previous, validate_web_build(build)["fingerprint"])

    def test_symlinked_host_files_cannot_be_published(self) -> None:
        build = self.make_build()
        outside = self.root / "private.txt"
        outside.write_text("outside fixture", encoding="utf-8")
        try:
            (build / "outside.txt").symlink_to(outside)
        except OSError:
            self.skipTest("This account cannot create symlinks")
        self.assertIsNone(validate_web_build(build))


class SkillSetupTests(unittest.TestCase):
    def test_bundles_and_references_are_copied_without_overwriting_local_edits(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aiweb-skill-test-") as directory:
            root = Path(directory)
            sources = root / "sources"
            for name in SKILL_NAMES:
                bundle = sources / name
                (bundle / "references").mkdir(parents=True)
                (bundle / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: Test\n---\n", encoding="utf-8"
                )
                (bundle / "references" / "guide.md").write_text("fixture reference", encoding="utf-8")
            setup = prepare_skills(root / "dsh", source_root=sources)
            self.assertFalse(setup["missing"])
            self.assertEqual(len(setup["skills"]), len(SKILL_NAMES))
            self.assertTrue((Path(setup["root"]) / "unity-cli" / "references" / "guide.md").is_file())
            original = sources / "unity-cli" / "SKILL.md"
            original_text = original.read_text(encoding="utf-8")
            installed = Path(setup["root"]) / "unity-cli" / "SKILL.md"
            installed.write_text("local customization", encoding="utf-8")
            second = prepare_skills(root / "dsh", source_root=sources)
            self.assertEqual(installed.read_text(encoding="utf-8"), "local customization")
            self.assertEqual(original.read_text(encoding="utf-8"), original_text)
            self.assertFalse(second["copied"])
            patch = Path(setup["patch_path"]).read_text(encoding="utf-8")
            self.assertIn("@deepseek-ai/dsh-skill-filesystem", patch)
            self.assertIn("customSkillDirs", patch)


if __name__ == "__main__":
    unittest.main()
