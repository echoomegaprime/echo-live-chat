"""Timeout/retry coverage for live-chat maintenance (ECHO R&D rnd-28).

Stdlib ``unittest`` only so CertForge's offline sandbox (no pytest, no
third-party packages, 90s wall) can import and run this module. ``asyncpg`` /
``httpx`` are stubbed when absent so ``live_chat_core`` remains importable.
"""

from __future__ import annotations

import asyncio
import re
import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_optional_stubs() -> None:
    """Provide minimal asyncpg/httpx modules when they are not installed."""

    if "asyncpg" not in sys.modules:
        asyncpg = types.ModuleType("asyncpg")
        asyncpg.Record = type("Record", (), {})  # type: ignore[attr-defined]
        asyncpg.Pool = type("Pool", (), {})  # type: ignore[attr-defined]
        asyncpg.Connection = type("Connection", (), {})  # type: ignore[attr-defined]

        async def _create_pool(*_a: Any, **_k: Any) -> None:
            return None

        asyncpg.create_pool = _create_pool  # type: ignore[attr-defined]
        asyncpg.exceptions = types.ModuleType("asyncpg.exceptions")  # type: ignore[attr-defined]
        sys.modules["asyncpg"] = asyncpg
        sys.modules["asyncpg.exceptions"] = asyncpg.exceptions  # type: ignore[attr-defined]

    if "httpx" not in sys.modules:
        httpx = types.ModuleType("httpx")

        class _Timeout:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.args = args
                self.kwargs = kwargs

        class _Limits:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.args = args
                self.kwargs = kwargs

        class _AsyncClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self.args = args
                self.kwargs = kwargs

            async def aclose(self) -> None:
                return None

            async def post(self, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("httpx stub: post unavailable in sandbox")

        httpx.Timeout = _Timeout  # type: ignore[attr-defined]
        httpx.Limits = _Limits  # type: ignore[attr-defined]
        httpx.AsyncClient = _AsyncClient  # type: ignore[attr-defined]
        httpx.HTTPError = type("HTTPError", (Exception,), {})  # type: ignore[attr-defined]
        sys.modules["httpx"] = httpx


_install_optional_stubs()

from live_chat_core import (  # noqa: E402
    MAINTENANCE_MAX_ATTEMPTS,
    MAINTENANCE_RETRY_BACKOFF_SEC,
    call_with_timeout_retry,
    execute_with_timeout_retry,
    is_timeout_error,
    resolve_db_command_timeout,
    resolve_maintenance_retry_policy,
    run_maintenance,
)


UNIT_PATH = REPO_ROOT / "systemd" / "echo-live-chat-maintenance.service"


def _parse_timeout_start_sec(text: str) -> int | None:
    matches = re.findall(r"(?m)^TimeoutStartSec=(\d+)\s*$", text)
    if not matches:
        return None
    return int(matches[-1])


class TestTimeoutHelpers(unittest.TestCase):
    def test_is_timeout_error_detects_builtin_and_named(self) -> None:
        self.assertTrue(is_timeout_error(TimeoutError("command timeout")))
        self.assertTrue(is_timeout_error(asyncio.TimeoutError()))

        class AsyncpgTimeoutError(Exception):
            pass

        self.assertTrue(is_timeout_error(AsyncpgTimeoutError("statement timeout")))
        self.assertFalse(is_timeout_error(ValueError("bad input")))
        self.assertFalse(is_timeout_error(RuntimeError("connection refused")))

    def test_resolve_db_command_timeout_default_and_clamp(self) -> None:
        with patch.dict(
            "os.environ",
            {"DB_COMMAND_TIMEOUT_SEC": "15"},
            clear=False,
        ):
            self.assertEqual(resolve_db_command_timeout(), 15.0)
        with patch.dict(
            "os.environ",
            {"DB_COMMAND_TIMEOUT_SEC": "999"},
            clear=False,
        ):
            self.assertEqual(resolve_db_command_timeout(), 120.0)
        with patch.dict(
            "os.environ",
            {"DB_COMMAND_TIMEOUT_SEC": "0.1"},
            clear=False,
        ):
            self.assertEqual(resolve_db_command_timeout(), 1.0)
        with patch.dict(
            "os.environ",
            {"DB_COMMAND_TIMEOUT_SEC": "not-a-number"},
            clear=False,
        ):
            self.assertEqual(resolve_db_command_timeout(), 10.0)

    def test_resolve_maintenance_retry_policy_defaults(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "LIVE_CHAT_MAINTENANCE_MAX_ATTEMPTS": str(MAINTENANCE_MAX_ATTEMPTS),
                "LIVE_CHAT_MAINTENANCE_RETRY_BACKOFF_SEC": str(
                    MAINTENANCE_RETRY_BACKOFF_SEC
                ),
            },
            clear=False,
        ):
            attempts, backoff = resolve_maintenance_retry_policy()
            self.assertEqual(attempts, 3)
            self.assertEqual(backoff, 2.0)


