#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
switchkit v0.7(2026-11-26,GPT 设计裁定落地)—— 决策/执行双层分发台
信息架构:常用(运行台/根据主要任务优化/处理规则)|设置(连接设置/模型检查/用量与费用)|帮助(新手教程/关于)
运行台三窗只讲同一条请求:决策层→执行层,下排「沿用原方式」;动效只跟真实事件;
根据主要任务优化五步(输入→勾选→开始对比→挑路线→保存规则);规则键匹配;问号悬浮讲解。
BYOK:key 只存文件路径;不代付;不宣称自动择优。
"""
import argparse, json, os, re, shutil, socket, sys, threading, time, urllib.request, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

CFG = Path.home() / ".config/switchkit"
DUAL, USAGE, DLOG = CFG / "dual.json", CFG / "usage_ledger.jsonl", CFG / "dispatch_log.jsonl"
ROUTES, OPTLOG = CFG / "routes.json", CFG / "optimization_results.jsonl"
_BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
MAP = _BASE / "exclude_map.json"

def load_map():
    return json.loads(MAP.read_text(encoding="utf-8")) if MAP.is_file() else {"hard_exclude": [], "deprioritize": [], "known_risks": []}
def load_dual():
    return json.loads(DUAL.read_text(encoding="utf-8")) if DUAL.is_file() else {"decision": {}, "execution": {}, "proxy_mode": "split"}
def save_dual(d):
    CFG.mkdir(parents=True, exist_ok=True); DUAL.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
def read_key(kf):
    """两用:该值是文件路径就读文件;否则视为 API key 原文直接用。"""
    if not kf: return None
    p = Path(os.path.expanduser(kf))
    try:
        if p.is_file(): return (p.read_text().strip() or None)
    except Exception: pass
    return kf.strip() or None
def append_jsonl(path, row):
    CFG.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f: f.write(json.dumps(row, ensure_ascii=False) + "\n")
def read_jsonl(path, limit=None):
    if not path.is_file(): return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        try: out.append(json.loads(ln))
        except Exception: pass
    return out[-limit:] if limit else out

def check_model(model):
    m = load_map()
    def hit(entries, kind):
        out = []
        for e in entries:
            pat = e.get("match_re")
            ok = bool(re.search(pat, model, re.I)) if pat else (model in e.get("model", "") or e.get("model", "") in model)
            if ok: out.append({"kind": kind, **{k: e.get(k) for k in ("model", "reason", "source", "risk", "handling", "evidence")}})
        return out
    hx, hd, hr = hit(m.get("hard_exclude", []), "exclude"), hit(m.get("deprioritize", []), "deprioritize"), hit(m.get("known_risks", []), "risk")
    status = "exclude" if hx else ("risk" if hr else ("deprioritize" if hd else "clean"))
    return {"model": model, "status": status, "hits": hx or hr or hd,
            "hint": {"exclude": "已知交付失效,不建议使用", "risk": "已知风险,效果可能不好",
                     "deprioritize": "脚手架不耐受,效果可能不好", "clean": "不在名单内(≠推荐)"}[status]}

TASK_RULES = [
    ("代码", re.compile(r"代码|函数|脚本|bug|调试|接口|正则|sql|python|shell|编译|重构|程序", re.I), "#2563eb"),
    ("写作", re.compile(r"写一|写作|文稿|摘要|润色|翻译|改写|标题|报告|文案|邮件|周报|总结", re.I), "#7c3aed"),
    ("分析", re.compile(r"分析|对比|评估|日志|数据|统计|实验|设计|方案|排查|复盘|故障|检查", re.I), "#059669"),
    ("问答", re.compile(r"是什么|为什么|怎么|如何|解释|介绍|科普", re.I), "#d97706"),
]
def classify(text):
    for label, pat, color in TASK_RULES:
        if pat.search(text or ""): return {"label": label, "color": color, "key": label}
    return {"label": "通用", "color": "#6b7280", "key": "通用"}

def _ssl_ctx():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

def call_layer(layer_name, cfg, system_prompt, user_prompt, task_id):
    key = read_key(cfg.get("key_file", ""))
    if not key: return {"error": f"{layer_name}层 API 未配置或不可读"}
    body = json.dumps({"model": cfg.get("model", ""), "messages": [
        {"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        "temperature": 0.3, "max_tokens": 900}).encode()
    req = urllib.request.Request(cfg.get("base_url", "").rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300, context=_ssl_ctx()) as r: d = json.loads(r.read())
    except Exception as e:
        return {"error": repr(e)[:200]}
    ch = (d.get("choices") or [{}])[0]
    content = (ch.get("message") or {}).get("content") or ""
    u = d.get("usage", {})
    append_jsonl(USAGE, {"ts": time.strftime("%F %T"), "task_id": task_id, "layer": layer_name, "model": cfg.get("model", ""),
                         "prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0), "ok": bool(content)})
    return {"content": content, "prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0), "sec": round(time.time() - t0, 1)}

DECIDE_SYS = ("你是决策层:先想后写。输出结构化执行规格,分四块:【目标】一句话;【前置】需要什么;【步骤】编号列出每步一句;【验收】怎么算完成。不写成品正文,≤200 字。")
EXEC_SYS = "你是执行层:严格按给定规格完成任务,不改规格、不自由发挥。直接输出成品。"
JUDGE_SYS = "你是盲评裁判,只输出 JSON:{\"a\":0-100,\"b\":0-100}。两个方案匿名标 A/B,按任务与规格判正确性与适用性。"
def _clean(s):
    s = re.sub(r"\*\*([^\s*][^*]*?)\*\*", r"\1", s or "")
    s = re.sub(r"(?m)^#{1,6}\s*", "", s)
    return s
def _digest(s, n=80): return re.sub(r"\s+", " ", _clean(s))[:n]

TAGS = [("代码", "#2563eb"), ("写作", "#7c3aed"), ("分析", "#059669"), ("问答", "#d97706"), ("通用", "#6b7280")]

def _models_now():
    d = load_dual()
    return {"d": d.get("decision", {}).get("model", ""), "e": d.get("execution", {}).get("model", "")}

def _score_fresh(row):
    """分数有效 = 打过分(gain 非空)且打分时的两层模型与当前一致(换模型即过期)。"""
    if row.get("gain") is None: return False
    m = row.get("models")
    return bool(m) and (m.get("d"), m.get("e")) == (_models_now()["d"], _models_now()["e"])

def route_for_key(key):
    try:
        rec = json.loads(ROUTES.read_text(encoding="utf-8")) if ROUTES.is_file() else {}
        for row in rec.get("rows", []):
            if row.get("key") == key:
                if row.get("choice") == "ours":
                    if _score_fresh(row) and row.get("enabled", True): return "ours", "命中规则(双层分工)"
                    if not _score_fresh(row): return "passthrough", "分数未打或已过期(模型已换)"
                    return "passthrough", "规则已停用"
                return "passthrough", "规则=沿用原方式"
    except Exception: pass
    return "passthrough", "未匹配规则"

INIT = {"running": False, "done": 0, "total": 20, "line": "", "t0": 0, "ts": ""}

def api_init_rules():
    """首启初始化:五类各跑一次基准对比,生成默认处理规则(有加分才默认启用)。"""
    samples = {
        "代码": "写一个把秒数格式化为「时:分:秒」的 Python 函数,带边界处理",
        "写作": "给团队写一段 80 字的周报总结模板说明",
        "分析": "对比每日全量备份与增量备份两种策略,给出选型建议",
        "问答": "用三句话解释什么是负载均衡",
        "通用": "整理一份出差前的物品清单",
    }
    rows = []
    INIT.update(running=True, done=0, total=20, line="准备中…", t0=time.time())
    try:
        for key, task in samples.items():
            INIT["line"] = f"{key} · 决策层定规格…"
            p = api_plan(task); INIT["done"] += 1
            INIT["line"] = f"{key} · 双层产出…"
            e = {"error": p.get("error")} if "error" in p else api_execute(task, p.get("plan", ""))
            INIT["done"] += 1
            INIT["line"] = f"{key} · 原方式产出…"
            o = api_oneshot(task); INIT["done"] += 1
            INIT["line"] = f"{key} · 盲评打分…"
            j = ({"a": None, "b": None} if (e.get("error") or o.get("error"))
                 else api_judge(task, p.get("plan", ""), e.get("answer", ""), o.get("answer", "")))
            INIT["done"] += 1
            gain = (j.get("a") - j.get("b")) if (j.get("a") is not None and j.get("b") is not None) else None
            rows.append({"key": key, "item": task, "gain": gain,
                         "choice": "ours" if (gain or 0) > 0 else "user", "enabled": (gain or 0) > 0,
                         "scored": True, "models": _models_now()})
        commit_routes({"rows": rows})
        INIT["line"] = "完成 ✓"
        return {"ok": True, "rows": rows, "note": "打分完成;有加分的默认启用。分数绑定当前两层模型,换模型后失效需重新打分"}
    finally:
        INIT["running"] = False

def toggle_rule(data):
    key, enabled = data.get("key"), bool(data.get("enabled", True))
    try:
        rec = json.loads(ROUTES.read_text(encoding="utf-8")) if ROUTES.is_file() else {"rows": []}
        rec.setdefault("rows", [])
        hit = False
        for row in rec["rows"]:
            if row.get("key") == key:
                if enabled and not _score_fresh(row):
                    return {"ok": False, "error": "未打分或分数过期(模型已换)——先「按当前模型打分」再启用"}
                row["enabled"] = enabled
                if enabled: row["choice"] = "ours"
                hit = True
        if not hit and enabled:
            return {"ok": False, "error": "该类型还没打分——先「按当前模型打分」再启用"}
        if hit:
            ROUTES.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"ok": hit, "enabled": enabled}
    except Exception as e:
        return {"ok": False, "error": repr(e)[:80]}

def log_stage(task_id, source, stage, **kw):
    append_jsonl(DLOG, {"ts": time.strftime("%F %T"), "source": source, "task_id": task_id, "stage": stage, **kw})

# ---------------- 分段 API(真事件驱动) ----------------
def api_gen_items(work):
    dual = load_dual()
    r = call_layer("决策", dual.get("decision", {}),
                   "生成器:按用户主工作方向出 3 个真实工作项,每行一个,动宾短语 8-20 字,不编号不解释不加客套。"
                   "示例行:整理上线前检查清单 / 排查线上服务故障 / 编写数据处理脚本。"
                   "若方向太笼统、推断不出具体工作,只输出一行:NEED_MORE",
                   f"我的主要工作:{work}", "OPT")
    if "error" in r: return {"error": r["error"]}
    lines = [t.strip("-• 123.。,。") for t in (r["content"] or "").splitlines() if t.strip()]
    if any("NEED_MORE" in t for t in lines): return {"need_more": True}
    items = [t for t in lines if 4 <= len(t) <= 40][:3]
    bad = re.search(r"请补充|看出来|我再为你|抱歉|你好", r["content"] or "")
    if not items or bad: return {"need_more": True}
    return {"items": [{"key": classify(t)["key"], "text": t, "tag": classify(t)["label"]} for t in items]}

def api_plan(task):
    dual = load_dual()
    r = call_layer("决策", dual.get("decision", {}), DECIDE_SYS, task, "PLAN")
    return {"error": r["error"]} if "error" in r else {"plan": r["content"], "tok": r["prompt_tokens"] + r["completion_tokens"], "model": dual.get("decision", {}).get("model", "")}

def api_execute(task, plan):
    dual = load_dual()
    r = call_layer("执行", dual.get("execution", {}), EXEC_SYS, f"【任务】{task}\n【规格(必须遵守)】\n{plan}", "EXEC")
    return {"error": r["error"]} if "error" in r else {"answer": r["content"], "tok": r["prompt_tokens"] + r["completion_tokens"], "model": dual.get("execution", {}).get("model", "")}

def api_oneshot(task):
    dual = load_dual()
    r = call_layer("执行", dual.get("execution", {}), EXEC_SYS, task, "ONE")
    return {"error": r["error"]} if "error" in r else {"answer": r["content"], "tok": r["prompt_tokens"] + r["completion_tokens"]}

def api_judge(task, plan, a, b):
    dual = load_dual()
    r = call_layer("决策", dual.get("decision", {}), JUDGE_SYS,
                   f"【任务】{task}\n【规格】{_digest(plan,200)}\n【方案A】\n{_digest(a,600)}\n【方案B】\n{_digest(b,600)}", "JUDGE")
    if "error" in r: return {"error": r["error"]}
    m = re.search(r"\{.*\}", r["content"] or "", re.S)
    try: j = json.loads(m.group(0))
    except Exception: return {"error": "判官输出无法解析"}
    return {"a": j.get("a"), "b": j.get("b"), "tok": r["prompt_tokens"] + r["completion_tokens"]}

BUILTIN_SNAPSHOT = {  # 兜底快照:仅收录本项目有实据的模型 id(价目台账 2026-11-24 / 实验跑过),非实时
    "siliconflow.cn": {"公司": ["zai-org/GLM-5.3-Flash", "deepseek-ai/DeepSeek-V4-Flash", "tencent/Hy4-preview",
                        "inclusionAI/Ling-flash-2.0", "ByteDance-Seed/Seed-OSS-36B-Instruct", "Qwen3.5-4B"]},
    "deepseek.com": {"公司": ["deepseek-chat", "deepseek-reasoner"]},
    "openai.com": {"公司": ["gpt-5.6", "gpt-5.6-mini"]},
}

VENDOR_MAP = {"xiaomi":"小米","z-ai":"智谱","zhipu":"智谱","qwen":"通义","deepseek":"DeepSeek","openai":"OpenAI",
 "anthropic":"Anthropic","google":"Google","meta":"Meta","mistralai":"Mistral","microsoft":"Microsoft",
 "amazon":"AWS","nvidia":"NVIDIA","x-ai":"xAI","moonshot":"月之暗面","minimax":"MiniMax","stepfun":"阶跃",
 "inclusionai":"蚂蚁","bytedance":"字节","tencent":"腾讯","upstage":"Upstage","perplexity":"Perplexity",
 "cohere":"Cohere","alibaba":"阿里","baidu":"百度","iflytek":"讯飞","01-ai":"零一","baichuan":"百川"}

def api_aggregate():
    """聚合源:OpenRouter 公开 /models(2026-11-28 实测 477 个,无需 key)。"""
    try:
        req = urllib.request.Request("https://openrouter.ai/api/v1/models")
        with urllib.request.urlopen(req, timeout=30, context=_ssl_ctx()) as r:
            d = json.loads(r.read())
        ids = sorted({m.get("id", "") for m in d.get("data", []) if m.get("id")})
        ids = [i for i in ids if not re.search(r"bge|rerank|embed|whisper|tts|wav2vec|stable", i, re.I)]
        return {"source": "aggregate", "source_note": f"实时聚合目录 OpenRouter /models({len(ids)} 个对话模型,按厂商分组)",
                "models": ids}
    except Exception as e:
        return {"source": "none", "source_note": f"聚合目录拉取失败({repr(e)[:80]})", "models": []}

def vendor_of(mid):
    pre = (mid.split("/")[0] if "/" in mid else mid).lower()
    for k, v in VENDOR_MAP.items():
        if k in pre: return v
    return pre[:1].upper() + pre[1:] if pre else "其他"

def api_models(base_url, key_file, vendor_hint=""):
    """主源:GET {base_url}/v1/models(平台官方目录,实时);失败回退内置快照并明示。"""
    key = read_key(key_file)
    url = base_url.rstrip("/")
    if not url.endswith("/v1"): url += "/v1"
    if key:
        try:
            req = urllib.request.Request(url + "/models", headers={"Authorization": "Bearer " + key, "x-api-key": key, "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=20, context=_ssl_ctx()) as r:
                d = json.loads(r.read())
            ids = sorted({m.get("id", "") for m in d.get("data", []) if m.get("id")})
            if ids: return {"source": "platform", "source_note": f"来自平台实时目录 {url}/models", "models": ids}
        except Exception as e:
            err = repr(e)[:100]
    else:
        err = "未配置 key"
    if vendor_hint:
        agg = api_aggregate()
        vv = vendor_hint.lower()
        hits = [i for i in agg.get("models", []) if vendor_of(i).lower() == vv or vv in vendor_of(i).lower()]
        if hits:
            return {"source": "aggregate_vendor", "source_note": f"该平台目录未拉到({err});以下为聚合目录中的「{vendor_hint}」模型(实时)", "models": hits}
    host = next((k for k in BUILTIN_SNAPSHOT if k in base_url), None)
    if host:
        return {"source": "builtin", "source_note": f"平台目录拉取失败({err}),以下为内置快照(2026-11-28 取数,非实时)",
                "models": BUILTIN_SNAPSHOT[host]["公司"]}
    return {"source": "none", "source_note": f"平台目录拉取失败({err}),也无内置快照——可手动输入模型 ID", "models": []}

def usage_summary():
    s = {}
    for r in read_jsonl(USAGE):
        d = s.setdefault(r.get("layer", "执行"), {"in": 0, "out": 0, "calls": 0})
        d["in"] += r.get("prompt_tokens", 0); d["out"] += r.get("completion_tokens", 0); d["calls"] += 1
    for k in ("决策", "执行", "优化", "生成", "盲评"): s.setdefault(k, {"in": 0, "out": 0, "calls": 0})
    return s

def commit_routes(data):
    stamp = _models_now()
    rows = [{"item": r.get("item", ""), "key": r.get("key") or classify(r.get("item", ""))["key"],
             "gain": r.get("gain"), "choice": r.get("choice", "hold"),
             "scored": True, "models": stamp} for r in data.get("rows", [])]
    rec = {"ts": time.strftime("%F %T"), "rows": rows}
    CFG.mkdir(parents=True, exist_ok=True)
    ROUTES.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    append_jsonl(OPTLOG, rec)
    return {"ok": True, "saved": len(rows)}

def run_split(task_text, source):
    """双层机制:真实事件流(决策开始→计划→交接→执行请求→返回)。"""
    tid = f"T{int(time.time()*1000)%1000000}"
    tag = classify(task_text)
    log_stage(tid, source, "decision_started", tag=tag["label"], task=_digest(task_text, 150))
    p = api_plan(task_text)
    if "error" in p:
        log_stage(tid, source, "decision_failed", error=p["error"][:80], tag=tag["label"])
        return {"task_id": tid, "tag": tag, "error": p["error"]}
    log_stage(tid, source, "plan_ready", tag=tag["label"], task=_digest(task_text, 150),
              decision={"model": p.get("model", ""), "tok": p["tok"], "plan": _clean(p["plan"])})
    log_stage(tid, source, "exec_requested", tag=tag["label"])
    e = api_execute(task_text, p["plan"])
    status = "ok" if "error" not in e else "exec_failed"
    steps = [s.strip("-• ") for s in (p["plan"] or "").splitlines() if s.strip()][:6]
    log_stage(tid, source, "exec_done", tag=tag["label"], task=_digest(task_text, 150), status=status,
              decision={"model": p.get("model", ""), "tok": p["tok"], "plan": _clean(p["plan"])},
              execution={"model": e.get("model", ""), "tok": e.get("tok", 0), "answer": _clean(_digest(e.get("answer", ""), 400)), "steps": steps},
              route="双层机制", route_reason="命中规则" if route_for_key(tag["key"])[0] == "ours" else "试跑/默认",
              summary=f"识别「{tag['label']}」→决策层出规格→执行层产出 {len(e.get('answer') or '')} 字")
    return {"task_id": tid, "tag": tag, "plan": _clean(p["plan"]), "answer": e.get("answer", ""), "error": e.get("error"),
            "usage": {"决策": p["tok"], "执行": e.get("tok", 0)}}

def run_passthrough(task_text, source, reason):
    """沿用原方式:收到→转发→返回,不拆分、不为判路调用模型(判路只用正则)。"""
    tid = f"P{int(time.time()*1000)%1000000}"
    tag = classify(task_text)
    dual = load_dual()
    log_stage(tid, source, "pass_received", tag=tag["label"], task=_digest(task_text, 150))
    r = call_layer("执行", dual.get("execution", {}), "", task_text, tid)
    log_stage(tid, source, "pass_done", tag=tag["label"], task=_digest(task_text, 150), status="ok" if "error" not in r else "failed",
              route="沿用原方式", route_reason=reason,
              passthrough={"model": dual.get("execution", {}).get("model", ""), "tok": r.get("prompt_tokens", 0) + r.get("completion_tokens", 0),
                           "answer": _clean(_digest(r.get("content", ""), 300))},
              summary=f"识别「{tag['label']}」→{reason}→沿用原方式(收到→转发→返回)")
    return {"task_id": tid, "tag": tag, "answer": r.get("content", ""), "error": r.get("error")}

PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>switchkit</title><style>
*{box-sizing:border-box}
:root{--bg:#eef1f5;--card:#fff;--line:#e6e9ef;--text:#1f2937;--sub:#6b7280;--accent:#2563eb;--ok:#059669;--warn:#d97706;--bad:#b91c1c}
body{font-family:-apple-system,"PingFang SC",sans-serif;margin:0;background:var(--bg);color:var(--text);display:flex;height:100vh;overflow:hidden}
.side{width:230px;background:#f7f8fa;border-right:1px solid var(--line);padding:20px 12px;flex-shrink:0;display:flex;flex-direction:column}
.brand{display:flex;align-items:center;gap:10px;margin:2px 12px 18px}
.logo{width:30px;height:30px;border-radius:8px;background:linear-gradient(135deg,#2563eb,#7c3aed);display:flex;align-items:center;justify-content:center;color:#fff;font-size:15px}
.brand b{font-size:17px;letter-spacing:.3px}
.grp{font-size:10.5px;letter-spacing:2px;color:#94a3b8;margin:14px 12px 5px}
.nav button{display:flex;align-items:center;gap:10px;width:100%;background:transparent;border:0;color:#475569;text-align:left;padding:9px 12px;border-radius:8px;font-size:13.5px;cursor:pointer}
.nav button.on{background:#e2e8f0;color:#0f172a;font-weight:600}
.nav button:hover{background:#eceff4}
.side .foot{margin-top:auto;font-size:11px;color:#94a3b8;padding:8px 12px}
.main{flex:1;overflow-y:auto;padding:26px 34px 70px}
.panel .card,.panel .banner,.panel .sect,.panel .console,.panel .slider,.panel .about-card{max-width:1120px}
.panel>table{max-width:1120px}
.phead{max-width:1120px}
.panel{display:none} .panel.on{display:block}
.phead{display:flex;align-items:center;gap:10px;margin-bottom:20px}
.phead h2{font-size:21px;margin:0;color:#0f172a}
.chip{font-size:11.5px;padding:3px 11px;border-radius:999px;background:#e2e8f0;color:#475569;margin-left:auto}
.chip.on{background:#dcfce7;color:#15803d}
.sect{font-size:13.5px;font-weight:700;color:#334155;margin:26px 2px 10px;display:flex;align-items:center;gap:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:0 0 14px}
.row{display:flex;align-items:center;gap:14px;padding:14px 18px;border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:0}
.row .lab{font-size:13px;font-weight:600;color:#334155;min-width:120px;display:flex;align-items:center;gap:6px}
.row .act{margin-left:auto;display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.row .hint{font-size:11px;color:#94a3b8;line-height:1.7;margin-top:4px}
.rowcol{display:block}
.rowcol .hint{margin-top:4px}
input,textarea,select{padding:8px 11px;border:1px solid #d1d5db;border-radius:8px;font-size:13px;background:#fff}
.row input{width:min(330px,100%)} .row textarea{width:min(520px,100%);min-height:76px;resize:vertical}
.row{flex-wrap:wrap} .row .act{max-width:100%;min-width:0}
.keyfield{-webkit-text-security:disc;text-security:disc;font-family:ui-monospace,Menlo,monospace;letter-spacing:1px}
.keyfield:hover,.keyfield:focus{-webkit-text-security:none;letter-spacing:0}
.toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;font-size:12px;padding:8px 16px;border-radius:8px;z-index:99999;display:none}
.keyview{display:block;max-width:min(420px,100%);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,Menlo,monospace;font-size:12px;background:#f4f6f9;border:1px solid var(--line);border-radius:8px;padding:7px 12px;cursor:pointer;user-select:all;margin-top:6px}
.keyview:hover{border-color:#93b4e8}
button.iconbtn{font-size:12px;font-weight:600}
textarea{padding:9px 11px;border:1px solid #d1d5db;border-radius:8px;font-size:13px;background:#fff}
button.pri{border:0;border-radius:8px;padding:9px 18px;font-size:13px;cursor:pointer;background:var(--accent);color:#fff;font-weight:600;transition:filter .15s ease,transform .1s ease}
button.pri:hover:not(:disabled){filter:brightness(1.08)} button.pri:active:not(:disabled){transform:translateY(1px)}
button.out{transition:border-color .15s ease,background .15s ease} button.out:hover{background:#f4f6f9}
button.green{transition:filter .15s ease} button.green:hover{filter:brightness(1.08)}
.row:hover{background:#fafbfd} .listrow{transition:background .15s ease} .listrow:hover{background:#fafbfd}
input:focus,textarea:focus,select:focus{outline:2px solid #93b4e8;outline-offset:1px;border-color:#93b4e8}
.chipx{display:inline-block;padding:2.5px 10px;border-radius:999px;font-size:11px;background:#eef1f5;color:#475569;cursor:pointer;border:1px solid var(--line)}
.chipx:hover{border-color:#93b4e8;color:#1d4ed8}
button.pri:disabled{opacity:.45}
button.out{background:#fff;color:#334155;border:1px solid #d1d5db;border-radius:8px;padding:8px 15px;font-size:12.5px;cursor:pointer}
button.out:hover{border-color:#94a3b8}
button.green{background:var(--ok);color:#fff;border:0;border-radius:8px;padding:9px 18px;font-size:13px;cursor:pointer;font-weight:600}
button.mini{background:#eef1f5;border:0;border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer;color:#475569}
.seg{display:inline-flex;background:#eef1f5;border-radius:9px;padding:3px}
.seg button{border:0;background:transparent;padding:6px 14px;font-size:12px;border-radius:7px;cursor:pointer;color:#475569}
.seg button.on{background:#fff;color:#0f172a;font-weight:600;box-shadow:0 1px 3px rgba(15,23,42,.12)}
.inlinehint{font-size:11.5px;color:#94a3b8}
.q{display:inline-flex;width:15px;height:15px;border-radius:50%;background:#b6c2d2;color:#fff;font-size:9.5px;align-items:center;justify-content:center;cursor:help;position:relative;top:-1px;flex-shrink:0}

.banner{display:flex;align-items:center;gap:12px;background:#f0f6ff;border:1px solid #cfe0f7;color:#1e40af;border-radius:10px;padding:13px 18px;font-size:12.5px;line-height:1.8;flex-wrap:wrap}
.empty{border:1.5px dashed #cbd5e1;border-radius:10px;padding:26px 22px;text-align:center;color:#94a3b8;font-size:12.5px;margin:6px 0}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
/* 运行台三窗 */
.console{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:0 0 14px}
.chead{display:flex;align-items:center;gap:11px;padding:13px 18px;border-bottom:1px solid var(--line)}
.tag{display:inline-block;padding:2.5px 12px;border-radius:999px;color:#fff;font-size:11px;font-weight:600}
.routechip{padding:2.5px 11px;border-radius:999px;font-size:10.5px;font-weight:600;background:#e2e8f0;color:#475569}
.routechip.ours{background:#dbeafe;color:#1d4ed8} .routechip.user{background:#fef3c7;color:#b45309}
.chead .ts{margin-left:auto;color:#94a3b8;font-size:11px}
.cduo{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line)}
.cbox{background:#fff;padding:15px 18px;min-height:128px}
.cbox h4{margin:0 0 8px;font-size:11px;letter-spacing:1.2px;color:#64748b;display:flex;align-items:center;gap:7px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.cbox .body{white-space:pre-wrap;line-height:1.95;font-size:12.5px;color:#334155;overflow-wrap:anywhere}
.cfull{padding:12px 18px;border-top:1px solid var(--line);min-height:46px;font-size:12.5px;color:#475569}
.cfoot{padding:11px 18px;background:#0f1c33;color:#c7d2e4;font-size:11.5px;display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.cfoot b{color:#7dd3fc}
.spin{display:inline-block;width:12px;height:12px;border:2.5px solid #cbd5e1;border-top-color:var(--accent);border-radius:50%;animation:rot .8s linear infinite}
@keyframes rot{to{transform:rotate(360deg)}}
.stage{opacity:.4}.stage.act{opacity:1;color:#1d4ed8;font-weight:600}.stage.done{opacity:.92}
.pass{color:#94a3b8}
.histrow{display:flex;gap:10px;padding:9px 13px;background:#fff;border:1px solid var(--line);border-radius:9px;margin:6px 0;cursor:pointer;align-items:center;font-size:12px}
.histrow:hover{border-color:#93b4e8}
.histrow .tag{min-width:52px;text-align:center}
/* 窄窗降级 */
@media (max-width:780px){
  .cduo{grid-template-columns:1fr} .grid2{grid-template-columns:1fr}
  .row input{width:100%} .row .act{width:100%;justify-content:flex-start}
  .side{width:186px} .main{padding:20px 18px 60px}
}
.banner2{background:#eef4ff;color:#1d4ed8;font-size:12px;padding:9px 16px;display:flex;align-items:center;gap:12px}
/* 向导 */
.wizhead{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.pickcard{display:flex;gap:11px;align-items:flex-start;border:1.5px solid var(--line);border-radius:10px;padding:12px 14px;margin:8px 0;cursor:pointer;background:#fff}
.pickcard.sel{border-color:var(--accent);background:#f5f9ff}
.pickcard input{width:auto;margin-top:3px}
.bar{height:6px;background:#e5e7eb;border-radius:99px;overflow:hidden;margin:10px 0}
.bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .35s}
.steps{margin-top:8px}
.step{display:flex;align-items:center;gap:10px;padding:7px 12px;border-radius:8px;font-size:12.5px;color:#6b7280;line-height:1.7}
.step.run{background:#eff6ff;color:#1d4ed8}.step.done{color:var(--ok)}
.notice{margin-top:12px;padding:11px 15px;border-radius:9px;font-size:12.5px;line-height:1.8}
.notice.ok-n{background:#ecfdf5;color:#047857;border:1px solid #a7f3d0}
.notice.warn-n{background:#fff7ed;color:#c2410c;border:1px solid #fdba74}
table{width:100%;border-collapse:collapse;font-size:12.5px}
td,th{border-bottom:1px solid var(--line);padding:10px 8px;text-align:left;vertical-align:middle}
th{font-size:11.5px;letter-spacing:.6px;color:#64748b;font-weight:600;background:#fafbfd}
td.num,th.num{text-align:right}
table td:last-child select{max-width:150px}
.gain{font-weight:700}.gain.up{color:var(--ok)}.gain.dn{color:var(--bad)}.gain.eq{color:#6b7280}
.sw{position:relative;display:inline-block;width:38px;height:21px;flex-shrink:0}
.sw input{opacity:0;width:0;height:0}
.swslider{position:absolute;inset:0;background:#cbd5e1;border-radius:99px;transition:.2s;cursor:pointer}
.swslider::before{content:'';position:absolute;width:17px;height:17px;border-radius:50%;background:#fff;top:2px;left:2px;transition:.2s}
.sw input:checked+.swslider{background:var(--accent)}
.sw input:checked+.swslider::before{transform:translateX(17px)}
/* 教程 */
.slider{position:relative;overflow:hidden;border-radius:12px;border:1px solid var(--line)}
.slides{display:flex;transition:transform .45s}
.slide{min-width:100%;padding:32px 40px;background:#fff;min-height:330px}
.slide h3{margin:0 0 14px;font-size:18px}
.slide p{font-size:13.5px;line-height:2.15;color:#475569;margin:0 0 10px}
.snav{display:flex;justify-content:center;gap:8px;padding:12px}
.dotn{width:8px;height:8px;border-radius:50%;background:#cbd5e1;cursor:pointer}
.dotn.on{background:var(--accent);width:22px;border-radius:99px}
.sarrow{position:absolute;top:50%;transform:translateY(-50%);background:rgba(15,23,42,.32);color:#fff;border:0;width:36px;height:36px;border-radius:50%;cursor:pointer;font-size:15px;z-index:3}
.sarrow.l{left:10px}.sarrow.r{right:10px}
.replay{position:absolute;right:16px;top:14px}
.dia{display:flex;align-items:center;gap:10px;margin:16px 0;flex-wrap:wrap}
.node{border:1.5px solid #93b4e8;border-radius:9px;padding:8px 14px;font-size:12px;background:#f5f9ff;color:#1d4ed8}
.node.dest{border-color:#34d399;background:#ecfdf5;color:#047857}
.flowline{width:44px;height:2px;background:#93b4e8;position:relative;overflow:hidden}
.flowline::after{content:'';position:absolute;width:12px;height:12px;border-radius:50%;background:var(--accent);top:-5px;animation:flow 1.6s linear infinite}
@keyframes flow{from{left:-12px}to{left:44px}}
/* 关于 */
.about-card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:26px 28px;max-width:720px}
.about-top{display:flex;align-items:center;gap:16px;padding-bottom:18px;border-bottom:1px solid var(--line)}
.about-top .logo{width:52px;height:52px;border-radius:13px;font-size:26px}
.about-links{display:flex;gap:10px;padding-top:18px;flex-wrap:wrap}
.meter{display:flex;gap:30px;flex-wrap:wrap}.meter b{font-size:21px;display:block}
.num{font-variant-numeric:tabular-nums} .red{color:#b91c1c}

.listrow{display:flex;align-items:center;gap:14px;padding:13px 18px;border-bottom:1px solid var(--line)}
.listrow:last-child{border-bottom:0}
.tile{width:38px;height:38px;border-radius:9px;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:15px;flex-shrink:0}
.lmain b{font-size:13.5px}
.badge{display:inline-block;padding:1.5px 9px;border-radius:999px;font-size:10.5px;font-weight:600;margin-left:8px}
.badge.ours{background:#dcfce7;color:#15803d} .badge.user{background:#f1f5f9;color:#64748b}
body.comfy .row{padding:19px 18px}
.livelog{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:#475569;background:#f4f6f9;border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin-top:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis} body.comfy .listrow{padding:17px 18px} body.comfy .cbox{padding:19px 20px}
.iconbtn{width:32px;height:32px;border:1px solid #d1d5db;border-radius:8px;background:#fff;cursor:pointer;font-size:13px;color:#475569}
.iconbtn:hover{border-color:#94a3b8}
.desc{font-size:12px;color:#6b7280;line-height:1.9;margin:0 0 12px}
button.pri.save{background:#e08b4e} button.pri.save:enabled{background:#e07b2e}
button.pri.save:disabled{background:#e8c9ae;color:#fff;opacity:1}
/* 接入工具 */
#clientbox .listrow{align-items:flex-start;flex-wrap:wrap}
#clientbox .inlinehint{margin:2px 0 0;line-height:1.7}
.clres{flex-basis:100%;margin:2px 0 0 26px}
.clgray{opacity:.5}
/* 选它按钮反馈 */
@keyframes flash{0%{transform:scale(1)}30%{transform:scale(1.14);box-shadow:0 0 0 5px rgba(37,99,235,.28)}100%{transform:scale(1)}}
.flash{animation:flash .55s ease}
.askstrip{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
</style></head><body>
<div class="side">
  <div class="brand"><div class="logo">✳</div><b>switchkit</b></div>
  <div class="nav">
    <div class="grp">常用</div>
    <button class="on" onclick="show('log',this)">📋 运行台</button>
    <button onclick="show('opt',this)">🧪 根据主要任务优化</button>
    <button onclick="show('tpl',this)">🗂 处理规则</button>
    <div class="grp">设置</div>
    <button onclick="show('cfg',this)">⚙️ 连接设置</button>
    <button onclick="show('check',this)">🔍 模型检查</button>
    <button onclick="show('meter',this)">🧮 用量与费用</button>
    <div class="grp">帮助</div>
    <button onclick="show('guide',this)">📖 新手教程</button>
    <button onclick="show('about',this)">ℹ️ 关于</button>
  </div>
  <div class="foot">switchkit v0.10</div>
</div>
<div class="main">

<div class="panel on" id="p-log">
  <div class="phead"><h2>📋 运行台</h2><span class="chip on">● 分发服务运行中</span></div>
  <div class="banner">把你的工具的 base_url 指到下面这个地址,请求就会自动流经这里。
    <span class="q" tabindex="0" data-tip="每条请求在这里可视化:&#10;上排两窗=决策层怎么想、执行层怎么做;&#10;下排=沿用原方式时的简略过程;&#10;页首标明这次走哪条路、为什么。">?</span>
    <button class="green" style="margin-left:auto;padding:7px 15px" onclick="show('guide',document.querySelectorAll('.nav button')[6])">查看连接步骤</button>
    <button class="out" onclick="selfTest()">⚡ 自测一次</button>
    <button class="out" onclick="toggleClients()">🔌 接入工具</button></div>
  <div class="card" id="clientbox" style="display:none;padding:0">
    <div class="sect" style="padding:12px 18px 2px">检测到的客户端(能用的点「启用」一键代配;不能正常用的置灰只展示;只改端点,不动 key;改前自动备份、可一键还原)
      <span class="q" tabindex="0" data-tip="自动代配=把下面的代理端点写进工具的通道配置;&#10;写入前留带时间戳的副本,每行可「还原」;&#10;不支持自定义端点的工具只展示、给手动步骤。">?</span></div>
    <div id="clientlist"></div>
    <div style="display:flex;gap:8px;padding:12px 18px;align-items:center;flex-wrap:wrap">
      <button class="mini" onclick="loadClients()">重新检测</button>
      <span class="inlinehint" id="clientmsg"></span></div>
  </div>
  <div class="card">
    <div class="row"><div class="lab rowcol">代理端点</div>
      <div class="act"><input id="ep" value="http://127.0.0.1:8787/v1" style="width:330px"><button class="iconbtn" title="恢复本机实际地址" onclick="ep.value='http://'+location.host+'/v1'">↺</button></div></div>
    <div class="row"><div class="lab rowcol">分流模式 <span class="q" tabindex="0" data-tip="按规则分流=读「处理规则」决定拆不拆双层;&#10;强制直通为调试用。">?</span></div>
      <div class="act"><div class="seg" id="segmode">
        <button class="on" onclick="segMode('split',this)">按规则分流</button>
        <button onclick="segMode('execution',this)">直通执行层</button>
        <button onclick="segMode('decision',this)">直通决策层</button>
      </div><select id="pmode" style="display:none"><option value="split">split</option><option value="execution">execution</option><option value="decision">decision</option></select></div></div>
  </div>
  <div class="console" id="console"></div>
  <div class="card" id="quickstart" style="padding:12px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
    <span class="inlinehint"><b>3 步连通:</b>①复制代理端点 ②贴到你工具的 base_url ③发条请求回来看 ——</span>
    <button class="mini" onclick="show('guide',document.querySelectorAll('.nav button')[6])">看教程</button>
    <button class="mini" onclick="selfTest()">或直接自测一次</button>
  </div>
  <div class="sect">历史记录(点击查看,不重跑)</div>
  <div id="hist"></div>
</div>

<div class="panel" id="p-opt">
  <div class="phead"><h2>🧪 根据主要任务优化</h2><span class="chip">优化结果自动写入处理规则</span></div>
  <div class="banner">说说你平时主要做什么,机器识别工作类型、逐项对比、把结果写进规则。</div>
  <div class="card" style="padding:18px">
    <div class="wizhead"><span id="wizdots"></span><b id="wizlabel" style="margin-left:auto;font-size:12px;color:#6b7280"></b></div>
    <div class="wiz" id="w0">
      <label style="font-size:13px;font-weight:600">你想优化哪类工作?</label>
      <div class="inlinehint" style="margin:2px 0 8px">写得越具体,生成的工作项越准。</div>
      <textarea id="work" placeholder="如:运维——上线前检查、故障排查、写自动化脚本、整理值班交接" style="width:100%"></textarea>
      <div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">
        <span class="chipx" onclick="fillWork('运维——上线前检查、故障排查、写自动化脚本、整理值班交接')">运维</span>
        <span class="chipx" onclick="fillWork('开发——写业务代码、修 bug、写单元测试、代码评审')">开发</span>
        <span class="chipx" onclick="fillWork('数据分析——清洗数据、出统计图表、写分析周报')">数据分析</span>
        <span class="chipx" onclick="fillWork('日常办公——写周报邮件、整理会议纪要、做汇报 PPT 大纲')">办公</span>
      </div>
      <div style="margin-top:12px"><button class="pri" onclick="stepItems()">生成工作项 →</button><span class="inlinehint" id="optmsg" style="margin-left:12px"></span></div>
    </div>
    <div class="wiz" id="w1" style="display:none">
      <div class="card" id="needbox" style="display:none;background:#fff7ed;border:1.5px solid #fdba74;padding:14px 16px">
        <b style="color:#c2410c">请先补充你的主要方向</b>
        <div class="inlinehint" id="needmsg" style="font-size:12.5px;color:#9a3412"></div>
      </div>
      <label style="font-size:13px;font-weight:600">勾选要优化的工作项(默认不选,至少一项)</label>
      <div id="itemcards"></div>
      <div style="display:flex;gap:10px;align-items:center;margin-top:10px">
        <input id="customItem" placeholder="没列到的?在这里补充你的工作项,回车添加" style="flex:1" onkeydown="if(event.key==='Enter')addCustomItem()">
        <button class="out" onclick="addCustomItem()">＋ 添加</button>
      </div>
      <div class="inlinehint" style="margin-top:4px">添加的项会自动打勾并参与对比。</div>
      <div style="margin-top:12px"><button class="pri" id="cmpbtn" disabled onclick="stepCompare()">开始优化 →</button>
        <button class="out" id="backbtn" onclick="showWiz(0)" style="margin-left:10px">上一步</button></div>
    </div>
    <div class="wiz" id="w2" style="display:none">
      <div class="bar"><i id="optbar"></i></div>
      <div class="steps" id="optsteps"></div>
      <div id="resultcard" style="display:none">
        <div id="adoptrow" style="display:none;margin:8px 0"><button class="out" onclick="adoptBest()">⭐ 一键采纳推荐(加分→双层,其余→沿用)</button>
          <span class="inlinehint" style="margin-left:10px">推荐已按对比分预选,可逐项改</span></div>
        <table id="rtab"></table></div>
      <div style="margin-top:12px"><button class="pri" id="nextbtn" disabled onclick="showWiz(3)">下一步:确认 →</button>
        <button class="out" onclick="showWiz(1)" style="margin-left:10px">上一步</button>
        <span class="inlinehint" id="cmpnote" style="margin-left:10px"></span></div>
    </div>
    <div class="wiz" id="w3" style="display:none">
      <label style="font-size:13px;font-weight:600">确认写入规则:识别到的每类工作,以后就按优化结果走</label>
      <div id="previewbox"></div>
      <div style="margin-top:12px"><button class="pri" onclick="commitRoutes()" id="savebtn">保存规则</button>
        <button class="out" onclick="showWiz(2)" style="margin-left:10px">上一步</button></div>
      <div class="notice" id="commitmsg" style="display:none"></div>
    </div>
  </div>
</div>

<div class="panel" id="p-tpl">
  <div class="phead"><h2>🗂 处理规则</h2><span class="chip">请求按此分流</span>
    <span class="q" tabindex="0" data-tip="处理规则 = switchkit 的工作机制:&#10;请求进来先识别工作类型(代码开发/文稿写作/分析排查/问答解释/日常事务),&#10;命中「走机制」的自动拆成决策层出方案 + 执行层出成品;&#10;其余走「沿用原方式」原样转发。&#10;「对比分」=你打分后才有,且绑定打分时的两层模型;&#10;没打分/换过模型的规则不能启用。">?</span></div>
  <div class="banner">先在「模型检查」选好模型、验证后填入两层,然后回这里<b>打分</b>;打了分、模型没换的规则才可启用,换过模型要重新打分。</div>
  <div class="card">
    <div class="row" style="flex-wrap:wrap"><div class="lab rowcol">打分</div>
      <div class="act"><button class="pri" onclick="initRules()" id="initbtn">按当前模型打分(试跑对比)</button>
        <span class="inlinehint" id="initmsg">五类工作各试跑一次对比(约 20 次调用,视模型速度 2-5 分钟);分数绑定当前两层模型,换了模型需重新打分</span></div>
      <div style="width:100%;margin-top:10px;display:none" id="initwrap">
        <div class="bar"><i id="initbar"></i></div>
        <div class="livelog" id="initlog">…</div>
      </div></div>
  </div>
  <div id="tpls"></div>
</div>

<div class="panel" id="p-cfg">
  <div class="phead"><h2>⚙️ 连接设置</h2><span class="chip">BYOK · 只存文件路径</span>
    <span class="q" tabindex="0" data-tip="两层各配一组:&#10;决策层=强推理模型(出方案);&#10;执行层=可便宜模型(出成品)。&#10;key 填「保存 key 的文件路径」,不填 key 正文。">?</span></div>
  <div class="sect">决策层(负责「想」)</div>
  <div class="card">
    <div class="row"><div class="lab rowcol">base_url <span class="q" tabindex="0" data-tip="OpenAI 兼容接口地址,结尾 /v1;&#10;填完自动识别平台并推荐常用模型。">?</span></div>
      <div class="act"><input id="db" placeholder="https://api.siliconflow.cn/v1" oninput="detectPlatform('db','dp')"></div>
      <div class="hint" id="dp" style="max-width:min(460px,100%)"></div></div>
    <div class="row"><div class="lab rowcol">模型 ID <span class="q" tabindex="0" data-tip="决策层出大纲、定判据,建议用强推理模型。">?</span></div>
      <div class="act"><input id="dm" list="models" oninput="hintModel('dm','dh')" placeholder="zai-org/GLM-5.3-Flash"></div>
      <div class="hint" id="dh"></div></div>
    <div class="row"><div class="lab rowcol">API <span class="q" tabindex="0" data-tip="保存 API 的本机文件路径,如 ~/.config/siliconflow/api_key。&#10;工具只读该文件:不上传、不入库、不代付。">?</span></div>
      <div class="act"><input id="dk" class="keyfield" style="width:min(360px,100%)" placeholder="粘贴 API" oninput="markDirtySafe()" onclick="copyField('dk')"></div></div>
  </div>
  <div class="sect">执行层(负责「做」)</div>
  <div class="card">
    <div class="row"><div class="lab rowcol">base_url <span class="q" tabindex="0" data-tip="同上;两层可用同一家或不同家。">?</span></div>
      <div class="act"><input id="eb" placeholder="https://api.siliconflow.cn/v1" oninput="detectPlatform('eb','ep2')"></div>
      <div class="hint" id="ep2" style="max-width:min(460px,100%)"></div></div>
    <div class="row"><div class="lab rowcol">模型 ID <span class="q" tabindex="0" data-tip="按规格出成品,可选便宜模型。&#10;注意:代码生成慎用 GLM-5.3-Flash(已知卡流风险)。">?</span></div>
      <div class="act"><input id="em" list="models" oninput="hintModel('em','eh')" placeholder="deepseek-ai/DeepSeek-V4-Flash"></div>
      <div class="hint" id="eh"></div></div>
    <div class="row"><div class="lab rowcol">API <span class="q" tabindex="0" data-tip="同上,填文件路径而非 key 本身。">?</span></div>
      <div class="act"><input id="ek" class="keyfield" style="width:min(360px,100%)" placeholder="粘贴 API" oninput="markDirtySafe()" onclick="copyField('ek')"></div></div>
  </div>
  <datalist id="models"></datalist>
  <div class="card">
    <div class="row"><div class="lab">保存</div>
      <div class="act"><button class="out" onclick="pingAll()">🔌 测试连接</button>
      <button class="pri save" id="savecfg" disabled onclick="saveCfg()">保存连接设置</button><span class="inlinehint" id="cfgmsg"></span></div></div>
    <div class="row"><div class="lab">界面密度 <span class="q" tabindex="0" data-tip="紧凑=信息更密;舒适=留白更多。&#10;立即生效并记住选择。">?</span></div>
      <div class="act"><div class="seg" id="segden">
        <button class="on" onclick="setDensity('compact')">紧凑</button>
        <button onclick="setDensity('comfy')">舒适</button></div></div></div>
  </div>
</div>

<div class="panel" id="p-check">
  <div class="phead"><h2>🔍 模型检查</h2><span class="chip">两层实测 · 定它放哪层</span>
    <span class="q" tabindex="0" data-tip="两步用法:&#10;① 下方「模型目录」按平台分组列出主流模型,点一行即选中;&#10;② 自动跳到实测区:测它在决策层/执行层各打多少分,并推荐放哪层。&#10;评分口径:判定分(通过90/能用但弱70/卡流30/失败0)− 延迟扣分(每秒0.5分,上限15)。&#10;目录主源=各平台官方 /models 接口(实时)。">?</span></div>
  <div class="sect">模型目录(点选即体检)</div>
  <div class="card">
    <div class="row"><div class="lab rowcol">平台</div>
      <div class="act">
        <select id="mplat" style="width:300px">
          <option value="__aggregate__">🌐 全部主流(聚合目录 · 覆盖最广)</option>
          <option value="https://api.siliconflow.cn/v1|~/.config/siliconflow/api_key|硅基">硅基流动 SiliconFlow</option>
          <option value="https://api.deepseek.com/v1|~/.config/deepseek/api_key|DeepSeek">DeepSeek(深度求索)</option>
          <option value="https://api.openai.com/v1|~/.config/openai/api_key|OpenAI">OpenAI(GPT)</option>
          <option value="https://api.anthropic.com/v1|~/.config/anthropic/api_key|Anthropic">Anthropic(Claude)</option>
          <option value="https://generativelanguage.googleapis.com/v1beta|~/.config/google/api_key|Google">Google(Gemini)</option>
          <option value="https://open.bigmodel.cn/api/paas/v4|~/.config/zhipu/api_key|智谱">智谱(GLM)</option>
          <option value="https://dashscope.aliyuncs.com/compatible-mode/v1|~/.config/qwen/api_key|通义">阿里通义(Qwen)</option>
          <option value="https://api.mimo.dev/v1|~/.config/mimo/api_key|小米">小米(MiMo)</option>
          <option value="https://api.minimax.chat/v1|~/.config/minimax/api_key|MiniMax">MiniMax</option>
          <option value="https://api.moonshot.cn/v1|~/.config/moonshot/api_key|月之暗面">月之暗面(Kimi)</option>
          <option value="https://ark.volcengine.com/api/v3|~/.config/bytedance/api_key|字节">字节(豆包)</option>
          <option value="https://api.hunyuan.cloud.tencent.com/v1|~/.config/tencent/api_key|腾讯">腾讯(混元)</option>
          <option value="https://qianfan.baidubce.com/v2|~/.config/baidu/api_key|百度">百度(文心)</option>
          <option value="https://api.x.ai/v1|~/.config/xai/api_key|xAI">xAI(Grok)</option>
          <option value="https://api.mistral.ai/v1|~/.config/mistral/api_key|Mistral">Mistral</option>
          <option value="custom|custom|custom">自定义(用连接设置的 base_url)</option>
        </select>
        <button class="out" onclick="loadModels()">🔄 刷新目录</button>
      </div>
      <div class="hint" id="msrc" style="margin-top:6px">选平台后点刷新;目录来自平台官方接口,实时获取(已自动滤除向量/语音类非对话模型)。</div></div>
  </div>
  <div class="card" id="mgroups"><div class="empty">点「刷新目录」拉取模型列表</div></div>
  <div class="card">
    <div class="row"><div class="lab rowcol">模型 ID</div>
      <div class="act"><input id="ckm" placeholder="如 ByteDance-Seed/Seed-OSS-36B" onkeydown="if(event.key==='Enter')recommendLayer()" style="width:340px">
      <button class="pri" onclick="recommendLayer()">检查</button></div></div>
  </div>
  <div id="ckout" class="card" style="display:none;padding:16px 18px;font-size:13px;line-height:2"></div>
</div>

<div class="panel" id="p-meter">
  <div class="phead"><h2>🧮 用量与费用</h2><span class="chip">按层分账</span>
    <span class="q" tabindex="0" data-tip="按层统计 token 用量与调用次数。&#10;只记 token 不记钱:金额按你平台的价目折算,不代扣不代付。">?</span></div>
  <div class="card"><div class="row"><div class="lab rowcol" style="min-width:100%">统计</div></div>
    <div class="row"><div class="meter" id="meter" style="padding:4px 0">…</div></div></div>
  <div class="sect">最近调用</div>
  <div class="card" id="recent"><div class="empty">还没有调用记录</div></div>
</div>

<div class="panel" id="p-guide">
  <div class="phead"><h2>📖 新手教程</h2><span class="chip">四步连通</span></div>
  <div class="slider">
    <button class="sarrow l" onclick="slide(-1)">‹</button>
    <div class="slides" id="slides">
      <div class="slide"><h3>第 1 步 · 粘贴你的 API</h3>
        <p>打开「连接设置」,在 <b>API</b> 栏直接粘贴你的 API key(决策层、执行层各一个)。</p>
        <p>平时显示为暗纹,鼠标悬停可查看,点击即复制;不上传、不代付,只留在你本机。</p>
        <div class="dia"><div class="node">📋 你的 API key</div><div class="flowline"></div><div class="node dest">switchkit 本机保存</div></div>
      </div>
      <div class="slide"><h3>第 2 步 · 复制代理地址</h3>
        <p>打开「运行台」,<b>代理端点</b>一栏就是你的接入地址,形如 http://127.0.0.1:8787/v1。</p>
        <p>选中它复制(或记住端口)。这个地址只在你本机有效。</p>
        <div class="dia"><div class="node">运行台 · 代理端点</div><div class="flowline"></div><div class="node dest">Cmd+C 复制</div></div>
      </div>
      <div class="slide"><h3>第 3 步 · 贴到你工具的 base_url</h3>
        <p>到你常用工具(Claude、任意 AI 客户端)的设置里,找到「服务器地址 / API 地址(base_url)」,<b>粘贴完整地址</b>。</p>
        <p>别贴错栏:这是地址栏,不是密钥栏。</p>
        <div class="dia"><div class="node">你的工具 · base_url 栏</div><div class="flowline"></div><div class="node dest">http://127.0.0.1:8787/v1</div></div>
      </div>
      <div class="slide"><h3>第 4 步 · 发一条测试请求</h3>
        <p>在你的工具里随便发条任务(比如「列出上线前的检查项」),然后回「运行台」。</p>
        <p>看到这条请求出现在大窗里 = <b>连通了</b>;没收到就回来检查上一步的地址。</p>
        <div class="dia"><div class="node">你的工具发一条</div><div class="flowline"></div><div class="node dest">运行台看到它 ✓</div></div>
      </div>
    </div>
    <button class="sarrow r" onclick="slide(1)">›</button>
    <button class="mini replay" onclick="go(cur)">重播本页动画</button>
    <div class="snav" id="snav"></div>
  </div>
</div>

<div class="panel" id="p-about">
  <div class="phead"><h2>ℹ️ 关于</h2></div>
  <div class="about-card">
    <div class="about-top">
      <div class="logo">✳</div>
      <div><div style="font-size:19px;font-weight:700">switchkit</div>
        <div class="inlinehint">版本 v0.10 · 决策/执行双层分发台</div></div>
    </div>
    <div class="row" style="padding-left:0;padding-right:0"><div class="lab rowcol" style="min-width:100%">
      理念:让贵的算力做决策,让便宜的算力做执行;替你分工、替你排雷,不替你做选择。<br>
      本机运行 · 自带 API(BYOK)· 你的 API 只存本机 · 不代付、不上传、不转售。</div></div>
    <div class="about-links">
      <a href="https://github.com/Li-start363/ai-notes" target="_blank"><button class="out">💬 GitHub 心得</button></a>
      <a href="https://github.com/Li-start363/contract-core" target="_blank"><button class="out">📦 contract-core</button></a>
      <button class="out" onclick="show('guide',document.querySelectorAll('.nav button')[6])">📖 新手教程</button>
    </div>
    <div class="inlinehint" style="margin-top:16px">有建议或使用问题,欢迎到仓库提 issue 或给作者留言,每条都会看。</div>
  </div>
</div>

</div>
<script>

const J=(u,o)=>fetch(u,o).then(r=>r.json());
const TAGINFO={'代码':['代码开发','写脚本、修 bug、接口调试'],'写作':['文稿写作','周报、邮件、文案、润色'],'分析':['分析排查','数据、故障、评估方案'],'问答':['问答解释','概念、原理、使用方法'],'通用':['日常事务','清单、整理、其他杂项']};
const tagName=k=>(TAGINFO[k]||[k])[0];
const tagDesc=k=>(TAGINFO[k]||['',''])[1];
const clean=s=>(s||'').replace(/\*\*([^\s*][^*]*?)\*\*/g,'$1').replace(/^#{1,6}\s*/gm,'');
const PLATS={'siliconflow.cn':['zai-org/GLM-5.3-Flash','deepseek-ai/DeepSeek-V4-Flash','tencent/Hy4-preview'],
 'deepseek.com':['deepseek-chat','deepseek-reasoner'],'openai.com':['gpt-5.6','gpt-5.6-mini']};
function show(id,btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('on'));
  document.getElementById('p-'+id).classList.add('on');
  document.querySelectorAll('.nav button').forEach(b=>b.classList.remove('on'));
  if(btn)btn.classList.add('on');
  if(id==='log')loadLog(); if(id==='meter'){loadMeter();loadRecent();} if(id==='tpl')loadTpl();
}
function detectPlatform(inp,hint){
  const v=document.getElementById(inp).value,h=document.getElementById(hint);
  const k=Object.keys(PLATS).find(d=>v.includes(d));
  if(k){h.textContent='已识别:'+k;h.className='inlinehint ok';
    const dl=document.getElementById('models');dl.innerHTML=[...new Set([...[...dl.options].map(o=>o.value),...PLATS[k]])].map(m=>`<option value="${m}">`).join('');
  }else{h.textContent=v?'未识别平台(不影响使用)':'';h.className='inlinehint';}
}
function hintModel(inp,hint){
  const v=document.getElementById(inp).value.trim(),h=document.getElementById(hint);
  if(!v){h.textContent='';return;}
  h.textContent='已填入;要不要放这一层,可去「模型检查」两层实测打分看推荐';
  h.className='inlinehint';
}
['db','dm','dk','eb','em','ek'].forEach(id=>{const el=document.getElementById(id); if(el)el.addEventListener('input',()=>{document.getElementById('savecfg').disabled=false;});});
function loadKeys(){
  ['decision','execution'].forEach(lay=>{
    const id=lay==='decision'?'kview_d':'kview_e';
    const cfg={decision:['dk'],execution:['ek']}[lay][0];
    const pv=document.getElementById(cfg)?document.getElementById(cfg).value.trim():'';
    if(pathEl)pathEl.textContent=pv?('文件路径:'+pv):'';
    J('/api/key_info?layer='+lay).then(r=>{
      const el=document.getElementById(id); if(!el)return;
      el.dataset.masked=r.masked||''; el.dataset.full=r.full||''; el.dataset.shown='0';
      el.textContent=r.masked||r.error||'未配置';
    });
  });
}
function refreshKey(lay){loadKeys();}
function revealKey(id,on){
  const el=document.getElementById(id); if(!el||!el.dataset.full)return;
  el.textContent=on?el.dataset.full:el.dataset.masked;
  el.dataset.shown=on?'1':'0';
}
function copyKey(lay,id){
  const el=document.getElementById(id); if(!el||!el.dataset.full)return;
  navigator.clipboard.writeText(el.dataset.full).then(()=>{
    const old=el.textContent; el.textContent='已复制 ✓'; setTimeout(()=>{el.textContent=el.dataset.shown==='1'?el.dataset.full:el.dataset.masked;},900);
  });
}
function saveCfg(){
  const d={decision:{model:dm.value,base_url:db.value,key_file:dk.value},
           execution:{model:em.value,base_url:eb.value,key_file:ek.value}, proxy_mode:document.getElementById('pmode').value};
  J('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)})
   .then(r=>{document.getElementById('cfgmsg').textContent=r.ok?'已保存 ✓':'保存失败';document.getElementById('savecfg').disabled=true;loadKeys();});
}
function saveMode(){
  J('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({proxy_mode:document.getElementById('pmode').value})});
}
/* ---- 运行台:三窗,只讲同一条请求 ---- */
function renderConsole(row,isHist){
  const c={'代码':'#2563eb','写作':'#7c3aed','分析':'#059669','问答':'#d97706'}[row.tag]||'#6b7280';
  const isPass=!!row.passthrough;
  const route=row.route||'—', reason=row.route_reason||'';
  const dec=row.decision, exe=row.execution, pt=row.passthrough;
  const stages=[];
  if(dec){stages.push(['决策层出规格', dec.plan?'done':'act']);}
  if(dec&&exe){stages.push(['交接','done']);stages.push(['执行层执行', exe.answer?'done':'act']);}
  document.getElementById('console').innerHTML=`
   ${isHist?'<div class="banner">📜 历史记录(不会重跑)<button class="mini" onclick="loadLog()" style="margin-left:auto">回到实时</button></div>':''}
   <div class="chead"><span class="tag" style="background:${c}" title="${tagDesc(row.tag||'')}">${tagName(row.tag||'?')}</span>
     <b style="font-size:13.5px">${row.task||''}</b>
     <span class="routechip ${isPass?'user':'ours'}">${route}</span>
     <span class="routechip">${reason||''}</span>
     <span class="ts">${row.ts||''} · ${row.source||''}</span></div>
   <div class="cduo">
     <div class="cbox"><h4><span class="dot" style="background:#2563eb"></span> 决策层 · 方案</h4>
       <div class="body">${isPass?'<span class="pass">本次未经过</span>':(dec?clean(dec.plan||''):(row.error?'<span class="bad">'+clean(row.error)+'</span>':'<span class="pass">本次未经过</span>'))}</div></div>
     <div class="cbox"><h4><span class="dot" style="background:#059669"></span> 执行层 · 执行</h4>
       <div class="body">${isPass?'<span class="pass">本次未经过</span>':
         (exe?((exe.steps||['产出成品']).map((s,i)=>`<div class="stage ${exe.answer?'done':'act'}">${exe.answer?'☑':'☐'} ${clean(s)}</div>`).join('')+`<div style="margin-top:7px;font-size:11px;color:#64748b">${exe.model||''} · ${exe.tok||0} tok</div>`)  :'<span class="pass">本次未经过</span>')}</div></div>
   </div>
   <div class="cfull">${isPass?`沿用原方式 · 收到 → 转发 → 返回<span class="pass"> (不拆分,${reason})</span>`:''}
     ${row.error?'<span class="bad">'+clean(row.error)+'</span>':''}</div>
   <div class="cfoot"><span>状态:<b>${row.status==='ok'||row.status==='exec_done'?'已完成':(row.status||'—')}</b></span>
     <span>实时 token:<b>${((dec&&dec.tok||0)+((exe&&exe.tok)||(pt&&pt.tok)||0)).toLocaleString()}</b></span>
     ${stages.length?`<span>${stages.map(s=>`<span class="stage ${s[1]}" style="margin-right:10px">${s[1]==='done'?'✓':(s[1]==='act'?'<span class="spin"></span>':'·')} ${s[0]}</span>`).join('')}</span>`:''}</div>`;
}
function loadLog(){
  J('/api/log').then(rows=>{
    const finals=rows.filter(r=>r.stage==='exec_done'||r.stage==='pass_done'||r.stage==='decision_failed'||r.stage==='pass_failed');
    if(!finals.length){
      document.getElementById('console').innerHTML=`<div class="chead"><span class="tag" style="background:#9ca3af">等待任务</span>
        <span style="color:#9ca3af;font-size:12.5px">等待你的工具发来第一条请求</span>
        <button class="mini" style="margin-left:auto" onclick="show('guide',document.querySelectorAll('.nav button')[6])">查看连接步骤</button></div>`;
      document.getElementById('hist').innerHTML=''; return;
    }
    renderConsole(finals[finals.length-1],false);
    document.getElementById('hist').innerHTML=finals.slice(0,-1).reverse().map(r=>
      `<div class="histrow" data-r="${encodeURIComponent(JSON.stringify(r))}">
        <span class="tag" style="background:${({'代码':'#2563eb','写作':'#7c3aed','分析':'#059669','问答':'#d97706'}[r.tag]||'#6b7280')};font-size:10px">${tagName(r.tag||'?')}</span>
        <span style="flex:1;color:#374151">${(r.task||'').slice(0,52)}</span>
        <span class="routechip ${r.passthrough?'user':'ours'}" style="font-size:10px">${r.route||'—'}</span>
        <span style="color:#9ca3af">${(r.ts||'').slice(5,16)}</span></div>`).join('')||'';
    document.querySelectorAll('.histrow').forEach(el=>el.onclick=()=>renderConsole(JSON.parse(decodeURIComponent(el.dataset.r)),true));
  });
}
function fillWork(t){document.getElementById('work').value=t;}
function adoptBest(){
  CMP.forEach((r,i)=>{const sel=document.getElementById('ch'+i); if(!sel)return;
    sel.value=(r.gain!=null&&r.gain>0)?'ours':'user';});
  updateSaveBtn();
}
function selfTest(){
  const c=document.getElementById('console');
  c.innerHTML='<div class="chead"><span class="tag" style="background:#2563eb">自测</span><span class="spin"></span><b style="font-size:13px">正在经代理发一条测试请求…</b></div>';
  J('/api/selftest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})}).then(r=>{
    if(r.error){c.innerHTML='<div class="chead"><span class="tag" style="background:#b91c1c">自测失败</span><span class="bad">'+clean(r.error)+'</span></div>';return;}
    loadLog();
  });
}
function pingAll(){
  const out=document.getElementById('cfgmsg'); out.textContent='测试中…';
  Promise.all(['decision','execution'].map(l=>J('/api/ping_layer',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({layer:l})})))
   .then(([d,e])=>{
    const f=(name,r)=>r.ok?'✅ '+name+'连通('+r.model+' · '+r.latency_sec+'s)':'⛔ '+name+'不通:'+(r.error||'').slice(0,60);
    out.innerHTML=f('决策层',d)+'<br>'+f('执行层',e);
  });
}
const VENDOR_ICON={'智谱':'zhipu','DeepSeek':'deepseek','Google':'google','Anthropic':'anthropic','通义':'alibabacloud','阿里':'alibabacloud','百度':'baidu','小米':'xiaomi','MiniMax':'minimax','字节':'bytedance','NVIDIA':'nvidia','Meta':'meta','腾讯':'tencent'};
const VENDOR_COLOR={'OpenAI':'#74aa9c','月之暗面':'#16191e','xAI':'#111111','Mistral':'#fa520f','Microsoft':'#00a4ef','AWS':'#ff9900','智谱':'#3859ff','腾讯':'#0052d9','Upstage':'#4b4bff','Perplexity':'#20808d','Cohere':'#39594d'};
function vendorIcon(v){
  const ic=VENDOR_ICON[v];
  if(ic)return `<img src="/icons/${ic}.svg" style="width:22px;height:22px" alt="${v}">`;
  const c=VENDOR_COLOR[v]||'#64748b';
  return `<div style="width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,${c},${c}bb);color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center">${(v||'·')[0]}</div>`;
}
function vendorOf(id){
  const VM={'xiaomi':'小米','z-ai':'智谱','zhipu':'智谱','qwen':'通义','deepseek':'DeepSeek','openai':'OpenAI','anthropic':'Anthropic','google':'Google','meta':'Meta','mistralai':'Mistral','microsoft':'Microsoft','amazon':'AWS','nvidia':'NVIDIA','x-ai':'xAI','moonshot':'月之暗面','minimax':'MiniMax','stepfun':'阶跃','inclusionai':'蚂蚁','bytedance':'字节','tencent':'腾讯','upstage':'Upstage','perplexity':'Perplexity','cohere':'Cohere','alibaba':'阿里','baidu':'百度','iflytek':'讯飞','01-ai':'零一','baichuan':'百川','gpt-':'OpenAI','glm':'智谱','ling':'蚂蚁','seed':'字节','hy':'腾讯','mimo':'小米','claude':'Anthropic','gemini':'Google'};
  const low=id.toLowerCase();
  for(const k in VM){if(low.includes(k))return VM[k];}
  const pre=(id.includes('/')?id.split('/')[0]:id);
  return pre.charAt(0).toUpperCase()+pre.slice(1);
}
function loadModels(){
  const v=document.getElementById('mplat').value;
  let bu,kf,vendor='';
  if(v.startsWith('custom')){bu=document.getElementById('db').value.trim();kf=document.getElementById('dk').value.trim();}
  else if(v==='__aggregate__'){bu='__aggregate__';kf='';}
  else{const parts=v.split('|');bu=parts[0];kf=parts[1];vendor=parts[2]||'';}
  if(!bu){document.getElementById('msrc').textContent='请先在连接设置填 base_url';return;}
  document.getElementById('msrc').textContent='拉取中…';
  J('/api/models_list?base_url='+encodeURIComponent(bu)+'&key_file='+encodeURIComponent(kf)+'&vendor='+encodeURIComponent(vendor)).then(r=>{
    document.getElementById('msrc').textContent=(r.source==='platform'?'🟢 ':'🔶 ')+r.source_note;
    const byV={};
    (r.models||[]).filter(id=>!/bge|rerank|embed|multimodal|whisper|tts|wav2vec|stable|vl-/i.test(id)).forEach(id=>{const v2=vendorOf(id);(byV[v2]=byV[v2]||[]).push(id);});
    const keys=Object.keys(byV).sort();
    document.getElementById('mgroups').innerHTML=keys.length?keys.map(v2=>
      `<div class="sect" style="margin:14px 2px 6px;font-size:12.5px">${v2}<span class="inlinehint" style="margin-left:8px">${byV[v2].length} 个</span></div>
       ${byV[v2].map(id=>`<div class="listrow" style="cursor:pointer" onclick="pickModel('${id}',this)">
         <div class="tile" style="background:#fff;border:1px solid var(--line);padding:4px">${vendorIcon(v2)}</div>
         <div class="lmain" style="flex:1"><b style="font-family:ui-monospace,Menlo,monospace;font-size:12.5px">${id}</b></div>
         <button class="mini">选它</button></div>`).join('')}`).join('')
      :'<div class="empty">该平台目录为空——可手动输入模型 ID 查询</div>';
  });
}
function pickModel(id, el){
  const btn = el && el.querySelector ? el.querySelector('button') : null;
  if(btn){btn.classList.remove('flash'); void btn.offsetWidth; btn.classList.add('flash');
    btn.textContent='已选 ✓'; setTimeout(()=>{btn.textContent='选它'; btn.classList.remove('flash');},1400);}
  document.getElementById('ckm').value=id;
  const out=document.getElementById('ckout'); out.style.display='block';
  out.innerHTML=`<div class="askstrip"><b>已填入模型 ID:</b>
    <code style="font-family:ui-monospace,Menlo,monospace;font-size:12.5px">${clean(id)}</code>
    <span class="inlinehint">是否立即实测两层(决策/执行各打多少分),并推荐放哪层?</span>
    <button class="pri" onclick="recommendLayer()">立即实测</button>
    <button class="out" onclick="document.getElementById('ckout').style.display='none'">暂不</button></div>`;
  out.scrollIntoView({behavior:'smooth',block:'center'});
}
function getPlat(){
  const v=document.getElementById('mplat').value;
  if(v.startsWith('custom'))return{bu:document.getElementById('db').value.trim(),kf:document.getElementById('dk').value.trim()};
  if(v==='__aggregate__')return{bu:'',kf:''};
  const p=v.split('|');return{bu:p[0],kf:p[1]};
}
function scoreOf(r){
  const base={pass:90,weak:70,stall:30,fail:0}[r.verdict]||0;
  const lat=parseFloat(r.latency)||0;
  return Math.max(0,Math.round((base-Math.min(15,lat*0.5))*10)/10);
}
function recommendLayer(){
  const model=document.getElementById('ckm').value.trim();
  const out=document.getElementById('ckout'); out.style.display='block';
  if(!model){out.innerHTML='<span class="warn">先在上方选一个模型或输入模型 ID。</span>';return;}
  const {bu,kf}=getPlat();
  if(!bu){out.innerHTML='<span class="warn">聚合目录只用于浏览——实测需要在上方选择该模型所属平台(需已配 API)。</span>';return;}
  out.innerHTML='<div class="askstrip"><span class="spin"></span><b>两层实测中…</b><span class="inlinehint">决策层(想)与执行层(做)各一次小调用,约 20-90 秒;会消耗少量你的 API token。</span></div>';
  const one=layer=>J('/api/eval_model',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({model,layer,base_url:bu,key_file:kf})});
  Promise.all([one('decision'),one('execution')]).then(([d,e])=>{
    const V={pass:['✅ 通过','ok'],weak:['⚠️ 能用但弱','warn'],stall:['⛔ 卡流/超时','bad'],fail:['⛔ 失败','bad']};
    const sd=scoreOf(d), se=scoreOf(e);
    const card=(name,r,s)=>`<div style="flex:1;min-width:230px;border:1px solid var(--line);border-radius:10px;padding:12px 14px">
      <b>${name}</b> <span class="${(V[r.verdict]||['?','warn'])[1]}" style="font-weight:700">${(V[r.verdict]||['?','warn'])[0]}</span>
      <div style="font-size:21px;font-weight:700;margin:6px 0">${s} 分</div>
      <div class="inlinehint">延迟 ${r.latency||'—'}s${r.tok?' · '+r.tok+' tok':''}</div>
      <div class="inlinehint">${r.sample?clean(r.sample).slice(0,120):(clean(r.error)||'')}</div></div>`;
    let rec,why;
    if(sd>=70&&se>=70){rec=sd>=se?'决策层':'执行层';why=`${sd} 分 vs ${se} 分,两层都够用,分高者优先`;}
    else if(sd>=70){rec='决策层';why=`决策层 ${sd} 分达标,执行层 ${se} 分不达标`;}
    else if(se>=70){rec='执行层';why=`执行层 ${se} 分达标,决策层 ${sd} 分不达标`;}
    else{rec='都不建议';why=`两层都不达标(决策 ${sd} / 执行 ${se}),建议换模型`;}
    out.innerHTML=`<div style="display:flex;gap:12px;flex-wrap:wrap">${card('决策层(想)',d,sd)}${card('执行层(做)',e,se)}</div>
      <div class="askstrip" style="margin-top:12px"><b style="font-size:15px">推荐:${rec}</b>
        <span class="inlinehint">${why}。口径:判定分(通过90/能用但弱70/卡流30/失败0)− 延迟扣分(每秒0.5分,上限15)</span></div>
      ${rec==='都不建议'?'':`<div class="askstrip" style="margin-top:8px">
        <button class="out" onclick="fillLayer('dm')">填入决策层</button>
        <button class="out" onclick="fillLayer('em')">填入执行层</button></div>`}`;
    out.scrollIntoView({behavior:'smooth',block:'center'});
  });
}
function fillLayer(inp){
  const v=document.getElementById('ckm').value.trim(); if(!v)return;
  document.getElementById(inp).value=v;
  hintModel(inp,inp==='dm'?'dh':'eh');
  const sc=document.getElementById('savecfg'); if(sc)sc.disabled=false;
  show('cfg',document.querySelectorAll('.nav button')[3]);
}
function loadRecent(){
  J('/api/recent_calls').then(rows=>{
    const el=document.getElementById('recent'); if(!el)return;
    el.innerHTML=rows.length?('<table><tr><th>时间</th><th>层</th><th>模型</th><th>tokens(in/out)</th></tr>'+
      rows.map(r=>`<tr><td class="num">${(r.ts||'').slice(5,16)}</td><td>${r.layer||''}</td><td>${r.model||''}</td><td class="num">${r.prompt_tokens||0}/${r.completion_tokens||0}</td></tr>`).join('')+'</table>')
      :'<div class="empty">还没有调用记录</div>';
  });
}
function loadMeter(){
  J('/api/usage').then(s=>{
    const tot=['决策','执行','优化','盲评','生成'].reduce((a,k)=>a+((s[k].in||0)+(s[k].out||0)),0);
    meter.innerHTML=tot?['决策','执行','优化','盲评','生成'].map(k=>
      `<div>${k}层<b class="num">${((s[k].in||0)+(s[k].out||0)).toLocaleString()}</b><span class="sub" style="margin:0">${s[k].calls||0} 次调用</span></div>`).join('')
      :'<div class="empty" style="width:100%">还没有用量记录——配好双层后跑一次优化或经代理端点发条请求。</div>';
  });
}
function setDensity(v){
  document.body.classList.toggle('comfy',v==='comfy');
  document.querySelectorAll('#segden button').forEach((b,i)=>b.classList.toggle('on',(i===1)===(v==='comfy')));
  J('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({density:v})});
}
/* ---- 根据主要任务优化:渐进向导(点一步弹下一步) ---- */
let ITEMS=[],CMP=[],WIZ=0;
function showWiz(n){
  WIZ=n;
  [0,1,2,3].forEach(i=>document.getElementById('w'+i).style.display=i===n?'block':'none');
  document.getElementById('wizdots').innerHTML=[0,1,2,3].map(i=>`<span class="dotn ${i<=n?'on':''}" style="display:inline-block"></span>`).join('');
  document.getElementById('wizlabel').textContent=['说方向','挑工作项','看差异','确认保存'][n];
  if(n===3)renderPreview();
}
function stepItems(){
  const w=document.getElementById('work').value.trim();
  if(w.length<8||/^\d+$/.test(w)){needMore('方向太短、看不出具体工作——请补充细节(例:运维——上线前检查、故障排查、写自动化脚本)');return;}
  document.getElementById('optmsg').textContent='生成中…';
  J('/api/gen_items',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({work:w})}).then(g=>{
    if(g.error){document.getElementById('optmsg').textContent='⛔ '+g.error;return;}
    if(g.need_more||!g.items||!g.items.length){needMore('没看出你的主要方向——请点「上一步」回去补充,越具体越好(例:做数据分析——清洗数据、出周报图表)');return;}
    ITEMS=g.items; document.getElementById('optmsg').textContent='';
    renderItems(false);
    document.getElementById('needbox').style.display='none';
    showWiz(1);
  });
}
function needMore(msg){
  showWiz(1);
  document.getElementById('needbox').style.display='block';
  document.getElementById('needmsg').textContent=msg;
  document.getElementById('itemcards').innerHTML='';
  document.getElementById('cmpbtn').disabled=true;
  document.getElementById('cmpbtn').textContent='开始对比 →';
  document.getElementById('backbtn').classList.add('green');
}
function renderItems(keepChecks){
  const prev=keepChecks?[...document.querySelectorAll('#itemcards input')].map(c=>c.checked):null;
  document.getElementById('itemcards').innerHTML=ITEMS.map((it,i)=>
    `<label class="pickcard" id="pc${i}"><input type="checkbox" ${(prev&&prev[i])||it.custom?'checked':''} onchange="this.closest('.pickcard').classList.toggle('sel',this.checked);updateCmpBtn()">
      <div><b>${clean(it.text)}</b><div class="inlinehint">类型:${tagName(it.tag)} —— ${tagDesc(it.tag)}${it.custom?' · 自定义':''}</div></div></label>`).join('');
  document.querySelectorAll('#itemcards .pickcard').forEach((el,i)=>{if(ITEMS[i].custom||(prev&&prev[i]))el.classList.add('sel');});
  updateCmpBtn();
}
function addCustomItem(){
  const el=document.getElementById('customItem'); const t=(el.value||'').trim();
  if(t.length<3){el.style.borderColor='#e07b2e';return;}
  el.style.borderColor='';
  ITEMS.push({key:classifyJS(t),tag:classifyJS(t),text:t,custom:true});
  el.value='';
  renderItems(true);
}
function updateCmpBtn(){
  const n=[...document.querySelectorAll('#itemcards input')].filter(c=>c.checked).length;
  document.getElementById('cmpbtn').disabled=n===0;
  document.getElementById('cmpbtn').textContent=n?`开始对比(${n} 项) →`:'开始对比 →';
}
async function stepCompare(){
  const sel=[...document.querySelectorAll('#itemcards input')].map((c,i)=>c.checked?i:-1).filter(i=>i>=0);
  if(!sel.length)return;
  showWiz(2);
  const btn=document.getElementById('cmpbtn');btn.disabled=true;
  document.getElementById('nextbtn').disabled=true;
  document.getElementById('cmpnote').textContent='优化中…';
  const S=document.getElementById('optsteps'),bar=document.getElementById('optbar');
  S.innerHTML=sel.map(i=>`<div class="step"><span class="ic">☐</span>对比:${clean(ITEMS[i].text)}</div>`).join('');
  CMP=[];
  const subTotal=sel.length*4; let sub=0; const t0=Date.now();
  let subStart=Date.now(), avgMs=25000;
  const tick=setInterval(()=>{
    const note=document.getElementById('cmpnote');
    if(note.dataset.running!=='1')return;
    const curRemain=Math.max(0,avgMs-(Date.now()-subStart));
    const remainMs=curRemain+avgMs*Math.max(0,subTotal-sub-1);
    const sec=Math.round(remainMs/1000);
    const etaTxt=sec>=90?('约 '+Math.round(sec/60)+' 分钟'):('约 '+sec+' 秒');
    note.innerHTML=note.dataset.label+' · 进度 '+sub+'/'+subTotal+' · 预计还需 <b>'+etaTxt+'</b>';
  },1000);
  const prog=label=>{
    const now=Date.now();
    if(sub>0){avgMs=avgMs*0.6+(now-subStart)*0.4;}
    subStart=now; sub++;
    bar.style.width=Math.round(100*sub/subTotal)+'%';
    const note=document.getElementById('cmpnote');
    note.dataset.running='1'; note.dataset.label=label;
  };
  for(let k=0;k<sel.length;k++){
    const it=ITEMS[sel[k]]; const rows=S.querySelectorAll('.step');rows[k].className='step run';
    prog('「'+it.text.slice(0,12)+'」决策层定规格…');
    const p=await J('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:it.text})});
    prog('「'+it.text.slice(0,12)+'」双层执行层产出…');
    const e=p.error?{error:p.error}:await J('/api/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:it.text,plan:p.plan||''})});
    prog('「'+it.text.slice(0,12)+'」原方式产出…');
    const o=await J('/api/oneshot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:it.text})});
    prog('「'+it.text.slice(0,12)+'」盲评打分…');
    const j=(e.answer&&o.answer)?await J('/api/judge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task:it.text,plan:p.plan||'',a:e.answer,b:o.answer})}):{};
    const gain=(j.a!=null&&j.b!=null)?(j.a-j.b):null;
    CMP.push({item:it.text,key:it.key,sa:j.a??null,sb:j.b??null,gain,tok:(p.tok||0)+(e.tok||0),oneTok:(o.tok||0)});
    rows[k].className='step done'; bar.style.width=Math.round(100*(k+1)/sel.length)+'%';
    renderResultTable();
  }
  clearInterval(tick);
  btn.disabled=false;
  const nd=document.getElementById('cmpnote'); nd.dataset.running='0';
  nd.textContent='优化完成 ✓(用时 '+Math.round((Date.now()-t0)/1000)+' 秒)可进入确认';
}
function renderResultTable(){
  const tab=document.getElementById('rtab');
  tab.innerHTML='<tr><th>工作项</th><th class="num">双层</th><th class="num">原方式</th><th>差异</th><th>走哪条路?</th></tr>'+
   CMP.map((r,i)=>{
    const g=r.gain,cls=g==null?'eq':(g>0?'up':(g<0?'dn':'eq'));
    const gt=g==null?'未判出差异':(g>0?'双层 +'+g+' 分':(g<0?'双层 '+g+' 分':'持平'));
    return `<tr><td>${clean(r.item)}</td><td class="num">${r.sa??'—'}</td><td class="num">${r.sb??'—'}</td><td class="gain ${cls}">${gt}</td>
      <td><select id="ch${i}" onchange="updateSaveBtn()"><option value="hold" selected>暂不更改</option><option value="ours">双层分工</option><option value="user">沿用原方式</option></select></td></tr>`;
   }).join('');
  document.getElementById('resultcard').style.display='block';
  document.getElementById('adoptrow').style.display='block';
  updateSaveBtn();
}
function updateSaveBtn(){
  const changed=CMP.some((r,i)=>document.getElementById('ch'+i)&&document.getElementById('ch'+i).value!=='hold');
  const b=document.getElementById('savebtn');
  b.disabled=!changed;
  b.textContent=changed?'保存规则':'保存规则(还没选任何变更)';
}
function renderPreview(){
  const names={hold:'不改规则(维持现状)',ours:'✅ 走机制(双层分工)',user:'➡️ 沿用原方式'};
  const tagColor={'代码':'#2563eb','写作':'#7c3aed','分析':'#059669','问答':'#d97706','通用':'#6b7280'};
  const changed=CMP.filter((r,i)=>document.getElementById('ch'+i)&&document.getElementById('ch'+i).value!=='hold');
  document.getElementById('previewbox').innerHTML=
   `<table><tr><th>识别的工作项</th><th>识别类型</th><th>优化结果</th><th>写入规则(该类型以后怎么走)</th></tr>`+
   CMP.map((r,i)=>{
     const ch=document.getElementById('ch'+i)?document.getElementById('ch'+i).value:'hold';
     const g=r.gain,gt=g==null?'未判出差异':(g>0?'双层 +'+g+' 分':(g<0?'双层 '+g+' 分':'持平'));
     return `<tr><td>${clean(r.item)}</td>
       <td><span class="tag" style="background:${tagColor[r.key]||'#6b7280'};font-size:11px">${tagName(r.key)}</span></td>
       <td class="gain ${g==null?'eq':(g>0?'up':(g<0?'dn':'eq'))}">${gt}</td>
       <td><b>${names[ch]}</b></td></tr>`;
   }).join('')+'</table>'+
   `<div class="inlinehint" style="margin-top:8px">${changed.length?`保存后将写入 ${changed.length} 条类型规则,即刻生效。`:'当前没有任何变更——请回上一步选择,或直接结束。'}</div>`;
}
function commitRoutes(){
  const rows=CMP.map((r,i)=>({item:r.item,key:r.key,gain:r.gain,choice:document.getElementById('ch'+i).value}))
                .filter(r=>r.choice!=='hold');
  const m=document.getElementById('commitmsg'); m.style.display='block';
  if(!rows.length){
    m.className='notice warn-n';
    m.textContent='每一项都选了「不改规则」——不会写入任何变更。想改变某类工作的处理方式,请回上一步把下拉换成「双层分工」或「沿用原方式」。';
    return;
  }
  J('/api/commit_routes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows})})
   .then(r=>{
     if(r.ok){m.className='notice ok-n';m.textContent='已保存 ✓ 写入 '+r.saved+' 条类型规则,即刻生效 —— 到「处理规则」可以查看或用开关调整。';}
     else{m.className='notice warn-n';m.textContent='保存失败,请重试。';}
   });
}
function loadTpl(){
  J('/api/templates').then(d=>{
    const cur=d.current, rows=(cur&&cur.rows)||[];
    const byKey={}; rows.forEach(r=>byKey[r.key]=r);
    const gainTxt=(g,fresh)=>g==null?'未打分':((g>0?'双层 +'+g+' 分':(g<0?'双层 '+g+' 分':'持平'))+(fresh?'':' · 已过期(模型已换)'));
    const all=[['代码','#2563eb'],['写作','#7c3aed'],['分析','#059669'],['问答','#d97706'],['通用','#6b7280']];
    const inRows=[],outRows=[];
    all.forEach(([k,c])=>{
      const r=byKey[k];
      const fresh=!!(r&&r.fresh);
      const on=r&&r.choice==='ours'&&r.enabled!==false&&fresh;
      const tile=`<div class="tile" style="background:${c}1a;color:${c}">${tagName(k)[0]}</div>`;
      const main=`<div class="lmain" style="flex:1"><b>${tagName(k)}</b><span class="badge ${on?'ours':'user'}">${on?'走机制':'沿用原方式'}</span>
        <div class="inlinehint">${tagDesc(k)} · 对比分:<span class="gain ${r&&r.gain>0?'up':((r&&r.gain<0)?'dn':'eq')}">${gainTxt(r?r.gain:null,fresh)}</span></div></div>`;
      inRows.push(`<div class="listrow">${tile}${main}<button class="mini" onclick="goOptimize('${tagName(k)}')">去对比</button><label class="sw"><input type="checkbox" ${on?'checked':''} onchange="toggleRule('${k}',this.checked);setTimeout(loadTpl,250)"><span class="swslider"></span></label></div>`);
      if(!on)outRows.push(`<div class="listrow">${tile}${main}<span class="inlinehint">${r?(r.gain==null?'未打分':(fresh?(r.gain>0?'对比加分但已手动停用':'未加分'):'分数过期(模型已换)')):'未打分'} — 想启用先「按当前模型打分」</span></div>`);
    });
    document.getElementById('tpls').innerHTML=
      `<div class="sect">✅ 走机制(双层分工)——开关即生效</div>
       <div class="card">${inRows.join('')}</div>
       <div class="sect">➡️ 不走机制(沿用原方式)</div>
       <div class="card">${outRows.join('')||'<div class="empty">全部类型都在走机制</div>'}</div>`;
  });
}
function goOptimize(kw){
  show('opt',document.querySelectorAll('.nav button')[1]);
  const el=document.getElementById('work'); if(el&&!el.value.trim())el.value=kw+'——'+({'代码开发':'写脚本、修 bug、接口调试','文稿写作':'周报、邮件、文案、润色','分析排查':'数据、故障、评估方案','问答解释':'概念、原理、使用方法','日常事务':'清单、整理、其他杂项'}[kw]||'');
}
function toggleRule(key,on){
  J('/api/toggle_rule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,enabled:on})}).then(r=>{
    if(r&&r.ok===false){const m=document.getElementById('initmsg');
      m.style.color='#b91c1c'; m.textContent='⛔ '+key+':'+(r.error||'开关失败');
      setTimeout(()=>{m.style.color='';m.textContent='五类工作各试跑一次对比(约 20 次调用,视模型速度 2-5 分钟);分数绑定当前两层模型,换了模型需重新打分';},4500);}
  });
}
function initRules(){
  const b=document.getElementById('initbtn');b.disabled=true;
  const wrap=document.getElementById('initwrap');wrap.style.display='block';
  document.getElementById('initmsg').innerHTML='<span class="spin"></span> 打分试跑中…';
  let baseEta=null, baseAt=Date.now();
  const tick=setInterval(()=>{
    J('/api/init_status').then(d=>{
      const pct=d.total?Math.round(100*d.done/d.total):0;
      document.getElementById('initbar').style.width=pct+'%';
      baseEta=d.eta_sec; baseAt=Date.now();
      document.getElementById('initlog').dataset.line='['+d.done+'/'+d.total+'] '+(d.line||'');
    }).catch(()=>{});
    const dec=baseEta==null?null:Math.max(0,Math.round(baseEta-(Date.now()-baseAt)/1000));
    if(dec!=null){
      const eta=dec>=90?('约 '+Math.round(dec/60)+' 分钟'):('约 '+dec+' 秒');
      const el=document.getElementById('initlog');
      const line=el.dataset.line||'';
      el.textContent=line+' —— 预计还需 ~'+eta;
    }
  },1000);
  J('/api/init_rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})}).then(r=>{
    clearInterval(tick);
    if(r.error){document.getElementById('initmsg').textContent='⛔ '+r.error;b.disabled=false;return;}
    document.getElementById('initbar').style.width='100%';
    document.getElementById('initlog').textContent='[20/20] 完成 ✓';
    document.getElementById('initmsg').textContent='✓ 基准打分表已生成 —— 有加分的已默认启用,可在下方调整';
    b.disabled=false;loadTpl();
  });
}
/* ---- 教程滑动 ---- */
let cur=0;
function renderNav(){document.getElementById('snav').innerHTML=[0,1,2,3].map(i=>`<div class="dotn ${i===cur?'on':''}" onclick="go(${i})"></div>`).join('');}
function go(i){cur=i;document.getElementById('slides').style.transform=`translateX(-${i*100}%)`;renderNav();}
function slide(d){go(Math.min(3,Math.max(0,cur+d)));}
renderNav();showWiz(0);
loadLog();loadMeter();document.getElementById('ep').value='http://'+location.host+'/v1';
const _q=new URLSearchParams(location.search);
if(_q.get('panel')){const _m={log:0,opt:1,tpl:2,cfg:3,check:4,meter:5,guide:6,about:7};
  show(_q.get('panel'),document.querySelectorAll('.nav button')[_m[_q.get('panel')]||0]);}
if(_q.get('clients')==='1')toggleClients();
fetch('/api/state').then(r=>r.json()).then(s=>{
  dm.value=s.dual.decision.model||'';db.value=s.dual.decision.base_url||'';dk.value=s.dual.decision.key_file||'';
  em.value=s.dual.execution.model||'';eb.value=s.dual.execution.base_url||'';ek.value=s.dual.execution.key_file||'';
  if(s.dual.proxy_mode)document.getElementById('pmode').value=s.dual.proxy_mode;
  if(s.dual.density)setDensity(s.dual.density);
  loadKeys();
  if(db.value)detectPlatform('db','dp');
});
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.activeElement.blur();});
setInterval(()=>{if(document.getElementById('p-log').classList.contains('on'))loadLog();},6000);

function syncSeg(){const v=document.getElementById('pmode').value;const idx={split:0,execution:1,decision:2}[v]||0;document.querySelectorAll('#segmode button').forEach((b,i)=>b.classList.toggle('on',i===idx));}
setTimeout(syncSeg,500);

/* ---- 顶层悬浮说明(不被任何容器裁剪,贴边自动翻转) ---- */
const _tip=document.createElement('div');
_tip.style.cssText='position:fixed;z-index:99999;max-width:320px;background:#0f172a;color:#e2e8f0;font-size:11.5px;line-height:1.85;padding:10px 13px;border-radius:9px;display:none;white-space:pre-wrap;pointer-events:none;box-shadow:0 6px 24px rgba(15,23,42,.28)';
document.body.appendChild(_tip);
function _showTip(el){
  _tip.textContent=el.getAttribute('data-tip')||''; _tip.style.display='block';
  const r=el.getBoundingClientRect(); const tw=_tip.offsetWidth, th=_tip.offsetHeight;
  let x=r.left+22, y=r.top-8;
  if(x+tw>window.innerWidth-12) x=r.left-tw-22; if(x<12) x=12;
  if(y+th>window.innerHeight-12) y=window.innerHeight-th-12; if(y<12) y=12;
  _tip.style.left=x+'px'; _tip.style.top=y+'px';
}
function _hideTip(){_tip.style.display='none';}
document.addEventListener('mouseover',e=>{const q=e.target.closest('.q'); if(q)_showTip(q);});
document.addEventListener('mouseout',e=>{if(e.target.closest('.q'))_hideTip();});
document.addEventListener('focusin',e=>{const q=e.target.closest('.q'); if(q)_showTip(q);});
document.addEventListener('focusout',_hideTip);
document.addEventListener('keydown',e=>{if(e.key==='Escape')_hideTip();});
window.addEventListener('scroll',_hideTip,true);


function segMode(v,btn){
  document.querySelectorAll('#segmode button').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  document.getElementById('pmode').value=v; saveMode();
}
/* ---- 接入工具:检测本机客户端 / 一键代配 / 还原 ---- */
function toggleClients(){const b=document.getElementById('clientbox');
  if(b.style.display==='none'){b.style.display='';loadClients();}else b.style.display='none';}
function clientRow(c){
  const can=(c.mode==='auto'&&c.installed);
  const badge=c.installed?'<span class="badge ours">已装</span>':'<span class="badge user">未装</span>';
  const cur=c.current?`<span class="inlinehint" style="margin-left:6px">当前:${clean(c.current)}</span>`:'';
  return `<div class="listrow${can?'':' clgray'}" id="cl-${c.id}">
    <div class="lmain" style="flex:1;min-width:220px"><b>${c.name}</b>${badge}${cur}
      <div class="inlinehint">${clean(c.note||'')}</div></div>
    ${can?`<button class="pri" style="padding:6px 14px" onclick="enableOne('${c.id}')">启用</button>`
        :'<span class="badge user">不可自动代配</span>'}
    <button class="mini" onclick="restoreClient('${c.id}')">还原</button></div>`;
}
function loadClients(){
  const msg=document.getElementById('clientmsg'); msg.textContent='检测中…';
  J('/api/clients/scan').then(r=>{
    if(r.error){msg.textContent='检测失败:'+clean(r.error||'');return;}
    window.__CEP=r.endpoint_root||('http://'+location.host);
    window.__CLHINT={};
    (r.clients||[]).forEach(c=>{window.__CLHINT[c.id]=c.write_hint||c.note||'';});
    document.getElementById('clientlist').innerHTML=(r.clients||[]).map(clientRow).join('');
    msg.textContent='点行内「启用」即可代配;只改端点,不动 key;改前自动备份、可一键还原。';
  });
}
function enableOne(id){
  const hint=(window.__CLHINT||{})[id]||'';
  skModal(`<h3 style="margin:0 0 10px;font-size:16px">是否自动帮你代理到这个端点?</h3>
    <div class="livelog">${clean(window.__CEP||'')}(Anthropic/Gemini 系填这个;OpenAI 系填 ${clean(window.__CEP||'')}/v1)</div>
    <div class="inlinehint" style="margin:10px 0">将改动:${clean(hint)}。改前自动备份、可一键还原;只动端点,不动 key;写入后回读校验。</div>
    <div style="display:flex;gap:8px;margin-top:14px"><button class="pri" onclick='confirmEnable(${JSON.stringify([id])})'>确认代配</button>
    <button class="out" onclick="closeModal()">取消</button></div>`);
}
function confirmEnable(ids){
  closeModal();
  const msg=document.getElementById('clientmsg'); msg.textContent='写入中…';
  J('/api/clients/enable',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})}).then(r=>{
    (r.results||[]).forEach(x=>{
      const el=document.getElementById('cl-'+x.id);
      if(el){const s=document.createElement('div');s.className='inlinehint clres';
        s.innerHTML=(x.ok?'✅ ':'⛔ ')+clean(x.detail||'')+(x.backup?' · 备份:'+clean(x.backup):'');
        el.appendChild(s);}
    });
    msg.textContent='完成,结果见每行;改动对已开着的工具要重启才生效。';
    setTimeout(loadClients,2500);
  });
}
function restoreClient(id){
  J('/api/clients/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})}).then(r=>{
    document.getElementById('clientmsg').textContent=r.ok?('✅ '+clean(r.detail||'已还原')):('⛔ '+clean(r.detail||'还原失败'));
    setTimeout(loadClients,1200);
  });
}
function skModal(html){
  closeModal();
  const d=document.createElement('div'); d.id='skmodal';
  d.style.cssText='position:fixed;inset:0;background:rgba(15,23,42,.45);display:flex;align-items:center;justify-content:center;z-index:50';
  d.innerHTML=`<div class="card" style="width:min(560px,92vw);max-height:80vh;overflow:auto;padding:22px 24px">${html}</div>`;
  d.onclick=e=>{if(e.target===d)closeModal();};
  document.body.appendChild(d);
}
function closeModal(){const d=document.getElementById('skmodal'); if(d)d.remove();}
</script></body></html>
"""

