from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import make_config  # noqa: E402

import gui_fs  # noqa: E402


class GuiFsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gui-fs-")
        self.root = Path(self.tmp.name)
        make_config(self.root)
        self.paths, self.cfg = gui_fs.load_paths(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_sanitize_work_name(self):
        self.assertEqual(gui_fs.sanitize_work_name("  RJ01012345 "), "RJ01012345")
        self.assertEqual(gui_fs.sanitize_work_name("a/b:c"), "a_b_c")
        self.assertEqual(gui_fs.sanitize_work_name("CON"), "")
        self.assertEqual(gui_fs.sanitize_work_name(".."), "")
        self.assertEqual(gui_fs.sanitize_work_name("作品 名"), "作品 名")
        self.assertEqual(len(gui_fs.sanitize_work_name("x" * 200)), 80)

    def test_detect_work_hint(self):
        self.assertEqual(gui_fs.detect_work_hint(["track.mp3"], "RJ01012345"), "RJ01012345")
        self.assertEqual(gui_fs.detect_work_hint(["RJ01012345-a.mp3", "rj01012345-b.mp3"]), "RJ01012345")
        self.assertEqual(gui_fs.detect_work_hint(["RJ01012345-a.mp3", "RJ09999999-b.mp3"]), "")
        self.assertEqual(gui_fs.detect_work_hint(["track.mp3"]), "")

    def test_split_relpath_and_safe_join(self):
        self.assertEqual(gui_fs.split_relpath("disc1/track.mp3")[0], ["disc1", "track.mp3"])
        for bad in ("../x.mp3", "/abs/x.mp3", "a/../../x", ""):
            parts, err = gui_fs.split_relpath(bad)
            self.assertTrue(err, bad)
        with self.assertRaises(ValueError):
            gui_fs.safe_join(self.root, ["..", "x"])

    def test_upload_list_move(self):
        audio = b"fake audio"
        gui_fs.save_upload(self.paths, "audio", "track01.mp3", io.BytesIO(audio), len(audio), "RJ01012345")
        pb = b"[00:00:01] hello"
        gui_fs.save_upload(self.paths, "promptbook", "track01.txt", io.BytesIO(pb), len(pb), "RJ01012345")
        staged = gui_fs.list_staged(self.paths)
        self.assertEqual(staged["work_hint"], "RJ01012345")
        self.assertTrue(staged["has_audio"])
        self.assertTrue(staged["has_promptbook"])
        kinds, err = gui_fs.validate_staged_for_mode(
            staged, "all", use_existing=False, existing={})
        self.assertFalse(err, err)
        self.assertEqual(kinds, ["audio", "promptbook"])
        counts = gui_fs.move_staged_to_workspace(self.paths, "RJ01012345", kinds)
        self.assertEqual(counts.get("audio"), 1)
        self.assertTrue((self.paths.input / "RJ01012345" / "track01.mp3").is_file())
        self.assertTrue((self.paths.promptbook / "RJ01012345" / "track01.txt").is_file())
        existing = gui_fs.existing_counts(self.paths, "RJ01012345")
        self.assertEqual(existing["audio"], 1)
        self.assertEqual(existing["promptbook"], 1)
        # 制作翻译：字幕单独上传/搬移
        sub = b"1\n00:00:01,000 --> 00:00:02,000\nhello\n"
        gui_fs.save_upload(self.paths, "subtitle", "track01.srt", io.BytesIO(sub), len(sub), "RJ01012345")
        staged2 = gui_fs.list_staged(self.paths)
        self.assertTrue(staged2["has_subtitle"])
        kinds2, err2 = gui_fs.validate_staged_for_mode(
            staged2, "translate", use_existing=False, existing=existing)
        self.assertFalse(err2, err2)
        counts2 = gui_fs.move_staged_to_workspace(self.paths, "RJ01012345", kinds2)
        self.assertEqual(counts2.get("subtitle"), 1)
        self.assertTrue((self.paths.ja / "RJ01012345" / "track01.srt").is_file())

    def test_upload_rejections(self):
        with self.assertRaises(ValueError):
            gui_fs.save_upload(self.paths, "audio", "../evil.mp3", io.BytesIO(b"x"), 1)
        with self.assertRaises(ValueError):
            gui_fs.save_upload(self.paths, "audio", "model.gguf", io.BytesIO(b"x"), 1)
        with self.assertRaises(ValueError):
            gui_fs.save_upload(self.paths, "subtitle", "x.zh.txt", io.BytesIO(b"x"), 1)
        with self.assertRaises(ValueError):
            gui_fs.save_upload(self.paths, "promptbook", "x.md", io.BytesIO(b"x"), 1)
        with self.assertRaises(ValueError):
            gui_fs.save_upload(self.paths, "nope", "x.mp3", io.BytesIO(b"x"), 1)

    def test_mode_validation(self):
        audio = b"x"
        gui_fs.save_upload(self.paths, "audio", "track01.mp3", io.BytesIO(audio), 1)
        staged = gui_fs.list_staged(self.paths)
        kinds, err = gui_fs.validate_staged_for_mode(staged, "translate", use_existing=False, existing={})
        self.assertTrue(err)
        gui_fs.clear_staged(self.paths)
        gui_fs.save_upload(self.paths, "subtitle", "track01.srt", io.BytesIO(b"x"), 1)
        staged = gui_fs.list_staged(self.paths)
        kinds, err = gui_fs.validate_staged_for_mode(staged, "translate", use_existing=False, existing={})
        self.assertFalse(err, err)
        self.assertEqual(kinds, ["subtitle"])
        kinds, err = gui_fs.validate_staged_for_mode(staged, "all", use_existing=False, existing={})
        self.assertTrue(err)
        # 仅图片不算音频
        gui_fs.clear_staged(self.paths)
        gui_fs.save_upload(self.paths, "audio", "cover.jpg", io.BytesIO(b"x"), 1)
        staged = gui_fs.list_staged(self.paths)
        kinds, err = gui_fs.validate_staged_for_mode(staged, "all", use_existing=False, existing={})
        self.assertTrue(err)

    def test_results_zip_preview(self):
        work = "RJ01012345"
        subs = self.paths.output / work / "字幕文件"
        (subs / "日文").mkdir(parents=True)
        (subs / "中文").mkdir(parents=True)
        (subs / "日文" / "track01.ja.txt").write_text("[00:00:01] こんにちは\n", encoding="utf-8")
        (subs / "中文" / "track01.lrc").write_text("[ti:x]\n[00:00.01]你好\n", encoding="utf-8")
        (subs / "中文" / "track01.zh.txt").write_text("[00:00:01] 你好\n", encoding="utf-8")
        collect = self.paths.collect / work
        collect.mkdir(parents=True)
        (collect / "track01.lrc").write_text("[ti:x]\n[00:00.01]你好\n", encoding="utf-8")
        res = gui_fs.scan_results(self.paths, self.cfg, work, "all")
        self.assertTrue(res["has_bundle"], res)
        self.assertTrue(res["has_collect"])
        self.assertGreaterEqual(len(res["lrc"]), 1)
        self.assertEqual(res["lrc"][0]["source"], "bundle")
        self.assertFalse(res["incomplete"])
        data = gui_fs.zip_lrcs(res)
        self.assertIsNotNone(data)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn("track01.lrc", zf.namelist())
        prev = gui_fs.preview_text(res["lrc"][0]["path"], self.paths.data_roots())
        self.assertTrue(prev["ok"])
        self.assertIn("你好", prev["text"])
        # 路径越界
        bad = gui_fs.preview_text(str(self.root / "config.yaml"), self.paths.data_roots())
        self.assertFalse(bad["ok"])

    def test_missing_translation(self):
        work = "W1"
        ja = self.paths.ja / work
        ja.mkdir(parents=True)
        (ja / "track01.ja.txt").write_text("[00:00:01] こんにちは\n", encoding="utf-8")
        res = gui_fs.scan_results(self.paths, self.cfg, work, "all")
        self.assertTrue(res["incomplete"] or res["missing"])
        self.assertIn("track01", [m for m in res["missing"]])

    def test_refine_work_name_from_inputs(self):
        import subtitle
        work_dir = self.paths.input / "W"
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "a.mp3").write_bytes(b"x")
        self.assertEqual(
            subtitle._work_name_from_inputs([str(work_dir / "a.mp3")], self.paths.input), "W")
        other = self.paths.input / "X"
        other.mkdir(parents=True, exist_ok=True)
        (other / "b.mp3").write_bytes(b"x")
        self.assertIsNone(subtitle._work_name_from_inputs(
            [str(work_dir / "a.mp3"), str(other / "b.mp3")], self.paths.input))
        outside = self.root / "outside.mp3"
        outside.write_bytes(b"x")
        self.assertIsNone(subtitle._work_name_from_inputs([str(outside)], self.paths.input))

    def test_existing_and_clear(self):
        self.assertFalse(gui_fs.existing_counts(self.paths, "nope")["input_exists"])
        gui_fs.save_upload(self.paths, "audio", "a.mp3", io.BytesIO(b"x"), 1, "W")
        self.assertGreater(gui_fs.list_staged(self.paths)["total_size"], 0)
        gui_fs.clear_staged(self.paths)
        self.assertEqual(gui_fs.list_staged(self.paths)["total_size"], 0)
        self.assertEqual(gui_fs.list_staged(self.paths)["work_hint"], "")


if __name__ == "__main__":
    unittest.main()
