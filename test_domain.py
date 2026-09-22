"""领域层规则测试：谱系、许可、纠错、版本与稳定引用。"""

import tempfile
import unittest
from pathlib import Path

from domain import (
    ALL_PERMS,
    PERM_CITE,
    PERM_DOWNLOAD,
    PERM_ONSITE_LISTEN,
    PERM_PUBLIC_LISTEN,
    ROLE_LIBRARIAN,
    ROLE_PUBLIC,
    DomainError,
    OralHistoryService,
)


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.svc = OralHistoryService()
        carrier = self.svc.register_carrier("三号带", "录音磁带", "家属甲", "2026-01-01T00:00:00")
        self.cid = carrier["carrier_id"]
        batch = self.svc.digitize(self.cid, "李某", "2026-01-02T00:00:00",
                                   "m/3.wav", "audio/wav", "sum:m")
        self.master_id = batch["master"]["file_id"]
        clean = self.svc.process("降噪", self.master_id, "李某",
                                 [{"path": "d/3.wav", "checksum": "sum:c"}],
                                 params={"profile": "hiss"})
        self.clean_id = clean["outputs"][0]["file_id"]
        segs = self.svc.process("切分", self.clean_id, "王某",
                                 [{"path": "s/a.wav", "checksum": "sum:a"},
                                  {"path": "s/b.wav", "checksum": "sum:b"}])
        self.seg_a, self.seg_b = [f["file_id"] for f in segs["outputs"]]
        spk = self.svc.register_speaker("化名甲", real_name="张甲",
                                        reveal_after="2031-01-01", visibility="馆内可见")
        self.spk_id = spk["speaker_id"]
        self.clip_a = self.svc.create_clip(self.cid, "谈话甲", self.seg_a,
                                           "00:00:00.000", "00:02:00.000",
                                           [self.spk_id])["clip_id"]
        self.clip_b = self.svc.create_clip(self.cid, "自述乙", self.seg_b,
                                           "00:02:00.000", "00:04:00.000",
                                           [self.spk_id])["clip_id"]
        self.svc.add_agreement(
            "家属甲", [self.clip_a, self.clip_b],
            {PERM_PUBLIC_LISTEN: True, PERM_ONSITE_LISTEN: True,
             PERM_CITE: True, PERM_DOWNLOAD: False},
            signed_at="2026-01-10T00:00:00")
        self.lines_v1 = [
            {"start_tc": "00:00:00.000", "end_tc": "00:00:10.000",
             "text": "我们夜里渡江。", "speaker_id": self.spk_id},
            {"start_tc": "00:00:10.000", "end_tc": "00:00:22.500",
             "text": "炮火很密。", "speaker_id": self.spk_id},
        ]


class TestCarrierAndProvenance(ServiceFixture):
    def test_repeated_digitization_keeps_separate_masters(self):
        self.svc.digitize(self.cid, "李某", "2026-02-01T00:00:00",
                           "m/3-rescan.wav", "audio/wav", "sum:m2")
        batches = [b for b in self.svc.batches.values() if b.carrier_id == self.cid]
        masters = [f for f in self.svc.files.values() if f.is_master and f.carrier_id == self.cid]
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(masters), 2)
        self.assertNotEqual(masters[0].checksum, masters[1].checksum)

    def test_processing_never_overwrites_master(self):
        chain = self.svc.provenance_chain(self.seg_a)
        files = [node["file"] for node in chain if node["type"] == "file"]
        # 切分片段 -> 降噪件 -> 母版；母版在链尾且从未被改动
        self.assertEqual(files[0]["file_id"], self.seg_a)
        self.assertTrue(files[-1]["is_master"])
        self.assertEqual(len([node for node in chain if node["type"] == "event"]), 2)
        for f in files[:-1]:
            self.assertFalse(f["is_master"])
        # 母版本身没有来源事件
        self.assertIsNone(files[-1]["derived_from_event_id"])

    def test_format_migration_extends_chain(self):
        migrated = self.svc.process("格式迁移", self.clean_id, "李某",
                                    [{"path": "d/3.flac", "media_type": "audio/flac",
                                      "checksum": "sum:f"}])
        chain = self.svc.provenance_chain(migrated["outputs"][0]["file_id"])
        self.assertEqual([n["type"] for n in chain],
                         ["file", "event", "file", "event", "file"])

    def test_timecodes_validated(self):
        with self.assertRaises(DomainError):
            self.svc.create_clip(self.cid, "坏片段", self.seg_a,
                                 "00:02:00.000", "00:01:00.000")
        with self.assertRaises(ValueError):
            self.svc.create_clip(self.cid, "坏片段", self.seg_a,
                                 "00:99:00", "00:01:00.000")


