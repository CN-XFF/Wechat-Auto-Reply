"""Read CLI capabilities and model metadata without starting a model turn."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path

from .codex_cli import CodexUnavailableError, run_codex


class CliCompatibilityError(RuntimeError):
    pass


def _environment():
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.pop("CODEX_API_KEY", None)
    return env


@lru_cache(maxsize=8)
def inspect_exec(executable: str) -> dict:
    options = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                   timeout=8, env=_environment(), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    version = run_codex(["--version"], executable=executable, **options)
    if version.returncode != 0:
        raise CliCompatibilityError("无法读取 Codex CLI 版本，请重新选择实际的命令行程序。")
    match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version.stdout or "")
    actual = str(version.args[0])
    help_result = run_codex(["exec", "--help"], executable=actual, strict_executable=True, **options)
    if help_result.returncode != 0:
        raise CliCompatibilityError("此 Codex CLI 无法提供 exec 功能信息，请升级 CLI 后刷新。")
    flags = sorted(set(re.findall(r"(?<!\w)--[a-z][a-z0-9-]*", help_result.stdout or "")))
    return {"version": match.group(0) if match else "版本未识别", "executable": actual, "flags": flags}


def missing_exec_flags(info: dict, output_schema: bool = True) -> list[str]:
    required = {"--ephemeral", "--sandbox", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "--model"}
    if output_schema:
        required.add("--output-schema")
    return sorted(required - set(info.get("flags", [])))


def require_exec_compatibility(executable: str, output_schema: bool = True) -> str:
    info = inspect_exec(executable)
    missing = missing_exec_flags(info, output_schema)
    if missing:
        raise CliCompatibilityError(
            f"Codex CLI {info['version']} 缺少本程序所需功能（{', '.join(missing)}）。"
            "请升级 CLI，再点击“刷新模型列表”。当前任务未执行。"
        )
    return info["executable"]


class _CatalogConnection:
    def __init__(self, executable: str, workdir: Path, cancelled: threading.Event):
        self.cancelled = cancelled
        self.deadline = time.monotonic() + 25
        self.messages = queue.Queue(maxsize=256)
        self.request_id = 0
        self.process = subprocess.Popen(
            [executable, "app-server"], cwd=str(workdir), env=_environment(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(1024 * 1024 + 1)
                if not line:
                    break
                if len(line) > 1024 * 1024:
                    break
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                try:
                    self.messages.put_nowait(item)
                except queue.Full:
                    break
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.messages.put_nowait(None)
            except queue.Full:
                pass

    def send(self, message: dict):
        if self.cancelled.is_set():
            raise CliCompatibilityError("查询已取消。")
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise CliCompatibilityError("CLI 模型目录连接已关闭，请升级 CLI 或重新刷新。") from None

    def request(self, method: str, params: dict):
        self.request_id += 1
        current_id = self.request_id
        self.send({"id": current_id, "method": method, "params": params})
        while not self.cancelled.is_set() and time.monotonic() < self.deadline:
            try:
                message = self.messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if message is None:
                raise CliCompatibilityError("CLI 未返回模型目录，可能版本过旧；请升级后刷新。")
            if not isinstance(message, dict):
                continue
            if message.get("id") == current_id and "method" not in message:
                if "error" in message:
                    raise CliCompatibilityError("CLI 不支持此目录查询或查询失败，请检查登录、网络和 CLI 版本。")
                return message.get("result")
            if "method" in message and "id" in message:
                # Never approve actions or supply authentication tokens.
                self.send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
        raise CliCompatibilityError("查询已取消。" if self.cancelled.is_set() else "模型目录查询超时，请检查网络或升级 CLI 后刷新。")

    def close(self):
        process = self.process
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def _normalise_models(rows: list) -> list[dict]:
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("hidden"):
            continue
        slug = row.get("model") or row.get("id")
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", slug) or slug in seen:
            continue
        modalities = row.get("inputModalities")
        if isinstance(modalities, list) and "text" not in modalities:
            continue
        efforts = []
        for entry in row.get("supportedReasoningEfforts") or []:
            effort = entry.get("reasoningEffort") if isinstance(entry, dict) else entry
            if isinstance(effort, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,29}", effort) and effort not in efforts:
                efforts.append(effort)
        display = row.get("displayName")
        result.append({"id": slug, "name": display[:100] if isinstance(display, str) else slug,
                       "efforts": efforts, "default_effort": row.get("defaultReasoningEffort"),
                       "is_default": bool(row.get("isDefault"))})
        seen.add(slug)
    return result


def refresh_cli_catalog(executable: str, workdir: Path, cancelled: threading.Event) -> dict:
    inspect_exec.cache_clear()
    info = inspect_exec(executable)
    result = {**info, "missing_flags": missing_exec_flags(info), "models": [], "login_state": "未知", "catalog_error": ""}
    if cancelled.is_set():
        return result
    connection = None
    try:
        connection = _CatalogConnection(info["executable"], workdir, cancelled)
        connection.request("initialize", {"clientInfo": {"name": "wechat_auto_reply", "title": "WeChat Auto Reply", "version": "1.0.9"}})
        connection.send({"method": "initialized"})
        try:
            account = connection.request("account/read", {"refreshToken": False})
            if isinstance(account, dict):
                result["login_state"] = "已登录" if account.get("account") else ("未登录" if account.get("requiresOpenaiAuth") else "无需登录")
            # Account identifiers and credentials never enter the result or logs.
        except CliCompatibilityError:
            pass
        rows = []
        cursor = None
        cursors = set()
        for _ in range(20):
            params = {"limit": 50, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            response = connection.request("model/list", params)
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise CliCompatibilityError("CLI 返回的模型目录格式不受支持，请升级后刷新。")
            rows.extend(response["data"])
            cursor = response.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in cursors:
                raise CliCompatibilityError("CLI 模型目录分页异常，请重新刷新。")
            cursors.add(cursor)
        else:
            raise CliCompatibilityError("CLI 模型目录过大，未完整读取，请升级后刷新。")
        result["models"] = _normalise_models(rows)
        if not result["models"]:
            result["catalog_error"] = "CLI 没有返回可用于文字回复的模型，请检查登录和版本。"
    except (OSError, CliCompatibilityError):
        result["catalog_error"] = "CLI 模型目录查询失败；请检查登录、网络或升级 CLI 后刷新。"
    finally:
        if connection is not None:
            connection.close()
    return result
