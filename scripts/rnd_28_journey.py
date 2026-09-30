#!/usr/bin/env python3
"""CertForge journey for ECHO R&D proposal #28 (rnd-28).

Verifies live-chat maintenance timeout/retry handling and TimeoutStartSec=300,
then runs the stdlib unittest suite via certforge_testkit. Offline-safe:
stdlib only, no live systemctl, no production edits.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    suffix = f" — {detail}" if detail and not condition else ""
    print(f"[{status}] {name}{suffix}")
    if not condition:
        FAILURES.append(name)


def main() -> int:
    unit = REPO_ROOT / "systemd" / "echo-live-chat-maintenance.service"
    core = REPO_ROOT / "live_chat_core.py"
    test_file = REPO_ROOT / "tests" / "test_maintenance_retry.py"
    cert = REPO_ROOT / ".echo" / "certification.json"
    suites = REPO_ROOT / ".echo" / "certforge_suites.json"
    testkit = REPO_ROOT / "scripts" / "certforge_testkit.py"

    check("unit file exists", unit.is_file())
    check("live_chat_core.py exists", core.is_file())
    check("rnd-28 test module exists", test_file.is_file())
    check("certforge_testkit.py exists", testkit.is_file())
    check("certforge_suites.json exists", suites.is_file())

    if unit.is_file():
        text = unit.read_text(encoding="utf-8")
        check("unit declares TimeoutStartSec=300", "TimeoutStartSec=300" in text)
        check("unit remains Type=oneshot", "Type=oneshot" in text)
        check(
            "unit still invokes live_chat_core.py maintenance",
            "live_chat_core.py maintenance" in text,
        )

    if core.is_file():
        source = core.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(core))
            check("live_chat_core.py parses", True)
        except SyntaxError as exc:
            check("live_chat_core.py parses", False, str(exc))
        check(
            "live_chat_core defines call_with_timeout_retry",
            "def call_with_timeout_retry" in source,
        )
        check(
            "live_chat_core defines execute_with_timeout_retry",
            "def execute_with_timeout_retry" in source,
        )
        check(
            "run_maintenance uses execute_with_timeout_retry",
            "execute_with_timeout_retry" in source
            and "async def run_maintenance" in source,
        )
        check(
            "create_pool uses resolve_db_command_timeout",
            "command_timeout=resolve_db_command_timeout()" in source,
        )
        check(
            "default retry policy is 3 attempts / 2s backoff",
            "MAINTENANCE_MAX_ATTEMPTS = 3" in source
            and "MAINTENANCE_RETRY_BACKOFF_SEC = 2.0" in source,
        )

    if suites.is_file():
        suites_data = json.loads(suites.read_text(encoding="utf-8"))
        paths = [s.get("path") for s in suites_data.get("suites", [])]
        check(
            "certforge_suites lists rnd-28 test",
            "tests/test_maintenance_retry.py" in paths,
        )

    if cert.is_file():
        data = json.loads(cert.read_text(encoding="utf-8"))
        rnd = data.get("rnd") or {}
        check("certification.json has rnd.proposal_id == 28", rnd.get("proposal_id") == 28)
        check(
            "certification.json rnd.policy_version",
            rnd.get("policy_version") == "certforge.rnd-staging.v1",
        )
        journey = rnd.get("journey") or []
        check(
            "certification.json rnd.journey points at rnd_28_journey.py",
            journey == ["python3", "-B", "scripts/rnd_28_journey.py"],
        )
        check("certification.json keeps version key", "version" in data)
        check("certification.json keeps top-level journey", "journey" in data)

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import certforge_testkit  # noqa: WPS433
    except ImportError as exc:
        check("certforge_testkit importable", False, str(exc))
    else:
        rc = certforge_testkit.run_suite(
            test_file,
            sys_paths=["."],
        )
        check("rnd-28 unittest suite green", rc == 0, f"exit={rc}")

    print()
    if FAILURES:
        print(f"RND-28 JOURNEY: FAIL ({len(FAILURES)} check(s) failed)")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("RND-28 JOURNEY: PASS (all checks green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
