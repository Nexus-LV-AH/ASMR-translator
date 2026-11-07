from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import make_config, write_gguf  # noqa: E402

import gui_fs  # noqa: E402
import gui_models  # noqa: E402


class _MockApi(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, str]] = []
    key = "sk-test"

    def log_message(self, *args):  # noqa: D401
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        type(self).calls.append((self.command, self.path, auth))
        return auth == f"Bearer {type(self).key}"

    def do_GET(self):  # noqa: N802
        if not self._auth_ok():
            return self._send(401, {"error": {"message": "bad key"}})
        self._send(200, {"data": [{"id": "test-model"}, {"id": "other-model"}]})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(n)
        if not self._auth_ok():
            return self._send(401, {"error": {"message": "bad key"}})
        self._send(200, {
            "choices": [{"message": {"content": "[1. 早上好。"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6},
        })


class GuiModelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gui-models-")
        self.root = Path(self.tmp.name)
        self.mock = ThreadingHTTPServer(("127.0.0.1", 0), _MockApi)
        self.mock_port = self.mock.server_address[1]
        threading.Thread(target=self.mock.serve_forever, daemon=True).start()
        _MockApi.calls = []
        os.environ.pop("TEST_API_KEY", None)
        self.local_path = write_gguf(self.root / "sakura-7b-q6k.gguf",
                                     name="sakura-7b-test", size_label="7B", file_type=18)
        self.registry = {
            "local-x": {"path": str(self.local_path), "by_tag": "Sakura Test Q6_K"},
            "api-x": {
                "api": "custom",
                "base_url": f"http://127.0.0.1:{self.mock_port}",
                "model": "test-model",
                "api_key_env": "TEST_API_KEY",
                "by_tag": "Test API",
            },
        }
        make_config(self.root, registry=self.registry, default="api-x")
        self.p1 = mock.patch.object(gui_models.CLI, "recall_model", return_value="")
        self.p2 = mock.patch.object(gui_models.CLI, "remember_model")
        self.p1.start()
        self.p2.start()
        self.store = gui_models.ConfigStore(self.root)
        self.paths, self.cfg = gui_fs.load_paths(self.root)

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.mock.shutdown()
        self.mock.server_close()
        os.environ.pop("TEST_API_KEY", None)
        self.tmp.cleanup()

    def test_models_and_auto_detection(self):
        payload = self.store.models_payload()
        models = {m["key"]: m for m in payload["models"]}
        self.assertIn("local-x", models)
        self.assertIn("api-x", models)
        local = models["local-x"]
        self.assertTrue(local["available"])
        self.assertEqual(local["quant"], "Q6_K")
        self.assertEqual(local["params"], "7B")
        self.assertIn("Q6_K", local["auto_display_name"])
        self.assertEqual(local["display_name"], "Sakura Test Q6_K")
        api = models["api-x"]
        self.assertFalse(api["available"])
        self.assertEqual(api["key_source"], "none")
        self.assertEqual(payload["default_key"], "local-x")
        self.assertTrue(any("API" in w for w in payload["config_warnings"]))

    def test_env_key_and_api_test(self):
        gui_models.env_set(self.root / ".env", "TEST_API_KEY", _MockApi.key)
        self.store.refresh(force=True)
        payload = self.store.models_payload()
        api = next(m for m in payload["models"] if m["key"] == "api-x")
        self.assertTrue(api["available"])
        self.assertEqual(api["key_source"], "env")
        self.assertEqual(payload["default_key"], "api-x")
        result = self.store.test_api(key="api-x", base_url=f"http://127.0.0.1:{self.mock_port}",
                                     model="test-model")
        self.assertTrue(result["ok"], result)
        self.assertIn("早上好", result["sample"])
        self.assertEqual(result["finish_reason"], "stop")
        self.assertNotIn("sk-test", json.dumps(result, ensure_ascii=False))
        # 测试成功后模型列表里有 test-model
        self.assertIn("test-model", result["models"])
        # 错误 key
        bad = self.store.test_api(key="api-x", api_key="sk-bad",
                                  base_url=f"http://127.0.0.1:{self.mock_port}", model="test-model")
        self.assertFalse(bad["ok"])
        self.assertNotIn("sk-bad", json.dumps(bad, ensure_ascii=False))

    def test_save_api_writes_env_and_local_config(self):
        (self.root / ".env").write_text("# comment\nOTHER=1\n", encoding="utf-8")
        saved = self.store.save_api(key="api-x", provider="custom", api_key="sk-new",
                                    base_url=f"http://127.0.0.1:{self.mock_port}",
                                    model="test-model", display_name="My API")
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["key"], "api-x")
        env_text = (self.root / ".env").read_text(encoding="utf-8")
        self.assertIn("# comment", env_text)
        self.assertIn("OTHER=1", env_text)
        self.assertIn("TEST_API_KEY=sk-new", env_text)
        local = gui_models._read_yaml(self.root / "config.local.yaml")
        entry = local["translation"]["registry"]["api-x"]
        self.assertEqual(entry["base_url"], f"http://127.0.0.1:{self.mock_port}")
        self.assertEqual(entry["model"], "test-model")
        self.assertEqual(entry["by_tag"], "My API")
        self.assertNotIn("sk-new", json.dumps(saved, ensure_ascii=False))
        # 清除 key
        cleared = self.store.clear_api_key("api-x")
        self.assertTrue(cleared["ok"])
        self.assertNotIn("TEST_API_KEY=sk-new", (self.root / ".env").read_text(encoding="utf-8"))
        payload = self.store.models_payload()
        api = next(m for m in payload["models"] if m["key"] == "api-x")
        self.assertFalse(api["available"])

    def test_add_remove_default_local_model(self):
        (self.root / "config.local.yaml").write_text(
            "some_key: keep_me\n", encoding="utf-8")
        self.store.refresh(force=True)
        new_model = write_gguf(self.root / "another-iq4xs.gguf",
                               name="sakura-another", size_label="14B", file_type=30)
        view = self.store.add_local_model(new_model, display_name="Another Local", make_default=True)
        self.assertEqual(view["kind"], "local")
        self.assertEqual(view["quant"], "IQ4_XS")
        local = gui_models._read_yaml(self.root / "config.local.yaml")
        self.assertEqual(local.get("some_key"), "keep_me")
        reg = local["translation"]["registry"]
        keys = [k for k, v in reg.items() if str(v.get("path", "")).endswith("another-iq4xs.gguf")]
        self.assertEqual(len(keys), 1)
        key = keys[0]
        self.assertEqual(local["translation"]["default"], key)
        # 默认切换
        self.store.set_default("local-x")
        local = gui_models._read_yaml(self.root / "config.local.yaml")
        self.assertEqual(local["translation"]["default"], "local-x")
        # 动态条目可移除；config.yaml 里的条目不能从界面删除
        self.store.remove_local_model(key)
        local = gui_models._read_yaml(self.root / "config.local.yaml")
        self.assertNotIn(key, local["translation"]["registry"])
        with self.assertRaises(ValueError):
            self.store.remove_local_model("local-x")

    def test_config_warnings_and_settings_payload(self):
        payload = self.store.settings_payload(self.paths)
        self.assertTrue(payload["ok"] is not False if "ok" in payload else True)
        self.assertIn("models", payload)
        self.assertIn("paths", payload)
        self.assertIn("config_path", payload)
        self.assertIn("env_path", payload)
        self.assertNotIn("sk-test", json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
