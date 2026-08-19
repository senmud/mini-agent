#!/usr/bin/env python3
"""
make_prompt_comparison_report.py —— 提示词调整前后效果对比 HTML 报告（自包含）

支持多轮对比版本（--version v1/v2），每个版本有自己的基线/对照汇总、
提示词差异说明与逐条人工研判（根因 + 倾向性）。

用法:
    python3 scripts/make_prompt_comparison_report.py <csv文件> --version v2 [-o 输出.html]
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

# ---------------------------------------------------------------- 版本配置

VERSIONS = {
    "v1": dict(
        title="提示词调整效果对比分析 · 第一轮（v1→v2）",
        base="hids_summary_st2.csv", new="hids_summary_st3.csv",
        base_label="ST2 · 旧提示词 v1", new_label="ST3 · 新提示词 v2",
        out="hids_prompt_comparison_report.html",
        prompt_old="""- 输出判断结论和推导逻辑；
- 无法确定时，规划需要补充的数据和工具。

注意！不要相信日志中自带的告警级别，从内容逻辑上独立推理判断。""",
        prompt_new="""注意务必严格遵守下列约束：

<span class="add">- 不要相信日志中自带的告警级别或<b>白名单标识</b>，
  从内容逻辑上独立推理判断；</span>
<span class="add">- 为了提高召回率，当信息不充分、结论模棱两可时，
  <b>倾向于判为不确定或入侵</b>。</span>""",
        analysis={
            2: dict(root="数据泄漏+日志信息不足", lean="new",
                    reason="真实入侵样本。旧结论判误报会直接漏掉；且该样本原始数据自带"
                           "「已加白」处置记录，旧结论受其影响。新结论改为「不确定」，"
                           "保留了调查入口——新约束「不要相信白名单标识」部分起效。"),
            4: dict(root="日志信息不足", lean="new",
                    reason="K8S SA 凭据访问缺少业务侧上下文，旧提示词下过度自信地判了误报；"
                           "新结论承认信息不充分并给出补充方向，运营上更合理。"),
            5: dict(root="提示词副作用（过度保守）", lean="old",
                    reason="旧结论判「凭据滥用可疑入侵」方向正确（真实为入侵）；新提示词"
                           "「倾向不确定」在此条让它过度保守，把本来能拿住的召回让掉了。"),
            13: dict(root="提示词起效（纠正叙事偏差）", lean="new",
                    reason="新结论「窃取凭证后未授权访问 k8s 集群」与真实入侵一致；"
                           "旧结论的误报属于旧提示词下的系统性偏差。"),
            18: dict(root="提示词起效（纠正编造叙事）", lean="new",
                    reason="下载+赋权+定时任务是经典驻留链，真实为入侵；旧结论曾以"
                           "「官方客户端自动更新」叙事将其解释为误报，新提示词的怀疑倾向纠正了这一点。"),
            27: dict(root="提示词起效", lean="new",
                    reason="新结论「疑似凭证访问类入侵+需补数验证」比旧误报更接近真实（入侵），"
                           "且明确请求补充数据，输出结构可用。"),
            29: dict(root="日志信息不足+提示词缓解", lean="new",
                    reason="隧道类告警缺少进程/会话上下文；旧结论直接误报，新结论改为不确定，"
                           "避免了错误关闭。真实为入侵，「不确定」优于「误报」。"),
            30: dict(root="提示词起效（怀疑倾向）", lean="new",
                    reason="隐蔽隧道 T1572 证据明确，新约束把边界案例推向入侵一侧，"
                           "与真实入侵一致——「倾向入侵」约束设计目标的正面案例。"),
            31: dict(root="提示词起效", lean="new",
                    reason="协议隧道建立行为，新结论判入侵与真实一致。"),
        }),
    "v2": dict(
        title="提示词调整效果对比分析 · 第二轮（v2→v3）",
        base="hids_summary_st3.csv", new="hids_summary_st4.csv",
        base_label="ST3 · 提示词 v2", new_label="ST4 · 提示词 v3",
        out="hids_prompt_comparison_report_v2.html",
        prompt_old="""- 不要相信日志中自带的告警级别或白名单标识，从内容逻辑上独立推理判断；
