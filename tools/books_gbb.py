#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""books/gbb 的实现：把 PDF 发到 GitHub Release，并把下载链接写回 index.md。

只用标准库 + 本机的 git / gh 命令。

核心约定（细节见 ../AGENTS.md）：
  * 扫描范围 = **当前工作目录及其子目录**（--all 才扫整个仓库）；
  * PDF 归属"从它所在目录往上找、最近的那个含 index.md 的目录"（可以找到 cwd 之上，
    但扫描不会把 cwd 之外的 PDF 拉进来）；
  * 一个 PDF 一个 tag：<目录名>__<文件名去扩展>__<YYYYMMDD-HHMM>；
  * 账本 ../.books-releases.json 记 {PDF 相对路径: {sha256, tag, url, uploaded_at}}，
    sha256 没变就跳过（不重传、不改 index.md）；
  * 只把托管块（books:downloads:begin … end）写进 index.md，块外一个字不动；
  * --pull：按账本 + index.md 托管块，把当前目录范围内本地缺失的 PDF 下载回来，
    不改 index.md、不提交、不推送。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BEGIN_MARK = "<!-- books:downloads:begin （由 books/gbb 自动生成，别手改这块） -->"
END_MARK = "<!-- books:downloads:end -->"
LEDGER_NAME = ".books-releases.json"
DEFAULT_REPO = "allinkernel/wblog-books"
DEFAULT_BRANCH = "main"
MAX_TAG_LEN = 100
MAX_RELEASE_BYTES = 2 * 1024 ** 3  # GitHub Release 单文件 2GB 上限
URL_TMPL = "https://github.com/{repo}/releases/tag/{tag}"

DEBUG = False


class ToolError(Exception):
    """可以直接展示给用户的错误。"""


@dataclass
class Item:
    pdf: Path
    rel: str          # 相对 books/ 的 POSIX 路径
    index_dir: Path
    index_rel: str    # 相对 books/ 的 index.md 路径
    sha: str
    size: int
    tag: str
    url: str
    kind: str         # new / changed / skip
    old: dict | None = None


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def debug(msg: str) -> None:
    if DEBUG:
        print(f"[debug] {msg}", file=sys.stderr)


def run(cmd: list, cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess:
    if DEBUG:
        print("  $ " + " ".join(shlex.quote(str(c)) for c in cmd), file=sys.stderr)
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"找不到命令 {cmd[0]!r}：{exc}") from exc
    if DEBUG and (proc.stdout or proc.stderr):
        print(f"    rc={proc.returncode} out={proc.stdout!r} err={proc.stderr!r}", file=sys.stderr)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ToolError(f"命令失败：{' '.join(shlex.quote(str(c)) for c in cmd)}\n{detail}")
    return proc


def rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def url_for(repo: str, tag: str) -> str:
    return URL_TMPL.format(repo=repo, tag=tag)


def dir_label(path: Path, root: Path) -> str:
    rel = rel_posix(path, root)
    return root.name if rel == "." else rel


def is_under(path: Path, parent: Path) -> bool:
    """path 是否在 parent 目录（或 parent 自己）之下；两边都会 resolve，容忍软链。"""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def scope_label(scan_root: Path, root: Path) -> str:
    rel = rel_posix(scan_root, root)
    return "整个仓库" if rel == "." else rel


def resolve_repo_root(start: Path) -> Path:
    """用 git 自己认仓库根（脚本被拷到别处、从子目录调用都能对上）。"""
    proc = run(["git", "-C", start, "rev-parse", "--show-toplevel"])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ToolError(
            f"从 {start} 开始找不到 git 仓库根。\n"
            "  请在 books/ 仓库里运行 books/gbb（默认扫描当前目录及其子目录）。"
        )
    return Path(proc.stdout.strip()).resolve()


# ---------------------------------------------------------------------------
# 扫描 / tag
# ---------------------------------------------------------------------------

