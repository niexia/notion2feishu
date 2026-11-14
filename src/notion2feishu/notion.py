#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Notion API 直连：搜索工作区、拉取页面 Markdown、下载资源、还原页面树。

依赖 Notion API 2026-03-11 的 page markdown 接口：
  GET /v1/pages/{page_id}/markdown?markdown_version=v2

不依赖第三方 SDK，只用标准库。生成出来的目录结构与 Notion 官方
Markdown & CSV 导出一致，可以直接交给 core.prepare 继续处理。
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

NOTION_VERSION = "2026-03-11"
API_BASE = "https://api.notion.com/v1"
USER_AGENT = "notion2feishu/0.2"

PAGE_TAG_RE = re.compile(r'<page\b[^>]*\burl="([^"]+)"[^>]*>(.*?)</page>', re.S | re.I)
PAGE_SELF_RE = re.compile(r'<page\b[^>]*\burl="([^"]+)"[^>]*/>', re.S | re.I)
DB_TAG_RE = re.compile(r'<database\b[^>]*\burl="([^"]+)"[^>]*>(.*?)</database>', re.S | re.I)
DB_SELF_RE = re.compile(r'<database\b[^>]*\burl="([^"]+)"[^>]*/>', re.S | re.I)
IMG_RE = re.compile(r'!\[([^\]]*)\]\(\s*<?(https?://[^)>\s]+)>?\s*\)', re.I)
FILE_TAG_RE = re.compile(r'<file\b[^>]*\bsource="([^"]+)"[^>]*>(.*?)</file>', re.S | re.I)
PDF_TAG_RE = re.compile(r'<pdf\b[^>]*\bsource="([^"]+)"[^>]*>(.*?)</pdf>', re.S | re.I)
FENCE_RE = re.compile(r"```.*?```", re.S)
TAG_RE = re.compile(r"<[^>]+>")


class NotionError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


def page_id_from(value: str) -> str:
    """从 Notion URL / 裸 ID 中提取标准 UUID（带连字符）。"""
    raw = urllib.parse.unquote((value or "").strip())
    raw = raw.split("#", 1)[0].split("?", 1)[0]

    uuid_m = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        raw, re.IGNORECASE,
    )
    if uuid_m:
        return str(uuid.UUID(uuid_m.group(1).lower()))

    hex_m = re.search(r"(?:^|[-/])([0-9a-f]{32})(?![0-9a-f])", raw, re.IGNORECASE)
    if not hex_m:
        hex_m = re.search(r"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])", raw, re.IGNORECASE)
    if not hex_m:
        raise NotionError(f"无法从 {value!r} 中解析 Notion page_id")
    return str(uuid.UUID(hex_m.group(1).lower()))


def _plain(text: str) -> str:
    return html.unescape(TAG_RE.sub("", text or "")).strip()


def page_title(page: dict) -> str:
    props = page.get("properties") or {}
    for prop in props.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            parts = prop.get("title") or []
            title = "".join(p.get("plain_text", "") for p in parts if isinstance(p, dict))
            if title.strip():
                return title.strip()
    if isinstance(page.get("title"), str) and page["title"].strip():
        return page["title"].strip()
    return ""


def parse_child_pages(markdown: str) -> List[dict]:
    """解析 <page url="...">标题</page>（含自闭合形式）。"""
    found: List[dict] = []
    seen = set()

    def add(url: str, title: str) -> None:
        try:
            pid = page_id_from(url)
        except NotionError:
            return
        if pid in seen:
            return
        seen.add(pid)
        found.append({"id": pid, "title": _plain(title) or "Untitled", "url": url})

    for m in PAGE_TAG_RE.finditer(markdown):
        add(m.group(1), m.group(2))
    for m in PAGE_SELF_RE.finditer(markdown):
        add(m.group(1), "")
    return found


