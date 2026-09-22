"""领域规则测试：覆盖需求中的全部关键不变量。

以一条完整叙事串联：磁带由将领后代捐赠 → 两次数字化 → 降噪/迁移 →
切分 → 说话人延迟解密 → 片段许可 → 分次发布 → 转写 → 听众纠错 →
旧引用仍解析 → 权利人收紧 → 利用记录可追溯 → 馆员全链路溯源。
"""

import unittest

from oralhistory import DomainError, Engine
from oralhistory.model import (
    ACT_CITE,
    ACT_DOWNLOAD,
    ACT_INTERNAL_READ,
    ACT_LIBRARY_LISTEN,
    ACT_PUBLIC_LISTEN,
    CORRECTION_ADOPTED,
    CORRECTION_PENDING,
    CORRECTION_REJECTED,
    KIND_DERIVATIVE,
    KIND_MASTER,
    OP_DENOISE,
    OP_MIGRATE,
    ROLE_CATALOGER,
    ROLE_DIGITIZER,
    ROLE_PUBLIC,
    ROLE_READER,
    STATUS_PUBLIC,
    STATUS_RESTRICTED,
    STATUS_SEALED,
)
from oralhistory.store import Store


# 固定时刻，保证“生效中版本”与解密日期的判断可重复
T0 = "2026-01-01T09:00:00+00:00"
T1 = "2026-02-01T09:00:00+00:00"
T2 = "2026-05-01T09:00:00+00:00"
T3 = "2026-08-01T09:00:00+00:00"


def _pad(prefix: bytes, size: int = 9000) -> bytes:
    """用可辨识前缀补足模拟音频长度（1 字节 = 1 毫秒）。"""
    return prefix + b"\0" * (size - len(prefix))


class _Scenario(unittest.TestCase):
    """构造跨测试共用的首批录音场景。"""

    def setUp(self):
        self.engine = Engine(Store())
        e = self.engine

        # 载体：老战士捐赠的开盘磁带
        self.carrier = e.register_carrier(
            "磁带 A-17", "开盘磁带", "将领后代 张某", "2025-11-10",
            note="含谈话、自述与歌曲",
        )

        # 同一载体两次数字化（重复数字化），各自产生母版
        self.dig1 = e.digitize(
            self.carrier.id, "数字化馆员 王", "Studer A80",
            _pad(b"first-digitization-master-pcm!"), started_at=T0,
        )
        self.dig2 = e.digitize(
            self.carrier.id, "数字化馆员 王", "Studer A80（复检）",
            _pad(b"second-digitization-master-pcm-0000"), started_at=T1,
        )
        self.master1, self.master2 = self.dig1["master"], self.dig2["master"]

        # 对第二个母版做降噪，再做格式迁移（链条：迁移件→降噪件→母版2）
        self.denoised = e.derive_file(
            self.master2.id, OP_DENOISE, _pad(b"denoised-content-00000000000000"),
            "数字化馆员 王", params={"profile": "voice-24k"},
        )
        self.migrated = e.derive_file(
            self.denoised.id, OP_MIGRATE, _pad(b"flac-migrated-content-000000000"),
            "数字化馆员 王", params={"target_format": "flac"},
        )

        # 说话人：真实姓名延迟到 2026-06-01 解密
        self.speaker = e.add_speaker(
            "未公开说话人", real_name="李怀山", declassify_at="2026-06-01",
        )

        # 片段 A：谈话（可公开收听，可引用，禁下载）；片段 B：歌曲（先不发布）
        self.seg_a = e.cut_segment(
            self.migrated.id, 0, 4000, "关于武汉会战的谈话",
            speaker_ids=[self.speaker.id],
        )
        self.seg_b = e.cut_segment(
            self.migrated.id, 4000, 8000, "战地歌曲《xxxx》",
        )

        self.lic_a = e.add_license(
            self.seg_a.id,
            {ACT_PUBLIC_LISTEN: True, ACT_LIBRARY_LISTEN: True,
             ACT_CITE: True, ACT_DOWNLOAD: False},
            "捐赠协议第 3 条：谈话部分公开，不供下载",
            "编目员 陈", effective_at=T0,
        )
        self.lic_b = e.add_license(
            self.seg_b.id,
            {ACT_PUBLIC_LISTEN: False, ACT_LIBRARY_LISTEN: True,
             ACT_CITE: True, ACT_DOWNLOAD: False},
            "捐赠协议第 4 条：歌曲仅限馆内",
            "编目员 陈", effective_at=T0,
        )

        # 首批只上线片段 A
        self.release1 = e.release("首批上线", [self.seg_a.id], released_at=T1)

        # 初版转写
        result = e.initial_transcription(
            self.seg_a.id,
            [
                {"start_ms": 0, "end_ms": 2000, "text": "民国二十七年我们在武汉。",
                 "speaker_id": self.speaker.id},
                {"start_ms": 2000, "end_ms": 4000,
                 "text": "那时候守在江边，炮声整夜不停。"},
            ],
            "编目员 陈",
        )
        self.version1 = result["version"]
        self.line1, self.line2 = result["lines"]

        # 关联文献
        self.document = e.add_document(
            "武汉会战纪略", "图书", "K265.2/41", segment_ids=[self.seg_a.id],
        )