def scan_pdfs(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        base = Path(dirpath)
        for name in sorted(filenames):
            if name.lower().endswith(".pdf"):
                found.append(base / name)
    return found


def find_index_dir(pdf_dir: Path, root: Path) -> Path | None:
    """从 PDF 所在目录往上找最近的含 index.md 的目录（含 books/ 本身）。"""
    current = pdf_dir
    while True:
        if (current / "index.md").is_file():
            return current
        if current == root:
            return None
        if current.parent == current:
            return None
        current = current.parent


def sanitize_tag_part(text: str) -> str:
    """只留 [A-Za-z0-9._-] 与 CJK/其它 unicode 字母数字；空格、引号、斜杠、冒号等换成 '-'。"""
    chars = []
    for ch in text:
        if ch.isalnum() or ch in "._-":
            chars.append(ch)
        else:
            chars.append("-")
    cleaned = "".join(chars)
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = cleaned.strip("-._")
    return cleaned


def make_tag(index_dir: Path, pdf: Path, stamp: str, used: set[str]) -> str:
    dir_name = sanitize_tag_part(index_dir.name) or "books"
    stem = sanitize_tag_part(pdf.stem) or "pdf"
    core = f"{dir_name}__{stem}"
    suffix = f"__{stamp}"
    room = MAX_TAG_LEN - len(suffix)
    if room < 1:
        raise ToolError("tag 长度上限太小，连时间戳都放不下")
    core = core[:room].rstrip("-._") or "pdf"
    base = core + suffix
    tag = base
    counter = 2
    while tag in used:
        extra = f"-{counter}"
        tag = base[: MAX_TAG_LEN - len(extra)].rstrip("-._") + extra
        counter += 1
    return tag


def used_tags(root: Path, ledger: dict) -> set[str]:
    tags: set[str] = set()
    proc = run(["git", "-C", root, "tag", "--list"])
    if proc.returncode == 0:
        tags.update(line.strip() for line in proc.stdout.splitlines() if line.strip())
    for entry in ledger.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("tag"):
            tags.add(entry["tag"])
        for prev in entry.get("previous", []) or []:
            if isinstance(prev, dict) and prev.get("tag"):
                tags.add(prev["tag"])
    return tags


# ---------------------------------------------------------------------------
# 账本 / index.md 托管块
# ---------------------------------------------------------------------------

def load_ledger(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"{path.name} 不是合法 JSON（{exc}）。\n"
            "  这个文件由 books/gbb 维护；实在坏了就先备份再删掉，重跑会重新生成。"
        ) from exc
    if not isinstance(data, dict):
        raise ToolError(f"{path.name} 顶层应该是对象（{{PDF 路径: {{...}}}}）")
    return data


def ledger_text(ledger: dict) -> str:
    return json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


ITEM_LINE_RE = re.compile(r"^\s*\d+\.\s+\[(.*)\]\((\S+)\)\s*$")


def parse_block_items(text: str) -> list[tuple[str, str]] | None:
    """读出托管块里的 [(label, url)]；没有 begin 标记返回 None，空块返回 []。"""
    if BEGIN_MARK not in text:
        return None
    if END_MARK not in text:
        return []
    start = text.index(BEGIN_MARK) + len(BEGIN_MARK)
    end = text.index(END_MARK, start)
    entries: list[tuple[str, str]] = []
    for line in text[start:end].splitlines():
        match = ITEM_LINE_RE.match(line)
        if not match:
            continue
        label = match.group(1).replace("\\[", "[").replace("\\]", "]")
        entries.append((label, match.group(2)))
    return entries


def render_block(items: list[Item], index_dir: Path,
                 preserved: list[tuple[str, str, str]] = ()) -> str:
    """渲染托管块。

    preserved 是 [(rel, label, url)]：扫描范围之外、但本来就记在这个 index.md 块里的
    旧条目。cwd 之上的 index.md 被更新时用它保住范围外的链接，免得整块被覆盖掉。
    """
    rows: dict[str, tuple[str, str]] = {}
    for item in items:
        rows[item.rel] = (rel_posix(item.pdf, index_dir), item.url)
    for rel, label, url in preserved:
        rows.setdefault(rel, (label, url))
    lines = [BEGIN_MARK, "# 下载链接"]
    for num, rel in enumerate(sorted(rows), 1):
        label, url = rows[rel]
        label = label.replace("[", "\\[").replace("]", "\\]")
        lines.append(f"{num}. [{label}]({url})")
    lines.append(END_MARK)
    return "\n".join(lines)


def replace_block(text: str, block: str, index_rel: str) -> str:
    if BEGIN_MARK in text:
        if END_MARK not in text:
            raise ToolError(
                f"{index_rel} 里有 books:downloads:begin 但没有 books:downloads:end。\n"
                "  请先手动补上结束标记（或整块删掉），再重跑。"
            )
        start = text.index(BEGIN_MARK)
        end = text.index(END_MARK, start) + len(END_MARK)
        return text[:start] + block + text[end:]
    if END_MARK in text:
        raise ToolError(
            f"{index_rel} 里有 books:downloads:end 但没有 books:downloads:begin。\n"
            "  请先手动补上开始标记（或整块删掉），再重跑。"
        )
    body = text.rstrip("\n")
    return body + "\n\n" + block + "\n"


# ---------------------------------------------------------------------------
# 提示 / 教程
# ---------------------------------------------------------------------------

def confirm(question: str, default_yes: bool, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"{question} —— 已自动确认（--yes）")
        return True
    if not sys.stdin.isatty():
        raise ToolError(
            "当前不是交互式终端，没法询问。\n"
            "  请在终端里手动运行 ./gbb，或者加 --yes 自动确认。"
        )
    suffix = " [Y/n] " if default_yes else " [y/N] "
    while True:
        try:
            answer = input(question + suffix).strip().lower()
        except EOFError:
            print()
            return False
        if not answer:
            return default_yes
        if answer in ("y", "yes", "是"):
            return True
        if answer in ("n", "no", "否"):
            return False
        print("  请输入 y 或 n。")


def print_gh_troubleshooting(repo: str, action: str = "上传") -> None:
    print(f"""
{action}这一步失败了，按下面顺序排查（从最常见的开始）：

  1) 先看登录状态：gh auth status
     没登录 / token 失效：gh auth login -h github.com
  2) token 权限必须包含 repo（{action} release 必需）
     只读 token 的典型报错：HTTP 403: Resource not accessible by personal access token
     重新授权带上 scope：gh auth login -h github.com -s repo
  3) GitHub Release 单个文件上限 2GB；超了请拆分 PDF，或换别的方式托管
  4) 网络 / 代理：确认能访问 https://api.github.com（公司网、代理经常拦）
  5) 仓库和权限：确认 {repo} 已创建，且当前账号对它至少有写权限
""")


def print_git_hints(root: Path, branch: str) -> None:
    print(f"""
推送失败了。先确认远端配好了没：

  1) 远端仓库建了吗（只需一次）：gh repo create {DEFAULT_REPO} --public
  2) 本地 remote 配了吗：git -C {root} remote -v
     没有就加：git -C {root} remote add origin https://github.com/{DEFAULT_REPO}.git
  3) 远端分支存在吗：分支不存在就先推一次
     git -C {root} push -u origin {branch}
  4) 登录状态：gh auth status

本地提交已经做好，问题修好后直接 `git -C {root} push origin {branch}` 即可，不用重跑。
""")


# ---------------------------------------------------------------------------
# git 写操作
# ---------------------------------------------------------------------------

def stage_paths(root: Path, paths: list[Path]) -> None:
    for path in paths:
        rel = rel_posix(path, root)
        if path.exists():
            run(["git", "-C", root, "add", "--", rel], check=True)
        else:
            run(["git", "-C", root, "rm", "-f", "--ignore-unmatch", "--", rel], check=False)


def git_commit(root: Path, paths: list[Path], message: str) -> str:
    rels = [rel_posix(p, root) for p in paths]
    fd, msg_path = tempfile.mkstemp(prefix="books-gbb-msg-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(message)
            if not message.endswith("\n"):
                fh.write("\n")
        proc = run(["git", "-C", root, "commit", "-F", msg_path, "--", *rels])
    finally:
        try:
            os.unlink(msg_path)
        except OSError:
            pass
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ToolError(f"git commit 失败：\n{detail}")
    head = run(["git", "-C", root, "rev-parse", "--short", "HEAD"])
    return head.stdout.strip()


def git_push(root: Path, branch: str) -> bool:
    proc = run(["git", "-C", root, "push", "origin", branch])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        print(f"\n推送失败：\n{detail}")
        return False
    tail = (proc.stderr or proc.stdout).strip()
    if tail:
        print(tail)
    return True


def rollback(root: Path, originals: dict[Path, bytes | None], branch: str, reason: str) -> None:
    """把本次改过的 index.md / 账本恢复原样，再提交推送一次。"""
    paths = list(originals)
    for path, raw in originals.items():
        if raw is None:
            if path.exists():
                path.unlink()
        else:
            path.write_bytes(raw)
    stage_paths(root, paths)
    rels = [rel_posix(p, root) for p in paths]
    diff = run(["git", "-C", root, "diff", "--cached", "--quiet", "--", *rels])
    if diff.returncode == 0:
        print("（没有需要回滚的已提交改动）")
        return
    message = (
        f"books: 回滚本次发布（{reason}）\n\n"
        "index.md 与 .books-releases.json 已恢复到运行前的状态；\n"
        "下次运行 books/gbb 会重新生成计划。\n"
    )
    commit = git_commit(root, paths, message)
    print(f"已回滚并提交：{commit}")
    if not git_push(root, branch):
        print("⚠️ 回滚提交没能推送，请手动 git push。")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="books/gbb",
        description="把 books/ 下的 PDF 发到 GitHub Release，并写回最近 index.md 的下载链接。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印计划：不写文件、不提交、不推送、不上传")
    parser.add_argument("--pull", action="store_true",
                        help="下载当前目录范围内本地缺失的 PDF（不改 index.md、不提交、不推送）")
    parser.add_argument("--all", action="store_true",
                        help="扫描整个仓库（默认只扫描当前工作目录及其子目录）")
    parser.add_argument("--yes", action="store_true",
                        help="跳过所有询问（两处都按默认答案执行）")
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help=f"GitHub 仓库 owner/name（默认 {DEFAULT_REPO}）")
    parser.add_argument("--branch", default=DEFAULT_BRANCH,
                        help=f"要提交/推送的分支（默认 {DEFAULT_BRANCH}）")
    parser.add_argument("--debug", action="store_true",
                        help="打印实际执行的命令与 gh 原始输出")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", args.repo):
        parser.error("--repo 的格式应该是 owner/name，例如 allinkernel/wblog-books")
    return args


