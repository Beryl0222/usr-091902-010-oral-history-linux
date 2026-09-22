"""端到端 HTTP 接口测试：公众/馆员双视角与完整业务闭环。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from domain import (
    PERM_CITE,
    PERM_DOWNLOAD,
    PERM_PUBLIC_LISTEN,
    ROLE_LIBRARIAN,
    build_demo_service,
)
from service import make_handler


class ApiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = build_demo_service()
        handler = make_handler(cls.service)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method: str, path: str, body=None, role: str = None,
                expect: int = None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if role:
            headers["X-Role"] = role
        req = Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        try:
            with urlopen(req, timeout=3) as response:
                payload = json.load(response)
                status = response.status
        except HTTPError as exc:
            payload = json.load(exc)
            status = exc.code
        if expect is not None:
            self.assertEqual(status, expect, f"{method} {path} -> {status}: {payload}")
        return status, payload

    def get(self, path, role=None, expect=None):
        return self.request("GET", path, role=role, expect=expect)

    def post(self, path, body=None, role=None, expect=None):
        return self.request("POST", path, body if body is not None else {}, role=role,
                            expect=expect)

    def setUp(self):
        # 每个用例从独立演示数据开始，避免相互污染
        self.__class__.service = build_demo_service()
        # handler 通过类属性 svc 引用服务；直接替换即可
        self.server.RequestHandlerClass.svc = self.__class__.service
        _, catalog = self.get("/catalog")
        titles = {c["title"]: c["clip_id"] for c in catalog["clips"]}
        self.clip_a = titles["渡江战斗回忆谈话"]
        self.clip_b = titles["个人自述片段"]
        # 歌曲片段未上线：馆员列表里取
        _, lib = self.get("/librarian/clips", role=ROLE_LIBRARIAN)
        self.clip_c = next(c["clip_id"] for c in lib["clips"]
                            if c["title"] == "战壕歌曲录音")

    # -- 公众目录与片段 ----------------------------------------------------

    def test_public_catalog_only_lists_released_listenable(self):
        _, catalog = self.get("/catalog")
        self.assertEqual({c["title"] for c in catalog["clips"]},
                         {"渡江战斗回忆谈话", "个人自述片段"})
        # 公众视图不含内部许可字段
        first = catalog["clips"][0]
        self.assertNotIn("agreement_ids", first)
        self.assertNotIn("releases", first)

    def test_public_cannot_reach_unreleased_clip_but_librarian_can(self):
        self.get(f"/clips/{self.clip_c}", expect=404)
        _, view = self.get(f"/clips/{self.clip_c}", role=ROLE_LIBRARIAN, expect=200)
        self.assertEqual(view["clip_id"], self.clip_c)

    def test_deferred_name_hidden_from_public(self):
        _, view = self.get(f"/clips/{self.clip_a}")
        speaker = view["speakers"][0]
        self.assertFalse(speaker["name_revealed"])
        self.assertIsNone(speaker["real_name"])
        _, lib = self.get(f"/clips/{self.clip_a}", role=ROLE_LIBRARIAN)
        self.assertEqual(lib["speakers"][0]["real_name"], "张某某")

    # -- 访问判定与留痕 ----------------------------------------------------

    def test_access_decisions_over_http(self):
        status, allowed = self.post(
            f"/clips/{self.clip_a}/access",
            {"actor": "读者刘某", "perm": PERM_PUBLIC_LISTEN}, expect=200)
        self.assertTrue(allowed["allowed"])

        _, denied = self.post(
            f"/clips/{self.clip_a}/access",
            {"actor": "读者刘某", "perm": PERM_DOWNLOAD})
        self.assertFalse(denied["allowed"])
        self.assertIsNone(denied["usage"]["justification_agreement_id"])

    def test_admin_routes_reject_public_role(self):
        status, payload = self.post("/carriers", {
            "title": "x", "carrier_type": "录音磁带", "donor": "d",
            "received_at": "2026-01-01T00:00:00"})
        self.assertEqual(status, 400)
        self.assertIn("馆员", payload["error"])
        self.get("/librarian/clips", expect=400)

    # -- 限制即时生效，旧利用保留 -------------------------------------------

    def test_new_restriction_immediately_constrains_public(self):
        self.post(f"/clips/{self.clip_a}/restrictions", {
            "denied_perms": [PERM_PUBLIC_LISTEN],
            "reason": "权利人补充限制", "issued_by": "馆员值班台",
        }, role=ROLE_LIBRARIAN, expect=201)

        _, catalog = self.get("/catalog")
        self.assertNotIn(self.clip_a, {c["clip_id"] for c in catalog["clips"]})
        _, access = self.post(f"/clips/{self.clip_a}/access",
                              {"actor": "读者", "perm": PERM_PUBLIC_LISTEN})
        self.assertFalse(access["allowed"])

        _, usage = self.get(f"/clips/{self.clip_a}/usage", role=ROLE_LIBRARIAN)
        self.assertGreaterEqual(len(usage["usage"]), 1)

    # -- 纠错核查 → 新版转写 → 旧引用继续解析 -------------------------------

    def test_correction_to_versioned_transcript_and_stable_citation(self):
        # 馆员先就 v1 铸造引用（模拟既有学术引用）
        _, cite = self.post(f"/clips/{self.clip_a}/citations",
                             {"version_no": 1, "line_no": 1, "created_by": "学者陈某"},
                             role=ROLE_LIBRARIAN, expect=201)
        token = cite["token"]

        # 听众提交纠错：先进入核查，不改变当前发布版
        _, correction = self.post(f"/clips/{self.clip_a}/corrections", {
            "submitter": "听众赵某",
            "payload": {"line_no": 2, "proposed_text": "炮火很密，但船没有停。"},
        }, expect=201)
        _, transcript = self.get(f"/transcripts/{self.clip_a}")
        self.assertEqual(transcript["version_no"], 1)

        _, pending = self.get(f"/corrections?status={quote('待核查')}", role=ROLE_LIBRARIAN)
        self.assertTrue(any(c["correction_id"] == correction["correction_id"]
                            for c in pending["corrections"]))

        # 馆员核查采纳，发布有依据的 v2
        new_lines = [
            {"start_tc": "00:00:00.000", "end_tc": "00:00:10.000",
             "text": "那天夜里我们渡过了江。", "speaker_id": transcript["lines"][0]["speaker_id"]},
            {"start_tc": "00:00:10.000", "end_tc": "00:00:22.500",
             "text": "炮火很密，但船没有停。", "speaker_id": transcript["lines"][0]["speaker_id"]},
        ]
        self.post(f"/corrections/{correction['correction_id']}/review", {
            "reviewer": "编目员王某", "approve": True,
            "resolution_note": "比对降噪版声纹，第2行据听众意见修订",
            "new_lines": new_lines,
        }, role=ROLE_LIBRARIAN, expect=200)

        _, v2 = self.get(f"/transcripts/{self.clip_a}")
        self.assertEqual(v2["version_no"], 2)
        self.assertIn("炮火", v2["lines"][1]["text"])

        # 旧版不再对公众开放，但旧引用仍解析到当前版本
        self.get(f"/transcripts/{self.clip_a}?version=1", expect=400)
        _, resolved = self.get(f"/citations/resolve?token={token}")
        self.assertEqual(resolved["frozen"]["version_no"], 1)
        self.assertEqual(resolved["current_version_no"], 2)
        self.assertTrue(resolved["timecode_mapping"]["same_audio_position"])

    # -- 馆员统一溯源 ------------------------------------------------------

    def test_librarian_trace_and_provenance(self):
        _, trace = self.get(f"/librarian/clips/{self.clip_a}/trace",
                             role=ROLE_LIBRARIAN, expect=200)
        files = [n["file"] for n in trace["source_provenance"] if n["type"] == "file"]
        self.assertTrue(files[-1]["is_master"], "溯源链必须最终回到母版")
        self.assertGreaterEqual(len(trace["research_decisions"]), 1)
        self.assertGreaterEqual(len(trace["agreements"]), 1)

        file_id = trace["clip"]["source_file_id"]
        _, chain = self.get(f"/files/{file_id}/provenance", role=ROLE_LIBRARIAN)
        self.assertGreaterEqual(len(chain["chain"]), 3)

    def test_citation_permission_independent_of_listen(self):
        # clip_b 有 cite 权而无下载权：引用允许、下载拒绝
        _, cite_ok = self.post(f"/clips/{self.clip_b}/access",
                                {"actor": "读者", "perm": PERM_CITE})
        self.assertTrue(cite_ok["allowed"])


if __name__ == "__main__":
    unittest.main()
