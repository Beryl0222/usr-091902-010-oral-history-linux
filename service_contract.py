"""服务契约：健康检查保持稳定，领域 API 走通主流程。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from oralhistory import Engine
from oralhistory.model import ACT_CITE, ACT_LIBRARY_LISTEN, ACT_PUBLIC_LISTEN
from oralhistory.store import Store
from service import SERVICE_ID, SERVICE_NAME, Handler, health_payload


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_health_payload_has_stable_identity(self):
        self.assertEqual(health_payload(), {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME})

    def test_health_endpoint_returns_json(self):
        with urlopen(f"{self.base_url}/health", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            self.assertEqual(json.load(response), health_payload())

    def test_unknown_route_is_not_exposed(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


class ApiFlowTest(unittest.TestCase):
    """通过 HTTP 走通：登记→数字化→切分→许可→发布→转写→纠错→收紧→溯源。"""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.server.engine = Engine(Store())  # 隔离于默认引擎

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, payload=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        with urlopen(request, timeout=3) as response:
            return response.status, json.load(response)

    def test_full_domain_flow_over_http(self):
        import base64

        # 载体 → 数字化母版
        _, carrier = self.call("POST", "/api/carriers", {
            "label": "磁带 B-02", "medium": "盒式磁带",
            "donor": "老战士 王某", "donated_at": "2025-12-01",
        })
        _, digitized = self.call("POST", f"/api/carriers/{carrier['id']}/digitize", {
            "operator": "数字化馆员 王", "equipment": "Tascam DA-3000",
            "content_base64": base64.b64encode(b"x" * 6000).decode("ascii"),
        })
        master_id = digitized["master"]["id"]

        # 降噪衍生品并核验来源
        _, denoised = self.call("POST", f"/api/files/{master_id}/derive", {
            "operation": "denoise", "created_by": "数字化馆员 王",
            "content_base64": base64.b64encode(b"y" * 6000).decode("ascii"),
            "params": {"profile": "voice"},
        })
        _, provenance = self.call("GET", f"/api/files/{denoised['id']}/provenance")
        self.assertTrue(provenance["verified"])
        self.assertEqual(len(provenance["chain"]), 2)

        # 说话人（延迟解密）与片段
        _, speaker = self.call("POST", "/api/speakers", {
            "display_name": "未公开说话人", "real_name": "赵守信",
            "declassify_at": "2030-01-01",
        })
        _, segment = self.call("POST", f"/api/files/{master_id}/segments", {
            "start_ms": 0, "end_ms": 3000, "title": "自述片段",
            "speaker_ids": [speaker["id"]],
        })
        seg_id = segment["id"]

        # 许可：公开+馆内+引用、禁下载；首批发布
        self.call("POST", f"/api/segments/{seg_id}/licenses", {
            "terms": {ACT_PUBLIC_LISTEN: True, ACT_LIBRARY_LISTEN: True,
                      ACT_CITE: True, "download": False},
            "note": "捐赠协议", "created_by": "编目员 陈",
        })
        self.call("POST", "/api/releases", {
            "name": "首批上线", "segment_ids": [seg_id],
        })

        # 初版转写
        _, transcription = self.call("POST", f"/api/segments/{seg_id}/transcription", {
            "created_by": "编目员 陈",
            "lines": [{"start_ms": 0, "end_ms": 1500, "text": "四〇年我们在山里。"},
                      {"start_ms": 1500, "end_ms": 3000, "text": "日子很苦。"}],
        })
        first_line_id = transcription["lines"][0]["id"]

        # 公众目录可见，但真实姓名未解密
        _, catalog = self.call("GET", "/api/catalog/public")
        self.assertEqual([i["segment_id"] for i in catalog["items"]], [seg_id])
        self.assertIsNone(catalog["items"][0]["speakers"][0]["real_name"])

        # 建立引用（锁定 v1）
        _, citation = self.call("POST", f"/api/segments/{seg_id}/citations", {
            "start_ms": 0, "end_ms": 1500, "created_by": "研究者 钱",
        })
        citation_id, v1_id = citation["id"], citation["version_id"]

        # 听众纠错 → 馆员采纳 → 发布 v2
        _, correction = self.call("POST", f"/api/segments/{seg_id}/corrections", {
            "line_id": first_line_id, "proposed_text": "三九年冬我们在山里。",
            "reason": "家属提供的家书落款为民国二十八年冬",
            "submitted_by": "听众 周",
        })
        _, review = self.call("POST", f"/api/corrections/{correction['id']}/review", {
            "adopt": True, "reviewed_by": "编目员 陈", "review_note": "家书互证",
        })
        self.assertEqual(review["version"]["basis"], [f"correction:{correction['id']}"])

        # 旧引用仍解析到 v1 原文，并能看到当前版本
        _, resolved = self.call("GET", f"/api/citations/{citation_id}")
        self.assertTrue(resolved["resolved"])
        self.assertEqual(resolved["cited_version"]["id"], v1_id)
        self.assertEqual(resolved["cited_lines"][0]["text"], "四〇年我们在山里。")
        self.assertEqual(resolved["current_lines"][0]["text"], "三九年冬我们在山里。")
        self.assertFalse(resolved["version_is_current"])

        # 收紧前先记一条合法公开访问
        _, access_before = self.call("POST", f"/api/segments/{seg_id}/access", {
            "action": ACT_PUBLIC_LISTEN, "actor": "读者 刘",
            "actor_role": "公众读者",
        })
        self.assertTrue(access_before["granted"])
        license_before = access_before["license_version_id"]

        # 权利人补充限制：立即约束新访问（拒绝同样留痕，HTTP 仍为 201）
        _, license2 = self.call("POST", f"/api/segments/{seg_id}/licenses", {
            "terms": {ACT_PUBLIC_LISTEN: False, ACT_LIBRARY_LISTEN: True,
                      ACT_CITE: False, "download": False},
            "note": "权利人来函暂停公开与引用", "created_by": "编目员 陈",
        })
        _, access_after = self.call("POST", f"/api/segments/{seg_id}/access", {
            "action": ACT_PUBLIC_LISTEN, "actor": "读者 刘",
            "actor_role": "公众读者",
        })
        self.assertFalse(access_after["granted"])
        self.assertEqual(access_after["license_version_id"], license2["id"])

        # 此前合法利用记录原样保留并指向旧许可版本
        _, records = self.call("GET", f"/api/access-records?segment_id={seg_id}")
        prior = [r for r in records if r["id"] == access_before["id"]]
        self.assertEqual(prior[0]["license_version_id"], license_before)
        self.assertTrue(prior[0]["granted"])

        # 馆员从任一句可全链路溯源
        _, trace = self.call("GET", f"/api/lines/{first_line_id}/trace")
        self.assertTrue(trace["audio_provenance"]["verified"])
        self.assertEqual(trace["audio_provenance"]["carrier"]["label"], "磁带 B-02")
        self.assertEqual(len(trace["transcription_chain"]), 2)
        self.assertEqual(len(trace["licenses"]), 2)


if __name__ == "__main__":
    unittest.main()
