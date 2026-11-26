# books

放 PDF 资料的地方：**PDF 不进 git**，由 GitHub Release 托管；每个 PDF 一个 release，
下载链接由 `books/gbb` 自动写进最近的 `index.md`。

## 用法

```sh
./gbb                   # 扫描当前目录（含子目录）并发布 / 更新
./gbb --dry-run         # 只看计划：不写文件、不提交、不推送、不上传
./gbb --all             # 扫描整个 books/ 仓库（默认只扫当前目录）
./gbb --pull            # 下载当前目录范围内本地缺失的 PDF
./gbb --pull --dry-run  # 只打印将执行的 gh release download 命令
./gbb --yes             # 询问都自动回答"是"
./gbb --no-verify       # 跳过远端核对，只信本地账本（默认会核对）
```

**gbb 每次都会核对远端**：账本（`.books-releases.json`）里记着"已上传"不算数，
运行时会用 `gh release view` 确认 release 真在、对应文件真在、大小也对得上；
草稿（上传被打断留下的）同样算没传好。对不上就补传，并沿用原来的 tag。
只有加 `--no-verify` 才会跳过这一步、只信本地账本。

默认只处理**当前所在的目录及其子目录**：在 `books/` 里跑就是整个仓库；
在 `.../intel体系结构官方手册/` 里跑就只处理 intel 那几本，不会把别的目录的 PDF 拉进来。

跑起来是两步：先列出识别到的 PDF 和各自的归属，问你是否修改 `index.md` 并提交推送；
推送完再问一次是否现在上传 release。

### 下载缺失的 PDF（`--pull`）

`./gbb --pull` 按 `.books-releases.json` 和当前目录覆盖到的 `index.md` 托管块，
找出**当前目录范围内**、记录里有但本地文件已经不存在的 PDF，逐个执行：

```sh
gh release download <tag> --repo allinkernel/wblog-books --pattern '<远端资产名>' --dir '<目标目录>' --clobber
```

**下载前也会核对远端**：账本说上传过、但远端 release 里其实没有这个文件（或还是草稿），
会提示「远端也没有，不能 pull」，不会拿一个不存在的资产去下载。
GitHub 会把资产名里的空格、引号换成 `.`；gbb 会自动用远端那个名字下载，再改回本地文件名。

目录不存在会自动建。跑完给总账（成功 / 失败 / 本来就在本地）；失败会打印 `gh` 排错步骤。
`--pull` **只把文件放回本地**：不改 `index.md`、不改账本、不提交、不推送。
加 `--dry-run` 只打印命令，不下载。

### 目录怎么放

- 扫描范围默认是**当前目录及其子目录**，想扫整个仓库用 `--all`；
- 一个目录里有 `index.md`，它就管**它下面所有层级**的 PDF；
- PDF 放在没有 `index.md` 的子目录也可以，会归到往上最近的那个 `index.md`；
- 往上一直找到 `books/` 都没有 `index.md` → 直接报错并列出这些 PDF，不会静默丢掉。

### 交互长什么样

```
== books/gbb ==
仓库根：/home/mindul/self/wblog/books
远端仓库：allinkernel/wblog-books    分支：main
扫描到 3 个 PDF：
  [需补传：release 是草稿且没有资产] cpu-体系结构-官方手册/intel体系结构官方手册/8086/INTEL-The_8086_family_user's_manual.pdf
         归属：cpu-体系结构-官方手册/intel体系结构官方手册/8086/index.md
         tag ：8086__INTEL-The_8086_family_user-s_manual__20261126-1214（补传继续用这个旧 tag，不新建）
  [跳过] cpu-体系结构-官方手册/intel体系结构官方手册/80386/INTEL 80386 PROGRAMMER'S REFERENCE MANUAL 1986.pdf
         归属：cpu-体系结构-官方手册/intel体系结构官方手册/80386/index.md
         tag ：80386__INTEL-80386-PROGRAMMER-S-REFERENCE-MANUAL-1986__20261126-1217
  [新增] cpu-体系结构-官方手册/intel体系结构官方手册/amd64-intel64/325462-087-sdm-vol-1-2abcd-3abcd-4.pdf
         归属：cpu-体系结构-官方手册/intel体系结构官方手册/amd64-intel64/index.md
         tag ：amd64-intel64__325462-087-sdm-vol-1-2abcd-3abcd-4__20261126-1217
...
将执行：
  git -C .../books add -- ...
  git -C .../books commit -F <提交信息> -- ...
  git -C .../books push origin main
  gh release upload 8086__... <pdf> --repo allinkernel/wblog-books --clobber
  gh release edit 8086__... --repo allinkernel/wblog-books --draft=false   # 把草稿发布出去
  gh release view amd64-intel64__... --repo allinkernel/wblog-books
  gh release create amd64-intel64__... <pdf> --repo allinkernel/wblog-books
确认按上面的计划修改 index.md、提交并推送到 main？（共 2 个 PDF） [y/N] y
...
已提交：abc1234
已推送 origin/main

现在把这 2 个 release 上传到 allinkernel/wblog-books？ [Y/n] y
[1/2] .../8086/INTEL-The_8086_family_user's_manual.pdf
  release 已存在，改用 gh release upload --clobber（不重建 release）
  release 是草稿，上传完成后发布：gh release edit --draft=false
  完成：https://github.com/allinkernel/wblog-books/releases/tag/8086__...
...
==== 总账 ====
成功 2 / 跳过 1 / 失败 0
```

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
- **`--pull` 说"全部已在本地"**：当前目录范围内的账本 / `index.md` 记录都对得上，没有缺文件。
- **`HTTP 403: Resource not accessible by personal access token`**
  token 只有只读权限。重新授权：`gh auth login -h github.com -s repo`，然后 `gh auth status` 确认。
- **单文件超过 2GB**：GitHub Release 不收，拆 PDF 或换别的方式托管。
- **tag 规则**：`<index.md 所在目录名>__<PDF 文件名去扩展>__<YYYYMMDD-HHMM>`；
  只保留字母、数字、`._-`（中文保留），空格 / 引号 / 斜杠 / 冒号换成 `-`，最长 100；
  同一分钟内重名自动加 `-2`、`-3`。
- **改了 PDF 会整仓重传吗**：不会。sha256 变了才用新 tag 重传；没变也会先核对远端，
  远端缺文件、大小不对或还是草稿才补传（沿用原来的 tag）。
- **上传失败或选择暂不上传**：脚本会把这次对 `index.md` 和 `.books-releases.json`
  的改动回滚并推送，修好后重跑 `./gbb` 即可，不会留下指向空 release 的链接。
- **上传被 Ctrl-C 打断了怎么办**：直接重跑 `./gbb`。它会发现远端 release 是空的 / 还是草稿，
  用原来的 tag 把文件补传上去，再把草稿发布出去。不要手动删 release 或换 tag。
- **`--no-verify` 是什么**：跳过远端核对、只信本地账本。默认不要用——
  账本可能记着"上传过"但远端其实没有（上传被打断就会这样）。
- **`index.md` 里那块下载链接**：由脚本维护，别手改；手改会在下次发布时被整块覆盖。
