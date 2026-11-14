#!/usr/bin/env python3
"""兼容入口：在仓库根目录直接 `python3 notion2feishu.py`。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from notion2feishu.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
