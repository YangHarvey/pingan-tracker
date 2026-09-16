#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中国平安（601318.SH）反转指标跟踪 —— 云端版
纯标准库实现，无第三方依赖，可在 GitHub Actions / 任意 cron 环境运行。

数据全部走公开 HTTP 接口：
  - 10Y 国债收益率：东方财富数据中心 RPTA_WEB_TREASURYYIELD（源：中国债券信息网）
  - 行情 / 估值 / K线 / 资金流：东方财富 push2 / push2his

推送通道（配置哪个就用哪个，都配就都发）：
    1) 企微群机器人 webhook：
       export WECOM_WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
    2) 邮件（HTML 报告作为附件，推荐）：
       export SMTP_HOST="smtp.exmail.qq.com"   # 腾讯企业邮 / smtp.qq.com / smtp.163.com / smtp.gmail.com
       export SMTP_PORT="465"                  # 465=SSL，587=STARTTLS（587 时需设 SMTP_STARTTLS=1）
       export SMTP_USER="you@example.com"
       export SMTP_PASS="授权码"                # 不是登录密码，是邮箱里生成的 SMTP 授权码
       export MAIL_TO="you@example.com"        # 可省略，默认发给自己

用法：
    export TZ=Asia/Shanghai
    python3 tracker.py                 # 正常跑 + 推送
    python3 tracker.py --dry-run       # 只生成报告，不推送
    python3 tracker.py --test-push     # 只发一条测试消息验证通道
