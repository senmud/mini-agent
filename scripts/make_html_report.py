#!/usr/bin/env python3
"""
make_html_report.py —— 生成 HIDS 定性研判稳定性分析 HTML 报告（自包含、无外部依赖）

实验设计：同批告警在 single-turn 模式下独立运行两次（ST1/ST2），
以两次结果的一致性分离三类原因：
  - 两次都判对/都判错 → 稳定（能力或系统性偏差）
  - 两次判定相反      → 证据处于边界：日志不确定性 + 解码噪声
  - 模型自称"无法定性/需补充数据" → 模型主动识别出的日志不确定性

用法:
    python3 scripts/make_html_report.py <csv文件> [-o hids_analysis_report.html]
"""
import argparse
import csv
import html
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_misses import INPUT_LEAK  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_summary(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_requests(path):
    text = open(path, encoding="utf-8").read()
    return [r.strip() for r in re.split(r"\n{3,}", text) if r.strip()]


def category(a, b):
    """按两次独立单轮运行的结果分类"""
    unsure = ("不确定", "未提取到结论")
    if a in unsure or b in unsure:
        return "模型存疑"
    if a == "入侵" and b == "入侵":
        return "稳定命中"
    if a != "入侵" and b != "入侵":
        return "稳定漏报"
    return "判定摇摆"


def attribute(cat, leak):
    if cat == "稳定命中":
        return "判定正确"
    if leak:
        return "输入数据泄漏"
    if cat in ("判定摇摆", "模型存疑"):
        return "日志不确定性"
    return "模型+提示词"


CAT_COLORS = {
    "稳定命中": "#16a34a",
    "稳定漏报": "#dc2626",
    "判定摇摆": "#d97706",
    "模型存疑": "#64748b",
}
BADGE = {
    "入侵": "b-hit",
    "正常/误报": "b-miss",
    "不确定": "b-unsure",
    "未提取到结论": "b-unsure",
    "稳定命中": "b-hit",
    "稳定漏报": "b-miss",
    "判定摇摆": "b-flip",
    "模型存疑": "b-unsure",
}
ATTR_CLASS = {
    "判定正确": "a-ok",
    "输入数据泄漏": "a-leak",
    "日志不确定性": "a-ambig",
    "模型+提示词": "a-model",
}


def badge(text):
    return f'<span class="badge {BADGE.get(text, "b-unsure")}">{html.escape(text)}</span>'


def main():
    ap = argparse.ArgumentParser(description="生成稳定性分析 HTML 报告")
    ap.add_argument("csv_file")
    ap.add_argument("-o", "--output", default="hids_analysis_report.html")
    ap.add_argument("--mt", default=os.path.join(ROOT, "hids_summary.csv"))
    ap.add_argument("--st1", default=os.path.join(ROOT, "hids_summary_st.csv"))
    ap.add_argument("--st2", default=os.path.join(ROOT, "hids_summary_st2.csv"))
    ap.add_argument("--requests", default=os.path.join(ROOT, "hids_log.txt"))
    args = ap.parse_args()

    with open(args.csv_file, newline="", encoding="utf-8-sig") as f:
        meta = list(csv.DictReader(f))
    mt, st1, st2 = load_summary(args.mt), load_summary(args.st1), load_summary(args.st2)
    reqs = load_requests(args.requests)
    n = len(meta)
    assert n == len(mt) == len(st1) == len(st2) == len(reqs), "数量不一致，无法对齐"

    rows = []
    for i in range(n):
        leak = bool(INPUT_LEAK.search(reqs[i]))
        cat = category(st1[i]["model_binary"], st2[i]["model_binary"])
        rows.append({
            "idx": i + 1,
            "alarm_id": meta[i]["alarm_id"],
            "rule": meta[i]["rule_name"],
            "old_ai": meta[i]["old_ai_result"],
            "mt": mt[i]["model_binary"],
            "s1": st1[i]["model_binary"],
            "s2": st2[i]["model_binary"],
            "c1": st1[i]["model_conclusion"],
            "c2": st2[i]["model_conclusion"],
            "leak": leak,
            "cat": cat,
            "attr": attribute(cat, leak),
        })

    hits = lambda rs: sum(1 for r in rs if r == "入侵")  # noqa: E731
    rec_mt, rec_s1, rec_s2 = hits([r["mt"] for r in rows]), hits([r["s1"] for r in rows]), hits([r["s2"] for r in rows])
    agree = sum(1 for r in rows if r["s1"] == r["s2"])
    cat_count = {c: sum(1 for r in rows if r["cat"] == c) for c in CAT_COLORS}
    attr_count = {a: sum(1 for r in rows if r["attr"] == a) for a in ATTR_CLASS}
    flips = [r for r in rows if r["cat"] == "判定摇摆"]
    unsure = [r for r in rows if r["cat"] == "模型存疑"]
    leak_rows = [r for r in rows if r["leak"]]
    leak_miss = [r for r in leak_rows if r["cat"] != "稳定命中"]
    stable_miss_noleak = [r for r in rows if r["cat"] == "稳定漏报" and not r["leak"]]
    unsure_quote_n = sum(1 for r in flips + unsure
                        if ("无法" in r["c1"] or "补充" in r["c1"] or "不确定" in r["c1"]
                            or "无法" in r["c2"] or "补充" in r["c2"]))

    def pct(x):
        return f"{x / n:.1%}"

    # ---------- HTML ----------
    css = """
:root { --ink:#1f2937; --muted:#6b7280; --line:#e5e7eb; --bg:#f4f6f8; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  line-height:1.65; }
.wrap { max-width:1080px; margin:0 auto; padding:28px 20px 60px; }
.hero { background:linear-gradient(135deg,#312e81 0%,#6d28d9 55%,#9333ea 100%);
  color:#fff; border-radius:18px; padding:36px 36px 30px; box-shadow:0 12px 32px rgba(76,29,149,.28); }
.hero h1 { margin:0 0 10px; font-size:27px; letter-spacing:.5px; }
.hero p.sub { margin:0 0 18px; opacity:.88; font-size:14.5px; }
.chips { display:flex; flex-wrap:wrap; gap:8px; }
.chip { background:rgba(255,255,255,.16); border:1px solid rgba(255,255,255,.28);
  padding:4px 12px; border-radius:999px; font-size:12.5px; }
h2 { font-size:19px; margin:42px 0 6px; padding-left:12px; border-left:4px solid #6d28d9; }
p.desc { color:var(--muted); font-size:13.5px; margin:0 0 16px 16px; }
.kpis { display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-top:22px; }
.kpi { background:#fff; border-radius:14px; padding:16px 14px; text-align:center;
  box-shadow:0 2px 10px rgba(0,0,0,.05); border:1px solid var(--line); }
.kpi .num { font-size:26px; font-weight:750; }
.kpi .lbl { font-size:12px; color:var(--muted); margin-top:2px; }
.kpi.hi .num { color:#16a34a; } .kpi.lo .num { color:#dc2626; }
.kpi.mid .num { color:#6d28d9; } .kpi.warn .num { color:#d97706; }
.card { background:#fff; border:1px solid var(--line); border-radius:14px;
  padding:20px 22px; box-shadow:0 2px 10px rgba(0,0,0,.04); margin-top:14px; }
.bars .row { display:flex; align-items:center; gap:12px; margin:9px 0; }
.bars .lab { width:92px; font-size:13.5px; text-align:right; color:var(--ink); }
.bars .track { flex:1; background:#eef0f3; border-radius:7px; height:24px; }
.bars .fill { height:24px; border-radius:7px; }
.bars .val { width:110px; font-size:12.5px; color:var(--muted); }
.grid3 { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:14px; }
.acard { background:#fff; border:1px solid var(--line); border-radius:14px; padding:18px 18px 14px;
  box-shadow:0 2px 10px rgba(0,0,0,.04); border-top:4px solid #ccc; }
.acard h3 { margin:0 0 4px; font-size:15.5px; }
.acard .big { font-size:30px; font-weight:780; }
.acard p { font-size:13px; color:var(--muted); margin:8px 0 10px; }
.acard .ids { font-family:ui-monospace,Menlo,monospace; font-size:11px; color:#374151;
  word-break:break-all; line-height:1.9; }
.a-leak { border-top-color:#9333ea; } .a-leak .big { color:#9333ea; }
.a-ambig { border-top-color:#d97706; } .a-ambig .big { color:#d97706; }
.a-model { border-top-color:#dc2626; } .a-model .big { color:#dc2626; }
.a-ok { border-top-color:#16a34a; } .a-ok .big { color:#16a34a; }
table { width:100%; border-collapse:collapse; background:#fff; border-radius:14px; overflow:hidden;
  box-shadow:0 2px 10px rgba(0,0,0,.05); font-size:13px; }
th { background:#f8f9fb; text-align:left; padding:10px 10px; font-size:12.5px; color:var(--muted);
  border-bottom:2px solid var(--line); white-space:nowrap; }
td { padding:9px 10px; border-bottom:1px solid #f0f1f4; vertical-align:middle; }
tbody tr:hover { background:#faf7ff; }
td.mono { font-family:ui-monospace,Menlo,monospace; font-size:11.5px; }
.badge { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; white-space:nowrap; }
.b-hit { background:#dcfce7; color:#15803d; } .b-miss { background:#fee2e2; color:#b91c1c; }
.b-flip { background:#fef3c7; color:#b45309; } .b-unsure { background:#e2e8f0; color:#475569; }
.b-leak { background:#f3e8ff; color:#7e22ce; }
.a-ok2 { color:#15803d; } .a-leak2 { color:#7e22ce; } .a-ambig2 { color:#b45309; } .a-model2 { color:#b91c1c; }
.flip { background:#fff; border:1px solid var(--line); border-radius:14px; padding:16px 18px; margin-top:12px;
  box-shadow:0 2px 8px rgba(0,0,0,.04); }
.flip .head { font-size:13.5px; margin-bottom:10px; }
.flip .head b { font-family:ui-monospace,Menlo,monospace; font-size:12px; }
.pair { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.quote { border-radius:10px; padding:10px 14px; font-size:13px; background:#fafafa; border:1px solid var(--line); }
.quote .t { font-size:11.5px; color:var(--muted); margin-bottom:4px; }
.q-hit { border-left:4px solid #16a34a; } .q-miss { border-left:4px solid #dc2626; }
.q-grey { border-left:4px solid #94a3b8; }
ol.findings li { margin:9px 0; }
footer { margin-top:44px; color:#9ca3af; font-size:12px; text-align:center; }
@media (max-width:860px){ .kpis{grid-template-columns:repeat(2,1fr);} .grid3{grid-template-columns:1fr;} .pair{grid-template-columns:1fr;} }
"""

    H = []
    H.append(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HIDS 定性研判稳定性分析报告</title><style>{css}</style></head><body><div class="wrap">
<div class="hero">
  <h1>HIDS 漏报样本定性研判 · 稳定性分析报告</h1>
  <p class="sub">同一批告警、同一模型与提示词，single-turn 独立运行两次 —— 分离「模型研判问题」与「日志本身不确定性」</p>
  <div class="chips">
    <span class="chip">数据集：FN_RECALL_MISS 31 条（人工标注全部为真实入侵）</span>
    <span class="chip">模型：doubao-seed-2.0-lite · temperature 0.3 · SSE 流式</span>
    <span class="chip">对照：多轮累积模式 1 次</span>
    <span class="chip">生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}</span>
  </div>
  <div class="kpis">
    <div class="kpi lo"><div class="num">{pct(rec_s1)}</div><div class="lbl">单轮第 1 次召回（{rec_s1}/31）</div></div>
    <div class="kpi lo"><div class="num">{pct(rec_s2)}</div><div class="lbl">单轮第 2 次召回（{rec_s2}/31）</div></div>
    <div class="kpi mid"><div class="num">{pct(rec_mt)}</div><div class="lbl">多轮模式召回（{rec_mt}/31）</div></div>
    <div class="kpi hi"><div class="num">{pct(agree)}</div><div class="lbl">两次判定一致（{agree}/31）</div></div>
    <div class="kpi warn"><div class="num">{len(flips)}</div><div class="lbl">判定翻转（两次相反）</div></div>
  </div>
</div>

<h2>实验设计：如何区分两类原因</h2>
<div class="card">
<p style="margin:0 0 8px">两条完全相同的告警日志，分别独立送给模型（single-turn，互不知晓对方结果）。若两次结论一致，说明证据对模型是「决定性」的；若相反，则说明证据处于边界 —— 要么是<b>日志本身信息不足</b>（不确定性），要么是<b>解码随机性</b>（temperature&gt;0）放大了边界证据的摇摆。再叠加检查输入数据是否携带「历史处置记录」，即可把问题归因到三个层面：</p>
<p style="margin:0;color:var(--muted);font-size:13px">① 输入数据泄漏（数据工程问题）&nbsp;&nbsp;② 日志不确定性（证据边界，模型说「无法定性」时往往是合理输出）&nbsp;&nbsp;③ 模型+提示词系统性偏差（稳定的错误研判）</p>
</div>

<h2>两次独立运行的判定稳定性</h2>
<p class="desc">31 条告警按两次单轮结果的组合分类。</p>
<div class="card bars">""")

    for c in ("稳定命中", "稳定漏报", "判定摇摆", "模型存疑"):
        cnt = cat_count[c]
        w = cnt / n * 100
        H.append(f'<div class="row"><div class="lab">{c}</div>'
                 f'<div class="track"><div class="fill" style="width:{w:.0f}%;background:{CAT_COLORS[c]}"></div></div>'
                 f'<div class="val">{cnt} 条 · {pct(cnt)}</div></div>')
    H.append("</div>")

    # ---- 归因卡片 ----
    H.append(f"""
<h2>原因归因</h2>
<p class="desc">按优先级归因：先看输入是否泄漏历史处置结论，再看两次是否一致，最后才是模型自身偏差。</p>
<div class="grid3">
  <div class="acard a-leak"><h3>① 输入数据泄漏</h3><div class="big">{attr_count['输入数据泄漏']}</div>
    <p>原始告警 JSON 自带「已加白」处置记录（white_list_*、action、user），模型把它当作无罪证据 —— 是<b>数据问题</b>，不是研判问题，改数据即可修复。</p>
    <div class="ids">{' '.join('#' + str(r['idx']) for r in leak_miss)}</div></div>
  <div class="acard a-ambig"><h3>② 日志不确定性</h3><div class="big">{attr_count['日志不确定性']}</div>
    <p>两次运行判定相反或模型自称「无法定性」：证据本身处于边界。其中 {unsure_quote_n} 条模型明确给出了「需补充的数据/工具」清单 —— 这是可用输出而非单纯错误。</p>
    <div class="ids">{' '.join('#' + str(r['idx']) for r in flips + unsure)}</div></div>
  <div class="acard a-model"><h3>③ 模型+提示词偏差</h3><div class="big">{attr_count['模型+提示词']}</div>
    <p>无泄漏记录、两次却都稳定判错：业务叙事自证、进程链信任传递等系统性推理缺陷，需要提示词/流程层解决。</p>
    <div class="ids">{' '.join('#' + str(r['idx']) for r in stable_miss_noleak)}</div></div>
</div>""")

    # ---- 明细表 ----
    H.append("""
<h2>逐条明细</h2>
<p class="desc">ST1 / ST2 = 两次独立单轮运行的二分类结论；「归因」列对应上图三类原因。</p>
<table><thead><tr><th>#</th><th>alarm_id</th><th>规则</th><th>旧AI</th><th>多轮</th><th>ST1</th><th>ST2</th><th>稳定性</th><th>泄漏</th><th>归因</th></tr></thead><tbody>""")
    for r in rows:
        attr_cls = {"判定正确": "a-ok2", "输入数据泄漏": "a-leak2",
                    "日志不确定性": "a-ambig2", "模型+提示词": "a-model2"}[r["attr"]]
        leak_cell = '<span class="badge b-leak">泄漏</span>' if r["leak"] else "—"
        H.append(
            f"<tr><td>{r['idx']}</td><td class='mono'>{html.escape(r['alarm_id'][:16])}…</td>"
            f"<td>{html.escape(r['rule'])}</td><td>{html.escape(r['old_ai'])}</td>"
            f"<td>{badge(r['mt'])}</td><td>{badge(r['s1'])}</td><td>{badge(r['s2'])}</td>"
            f"<td>{badge(r['cat'])}</td><td>{leak_cell}</td>"
            f"<td class='{attr_cls}'><b>{r['attr']}</b></td></tr>")
    H.append("</tbody></table>")

    # ---- 翻转对照 ----
    H.append(f"""
<h2>判定翻转样本对照（{len(flips)} 条）</h2>
<p class="desc">完全相同的输入，两次运行给出相反结论 —— 日志证据处于边界的直接证据。左右并列两次运行的判断结论原文。</p>""")
    for r in flips:
        q1 = "q-hit" if r["s1"] == "入侵" else "q-miss"
        q2 = "q-hit" if r["s2"] == "入侵" else "q-miss"
        H.append(f"""
<div class="flip"><div class="head"><b>#{r['idx']} {html.escape(r['alarm_id'][:20])}</b> · {html.escape(r['rule'])} · {badge(r['s1'])} → {badge(r['s2'])}</div>
<div class="pair">
  <div class="quote {q1}"><div class="t">单轮第 1 次</div>{html.escape(r['c1'][:260])}</div>
  <div class="quote {q2}"><div class="t">单轮第 2 次</div>{html.escape(r['c2'][:260])}</div>
</div></div>""")

    # ---- 稳定漏报（模型+提示词）----
    if stable_miss_noleak:
        H.append(f"""
<h2>稳定误报且无数据泄漏（{len(stable_miss_noleak)} 条）</h2>
<p class="desc">两次独立运行都判为正常/误报，输入中也没有「已加白」记录可甩锅 —— 这是模型+提示词层面的系统性问题样本。</p>""")
        for r in stable_miss_noleak:
            H.append(f"""
<div class="flip"><div class="head"><b>#{r['idx']} {html.escape(r['alarm_id'][:20])}</b> · {html.escape(r['rule'])}</div>
<div class="quote q-miss"><div class="t">两次运行的一致结论</div>{html.escape(r['c2'][:300])}</div></div>""")

    # ---- 结论与建议 ----
    diff = abs(rec_s1 - rec_s2)
    H.append(f"""
<h2>核心结论</h2>
<div class="card"><ol class="findings">
<li><b>研判结果高度不稳定：这是首要发现。</b>
两次独立运行只有 {agree}/31（{pct(agree)}）判定一致，{len(flips)} 条完全翻转，
两次都判入侵的「稳定命中」仅 {cat_count['稳定命中']} 条；三次运行召回在 {min(rec_s1, rec_s2, rec_mt)}/31 ~ {max(rec_s1, rec_s2, rec_mt)}/31 之间波动。
temperature=0.3 下，模型对多数样本的定性处于「边界摇摆」状态 —— 单次运行结果几乎不可复现，
任何基于单次运行的评估结论都不可信。</li>
<li><b>日志不确定性是最大的单一类别（{attr_count['日志不确定性']} 条）。</b>
相同输入两次相反结论，说明这些日志的证据本身就不足以二选一定性；
其中 {unsure_quote_n} 条模型在至少一次运行中明确写出「无法完全定性，需补充 XX 数据/工具」——
模型其实「知道」日志不够，对这些样本正确的做法是把补充数据清单接入调查流程，而不是逼它二选一。</li>
<li><b>输入数据泄漏（{attr_count['输入数据泄漏']} 条）是最容易修复的一类。</b>
原始数据自带「已加白」处置记录，模型照单全收当作无罪证据；剥离字段即可直接改善，与模型能力无关。</li>
<li><b>模型+提示词的系统性偏差占 {attr_count['模型+提示词']} 条</b>：
无泄漏记录、两次却都稳定判错，典型话术是「业务叙事自证」与「进程链信任传递」，
需要红队对抗式提示词与证据校验要求来约束。</li>
<li><b>多轮模式的 {pct(rec_mt)} 不代表能力。</b>
它混入了重复告警的自一致收益与负锚定损失，不可复现；正式评估应采用
「single-turn × k 次独立采样 + 多数投票」（self-consistency），并把 temperature 降到 0。</li>
</ol></div>

<h2>回答核心问题：模型问题还是日志问题？</h2>
<div class="card">
<p style="margin:0"><b>两者都有，且相互放大</b>：约一半样本（{attr_count['日志不确定性']} 条日志不确定性 + {attr_count['模型+提示词']} 条模型偏差中的边界案例）的日志证据本身不具决定性，
而模型在边界证据上又缺乏稳定的裁决策略（不强制攻击假设检验、允许编造业务叙事、temperature&gt;0 引入随机性），
导致「不确定」被随机落地为「误报」或「入侵」。另有 {attr_count['输入数据泄漏']} 条是纯数据工程问题。
可执行的优先级：<b>剥离泄漏字段（最易）→ 降温+多次投票（最通用）→ 红队提示词（治本）→ 补充数据流程（消化不确定性）</b>。</p>
</div>

<h2>改进建议</h2>
<div class="card"><ol class="findings">
<li><b>数据层（收益最大）</b>：送入模型前剥离 raw_risk_data 中的处置/白名单字段
（white_list_*、action、handle_time、处置 user），并在提示词中声明「处置记录可能是错误或伪造的，禁止作为判据」。</li>
<li><b>评估协议</b>：按 alarm_id 去重；single-turn + 每条独立采样 3~5 次多数投票；temperature=0。</li>
<li><b>提示词层</b>：强制红队流程 —— 先构造攻击假设并列举证据，再列反证，最后裁决；
「业务解释必须有日志直接证据，禁止编造」；不确定时输出「可疑」而非「误报」。</li>
<li><b>流程层</b>：把「需补充数据/工具」输出结构化，作为调查工单自动流转，而不是当作漏报统计。</li>
</ol></div>

<footer>mini-agent · scripts/make_html_report.py 生成 · 数据文件均已本地 gitignore，请勿外传</footer>
</div></body></html>""")

    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(H))

    print(f"报告已生成: {args.output}")
    print(f"召回: 多轮 {rec_mt}/31 | 单轮1 {rec_s1}/31 | 单轮2 {rec_s2}/31")
    print(f"两次一致 {agree}/31；稳定命中 {cat_count['稳定命中']} 稳定漏报 {cat_count['稳定漏报']} "
          f"翻转 {cat_count['判定摇摆']} 存疑 {cat_count['模型存疑']}")
    print("归因:", {k: v for k, v in attr_count.items() if v})


if __name__ == "__main__":
    main()