class LayeredProvenanceTest(_Scenario):
    def test_repeated_digitization_yields_distinct_masters(self):
        self.assertNotEqual(self.dig1["batch"].id, self.dig2["batch"].id)
        self.assertNotEqual(self.master1.id, self.master2.id)
        self.assertEqual(self.master1.kind, KIND_MASTER)
        self.assertEqual(self.master2.kind, KIND_MASTER)
        # 两个母版都回溯到同一载体
        for master in (self.master1, self.master2):
            report = self.engine.verify_provenance(master.id)
            self.assertTrue(report["verified"])
            self.assertEqual(report["chain"][-1]["kind"], KIND_MASTER)

    def test_derivative_chain_records_every_operation_and_checksums(self):
        report = self.engine.verify_provenance(self.migrated.id)
        self.assertTrue(report["verified"])
        chain = report["chain"]
        # chain[0]=迁移件, chain[1]=降噪件, chain[2]=母版2
        self.assertEqual([step["operation"] for step in chain[:2]],
                         [OP_MIGRATE, OP_DENOISE])
        self.assertEqual(chain[0]["source_file_id"], self.denoised.id)
        self.assertTrue(chain[0]["source_checksum_ok"])
        self.assertEqual(chain[1]["source_file_id"], self.master2.id)
        self.assertTrue(chain[1]["source_checksum_ok"])
        self.assertEqual(chain[2]["batch_id"], self.dig2["batch"].id)

    def test_provenance_detects_tampering_without_overwriting_master(self):
        # 模拟来源文件被改动：校验和与衍生品登记时记录的不再一致
        self.master2.content = b"tampered"
        from hashlib import sha256
        self.master2.checksum = sha256(b"tampered").hexdigest()
        report = self.engine.verify_provenance(self.migrated.id)
        self.assertFalse(report["verified"])
        self.assertFalse(report["chain"][1]["source_checksum_ok"])
        # 降噪件登记的来源校验和仍是原始母版指纹，即母版原貌有据可查
        original = self.dig1["master"]  # 另一个母版完全未受影响
        self.assertTrue(self.engine.verify_provenance(original.id)["verified"])

    def test_unknown_derivative_operation_rejected(self):
        with self.assertRaises(DomainError):
            self.engine.derive_file(
                self.master2.id, "splice", b"x", "数字化馆员 王",
            )

    def test_cut_range_must_fit_file(self):
        with self.assertRaises(DomainError):
            self.engine.cut_segment(self.migrated.id, 5000, 99999, "越界片段")


