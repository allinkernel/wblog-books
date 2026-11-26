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
- 托管块每行的格式固定是：
  ``N. [相对路径](release 链接) · <十进制大小> · sha256:<前 8 位>``
  （例 `· 52.1 MB · sha256:7986483b`）。大小用**十进制**（和 GitHub release 页显示一致）；
  完整 sha256 在账本里，块里只放前 8 位，够人肉核对 `sha256sum`。
  解析时要容忍没有尾巴的旧行；`render_block` 对范围外保留条目（preserved）**原样保留**
  它原来的大小/hash 文本，不要重新编造。
- PDF 不进 git：`git -C books ls-files '*.pdf'` 应为空。不要把 PDF `git add` 回去；
  用户磁盘上的 PDF 由脚本扫描，不看 git 索引。

## tag 与 `.books-releases.json`

- tag = `<index.md 目录名>__<PDF 文件名去扩展>__<YYYYMMDD-HHMM>`。
- 字符只留字母、数字、`._-`（CJK 保留）；空格、`'`、`/`、`:` 等一律换成 `-`；
  长度 ≤ 100（先截断主体）；同一时间戳重名依次加 `-2`、`-3`。
- `.books-releases.json`（**进 git**）是
  `{PDF 相对路径: {sha256, tag, url, size, asset_size, last_remote_state, uploaded_at}}`。
  - `size` = 本地字节数；`asset_size` = 这次上传/补传后远端资产应有的字节数
    （成功即等于 `size`；失败整轮回滚）。`last_remote_state` 记上次远端状态
    （`published` / `draft` / 未知），给 `--no-verify` 提示用。
  - sha256 变了 → 新 tag（时间戳新的），旧 tag **不删**、旧 release 留在 GitHub 上；
    值里可选的 `previous` 数组按时间顺序保留历次
    `{sha256, tag, url, size, asset_size, last_remote_state, uploaded_at}`。
  - **sha256 没变不等于跳过**：默认还要核对远端（见下节）。只有核对通过才跳过；
    远端缺 release / 缺资产 / 大小不符 / 是草稿，都要补传，而且**沿用账本里的旧 tag**。
- 账本在**上传之前**就和 `index.md` 一起提交，所以 `uploaded_at` 是本次运行的时刻；
  上传失败或用户选择暂不上传时，脚本会把 `index.md` 和账本**整轮回滚**再提交推送，
  避免账本谎报"已上传"。⚠️ 被 Ctrl-C 打断时回滚来不及跑，账本仍会谎报"已上传"——
  这正是默认要做远端核对的原因。
- 想强制重传：把该 PDF 的账本条目删掉（或 `sha256` 改掉）再跑；不要手改 `index.md` 的块。

## 远端核对（默认开，`--no-verify` 才关）

账本只是"上次以为成功"的线索，**远端才是事实**。规划阶段对每个"账本有记录且
sha256 未变"的 PDF 执行（**只读**）：

```sh
gh release view <tag> --repo <repo> --json tagName,assets,isDraft
```

判定：

| 远端状态 | 计划状态 | 上传动作 |
|---|---|---|
| release 不存在（`gh` 非 0，且输出明确 `not found` / 404） | `[需补传：远端没有 release]` | `gh release create <旧 tag>` |
| release 在、没有对应资产 | `[需补传：release 里没有这个文件]` | `gh release upload <旧 tag> --clobber` |
| 资产在、size 与本地字节数不同 | `[需补传：大小不一致]` | 同上 |
| 资产在、size 相同、**不是草稿** | `[跳过]` | 不上传 |
| `isDraft=true`（哪怕资产在） | `[需补传：release 是草稿…]` | `gh release upload --clobber`，**再** `gh release edit <旧 tag> --draft=false` |
| 核对失败（网络 / 权限 / 输出坏了；非 0 且非 404） | 警告 + `[需补传：远端核对失败，按未上传处理]` | 仍然上传（宁可重传，不留死链） |

