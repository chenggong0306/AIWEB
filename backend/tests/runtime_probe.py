"""Exercise the installed DSH runtime against a local HTTP fixture.

This is an integration probe, not a model test: no real API key is read and
the SDK endpoint is always a temporary loopback-only HTTP server. DSH still
boots its actual plugins and executes one harmless shell command. All DSH
state and shell work are confined to a fresh temporary directory.

Run from backend: uv run python tests/runtime_probe.py
Optional: --patch PATH --expect-skill unity-cli --keep
When a skill is expected, the fixture first calls the actual DSH skill tool
and checks that its full body reaches a subsequent model request. Merely
advertising the skill name in a catalog is not counted as loading the skill.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deepseek_harness import DeepSeekHarness


MARKER = "DSH_LOCAL_RUNTIME_PROBE_OK"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="sdk-minimal")
    parser.add_argument("--patch", action="append", default=[])
    parser.add_argument("--expect-skill", action="append", default=[])
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    expected_names = list(dict.fromkeys(args.expect_skill))
    skill_body_markers: dict[str, str] = {}
    for name in expected_names:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            parser.error("--expect-skill must name a packaged skill")
        skill_file = Path(__file__).resolve().parents[1] / "resources" / "skills" / name / "SKILL.md"
        if not skill_file.is_file():
            parser.error(f"Cannot locate packaged instructions for expected skill: {name}")
        # A section heading belongs to the full body and is absent from the
        # registry catalog (which contains only the name and description).
        marker = next((line.strip() for line in skill_file.read_text(encoding="utf-8").splitlines() if line.startswith("## ")), None)
        if marker is None:
            parser.error(f"Expected skill has no body section heading to verify: {name}")
        skill_body_markers[name] = marker

    root = Path(tempfile.mkdtemp(prefix="dsh-local-runtime-probe-"))
    workspace = root / "workspace"
    home = root / "dsh-home"
    workspace.mkdir()
    home.mkdir()
    requests: list[dict] = []
    observed: list[dict] = []
    state: dict = {"status": "pending", "fixture_only": True}
    completed = threading.Event()

    class FixtureHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                requests.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                if len(requests) <= len(expected_names):
                    index = len(requests) - 1
                    delta = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "index": 0,
                            "id": f"probe-skill-{index}",
                            "type": "function",
                            "function": {"name": "skill", "arguments": json.dumps({"name": expected_names[index]})},
                        }],
                    }
                    reason = "tool_calls"
                elif len(requests) == len(expected_names) + 1:
                    tool_names = [tool.get("function", {}).get("name") for tool in body.get("tools", [])]
                    shell = "pwsh" if os.name == "nt" else "bash"
                    if shell not in tool_names:
                        state["missing_shell_tool"] = shell
                    command = f"Write-Output '{MARKER}'" if shell == "pwsh" else f"printf '%s\\n' '{MARKER}'"
                    delta = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "index": 0,
                            "id": "probe-tool-1",
                            "type": "function",
                            "function": {"name": shell, "arguments": json.dumps({"command": command})},
                        }],
                    }
                    reason = "tool_calls"
                else:
                    delta = {"role": "assistant", "content": "Local fixture response."}
                    reason = "stop"
                chunks = [
                    {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
                    {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                ]
                for chunk in chunks:
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    harness = DeepSeekHarness(
        profile=args.profile,
        cwd=str(workspace),
        runtime_cwd=str(workspace),
        dsh_home=str(home),
        patches=tuple(str(Path(patch).resolve()) for patch in args.patch),
        provider="deepseek-official",
        model="deepseek-v4-flash",
        api_key="LOCAL_FIXTURE_NOT_A_REAL_KEY",
        base_url=f"http://127.0.0.1:{server.server_port}",
        env={"DSH_RUNTIME_MODE": "exe"},
        initialize_timeout_seconds=min(args.timeout, 20),
        request_timeout_seconds=10,
        shutdown_timeout_seconds=1,
        max_tokens=100,
    )

    def notification(item: object) -> None:
        # Local fixture outputs only. Never dump arbitrary runtime events.
        if getattr(item, "method", "") != "session.event":
            return
        event = item.payload.get("event", {})
        if event.get("type") in {"tool/call", "tool/result"}:
            observed.append(event)

    def worker() -> None:
        try:
            harness.start()
            first = harness.run("probe-first: run the harmless shell marker", session_id="runtime-probe", on_notification=notification)
            second = harness.run("probe-second: continue the same session", session_id="runtime-probe", on_notification=notification)
            last_messages = requests[-1].get("messages", []) if requests else []
            last_context = json.dumps(last_messages, ensure_ascii=False)
            advertised = json.dumps(requests[0], ensure_ascii=False) if requests else ""
            tool_ok = any(
                MARKER in json.dumps(event.get("data", {}))
                and not any(block.get("isError") for block in event.get("data", {}).get("message", {}).get("content", []))
                for event in observed if event.get("type") == "tool/result"
            )
            skills: dict[str, bool] = {}
            followup_skills: dict[str, bool] = {}
            for index, name in enumerate(expected_names):
                call_id = f"probe-skill-{index}"
                # Check what was actually forwarded back to the local model,
                # not a catalog entry or the fixture's tool-call arguments.
                model_received_body = any(
                    message.get("role") == "tool"
                    and message.get("tool_call_id") == call_id
                    and skill_body_markers[name] in json.dumps(message.get("content", ""), ensure_ascii=False)
                    for request in requests[index + 1:]
                    for message in request.get("messages", [])
                )
                successful_result = any(
                    block.get("toolCallId") == call_id
                    and not block.get("isError", False)
                    for event in observed if event.get("type") == "tool/result"
                    for block in event.get("data", {}).get("message", {}).get("content", [])
                )
                followup_skills[name] = any(
                    message.get("role") == "tool"
                    and message.get("tool_call_id") == call_id
                    and skill_body_markers[name] in json.dumps(message.get("content", ""), ensure_ascii=False)
                    for message in last_messages
                )
                skills[name] = model_received_body and successful_result and followup_skills[name]
            context_ok = "probe-first" in last_context and "probe-second" in last_context
            ok = tool_ok and context_ok and all(skills.values()) and first.finish_reason == second.finish_reason == "completed"
            state.update(
                status="passed" if ok else "failed",
                tool_execution_ok=tool_ok,
                same_runtime_multi_turn_ok=context_ok,
                expected_skills=skills,
                skill_catalog_seen={name: name in advertised for name in expected_names},
                skill_body_markers=skill_body_markers,
                skill_body_retained_in_followup=followup_skills,
                first_finish_reason=first.finish_reason,
                second_finish_reason=second.finish_reason,
                fixture_request_count=len(requests),
                advertised_tools=[tool.get("function", {}).get("name") for tool in requests[0].get("tools", [])] if requests else [],
                tool_event_types=[event.get("type") for event in observed],
            )
        except Exception as error:
            state.update(status="failed", error=f"{type(error).__name__}: {error}")
        finally:
            harness.close()
            completed.set()

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()
    try:
        if not completed.wait(args.timeout):
            state.update(status="failed", error="Runtime probe exceeded its wall-clock limit.")
            harness.close()
            worker_thread.join(3)
    except KeyboardInterrupt:
        state.update(status="failed", error="Interrupted")
        harness.close()
    finally:
        server.shutdown()
        server.server_close()
        if args.keep:
            state["temporary_directory"] = str(root)
        else:
            try:
                resolved_root = root.resolve()
                if resolved_root.parent != Path(tempfile.gettempdir()).resolve() or not resolved_root.name.startswith("dsh-local-runtime-probe-"):
                    raise OSError("Refusing to clean an unexpected temporary path")
                shutil.rmtree(resolved_root)
            except OSError:
                state["temporary_directory"] = str(root)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0 if state["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