class LicenseAndAccessTest(_Scenario):
    def test_public_can_listen_only_released_licensed_segment(self):
        # 片段 A 已发布且授权公开：准许
        ok = self.engine.request_access(
            self.seg_a.id, ACT_PUBLIC_LISTEN, "读者 刘某", ROLE_PUBLIC, at=T2,
        )
        self.assertTrue(ok.granted)
        self.assertEqual(ok.license_version_id, self.lic_a.id)

        # 片段 B 未发布且未授权公开：拒绝并留痕
        denied = self.engine.request_access(
            self.seg_b.id, ACT_PUBLIC_LISTEN, "读者 刘某", ROLE_PUBLIC, at=T2,
        )
        self.assertFalse(denied.granted)

        # 馆内读者可在馆内收听 B
        library = self.engine.request_access(
            self.seg_b.id, ACT_LIBRARY_LISTEN, "读者 刘某", ROLE_READER, at=T2,
        )
        self.assertTrue(library.granted)

        # A 明确禁止下载
        no_download = self.engine.request_access(
            self.seg_a.id, ACT_DOWNLOAD, "读者 刘某", ROLE_READER, at=T2,
        )
        self.assertFalse(no_download.granted)

        # 公众角色不到馆，不能使用馆内收听
        no_remote_library = self.engine.request_access(
            self.seg_b.id, ACT_LIBRARY_LISTEN, "读者 刘某", ROLE_PUBLIC, at=T2,
        )
        self.assertFalse(no_remote_library.granted)

    def test_added_restriction_takes_effect_immediately_but_keeps_prior_log(self):
        # T2：权利人补充限制，公开收听与引用全部收回（新版本立即生效）
        before = self.engine.request_access(
            self.seg_a.id, ACT_PUBLIC_LISTEN, "读者 刘某", ROLE_PUBLIC, at=T2,
        )
        cite_before = self.engine.request_access(
            self.seg_a.id, ACT_CITE, "研究者 赵某", ROLE_READER, at=T2,
        )
        self.assertTrue(before.granted)
        self.assertTrue(cite_before.granted)

        lic_a2 = self.engine.add_license(
            self.seg_a.id,
            {ACT_PUBLIC_LISTEN: False, ACT_LIBRARY_LISTEN: True,
             ACT_CITE: False, ACT_DOWNLOAD: False},
            "权利人 2026-07 来函：谈话暂停公开与引用",
            "编目员 陈", effective_at=T3,
        )

        after = self.engine.evaluate_access(
            self.seg_a.id, ACT_PUBLIC_LISTEN, ROLE_PUBLIC, at=T3,
        )
        self.assertFalse(after["granted"])
        self.assertEqual(after["license_version_id"], lic_a2.id)
        # 片段状态随收紧回到限制开放
        self.assertEqual(self.seg_a.status, STATUS_RESTRICTED)

        # 权利人后续追加授权同样以新版本登记，不覆盖限制版本与历史记录
        lic_a3 = self.engine.add_license(
            self.seg_a.id,
            {ACT_PUBLIC_LISTEN: True, ACT_LIBRARY_LISTEN: True,
             ACT_CITE: False, ACT_DOWNLOAD: False},
            "权利人 2026-08 来函：恢复公开收听，仍不开放引用",
            "编目员 陈", effective_at=T3,
        )
        reopened = self.engine.evaluate_access(
            self.seg_a.id, ACT_PUBLIC_LISTEN, ROLE_PUBLIC, at=T3,
        )
        self.assertTrue(reopened["granted"])
        self.assertEqual(reopened["license_version_id"], lic_a3.id)
        still_no_cite = self.engine.evaluate_access(
            self.seg_a.id, ACT_CITE, ROLE_READER, at=T3,
        )
        self.assertFalse(still_no_cite["granted"])

        # 收紧前的利用记录原样保留，且仍指向当时合法的许可版本
        records = self.engine.list_access_records(self.seg_a.id)
        prior = [r for r in records if r.at <= T2]
        self.assertTrue(prior)
        self.assertTrue(all(r.license_version_id == self.lic_a.id for r in prior))
        self.assertTrue(all(r.granted for r in prior))
        # 许可版本史三版俱在，限制版本与恢复版本都不被覆盖
        self.assertEqual(
            [lic.number for lic in self.engine.list_licenses(self.seg_a.id)],
            [1, 2, 3],
        )
        # 恢复公开后片段状态随之更新
        self.assertEqual(self.seg_a.status, STATUS_PUBLIC)

    def test_staff_internal_read_always_logged(self):
        record = self.engine.request_access(
            self.seg_b.id, ACT_INTERNAL_READ, "编目员 陈", ROLE_CATALOGER, at=T2,
        )
        self.assertTrue(record.granted)
        self.assertEqual(record.actor_role, ROLE_CATALOGER)

    def test_sealed_segment_denied_to_public(self):
        self.engine.seal_segment(self.seg_b.id)
        decision = self.engine.evaluate_access(
            self.seg_b.id, ACT_LIBRARY_LISTEN, ROLE_READER, at=T2,
        )
        self.assertFalse(decision["granted"])
        self.assertEqual(decision["reason"], "片段已封存")
        # 封存不影响馆员溯源
        self.assertTrue(self.engine.evaluate_access(
            self.seg_b.id, ACT_INTERNAL_READ, ROLE_DIGITIZER, at=T2,
        )["granted"])
        # 封存片段不出现在公众目录
        self.assertEqual(
            [i["segment_id"] for i in self.engine.public_catalog(at=T2)["items"]],
            [self.seg_a.id],
        )