def print_plan(items: list[Item], todo: list[Item], root: Path, args: argparse.Namespace,
               index_changes: dict[Path, str], ledger_changed: bool) -> None:
    print(f"扫描到 {len(items)} 个 PDF：")
    for item in items:
        if item.kind == "new":
            status = "新增"
        elif item.kind == "changed":
            status = "变更"
        else:
            status = "跳过（sha256 未变）"
        print(f"  [{status}] {item.rel}")
        print(f"         归属：{item.index_rel}")
        print(f"         tag ：{item.tag}")
        if item.kind == "changed" and item.old and item.old.get("tag"):
            print(f"         旧 tag：{item.old['tag']}（保留在 GitHub 上，不删）")

    print("\n计划写入：")
    for path in sorted(index_changes, key=lambda p: rel_posix(p, root)):
        print(f"  - {rel_posix(path, root)}（更新「下载链接」托管块）")
    if ledger_changed:
        print(f"  - {LEDGER_NAME}（记录 {len(todo)} 条发布账）")

    print("\n将执行：")
    rels = sorted({rel_posix(p, root) for p in index_changes} | ({LEDGER_NAME} if ledger_changed else set()))
    print(f"  git -C {root} add -- {' '.join(rels)}")
    print(f"  git -C {root} commit -F <提交信息> -- {' '.join(rels)}")
    print(f"  git -C {root} push origin {args.branch}")
    for item in todo:
        print(f"  gh release view {item.tag} --repo {args.repo}   # 判断 tag 有没有被占")
        print(f"  gh release create {item.tag} <pdf> --repo {args.repo}  # 已存在则改 upload --clobber")