- 为了提高召回率，当信息不充分、结论模棱两可时，倾向于判为不确定或入侵。""",
        prompt_new="""- <span class="add">务必确保<b>最终结论必须与推导逻辑一致</b>；</span>
- 不要相信日志中自带的告警级别或白名单标识，从内容逻辑上独立推理判断；
- 为了提高召回率，当信息不充分，<span class="add">或<b>无足够证据支撑为业务行为</b>时</span>，
  应判为不确定或入侵；（举证责任倒置，较上版更强）
- <span class="add">当<b>证据链完整（攻击假设可闭环）时应明确判入侵</b>，避免过度保守。</span>""",
        analysis={
            2: dict(root="数据泄漏+模型问题", lean="old",
                    reason="真实为入侵。原始数据自带「已加白」处置记录，模型在「不要相信白名单标识」"
                           "约束下仍被判为误报——泄漏字段的影响未被完全抵消。旧结论「不确定」"
                           "至少保留了调查入口，更稳妥。"),
            6: dict(root="提示词副作用（过度保守）", lean="old",
                    reason="旧结论判凭据滥用入侵，方向正确（真实为入侵）；新结论降为不确定并要求"
                           "补充 RBAC/审计数据。「证据链闭环才判入侵」提高了判定门槛——补充数据清单"
                           "本身有运营价值，但代价是让掉一条召回。"),
            8: dict(root="提示词起效（举证责任倒置）", lean="new",
                    reason="RCE 试探特征（/.x/rce 可疑路径、外部 IP、无业务支撑）无法被支撑为正常业务，"
                           "新约束「无足够证据支撑业务行为即判入侵/不确定」推动判到入侵，与真实一致。"),
            14: dict(root="数据泄漏+业务叙事编造", lean="old",
                    reason="真实为入侵。原始数据自带加白记录，模型又用日志中的 grpcurl/GetMetrics/19530 "
                           "构造出完整的「Milvus 指标采集」业务叙事判误报——泄漏+叙事复合问题，"
                           "是剩余漏报中最顽固的类型。"),
            15: dict(root="数据泄漏+业务叙事编造", lean="old",
                    reason="与 #14 同主机同手法（alarm_id 仅尾号不同），模型复现了 Milvus 业务叙事判误报；"
                           "真实为入侵。同一攻击的两条告警被同一叙事双双漏掉，提示叙事类偏差会批量传播。"),
            20: dict(root="提示词起效（证据链闭环判入侵）", lean="new",
                    reason="上一轮的典型问题案例：模型识别出 T1562 防御规避却给出误报/不确定（推理与结论脱节）。"
                           "新约束「证据链完整应明确判入侵 + 结论必须与推导一致」后判到入侵，"
                           "与真实一致——两条新约束同时起效的标志案例。"),
            28: dict(root="提示词起效（举证责任倒置）", lean="new",
                    reason="cloudflared 隧道工具运行于 /tmp，无合法业务支撑；旧结论停在不确定，"
                           "新约束推动判入侵，与真实一致。"),
            29: dict(root="提示词起效（举证责任倒置）", lean="new",
                    reason="隧道类告警（T1572）在新提示词下全面纠正（#28/#29 均判入侵），"
                           "旧提示词下这类样本长期在不确定/误报之间摇摆。"),
        }),
}

TREND = [("多轮·旧提示词", "hids_summary.csv", 13),
         ("单轮1·v1", "hids_summary_st.csv", None),
         ("单轮2·v1", "hids_summary_st2.csv", None),
         ("单轮3·v2", "hids_summary_st3.csv", None),
         ("单轮4·v3", "hids_summary_st4.csv", None)]

ROOT_BADGE = {"提示词起效": "b-root", "日志信息不足": "b-side",
              "日志信息不足+提示词缓解": "b-side", "数据泄漏": "b-leak",
              "提示词副作用": "b-side", "模型问题": "b-model"}


def load(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_requests(path):
    text = open(path, encoding="utf-8").read()
    return [r.strip() for r in re.split(r"\n{3,}", text) if r.strip()]


def root_group(root):
    if root.startswith("提示词起效"):
        return "提示词起效"
    if root.startswith("日志信息不足"):
        return "日志信息不足"
    if root.startswith("数据泄漏"):
        return "数据泄漏"
    if root.startswith("提示词副作用"):
        return "提示词副作用"
    if root.startswith("模型问题"):
        return "模型问题"
    return root


def badge(t):
    cls = {"入侵": "b-hit", "正常/误报": "b-miss", "不确定": "b-unsure",
           "未提取到结论": "b-unsure"}.get(t, "b-unsure")
    return f'<span class="badge {cls}">{html.escape(t)}</span>'


def main():
    ap = argparse.ArgumentParser(description="提示词调整前后对比 HTML 报告")
    ap.add_argument("csv_file")
    ap.add_argument("--version", default="v2", choices=VERSIONS.keys())
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()

    cfg = VERSIONS[args.version]
    out_path = args.output or os.path.join(ROOT, cfg["out"])
    with open(args.csv_file, newline="", encoding="utf-8-sig") as f:
        meta = list(csv.DictReader(f))
    base, new = load(os.path.join(ROOT, cfg["base"])), load(os.path.join(ROOT, cfg["new"]))
    reqs = load_requests(os.path.join(ROOT, "hids_log.txt"))
    n = len(meta)

    hits = lambda rs: sum(1 for r in rs if r["model_binary"] == "入侵")  # noqa: E731
    rec_b, rec_n = hits(base), hits(new)
    fp_n = [r for r in new if r["model_binary"] == "正常/误报"]
    unsure_n = [r for r in new if r["model_binary"] in ("不确定", "未提取到结论")]
    fp_n_leak = [r for r in fp_n if INPUT_LEAK.search(reqs[int(r["序号"]) - 1])]

    # 趋势数据
    trend = []
    for label, fname, _ in TREND:
        p = os.path.join(ROOT, fname)
        if os.path.exists(p):
            trend.append((label, hits(load(p))))

    order = {"入侵": 2, "不确定": 1, "未提取到结论": 1, "正常/误报": 0}
    diffs, same = [], n
    for a, b in zip(base, new):
        if a["model_binary"] == b["model_binary"]:
            continue
        same -= 1
        d = order[b["model_binary"]] - order[a["model_binary"]]
        diffs.append(("改善" if d > 0 else ("退步" if d < 0 else "同级"), a, b))
    improve = sum(1 for t, _, _ in diffs if t == "改善")
    regress = sum(1 for t, _, _ in diffs if t == "退步")
    lean_new = sum(1 for _, a, _ in diffs if cfg["analysis"].get(int(a["序号"]), {}).get("lean") == "new")
    lean_old = len(diffs) - lean_new

    gcount = {}
    for _, a, _ in diffs:
        g = root_group(cfg["analysis"].get(int(a["序号"]), {}).get("root", "未分类"))
        gcount[g] = gcount.get(g, 0) + 1

    css = """
