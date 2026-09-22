"""口述史录音开放的领域层。

分层模型（自上而下引用，自下而上可溯源）：

    载体 Carrier ── 数字化批次 DigitizationBatch ── 数字母版 AudioFile
          │                                              │
          │                                       处理事件 ProcessEvent
          │                                       （降噪/切分/格式迁移，只派生不覆写）
          │                                              │
          └────────────── 片段 Clip ───────────── 派生音频文件
                              │
                ┌─────────────┼──────────────────────┐
           说话人 Speaker  转写版本 TranscriptVersion  关联文献 Document
                              │
                         转写行 TranscriptLine ── 稳定引用 Citation

捐赠协议 DonorAgreement 按片段授权（公开收听/馆内收听/引用/下载）；
补充限制 Restriction 即时生效，但此前已发生的合法利用保存在 UsageRecord 中。
听众纠错 Correction 先进入核查，采纳后发布新转写版；引用按“生成时版本”
冻结时间码，旧版时间码经映射继续解析到当前片段位置。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# 常量：许可位与统一状态词汇（与 fixtures/domain.json 保持一致语义）
# ---------------------------------------------------------------------------

PERM_PUBLIC_LISTEN = "public_listen"  # 公众在线收听
PERM_ONSITE_LISTEN = "onsite_listen"  # 馆内使用（馆员/到馆读者）
PERM_CITE = "cite"                    # 允许引用
PERM_DOWNLOAD = "download"            # 允许下载
ALL_PERMS = (PERM_PUBLIC_LISTEN, PERM_ONSITE_LISTEN, PERM_CITE, PERM_DOWNLOAD)

ROLE_PUBLIC = "public"
ROLE_LIBRARIAN = "librarian"

CARRIER_TYPES = ("录音磁带", "开盘带", "盒式磁带", "钢丝录音", "唱片", "光盘")
CARRIER_STATUS = ("待数字化", "数字化中", "已数字化", "已封存")
CLIP_STATUS = ("待编目", "限制开放", "公开开放", "已封存")
PROCESS_KINDS = ("采集", "降噪", "切分", "格式迁移")
TRANSCRIPT_STATUS = ("待核查", "已发布", "已撤回")
CORRECTION_STATUS = ("待核查", "已采纳", "已驳回")
RESEARCH_STATUS = ("待考", "已确认", "存疑")
RELEASE_STATUS = ("已上线", "已撤回")
SPEAKER_VISIBILITY = ("匿名", "馆内可见", "公开")
VISIBILITY_ORDER = {"匿名": 0, "馆内可见": 1, "公开": 2}

_TC_RE = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)(\.\d{1,3})?$")
_CITE_RE = re.compile(r"^cite:([0-9a-f]{8})-v(\d+)-l(\d+)(?:-([0-9a-f]{8}))?$")


def now_ts() -> str:
    """单调、可排序的时间戳（毫秒精度，便于同秒内多次修订仍有先后）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


def parse_tc(value: str) -> float:
    """把 ``HH:MM:SS[.mmm]`` 解析为秒；非法时抛出 ValueError。"""
    match = _TC_RE.match(value or "")
    if not match:
        raise ValueError(f"时间码格式应为 HH:MM:SS[.mmm]：{value!r}")
    h, m, s, ms = match.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + (int((ms or ".0")[1:]) / 1000.0)


def short_id() -> str:
    return uuid.uuid4().hex[:8]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{short_id()}"


class DomainError(Exception):
    """所有可预期的业务校验错误（HTTP 层映射为 4xx）。"""


@dataclass
class _Seq:
    """按父对象维护的单调序号。"""

    value: int = 0

    def next(self) -> int:
        self.value += 1
        return self.value


# ---------------------------------------------------------------------------
# 捐赠协议与补充限制
# ---------------------------------------------------------------------------

@dataclass
class DonorAgreement:
    """捐赠协议：可针对一组片段授予不同许可，是许可的合法来源。"""

    agreement_id: str
    donor: str
    clip_ids: list[str]
    grants: dict[str, bool]
    note: str = ""
    created_at: str = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agreement_id": self.agreement_id,
            "donor": self.donor,
            "clip_ids": list(self.clip_ids),
            "grants": dict(self.grants),
            "note": self.note,
            "created_at": self.created_at,
        }


@dataclass
class Restriction:
    """权利人/馆员事后追加的限制。

    ``effective`` 之前发生的合法利用不受追溯（usage 记录保留）；
    生效后所有新访问立即按收窄后的许可判定。
    """

    restriction_id: str
    clip_id: str
    denied_perms: list[str]
    reason: str
    issued_by: str
    effective_at: str
    usage_cutoff_at: str  # 与 effective_at 相同：此后新利用一律禁止
    lifted_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "restriction_id": self.restriction_id,
            "clip_id": self.clip_id,
            "denied_perms": list(self.denied_perms),
            "reason": self.reason,
            "issued_by": self.issued_by,
            "effective_at": self.effective_at,
            "lifted_at": self.lifted_at,
            "active": self.lifted_at is None,
        }


@dataclass
class UsageRecord:
    """一次访问利用的留痕；即使许可事后被收窄，记录仍保留且标注现状。"""

    usage_id: str
    clip_id: str
    actor: str
    perm: str
    at: str
    justification_agreement_id: Optional[str]
    detail: str = ""
    still_permitted: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "usage_id": self.usage_id,
            "clip_id": self.clip_id,
            "actor": self.actor,
            "perm": self.perm,
            "at": self.at,
            "justification_agreement_id": self.justification_agreement_id,
            "detail": self.detail,
            "still_permitted": self.still_permitted,
        }


# ---------------------------------------------------------------------------
# 物理层：载体 → 数字化批次 → 文件 → 处理谱系
# ---------------------------------------------------------------------------

