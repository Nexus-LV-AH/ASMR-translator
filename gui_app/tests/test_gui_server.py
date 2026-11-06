from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import make_config, write_gguf  # noqa: E402

import gui_fs  # noqa: E402
import gui_server  # noqa: E402


class GuiServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gui-server-")
        self.root = Path(self.tmp.name)
        self.model = write_gguf(self.root / "sakura-7b-q6k.gguf",
                                name="sakura-7b-test", size_label="7B", file_type=18)
        make_config(self.root, registry={
            "local-x": {"path": str(self.model), "by_tag": "Sakura Test Q6_K"},
        }, default="local-x")
        self.token = "test-token"
        self.app = gui_server.GuiApp(self.root, self.root / "config.yaml",
                                     token=self.token, port=0, no_browser=True)
        self.server = gui_server.make_server(self.app, 0)
        self.port = self.app.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.app.cleanup()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()

    def _req(self, method: str, path: str, body: bytes | None = None,
             headers: dict | None = None, *, token: bool = True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"X-GUI-Token": self.token} if token else {}
        if body is not None and "Content-Type" not in (headers or {}):
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def _json(self, method: str, path: str, obj: dict | None = None, **kw):
        body = json.dumps(obj or {}, ensure_ascii=False).encode("utf-8") if method == "POST" else None
        status, data = self._req(method, path, body, **kw)
        try:
            return status, json.loads(data)
        except ValueError:
            return status, {"_raw": data.decode("utf-8", errors="replace")}

    def test_page_static_and_auth(self):
        status, data = self._req("GET", f"/?token={self.token}", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", data.lower())
        status, data = self._req("GET", "/static/app.js", token=False)
        self.assertEqual(status, 200)
        self.assertGreater(len(data), 1000)
        status, _ = self._req("GET", "/?token=wrong", token=False)
        self.assertEqual(status, 403)
        status, _ = self._req("GET", "/api/state", token=False)
        self.assertEqual(status, 403)
        status, _ = self._req("GET", "/api/state", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_state_and_settings(self):
        status, data = self._json("GET", "/api/state")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        models = data["settings"]["models"]
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["key"], "local-x")
        self.assertTrue(models[0]["available"])
        self.assertEqual(models[0]["quant"], "Q6_K")
        self.assertEqual(data["settings"]["default_key"], "local-x")
        self.assertIn("paths", data["settings"])
        status, settings = self._json("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertIn("models", settings)
        status, health = self._json("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])
        self.assertEqual(health["port"], self.port)

    def test_upload_state_clear_and_validation(self):
        status, _ = self._json("POST", "/api/start", {
            "mode": "all", "work": "W", "model": "local-x",
        })
        self.assertEqual(status, 400)
        status, data = self._json("POST", "/api/start", {
            "mode": "all", "work": "", "model": "local-x",
        })
        self.assertEqual(status, 400)
        status, data = self._req(
            "POST", "/api/upload?kind=audio&relpath=track01.mp3&work_hint=W",
            b"fake", {"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200, data)
        body = json.loads(data)
        self.assertTrue(body["ok"])
        self.assertEqual(body["staged"]["work_hint"], "W")
        status, data = self._json("GET", "/api/state")
        self.assertEqual(len(data["staged"]["audio"]), 1)
        status, data = self._json("POST", "/api/clear", {})
        self.assertEqual(status, 200)
        status, data = self._json("GET", "/api/state")
        self.assertEqual(data["staged"]["total_size"], 0)

    def test_inspect_and_unknown_model(self):
        status, data = self._json("POST", "/api/models/inspect", {"path": str(self.model)})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["model"]["quant"], "Q6_K")
        status, data = self._json("POST", "/api/start", {
            "mode": "all", "work": "W", "model": "nope",
        })
        self.assertEqual(status, 400)
        self.assertIn("未知模型", data.get("error", ""))

    def test_shutdown_endpoint(self):
        status, data = self._json("POST", "/api/shutdown", {})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        # 服务线程会在稍后停止；tearDown 仍会做幂等清理
        time.sleep(0.3)


if __name__ == "__main__":
    unittest.main()
