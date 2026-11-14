# notion2feishu 产品设计：从「命令行迁移」到「人人可用」

> 状态：设计稿。P1（Notion API 直连）和 P2（本地 Web 向导）已在 v0.2.0 落地，见 README。目标是把现在的「导出 zip → 跑 CLI」开发者流程，升级成
> 「连 Notion → 选页面 → 选目标 → 预览 → 后台迁移 → 看结果」的产品流程。

---

## 0. 结论

现在的形态适合开发者和 Agent，但不适合「Notion 里有几十个顶层页面、上百个子页面」的普通用户：

- 需要手动导出 zip；
- 需要记住 `prepare` / `push` / `--space-id` / `--folder-token`；
- 没有「选哪些页面」的界面；
- 失败后只能看命令行日志；
- 没有增量，重跑容易重复建文档。

目标产品形态：

**一个本地 Web 向导（`notion2feishu` 直接打开浏览器）+ 一条 CLI + 一个 MCP Server，三者共用同一个 core。**

核心差异：

| | 现在 | 目标 |
|---|---|---|
| 导出 | 手动点 Notion 导出、解压 | 连 Notion API，直接拉取 |
| 选择 | 整个 zip 全量 | 工作区树 + 勾选/排除 |
| 目标 | 手写 token/参数 | 选择「我的文档库 / 知识库 / 云盘」 |
| 预览 | `--dry-run` 看命令 | 图形化预览：页数、图片、冲突、警告 |
| 运行 | 前台跑，终端日志 | 后台任务，进度条，可暂停/继续 |
| 失败 | 自己看 stderr | 失败列表 + 单页重试 |
| 重跑 | 容易重复 | 增量：只同步变更页面 |

---

## 1. 用户与场景

### 1.1 个人全量迁移

Notion 里有 `Work tutorial`、`Engineer`、`Reading`、`Life` 等十几个顶层页面，
下面几百个子页面和图片。希望一次性搬到飞书，保留目录。

关键诉求：**不要手动导出、不要逐篇点、能中断续跑。**

### 1.2 选择性迁移

只想迁 `Work tutorial` / `Engineer`，不迁 `Life` / `Story`。
或者排除 `Changelog` 这种自动生成的页面。

关键诉求：**工作区树勾选、排除名单、保存选择。**

### 1.3 增量同步

第一次迁完后，Notion 里又更新了几篇。希望再跑一次只更新变更的页面。

关键诉求：**记录 `last_edited_time` 和内容 hash，默认只处理变更。**

### 1.4 团队迁移

把某个团队空间迁到飞书知识库，需要：
- 选择目标知识库和父节点；
- 迁移后权限继承；
- 失败可审计。

### 1.5 Agent 使用

Codex / Trae / DeepSeek / 豆包等 Agent 需要：
- 结构化输入输出；
- 默认 dry-run；
- 明确的「确认后才写入」；
- 不接触明文 token。

---

## 2. 设计原则

1. **零导出**：用户不应该再点 Notion 的导出按钮。
2. **先预览，后写入**：任何写操作前都能看到「会发生什么」。
3. **默认非破坏**：只新增/更新，不删除；覆盖必须显式确认。
4. **可恢复**：任务状态落盘，中断、限流、失败都能继续。
5. **进度可见**：当前页、成功/失败/跳过、预计剩余时间。
6. **人类与 Agent 同一套能力**：GUI 是 CLI 的壳，不另写一套逻辑。
7. **密钥不落仓库**：token 放 OS keychain 或本地配置，日志脱敏。

---

## 3. 主流程（5 步）

### Step 1：连接 Notion

- 首次：粘贴 Internal Integration Token；
- 校验：调用 `GET /v1/users/me` 或列可访问页面；
- 保存到本地配置（keychain 优先）；
- 提示用户把 integration 连到根页面。

> 长期可做 Notion OAuth（公开应用），但 v1 用 token 最简单。

### Step 2：选择页面

扫描工作区里 integration 可见的顶层页面，展示树：

```text
☑ Work tutorial                 42 页 / 118 图
  ☑ Make business planning      12 页 / 30 图
  ☑ Manage a team                8 页 / 22 图
  ☐ Hiring                       6 页 / 10 图
☑ Engineer                      88 页 / 240 图
☐ Life                          31 页 / 60 图
☐ Story                         12 页 / 8 图
```

能力：

- 全选 / 全不选 / 反选；
- 搜索标题；
- 排除名单（`Changelog`、`Daily` 等）；
- 显示每棵子树的页数、图片数、数据库数；
- 记住上次选择。

### Step 3：选择目标

```text
迁移到：
  ● 我的文档库（默认）
  ○ 团队知识库   [选择空间 ▾]
  ○ 云盘文件夹   [选择文件夹 ▾]

容器节点：
  ☑ 在目标下创建「Notion 导入 2026-11-15」
```

默认创建容器节点，避免十几个顶层页面直接铺在「我的文档库」根目录。

### Step 4：预览

