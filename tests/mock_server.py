#!/usr/bin/env python3
"""
本地 mock 的 OpenAI 兼容服务，用于离线验证 mini-agent 的 SSE 与非流式链路。
用法: python3 tests/mock_server.py [port]   （默认 18923）
处理完 2 个请求后自动退出；15 秒安全超时兜底。
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    count = 0

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def do_POST(self):
        Handler.count += 1
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            req = json.loads(body.decode("utf-8"))
        except Exception:
            self._send_error(400, "invalid json body")
            return

        if not req.get("model") or not isinstance(req.get("messages"), list) or not req["messages"]:
            self._send_error(400, "missing model or messages")
            return

        stream = bool(req.get("stream", False))
        if stream:
            self._send_sse(req)
        else:
            self._send_plain(req)

        if Handler.count >= 2:
            threading.Timer(0.2, os._exit, args=(0,)).start()

    def _send_error(self, code, message):
        payload = json.dumps({"error": {"message": message, "code": code}}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_sse(self, req):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
            {"choices": [{"delta": {"reasoning_content": "(思考: 已收到问题，准备回答) "}}]},
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {"content": "，我是 mock 助手。"}}]},
            {"choices": [{"delta": {"content": "转义测试: \"引号\" \\ 换行\n"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        for chunk in chunks:
            self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_plain(self, req):
        resp = {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "这是非流式 mock 回答。"},
                    "finish_reason": "stop",
                }
            ]
        }
        payload = json.dumps(resp, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18923
    threading.Timer(15, os._exit, args=(2,)).start()  # 安全超时
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