"""

import argparse
import datetime as dt
import html
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BASE = Path(__file__).resolve().parent
CN_TZ = dt.timezone(dt.timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def now_cn():
    return dt.datetime.now(CN_TZ)


_LAST_REQ = [0.0]
_MIN_INTERVAL = 0.9  # 东财会限流：连续请求间隔过短会直接断连，这里做全局节流


def http_json(url, retries=5, timeout=25, referer="https://data.eastmoney.com/"):
    """带节流 + 指数退避重试的 JSON GET。失败抛异常，由调用方决定降级。"""
    last_err = None
    for attempt in range(retries):
        gap = time.time() - _LAST_REQ[0]
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": referer,
                "Accept": "application/json, text/plain, */*",
            })
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                _LAST_REQ[0] = time.time()
                return json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            _LAST_REQ[0] = time.time()
            time.sleep(1.2 * (2 ** attempt))
    raise RuntimeError(f"请求失败（已重试 {retries} 次）：{url} —— {last_err}")


def fnum(v, default=None):
    """东财字段常见 '-' / None / 0 占位，统一转 float。"""
    try:
        if v is None or v == "-" or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def fmt_pct(v, digits=2, sign=True):
    if v is None:
        return "未取到"
    s = f"{v:+.{digits}f}%" if sign else f"{v:.{digits}f}%"
    return s


def fmt_bp(v):
    return "未取到" if v is None else f"{v:+.1f}bp"


# --------------------------------------------------------------------------
# 数据抓取
# --------------------------------------------------------------------------
def fetch_yield_10y(days=40):
    """
    中国 10 年期国债收益率（中债登口径，经东财数据中心转发）。
    返回 dict：latest / prev / series(list of (date, y10))
    """
    url = ("https://datacenter.eastmoney.com/api/data/get"
           "?type=RPTA_WEB_TREASURYYIELD&sty=ALL&st=SOLAR_DATE&sr=-1"
           "&token=894050c76af8597a853f5b408b759f5d&p=1&ps=%d&pageNo=1&pageNum=1" % days)
    js = http_json(url)
    rows = (js.get("result") or {}).get("data") or []
    series = []
    for r in rows:
        d = (r.get("SOLAR_DATE") or "")[:10]
        y = fnum(r.get("EMM00166466"))  # 中国国债收益率 10 年
        if d and y:
            series.append((d, y))
    series.sort(key=lambda x: x[0])  # 升序
    if not series:
        raise RuntimeError("国债收益率序列为空")
    latest_d, latest_y = series[-1]
    prev_y = series[-2][1] if len(series) >= 2 else None

    def ago(n):
        return series[-1 - n][1] if len(series) > n else None

    return {
        "date": latest_d,
        "latest": latest_y,
        "prev": prev_y,
        "d1": None if prev_y is None else (latest_y - prev_y) * 100,
        "w1": None if ago(5) is None else (latest_y - ago(5)) * 100,
        "m1": None if ago(20) is None else (latest_y - ago(20)) * 100,
        "series": series,
        "source": "东方财富数据中心 RPTA_WEB_TREASURYYIELD（源：中国债券信息网）",
    }


def http_text(url, retries=4, timeout=20, referer="https://gu.qq.com/", encoding="gbk"):
    """带节流的文本 GET（腾讯行情为 GBK 编码）。"""
    last_err = None
    for attempt in range(retries):
        gap = time.time() - _LAST_REQ[0]
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
                _LAST_REQ[0] = time.time()
                return resp.read().decode(encoding, "ignore")
        except Exception as e:  # noqa: BLE001
            last_err = e
            _LAST_REQ[0] = time.time()
            time.sleep(1.2 * (2 ** attempt))
    raise RuntimeError(f"请求失败（已重试 {retries} 次）：{url} —— {last_err}")


def _quotes_tencent(items):
    """腾讯批量行情：一次请求拿全部标的，含 PE(TTM)/PB/总市值。"""
    syms = ",".join(_tencent_symbol(i["secid"]) for i in items)
    raw = http_text(f"https://qt.gtimg.cn/q={syms}")
    out = {}
    for line in raw.split(";"):
        if '="' not in line:
            continue
        v = line.split('="')[1].strip('"').split("~")
        if len(v) < 47:
            continue
        out[v[2]] = {
            "code": v[2], "name": v[1],
            "price": fnum(v[3]),
            "chg_pct": fnum(v[32]),
            "pe": fnum(v[39]),          # 市盈率 TTM
            "pb": fnum(v[46]),          # 市净率
            "mktcap_yi": fnum(v[45]),   # 总市值（亿元）
            "floatcap_yi": fnum(v[44]),
            "total_mv": (fnum(v[45]) or 0) * 1e8,
        }
    return out


def _tencent_symbol(secid):
    return ("sh" if str(secid).startswith("1.") else "sz") + str(secid).split(".")[1]


def _quotes_eastmoney(items):
    """东财批量行情（备源）。含 f25 年初至今涨跌幅。"""
    secids = ",".join(i["secid"] for i in items)
    fields = "f2,f3,f9,f12,f14,f23,f24,f25,f20,f21,f116"
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2"
           f"&secids={secids}&fields={fields}&ut=b2884a393a59ad64002292a3e90d46a5")
    js = http_json(url, referer="https://quote.eastmoney.com/")
    out = {}
    for it in ((js.get("data") or {}).get("diff") or []):
        out[it.get("f12")] = {
            "code": it.get("f12"),
            "name": it.get("f14"),
            "price": fnum(it.get("f2")),
            "chg_pct": fnum(it.get("f3")),
            "pe": (lambda v: None if v in (None, 0) else v / 100)(fnum(it.get("f9"))),
            "pb": (lambda v: None if v in (None, 0) else v / 100)(fnum(it.get("f23"))),
            "d60": fnum(it.get("f24")),
            "ytd": fnum(it.get("f25")),
            "mktcap_yi": (lambda v: None if v is None else v / 1e8)(fnum(it.get("f20"))),
            "floatcap_yi": (lambda v: None if v is None else v / 1e8)(fnum(it.get("f21"))),
            "total_mv": fnum(it.get("f116")),
        }
    return out


def fetch_quotes(items):
    """
    批量实时行情。主源腾讯（一次请求拿到 PE/PB/总市值），
    再尝试用东财补 YTD 与 60 日涨幅；补不到也不影响主流程。
    """
    out = {}
    try:
        out = _quotes_tencent(items)
    except Exception:  # noqa: BLE001
        out = {}
    if not out:
        return _quotes_eastmoney(items)
    try:
        for code, v in _quotes_eastmoney(items).items():
            if code in out:
                out[code]["ytd"] = v.get("ytd")
                out[code]["d60"] = v.get("d60")
    except Exception:  # noqa: BLE001
        pass
    return out


def _kline_tencent(secid, n=280):
    """腾讯日 K（前复权）。返回 [(date, close, pct)] 升序。"""
    sym = _tencent_symbol(secid)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq"
    js = http_json(url, referer="https://gu.qq.com/")
    node = (js.get("data") or {}).get(sym) or {}
    out, prev = [], None
    for row in (node.get("qfqday") or node.get("day") or []):
        if len(row) < 3:
            continue
        d, close = row[0], fnum(row[2])
        pct = None if (prev in (None, 0) or close is None) else (close / prev - 1) * 100
        out.append((d, close, pct))
        prev = close
    return out


def _kline_eastmoney(secid, beg=None):
    """东财日 K（前复权）。push2his 偶发断连，仅作备源。"""
    if beg is None:
        beg = (now_cn() - dt.timedelta(days=430)).strftime("%Y%m%d")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&klt=101&fqt=1&beg={beg}&end=20500101"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f53,f59"
           "&ut=fa5fd1943c7b386f172d6893dbfba10b")
    js = http_json(url, retries=2, referer="https://quote.eastmoney.com/")
    out = []
    for line in ((js.get("data") or {}).get("klines") or []):
        p = line.split(",")
        if len(p) >= 3:
            out.append((p[0], fnum(p[1]), fnum(p[2])))
    return out


def fetch_kline(secid, beg=None):
    """
    前复权日 K，主源腾讯、备源东财。返回 [(date, close, pct), ...] 升序。
    用于自算各周期涨幅、YTD、52 周高低 —— 口径统一，不依赖行情方字段。
    """
    out = []
    try:
        out = _kline_tencent(secid)
    except Exception:  # noqa: BLE001
        out = []
    if not out:
        out = _kline_eastmoney(secid, beg)
    if not out:
        raise RuntimeError("腾讯与东财 K 线均取不到")
    out.sort(key=lambda x: x[0])
    return out


def fetch_fundflow(secid, lmt=12):
    """主力资金流历史日线。返回 list[(date, 主力净流入元, 主力净占比%)] 升序。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
           f"?lmt={lmt}&klt=101&secid={secid}"
           "&fields1=f1,f2,f3,f7&fields2=f51,f52,f57"
           "&ut=b2884a393a59ad64002292a3e90d46a5")
    # 次要指标：失败即标「未取到」。海外 IP（如 GitHub Actions）常被东财拒绝，
    # 重试次数压到 2 次、超时 10s，避免无谓拖慢整个任务（曾导致单跑 50s+）。
    js = http_json(url, retries=2, timeout=10, referer="https://quote.eastmoney.com/")
    out = []
    for line in ((js.get("data") or {}).get("klines") or []):
        p = line.split(",")
        if len(p) >= 3:
            out.append((p[0], fnum(p[1]) or 0.0, fnum(p[2])))
    out.sort(key=lambda x: x[0])
    return out