> 上表的"上传动作"是**语义**；实际上传默认走 `curl --progress-bar`（见下节），
> `gh release upload --clobber` 是回退路径。新建时先 `gh release create --draft`（空 release），
> 传完再 `gh release edit --draft=false`。

- **资产名按 GitHub 的归一化名匹配**：GitHub 会把资产名里 `[A-Za-z0-9._-]` 之外的
  字符换成 `.`。实测本地 `INTEL 80386 PROGRAMMER'S REFERENCE MANUAL 1986.pdf`
  在远端叫 `INTEL.80386.PROGRAMMER.S.REFERENCE.MANUAL.1986.pdf`。
  `asset_name_key()` 先精确匹配、再按归一化名匹配，别只比原名。
- **草稿（draft）就是 8086 那次的现场**：`gh release create` 中途被打断会留下
  一个草稿 + 0 资产，tag 已经被草稿占用；这时再 `gh release create <旧 tag>` 会失败，
  **必须** `upload --clobber` 再 `edit --draft=false`。上传失败就**不**发布草稿
  （保持原样、不留半成品），整轮失败回滚。
- 补传**复用账本里的旧 tag**（`index.md` 里的链接就是它）；只有 sha256 变了才生成新 tag。
- 核对失败**不许静默跳过**。`--no-verify` 是明确"跳过远端核对、只信本地账本"（默认关）；
  账本 `last_remote_state` 不是 `published` 时，`--no-verify` 会提示"上次可能是草稿/未知"。

## 上传：curl 进度条 + 回退 `gh`（2026-11-26）

`gh release create/upload` 是**一次 HTTPS 请求、没有任何进度输出**；100MB 的手册按实测
0.16–0.23 MB/s 要等十来分钟、全程黑屏。所以资产上传改成 `curl`（只有它能给真实进度）：

1. **先建空 release**（不带资产）拿 id：
   - 新建时：`gh release create <tag> --repo <repo> --draft --title … --notes …`
     （**用 `--draft`**：`index.md` 在传之前就已经推上去了，先公开一个 0 资产的 release
     等于留死链；传完再发）；
   - 补传时 release 已存在，直接取 id；
   - 取 id：`gh release view <tag> --repo <repo> --json databaseId,isDraft` → `.databaseId`。
2. **curl 上传**：

   ```sh
   curl -T <pdf> --progress-bar -X POST \
        -H 'Content-Type: application/pdf' -K - --fail \
        'https://uploads.github.com/repos/<owner>/<repo>/releases/<id>/assets?name=<远端归一化名>'
   ```

   - 目标域名是 **uploads.github.com**，不是 api.github.com；`?name=` 用
     `asset_name_key()` 归一化后的名字（GitHub 会把空格/引号换成 `.`）。
   - **token 红线**：token 由 `gh auth token` 读出，只写成 curl 配置文件
     `header = "Authorization: token <token>"`，通过 **`-K -` 从 stdin** 喂进去。
     **绝不许进 curl 的 argv**（`ps` / shell 历史 / 日志都会漏）；
     `gh_auth_token()` 故意不走会打 debug 日志的 `run()`，也不要打印它的输出。
   - `--fail` 必须有：不加的话 HTTP 4xx/5xx 时 curl 退出码仍是 0，会被误判成成功。
   - 进度条画在 **stderr**，所以调用 curl 时 **stderr 不能 capture**（否则进度条被吞掉）。
3. **传完**：打印实测 `MB/s` + 耗时；是草稿就
   `gh release edit <tag> --repo <repo> --draft=false`。