def parse_databases(markdown: str) -> List[dict]:
    found: List[dict] = []
    seen = set()

    def add(url: str, title: str) -> None:
        if url in seen:
            return
        seen.add(url)
        found.append({"title": _plain(title) or "Untitled database", "url": url})

    for m in DB_TAG_RE.finditer(markdown):
        add(m.group(1), m.group(2))
    for m in DB_SELF_RE.finditer(markdown):
        add(m.group(1), "")
    return found


class NotionClient:
    def __init__(
        self,
        token: str,
        version: str = NOTION_VERSION,
        timeout: int = 60,
        min_interval: float = 0.34,
    ):
        if not token:
            raise NotionError("缺少 Notion Integration Token")
        self.token = token
        self.version = version
        self.timeout = timeout
        self.min_interval = min_interval
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: Optional[dict] = None,
        retries: int = 5,
    ) -> dict:
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.version,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        last_error: Optional[Exception] = None
        for attempt in range(retries + 1):
            self._throttle()
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                payload = {}
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    pass
                code = payload.get("code")
                message = payload.get("message") or raw[:300]
                status = exc.code
                if status in (429, 529) or 500 <= status < 600:
                    retry_after = exc.headers.get("Retry-After", "1")
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = 1.0
                    last_error = NotionError(message, status, code)
                    time.sleep(max(1.0, delay) * (attempt + 1))
                    continue
                if status == 401:
                    raise NotionError(
                        "Notion token 无效或已过期。请重新复制 Internal Integration Token。",
                        status, code,
                    ) from exc
                if status == 403:
                    raise NotionError(
                        "Notion 集成没有该页面的 read_content 权限；请把 integration 连接到根页面。",
                        status, code,
                    ) from exc
                if status == 404:
                    raise NotionError(
                        "Notion 页面不存在，或 integration 没有被连接到该页面。",
                        status, code,
                    ) from exc
                raise NotionError(message or f"Notion API {status}", status, code) from exc
            except urllib.error.URLError as exc:
                last_error = NotionError(f"Notion 网络错误：{exc.reason}")
                time.sleep(2 ** attempt)
                continue
        raise last_error or NotionError("Notion API 请求失败")

    def validate(self) -> dict:
        try:
            return self._request("GET", "/users/me")
        except NotionError as exc:
            # 少数 integration 不能读 /users/me，但 search 可用；用 search 做兜底验证。
            if exc.status in (403, 404):
                self._request("POST", "/search", body={"page_size": 1})
                return {}
            raise

    def get_page(self, page_id: str) -> dict:
        return self._request("GET", f"/pages/{page_id}")

    def get_markdown(self, page_id: str) -> dict:
        return self._request(
            "GET", f"/pages/{page_id}/markdown",
            params={"markdown_version": "v2"},
        )

    def get_block_markdown(self, block_id: str) -> dict:
        return self.get_markdown(block_id)

    def search_pages(self, max_pages: int = 2000) -> List[dict]:
        results: List[dict] = []
        cursor = None
        while len(results) < max_pages:
            body = {
                "filter": {"value": "page", "property": "object"},
                "page_size": min(100, max_pages - len(results)),
            }
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", "/search", body=body)
            batch = data.get("results") or []
            results.extend(batch)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return results

    def search_databases(self, max_items: int = 500) -> List[dict]:
        results: List[dict] = []
        cursor = None
        while len(results) < max_items:
            body = {
                "filter": {"value": "database", "property": "object"},
                "page_size": min(100, max_items - len(results)),
            }
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", "/search", body=body)
            results.extend(data.get("results") or [])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return results

    def download(self, url: str, dest_dir: Path, prefix: str, max_bytes: int = 80 * 1024 * 1024) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            ctype = (resp.headers.get_content_type() or "").lower()
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise NotionError(f"资源超过 {max_bytes // 1024 // 1024}MB：{url[:120]}")

        name = os.path.basename(urllib.parse.urlparse(url).path)
        name = urllib.parse.unquote(name) or ""
        ext = Path(name).suffix.lower()
        if not ext or len(ext) > 8:
            ext = mimetypes.guess_extension(ctype or "") or ".bin"
        safe = re.sub(r'[^\w.\-]+', "_", prefix)[:80] or "asset"
        candidate = dest_dir / f"{safe}{ext}"
        n = 2
        while candidate.exists():
            candidate = dest_dir / f"{safe}-{n}{ext}"
            n += 1
        candidate.write_bytes(data)
        return candidate