class TestPermissionsAndRestrictions(ServiceFixture):
    def test_effective_permissions_reflect_agreement(self):
        granted = self.svc.effective_permissions(self.clip_a, "2026-01-11T00:00:00")["granted"]
        self.assertTrue(granted[PERM_PUBLIC_LISTEN])
        self.assertFalse(granted[PERM_DOWNLOAD])

    def test_agreement_does_not_apply_before_signing(self):
        granted = self.svc.effective_permissions(self.clip_a, "2026-01-09T00:00:00")["granted"]
        self.assertFalse(any(granted.values()))

    def test_access_decision_and_role_scope(self):
        ok = self.svc.request_access(self.clip_a, "读者1", ROLE_PUBLIC,
                                    PERM_PUBLIC_LISTEN, at="2026-01-11T00:00:00")
        self.assertTrue(ok["allowed"])
        denied_download = self.svc.request_access(self.clip_a, "读者1", ROLE_PUBLIC,
                                                PERM_DOWNLOAD, at="2026-01-11T00:00:00")
        self.assertFalse(denied_download["allowed"])
        # 公众不享馆内权限，即便协议有授
        denied_onsite = self.svc.request_access(self.clip_a, "读者1", ROLE_PUBLIC,
                                              PERM_ONSITE_LISTEN, at="2026-01-11T00:00:00")
        self.assertFalse(denied_onsite["allowed"])
        lib = self.svc.request_access(self.clip_a, "馆员王", ROLE_LIBRARIAN,
                                     PERM_ONSITE_LISTEN, at="2026-01-11T00:00:00")
        self.assertTrue(lib["allowed"])

    def test_restriction_is_immediate_but_keeps_prior_legal_usage(self):
        self.svc.request_access(self.clip_a, "读者1", ROLE_PUBLIC,
                               PERM_PUBLIC_LISTEN, at="2026-01-11T00:00:00",
                               detail="在线收听")
        self.svc.add_restriction(self.clip_a, [PERM_PUBLIC_LISTEN],
                                "权利人要求暂缓公开", "家属甲",
                                effective_at="2026-01-20T00:00:00")
        after = self.svc.request_access(self.clip_a, "读者2", ROLE_PUBLIC,
                                       PERM_PUBLIC_LISTEN, at="2026-01-21T00:00:00")
        self.assertFalse(after["allowed"])

        history = self.svc.usage_history(self.clip_a)
        self.assertEqual(len(history), 2)
        prior, later = history
        # 此前的合法利用记录仍在、依据协议保留
        self.assertEqual(prior["detail"], "在线收听")
        self.assertIsNotNone(prior["justification_agreement_id"])
        self.assertFalse(prior["still_permitted"])  # 现状已不允许，但记录未删
        self.assertIsNone(later["justification_agreement_id"])

    def test_restriction_lifted_restores_access(self):
        rst = self.svc.add_restriction(self.clip_a, [PERM_CITE], "复核中", "家属甲",
                                       effective_at="2026-01-20T00:00:00")
        self.assertFalse(
            self.svc.request_access(self.clip_a, "r", ROLE_PUBLIC, PERM_CITE,
                                  at="2026-01-21T00:00:00")["allowed"])
        self.svc.lift_restriction(rst["restriction_id"], lifted_at="2026-01-22T00:00:00")
        self.assertTrue(
            self.svc.request_access(self.clip_a, "r", ROLE_PUBLIC, PERM_CITE,
                                  at="2026-01-23T00:00:00")["allowed"])


class TestReleaseAndCatalog(ServiceFixture):
    def test_staged_release_and_catalog_visibility(self):
        self.svc.create_release_wave("第一批", "2026-02-01T00:00:00", [self.clip_a])
        catalog = self.svc.public_catalog(at="2026-02-02T00:00:00")
        self.assertEqual([c["clip_id"] for c in catalog], [self.clip_a])

        self.svc.create_release_wave("第二批", "2026-03-01T00:00:00", [self.clip_b])
        catalog = self.svc.public_catalog(at="2026-03-02T00:00:00")
        self.assertEqual({c["clip_id"] for c in catalog}, {self.clip_a, self.clip_b})

    def test_new_restriction_pulls_released_clip_from_public(self):
        self.svc.create_release_wave("第一批", "2026-02-01T00:00:00",
                                    [self.clip_a, self.clip_b])
        self.svc.add_restriction(self.clip_a, [PERM_PUBLIC_LISTEN],
                                "权利人临时收回", "家属甲",
                                effective_at="2026-02-10T00:00:00")
        catalog = self.svc.public_catalog(at="2026-02-11T00:00:00")
        self.assertEqual([c["clip_id"] for c in catalog], [self.clip_b])

    def test_unauthorized_clip_cannot_release(self):
        # clip_a 去掉公众收听授权
        self.svc.add_restriction(self.clip_a, [PERM_PUBLIC_LISTEN], "暂缓", "家属甲",
                                effective_at="2026-01-05T00:00:00")
        with self.assertRaises(DomainError):
            self.svc.create_release_wave("第一批", "2026-02-01T00:00:00", [self.clip_a])

    def test_wave_withdraw_hides_clip(self):
        wave = self.svc.create_release_wave("第一批", "2026-02-01T00:00:00", [self.clip_a])
        self.svc.withdraw_release(wave["wave_id"], withdrawn_at="2026-02-05T00:00:00")
        self.assertEqual(self.svc.public_catalog(at="2026-02-06T00:00:00"), [])