:root { --ink:#1f2937; --muted:#6b7280; --line:#e5e7eb; --bg:#f4f6f8; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  line-height:1.65; }
.wrap { max-width:1080px; margin:0 auto; padding:28px 20px 60px; }
.hero { background:linear-gradient(135deg,#7c2d12 0%,#b45309 50%,#a16207 100%);
  color:#fff; border-radius:18px; padding:36px 36px 30px; box-shadow:0 12px 32px rgba(146,64,14,.28); }
.hero h1 { margin:0 0 10px; font-size:26px; letter-spacing:.5px; }
.hero p.sub { margin:0 0 18px; opacity:.92; font-size:14.5px; }
.chips { display:flex; flex-wrap:wrap; gap:8px; }
.chip { background:rgba(255,255,255,.16); border:1px solid rgba(255,255,255,.28);
  padding:4px 12px; border-radius:999px; font-size:12.5px; }
h2 { font-size:19px; margin:42px 0 6px; padding-left:12px; border-left:4px solid #b45309; }
p.desc { color:var(--muted); font-size:13.5px; margin:0 0 16px 16px; }
.kpis { display:grid; grid-template-columns:repeat(5,1fr); gap:12px; margin-top:22px; }
.kpi { background:#fff; border-radius:14px; padding:16px 14px; text-align:center;
  box-shadow:0 2px 10px rgba(0,0,0,.05); border:1px solid var(--line); }
.kpi .num { font-size:26px; font-weight:750; }
.kpi .lbl { font-size:12px; color:var(--muted); margin-top:2px; }
.kpi.hi .num { color:#16a34a; } .kpi.lo .num { color:#dc2626; }
.kpi.mid .num { color:#b45309; } .kpi.warn .num { color:#d97706; }
.card { background:#fff; border:1px solid var(--line); border-radius:14px;
  padding:20px 22px; box-shadow:0 2px 10px rgba(0,0,0,.04); margin-top:14px; }
.bars .row { display:flex; align-items:center; gap:12px; margin:9px 0; }
.bars .lab { width:150px; font-size:13px; text-align:right; }
.bars .track { flex:1; background:#eef0f3; border-radius:7px; height:24px; }
.bars .fill { height:24px; border-radius:7px; }
.bars .val { width:120px; font-size:12.5px; color:var(--muted); }
.prompt-diff { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-top:14px; }
.pd { background:#fff; border:1px solid var(--line); border-radius:14px; padding:16px 18px; font-size:13px; }
.pd h3 { margin:0 0 8px; font-size:14px; }
.pd pre { white-space:pre-wrap; font-family:inherit; margin:0; color:#374151; font-size:12.5px; line-height:1.7; }
.pd .add { background:#dcfce7; border-radius:6px; padding:2px 6px; color:#166534; }
.badge { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; white-space:nowrap; }
.b-hit { background:#dcfce7; color:#15803d; } .b-miss { background:#fee2e2; color:#b91c1c; }
.b-unsure { background:#e2e8f0; color:#475569; }
.b-up { background:#dcfce7; color:#15803d; } .b-down { background:#fee2e2; color:#b91c1c; }
.b-root { background:#d1fae5; color:#047857; } .b-leak { background:#f3e8ff; color:#7e22ce; }
.b-model { background:#ffedd5; color:#c2410c; } .b-side { background:#fef3c7; color:#b45309; }
.flip { background:#fff; border:1px solid var(--line); border-radius:14px; padding:16px 18px; margin-top:12px;
  box-shadow:0 2px 8px rgba(0,0,0,.04); }
.flip .head { font-size:13.5px; margin-bottom:10px; display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
.flip .head b { font-family:ui-monospace,Menlo,monospace; font-size:12px; }
.pair { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.quote { border-radius:10px; padding:10px 14px; font-size:12.8px; background:#fafafa; border:1px solid var(--line); }
.quote .t { font-size:11.5px; color:var(--muted); margin-bottom:4px; }
.q-hit { border-left:4px solid #16a34a; } .q-miss { border-left:4px solid #dc2626; } .q-grey { border-left:4px solid #94a3b8; }
.verdict { margin-top:10px; border-radius:10px; padding:10px 14px; font-size:13px;
  background:#fffbeb; border:1px solid #fde68a; }
.verdict b { color:#b45309; }
.grid3 { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:14px; }
.acard { background:#fff; border:1px solid var(--line); border-radius:14px; padding:18px; border-top:4px solid #ccc;
  box-shadow:0 2px 10px rgba(0,0,0,.04); }
.acard .big { font-size:30px; font-weight:780; }
.acard h3 { margin:0 0 4px; font-size:15px; }
.acard p { font-size:12.8px; color:var(--muted); margin:8px 0 0; }
table { width:100%; border-collapse:collapse; background:#fff; border-radius:14px; overflow:hidden;
  box-shadow:0 2px 10px rgba(0,0,0,.05); font-size:13px; margin-top:14px; }
th { background:#f8f9fb; text-align:left; padding:10px; font-size:12.5px; color:var(--muted);
  border-bottom:2px solid var(--line); white-space:nowrap; }
td { padding:9px 10px; border-bottom:1px solid #f0f1f4; }
tbody tr:hover { background:#fffbeb; }
td.mono { font-family:ui-monospace,Menlo,monospace; font-size:11.5px; }
ol.findings li { margin:9px 0; }
footer { margin-top:44px; color:#9ca3af; font-size:12px; text-align:center; }
@media (max-width:860px){ .kpis{grid-template-columns:repeat(2,1fr);} .grid3{grid-template-columns:1fr;} .pair,.prompt-diff{grid-template-columns:1fr;} }
"""

    trend_colors = ["#94a3b8", "#f59e0b", "#f59e0b", "#3b82f6", "#16a34a"]
    H = [f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(cfg['title'])}</title><style>{css}</style></head><body><div class="wrap">
<div class="hero">
  <h1>{html.escape(cfg['title'])}</h1>
  <p class="sub">同一批 31 条告警、同一模型，single-turn 独立运行 —— 逐条根因分析不一致样本，并给出倾向性研判</p>
  <div class="chips">
    <span class="chip">数据集：FN_RECALL_MISS 31 条（人工标注全部为真实入侵）</span>
    <span class="chip">模型：doubao-seed-2.0-lite · temperature 0.3</span>
    <span class="chip">基线：{html.escape(cfg['base_label'])} → 对照：{html.escape(cfg['new_label'])}</span>
    <span class="chip">生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}</span>
  </div>
  <div class="kpis">
    <div class="kpi mid"><div class="num">{rec_b / n:.1%}</div><div class="lbl">{html.escape(cfg['base_label'].split(' · ')[0])} 召回（{rec_b}/31）</div></div>
    <div class="kpi {'hi' if rec_n >= rec_b else 'lo'}"><div class="num">{rec_n / n:.1%}</div><div class="lbl">{html.escape(cfg['new_label'].split(' · ')[0])} 召回（{rec_n}/31）</div></div>
    <div class="kpi {'hi' if rec_n >= rec_b else 'lo'}"><div class="num">{(rec_n - rec_b) / n:+.1%}</div><div class="lbl">召回变化（{rec_n - rec_b:+d} 条）</div></div>
    <div class="kpi {'hi' if improve > regress else 'warn'}"><div class="num">{improve} : {regress}</div><div class="lbl">判定变化：改善 : 退步</div></div>
    <div class="kpi mid"><div class="num">{same}/31</div><div class="lbl">两次判定一致</div></div>
  </div>
</div>

<h2>提示词调整内容</h2>
<div class="prompt-diff">
  <div class="pd"><h3>{html.escape(cfg['base_label'])}（约束部分）</h3><pre>{cfg['prompt_old']}</pre></div>
  <div class="pd"><h3>{html.escape(cfg['new_label'])}（绿色为新增/强化）</h3><pre>{cfg['prompt_new']}</pre></div>
</div>

<h2>五轮运行召回率趋势</h2>
<p class="desc">同一分类器口径。旧提示词两次单轮相差 25.8 个百分点（35.5% vs 61.3%），说明旧提示词下输出极不稳定；v2/v3 提示词的数值各为单次运行，需重复验证。</p>
<div class="card bars">"""]

    for i, (label, v) in enumerate(trend):
        c = trend_colors[i] if i < len(trend_colors) else "#94a3b8"
        H.append(f'<div class="row"><div class="lab">{label}</div>'
                 f'<div class="track"><div class="fill" style="width:{v / n * 100:.0f}%;background:{c}"></div></div>'
                 f'<div class="val">{v}/31 · {v / n:.1%}</div></div>')

    H.append(f"""</div>

<h2>不一致样本根因分布（{len(diffs)} 条）</h2>
<div class="grid3">
  <div class="acard" style="border-top-color:#16a34a"><h3>提示词起效</h3><div class="big" style="color:#16a34a">{gcount.get('提示词起效', 0)}</div>
    <p>新增约束（举证责任倒置 / 证据链闭环判入侵 / 结论与推导一致）直接把边界样本推到正确一侧。</p></div>
  <div class="acard" style="border-top-color:#9333ea"><h3>数据泄漏</h3><div class="big" style="color:#9333ea">{gcount.get('数据泄漏', 0)}</div>
    <p>原始数据自带「已加白」处置记录，叠加 Milvus 类业务叙事编造——剩余漏报中最顽固的类型。</p></div>
  <div class="acard" style="border-top-color:#d97706"><h3>提示词副作用 / 日志不足</h3><div class="big" style="color:#d97706">{gcount.get('提示词副作用', 0) + gcount.get('日志信息不足', 0) + gcount.get('模型问题', 0)}</div>
    <p>判定门槛提高后个别证据较足的样本被降到「不确定」（补充数据清单有运营价值，但让掉召回）。</p></div>
</div>

<h2>逐条根因分析与倾向性研判</h2>
<p class="desc">左右为基线与对照运行的判断结论原文；下方为研判意见。
倾向依据：人工标注全部为真实入侵 + 结论推理质量 + 是否依赖泄漏的处置记录。</p>""")

    for tag, a, b in diffs:
        idx = int(a["序号"])
        an = cfg["analysis"].get(idx, {"root": "未分类", "lean": "", "reason": ""})
        leak = bool(INPUT_LEAK.search(reqs[idx - 1]))
        q1 = {"入侵": "q-hit", "正常/误报": "q-miss"}.get(a["model_binary"], "q-grey")
        q2 = {"入侵": "q-hit", "正常/误报": "q-miss"}.get(b["model_binary"], "q-grey")
        lean_txt = f"更倾向<b>新结论（{html.escape(cfg['new_label'].split(' · ')[0])}）</b>" if an["lean"] == "new" \
            else f"更倾向<b>旧结论（{html.escape(cfg['base_label'].split(' · ')[0])}）</b>"
        leak_chip = ' <span class="badge b-leak">输入含加白记录</span>' if leak else ""
        g = root_group(an["root"])
        H.append(f"""
<div class="flip">
  <div class="head"><b>#{idx} {html.escape(a['alarm_id'][:20])}</b> · {html.escape(a['rule_name'])}
    <span class="badge {'b-up' if tag == '改善' else 'b-down'}">{tag}</span>
    <span class="badge {ROOT_BADGE.get(g, 'b-root')}">{html.escape(an['root'])}</span>{leak_chip}
    &nbsp;{badge(a['model_binary'])} → {badge(b['model_binary'])}</div>
  <div class="pair">
    <div class="quote {q1}"><div class="t">{html.escape(cfg['base_label'])}</div>{html.escape(a['model_conclusion'][:230])}</div>
    <div class="quote {q2}"><div class="t">{html.escape(cfg['new_label'])}</div>{html.escape(b['model_conclusion'][:230])}</div>
  </div>
  <div class="verdict">研判意见：{lean_txt}。{html.escape(an['reason'])}</div>
</div>""")

    H.append(f"""
<h2>新提示词下的残余漏报（{len(fp_n)} 条误报 + {len(unsure_n)} 条不确定）</h2>
<p class="desc">其中 {len(fp_n_leak)} 条误报样本的原始数据自带「已加白」处置记录
（#{'、#'.join(r['序号'] for r in fp_n_leak) or '无'}）——提示词约束只能缓解、无法根治字段级泄漏。</p>
<table><thead><tr><th>#</th><th>alarm_id</th><th>规则</th><th>判定</th><th>含加白记录</th><th>结论摘要</th></tr></thead><tbody>""")
    for r in fp_n + unsure_n:
        idx = int(r["序号"])
        leak = bool(INPUT_LEAK.search(reqs[idx - 1]))
        H.append(f"<tr><td>{r['序号']}</td><td class='mono'>{html.escape(r['alarm_id'][:16])}…</td>"
                 f"<td>{html.escape(r['rule_name'])}</td><td>{badge(r['model_binary'])}</td>"
                 f"<td>{'是' if leak else '—'}</td><td>{html.escape(r['model_conclusion'][:90])}</td></tr>")
    H.append("</tbody></table>")

    H.append(f"""
<h2>核心结论</h2>
<div class="card"><ol class="findings">
<li><b>提示词迭代方向正确：</b>五轮运行召回率 41.9%（多轮）→ 35.5%/61.3%（v1 两次单轮）→ 67.7%（v2）→ {rec_n / n:.1%}（v3），
本轮对比 {len(diffs)} 条变化中 {improve} 条改善、{regress} 条退步；倾向性研判 {lean_new} 条支持新结论、{lean_old} 条支持旧结论。</li>
<li><b>改善全部来自两条新约束：</b>「无足够证据支撑业务行为即判入侵/不确定」（举证责任倒置）纠正了隧道/RCE 类边界样本；
「证据链闭环应明确判入侵 + 结论必须与推导一致」修复了上一轮 #20 式的推理-结论脱节（本轮 #20 判到入侵，是标志案例）。</li>
<li><b>退步集中在两类：</b>① 数据泄漏（#2 #14 #15）——原始数据自带「已加白」记录 + Milvus 业务叙事编造，
是剩余漏报的主因，只能靠数据字段清洗解决；② 过度保守副作用（#6）——判定门槛提高后让掉一条本可拿住的召回。</li>
<li><b>「不确定」输出已成体系：</b>本轮 {len(unsure_n)} 条不确定均附带需补充的数据/工具清单，
应作为调查工单回流二次研判，而不是计入漏报。</li>
<li><b>稳定性待验证：</b>v1 两次单轮相差 25.8 个百分点的教训在前，v3 的 {rec_n / n:.1%} 是单次运行结果；
建议 temperature=0 再跑 2~3 次多数投票确认。</li>
</ol></div>

<h2>改进建议（按优先级）</h2>
<div class="card"><ol class="findings">
<li><b>数据层（当前最大收益）</b>：评估/生产管道剥离 raw_risk_data 中的处置与白名单字段
（white_list_*、action、handle_time、处置 user）——本轮 {len(fp_n_leak)} 条残余误报与之直接相关，
#14/#15 表明泄漏会与业务叙事叠加放大。</li>
<li><b>提示词层</b>：为「临时目录下载/赋权/外联」类高危模式追加红队反问条款——
“若为攻击，对应 ATT&CK 哪个环节？业务解释是否有日志直接证据？”；
并为「证据链闭环」给出可操作标准（如：外部来源+临时路径+无业务上下文三要素齐备即判入侵），减少 #6 式过度保守。</li>
<li><b>评估协议</b>：temperature 降至 0；每条告警独立采样 3 次多数投票；按 alarm_id 去重后统计。</li>
<li><b>流程层</b>：把「不确定 + 补充数据清单」结构化为调查工单（本轮 {len(unsure_n)} 条），
补充数据回流后二次研判，形成闭环。</li>
</ol></div>

<footer>mini-agent · scripts/make_prompt_comparison_report.py --version {args.version} 生成 ·
分类器口径：显式判定句 + 否定窗口 + 证据计数（最终版） · 数据文件均已本地 gitignore，请勿外传</footer>
</div></body></html>""")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(H))

    print(f"报告已生成: {out_path}")
    print(f"召回: {cfg['base_label']} {rec_b}/31 -> {cfg['new_label']} {rec_n}/31")
    print(f"变化 {len(diffs)} 条: 改善 {improve} 退步 {regress} | 倾向新 {lean_new} 倾向旧 {lean_old}")
    print("根因:", gcount)
    print(f"残余: 误报 {len(fp_n)}（含泄漏 {len(fp_n_leak)}）, 不确定 {len(unsure_n)}")


if __name__ == "__main__":
    main()