class ReleaseAndDeclassificationTest(_Scenario):
    def test_unreleased_segment_absent_from_public_catalog(self):
        ids = [i["segment_id"]
               for i in self.engine.public_catalog(at=T1)["items"]]
        self.assertEqual(ids, [self.seg_a.id])

    def test_second_release_makes_more_public(self):
        # 片段 B 获公开授权后随第二批上线
        self.engine.add_license(
            self.seg_b.id,
            {ACT_PUBLIC_LISTEN: True, ACT_LIBRARY_LISTEN: True,
             ACT_CITE: True, ACT_DOWNLOAD: False},
            "权利人同意歌曲随第二批公开", "编目员 陈", effective_at=T2,
        )
        self.engine.release("第二批上线", [self.seg_b.id], released_at=T2)
        ids = sorted(i["segment_id"]
                     for i in self.engine.public_catalog(at=T2)["items"])
        self.assertEqual(ids, sorted([self.seg_a.id, self.seg_b.id]))

    def test_delayed_name_declassification(self):
        # 解密日前：公众看不到真实姓名
        catalog_before = self.engine.public_catalog(at=T2)
        speaker_view = catalog_before["items"][0]["speakers"][0]
        self.assertIsNone(speaker_view["real_name"])
        self.assertFalse(speaker_view["name_public"])

        # 解密日后：真实姓名出现
        catalog_after = self.engine.public_catalog(at=T3)
        speaker_view = catalog_after["items"][0]["speakers"][0]
        self.assertEqual(speaker_view["real_name"], "李怀山")
        self.assertTrue(speaker_view["name_public"])

        # 馆员始终可见
        self.assertEqual(self.speaker.real_name, "李怀山")