class TestSpeakerDeferredReveal(ServiceFixture):
    def test_name_hidden_before_date_and_after_for_librarian(self):
        speaker = self.svc.speakers[self.spk_id]
        self.assertIsNone(speaker.view(ROLE_PUBLIC, at="2030-12-31T00:00:00")["real_name"])
        self.assertEqual(speaker.view(ROLE_PUBLIC, at="2031-06-01T00:00:00")["real_name"], "张甲")
        # 馆员始终可见
        self.assertEqual(speaker.view(ROLE_LIBRARIAN, at="2026-01-01T00:00:00")["real_name"], "张甲")

    def test_anonymous_speaker_never_revealed(self):
        spk = self.svc.register_speaker("无名歌者", visibility="匿名")
        self.assertFalse(self.svc.speakers[spk["speaker_id"]].revealed("2999-01-01T00:00:00"))


class TestTranscriptCorrectionCitation(ServiceFixture):
    def _publish_v1(self):
        return self.svc.save_transcript(self.clip_a, self.lines_v1,
                                        created_by="王某", status="已发布", basis="初编")

    def test_published_version_is_immutable(self):
        self._publish_v1()
        # 不能以“首版”再写：修订必须成为新版本
        with self.assertRaises(DomainError):
            self.svc.save_transcript(self.clip_a, self.lines_v1, created_by="王某",
                                    status="待核查", supersedes=False)
        v2 = self.svc.save_transcript(self.clip_a, self.lines_v1, created_by="王某")
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["supersedes_version"], 1)

    def test_overlapping_lines_rejected(self):
        bad = [
            {"start_tc": "00:00:00.000", "end_tc": "00:00:12.000", "text": "甲"},
            {"start_tc": "00:00:10.000", "end_tc": "00:00:20.000", "text": "乙"},
        ]
        with self.assertRaises(DomainError):
            self.svc.save_transcript(self.clip_a, bad, created_by="王某")

    def test_correction_workflow_publishes_evidence_based_version(self):
        self._publish_v1()
        cor = self.svc.submit_correction(self.clip_a, "听众刘某",
                                         {"line_no": 2, "proposed_text": "炮火很密，船没有停。"})
        self.assertEqual(cor["status"], "待核查")
        # 采纳前没有新版
        self.assertEqual(
            self.svc.get_transcript(self.clip_a, role=ROLE_LIBRARIAN)["version_no"], 1)

        rejected = self.svc.submit_correction(self.clip_a, "听众某", {"line_no": 1, "proposed_text": "x"})
        self.svc.review_correction(rejected["correction_id"], "编目员王某", False,
                                  resolution_note="与母版录音不符")
        self.assertEqual(self.svc.corrections[rejected["correction_id"]].status, "已驳回")

        new_lines = [
            dict(self.lines_v1[0]),
            {"start_tc": "00:00:10.000", "end_tc": "00:00:22.500",
             "text": "炮火很密，船没有停。", "speaker_id": self.spk_id},
        ]
        reviewed = self.svc.review_correction(
            cor["correction_id"], "编目员王某", True,
            resolution_note="比对降噪版声纹后采纳", new_lines=new_lines)
        self.assertEqual(reviewed["status"], "已采纳")
        self.assertEqual(reviewed["resulting_version_no"], 2)
        current = self.svc.get_transcript(self.clip_a, role=ROLE_LIBRARIAN)
        self.assertEqual(current["version_no"], 2)
        self.assertIn("cor_", current["basis"])
        # v1 转为已撤回但仍可按版本号取读（馆员）
        v1 = self.svc.get_transcript(self.clip_a, version_no=1, role=ROLE_LIBRARIAN)
        self.assertEqual(v1["status"], "已撤回")
        # 公众不能读取已撤回的历史版本
        with self.assertRaises(DomainError):
            self.svc.get_transcript(self.clip_a, version_no=1, role=ROLE_PUBLIC)

    def test_old_timecode_citation_keeps_resolving(self):
        self._publish_v1()
        citation = self.svc.mint_citation(self.clip_a, 1, 1, "学者陈某")
        token = citation["token"]
        self.assertTrue(token.startswith(f"cite:{self.clip_a.replace('clip_', '')}-v1-l1"))

        # 发布修订版（第 2 行文本变化，第 1 行与时间码不变）
        new_lines = [
            dict(self.lines_v1[0]),
            {"start_tc": "00:00:10.000", "end_tc": "00:00:22.500",
             "text": "炮火很密，船没有停。", "speaker_id": self.spk_id},
        ]
        self.svc.save_transcript(self.clip_a, new_lines, created_by="王某",
                                status="已发布", basis="听众纠错经核查")
        resolved = self.svc.resolve_citation(token)
        self.assertEqual(resolved["frozen"]["version_no"], 1)
        self.assertEqual(resolved["current_version_no"], 2)
        self.assertTrue(resolved["still_current"])
        self.assertTrue(resolved["timecode_mapping"]["same_audio_position"])

        # 行被合并进更长的新行：旧引用仍解析到覆盖该声音位置的当前行
        cite_line2 = self.svc.mint_citation(self.clip_a, 2, 2, "学者陈某")["token"]
        merged = [
            {"start_tc": "00:00:00.000", "end_tc": "00:00:22.500",
             "text": "我们夜里渡江；炮火很密，船没有停。", "speaker_id": self.spk_id},
        ]
        self.svc.save_transcript(self.clip_a, merged, created_by="王某",
                                status="已发布", basis="合并相邻行")
        r2 = self.svc.resolve_citation(cite_line2)
        self.assertEqual(r2["status"], "line_merged_or_removed")
        self.assertIsNotNone(r2["current_line"])
        self.assertIn("渡江", r2["current_line"]["text"])

    def test_citation_to_nonexistent_line_rejected(self):
        self._publish_v1()
        with self.assertRaises(DomainError):
            self.svc.mint_citation(self.clip_a, 1, 99, "学者陈某")
        with self.assertRaises(DomainError):
            self.svc.resolve_citation("not-a-citation")


