from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import ROOT, write_gguf  # noqa: E402

import gguf_meta  # noqa: E402


class GgufMetaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gguf-test-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_metadata_name_quant_params(self):
        p = write_gguf(self.root / "sakura-14b-qwen3-v1.5-q6k.gguf",
                       name="sakura-14b-qwen3-v1.5", arch="qwen3", size_label="15B",
                       file_type=18, context=32768)
        info = gguf_meta.inspect_model(p)
        d = info.as_dict()
        self.assertTrue(d["ok"], d.get("error"))
        self.assertEqual(d["name"], "sakura-14b-qwen3-v1.5")
        self.assertEqual(d["architecture"], "qwen3")
        self.assertEqual(d["params"], "15B")
        self.assertEqual(d["quant"], "Q6_K")
        self.assertEqual(d["quant_source"], "metadata")
        self.assertEqual(d["context_length"], 32768)
        self.assertTrue(d["has_chat_template"])
        self.assertEqual(d["file_type"], 18)
        self.assertIn("Sakura", d["auto_display_name"])
        self.assertIn("Q6_K", d["auto_display_name"])
        self.assertTrue(d["translation_specialized"])
        self.assertEqual(d["warnings"], [])

    def test_file_type_iq4xs(self):
        p = write_gguf(self.root / "model-iq4xs.gguf", name="sakura-14b-qwen3-v1.5",
                       file_type=30)
        d = gguf_meta.inspect_model(p).as_dict()
        self.assertTrue(d["ok"])
        self.assertEqual(d["quant"], "IQ4_XS")

    def test_filename_quant_fallback(self):
        p = write_gguf(self.root / "Sakura-Galtransl-14B-v3.8-Q4_K_M.gguf",
                       name="Sakura-Galtransl-14B-v3.8", size_label="14B", file_type=None)
        d = gguf_meta.inspect_model(p).as_dict()
        self.assertTrue(d["ok"])
        self.assertEqual(d["quant"], "Q4_K_M")
        self.assertEqual(d["quant_source"], "filename")
        self.assertIn("GalTransl", d["auto_display_name"])

    def test_compact_quant_filename(self):
        p = write_gguf(self.root / "sakura-14b-qwen3-v1.5-q6k.gguf",
                       name="sakura-14b-qwen3-v1.5", file_type=None)
        d = gguf_meta.inspect_model(p).as_dict()
        self.assertEqual(d["quant"], "Q6_K")

    def test_non_translation_warning(self):
        p = write_gguf(self.root / "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
                       name="Qwen2.5-7B-Instruct", size_label="7B", file_type=15)
        d = gguf_meta.inspect_model(p).as_dict()
        self.assertTrue(d["ok"])
        self.assertFalse(d["translation_specialized"])
        self.assertTrue(any("翻译" in w for w in d["warnings"]))
        self.assertIn("Qwen2.5", d["auto_display_name"])

    def test_no_chat_template_warning(self):
        p = write_gguf(self.root / "x.gguf", chat_template=False)
        d = gguf_meta.inspect_model(p).as_dict()
        self.assertFalse(d["has_chat_template"])
        self.assertTrue(any("chat_template" in w for w in d["warnings"]))

    def test_invalid_magic(self):
        p = self.root / "bad.gguf"
        p.write_bytes(b"NOPE" + b"\x00" * 100)
        d = gguf_meta.inspect_model(p).as_dict()
        self.assertFalse(d["ok"])
        self.assertIn("GGUF", d["error"])

    def test_truncated_metadata_keeps_filename(self):
        p = self.root / "sakura-7b-q6k.gguf"
        # 声称有 2 个 KV，但正文缺失 → 解析不完整，回退文件名
        p.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 100) + b"\x00" * 48)
        d = gguf_meta.inspect_model(p).as_dict()
        self.assertTrue(d["ok"])
        self.assertFalse(d["metadata_complete"])
        self.assertEqual(d["quant"], "Q6_K")

    def test_budget_fallback(self):
        p = write_gguf(self.root / "sakura-7b-q6k.gguf", name="sakura-7b-q6k")
        d = gguf_meta.inspect_model(p, budget=8).as_dict()
        self.assertTrue(d["ok"])
        self.assertFalse(d["metadata_complete"])
        self.assertEqual(d["quant"], "Q6_K")

    def test_split_first_part(self):
        p2 = write_gguf(self.root / "model-00002-of-00002.gguf", split_count=2, split_no=2)
        write_gguf(self.root / "model-00001-of-00002.gguf", split_count=2, split_no=1)
        d = gguf_meta.inspect_model(p2).as_dict()
        self.assertEqual(d["split_count"], 2)
        self.assertTrue(d["load_path"].endswith("model-00001-of-00002.gguf"))
        self.assertTrue(any("第 1 片" in w or "分片" in w for w in d["warnings"]))

    def test_scan_directory(self):
        write_gguf(self.root / "a-q6k.gguf", name="sakura-a")
        write_gguf(self.root / "b-q4.gguf", name="sakura-b", file_type=15)
        write_gguf(self.root / "c-00001-of-00002.gguf", name="sakura-c", split_count=2, split_no=1)
        write_gguf(self.root / "c-00002-of-00002.gguf", name="sakura-c", split_count=2, split_no=2)
        cands = gguf_meta.scan_gguf_files(self.root)
        names = [c["file_name"] for c in cands]
        self.assertEqual(len(cands), 3)
        self.assertIn("a-q6k.gguf", names)
        self.assertIn("c-00001-of-00002.gguf", names)

    def test_humanize(self):
        self.assertEqual(gguf_meta.humanize_model_name("sakura-14b-qwen3-v1.5"),
                         "Sakura 14B Qwen3 v1.5")
        self.assertIn("Qwen2.5 7B Instruct",
                      gguf_meta.humanize_model_name("Qwen2.5-7B-Instruct-Q4_K_M"))
        self.assertIn("GalTransl", gguf_meta.humanize_model_name("Sakura-Galtransl-14B-v3.8"))

    @unittest.skipUnless((Path("/Users/nexus_g/modelbase/sakura-14b-qwen3-v1.5-q6k.gguf")).is_file(),
                         "本机模型不存在")
    def test_real_local_models(self):
        for name, quant, gb in (("sakura-14b-qwen3-v1.5-q6k.gguf", "Q6_K", 11.29),
                                ("sakura-14b-qwen3-v1.5-iq4xs.gguf", "IQ4_XS", 7.62)):
            d = gguf_meta.inspect_model(Path("/Users/nexus_g/modelbase") / name).as_dict()
            self.assertTrue(d["ok"])
            self.assertEqual(d["quant"], quant)
            self.assertAlmostEqual(d["size_gb"], gb, delta=0.05)
            self.assertEqual(d["name"], "sakura-14b-qwen3-v1.5")


if __name__ == "__main__":
    unittest.main()