def build_commit_message(todo: list[Item]) -> str:
    new_items = [it for it in todo if it.kind == "new"]
    changed_items = [it for it in todo if it.kind == "changed"]
    lines = [f"books: 发布 {len(todo)} 个 PDF 到 Release", ""]
    if new_items:
        lines.append(f"新增 {len(new_items)} 个：")
        for item in new_items:
            lines.append(f"- {item.rel}")
            lines.append(f"  tag: {item.tag}")
    if changed_items:
        if new_items:
            lines.append("")
        lines.append(f"变更 {len(changed_items)} 个：")
        for item in changed_items:
            lines.append(f"- {item.rel}")
            lines.append(f"  tag: {item.tag}（旧 tag: {item.old.get('tag') if item.old else '无'}）")
    lines.append("")
    lines.append("index.md 的「下载链接」块由 books/gbb 自动生成，别手改。")
    return "\n".join(lines) + "\n"


def do_uploads(args: argparse.Namespace, root: Path, todo: list[Item]) -> list[tuple[Item, str]]:
    """返回失败列表 [(item, 原因)]。"""
    failures: list[tuple[Item, str]] = []
    for num, item in enumerate(todo, 1):
        print(f"\n[{num}/{len(todo)}] {item.rel}")
        if item.size > MAX_RELEASE_BYTES:
            reason = f"文件 {human_size(item.size)} 超过 GitHub Release 单文件 2GB 上限"
            print(f"  失败：{reason}")
            failures.append((item, reason))
            continue
        notes = (
            f"来源目录：{dir_label(item.pdf.parent, root)}；"
            f"大小：{human_size(item.size)}；"
            f"sha256：{item.sha[:12]}"
        )
        title = f"{dir_label(item.pdf.parent, root)} / {item.pdf.name}"
        exists = run(["gh", "release", "view", item.tag, "--repo", args.repo]).returncode == 0
        if exists:
            print(f"  tag 已存在，改用 gh release upload --clobber（不重建 release）")
            cmd = ["gh", "release", "upload", item.tag, item.pdf, "--repo", args.repo, "--clobber"]
        else:
            cmd = ["gh", "release", "create", item.tag, item.pdf, "--repo", args.repo,
                   "--title", title, "--notes", notes]
        proc = run(cmd)
        if proc.returncode == 0:
            print(f"  完成：{item.url}")
        else:
            detail = (proc.stderr or proc.stdout).strip()
            print(f"  失败：{detail or '（gh 没输出原因）'}")
            failures.append((item, detail or "gh 返回非 0"))
            break  # 多半是登录/权限/网络问题，后面的不再硬试
    return failures


