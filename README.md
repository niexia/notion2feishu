# notion2feishu

[![test](https://github.com/niexia/notion2feishu/actions/workflows/ci.yml/badge.svg)](https://github.com/niexia/notion2feishu/actions/workflows/ci.yml)

把 **Notion 导出**迁移到 **飞书云文档**：保留父子层级、本地图片、页间链接。

一条命令跑完：自动找导出、自动解压、自动检查授权、默认迁到「我的文档库」。

> English: `pipx install notion2feishu` → `notion2feishu` (interactive wizard) or
> `notion2feishu migrate --src export.zip --yes`.

---

## 快速开始

```bash
# 1. 安装（任选一种）
pipx install notion2feishu          # 发布后
# pipx install .                    # 从源码安装
# uv tool install notion2feishu     # 或用 uv

# 2. 一次性授权（需要 lark-cli）
lark-cli auth login --domain docs,drive,wiki,base

# 3. 迁移
notion2feishu
```

`notion2feishu` 不带参数时会：

1. 在**当前目录 / Downloads / Desktop** 里找最近的 Notion 导出（`.zip` 或解压后的目录）；
2. 问一次你的飞书域名（如 `https://acme.feishu.cn`，回车用 `https://feishu.cn`），并记住；
3. 显示源、目标、页面数，确认后开始迁移；
4. 默认迁到**我的文档库**（`my_library`），保留 Notion 的页面树。

也可以显式指定：

```bash
notion2feishu migrate --src ~/Downloads/ExportBlock-xxxx.zip --yes
```

`--yes` 会跳过确认，适合脚本；如果没传 `--base-url`，会使用之前记住的域名。

---

## 为什么不用飞书网页直接导入

| | 网页导入 `.md` | Agent 上传 zip | 本工具 |
|---|---|---|---|
| 正文 | ✅ | ✅ | ✅ |
| 本地图片 | ❌ 只剩相对路径 | ✅ | ✅ 自动上传 |
| 父子层级 | ❌ 平铺 | ❌ 丢失 | ✅ Wiki 树 / 云盘文件夹 |
| 页间链接 | ❌ 失效 | 部分保留 | ✅ 回填成飞书 URL |
| 断点续跑 | — | — | ✅ 自动复用 `state.json` |

原理只有三步：

```text
Notion 导出
  └─ 1. 建空节点树（wiki +node-create / drive +create-folder）
       └─ 2. 写正文（docs +update，图片自动上传）
            └─ 3. 全部建完后回填页间链接
```

先建树再写正文，所以不会出现「图片上传成功但层级丢了」；最后统一回填链接，所以页间跳转不会断。

---

## 安装

要求：Python 3.9+，且已安装并配置 [`lark-cli`](https://github.com/larksuite/lark-cli)。

### pipx（推荐）

```bash
pipx install notion2feishu
```

### uv

```bash
uv tool install notion2feishu
# 或在仓库里：uvx --from . notion2feishu
```

### 从源码

```bash
git clone <repo> && cd notion2feishu
pipx install .
# 或开发模式：python3 -m venv .venv && . .venv/bin/activate && pip install -e .
```

如果直接用 `pip install .` 遇到 `bdist_wheel` / 老版本 pip 报错，先升级：

```bash
python3 -m pip install --upgrade pip
```

安装后 `notion2feishu` 就在 PATH 里。仓库根目录也保留了
`python3 notion2feishu.py` 的兼容入口。

---

## 常用命令

```bash
# 向导：自动找导出 + 确认 + 迁移
notion2feishu

# 显式指定 zip / 目录
notion2feishu migrate --src ~/Downloads/ExportBlock-xxxx.zip

# 只看树，不写飞书
notion2feishu plan --src ~/Downloads/ExportBlock-xxxx.zip

# 检查 lark-cli 授权与 wiki 权限
notion2feishu doctor

# 只做本地准备（清洗 md、复制图片、生成 manifest）
notion2feishu prepare --src ~/Downloads/ExportBlock-xxxx.zip --out ./work

# 在已有工作目录上继续/重跑
notion2feishu push --work ./work --target wiki --space-id my_library

# 版本
notion2feishu version
```

### `migrate` 常用参数

| 参数 | 说明 |
|---|---|
| `--src` | 导出 `.zip` 或目录；省略时自动找最近的 |
| `--target wiki\|drive` | 默认 `wiki`；传 `--folder-token` 时自动切到 `drive` |
| `--space-id` | Wiki 空间；默认 `my_library`（个人文档库） |
| `--parent-node-token` | 挂到某个已有 Wiki 节点下 |
| `--folder-token` | 云盘目标文件夹 token |
| `--base-url` | 租户域名，用于回填链接；也可用 `N2F_BASE_URL` |
| `--limit N` | 只迁前 N 页，试点用 |
| `--dry-run` | 只准备本地文件并打印 lark-cli 命令，不写飞书 |
| `--yes` | 跳过确认（CI / 脚本） |
| `--fresh` | 清空工作目录重新准备 |
| `--no-login` | 不自动执行 `lark-cli auth login` |
| `--skip-fill` | 只建空节点，不写正文 |
| `--resume` | 跳过 state 里已完成的页面（`migrate` 默认开启） |

---

## 迁到哪里

### 1. 我的文档库（默认，最省事）

```bash
notion2feishu
# 等价于 --target wiki --space-id my_library
```

Notion 的每个页面 → 一个 Wiki 节点，子页面挂在父节点下，层级最自然。

### 2. 团队知识库

```bash
# 先列出空间，拿 space_id
lark-cli wiki +space-list --as user

notion2feishu migrate --src export.zip --target wiki --space-id <space_id>
```

想挂到某个已有节点下：

```bash
notion2feishu migrate --src export.zip \
  --target wiki --space-id <space_id> \
  --parent-node-token <node_token>
```

### 3. 云盘文件夹

```bash
# 在飞书云盘建一个空文件夹，从 URL 取 fldcn...
notion2feishu migrate --src export.zip \
  --target drive --folder-token fldcnXXXXXXXX
```

云盘模式会为每一页建一个同名文件夹，文档放在文件夹里；子页面文件夹放在父页面文件夹里。
结构比 Wiki 多一层，但父子关系完整保留。

---

## 授权

### 第一次使用

```bash
lark-cli auth login --domain docs,drive,wiki,base
lark-cli auth status
lark-cli wiki +space-list --as user
```

`notion2feishu` 发现没有 user 身份时，会提示并询问是否直接运行 `lark-cli auth login`。

### 授权页显示「当前无需开通或授权任何权限」

这**通常不是账号被封**，而是 `lark-cli` 在沙箱/受限环境里写不了
`~/.lark-cli/cache`，导致授权链接没带上 scope。解决方式：

1. 在**普通终端**（不要通过 Agent/沙箱）运行：
   ```bash
   lark-cli auth login --domain docs,drive,wiki,base
   ```
2. 授权页应出现文档、云空间、知识库的权限列表；确认后回到命令行。

### 如果租户确实不允许第三方授权

工具必须用 user token 才能把文档建到你名下，这种情况绕不过去。可选：

1. 换一个能授权的飞书账号/租户；
2. 在飞书开放平台自建应用，加 `docs/drive/wiki` 权限，再用 `lark-cli` 绑定；
3. 只进云盘时，走 bot + 共享文件夹的降级方案：
   ```bash
   notion2feishu push --work ./work \
     --target drive --folder-token <共享文件夹token> --identity bot
   ```
   bot 创建的文档 owner 是机器人，需要在文件夹/文档里再把人加为协作者。

---

## 数据库（多维表格）

本工具目前只迁页面。Notion 数据库导出为 `.csv`，`plan` 会把它们列出来，但不会自动建表。

```bash
# CSV → 新建多维表格
lark-cli drive +import \
  --file "数据库.csv" --type bitable \
  --name "数据库名" --folder-token fldcnXXXXXXXX

# 要放进知识库
lark-cli wiki +move \
  --obj-token <bitable_token> --obj-type bitable \
  --target-space-id <space_id> \
  --target-parent-token <父节点token>
```

CSV 只保留数据，**视图、筛选、排序、关联、公式、汇总不会迁**，字段类型需要导入后手工调整。

---

## 会保留 / 会降级

**保留**

- 父子页面层级
- 标题、段落、加粗/斜体/下划线、行内代码
- 有序/无序列表、待办
- 图片、附件（本地图片自动上传；附件转 `<source>`）
- 外部链接、页间链接（回填为飞书 URL）

**降级**

- Notion callout → 飞书引用块（颜色/图标丢失）
- toggle / 分栏 → 普通段落
- 同步块 → 展开成普通内容
- 数据库 → 只列 CSV，不自动建表
- 评论、历史版本、权限/协作者 → 不迁

---

## 工作目录与断点续跑

默认工作目录：`~/.notion2feishu/work/<源路径 hash>`。

```text
work/
├── manifest.json          # 页面树、标题、父子关系、图片/链接数
├── pages/<page_id>.md     # 清洗后的 Markdown（可人工修改）
├── assets/<page_id>/...   # 图片
├── <page_id>.final.md     # 回填链接后的正文
└── state.json             # 已创建的 node_token / obj_token / URL
```

- 重新运行 `notion2feishu` 会复用同一工作目录，自动续跑；
- `--fresh` 清空重来；
- 改变迁移目标时，加 `--force-state` 清空状态；
- 配置（域名、space_id）保存在 `~/.notion2feishu/config.json`；
- 设置 `N2F_HOME` 可以把配置和工作目录换到别处（测试/CI 用）。

---

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `找不到 lark-cli` | 先安装并 `auth login` |
| `user 身份不可用` | 按提示运行 `lark-cli auth login --domain docs,drive,wiki,base` |
| 授权页「当前无需开通…」 | 在普通终端重跑 login；见「授权」一节 |
| `21001 access denied` | 当前身份没有 wiki 权限，或被租户数据安全策略拦截 |
| 图片 403 / 404 | 检查 `assets/<page_id>/` 是否完整；重新 `prepare --force` |
| 同一篇出现两次 | 没走 `--resume` 重跑了；删掉重复节点，或 `--fresh` 换工作目录 |
| `232140101` / `233523001` | 同目录并发冲突；本工具已串行，仍出现就把 `--sleep` 调到 1.5 |
| 长文档写一半失败 | `--limit` 缩小范围；或把该页拆成两篇后手动补 |
| `@file` unsafe path | 必须在工作目录下执行；用 `--work` 指定 |

更多诊断：

```bash
notion2feishu doctor
```

---

## 开发

```bash
git clone <repo> && cd notion2feishu
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

# 跑测试（只用标准库 unittest）
python3 -m unittest discover -s tests -v

# 本地入口
python3 -m notion2feishu --help
python3 notion2feishu.py --help
```

布局：

```text
src/notion2feishu/
├── cli.py       # 参数、向导、自动找导出/解压、授权
└── core.py      # 扫描、清洗、改写、lark-cli 调用
tests/
```

欢迎 PR。提交前请跑测试；新增行为尽量补一个 fixture 测试。

---

## License

MIT