@dataclass
class Carrier:
    """磁带等物理载体，捐赠信息与保存状态挂在此层。"""

    carrier_id: str
    title: str
    carrier_type: str
    donor: str
    received_at: str
    status: str = "待数字化"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class DigitizationBatch:
    """一次数字化作业。同一载体可有多个批次（重复数字化）。"""

    batch_id: str
    carrier_id: str
    operator: str
    captured_at: str
    note: str = ""
    master_file_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class AudioFile:
    """数字音频文件。母版不可变；所有处理只产生新的派生文件。

    ``derived_from_file_id`` + ``derived_from_event_id`` 构成谱系边。
    """

    file_id: str
    carrier_id: str
    batch_id: Optional[str]
    path: str
    media_type: str
    checksum: str
    is_master: bool
    role: str                       # master / denoised / segment / migrated
    derived_from_file_id: Optional[str] = None
    derived_from_event_id: Optional[str] = None
    created_at: str = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ProcessEvent:
    """降噪、切分、格式迁移等处理动作的留痕，可验证来源、不覆写母版。"""

    event_id: str
    kind: str
    operator: str
    source_file_id: str
    output_file_ids: list[str]
    params: dict[str, Any]
    at: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------
# 内容层：说话人 / 片段 / 文献 / 考证
# ---------------------------------------------------------------------------

@dataclass
class Speaker:
    """说话人。真实姓名可延迟解密：未到 ``reveal_after`` 前对外只呈现化名。"""

    speaker_id: str
    public_pseudonym: str
    real_name: Optional[str] = None
    reveal_after: Optional[str] = None   # ISO 日期；为空表示长期不解密
    visibility: str = "匿名"
    note: str = ""

    def revealed(self, at: Optional[str] = None) -> bool:
        if not self.real_name or self.visibility == "匿名":
            return False
        if self.visibility == "公开":
            return True
        # 馆内可见：不按日期自动公开，仅馆员可见
        if self.reveal_after is None:
            return False
        return (at or now_ts()) >= self.reveal_after

    def view(self, role: str, at: Optional[str] = None) -> dict[str, Any]:
        data = self.to_dict()
        is_lib = role == ROLE_LIBRARIAN
        can_see_name = is_lib or self.revealed(at)
        if not can_see_name:
            data["real_name"] = None
            data["name_revealed"] = False
        else:
            data["name_revealed"] = True
        if not is_lib:
            data.pop("note", None)
        data["effective_visibility"] = "公开" if self.revealed(at) else self.visibility
        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_id": self.speaker_id,
            "public_pseudonym": self.public_pseudonym,
            "real_name": self.real_name,
            "reveal_after": self.reveal_after,
            "visibility": self.visibility,
            "note": self.note,
        }


@dataclass
class ResearchDecision:
    """年代考证等编辑决定及其依据，馆员可从片段一路追溯。"""

    decision_id: str
    clip_id: str
    topic: str
    conclusion: str
    basis: str
    decided_by: str
    status: str = "待考"
    at: str = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Document:
    """与片段关联的文献（捐赠文本、档案、书目等）。"""

    document_id: str
    title: str
    kind: str
    ref: str
    clip_ids: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Clip:
    """音频片段：许可、上线、说话人、转写、考证的聚合根。"""

    clip_id: str
    carrier_id: str
    title: str
    source_file_id: str            # 片段切分自哪个（派生）音频文件
    start_tc: str
    end_tc: str
    speaker_ids: list[str] = field(default_factory=list)
    status: str = "待编目"
    published_file_id: Optional[str] = None   # 公众实际收听到的发布文件
    releases: list[dict[str, Any]] = field(default_factory=list)
    agreement_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_ts)
    updated_at: str = field(default_factory=now_ts)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ReleaseWave:
    """分次上线的发布波次；只在波次中的片段才对公众可见。"""

    wave_id: str
    label: str
    released_at: str
    clip_ids: list[str] = field(default_factory=list)
    status: str = "已上线"
    withdrawn_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------
# 转写层：版本 / 行 / 纠错 / 引用
# ---------------------------------------------------------------------------

@dataclass
class TranscriptVersion:
    """转写版本。行号在版本内稳定；发布后不可改，只能再发新版。"""

    clip_id: str
    version_no: int
    lines: list["TranscriptLine"]
    status: str = "待核查"
    created_by: str = ""
    created_at: str = field(default_factory=now_ts)
    published_at: Optional[str] = None
    basis: str = ""                # 采纳的纠错/考证依据
    supersedes_version: Optional[int] = None

    def to_dict(self, include_lines: bool = True) -> dict[str, Any]:
        data = {
            "clip_id": self.clip_id,
            "version_no": self.version_no,
            "status": self.status,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "published_at": self.published_at,
            "basis": self.basis,
            "supersedes_version": self.supersedes_version,
        }
        if include_lines:
            data["lines"] = [line.to_dict() for line in self.lines]
        return data


@dataclass
class TranscriptLine:
    line_no: int
    text: str
    start_tc: str
    end_tc: str
    speaker_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Correction:
    """听众纠错：先核查，采纳后才产生新转写版。"""

    correction_id: str
    clip_id: str
    submitter: str
    payload: dict[str, Any]       # {"line_no": n, "proposed_text": ...} 等
    status: str = "待核查"
    submitted_at: str = field(default_factory=now_ts)
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    resolution_note: str = ""
    resulting_version_no: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Citation:
    """冻结式稳定引用：记录生成时的版本号与当时时间码。

    转写再版后，旧引用经版本行映射 + 时间码映射仍可解析；
    若行已被删除/合并，解析状态会如实标注而非静默指向错误内容。
    """

    citation_id: str
    clip_id: str
    version_no: int
    line_no: int
    timecode: str
    created_at: str
    created_by: str
    resolved_at: Optional[str] = None

    @property
    def token(self) -> str:
        tail = f"-{self.resolved_at[:8].replace('-', '')}" if self.resolved_at else ""
        return f"cite:{self.clip_id.replace('clip_', '')}-v{self.version_no}-l{self.line_no}{tail}"

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["token"] = self.token
        return data


# ===========================================================================
# 应用服务
# ===========================================================================

