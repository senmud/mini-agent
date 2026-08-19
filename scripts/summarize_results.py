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

# 每条答案的起始标题：以「事件」开头的定性族标题，兼容多种变体：
# "### 1. 事件性质判断"、"### 一、事件性质判定"、"### 事件性质定性"、
# "### 事件定性"、"### 事件定性分析" 等（单轮/多轮模式格式不同）。
# 注意：答案内部的"行为定性分析/定性分析结论"等小节不作为切分点。
ANSWER_START = re.compile(
    r"^#{2,3}\s*(?:\d+[\.、]\s*|[一二三四五六七八九十]+[、\.]\s*)?"
    r"事件(?:性质(?:判断|判定|定性)?|定性(?:分析|结论)?)",
    re.M)


def _split_concl_first(part):
    """块内二次切分：处理「结论先行」格式的答案（以 ### 结论 开头，
    紧跟 ### 推导逻辑）。同时满足两个条件才切：
      a) 下一个标题是推导逻辑；
      b) 上一个标题是收尾类小节（需补充/补充说明等）或另一个结论标题
    ——避免误切答案内部「结论→推导逻辑」的正常小节顺序。"""
    heads = [(m.start(), m.group(0)) for m in re.finditer(r"^#{2,4} [^\n]*", part, re.M)]
    cuts = []
    for i, (pos, txt) in enumerate(heads):
        if txt.startswith("### 结论") and i + 1 < len(heads) and i > 0:
            nxt = heads[i + 1][1]
            prev = heads[i - 1][1]
            if re.match(r"#{2,4} 推导逻辑", nxt) and (
                    re.search(r"需补充|补充说明|补充数据|补充验证|下一步", prev)
                    or prev.startswith("### 结论")):
                cuts.append(pos)
    if not cuts:
        return [part]
    out, prev = [], 0
    for c in cuts:
        out.append(part[prev:c])
        prev = c
    out.append(part[prev:])
    return [x for x in out if x.strip()]


def split_answers(text):
    parts = ANSWER_START.split(text)
    result = []
    for p in parts[1:]:
        result.extend(_split_concl_first(p))
    return [p.strip() for p in result if p.strip()]


# 结论段的截断边界：推导/需补充/补充说明等小节（补充数据中的核实问句
# 常含"是否属于正常业务"之类表述，不截断会污染分类）
CUT_SEC = r"^#{2,4}[^\n]*(?:推导|需补充|补充数据|补充说明|补充验证|下一步)"


def extract_conclusion(answer):
    """提取结论段，兼容：'### 3. 判断结论与推导逻辑'、'### 三、判断结论'、
    '### 2. 分析与结论'、'#### 结论：...' 以及结论直接写在标题冒号后的格式"""
    # 优先找包含「结论」的标题段（到下一个同级及以上标题为止）
    m = re.search(r"^(#{2,4}[^\n]*结论[^\n]*)\n(.*?)(?=^#{2,3}\s|\Z)",
                  answer, re.M | re.S)
    if m:
        heading, body = m.group(1), m.group(2)
        # 标题本身可能带结论（如 "### 结论：该事件属于入侵行为"）
        inline = ""
        hm = re.match(r"^#{2,4}\s*[^：:\n]*[：:]\s*(.+)$", heading)
        if hm:
            inline = hm.group(1).strip()
        body = re.split(CUT_SEC + r"|^\s*推导逻辑[：:]?", body, maxsplit=1, flags=re.M)[0]
        lines = [l.strip() for l in body.strip().splitlines() if l.strip()]
        txt = " ".join(x for x in [inline] + lines if x)
    else:
        # 无结论标题：用整个答案（截至推导/需补充类小节）
        body = re.split(CUT_SEC + r"|^\s*推导逻辑[：:]?", answer, maxsplit=1, flags=re.M)[0]
        txt = " ".join(line.strip() for line in body.strip().splitlines() if line.strip())
    # 去掉行首的"结论："前缀与 markdown 加粗，便于后续匹配
    txt = re.sub(r"^[#>*\s]*结论[：:]?\s*", "", txt)
    return txt.replace("**", "")


