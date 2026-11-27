# books/ —— 给 agent 的规则

> 用户向说明看 `README.md`。这里只写机制、语义和红线。

## 这是什么

PDF 资料仓。PDF 本体**不进 git**（`.gitignore` 里有 `*.pdf`），由 GitHub Release 托管；
`books/gbb`（入口 bash，实现在 `tools/books_gbb.py`）负责：
扫描（**默认只扫当前目录及其子目录**，`--all` 才扫整个仓库）→ 归属 `index.md` → 询问 →
改 `index.md` + 账本 → 提交推送 → 询问 → 压缩/分卷 → 并发上传 release。
另有 `--pull`：按 `index.md` 托管块，把当前目录范围内本地缺失的 PDF 下载回来。

**一个 PDF = 一个 release tag**；release 资产是 `<pdf>.tar.xz`（xz 多线程），
产物 > 10 MiB 自动切成 10 MiB 分卷；旧 tag 的 release 永远不删。

## `gbb` 结构总览

> 给 agent 查结构用。函数名都在 `tools/books_gbb.py` 里。

### 数据流

```
扫描(scwd/--all) → 找归属 index.md → 算 sha256+mtime → 更新判据 → 远端核对(gh release view，失败重试 3 次)
   → 打印计划/询问 → 并行压缩+分卷(tar/split，xz 按并发分核) → 写 index.md + .books-releases.json
   → git add/commit/push → 询问
   → 并发调 github_release_push 上传分卷（服务器中转） → 发布草稿 → 回读核对 → 清理临时目录
```

`--pull` 是另一条路：`index.md` 托管块（+ 账本补全 sha256）→ `gh release view` 列资产
→ 并发下载到临时目录 → 拼/解包 → 校验 sha256 → 落盘 → 清理。

### 文件与目录

| 路径 | 作用 |
|---|---|
| `books/gbb` | bash 入口，`exec python3 tools/books_gbb.py "$@"` |
| `books/tools/books_gbb.py` | 全部实现（约 2300 行，只用标准库 + 系统命令） |
| `.books-releases.json` | 账本（**进 git**）：sha256 / mtime / tag / url / size / asset_size / archive / previous |
| `index.md` 托管块 | `books:downloads:begin … end` 之间的下载行；块外一个字节不动 |
| `.<pdf>.gbb_tar_dir/` | 临时目录（隐藏，PDF 同目录）：`.gbb-archive.json` + `.tar.xz` + `.part.NN` |
| `.gbb-archive.json` | 临时目录里的 manifest：sha256 / mtime / 分卷名与大小，复用分卷前先校验它 |

### 函数职责（按执行顺序）

| 函数 | 职责 |
|---|---|
| `scan_pdfs` / `find_index_dir` | 扫描 PDF；跳过 `.gbb_tar_dir`；往上找最近的 `index.md` |
| `sha256_file` / `mtime_info` | 更新判据的两个输入（sha256 全量；mtime ISO8601 到秒 + 展示文本） |
| `make_tag` / `used_tags` | 生成 `<目录名>__<文件名>__<YYYYMMDD-HHMM>`，避让已有 tag |
| `check_remote_book` / `classify_book_assets` | 只读核对：release / 草稿 / 分卷形态 / 分卷名与大小 |
| `prepare_archive` / `load_archive_manifest` | 压缩 + 分卷；sha256+mtime 对得上就复用已有分卷 |
| `prepare_archives` / `_xz_threads` / `xz_program` | 本轮多本**并行压缩**（`--jobs`）；xz 按并发分核（`-T<k>`，只压一本时 `-T0`） |
| `render_block` / `parse_block_items` / `replace_block` | 渲染/解析/替换 `index.md` 托管块 |
| `find_github_release_push` / `grp_missing_message` | 找默认上传通道（PATH → `~/self/wtool/.../bin/`）；找不到就明确报错，**不退回本机直传** |
| `_gh_retry` | `gh` 只读调用重试 3 次（间隔 2s / 4s）；4xx（除 408/429）不重试 |
| `upload_archive` / `_grp_upload` / `_curl_upload` / `_gh_upload` | 分卷并发上传：默认 `github_release_push`；`--upload-local` 才 curl 优先、失败退 `gh release upload --clobber` |
| `UploadProgress` | 并发时的汇总进度行（只统计已完成分卷） |
| `verify_remote_parts` | 传完回读远端，逐个核对分卷名与大小（**两条路都保留**） |
| `_publish_draft_if_needed` | 草稿传完再 `gh release edit --draft=false`（重试 3 次；不成功就报错、留草稿） |
| `rollback` | 上传失败 / 选择不上传时，把 index.md 与账本回滚并再提交推送 |
| `collect_pull_records` | 以 `index.md` 托管块为准收集记录，账本补全 sha256 / 大小 |
| `run_pull_job` / `install_downloaded` | 并发下载 → 校验分卷 → 拼接/解压 → 校验 sha256 → 落盘 |