def _protect_fences(text: str) -> Tuple[str, List[str]]:
    blocks: List[str] = []

    def repl(m: re.Match) -> str:
        blocks.append(m.group(0))
        return f"\x00N2F_FENCE_{len(blocks) - 1}\x00"

    return FENCE_RE.sub(repl, text), blocks


def _restore_fences(text: str, blocks: List[str]) -> str:
    for i, block in enumerate(blocks):
        text = text.replace(f"\x00N2F_FENCE_{i}\x00", block)
    return text


def _quote_block(body: str) -> str:
    lines = body.strip("\n").split("\n")
    return "\n".join("> " + line if line.strip() else ">" for line in lines)


def normalize_markdown(markdown: str, child_links: Dict[str, str], databases: List[dict]) -> str:
    """把 Notion-flavored Markdown 转成飞书能接受的 Markdown。

    child_links: page_id -> 相对当前 md 的 .md 路径（已 URL 编码）。
    """
    text, fences = _protect_fences(markdown)

    def page_link(url: str, title: str) -> str:
        title = _plain(title)
        try:
            pid = page_id_from(url)
        except NotionError:
            return f"[{title or url}]({url})"
        link = child_links.get(pid)
        if link:
            return f"[{title or '子页面'}]({link})"
        return f"[{title or url}]({url})"

    text = PAGE_TAG_RE.sub(lambda m: page_link(m.group(1), m.group(2)), text)
    text = PAGE_SELF_RE.sub(lambda m: page_link(m.group(1), ""), text)

    text = DB_TAG_RE.sub(lambda m: f"[{_plain(m.group(2)) or 'database'}]({m.group(1)})", text)
    text = DB_SELF_RE.sub(lambda m: f"[database]({m.group(1)})", text)

    text = re.sub(r"<callout\b[^>]*>\s*(.*?)\s*</callout>", lambda m: _quote_block(m.group(1)), text, flags=re.S | re.I)
    text = re.sub(r"</?callout\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</?toggle\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</?columns?\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</?synced-block\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<table-of-contents\s*/>", "", text, flags=re.I)
    text = re.sub(r"<bookmark\b[^>]*\bsource=\"([^\"]+)\"[^>]*/>", lambda m: f"[{m.group(1)}]({m.group(1)})", text, flags=re.I)
    text = re.sub(r"<embed\b[^>]*\bsource=\"([^\"]+)\"[^>]*/>", lambda m: f"[{m.group(1)}]({m.group(1)})", text, flags=re.I)
    text = re.sub(
        r"<(video|audio)\b[^>]*\bsource=\"([^\"]+)\"[^>]*>(.*?)</\1>",
        lambda m: f"[{_plain(m.group(3)) or m.group(2)}]({m.group(2)})",
        text, flags=re.S | re.I,
    )
    text = re.sub(
        r"<unknown\b[^>]*\balt=\"([^\"]*)\"[^>]*/>",
        lambda m: f"*[不支持的内容：{_plain(m.group(1)) or 'unknown'}]*",
        text, flags=re.I,
    )
    text = re.sub(r"<unknown\b[^>]*/>", "*[不支持的内容]*", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</?meeting-notes[^>]*>", "", text, flags=re.I)
    text = re.sub(r"</?transcript[^>]*>", "", text, flags=re.I)

    return _restore_fences(text, fences)


def _unique_dir(parent: Path, title: str, page_id: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|]+', "_", title).strip().strip(".") or "Untitled"
    candidate = parent / safe
    if not candidate.exists():
        return candidate
    candidate = parent / f"{safe} ({page_id[:6]})"
    if not candidate.exists():
        return candidate
    n = 2
    while (parent / f"{safe} ({page_id[:6]}-{n})").exists():
        n += 1
    return parent / f"{safe} ({page_id[:6]}-{n})"


def _rel_link(path: Path, from_dir: Path) -> str:
    rel = os.path.relpath(str(path), str(from_dir)).replace(os.sep, "/")
    return urllib.parse.quote(rel, safe="/.@_-")


def pull_page_tree(
    client: NotionClient,
    root_ids: List[str],
    out: Path,
    progress: Optional[Callable[[int, Optional[int], str], None]] = None,
    limit: Optional[int] = None,
) -> dict:
    out = out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    visited: Dict[str, Tuple[str, Path]] = {}
    report: dict = {"pages": [], "databases": [], "errors": [], "images": 0, "files": 0}
    state = {"count": 0, "limit_hit": False}

    def notify(message: str) -> None:
        if progress:
            progress(state["count"], None, message)

    def visit(pid: str, title_hint: str, parent_dir: Path) -> Tuple[str, Path]:
        if pid in visited:
            return visited[pid]
        if limit is not None and state["count"] >= limit:
            state["limit_hit"] = True
            raise NotionError(f"已达到 --limit {limit}，停止拉取")

        page_obj = client.get_page(pid)
        title = page_title(page_obj) or title_hint or "Untitled"
        page_dir = _unique_dir(parent_dir, title, pid)
        page_dir.mkdir(parents=True, exist_ok=True)
        compact_id = pid.replace("-", "")
        safe_md_title = re.sub(r'[\\/:*?"<>|]+', "_", title)[:120] or "Untitled"
        md_path = parent_dir / f"{safe_md_title} {compact_id}.md"

        notify(f"读取页面：{title}")
        resp = client.get_markdown(pid)
        markdown = resp.get("markdown") or ""
        warnings = []
        if resp.get("truncated"):
            warnings.append("页面超过 Notion 单次返回上限，已尝试补拉 unknown blocks")
        for block_id in (resp.get("unknown_block_ids") or [])[:50]:
            try:
                sub = client.get_block_markdown(block_id)
                if sub.get("markdown"):
                    markdown += "\n\n" + sub["markdown"]
            except NotionError as exc:
                warnings.append(f"无法补拉 block {block_id}: {exc}")

        # Notion 的图片是预签名 URL，先下载，避免递归子页面太久导致 URL 过期。
        markdown = _download_assets(client, markdown, page_dir, md_path.parent, report, notify)

        child_refs = parse_child_pages(markdown)
        db_refs = parse_databases(markdown)
        child_links: Dict[str, str] = {}
        child_titles: Dict[str, str] = {}

        for ref in child_refs:
            try:
                child_title, child_md = visit(ref["id"], ref["title"], page_dir)
                child_titles[ref["id"]] = child_title
                child_links[ref["id"]] = _rel_link(child_md, md_path.parent)
            except NotionError as exc:
                if state["limit_hit"]:
                    break
                report["errors"].append({
                    "page": ref.get("title") or ref["id"],
                    "page_id": ref["id"],
                    "error": str(exc),
                })
                notify(f"跳过子页面 {ref.get('title') or ref['id']}: {exc}")

        for db in db_refs:
            report["databases"].append({
                "title": db["title"],
                "url": db["url"],
                "parent": title,
            })

        markdown = normalize_markdown(markdown, child_links, db_refs)
        md_path.write_text(markdown, encoding="utf-8")

        visited[pid] = (title, md_path)
        state["count"] += 1
        report["pages"].append({
            "id": pid,
            "title": title,
            "md": _rel_link(md_path, out),
            "warnings": warnings,
        })
        notify(f"完成：{title}（{state['count']} 页）")
        return title, md_path

    for rid in root_ids:
        try:
            visit(rid, "", out)
        except NotionError as exc:
            if state["limit_hit"]:
                break
            report["errors"].append({"page": rid, "page_id": rid, "error": str(exc)})
            notify(f"跳过根页面 {rid}: {exc}")

    report["total_pages"] = state["count"]
    report["limit_hit"] = state["limit_hit"]
    return report


def _download_assets(
    client: NotionClient,
    markdown: str,
    page_dir: Path,
    md_dir: Path,
    report: dict,
    notify: Callable[[str], None],
) -> str:
    counter = {"n": 0}

    def img_repl(m: re.Match) -> str:
        alt, url = m.group(1), html.unescape(m.group(2))
        counter["n"] += 1
        try:
            path = client.download(url, page_dir, f"image-{counter['n']:03d}")
            report["images"] += 1
            return f"![{alt}](<{_rel_link(path, md_dir)}>)"
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"page": page_dir.name, "error": f"图片下载失败：{exc}"})
            return f"*[图片下载失败：{alt or 'image'}]*"

    # 只处理不在 code fence 里的图片：先保护 fence
    text, fences = _protect_fences(markdown)
    text = IMG_RE.sub(img_repl, text)

    def file_repl(m: re.Match) -> str:
        url, caption = html.unescape(m.group(1)), _plain(m.group(2))
        counter["n"] += 1
        try:
            path = client.download(url, page_dir, f"file-{counter['n']:03d}")
            report["files"] += 1
            return f"[{caption or '附件'}](<{_rel_link(path, md_dir)}>)"
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"page": page_dir.name, "error": f"附件下载失败：{exc}"})
            return f"[{caption or '附件'}]({url})"

    text = FILE_TAG_RE.sub(file_repl, text)
    text = PDF_TAG_RE.sub(file_repl, text)
    return _restore_fences(text, fences)


