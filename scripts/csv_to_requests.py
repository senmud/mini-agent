#!/usr/bin/env python3
"""
csv_to_requests.py —— 从 CSV 中提取指定列（默认 raw_risk_data），每行作为一个请求，
生成 mini-agent 批量请求文件（请求之间以连续 3 个换行符分隔）。

用法:
    python3 scripts/csv_to_requests.py <csv文件> [-o 输出文件] [--column 列名] [--limit N]

说明:
    - 使用 csv 模块解析，正确处理带引号、内嵌逗号/换行的字段
    - 自动去除 UTF-8 BOM
    - 单个请求内部连续 >=3 个换行会被折叠为 2 个，避免与请求分隔符冲突
    - 跳过空字段；首尾空白自动去除
"""
import argparse
import csv
import re
import sys


def main() -> None:
    ap = argparse.ArgumentParser(description="从 CSV 提取指定列生成 mini-agent 批量请求文件")
    ap.add_argument("csv_file", help="输入 CSV 文件路径")
    ap.add_argument("-o", "--output", default="hids_log.txt", help="输出请求文件（默认 hids_log.txt）")
    ap.add_argument("--column", default="raw_risk_data", help="要提取的列名（默认 raw_risk_data）")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 个有效请求（0 = 不限制）")
    args = ap.parse_args()

    reqs = []
    total = 0
    with open(args.csv_file, newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        if args.column not in fields:
            print(f"[错误] 未找到列 '{args.column}'，可用列: {fields}", file=sys.stderr)
            sys.exit(1)
        for row in reader:
            total += 1
            v = (row.get(args.column) or "").strip()
            if not v:
                continue
            v = re.sub(r"\n{3,}", "\n\n", v)  # 防止与请求分隔符冲突
            reqs.append(v)
            if args.limit and len(reqs) >= args.limit:
                break

    with open(args.output, "w", encoding="utf-8") as f:
        if reqs:
            f.write("\n\n\n".join(reqs) + "\n")

    lens = sorted(len(r) for r in reqs)
    print(f"[统计] CSV 共 {total} 行，有效请求 {len(reqs)} 个，已写入 {args.output}", file=sys.stderr)
    if lens:
        print(f"[统计] 请求长度(字符): min={lens[0]}  median={lens[len(lens)//2]}  max={lens[-1]}", file=sys.stderr)


if __name__ == "__main__":
    main()