### 状态机（计划里的方括号）

| 状态 | 含义 | 动作 |
|---|---|---|
| `[新增]` | 账本没记录 | 新 tag，create + 上传 |
| `[变更：…]` | sha256 变了 / mtime 变了 / 账本没 tag | 新 tag，create + 上传；旧 release 不删 |
| `[需补传：…]` | sha256 与 mtime 都没变，但远端不对（缺 release / 缺分卷 / 分卷不齐 / 大小不符 / 草稿 / 旧资产形态 / 核对失败） | 沿用账本旧 tag 补传 |
| `[跳过]` | sha256 与 mtime 都没变，且远端核对通过 | 不上传 |
| `[跳过（未核对远端，--no-verify）]` | 只信账本 | 不上传 |

### 失败与回退

- **压缩/分卷失败**：还没写 index.md / 账本，直接退出；临时目录清掉。
- **git push 失败**：本地提交保留，提示手动 push；不进入上传。
- **上传失败**：临时目录**保留**（下次复用分卷）；`rollback` 把 index.md 与账本恢复并再提交推送，
  避免留下指向空 release 的链接；草稿**不发布**。
- **用户选择暂不上传**：同上回滚。
- **传完回读核对失败**（网络）：只警告，不判上传失败；**release 里真的缺分卷**才判失败。
- **Ctrl-C**：来不及回滚，账本可能谎报"已上传" → 下次默认远端核对会发现并补传。
- **账本 index.md 没有实际变化时跳过提交**（`git_commit` 返回 `None`）：账本里唯一会变的
  是 `uploaded_at`（精确到秒），同一秒内重跑就一模一样；旧版会把空提交当"提交失败"整轮中止。
  跳过提交后照常进入上传。

### `--pull` 流程

1. `collect_pull_records`：读 cwd 子树 + 往上最近一个 `index.md` 的托管块；
   tag / 大小 / sha256 前 8 位 / mtime 来自 `index.md`，完整 sha256 来自账本（index 优先）。
2. 本地已有 → 跳过；只处理 cwd 范围。
3. `gh release view <tag> --json assets`（只读）列出资产，`classify_book_assets` 分四类：
   `parts` / `single` / `pdf`（旧形态）/ `none`。
4. 分卷形态：编号必须从 `00` 连续、数量与账本 `archive.parts` 一致，否则**报错不静默**。
5. 并发 `gh release download <tag> --pattern <资产名> --dir <临时目录> --clobber`。
6. `cat` 分卷 → `tar -xJf` → 校验 sha256（账本全量 / index 前 8 位）→ `os.replace` 到原路径。
7. 旧形态（单个裸 `.pdf`）直接下载 → 校验 → 落盘；没有分卷的 release 不报错。
8. 成功清理临时目录；失败保留。`--pull` **不改** index.md / 账本，不提交、不推送。
9. `gh release view` 失败时退化为 `gh release download --pattern '<文件名>*'` 通配下载；
   下载后仍走同一套校验。

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
  ``N. [相对路径](release 链接) · <十进制大小> · sha256:<前 8 位> · <YYYY-MM-DD HH:MM>``
  （例 `· 52.1 MB · sha256:7986483b · 2026-11-26 21:40`）。大小用**十进制**
  （和 GitHub release 页显示一致）；完整 sha256 / mtime 在账本里。
  `render_block` 对范围外保留条目（preserved）**原样保留**它原来的尾巴，不要重新编造。
  解析时要容忍没有尾巴的旧行（80386 的旧块就没有大小/hash）。