class CorrectionAndCitationTest(_Scenario):
    def test_correction_flow_publishes_grounded_new_version(self):
        # 听众对第 1 行提交纠错，先进入待核查
        correction = self.engine.submit_correction(
            self.seg_a.id, "民国二十七年夏我们在武汉外围。",
            "据捐赠者提供的日记，谈话发生在夏季且地点为武汉外围",
            "听众 周", line_id=self.line1.id,
        )
        self.assertEqual(correction.status, CORRECTION_PENDING)

        # 待核查纠错不改变当前转写
        current = self.engine._current_version(self.seg_a.id)
        self.assertEqual(current.id, self.version1.id)

        # 馆员核查驳回另一则无依据纠错
        weak = self.engine.submit_correction(
            self.seg_a.id, "炮声三天不停", "我觉得是三天",
            "听众 某", line_id=self.line2.id,
        )
        rejected = self.engine.review_correction(
            weak.id, False, "编目员 陈", review_note="无佐证材料，维持原文",
        )
        self.assertFalse(rejected["adopted"])
        self.assertEqual(weak.status, CORRECTION_REJECTED)

        # 采纳第一则：发布有依据的新版本
        outcome = self.engine.review_correction(
            correction.id, True, "编目员 陈",
            review_note="与捐赠日记原件互证，予以采纳",
        )
        self.assertTrue(outcome["adopted"])
        version2 = outcome["version"]
        self.assertEqual(version2.number, 2)
        self.assertEqual(version2.basis, [f"correction:{correction.id}"])
        self.assertEqual(version2.supersedes, self.version1.id)
        self.assertEqual(correction.status, CORRECTION_ADOPTED)
        self.assertEqual(correction.resulting_version_id, version2.id)

        new_lines = self.engine._version_lines(version2.id)
        self.assertEqual(new_lines[0].text, "民国二十七年夏我们在武汉外围。")
        # 未被纠正的行原样保留
        self.assertEqual(new_lines[1].text, "那时候守在江边，炮声整夜不停。")

        # 不能重复核查
        with self.assertRaises(DomainError):
            self.engine.review_correction(correction.id, True, "编目员 陈")

    def test_old_timecode_citation_still_resolves_after_revision(self):
        # 修订前，研究者依据 v1 建立引用
        citation = self.engine.create_citation(
            self.seg_a.id, 0, 2000, "研究者 赵某",
        )
        self.assertEqual(citation.version_id, self.version1.id)

        correction = self.engine.submit_correction(
            self.seg_a.id, "民国二十七年夏我们在武汉外围。",
            "日记互证", "听众 周", line_id=self.line1.id,
        )
        self.engine.review_correction(correction.id, True, "编目员 陈")

        resolved = self.engine.resolve_citation(citation.id)
        self.assertTrue(resolved["resolved"])
        # 旧引用仍解析到旧版本与旧文字
        self.assertEqual(resolved["cited_version"]["id"], self.version1.id)
        self.assertEqual(
            resolved["cited_lines"][0]["text"], "民国二十七年我们在武汉。"
        )
        # 同时给出当前版本中同一时间码区间的对应行
        self.assertFalse(resolved["version_is_current"])
        self.assertEqual(
            resolved["current_lines"][0]["text"],
            "民国二十七年夏我们在武汉外围。",
        )

    def test_timecode_only_correction_finds_line_by_overlap(self):
        correction = self.engine.submit_correction(
            self.seg_a.id, "那一年我们驻守江边。",
            "口述者家属来信更正措辞", "听众 钱", start_ms=2000, end_ms=4000,
        )
        outcome = self.engine.review_correction(correction.id, True, "编目员 陈")
        new_lines = self.engine._version_lines(outcome["version"].id)
        self.assertEqual(new_lines[1].text, "那一年我们驻守江边。")
        self.assertEqual(new_lines[0].text, "民国二十七年我们在武汉。")