```text
将迁移 130 页 / 358 张图片 / 3 个数据库
目标：我的文档库 / Notion 导入 2026-11-15

⚠ 3 个数据库只会保留链接，不会自动建多维表格
⚠ 2 个页面超过 20,000 blocks，需要二次拉取
⚠ 1 个页面与已有文档重名，将创建副本
预计耗时：8–12 分钟
```

操作：

- `预览树`：展开看每页的目标路径；
- `试跑 3 页`：先迁一个样本；
- `开始迁移`。

### Step 5：运行与结果

```text
进度  ███████████░░░░  68%   88/130
当前：Engineer / 性能优化 / 缓存
成功 86 · 失败 0 · 跳过 2

[暂停] [取消]
```

完成后：

```text
✅ 迁移完成
130 页 · 358 图 · 用时 9m12s

根页面：
  Work tutorial   https://xxx.feishu.cn/wiki/...
  Engineer        https://xxx.feishu.cn/wiki/...

[打开飞书] [下载报告 CSV] [重试失败项]
```

失败项单独列出：页面标题、错误码、可重试按钮。

---

## 4. 关键 UX 决策

### 4.1 为什么默认创建容器节点

用户的 Notion 有十几个顶层页面。如果直接建在「我的文档库」根目录，会污染整个空间。
默认建一个「Notion 导入 YYYY-MM-DD」节点，迁完可以整体移动/分享/归档。

### 4.2 树勾选，而不是输入页面 ID

普通用户不知道 page_id；工作区树是唯一自然的入口。
底层仍然用 page_id 做映射。

### 4.3 先统计，再开跑

在用户点「开始」之前，必须知道：
- 多少页、多少图、多少数据库；
- 哪些页面会失败（权限/超大/不支持）；
- 目标是否可写。

避免「跑了半小时才发现空间没权限」。

### 4.4 任务化，而不是一次 HTTP 请求

大量文档迁移是分钟级任务：
- 后台线程/进程执行；
- 状态写 SQLite；
- 浏览器关闭不影响；
- 重新打开页面能看到进度；
- 支持暂停（当前页完成后停）和继续。

### 4.5 失败可解释

错误分类：

| 类型 | 例子 | 处理 |
|---|---|---|
| 权限 | 子页面没连 integration | 跳过 + 提示去 Notion 连接 |
| 限流 | 429 | 自动退避重试 |
| 超大 | 页面 > 20k blocks | 二次拉取 / 拆页 |
| 资源 | 图片 403 过期 | 重新拉取页面 |
| 飞书 | 空间无权限 / token 失效 | 停止任务，提示重新授权 |
| 不支持 | 数据库/嵌入 | 降级为链接，报告里标注 |

### 4.6 增量而不是重复

第一版全量迁移后，记录：

```json
{
  "notion_page_id": "...",
  "feishu_obj_token": "...",
  "last_edited_time": "2026-11-14T...",
  "content_hash": "sha256:...",
  "status": "synced"
}
```

再次运行时：
- `last_edited_time` 没变 → 跳过；
- 变了 → 更新已有文档（`docs +update`）；
- 新页面 → 新建；
- Notion 删除的页面 → 默认不删飞书文档，只在报告里标记。

---

## 5. 技术架构

```text
┌────────────────────────────────────────────────────┐
│  UI: 本地 Web (127.0.0.1)  │  CLI  │  MCP Server    │
└──────────────────────┬─────────────────────────────┘
                       │ 同一套 JSON 任务接口
┌──────────────────────▼─────────────────────────────┐
│  Job Service                                       │
│  - create job / pause / resume / cancel            │
│  - SQLite: jobs, pages, assets, mappings, errors   │
└──────────────────────┬─────────────────────────────┘
                       │
┌──────────────────────▼─────────────────────────────┐
│  Planner                                           │
│  - Notion 树扫描、选择、排除                        │
│  - 统计、冲突检测、限流预估                         │
└──────────────┬───────────────────────┬─────────────┘
               │                       │
┌──────────────▼─────────┐  ┌──────────▼─────────────┐
│ Notion Connector       │  │ Feishu Adapter          │
│ - /pages/{id}/markdown │  │ - lark-cli / OpenAPI    │
│ - /pages/{id}          │  │ - wiki node / docx      │
│ - 资产下载              │  │ - 图片/附件上传          │
│ - 429 退避              │  │ - 限流/重试              │
└──────────────┬─────────┘  └──────────┬─────────────┘
               │                       │
        ┌──────▼───────────────────────▼──────┐
        │ Normalizer                           │
        │ Notion v2 Markdown → Feishu Markdown │
        │ callout/toggle/columns/page/database │
        └──────────────────────────────────────┘
```

### 组件职责

- **Notion Connector**：只管拉取，不关心飞书。
- **Normalizer**：把 Notion-flavored Markdown 转成飞书能吃的 Markdown/XML。
- **Planner**：只读，产出迁移计划（树、数量、警告）。
- **Job Service**：状态机 + 持久化 + 重试。
- **Feishu Adapter**：包 `lark-cli` 或直接 OpenAPI，负责幂等和限流。
- **UI/CLI/MCP**：薄壳，不写业务逻辑。

### 为什么用 SQLite

几百页 × 每页多个状态，JSON 文件不够：
- 并发读写；
- 按状态查询（失败项、待重试）；
- 任务恢复；
- 报告导出。