- PDF 不进 git：`git -C books ls-files '*.pdf'` 应为空。不要把 PDF `git add` 回去；
  用户磁盘上的 PDF 由脚本扫描，不看 git 索引。
- `.<pdf>.gbb_tar_dir/` 是隐藏临时目录，`scan_pdfs` 会跳过它（不要把它里面的
  `.tar.xz` / 解压出的 PDF 当书）。

## 更新判据：sha256 + mtime，任一变化就更新

- **sha256 变了**（内容变了）→ `[变更：sha256 变了]` → 新 tag、新 release，旧 release 不删。
- **mtime 变了**（内容没变）→ `[变更：mtime 变了（内容没变）]` → **同样**新 tag、新 release。
- 两个都没变 → 才进入远端核对；远端 OK 才 `[跳过]`。
- mtime 用**文件 mtime**（`st_mtime`）：ISO8601 到秒进账本，展示成 `YYYY-MM-DD HH:MM`。
- ⚠️ mtime 变化会触发重传是**故意的**：`touch`、拷贝文件、`git checkout` 老 PDF 都会改 mtime
  → 会多传一次。`README.md` 里写明了这个取舍，别把它当 bug 改掉。
- 账本里 mtime 缺失的旧条目（升级前的账本）：sha256 没变时按"需要核对远端"处理，
  不因为"没有 mtime"就发新 tag。

## tag 与 `.books-releases.json`

- tag = `<index.md 目录名>__<PDF 文件名去扩展>__<YYYYMMDD-HHMM>`。
- 字符只留字母、数字、`._-`（CJK 保留）；空格、`'`、`/`、`:` 等一律换成 `-`；
  长度 ≤ 100（先截断主体）；同一时间戳重名依次加 `-2`、`-3`。
- `.books-releases.json`（**进 git**）每个 PDF 一条：

  ```json
  {
    "sha256": "<完整>",
    "mtime": "2026-11-26T21:40:12+08:00",
    "tag": "...",
    "url": "https://github.com/<repo>/releases/tag/<tag>",
    "size": 25916378,
    "asset_size": 25916378,
    "last_remote_state": "published",
    "uploaded_at": "2026-11-26T21:40:30+08:00",
    "archive": {
      "name": "x.pdf.tar.xz",
      "size": 25916378,
      "parts": [{"name": "x.pdf.tar.xz.part.00", "size": 10485760}, ...]
    },
    "previous": [ {...旧条目...} ]
  }
  ```

  - `size` = **PDF 本地字节数**；`asset_size` = 这次上传后**远端资产总字节数**
    （分卷大小之和；成功即等于它，失败会整轮回滚）。
  - `archive.parts` 是上传后的资产清单（单个 `.tar.xz` 不分卷时就是一条）。
    规划阶段的远端核对拿它当"应该有什么、多大"的基准。
  - sha256 或 mtime 变了 → 新 tag；旧条目进 `previous[]`（按时间顺序），旧 release 留在 GitHub 上。
  - **sha256 与 mtime 都没变不等于跳过**：默认还要核对远端（见下节）。只有核对通过才跳过；
    远端缺 release / 缺分卷 / 分卷不齐 / 大小不符 / 是草稿 / 还是旧 `.pdf` 形态，都要补传，
    而且**沿用账本里的旧 tag**。
- 账本在**上传之前**就和 `index.md` 一起提交，所以 `uploaded_at` 是本次运行的时刻；
  上传失败或用户选择暂不上传时，脚本会把 `index.md` 和账本**整轮回滚**再提交推送，
  避免账本谎报"已上传"。⚠️ 被 Ctrl-C 打断时回滚来不及跑 —— 这正是默认要做远端核对的原因。
- 想强制重传：把该 PDF 的账本条目删掉（或 `sha256` / `mtime` 改掉）再跑；不要手改 `index.md` 的块。

## 压缩 / 分卷 / 临时目录

