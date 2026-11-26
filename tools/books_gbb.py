#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""books/gbb 的实现：把 PDF 压成 tar.xz（过大再分卷）发到 GitHub Release，
并把下载链接写回最近的 index.md。

只用标准库 + 本机的 tar / xz / split / git / gh / curl 命令。

核心约定（细节见 ../AGENTS.md）：
  * 扫描范围 = **当前工作目录及其子目录**（--all 才扫整个仓库）；
  * PDF 归属"从它所在目录往上找、最近的那个含 index.md 的目录"（可以找到 cwd 之上，
    但扫描不会把 cwd 之外的 PDF 拉进来）；
  * 一个 PDF / 一本书一个 tag：<目录名>__<文件名去扩展>__<YYYYMMDD-HHMM>；
  * **更新判据 = sha256 + 文件 mtime，两个条件任一变化都要更新**：
      - sha256 变  -> 新 tag、新 release（旧 release 不删）
      - mtime 变   -> 同样新 tag、新 release（`git checkout` 老 PDF 会改 mtime，
                      所以会故意重传一次；这是明确的取舍，README 里写着）
  * 上传的不是裸 PDF，而是 `tar -C <目录> -cJf` 出来的 `<pdf>.tar.xz`
    （xz 用 `-6 -T0` 多线程，保留原名，解压后文件名不变）；产物 > 10 MiB 就
    `split -b 10M -d -a 2` 成 `.part.00 .part.01 …`，每个分卷单独是一个资产；
  * 临时目录 = PDF 同目录下的隐藏目录 `.<pdf>.gbb_tar_dir`
    （放 .tar.xz、分卷和 `.gbb-archive.json`）。上传成功清理，`--keep-temp` 保留；
    失败保留，下次运行校验 manifest 后复用已有分卷、不重复压缩；
  * 分卷并发上传（`--jobs`，默认 4）：并发时**不画单文件进度条**，每 2 秒刷一行
    "已完成 x/N 个分卷 · 已传 … / … · …/s"（非 TTY 每 10 秒一行）；
    `--jobs 1` 且是终端时用 curl 的单文件真实进度条。进度只来自"已完成分卷"，
    绝不编造；
  * 账本 ../.books-releases.json 记
    {PDF 相对路径: {sha256, mtime, tag, url, size, asset_size, last_remote_state,
                   uploaded_at, archive:{name,size,parts:[{name,size}]}}}；
  * **账本只是"上次以为成功"的线索，远端才是事实**：sha256 与 mtime 都没变也要先
    `gh release view` 核对 release / 分卷 / 大小（草稿也算没传好），对不上就补传；
    默认核对，`--no-verify` 才只信账本；
  * 只把托管块（books:downloads:begin … end）写进 index.md，块外一个字不动；
    块里每行带「大小 + sha256 前 8 位 + mtime」（完整 hash 在账本里）；
  * 写 index.md + 账本 -> commit -> push -> 再上传（推送前链接就已经就位）；
  * --pull：**以 index.md 托管块为准**（tag / 大小 / sha256 前 8 位 / mtime），
    账本补全 sha256；`gh release view` 列出全部分卷 -> 并发下载到
    `.<pdf>.gbb_tar_dir` -> 校验分卷齐全 -> cat 拼接 -> tar -xJf -> 校验 sha256
    -> 落到原路径 -> 清理临时目录。旧形态（release 里只有一个 .pdf 资产）直接下载。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

BEGIN_MARK = "<!-- books:downloads:begin （由 books/gbb 自动生成，别手改这块） -->"
END_MARK = "<!-- books:downloads:end -->"
LEDGER_NAME = ".books-releases.json"
DEFAULT_REPO = "allinkernel/wblog-books"
DEFAULT_BRANCH = "main"
MAX_TAG_LEN = 100
URL_TMPL = "https://github.com/{repo}/releases/tag/{tag}"
# 上传资产走 GitHub 的 uploads 域名（不是 api.github.com）；release id 先建空 release 拿到。
UPLOAD_API_TMPL = "https://uploads.github.com/repos/{repo}/releases/{release_id}/assets?name={name}"
# 分卷：单个分卷上限 10 MiB（split -b 10M = 10*1024*1024），超过就切。
PART_BYTES = 10 * 1024 * 1024
SPLIT_SIZE_ARG = "10M"
# xz 用多线程（-T0 = 所有核）；-9 太慢，-6 是默认档，压缩率已经够。
XZ_PROGRAM = "xz -6 -T0"
ARCHIVE_SUFFIX = ".tar.xz"
TEMP_DIR_SUFFIX = ".gbb_tar_dir"
MANIFEST_NAME = ".gbb-archive.json"
CONTENT_TYPE = "application/octet-stream"
DEFAULT_JOBS = 4
# `gh release view` 对"release 不存在"的说法：stderr 一行 `release not found`，
# 或者 HTTP 404。别把网络 / 权限错误也当成"没有 release"，那是两回事。
NOT_FOUND_RE = re.compile(r"(?:\b404\b|not found)", re.IGNORECASE)
PART_SUFFIX_RE = re.compile(r"\.part\.(\d+)$")

DEBUG = False
PRINT_LOCK = threading.Lock()


class ToolError(Exception):
    """可以直接展示给用户的错误。"""


@dataclass
class Item:
    pdf: Path
    rel: str          # 相对 books/ 的 POSIX 路径
    index_dir: Path
    index_rel: str    # 相对 books/ 的 index.md 路径
    sha: str
    mtime: str        # ISO8601 到秒
    mtime_text: str   # 展示成 `2026-11-26 21:40`
    size: int
    tag: str
    url: str
    kind: str         # new / changed / reupload / skip
    reason: str = ""          # changed / reupload 的人话原因（进计划、提交信息）
    old: dict | None = None
    is_draft: bool = False
    upload_mode: str = "unknown"   # create / upload / unknown（规划阶段已知的补传路径）
    verify_label: str = ""         # skip 时：空 = 真跳过；--no-verify = 没核对
    archive_info: "ArchiveInfo | None" = None


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


def human_size_mb(num: int) -> str:
    """十进制大小，和 GitHub release 页 / 下载器显示的口径一致（52.1 MB）。"""
    value = float(num)
    if value < 1000:
        return f"{int(value)} B"
    if value < 1000 ** 2:
        return f"{value / 1000:.1f} KB"
    if value < 1000 ** 3:
        return f"{value / 1000 ** 2:.1f} MB"
    return f"{value / 1000 ** 3:.2f} GB"


def human_rate(bytes_per_sec: float) -> str:
    if bytes_per_sec < 1000:
        return f"{bytes_per_sec:.0f} B/s"
    if bytes_per_sec < 1000 ** 2:
        return f"{bytes_per_sec / 1000:.1f} KB/s"
    return f"{bytes_per_sec / 1000 ** 2:.1f} MB/s"