SQLite 是标准库，零依赖。

---

## 6. 数据模型

```sql
jobs(
  id, created_at, updated_at,
  source_root, target_kind, target_id, container_token,
  status, total_pages, done_pages, failed_pages
)

pages(
  job_id, notion_page_id, parent_id, title, order,
  notion_last_edited, content_hash,
  status, feishu_obj_token, feishu_url, error_code, error_msg
)

assets(
  job_id, notion_page_id, source_url, local_path,
  status, feishu_token, error_msg
)

mappings(
  notion_page_id PRIMARY KEY, feishu_obj_token,
  feishu_url, last_synced_at, content_hash
)
```

---

## 7. 迁移语义

### 7.1 树映射

- 顶层页面 → 容器节点下的子节点；
- 子页面 → 父节点下的子节点；
- 数据库 → 多维表格（P3），P1 先保留链接；
- 页面标题重复 → 允许，按 page_id 区分；
- 空页面 → 建空文档（保留结构）。

### 7.2 更新策略

| 模式 | 行为 |
|---|---|
| `create`（默认首次） | 只新建，已存在则跳过 |
| `update` | 已存在则更新正文 |
| `copy` | 已存在也新建副本 |
| `dry-run` | 不写飞书，只出计划 |

### 7.3 删除策略

默认 **不删除** 飞书文档。
Notion 侧删除只写报告；需要归档时用户手动处理。

### 7.4 数据库

P1 先把 `<database>` 转成链接 + 报告；
P3 再做：
- 读 data source schema / rows；
- 建多维表格；
- 字段类型映射；
- 关联/公式/视图重建。

---

## 8. Agent 接入

### 8.1 统一能力

CLI 增加：

```bash
notion2feishu scan --json          # 扫描工作区，返回树/计数
notion2feishu plan --json          # 迁移计划
notion2feishu pull --page <url>    # 拉取到本地
notion2feishu migrate --json       # 执行，返回结构化结果
notion2feishu status --json        # 任务状态
```

所有命令支持 `--json`，字段稳定，便于 Agent 解析。

### 8.2 MCP 工具

```text
notion2feishu.scan()
notion2feishu.plan(source, target)
notion2feishu.pull(page, out)
notion2feishu.migrate(source, target, mode="dry_run"|"create"|"update", confirm=false)
notion2feishu.status(job_id)
```

安全：

- `migrate` 默认 `dry_run`；
- `confirm=true` 才写；
- token 只从环境变量读；
- 不提供删除工具；
- 返回值脱敏。

### 8.3 各 Agent

| Agent | 推荐接入 |
|---|---|
| Codex CLI | `AGENTS.md` + shell，直接跑 CLI |
| Trae | MCP Server + 规则文件 |
| DeepSeek | API function calling → HTTP 服务 |
| 豆包 | 智能体工具/MCP → HTTP 服务 |
| Claude / DSH | Skill + CLI |

### 8.4 HTTP 服务（给不能跑本地的 Agent）

```text
POST /v1/jobs        创建迁移任务
GET  /v1/jobs/{id}   查询进度
GET  /v1/jobs/{id}/report
POST /v1/jobs/{id}/retry
```

鉴权：Bearer token；任务隔离；token 加密存储。

---

## 9. 路线图

### P0（已完成）

- CLI：`plan / prepare / push / migrate / doctor`
- zip / 目录输入
- 图片上传、链接回填、断点续跑
- npx 启动器

### P1：Notion API 直连（1–2 天）

- `pull` 子命令
- `--notion-page` + `NOTION_TOKEN`
- 子页面递归、图片下载、v2 标签归一化
- 多顶层页面 / 排除名单
- `scan --json`

### P2：本地 Web 向导（3–5 天）

- `notion2feishu` 启动本地服务并打开浏览器
- 连接 / 选择 / 目标 / 预览 / 运行 / 结果
- 后台任务 + SQLite + 暂停/继续
- 失败重试、CSV 报告

### P3：增量 + 数据库（3–5 天）

- mapping 表 + `last_edited_time`
- 只同步变更
- 数据库 → 多维表格
- 冲突策略

### P4：Agent 生态（2–3 天）

- `AGENTS.md`
- Skill
- MCP Server
- HTTP API

---

## 10. 开放问题

1. **Notion 连接方式**：Internal Token vs OAuth。个人用 token 最快；公开产品用 OAuth。
2. **飞书目标**：我的文档库 vs 知识库 vs 云盘。默认哪个？建议「我的文档库」。
3. **数据库优先级**：很多用户的 Notion 核心是数据库；如果占比高，P3 要提前。
4. **团队权限**：迁移后文档 owner 是执行者，团队空间权限怎么继承？
5. **是否提供飞书机器人**：在飞书里发 Notion 链接直接迁移，体验更原生，但需要托管服务。

---

## 11. 衡量指标

- 首次成功迁移一页的时间（目标 < 3 分钟）
- 100 页迁移的人工干预次数（目标 < 5 次）
- 失败率 / 自动重试成功率
- 中断后续跑成功率
- 迁移后用户手动修复比例
