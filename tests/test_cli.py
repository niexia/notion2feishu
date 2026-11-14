import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from notion2feishu import cli  # noqa: E402

A_ID = "a" * 32


class SourceTest(unittest.TestCase):
    def test_normalize_descends_into_single_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            export = Path(tmp) / "wrapper" / "Export"
            export.mkdir(parents=True)
            (export / f"A {A_ID}.md").write_text("# A\n", encoding="utf-8")
            got = cli.normalize_export_dir(Path(tmp) / "wrapper")
            self.assertEqual(got, export.resolve())

    def test_extract_zip_skips_macosx(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zpath = tmp_path / "ExportBlock.zip"
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr(f"Export/A {A_ID}.md", "# A\n")
                zf.writestr("__MACOSX/._junk", "x")
            old = cli.CACHE_ROOT
            cli.CACHE_ROOT = tmp_path / "cache"
            try:
                got = cli.extract_zip(zpath)
            finally:
                cli.CACHE_ROOT = old
            self.assertTrue((got / f"A {A_ID}.md").exists())
            self.assertFalse((got.parent / "__MACOSX").exists())


if __name__ == "__main__":
    unittest.main()