class ResearchDatingTest(_Scenario):
    def test_dating_revisions_keep_history_and_ground_versions(self):
        d1 = self.engine.add_research_decision(
            self.seg_a.id, "年代考证", "谈话所述为 1938 年 6—10 月",
            "对照《武汉会战纪略》与口述地名", "编目员 陈",
        )
        rev1 = self.engine.revise_dating(
            self.seg_a.id, "1938—1938", "初考", "编目员 陈",
            decision_id=d1.id,
        )
        self.assertEqual(
            self.engine.current_dating(self.seg_a.id).date_range, "1938—1938"
        )

        # 依据考证决定重订转写，版本依据可查
        outcome = self.engine.revise_transcription_by_decision(
            self.seg_a.id,
            [
                {"start_ms": 0, "end_ms": 2000,
                 "text": "一九三八年夏我们在武汉外围。",
                 "speaker_id": self.speaker.id},
                {"start_ms": 2000, "end_ms": 4000,
                 "text": "那时候守在江边，炮声整夜不停。"},
            ],
            d1.id, "编目员 陈",
        )
        self.assertEqual(outcome["version"].basis, [f"decision:{d1.id}"])
        self.assertEqual(outcome["version"].number, 2)

        # 年代再次修订，历史保留
        d2 = self.engine.add_research_decision(
            self.seg_a.id, "年代考证", "据录音中提到的粤汉线战事，修正为 1938 年 7 月",
            "补充查阅战报", "编目员 陈",
        )
        self.engine.revise_dating(
            self.seg_a.id, "1938-07", "据战报细化", "编目员 陈",
            decision_id=d2.id,
        )
        datings = [d for d in self.engine.store.datings.values()
                   if d.segment_id == self.seg_a.id]
        self.assertEqual([d.date_range for d in datings], ["1938—1938", "1938-07"])
        self.assertEqual(
            self.engine.current_dating(self.seg_a.id).id,
            self.engine.trace_line(self.line1.id)["dating_revisions"][-1]["id"],
        )


class TraceabilityTest(_Scenario):
    def test_trace_from_any_line_covers_full_chain(self):
        trace = self.engine.trace_line(self.line2.id)
        # 声段 → 处理历史 → 批次 → 载体
        self.assertEqual(trace["segment"]["id"], self.seg_a.id)
        prov = trace["audio_provenance"]
        self.assertTrue(prov["verified"])
        self.assertEqual(prov["segment_file_id"], self.migrated.id)
        self.assertEqual(prov["carrier"]["label"], "磁带 A-17")
        self.assertEqual(prov["digitization_batch"]["operator"], "数字化馆员 王")
        # 许可版本、考证、发布、文献全部可从一句追到
        self.assertEqual([lic["id"] for lic in trace["licenses"]], [self.lic_a.id])
        self.assertEqual(trace["releases"][0]["name"], "首批上线")
        self.assertEqual(trace["documents"][0]["title"], "武汉会战纪略")

        # 采纳一则纠错后，溯源链中能看到两个转写版本与纠错去向
        correction = self.engine.submit_correction(
            self.seg_a.id, "那时候守在江边，炮声两夜不停。",
            "口述者本人来信", "听众 孙", line_id=self.line2.id,
        )
        self.engine.review_correction(correction.id, True, "编目员 陈")
        trace = self.engine.trace_line(self.line2.id)
        numbers = [v["number"] for v in trace["transcription_chain"]]
        self.assertEqual(sorted(numbers), [1, 2])
        self.assertEqual(trace["corrections"][0]["status"], CORRECTION_ADOPTED)
        self.assertIsNotNone(trace["corrections"][0]["resulting_version_id"])

    def test_snapshot_roundtrip_preserves_everything(self):
        self.engine.request_access(
            self.seg_a.id, ACT_PUBLIC_LISTEN, "读者 刘某", ROLE_PUBLIC, at=T2,
        )
        snapshot = self.engine.store.to_snapshot()
        restored = Engine(Store.from_snapshot(snapshot))

        self.assertEqual(restored.store.to_snapshot(), snapshot)
        # 音频内容与来源校验和仍可核验
        self.assertTrue(
            restored.verify_provenance(self.migrated.id)["verified"]
        )
        # 利用记录与许可版本完整
        records = restored.list_access_records(self.seg_a.id)
        self.assertTrue(any(r.granted for r in records))
        self.assertEqual(
            restored._current_license(self.seg_a.id, T3).id, self.lic_a.id
        )
        # 旧引用在重启后仍解析
        citation = restored.create_citation(
            self.seg_a.id, 0, 2000, "研究者 赵某",
        )
        resolved = restored.resolve_citation(citation.id)
        self.assertTrue(resolved["resolved"])
        self.assertEqual(
            resolved["cited_lines"][0]["text"], "民国二十七年我们在武汉。"
        )


if __name__ == "__main__":
    unittest.main()
