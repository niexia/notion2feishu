import json
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notion2feishu import web  # noqa: E402


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old_config = web.CONFIG_PATH
        self.old_db = web.JOBS_DB
        self.old_jobs = web.JOB_ROOT
        web.CONFIG_PATH = root / "config.json"
        web.JOBS_DB = root / "jobs.db"
        web.JOB_ROOT = root / "jobs"
        self.store = web.JobStore(web.JOBS_DB)
        web.Handler.store = self.store
        web.Handler.runner = web.Runner(self.store)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        web.CONFIG_PATH = self.old_config
        web.JOBS_DB = self.old_db
        web.JOB_ROOT = self.old_jobs
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return resp.status, resp.read()

    def test_index(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"notion2feishu", body)
        self.assertIn(b"\xe9\x80\x89\xe6\x8b\xa9\xe9\xa1\xb5\xe9\x9d\xa2", body)  # 选择页面

    def test_config(self):
        status, body = self.get("/api/config")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("notion_connected", data)
        self.assertFalse(data["notion_connected"])
        self.assertIn("lark_user_ready", data)

    def test_job_store(self):
        job = self.store.create({"roots": ["x"], "target": "wiki"})
        self.assertEqual(self.store.get(job["id"])["status"], "queued")
        self.store.update(job["id"], status="running", progress=10)
        self.assertEqual(self.store.get(job["id"])["progress"], 10)
        self.store.append_log(job["id"], ["hello"])
        self.assertEqual(self.store.get(job["id"])["logs"][-1], "hello")


if __name__ == "__main__":
    unittest.main()


class RunnerTest(unittest.TestCase):
    def test_job_orchestration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = web.JobStore(root / "jobs.db")
            runner = web.Runner(store)
            old_jobs = web.JOB_ROOT
            web.JOB_ROOT = root / "jobs"

            def fake_pull(client, roots, source, progress=None, limit=None):
                source = Path(source)
                source.mkdir(parents=True, exist_ok=True)
                (source / "A.md").write_text("# A\n", encoding="utf-8")
                if progress:
                    progress(1, 1, "done")
                return {"pages": [{"id": "a", "title": "A"}], "databases": [],
                        "errors": [], "images": 1, "files": 0, "total_pages": 1,
                        "limit_hit": False}

            def fake_prepare(ns):
                work = Path(ns.out)
                work.mkdir(parents=True, exist_ok=True)
                (work / "manifest.json").write_text(
                    json.dumps({"roots": ["a"], "pages": [{"id": "a", "children": []}]}),
                    encoding="utf-8",
                )
                return 0

            def fake_push(ns):
                work = Path(ns.work)
                work.mkdir(parents=True, exist_ok=True)
                (work / "state.json").write_text(
                    json.dumps({"pages": {"a": {"url": "https://example.feishu.cn/wiki/x", "title": "A"}}}),
                    encoding="utf-8",
                )
                return 0

            spec = {
                "roots": ["a"], "target": "wiki", "space_id": "my_library",
                "parent_node_token": None, "folder_token": None,
                "base_url": "https://example.feishu.cn", "container_title": "",
            }
            job = store.create(spec)
            try:
                with mock.patch.object(web, "notion_token", return_value="ntn_test"), \
                     mock.patch.object(web.notion, "NotionClient", return_value=object()), \
                     mock.patch.object(web.notion, "pull_page_tree", side_effect=fake_pull), \
                     mock.patch.object(web.core, "cmd_prepare", side_effect=fake_prepare), \
                     mock.patch.object(web.core, "cmd_push", side_effect=fake_push):
                    runner.start(job["id"])
                    deadline = time.time() + 5
                    while time.time() < deadline:
                        current = store.get(job["id"])
                        if current["status"] in ("done", "failed"):
                            break
                        time.sleep(0.05)
                current = store.get(job["id"])
                self.assertEqual(current["status"], "done", current.get("error"))
                self.assertEqual(current["result"]["pages"], 1)
                self.assertEqual(current["result"]["root_links"][0]["url"], "https://example.feishu.cn/wiki/x")
            finally:
                web.JOB_ROOT = old_jobs