- 压缩（**保留原名**，解压后文件名不变），**本轮多本并行压**（`prepare_archives`）：

  ```sh
  tar -C <pdf 所在目录> --use-compress-program='xz -6 -T<k>' -cf <pdf>.tar.xz -- <pdf>
  ```

  - `k = max(1, cpu_count // jobs_actual)`，`jobs_actual = min(--jobs, 待压缩本数)`；
    `--jobs 1`（或只压一本）时用 `-T0` 吃满全核。
  - ⚠️ **N 个 `xz -T0` 会互相抢核，反而比串行还慢** —— 并行时一定要分核，别改回 `-T0`。
  - 任一本压缩失败 → **整轮中止**（不写 index.md / 账本、不提交、不上传），报出是哪一本。
  - 每本一行 `压缩完成：xx.x MB，用时 y.y 秒`（并行时带 `[文件名] ` 前缀，顺序可能交错）；
    压多本时最后再汇总一行 `压缩完成：5 本，共 124.0 MB，用时 18.3 秒（并行 4）`。
  - 不要用 `-9`（太慢）。
- 分卷：`.tar.xz` **> 10 MiB** 才切，`split -b 10M -d -a 2 <tar.xz> <tar.xz>.part.`，
  得到 `.part.00 .part.01 …`，每个 ≤ 10 MiB。`<tar.xz>` 本身仍留在临时目录里。
- 临时目录 = PDF 同目录的 `.<pdf>.gbb_tar_dir/`（例 `.ABC.pdf.gbb_tar_dir`），里面：
  `.gbb-archive.json`（manifest）、`<pdf>.tar.xz`、可选 `.part.NN`。
- 成功上传 → 清理；`--keep-temp` 保留。失败 → 保留。
- 复用分卷：`load_archive_manifest` 先比对 manifest 里的 **sha256 + mtime**，
  再核对每个文件存在且大小一致；全对才复用，否则整个临时目录删掉重压。

## 远端核对（默认开，`--no-verify` 才关）

账本只是"上次以为成功"的线索，**远端才是事实**。规划阶段对每个"账本有记录且
sha256 + mtime 未变"的 PDF 执行（**只读**）：

```sh
gh release view <tag> --repo <repo> --json tagName,assets,isDraft
```

判定：

| 远端状态 | 计划状态 | 上传动作 |
|---|---|---|
| release 不存在（`gh` 非 0，且明确 `not found` / 404） | `[需补传：远端没有 release]` | `gh release create <旧 tag> --draft` |
| release 在、没有这本书的资产 | `[需补传：release 里没有这个文件]` | 上传分卷（沿用旧 tag） |
| 资产是旧形态（只有一个裸 `.pdf`，没有 `.tar.xz` / 分卷） | `[需补传：资产形态变了（旧版是单个 .pdf，现在传 .tar.xz / 分卷）]` | 上传分卷（旧 `.pdf` 资产不删） |
| 分卷编号不连续 / 数量与账本不一致 / 大小不符 | `[需补传：分卷不齐 / 分卷大小不一致 …]` | 上传分卷 |
| 分卷都在、大小与账本一致、**不是草稿** | `[跳过]` | 不上传 |
| `isDraft=true`（哪怕资产在） | `[需补传：release 是草稿…]` | 上传分卷，**再** `gh release edit --draft=false` |
| 核对失败（网络 / 权限 / 输出坏了；非 0 且非 404） | 警告 + `[需补传：远端核对失败，按未上传处理]` | 仍然上传（宁可重传，不留死链） |

- **资产名按 GitHub 的归一化名匹配**：GitHub 会把资产名里 `[A-Za-z0-9._-]` 之外的
  字符换成 `.`。实测本地 `INTEL 80386 PROGRAMMER'S REFERENCE MANUAL 1986.pdf`
  在远端叫 `INTEL.80386.PROGRAMMER.S.REFERENCE.MANUAL.1986.pdf`。
  `asset_name_key()` 按同样的规则归一化后再比，别只比原名。
- 账本 `archive.parts` 存在时，远端分卷的**名字和大小**都要对上；没有（旧账本）时
  至少核对"分卷编号连续"。
- 草稿（draft）就是 8086 / arm 那次的现场：`gh release create` 中途被打断会留下
  一个草稿 + 0 资产，tag 已经被草稿占用；这时再 `gh release create <旧 tag>` 会失败，
  **必须**上传分卷再 `edit --draft=false`。上传失败就**不**发布草稿。
- 补传**复用账本里的旧 tag**（`index.md` 里的链接就是它）；只有 sha256 / mtime 变了才生成新 tag。
- 核对失败**不许静默跳过**。`--no-verify` 是明确"跳过远端核对、只信本地账本"（默认关）；
  账本 `last_remote_state` 不是 `published` 时，`--no-verify` 会提示"上次可能是草稿/未知"。