class OralHistoryService:
    """线程安全的内存领域服务；可通过 save/load 序列化为单个 JSON 文件。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.carriers: dict[str, Carrier] = {}
        self.batches: dict[str, DigitizationBatch] = {}
        self.files: dict[str, AudioFile] = {}
        self.events: dict[str, ProcessEvent] = {}
        self.clips: dict[str, Clip] = {}
        self.speakers: dict[str, Speaker] = {}
        self.documents: dict[str, Document] = {}
        self.agreements: dict[str, DonorAgreement] = {}
        self.restrictions: dict[str, Restriction] = {}
        self.usages: list[UsageRecord] = []
        self.research: dict[str, ResearchDecision] = {}
        self.versions: dict[str, list[TranscriptVersion]] = {}
        self.corrections: dict[str, Correction] = {}
        self.citations: dict[str, Citation] = {}
        self.waves: dict[str, ReleaseWave] = {}
        self._clip_seq = _Seq()

    # ----- 工具 -----------------------------------------------------------

    def _require(self, store: dict[str, Any], key: str, label: str) -> Any:
        if key not in store:
            raise DomainError(f"{label}不存在：{key}")
        return store[key]

    def _clip_versions(self, clip_id: str) -> list[TranscriptVersion]:
        return self.versions.setdefault(clip_id, [])

    def _latest_version(self, clip_id: str, *, published_only: bool = False) -> Optional[TranscriptVersion]:
        versions = self._clip_versions(clip_id)
        for version in reversed(versions):
            if not published_only or version.status == "已发布":
                return version
        return None

    # ----- 载体与数字化 ---------------------------------------------------

    def register_carrier(self, title: str, carrier_type: str, donor: str,
                         received_at: str, note: str = "") -> dict[str, Any]:
        if carrier_type not in CARRIER_TYPES:
            raise DomainError(f"载体类型应为 {CARRIER_TYPES} 之一")
        carrier = Carrier(_new_id("carrier"), title, carrier_type, donor, received_at, note=note)
        with self._lock:
            self.carriers[carrier.carrier_id] = carrier
        return carrier.to_dict()

    def digitize(self, carrier_id: str, operator: str, captured_at: str,
                 master_path: str, media_type: str, checksum: str,
                 note: str = "") -> dict[str, Any]:
        """登记一次数字化：生成批次及其不可变母版文件。重复数字化即再次调用。"""
        with self._lock:
            carrier = self._require(self.carriers, carrier_id, "载体")
            batch = DigitizationBatch(_new_id("batch"), carrier_id, operator, captured_at, note)
            master = AudioFile(
                _new_id("file"), carrier_id, batch.batch_id, master_path, media_type,
                checksum, is_master=True, role="master",
            )
            batch.master_file_id = master.file_id
            self.batches[batch.batch_id] = batch
            self.files[master.file_id] = master
            carrier.status = "数字化中" if carrier.status == "待数字化" else carrier.status
            for other in self.batches.values():
                if other.carrier_id == carrier_id:
                    carrier.status = "已数字化"
            return {"batch": batch.to_dict(), "master": master.to_dict()}

    def process(self, kind: str, source_file_id: str, operator: str,
                outputs: list[dict[str, Any]], params: Optional[dict[str, Any]] = None,
                note: str = "") -> dict[str, Any]:
        """对源文件做降噪/切分/格式迁移；母版永不被覆盖，输出为新派生文件。"""
        if kind not in PROCESS_KINDS:
            raise DomainError(f"处理类型应为 {PROCESS_KINDS} 之一")
        if not outputs:
            raise DomainError("处理至少要产出一个派生文件")
        with self._lock:
            source = self._require(self.files, source_file_id, "音频文件")
            event = ProcessEvent(_new_id("event"), kind, operator, source_file_id, [],
                                 params or {}, now_ts(), note)
            for spec in outputs:
                if self._is_master_ancestor(source.file_id) and spec.get("is_master"):
                    raise DomainError("母版谱系不可被覆写：派生文件不得标记为母版")
                audio = AudioFile(
                    _new_id("file"),
                    carrier_id=source.carrier_id,
                    batch_id=source.batch_id,
                    path=spec["path"],
                    media_type=spec.get("media_type", source.media_type),
                    checksum=spec["checksum"],
                    is_master=False,
                    role={"降噪": "denoised", "切分": "segment", "格式迁移": "migrated",
                          "采集": "master"}[kind],
                    derived_from_file_id=source.file_id,
                    derived_from_event_id=event.event_id,
                )
                self.files[audio.file_id] = audio
                event.output_file_ids.append(audio.file_id)
            self.events[event.event_id] = event
            return {"event": event.to_dict(),
                    "outputs": [self.files[fid].to_dict() for fid in event.output_file_ids]}

    def _is_master_ancestor(self, file_id: str) -> bool:
        current = self.files.get(file_id)
        while current:
            if current.is_master:
                return True
            if not current.derived_from_file_id:
                return False
            current = self.files.get(current.derived_from_file_id)
        return False

    def provenance_chain(self, file_id: str) -> list[dict[str, Any]]:
        """从任一文件回溯到母版的完整谱系（文件 + 处理事件交替）。"""
        with self._lock:
            chain: list[dict[str, Any]] = []
            current = self._require(self.files, file_id, "音频文件")
            while True:
                chain.append({"type": "file", "file": current.to_dict()})
                if current.is_master or not current.derived_from_file_id:
                    break
                event = self.events.get(current.derived_from_event_id)
                if event:
                    chain.append({"type": "event", "event": event.to_dict()})
                current = self.files[current.derived_from_file_id]
            return chain

    # ----- 说话人与片段 ---------------------------------------------------

    def register_speaker(self, pseudonym: str, real_name: Optional[str] = None,
                         reveal_after: Optional[str] = None, visibility: str = "匿名",
                         note: str = "") -> dict[str, Any]:
        if visibility not in SPEAKER_VISIBILITY:
            raise DomainError(f"可见性应为 {SPEAKER_VISIBILITY} 之一")
        with self._lock:
            speaker = Speaker(_new_id("spk"), pseudonym, real_name, reveal_after, visibility, note)
            self.speakers[speaker.speaker_id] = speaker
            return speaker.to_dict()

    def create_clip(self, carrier_id: str, title: str, source_file_id: str,
                    start_tc: str, end_tc: str, speaker_ids: Optional[list[str]] = None
                    ) -> dict[str, Any]:
        with self._lock:
            carrier = self._require(self.carriers, carrier_id, "载体")
            source = self._require(self.files, source_file_id, "音频文件")
            if source.carrier_id != carrier_id:
                raise DomainError("片段源文件与载体不属于同一载体")
            start, end = parse_tc(start_tc), parse_tc(end_tc)
            if start >= end:
                raise DomainError("片段结束时间码必须晚于开始时间码")
            for sid in speaker_ids or []:
                self._require(self.speakers, sid, "说话人")
            clip = Clip(
                _new_id("clip"), carrier_id, title, source_file_id, start_tc, end_tc,
                speaker_ids=list(speaker_ids or []),
                seq=self._clip_seq.next(),
            )
            self.clips[clip.clip_id] = clip
            carrier.status = "已数字化" if carrier.status == "数字化中" else carrier.status
            return clip.to_dict()

    def add_related_document(self, title: str, kind: str, ref: str,
                             clip_ids: Optional[list[str]] = None, note: str = ""
                             ) -> dict[str, Any]:
        with self._lock:
            for cid in clip_ids or []:
                self._require(self.clips, cid, "片段")
            doc = Document(_new_id("doc"), title, kind, ref, list(clip_ids or []), note)
            self.documents[doc.document_id] = doc
            return doc.to_dict()

    def add_research_decision(self, clip_id: str, topic: str, conclusion: str,
                              basis: str, decided_by: str, status: str = "待考"
                              ) -> dict[str, Any]:
        if status not in RESEARCH_STATUS:
            raise DomainError(f"考证状态应为 {RESEARCH_STATUS} 之一")
        with self._lock:
            self._require(self.clips, clip_id, "片段")
            decision = ResearchDecision(_new_id("rd"), clip_id, topic, conclusion,
                                        basis, decided_by, status)
            self.research[decision.decision_id] = decision
            return decision.to_dict()

    # ----- 许可：协议、限制、判定、留痕 -----------------------------------

    def add_agreement(self, donor: str, clip_ids: list[str],
                      grants: dict[str, bool], note: str = "",
                      signed_at: Optional[str] = None) -> dict[str, Any]:
        for perm in grants:
            if perm not in ALL_PERMS:
                raise DomainError(f"未知许可：{perm}（合法值 {ALL_PERMS}）")
        if not clip_ids:
            raise DomainError("协议至少覆盖一个片段")
        with self._lock:
            for cid in clip_ids:
                self._require(self.clips, cid, "片段")
            agreement = DonorAgreement(_new_id("agr"), donor, list(clip_ids),
                                       {p: bool(v) for p, v in grants.items()}, note,
                                       created_at=signed_at or now_ts())
            self.agreements[agreement.agreement_id] = agreement
            for cid in clip_ids:
                clip = self.clips[cid]
                if agreement.agreement_id not in clip.agreement_ids:
                    clip.agreement_ids.append(agreement.agreement_id)
                clip.updated_at = now_ts()
            self._refresh_clip_status(clip_ids)
            return agreement.to_dict()

    def add_restriction(self, clip_id: str, denied_perms: list[str], reason: str,
                        issued_by: str, effective_at: Optional[str] = None
                        ) -> dict[str, Any]:
        for perm in denied_perms:
            if perm not in ALL_PERMS:
                raise DomainError(f"未知许可：{perm}")
        with self._lock:
            self._require(self.clips, clip_id, "片段")
            effective_at = effective_at or now_ts()
            restriction = Restriction(
                _new_id("rst"), clip_id, list(denied_perms), reason, issued_by,
                effective_at, effective_at,
            )
            self.restrictions[restriction.restriction_id] = restriction
            self.clips[clip_id].updated_at = now_ts()
            self._refresh_clip_status([clip_id])
            return restriction.to_dict()

    def lift_restriction(self, restriction_id: str, lifted_at: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            restriction = self._require(self.restrictions, restriction_id, "限制")
            restriction.lifted_at = lifted_at or now_ts()
            self._refresh_clip_status([restriction.clip_id])
            return restriction.to_dict()

    def _active_restrictions(self, clip_id: str, at: str) -> list[Restriction]:
        return [
            r for r in self.restrictions.values()
            if r.clip_id == clip_id and r.lifted_at is None and r.effective_at <= at
        ]

    def effective_permissions(self, clip_id: str, at: Optional[str] = None) -> dict[str, Any]:
        """计算某时刻的实际许可：协议授权 ∩ 未被限制收窄。"""
        at = at or now_ts()
        with self._lock:
            clip = self._require(self.clips, clip_id, "片段")
            granted = {perm: False for perm in ALL_PERMS}
            sources: dict[str, str] = {}
            for aid in clip.agreement_ids:
                agreement = self.agreements[aid]
                if agreement.created_at <= at:
                    for perm, value in agreement.grants.items():
                        if value and not granted[perm]:
                            granted[perm] = True
                            sources[perm] = aid
            denied: dict[str, str] = {}
            for restriction in self._active_restrictions(clip_id, at):
                for perm in restriction.denied_perms:
                    granted[perm] = False
                    denied[perm] = restriction.restriction_id
            return {
                "clip_id": clip_id,
                "at": at,
                "granted": {p: granted[p] for p in ALL_PERMS},
                "grant_sources": sources,
                "denied_by": denied,
                "agreement_ids": list(clip.agreement_ids),
                "active_restriction_ids": [r.restriction_id
                                           for r in self._active_restrictions(clip_id, at)],
            }

    def _refresh_clip_status(self, clip_ids: list[str]) -> None:
        for cid in clip_ids:
            clip = self.clips[cid]
            if clip.status == "已封存":
                continue
            effective = self.effective_permissions(cid)["granted"]
            if effective[PERM_PUBLIC_LISTEN]:
                clip.status = "公开开放"
            elif any(effective.values()):
                clip.status = "限制开放"
            else:
                clip.status = "待编目"

    def _role_perm(self, role: str, perm: str) -> bool:
        if role == ROLE_LIBRARIAN:
            return perm in (PERM_ONSITE_LISTEN, PERM_CITE, PERM_DOWNLOAD)
        if role == ROLE_PUBLIC:
            return perm in (PERM_PUBLIC_LISTEN, PERM_CITE, PERM_DOWNLOAD)
        raise DomainError(f"未知角色：{role}")

    def request_access(self, clip_id: str, actor: str, role: str, perm: str,
                       at: Optional[str] = None, detail: str = "") -> dict[str, Any]:
        """访问判定的唯一入口：判定即时生效，并为每次利用留痕。"""
        if perm not in ALL_PERMS:
            raise DomainError(f"未知许可：{perm}")
        at = at or now_ts()
        with self._lock:
            self._require(self.clips, clip_id, "片段")
            effective = self.effective_permissions(clip_id, at)
            allowed = self._role_perm(role, perm) and effective["granted"][perm]
            justification = effective["grant_sources"].get(perm)
            record = UsageRecord(
                _new_id("use"), clip_id, actor, perm, at,
                justification if allowed else None, detail,
                still_permitted=allowed,
            )
            self.usages.append(record)
            return {"allowed": allowed, "usage": record.to_dict(),
                    "effective": effective}

    def usage_history(self, clip_id: Optional[str] = None) -> list[dict[str, Any]]:
        """合法利用记录；事后限制不删除旧记录，只在快照中标注当前是否仍允许。"""
        with self._lock:
            result = []
            for record in self.usages:
                if clip_id and record.clip_id != clip_id:
                    continue
                data = record.to_dict()
                current = self.effective_permissions(record.clip_id)["granted"]
                data["still_permitted"] = current[record.perm]
                result.append(data)
            return result

    # ----- 上线波次与公众目录 --------------------------------------------

    def create_release_wave(self, label: str, released_at: str,
                            clip_ids: list[str]) -> dict[str, Any]:
        with self._lock:
            wave = ReleaseWave(_new_id("wave"), label, released_at, [], "已上线")
            self.waves[wave.wave_id] = wave
            for cid in clip_ids:
                self._release_clip(cid, wave)
            return wave.to_dict()

    def add_clips_to_wave(self, wave_id: str, clip_ids: list[str]) -> dict[str, Any]:
        with self._lock:
            wave = self._require(self.waves, wave_id, "上线波次")
            if wave.status != "已上线":
                raise DomainError("波次已撤回，不能继续加入片段")
            for cid in clip_ids:
                self._release_clip(cid, wave)
            return wave.to_dict()

    def _release_clip(self, clip_id: str, wave: ReleaseWave) -> None:
        clip = self._require(self.clips, clip_id, "片段")
        perms = self.effective_permissions(clip_id, wave.released_at)["granted"]
        if not perms[PERM_PUBLIC_LISTEN]:
            raise DomainError(f"片段 {clip_id} 未获公众收听授权，不能上线")
        if not clip.published_file_id:
            clip.published_file_id = clip.source_file_id
        if clip_id not in wave.clip_ids:
            wave.clip_ids.append(clip_id)
        clip.releases.append({"wave_id": wave.wave_id, "at": wave.released_at,
                              "status": "已上线"})
        clip.status = "公开开放"
        clip.updated_at = now_ts()

    def withdraw_release(self, wave_id: str, withdrawn_at: Optional[str] = None) -> dict[str]:
        with self._lock:
            wave = self._require(self.waves, wave_id, "上线波次")
            wave.status = "已撤回"
            wave.withdrawn_at = withdrawn_at or now_ts()
            for cid in wave.clip_ids:
                clip = self.clips.get(cid)
                if clip:
                    clip.releases.append({"wave_id": wave.wave_id,
                                          "at": wave.withdrawn_at, "status": "已撤回"})
            self._refresh_clip_status(wave.clip_ids)
            return wave.to_dict()

    def public_catalog(self, role: str = ROLE_PUBLIC, at: Optional[str] = None
                       ) -> list[dict[str, Any]]:
        """公众目录：只有“处于已上线波次且当前仍有公众收听权”的片段出现。"""
        at = at or now_ts()
        with self._lock:
            catalog = []
            for clip in self.clips.values():
                wave_id = self._active_wave(clip.clip_id, at)
                perms = self.effective_permissions(clip.clip_id, at)["granted"]
                if not wave_id or not perms[PERM_PUBLIC_LISTEN]:
                    continue
                catalog.append(self._clip_view(clip, role, at, wave_id))
            return sorted(catalog, key=lambda item: item["seq"])

    def _active_wave(self, clip_id: str, at: str) -> Optional[str]:
        latest: Optional[str] = None
        for wave in self.waves.values():
            if clip_id in wave.clip_ids and wave.released_at <= at and wave.status == "已上线":
                latest = wave.wave_id
        return latest

    def _clip_view(self, clip: Clip, role: str, at: str, wave_id: Optional[str]) -> dict[str, Any]:
        perms = self.effective_permissions(clip.clip_id, at)
        version = self._latest_version(clip.clip_id, published_only=True)
        data = clip.to_dict()
        data["effective_permissions"] = perms
        data["wave_id"] = wave_id
        data["speakers"] = [self.speakers[s].view(role, at) for s in clip.speaker_ids
                            if s in self.speakers]
        data["current_transcript_version"] = version.version_no if version else None
        if role != ROLE_LIBRARIAN:
            data.pop("agreement_ids", None)
            data.pop("releases", None)
        return data

    # ----- 转写：版本、纠错、引用 ----------------------------------------

    def save_transcript(self, clip_id: str, lines: list[dict[str, Any]],
                        created_by: str, status: str = "待核查",
                        basis: str = "", supersedes: Optional[bool] = None
                        ) -> dict[str, Any]:
        """保存转写版本；已发布版本不可改，新内容一律成为新版本。"""
        if status not in TRANSCRIPT_STATUS:
            raise DomainError(f"转写状态应为 {TRANSCRIPT_STATUS} 之一")
        with self._lock:
            clip = self._require(self.clips, clip_id, "片段")
            if not lines:
                raise DomainError("转写至少包含一行")
            parsed: list[TranscriptLine] = []
            prev_end = -1.0
            for index, raw in enumerate(lines, start=1):
                start, end = parse_tc(raw["start_tc"]), parse_tc(raw["end_tc"])
                if start >= end:
                    raise DomainError(f"第 {index} 行结束时间码必须晚于开始时间码")
                if start < prev_end:
                    raise DomainError(f"第 {index} 行时间码与前一行重叠/倒退")
                speaker_id = raw.get("speaker_id")
                if speaker_id and speaker_id not in self.speakers:
                    raise DomainError(f"第 {index} 行引用了不存在的说话人：{speaker_id}")
                parsed.append(TranscriptLine(index, raw["text"], raw["start_tc"],
                                             raw["end_tc"], speaker_id))
                prev_end = end
            versions = self._clip_versions(clip_id)
            latest = versions[-1] if versions else None
            if supersedes is False and latest is not None:
                raise DomainError("该片段已有转写版本，修订必须以新版本取代旧版")
            version = TranscriptVersion(
                clip_id, (latest.version_no + 1) if latest else 1, parsed,
                status=status, created_by=created_by, basis=basis,
                supersedes_version=latest.version_no if latest else None,
                published_at=now_ts() if status == "已发布" else None,
            )
            versions.append(version)
            clip.updated_at = now_ts()
            return version.to_dict()

    def publish_transcript(self, clip_id: str, version_no: int, published_by: str,
                           basis: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            self._require(self.clips, clip_id, "片段")
            version = self._find_version(clip_id, version_no)
            if version.status == "已发布":
                raise DomainError("该版本已发布")
            version.status = "已发布"
            version.published_at = now_ts()
            if basis:
                version.basis = basis
            # 旧版保留为已撤回态，历史引用仍按旧版解析
            for older in self._clip_versions(clip_id):
                if older is not version and older.status == "已发布":
                    older.status = "已撤回"
            return version.to_dict()

    def get_transcript(self, clip_id: str, version_no: Optional[int] = None,
                       role: str = ROLE_PUBLIC) -> dict[str, Any]:
        with self._lock:
            self._require(self.clips, clip_id, "片段")
            if version_no is None:
                version = self._latest_version(clip_id, published_only=True)
                if version is None:
                    raise DomainError("该片段暂无已发布转写")
            else:
                version = self._find_version(clip_id, version_no)
                if version.status != "已发布" and role != ROLE_LIBRARIAN:
                    raise DomainError("该版本尚未发布，仅限馆员查看")
            data = version.to_dict()
            if role != ROLE_LIBRARIAN:
                data.pop("basis", None)
            return data

    def _find_version(self, clip_id: str, version_no: int) -> TranscriptVersion:
        for version in self._clip_versions(clip_id):
            if version.version_no == version_no:
                return version
        raise DomainError(f"片段 {clip_id} 不存在转写版本 v{version_no}")

    def submit_correction(self, clip_id: str, submitter: str,
                          payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require(self.clips, clip_id, "片段")
            correction = Correction(_new_id("cor"), clip_id, submitter, dict(payload))
            self.corrections[correction.correction_id] = correction
            return correction.to_dict()

    def review_correction(self, correction_id: str, reviewer: str, approve: bool,
                          resolution_note: str = "",
                          new_lines: Optional[list[dict[str, Any]]] = None,
                          publish: bool = True) -> dict[str, Any]:
        """馆员核查纠错：采纳则据依据发布新转写版；驳回则说明理由。"""
        with self._lock:
            correction = self._require(self.corrections, correction_id, "纠错")
            if correction.status != "待核查":
                raise DomainError("该纠错已完成核查")
            correction.reviewed_by = reviewer
            correction.reviewed_at = now_ts()
            correction.resolution_note = resolution_note
            if not approve:
                correction.status = "已驳回"
                return correction.to_dict()
            correction.status = "已采纳"
            if new_lines is not None:
                draft = self.save_transcript(
                    correction.clip_id, new_lines, created_by=reviewer,
                    status="待核查",
                    basis=f"采纳纠错 {correction.correction_id}：{resolution_note}",
                )
                correction.resulting_version_no = draft["version_no"]
                if publish:
                    self.publish_transcript(
                        correction.clip_id, draft["version_no"], reviewer,
                        basis=f"采纳纠错 {correction.correction_id}：{resolution_note}",
                    )
            return correction.to_dict()

    def list_corrections(self, clip_id: Optional[str] = None,
                         status: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                c.to_dict() for c in self.corrections.values()
                if (clip_id is None or c.clip_id == clip_id)
                and (status is None or c.status == status)
            ]

    # ----- 稳定引用 -------------------------------------------------------

    def mint_citation(self, clip_id: str, version_no: int, line_no: int,
                      created_by: str) -> dict[str, Any]:
        """引用在生成时冻结版本号与时间码。"""
        with self._lock:
            self._require(self.clips, clip_id, "片段")
            version = self._find_version(clip_id, version_no)
            if line_no < 1 or line_no > len(version.lines):
                raise DomainError(f"版本 v{version_no} 不存在第 {line_no} 行")
            line = version.lines[line_no - 1]
            citation = Citation(_new_id("cite"), clip_id, version_no, line_no,
                                line.start_tc, now_ts(), created_by)
            self.citations[citation.citation_id] = citation
            return citation.to_dict()

    def resolve_citation(self, token: str, at: Optional[str] = None) -> dict[str, Any]:
        """解析引用：旧版时间码映射到当前片段位置，已删除行如实标注。"""
        match = _CITE_RE.match(token or "")
        if not match:
            raise DomainError(f"无法识别的引用编号：{token!r}")
        clip_hex, version_no, line_no, _stamp = match.groups()
        clip_id = f"clip_{clip_hex}"
        at = at or now_ts()
        with self._lock:
            clip = self._require(self.clips, clip_id, "片段")
            frozen = self._find_version(clip_id, int(version_no))
            if int(line_no) > len(frozen.lines):
                raise DomainError("引用行在其冻结版本中不存在")
            frozen_line = frozen.lines[int(line_no) - 1]
            current = self._latest_version(clip_id, published_only=True)
            result: dict[str, Any] = {
                "token": token,
                "clip_id": clip_id,
                "frozen": {"version_no": frozen.version_no,
                           "line": frozen_line.to_dict()},
                "resolved_at": at,
                "still_current": False,
                "status": "ok",
            }
            if current is None:
                result["status"] = "no_published_version"
                return result
            result["current_version_no"] = current.version_no
            result["version_changed"] = current.version_no != frozen.version_no
            if int(line_no) <= len(current.lines):
                current_line = current.lines[int(line_no) - 1]
                result["current_line"] = current_line.to_dict()
                # 时间码映射：以片段起点为基准保持同一音频位置
                same_position = (frozen_line.start_tc == current_line.start_tc
                                 or self._maps_to_clip(frozen_line, current_line))
                result["timecode_mapping"] = {
                    "frozen_start_tc": frozen_line.start_tc,
                    "current_start_tc": current_line.start_tc,
                    "same_audio_position": same_position,
                }
                result["still_current"] = (
                    frozen_line.text == current_line.text
                    and frozen_line.start_tc == current_line.start_tc
                )
            else:
                # 新版行被合并/删除：用冻结时间码在当前版本中定位覆盖该位置的行
                target = parse_tc(frozen_line.start_tc)
                covering = [line for line in current.lines
                            if parse_tc(line.start_tc) <= target < parse_tc(line.end_tc)]
                result["status"] = "line_merged_or_removed"
                result["current_line"] = covering[0].to_dict() if covering else None
            return result

    def _maps_to_clip(self, frozen_line: TranscriptLine, current_line: TranscriptLine) -> bool:
        """行文本一致且时间段相同即视为同一声音位置（允许行号重排）。"""
        return (
            frozen_line.text == current_line.text
            and parse_tc(frozen_line.start_tc) == parse_tc(current_line.start_tc)
        )

    # ----- 馆员统一溯源 ---------------------------------------------------

    def trace_clip(self, clip_id: str) -> dict[str, Any]:
        """馆员视图：从片段一路追到原始声段、处理历史、许可版本与考证决定。"""
        with self._lock:
            clip = self._require(self.clips, clip_id, "片段")
            versions = self._clip_versions(clip_id)
            return {
                "clip": clip.to_dict(),
                "carrier": self.carriers[clip.carrier_id].to_dict(),
                "source_provenance": self.provenance_chain(clip.source_file_id),
                "batches": [b.to_dict() for b in self.batches.values()
                            if b.carrier_id == clip.carrier_id],
                "speakers": [self.speakers[s].view(ROLE_LIBRARIAN)
                             for s in clip.speaker_ids if s in self.speakers],
                "agreements": [self.agreements[a].to_dict() for a in clip.agreement_ids],
                "restrictions": [r.to_dict() for r in self.restrictions.values()
                                 if r.clip_id == clip_id],
                "effective_permissions": self.effective_permissions(clip_id),
                "transcript_versions": [v.to_dict() for v in versions],
                "corrections": [c.to_dict() for c in self.corrections.values()
                                if c.clip_id == clip_id],
                "research_decisions": [d.to_dict() for d in self.research.values()
                                       if d.clip_id == clip_id],
                "related_documents": [d.to_dict() for d in self.documents.values()
                                      if clip_id in d.clip_ids],
                "releases": list(clip.releases),
                "usage_records": self.usage_history(clip_id),
                "citations": [c.to_dict() for c in self.citations.values()
                              if c.clip_id == clip_id],
            }

    # ----- 持久化 ---------------------------------------------------------

    def save(self, path: str | Path) -> None:
        with self._lock:
            data = self._dump()
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _dump(self) -> dict[str, Any]:
        return {
            "carriers": [c.to_dict() for c in self.carriers.values()],
            "batches": [b.to_dict() for b in self.batches.values()],
            "files": [f.to_dict() for f in self.files.values()],
            "events": [e.to_dict() for e in self.events.values()],
            "clips": [c.to_dict() for c in self.clips.values()],
            "speakers": [s.to_dict() for s in self.speakers.values()],
            "documents": [d.to_dict() for d in self.documents.values()],
            "agreements": [a.to_dict() for a in self.agreements.values()],
            "restrictions": [r.to_dict() for r in self.restrictions.values()],
            "usages": [u.to_dict() for u in self.usages],
            "research": [d.to_dict() for d in self.research.values()],
            "corrections": [c.to_dict() for c in self.corrections.values()],
            "waves": [w.to_dict() for w in self.waves.values()],
            "citations": [c.to_dict() for c in self.citations.values()],
            "versions": [
                {"clip_id": clip_id, **v.to_dict()}
                for clip_id, versions in self.versions.items() for v in versions
            ],
            "clip_seq": self._clip_seq.value,
        }

    @classmethod
    def load(cls, path: str | Path) -> "OralHistoryService":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        svc = cls()
        svc._load(data)
        return svc

    def _load(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.carriers = {d["carrier_id"]: Carrier(**d) for d in data.get("carriers", [])}
            self.batches = {d["batch_id"]: DigitizationBatch(**d) for d in data.get("batches", [])}
            self.files = {d["file_id"]: AudioFile(**d) for d in data.get("files", [])}
            self.events = {d["event_id"]: ProcessEvent(**d) for d in data.get("events", [])}
            self.clips = {d["clip_id"]: Clip(**d) for d in data.get("clips", [])}
            self.speakers = {d["speaker_id"]: Speaker(**d) for d in data.get("speakers", [])}
            self.documents = {d["document_id"]: Document(**d) for d in data.get("documents", [])}
            self.agreements = {d["agreement_id"]: DonorAgreement(**d)
                               for d in data.get("agreements", [])}
            self.restrictions = {d["restriction_id"]: Restriction(**d)
                                 for d in data.get("restrictions", [])}
            self.usages = [UsageRecord(**{k: v for k, v in d.items() if k != "token"})
                           for d in data.get("usages", [])]
            self.research = {d["decision_id"]: ResearchDecision(**d)
                             for d in data.get("research", [])}
            self.corrections = {d["correction_id"]: Correction(**d)
                                for d in data.get("corrections", [])}
            self.waves = {d["wave_id"]: ReleaseWave(**d) for d in data.get("waves", [])}
            self.citations = {d["citation_id"]: Citation(
                **{k: v for k, v in d.items() if k != "token"})
                for d in data.get("citations", [])}
            self.versions = {}
            for raw in data.get("versions", []):
                lines = [TranscriptLine(**line) for line in raw.get("lines", [])]
                version = TranscriptVersion(
                    clip_id=raw["clip_id"], version_no=raw["version_no"], lines=lines,
                    status=raw["status"], created_by=raw.get("created_by", ""),
                    created_at=raw["created_at"], published_at=raw.get("published_at"),
                    basis=raw.get("basis", ""),
                    supersedes_version=raw.get("supersedes_version"),
                )
                self.versions.setdefault(raw["clip_id"], []).append(version)
            for versions in self.versions.values():
                versions.sort(key=lambda v: v.version_no)
            self._clip_seq = _Seq(data.get("clip_seq", len(self.clips)))


def build_demo_service() -> OralHistoryService:
    """构造一个贯穿全部业务规则的演示服务（供联调与文档使用）。"""

    svc = OralHistoryService()
    carrier = svc.register_carrier("抗美援朝战地采访磁带第3盘", "录音磁带",
                                   "老战士张某家属", "2026-01-10T09:00:00",
                                   note="家属捐赠，含谈话、自述与歌曲")
    cid = carrier["carrier_id"]
    first = svc.digitize(cid, "数字化馆员李某", "2026-01-12T10:00:00",
                         "masters/tape3_batch1.wav", "audio/wav", "sha256:aaaa")
    # 同一载体重复数字化（不同设备复扫）
    second = svc.digitize(cid, "数字化馆员李某", "2026-02-02T10:00:00",
                          "masters/tape3_batch2.wav", "audio/wav", "sha256:bbbb")
    denoised = svc.process("降噪", first["master"]["file_id"], "李某",
                           [{"path": "derived/tape3_clean.wav", "checksum": "sha256:cccc"}],
                           params={"profile": "tape-hiss"})
    migrated = svc.process("格式迁移", denoised["outputs"][0]["file_id"], "李某",
                           [{"path": "derived/tape3_clean.flac", "media_type": "audio/flac",
                             "checksum": "sha256:dddd"}], params={"target": "flac/24"})
    segments = svc.process("切分", migrated["outputs"][0]["file_id"], "编目员王某",
                           [{"path": "segments/clip_a.wav", "checksum": "sha256:1111"},
                            {"path": "segments/clip_b.wav", "checksum": "sha256:2222"},
                            {"path": "segments/clip_c.wav", "checksum": "sha256:3333"}],
                           note="谈话/自述/歌曲三段")
    seg_ids = [f["file_id"] for f in segments["outputs"]]

    spk1 = svc.register_speaker("受访者甲", real_name="张某某",
                                reveal_after="2031-01-01", visibility="馆内可见")
    spk2 = svc.register_speaker("歌者乙", visibility="匿名")  # 姓名尚未解密
    clip_a = svc.create_clip(cid, "渡江战斗回忆谈话", seg_ids[0],
                             "00:00:00.000", "00:03:00.000", [spk1["speaker_id"]])
    clip_b = svc.create_clip(cid, "个人自述片段", seg_ids[1],
                             "00:03:00.000", "00:06:00.000", [spk1["speaker_id"]])
    clip_c = svc.create_clip(cid, "战壕歌曲录音", seg_ids[2],
                             "00:06:00.000", "00:08:30.000", [spk2["speaker_id"]])

    svc.add_related_document("捐赠协议书（家属）", "协议文本", "DOC-2026-007",
                             [clip_a["clip_id"], clip_b["clip_id"], clip_c["clip_id"]])
    svc.add_research_decision(clip_a["clip_id"], "录制年代", "初步判断为1953年",
                              "依据谈话中提及的停战消息与磁带型号", "考证员赵某",
                              status="存疑")

    # 许可：谈话与自述可公开收听+引用；歌曲仅馆内（版权另议）；均不可下载
    svc.add_agreement("老战士张某家属", [clip_a["clip_id"], clip_b["clip_id"]],
                      {PERM_PUBLIC_LISTEN: True, PERM_ONSITE_LISTEN: True,
                       PERM_CITE: True, PERM_DOWNLOAD: False},
                      note="谈话与自述按协议公开", signed_at="2026-02-20T10:00:00")
    svc.add_agreement("老战士张某家属", [clip_c["clip_id"]],
                      {PERM_PUBLIC_LISTEN: False, PERM_ONSITE_LISTEN: True,
                       PERM_CITE: False, PERM_DOWNLOAD: False},
                      note="歌曲片段仅限馆内使用", signed_at="2026-02-20T10:05:00")

    v1 = svc.save_transcript(clip_a["clip_id"], [
        {"start_tc": "00:00:00.000", "end_tc": "00:00:10.000",
         "text": "那天夜里我们渡过了江。", "speaker_id": spk1["speaker_id"]},
        {"start_tc": "00:00:10.000", "end_tc": "00:00:22.500",
         "text": "炮火很密，但船没有停。", "speaker_id": spk1["speaker_id"]},
    ], created_by="编目员王某", status="已发布", basis="初编")

    svc.create_release_wave("第一批上线", "2026-03-01T09:00:00",
                            [clip_a["clip_id"], clip_b["clip_id"]])
    return svc
