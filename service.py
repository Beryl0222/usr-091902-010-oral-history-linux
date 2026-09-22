"""口述史录音开放的运行入口：健康检查与领域 HTTP API。

- GET  /health                            服务身份（保持稳定）
- GET  /api/catalog/public                公众目录（仅已发布且许可公开收听）
- POST /api/carriers                      登记载体
- POST /api/carriers/{id}/digitize        数字化，生成批次与母版
- POST /api/files/{id}/derive             降噪 / 格式迁移，生成衍生品
- GET  /api/files/{id}/provenance         核验来源链
- POST /api/speakers                      登记说话人（可延迟解密姓名）
- POST /api/speakers/{id}/release-name    设定解密日期
- POST /api/files/{id}/segments           切分片段
- POST /api/segments/{id}/seal            封存片段
- POST /api/documents                     登记关联文献
- POST /api/segments/{id}/licenses        登记许可版本（只能收窄）
- GET  /api/segments/{id}/licenses        许可版本史
- POST /api/segments/{id}/access          访问请求（评估并留利用记录）
- GET  /api/access-records                利用记录（可按片段过滤）
- POST /api/releases                      发布批次（分次上线）
- POST /api/segments/{id}/transcription   初版转写
- POST /api/segments/{id}/corrections     听众提交纠错
- POST /api/corrections/{id}/review       馆员核查（采纳即发布新版）
- GET  /api/corrections                   纠错列表
- POST /api/segments/{id}/decisions       考证决定
- POST /api/segments/{id}/datings         年代修订
- POST /api/segments/{id}/retranscribe    依据考证决定重订转写
- POST /api/segments/{id}/citations       建立稳定引用
- GET  /api/citations/{id}                解析引用（旧版本+当前版本）
- GET  /api/lines/{id}/trace              馆员全链路溯源
"""

import argparse
import base64
import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from oralhistory import Engine, DomainError
from oralhistory.model import AudioFile
from oralhistory.store import Store

SERVICE_ID = "oral-history"
SERVICE_NAME = "口述史录音开放"

# 默认共享引擎（未指定 --data 时使用，进程内有效）
default_engine = Engine(Store())
_engine_lock = threading.Lock()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _jsonable(value):
    """把 dataclass / 字节内容转为可 JSON 序列化的形式。"""
    if isinstance(value, AudioFile):
        data = asdict(value)
        data.pop("content", None)  # 音频内容不通过接口返回
        return data
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _result(value, status=200):
    return status, _jsonable(value)


