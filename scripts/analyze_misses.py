#!/usr/bin/env python3
"""
analyze_misses.py —— 漏报 badcase 深挖

提取每条漏报（人工=真实，模型=正常/误报）的判断结论与推导逻辑，
标记模型使用的合理化话术（加白/白名单、运维监控叙事、进程链可信等），
并检查原始告警数据中是否本身就含有「加白/处置」类字段（数据泄漏嫌疑），
输出明细到 hids_misses_detail.txt 供人工复盘。

用法:
    python3 scripts/analyze_misses.py <csv文件> [-a hids_answers.md]
        [-r hids_log.txt] [-o hids_misses_detail.txt]
"""
import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from summarize_results import split_answers, extract_conclusion, classify, human_binary

# 模型答案中的合理化话术关键词
RHETORIC = {
    "加白/白名单": r"加白|白名单|whitelist",
    "处置记录佐证": r"处置记录|已处置|人工标记|人工确认",
    "运维/监控/业务叙事": r"运维|监控采集|业务逻辑|DevOps|自动化任务",
    "进程链可信": r"systemd|来源可信|合法进程|官方客户端|可信进程",
    "内网无外联": r"内网合法|无对外|没有对外|无外联",
}
# 原始告警数据中若含这些词，说明"处置状态"可能泄漏进了输入
INPUT_LEAK = re.compile(r"加白|白名单|Whitelist|white_list|disposal|DisposalStatus", re.I)


def extract_section(answer, title):
    m = re.search(r"###\s*" + re.escape(title) + r"\s*\n(.*?)(?=\n### |\Z)", answer, re.S)
    if not m:
        return ""
    return " ".join(line.strip() for line in m.group(1).strip().splitlines())


def main():
    ap = argparse.ArgumentParser(description="漏报 badcase 深挖")
    ap.add_argument("csv_file")
    ap.add_argument("-a", "--answers", default="hids_answers.md")
    ap.add_argument("-r", "--requests", default="hids_log.txt")
    ap.add_argument("-o", "--output", default="hids_misses_detail.txt")
    ap.add_argument("--maxlen", type=int, default=420, help="推导逻辑截断长度")
    args = ap.parse_args()

    with open(args.csv_file, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    with open(args.answers, encoding="utf-8") as f:
        answers = split_answers(f.read())
    reqs = [r.strip() for r in re.split(r"\n{3,}", open(args.requests, encoding="utf-8").read()) if r.strip()]
    if not (len(rows) == len(answers) == len(reqs)):
        print(f"[错误] 数量不一致: csv={len(rows)} answers={len(answers)} requests={len(reqs)}",
              file=sys.stderr)
        sys.exit(1)

    misses = []
    for i, (row, ans, req) in enumerate(zip(rows, answers, reqs), 1):
        concl = extract_conclusion(ans)
        if human_binary((row.get("human_result_cn") or "").strip()) == "入侵" \
                and classify(concl) == "正常/误报":
            misses.append((i, row, ans, concl, req))

    with open(args.output, "w", encoding="utf-8") as out:
        out.write(f"漏报 badcase 明细（共 {len(misses)} 条，人工=真实，模型=正常/误报）\n")
        out.write("=" * 70 + "\n\n")
        n_leak = 0
        rhetoric_count = {k: 0 for k in RHETORIC}
        for i, row, ans, concl, req in misses:
            logic = extract_section(ans, "推导逻辑")[:args.maxlen]
            flags = [name for name, pat in RHETORIC.items() if re.search(pat, ans)]
            for name in flags:
                rhetoric_count[name] += 1
            input_leak = bool(INPUT_LEAK.search(req))
            if input_leak:
                n_leak += 1
            out.write(f"===== #{i:02d} alarm_id={row['alarm_id']} rule={row['rule_name']} =====\n")
            out.write(f"[模型结论] {concl}\n")
            out.write(f"[话术标记] {', '.join(flags) or '无'}\n")
            out.write(f"[原始数据含加白/处置字段] {'是 ← 数据泄漏嫌疑' if input_leak else '否'}\n")
            out.write(f"[推导逻辑] {logic}\n\n")
        out.write("=" * 70 + "\n话术出现频次（每条漏报最多计一次）:\n")
        for name, c in rhetoric_count.items():
            out.write(f"  {c:2d}/{len(misses)}  {name}\n")
        out.write(f"原始数据自带加白/处置字段的漏报: {n_leak}/{len(misses)}\n")

    # 终端只打印摘要
    print(f"漏报 {len(misses)} 条，明细已写入 {args.output}")
    print("话术频次:", {k: v for k, v in rhetoric_count.items() if v})
    print(f"原始数据自带加白/处置字段: {n_leak}/{len(misses)}")


if __name__ == "__main__":
    main()