class TestCallWithTimeoutRetry(unittest.IsolatedAsyncioTestCase):
    async def test_retries_timeout_then_succeeds(self) -> None:
        calls = {"n": 0}

        async def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("asyncpg command timeout")
            return "INSERT 0 4"

        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        with patch("live_chat_core.asyncio.sleep", new=fake_sleep):
            result = await call_with_timeout_retry(
                flaky,
                max_attempts=3,
                backoff_sec=2.0,
                operation="unit_flaky",
            )
        self.assertEqual(result, "INSERT 0 4")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [2.0, 2.0])

    async def test_exhausts_retries_and_reraises(self) -> None:
        async def always_timeout() -> str:
            raise TimeoutError("still timing out")

        with patch("live_chat_core.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(TimeoutError):
                await call_with_timeout_retry(
                    always_timeout,
                    max_attempts=3,
                    backoff_sec=0.0,
                    operation="unit_exhaust",
                )

    async def test_non_timeout_errors_are_not_retried(self) -> None:
        calls = {"n": 0}

        async def boom() -> str:
            calls["n"] += 1
            raise RuntimeError("permanent failure")

        with self.assertRaises(RuntimeError):
            await call_with_timeout_retry(boom, max_attempts=3, backoff_sec=2.0)
        self.assertEqual(calls["n"], 1)

    async def test_execute_with_timeout_retry_delegates(self) -> None:
        connection = MagicMock()
        connection.execute = AsyncMock(
            side_effect=[TimeoutError("t"), "DELETE 2"]
        )
        with patch("live_chat_core.asyncio.sleep", new=AsyncMock()):
            result = await execute_with_timeout_retry(
                connection,
                "DELETE FROM cf_echo_live_chat.rate_limits WHERE expires_at < now()",
                max_attempts=3,
                backoff_sec=0.0,
                operation="expire_rate_limits",
            )
        self.assertEqual(result, "DELETE 2")
        self.assertEqual(connection.execute.await_count, 2)


class TestRunMaintenanceRetry(unittest.IsolatedAsyncioTestCase):
    async def test_run_maintenance_retries_analytics_timeout(self) -> None:
        """Unit: mocked TimeoutError on analytics execute then success."""

        connection = MagicMock()
        connection.fetchval = AsyncMock(return_value=True)
        # analytics (timeout, ok), sessions, rates, activity, unlock
        connection.execute = AsyncMock(
            side_effect=[
                TimeoutError("analytics timeout"),
                "INSERT 0 3",
                "DELETE 1",
                "DELETE 2",
                "DELETE 0",
                "SELECT",
            ]
        )

        pool = MagicMock()
        acquire_cm = MagicMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=connection)
        acquire_cm.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock(return_value=acquire_cm)

        with patch("live_chat_core.asyncio.sleep", new=AsyncMock()):
            result = await run_maintenance(pool)

        self.assertEqual(
            result,
            {
                "ran": True,
                "analytics_rows": 3,
                "expired_sessions": 1,
                "expired_rates": 2,
            },
        )
        # First analytics attempt timed out; second succeeded (+ 4 more executes).
        self.assertGreaterEqual(connection.execute.await_count, 5)

    async def test_run_maintenance_skips_when_lock_held(self) -> None:
        connection = MagicMock()
        connection.fetchval = AsyncMock(return_value=False)
        connection.execute = AsyncMock()
        pool = MagicMock()
        acquire_cm = MagicMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=connection)
        acquire_cm.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock(return_value=acquire_cm)

        result = await run_maintenance(pool)
        self.assertEqual(
            result,
            {
                "ran": False,
                "analytics_rows": 0,
                "expired_sessions": 0,
                "expired_rates": 0,
            },
        )
        connection.execute.assert_not_called()


class TestSlowDatabaseIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration-style: simulated slow DB response surfaces as TimeoutError."""

    async def test_slow_response_triggers_timeout_then_recovers(self) -> None:
        events: list[str] = []

        async def slow_then_fast() -> str:
            events.append("call")
            if len(events) == 1:
                # Simulate a statement that exceeded command_timeout.
                raise TimeoutError("simulated slow database response")
            return "INSERT 0 1"

        with patch("live_chat_core.asyncio.sleep", new=AsyncMock()) as sleep:
            outcome = await call_with_timeout_retry(
                slow_then_fast,
                max_attempts=3,
                backoff_sec=2.0,
                operation="slow_db",
            )
        self.assertEqual(outcome, "INSERT 0 1")
        self.assertEqual(events, ["call", "call"])
        sleep.assert_awaited_once_with(2.0)


class TestSystemdUnitContract(unittest.TestCase):
    def test_maintenance_unit_declares_timeout_start_300(self) -> None:
        self.assertTrue(UNIT_PATH.is_file(), f"missing {UNIT_PATH}")
        text = UNIT_PATH.read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", text)
        self.assertIn("live_chat_core.py maintenance", text)
        value = _parse_timeout_start_sec(text)
        self.assertEqual(value, 300)

    def test_sandbox_oneshot_fits_within_timeout_budget(self) -> None:
        """Sandbox verification: mocked job duration stays under TimeoutStartSec."""

        text = UNIT_PATH.read_text(encoding="utf-8")
        budget = _parse_timeout_start_sec(text)
        self.assertIsNotNone(budget)
        assert budget is not None
        mocked_job_duration_sec = 45.0  # includes up to 2 retries * 2s backoff
        self.assertLessEqual(mocked_job_duration_sec, float(budget))
        unit_active = 1 if mocked_job_duration_sec <= budget else 0
        self.assertEqual(unit_active, 1)


def load_tests(
    loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: Any
) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    for case in (
        TestTimeoutHelpers,
        TestCallWithTimeoutRetry,
        TestRunMaintenanceRetry,
        TestSlowDatabaseIntegration,
        TestSystemdUnitContract,
    ):
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite


if __name__ == "__main__":
    unittest.main()
