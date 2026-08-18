#!/usr/bin/env python3
"""探测火山方舟接入点：对比 config.ini 中的 endpoint 与标准 OpenAI 兼容端点。
只打印 HTTP 状态码与响应片段，不打印密钥。"""
import json
import urllib.error
import urllib.request

# 从 config.ini 读取第一个非 env: 的 api_key（不打印）
key = None
for line in open("config.ini", encoding="utf-8"):
    s = line.strip()
    if s.lower().startswith("api_key") and "=" in s and not s.startswith("#"):
        v = s.split("=", 1)[1].strip()
        if v and not v.startswith("env:"):
            key = v
            break
if not key:
    raise SystemExit("config.ini 中未找到可用 api_key")

body = json.dumps({
    "model": "doubao-seed-2.0-lite",
    "messages": [{"role": "user", "content": "ping"}],
    "stream": False,
}).encode()

for url in [
    "https://ark.cn-beijing.volces.com/api/coding",
    "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
]:
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(url, "-> HTTP", r.status)
            print(r.read(300).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        print(url, "-> HTTP", e.code)
        print(e.read(300).decode("utf-8", "replace"))
    except Exception as e:
        print(url, "-> ERROR", type(e).__name__, str(e)[:200])
    print()