4. **回退**（自动，不询问）：`curl` 不存在 / 非交互式（`sys.stdout.isatty()` 假，
   或 stdin 不是终端，如 `</dev/null`）/ `curl` 失败 → 改用
   `gh release upload <tag> <pdf> --repo <repo> --clobber`，并**明说"这次没有进度条"**。
   - 非 TTY 分支**不编造进度**：只打印 `正在上传 <大小>，约需 N 分钟`
     （按实测 0.2 MB/s 估算，并注明是估算），然后等 `gh` 返回。
   - 新建 + 非 TTY 时仍用一次 `gh release create <tag> <pdf> …` 完成（不先建空 release）。

## 旧版怎么找回（hash 变了 = 新 tag，旧 release 不删）

sha256 变了 → `make_tag()` 生成新 tag、新 release；旧 tag 留在账本的 `previous[]` 里，
GitHub 上的旧 release **不删**。`index.md` 进 git，所以旧版本的发布记录能从历史里翻：

```sh
git log -p -- <某个 index.md>          # 哪次提交改了哪一行（旧 tag / 旧 sha256）
git show <旧提交>:<某个 index.md>      # 整份旧 index.md，复制旧 tag
# 打开 https://github.com/allinkernel/wblog-books/releases/tag/<旧 tag> 下载旧 PDF
```

`index.md` 的托管块是**整块替换**，块外不动；**不要**在块外另加"版本历史"段落 ——
回溯靠 git 历史，正文不要掺版本记录。

## `--pull`（下载本地缺失的 PDF）

- 数据来源两处：账本 `.books-releases.json` + 当前目录覆盖到的 `index.md` 托管块
  （cwd 子树里的所有 `index.md`，外加 cwd 往上最近的那个）；两处按路径取并集，
  同一路径的 tag 不一致会打警告。
- 只处理**属于 cwd 子树**的记录；本地文件已存在 → 跳过。
- 下载前用 `gh release view` 交叉核对（与规划阶段同一套）：远端没有这个 release、
  或资产缺失（草稿且没资产也算）→ 打印 `[远端也没有] … 不能 pull` 并按失败处理；
  核对失败（网络/权限）→ 打警告但仍按记录尝试下载。`--no-verify` 时同样跳过这一步。
- 逐个执行
  `gh release download <tag> --repo allinkernel/wblog-books --pattern '<远端资产名>' --dir '<目标目录>' --clobber`；
  目标目录不存在先 `mkdir -p`。`--pattern` 优先用核对到的远端资产名（GitHub 会把
  空格/引号换成 `.`），核对不到时用本地文件名的归一化名；下载后若落盘名不是本地文件名，
  改回本地文件名（否则 `index.md` / 账本对不上）。
- `--dry-run --pull` 会调 `gh release view`（只读核对），但**不调** `gh release download`。
- **不改** `index.md`、**不改**账本、**不提交**、**不推送**；跑完给总账
  （成功 / 失败 / 本来就在本地 / 远端也没有），失败打印 `gh` 排错教程并非 0 退出。

## 红线

- **别手改** `index.md` 里的 `books:downloads:begin … end` 块（会被整块覆盖）。
- **别 `git add -A` / `git add .`**：只 add 脚本改过的那几个文件（脚本用的是 pathspec）。
- **agent 不许在真仓跑 `books/gbb` 的写流程**：正常流程会 `git push` +
  `gh release create/upload/edit`；`--pull` 会真的联网下载、往工作树写文件。
  真仓只允许 `--dry-run`（含 `--pull --dry-run`）——它会调 `gh release view`，
  那是**只读**核对，允许。
  要跑真流程就把 `gbb`+`tools/` 拷到 `/tmp` 夹具，用假 `git`（`push` 吞掉、其余转真 `git`）
  和假 `gh`（记录参数、按 tag 返回预设 JSON / 造一个假 PDF）跑。
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
   `index.md` 新行带 `· <十进制大小> · sha256:<新 sha 前 8 位>`（与 `sha256sum` 一致），
   旧 tag 进账本 `previous`、旧 release 仍在（桩不删）；
   连跑两次 → 第二次全部跳过（假 `gh` 要记住第一次 `create` 的 release / 资产，
   这样第二次 `gh release view` 能核对通过；只验账本那半也可以加 `--no-verify`）；
   `a/x.pdf` 但往上没有 `index.md` → 报错并列出；
   从 `a/` 跑只列 `a/` 下的 PDF（`--all` 才列全仓）；
   cwd 之上的 `index.md` 被更新时，块里范围外的旧条目要保留。
