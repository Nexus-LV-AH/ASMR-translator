from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import make_config  # noqa: E402

import gui_fs  # noqa: E402
import gui_jobs  # noqa: E402


class GuiJobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gui-jobs-")
        self.root = Path(self.tmp.name)
        make_config(self.root)
        self.paths, self.cfg = gui_fs.load_paths(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_commands(self):
        phases = gui_jobs.build_commands(
            "all", self.paths, "RJ1", "deepseek-api",
            {"force": True, "bilingual": True, "limit": 20, "no_consolidate": True},
        )
        self.assertEqual([p.name for p in phases], ["extract", "refine", "translate"])
        extract = phases[0].argv
        self.assertIn("extract_ja.py", extract[1])
        self.assertIn("--force", extract)
        refine = phases[1].argv
        self.assertIn("--work", refine)
        self.assertIn("RJ1", refine)
        self.assertIn("--model", refine)
        translate = phases[2].argv
        self.assertIn("--model", translate)
        self.assertIn("--bilingual", translate)
        self.assertIn("--limit", translate)
        self.assertIn("--no-consolidate", translate)
        self.assertTrue(any("translate.py" in a for a in translate))
        # 只提取 + 无模型 → refine --no-model
        phases2 = gui_jobs.build_commands("extract", self.paths, "RJ1", "", {})
        self.assertEqual([p.name for p in phases2], ["extract", "refine"])
        self.assertIn("--no-model", phases2[1].argv)
        # 制作翻译需要模型
        with self.assertRaises(ValueError):
            gui_jobs.build_commands("translate", self.paths, "RJ1", "", {})

    def test_progress_parser_extract_translate(self):
        p = gui_jobs.ProgressParser("all", total_files=2, total_tracks=2)
        p.begin_phase("extract")
        for line in [
            "待处理文件 2 个",
            "开始处理：a.mp3",
            "[1/4] 解码",
            "[2/4] 分段",
            "[3/4] 转写 10 段",
            "转写进度 5/10",
            "[4/4] 组装",
            "完成：12 条字幕 → a.ja.txt",
        ]:
            p.feed("extract", line)
            self.assertGreaterEqual(p.snapshot()["percent"], 0)
        s = p.snapshot()
        self.assertEqual(s["completed_files"], 1)
        self.assertGreater(s["percent"], 10)
        p.end_phase("extract")
        self.assertEqual(p.snapshot()["percent"], 45.0)
        p.begin_phase("refine")
        p.feed("refine", "没有台本（promptbook 下没有 .txt）→ 跳过台本精修，继续后面的流程")
        p.end_phase("refine")
        self.assertTrue(p.snapshot()["refine_skipped"])
        p.begin_phase("translate")
        p.set_total_tracks(2)
        p.feed("translate", "翻译 a.ja.txt：30 行，每批 12 行")
        p.feed("translate", "进度 15/30 行（2 行/秒，剩余约 8 秒）")
        s = p.snapshot()
        self.assertEqual(s["stage"], "translate")
        self.assertEqual(s["eta_seconds"], 8)
        self.assertGreater(s["percent"], 55)
        p.feed("translate", "完成：a.ja.txt（a.lrc，用时 10s）")
        p.feed("translate", "任务结束，开始归档工作区（W）")
        p.feed("translate", "已归档作品 W")
        s = p.snapshot()
        self.assertEqual(s["stage"], "archive")
        self.assertGreaterEqual(s["percent"], 90)

    def _fake_builder(self, mode, paths, work, model, options):
        code = (
            "print('待处理文件 1 个');"
            "print('开始处理：a.mp3');"
            "print('转写进度 1/1');"
            "print('完成：3 条字幕 → a.ja.txt');"
            "print('任务结束，开始归档工作区');"
            "print('已归档作品 W')"
        )
        return [gui_jobs.Phase("extract", "fake", [sys.executable, "-u", "-c", code])]

    def test_job_manager_run(self):
        mgr = gui_jobs.JobManager(self.root, self.paths, command_builder=self._fake_builder)
        job = mgr.start(mode="extract", work="W", model="", options={"prevent_sleep": False})
        self.assertIn(job["status"], ("preparing", "running"))
        deadline = time.time() + 15
        while time.time() < deadline:
            snap = mgr.snapshot(0)
            if snap["job"]["status"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.05)
        snap = mgr.snapshot(0)
        self.assertEqual(snap["job"]["status"], "done", snap["job"].get("error"))
        self.assertEqual(snap["job"]["percent"], 100.0)
        self.assertGreater(len(snap["logs"]["entries"]), 3)
        self.assertTrue(Path(snap["job"]["log_file"]).is_file())
        self.assertTrue((self.paths.gui / "last_job.json").is_file())
        self.assertTrue(any("任务完成" in e["text"] for e in snap["logs"]["entries"]))
        mgr.shutdown()

    def test_job_manager_cancel(self):
        def slow_builder(mode, paths, work, model, options):
            code = "import time; print('待处理文件 1 个'); print('开始处理：a.mp3'); time.sleep(30)"
            return [gui_jobs.Phase("extract", "slow", [sys.executable, "-u", "-c", code])]

        mgr = gui_jobs.JobManager(self.root, self.paths, command_builder=slow_builder)
        mgr.start(mode="extract", work="W2", model="", options={"prevent_sleep": False})
        time.sleep(0.8)
        self.assertTrue(mgr.cancel())
        deadline = time.time() + 20
        while time.time() < deadline:
            snap = mgr.snapshot(0)
            if snap["job"]["status"] in ("cancelled", "done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual(mgr.snapshot(0)["job"]["status"], "cancelled")
        self.assertFalse(mgr.is_running())
        mgr.shutdown()

    def test_busy_start(self):
        def slow_builder(mode, paths, work, model, options):
            return [gui_jobs.Phase("extract", "slow", [sys.executable, "-u", "-c", "import time; time.sleep(20)"])]
        mgr = gui_jobs.JobManager(self.root, self.paths, command_builder=slow_builder)
        mgr.start(mode="extract", work="W3", model="", options={"prevent_sleep": False})
        time.sleep(0.3)
        with self.assertRaises(RuntimeError):
            mgr.start(mode="extract", work="W3", model="", options={"prevent_sleep": False})
        mgr.cancel()
        time.sleep(0.5)
        mgr.shutdown()


if __name__ == "__main__":
    unittest.main()