def _tag_from_url(url: str) -> str:
    match = re.search(r"/releases/tag/([^/?#\s]+)", url or "")
    return match.group(1) if match else ""


def index_files_covering(scan_root: Path, root: Path) -> list[Path]:
    """cwd 子树里的所有 index.md，外加 cwd 往上最近的那个 index.md（如果在仓库内）。"""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        if "index.md" in filenames:
            found.append(Path(dirpath) / "index.md")
    if scan_root != root:
        current = scan_root.parent
        while True:
            candidate = current / "index.md"
            if candidate.is_file():
                found.append(candidate)
                break
            if current == root or current.parent == current:
                break
            current = current.parent
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def collect_pull_records(root: Path, scan_root: Path, ledger: dict) -> dict[str, dict]:
    """账本 + index.md 托管块 -> {rel: {tags: [...], sources: [...]}}，只留 cwd 子树内的。"""
    records: dict[str, dict] = {}

    def add(rel: str, tag: str, source: str) -> None:
        if not is_under(root / rel, scan_root):
            return
        info = records.setdefault(rel, {"tags": [], "sources": []})
        if tag and tag not in info["tags"]:
            info["tags"].append(tag)
        if source not in info["sources"]:
            info["sources"].append(source)

    for rel, value in sorted(ledger.items()):
        if not isinstance(value, dict):
            continue
        add(rel, value.get("tag") or "", LEDGER_NAME)

    for index_md in index_files_covering(scan_root, root):
        try:
            text = index_md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  （跳过读不了的 {rel_posix(index_md, root)}：{exc}）")
            continue
        entries = parse_block_items(text)
        if not entries:
            continue
        index_dir = index_md.parent
        for label, url in entries:
            try:
                rel = (index_dir / label).resolve().relative_to(root.resolve()).as_posix()
            except (OSError, ValueError):
                continue
            add(rel, _tag_from_url(url), rel_posix(index_md, root))
    return records