def elapsed_text(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    total = int(round(seconds))
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def upload_speed_text(size: int, seconds: float) -> str:
    if seconds <= 0:
        return "速度未知"
    return f"{size / 1e6 / seconds:.2f} MB/s"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mtime_info(path: Path) -> tuple[str, str]:
    """(ISO8601 到秒, 展示成 `YYYY-MM-DD HH:MM`)；都用本地时区。"""
    dt = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return dt.isoformat(timespec="seconds"), dt.strftime("%Y-%m-%d %H:%M")


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
        # `.ABC.pdf.gbb_tar_dir` 是压缩/下载的临时目录，里面的 PDF 不算书。
        dirnames[:] = sorted(
            d for d in dirnames
            if d != ".git" and not d.endswith(TEMP_DIR_SUFFIX)
        )
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
# 压缩 / 分卷（临时目录 .<pdf>.gbb_tar_dir）
# ---------------------------------------------------------------------------

@dataclass
class ArchivePart:
    path: Path
    name: str
    size: int


@dataclass
class ArchiveInfo:
    tmp_dir: Path
    archive: Path
    archive_name: str
    parts: list[ArchivePart]
    reused: bool = False

    @property
    def total_size(self) -> int:
        return sum(part.size for part in self.parts)

    def as_ledger(self) -> dict:
        return {
            "name": self.archive_name,
            "size": self.archive.stat().st_size,
            "parts": [{"name": part.name, "size": part.size} for part in self.parts],
        }


def tmp_dir_for(pdf: Path) -> Path:
    return pdf.parent / f".{pdf.name}{TEMP_DIR_SUFFIX}"


def archive_name_for(pdf: Path) -> str:
    return pdf.name + ARCHIVE_SUFFIX


def load_archive_manifest(tmp: Path, pdf: Path, sha: str, mtime: str) -> ArchiveInfo | None:
    """读 `.gbb-archive.json`；只有 sha256 与 mtime 都对上、文件也齐才允许复用。

    复用分卷的前提是"还是同一个版本的 PDF"：mtime 或 sha256 任一不同就重压，
    免得把上一次的旧压缩包当成这次的内容传上去。
    """
    path = tmp / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("pdf") != pdf.name or data.get("sha256") != sha or data.get("mtime") != mtime:
        return None
    archive_meta = data.get("archive")
    if not isinstance(archive_meta, dict):
        return None
    archive_name = archive_meta.get("name")
    if not isinstance(archive_name, str) or Path(archive_name).name != archive_name:
        return None
    archive = tmp / archive_name
    if not archive.is_file():
        return None
    archive_size = archive_meta.get("size")
    if not isinstance(archive_size, int) or isinstance(archive_size, bool):
        return None
    if archive.stat().st_size != archive_size:
        return None
    parts: list[ArchivePart] = []
    raw_parts = data.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        return None
    for raw in raw_parts:
        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        size = raw.get("size")
        if not isinstance(name, str) or Path(name).name != name:
            return None
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            return None
        part_path = tmp / name
        if not part_path.is_file() or part_path.stat().st_size != size:
            return None
        parts.append(ArchivePart(part_path, name, size))
    return ArchiveInfo(tmp, archive, archive_name, parts, reused=True)


def write_archive_manifest(info: ArchiveInfo, pdf: Path, sha: str, mtime: str) -> None:
    data = {
        "version": 1,
        "pdf": pdf.name,
        "sha256": sha,
        "mtime": mtime,
        "archive": {"name": info.archive_name, "size": info.archive.stat().st_size},
        "parts": [{"name": part.name, "size": part.size} for part in info.parts],
    }
    (info.tmp_dir / MANIFEST_NAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def prepare_archive(pdf: Path, sha: str, mtime: str) -> ArchiveInfo:
    """把 PDF 压成 <pdf>.tar.xz（xz -6 -T0），超过 10 MiB 再分卷；能复用就复用。"""
    tmp = tmp_dir_for(pdf)
    reused = load_archive_manifest(tmp, pdf, sha, mtime)
    if reused is not None:
        names = " / ".join(part.name for part in reused.parts[:3])
        more = "" if len(reused.parts) <= 3 else f" …（共 {len(reused.parts)} 个）"
        print(f"  复用上次留下的分卷（sha256 与 mtime 都没变，不重新压缩）：{names}{more}")
        return reused

    if tmp.exists():
        if not (tmp.is_dir() and tmp.name.endswith(TEMP_DIR_SUFFIX)):
            raise ToolError(f"临时路径 {tmp} 不是 gbb 的临时目录，不敢删")
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    archive_name = archive_name_for(pdf)
    archive = tmp / archive_name

    print(f"  压缩：{pdf.name} → .{pdf.name}{TEMP_DIR_SUFFIX}/{archive_name}")
    started = time.monotonic()
    tar_cmd = [
        "tar", "-C", str(pdf.parent),
        f"--use-compress-program={XZ_PROGRAM}",
        "-cf", str(archive), "--", pdf.name,
    ]
    proc = run(tar_cmd)
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        detail = (proc.stderr or proc.stdout).strip()
        raise ToolError(
            f"tar / xz 压缩失败（{XZ_PROGRAM}）：\n{detail or '（没有输出）'}\n"
            "  确认本机有 tar 与 xz（xz 要支持 -T0 多线程）。"
        )
    archive_size = archive.stat().st_size
    print(f"  压缩完成：{human_size_mb(archive_size)}，用时 {elapsed_text(time.monotonic() - started)}")

    part_paths: list[Path] = [archive]
    if archive_size > PART_BYTES:
        split_cmd = [
            "split", "-b", SPLIT_SIZE_ARG, "-d", "-a", "2",
            str(archive), str(archive) + ".part.",
        ]
        proc = run(split_cmd)
        if proc.returncode != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            detail = (proc.stderr or proc.stdout).strip()
            raise ToolError(f"split 分卷失败：\n{detail or '（没有输出）'}")
        part_paths = sorted(
            (p for p in tmp.iterdir() if p.is_file() and p.name.startswith(archive_name + ".part.")),
            key=lambda p: p.name,
        )
        if len(part_paths) < 2:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ToolError("产物超过 10 MiB，但 split 之后没有拿到多个分卷")
        print(f"  分卷：{len(part_paths)} 个（每个 ≤ 10 MiB）")

    parts = [ArchivePart(path, path.name, path.stat().st_size) for path in part_paths]
    for part in parts:
        if part.size > PART_BYTES:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ToolError(f"分卷 {part.name} 有 {human_size_mb(part.size)}，超过 10 MiB 上限")
    info = ArchiveInfo(tmp, archive, archive_name, parts)
    write_archive_manifest(info, pdf, sha, mtime)
    total = human_size_mb(info.total_size)
    print(f"  待上传 {len(parts)} 个资产，共 {total}")
    return info


def cleanup_temp(info: ArchiveInfo, keep: bool) -> None:
    if keep:
        print(f"  保留临时目录：{info.tmp_dir}")
        return
    if info.tmp_dir.is_dir() and info.tmp_dir.name.endswith(TEMP_DIR_SUFFIX):
        shutil.rmtree(info.tmp_dir, ignore_errors=True)
        print(f"  已清理临时目录：{info.tmp_dir.name}")


# ---------------------------------------------------------------------------
# 远端核对（gh release view）—— 账本只当线索，远端才是事实
# ---------------------------------------------------------------------------

@dataclass
class RemoteRelease:
    state: str                 # ok / no-release / error
    detail: str = ""
    is_draft: bool = False
    assets: list = field(default_factory=list)


@dataclass
class BookAssets:
    """一个 release 里属于这本书的资产形态。"""

    form: str                   # parts / single / pdf / none
    parts: list = field(default_factory=list)        # parts 形态：按 .part.NN 排好序
    part_indexes: list[int] = field(default_factory=list)
    single: dict | None = None                       # single 形态：一个 <pdf>.tar.xz
    pdf: dict | None = None                          # 旧形态：一个裸 .pdf


@dataclass
class RemoteCheck:
    state: str                 # ok / no-release / no-asset / form-changed / incomplete / draft / error
    detail: str = ""
    form: str = ""
    is_draft: bool = False


def asset_name_key(name: str) -> str:
    """GitHub 会把资产名里 [A-Za-z0-9._-] 之外的字符换成 '.'。

    实测：本地 `INTEL 80386 PROGRAMMER'S REFERENCE MANUAL 1986.pdf` 上传后，
    远端资产名是 `INTEL.80386.PROGRAMMER.S.REFERENCE.MANUAL.1986.pdf`。
    所以"按文件名匹配"要先做同样的归一化。
    """
    return "".join(ch if (ch.isalnum() or ch in ".-_") else "." for ch in name)


def fetch_remote_release(repo: str, tag: str) -> RemoteRelease:
    """读 `gh release view <tag> --repo <repo> --json tagName,assets,isDraft`。

    只读。三种结果分清楚：
      * ok         —— release 在（是不是草稿看 is_draft）；
      * no-release —— gh 明确说 release 不存在；
      * error      —— 网络 / 权限 / 输出坏了：调用方必须按"未上传"处理，不能静默跳过。
    """
    try:
        proc = run(["gh", "release", "view", tag, "--repo", repo,
                    "--json", "tagName,assets,isDraft"])
    except ToolError as exc:
        return RemoteRelease("error", str(exc))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if NOT_FOUND_RE.search(detail):
            return RemoteRelease("no-release", detail or "release not found")
        return RemoteRelease("error", detail or f"gh release view 退出码 {proc.returncode}")
    raw = proc.stdout.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return RemoteRelease("error", f"gh release view 输出不是合法 JSON：{raw[:200]!r}")
    if not isinstance(data, dict):
        return RemoteRelease("error", "gh release view 输出的 JSON 顶层不是对象")
    assets = data.get("assets")
    if not isinstance(assets, list):
        return RemoteRelease("error", "gh release view 输出里没有 assets 数组")
    return RemoteRelease("ok", "", bool(data.get("isDraft")), assets)


def classify_book_assets(assets: list, pdf_name: str) -> BookAssets:
    """把 release 的资产分成：分卷 / 单个 tar.xz / 旧版裸 pdf / 什么都没有。"""
    key = asset_name_key(pdf_name)
    tar_key = key + ARCHIVE_SUFFIX

    numbered: list[tuple[int, dict]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset_name_key(str(asset.get("name") or ""))
        if not name.startswith(tar_key + ".part."):
            continue
        match = PART_SUFFIX_RE.search(name)
        numbered.append((int(match.group(1)) if match else -1, asset))
    if numbered:
        numbered.sort(key=lambda pair: pair[0])
        return BookAssets(
            "parts",
            parts=[asset for _idx, asset in numbered],
            part_indexes=[idx for idx, _asset in numbered],
        )

    for asset in assets:
        if isinstance(asset, dict) and asset_name_key(str(asset.get("name") or "")) == tar_key:
            return BookAssets("single", single=asset)
    for asset in assets:
        if isinstance(asset, dict) and asset_name_key(str(asset.get("name") or "")) == key:
            return BookAssets("pdf", pdf=asset)
    return BookAssets("none")


def _asset_size(asset: dict) -> int | None:
    try:
        value = int(asset.get("size"))
    except (TypeError, ValueError):
        return None
    return value


def _expected_remote_items(book: BookAssets) -> list[dict]:
    if book.form == "parts":
        return list(book.parts)
    if book.form == "single" and book.single:
        return [book.single]
    if book.form == "pdf" and book.pdf:
        return [book.pdf]
    return []


def check_remote_book(repo: str, tag: str, pdf_name: str, old: dict | None) -> RemoteCheck:
    """核对账本里这本书在远端是否完好：release 在、不是草稿、分卷形态和大小对得上。"""
    meta = fetch_remote_release(repo, tag)
    if meta.state == "error":
        return RemoteCheck("error", meta.detail)
    if meta.state == "no-release":
        return RemoteCheck("no-release", meta.detail)
    book = classify_book_assets(meta.assets, pdf_name)
    if meta.is_draft:
        detail = "release 是草稿（未发布）"
        if book.form == "none":
            detail = "release 是草稿且没有资产"
        return RemoteCheck("draft", detail, form=book.form, is_draft=True)
    if book.form == "none":
        return RemoteCheck("no-asset", "release 里没有这个文件", form="none")
    if book.form == "pdf":
        return RemoteCheck(
            "form-changed",
            "资产形态变了（旧版是单个 .pdf，现在传 .tar.xz / 分卷）",
            form="pdf",
        )

    remote_items = _expected_remote_items(book)
    if book.form == "parts":
        indexes = book.part_indexes
        if indexes != list(range(len(indexes))):
            missing = sorted(set(range(indexes[-1] + 1)) - set(indexes)) if indexes else []
            detail = "分卷不齐"
            if missing:
                detail += "，缺 " + ", ".join(f".part.{n:02d}" for n in missing)
            return RemoteCheck("incomplete", detail, form="parts")

    expected = (old or {}).get("archive") or {}
    expected_parts = expected.get("parts") if isinstance(expected, dict) else None
    if isinstance(expected_parts, list) and expected_parts:
        if len(expected_parts) != len(remote_items):
            return RemoteCheck(
                "incomplete",
                f"分卷数量不一致（账本 {len(expected_parts)} 个，远端 {len(remote_items)} 个）",
                form=book.form,
            )
        for exp, got in zip(expected_parts, remote_items):
            exp_name = asset_name_key(str(exp.get("name") or ""))
            got_name = asset_name_key(str(got.get("name") or ""))
            if exp_name != got_name:
                return RemoteCheck(
                    "incomplete",
                    f"分卷名不一致（账本 {exp.get('name')!r}，远端 {got.get('name')!r}）",
                    form=book.form,
                )
            if _asset_size(got) != exp.get("size"):
                return RemoteCheck(
                    "incomplete",
                    f"分卷大小不一致（{got.get('name')!r}："
                    f"账本 {exp.get('size')}，远端 {_asset_size(got)}）",
                    form=book.form,
                )
    total = sum(_asset_size(asset) or 0 for asset in remote_items)
    detail = f"{len(remote_items)} 个资产，共 {human_size_mb(total)}"
    return RemoteCheck("ok", detail, form=book.form)


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


ITEM_LINE_RE = re.compile(
    r"^\s*\d+\.\s+\[(.*)\]\((\S+)\)(?:\s+(.*?))?\s*$"
)


@dataclass
class BlockEntry:
    """index.md 托管块里的一行：`N. [label](url) · 大小 · sha256:xxxxxxxx · mtime`。"""

    label: str
    url: str
    extra: str = ""   # 链接后面那段（大小 / hash / mtime）；旧格式的行可能为空，原样保留


@dataclass
class BlockExtra:
    size_text: str = ""
    sha_prefix: str = ""
    mtime_text: str = ""


def parse_block_extra(extra: str) -> BlockExtra:
    parsed = BlockExtra()
    for token in (extra or "").split("·"):
        text = token.strip()
        if not text:
            continue
        if text.lower().startswith("sha256:"):
            parsed.sha_prefix = text.split(":", 1)[1].strip()
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", text):
            parsed.mtime_text = text
        elif re.fullmatch(r"[\d.]+\s*[KMG]?B", text, re.IGNORECASE):
            parsed.size_text = text
    return parsed


def parse_block_items(text: str) -> list[BlockEntry] | None:
    """读出托管块里的条目；没有 begin 标记返回 None，空块返回 []。"""
    if BEGIN_MARK not in text:
        return None
    if END_MARK not in text:
        return []
    start = text.index(BEGIN_MARK) + len(BEGIN_MARK)
    end = text.index(END_MARK, start)
    entries: list[BlockEntry] = []
    for line in text[start:end].splitlines():
        match = ITEM_LINE_RE.match(line)
        if not match:
            continue
        label = match.group(1).replace("\\[", "[").replace("\\]", "]")
        extra = (match.group(3) or "").strip()
        entries.append(BlockEntry(label, match.group(2), extra))
    return entries


def render_block(items: list[Item], index_dir: Path,
                 preserved: list[tuple[str, BlockEntry]] = ()) -> str:
    """渲染托管块。

    每行：`N. [相对路径](release 链接) · <十进制大小> · sha256:<前 8 位> · <mtime>`
    （大小用十进制，和 release 页一致；完整 hash 在账本里）。

    preserved 是 [(rel, BlockEntry)]：扫描范围之外、但本来就记在这个 index.md 块里的
    旧条目。cwd 之上的 index.md 被更新时用它保住范围外的链接和它们原来的尾巴，
    免得整块被覆盖掉。
    """
    rows: dict[str, tuple[str, str, str]] = {}
    for item in items:
        extra = (
            f"· {human_size_mb(item.size)} · sha256:{item.sha[:8]} · {item.mtime_text}"
        )
        rows[item.rel] = (rel_posix(item.pdf, index_dir), item.url, extra)
    for rel, entry in preserved:
        rows.setdefault(rel, (entry.label, entry.url, entry.extra))
    lines = [BEGIN_MARK, "# 下载链接"]
    for num, rel in enumerate(sorted(rows), 1):
        label, url, extra = rows[rel]
        label = label.replace("[", "\\[").replace("]", "\\]")
        line = f"{num}. [{label}]({url})"
        if extra:
            line += " " + extra
        lines.append(line)
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
  3) 网络 / 代理：确认能访问 https://api.github.com 和 https://uploads.github.com
  4) 仓库和权限：确认 {repo} 已创建，且当前账号对它至少有写权限
  5) 分卷都是 10 MiB，不会碰到 GitHub 单个资产 2GB 的上限
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
# 参数 / 计划输出
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="books/gbb",
        description="把 books/ 下的 PDF 压成 tar.xz（过大分卷）发到 GitHub Release，"
                    "并把下载链接写回最近 index.md 的托管块。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印计划：不写文件、不压缩、不提交、不推送、不上传")
    parser.add_argument("--pull", action="store_true",
                        help="按 index.md 托管块下载当前目录范围内本地缺失的 PDF"
                             "（不改 index.md、不提交、不推送）")
    parser.add_argument("--all", action="store_true",
                        help="扫描整个仓库（默认只扫描当前工作目录及其子目录）")
    parser.add_argument("--yes", action="store_true",
                        help="跳过所有询问（两处都按默认答案执行）")
    parser.add_argument("--no-verify", action="store_true",
                        help="跳过远端核对，只信本地账本（默认每次都会 gh release view 核对远端）")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"并发上传/下载的分卷数（默认 {DEFAULT_JOBS}；"
                             f"1 = 单个分卷一条真实进度条）")
    parser.add_argument("--keep-temp", action="store_true",
                        help="上传成功后保留 .<pdf>.gbb_tar_dir（默认清理；失败时总会保留）")
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help=f"GitHub 仓库 owner/name（默认 {DEFAULT_REPO}）")
    parser.add_argument("--branch", default=DEFAULT_BRANCH,
                        help=f"要提交/推送的分支（默认 {DEFAULT_BRANCH}）")
    parser.add_argument("--verbose", action="store_true",
                        help="打印实际执行的命令与远端核对细节")
    parser.add_argument("--debug", action="store_true",
                        help="同 --verbose（保留兼容）")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", args.repo):
        parser.error("--repo 的格式应该是 owner/name，例如 allinkernel/wblog-books")
    if args.jobs < 1 or args.jobs > 64:
        parser.error("--jobs 要在 1..64 之间")
    return args


