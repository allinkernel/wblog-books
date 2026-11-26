# books/ —— 给 agent 的规则

> 用户向说明看 `README.md`。这里只写机制、语义和红线。

## 这是什么

PDF 资料仓。PDF 本体**不进 git**（`.gitignore` 里有 `*.pdf`），由 GitHub Release 托管；
`books/gbb`（入口 bash，实现在 `tools/books_gbb.py`）负责：
扫描（**默认只扫当前目录及其子目录**，`--all` 才扫整个仓库）→ 归属 `index.md` → 询问 →
改 `index.md` + 账本 → 提交推送 → 询问 → 上传 release。
另有 `--pull`：按账本 + `index.md` 托管块，把当前目录范围内本地缺失的 PDF 下载回来。

## 结构约定

- 扫描范围 = **当前工作目录及其子树**（`os.getcwd()` 递归）；`--all` 才用仓库根。
  git 操作仍在仓库根做（`git rev-parse --show-toplevel`）。
- 含 `index.md` 的目录管**它下面任意层级**的所有 PDF；PDF 放在没有 `index.md` 的子目录，
  归到往上最近的那个 `index.md`（cwd 的祖先也算）。
- 最近的 `index.md` 可以在 cwd 之上：此时只更新那个 `index.md` 的托管块，
  块里属于 cwd 范围之外的旧条目**原样保留**；扫描不会把 cwd 之外的 PDF 拉进来。
- 往上一直找到 `books/` 都没有 `index.md` = 错误。脚本会一次列出所有这样的 PDF 并非 0 退出；
  **不要**改成"猜一个归属"或静默跳过。
- `index.md` 里只维护 `<!-- books:downloads:begin … -->` 与 `<!-- books:downloads:end -->`
  之间的块：已存在就整块替换，不存在就空一行追加到末尾；块外一个字节都不动。
- 下载链接用 release 页：`https://github.com/<repo>/releases/tag/<tag>`。
- 列表项用**相对该 index.md 目录的路径**（带 `.pdf`），这样不同目录的同名 PDF 能区分。
- PDF 不进 git：`git -C books ls-files '*.pdf'` 应为空。不要把 PDF `git add` 回去；
  用户磁盘上的 PDF 由脚本扫描，不看 git 索引。

## tag 与 `.books-releases.json`

- tag = `<index.md 目录名>__<PDF 文件名去扩展>__<YYYYMMDD-HHMM>`。
- 字符只留字母、数字、`._-`（CJK 保留）；空格、`'`、`/`、`:` 等一律换成 `-`；
  长度 ≤ 100（先截断主体）；同一时间戳重名依次加 `-2`、`-3`。
- `.books-releases.json`（**进 git**）是 `{PDF 相对路径: {sha256, tag, url, uploaded_at}}`。
  - sha256 相同 → 跳过：不重传、不改 `index.md`、不提交；
  - sha256 变了 → 新 tag（时间戳新的），旧 tag **不删**、旧 release 留在 GitHub 上；
    值里可选的 `previous` 数组按时间顺序保留历次 `{sha256, tag, url, uploaded_at}`。
- 账本在**上传之前**就和 `index.md` 一起提交，所以 `uploaded_at` 是本次运行的时刻；
  上传失败或用户选择暂不上传时，脚本会把 `index.md` 和账本**整轮回滚**再提交推送，
  避免账本谎报"已上传"。
- 想强制重传：把该 PDF 的账本条目删掉（或 `sha256` 改掉）再跑；不要手改 `index.md` 的块。

## `--pull`（下载本地缺失的 PDF）

- 数据来源两处：账本 `.books-releases.json` + 当前目录覆盖到的 `index.md` 托管块
  （cwd 子树里的所有 `index.md`，外加 cwd 往上最近的那个）；两处按路径取并集，
  同一路径的 tag 不一致会打警告。
- 只处理**属于 cwd 子树**的记录；本地文件已存在 → 跳过。
- 逐个执行
  `gh release download <tag> --repo allinkernel/wblog-books --pattern '<basename>' --dir '<目标目录>' --clobber`；
  目标目录不存在先 `mkdir -p`。
- `--dry-run --pull` 只打印命令，不下载。
- **不改** `index.md`、**不改**账本、**不提交**、**不推送**；跑完给总账
  （成功 / 失败 / 本来就在本地），失败打印 `gh` 排错教程并非 0 退出。

## 红线

- **别手改** `index.md` 里的 `books:downloads:begin … end` 块（会被整块覆盖）。
- **别 `git add -A` / `git add .`**：只 add 脚本改过的那几个文件（脚本用的是 pathspec）。
- **agent 不许在真仓跑 `books/gbb` 的写流程**：正常流程会 `git push` + `gh release create/upload`；
  `--pull` 会真的联网下载、往工作树写文件。真仓只允许 `--dry-run`（含 `--pull --dry-run`）。
  要跑真流程就把 `gbb`+`tools/` 拷到 `/tmp` 夹具，用假 `git`（`push` 吞掉、其余转真 `git`）
  和假 `gh`（记录参数、造一个假 PDF）跑。
- 别把 PDF 重新纳入 git；别在 `books/` 里 `git reset --hard` / `checkout --` 洗掉别人改动。
- 上传失败时的排错教程由脚本打印（`gh auth status` / `repo` scope / 2GB / 代理 / 403 长什么样）。

## 怎么测

```sh
python3 -m py_compile books/tools/books_gbb.py
bash -n books/gbb

# 真仓只读：跑前后 git status 必须一致
git -C books status --short
books/gbb --dry-run
git -C books status --short
```

夹具（不碰真仓）：

1. 建 `/tmp/books-fixture/`，把 `gbb` 和 `tools/` 拷进去（脚本根 = `tools/` 的父目录），
   `git init -b main` 并配一个本地 bare `origin`；
2. 发布流程：`a/b/c/x.pdf` 归 `a/index.md`；同一个 PDF 改一个字节 → 新 tag 且旧条目还在；
   连跑两次 → 第二次全部跳过；`a/x.pdf` 但往上没有 `index.md` → 报错并列出；
   从 `a/` 跑只列 `a/` 下的 PDF（`--all` 才列全仓）；
   cwd 之上的 `index.md` 被更新时，块里范围外的旧条目要保留。
3. 推送/上传序列：`PATH=/tmp/stub:$PATH`，假 `git` 记录参数（`push` 直接吞掉，其余转真 `git`），
   假 `gh` 记录参数并造 release 标记；断言日志顺序 `add → commit → push → release create`，
   且脚本不会绕过这两个命令去联网。失败路径用返回 1 的假 `gh`，断言打印排错教程且退出码非 0。
4. `--pull`（同样只碰夹具）：账本和 `index.md` 块各放几条、本地删掉 3 个 PDF →
   `--pull --dry-run` 打印 3 条 `gh release download` 且不调用 `gh`；用假 `gh` 真跑 →
   3 次调用、假 PDF 落盘、`git status` 里没有 `index.md` / 账本改动；再跑一次 → "全部已在本地"。
   假 `gh` 返回 1 → 打印排错教程、退出码非 0。cwd 之外的记录不能被拉进来。
