#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口：默认走 migrate 向导，尽量一条命令跑完。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import List, NoReturn, Optional, Tuple

from . import core, notion

try:
    from . import __version__
except ImportError:  # pragma: no cover
    __version__ = "0.1.0"

APP_HOME = Path(os.environ.get("N2F_HOME", str(Path.home() / ".notion2feishu")))
WORK_ROOT = APP_HOME / "work"
CACHE_ROOT = APP_HOME / "cache"
CONFIG_PATH = APP_HOME / "config.json"

KNOWN_COMMANDS = {"migrate", "prepare", "push", "plan", "scan", "pull", "ui", "doctor", "version"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def log(msg: str = "") -> None:
    print(msg, flush=True)


def fail(msg: str, code: int = 2) -> NoReturn:
    print(f"错误：{msg}", file=sys.stderr)
    raise SystemExit(code)


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # 只读环境/沙箱里不阻塞主流程


def normalize_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return "https://feishu.cn"
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def auth_status() -> dict:
    try:
        proc = subprocess.run(
            ["lark-cli", "auth", "status"], capture_output=True, text=True, timeout=30
        )
        raw = proc.stdout or ""
        start = raw.find("{")
        if start < 0:
            return {}
        return json.loads(raw[start:])
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def ensure_lark() -> None:
    if not shutil.which("lark-cli"):
        fail(
            "找不到 lark-cli。请先安装并登录：\n"
            "  1) 安装方式见 https://github.com/larksuite/lark-cli（或你现有的 lark-cli 安装方式）\n"
            "  2) lark-cli auth login --domain docs,drive,wiki,base"
        )


def ensure_auth(identity: str, auto_login: bool, yes: bool) -> None:
    ensure_lark()
    status = auth_status()
    ident = (status.get("identities") or {}).get(identity) or {}
    if ident.get("available"):
        return

    if identity == "bot":
        fail("bot 身份不可用，请检查 lark-cli 配置。")

    log("检测到 lark-cli 还没有 user 身份。迁移需要把文档建在你名下，必须先授权。")
    if not auto_login or not sys.stdin.isatty():
        fail(
            "请先在终端执行：\n"
            "  lark-cli auth login --domain docs,drive,wiki,base\n"
            "然后重新运行本命令。"
        )

    if not yes:
        try:
            ans = input("现在运行 lark-cli auth login？[Y/n] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans in ("n", "no"):
            fail("已取消。完成授权后再运行。")

    rc = subprocess.run(
        ["lark-cli", "auth", "login", "--domain", "docs,drive,wiki,base"]
    ).returncode
    if rc != 0:
        fail("授权命令失败，请检查 lark-cli 的输出。")

    status = auth_status()
    ident = (status.get("identities") or {}).get("user") or {}
    if not ident.get("available"):
        fail(
            "授权后仍然没有 user 身份。若授权页显示「当前无需开通或授权任何权限」，\n"
            "通常是 lark-cli 无法写 scope 缓存，或租户不允许第三方授权；\n"
            "请查看 README 的「授权走不通时」一节。"
        )


# ---------------------------------------------------------------------------
# 源目录：自动找 / 解压 / 规范化
# ---------------------------------------------------------------------------

def find_latest_export() -> Optional[Path]:
    bases = [Path.cwd(), Path.home() / "Downloads", Path.home() / "Desktop"]
    seen: set = set()
    candidates: List[Path] = []

    for base in bases:
        if not base.is_dir():
            continue
        for pattern in ("*ExportBlock*", "*Export*.zip"):
            for p in base.glob(pattern):
                if p in seen:
                    continue
                seen.add(p)
                if p.is_dir() or p.suffix.lower() == ".zip":
                    candidates.append(p)

    if not candidates:
        return None

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    return max(candidates, key=mtime)


def extract_zip(zip_path: Path) -> Path:
    zip_path = zip_path.expanduser().resolve()
    st = zip_path.stat()
    key = hashlib.sha1(
        f"{zip_path}:{st.st_size}:{st.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:16]
    dest = CACHE_ROOT / "src" / key

    if dest.exists() and any(dest.rglob("*.md")):
        return normalize_export_dir(dest)

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename
                if name.startswith("__MACOSX/") or name.startswith("._"):
                    continue
                target = (dest / name).resolve()
                if not is_within(target, dest.resolve()):
                    fail(f"zip 内路径越界，拒绝解压：{name}")
                if info.is_dir():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile:
        fail(f"不是有效的 zip：{zip_path}")

    return normalize_export_dir(dest)


def normalize_export_dir(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.exists():
        fail(f"路径不存在：{path}")
    if path.is_file():
        fail(f"期望一个目录或 .zip，实际是文件：{path}")

    # 常见情况：解压后多了一层目录，或用户指到了导出目录的父目录
    if not any(path.glob("*.md")):
        subdirs = [
            d for d in path.iterdir()
            if d.is_dir() and not d.name.startswith(("__", "."))
        ]
        with_md = [d for d in subdirs if any(d.glob("*.md"))]
        if len(with_md) == 1:
            path = with_md[0].resolve()

    if not any(path.rglob("*.md")):
        fail(
            f"这个目录不像 Notion 导出（没有找到 .md）：{path}\n"
            "请指向解压后的导出目录，或直接传导出的 .zip。"
        )
    return path


def resolve_source(src_arg: Optional[str], quiet: bool = False) -> Path:
    if src_arg:
        p = Path(src_arg).expanduser()
        if not p.exists():
            fail(f"找不到源：{p}")
        if p.is_file() and p.suffix.lower() == ".zip":
            p = extract_zip(p)
        return normalize_export_dir(p)

    p = find_latest_export()
    if not p:
        fail(
            "没有找到 Notion 导出。请先导出，然后任选一种方式：\n"
            "  notion2feishu migrate --src ~/Downloads/你的导出.zip\n"
            "  notion2feishu migrate --src ~/Downloads/解压后的目录"
        )
    if not quiet:
        log(f"未指定 --src，使用最近的导出：{p}")
    if p.is_file() and p.suffix.lower() == ".zip":
        p = extract_zip(p)
    return normalize_export_dir(p)


def default_work(src: Path) -> Path:
    key = hashlib.sha1(str(src.resolve()).encode("utf-8")).hexdigest()[:12]
    return WORK_ROOT / key


# ---------------------------------------------------------------------------
# 目标与域名
# ---------------------------------------------------------------------------

def resolve_target(args: argparse.Namespace) -> Tuple[str, Optional[str], Optional[str]]:
    """返回 (target, space_id, folder_token)。"""
    target = args.target
    space_id = args.space_id
    folder_token = args.folder_token

    if target is None:
        target = "drive" if folder_token else "wiki"

    if target == "drive":
        if not folder_token:
            fail(
                "迁移到云盘需要 --folder-token。\n"
                "在飞书云盘打开目标文件夹，URL 里的 fldcn... 就是它；\n"
                "或改用默认的 --target wiki --space-id my_library。"
            )
        if space_id:
            fail("--folder-token 和 --space-id 不能同时使用。")
        return target, None, folder_token

    if folder_token:
        fail("--folder-token 只能和 --target drive 一起用。")
    space_id = space_id or load_config().get("space_id") or "my_library"
    return target, space_id, None


def resolve_base_url(args: argparse.Namespace, yes: bool) -> str:
    for candidate in (
        args.base_url,
        os.environ.get("N2F_BASE_URL"),
        load_config().get("base_url"),
    ):
        if candidate:
            return normalize_url(str(candidate))

    if sys.stdin.isatty() and not yes:
        try:
            value = input(
                "你的飞书域名（浏览器地址栏，如 https://acme.feishu.cn）\n"
                "回车使用 https://feishu.cn："
            ).strip()
        except EOFError:
            value = ""
        if value:
            value = normalize_url(value)
            cfg = load_config()
            cfg["base_url"] = value
            save_config(cfg)
            return value

    return "https://feishu.cn"


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def _rmtree_checked(path: Path) -> None:
    if path in (Path("/"), Path.home()):
        fail(f"拒绝删除危险路径：{path}")
    if path.exists():
        shutil.rmtree(path)


def _notion_client(args: argparse.Namespace) -> notion.NotionClient:
    try:
        token = (
            getattr(args, "notion_token", None)
            or os.environ.get("NOTION_TOKEN")
            or load_config().get("notion_token")
        )
        return notion.NotionClient(notion.token_from_env_or_arg(token))
    except notion.NotionError as exc:
        fail(str(exc))


def cmd_migrate(args: argparse.Namespace) -> int:
    ensure_lark()
    if args.src and args.notion_page:
        fail("--src 和 --notion-page 只能二选一。")

    target, space_id, folder_token = resolve_target(args)
    base_url = resolve_base_url(args, args.yes)

    notion_ids: List[str] = []
    for value in (args.notion_page or []):
        try:
            notion_ids.append(notion.page_id_from(value))
        except notion.NotionError as exc:
            fail(str(exc))

    if notion_ids:
        key = hashlib.sha1("|".join(notion_ids).encode("utf-8")).hexdigest()[:12]
        work = Path(args.work).expanduser().resolve() if args.work else (WORK_ROOT / f"notion-{key}")
        src = work / "source"
    else:
        src = resolve_source(args.src)
        work = Path(args.work).expanduser().resolve() if args.work else default_work(src)

    log("")
    log(f"源        {src if not notion_ids else 'Notion: ' + ', '.join(notion_ids)}")
    log(f"工作目录  {work}")
    if target == "wiki":
        label = "我的文档库 (my_library)" if space_id == "my_library" else f"知识库 {space_id}"
    else:
        label = f"云盘文件夹 {folder_token}"
    log(f"迁移到    {label}")
    log(f"域名      {base_url}")

    if not args.dry_run:
        ensure_auth(args.identity, auto_login=not args.no_login, yes=args.yes)

    notion_report = None
    if notion_ids:
        client = _notion_client(args)
        try:
            client.validate()
        except notion.NotionError as exc:
            fail(str(exc))
        if args.fresh:
            _rmtree_checked(work)
        if src.exists():
            shutil.rmtree(src)

        log("\n拉取 Notion（API 直连，不需要手动导出）...")

        def progress(done: int, total: Optional[int], message: str) -> None:
            log(f"  [{done}] {message}")

        try:
            notion_report = notion.pull_page_tree(
                client, notion_ids, src, progress=progress, limit=args.pull_limit
            )
        except notion.NotionError as exc:
            fail(f"拉取失败：{exc}")

        (work / "pull-report.json").write_text(
            json.dumps(notion_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log(
            f"拉取完成：{notion_report['total_pages']} 页 / "
            f"{notion_report['images']} 图 / {notion_report['databases']} 个数据库"
        )
        if notion_report["total_pages"] == 0:
            fail("没有拉到任何页面。请确认 integration 已连接到根页面。")
        if notion_report.get("errors"):
            log(f"⚠ {len(notion_report['errors'])} 个页面/资源失败，详见 pull-report.json")

    if not args.dry_run and not args.yes:
        if notion_report is not None:
            page_count = notion_report["total_pages"]
        else:
            pages, _ = core.discover(src)
            page_count = len(pages)
        log(f"页面数    {page_count}")
        try:
            ans = input("\n开始迁移？[y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes"):
            log("已取消。")
            return 0

    if args.dry_run:
        log("\n[dry-run] 只准备本地文件并打印命令，不写入飞书。")

    if not notion_ids and args.fresh:
        _rmtree_checked(work)

    if notion_ids or not (work / "manifest.json").exists():
        rc = core.cmd_prepare(SimpleNamespace(src=str(src), out=str(work), force=True))
        if rc != 0:
            return rc
    else:
        log(f"\n复用已有工作目录（--fresh 可重新准备）：{work}")

    push_args = SimpleNamespace(
        work=str(work),
        target=target,
        space_id=space_id,
        parent_node_token=args.parent_node_token,
        folder_token=folder_token,
        identity=args.identity,
        limit=args.limit,
        resume=True,
        skip_fill=args.skip_fill,
        dry_run=args.dry_run,
        sleep=args.sleep,
        force_state=args.force_state,
        quiet=False,
        base_url=base_url,
    )
    return core.cmd_push(push_args)


def cmd_scan(args: argparse.Namespace) -> int:
    client = _notion_client(args)
    try:
        client.validate()
        data = notion.scan_workspace(client)
    except notion.NotionError as exc:
        fail(str(exc))

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if not data["roots"]:
        fail(
            "没有扫描到页面。请确认：\n"
            "  1) integration 已创建；\n"
            "  2) 根页面已通过 ••• → Connections 连接到 integration。"
        )

    def show(node: dict, depth: int) -> None:
        print("  " * depth + f"- {node['title']}  ({node['count']} 页)")
        for child in node.get("children", []):
            show(child, depth + 1)

    for root in data["roots"]:
        show(root, 0)
    print(f"\n共 {len(data['roots'])} 个顶层页面 / {data['total_pages']} 页")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    client = _notion_client(args)
    try:
        client.validate()
    except notion.NotionError as exc:
        fail(str(exc))

    ids = []
    for value in args.page:
        try:
            ids.append(notion.page_id_from(value))
        except notion.NotionError as exc:
            fail(str(exc))

    out = Path(args.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()) and not args.force:
        fail(f"输出目录非空：{out}\n加 --force 覆盖，或换一个 --out")

    def progress(done: int, total: Optional[int], message: str) -> None:
        if args.json:
            print(f"[{done}] {message}", file=sys.stderr)
        else:
            print(f"  [{done}] {message}", flush=True)

    try:
        report = notion.pull_page_tree(client, ids, out, progress=progress, limit=args.limit)
    except notion.NotionError as exc:
        fail(f"拉取失败：{exc}")

    report_path = out / "pull-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"\n完成：{report['total_pages']} 页 / {report['images']} 图 / "
            f"{report['files']} 附件 / {report['databases']} 个数据库"
        )
        if report.get("errors"):
            print(f"⚠ {len(report['errors'])} 个错误，详见 {report_path}")
        print(f"本地导出：{out}")
        print(f"下一步：notion2feishu migrate --src {out}")
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    src = resolve_source(args.src)
    out = Path(args.out).expanduser().resolve()
    return core.cmd_prepare(SimpleNamespace(src=str(src), out=str(out), force=args.force))


def cmd_push(args: argparse.Namespace) -> int:
    target = args.target or ("drive" if args.folder_token else "wiki")
    space_id = args.space_id
    if target == "wiki":
        space_id = space_id or "my_library"
    ns = SimpleNamespace(
        work=str(Path(args.work).expanduser().resolve()),
        target=target,
        space_id=space_id,
        parent_node_token=args.parent_node_token,
        folder_token=args.folder_token,
        identity=args.identity,
        limit=args.limit,
        resume=args.resume,
        skip_fill=args.skip_fill,
        dry_run=args.dry_run,
        sleep=args.sleep,
        force_state=args.force_state,
        quiet=False,
        base_url=normalize_url(args.base_url or "https://feishu.cn"),
    )
    return core.cmd_push(ns)


def cmd_plan(args: argparse.Namespace) -> int:
    src = resolve_source(args.src)
    return core.cmd_plan(SimpleNamespace(src=str(src)))


def cmd_ui(args: argparse.Namespace) -> int:
    from . import web
    web.serve(port=args.port, open_browser=not args.no_open)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ensure_lark()
    return core.cmd_doctor(args)


def cmd_version(args: argparse.Namespace) -> int:
    print(f"notion2feishu {__version__}")
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def _add_common_push_flags(p: argparse.ArgumentParser, with_resume: bool = True) -> None:
    p.add_argument("--space-id", help="Wiki 空间 ID；个人文档库用 my_library")
    p.add_argument("--parent-node-token", help="Wiki 父节点 token（可选）")
    p.add_argument("--folder-token", help="云盘目标文件夹 token")
    p.add_argument("--base-url", help="租户域名，如 https://acme.feishu.cn")
    p.add_argument("--identity", choices=["user", "bot"], default="user")
    p.add_argument("--limit", type=int, help="只迁前 N 页")
    p.add_argument("--skip-fill", action="store_true", help="只建空节点，不写正文")
    p.add_argument("--dry-run", action="store_true", help="只打印命令，不写飞书")
    p.add_argument("--sleep", type=float, default=0.8, help="每次写操作后的间隔秒数")
    p.add_argument("--force-state", action="store_true", help="目标变化时清空 state")
    if with_resume:
        p.add_argument("--resume", action="store_true", help="跳过 state 里已完成的页面")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notion2feishu",
        description="把 Notion 导出迁移到飞书云文档：保留层级、图片和页间链接。",
    )
    parser.add_argument("--version", action="version", version=f"notion2feishu {__version__}")
    sub = parser.add_subparsers(dest="command")

    m = sub.add_parser("migrate", help="一条命令完成：拉取/找导出 → 准备 → 建树 → 写正文")
    m.add_argument("--src", help="导出的 .zip 或目录；省略时自动找最近的导出")
    m.add_argument("--notion-page", action="append", metavar="URL",
                   help="Notion 页面 URL/ID；可重复。传了它就不需要手动导出")
    m.add_argument("--notion-token", help="Notion Integration Token（默认读 NOTION_TOKEN）")
    m.add_argument("--pull-limit", type=int, help="只从 Notion 拉前 N 页（调试用）")
    m.add_argument("--work", help="工作目录（默认 ~/.notion2feishu/work/<hash>）")
    m.add_argument("--target", choices=["wiki", "drive"], help="默认 wiki（我的文档库）")
    _add_common_push_flags(m)
    m.add_argument("--yes", action="store_true", help="跳过确认，适合脚本/CI")
    m.add_argument("--fresh", action="store_true", help="清空工作目录重新准备")
    m.add_argument("--no-login", action="store_true", help="不自动执行 lark-cli auth login")
    m.set_defaults(func=cmd_migrate)

    prep = sub.add_parser("prepare", help="只做本地准备：清洗 Markdown、复制图片、生成清单")
    prep.add_argument("--src", required=True, help="导出的 .zip 或目录")
    prep.add_argument("--out", default="./notion2feishu-work", help="输出工作目录")
    prep.add_argument("--force", action="store_true", help="覆盖已有目录")
    prep.set_defaults(func=cmd_prepare)

    push = sub.add_parser("push", help="只推送：在已有工作目录上建节点并写正文")
    push.add_argument("--work", required=True, help="prepare 生成的工作目录")
    push.add_argument("--target", choices=["wiki", "drive"])
    _add_common_push_flags(push)
    push.set_defaults(func=cmd_push)

    scan = sub.add_parser("scan", help="扫描 Notion 工作区，打印可迁移的顶层页面")
    scan.add_argument("--notion-token", help="Notion Integration Token（默认读 NOTION_TOKEN）")
    scan.add_argument("--json", action="store_true", help="输出 JSON（给 Agent 用）")
    scan.set_defaults(func=cmd_scan)

    pull = sub.add_parser("pull", help="从 Notion API 拉取页面到本地（不写飞书）")
    pull.add_argument("--page", action="append", required=True, metavar="URL",
                      help="Notion 页面 URL/ID；可重复")
    pull.add_argument("--out", default="./notion-export", help="本地输出目录")
    pull.add_argument("--notion-token", help="Notion Integration Token（默认读 NOTION_TOKEN）")
    pull.add_argument("--limit", type=int, help="只拉前 N 页")
    pull.add_argument("--force", action="store_true", help="输出目录非空时覆盖")
    pull.add_argument("--json", action="store_true", help="输出 JSON（给 Agent 用）")
    pull.set_defaults(func=cmd_pull)

    plan = sub.add_parser("plan", help="只扫描，打印页面树")
    plan.add_argument("--src", required=True, help="导出的 .zip 或目录")
    plan.set_defaults(func=cmd_plan)

    ui = sub.add_parser("ui", help="启动本地 Web 向导（浏览器操作）")
    ui.add_argument("--port", type=int, default=0, help="端口；0 表示随机可用端口")
    ui.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ui.set_defaults(func=cmd_ui)

    doc = sub.add_parser("doctor", help="检查 lark-cli 登录与权限")
    doc.set_defaults(func=cmd_doctor)

    ver = sub.add_parser("version", help="打印版本")
    ver.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 极简用法：notion2feishu / notion2feishu xxx.zip / notion2feishu --src xxx.zip
    if not argv:
        argv = ["ui"] if sys.stdin.isatty() else ["migrate"]
    elif argv[0] in ("-h", "--help", "--version"):
        pass
    elif argv[0].startswith("-"):
        argv = ["migrate"] + argv
    elif argv[0] not in KNOWN_COMMANDS:
        argv = ["migrate", "--src", argv[0]] + argv[1:]

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