def do_pull(args: argparse.Namespace, root: Path, scan_root: Path, ledger: dict) -> int:
    """--pull：下载 cwd 范围内、账本/索引块里有记录但本地不存在的 PDF。"""
    records = collect_pull_records(root, scan_root, ledger)
    scope = scope_label(scan_root, root)
    if not records:
        print(f"\n扫描范围（{scope}）里没有找到任何发布记录（账本和 index.md 托管块都空）。")
        return 0

    present = [rel for rel in records if (root / rel).is_file()]
    missing = [rel for rel in records if not (root / rel).is_file()]
    print(f"\n发布记录 {len(records)} 条：本地已有 {len(present)} 条。")
    if not missing:
        print(f"全部已在本地（{scope} 范围内 {len(records)} 条记录都不缺）")
        return 0

    print(f"发现 {len(missing)} 个缺失：")
    jobs: list[tuple[str, str, Path, list[str] | None]] = []
    for rel in sorted(missing):
        info = records[rel]
        tag = info["tags"][0] if info["tags"] else ""
        note = ""
        if len(info["tags"]) > 1:
            note = f"  ⚠️ 账本/index.md 的 tag 不一致：{' / '.join(info['tags'])}"
        if tag:
            print(f"  [缺失] {rel}  ← {tag}{note}")
        else:
            print(f"  [缺失] {rel}  ← （没有 tag，无法下载）{note}")
        target_dir = (root / rel).parent
        cmd = None
        if tag:
            cmd = ["gh", "release", "download", tag, "--repo", args.repo,
                   "--pattern", Path(rel).name, "--dir", str(target_dir), "--clobber"]
        jobs.append((rel, tag, target_dir, cmd))

    if args.dry_run:
        print("\n将执行：")
        for rel, _tag, _target_dir, cmd in jobs:
            if cmd is None:
                print(f"  # {rel}：没有 tag，跳过")
            else:
                print("  " + " ".join(shlex.quote(c) for c in cmd))
        print("\n[dry-run] 没有下载任何文件。")
        return 0

    print()
    if not confirm(
        f"现在下载这 {len(jobs)} 个缺失文件？（只放回本地；不改 index.md、不提交）",
        default_yes=True,
        assume_yes=args.yes,
    ):
        print("已取消，没有下载任何文件。")
        return 0

    print()
    ok = 0
    failures: list[tuple[str, str]] = []
    for num, (rel, _tag, target_dir, cmd) in enumerate(jobs, 1):
        print(f"[{num}/{len(jobs)}] {rel}")
        if cmd is None:
            reason = "记录里没有 tag（账本和 index.md 托管块都没给 release 链接）"
            print(f"  失败：{reason}")
            failures.append((rel, reason))
            continue
        print("  $ " + " ".join(shlex.quote(c) for c in cmd))
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            proc = run(cmd)
        except ToolError as exc:
            print(f"  失败：{exc}")
            failures.append((rel, str(exc)))
            continue
        target = root / rel
        if proc.returncode == 0 and target.is_file():
            print(f"  完成：{rel}（{human_size(target.stat().st_size)}）")
            ok += 1
        else:
            detail = (proc.stderr or proc.stdout).strip() or f"gh 退出码 {proc.returncode}"
            print(f"  失败：{detail}")
            failures.append((rel, detail))

    print("\n==== 总账（--pull）====")
    print(f"成功 {ok} / 失败 {len(failures)} / 本来就在本地 {len(present)}")
    if failures:
        for rel, reason in failures:
            print(f"  ✗ {rel}：{reason}")
        print_gh_troubleshooting(args.repo, "下载")
        print("下载失败的文件没有放回本地；修好后重跑 ./gbb --pull 即可。")
        return 1
    print("\n全部下载完成。index.md 和账本没有改动，也没有提交/推送。")
    return 0


