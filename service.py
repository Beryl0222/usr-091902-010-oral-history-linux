"""口述史录音开放的 HTTP 服务入口。

在原有健康检查之外，把 ``domain.OralHistoryService`` 的能力暴露为 JSON 接口：

公众（默认）                 馆员（请求头 ``X-Role: librarian``）
─────────────────────────────────────────────────────────────────────────
GET  /health                 （共用）健康检查
GET  /catalog                公众目录：仅已上线且当前仍可公开收听的片段
GET  /clips/{id}           片段视图（馆员可见许可/波次等内部字段）
GET  /transcripts/{cid}      已发布转写（馆员可读历史版本与待核查版）
GET  /citations/resolve     稳定引用解析（旧时间码继续可解析）
POST /clips/{id}/access     访问利用判定与留痕
POST /clips/{id}/corrections 听众提交纠错（先入核查）
                             以下管理接口仅馆员可用：
                             POST /carriers, /carriers/{id}/digitize
                             POST /files/{id}/process
                             GET  /files/{id}/provenance
                             POST /speakers, /clips, /documents
                             POST /clips/{id}/research
                             POST /agreements, /clips/{id}/restrictions
                             POST /restrictions/{id}/lift
                             POST /waves, /waves/{id}/clips, /waves/{id}/withdraw
                             POST /clips/{id}/transcripts
                             POST /transcripts/{cid}/{ver}/publish
                             POST /corrections/{id}/review
                             POST /clips/{id}/citations
                             GET  /corrections, /clips/{id}/usage
                             GET  /librarian/clips, /librarian/clips/{id}/trace
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from domain import (
    DomainError,
    OralHistoryService,
    PERM_PUBLIC_LISTEN,
    ROLE_LIBRARIAN,
    ROLE_PUBLIC,
    build_demo_service,
    now_ts,
)

SERVICE_ID = "oral-history"
SERVICE_NAME = "口述史录音开放"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def make_handler(service: OralHistoryService) -> type:
    """构造绑定指定领域服务实例的 Handler 类（便于测试隔离）。"""

    class Handler(BaseHTTPRequestHandler):
        """JSON API：GET 为读接口，POST 为写接口。"""

        svc = service

        # -- 基础框架 ----------------------------------------------------

        def _send_json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._send_json({"error": message, "status": status}, status)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise DomainError(f"请求体不是合法 JSON：{exc}")
            if not isinstance(data, dict):
                raise DomainError("请求体必须是 JSON 对象")
            return data

        def _role(self) -> str:
            role = self.headers.get("X-Role", ROLE_PUBLIC)
            if role not in (ROLE_PUBLIC, ROLE_LIBRARIAN):
                raise DomainError(f"未知角色：{role}")
            return role

        def _require_librarian(self) -> str:
            role = self._role()
            if role != ROLE_LIBRARIAN:
                raise DomainError("该接口仅限馆员使用")
            return role

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                self.route_get(parsed.path, parse_qs(parsed.query))
            except DomainError as exc:
                status = 404 if "不存在" in str(exc) else 400
                self._error(status, str(exc))

        def do_POST(self):
            try:
                parsed = urlparse(self.path)
                self.route_post(parsed.path)
            except DomainError as exc:
                status = 404 if "不存在" in str(exc) else 400
                self._error(status, str(exc))

        def log_message(self, *_args):
            return

        # -- GET 路由 -----------------------------------------------------

        def route_get(self, path: str, query: dict) -> None:
            if path == "/health":
                self._send_json(health_payload())
                return
            if path == "/catalog":
                at = query.get("at", [None])[0]
                self._send_json({"clips": self.svc.public_catalog(ROLE_PUBLIC, at)})
                return
            if path == "/librarian/clips":
                self._require_librarian()
                at = query.get("at", [None])[0] or now_ts()
                self._send_json({
                    "clips": [
                        self.svc._clip_view(self.svc.clips[cid], ROLE_LIBRARIAN,
                                             at, self.svc._active_wave(cid, at))
                        for cid in sorted(self.svc.clips,
                                           key=lambda c: self.svc.clips[c].seq)
                    ]
                })
                return

            match = re.fullmatch(r"/clips/([^/]+)", path)
            if match:
                clip_id = match.group(1)
                self._send_json(self._clip_public_view(clip_id))
                return

            match = re.fullmatch(r"/librarian/clips/([^/]+)/trace", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.trace_clip(match.group(1)))
                return

            match = re.fullmatch(r"/files/([^/]+)/provenance", path)
            if match:
                self._require_librarian()
                self._send_json({"chain": self.svc.provenance_chain(match.group(1))})
                return

            match = re.fullmatch(r"/transcripts/([^/]+)", path)
            if match:
                clip_id = match.group(1)
                version = query.get("version", [None])[0]
                version_no = int(version) if version else None
                self._send_json(self.svc.get_transcript(
                    clip_id, version_no, role=self._role()))
                return

            match = re.fullmatch(r"/citations/resolve", path)
            if match:
                token = query.get("token", [None])[0]
                self._send_json(self.svc.resolve_citation(token))
                return

            match = re.fullmatch(r"/corrections", path)
            if match:
                self._require_librarian()
                clip_id = query.get("clip_id", [None])[0]
                status = query.get("status", [None])[0]
                self._send_json({"corrections": self.svc.list_corrections(clip_id, status)})
                return

            match = re.fullmatch(r"/clips/([^/]+)/usage", path)
            if match:
                self._require_librarian()
                self._send_json({"usage": self.svc.usage_history(match.group(1))})
                return

            match = re.fullmatch(r"/clips/([^/]+)/permissions", path)
            if match:
                clip_id = match.group(1)
                at = query.get("at", [None])[0]
                self._send_json(self.svc.effective_permissions(clip_id, at))
                return

            self._error(404, f"未知路径：{path}")

        def _clip_public_view(self, clip_id: str) -> dict:
            at = now_ts()
            clip = self.svc.clips.get(clip_id)
            if clip is None:
                raise DomainError(f"片段不存在：{clip_id}")
            role = self._role()
            wave_id = self.svc._active_wave(clip_id, at)
            if role != ROLE_LIBRARIAN:
                perms = self.svc.effective_permissions(clip_id, at)["granted"]
                if not wave_id or not perms[PERM_PUBLIC_LISTEN]:
                    # 不透露未上线片段的任何内容
                    raise DomainError(f"片段不存在或未开放：{clip_id}")
            return self.svc._clip_view(clip, role, at, wave_id)

        # -- POST 路由 ----------------------------------------------------

        def route_post(self, path: str, data: dict = None) -> None:
            data = data if data is not None else self._body()

            if path == "/carriers":
                self._require_librarian()
                self._send_json(self.svc.register_carrier(
                    data["title"], data["carrier_type"], data["donor"],
                    data["received_at"], data.get("note", "")), 201)
                return

            match = re.fullmatch(r"/carriers/([^/]+)/digitize", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.digitize(
                    match.group(1), data["operator"], data["captured_at"],
                    data["master_path"], data["media_type"], data["checksum"],
                    data.get("note", "")), 201)
                return

            match = re.fullmatch(r"/files/([^/]+)/process", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.process(
                    data["kind"], match.group(1), data["operator"],
                    data["outputs"], data.get("params"), data.get("note", "")), 201)
                return

            if path == "/speakers":
                self._require_librarian()
                self._send_json(self.svc.register_speaker(
                    data["pseudonym"], data.get("real_name"),
                    data.get("reveal_after"),
                    data.get("visibility", "匿名"), data.get("note", "")), 201)
                return

            if path == "/clips":
                self._require_librarian()
                self._send_json(self.svc.create_clip(
                    data["carrier_id"], data["title"], data["source_file_id"],
                    data["start_tc"], data["end_tc"], data.get("speaker_ids")), 201)
                return

            if path == "/documents":
                self._require_librarian()
                self._send_json(self.svc.add_related_document(
                    data["title"], data["kind"], data["ref"],
                    data.get("clip_ids"), data.get("note", "")), 201)
                return

            match = re.fullmatch(r"/clips/([^/]+)/research", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.add_research_decision(
                    match.group(1), data["topic"], data["conclusion"],
                    data["basis"], data["decided_by"],
                    data.get("status", "待考")), 201)
                return

            if path == "/agreements":
                self._require_librarian()
                self._send_json(self.svc.add_agreement(
                    data["donor"], data["clip_ids"], data["grants"],
                    data.get("note", ""), data.get("signed_at")), 201)
                return

            match = re.fullmatch(r"/clips/([^/]+)/restrictions", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.add_restriction(
                    match.group(1), data["denied_perms"], data["reason"],
                    data["issued_by"], data.get("effective_at")), 201)
                return

            match = re.fullmatch(r"/restrictions/([^/]+)/lift", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.lift_restriction(
                    match.group(1), data.get("lifted_at")))
                return

            match = re.fullmatch(r"/clips/([^/]+)/access", path)
            if match:
                clip_id = match.group(1)
                self._send_json(self.svc.request_access(
                    clip_id, data["actor"], self._role(), data["perm"],
                    data.get("at"), data.get("detail", "")))
                return

            if path == "/waves":
                self._require_librarian()
                self._send_json(self.svc.create_release_wave(
                    data["label"], data["released_at"], data["clip_ids"]), 201)
                return

            match = re.fullmatch(r"/waves/([^/]+)/clips", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.add_clips_to_wave(
                    match.group(1), data["clip_ids"]))
                return

            match = re.fullmatch(r"/waves/([^/]+)/withdraw", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.withdraw_release(
                    match.group(1), data.get("withdrawn_at")))
                return

            match = re.fullmatch(r"/clips/([^/]+)/transcripts", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.save_transcript(
                    match.group(1), data["lines"], data["created_by"],
                    data.get("status", "待核查"), data.get("basis", ""),
                    data.get("supersedes")), 201)
                return

            match = re.fullmatch(r"/transcripts/([^/]+)/(\d+)/publish", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.publish_transcript(
                    match.group(1), int(match.group(2)), data["published_by"],
                    data.get("basis")))
                return

            match = re.fullmatch(r"/clips/([^/]+)/corrections", path)
            if match:
                # 提交纠错面向公众；是否馆员由提交人身份数据决定，不做权限拦截
                self._send_json(self.svc.submit_correction(
                    match.group(1), data["submitter"], data["payload"]), 201)
                return

            match = re.fullmatch(r"/corrections/([^/]+)/review", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.review_correction(
                    match.group(1), data["reviewer"], bool(data["approve"]),
                    data.get("resolution_note", ""), data.get("new_lines"),
                    data.get("publish", True)))
                return

            match = re.fullmatch(r"/clips/([^/]+)/citations", path)
            if match:
                self._require_librarian()
                self._send_json(self.svc.mint_citation(
                    match.group(1), int(data["version_no"]),
                    int(data["line_no"]), data["created_by"]), 201)
                return

            self._error(404, f"未知路径：{path}")

    return Handler


# 默认服务：演示数据（可被 --data 指定的持久化文件覆盖）
default_service = build_demo_service()
Handler = make_handler(default_service)


def make_server(service: OralHistoryService, port: int) -> ThreadingHTTPServer:
    handler_cls = make_handler(service)
    return ThreadingHTTPServer(("0.0.0.0", port), handler_cls)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--data", help="JSON 持久化文件路径；不存在时以演示数据启动")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        demo = build_demo_service()
        assert len(demo.carriers) >= 1 and len(demo.clips) >= 1
        print("基础检查通过")
        return
    if args.data:
        from pathlib import Path
        data_path = Path(args.data)
        service = OralHistoryService.load(data_path) if data_path.exists() \
            else build_demo_service()
    else:
        data_path = None
        service = default_service
    server = make_server(service, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # 退出时把运行期间的写入原子落盘（仅在指定 --data 时）
        if data_path is not None:
            tmp = data_path.with_suffix(data_path.suffix + ".tmp")
            service.save(tmp)
            tmp.replace(data_path)
        server.server_close()


if __name__ == "__main__":
    main()