## 上传：默认 `github_release_push`（服务器中转）

1. **先确认，再压缩**：计划打印完、用户确认"修改 index.md 并推送"之后，才
   `prepare_archives` 做压缩/分卷（**多本并行**，见上节）。**账本必须在压缩之后写**
   （要记 `archive.parts` 的名字和大小）；顺序仍是
   **写 index.md + 账本 → commit → push → 上传**。
2. **建 release 仍由 gbb 做**：新建时 `gh release create <tag> --repo <repo> --draft
   --title … --notes …`（空草稿，不带资产）；补传时 release 已存在，取
   `gh release view <tag> --json databaseId,isDraft`。两次都走 `_gh_retry`；
   create 报 422（已存在）且取不到 id 时，服务器中转那条路会继续上传（它用不到 databaseId）。
3. **并发上传分卷**（`--jobs`，默认 4；线程池，**一个分卷一个子进程**）：

   ```sh
   github_release_push <分卷文件> --repo <owner/repo> --tag <tag>
   ```

   - **用它的 `--repo/--tag` 形式，不拼 release URL**（tag 里的字符转义是它的事）。
   - 它自己 scp 到服务器、服务器 `gh release upload --clobber`、回读资产 size；
     **本机不直连 GitHub**，所以家里上行慢 / 本机 `gh` 抖都不拖累上传。
   - 输出收敛：成功只留 `✓ <分卷名>（xx.x MB）`；失败取它 stderr 最后 5 行，并提示
     `github_release_push --check`。
   - **并发时不画单文件进度条**：用一行汇总（每 2 秒 `\r` 刷新，非 TTY 每 10 秒一行），
     全部分卷收完再逐卷打 `✓` 行：
     `已完成 3/11 个分卷 · 已传 31.5 MB / 101.6 MB · 1.2 MB/s`
   - `--jobs 1` 且是终端 → 子进程 stderr 直接继承，**转发它自己的 scp 进度条**
     （stdout 仍然收掉，只留摘要）。
   - **进度只能来自"已完成分卷数/字节"**，不许编造"正在传多少"。
   - 远端已有且大小相同的分卷直接跳过（续传）。
4. **传完**：草稿 → `gh release edit <tag> --draft=false`（重试 3 次；三次都失败就
   **报错、非 0、草稿留着** —— 重跑会被判成"草稿需补传"，幂等）；再 `verify_remote_parts`
   回读远端逐个核对分卷名 + 大小（**这一步由 gbb 自己做，不依赖 github_release_push**）；
   最后清理临时目录（`--keep-temp` 保留）。
5. **`--upload-local`**（降级路径，默认**不用**）：本机直传 —— `curl -T` 优先
   （token 只写进 `-K -` 的 stdin 配置，**绝不进 argv**；`--fail` 必须有），
   没有 curl / 取不到 token / 失败 / 同名资产大小不对 → 该分卷退
   `gh release upload --clobber`。实测本机直传 0.16–0.23 MB/s。
6. **找不到 `github_release_push`**（`PATH` 和
   `~/self/wtool/tools/github-release-push/bin/github_release_push` 都没有）→
   在写任何文件之前就**明确报错**并给安装提示
   （`wtool install tools/github-release-push`，或直接用那个路径），
   **不许静默退回本机直传**。

## `gh` 抖动：重试 3 次（`_gh_retry`）

本机 `gh` 偶发 `EOF`（Clash TUN 把 `api.github.com` 解析成 `198.18.0.x` 假 IP，
**不是凭证问题**）。所以：

- `gh release view`（远端核对 / 取 release id）、`gh release create`、
  `gh release edit --draft=false` 全部走 `_gh_retry`：最多 3 次，间隔 2s / 4s，
  重试成功打一行 `gh release view 重试后成功（第 3 次）`。
- **4xx 不重试**（除 408 / 429）：权限 403、已存在 422、不存在 404 重试也没用。
- 三次都失败时维持原语义：**远端核对失败 = 警告 + 按未上传处理并继续**（不中止整轮）；
  `edit --draft=false` 失败 = 报错并用非 0 退出（草稿保留）。
