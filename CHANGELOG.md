# Changelog

## 0.2.1

- 重新设计 Web 向导：响应式布局、移动端底部操作栏、可折叠页面树、任务阶段进度、暗色模式。
- README 增加桌面/移动端截图。

## 0.2.0

- Notion API 直连：`scan`、`pull`、`migrate --notion-page`，不再需要手动导出。
- Notion-flavored Markdown 归一化：callout/toggle/columns/page/database/file 等。
- 本地 Web 向导：连接 Notion、选择页面、选择目标、后台任务、进度与结果链接。
- `notion2feishu` 无参数在终端打开 Web 向导；非 TTY 仍走 CLI。
- `AGENTS.md`、`skills/notion2feishu/SKILL.md`、`llms.txt`。

## 0.1.0

- 一条命令 `notion2feishu`：自动找导出、解压、授权检查、迁移。
- 保留 Notion 父子层级（Wiki 节点 / 云盘文件夹）。
- 本地图片自动上传，附件转 `<source>`。
- 页间链接在全部文档建完后回填为飞书 URL。
- `--dry-run`、`--resume`、`--fresh`、`--limit`。
- 支持 `.zip` 和目录作为输入。
- 提供 `npx github:niexia/notion2feishu` 启动器（仍需本机 Python 3.9+）。
- MIT License。
