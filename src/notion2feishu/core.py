#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心逻辑：扫描 Notion 导出、清洗 Markdown、调用 lark-cli。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量与正则
# ---------------------------------------------------------------------------

PAGE_ID_RE = re.compile(r"[ _-]([0-9a-f]{32})$", re.IGNORECASE)
MD_ID_RE = re.compile(r"([0-9a-f]{32})\.md$", re.IGNORECASE)
IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^)\n]+?))\s*\)")
LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(\s*(?:<([^>]+)>|([^)\n]+?))\s*\)")
PLACEHOLDER_RE = re.compile(r"__FEISHU_URL_([0-9a-f]{32})__")
META_RE = re.compile(r"^(Owner|Last edited time|Created time)\s*:", re.IGNORECASE)
ASIDE_RE = re.compile(r"<aside>\s*(.*?)\s*</aside>", re.IGNORECASE | re.DOTALL)

SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|]')

ATTACHMENT_EXTS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".mp3", ".wav", ".m4a", ".mp4", ".mov", ".webm",
    ".key", ".pages", ".numbers", ".epub", ".csv",
}

DEFAULT_SLEEP = 0.8
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Page:
    id: str
    title: str
    md_path: Path                 # 源 md 绝对路径
    rel_md: str                   # 相对导出根目录
    asset_dir: Optional[Path] = None
    parent_id: Optional[str] = None
    children: List[str] = field(default_factory=list)
    order: int = 0
    image_count: int = 0
    link_count: int = 0
    attachment_count: int = 0

    def to_manifest(self, src: Path) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "rel_md": self.rel_md,
            "md": f"pages/{self.id}.md",
            "asset_dir": str(self.asset_dir.relative_to(src)) if self.asset_dir else None,
            "parent_id": self.parent_id,
            "children": self.children,
            "order": self.order,
            "image_count": self.image_count,
            "link_count": self.link_count,
            "attachment_count": self.attachment_count,
        }


# ---------------------------------------------------------------------------
# 发现与解析
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    name = SAFE_NAME_RE.sub("_", name).strip().strip(".")
    return name or "Untitled"


def _page_identity(md_path: Path) -> Tuple[str, str]:
    stem = md_path.stem
    m = PAGE_ID_RE.search(stem)
    if m:
        page_id = m.group(1).lower()
        title = stem[: m.start()].rstrip(" _-").strip()
    else:
        page_id = hashlib.sha1(str(md_path).encode("utf-8")).hexdigest()[:32]
        title = stem.strip()
    return page_id, (title or "Untitled")


