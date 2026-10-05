#!/usr/bin/env python3
"""最小 pytest 兼容运行器（离线环境没有 pytest 时兜底用）。

仅实现本仓库测试实际用到的能力：函数型 fixture（含依赖注入）、
``pytest.raises``、``pytest.approx``、``tmp_path`` 与 ``capsys``。
不是 pytest 的替代实现，仅供门禁自检；正式 CI 仍用 pytest。
"""

from __future__ import annotations

import importlib.util
import inspect
import io
import sys
import tempfile
import traceback
import types
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))


class Approx:
    def __init__(self, expected, rel=1e-6, abs_=1e-9):
        self.expected = expected
        self.rel = rel
        self.abs = abs_

    def __eq__(self, other):
        return abs(other - self.expected) <= max(self.rel * abs(self.expected), self.abs)

    def __repr__(self):
        return f"approx({self.expected!r})"


class Raises:
    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"期望抛出 {self.exc_type.__name__}，但没有抛出")
        if not issubclass(exc_type, self.exc_type):
            return False
        self.value = exc
        return True


pytest = types.ModuleType("pytest")
pytest.approx = lambda expected, rel=1e-6, abs=1e-9: Approx(expected, rel, abs)
pytest.raises = lambda exc_type: Raises(exc_type)


def fixture(fn=None, **_kwargs):
    def wrap(func):
        func.__pytest_fixture__ = True
        return func
    return wrap(fn) if fn is not None else wrap


pytest.fixture = fixture
sys.modules["pytest"] = pytest


class CapSys:
    def __init__(self):
        self._out = io.StringIO()
        self._err = io.StringIO()
        self._old_out = self._old_err = None

    def __enter__(self):
        self._old_out, self._old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self._out, self._err
        return self

    def __exit__(self, *args):
        sys.stdout, sys.stderr = self._old_out, self._old_err

    def readouterr(self):
        out, err = self._out.getvalue(), self._err.getvalue()
        self._out.seek(0)
        self._out.truncate(0)
        self._err.seek(0)
        self._err.truncate(0)
        return types.SimpleNamespace(out=out, err=err)


def load_conftest():
    path = TESTS / "conftest.py"
    spec = importlib.util.spec_from_file_location("conftest", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["conftest"] = module
    spec.loader.exec_module(module)
    fixtures = {
        name: value
        for name, value in vars(module).items()
        if callable(value) and getattr(value, "__pytest_fixture__", False)
    }
    return fixtures


def resolve(arg, fixtures, cache):
    if arg == "tmp_path":
        return Path(tempfile.mkdtemp(prefix="pytest-shim-"))
    if arg == "capsys":
        return CapSys()
    if arg not in fixtures:
        raise AssertionError(f"未知 fixture: {arg}")
    if arg not in cache:
        fn = fixtures[arg]
        deps = _deps(fn, fixtures, cache)
        cache[arg] = fn(**deps)
    return cache[arg]


def _deps(fn, fixtures, cache):
    deps = {}
    for name, param in inspect.signature(fn).parameters.items():
        if name in fixtures or name in ("tmp_path", "capsys"):
            deps[name] = resolve(name, fixtures, cache)
    return deps


def main() -> int:
    fixtures = load_conftest()
    failures = 0
    total = 0
    for path in sorted(TESTS.glob("test_*.py")):
        modname = path.stem
        spec = importlib.util.spec_from_file_location(modname, path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"ERROR collecting {path.name}")
            traceback.print_exc()
            continue
        for name, fn in sorted(vars(module).items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            total += 1
            cache = {}
            kwargs = {}
            try:
                for param in inspect.signature(fn).parameters:
                    kwargs[param] = resolve(param, fixtures, cache)
                capsys = next((v for v in kwargs.values() if isinstance(v, CapSys)), None)
                if capsys is not None:
                    with capsys:
                        fn(**kwargs)
                else:
                    fn(**kwargs)
                print(f"PASS {path.name}::{name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {path.name}::{name}")
                traceback.print_exc()
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