def item_status_text(item: Item) -> str:
    """计划里每个 PDF 的方括号状态。"""
    if item.kind == "new":
        return "新增"
    if item.kind == "changed":
        return f"变更：{item.reason}"
    if item.kind == "reupload":
        return f"需补传：{item.reason}"
    if item.verify_label == "--no-verify":
        return "跳过（未核对远端，--no-verify）"
    return "跳过"


def print_items(items: list[Item]) -> None:
    print(f"扫描到 {len(items)} 个 PDF：")
    for item in items:
        print(f"  [{item_status_text(item)}] {item.rel}")
        print(f"         归属：{item.index_rel}")
        if item.kind == "reupload":
            print(f"         tag ：{item.tag}（补传继续用这个旧 tag，不新建）")
        else:
            print(f"         tag ：{item.tag}")
        print(f"         mtime：{item.mtime_text} · sha256:{item.sha[:8]}")
        if item.kind == "changed" and item.old and item.old.get("tag"):
            print(f"         旧 tag：{item.old['tag']}（保留在 GitHub 上，不删）")


def shq(value: object) -> str:
    return shlex.quote(str(value))


def print_upload_plan(item: Item, args: argparse.Namespace) -> None:
    """上传这一步会执行的关键命令（dry-run 只打印，不压缩、不跑 curl）。"""
    tmp = tmp_dir_for(item.pdf)
    archive = tmp / archive_name_for(item.pdf)
    print(f"  [{item.rel}]")
    print(f"  tar -C {shq(item.pdf.parent)} --use-compress-program={shq(XZ_PROGRAM)} "
          f"-cf {shq(archive)} -- {shq(item.pdf.name)}")
    print(f"  split -b {SPLIT_SIZE_ARG} -d -a 2 {shq(archive)} {shq(str(archive) + '.part.')}"
          f"  （产物 > 10 MiB 时）")
    if item.upload_mode == "create":
        print(f"  gh release create {item.tag} --repo {args.repo} --draft "
              f"--title <...> --notes <...>")
    elif item.upload_mode == "unknown":
        print(f"  gh release create {item.tag} --repo {args.repo} --draft "
              f"--title <...> --notes <...>  （release 不存在时）")
    print(f"  gh release view {item.tag} --repo {args.repo} --json databaseId,isDraft")
    print(f"  curl -T <分卷> -X POST -H 'Content-Type: {CONTENT_TYPE}' -K - --fail "
          f"'https://uploads.github.com/repos/{args.repo}/releases/<id>/assets?name=<分卷名>'"
          f"  （--jobs {args.jobs} 并发）")
    if item.upload_mode != "upload" or item.is_draft:
        print(f"  gh release edit {item.tag} --repo {args.repo} --draft=false")


