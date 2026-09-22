"""存储层：内存仓库 + JSON 快照。

不变量由结构保证：
- 音频文件与利用记录只增不改——仓库不提供更新或删除方法，
  重复写入同一标识会被拒绝，因此母版与审计日志不可覆盖；
- 快照可整体往返（含音频内容的 base64 编码），供服务进程持久化。
"""

from __future__ import annotations

import base64
from dataclasses import asdict

from . import model


class StoreError(Exception):
    """存储层冲突（如重复标识）。"""


# 集合名 → (实体类型, ID 前缀)
COLLECTIONS = {
    "carriers": (model.Carrier, "car"),
    "batches": (model.DigitizationBatch, "bat"),
    "files": (model.AudioFile, "fil"),
    "segments": (model.AudioSegment, "seg"),
    "speakers": (model.Speaker, "spk"),
    "versions": (model.TranscriptionVersion, "tvr"),
    "lines": (model.TranscriptionLine, "lin"),
    "licenses": (model.LicenseVersion, "lic"),
    "access_records": (model.AccessRecord, "acc"),
    "corrections": (model.Correction, "cor"),
    "decisions": (model.ResearchDecision, "dec"),
    "datings": (model.DatingRevision, "dat"),
    "releases": (model.ReleaseBatch, "rel"),
    "citations": (model.Citation, "cit"),
    "documents": (model.Document, "doc"),
}


class Store:
    """按集合存放全部领域记录的内存仓库。"""

    def __init__(self):
        for name in COLLECTIONS:
            setattr(self, name, {})
        self._counters = {}

    def new_id(self, collection: str) -> str:
        """为集合分配单调递增标识，如 seg-0003。"""
        prefix = COLLECTIONS[collection][1]
        number = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = number
        return f"{prefix}-{number:04d}"

    def add(self, collection: str, record):
        """登记一条新记录；标识重复即拒绝（只增不改）。"""
        bucket = getattr(self, collection)
        if record.id in bucket:
            raise StoreError(f"{collection} 中已存在标识 {record.id}")
        bucket[record.id] = record
        return record

    # -- 快照 ------------------------------------------------------------

    def to_snapshot(self) -> dict:
        """导出为可 JSON 序列化的字典。"""
        data = {"counters": dict(self._counters)}
        for name in COLLECTIONS:
            rows = []
            for record in getattr(self, name).values():
                row = asdict(record)
                if isinstance(record, model.AudioFile):
                    row["content"] = base64.b64encode(record.content).decode("ascii")
                rows.append(row)
            data[name] = rows
        return data

    @classmethod
    def from_snapshot(cls, data: dict) -> "Store":
        store = cls()
        for name, (entity, _prefix) in COLLECTIONS.items():
            for row in data.get(name, []):
                row = dict(row)
                if entity is model.AudioFile:
                    row["content"] = base64.b64decode(row["content"])
                record = entity(**row)
                getattr(store, name)[record.id] = record
        store._counters = dict(data.get("counters", {}))
        return store
