#!/usr/bin/env python3
"""Minimal stdlib test runner for CertForge offline journeys.

Loads unittest modules by path, optionally extending sys.path, and exits
nonzero on failure. No pytest / third-party dependencies.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType


def _load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def run_suite(test_path: Path, sys_paths: list[str]) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    for rel in sys_paths:
        abs_path = str((repo_root / rel).resolve()) if not Path(rel).is_absolute() else rel
        if abs_path not in sys.path:
            sys.path.insert(0, abs_path)

    path = test_path if test_path.is_absolute() else (repo_root / test_path)
    if not path.is_file():
        print(f"[FAIL] test file missing: {path}")
        return 1

    module = _load_module(path)
    loader = unittest.TestLoader()
    if hasattr(module, "load_tests"):
        suite = module.load_tests(loader, unittest.TestSuite(), None)
    else:
        suite = loader.loadTestsFromModule(module)

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = result.testsRun - len(result.failures) - len(result.errors)
    print(
        f"\ncertforge_testkit: {passed}/{result.testsRun} passed "
        f"(failures={len(result.failures)} errors={len(result.errors)})"
    )
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("test_path", help="Repo-relative or absolute test module path")
    parser.add_argument(
        "--sys-path",
        action="append",
        default=[],
        dest="sys_paths",
        help="Directory to prepend to sys.path (repeatable)",
    )
    args = parser.parse_args(argv)
    return run_suite(Path(args.test_path), list(args.sys_paths))


if __name__ == "__main__":
    sys.exit(main())