# 明确的不确定表述（未倒向任一侧）
UNSURE = re.compile(r"无法(?:明确|完全|最终|准确|直接)?(?:定性|判定|判断|确定|区分)")

# ---------- 结论分类：否定窗口 + 显式判定句 + 证据计数 ----------
NEG_WORDS = re.compile(r"无|没有|不存在|并非|并不是|不是|不属于|而非|不符合|不足以|缺乏|无法")
NORMAL_KW = re.compile(r"正常业务|正常运维|自动化运维|业务行为|误报")
INTR_KW = re.compile(r"入侵|违规")
# 显式判定短语："属于入侵"、"判定为误报"、"结论：...入侵" 等
EXPL_INTR = re.compile(
    r"(?:属于|判定为|判断为|定性为|认定为|贴近|倾向(?:于)?|结论[：:][^。；\n]{0,12}?)"
    r"[^。；\n]{0,8}?入侵")
EXPL_NORM = re.compile(
    r"(?:属于|判定为|判断为|定性为|认定为|贴近|倾向(?:于)?|结论[：:][^。；\n]{0,12}?)"
    r"[^。；\n]{0,8}?(?:正常业务|正常运维|误报)")


def _negated(t, pos):
    """pos 处的关键词是否处于否定语境（同一小句内、前 25 字内有否定词，
    或紧邻不/未前缀，如"不属于入侵"）"""
    win = t[max(0, pos - 25):pos]
    win = re.split(r"[。；;，,]", win)[-1]
    if NEG_WORDS.search(win):
        return True
    pre = t[max(0, pos - 2):pos]
    return pre.endswith("不") or pre.endswith("未")


def _expl_negated(t, m):
    """显式判定短语是否应作废：短语间隙内含否定词（如"贴近入侵行为，
    而非正常业务"中的正常侧），或短语起点本身处于否定语境"""
    if re.search(r"而非|并非|并不是|不是|不属于|不符合", m.group(0)):
        return True
    return _negated(t, m.start())


def classify(concl):
    """结论三分类：入侵 / 正常误报 / 不确定。
    策略：先识别明确的不确定表述；再对非否定语境的证据关键词计数，
    两侧都有时以最后出现的判定为准（最终结论通常在末尾）。"""
    if not concl:
        return "未提取到结论"
    # 1) 不确定：含"无法定性/判定/判断/确定/区分"，且满足其一：
    #    a) 其前文无倒向性判定；
    #    b) 该表述位于结尾 100 字内且结尾段本身无倒向词
    m = UNSURE.search(concl)
    if m:
        before_clean = not re.search(r"入侵|违规|误报|正常业务", concl[:m.start()])
        tail = concl[-100:]
        tail_clean = not re.search(r"入侵|违规|误报|正常业务", tail)
        if before_clean or (m.start() >= len(concl) - 100 and tail_clean):
            return "不确定"
    # 2) 显式判定短语（剔除否定语境，如"不属于入侵"、"贴近…而非正常业务"）
    ei = [x.start() for x in EXPL_INTR.finditer(concl) if not _expl_negated(concl, x)]
    en = [x.start() for x in EXPL_NORM.finditer(concl) if not _expl_negated(concl, x)]
    if ei and not en:
        return "入侵"
    if en and not ei:
        return "正常/误报"
    if ei and en:  # 两侧都有显式判定时，以最后出现的为准
        return "入侵" if max(ei) > max(en) else "正常/误报"
    # 3) 证据计数（剔除否定语境，如"无证据支撑为正常业务行为"）
    n_norm = [x.start() for x in NORMAL_KW.finditer(concl) if not _negated(concl, x.start())]
    n_intr = [x.start() for x in INTR_KW.finditer(concl) if not _negated(concl, x.start())]
    if n_norm and not n_intr:
        return "正常/误报"
    if n_intr and not n_norm:
        return "入侵"
    if n_norm and n_intr:
        return "入侵" if max(n_intr) > max(n_norm) else "正常/误报"
    # 4) 无明确证据词
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