class Handler(BaseHTTPRequestHandler):
    """健康检查与领域 API 路由。"""

    def _engine(self) -> Engine:
        return getattr(self.server, "engine", default_engine)

    def _data_path(self):
        return getattr(self.server, "data_path", None)

    # -- 基础读写 --------------------------------------------------------

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DomainError("请求体不是合法的 UTF-8 JSON")
        if not isinstance(data, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return data

    def _persist(self):
        path = self._data_path()
        if path is not None:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self._engine().store.to_snapshot(), handle, ensure_ascii=False)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        query = urlparse(self.path).query
        try:
            if path == "/health":
                self._send(200, health_payload())
                return
            if path == "/api/catalog/public":
                self._send(*_result(self._engine().public_catalog()))
                return
            if path == "/api/corrections":
                params = dict(
                    item.split("=", 1) for item in query.split("&") if "=" in item
                )
                self._send(*_result(self._engine().list_corrections(
                    segment_id=params.get("segment_id"),
                    status=params.get("status"),
                )))
                return
            if path == "/api/access-records":
                params = dict(
                    item.split("=", 1) for item in query.split("&") if "=" in item
                )
                self._send(*_result(self._engine().list_access_records(
                    segment_id=params.get("segment_id"),
                )))
                return
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "files"] \
                    and parts[3] == "provenance":
                self._send(*_result(self._engine().verify_provenance(parts[2])))
                return
            if len(parts) == 4 and parts[:2] == ["api", "segments"] \
                    and parts[3] == "licenses":
                self._send(*_result(self._engine().list_licenses(parts[2])))
                return
            if len(parts) == 3 and parts[:2] == ["api", "citations"]:
                self._send(*_result(self._engine().resolve_citation(parts[2])))
                return
            if len(parts) == 4 and parts[:2] == ["api", "lines"] \
                    and parts[3] == "trace":
                self._send(*_result(self._engine().trace_line(parts[2])))
                return
            self._send(404, {"error": f"未找到路由：{path}"})
        except DomainError as error:
            self._send(self._error_status(str(error)), {"error": str(error)})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = path.strip("/").split("/")
        engine = self._engine()
        try:
            body = self._read_body()
            with _engine_lock:
                try:
                    status, payload = self._dispatch(engine, parts, body)
                except KeyError as error:
                    raise DomainError(f"请求缺少必填字段：{error.args[0]}") from error
                self._persist()
            self._send(status, payload)
        except DomainError as error:
            message = str(error)
            self._send(self._error_status(message), {"error": message})

    @staticmethod
    def _error_status(message):
        return 404 if "不存在" in message else 400

    def _dispatch(self, engine: Engine, parts, body):
        """POST 路由分发表。"""
        p = parts

        if p[:2] == ["api", "carriers"]:
            if len(p) == 2:
                return _result(engine.register_carrier(
                    body["label"], body["medium"], body["donor"],
                    body["donated_at"], body.get("note", ""),
                ), 201)
            if len(p) == 4 and p[3] == "digitize":
                content = base64.b64decode(body["content_base64"])
                return _result(engine.digitize(
                    p[2], body["operator"], body["equipment"], content,
                    started_at=body.get("started_at"), note=body.get("note", ""),
                ), 201)

        if p[:2] == ["api", "files"] and len(p) == 4 and p[3] == "derive":
            content = base64.b64decode(body["content_base64"])
            return _result(engine.derive_file(
                p[2], body["operation"], content, body["created_by"],
                params=body.get("params"), at=body.get("at"),
            ), 201)
        if p[:2] == ["api", "files"] and len(p) == 4 and p[3] == "segments":
            return _result(engine.cut_segment(
                p[2], int(body["start_ms"]), int(body["end_ms"]),
                body["title"], speaker_ids=body.get("speaker_ids"),
                note=body.get("note", ""),
            ), 201)

        if p[:2] == ["api", "speakers"]:
            if len(p) == 2:
                return _result(engine.add_speaker(
                    body["display_name"], real_name=body.get("real_name"),
                    declassify_at=body.get("declassify_at"),
                    note=body.get("note", ""),
                ), 201)
            if len(p) == 4 and p[3] == "release-name":
                return _result(engine.schedule_name_release(
                    p[2], body["declassify_at"], body.get("real_name"),
                ))

        if p[:2] == ["api", "documents"] and len(p) == 2:
            return _result(engine.add_document(
                body["title"], body["kind"], body["reference"],
                segment_ids=body.get("segment_ids"),
            ), 201)

        if p[:2] == ["api", "segments"]:
            if len(p) == 4 and p[3] == "seal":
                return _result(engine.seal_segment(p[2]))
            if len(p) == 4 and p[3] == "licenses":
                return _result(engine.add_license(
                    p[2], body["terms"], body["note"], body["created_by"],
                    effective_at=body.get("effective_at"),
                ), 201)
            if len(p) == 4 and p[3] == "access":
                return _result(engine.request_access(
                    p[2], body["action"], body["actor"], body["actor_role"],
                    at=body.get("at"),
                ), 201)
            if len(p) == 4 and p[3] == "transcription":
                return _result(engine.initial_transcription(
                    p[2], body["lines"], body["created_by"], at=body.get("at"),
                ), 201)
            if len(p) == 4 and p[3] == "corrections":
                return _result(engine.submit_correction(
                    p[2], body["proposed_text"], body["reason"],
                    body["submitted_by"], line_id=body.get("line_id"),
                    start_ms=body.get("start_ms"), end_ms=body.get("end_ms"),
                ), 201)
            if len(p) == 4 and p[3] == "decisions":
                return _result(engine.add_research_decision(
                    p[2], body["kind"], body["conclusion"], body["rationale"],
                    body["decided_by"], at=body.get("at"),
                ), 201)
            if len(p) == 4 and p[3] == "datings":
                return _result(engine.revise_dating(
                    p[2], body["date_range"], body["rationale"],
                    body["revised_by"], decision_id=body.get("decision_id"),
                    at=body.get("at"),
                ), 201)
            if len(p) == 4 and p[3] == "retranscribe":
                return _result(engine.revise_transcription_by_decision(
                    p[2], body["lines"], body["decision_id"],
                    body["created_by"], at=body.get("at"),
                ), 201)
            if len(p) == 4 and p[3] == "citations":
                return _result(engine.create_citation(
                    p[2], int(body["start_ms"]), int(body["end_ms"]),
                    body["created_by"], version_id=body.get("version_id"),
                    at=body.get("at"),
                ), 201)

        if p[:2] == ["api", "corrections"] and len(p) == 4 and p[3] == "review":
            return _result(engine.review_correction(
                p[2], bool(body["adopt"]), body["reviewed_by"],
                review_note=body.get("review_note", ""), at=body.get("at"),
            ))

        if p[:2] == ["api", "releases"] and len(p) == 2:
            return _result(engine.release(
                body["name"], body["segment_ids"],
                released_at=body.get("released_at"),
            ), 201)

        raise DomainError(f"未找到 POST 路由：{'/'.join(parts)}")

    def log_message(self, *_args):
        return


def build_server(port, data_path=None):
    """构造带领域引擎（可选快照持久化）的 HTTP 服务。"""
    if data_path:
        try:
            with open(data_path, encoding="utf-8") as handle:
                engine = Engine(Store.from_snapshot(json.load(handle)))
        except FileNotFoundError:
            engine = Engine(Store())
    else:
        engine = default_engine
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.engine = engine
    server.data_path = data_path
    return server


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", help="JSON 快照路径；启动载入、每次写操作后保存")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        from oralhistory import Engine as _Engine
        from oralhistory.store import Store as _Store
        snapshot = _Engine(_Store()).store.to_snapshot()
        assert _Store.from_snapshot(snapshot).to_snapshot() == snapshot
        print("基础检查通过")
        return
    build_server(args.port, args.data).serve_forever()


if __name__ == "__main__":
    main()
