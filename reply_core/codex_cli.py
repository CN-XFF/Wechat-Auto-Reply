"""Find accessible native Codex CLI binaries only when an AI task needs one."""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


class CodexUnavailableError(RuntimeError):
    """The local CLI could not be found or started; the main UI can stay open."""


def _npm_binaries(prefix: Path):
    triplets = ["x86_64-pc-windows-msvc"]
    if platform.machine().lower() in {"arm64", "aarch64"}:
        triplets.insert(0, "aarch64-pc-windows-msvc")
    packages = prefix / "node_modules" / "@openai"
    for triplet in triplets:
        architecture = "arm64" if triplet.startswith("aarch64") else "x64"
        for package in (
            packages / "codex",
            packages / f"codex-win32-{architecture}",
            packages / "codex" / "node_modules" / "@openai" / f"codex-win32-{architecture}",
        ):
            for binary_directory in ("bin", "codex"):
                yield package / "vendor" / triplet / binary_directory / "codex.exe"


def _candidate_paths(configured: str):
    configured = os.path.expandvars(os.path.expanduser(configured.strip().strip('"')))
    if configured and configured.lower() not in {"codex", "codex.exe"}:
        # Do not resolve links: some Windows app aliases fail during traversal.
        yield Path(configured)
    directories = [Path(p.strip().strip('"')) for p in os.environ.get("PATH", "").split(os.pathsep) if p.strip().strip('"')]
    for directory in directories:
        yield directory / ("codex.exe" if os.name == "nt" else "codex")
    if os.name == "nt":
        # npm's .cmd wrapper is not required: invoke its native binary directly.
        for directory in directories:
            yield from _npm_binaries(directory)
        appdata = os.environ.get("APPDATA")
        if appdata:
            yield from _npm_binaries(Path(appdata) / "npm")
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            yield Path(localappdata) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
            yield Path(localappdata) / "Microsoft" / "WinGet" / "Links" / "codex.exe"
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            yield Path(userprofile) / ".local" / "bin" / "codex.exe"
            yield Path(userprofile) / ".cargo" / "bin" / "codex.exe"
        cargo_home = os.environ.get("CARGO_HOME")
        if cargo_home:
            yield Path(cargo_home) / "bin" / "codex.exe"


def run_codex(arguments: list[str], *, executable: str = "", strict_executable: bool = False, **kwargs):
    """Skip inaccessible aliases; retry only if creating the process fails."""
    seen = set()
    inaccessible = False
    candidates = [Path(executable)] if strict_executable else _candidate_paths(executable)
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key in seen:
            continue
        seen.add(key)
        if os.name == "nt" and candidate.suffix.lower() != ".exe":
            continue
        try:
            if not candidate.is_file():
                continue
        except OSError:
            inaccessible = True
            continue
        try:
            return subprocess.run([str(candidate), *arguments], **kwargs)
        except OSError as exc:
            if getattr(exc, "winerror", None) in {2, 3, 5, 126, 193, 216, 448} or exc.errno in {2, 8, 13}:
                inaccessible = True
                continue
            raise CodexUnavailableError("Codex 命令行未能启动，请检查本机安装、权限和程序路径。") from exc
    detail = "已跳过 Windows 无法访问的程序路径。" if inaccessible else ""
    raise CodexUnavailableError(
        detail + "没有找到可用的 Codex 命令行程序。请安装并登录 Codex CLI 后重新启动本程序；"
        "如已安装，请在主界面点击“选择 Codex 程序…”指定可访问的 codex.exe。"
    )