def print_plan(items: list[Item], todo: list[Item], root: Path, args: argparse.Namespace,
               index_changes: dict[Path, str], ledger_changed: bool) -> None:
    print_items(items)

    print("\n计划写入：")
    for path in sorted(index_changes, key=lambda p: rel_posix(p, root)):
        print(f"  - {rel_posix(path, root)}（更新「下载链接」托管块）")
    if ledger_changed:
        print(f"  - {LEDGER_NAME}（记录 {len(todo)} 条发布账）")

    print("\n将执行：")
    rels = sorted({rel_posix(p, root) for p in index_changes} | ({LEDGER_NAME} if ledger_changed else set()))
    print(f"  git -C {shq(root)} add -- {' '.join(shq(r) for r in rels)}")
    print(f"  git -C {shq(root)} commit -F <提交信息> -- {' '.join(shq(r) for r in rels)}")
    print(f"  git -C {shq(root)} push origin {args.branch}")
    for item in todo:
        print_upload_plan(item, args)


def build_commit_message(todo: list[Item]) -> str:
    new_items = [it for it in todo if it.kind == "new"]
    changed_items = [it for it in todo if it.kind == "changed"]
    reupload_items = [it for it in todo if it.kind == "reupload"]
    if reupload_items and not new_items and not changed_items:
        lines = [f"books: 补传 {len(reupload_items)} 个 PDF 到 Release", ""]
    else:
        lines = [f"books: 发布 {len(todo)} 个 PDF 到 Release", ""]
    if new_items:
        lines.append(f"新增 {len(new_items)} 个：")
        for item in new_items:
            lines.append(f"- {item.rel}")
            lines.append(f"  tag: {item.tag}")
            lines.append(f"  mtime: {item.mtime_text} · sha256: {item.sha[:12]}")
    if changed_items:
        if new_items:
            lines.append("")
        lines.append(f"变更 {len(changed_items)} 个：")
        for item in changed_items:
            lines.append(f"- {item.rel}")
            lines.append(f"  tag: {item.tag}（旧 tag: {item.old.get('tag') if item.old else '无'}）")
            lines.append(f"  原因: {item.reason} · mtime: {item.mtime_text}")
    if reupload_items:
        if new_items or changed_items:
            lines.append("")
        lines.append(f"补传 {len(reupload_items)} 个（复用账本里的旧 tag）：")
        for item in reupload_items:
            lines.append(f"- {item.rel}")
            lines.append(f"  tag: {item.tag}（{item.reason}）")
    lines.append("")
    lines.append("index.md 的「下载链接」块由 books/gbb 自动生成，别手改；")
    lines.append("资产是 .tar.xz（过大按 10 MiB 分卷），旧 release 不删。")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 上传：压缩产物（分卷）并发 curl
# ---------------------------------------------------------------------------

def fetch_release_id(repo: str, tag: str) -> tuple[int | None, bool, str]:
    """取 release 的 databaseId（curl 上传要用）+ isDraft。只读。"""
    try:
        proc = run(["gh", "release", "view", tag, "--repo", repo,
                    "--json", "databaseId,isDraft"])
    except ToolError as exc:
        return None, False, str(exc)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return None, False, detail or f"gh release view 退出码 {proc.returncode}"
    try:
        data = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return None, False, f"gh release view 输出不是合法 JSON：{proc.stdout.strip()[:200]!r}"
    if not isinstance(data, dict):
        return None, False, "gh release view 输出的 JSON 顶层不是对象"
    release_id = data.get("databaseId")
    if not isinstance(release_id, int) or isinstance(release_id, bool):
        return None, False, f"release 没有 databaseId：{release_id!r}"
    return release_id, bool(data.get("isDraft")), ""


def gh_auth_token() -> str:
    """从 `gh auth token` 取 token。

    ⚠️ 只放内存：不打印、不进 debug 日志、**不进 curl 的 argv**（见 _curl_upload）。
    `gh auth token` 的输出里就是凭据，所以这里故意不走会打日志的 run()。
    """
    try:
        proc = subprocess.run(["gh", "auth", "token"], text=True, capture_output=True)
    except FileNotFoundError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


class UploadProgress:
    """并发上传时的汇总进度行：只统计**已完成**的分卷，绝不编造。"""

    def __init__(self, total_parts: int, total_bytes: int, tty: bool):
        self.total_parts = total_parts
        self.total_bytes = total_bytes
        self.tty = tty
        self.interval = 2.0 if tty else 10.0
        self.done_parts = 0
        self.done_bytes = 0
        self.started = time.monotonic()
        self.last_print = 0.0
        self.width = 0
        self.lock = threading.Lock()

    def _line(self) -> str:
        elapsed = max(time.monotonic() - self.started, 1e-9)
        rate = self.done_bytes / elapsed
        return (f"已完成 {self.done_parts}/{self.total_parts} 个分卷 · "
                f"已传 {human_size_mb(self.done_bytes)} / {human_size_mb(self.total_bytes)} · "
                f"{human_rate(rate)}")

    def render(self, force: bool = False, final: bool = False) -> None:
        with self.lock:
            now = time.monotonic()
            if not force and not final and now - self.last_print < self.interval:
                return
            self.last_print = now
            text = self._line()
            if self.tty:
                pad = max(0, self.width - len(text))
                sys.stdout.write("\r" + text + " " * pad)
                if final:
                    sys.stdout.write("\n")
                sys.stdout.flush()
                self.width = max(self.width, len(text))
            else:
                print("  " + text, flush=True)

    def add(self, nbytes: int) -> None:
        with self.lock:
            self.done_parts += 1
            self.done_bytes += nbytes
        self.render()

    def ticker(self, stop: threading.Event) -> None:
        while not stop.wait(0.5):
            self.render()


