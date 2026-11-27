# books

放 PDF 资料的地方：**PDF 不进 git**，由 GitHub Release 托管。

- **一个 PDF = 一个 release tag**；
- release 里的资产不是裸 PDF，而是压好的 `<pdf>.tar.xz`（xz 多线程），
  产物超过 **10 MiB** 会自动切成 `.part.00 .part.01 …` 分卷；
- 下载链接（带大小、sha256 前 8 位、mtime）由 `books/gbb` 自动写进最近的 `index.md`。

## 用法

```sh
./gbb                     # 扫描当前目录（含子目录）并发布 / 更新
./gbb --dry-run           # 只看计划：不写文件、不压缩、不提交、不推送、不上传
./gbb --all               # 扫描整个 books/ 仓库（默认只扫当前目录）
./gbb --pull              # 按 index.md 下载当前目录范围内本地缺失的 PDF
./gbb --pull --dry-run    # 只打印将执行的 gh release download / 解压命令
./gbb --yes               # 询问都自动回答"是"
./gbb --jobs 4            # 分卷并发上传/下载的线程数（默认 4；1 = 单文件进度条）
./gbb --keep-temp         # 上传成功后也保留 .<pdf>.gbb_tar_dir（默认清理）
./gbb --no-verify         # 跳过远端核对，只信本地账本（默认会核对）
```

**gbb 每次都会核对远端**：账本（`.books-releases.json`）里记着"已上传"不算数，
运行时会用 `gh release view` 确认 release 真在、分卷真在、大小也对得上；
草稿（上传被打断留下的）同样算没传好。对不上就补传，并沿用原来的 tag。
只有加 `--no-verify` 才会跳过这一步、只信本地账本。

默认只处理**当前所在的目录及其子目录**：在 `books/` 里跑就是整个仓库；
在 `.../intel体系结构官方手册/` 里跑就只处理 intel 那几本，不会把别的目录的 PDF 拉进来。

跑起来是两步：先列出识别到的 PDF 和各自的归属，问你是否修改 `index.md` 并提交推送；
推送完再问一次是否现在上传 release。

## 什么时候会重传：sha256 **或** mtime 变了

每本书在账本里记两样东西：**sha256**（完整值）和 **PDF 文件的 mtime**（ISO8601 到秒）。
两个条件**任一变化**就当作"这本书要更新"：

| 变化 | 结果 |
|---|---|
| sha256 变了（内容改了） | 新 tag、新 release，旧 release 保留不删 |
| mtime 变了（内容没变） | **也是**新 tag、新 release，旧 release 保留不删 |
| 两个都没变 | 再看远端核对；远端完好就 `[跳过]` |

⚠️ **mtime 变化也会触发重传**，这是故意的取舍：

- `touch` 一下 PDF、把文件从别处拷回来、**`git checkout` 老版本的 PDF**，都会改 mtime；
- 于是下次 `./gbb` 会把它当成"要更新"，**重传一次**（压缩包不同，所以必须发新 release）；
- 代价是偶尔多传一次；换来的是"文件被动过"不会被静默忽略。

完整 sha256 和 mtime 都在账本里；`index.md` 的托管块里放 sha256 前 8 位和
`YYYY-MM-DD HH:MM`，够人肉核对。

## 上传：压缩 → 分卷 → 并发

1. **压缩**（保留原文件名，解压后还是 `<pdf>` 这个名字）：

   ```sh
   tar -C <PDF 所在目录> --use-compress-program='xz -6 -T0' -cf <pdf>.tar.xz -- <pdf>
   ```

   xz 用 `-6 -T0`（所有核并行）；`-9` 太慢，不用。

2. **分卷**：`.tar.xz` 超过 10 MiB 就切：

   ```sh
   split -b 10M -d -a 2 <pdf>.tar.xz <pdf>.tar.xz.part.
   ```

   得到 `<pdf>.tar.xz.part.00`、`.part.01`…，**每个 ≤ 10 MiB**，每个分卷是 release 里
   一个独立的资产。