EVAL_LEDGER = CFG / "eval_ledger.jsonl"

def api_eval_model(model, layer, base_url, key_file):
    """分层微实测:决策层测出规格,执行层测按规格交付;45s 超时即判卡流。"""
    import ssl as _ssl
    prompts = {
        "decision": ("你是决策层。任务:写一段值班周报。只输出执行规格:【目标】【步骤】【验收】,不超过 100 字。",
                     lambda out: ("目标" in out or "步骤" in out) and len(out) > 20),
        "execution": ("你是执行层。直接输出成品:写一个把秒数格式化为「时:分:秒」的 Python 函数,不解释。",
                      lambda out: ("def " in out or "return" in out) and ":" in out),
    }
    sys_p, judge = prompts[layer]
    key = read_key(key_file)
    if not key: return {"ok": False, "verdict": "fail", "error": "该平台未配置 API"}
    url = base_url.rstrip("/");  url = url if url.endswith("/v1") else url + "/v1"
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": sys_p}],
                       "temperature": 0.2, "max_tokens": 400}).encode()
    req = urllib.request.Request(url + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=45, context=_ssl_ctx()) as r: d = json.loads(r.read())
    except Exception as e:
        err = repr(e)[:90]
        verdict = "stall" if "timed out" in err.lower() else "fail"
        rec = {"ts": time.strftime("%F %T"), "model": model, "layer": layer, "verdict": verdict, "latency": round(time.time()-t0,1)}
        append_jsonl(EVAL_LEDGER, rec)
        return {"ok": False, "verdict": verdict, "error": err, "latency": rec["latency"]}
    out = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    lat = round(time.time() - t0, 1)
    u = d.get("usage", {})
    verdict = "pass" if judge(out) else "weak"
    rec = {"ts": time.strftime("%F %T"), "model": model, "layer": layer, "verdict": verdict,
           "latency": lat, "tok": u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)}
    append_jsonl(EVAL_LEDGER, rec)
    return {"ok": verdict == "pass", "verdict": verdict, "latency": lat, "tok": rec["tok"], "sample": _digest(out, 160)}

