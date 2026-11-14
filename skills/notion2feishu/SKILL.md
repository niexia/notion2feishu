---
name: notion2feishu
description: Migrate Notion pages or exported Markdown into Feishu (Lark) docs, preserving hierarchy, images and page links. Use when the user asks to migrate/backup Notion to Feishu, or when a Notion export zip needs to become Feishu wiki docs.
---

# notion2feishu

## Prerequisites

- `lark-cli` installed and logged in as a user:
  `lark-cli auth login --domain docs,drive,wiki,base`
- For API pull: a Notion Internal Integration token in `NOTION_TOKEN`,
  and the integration connected to the root page.
- For local export: a Notion Markdown/CSV zip or directory.

## Workflow

1. **Discover** (read-only):
   ```bash
   notion2feishu doctor
   notion2feishu scan --json
   ```
2. **Pull to local** (read-only for Feishu):
   ```bash
   notion2feishu pull --page "<notion-url>" --out ./notion-export --json
   ```
3. **Dry run**:
   ```bash
   notion2feishu migrate --notion-page "<notion-url>" \
     --target wiki --space-id my_library --dry-run
   ```
4. **Confirm with the user**, then run without `--dry-run` and with `--yes`.

## Local export

```bash
notion2feishu plan --src ./export.zip
notion2feishu migrate --src ./export.zip --target wiki --space-id my_library --yes
```

## Rules

- Never echo or persist `NOTION_TOKEN`/lark credentials.
- Never delete or overwrite existing Feishu docs.
- Do not run the real migration without the user's confirmation of target.
- If `lark-cli` user auth is missing, stop and report the exact login command.
