#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 Web 向导：连接 Notion → 选页面 → 选目标 → 预览 → 后台迁移 → 结果。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import core, notion

APP_HOME = Path(os.environ.get("N2F_HOME", str(Path.home() / ".notion2feishu")))
CONFIG_PATH = APP_HOME / "config.json"
JOBS_DB = APP_HOME / "jobs.db"
JOB_ROOT = APP_HOME / "jobs"
UI_PATH = Path(__file__).with_name("ui.html")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def notion_token() -> str:
    cfg = load_config()
    return (cfg.get("notion_token") or os.environ.get("NOTION_TOKEN") or "").strip()


def lark_user_ready() -> bool:
    try:
        proc = subprocess.run(
            ["lark-cli", "auth", "status"], capture_output=True, text=True, timeout=20
        )
        raw = proc.stdout or ""
        start = raw.find("{")
        if start < 0:
            return False
        data = json.loads(raw[start:])
        return bool((data.get("identities") or {}).get("user", {}).get("available"))
    except Exception:  # noqa: BLE001
        return False


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 10:
        return "已连接"
    return f"{token[:4]}…{token[-4:]}"


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

class JobStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        self.conn.commit()
        self._mark_interrupted()

    def _mark_interrupted(self) -> None:
        with self.lock:
            rows = self.conn.execute("SELECT id, data FROM jobs").fetchall()
            for jid, raw in rows:
                try:
                    job = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if job.get("status") in ("queued", "running"):
                    job["status"] = "failed"
                    job["error"] = "本地服务重启，任务已中断"
                    self.conn.execute(
                        "UPDATE jobs SET data=?, updated_at=? WHERE id=?",
                        (json.dumps(job, ensure_ascii=False), time.time(), jid),
                    )
            self.conn.commit()

    def create(self, spec: dict) -> dict:
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        job = {
            "id": jid,
            "status": "queued",
            "phase": "queued",
            "progress": 0,
            "message": "排队中",
            "done": 0,
            "total": 0,
            "logs": [],
            "result": {},
            "error": None,
            "spec": spec,
            "created_at": now,
            "updated_at": now,
        }
        with self.lock:
            self.conn.execute(
                "INSERT INTO jobs (id, data, updated_at) VALUES (?, ?, ?)",
                (jid, json.dumps(job, ensure_ascii=False), now),
            )
            self.conn.commit()
        return job

    def get(self, jid: str) -> Optional[dict]:
        with self.lock:
            row = self.conn.execute("SELECT data FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def update(self, jid: str, **fields) -> Optional[dict]:
        with self.lock:
            row = self.conn.execute("SELECT data FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job.update(fields)
            job["updated_at"] = time.time()
            self.conn.execute(
                "UPDATE jobs SET data=?, updated_at=? WHERE id=?",
                (json.dumps(job, ensure_ascii=False), job["updated_at"], jid),
            )
            self.conn.commit()
            return job

    def append_log(self, jid: str, lines: List[str]) -> None:
        if not lines:
            return
        with self.lock:
            row = self.conn.execute("SELECT data FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                return
            job = json.loads(row[0])
            logs = job.setdefault("logs", [])
            logs.extend(lines)
            job["logs"] = logs[-800:]
            job["updated_at"] = time.time()
            self.conn.execute(
                "UPDATE jobs SET data=?, updated_at=? WHERE id=?",
                (json.dumps(job, ensure_ascii=False), job["updated_at"], jid),
            )
            self.conn.commit()


class JobLogger(io.TextIOBase):
    """捕获 core 的 print，同时把进度行映射到 job.progress。"""

    def __init__(self, store: JobStore, jid: str):
        self.store = store
        self.jid = jid
        self.buf = ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if not text:
            return 0
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip()
            if not line:
                continue
            self.store.append_log(self.jid, [line])
            m = re.search(r"\[(建节点|写正文)\s+(\d+)/(\d+)\]", line)
            if m:
                done, total = int(m.group(2)), int(m.group(3))
                if m.group(1) == "建节点":
                    progress = 45 + int(25 * done / max(1, total))
                else:
                    progress = 70 + int(25 * done / max(1, total))
                self.store.update(
                    self.jid, phase="pushing", done=done, total=total,
                    progress=min(95, progress), message=line,
                )
        return len(text)

    def flush(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, store: JobStore):
        self.store = store

    def start(self, jid: str) -> None:
        thread = threading.Thread(target=self._run, args=(jid,), daemon=True)
        thread.start()

    def _container(self, spec: dict) -> Tuple[Optional[str], Optional[str]]:
        title = (spec.get("container_title") or "").strip()
        if not title:
            return None, None
        identity = ["--as", "user"]
        if spec["target"] == "wiki":
            cmd = [
                "wiki", "+node-create",
                "--space-id", spec.get("space_id") or "my_library",
                "--title", title,
                "--obj-type", "docx",
                "--format", "json",
            ] + identity
            parent = spec.get("parent_node_token")
            if parent:
                cmd += ["--parent-node-token", parent]
            res = core._run_cli(cmd)
            token = core._deep_find(res, ["node_token"])
            if not token:
                raise RuntimeError("创建知识库容器节点失败")
            return token, None
        else:
            cmd = [
                "drive", "+create-folder",
                "--name", title,
                "--folder-token", spec["folder_token"],
                "--format", "json",
            ] + identity
            res = core._run_cli(cmd)
            token = core._deep_find(res, ["token", "folder_token"])
            if not token:
                raise RuntimeError("创建云盘容器文件夹失败")
            return None, token

    def _run(self, jid: str) -> None:
        job = self.store.get(jid)
        if not job:
            return
        spec = job["spec"]
        job_dir = JOB_ROOT / jid
        source = job_dir / "source"
        work = job_dir / "work"
        logger = JobLogger(self.store, jid)
        try:
            token = notion_token()
            if not token:
                raise RuntimeError("Notion token 未配置")
            self.store.update(jid, status="running", phase="pulling", progress=5, message="拉取 Notion 页面")

            client = notion.NotionClient(token)

            def progress(done: int, total: Optional[int], message: str) -> None:
                self.store.update(
                    jid, phase="pulling", done=done, total=total or 0,
                    progress=min(35, 5 + done), message=message,
                )

            report = notion.pull_page_tree(client, spec["roots"], source, progress=progress)
            (job_dir / "pull-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if report["total_pages"] == 0:
                raise RuntimeError("没有拉到任何页面，请确认 integration 已连接根页面")

            self.store.update(
                jid, phase="preparing", progress=38,
                total=report["total_pages"], message="清理 Markdown、复制图片",
            )
            with contextlib.redirect_stdout(logger):
                rc = core.cmd_prepare(SimpleNamespace(src=str(source), out=str(work), force=True))
            if rc != 0:
                raise RuntimeError("prepare 失败")

            container_node, container_folder = self._container(spec)
            parent_node = container_node or spec.get("parent_node_token")
            folder_token = container_folder or spec.get("folder_token")

            self.store.update(jid, phase="pushing", progress=45, message="创建飞书文档")
            push_args = SimpleNamespace(
                work=str(work),
                target=spec["target"],
                space_id=spec.get("space_id") or "my_library",
                parent_node_token=parent_node,
                folder_token=folder_token,
                identity="user",
                limit=None,
                resume=True,
                skip_fill=False,
                dry_run=False,
                sleep=0.8,
                force_state=False,
                quiet=False,
                base_url=spec.get("base_url") or "https://feishu.cn",
            )
            with contextlib.redirect_stdout(logger):
                rc = core.cmd_push(push_args)
            if rc != 0:
                raise RuntimeError("push 失败，详见日志")

            state_path = work / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
            links = []
            for rid in manifest.get("roots", []):
                info = (state.get("pages") or {}).get(rid) or {}
                if info.get("url"):
                    links.append({"title": info.get("title") or rid, "url": info["url"]})

            result = {
                "pages": report["total_pages"],
                "images": report["images"],
                "files": report["files"],
                "databases": report["databases"],
                "root_links": links,
                "errors": report.get("errors") or [],
            }
            (job_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.store.update(
                jid, status="done", phase="done", progress=100,
                message="迁移完成", result=result,
            )
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            msg = str(exc) or exc.__class__.__name__
            self.store.append_log(jid, [f"[错误] {msg}"])
            self.store.update(jid, status="failed", phase="failed", error=msg, message="迁移失败")



# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    store: JobStore = None  # type: ignore
    runner: Runner = None  # type: ignore

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, _json_bytes(obj))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                html = UI_PATH.read_text(encoding="utf-8")
            except OSError:
                self._send(500, b"ui.html missing", "text/plain; charset=utf-8")
                return
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/config":
            cfg = load_config()
            token = notion_token()
            self._json(200, {
                "notion_connected": bool(token),
                "notion_hint": mask_token(token),
                "base_url": cfg.get("base_url") or "https://feishu.cn",
                "space_id": cfg.get("space_id") or "my_library",
                "target": cfg.get("target") or "wiki",
                "lark_user_ready": lark_user_ready(),
            })
            return

        if path == "/api/scan":
            token = notion_token()
            if not token:
                self._json(400, {"error": "请先连接 Notion"})
                return
            try:
                client = notion.NotionClient(token)
                client.validate()
                data = notion.scan_workspace(client)
            except notion.NotionError as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
                return
            self._json(200, data)
            return

        m = re.match(r"^/api/jobs/([0-9a-f]+)(/logs|/report)?$", path)
        if m:
            job = self.store.get(m.group(1))
            if not job:
                self._json(404, {"error": "job not found"})
                return
            suffix = m.group(2)
            if suffix == "/logs":
                text = "\n".join(job.get("logs") or [])
                self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
            elif suffix == "/report":
                self._send(
                    200,
                    _json_bytes(job.get("result") or {}),
                    "application/json; charset=utf-8",
                )
            else:
                self._json(200, job)
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_json()

        if path == "/api/connect":
            token = (body.get("token") or "").strip()
            if not token:
                self._json(400, {"error": "token 不能为空"})
                return
            try:
                notion.NotionClient(token).validate()
            except notion.NotionError as exc:
                self._json(400, {"error": str(exc)})
                return
            cfg = load_config()
            cfg["notion_token"] = token
            save_config(cfg)
            self._json(200, {"ok": True, "notion_hint": mask_token(token)})
            return

        if path == "/api/disconnect":
            cfg = load_config()
            cfg.pop("notion_token", None)
            save_config(cfg)
            self._json(200, {"ok": True})
            return

        if path == "/api/jobs":
            if not notion_token():
                self._json(400, {"error": "请先连接 Notion"})
                return
            if not lark_user_ready():
                self._json(400, {
                    "error": "飞书 user 身份未登录。请在终端运行：lark-cli auth login --domain docs,drive,wiki,base"
                })
                return
            roots = body.get("roots") or []
            if not isinstance(roots, list) or not roots:
                self._json(400, {"error": "至少选择一个页面"})
                return
            target = body.get("target") or "wiki"
            if target not in ("wiki", "drive"):
                self._json(400, {"error": "target 只能是 wiki 或 drive"})
                return
            if target == "drive" and not (body.get("folder_token") or "").strip():
                self._json(400, {"error": "云盘模式需要 folder_token"})
                return
            spec = {
                "roots": roots,
                "target": target,
                "space_id": (body.get("space_id") or "my_library").strip(),
                "parent_node_token": (body.get("parent_node_token") or "").strip() or None,
                "folder_token": (body.get("folder_token") or "").strip() or None,
                "base_url": (body.get("base_url") or "https://feishu.cn").strip(),
                "container_title": (body.get("container_title") or "").strip(),
            }
            cfg = load_config()
            cfg["space_id"] = spec["space_id"]
            cfg["base_url"] = spec["base_url"]
            cfg["target"] = target
            save_config(cfg)
            job = self.store.create(spec)
            self.runner.start(job["id"])
            self._json(200, {"id": job["id"]})
            return

        self._json(404, {"error": "not found"})


def serve(port: int = 0, open_browser: bool = True) -> None:
    store = JobStore(JOBS_DB)
    runner = Runner(store)
    Handler.store = store
    Handler.runner = runner
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"notion2feishu UI: {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
