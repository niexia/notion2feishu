import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notion2feishu import core, notion  # noqa: E402

A = "a" * 32
B = "b" * 32
C = "c" * 32
AID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
CID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class FakeNotion:
    titles = {AID: "A", BID: "B", CID: "C"}
    markdown = {
        AID: f'# A\n\n<page url="https://www.notion.so/B-{B}">B</page>\n\n![img](https://example.com/a.png)\n',
        BID: f'# B\n\n<page url="https://www.notion.so/C-{C}">C</page>\n\n<file source="https://example.com/f.pdf">f</file>\n',
        CID: "# C\n\nhello\n",
    }

    def get_page(self, pid):
        return {"properties": {"title": {"type": "title", "title": [{"plain_text": self.titles[pid]}]}}}

    def get_markdown(self, pid):
        return {"markdown": self.markdown[pid], "truncated": False, "unknown_block_ids": []}

    def get_block_markdown(self, pid):
        return {"markdown": ""}

    def download(self, url, dest_dir, prefix, max_bytes=0):
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / (prefix + Path(url).suffix)
        path.write_bytes(b"fake")
        return path

    def search_pages(self, max_pages=2000):
        return [
            {"id": AID, "properties": {"title": {"type": "title", "title": [{"plain_text": "A"}]}},
             "parent": {"type": "workspace"}, "last_edited_time": "2026-01-01"},
            {"id": BID, "properties": {"title": {"type": "title", "title": [{"plain_text": "B"}]}},
             "parent": {"type": "page_id", "page_id": AID}, "last_edited_time": "2026-01-02"},
            {"id": CID, "properties": {"title": {"type": "title", "title": [{"plain_text": "C"}]}},
             "parent": {"type": "workspace"}, "last_edited_time": "2026-01-03"},
        ]


class PageIdTest(unittest.TestCase):
    def test_urls_and_ids(self):
        self.assertEqual(notion.page_id_from(f"https://www.notion.so/Title-{A}?pvs=4"), AID)
        self.assertEqual(notion.page_id_from(AID, ), AID)
        self.assertEqual(notion.page_id_from(A), AID)


class MarkdownTest(unittest.TestCase):
    def test_parse_children_and_db(self):
        md = (
            f'<page url="https://www.notion.so/B-{B}">B</page>\n'
            f'<page url="https://www.notion.so/C-{C}"/>\n'
            f'<database url="https://www.notion.so/db-{A}">DB</database>\n'
        )
        kids = notion.parse_child_pages(md)
        self.assertEqual([k["id"] for k in kids], [BID, CID])
        dbs = notion.parse_databases(md)
        self.assertEqual(dbs[0]["title"], "DB")

    def test_normalize_keeps_code_fence(self):
        md = "```html\n<callout>keep</callout>\n```\n\n<callout>\n💡 hi\n</callout>\n"
        out = notion.normalize_markdown(md, {}, [])
        self.assertIn("<callout>keep</callout>", out)
        self.assertIn("> 💡 hi", out)


class PullTest(unittest.TestCase):
    def test_pull_builds_export_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "source"
            report = notion.pull_page_tree(FakeNotion(), [AID], out)
            self.assertEqual(report["total_pages"], 3)
            self.assertEqual(report["images"], 1)
            self.assertEqual(report["files"], 1)
            self.assertTrue((out / f"A {A}.md").exists())
            self.assertTrue((out / "A" / f"B {B}.md").exists())
            self.assertTrue((out / "A" / "B" / f"C {C}.md").exists())

            pages, roots = core.discover(out)
            self.assertEqual(len(pages), 3)
            self.assertEqual(roots, [A])
            self.assertEqual(pages[A].children, [B])
            self.assertEqual(pages[B].children, [C])

            work = Path(tmp) / "work"
            core.cmd_prepare(type("NS", (), {"src": str(out), "out": str(work), "force": True})())
            manifest = core.load_manifest(work)
            self.assertEqual(len(manifest["pages"]), 3)
            self.assertEqual(sum(p["image_count"] for p in manifest["pages"]), 1)

    def test_scan_workspace(self):
        tree = notion.scan_workspace(FakeNotion())
        self.assertEqual(tree["total_pages"], 3)
        self.assertEqual([r["id"] for r in tree["roots"]], [AID, CID])
        self.assertEqual(tree["roots"][0]["count"], 2)


if __name__ == "__main__":
    unittest.main()