def _main(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    root = resolve_repo_root(cwd)
    if not (root / ".git").exists():
        raise ToolError(f"{root} 不是 git 仓库（找不到 .git）")
    try:
        cwd_rel = cwd.relative_to(root).as_posix()
    except ValueError:
        raise ToolError(
            f"当前目录 {cwd} 不在仓库 {root} 里。\n"
            "  请先 cd 到 books/ 里想处理的目录，再运行 books/gbb。"
        ) from None

    scan_root = root if args.all else cwd
    if args.all:
        scope_text = "整个仓库（--all）"
    elif scan_root == root:
        scope_text = "仓库根（当前目录）"
    else:
        scope_text = f"{cwd_rel}（当前目录）"

    print("== books/gbb ==")
    print(f"仓库根：{root}")
    print(f"扫描范围：{scope_text}")
    print(f"远端仓库：{args.repo}    分支：{args.branch}")

    branch_proc = run(["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"])
    current = branch_proc.stdout.strip() if branch_proc.returncode == 0 else ""

    ledger_path = root / LEDGER_NAME
    ledger = load_ledger(ledger_path)
    if args.pull:
        return do_pull(args, root, scan_root, ledger)

    if branch_proc.returncode != 0:
        raise ToolError("读不到当前分支：" + (branch_proc.stderr or branch_proc.stdout).strip())
    has_branch = run(["git", "-C", root, "rev-parse", "--verify", "--quiet",
                      f"refs/heads/{args.branch}"]).returncode == 0
    if not has_branch:
        raise ToolError(
            f"分支 {args.branch} 不存在。\n"
            f"  先建分支并推到远端（只需一次）：\n"
            f"    git -C {root} checkout -b {args.branch}\n"
            f"    git -C {root} push -u origin {args.branch}"
        )
    if current != args.branch:
        raise ToolError(
            f"当前在分支 {current}，不是 {args.branch}。\n"
            f"  请先：git -C {root} checkout {args.branch}"
        )

    ledger_raw_before = ledger_path.read_bytes() if ledger_path.exists() else None

    pdfs = scan_pdfs(scan_root)
    if not pdfs:
        print(f"\n没有在 {scope_label(scan_root, root)} 下找到任何 PDF。")
        if not args.all:
            print("  （默认只扫描当前目录；想扫整个仓库用 --all）")
        return 0

    missing: list[Path] = []
    rows: list[tuple[Path, Path]] = []
    for pdf in pdfs:
        index_dir = find_index_dir(pdf.parent, root)
        if index_dir is None:
            missing.append(pdf)
        else:
            rows.append((pdf, index_dir))

    if missing:
        print(f"\n错误：有 {len(missing)} 个 PDF 找不到可归属的 index.md。")
        print("  规则：从 PDF 所在目录往上找，最近的含 index.md 的目录就管它下面所有层级的 PDF；")
        print("       一直找到 books/ 都没有，就是下面这些：")
        for pdf in sorted(missing, key=lambda p: rel_posix(p, root)):
            print(f"    - {rel_posix(pdf, root)}")
        print("\n  请先在合适的位置建 index.md（目录名会进 tag），再重跑。")
        return 1

    tags = used_tags(root, ledger)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")

    items: list[Item] = []
    for pdf, index_dir in sorted(rows, key=lambda row: rel_posix(row[0], root)):
        rel = rel_posix(pdf, root)
        sha = sha256_file(pdf)
        size = pdf.stat().st_size
        old = ledger.get(rel) if isinstance(ledger.get(rel), dict) else None
        if old and old.get("sha256") == sha and old.get("tag"):
            kind = "skip"
            tag = old["tag"]
            url = old.get("url") or url_for(args.repo, tag)
        else:
            kind = "changed" if old and old.get("sha256") else "new"
            tag = make_tag(index_dir, pdf, stamp, tags)
            tags.add(tag)
            url = url_for(args.repo, tag)
        items.append(Item(
            pdf=pdf,
            rel=rel,
            index_dir=index_dir,
            index_rel=rel_posix(index_dir / "index.md", root),
            sha=sha,
            size=size,
            tag=tag,
            url=url,
            kind=kind,
            old=old,
        ))

    todo = [it for it in items if it.kind != "skip"]
    skipped = len(items) - len(todo)
    if not todo:
        print(f"\n全部已是最新：{len(items)} 个 PDF 的 sha256 都没变，跳过 {skipped} 个。")
        print("没有改 index.md，也没有提交或上传。")
        return 0

    by_index: dict[Path, list[Item]] = {}
    for item in items:
        by_index.setdefault(item.index_dir, []).append(item)

    index_changes: dict[Path, str] = {}
    for index_dir in sorted(by_index, key=lambda p: rel_posix(p, root)):
        if not any(it.kind != "skip" and it.index_dir == index_dir for it in todo):
            continue
        index_md = index_dir / "index.md"
        try:
            old_text = index_md.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"{rel_posix(index_md, root)} 不是 UTF-8 文本：{exc}") from exc
        preserved: list[tuple[str, str, str]] = []
        if not is_under(index_dir, scan_root):
            # 这个 index.md 在 cwd 之上：块里可能还有范围外的旧条目，原样保住
            scanned_rels = {it.rel for it in by_index[index_dir]}
            for label, url in parse_block_items(old_text) or []:
                try:
                    rel = (index_dir / label).resolve().relative_to(root.resolve()).as_posix()
                except (OSError, ValueError):
                    continue
                if rel not in scanned_rels:
                    preserved.append((rel, label, url))
        block = render_block(by_index[index_dir], index_dir, preserved)
        new_text = replace_block(old_text, block, rel_posix(index_md, root))
        if new_text != old_text:
            index_changes[index_md] = new_text

    ledger_new = copy.deepcopy(ledger)
    for item in todo:
        entry = {
            "sha256": item.sha,
            "tag": item.tag,
            "url": item.url,
            "uploaded_at": now_iso,
        }
        old = item.old
        if old and old.get("sha256") and old.get("sha256") != item.sha:
            previous = list(old.get("previous") or [])
            previous.append({
                "sha256": old.get("sha256"),
                "tag": old.get("tag"),
                "url": old.get("url"),
                "uploaded_at": old.get("uploaded_at"),
            })
            entry["previous"] = previous
        ledger_new[item.rel] = entry
    new_ledger_raw = ledger_text(ledger_new).encode("utf-8")
    ledger_changed = new_ledger_raw != ledger_raw_before

    print()
    print_plan(items, todo, root, args, index_changes, ledger_changed)

    if args.dry_run:
        print("\n[dry-run] 没有写任何文件，也没有执行 git / gh 写命令。")
        return 0

    print()
    if not confirm(
        f"确认按上面的计划修改 index.md、提交并推送到 {args.branch}？（共 {len(todo)} 个 PDF）",
        default_yes=False,
        assume_yes=args.yes,
    ):
        print("已取消，没有改任何文件。")
        return 0

    originals: dict[Path, bytes | None] = {path: path.read_bytes() for path in index_changes}
    originals[ledger_path] = ledger_raw_before

    for path, text in index_changes.items():
        path.write_text(text, encoding="utf-8")
        print(f"已写入 {rel_posix(path, root)}")
    ledger_path.write_bytes(new_ledger_raw)
    print(f"已写入 {LEDGER_NAME}")

    write_paths = sorted(index_changes) + [ledger_path]
    stage_paths(root, write_paths)
    message = build_commit_message(todo)
    commit_hash = git_commit(root, write_paths, message)
    print(f"已提交：{commit_hash}")

    if not git_push(root, args.branch):
        print_git_hints(root, args.branch)
        return 1
    print(f"已推送 origin/{args.branch}")

    print()
    if not confirm(
        f"现在把这 {len(todo)} 个 release 上传到 {args.repo}？（上传后 index.md 里的链接才可用）",
        default_yes=True,
        assume_yes=args.yes,
    ):
        print("选择不上传：正在回滚本次对 index.md 和账本的改动（本地与远端各回退一个提交）……")
        rollback(root, originals, args.branch, "用户选择暂不上传 release")
        print("已回滚。想上传时再跑一次 ./gbb 即可。")
        return 0

    failures = do_uploads(args, root, todo)

    if failures:
        print("\n==== 上传失败 ====")
        for item, reason in failures:
            print(f"  - {item.rel}（tag {item.tag}）：{reason}")
        print_gh_troubleshooting(args.repo)
        print("现在回滚本次对 index.md 和账本的改动，避免留下指向空 release 的链接……")
        rollback(root, originals, args.branch, "release 上传失败")
        return 1

    print("\n==== 总账 ====")
    print(f"成功 {len(todo)} / 跳过 {skipped} / 失败 0")
    for item in todo:
        print(f"  ✓ {item.rel}\n    {item.url}")
    print("\n完成。")
    return 0


def main(argv: list[str] | None = None) -> int:
    global DEBUG
    args = parse_args(sys.argv[1:] if argv is None else argv)
    DEBUG = args.debug
    try:
        return _main(args)
    except ToolError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