# --------------------------------------------------------------------------
# 指标计算
# --------------------------------------------------------------------------
def kline_changes(kl):
    """由日 K 计算各周期涨幅 / YTD / 52 周高低。"""
    if not kl:
        return {}
    dates = [x[0] for x in kl]
    closes = [x[1] for x in kl]
    last = closes[-1]

    def chg(n):
        return None if len(closes) <= n else (last / closes[-1 - n] - 1) * 100

    # YTD：相对上一年最后一个交易日
    cur_year = last_date = dates[-1][:4]
    ytd = None
    for i in range(len(dates) - 1, -1, -1):
        if dates[i][:4] < cur_year:
            ytd = (last / closes[i] - 1) * 100
            break
    # 52 周：取最近 250 个交易日
    win = closes[-250:]
    return {
        "last_date": last_date,
        "close": last,
        "d1": kl[-1][2] if kl[-1][2] is not None else None,
        "d5": chg(5), "d10": chg(10), "d20": chg(20), "d60": chg(60),
        "ytd": ytd,
        "low52": min(win), "high52": max(win),
    }


def two_weeks_above(series, thr):
    """最近 10 个交易日（约两周）是否全部站上阈值。"""
    tail = series[-10:]
    return len(tail) >= 10 and all(y >= thr for _, y in tail)


def consecutive_outperform(a_kl, b_kl, n=5):
    """最近 n 个交易日 a 是否逐日跑赢 b。"""
    if len(a_kl) <= n or len(b_kl) <= n:
        return False, None
    a_pct = {d: p for d, _, p in a_kl}
    b_pct = {d: p for d, _, p in b_kl}
    common = [d for d in a_pct if d in b_pct]
    common.sort()
    tail = common[-n:]
    if len(tail) < n:
        return False, None
    ok = all((a_pct[d] or 0) > (b_pct[d] or 0) for d in tail)
    return ok, tail


