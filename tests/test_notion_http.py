import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notion2feishu import notion  # noqa: E402

AID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/users/me":
            self._json({"object": "user", "id": "bot"})
        elif path.startswith("/v1/pages/") and path.endswith("/markdown"):
            pid = path.split("/")[3]
            md = {
                AID: f'# A\n\n<page url="https://www.notion.so/B-{BID.replace("-", "")}">B</page>\n\n'
                     f'![img](http://127.0.0.1:{self.server.server_address[1]}/img.png)\n',
                BID: "# B\n\nhello\n",
            }[pid]
            self._json({"object": "page_markdown", "id": pid, "markdown": md,
                        "truncated": False, "unknown_block_ids": []})
        elif path.startswith("/v1/pages/"):
            pid = path.split("/")[3]
            title = "A" if pid == AID else "B"
            self._json({"object": "page", "id": pid,
                        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}}})
        elif path == "/img.png":
            data = b"fake-png"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._json({"object": "error", "message": "not found"}, 404)


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.old = notion.API_BASE
        notion.API_BASE = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def tearDown(self):
        notion.API_BASE = self.old
        self.server.shutdown()
        self.server.server_close()

    def test_client_and_pull(self):
        client = notion.NotionClient("ntn_test", min_interval=0)
        self.assertIn("object", client.validate())
        with tempfile.TemporaryDirectory() as tmp:
            report = notion.pull_page_tree(client, [AID], Path(tmp) / "source")
            self.assertEqual(report["total_pages"], 2)
            self.assertEqual(report["images"], 1)
            self.assertTrue((Path(tmp) / "source" / "A" / "image-001.png").exists())


if __name__ == "__main__":
    unittest.main()


class MigrateNotionTest(unittest.TestCase):
    def test_cmd_migrate_dry_run(self):
        from types import SimpleNamespace
        from contextlib import redirect_stdout
        import io
        from unittest import mock
        from notion2feishu import cli

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        old = notion.API_BASE
        notion.API_BASE = f"http://127.0.0.1:{server.server_address[1]}/v1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                args = SimpleNamespace(
                    src=None,
                    notion_page=[f"https://www.notion.so/A-{AID.replace('-', '')}"],
                    notion_token="ntn_test",
                    pull_limit=None,
                    work=str(Path(tmp) / "work"),
                    target="wiki",
                    space_id="my_library",
                    parent_node_token=None,
                    folder_token=None,
                    identity="user",
                    limit=None,
                    skip_fill=False,
                    dry_run=True,
                    sleep=0,
                    force_state=False,
                    yes=True,
                    fresh=True,
                    no_login=True,
                    base_url="https://example.feishu.cn",
                )
                buf = io.StringIO()
                with mock.patch.object(cli, "ensure_lark", lambda: None), redirect_stdout(buf):
                    rc = cli.cmd_migrate(args)
                self.assertEqual(rc, 0)
                self.assertTrue((Path(tmp) / "work" / "manifest.json").exists())
                self.assertIn("建节点：2", buf.getvalue())
        finally:
            notion.API_BASE = old
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