- 上传本身不受影响：`github_release_push` 走 ssh 到服务器，本机不连 GitHub。

## 旧版怎么找回（hash / mtime 变了 = 新 tag，旧 release 不删）

sha256 或 mtime 变了 → `make_tag()` 生成新 tag、新 release；旧 tag 留在账本的 `previous[]` 里，
GitHub 上的旧 release **不删**。`index.md` 进 git，所以旧版本的发布记录能从历史里翻：

```sh
git log -p -- <某个 index.md>          # 哪次提交改了哪一行（旧 tag / 旧 sha256）
git show <旧提交>:<某个 index.md>      # 整份旧 index.md，复制旧 tag
# 打开 https://github.com/allinkernel/wblog-books/releases/tag/<旧 tag> 下载旧 PDF
```

`index.md` 的托管块是**整块替换**，块外不动；**不要**在块外另加"版本历史"段落 ——
回溯靠 git 历史，正文不要掺版本记录。

## `--pull`（下载本地缺失的 PDF）

- **以 `index.md` 托管块为准**：cwd 子树里所有 `index.md` + cwd 往上最近的那个；
  每行给 tag / 大小 / sha256 前 8 位 / mtime。账本补全完整 sha256、
  以及 `index.md` 没记到的旧记录（同一路径 tag 不一致时以 `index.md` 为准并打警告）。
- 只处理**属于 cwd 子树**的记录；本地文件已存在 → 跳过。
- 下载前 `gh release view` 交叉核对（与规划阶段同一套）：release 不存在 / 没有这本书的资产
  → 打印 `[远端也没有] … 不能 pull` 并按失败处理；核对失败（网络/权限）→ 打警告并退化为
  `gh release download --pattern '<文件名>*'` 通配下载。
- 资产形态：
  - **分卷**：编号必须从 `00` 连续；数量与账本 `archive.parts` 对得上；少一个就报错；
    并发下载到 `.<pdf>.gbb_tar_dir` → 校验每个分卷大小 → `cat` 拼接 →
    `tar -C <tmp> -xJf <pdf>.tar.xz` → 校验 PDF sha256 → `os.replace` 到原路径。
  - **单个 `.tar.xz`**：下载 → 解压 → 校验 → 落盘。
  - **旧形态裸 `.pdf`**：直接下载那个 `.pdf` → 校验 → 落盘（**不要**因为没有分卷就报错）。
- `--dry-run --pull` 会调 `gh release view`（只读核对），但**不调** `gh release download`；
  打印 `gh release download` / `cat` / `tar` / `sha256sum` / `mv` 计划。
- **不改** `index.md`、**不改**账本、**不提交**、**不推送**；跑完给总账
  （成功 / 失败 / 本来就在本地 / 远端也没有），失败打印 `gh` 排错教程并非 0 退出。
- 下载成功清理临时目录；失败保留（下次重下会覆盖同名文件）。

## 红线

- **上传默认走 `github_release_push`**（wtool 私有工具仓 `tools/github-release-push`：
  先 scp 到服务器，再由服务器 `gh release upload`）：本机直传 GitHub 实测 0.16–0.23 MB/s，
  所以默认通道是服务器中转 —— 本机不直连 GitHub，**不需要任何代理环境变量**
  （别再往文档 / 提示里写代理）。`--upload-local` 才是本机直传。
  "慢 10× 以上"是用户口径，本轮实测比值见该仓 `journal.md` —— 别把 10× 写成本轮量到的数。
- **`--jobs` 同时管三件事**：并行压缩的本数、上传分卷的并发进程数、`--pull` 的下载线程数。

- **别手改** `index.md` 里的 `books:downloads:begin … end` 块（会被整块覆盖）。
- **别 `git add -A` / `git add .`**：只 add 脚本改过的那几个文件（脚本用的是 pathspec）。
- **agent 不许在真仓跑 `books/gbb` 的写流程**：正常流程会 `git push` +
  `gh release create/upload/edit`；`--pull` 会真的联网下载、往工作树写文件。
  真仓只允许 `--dry-run`（含 `--pull --dry-run`）——它会调 `gh release view`，
  那是**只读**核对，允许。
  要跑真流程就把 `gbb`+`tools/` 拷到 `/tmp` 夹具，用假 `git`（`push` 吞掉、其余转真 `git`）
  和假 `gh` / 假 `curl`（记录参数、按 tag 返回预设 JSON / 造资产）跑。