class TestLibrarianTraceAndPersistence(ServiceFixture):
    def test_trace_clip_covers_all_layers(self):
        self.svc.save_transcript(self.clip_a, self.lines_v1, created_by="王某",
                                status="已发布")
        self.svc.add_related_document("捐赠协议", "协议文本", "DOC-1", [self.clip_a])
        self.svc.add_research_decision(self.clip_a, "年代", "1953 年", "磁带型号",
                                       "赵某", status="存疑")
        self.svc.create_release_wave("第一批", "2026-02-01T00:00:00", [self.clip_a])
        self.svc.request_access(self.clip_a, "读者1", ROLE_PUBLIC, PERM_PUBLIC_LISTEN,
                               at="2026-02-02T00:00:00")
        trace = self.svc.trace_clip(self.clip_a)
        for key in ("clip", "carrier", "source_provenance", "batches", "speakers",
                     "agreements", "restrictions", "effective_permissions",
                     "transcript_versions", "corrections", "research_decisions",
                     "related_documents", "releases", "usage_records", "citations"):
            self.assertIn(key, trace)
        # 从转写句一路回到原始声段
        files = [n["file"] for n in trace["source_provenance"] if n["type"] == "file"]
        self.assertTrue(files[-1]["is_master"])
        self.assertEqual(trace["research_decisions"][0]["conclusion"], "1953 年")

    def test_save_load_roundtrip(self):
        self.svc.save_transcript(self.clip_a, self.lines_v1, created_by="王某",
                                status="已发布")
        self.svc.create_release_wave("第一批", "2026-02-01T00:00:00", [self.clip_a])
        self.svc.mint_citation(self.clip_a, 1, 1, "陈某")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.svc.save(path)
            restored = OralHistoryService.load(path)
            self.assertEqual(set(restored.clips), set(self.svc.clips))
            self.assertEqual(len(restored.files), len(self.svc.files))
            self.assertEqual(len(restored.events), len(self.svc.events))
            v = restored.get_transcript(self.clip_a)
            self.assertEqual(v["lines"][0]["text"], "我们夜里渡江。")
            token = next(iter(restored.citations.values())).token
            self.assertEqual(restored.resolve_citation(token)["status"], "ok")
            self.assertEqual(
                [c["clip_id"] for c in restored.public_catalog(at="2026-02-02T00:00:00")],
                [self.clip_a])


if __name__ == "__main__":
    unittest.main()