def eval_status():
    st = {}
    for r in read_jsonl(EVAL_LEDGER):
        st[r["model"] + "|" + r["layer"]] = r
    return st

# ============ 客户端接入:检测 / 代配 / 还原(2026-11-27) ============
# 红线口径(2026-11-27 拍板):代写前备份、逐项确认、一键还原、只动端点不动 key。
# 端点写法随客户端协议而异:Claude 系 SDK 在 base 后自拼 /v1/messages → 写入值不带 /v1;
# OpenAI Responses 系(Codex)按 provider base_url 拼 /responses → 写入值带 /v1;
# Gemini 系 GOOGLE_GEMINI_BASE_URL 后自拼 /v1beta/... → 写入值不带 /v1。
BACKUP_INDEX = CFG / "client_backups.json"

def _find_bin(*names):
    for n in names:
        p = shutil.which(n)
        if p: return p
    for n in names:
        p = Path.home() / ".local/bin" / n
        if p.is_file() and os.access(p, os.X_OK): return str(p)
    return None

def _backup_file(path):
    """改配置前先留带时间戳的副本,并登记索引(供一键还原)。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = Path(str(path) + ".bak-" + ts)
    shutil.copy2(path, bak)
    idx = json.loads(BACKUP_INDEX.read_text(encoding="utf-8")) if BACKUP_INDEX.is_file() else {}
    idx.setdefault(str(path), []).append({"backup": str(bak), "ts": ts, "action": "auto-config"})
    CFG.mkdir(parents=True, exist_ok=True)
    BACKUP_INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(bak)

def _last_backup(path):
    idx = json.loads(BACKUP_INDEX.read_text(encoding="utf-8")) if BACKUP_INDEX.is_file() else {}
    lst = idx.get(str(path), [])
    return lst[-1]["backup"] if lst else None

def _claude_info():
    home = Path.home()
    binp = _find_bin("claude")
    cfg = home / ".claude" / "settings.json"
    ccswitch = (home / ".claude" / "ccswitch-live").is_dir()
    cur = None
    if cfg.is_file():
        try: cur = (json.loads(cfg.read_text(encoding="utf-8")).get("env") or {}).get("ANTHROPIC_BASE_URL")
        except Exception: cur = "(配置损坏,读不出)"
    installed = bool(binp) or cfg.is_file()
    if ccswitch:
        note = "检测到 cc-switch 在管通道,自动代配会与它打架,请在 cc-switch 里切换;此处不代写。"
        mode = "manual"
    else:
        note = "写入 ~/.claude/settings.json 的 env.ANTHROPIC_BASE_URL(不带 /v1,SDK 自拼 /v1/messages);原无 key 才补占位 key。"
        mode = "auto"
    return {"id": "claude_code", "name": "Claude Code CLI", "installed": installed,
            "path": binp or (str(cfg) if cfg.is_file() else None), "current": cur,
            "mode": mode, "config_file": str(cfg), "note": note,
            "write_hint": f"改 {cfg} 的 env.ANTHROPIC_BASE_URL"}

def _codex_info():
    home = Path.home()
    binp = _find_bin("codex")
    cfg = home / ".codex" / "config.toml"
    cur = None
    if cfg.is_file():
        try:
            import tomllib
            d = tomllib.loads(cfg.read_text(encoding="utf-8"))
            prov = d.get("model_providers", {}).get(d.get("model_provider", ""), {})
            cur = prov.get("base_url") or ("(未配自定义 provider,默认官方)" if d.get("model_provider") in (None, "openai") else d.get("model_provider"))
        except Exception as e: cur = "(TOML 解析失败:" + str(e)[:40] + ")"
    return {"id": "codex", "name": "Codex CLI", "installed": bool(binp) or cfg.is_file(),
            "path": binp or (str(cfg) if cfg.is_file() else None), "current": cur, "mode": "auto",
            "config_file": str(cfg),
            "note": "写入 ~/.codex/config.toml:新增 [model_providers.switchkit](wire_api=\"responses\")并把 model_provider 指过去;不动其他段。",
            "write_hint": f"改 {cfg} 的 model_provider / [model_providers.switchkit]"}

def _gemini_info():
    home = Path.home()
    binp = _find_bin("gemini")
    rc = home / ".zshrc"
    cur = None
    if rc.is_file():
        m = re.search(r'(?m)^\s*export\s+GOOGLE_GEMINI_BASE_URL=(.+)$', rc.read_text(encoding="utf-8"))
        if m: cur = m.group(1).strip().strip('"').strip("'")
    return {"id": "gemini", "name": "Gemini CLI", "installed": bool(binp),
            "path": binp, "current": cur, "mode": "auto" if binp else "auto",
            "config_file": str(rc),
            "note": "在 ~/.zshrc 追加 export GOOGLE_GEMINI_BASE_URL(官方支持的自定义端点变量);新开终端生效。本机未装,字段按官方文档写、未真身验证。",
            "write_hint": f"在 {rc} 追加 export GOOGLE_GEMINI_BASE_URL"}

def _manual_infos():
    home = Path.home()
    rows = [
        ("claude_desktop", "Claude 桌面端", ["/Applications/Claude.app"], "消费级应用,不支持自定义端点;要用 switchkit 请改用 Claude Code CLI。"),
        ("chatgpt", "ChatGPT 桌面端", ["/Applications/ChatGPT.app"], "不支持自定义端点,无法接入。"),
        ("cherry", "Cherry Studio", ["/Applications/Cherry Studio.app", str(home / "Cherry" / "Cherry Studio.app")], "通道配置在它自己的设置界面里,直改它内部文件风险高;请在它的服务商设置里填下面的代理端点。"),
        ("ccswitch", "cc-switch", ["/Applications/cc-switch.app", str(home / ".claude" / "ccswitch-live")], "通道切换器;它在管时 switchkit 不代写 Claude Code 配置。"),
    ]
    out = []
    for cid, name, paths, note in rows:
        hit = next((p for p in paths if Path(p).exists), None)
        out.append({"id": cid, "name": name, "installed": bool(hit), "path": hit,
                    "current": None, "mode": "manual", "config_file": None,
                    "note": note, "write_hint": None})
    return out

def scan_clients():
    return [_claude_info(), _codex_info(), _gemini_info()] + _manual_infos()

def _enable_claude_code(root):
    p = Path.home() / ".claude" / "settings.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    bak = _backup_file(p) if p.is_file() else None
    env = data.setdefault("env", {})
    prev = env.get("ANTHROPIC_BASE_URL")
    env["ANTHROPIC_BASE_URL"] = root
    extra = ""
    if not env.get("ANTHROPIC_API_KEY") and not env.get("ANTHROPIC_AUTH_TOKEN"):
        env["ANTHROPIC_API_KEY"] = "switchkit-local"; extra = "原无 key,补了占位 key(本地代理不校验)"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    back = (json.loads(p.read_text(encoding="utf-8")).get("env") or {}).get("ANTHROPIC_BASE_URL")
    return {"ok": back == root, "detail": f"ANTHROPIC_BASE_URL:{prev or '(空)'} → {root}。{extra}重启 Claude Code 后生效。",
            "backup": bak, "config_file": str(p)}

def _enable_codex(root):
    p = Path.home() / ".codex" / "config.toml"
    txt = p.read_text(encoding="utf-8") if p.is_file() else ""
    bak = _backup_file(p) if p.is_file() else None
    txt = re.sub(r'(?ms)^\[model_providers\.switchkit\].*?(?=^\[|\Z)', '', txt)
    if re.search(r'(?m)^model_provider\s*=', txt):
        txt = re.sub(r'(?m)^model_provider\s*=.*$', 'model_provider = "switchkit"', txt, count=1)
    else:
        txt = 'model_provider = "switchkit"\n' + txt
    block = ('[model_providers.switchkit]\n'
             'name = "switchkit"\n'
             f'base_url = "{root}/v1"\n'
             'wire_api = "responses"\n')
    txt = txt.rstrip() + "\n\n" + block
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(txt, encoding="utf-8")
    try:
        import tomllib
        d = tomllib.loads(p.read_text(encoding="utf-8"))
        ok = d.get("model_provider") == "switchkit" and \
            d.get("model_providers", {}).get("switchkit", {}).get("base_url") == root + "/v1"
        detail = f"model_provider → switchkit,base_url={root}/v1,wire_api=responses(回读校验{'通过' if ok else '未过'})。重启 Codex 后生效。"
    except Exception as e:
        ok = False; detail = "写入后 TOML 解析失败:" + str(e)[:60]
    return {"ok": ok, "detail": detail, "backup": bak, "config_file": str(p)}

def _enable_gemini(root):
    rc = Path.home() / ".zshrc"
    txt = rc.read_text(encoding="utf-8") if rc.is_file() else ""
    bak = _backup_file(rc) if rc.is_file() else None
    line = f'export GOOGLE_GEMINI_BASE_URL="{root}"'
    if "GOOGLE_GEMINI_BASE_URL" in txt:
        txt2 = re.sub(r'(?m)^\s*export\s+GOOGLE_GEMINI_BASE_URL=.*$', line, txt)
    else:
        txt2 = txt.rstrip() + f"\n\n# switchkit 自动接入(还原:删掉这两行)\n{line}\n"
    rc.parent.mkdir(parents=True, exist_ok=True)
    rc.write_text(txt2, encoding="utf-8")
    ok = line in rc.read_text(encoding="utf-8")
    return {"ok": ok, "detail": f"{line} 已写入 {rc}(回读{'一致' if ok else '不一致'});新开终端或 source ~/.zshrc 后生效。",
            "backup": bak, "config_file": str(rc)}

_ENABLERS = {"claude_code": _enable_claude_code, "codex": _enable_codex, "gemini": _enable_gemini}

def enable_clients(ids, root):
    out = []
    for cid in ids:
        fn = _ENABLERS.get(cid)
        if not fn:
            out.append({"id": cid, "ok": False, "detail": "该客户端不支持自动代配(见说明)", "backup": None}); continue
        try: out.append({"id": cid, **fn(root)})
        except Exception as e: out.append({"id": cid, "ok": False, "detail": "写入异常:" + str(e)[:80], "backup": None})
    return out

def restore_client(cid):
    info = {c["id"]: c for c in scan_clients()}.get(cid)
    if not info or not info.get("config_file"): return {"ok": False, "detail": "未知客户端"}
    bak = _last_backup(info["config_file"])
    if not bak or not Path(bak).is_file(): return {"ok": False, "detail": "没有可用备份"}
    shutil.copy2(bak, info["config_file"])
    return {"ok": True, "detail": f"已从 {bak} 还原 {info['config_file']};重启对应工具后生效。"}

def dispatch_text(user_text, source="proxy"):
    """各协议入口共用的分流内核(与 proxy_chat 同逻辑;不改 proxy_chat 行为)。"""
    dual = load_dual(); mode = dual.get("proxy_mode", "split")
    tag = classify(user_text)
    if mode == "split":
        choice, reason = route_for_key(tag["key"])
        if choice in ("user", "passthrough"):
            r = run_passthrough(user_text, source, reason)
            return r.get("answer") or r.get("error") or "", "沿用原方式", reason, tag
        r = run_split(user_text, source)
        return r.get("answer") or r.get("error") or "", "双层分工", reason, tag
    layer = "执行" if mode == "execution" else "决策"
    cfg = dual.get("execution" if layer == "执行" else "decision", {})
    rr = call_layer(layer, cfg, "", user_text, "PROXY")
    content = rr.get("content") or rr.get("error") or ""
    log_stage("P" + str(int(time.time()) % 1000000), source, "pass_done", tag=tag["label"],
              task=_digest(user_text, 150), route=f"直通{layer}层", route_reason=f"强制 {mode} 模式", status="ok",
              passthrough={"model": cfg.get("model", ""), "tok": 0, "answer": _digest(content, 300)})
    return content, f"直通{layer}层", f"强制 {mode} 模式", tag

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _post(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/":
            b = PAGE.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        elif u.path == "/api/state": self._json({"dual": load_dual()})
        elif u.path == "/api/usage": self._json(usage_summary())
        elif u.path == "/api/log": self._json(read_jsonl(DLOG, 200))
        elif u.path == "/api/key_info":
            lay = q.get("layer", ["decision"])[0]
            cfg = load_dual().get("execution" if lay == "execution" else "decision", {})
            k = read_key(cfg.get("key_file", ""))
            if not k: self._json({"error": "API 未配置或不可读"})
            else:
                masked = (k[:4] + "•" * max(6, len(k) - 8) + k[-4:]) if len(k) > 12 else "•" * len(k)
                self._json({"masked": masked, "full": k, "len": len(k)})
        elif u.path.startswith("/icons/"):
            fp = _BASE / "icons" / Path(u.path).name
            if fp.is_file():
                b = fp.read_bytes()
                self.send_response(200); self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            else: self.send_response(404); self.end_headers()
        elif u.path == "/api/eval_status": self._json(eval_status())
        elif u.path == "/api/models_list":
            bu = q.get("base_url", [""])[0]
            if bu == "__aggregate__":
                self._json(api_aggregate())
            else:
                self._json(api_models(bu, q.get("key_file", [""])[0], q.get("vendor", [""])[0]))
        elif u.path == "/api/recent_calls":
            self._json(read_jsonl(USAGE, 5)[::-1])
        elif u.path == "/api/init_status":
            d = dict(INIT)
            el = (time.time() - d["t0"]) if d["t0"] else 0
            d["eta_sec"] = round((el / d["done"]) * (d["total"] - d["done"])) if d["done"] else round(d["total"] * 25)
            self._json(d)
        elif u.path == "/api/templates":
            cur = json.loads(ROUTES.read_text(encoding="utf-8")) if ROUTES.is_file() else None
            if cur:
                for r in cur.get("rows", []): r["fresh"] = _score_fresh(r)
            self._json({"current": cur})
        elif u.path == "/api/check":
            txt = q.get("model", [""])[0]
            try: txt = txt.encode("latin-1").decode("utf-8")
            except Exception: pass
            self._json(check_model(txt))
        elif u.path == "/api/clients/scan":
            self._json({"endpoint_root": f"http://127.0.0.1:{self.server.server_address[1]}",
                        "endpoint_openai": f"http://127.0.0.1:{self.server.server_address[1]}/v1",
                        "clients": scan_clients()})
        else: self.send_response(404); self.end_headers()
    def do_POST(self):
        p = urlparse(self.path).path; d = self._post()
        if p == "/api/config":
            cur = load_dual(); cur.update(d); save_dual(cur); self._json({"ok": True})
        elif p == "/api/gen_items": self._json(api_gen_items(d.get("work", "")))
        elif p == "/api/plan": self._json(api_plan(d.get("task", "")))
        elif p == "/api/execute": self._json(api_execute(d.get("task", ""), d.get("plan", "")))
        elif p == "/api/oneshot": self._json(api_oneshot(d.get("task", "")))
        elif p == "/api/judge": self._json(api_judge(d.get("task", ""), d.get("plan", ""), d.get("a", ""), d.get("b", "")))
        elif p == "/api/commit_routes": self._json(commit_routes(d))
        elif p == "/api/toggle_rule": self._json(toggle_rule(d))
        elif p == "/api/init_rules": self._json(api_init_rules())
        elif p == "/api/selftest":
            r = run_split("用一句话介绍你是做什么的", "selftest")
            self._json({"ok": not r.get("error"), "tag": r.get("tag", {}).get("label"),
                        "answer": _digest(r.get("answer", ""), 120), "usage": r.get("usage", {}),
                        "error": r.get("error")})
        elif p == "/api/eval_model":
            self._json(api_eval_model(d.get("model", ""), d.get("layer", "decision"),
                                      d.get("base_url", ""), d.get("key_file", "")))
        elif p == "/api/ping_layer":
            lay = d.get("layer", "decision")
            cfg = load_dual().get("execution" if lay == "execution" else "decision", {})
            r = call_layer(lay, cfg, "", "只回复两个字:通了", "PING")
            self._json({"ok": not r.get("error"), "latency_sec": r.get("sec"), "error": r.get("error"),
                        "model": cfg.get("model", "")})
        elif p == "/v1/chat/completions": self.proxy_chat(d)
        elif p == "/v1/messages": self.proxy_anthropic(d)
        elif p == "/v1/responses": self.proxy_responses(d)
        elif p.startswith("/v1beta/models/") and p.endswith(":streamGenerateContent"): self.proxy_gemini(d, True)
        elif p.startswith("/v1beta/models/") and p.endswith(":generateContent"): self.proxy_gemini(d, False)
        elif p == "/api/clients/enable":
            self._json({"results": enable_clients(d.get("ids", []), f"http://127.0.0.1:{self.server.server_address[1]}")})
        elif p == "/api/clients/restore": self._json(restore_client(d.get("id", "")))
        else: self.send_response(404); self.end_headers()
    def proxy_chat(self, payload):
        msgs = payload.get("messages", [])
        user_text = next((m.get("content", "") for m in reversed(msgs) if m.get("role") == "user"), "")
        dual = load_dual(); mode = dual.get("proxy_mode", "split")
        tag = classify(user_text)
        if mode == "split":
            choice, reason = route_for_key(tag["key"])
            if choice == "user" or choice == "passthrough":
                r = run_passthrough(user_text, "proxy", reason); content = r.get("answer") or r.get("error") or ""
            else:
                r = run_split(user_text, "proxy"); content = r.get("answer") or r.get("error") or ""
        else:
            layer = "执行" if mode == "execution" else "决策"
            cfg = dual.get("execution" if layer == "执行" else "decision", {})
            rr = call_layer(layer, cfg, "", user_text, "PROXY")
            content = rr.get("content") or rr.get("error") or ""
            log_stage("P" + str(int(time.time()) % 1000000), "proxy", "pass_done", tag=tag["label"],
                      task=_digest(user_text, 150), route=f"直通{layer}层", route_reason=f"强制 {mode} 模式", status="ok",
                      passthrough={"model": cfg.get("model", ""), "tok": 0, "answer": _digest(content, 300)})
        self._json({"id": "chatcmpl-switchkit", "object": "chat.completion", "created": int(time.time()),
                    "model": payload.get("model", "switchkit"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})

    # ---- 协议适配:同一个分流内核,三种线格式(只转文本,工具调用不转) ----
    @staticmethod
    def _last_user_text(msgs, role_key="role", want="user", content_key="content"):
        for m in reversed(msgs or []):
            if isinstance(m, dict) and m.get(role_key) == want:
                c = m.get(content_key, "")
                if isinstance(c, list):
                    c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("text"))
                return c if isinstance(c, str) else str(c)
        return ""

    def _sse(self, events):
        self.send_response(200); self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache"); self.end_headers()
        for name, obj in events:
            self.wfile.write(("event: %s\ndata: %s\n\n" % (name, json.dumps(obj, ensure_ascii=False))).encode())
            self.wfile.flush()

    def proxy_anthropic(self, payload):
        """Anthropic Messages(/v1/messages)→ 分流内核;Claude Code 走这条。"""
        user_text = self._last_user_text(payload.get("messages", []))
        content, route, reason, tag = dispatch_text(user_text, "anthropic")
        model = payload.get("model", "switchkit")
        head = {"id": "msg_switchkit", "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "usage": {"input_tokens": 0, "output_tokens": 0}}
        if payload.get("stream"):
            self._sse([
                ("message_start", {"type": "message_start", "message": head}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": content}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": 0}}),
                ("message_stop", {"type": "message_stop"})])
            return
        self._json({"id": "msg_switchkit", "type": "message", "role": "assistant", "model": model,
                    "content": [{"type": "text", "text": content}], "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}})

    def proxy_responses(self, payload):
        """OpenAI Responses(/v1/responses)→ 分流内核;Codex 现行 wire_api 只认这个。"""
        inp = payload.get("input", "")
        if isinstance(inp, str):
            user_text = inp
        elif isinstance(inp, list):
            user_text = ""
            for it in inp:
                if isinstance(it, str): user_text = it; continue
                if isinstance(it, dict):
                    c = it.get("content", "")
                    if isinstance(c, list):
                        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("text"))
                    if it.get("role") == "user" and (c or "").strip(): user_text = c
            if not user_text:
                user_text = " ".join(str(it) for it in inp)[:4000]
        else:
            user_text = str(inp)
        content, route, reason, tag = dispatch_text(user_text, "responses")
        model = payload.get("model", "switchkit")
        msg = {"id": "msg_switchkit", "type": "message", "status": "completed", "role": "assistant",
               "content": [{"type": "output_text", "text": content, "annotations": []}]}
        resp = {"id": "resp_switchkit", "object": "response", "created_at": int(time.time()), "status": "completed",
                "model": model, "output": [msg], "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}
        if payload.get("stream"):
            self._sse([
                ("response.created", {"type": "response.created", "response": {**resp, "output": []}}),
                ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                                                "item": {"id": "msg_switchkit", "type": "message", "role": "assistant", "status": "in_progress", "content": []}}),
                ("response.content_part.added", {"type": "response.content_part.added", "item_id": "msg_switchkit",
                                                 "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}),
                ("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "msg_switchkit",
                                                "output_index": 0, "content_index": 0, "delta": content}),
                ("response.output_text.done", {"type": "response.output_text.done", "item_id": "msg_switchkit",
                                               "output_index": 0, "content_index": 0, "text": content}),
                ("response.content_part.done", {"type": "response.content_part.done", "item_id": "msg_switchkit",
                                                "output_index": 0, "content_index": 0,
                                                "part": {"type": "output_text", "text": content, "annotations": []}}),
                ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": msg}),
                ("response.completed", {"type": "response.completed", "response": resp})])
            return
        self._json(resp)

    def proxy_gemini(self, payload, stream):
        """Gemini generateContent → 分流内核;Gemini CLI 走这条(只转文本)。"""
        user_text = ""
        for c in reversed(payload.get("contents", []) or []):
            if isinstance(c, dict) and c.get("role") == "user":
                user_text = " ".join(p.get("text", "") for p in c.get("parts", []) if isinstance(p, dict) and p.get("text"))
                break
        content, route, reason, tag = dispatch_text(user_text, "gemini")
        model = payload.get("model", "switchkit")
        resp = {"candidates": [{"content": {"parts": [{"text": content}], "role": "model"},
                                "finishReason": "STOP", "index": 0}],
                "usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0, "totalTokenCount": 0},
                "modelVersion": model}
        if stream:
            self._sse([("data", resp)])
            return
        self._json(resp)

def cmd_serve(args):
    try: srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e: sys.exit(f"端口 {args.port} 不可用:{e}")
    print(f"switchkit:http://127.0.0.1:{args.port}/")
    threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}/")).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\n已停")

def main():
    ap = argparse.ArgumentParser(prog="switchkit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8787); p.set_defaults(fn=cmd_serve)
    a = ap.parse_args(); a.fn(a)

if __name__ == "__main__":
    main()