3. **临时目录**：都在 PDF 同目录下的隐藏目录里：

   ```
   .ABC.pdf.gbb_tar_dir/
     .gbb-archive.json          # 记录 sha256 / mtime / 分卷清单，用来复用
     ABC.pdf.tar.xz             # 整包（分卷时也留着）
     ABC.pdf.tar.xz.part.00     # 只有超过 10 MiB 才有
     ABC.pdf.tar.xz.part.01
   ```

   - 上传成功 → 自动清理；
   - 上传失败 → **保留**，下次运行会检查 sha256 + mtime 是否还对得上，
     对得上就直接**复用已有分卷，不重复压缩**；
   - `--keep-temp` 可以强制保留（想手动看看压缩包时用）。

4. **并发上传**：`--jobs N`（默认 4）个线程同时传分卷。
   - 并发时**不画单文件进度条**（会打架），改画一行汇总，每 2 秒刷新：

     ```
     已完成 3/11 个分卷 · 已传 31.5 MB / 101.6 MB · 1.2 MB/s
     ```

     输出不是终端时（重定向到文件 / 管道）每 10 秒打印一行。
   - `--jobs 1` 且是在终端里跑 → 每个分卷一条 curl 的真实进度条。
   - 进度**只统计已经传完的分卷**（个数和字节），不会编造"正在传多少"。

5. 传完再 `gh release edit --draft=false` 把草稿发布出去，并回读远端核对
   每个分卷的名字和大小。

## 以后都用 `github_release_push` 传