3. 推送/上传序列：`PATH=/tmp/stub:$PATH`，假 `git` 记录参数（`push` 直接吞掉，其余转真 `git`），
   假 `gh` 记录参数并造 release 标记；断言日志顺序 `add → commit → push → release create`，
   且脚本不会绕过这两个命令去联网。失败路径用返回 1 的假 `gh`，断言打印排错教程且退出码非 0。
4. `--pull`（同样只碰夹具）：账本和 `index.md` 块各放几条、本地删掉 3 个 PDF，
   假 `gh` 对三条 tag 都返回"资产在"→ `--pull --dry-run` 打印 3 条
   `gh release download`（同时有 3 次只读 `gh release view`，但**没有** download 调用）；
   用假 `gh` 真跑 → 3 次 download、假 PDF 落盘、`git status` 里没有 `index.md` /
   账本改动；再跑一次 → "全部已在本地"。假 `gh` 返回 1 → 打印排错教程、退出码非 0。
   cwd 之外的记录不能被拉进来。
5. **远端核对（夹具，桩 `gh` 按 tag 返回预设 JSON）**，逐个断言：
   - 账本无记录 → `[新增]`；
   - 账本有、sha 未变、release 不存在（桩 stderr `release not found`）→
     `[需补传：远端没有 release]`，`release create` 用的是**账本旧 tag**；
   - release 在但没有资产 → `[需补传：release 里没有这个文件]`，`upload --clobber` 用旧 tag；
   - 资产在但 size 不符 → `[需补传：大小不一致]`；
   - 资产在、size 相符（资产名用归一化后的 `x.y.pdf`，本地叫 `x y's.pdf`）→ `[跳过]`，
     **0 次**上传调用；
   - `isDraft=true, assets=[]` → `[需补传：release 是草稿…]`；断言调用里**没有**
     `release create`，有 `release upload --clobber` + `release edit --draft=false`，
     且 upload 在 edit 之前；
   - 桩返回非 0 且非 404（如 500）→ 警告 + "按未上传处理" + 仍然上传；
   - `--no-verify` → 桩调用日志里**没有** `release view`（新文件也只 `create`）；
   - `--pull`：账本说上传过、本地缺失、远端也没有 → `[远端也没有] … 不能 pull`；
     核对失败 → 警告但仍下载；下载后落盘名与本地文件名一致。
6. **上传进度（夹具，桩 `curl` 把 argv + 收到的 stdin 都记日志）**：
   - 交互式终端（用 `script -qec` 造 pty）→ `curl` 被调用，argv 里有 `-T <pdf>`、
     `--progress-bar`、`uploads.github.com/.../assets?name=<归一化名>`；
     **argv 里没有 `gho_` / `token=`**，而 stdin（`-K -` 的配置）里有
     `Authorization: token …`；输出有 `MB/s`；草稿走完后有 `release edit --draft=false`。
   - `curl` 退出非 0 → 日志里出现 `gh release upload <tag> <pdf> --clobber`（自动回退）。
   - 非 TTY（stdout 重定向 + `</dev/null`）→ 日志里**没有** curl，输出没有进度条，
     但有 `约需 N 分钟`/`约需 N 秒`，并注明是估算。
   - PATH 里去掉 curl（但保留 python3/git/gh）→ 回退 gh，理由打印"本机没有 curl"。
   - `--dry-run` 只打印 curl 计划（`-T <pdf> --progress-bar … assets?name=`），**不调** curl。