def scan_workspace(client: NotionClient) -> dict:
    """用 search 找到 integration 可见的页面，并还原成森林。"""
    raw = client.search_pages()
    by_id: Dict[str, dict] = {}
    database_rows = 0
    for p in raw:
        pid = p.get("id")
        if not pid:
            continue
        parent = p.get("parent") or {}
        if parent.get("type") == "database_id":
            # 数据库行不是普通页面，页面迁移阶段不在这里展开
            database_rows += 1
            continue
        by_id[pid] = {
            "id": pid,
            "title": page_title(p) or "Untitled",
            "last_edited_time": p.get("last_edited_time"),
            "parent": p.get("parent") or {},
            "children": [],
        }

    roots: List[str] = []
    for pid, node in by_id.items():
        parent = node["parent"]
        parent_id = parent.get("page_id")
        if parent.get("type") == "page_id" and parent_id in by_id:
            by_id[parent_id]["children"].append(pid)
        else:
            roots.append(pid)

    def sort_key(pid: str):
        return (by_id[pid]["title"] or "").lower()

    for node in by_id.values():
        node["children"].sort(key=sort_key)
    roots.sort(key=sort_key)

    def build(pid: str, seen: set) -> dict:
        node = by_id[pid]
        seen = set(seen)
        seen.add(pid)
        children = [build(c, seen) for c in node["children"] if c not in seen]
        return {
            "id": node["id"],
            "title": node["title"],
            "last_edited_time": node["last_edited_time"],
            "children": children,
            "count": 1 + sum(c["count"] for c in children),
        }

    tree = [build(pid, set()) for pid in roots]
    return {
        "roots": tree,
        "total_pages": len(by_id),
        "database_rows": database_rows,
        "search_returned": len(raw),
    }


def token_from_env_or_arg(value: Optional[str]) -> str:
    token = (value or os.environ.get("NOTION_TOKEN") or "").strip()
    if not token:
        raise NotionError(
            "缺少 Notion token。设置环境变量 NOTION_TOKEN，或传 --notion-token。\n"
            "创建方式：https://www.notion.so/my-integrations"
        )
    return token
