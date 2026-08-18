#!/usr/bin/env python3
"""
summarize_results.py —— 汇总 mini-agent 对 HIDS 告警的批量分析结果

将 CSV 中的告警元信息/人工标注与 hids_answers.md 中的模型结论逐条对齐，
二值化比对（入侵 vs 正常/误报），生成 hids_summary.csv 并打印召回统计。

用法:
    python3 scripts/summarize_results.py <csv文件> [-a hids_answers.md] [-o hids_summary.csv]
"""
import argparse
import csv
import re
import sys
from collections import Counter

# 每条答案的起始标题（与模型实际输出格式一致）
ANSWER_START = re.compile(r"^### 事件性质判断", re.M)


def split_answers(text):
    parts = ANSWER_START.split(text)
    return [p.strip() for p in parts[1:]]


def extract_conclusion(answer):
    """提取『### 判断结论』到下一个 '### ' 标题之间的文本，压成一行"""
    m = re.search(r"### 判断结论\s*\n(.*?)(?=\n### |\Z)", answer, re.S)
    if not m:
        return ""
    return " ".join(line.strip() for line in m.group(1).strip().splitlines())


def classify(concl):
    """把结论二值化；先判否定表述（'不属于入侵'含'入侵'字样，须优先匹配）"""
    if re.search(r"误报|正常业务|正常运维|自动化运维|合法|不属于入侵|非入侵", concl):
        return "正常/误报"
    if re.search(r"入侵|违规|恶意|攻击", concl):
        return "入侵"
    return "不确定"


def human_binary(human_cn):
    if human_cn in ("真实", "入侵", "违规"):
        return "入侵"
    if human_cn in ("误报", "虚假", "正常"):
        return "正常/误报"
    return human_cn or "未知"


def main():
    ap = argparse.ArgumentParser(description="汇总 mini-agent HIDS 告警分析结果")
    ap.add_argument("csv_file")
    ap.add_argument("-a", "--answers", default="hids_answers.md")
    ap.add_argument("-o", "--output", default="hids_summary.csv")
    args = ap.parse_args()

    with open(args.csv_file, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    with open(args.answers, encoding="utf-8") as f:
        answers = split_answers(f.read())
    if len(rows) != len(answers):
        print(f"[错误] CSV {len(rows)} 行与答案 {len(answers)} 条数量不一致，无法对齐",
              file=sys.stderr)
        sys.exit(1)

    out_rows = []
    for i, (row, ans) in enumerate(zip(rows, answers), 1):
        concl = extract_conclusion(ans)
        label = classify(concl) if concl else "未提取到结论"
        human_cn = (row.get("human_result_cn") or "").strip()
        match = "一致" if label == human_binary(human_cn) else (
            "不一致" if label in ("入侵", "正常/误报") else "无法判定")
        out_rows.append({
            "序号": i,
            "alarm_id": row.get("alarm_id", ""),
            "rule_name": row.get("rule_name", ""),
            "human_result_cn": human_cn,
            "old_ai_result": row.get("old_ai_result", ""),
            "model_binary": label,
            "与人工一致": match,
            "model_conclusion": concl,
        })

    # utf-8-sig 便于 Excel 直接打开
    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    n = len(out_rows)
    real_idx = [r for r in out_rows if human_binary(r["human_result_cn"]) == "入侵"]
    tp = sum(1 for r in real_idx if r["model_binary"] == "入侵")
    fn = sum(1 for r in real_idx if r["model_binary"] == "正常/误报")
    unk = len(real_idx) - tp - fn

    print(f"共 {n} 条告警；人工标注为真实告警的: {len(real_idx)} 条")
    if real_idx:
        print(f"模型判定入侵（与人工一致）: {tp}  召回率 {tp / len(real_idx):.1%}")
        print(f"模型误判为正常/误报（漏报）: {fn}")
        if unk:
            print(f"无法判定: {unk}")
        miss = [r for r in real_idx if r["model_binary"] == "正常/误报"]
        if miss:
            print("\n漏报按 rule_name 分布:")
            for k, v in Counter(r["rule_name"] for r in miss).most_common():
                print(f"  {v:2d}  {k}")
            print("\n漏报样本的旧 AI 结果分布:",
                  dict(Counter(r["old_ai_result"] for r in miss)))

    print("\n序号 | alarm_id          | rule_name        | 人工 | 旧AI        | 模型      | 一致")
    print("-" * 100)
    for r in out_rows:
        print(f"{r['序号']:>4} | {r['alarm_id'][:17]:<17} | {r['rule_name'][:16]:<16} | "
              f"{r['human_result_cn']:<4} | {r['old_ai_result']:<11} | {r['model_binary']:<9} | "
              f"{r['与人工一致']}")
    print(f"\n输出: {args.output}")


if __name__ == "__main__":
    main()