- 别把 PDF 重新纳入 git；别在 `books/` 里 `git reset --hard` / `checkout --` 洗掉别人改动。
- 上传失败的排错教程由脚本打印：默认通道打 `github_release_push --check`（服务器连通性 /
  服务器上的 `gh` / `gh auth status`）；`--upload-local` 打 `gh auth status` / repo scope / 403 那一套。

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

1. 建 `/tmp/<夹具>/cases/<case>/`，把 `gbb` 和 `tools/` 拷进去（脚本根 = `tools/` 的父目录），
   `git init -b main` + 本地 bare `origin`；PDF 与 `index.md` 自己造。
   桩放在 PATH 最前面，用环境变量传状态文件路径。
2. **压缩/分卷**：造 200 KB 与 10.6 MB 的**随机**内容 PDF（随机才压不小）→ 小的只有
   `.tar.xz`；大的有 `.tar.xz` + ≥2 个 `.part.NN`，每个 ≤ 10485760 字节；
   `tar -xJf` 解回的**同名 PDF** sha256 与原文件一致。
3. **并行压缩**：多本时打印 `并行压缩 N 本（--jobs 4，每本 xz -6 -T<k>）`，每本一行
   `压缩完成：xx.x MB，用时 y.y 秒`，最后一行汇总 `压缩完成：N 本，共 …，用时 …（并行 N）`；
   `--jobs 1` 时是 `串行压缩`、每本 `xz -6 -T0`。串行 / 并行各跑一次记真实数字。
4. **默认上传通道**（桩 `github_release_push` 记 argv + 起止时间）：
   每个分卷一次调用、argv 是 `<分卷> --repo <repo> --tag <tag>`（**不是** URL）；
   `--jobs 4` 时两个进程的时间区间要重叠；每个分卷成功一行 `✓ <分卷名>（…）`；
   桩返回非 0 → `gbb` 非 0 退出 + `rollback`（`git log` 里出现"回滚本次发布"、
   index.md 复原、账本删掉）。
5. **找不到命令**：`PATH` 收窄成只含 git/gh、`HOME` 指向空目录（兜底路径不存在）→
   非 0 退出、"找不到 github_release_push" + `wtool install …` 提示，
   而且**没写任何文件**、**没有**退回本机直传。
6. **`--upload-local`**：桩 `curl` 记到 argv（`-T` / `--fail` / `uploads.github.com`）；
   桩 `github_release_push` 一次都不该被调用。
7. **`gh` 重试**（桩 `gh` 可注入 EOF）：前 2 次 EOF、第 3 次成功 → 整轮成功，
   输出里有 `gh release view 重试后成功（第 3 次）`；3 次全 EOF → 远端核对按"未上传"处理、
   整轮不中止；`edit --draft=false` 3 次全失败 → 非 0 + 报错 + 草稿仍在。
8. **输出收敛**：`--dry-run` 输出里 `grep -cE 'tar -C|split -b|gh release|curl -T|git -C|将执行'`
   = 0；加 `--verbose` 才出现命令行。
9. **更新判据 / 续传 / `--pull` / 发布流程**（沿用）：
   `touch` 改 mtime（内容不变）→ `[变更：mtime 变了…]` 且 tag 与账本不同；
   改内容 → `[变更：sha256 变了]`；都不变 → `[跳过]`；
   桩上传失败 → 临时目录保留、重跑打印"复用上次留下的分卷"且不再压缩；
   `--pull`：`index.md` 3 行 + 账本 → 三本缺失，分卷那本并发下载→拼→解→sha256 通过，
   旧形态（单个 `.pdf` 资产）直接下载成功，分卷缺 1 个（只有 00、02）→ 报错非 0，
   `git status` 里不能有 index.md / 账本改动；
   `gh` 记住 create 的 release / 资产，连跑两次 → 第二次全部跳过；
   改 PDF → 新 tag、旧 tag 进 `previous`、旧 release 仍在（桩不删）。
10. **上传序列**：断言日志顺序 `add → commit → push → release create`；
    失败路径断言打印排错教程（默认 `github_release_push --check`）且退出码非 0。
