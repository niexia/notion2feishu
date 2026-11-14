import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notion2feishu import core  # noqa: E402

A_ID = "a" * 32
B_ID = "b" * 32
C_ID = "c" * 32


def make_export(root: Path) -> Path:
    export = root / "Export"
    (export / "A" / "B").mkdir(parents=True)
    (export / f"A {A_ID}.md").write_text(
        "# A\n\n"
        "Owner: Alice\n"
        "Last edited time: September 13, 2026 10:47 PM\n"
        "Created time: January 6, 2024 6:28 PM\n\n"
        f"[B](A/B%20{B_ID}.md)\n\n"
        "<aside>\n"
        "💡 提示内容\n"
        "</aside>\n",
        encoding="utf-8",
    )
    (export / "A" / f"B {B_ID}.md").write_text(
        "# B\n\n"
        f"![pic](B/pic.png)\n\n"
        f"[C](B/C%20{C_ID}.md)\n",
        encoding="utf-8",
    )
    (export / "A" / "B" / "pic.png").write_bytes(b"fake-png")
    (export / "A" / "B" / f"C {C_ID}.md").write_text("# C\n\nhello\n", encoding="utf-8")
    return export


class DiscoverTest(unittest.TestCase):
    def test_tree_and_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            export = make_export(Path(tmp))
            pages, roots = core.discover(export)

            self.assertEqual(len(pages), 3)
            self.assertEqual(roots, [A_ID])
            self.assertEqual(pages[A_ID].title, "A")
            self.assertEqual(pages[A_ID].children, [B_ID])
            self.assertEqual(pages[B_ID].children, [C_ID])
            self.assertTrue(pages[B_ID].asset_dir.name == "B")
            self.assertEqual([pages[pid].order for pid in (A_ID, B_ID, C_ID)], [0, 1, 2])

    def test_prepare_images_links_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            export = make_export(Path(tmp))
            out = Path(tmp) / "work"
            core.cmd_prepare(type("NS", (), {"src": str(export), "out": str(out), "force": True})())

            manifest = core.load_manifest(out)
            self.assertEqual(len(manifest["pages"]), 3)
            by_id = {p["id"]: p for p in manifest["pages"]}
            self.assertEqual(by_id[A_ID]["children"], [B_ID])
            self.assertEqual(by_id[B_ID]["image_count"], 1)

            a_md = (out / "pages" / f"{A_ID}.md").read_text(encoding="utf-8")
            self.assertNotIn("# A", a_md)
            self.assertNotIn("Owner:", a_md)
            self.assertIn(f"__FEISHU_URL_{B_ID}__", a_md)
            self.assertIn("> 💡 提示内容", a_md)

            b_md = (out / "pages" / f"{B_ID}.md").read_text(encoding="utf-8")
            self.assertIn(f"<@./assets/{B_ID}/pic.png>", b_md)
            self.assertIn(f"__FEISHU_URL_{C_ID}__", b_md)
            self.assertTrue((out / "assets" / B_ID / "pic.png").exists())


if __name__ == "__main__":
    unittest.main()