def _find_asset_dir(page: Page) -> Optional[Path]:
    base = page.md_path.parent
    candidates = [
        base / page.title,
        base / page.md_path.stem,
        base / (page.title + "_files"),
        base / (page.md_path.stem + "_files"),
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    # 容错：目录名被 Notion 做过字符替换时，按前缀匹配
    prefix = page.title[:20]
    if prefix:
        for cand in sorted(base.iterdir()):
            if cand.is_dir() and cand.name.startswith(prefix):
                return cand
    return None


def discover(src: Path) -> Tuple[Dict[str, Page], List[str]]:
    """扫描导出目录，还原页面树。"""
    src = src.resolve()
    if not src.is_dir():
        raise SystemExit(f"导出目录不存在：{src}")

    md_files = sorted(
        p for p in src.rglob("*.md")
        if not any(part.startswith(".") for part in p.relative_to(src).parts)
    )
    if not md_files:
        raise SystemExit(f"在 {src} 下没有找到 .md 文件")

    pages: Dict[str, Page] = {}
    by_path: Dict[Path, Page] = {}

    for md in md_files:
        page_id, title = _page_identity(md)
        # 极小概率 ID 冲突（同名同 ID），加路径后缀
        while page_id in pages:
            page_id = hashlib.sha1((str(md) + page_id).encode()).hexdigest()[:32]
        page = Page(
            id=page_id,
            title=title,
            md_path=md.resolve(),
            rel_md=str(md.relative_to(src)),
        )
        page.asset_dir = _find_asset_dir(page)
        pages[page_id] = page
        by_path[md.resolve()] = page

    # 父节点：md 的所在目录 == 某个页面的 asset_dir
    dir_to_page: Dict[Path, Page] = {
        p.asset_dir.resolve(): p for p in pages.values() if p.asset_dir
    }
    for page in pages.values():
        parent_dir = page.md_path.parent
        parent = dir_to_page.get(parent_dir)
        if parent is None and parent_dir != src:
            # 容错：向上找最近的 asset_dir
            cur = parent_dir
            while cur != src and cur.parent != cur:
                if cur in dir_to_page:
                    parent = dir_to_page[cur]
                    break
                cur = cur.parent
        if parent and parent.id != page.id:
            page.parent_id = parent.id
            parent.children.append(page.id)

    # 子页面顺序：优先按父页面 md 里的链接顺序（这就是 Notion 里的显示顺序），
    # 没有出现在链接里的再按文件名兜底。
    for page in pages.values():
        ordered: List[str] = []
        try:
            parent_text = page.md_path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            parent_text = ""
        for m in LINK_RE.finditer(parent_text):
            dest = (m.group(2) or m.group(3) or "").strip()
            if not dest or not dest.split("#", 1)[0].lower().endswith(".md"):
                continue
            target = _resolve_target_page(dest, page.md_path.parent, by_path, pages)
            if target and target.parent_id == page.id and target.id not in ordered:
                ordered.append(target.id)
        rest = sorted(
            [cid for cid in page.children if cid not in ordered],
            key=lambda cid: pages[cid].rel_md,
        )
        page.children = ordered + rest

    roots = sorted(
        [p.id for p in pages.values() if not p.parent_id],
        key=lambda pid: pages[pid].rel_md,
    )

    # DFS 编号
    counter = 0
    def walk(pid: str) -> None:
        nonlocal counter
        pages[pid].order = counter
        counter += 1
        for cid in pages[pid].children:
            walk(cid)
    for rid in roots:
        walk(rid)

    return pages, roots


# ---------------------------------------------------------------------------
# Markdown 清洗
# ---------------------------------------------------------------------------

def _strip_metadata(text: str, title: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # 去掉 Notion 自动加的三行元信息（只在文件开头附近找）
    kept: List[str] = []
    for idx, line in enumerate(lines):
        if idx < 15 and META_RE.match(line.strip()):
            continue
        kept.append(line)

    # 去掉与文档标题重复的第一个 H1
    while kept and not kept[0].strip():
        kept.pop(0)
    if kept and kept[0].startswith("# ") and kept[0][2:].strip() == title:
        kept.pop(0)

    text = "\n".join(kept).lstrip("\n")
    return text


def _convert_asides(text: str) -> str:
    """Notion 的 <aside> 提示块 → 飞书引用块（Markdown 不支持 callout）。"""
    def repl(m: re.Match) -> str:
        body = m.group(1).strip("\n")
        out = []
        for ln in body.split("\n"):
            out.append("> " + ln if ln.strip() else ">")
        return "\n".join(out)
    return ASIDE_RE.sub(repl, text)


# ---------------------------------------------------------------------------
# 图片与链接改写
# ---------------------------------------------------------------------------

def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _copy_asset(src_path: Path, page: Page, src_root: Path, out: Path) -> str:
    if page.asset_dir and _is_within(src_path, page.asset_dir):
        rel = src_path.relative_to(page.asset_dir)
    else:
        rel = Path(src_path.name)
    dest = out / "assets" / page.id / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or dest.stat().st_size != src_path.stat().st_size:
        shutil.copy2(src_path, dest)
    return dest.relative_to(out).as_posix()


def rewrite_images(text: str, page: Page, src_root: Path, out: Path) -> Tuple[str, int]:
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        alt = m.group(1) or ""
        dest = (m.group(2) or m.group(3) or "").strip()
        if not dest or dest.startswith(("http://", "https://", "data:")):
            return m.group(0)
        decoded = urllib.parse.unquote(dest.split("#", 1)[0])
        src_path = (page.md_path.parent / decoded).resolve()
        if not src_path.exists() or not src_path.is_file():
            return f"*[图片缺失：{alt or decoded}]*"
        rel = _copy_asset(src_path, page, src_root, out)
        count += 1
        return f"![{alt}](<@./{rel}>)"

    return IMG_RE.sub(repl, text), count


def _resolve_target_page(dest: str, base_dir: Path, by_path: Dict[Path, Page],
                         by_id: Dict[str, Page]) -> Optional[Page]:
    path_part = urllib.parse.unquote(dest.split("#", 1)[0])
    target = (base_dir / path_part).resolve()
    if target in by_path:
        return by_path[target]
    m = MD_ID_RE.search(path_part)
    if m:
        return by_id.get(m.group(1).lower())
    return None


def rewrite_links(text: str, page: Page, by_path: Dict[Path, Page],
                  by_id: Dict[str, Page], src_root: Path, out: Path) -> Tuple[str, int, int]:
    links = 0
    attachments = 0

    def repl(m: re.Match) -> str:
        nonlocal links, attachments
        label = m.group(1)
        dest = (m.group(2) or m.group(3) or "").strip()
        if not dest or dest.startswith(("http://", "https://", "mailto:", "tel:", "#", "data:")):
            return m.group(0)
        path_part = dest.split("#", 1)[0]
        if path_part.lower().endswith(".md"):
            target = _resolve_target_page(dest, page.md_path.parent, by_path, by_id)
            if target is None:
                return m.group(0)
            links += 1
            return f"[{label}](__FEISHU_URL_{target.id}__)"
        suffix = Path(urllib.parse.unquote(path_part)).suffix.lower()
        if suffix in ATTACHMENT_EXTS:
            src_path = (page.md_path.parent / urllib.parse.unquote(path_part)).resolve()
            if src_path.exists() and src_path.is_file():
                rel = _copy_asset(src_path, page, src_root, out)
                attachments += 1
                return f'<source path="@./{rel}"/>'
        return m.group(0)

    return LINK_RE.sub(repl, text), links, attachments


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------

def cmd_plan(args: argparse.Namespace) -> int:
    src = Path(args.src).expanduser()
    pages, roots = discover(src)

    def show(pid: str, depth: int) -> None:
        p = pages[pid]
        print("  " * depth + f"- {p.title}  [{p.id}]  {p.rel_md}")
        for cid in p.children:
            show(cid, depth + 1)

    print(f"共 {len(pages)} 页，{len(roots)} 个根页面：\n")
    for rid in roots:
        show(rid, 0)
    print()
    for pid in roots:
        pass
    image_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".pdf", ".zip"}
    unique_assets = {
        f.resolve()
        for p in pages.values() if p.asset_dir
        for f in p.asset_dir.rglob("*")
        if f.is_file() and f.suffix.lower() in image_suffixes
    }
    print(f"图片/附件文件数：{len(unique_assets)}")

    csvs = sorted(src.rglob("*.csv"))
    if csvs:
        print(f"\nCSV 数据库文件：{len(csvs)}（本工具不自动导入，需单独转多维表格）")
        for c in csvs:
            print(f"  - {c.relative_to(src)}")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    src = Path(args.src).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    pages, roots = discover(src)

    if out.exists() and any(out.iterdir()) and not args.force:
        raise SystemExit(f"输出目录非空：{out}\n加 --force 覆盖，或换一个 --out")

    out.mkdir(parents=True, exist_ok=True)
    (out / "pages").mkdir(exist_ok=True)
    (out / "assets").mkdir(exist_ok=True)

    by_path = {p.md_path: p for p in pages.values()}
    by_id = pages

    total_images = 0
    total_links = 0
    total_attachments = 0
    for page in sorted(pages.values(), key=lambda p: p.order):
        raw = page.md_path.read_text(encoding="utf-8-sig", errors="replace")
        text = _strip_metadata(raw, page.title)
        text = _convert_asides(text)
        text, img_n = rewrite_images(text, page, src, out)
        text, link_n, att_n = rewrite_links(text, page, by_path, by_id, src, out)
        page.image_count = img_n
        page.link_count = link_n
        page.attachment_count = att_n
        total_images += img_n
        total_links += link_n
        total_attachments += att_n
        (out / "pages" / f"{page.id}.md").write_text(text, encoding="utf-8")

    manifest = {
        "version": 1,
        "source": str(src),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": roots,
        "pages": [pages[pid].to_manifest(src) for pid in sorted(pages, key=lambda x: pages[x].order)],
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"已准备：{out}")
    print(f"  页面：{len(pages)}（根 {len(roots)}）")
    print(f"  图片：{total_images} 张，已复制到 assets/")
    print(f"  页间链接：{total_links} 个（已写成占位符，push 时回填）")
    print(f"  附件：{total_attachments} 个（已复制并改写成 <source>）")
    print(f"  清单：{out / 'manifest.json'}")
    return 0


# ---------------------------------------------------------------------------
# lark-cli 调用
# ---------------------------------------------------------------------------

def _run_cli(args: List[str], cwd: Optional[Path] = None,
             retries: int = MAX_RETRIES, quiet: bool = False) -> dict:
    cmd = ["lark-cli"] + args
    last_err = ""
    for attempt in range(1, retries + 1):
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode == 0:
            raw = proc.stdout.strip()
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {"_raw": raw}
        last_err = (proc.stdout + "\n" + proc.stderr).strip()
        retryable = any(x in last_err.lower() for x in ("rate", "429", "500", "502", "503", "timeout"))
        if attempt < retries and retryable:
            time.sleep(2 ** attempt)
            continue
        break
    raise RuntimeError(f"lark-cli 失败：{' '.join(cmd)}\n{last_err}")


def _deep_find(obj, keys: Iterable[str]):
    """在嵌套 JSON 中找第一个 key 的值。"""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return obj[k]
        for v in obj.values():
            got = _deep_find(v, keys)
            if got not in (None, ""):
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _deep_find(v, keys)
            if got not in (None, ""):
                return got
    return None


def _identity_flag(identity: str) -> List[str]:
    return ["--as", "bot"] if identity == "bot" else ["--as", "user"]


def load_manifest(work: Path) -> dict:
    return json.loads((work / "manifest.json").read_text(encoding="utf-8"))


def _load_state(work: Path) -> dict:
    state_path = work / "state.json"
    if state_path.exists():
        return json.loads(state_path.read_text(encoding="utf-8"))
    return {"target": None, "pages": {}}


def _save_state(work: Path, state: dict) -> None:
    (work / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _flatten(manifest: dict) -> List[dict]:
    pages = {p["id"]: p for p in manifest["pages"]}
    ordered: List[dict] = []

    def walk(pid: str) -> None:
        ordered.append(pages[pid])
        for cid in pages[pid]["children"]:
            walk(cid)

    for rid in manifest["roots"]:
        walk(rid)
    return ordered


def _replace_placeholders(text: str, state: dict) -> Tuple[str, int]:
    missing = 0

    def repl(m: re.Match) -> str:
        nonlocal missing
        pid = m.group(1)
        info = state["pages"].get(pid) or {}
        url = info.get("url")
        if url:
            return url
        missing += 1
        return "https://feishu.cn"

    return PLACEHOLDER_RE.sub(repl, text), missing


def cmd_push(args: argparse.Namespace) -> int:
    work = Path(args.work).expanduser().resolve()
    manifest_path = work / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"找不到 {manifest_path}，先运行 prepare")

    if not args.dry_run and not shutil.which("lark-cli"):
        raise SystemExit("找不到 lark-cli，请先安装并完成 auth login")

    if not args.dry_run:
        try:
            status = json.loads(subprocess.run(
                ["lark-cli", "auth", "status"], capture_output=True, text=True
            ).stdout or "{}")
            ident = (status.get("identities") or {}).get(args.identity) or {}
            if not ident.get("available"):
                hint = "lark-cli auth login --domain docs,drive,wiki,base" if args.identity == "user" else "检查 bot 配置"
                raise SystemExit(
                    f"当前 {args.identity} 身份不可用，先执行：{hint}\n"
                    f"可用 `python3 notion2feishu.py doctor` 查看详情。"
                )
        except json.JSONDecodeError:
            pass

    manifest = load_manifest(work)
    pages = _flatten(manifest)
    pages_by_id = {p["id"]: p for p in pages}

    if args.limit:
        pages = pages[: args.limit]

    target_sig = {
        "target": args.target,
        "space_id": args.space_id,
        "parent_node_token": args.parent_node_token,
        "folder_token": args.folder_token,
        "identity": args.identity,
    }

    state = _load_state(work)
    if state.get("target") and state["target"] != target_sig:
        if not args.force_state:
            raise SystemExit(
                "state.json 记录的迁移目标与本次参数不同。\n"
                "换 --work 目录，或加 --force-state 清空状态重跑。"
            )
        state = {"target": target_sig, "pages": {}}
    state["target"] = target_sig
    state.setdefault("pages", {})

    if args.target == "wiki" and not args.space_id:
        raise SystemExit("--target wiki 需要 --space-id（个人文档库用 my_library）")
    if args.target == "drive" and not args.folder_token:
        raise SystemExit("--target drive 需要 --folder-token")

    if args.base_url.rstrip("/") == "https://feishu.cn":
        print(
            "[提醒] 没有指定 --base-url，页间链接会使用通用域名 feishu.cn；"
            "建议传你的租户域名，如 https://acme.feishu.cn",
            file=sys.stderr,
        )

    identity = _identity_flag(args.identity)
    sleep = args.sleep

    def lark(command: List[str]) -> dict:
        if args.dry_run:
            print("  $ " + shlex.join(command))
            return {}

        res = _run_cli(command, cwd=work, quiet=args.quiet)
        if sleep:
            time.sleep(sleep)
        return res

    # ---------- 阶段 1：建空节点 / 空文档，保留层级 ----------
    created = 0
    failed: List[Tuple[str, str]] = []
    dry_tokens: Dict[str, dict] = {}  # dry-run 时用占位 token，避免污染 state.json

    for page_no, page in enumerate(pages, 1):
        pid = page["id"]
        info = state["pages"].setdefault(pid, {})
        if info.get("created") and args.resume:
            continue
        if not args.dry_run:
            print(f"[建节点 {page_no}/{len(pages)}] {page['title']}")
        try:
            if args.target == "wiki":
                parent = args.parent_node_token
                if page["parent_id"]:
                    if args.dry_run:
                        parent = (dry_tokens.get(page["parent_id"]) or {}).get("node_token")
                    else:
                        parent = state["pages"].get(page["parent_id"], {}).get("node_token")
                    if not parent:
                        raise RuntimeError(f"父页面 {page['parent_id']} 尚未创建")
                cmd = [
                    "wiki", "+node-create",
                    "--space-id", args.space_id,
                    "--title", page["title"],
                    "--obj-type", "docx",
                    "--format", "json",
                ] + identity
                if parent:
                    cmd += ["--parent-node-token", parent]
                if args.dry_run:
                    lark(cmd)
                    dry_tokens[pid] = {
                        "node_token": f"<node_token:{pid}>",
                        "obj_token": f"<obj_token:{pid}>",
                        "url": f"{args.base_url.rstrip('/')}/wiki/<node_token:{pid}>",
                    }
                    created += 1
                    continue
                res = lark(cmd)
                node_token = _deep_find(res, ["node_token"])
                obj_token = _deep_find(res, ["obj_token"])
                url = _deep_find(res, ["url", "node_url"])
                if not node_token or not obj_token:
                    raise RuntimeError(f"无法解析 node-create 返回：{json.dumps(res, ensure_ascii=False)[:500]}")
                info.update({
                    "created": True,
                    "node_token": node_token,
                    "obj_token": obj_token,
                    "url": url or f"{args.base_url.rstrip('/')}/wiki/{node_token}",
                    "title": page["title"],
                })
            else:
                parent_folder = args.folder_token
                if page["parent_id"]:
                    if args.dry_run:
                        parent_folder = (dry_tokens.get(page["parent_id"]) or {}).get("folder_token")
                    else:
                        parent_folder = state["pages"].get(page["parent_id"], {}).get("folder_token")
                    if not parent_folder:
                        raise RuntimeError(f"父页面 {page['parent_id']} 尚未创建")
                folder_cmd = [
                    "drive", "+create-folder",
                    "--name", page["title"],
                    "--folder-token", parent_folder,
                    "--format", "json",
                ] + identity
                if args.dry_run:
                    lark(folder_cmd)
                    folder_token = f"<folder_token:{pid}>"
                    doc_cmd = [
                        "docs", "+create",
                        "--title", page["title"],
                        "--parent-token", folder_token,
                        "--format", "json",
                    ] + identity
                    lark(doc_cmd)
                    dry_tokens[pid] = {
                        "folder_token": folder_token,
                        "obj_token": f"<obj_token:{pid}>",
                        "url": f"{args.base_url.rstrip('/')}/docx/<obj_token:{pid}>",
                    }
                    created += 1
                    continue
                fres = lark(folder_cmd)
                folder_token = _deep_find(fres, ["token", "folder_token"])
                if not folder_token:
                    raise RuntimeError(f"无法解析 create-folder 返回：{json.dumps(fres, ensure_ascii=False)[:500]}")
                doc_cmd = [
                    "docs", "+create",
                    "--title", page["title"],
                    "--parent-token", folder_token,
                    "--format", "json",
                ] + identity
                dres = lark(doc_cmd)
                obj_token = _deep_find(dres, ["document_id", "obj_token"])
                url = _deep_find(dres, ["url"])
                if not obj_token:
                    raise RuntimeError(f"无法解析 docs +create 返回：{json.dumps(dres, ensure_ascii=False)[:500]}")
                info.update({
                    "created": True,
                    "folder_token": folder_token,
                    "obj_token": obj_token,
                    "url": url or f"{args.base_url.rstrip('/')}/docx/{obj_token}",
                    "title": page["title"],
                })
            created += 1
            _save_state(work, state)
        except Exception as exc:  # noqa: BLE001
            failed.append((pid, str(exc)))
            print(f"[失败] 建节点 {page['title']}: {exc}", file=sys.stderr)

    # ---------- 阶段 2：回填链接 + 写正文 ----------
    filled = 0
    if not args.skip_fill:
        for page_no, page in enumerate(pages, 1):
            pid = page["id"]
            info = state["pages"].get(pid, {})
            obj_token = (dry_tokens.get(pid) or {}).get("obj_token") if args.dry_run else info.get("obj_token")
            if not obj_token:
                continue
            if info.get("filled") and args.resume:
                continue
            try:
                if not args.dry_run:
                    print(f"[写正文 {page_no}/{len(pages)}] {page['title']}")
                src_md = work / page["md"]
                text = src_md.read_text(encoding="utf-8")
                url_state = {"pages": {**state["pages"], **dry_tokens}} if args.dry_run else state
                text, missing = _replace_placeholders(text, url_state)
                if missing:
                    print(f"[提醒] {page['title']} 有 {missing} 个链接未解析，已用占位 URL", file=sys.stderr)
                final_path = work / f"{pid}.final.md"
                final_path.write_text(text, encoding="utf-8")

                cmd = [
                    "docs", "+update",
                    "--doc", obj_token,
                    "--command", "append",
                    "--doc-format", "markdown",
                    "--content", f"@{pid}.final.md",
                    "--format", "json",
                ] + identity
                if args.dry_run:
                    lark(cmd)
                    info["filled"] = True
                    filled += 1
                    continue
                lark(cmd)
                info["filled"] = True
                filled += 1
                _save_state(work, state)
            except Exception as exc:  # noqa: BLE001
                failed.append((pid, str(exc)))
                print(f"[失败] 写正文 {page['title']}: {exc}", file=sys.stderr)

    if not args.dry_run:
        _save_state(work, state)

    print("\n=== 结果 ===")
    print(f"建节点：{created}，写正文：{filled}，失败：{len(failed)}")
    for pid, err in failed:
        print(f"  - {pages_by_id.get(pid, {}).get('title', pid)}: {err}")
    if args.dry_run:
        print("\n这是 dry-run，没有真正写入飞书。去掉 --dry-run 再执行。")
    else:
        print("\n根页面链接：")
        for rid in manifest.get("roots", []):
            info = state["pages"].get(rid, {})
            if info.get("url"):
                print(f"  {info.get('title', rid)}: {info['url']}")
        print(f"状态文件：{work / 'state.json'}（--resume 可续跑）")
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    print("=== lark-cli auth status ===")
    proc = subprocess.run(["lark-cli", "auth", "status"], capture_output=True, text=True)
    print(proc.stdout.strip() or proc.stderr.strip())

    print("\n=== 用 user 身份读知识库空间 ===")
    proc = subprocess.run(
        ["lark-cli", "wiki", "+space-list", "--as", "user", "--format", "json"],
        capture_output=True, text=True,
    )
    print((proc.stdout or proc.stderr).strip()[:1500])

    print("\n提示：")
    print("- 如果这里报 21001 / access denied，说明当前身份没有 wiki 权限。")
    print("- 如果授权页显示「当前无需开通或授权任何权限」，通常是 lark-cli 无法写 scope 缓存。")
    print("  在普通终端（不要在沙箱里）重跑：")
    print("  lark-cli auth login --domain docs,drive,wiki,base")
    print("- bot 身份通常不能建 wiki 节点，只能作为 drive 的降级方案。")
    return 0


