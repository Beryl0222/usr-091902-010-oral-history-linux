"""口述史录音开放的领域模型。

分层结构（每层只通过标识引用上层，便于独立修订）：

    载体 Carrier
      └─ 数字化批次 DigitizationBatch（同一载体允许多次重复数字化）
            └─ 音频文件 AudioFile（母版 master / 衍生 derivative，只增不改）
                  └─ 音频片段 AudioSegment（切分操作的来源记录）
                        ├─ 转写版本 TranscriptionVersion → 转写行 TranscriptionLine
                        ├─ 许可版本 LicenseVersion（捐赠协议按片段限定）
                        ├─ 纠错 Correction / 考证决定 ResearchDecision / 年代修订 DatingRevision
                        └─ 发布批次 ReleaseBatch（分次上线）

关联实体：说话人 Speaker（支持延迟解密）、引用 Citation（旧时间码可解析）、
关联文献 Document、利用记录 AccessRecord（只增不改的审计日志）。

词表与 fixtures/domain.json 保持一致；状态与行为的中文名用于接口展示，
代码内部使用英文常量，二者在本模块集中对应。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 常量词表
# ---------------------------------------------------------------------------

# 文件类别
KIND_MASTER = "master"        # 母版：数字化批次的直接产物
KIND_DERIVATIVE = "derivative"  # 衍生：由既有文件经处理操作产生

# 处理操作（对应 fixtures 的“处理操作”：降噪 / 格式迁移；切分由片段记录承载）
OP_DENOISE = "denoise"
OP_MIGRATE = "migrate"
DERIVATIVE_OPERATIONS = (OP_DENOISE, OP_MIGRATE)
OPERATION_LABELS = {OP_DENOISE: "降噪", OP_MIGRATE: "格式迁移", "cut": "切分"}

# 许可行为（对应 fixtures 的“许可行为”：公开收听 / 馆内收听 / 引用 / 下载）
ACT_PUBLIC_LISTEN = "public_listen"
ACT_LIBRARY_LISTEN = "library_listen"
ACT_CITE = "cite"
ACT_DOWNLOAD = "download"
LICENSE_TERMS = (ACT_PUBLIC_LISTEN, ACT_LIBRARY_LISTEN, ACT_CITE, ACT_DOWNLOAD)
TERM_LABELS = {
    ACT_PUBLIC_LISTEN: "公开收听",
    ACT_LIBRARY_LISTEN: "馆内收听",
    ACT_CITE: "引用",
    ACT_DOWNLOAD: "下载",
}
# 馆员工作访问（不占用捐赠协议授权，但同样记入利用记录）
ACT_INTERNAL_READ = "internal_read"
ACCESS_ACTIONS = LICENSE_TERMS + (ACT_INTERNAL_READ,)

# 参与角色（与 fixtures 的“参与角色”一致，另增馆内读者与系统）
ROLE_PUBLIC = "公众读者"
ROLE_READER = "馆内读者"
ROLE_DIGITIZER = "数字化馆员"
ROLE_CATALOGER = "编目员"
ROLE_DONOR = "捐赠方"
STAFF_ROLES = (ROLE_DIGITIZER, ROLE_CATALOGER)

# 纠错状态（与 fixtures 的“纠错状态”一致）
CORRECTION_PENDING = "待核查"
CORRECTION_ADOPTED = "已采纳"
CORRECTION_REJECTED = "已驳回"

# 考证类型（与 fixtures 的“考证类型”一致）
DECISION_DATING = "年代考证"
DECISION_SPEAKER = "人名考证"
DECISION_CONTENT = "内容校注"
DECISION_KINDS = (DECISION_DATING, DECISION_SPEAKER, DECISION_CONTENT)

# 载体 / 片段状态（与 fixtures 的“参考状态”一致）
STATUS_PENDING_DIGITIZATION = "待数字化"
STATUS_PENDING_CATALOGING = "待编目"
STATUS_RESTRICTED = "限制开放"
STATUS_PUBLIC = "公开开放"
STATUS_SEALED = "已封存"


# ---------------------------------------------------------------------------
# 实体
# ---------------------------------------------------------------------------


@dataclass
class Carrier:
    """载体：一盘磁带等物理对象，数字化谱系的根。"""

    id: str
    label: str            # 馆藏编号 / 题名，如“磁带 A-17”
    medium: str           # 载体类型：开盘磁带 / 盒式磁带 / 钢丝带…
    donor: str            # 捐赠方（老战士、研究者、将领后代…）
    donated_at: str
    status: str = STATUS_PENDING_DIGITIZATION
    note: str = ""


@dataclass
class DigitizationBatch:
    """数字化批次：一次数字化作业。同一载体可分批重复数字化。"""

    id: str
    carrier_id: str
    operator: str         # 数字化馆员
    equipment: str
    started_at: str
    note: str = ""


@dataclass
class AudioFile:
    """音频文件。母版不可覆盖；衍生品记录来源文件与登记时的来源校验和。

    content 为模拟音频内容，duration_ms 由其长度推导（1 字节 = 1 毫秒），
    使切分范围校验可以在无真实音频的条件下进行。
    """

    id: str
    kind: str                      # KIND_MASTER / KIND_DERIVATIVE
    checksum: str                  # 内容 SHA-256
    duration_ms: int
    content: bytes
    batch_id: str | None = None        # 母版：所属数字化批次
    source_file_id: str | None = None  # 衍生品：来源文件
    source_checksum: str | None = None  # 衍生品：登记时的来源校验和
    operation: str | None = None       # 衍生品：denoise / migrate
    params: dict = field(default_factory=dict)  # 处理参数（如降噪强度、目标格式）
    created_by: str = ""
    created_at: str = ""


@dataclass
class AudioSegment:
    """音频片段：对某一音频文件 [start_ms, end_ms) 的切分。

    片段本身即“切分”操作的来源记录；片段一经登记不再修改，
    重新切分会产生新片段。转写与引用的时间码均以片段起点为 0。
    """

    id: str
    file_id: str
    start_ms: int
    end_ms: int
    title: str
    speaker_ids: list[str]
    status: str = STATUS_PENDING_CATALOGING
    note: str = ""


@dataclass
class Speaker:
    """说话人。real_name 在 declassify_at 之前不对公众公开（延迟解密）。"""

    id: str
    display_name: str                 # 公开用名（化名或“未公开”）
    real_name: str | None = None
    declassify_at: str | None = None  # ISO 日期（YYYY-MM-DD）；None 表示无限期保密
    note: str = ""


@dataclass
class TranscriptionVersion:
    """转写版本。每次采纳纠错或依据考证决定修订即产生新版本，旧版本保留。"""

    id: str
    segment_id: str
    number: int
    basis: list[str]                  # 依据："initial" / 纠错编号 / 考证决定编号
    created_by: str
    created_at: str
    supersedes: str | None = None


@dataclass
class TranscriptionLine:
    """转写行。时间码以片段起点为 0，与音频位置一一对应。"""

    id: str
    version_id: str
    seq: int
    start_ms: int
    end_ms: int
    text: str
    speaker_id: str | None = None


@dataclass
class LicenseVersion:
    """捐赠协议在某片段上的一个版本。权利人补充限制即产生新版本，
    新版本立即约束此后的访问；旧版本保留，用于佐证此前的合法利用。"""

    id: str
    segment_id: str
    number: int
    terms: dict                       # {行为: bool}，键取自 LICENSE_TERMS
    note: str
    created_by: str
    effective_at: str


@dataclass
class AccessRecord:
    """利用记录：每次访问请求（无论准许或拒绝）的审计条目，只增不改。"""

    id: str
    segment_id: str
    action: str
    actor: str
    actor_role: str
    granted: bool
    reason: str
    license_version_id: str | None    # 评估时使用的许可版本
    at: str


@dataclass
class Correction:
    """听众纠错。先进入核查，馆员采纳后产生有依据的新转写版本。"""

    id: str
    segment_id: str
    line_id: str | None               # 目标转写行（采纳时自动套用）
    start_ms: int | None              # 建议的起始时间码（可选）
    end_ms: int | None
    proposed_text: str
    reason: str
    submitted_by: str
    status: str = CORRECTION_PENDING
    reviewed_by: str | None = None
    review_note: str = ""
    resulting_version_id: str | None = None


@dataclass
class ResearchDecision:
    """考证决定：年代、人名、内容的考证结论，可作为转写版本的依据。"""

    id: str
    segment_id: str
    kind: str                         # DECISION_KINDS 之一
    conclusion: str
    rationale: str
    decided_by: str
    at: str


@dataclass
class DatingRevision:
    """片段年代的一次修订（年代考证可持续修订，历史全部保留）。"""

    id: str
    segment_id: str
    date_range: str                   # 如 “1938—1940”
    rationale: str
    decision_id: str | None
    revised_by: str
    at: str


@dataclass
class ReleaseBatch:
    """发布批次：首批录音分次上线，公众仅可见已发布且获许可的片段。"""

    id: str
    name: str
    segment_ids: list[str]
    released_at: str


@dataclass
class Citation:
    """稳定引用：锁定片段、转写版本与时间码区间。

    即使该版本被新版本取代，引用仍可解析到原版本的转写行与音频位置，
    并给出当前版本中时间重叠的对应行。
    """

    id: str
    segment_id: str
    version_id: str
    start_ms: int
    end_ms: int
    created_by: str
    created_at: str


@dataclass
class Document:
    """关联文献：与片段互相印证的图书、档案、照片等。"""

    id: str
    title: str
    kind: str                         # 图书 / 档案 / 照片 / 论文…
    reference: str                    # 索书号 / 档号 / 链接
    segment_ids: list[str] = field(default_factory=list)