def _curl_upload(args: argparse.Namespace, part: ArchivePart, release_id: int,
                 token: str, progress_bar: bool) -> tuple[bool, str]:
    """单个分卷走 curl 上传。

    ⚠️ token 只写进 `-K -` 从 stdin 读的配置里
    （`header = "Authorization: token <token>"`），**绝不放进 argv**。
    进度条画在 stderr 上：progress_bar=True 时不捕获 stderr，让它直接进终端；
    并发时不画进度条（`-sS`），stderr 捕获下来只用于报错。
    """
    asset_name = asset_name_key(part.name)
    upload_url = UPLOAD_API_TMPL.format(
        repo=args.repo, release_id=release_id, name=quote(asset_name, safe=""),
    )
    config_text = f'header = "Authorization: token {token}"\n'
    cmd = ["curl"]
    cmd += ["--progress-bar"] if progress_bar else ["-sS"]
    cmd += [
        "-T", str(part.path), "-X", "POST",
        "-H", f"Content-Type: {CONTENT_TYPE}",
        "-K", "-", "--fail", upload_url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=config_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=None if progress_bar else subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return False, f"找不到 curl：{exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() if not progress_bar else ""
        if detail:
            detail = detail.splitlines()[-1][:300]
        return False, detail or f"curl 退出码 {proc.returncode}"
    return True, ""


def _gh_upload(args: argparse.Namespace, item: Item, part: ArchivePart) -> tuple[bool, str]:
    proc = run(["gh", "release", "upload", item.tag, str(part.path),
                "--repo", args.repo, "--clobber"])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return False, detail or f"gh 退出码 {proc.returncode}"
    return True, ""


def _upload_one(args: argparse.Namespace, item: Item, part: ArchivePart,
                release_id: int, token: str, use_curl: bool,
                force_gh: bool, progress_bar: bool) -> tuple[bool, str, str]:
    """传一个分卷：优先 curl；没有 curl / 失败 / 需要覆盖时退回 gh --clobber。"""
    if use_curl and not force_gh:
        ok, why = _curl_upload(args, part, release_id, token, progress_bar)
        if ok:
            return True, "", "curl"
        with PRINT_LOCK:
            print(f"  ⚠️ {part.name}：curl 上传失败（{why}），改用 gh release upload --clobber")
    ok, why = _gh_upload(args, item, part)
    return ok, "" if ok else why, "gh"


def verify_remote_parts(args: argparse.Namespace, item: Item,
                        archive: ArchiveInfo) -> tuple[bool, str]:
    """传完再核一次远端：每个分卷都在、大小都对。核对失败只警告，不误判上传失败。"""
    meta = fetch_remote_release(args.repo, item.tag)
    if meta.state == "error":
        print(f"  ⚠️ 上传后核对失败（{meta.detail or 'gh 没输出原因'}），按已上传处理")
        return True, ""
    if meta.state == "no-release":
        return False, "上传后远端还是找不到这个 release"
    book = classify_book_assets(meta.assets, item.pdf.name)
    remote = {}
    for asset in _expected_remote_items(book):
        remote[asset_name_key(str(asset.get("name") or ""))] = _asset_size(asset)
    for part in archive.parts:
        if remote.get(asset_name_key(part.name)) != part.size:
            return False, f"远端资产缺失或大小不符：{part.name}"
    return True, ""


def _publish_draft_if_needed(args: argparse.Namespace, item: Item,
                             is_draft: bool) -> tuple[bool, str]:
    if not is_draft:
        return True, ""
    print("  release 是草稿，上传完成后发布：gh release edit --draft=false")
    edit = run(["gh", "release", "edit", item.tag, "--repo", args.repo,
                "--draft=false"])
    if edit.returncode != 0:
        detail = (edit.stderr or edit.stdout).strip()
        print(f"  失败：文件传上去了，但草稿没能发布：{detail or '（gh 没输出原因）'}")
        print("  草稿没有公开，下载链接对别人仍然不可用；没有留半成品，重跑会再补。")
        return False, f"草稿发布失败：{detail or 'gh 返回非 0'}"
    print("  草稿已发布，下载链接现在可用了。")
    return True, ""


def upload_archive(args: argparse.Namespace, item: Item, archive: ArchiveInfo,
                   release_id: int, is_draft: bool, token: str) -> tuple[bool, str]:
    """把一个 release 的全部分卷传上去（并发），必要时发布草稿。"""
    use_curl = bool(token) and shutil.which("curl") is not None
    if not use_curl:
        print("  没有 curl / 取不到 token，改用 gh release upload（没有进度条）")

    meta = fetch_remote_release(args.repo, item.tag)
    remote_assets = meta.assets if meta.state == "ok" else []
    remote_map: dict[str, dict] = {}
    for asset in remote_assets:
        if isinstance(asset, dict):
            remote_map[asset_name_key(str(asset.get("name") or ""))] = asset

    pending: list[ArchivePart] = []
    done_bytes = 0
    done_parts = 0
    force_gh: set[str] = set()
    for part in archive.parts:
        asset = remote_map.get(asset_name_key(part.name))
        if asset is not None and _asset_size(asset) == part.size:
            done_bytes += part.size
            done_parts += 1
            continue
        if asset is not None:
            # 同名资产还在但大小不对：curl 新建会 422，直接走 gh --clobber。
            force_gh.add(part.name)
        pending.append(part)

    if not pending:
        print(f"  {done_parts} 个分卷远端都已在，跳过上传")
    else:
        if done_parts:
            print(f"  {done_parts} 个分卷远端已在，只传缺的 {len(pending)} 个")
        tty = sys.stdout.isatty()
        bar_mode = use_curl and args.jobs == 1 and tty and len(pending) > 0
        started = time.monotonic()
        if bar_mode:
            for part in pending:
                label = f"  上传 {part.name}（{human_size_mb(part.size)}）"
                print(label)
                ok, why, _how = _upload_one(
                    args, item, part, release_id, token, use_curl,
                    part.name in force_gh, progress_bar=True,
                )
                if not ok:
                    return False, f"{part.name} 上传失败：{why}"
            total = sum(part.size for part in pending)
            elapsed = time.monotonic() - started
            print(f"  已上传 {len(pending)} 个分卷 · 共 {human_size_mb(total)} · "
                  f"用时 {elapsed_text(elapsed)}（实测 {upload_speed_text(total, elapsed)}）")
        else:
            progress = UploadProgress(len(archive.parts), archive.total_size, tty)
            if done_parts:
                progress.done_parts = done_parts
                progress.done_bytes = done_bytes
            progress.render(force=True)
            stop = threading.Event()
            ticker = threading.Thread(target=progress.ticker, args=(stop,), daemon=True)
            ticker.start()
            errors: list[tuple[str, str]] = []
            try:
                with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                    futures = {
                        pool.submit(
                            _upload_one,
                            args, item, part, release_id, token, use_curl,
                            part.name in force_gh, False,
                        ): part
                        for part in pending
                    }
                    for future in as_completed(futures):
                        part = futures[future]
                        try:
                            ok, why, _how = future.result()
                        except Exception as exc:  # 线程里的意外也要报出来
                            ok, why = False, str(exc)
                        if ok:
                            progress.add(part.size)
                        else:
                            errors.append((part.name, why or "上传失败"))
            finally:
                stop.set()
                ticker.join(timeout=1.0)
                progress.render(final=True, force=True)
            if errors:
                for name, why in errors:
                    print(f"  ✗ {name}：{why}")
                return False, f"{len(errors)} 个分卷上传失败"

    if is_draft:
        ok, why = _publish_draft_if_needed(args, item, True)
        if not ok:
            return False, why
    ok, why = verify_remote_parts(args, item, archive)
    if not ok:
        return False, why
    return True, ""


def do_uploads(args: argparse.Namespace, root: Path, todo: list[Item]) -> list[tuple[Item, str]]:
    """返回失败列表 [(item, 原因)]。"""
    failures: list[tuple[Item, str]] = []
    token = gh_auth_token()
    for num, item in enumerate(todo, 1):
        print(f"\n[{num}/{len(todo)}] {item.rel}")
        archive = item.archive_info
        if archive is None:
            failures.append((item, "内部错误：没有准备好压缩分卷"))
            break
        with PRINT_LOCK:
            print(f"  上传 {len(archive.parts)} 个资产，共 {human_size_mb(archive.total_size)}"
                  f"（PDF {human_size_mb(item.size)}）")
        notes = (
            f"来源目录：{dir_label(item.pdf.parent, root)}；"
            f"PDF 大小：{human_size_mb(item.size)}；"
            f"sha256：{item.sha[:12]}；mtime：{item.mtime_text}"
        )
        title = f"{dir_label(item.pdf.parent, root)} / {item.pdf.name}"

        # 走 create 还是 upload：规划阶段已经核对过的直接沿用；不知道的（新增/变更、
        # 或核对失败的）上传前再核一次。--no-verify = 只信账本，新 tag 直接 create。
        mode = item.upload_mode
        is_draft = item.is_draft
        if mode == "unknown":
            if args.no_verify:
                mode = "create"
            else:
                meta = fetch_remote_release(args.repo, item.tag)
                if meta.state == "ok":
                    mode, is_draft = "upload", meta.is_draft
                elif meta.state == "no-release":
                    mode, is_draft = "create", False
                else:
                    print("  ⚠️ 上传前核对远端失败，按尚未上传处理（尝试 create）："
                          f"{meta.detail or '（gh 没输出原因）'}")
                    mode, is_draft = "create", False

        release_id: int | None = None
        if mode == "create":
            print("  建 release（--draft，不带资产）")
            create = run(["gh", "release", "create", item.tag, "--repo", args.repo,
                          "--title", title, "--notes", notes, "--draft"])
            if create.returncode != 0:
                detail = (create.stderr or create.stdout).strip()
                release_id, remote_draft, _detail = fetch_release_id(args.repo, item.tag)
                if release_id is None:
                    print(f"  失败：{detail or '（gh 没输出原因）'}")
                    failures.append((item, detail or "gh 返回非 0"))
                    break  # 多半是登录/权限/网络问题，后面的不再硬试
                print("  release 已经存在了，改走上传（不重建）")
                is_draft = is_draft or remote_draft
                mode = "upload"

        if release_id is None:
            release_id, remote_draft, detail = fetch_release_id(args.repo, item.tag)
            if release_id is None:
                print(f"  ⚠️ 取不到 release id（{detail}），没法定 curl 的上传地址")
                failures.append((item, detail or "取不到 release id"))
                break
            is_draft = is_draft or remote_draft

        ok, reason = upload_archive(args, item, archive, release_id, is_draft, token)
        if not ok:
            print(f"  失败：{reason}")
            failures.append((item, reason or "上传失败"))
            break
        cleanup_temp(archive, args.keep_temp)
        print(f"  完成：{item.url}")
    return failures


# ---------------------------------------------------------------------------
# --pull：以 index.md 托管块为准
# ---------------------------------------------------------------------------

@dataclass
class PullRecord:
    rel: str
    tag: str = ""
    size: int | None = None
    size_text: str = ""
    sha_full: str = ""
    sha_prefix: str = ""
    mtime_text: str = ""
    sources: list[str] = field(default_factory=list)
    archive: dict | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class PullJob:
    rec: PullRecord
    target: Path
    tmp: Path
    pdf_name: str
    form: str                     # parts / single / pdf / glob
    asset_names: list[str] = field(default_factory=list)
    asset_sizes: list[int] = field(default_factory=list)
    fallback_glob: str = ""


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


def collect_pull_records(root: Path, scan_root: Path, ledger: dict) -> dict[str, PullRecord]:
    """以 index.md 托管块为准收集下载记录，账本补全 sha256 / 大小。

    index.md 是"已经发布的下载链接"的权威；账本里可能有更完整的 sha256，
    也可能有 index.md 没记到的旧文件（legacy），所以两边并集，index.md 的 tag 优先。
    """
    records: dict[str, PullRecord] = {}

    def get(rel: str) -> PullRecord:
        return records.setdefault(rel, PullRecord(rel=rel))

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
        for entry in entries:
            try:
                rel = (index_dir / entry.label).resolve().relative_to(root.resolve()).as_posix()
            except (OSError, ValueError):
                continue
            if not is_under(root / rel, scan_root):
                continue
            rec = get(rel)
            tag = _tag_from_url(entry.url)
            extra = parse_block_extra(entry.extra)
            source = rel_posix(index_md, root)
            if source not in rec.sources:
                rec.sources.append(source)
            if tag and rec.tag and rec.tag != tag:
                rec.warnings.append(f"{source} 的 tag（{tag}）和已收集到的（{rec.tag}）不一致")
            elif tag and not rec.tag:
                rec.tag = tag
            if extra.sha_prefix and not rec.sha_prefix:
                rec.sha_prefix = extra.sha_prefix
            if extra.size_text and not rec.size_text:
                rec.size_text = extra.size_text
            if extra.mtime_text and not rec.mtime_text:
                rec.mtime_text = extra.mtime_text

    for rel, value in sorted(ledger.items()):
        if not isinstance(value, dict):
            continue
        if not is_under(root / rel, scan_root):
            continue
        rec = get(rel)
        if LEDGER_NAME not in rec.sources:
            rec.sources.append(LEDGER_NAME)
        tag = value.get("tag") or ""
        if tag and rec.tag and rec.tag != tag:
            rec.warnings.append(f"账本的 tag（{tag}）和 index.md 的（{rec.tag}）不一致，按 index.md")
        elif tag and not rec.tag:
            rec.tag = tag
        sha = value.get("sha256") or ""
        if sha:
            if rec.sha_full and rec.sha_full != sha:
                rec.warnings.append("账本里的 sha256 自相矛盾（同一个路径出现两个值）")
            rec.sha_full = sha
        size = value.get("size")
        if isinstance(size, int) and not isinstance(size, bool):
            rec.size = size
        archive = value.get("archive")
        if isinstance(archive, dict):
            rec.archive = archive

    for rec in records.values():
        if rec.sha_prefix and rec.sha_full and not rec.sha_full.startswith(rec.sha_prefix.lower()):
            rec.warnings.append(
                f"index.md 的 sha256 前 8 位（{rec.sha_prefix}）与账本全量 sha256 不一致"
            )
    return records


def _download_asset(args: argparse.Namespace, tag: str, asset_name: str,
                    target_dir: Path, expected_size: int | None) -> tuple[bool, Path | None, str]:
    cmd = ["gh", "release", "download", tag, "--repo", args.repo,
           "--pattern", asset_name, "--dir", str(target_dir), "--clobber"]
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        proc = run(cmd)
    except ToolError as exc:
        return False, None, str(exc)
    path = target_dir / asset_name
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return False, None, detail or f"gh 退出码 {proc.returncode}"
    if not path.is_file():
        return False, None, f"gh 说下载成功，但没找到 {path.name}"
    actual = path.stat().st_size
    if expected_size is not None and actual != expected_size:
        return False, None, f"大小不符（远端 {expected_size}，落盘 {actual}）"
    return True, path, ""


def _verify_pulled_pdf(rec: PullRecord, path: Path) -> tuple[bool, str]:
    if rec.size is not None and path.stat().st_size != rec.size:
        return False, (f"大小不符（记录 {rec.size}，实际 {path.stat().st_size}）")
    actual = sha256_file(path)
    if rec.sha_full:
        if actual != rec.sha_full.lower():
            return False, (f"sha256 校验失败（账本 {rec.sha_full[:12]}…，"
                           f"实际 {actual[:12]}…）")
    elif rec.sha_prefix:
        if not actual.startswith(rec.sha_prefix.lower()):
            return False, (f"sha256 校验失败（index.md 前 8 位 {rec.sha_prefix}，"
                           f"实际 {actual[:8]}）")
    else:
        print("  ⚠️ 记录里没有 sha256，跳过完整性校验")
    return True, actual


def install_downloaded(args: argparse.Namespace, job: PullJob,
                       downloaded: list[Path]) -> tuple[bool, str]:
    """把已经下载到临时目录的资产拼/解/校验，落到原路径，最后清理临时目录。"""
    pdf_name = job.pdf_name
    tmp = job.tmp
    key = asset_name_key(pdf_name)
    tar_key = key + ARCHIVE_SUFFIX

    part_files: list[tuple[int, Path]] = []
    others: list[Path] = []
    for path in downloaded:
        match = re.search(re.escape(tar_key) + r"\.part\.(\d+)$", path.name)
        if match:
            part_files.append((int(match.group(1)), path))
        else:
            others.append(path)
    part_files.sort(key=lambda pair: pair[0])

    if part_files:
        indexes = [idx for idx, _path in part_files]
        if indexes != list(range(len(indexes))):
            missing = sorted(set(range(indexes[-1] + 1)) - set(indexes))
            detail = "分卷缺号"
            if missing:
                detail += "：" + ", ".join(f".part.{n:02d}" for n in missing)
            return False, detail
        expected_parts = (job.rec.archive or {}).get("parts") or []
        if expected_parts and len(expected_parts) != len(part_files):
            return False, (f"分卷数量不一致（账本 {len(expected_parts)} 个，"
                           f"下载到 {len(part_files)} 个）")
        archive = tmp / (pdf_name + ARCHIVE_SUFFIX)
        if archive.exists():
            archive.unlink()
        with open(archive, "wb") as out:
            for _idx, path in part_files:
                with open(path, "rb") as fh:
                    shutil.copyfileobj(fh, out, 1024 * 1024)
        print(f"  已拼接 {len(part_files)} 个分卷：{archive.name}（{human_size_mb(archive.stat().st_size)}）")
        tar_path = archive
    else:
        tar_path = next((p for p in others if p.name == tar_key), None)
        if tar_path is None:
            pdf_path = next((p for p in others if p.name == key), None)
            if pdf_path is None:
                return False, "临时目录里没有可识别的资产（既没有分卷，也没有 .tar.xz / .pdf）"
            ok, info = _verify_pulled_pdf(job.rec, pdf_path)
            if not ok:
                return False, info
            job.target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(pdf_path, job.target)
            shutil.rmtree(tmp, ignore_errors=True)
            return True, info

    extracted = tmp / pdf_name
    if extracted.exists():
        extracted.unlink()
    proc = run(["tar", "-C", str(tmp), "-xJf", str(tar_path)])
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return False, f"tar -xJf 解压失败：{detail or '（没有输出）'}"
    if not extracted.is_file():
        return False, f"解压后没有找到 {pdf_name}"
    ok, info = _verify_pulled_pdf(job.rec, extracted)
    if not ok:
        return False, info
    job.target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(extracted, job.target)
    shutil.rmtree(tmp, ignore_errors=True)
    return True, info


def run_pull_job(args: argparse.Namespace, job: PullJob) -> tuple[bool, str]:
    tmp = job.tmp
    tmp.mkdir(parents=True, exist_ok=True)
    if job.form == "glob":
        cmd = ["gh", "release", "download", job.rec.tag, "--repo", args.repo,
               "--pattern", job.fallback_glob, "--dir", str(tmp), "--clobber"]
        proc = run(cmd)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            return False, detail or f"gh 退出码 {proc.returncode}"
        downloaded = [
            path for path in tmp.iterdir()
            if path.is_file() and path.name.startswith(job.fallback_glob[:-1])
        ]
        if not downloaded:
            return False, "远端没有匹配到任何资产"
        return install_downloaded(args, job, downloaded)

    names = list(job.asset_names)
    sizes = list(job.asset_sizes)
    if not names:
        return False, "远端没有可用资产"
    if len(names) > 1:
        print(f"  并发下载 {len(names)} 个分卷（--jobs {args.jobs}）")
        downloaded: list[Path] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=min(args.jobs, len(names))) as pool:
            futures = {
                pool.submit(_download_asset, args, job.rec.tag, name, tmp, sizes[i]): name
                for i, name in enumerate(names)
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    ok, path, why = future.result()
                except Exception as exc:
                    ok, path, why = False, None, str(exc)
                if ok and path is not None:
                    downloaded.append(path)
                    with PRINT_LOCK:
                        print(f"  ✓ {name}（{human_size_mb(path.stat().st_size)}）")
                else:
                    errors.append(f"{name}：{why or '下载失败'}")
                    with PRINT_LOCK:
                        print(f"  ✗ {name}：{why or '下载失败'}")
        if errors:
            return False, f"{len(errors)} 个分卷下载失败"
        return install_downloaded(args, job, downloaded)

    ok, path, why = _download_asset(args, job.rec.tag, names[0], tmp, sizes[0])
    if not ok or path is None:
        return False, why or "下载失败"
    print(f"  ✓ {names[0]}（{human_size_mb(path.stat().st_size)}）")
    return install_downloaded(args, job, [path])


def print_pull_plan(args: argparse.Namespace, jobs: list[PullJob]) -> None:
    if not jobs:
        return
    print("\n将执行：")
    for job in jobs:
        tag = job.rec.tag
        if job.form == "glob":
            patterns = [job.fallback_glob]
        else:
            patterns = job.asset_names
        for name in patterns:
            print(f"  gh release download {tag} --repo {args.repo} --pattern {shq(name)} "
                  f"--dir {shq(job.tmp)} --clobber")
        if job.form == "glob":
            continue
        if job.form == "parts":
            parts = " ".join(shq(str(job.tmp / name)) for name in job.asset_names)
            print(f"  cat {parts} > {shq(job.tmp / (job.pdf_name + ARCHIVE_SUFFIX))}")
            print(f"  tar -C {shq(job.tmp)} -xJf {shq(job.tmp / (job.pdf_name + ARCHIVE_SUFFIX))}")
            source = job.tmp / job.pdf_name
        elif job.form == "single":
            print(f"  tar -C {shq(job.tmp)} -xJf {shq(job.tmp / job.asset_names[0])}")
            source = job.tmp / job.pdf_name
        else:  # 旧形态：下载下来就是一个裸 .pdf
            source = job.tmp / job.asset_names[0]
        print(f"  sha256sum {shq(source)}")
        print(f"  mv {shq(source)} {shq(job.target)}")


def do_pull(args: argparse.Namespace, root: Path, scan_root: Path, ledger: dict) -> int:
    """--pull：下载 cwd 范围内、index.md / 账本里有记录但本地不存在的 PDF。"""
    records = collect_pull_records(root, scan_root, ledger)
    scope = scope_label(scan_root, root)
    if not records:
        print(f"\n扫描范围（{scope}）里没有找到任何发布记录（index.md 托管块和账本都空）。")
        return 0

    present = [rel for rel in records if (root / rel).is_file()]
    missing = [rel for rel in records if not (root / rel).is_file()]
    print(f"\n发布记录 {len(records)} 条：本地已有 {len(present)} 条。")
    if not missing:
        print(f"全部已在本地（{scope} 范围内 {len(records)} 条记录都不缺）")
        return 0

    print(f"发现 {len(missing)} 个缺失：")
    jobs: list[PullJob] = []
    remote_missing: list[tuple[str, str, str]] = []
    no_tag: list[tuple[str, str]] = []
    for rel in sorted(missing):
        rec = records[rel]
        for warning in rec.warnings:
            print(f"  ⚠️ {rel}：{warning}")
        if not rec.tag:
            print(f"  [缺失] {rel}  ← （没有 tag，无法下载）")
            no_tag.append((rel, "index.md 和账本里都没有 release 链接"))
            continue
        target = root / rel
        tmp = tmp_dir_for(target)
        pdf_name = target.name
        print(f"  [缺失] {rel}  ← {rec.tag}")
        if rec.mtime_text:
            print(f"        mtime：{rec.mtime_text} · sha256：{rec.sha_full[:8] or rec.sha_prefix or '（没记）'}")

        if args.no_verify:
            # 只信 index.md / 账本：按旧形态先试一把（没有分卷时也能下）。
            jobs.append(PullJob(rec, target, tmp, pdf_name, "glob",
                                fallback_glob=asset_name_key(pdf_name) + "*"))
            continue

        meta = fetch_remote_release(args.repo, rec.tag)
        if meta.state == "no-release":
            print(f"  [远端也没有] {rel}  ← {rec.tag}")
            print("        index.md / 账本说发布过，但远端没有这个 release，不能 pull。")
            remote_missing.append((rel, rec.tag, "远端没有这个 release"))
            continue
        if meta.state == "error":
            print(f"  ⚠️ 远端核对失败（{meta.detail or 'gh 没输出原因'}），"
                  f"改按文件名通配下载")
            jobs.append(PullJob(rec, target, tmp, pdf_name, "glob",
                                fallback_glob=asset_name_key(pdf_name) + "*"))
            continue
        book = classify_book_assets(meta.assets, pdf_name)
        if book.form == "none":
            print(f"  [远端也没有] {rel}  ← {rec.tag}")
            print("        release 里没有这本书的资产（分卷 / tar.xz / 旧版 .pdf 都没有）。")
            remote_missing.append((rel, rec.tag, "release 里没有这本书的资产"))
            continue
        if meta.is_draft:
            print(f"  ⚠️ {rel}：release 还是草稿（下载链接对别人不可用），仍尝试下载")

        if book.form == "parts":
            indexes = book.part_indexes
            if indexes != list(range(len(indexes))):
                missing_idx = sorted(set(range(indexes[-1] + 1)) - set(indexes)) if indexes else []
                detail = "分卷不齐"
                if missing_idx:
                    detail += "，缺 " + ", ".join(f".part.{n:02d}" for n in missing_idx)
                print(f"  ✗ {rel}：{detail}（远端资产不完整）")
                remote_missing.append((rel, rec.tag, detail))
                continue
            expected_parts = (rec.archive or {}).get("parts") or []
            if expected_parts and len(expected_parts) != len(book.parts):
                detail = (f"分卷数量不一致（账本 {len(expected_parts)} 个，"
                          f"远端 {len(book.parts)} 个）")
                print(f"  ✗ {rel}：{detail}")
                remote_missing.append((rel, rec.tag, detail))
                continue
            names = [str(asset.get("name") or "") for asset in book.parts]
            sizes = [_asset_size(asset) or 0 for asset in book.parts]
            jobs.append(PullJob(rec, target, tmp, pdf_name, "parts", names, sizes))
            print(f"        远端 {len(names)} 个分卷，共 "
                  f"{human_size_mb(sum(sizes))}")
        elif book.form == "single" and book.single:
            name = str(book.single.get("name") or "")
            jobs.append(PullJob(rec, target, tmp, pdf_name, "single",
                                [name], [_asset_size(book.single) or 0]))
        elif book.form == "pdf" and book.pdf:
            name = str(book.pdf.get("name") or "")
            jobs.append(PullJob(rec, target, tmp, pdf_name, "pdf",
                                [name], [_asset_size(book.pdf) or 0]))

    if args.dry_run:
        print_pull_plan(args, jobs)
        if remote_missing:
            print(f"\n其中 {len(remote_missing)} 个远端也没有，不能 pull：")
            for rel, tag, reason in remote_missing:
                print(f"  ✗ {rel}（tag {tag}）：{reason}")
        print("\n[dry-run] 没有下载任何文件。")
        return 0

    if jobs:
        print()
        if not confirm(
            f"现在下载这 {len(jobs)} 个缺失文件？（只放回本地；不改 index.md、不提交）",
            default_yes=True,
            assume_yes=args.yes,
        ):
            print("已取消，没有下载任何文件。")
            return 0

    print()
    ok_count = 0
    failures: list[tuple[str, str]] = [
        (rel, f"远端也没有：{reason}") for rel, _tag, reason in remote_missing
    ] + [(rel, reason) for rel, reason in no_tag]
    download_failures: list[tuple[str, str]] = []
    for num, job in enumerate(jobs, 1):
        print(f"[{num}/{len(jobs)}] {job.rec.rel}")
        try:
            ok, info = run_pull_job(args, job)
        except ToolError as exc:
            ok, info = False, str(exc)
        if ok:
            print(f"  完成：{job.rec.rel}（{human_size_mb(job.target.stat().st_size)}）"
                  f" · sha256:{info[:8]}")
            ok_count += 1
        else:
            print(f"  失败：{info}")
            failures.append((job.rec.rel, info))
            download_failures.append((job.rec.rel, info))

    print("\n==== 总账（--pull）====")
    print(f"成功 {ok_count} / 失败 {len(failures)} / 本来就在本地 {len(present)}")
    if remote_missing:
        print(f"\n{len(remote_missing)} 个记录远端也没有资产，不能 pull：")
        for rel, tag, reason in remote_missing:
            print(f"  ✗ {rel}（tag {tag}）：{reason}")
        print("  index.md 说发布过，但远端 release 里查不到对应资产；")
        print("  先在能上传的机器上跑 ./gbb 补传（或把这条从 index.md / 账本里删掉），再 --pull。")
    if download_failures:
        for rel, reason in download_failures:
            print(f"  ✗ {rel}：{reason}")
        print_gh_troubleshooting(args.repo, "下载")
        print("下载失败的文件没有放回本地；修好后重跑 ./gbb --pull 即可。")
    if failures:
        return 1
    print("\n全部下载完成。index.md 和账本没有改动，也没有提交/推送。")
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

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

    remote_warnings: list[tuple[Item, str]] = []
    noverify_warnings: list[tuple[Item, str]] = []
    items: list[Item] = []
    for pdf, index_dir in sorted(rows, key=lambda row: rel_posix(row[0], root)):
        rel = rel_posix(pdf, root)
        sha = sha256_file(pdf)
        mtime, mtime_text = mtime_info(pdf)
        size = pdf.stat().st_size
        old = ledger.get(rel) if isinstance(ledger.get(rel), dict) else None
        item = Item(
            pdf=pdf,
            rel=rel,
            index_dir=index_dir,
            index_rel=rel_posix(index_dir / "index.md", root),
            sha=sha,
            mtime=mtime,
            mtime_text=mtime_text,
            size=size,
            tag="",
            url="",
            kind="",
            old=old,
        )
        if old and old.get("sha256"):
            old_tag = old.get("tag") or ""
            old_mtime = old.get("mtime") or ""
            if old.get("sha256") != sha:
                item.kind = "changed"
                item.reason = "sha256 变了"
            elif old_mtime and old_mtime != mtime:
                item.kind = "changed"
                item.reason = "mtime 变了（内容没变）"
            elif not old_tag:
                item.kind = "changed"
                item.reason = "账本里没有 tag"
            else:
                # 账本说"上传过"只是线索：默认必须去远端核对，账本不当事实。
                tag = old_tag
                item.tag = tag
                item.url = old.get("url") or url_for(args.repo, tag)
                if args.no_verify:
                    item.kind = "skip"
                    item.verify_label = "--no-verify"
                    last_state = str(old.get("last_remote_state") or "").strip()
                    if last_state != "published":
                        noverify_warnings.append((item, last_state or "未知（账本里没记）"))
                else:
                    check = check_remote_book(args.repo, tag, pdf.name, old)
                    item.is_draft = check.is_draft
                    if check.state == "ok":
                        item.kind = "skip"
                        debug(f"远端核对通过：{rel}（{check.detail}）")
                    else:
                        item.kind = "reupload"
                        if check.state == "no-release":
                            item.reason = "远端没有 release"
                            item.upload_mode = "create"
                        elif check.state == "no-asset":
                            item.reason = "release 里没有这个文件"
                            item.upload_mode = "upload"
                        elif check.state == "form-changed":
                            item.reason = check.detail
                            item.upload_mode = "upload"
                        elif check.state == "incomplete":
                            item.reason = check.detail
                            item.upload_mode = "upload"
                        elif check.state == "draft":
                            item.reason = check.detail
                            item.upload_mode = "upload"
                        else:  # error：网络 / 权限 / 输出坏了
                            item.reason = "远端核对失败，按未上传处理"
                            item.upload_mode = "unknown"
                            remote_warnings.append((item, check.detail))
            if item.kind == "changed":
                item.tag = make_tag(index_dir, pdf, stamp, tags)
                tags.add(item.tag)
                item.url = url_for(args.repo, item.tag)
                item.upload_mode = "create" if args.no_verify else "unknown"
        else:
            item.kind = "changed" if old else "new"
            item.reason = "账本里没有 sha256（旧格式）" if old else ""
            item.tag = make_tag(index_dir, pdf, stamp, tags)
            tags.add(item.tag)
            item.url = url_for(args.repo, item.tag)
            item.upload_mode = "create" if args.no_verify else "unknown"
        items.append(item)

    if remote_warnings:
        print(f"\n⚠️ 远端核对失败 {len(remote_warnings)} 个（按未上传处理，仍会上传）：")
        for item, detail in remote_warnings:
            print(f"  - {item.rel}")
            print(f"    原因：{detail or '（gh 没输出原因）'}")
            print("    处理：远端核对失败，按未上传处理（宁可重传，也不留死链）。")
    if noverify_warnings:
        print(f"\n⚠️ --no-verify：{len(noverify_warnings)} 个 PDF 没有核对远端，只按账本跳过：")
        for item, state in noverify_warnings:
            print(f"  - {item.rel}（账本上次记的远端状态：{state}）")
        print("    如果是草稿 / 未知，下载链接可能对别人仍然不可用；")
        print("    要确认就去掉 --no-verify 重跑，或手动 gh release view 看一眼。")

    todo = [it for it in items if it.kind != "skip"]
    skipped = len(items) - len(todo)
    if not todo:
        print()
        print_items(items)
        if any(it.verify_label == "--no-verify" for it in items):
            print(f"\n全部已是最新：{len(items)} 个 PDF 的 sha256 与 mtime 都没变，"
                  f"按账本跳过 {skipped} 个（--no-verify，未核对远端）。")
        else:
            print(f"\n全部已是最新：{len(items)} 个 PDF 的 sha256 与 mtime 都没变，"
                  f"远端核对通过，跳过 {skipped} 个。")
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
        preserved: list[tuple[str, BlockEntry]] = []
        if not is_under(index_dir, scan_root):
            # 这个 index.md 在 cwd 之上：块里可能还有范围外的旧条目，原样保住
            scanned_rels = {it.rel for it in by_index[index_dir]}
            for entry in parse_block_items(old_text) or []:
                try:
                    rel = (index_dir / entry.label).resolve().relative_to(root.resolve()).as_posix()
                except (OSError, ValueError):
                    continue
                if rel not in scanned_rels:
                    preserved.append((rel, entry))
        block = render_block(by_index[index_dir], index_dir, preserved)
        new_text = replace_block(old_text, block, rel_posix(index_md, root))
        if new_text != old_text:
            index_changes[index_md] = new_text

    print()
    print_plan(items, todo, root, args, index_changes, ledger_changed=bool(todo))

    if args.dry_run:
        print("\n[dry-run] 没有写任何文件、没有压缩，也没有执行 git / gh 写命令。")
        return 0

    print()
    if not confirm(
        f"确认按上面的计划修改 index.md、提交并推送到 {args.branch}？（共 {len(todo)} 个 PDF）",
        default_yes=False,
        assume_yes=args.yes,
    ):
        print("已取消，没有改任何文件。")
        return 0

    # 先把压缩/分卷做出来（复用上次失败留下的分卷），再写账本 —— 账本里要记
    # 分卷名和大小，之后规划阶段的远端核对才能按"名字 + 大小"验完整性。
    print("\n准备压缩分卷：")
    try:
        for item in todo:
            print(f"  {item.rel}")
            item.archive_info = prepare_archive(item.pdf, item.sha, item.mtime)
    except ToolError as exc:
        print(f"\n错误：{exc}")
        print("没有写 index.md / 账本，也没有提交或推送。")
        return 1

    originals: dict[Path, bytes | None] = {path: path.read_bytes() for path in index_changes}
    originals[ledger_path] = ledger_raw_before

    ledger_new = copy.deepcopy(ledger)
    # 远端核对通过的跳过项：顺手把 mtime / size / asset_size / last_remote_state 补进账本，
    # 以后 --no-verify 离线判断才有数据可用（sha/tag/url/uploaded_at 一律不动）。
    for item in items:
        if item.kind != "skip" or item.verify_label == "--no-verify":
            continue
        entry = ledger_new.get(item.rel)
        if not isinstance(entry, dict):
            continue
        entry["mtime"] = item.mtime
        entry["size"] = item.size
        archive_meta = entry.get("archive")
        if isinstance(archive_meta, dict) and isinstance(archive_meta.get("parts"), list):
            total = sum(int(part.get("size") or 0)
                        for part in archive_meta["parts"] if isinstance(part, dict))
            entry["asset_size"] = total
        entry["last_remote_state"] = "published"
    for item in todo:
        info = item.archive_info
        if info is None:
            raise ToolError(f"内部错误：{item.rel} 没有压缩分卷信息")
        entry = {
            "sha256": item.sha,
            "mtime": item.mtime,
            "tag": item.tag,
            "url": item.url,
            # size = PDF 本地字节数；asset_size = 这次上传/补传后远端资产的总字节数
            # （成功即等于分卷总大小；失败会整轮回滚）。
            "size": item.size,
            "asset_size": info.total_size,
            "last_remote_state": "published",
            "uploaded_at": now_iso,
            "archive": info.as_ledger(),
        }
        old = item.old
        if old and old.get("tag") and old.get("tag") != item.tag:
            previous = list(old.get("previous") or [])
            prev_entry = {
                key: old.get(key)
                for key in ("sha256", "mtime", "tag", "url", "size", "asset_size",
                            "last_remote_state", "uploaded_at", "archive")
                if old.get(key) is not None
            }
            previous.append(prev_entry)
            entry["previous"] = previous
        ledger_new[item.rel] = entry
    new_ledger_raw = ledger_text(ledger_new).encode("utf-8")

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
        print("临时目录保留着：修好后重跑 ./gbb 会复用已经做好的分卷，不重新压缩。")
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
    DEBUG = bool(args.verbose or args.debug)
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
