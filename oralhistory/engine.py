"""领域引擎：口述史录音开放的全部操作与策略。

关键规则：

母版不覆盖
    digitize 只登记母版；denoise / migrate 产生新衍生品并记录来源文件
    及其登记时校验和，来源链条可逐级核验。仓库无修改/删除文件的方法。

许可按片段、版本化、即时生效
    每次授权或权利人补充限制都产生新 LicenseVersion；访问评估只采用
    “在请求时刻已经生效”的最新版本。新版本不动既有 AccessRecord，
    因此此前的合法利用始终可查、可佐证。

公众只能稳定收听获准部分
    公开收听 = 当前许可授权 public_listen 且片段已在已上线发布批次中；
    说话人真实姓名在解密日之后才出现在公众视图。

纠错先核查、采纳后新版
    Correction 进入“待核查”；采纳时复制当前版本全部行、套用修改，
    发布带依据的新 TranscriptionVersion。引用锁定旧版本与旧时间码，
    解析永远落到原版本行，同时给出当前版本的时间重叠对应行。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from . import model
from .model import (
    ACCESS_ACTIONS,
    ACT_INTERNAL_READ,
    ACT_LIBRARY_LISTEN,
    ACT_PUBLIC_LISTEN,
    CORRECTION_ADOPTED,
    CORRECTION_PENDING,
    CORRECTION_REJECTED,
    DECISION_KINDS,
    DERIVATIVE_OPERATIONS,
    KIND_DERIVATIVE,
    KIND_MASTER,
    LICENSE_TERMS,
    ROLE_PUBLIC,
    STAFF_ROLES,
    STATUS_PENDING_CATALOGING,
    STATUS_PUBLIC,
    STATUS_RESTRICTED,
    STATUS_SEALED,
    TERM_LABELS,
)
from .store import Store


class DomainError(Exception):
    """业务规则冲突。"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()


