# Agent guide: notion2feishu

Use this repo when a user asks to migrate/backup Notion pages into Feishu (Lark) docs.

## Safety rules

- Never print or commit `NOTION_TOKEN`, `GH_TOKEN`, or lark-cli credentials.
- Always run `notion2feishu doctor` (or `lark-cli auth status`) before writing.
- Always do a dry run first:
  `notion2feishu migrate --notion-page <url> --dry-run`
- Do not delete or overwrite existing Feishu docs. This tool only creates/appends.
- Ask the user to confirm the target (我的文档库 / 知识库 / 云盘) before a real run.
- If auth is missing, stop and give the exact command; do not invent tokens.

## Local Web wizard

```bash
notion2feishu          # opens http://127.0.0.1:<port> in a browser
notion2feishu ui --no-open --port 8765
```

The wizard does connect → scan → select → target → run → result. It stores the
Notion token in `~/.notion2feishu/config.json` with mode 600.
In non-TTY/agent contexts `notion2feishu` with no args falls back to the CLI
`migrate`; use explicit `scan` / `pull` / `migrate` commands instead of the UI.

## Read-only discovery

```bash
notion2feishu scan --json
notion2feishu pull --page "<notion-url>" --out ./notion-export --json
notion2feishu plan --src ./notion-export
```

`scan --json` returns:

```json
{"roots":[{"id":"...","title":"...","count":42,"children":[...]}],"total_pages":130}
```

Use `roots[].id` as the selection; `pull` accepts the same page URLs/IDs.

## Run

```bash
export NOTION_TOKEN=ntn_xxx
lark-cli auth login --domain docs,drive,wiki,base

# dry run
notion2feishu migrate --notion-page "<url>" --target wiki --space-id my_library --dry-run

# real run, after user confirmation
notion2feishu migrate --notion-page "<url>" --target wiki --space-id my_library --yes
```

For a local zip export:

```bash
notion2feishu migrate --src ./export.zip --target wiki --space-id my_library
```

## Targets

- `--target wiki --space-id my_library` (default; personal document library)
- `--target wiki --space-id <space_id>` (team wiki; get it from `lark-cli wiki +space-list --as user`)
- `--target drive --folder-token fldcn...` (cloud drive folder)

## Failure handling

- 401/403 from Notion: token invalid or integration not connected to the root page.
- `21001` from Feishu: user lacks wiki permission or tenant policy blocks it.
- Images 403: pre-signed URL expired; rerun `pull`/`migrate` to refresh.
- Use `--resume` for CLI `push`; `migrate` already resumes by default.