# --------------------------------------------------------------------------
# 历史（环比）
# --------------------------------------------------------------------------
def load_history(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_history(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# 判定
# --------------------------------------------------------------------------
def judge(cfg, y10, pev, outperf5, two_weeks_flag):
    th = cfg["thresholds"]
    y, p = th["yield_10y"], th["pev"]
    conds = []
    level = "未到"

    watch_y = y10 is not None and y10 >= y["watch"]
    watch_p = pev is not None and pev >= p["watch"]
    confirm_y = y10 is not None and y10 >= y["confirm"]
    confirm_p = pev is not None and pev >= p["confirm"]
    prelim = bool(two_weeks_flag) and pev is not None and pev >= p["prelim"]

    if watch_y or watch_p or outperf5:
        level = "观察"
    if prelim:
        level = "初步确认"
    if confirm_y or confirm_p:
        level = "确认"

    conds.append(("10Y 国债 ≥ %.2f%%（观察线）" % y["watch"], watch_y,
                  "未取到" if y10 is None else "%.4f%%" % y10))
    conds.append(("P/EV ≥ %.2f（观察线）" % p["watch"], watch_p,
                  "未取到" if pev is None else "%.2f×" % pev))
    conds.append(("保险板块连续 5 日跑赢沪深300", bool(outperf5), "是" if outperf5 else "否"))
    conds.append(("10Y 连续两周 ≥ %.2f%% 且 P/EV ≥ %.2f" % (y["prelim"], p["prelim"]),
                  prelim, "是" if prelim else "否"))
    conds.append(("10Y 突破 %.2f%% 或 P/EV ≥ %.2f（确认线）" % (y["confirm"], p["confirm"]),
                  confirm_y or confirm_p, "未取到" if y10 is None else "%.4f%%" % y10))

    risk = []
    if y10 is not None and y10 < y["deteriorate"]:
        risk.append(f"10Y 跌破 {y['deteriorate']:.2f}% 恶化线，推迟介入")
    return level, conds, risk


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def render_markdown(ctx):
    """企微 markdown：不支持表格，统一用列表。控制在 4096 字节内。"""
    y = ctx["yield"]
    s = ctx["stock"]
    L = []
    L.append(f"**中国平安反转指标跟踪 · {ctx['data_date']}**")
    L.append(f"> 信号档位：**{ctx['level']}**　{'｜'.join(ctx['risk']) if ctx['risk'] else '无恶化信号'}")
    L.append("")
    L.append("**核心指标**")
    y10 = "未取到" if y["latest"] is None else f"{y['latest']:.4f}%"
    L.append(f"- **10Y 国债**：{y10}（{y['date']}）　周变动 {fmt_bp(y['w1'])}")
    L.append(f"- **P/EV**：{'%.2f×' % ctx['pev'] if ctx['pev'] else '未取到'}　"
             f"**PE**：{'%.2f' % s['pe'] if s.get('pe') else '未取到'}　"
             f"**PB**：{'%.2f' % s['pb'] if s.get('pb') else '未取到'}")
    close = "未取到" if s.get("close") is None else f"{s['close']:.2f} 元"
    L.append(f"- **收盘**：{close}（{fmt_pct(s.get('d1'))}）　**YTD** {fmt_pct(s.get('ytd'))}")
    L.append("")
    L.append("**触发情况**")
    hit = [c[0] for c in ctx["conds"] if c[1]]
    L.append("- " + ("；".join(hit) if hit else "本档条件均未触发，维持原判"))
    L.append("")
    L.append("**边际变化**")
    for line in ctx["highlights"]:
        L.append(f"- {line}")
    L.append("")
    L.append(f"**建议**：{ctx['advice']}")
    if ctx.get("page_url"):
        L.append(f"\n[完整报告]({ctx['page_url']})")
    L.append("\n> 数据来自东方财富公开接口，仅供参考，不构成投资建议。")
    return "\n".join(L)


def render_html(ctx):
    y = ctx["yield"]
    s = ctx["stock"]
    esc = html.escape

    def n2(v, d=2):
        return "未取到" if v is None else f"{v:.{d}f}"

    def cls(v):
        if v is None:
            return "flat"
        return "up" if v > 0 else ("down" if v < 0 else "flat")

    peer_rows = "".join(
        f"<tr><td>{esc(p['name'])}</td><td class='num'>{p['price']:.2f}</td>"
        f"<td class='num {cls(p['chg_pct'])}'>{fmt_pct(p['chg_pct'])}</td>"
        f"<td class='num {cls(p['ytd'])}'>{fmt_pct(p['ytd'])}</td>"
        f"<td class='num'>{('%.2f' % p['pe']) if p['pe'] else '—'}</td>"
        f"<td class='num'>{('%.2f' % p['pb']) if p['pb'] else '—'}</td></tr>"
        for p in ctx["peers"]
    )

    cond_rows = "".join(
        f"<tr><td>{esc(c[0])}</td><td class='num'>{esc(str(c[2]))}</td>"
        f"<td class='{'hit' if c[1] else 'miss'}'>{'已触发' if c[1] else '未触发'}</td></tr>"
        for c in ctx["conds"]
    )

    yv = y["latest"] if y["latest"] is not None else 0.0
    ylab = "未取到" if y["latest"] is None else f"{y['latest']:.4f}%"
    bench = ctx["bench_chg"]
    excess = {k: (None if (ctx["stock"].get(k) is None or bench.get(k) is None)
                  else ctx["stock"][k] - bench[k]) for k in ("d5", "d10", "d20", "d60", "ytd")}

    hl = "".join(f"<li>{esc(x)}</li>" for x in ctx["highlights"])

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>中国平安反转指标跟踪 · {ctx['data_date']}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
:root{{--up:#c62828;--down:#2e7d32;--flat:#666;--bg:#f7f8fa;--card:#fff;--line:#e6e8eb;--txt:#1f2329;--sub:#646a73;--accent:#1a6fd4}}
*{{box-sizing:border-box}}
body{{margin:0;padding:16px;background:var(--bg);color:var(--txt);font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}}
.wrap{{max-width:900px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}}
h2{{font-size:16px;margin:24px 0 10px;padding-left:9px;border-left:3px solid var(--accent)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}}
.badge{{display:inline-block;padding:3px 12px;border-radius:999px;font-size:13px;font-weight:600;color:#fff;background:{ctx['level_color']}}}
.meta{{color:var(--sub);font-size:13px;margin-top:6px}}
.concl{{font-size:15px;margin:12px 0 0}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{padding:9px 6px;border-bottom:1px solid var(--line);text-align:left}}
th{{color:var(--sub);font-weight:500;font-size:13px}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.up{{color:var(--up)}} .down{{color:var(--down)}} .flat{{color:var(--flat)}}
.hit{{color:var(--up);font-weight:600}} .miss{{color:var(--sub)}}
.chart{{width:100%;height:280px}}
ul{{margin:8px 0;padding-left:20px}} li{{margin:4px 0}}
.disc{{color:var(--sub);font-size:12px;line-height:1.6}}
</style></head><body><div class="wrap">

<div class="card">
  <h1>中国平安 601318 · 反转指标跟踪</h1>
  <div><span class="badge">信号档位：{ctx['level']}</span></div>
  <div class="meta">数据日期 {ctx['data_date']}（上一交易日收盘）｜生成于 {ctx['gen_time']}</div>
  <p class="concl">{esc(ctx['advice'])}</p>
</div>

<div class="card">
  <h2>核心指标</h2>
  <table>
    <tr><th>指标</th><th class="num">本期</th><th class="num">上期</th><th class="num">变动</th></tr>
    <tr><td>10Y 国债收益率<div class="meta">{y['date']}</div></td><td class="num">{('%.4f%%' % y['latest']) if y['latest'] is not None else '未取到'}</td><td class="num">{('%.4f%%' % ctx['prev']['yield_10y']) if ctx['prev'].get('yield_10y') else '—'}</td><td class="num {cls(ctx['dy10'])}">{fmt_bp(ctx['dy10'])}</td></tr>
    <tr><td>P/EV</td><td class="num">{('%.2f×' % ctx['pev']) if ctx['pev'] else '未取到'}</td><td class="num">{('%.2f×' % ctx['prev']['pev']) if ctx['prev'].get('pev') else '—'}</td><td class="num">{ctx['dpev']}</td></tr>
    <tr><td>收盘价</td><td class="num">{n2(s.get('close'))} 元</td><td class="num">{('%.2f' % ctx['prev']['close']) if ctx['prev'].get('close') else '—'}</td><td class="num {cls(s.get('d1'))}">{fmt_pct(s.get('d1'))}</td></tr>
    <tr><td>YTD</td><td class="num {cls(s['ytd'])}">{fmt_pct(s['ytd'])}</td><td class="num">{fmt_pct(ctx['prev'].get('ytd'))}</td><td class="num">{fmt_pct(ctx['dytd'])}</td></tr>
    <tr><td>PE(TTM) / PB</td><td class="num">{('%.2f / %.2f' % (s['pe'], s['pb'])) if s['pe'] and s['pb'] else '未取到'}</td><td class="num">—</td><td class="num">—</td></tr>
    <tr><td>主力净流（当日 / 5日）</td><td class="num">{ctx['flow_txt']}</td><td class="num">—</td><td class="num">—</td></tr>
    <tr><td>预定利率研究值</td><td class="num">{ctx['manual']['preset_rate_research']}%（{ctx['manual']['preset_rate_asof']}）</td><td class="num">—</td><td class="num">—</td></tr>
  </table>
</div>

<div class="card">
  <h2>长端利率信号标尺</h2>
  <div id="gauge" class="chart"></div>
</div>

<div class="card">
  <h2>平安 vs 沪深300 超额收益</h2>
  <div id="excess" class="chart"></div>
</div>

<div class="card">
  <h2>触发条件清单</h2>
  <table><tr><th>条件</th><th class="num">当前值</th><th>状态</th></tr>{cond_rows}</table>
</div>

<div class="card">
  <h2>本期边际变化</h2>
  <ul>{hl}</ul>
</div>

<div class="card">
  <h2>保险板块同业</h2>
  <table><tr><th>名称</th><th class="num">现价</th><th class="num">日涨跌</th><th class="num">YTD</th><th class="num">PE</th><th class="num">PB</th></tr>{peer_rows}</table>
  <div class="meta">YTD 由日 K 线自算（前复权），与行情软件口径可能略有差异。</div>
</div>

<div class="card">
  <h2>下一步观察要点</h2>
  <ul>
    <li>10Y 国债能否连续两周站稳 1.85%（右侧信号）</li>
    <li>主力资金流何时由负转正</li>
    <li>中保协 Q3 预定利率研究值（10 月公布）是否延续回升</li>
    <li>52 周区间 {n2(s.get('low52'))} / {n2(s.get('high52'))}，下方关注 MA60 支撑</li>
  </ul>
</div>

<p class="disc">
数据来源：东方财富公开接口（10Y 国债源出中国债券信息网）；P/EV = 总市值 / 内含价值，
内含价值取 {ctx['manual']['ev_asof']} 披露值 {ctx['manual']['ev_total_yi']:.0f} 亿元（人工维护，请按最新财报核对）。
本内容基于公开数据分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。
</p>

<script>
var gauge = echarts.init(document.getElementById('gauge'));
gauge.setOption({{
  grid:{{left:40,right:30,top:30,bottom:40}},
  xAxis:{{type:'category',data:['1.60 恶化','1.75 观察','1.85 初步','2.00 确认'],axisLabel:{{color:'#646a73',fontSize:11}}}},
  yAxis:{{type:'value',min:{ctx['gauge_min']},max:{ctx['gauge_max']},axisLabel:{{formatter:'{{value}}%',color:'#646a73',fontSize:11}},splitLine:{{lineStyle:{{color:'#eef0f2'}}}}}},
  series:[
    {{type:'bar',barWidth:22,silent:true,data:[{ctx['gauge_min']},1.75,1.85,2.00],
      itemStyle:{{color:function(p){{return ['#2e7d32','#f0a020','#f0a020','#c62828'][p.dataIndex]}}}},
      markLine:{{symbol:'none',data:[{{yAxis:{yv}}}],lineStyle:{{color:'#1a6fd4',width:2,type:'solid'}},
        label:{{formatter:'当前 {ylab}',position:'insideEndTop',color:'#1a6fd4',fontSize:11}}}}}},
    {{type:'line',data:[{yv},{yv},{yv},{yv}],lineStyle:{{color:'#1a6fd4',width:2}},symbol:'none'}}
  ]
}});
var ex = echarts.init(document.getElementById('excess'));
ex.setOption({{
  tooltip:{{trigger:'axis',valueFormatter:function(v){{return v==null?'—':v.toFixed(2)+'%'}}}},
  legend:{{data:['中国平安','沪深300','超额'],textStyle:{{color:'#646a73',fontSize:11}},top:0}},
  grid:{{left:45,right:20,top:36,bottom:28}},
  xAxis:{{type:'category',data:{json.dumps(['近5日','近10日','近20日','近60日','年初至今'])},axisLabel:{{color:'#646a73',fontSize:11}}}},
  yAxis:{{type:'value',axisLabel:{{formatter:'{{value}}%',color:'#646a73',fontSize:11}},splitLine:{{lineStyle:{{color:'#eef0f2'}}}}}},
  series:[
    {{name:'中国平安',type:'bar',data:{json.dumps(ctx['chg_series'])},itemStyle:{{color:'#5b8ff9'}}}},
    {{name:'沪深300',type:'bar',data:{json.dumps(ctx['bench_series'])},itemStyle:{{color:'#c9cdd4'}}}},
    {{name:'超额',type:'line',data:{json.dumps([excess[k] for k in ('d5','d10','d20','d60','ytd')])},lineStyle:{{color:'#c62828',width:2}},itemStyle:{{color:'#c62828'}}}}
  ]
}});
window.addEventListener('resize',function(){{gauge.resize();ex.resize()}});
</script>
</div></body></html>"""


# --------------------------------------------------------------------------
# 推送
# --------------------------------------------------------------------------
def push_wecom(webhook, markdown):
    if not webhook:
        return False, "未配置 WECOM_WEBHOOK_URL，跳过推送"
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": markdown}}).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload,
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
            r = json.loads(resp.read().decode("utf-8", "ignore"))
        ok = r.get("errcode") == 0
        return ok, json.dumps(r, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只生成报告，不推送")
    ap.add_argument("--test-push", action="store_true", help="只发一条测试消息验证通道，不跑数据")
    ap.add_argument("--outdir", default=str(BASE / "reports"))
    args = ap.parse_args()

    if args.test_push:
        ok, msg = push_wecom(
            os.environ.get("WECOM_WEBHOOK_URL", ""),
            "**通道自检**\n> 中国平安反转跟踪 · 企微推送链路\n\n收到这条说明 webhook 配置正确，"
            "云端定时任务可以正常投递。")
        print(f"[通道测试] {'成功' if ok else '失败'} —— {msg}")
        return 0

    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    errors = []
    try:
        y = fetch_yield_10y()
    except Exception as e:  # noqa: BLE001
        errors.append(f"10Y 国债：{e}")
        y = {"date": "—", "latest": None, "prev": None, "d1": None, "w1": None,
             "m1": None, "series": [], "source": "—"}

    try:
        targets = cfg["peers"] + [cfg["benchmark"]]
        quotes = fetch_quotes(targets)
    except Exception as e:  # noqa: BLE001
        errors.append(f"行情：{e}")
        quotes = {}

    try:
        skl = fetch_kline(cfg["stock"]["secid"])
        schg = kline_changes(skl)
    except Exception as e:  # noqa: BLE001
        errors.append(f"平安 K 线：{e}")
        skl, schg = [], {}

    try:
        bkl = fetch_kline(cfg["benchmark"]["secid"])
        bchg = kline_changes(bkl)
    except Exception as e:  # noqa: BLE001
        errors.append(f"沪深300 K 线：{e}")
        bkl, bchg = [], {}

    try:
        flow = fetch_fundflow(cfg["stock"]["secid"])
    except Exception as e:  # noqa: BLE001
        errors.append(f"资金流：{e}")
        flow = []

    sq = quotes.get(cfg["stock"]["code"], {})
    sq["ytd_f"], sq["d60_f"] = sq.get("ytd"), sq.get("d60")  # 批量字段，作 K 线失败时的兜底
    if schg.get("close") is not None:
        sq["close"] = schg["close"]
    if schg.get("d1") is None and sq.get("chg_pct") is not None:
        schg["d1"] = sq["chg_pct"]
    sq.update({k: schg.get(k) for k in ("d1", "d5", "d10", "d20", "d60", "ytd", "low52", "high52") if k in schg})
    for k in ("close", "d1", "d5", "d10", "d20", "d60", "ytd", "low52", "high52", "pe", "pb"):
        sq.setdefault(k, None)
    # K 线缺失时，用批量行情的 f25 / f24 兜底 YTD 与 60 日涨幅
    if sq.get("ytd") is None:
        sq["ytd"] = sq.get("ytd_f")
    if sq.get("d60") is None:
        sq["d60"] = sq.get("d60_f")

    # P/EV
    man = cfg["manual"]
    pev = None
    mv = sq.get("total_mv") or (sq.get("mktcap_yi") * 1e8 if sq.get("mktcap_yi") else None)
    if mv and man.get("ev_total_yi"):
        pev = mv / 1e8 / man["ev_total_yi"]

    # 连续两周 / 连续 5 日跑赢
    two_flag = two_weeks_above(y["series"], cfg["thresholds"]["yield_10y"]["prelim"]) if y["series"] else False
    outperf5, _ = consecutive_outperform(skl, bkl, 5)

    level, conds, risk = judge(cfg, y["latest"], pev, outperf5, two_flag)

    # 环比
    hist = load_history(BASE / "data" / "history.json")
    prev = hist.get("last") or cfg["baseline"]
    dy10 = None if (y["latest"] is None or prev.get("yield_10y") is None) else (y["latest"] - prev["yield_10y"]) * 100
    dytd = None if (sq.get("ytd") is None or prev.get("ytd") is None) else sq["ytd"] - prev["ytd"]
    dpev = "—"
    if pev is not None and prev.get("pev") is not None:
        dpev = f"{pev - prev['pev']:+.2f}"
    dclose = None if (sq.get("close") is None or prev.get("close") is None) else sq["close"] - prev["close"]

    # 资金流文本
    if flow:
        f_today = flow[-1][1] / 1e8
        f5 = sum(x[1] for x in flow[-5:]) / 1e8
        flow_txt = f"{f_today:+.2f} 亿 / {f5:+.2f} 亿"
    else:
        flow_txt = "未取到"

    # 边际变化
    hl = []
    if y["w1"] is not None:
        hl.append(f"10Y 国债周变动 {fmt_bp(y['w1'])}、日变动 {fmt_bp(y['d1'])}，"
                  f"{'长端企稳' if y['w1'] > 0 else '长端仍在下行'}")
    if dy10 is not None:
        hl.append(f"较上期（{prev.get('date', '—')}）10Y 变动 {fmt_bp(dy10)}")
    if dclose is not None:
        hl.append(f"股价较上期 {dclose:+.2f} 元")
    for k, label in (("d5", "近5日"), ("d20", "近20日"), ("ytd", "YTD")):
        if sq.get(k) is not None and bchg.get(k) is not None:
            hl.append(f"{label}超额 {sq[k] - bchg[k]:+.2f}pct（平安 {sq[k]:+.2f}% vs 沪深300 {bchg[k]:+.2f}%）")
    if flow:
        f5 = sum(x[1] for x in flow[-5:]) / 1e8
        hl.append(f"主力资金近 5 日 {f5:+.2f} 亿，{'买盘回流' if f5 > 0 else '买盘未回流'}")
    if outperf5:
        hl.append("保险板块已连续 5 个交易日跑赢沪深300")
    if errors:
        # 摘要里只提示指标名，完整错误保留在控制台日志
        hl.append("未取到：" + "、".join(e.split("：")[0] for e in errors))

    advice = {
        "确认": "核心条件已满足，趋势基本确立，可考虑按计划加仓。",
        "初步确认": "利率与估值双条件初步满足，右侧信号成立，可小仓位试探。",
        "观察": "已出现边际改善信号，但尚未确认，建议继续观察、暂不追高。",
        "未到": "核心条件未触发，维持观望；下行靠 5% 股息率与低估值托底。",
    }[level]
    if risk:
        advice += "　⚠ " + "；".join(risk)

    # 报告日期取最新交易日；国债数据通常 T+1，其自身日期单独标注
    data_date = (skl[-1][0] if skl else None) or \
                (y.get("date") if y.get("date") != "—" else now_cn().strftime("%Y-%m-%d"))

    # 在线报告链接：优先读环境变量（CI 里自动拼 Pages 地址），其次读 config.json
    page_base = os.environ.get("REPORT_PAGE_BASE", "").strip() or \
        cfg.get("push", {}).get("page_base_url", "").strip()
    page_url = page_base.rstrip("/") + f"/{data_date.replace('-', '')}.html" if page_base else ""

    ctx = {
        "data_date": data_date,
        "gen_time": now_cn().strftime("%Y-%m-%d %H:%M"),
        "level": level,
        "level_color": {"确认": "#c62828", "初步确认": "#e67e22", "观察": "#1a6fd4", "未到": "#646a73"}[level],
        "yield": y, "stock": sq, "pev": pev, "prev": prev,
        "dy10": dy10, "dytd": dytd, "dpev": dpev, "dclose": dclose,
        "bench_chg": bchg,
        "chg_series": [sq.get(k) for k in ("d5", "d10", "d20", "d60", "ytd")],
        "bench_series": [bchg.get(k) for k in ("d5", "d10", "d20", "d60", "ytd")],
        "peers": [{"name": p["name"],
                   "price": (quotes.get(p["code"], {}) or {}).get("price") or 0,
                   "chg_pct": (quotes.get(p["code"], {}) or {}).get("chg_pct") or 0,
                   "pe": (quotes.get(p["code"], {}) or {}).get("pe"),
                   "pb": (quotes.get(p["code"], {}) or {}).get("pb"),
                   "ytd": (quotes.get(p["code"], {}) or {}).get("ytd")}
                  for p in cfg["peers"]],
        "conds": conds, "risk": risk, "highlights": hl, "advice": advice,
        "flow_txt": flow_txt, "manual": man, "page_url": page_url,
        "gauge_min": round(min(1.55, (y["latest"] or 1.7) - 0.05), 2),
        "gauge_max": 2.15,
    }

    md = render_markdown(ctx)
    html_text = render_html(ctx)
    out_html = outdir / f"{data_date.replace('-', '')}.html"
    out_html.write_text(html_text, encoding="utf-8")
    (outdir / "latest.md").write_text(md, encoding="utf-8")
    # 供 GitHub Pages 发布：永远指向最新一期
    (outdir / "latest.html").write_text(html_text, encoding="utf-8")

    pushed = (False, "dry-run")
    if not args.dry_run:
        pushed = push_wecom(os.environ.get("WECOM_WEBHOOK_URL", ""), md)

    if not errors or y["latest"] is not None:
        save_history(BASE / "data" / "history.json", {
            "last": {"date": data_date, "close": sq.get("close"), "ytd": sq.get("ytd"),
                     "yield_10y": y["latest"], "pev": pev,
                     "main_net_flow_5d_yi": (sum(x[1] for x in flow[-5:]) / 1e8) if flow else None},
            "updated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        })

    print(f"[OK] 报告：{out_html}")
    print(f"[OK] 档位：{level}｜10Y {y['latest']}｜收盘 {sq.get('close')}"
          f"｜P/EV {'%.3f' % pev if pev else '未取到'}")
    print(f"[推送] {pushed[1]}")
    for e in errors:
        print(f"[WARN] {e}", file=sys.stderr)

    # 退出码语义：只有「关键数据缺失」或「配了 webhook 却推送失败」才算任务失败。
    # 资金流等次要指标在部分网络环境（如 Actions 海外 IP）取不到属正常降级，
    # 若因此返回非 0，会让 CI 后续步骤（Pages 发布）被整体跳过。
    critical_missing = (y.get("latest") is None) or (sq.get("close") is None)
    push_failed = (not args.dry_run) and os.environ.get("WECOM_WEBHOOK_URL") and not pushed[0]
    if critical_missing:
        print("[ERROR] 关键数据缺失（10Y 国债或行情），任务失败", file=sys.stderr)
        return 1
    if push_failed:
        print("[ERROR] 已配置 webhook 但推送失败，任务失败", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