def _checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class Engine:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _get(self, collection: str, record_id: str):
        record = getattr(self.store, collection).get(record_id)
        if record is None:
            raise DomainError(f"标识 {record_id} 不存在于 {collection}")
        return record

    def _add(self, collection: str, record):
        return self.store.add(collection, record)

    def _current_license(self, segment_id: str, at: str):
        """在 at 时刻有效的最新许可版本；无则返回 None。"""
        effective = [
            lic for lic in self.store.licenses.values()
            if lic.segment_id == segment_id and lic.effective_at <= at
        ]
        return max(effective, key=lambda lic: lic.number, default=None)

    def _current_version(self, segment_id: str):
        versions = [
            v for v in self.store.versions.values() if v.segment_id == segment_id
        ]
        return max(versions, key=lambda v: v.number, default=None)

    def _version_lines(self, version_id: str) -> list[model.TranscriptionLine]:
        return sorted(
            (lin for lin in self.store.lines.values() if lin.version_id == version_id),
            key=lambda lin: (lin.start_ms, lin.seq),
        )

    def _is_released(self, segment_id: str, at: str) -> bool:
        return any(
            segment_id in rel.segment_ids and rel.released_at <= at
            for rel in self.store.releases.values()
        )

    def _lineage(self, file_id: str) -> list[dict]:
        """从指定文件逐级回溯到母版，逐级核验登记的来源校验和。"""
        chain = []
        current = self._get("files", file_id)
        while True:
            entry = {
                "file_id": current.id,
                "kind": current.kind,
                "operation": current.operation,
                "checksum": current.checksum,
                "params": dict(current.params),
                "created_by": current.created_by,
                "created_at": current.created_at,
            }
            if current.kind == KIND_DERIVATIVE:
                source = self.store.files.get(current.source_file_id)
                entry["source_file_id"] = current.source_file_id
                entry["source_checksum_recorded"] = current.source_checksum
                entry["source_exists"] = source is not None
                entry["source_checksum_ok"] = (
                    source is not None and source.checksum == current.source_checksum
                )
                current = source
                chain.append(entry)
                if current is None:
                    break
            else:
                entry["batch_id"] = current.batch_id
                chain.append(entry)
                break
        return chain

    def _speaker_view(self, speaker: model.Speaker, staff: bool, at_date: str) -> dict:
        declassified = (
            speaker.declassify_at is not None
            and speaker.declassify_at <= at_date
        )
        return {
            "id": speaker.id,
            "display_name": speaker.display_name,
            "real_name": speaker.real_name if (staff or declassified) else None,
            "name_public": declassified,
        }

    # ------------------------------------------------------------------
    # 载体与数字化
    # ------------------------------------------------------------------

    def register_carrier(self, label, medium, donor, donated_at, note=""):
        carrier = model.Carrier(
            id=self.store.new_id("carriers"),
            label=label, medium=medium, donor=donor,
            donated_at=donated_at, note=note,
        )
        return self._add("carriers", carrier)

    def digitize(self, carrier_id, operator, equipment, content: bytes,
                 started_at=None, note=""):
        """登记一次数字化：产生批次与母版文件。重复数字化产生独立批次。"""
        carrier = self._get("carriers", carrier_id)
        at = started_at or utcnow()
        batch = model.DigitizationBatch(
            id=self.store.new_id("batches"), carrier_id=carrier_id,
            operator=operator, equipment=equipment, started_at=at, note=note,
        )
        self._add("batches", batch)
        master = model.AudioFile(
            id=self.store.new_id("files"), kind=KIND_MASTER,
            checksum=_checksum(content), duration_ms=len(content), content=content,
            batch_id=batch.id, created_by=operator, created_at=at,
        )
        self._add("files", master)
        carrier.status = STATUS_PENDING_CATALOGING
        return {"batch": batch, "master": master}

    def derive_file(self, source_file_id, operation, content: bytes,
                    created_by, params=None, at=None):
        """由既有文件经降噪或格式迁移生成衍生品，登记来源与来源校验和。"""
        if operation not in DERIVATIVE_OPERATIONS:
            raise DomainError(
                f"不支持的处理操作 {operation}，允许：{', '.join(DERIVATIVE_OPERATIONS)}"
            )
        source = self._get("files", source_file_id)
        derivative = model.AudioFile(
            id=self.store.new_id("files"), kind=KIND_DERIVATIVE,
            checksum=_checksum(content), duration_ms=len(content), content=content,
            source_file_id=source.id, source_checksum=source.checksum,
            operation=operation, params=dict(params or {}),
            created_by=created_by, created_at=at or utcnow(),
        )
        return self._add("files", derivative)

    def verify_provenance(self, file_id) -> dict:
        """核验文件来源链：回溯到母版且每级来源校验和一致。"""
        chain = self._lineage(file_id)
        master = chain[-1]
        ok = (
            master["kind"] == KIND_MASTER
            and all(step.get("source_checksum_ok", True) for step in chain)
        )
        return {"verified": ok, "chain": chain}

    # ------------------------------------------------------------------
    # 切分、说话人、文献
    # ------------------------------------------------------------------

    def cut_segment(self, file_id, start_ms, end_ms, title,
                    speaker_ids=None, note=""):
        audio = self._get("files", file_id)
        if not (0 <= start_ms < end_ms <= audio.duration_ms):
            raise DomainError(
                f"切分范围 [{start_ms}, {end_ms}) 越界，文件时长 {audio.duration_ms}ms"
            )
        for spk_id in speaker_ids or []:
            self._get("speakers", spk_id)
        segment = model.AudioSegment(
            id=self.store.new_id("segments"), file_id=file_id,
            start_ms=start_ms, end_ms=end_ms, title=title,
            speaker_ids=list(speaker_ids or []), note=note,
        )
        return self._add("segments", segment)

    def add_speaker(self, display_name, real_name=None, declassify_at=None, note=""):
        speaker = model.Speaker(
            id=self.store.new_id("speakers"), display_name=display_name,
            real_name=real_name, declassify_at=declassify_at, note=note,
        )
        return self._add("speakers", speaker)

    def schedule_name_release(self, speaker_id, declassify_at, real_name=None):
        """设定或调整说话人真实姓名的解密日期（延迟解密）。"""
        speaker = self._get("speakers", speaker_id)
        speaker.declassify_at = declassify_at
        if real_name is not None:
            speaker.real_name = real_name
        return speaker

    def add_document(self, title, kind, reference, segment_ids=None):
        for seg_id in segment_ids or []:
            self._get("segments", seg_id)
        document = model.Document(
            id=self.store.new_id("documents"), title=title, kind=kind,
            reference=reference, segment_ids=list(segment_ids or []),
        )
        return self._add("documents", document)

    def seal_segment(self, segment_id):
        segment = self._get("segments", segment_id)
        segment.status = STATUS_SEALED
        return segment

    # ------------------------------------------------------------------
    # 许可版本
    # ------------------------------------------------------------------

    def add_license(self, segment_id, terms: dict, note, created_by, effective_at=None):
        """登记一个许可版本（可补充限制，也可追加授权）。

        各版本只约束其生效之后的访问评估；版本只增不改，
        既有的利用记录固定指向当时适用的版本，因此授权范围如何
        双向调整都不会改写历史。
        """
        segment = self._get("segments", segment_id)
        for key in terms:
            if key not in LICENSE_TERMS:
                raise DomainError(f"未知许可行为：{key}")
        full_terms = {term: bool(terms.get(term, False)) for term in LICENSE_TERMS}

        number = len([
            lic for lic in self.store.licenses.values()
            if lic.segment_id == segment_id
        ]) + 1
        license_version = model.LicenseVersion(
            id=self.store.new_id("licenses"), segment_id=segment_id, number=number,
            terms=full_terms, note=note, created_by=created_by,
            effective_at=effective_at or utcnow(),
        )
        self._add("licenses", license_version)
        if segment.status != STATUS_SEALED:
            segment.status = (
                STATUS_PUBLIC if full_terms[ACT_PUBLIC_LISTEN] else STATUS_RESTRICTED
            )
        return license_version

    def list_licenses(self, segment_id) -> list[model.LicenseVersion]:
        return sorted(
            (lic for lic in self.store.licenses.values()
             if lic.segment_id == segment_id),
            key=lambda lic: lic.number,
        )

    # ------------------------------------------------------------------
    # 访问评估与利用记录
    # ------------------------------------------------------------------

    def evaluate_access(self, segment_id, action, actor_role, at=None) -> dict:
        """只做评估、不记日志。"""
        at = at or utcnow()
        segment = self._get("segments", segment_id)

        if action not in ACCESS_ACTIONS:
            raise DomainError(f"未知访问行为：{action}")

        if actor_role in STAFF_ROLES:
            lic = self._current_license(segment_id, at)
            return {
                "granted": True,
                "reason": "馆员工作访问",
                "license_version_id": lic.id if lic else None,
            }

        if segment.status == STATUS_SEALED:
            return {"granted": False, "reason": "片段已封存", "license_version_id": None}

        lic = self._current_license(segment_id, at)
        if lic is None:
            return {"granted": False, "reason": "尚无有效捐赠协议",
                    "license_version_id": None}

        if action == ACT_INTERNAL_READ:
            return {"granted": False, "reason": "内部工作行为仅限馆员",
                    "license_version_id": lic.id}

        if action == ACT_LIBRARY_LISTEN and actor_role == ROLE_PUBLIC:
            return {"granted": False, "reason": "馆内收听仅限到馆读者",
                    "license_version_id": lic.id}

        if not lic.terms.get(action):
            return {
                "granted": False,
                "reason": f"当前许可版本 {lic.id} 未授权“{TERM_LABELS[action]}”",
                "license_version_id": lic.id,
            }

        if action == ACT_PUBLIC_LISTEN and actor_role == ROLE_PUBLIC:
            if not self._is_released(segment_id, at):
                return {
                    "granted": False,
                    "reason": "片段尚未在已上线发布批次中",
                    "license_version_id": lic.id,
                }

        return {
            "granted": True,
            "reason": f"依据许可版本 {lic.id}（{lic.effective_at} 生效）",
            "license_version_id": lic.id,
        }

    def request_access(self, segment_id, action, actor, actor_role, at=None):
        """评估访问并留下只增不改的利用记录（准许与拒绝都记录）。"""
        at = at or utcnow()
        decision = self.evaluate_access(segment_id, action, actor_role, at)
        record = model.AccessRecord(
            id=self.store.new_id("access_records"), segment_id=segment_id,
            action=action, actor=actor, actor_role=actor_role,
            granted=decision["granted"], reason=decision["reason"],
            license_version_id=decision["license_version_id"], at=at,
        )
        return self._add("access_records", record)

    def list_access_records(self, segment_id=None) -> list[model.AccessRecord]:
        records = self.store.access_records.values()
        if segment_id is not None:
            records = (r for r in records if r.segment_id == segment_id)
        return sorted(records, key=lambda r: r.at)

    # ------------------------------------------------------------------
    # 发布批次（分次上线）
    # ------------------------------------------------------------------

    def release(self, name, segment_ids, released_at=None):
        for seg_id in segment_ids:
            self._get("segments", seg_id)
        batch = model.ReleaseBatch(
            id=self.store.new_id("releases"), name=name,
            segment_ids=list(segment_ids), released_at=released_at or utcnow(),
        )
        return self._add("releases", batch)

    # ------------------------------------------------------------------
    # 转写
    # ------------------------------------------------------------------

    def _validate_range(self, segment, start_ms, end_ms):
        if not (0 <= start_ms < end_ms <= segment.end_ms - segment.start_ms):
            raise DomainError(
                f"时间码 [{start_ms}, {end_ms}) 超出片段时长 "
                f"{segment.end_ms - segment.start_ms}ms"
            )

    def create_transcription(self, segment_id, lines, created_by,
                             basis=("initial",), at=None):
        """内部：依据 basis 发布一个新转写版本（首个版本 basis=initial）。"""
        segment = self._get("segments", segment_id)
        supersedes = self._current_version(segment_id)
        if supersedes is None and list(basis) != ["initial"]:
            raise DomainError("片段尚无初版转写，不能以修订依据创建首版")
        version = model.TranscriptionVersion(
            id=self.store.new_id("versions"), segment_id=segment_id,
            number=(supersedes.number + 1 if supersedes else 1),
            basis=list(basis), created_by=created_by, created_at=at or utcnow(),
            supersedes=supersedes.id if supersedes else None,
        )
        self._add("versions", version)
        new_lines = []
        for seq, row in enumerate(lines, start=1):
            start_ms = int(row["start_ms"])
            end_ms = int(row["end_ms"])
            self._validate_range(segment, start_ms, end_ms)
            speaker_id = row.get("speaker_id")
            if speaker_id is not None:
                self._get("speakers", speaker_id)
            line = model.TranscriptionLine(
                id=self.store.new_id("lines"), version_id=version.id, seq=seq,
                start_ms=start_ms, end_ms=end_ms, text=row["text"],
                speaker_id=speaker_id,
            )
            self._add("lines", line)
            new_lines.append(line)
        return {"version": version, "lines": new_lines}

    def initial_transcription(self, segment_id, lines, created_by, at=None):
        existing = self._current_version(segment_id)
        if existing is not None:
            raise DomainError(f"片段已有初版转写 {existing.id}，修订须经纠错或考证流程")
        return self.create_transcription(
            segment_id, lines, created_by, basis=("initial",), at=at
        )

    # ------------------------------------------------------------------
    # 听众纠错 → 核查 → 采纳发布
    # ------------------------------------------------------------------

    def submit_correction(self, segment_id, proposed_text, reason, submitted_by,
                          line_id=None, start_ms=None, end_ms=None):
        segment = self._get("segments", segment_id)
        if line_id is None and start_ms is None:
            raise DomainError("纠错须指明目标转写行（line_id）或起始时间码")
        if start_ms is not None:
            self._validate_range(
                segment, int(start_ms),
                int(end_ms) if end_ms is not None else int(start_ms) + 1,
            )
        if line_id is not None:
            self._get("lines", line_id)
        correction = model.Correction(
            id=self.store.new_id("corrections"), segment_id=segment_id,
            line_id=line_id,
            start_ms=int(start_ms) if start_ms is not None else None,
            end_ms=int(end_ms) if end_ms is not None else None,
            proposed_text=proposed_text, reason=reason, submitted_by=submitted_by,
        )
        return self._add("corrections", correction)

    def review_correction(self, correction_id, adopt, reviewed_by,
                          review_note="", at=None):
        """馆员核查。采纳则发布以该纠错为依据的新转写版本。"""
        correction = self._get("corrections", correction_id)
        if correction.status != CORRECTION_PENDING:
            raise DomainError(f"纠错 {correction_id} 已核查，不能重复处理")

        if not adopt:
            correction.status = CORRECTION_REJECTED
            correction.reviewed_by = reviewed_by
            correction.review_note = review_note
            return {"adopted": False, "correction": correction}

        segment = self._get("segments", correction.segment_id)
        current = self._current_version(segment.id)
        if current is None:
            raise DomainError("片段尚无转写，无法套用纠错")
        old_lines = self._version_lines(current.id)

        target = None
        if correction.line_id is not None:
            target = next(
                (lin for lin in old_lines if lin.id == correction.line_id), None
            )
            if target is None or target.version_id != current.id:
                raise DomainError("纠错目标行不属于当前转写版本，请按时间码重新定位")
        else:
            target = next(
                (lin for lin in old_lines
                 if lin.start_ms <= correction.start_ms < lin.end_ms),
                None,
            )
            if target is None:
                raise DomainError("当前版本中没有与纠错时间码重叠的转写行")

        result = self.create_transcription(
            segment.id,
            lines=[
                {
                    "start_ms": (
                        correction.start_ms
                        if correction.start_ms is not None else lin.start_ms
                    ),
                    "end_ms": (
                        correction.end_ms
                        if correction.end_ms is not None else lin.end_ms
                    ),
                    "text": (
                        correction.proposed_text if lin.id == target.id else lin.text
                    ),
                    "speaker_id": lin.speaker_id,
                }
                for lin in old_lines
            ],
            created_by=reviewed_by,
            basis=(f"correction:{correction.id}",),
            at=at,
        )
        correction.status = CORRECTION_ADOPTED
        correction.reviewed_by = reviewed_by
        correction.review_note = review_note
        correction.resulting_version_id = result["version"].id
        return {"adopted": True, "correction": correction, **result}

    def list_corrections(self, segment_id=None, status=None):
        rows = self.store.corrections.values()
        if segment_id is not None:
            rows = (r for r in rows if r.segment_id == segment_id)
        if status is not None:
            rows = (r for r in rows if r.status == status)
        return sorted(rows, key=lambda r: r.id)

    # ------------------------------------------------------------------
    # 考证与年代修订
    # ------------------------------------------------------------------

    def add_research_decision(self, segment_id, kind, conclusion, rationale,
                              decided_by, at=None):
        if kind not in DECISION_KINDS:
            raise DomainError(f"未知考证类型：{kind}")
        self._get("segments", segment_id)
        decision = model.ResearchDecision(
            id=self.store.new_id("decisions"), segment_id=segment_id, kind=kind,
            conclusion=conclusion, rationale=rationale, decided_by=decided_by,
            at=at or utcnow(),
        )
        return self._add("decisions", decision)

    def revise_dating(self, segment_id, date_range, rationale, revised_by,
                      decision_id=None, at=None):
        self._get("segments", segment_id)
        if decision_id is not None:
            decision = self._get("decisions", decision_id)
            if decision.segment_id != segment_id:
                raise DomainError("考证决定不属于该片段")
        revision = model.DatingRevision(
            id=self.store.new_id("datings"), segment_id=segment_id,
            date_range=date_range, rationale=rationale, decision_id=decision_id,
            revised_by=revised_by, at=at or utcnow(),
        )
        return self._add("datings", revision)

    def current_dating(self, segment_id):
        # 同刻多次修订时以标识次序兜底，保证取到最新一条
        return max(
            (d for d in self.store.datings.values() if d.segment_id == segment_id),
            key=lambda d: (d.at, d.id), default=None,
        )

    def revise_transcription_by_decision(self, segment_id, lines, decision_id,
                                         created_by, at=None):
        """依据考证决定发布新转写版本（整版重订，旧版保留）。"""
        decision = self._get("decisions", decision_id)
        if decision.segment_id != segment_id:
            raise DomainError("考证决定不属于该片段")
        return self.create_transcription(
            segment_id, lines, created_by=created_by,
            basis=(f"decision:{decision.id}",), at=at,
        )

    # ------------------------------------------------------------------
    # 稳定引用
    # ------------------------------------------------------------------

    def create_citation(self, segment_id, start_ms, end_ms, created_by,
                        version_id=None, at=None):
        segment = self._get("segments", segment_id)
        self._validate_range(segment, int(start_ms), int(end_ms))
        if version_id is None:
            version = self._current_version(segment_id)
            if version is None:
                raise DomainError("片段尚无转写，无法建立引用")
        else:
            version = self._get("versions", version_id)
            if version.segment_id != segment_id:
                raise DomainError("转写版本不属于该片段")
        citation = model.Citation(
            id=self.store.new_id("citations"), segment_id=segment_id,
            version_id=version.id, start_ms=int(start_ms), end_ms=int(end_ms),
            created_by=created_by, created_at=at or utcnow(),
        )
        return self._add("citations", citation)

    @staticmethod
    def _line_payload(line: model.TranscriptionLine) -> dict:
        return {
            "id": line.id, "seq": line.seq,
            "start_ms": line.start_ms, "end_ms": line.end_ms,
            "text": line.text, "speaker_id": line.speaker_id,
        }

    def resolve_citation(self, citation_id, staff=False, at=None):
        """解析引用：永远返回其锁定版本的行；同时附当前版本的时间重叠行。"""
        at = at or utcnow()
        citation = self._get("citations", citation_id)
        cited_version = self._get("versions", citation.version_id)
        segment = self._get("segments", citation.segment_id)
        current = self._current_version(segment.id)

        def overlapping(version_id):
            return [
                self._line_payload(lin)
                for lin in self._version_lines(version_id)
                if lin.start_ms < citation.end_ms and lin.end_ms > citation.start_ms
            ]

        return {
            "citation": {
                "id": citation.id, "segment_id": citation.segment_id,
                "start_ms": citation.start_ms, "end_ms": citation.end_ms,
                "created_by": citation.created_by, "created_at": citation.created_at,
            },
            "resolved": True,
            "cited_version": {
                "id": cited_version.id, "number": cited_version.number,
                "basis": cited_version.basis,
            },
            "cited_lines": overlapping(cited_version.id),
            "current_version_id": current.id,
            "current_version_number": current.number,
            "current_lines": overlapping(current.id),
            "version_is_current": current.id == cited_version.id,
            "segment_duration_ms": segment.end_ms - segment.start_ms,
        }

    # ------------------------------------------------------------------
    # 公众目录与馆员溯源
    # ------------------------------------------------------------------

    def public_catalog(self, at=None):
        """公众可稳定收听的片段：已发布、当前许可授权公开收听、姓名按解密日遮蔽。"""
        at = at or utcnow()
        at_date = at[:10]
        items = []
        for segment in sorted(self.store.segments.values(), key=lambda s: s.id):
            if segment.status == STATUS_SEALED:
                continue
            lic = self._current_license(segment.id, at)
            if lic is None or not lic.terms[ACT_PUBLIC_LISTEN]:
                continue
            if not self._is_released(segment.id, at):
                continue
            version = self._current_version(segment.id)
            items.append({
                "segment_id": segment.id,
                "title": segment.title,
                "time_range_ms": [segment.start_ms, segment.end_ms],
                "file_id": segment.file_id,
                "speakers": [
                    self._speaker_view(self._get("speakers", spk), False, at_date)
                    for spk in segment.speaker_ids
                ],
                "transcription_version": version.id if version else None,
                "dating": (
                    self.current_dating(segment.id).date_range
                    if self.current_dating(segment.id) else None
                ),
            })
        return {"at": at, "items": items}

    def trace_line(self, line_id, at=None):
        """馆员从任一转写句追到声段、处理历史、许可版本与考证决定。"""
        at = at or utcnow()
        line = self._get("lines", line_id)
        version = self._get("versions", line.version_id)
        segment = self._get("segments", version.segment_id)

        # 片段的完整转写版本链（含本行版本之后的修订），便于馆员发现
        # 这句话后来是否被纠错或考证重订；锚点版本单独标注。
        all_versions = sorted(
            (v for v in self.store.versions.values()
             if v.segment_id == segment.id),
            key=lambda v: v.number,
        )
        version_chain = [
            {
                "version_id": cursor.id, "number": cursor.number,
                "basis": cursor.basis, "created_by": cursor.created_by,
                "created_at": cursor.created_at, "supersedes": cursor.supersedes,
                "is_line_version": cursor.id == version.id,
            }
            for cursor in all_versions
        ]

        provenance = self.verify_provenance(segment.file_id)
        master_entry = provenance["chain"][-1]
        batch = self.store.batches.get(master_entry.get("batch_id"))
        carrier = self.store.carriers.get(batch.carrier_id) if batch else None

        current_lic = self._current_license(segment.id, at)
        return {
            "line": self._line_payload(line),
            "segment": {
                "id": segment.id, "title": segment.title, "status": segment.status,
                "start_ms": segment.start_ms, "end_ms": segment.end_ms,
                "speaker_ids": segment.speaker_ids,
            },
            "transcription_chain": version_chain,
            "audio_provenance": {
                "verified": provenance["verified"],
                "segment_file_id": segment.file_id,
                "chain": provenance["chain"],
                "digitization_batch": (
                    {"id": batch.id, "operator": batch.operator,
                     "equipment": batch.equipment, "started_at": batch.started_at}
                    if batch else None
                ),
                "carrier": (
                    {"id": carrier.id, "label": carrier.label, "medium": carrier.medium,
                     "donor": carrier.donor, "donated_at": carrier.donated_at}
                    if carrier else None
                ),
            },
            "licenses": [
                {
                    "id": lic.id, "number": lic.number, "terms": lic.terms,
                    "note": lic.note, "effective_at": lic.effective_at,
                    "is_current": current_lic is not None and lic.id == current_lic.id,
                }
                for lic in self.list_licenses(segment.id)
            ],
            "research_decisions": [
                {"id": d.id, "kind": d.kind, "conclusion": d.conclusion,
                 "rationale": d.rationale, "decided_by": d.decided_by, "at": d.at}
                for d in self.store.decisions.values()
                if d.segment_id == segment.id
            ],
            "dating_revisions": [
                {"id": d.id, "date_range": d.date_range, "rationale": d.rationale,
                 "decision_id": d.decision_id, "revised_by": d.revised_by, "at": d.at}
                for d in self.store.datings.values()
                if d.segment_id == segment.id
            ],
            "corrections": [
                {"id": c.id, "status": c.status, "proposed_text": c.proposed_text,
                 "reason": c.reason, "submitted_by": c.submitted_by,
                 "reviewed_by": c.reviewed_by,
                 "resulting_version_id": c.resulting_version_id}
                for c in self.list_corrections(segment.id)
            ],
            "releases": [
                {"id": rel.id, "name": rel.name, "released_at": rel.released_at}
                for rel in self.store.releases.values()
                if segment.id in rel.segment_ids
            ],
            "documents": [
                {"id": doc.id, "title": doc.title, "kind": doc.kind,
                 "reference": doc.reference}
                for doc in self.store.documents.values()
                if segment.id in doc.segment_ids
            ],
        }