新增/更新分卷时，**不要再从本机直传 GitHub**（本机直传只有 0.16–0.23 MB/s）。
用 wtool 的私有工具
[`tools/github-release-push`](https://github.com/allinkernel/wtool-github-release-push)：

```sh
github_release_push <分卷文件> <release-URL>

# 例：这本 PDF 的所有分卷
for f in ".我的书.pdf.gbb_tar_dir/"*.part.*; do
  github_release_push "$f" \
    "https://github.com/allinkernel/wblog-books/releases/tag/<tag>"
done
```

同名资产会被覆盖；传完会回读远端核对大小（有 sha256 时一起核）。
装法、首次配置（服务器 + `gh` + 公钥）见那个仓的 `README.md` / `wtool-doc`。

## 下载缺失的 PDF（`--pull`）

`./gbb --pull` **以 `index.md` 的托管块为准**（账本补全 sha256），找出**当前目录范围内**
记录里有、但本地文件已经不存在的 PDF：

1. 从 `index.md` 每行读出 tag、大小、sha256 前 8 位、mtime；账本给出完整 sha256；
2. `gh release view <tag> --json assets` 列出这个 release 的**全部分卷**；
3. 并发下载到 `.ABC.pdf.gbb_tar_dir`（`--jobs` 控制线程数）；
4. 校验分卷齐全（编号必须从 `00` 连续、数量与账本对上）；
5. `cat .part.* > ABC.pdf.tar.xz` 拼接，`tar -xJf` 解出 `ABC.pdf`；
6. 校验 sha256（与账本全量 / `index.md` 前 8 位比对）；
7. 落到原路径，清理临时目录。

**兼容旧形态**：以前的 release 里是**一个裸 `.pdf` 资产**（80386 那两本就是）。
`--pull` 遇到没有分卷、但有裸 `.pdf` 的 release，会直接下载它，不会报错。

**分卷缺一个就报错**：远端只有 `.part.00` 和 `.part.02`（缺 `.part.01`）时，
会打印「分卷不齐，缺 .part.01」并按失败处理，不会拼出一个坏包。

**下载失败时临时目录保留**，方便看下载到哪一步；修好后重跑 `./gbb --pull` 即可。
`--pull` **不改** `index.md`、**不改**账本、**不提交**、**不推送**。

## 目录怎么放

- 扫描范围默认是**当前目录及其子目录**，想扫整个仓库用 `--all`；
- 一个目录里有 `index.md`，它就管**它下面所有层级**的 PDF；
- PDF 放在没有 `index.md` 的子目录也可以，会归到往上最近的那个 `index.md`；
- 往上一直找到 `books/` 都没有 `index.md` → 直接报错并列出这些 PDF，不会静默丢掉。

## 交互长什么样

```
== books/gbb ==
仓库根：/home/mindul/self/wblog/books
远端仓库：allinkernel/wblog-books    分支：main
扫描到 2 个 PDF：
  [需补传：资产形态变了（旧版是单个 .pdf，现在传 .tar.xz / 分卷）] 80386/INTEL 80386 ....pdf
         归属：80386/index.md
         tag ：80386__INTEL-80386-...__20261126-1217（补传继续用这个旧 tag，不新建）
         mtime：2026-11-26 10:35 · sha256:75b4310c
  [跳过] 8086/INTEL-The_8086_family_user's_manual.pdf
         归属：8086/index.md
         tag ：8086__INTEL-The_8086_family_user-s_manual__20261126-1214
         mtime：2026-11-26 10:35 · sha256:7986483b

计划写入：
  - 80386/index.md（更新「下载链接」托管块）
  - .books-releases.json（记录 1 条发布账）

将执行：
  git -C .../books add -- ...
  git -C .../books commit -F <提交信息> -- ...
  git -C .../books push origin main
  [80386/INTEL 80386 ....pdf]
  tar -C .../80386 --use-compress-program='xz -6 -T0' -cf .../.INTEL ....pdf.gbb_tar_dir/... -- ...
  split -b 10M -d -a 2 .../.INTEL ....pdf.gbb_tar_dir/... ...
  gh release view 80386__... --repo allinkernel/wblog-books --json databaseId,isDraft
  curl -T <分卷> -X POST -H 'Content-Type: application/octet-stream' -K - --fail 'https://uploads.github.com/...'
  gh release edit 80386__... --repo allinkernel/wblog-books --draft=false
确认按上面的计划修改 index.md、提交并推送到 main？（共 1 个 PDF） [y/N] y

准备压缩分卷：
  80386/INTEL 80386 ....pdf
  压缩：INTEL 80386 ....pdf → .INTEL ....pdf.gbb_tar_dir/INTEL ....pdf.tar.xz
  压缩完成：1.0 MB，用时 0.2 秒
  待上传 1 个资产，共 1.0 MB
已写入 80386/index.md
已写入 .books-releases.json
已提交：abc1234
已推送 origin/main

现在把这 1 个 release 上传到 allinkernel/wblog-books？ [Y/n] y
[1/1] 80386/INTEL 80386 ....pdf
  上传 1 个资产，共 1.0 MB（PDF 1.0 MB）
  建 release（--draft，不带资产）
  已完成 1/1 个分卷 · 已传 1.0 MB / 1.0 MB · 3.2 MB/s
  草稿已发布，下载链接现在可用了。
  已清理临时目录：.INTEL ....pdf.gbb_tar_dir
  完成：https://github.com/allinkernel/wblog-books/releases/tag/80386__...
```

## `index.md` 里长什么样

每个 `index.md` 的托管块（`books:downloads:begin … end`）里，每行带**大小**、
**sha256 前 8 位**和 **mtime**：

```markdown
1. [INTEL-The_8086_family_user's_manual.pdf](https://github.com/allinkernel/wblog-books/releases/tag/8086__...__20261126-1214) · 52.1 MB · sha256:7986483b · 2026-11-26 21:40
```

完整的 sha256 在 `.books-releases.json` 里。PDF 被改过（sha256 变了）或
mtime 变了 → 会用**新 tag** 发**新 release**，旧 release **不删**、
旧 tag 留在账本的 `previous` 里。

## 想下载旧版本的 PDF

`index.md` 是进 git 的，所以旧版本记录可以从 git 历史里翻出来：

```sh
# 1. 看某个 index.md 改过哪些次（提交、时间、diff）
git log -p -- cpu-体系结构-官方手册/intel体系结构官方手册/8086/index.md

# 2. 把某一个旧提交里的 index.md 完整打出来（找到那一行的旧 tag）
git show <旧提交>:cpu-体系结构-官方手册/intel体系结构官方手册/8086/index.md

# 3. 用旧 tag 打开 release 页，下载的就是旧版
#    https://github.com/allinkernel/wblog-books/releases/tag/<旧 tag>
```

（旧 release 的资产可能还是裸 `.pdf`，也可能是后来重传的 `.tar.xz` 分卷，两种都能下。）

## 首次准备（只需要一次）

```sh
# 1. 登录 GitHub（上传 release 需要 repo 权限）
gh auth login -h github.com

# 2. 建远端仓库
gh repo create allinkernel/wblog-books --public --description 'CPU 体系结构官方手册 PDF'

# 3. 把本地 origin 指过去，并把 main 推上去
git -C books remote add origin https://github.com/allinkernel/wblog-books.git
git -C books push -u origin main
```

（`remote add` 只在还没有 origin 时用；已经有就 `git -C books remote set-url origin …`。）

## 常见问题

- **在子目录里跑 gbb，为什么只列出几本 PDF**：默认扫描范围就是当前目录及其子目录；
  想处理整个仓库加 `--all`（或者在 `books/` 根目录跑）。
- **`--pull` 说"全部已在本地"**：当前目录范围内的 index.md / 账本记录都对得上，没有缺文件。
- **为什么第一次跑新版本会把老 release 重传一遍**：旧 release 里的资产是裸 `.pdf`
  （或者还是个空草稿），新版本统一传 `.tar.xz` 分卷，资产形态变了就补传；
  **沿用原来的 tag**，`index.md` 里的链接不变，旧 release 也不会多出来。
- **`HTTP 403: Resource not accessible by personal access token`**
  token 只有只读权限。重新授权：`gh auth login -h github.com -s repo`，然后 `gh auth status` 确认。
- **`HTTP 422` / 资产已存在**：gbb 会自动退回 `gh release upload --clobber` 覆盖同名分卷。
- **上传/下载很慢**：`--jobs` 调大一点（默认 4）；GitHub 单线程上传实测只有 0.2 MB/s 左右。
- **tag 规则**：`<index.md 所在目录名>__<PDF 文件名去扩展>__<YYYYMMDD-HHMM>`；
  只保留字母、数字、`._-`（中文保留），空格 / 引号 / 斜杠 / 冒号换成 `-`，最长 100；
  同一分钟内重名自动加 `-2`、`-3`。
- **改了 PDF 会整仓重传吗**：不会。只有 sha256 / mtime 变了的那几本会发新 release；
  没变的会先核对远端，远端缺分卷、大小不对或还是草稿才补传（沿用原来的 tag）。
- **mtime 变了但内容没变，为什么也要重传**：故意的，见上面「什么时候会重传」。
  `git checkout` 老 PDF 会改 mtime，所以会多传一次；这是避免"文件被动过"被忽略的取舍。
- **上传失败或选择暂不上传**：脚本会把这次对 `index.md` 和 `.books-releases.json`
  的改动回滚并推送，修好后重跑 `./gbb` 即可，不会留下指向空 release 的链接；
  已经做好的分卷留在 `.<pdf>.gbb_tar_dir` 里，下次直接复用。
- **上传被 Ctrl-C 打断了怎么办**：直接重跑 `./gbb`。它会校验分卷 manifest，
  复用已经压好的分卷，再把远端补齐。不要手动删 release 或换 tag。
- **`--no-verify` 是什么**：跳过远端核对、只信本地账本。默认不要用——
  账本可能记着"上传过"但远端其实没有（上传被打断就会这样）。
- **`index.md` 里那块下载链接**：由脚本维护，别手改；手改会在下次发布时被整块覆盖。
- **临时目录 `.ABC.pdf.gbb_tar_dir` 能手动删吗**：能（gbb 会重新压缩）；
  但它就是续传用的，删了只是下次多花点时间压缩。
