# Contributing

## 开发环境

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试不依赖真实飞书：用临时目录构造 Notion 导出 fixture，验证树还原、图片复制、
链接占位符、元信息清理等纯本地逻辑。

## 提交 PR

- 保持 `core.py` 不依赖第三方包；
- 新增解析/改写行为时补一个 fixture 测试；
- 不要提交 `state.json`、`*.final.md`、`.notion2feishu/`；
- 涉及 lark-cli 的变更，请在 PR 描述里附上 `notion2feishu push --dry-run` 的输出。
