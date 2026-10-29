#!/usr/bin/env python3
"""Read-only semantic comparison of repository hosting source and installed artifacts."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import plistlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_tool(name: str):
    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shell_semantics(payload: bytes) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in payload.decode("utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def python_semantics(payload: bytes) -> str:
    return ast.dump(ast.parse(payload.decode("utf-8")), include_attributes=False)


def equivalent(path: Path, generated: bytes) -> bool:
    installed = path.read_bytes()
    if path.suffix == ".plist":
        return plistlib.loads(installed) == plistlib.loads(generated)
    if path.suffix == ".zsh":
        return shell_semantics(installed) == shell_semantics(generated)
    if path.suffix == ".py":
        return python_semantics(installed) == python_semantics(generated)
    return installed == generated


def compare(*, home: Path, production_root: Path, expected_head: str) -> dict[str, bool]:
    admin_parent = load_tool("production_launchd_control")
    extraction = load_tool("content_extraction_launchd_control")
    generated = {}
    generated.update(admin_parent.render_artifacts(
        source_root=ROOT,
        home=home,
        production_root=production_root,
        expected_head=expected_head,
    ))
    generated.update(extraction.render_artifacts(
        source_root=ROOT,
        home=home,
        production_root=production_root,
        expected_head=expected_head,
        volume_root=Path("/Volumes/Zhang"),
    ))
    return {
        str(path): path.is_file() and equivalent(path, content)
        for path, (content, _mode) in generated.items()
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--production-root", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args(argv)
    results = compare(
        home=args.home.resolve(),
        production_root=args.production_root.resolve(),
        expected_head=args.expected_head,
    )
    print(json.dumps({"equivalent": all(results.values()), "artifacts": results}, indent=2))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
