# -*- coding: utf-8 -*-
"""
기업 페이지 실적 자동 갱신 (company_financials.json)

업데이트 조건
- 회사별로 저장된 마지막 분기(또는 반기) 다음 기간을 '대기 기간'으로 본다.
- 대기 기간 종료일 + 14일부터 확인을 시작해, 발표가 확인될 때까지 12시간마다(종료 후 75일이 지나면 72시간마다) 조회한다.
- 조회: Gemini + Google 검색 그라운딩으로 회사 공식 실적 자료에서 매출·이익 지표(기존과 같은 정의)와
  전년 동기 비교치, 통신사 KPI(CAPEX·ARPU·해지율·가입자)를 추출한다. 미국 SEC 제출사는 XBRL 공시값으로 매출·영업이익을 덮어쓴다.
- 검증: ① 출처가 회사 공식 도메인/규제기관 ② 발표 자료의 전년 동기 매출이 저장된 전년 동기 값과 ±3% 이내
  (저장값이 없으면 직전 분기 대비 ±50% 이내) ③ |이익| ≤ 매출. 통과하면 자동 반영, 실패하면 '검증 대기'로 기록만 한다.
- 4분기(또는 하반기) 반영 시 4개 분기 합으로 연간 행을 추가한다.
상태는 earnings_state.json, 화면 표시는 각 회사의 auto_status 필드.
"""
import json, os, re, time, calendar, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
FIN_FILE, STATE_FILE = "company_financials.json", "earnings_state.json"
MAX_CALLS_PER_RUN = 6
START_AFTER_DAYS, FAST_UNTIL_DAYS = 14, 75
RECHECK_FAST_H, RECHECK_SLOW_H = 12, 72
KEEP_QUARTERS, KEEP_ANNUAL = 8, 3

TELCO_KPI = {
    "capex": "quarterly capital expenditures for this quarter only (3 months)",
    "arpu": "ARPU for this quarter",
    "churn": "monthly churn rate (%) for this quarter",
    "subs": "subscribers at end of quarter (millions)",
}
# 회사별 설정: 이름, 단위, 지표 정의, 공식 출처 도메인, KPI 정의(단위 포함), SEC CIK
CFG = {
    "에릭슨": dict(name="Ericsson (Telefonaktiebolaget LM Ericsson)", unit="SEK million",
                rev="Net sales", op="Adjusted EBIT (EBIT excluding restructuring charges)",
                domains=["ericsson.com", "cision.com", "sec.gov"]),
    "노키아": dict(name="Nokia Corporation", unit="EUR million", rev="Net sales (reported)",
                op="Comparable operating profit", domains=["nokia.com", "sec.gov", "globenewswire.com"]),
    "zte": dict(name="ZTE Corporation (中兴通讯)", unit="CNY million",
                rev="Operating revenue (营业收入) for the 3-month quarter only (if only year-to-date is reported, subtract the prior cumulative)",
                op="Net profit attributable to holders of ordinary shares of the listed company (归属于上市公司股东的净利润) for the 3-month quarter only",
                domains=["zte.com.cn", "hkexnews.hk", "cninfo.com.cn", "szse.cn"]),
    "화웨이": dict(name="Huawei Investment & Holding Co., Ltd.", unit="CNY million", rev="Revenue", op="Net profit",
                domains=["huawei.com"]),
    "ast": dict(name="AST SpaceMobile, Inc. (Nasdaq: ASTS)", unit="USD million", rev="Total revenues",
                op="Operating loss / income", domains=["ast-science.com", "sec.gov", "businesswire.com"], cik="1780312"),
    "스타링크": dict(name="SpaceX (Space Exploration Technologies Corp., Nasdaq: SPCX)", unit="USD million",
                rev="Total revenue", op="Operating income (loss)", domains=["spacex.com", "sec.gov", "q4cdn.com"], cik="1181412",
                kpi={"capex": "Total capital expenditures for the quarter, USD million",
                     "subs": "Starlink (consumer) subscribers at quarter end, millions"}),
    "버라이즌": dict(name="Verizon Communications Inc.", unit="USD million", rev="Total operating revenues",
                op="Operating income", domains=["verizon.com", "sec.gov"], cik="732712",
                kpi={"capex": "Capital expenditures (including capitalized software) for the 3-month quarter, USD million",
                     "arpu": "Total wireless retail postpaid ARPA (USD per account per month)",
                     "churn": "Consumer wireless retail postpaid phone churn (%, monthly)"}),
    "kddi": dict(name="KDDI Corporation", unit="JPY million", rev="Operating revenue (売上高, IFRS)",
                op="Operating income (営業利益, IFRS)", domains=["kddi.com"],
                kpi={"capex": "設備投資額（支払）cash-basis capex for the 3-month quarter, in 億円 (100 million yen)",
                     "arpu": "モバイルARPU (yen per month)", "churn": "主要回線解約率 (%, monthly)"}),
    "도코모": dict(name="NTT DOCOMO, INC. (docomo group)", unit="JPY million", rev="Operating revenues",
                op="Operating profit", domains=["docomo.ne.jp", "group.ntt", "ntt.co.jp"],
                kpi={"capex": "設備投資 (capex) for the 3-month quarter, in 億円 (100 million yen)",
                     "arpu": "モバイル通信ARPU (yen per month)", "churn": "ハンドセット解約率 (%, monthly)"}),
    "지오": dict(name="Jio Platforms Limited (Reliance Jio)", unit="INR million",
                rev="Revenue from operations (Jio Platforms consolidated)", op="EBITDA (Jio Platforms)",
                domains=["ril.com", "jio.com", "jioplatforms.com", "bseindia.com", "nseindia.com"],
                kpi={"arpu": "ARPU (₹ per subscriber per month)", "churn": "Monthly churn (%)"}),
    "skt": dict(name="SK Telecom (SKT), consolidated", unit="KRW billion", rev="연결 영업수익 (Operating revenue)",
                op="연결 영업이익 (Operating income)", domains=["sktelecom.com", "dart.fss.or.kr"],
                kpi={"capex": "CAPEX (SKT+SKB 합산) for the 3-month quarter, in 억 원 (100 million KRW)",
                     "arpu": "이동통신 ARPU (원/월, SKT 별도)", "churn": "월평균 해지율 (%)"}),
    "kt": dict(name="KT Corporation, consolidated", unit="KRW billion", rev="연결 영업수익 (Operating revenue)",
                op="연결 영업이익 (Operating income)", domains=["kt.com", "dart.fss.or.kr", "sec.gov"],
                kpi={"capex": "KT 별도 CAPEX for the 3-month quarter only (if cumulative, subtract), in 억 원",
                     "arpu": "무선 ARPU (원/월)", "churn": "무선 해지율 (%, 월평균)"}),
    "lg유플러스": dict(name="LG Uplus Corp., consolidated", unit="KRW billion", rev="연결 영업수익 (Operating revenue)",
                op="연결 영업이익 (Operating income)", domains=["lguplus.com", "lg.co.kr", "dart.fss.or.kr"],
                kpi={"capex": "CAPEX for the 3-month quarter, in 억 원",
                     "arpu": "MNO 서비스 ARPU (IoT 제외, 원/월)", "churn": "MNO 해지율 (%)"}),
}
# Yahoo 재무 시계열(공시 집계): (티커, 단위 나눗수, 이익 지표 — None이면 매출만 대조하고 이익은 공식 자료 추출값 사용)
YAHOO = {"버라이즌": ("VZ", 1e6, "OperatingIncome"), "ast": ("ASTS", 1e6, "OperatingIncome"), "스타링크": ("SPCX", 1e6, "OperatingIncome"), "에릭슨": ("ERIC", 1e6, None), "노키아": ("NOK", 1e6, None), "zte": ("0763.HK", 1e6, "NetIncomeCommonStockholders"), "kddi": ("9433.T", 1e6, "OperatingIncome"), "skt": ("017670.KS", 1e9, "OperatingIncome"), "kt": ("030200.KS", 1e9, "OperatingIncome"), "lg유플러스": ("032640.KS", 1e9, "OperatingIncome")}
DIAG = []
KPI_KEYS = {"capex": "capex_quarterly", "arpu": "arpu_quarterly", "churn": "churn_quarterly", "subs": "subs_quarterly"}

# ── 라벨 해석·다음 기간 라벨 생성 (회사마다 표기가 달라 기존 표기를 그대로 이어감) ──
def _last_day(y, m): return calendar.monthrange(y, m)[1]

def parse_label(lb):
    """라벨 → (기간 종료 연, 종료 월, 다음 라벨 생성 함수, 반기 여부)"""
    s = str(lb).strip()
    m = re.match(r"^(\d{4}) Q([1-4])$", s)
    if m:
        y, q = int(m[1]), int(m[2])
        return y, q * 3, lambda: f"{y + (q == 4)} Q{q % 4 + 1}", False
    m = re.match(r"^(\d{4})Q([1-4])$", s)
    if m:
        y, q = int(m[1]), int(m[2])
        return y, q * 3, lambda: f"{y + (q == 4)}Q{q % 4 + 1}", False
    m = re.match(r"^Q([1-4]) FY(\d{4})$", s)
    if m:
        q, y = int(m[1]), int(m[2])
        return y, q * 3, lambda: f"Q{q % 4 + 1} FY{y + (q == 4)}", False
    m = re.match(r"^([1-4])Q(\d{2}) ?\((\d{4})\.(\d{1,2})-(\d{1,2})\)$", s)
    if m:
        q, Y, b = int(m[1]), int(m[3]), int(m[5])
        sp = " " if " (" in s else ""
        def nxt():
            ny, sm = (Y + 1, 1) if b == 12 else (Y, b + 1)
            return f"{q % 4 + 1}Q{str(ny)[2:]}{sp}({ny}.{sm}-{sm + 2})"
        return Y, b, nxt, False
    m = re.match(r"^FY(\d{2}) ([1-4])Q( ?)\((\d{4})\.(\d{1,2})-(\d{1,2})\)$", s)
    if m:
        fy, q, sp, Y, b = int(m[1]), int(m[2]), m[3], int(m[4]), int(m[6])
        def nxt():
            ny, sm = (Y + 1, 1) if b == 12 else (Y, b + 1)
            nfy = fy + 1 if q == 4 else fy
            return f"FY{nfy:02d} {q % 4 + 1}Q{sp}({ny}.{sm}-{sm + 2})"
        return Y, b, nxt, False
    m = re.match(r"^(\d{4}) H([12])$", s)
    if m:
        y, h = int(m[1]), int(m[2])
        return y, h * 6, lambda: f"{y + (h == 2)} H{h % 2 + 1}", True
    return None

def next_period(f):
    q = f.get("quarterly") or []
    if not q: return None
    p = parse_label(q[-1]["label"])
    if not p: return None
    lb = p[2]()
    np_ = parse_label(lb)
    return {"label": lb, "end_y": np_[0], "end_m": np_[1], "half": np_[3]}

def find_label(lst, end_y, end_m):
    for i, r in enumerate(lst or []):
        p = parse_label(r.get("label", ""))
        if p and p[0] == end_y and p[1] == end_m: return i
    return None

# ── Gemini (Google 검색 그라운딩) ──
def _gemini(prompt, key, models):
    payload = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}],
               "generationConfig": {"temperature": 0}}
    errs = []
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        req = urllib.request.Request(url, method="POST", data=json.dumps(payload).encode("utf-8"))
        req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                res = json.loads(r.read().decode("utf-8"))
            cand = res["candidates"][0]
            text = "".join(p.get("text", "") for p in cand["content"]["parts"])
            chunks = [(c.get("web") or {}) for c in (cand.get("groundingMetadata") or {}).get("groundingChunks", [])]
            return text, chunks, model
        except urllib.error.HTTPError as ex:
            try: body = ex.read().decode("utf-8", "ignore")
            except Exception: body = ""
            msg = re.search(r'"message":\s*"([^"]{0,120})', body)
            errs.append(f"{model}: HTTP {ex.code} {msg.group(1) if msg else ''}".strip())
        except Exception as ex:
            errs.append(f"{model}: {str(ex)[:80]}")
    raise RuntimeError(" | ".join(errs)[:600] or "Gemini 호출 실패")

def _parse_json(text):
    m = re.search(r"\{[\s\S]*\}", text or "")
    if not m: return None
    try: return json.loads(m.group())
    except Exception:
        try: return json.loads(re.sub(r",\s*}", "}", m.group()))
        except Exception: return None

def _num(v):
    if v is None or v == "": return None
    try: return float(str(v).replace(",", ""))
    except Exception: return None

def _host(u):
    m = re.match(r"https?://([^/]+)", str(u or ""))
    return (m.group(1).lower() if m else "")

def _domain_ok(cfg, url, chunks):
    doms = cfg["domains"]
    hosts = [_host(url)] + [str(c.get("title", "")).lower() for c in chunks] + [_host(c.get("uri", "")) for c in chunks]
    return any(any(h == d or h.endswith("." + d) or h.endswith(d) for d in doms) for h in hosts if h)

def build_prompt(key, cfg, per):
    period = f"the half-year ending {per['end_y']}-{per['end_m']:02d}" if per["half"] else \
             f"the fiscal quarter ending {per['end_y']}-{per['end_m']:02d} (3 months, {per['label']})"
    h2 = ("\nThis is the second half (H2). The company reports full-year figures: return fy_revenue and fy_op for the full year "
          "instead of revenue/op if a separate H2 figure is not published.") if per["half"] and per["label"].endswith("H2") else ""
    kpi = cfg.get("kpi") or {}
    kpi_lines = "".join(f'\n- "{k}": {d}' for k, d in kpi.items())
    kpi_keys = "".join(f', "{k}": n' for k in kpi)
    return f"""You extract financial results from OFFICIAL company sources. Use Google Search to find the official earnings release / IR presentation / regulatory filing of {cfg['name']} for {period}.
Rules:
- Use only the company's own release or IR materials or a regulatory filing (preferred domains: {', '.join(cfg['domains'])}). Do NOT use news articles or analyst estimates.
- If results for this exact period are NOT yet published, return {{"found": false}}.
- Figures in {cfg['unit']} as plain numbers (no commas, no units). Use negative numbers for losses.
- "revenue": {cfg['rev']}
- "op": {cfg['op']}
- "yoy_revenue", "yoy_op": the same two metrics for the same period one year earlier, as shown in the release (comparatives).{kpi_lines}{h2}
Return ONLY one JSON object, no prose:
{{"found": true, "period_in_release": "...", "report_date": "YYYY-MM-DD", "revenue": n, "op": n, "yoy_revenue": n, "yoy_op": n{kpi_keys}, "fy_revenue": null, "fy_op": null, "source_url": "https://...", "source_title": "..."}}"""

# ── SEC XBRL (미국 제출사: 매출·영업이익 공식값) ──
def sec_quarter(cik, end_y, end_m):
    ua = "MXNewsDashboard noreply@users.noreply.github.com"
    out = {}
    for field, tags in (("revenue", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"]),
                        ("op", ["OperatingIncomeLoss"])):
        for tag in tags:
            try:
                req = urllib.request.Request(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{int(cik):010d}/us-gaap/{tag}.json",
                                             headers={"User-Agent": ua, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = json.loads(r.read().decode("utf-8"))
            except Exception as ex:
                DIAG.append(f"SEC {cik} {tag}: {str(ex)[:80]}")
                continue
            end = f"{end_y}-{end_m:02d}-{_last_day(end_y, end_m):02d}"
            for u in data.get("units", {}).get("USD", []):
                if u.get("end") != end or not u.get("start"): continue
                d = (datetime.strptime(u["end"], "%Y-%m-%d") - datetime.strptime(u["start"], "%Y-%m-%d")).days
                if 80 <= d <= 100:
                    out[field] = u["val"] / 1e6
                    out["accn"] = u.get("accn", "")
                    break
            if field in out: break
    DIAG.append(f"SEC {cik} {end_y}-{end_m:02d}: 결과 {sorted(k for k in out if k in ('revenue','op'))}")
    if "revenue" in out and "op" in out:
        accn = out.get("accn", "").replace("-", "")
        out["url"] = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn}/" if accn else "https://www.sec.gov/"
        return out
    return None

# ── 반영 ──
def _round(v, ref):
    dec = 1 if any(isinstance(r, float) and r != int(r) for r in ref) else 0
    return round(v, dec) if dec else round(v)

def apply_result(f, per, res, src_url, today):
    q = f["quarterly"]
    ref = [r.get("revenue") for r in q[-4:] if r.get("revenue") is not None]
    row = {"label": per["label"], "revenue": _round(res["revenue"], ref), "op": _round(res["op"], ref),
           "src": src_url, "auto": today}
    q.append(row)
    del q[:-KEEP_QUARTERS]
    # 연간 행: 4분기(또는 하반기) 반영 시 직전 4개 분기 합으로 추가
    p = parse_label(per["label"])
    is_fy_end = False
    if per["half"]: is_fy_end = per["label"].endswith("H2")
    else:
        m = re.search(r"([1-4])Q|Q([1-4])", per["label"])
        qn = int(m.group(1) or m.group(2)) if m else 0
        is_fy_end = qn == 4
    if is_fy_end and f.get("annual"):
        n = 2 if per["half"] else 4
        last_n = q[-n:]
        if len(last_n) == n and all(r.get("revenue") is not None and r.get("op") is not None for r in last_n):
            prev = f["annual"][-1]["label"]
            def bump(mo):
                s = mo.group(0); return str(int(s) + 1).zfill(len(s))
            new_lb = re.sub(r"\d+", bump, prev)
            if not any(a["label"] == new_lb for a in f["annual"]):
                f["annual"].append({"label": new_lb, "revenue": _round(sum(r["revenue"] for r in last_n), ref),
                                    "op": _round(sum(r["op"] for r in last_n), ref), "src": src_url, "auto": today})
                del f["annual"][:-KEEP_ANNUAL]
    return apply_kpi(f, per, res)

def apply_kpi(f, per, res):
    kpi = f.get("kpi") or {}
    applied = []
    for k, lst_key in KPI_KEYS.items():
        v = _num(res.get(k))
        lst = kpi.get(lst_key)
        if v is None or not lst: continue
        vals = [r.get("v") for r in lst if r.get("v") is not None]
        if vals:
            last = vals[-1]
            lim = {"capex": 1.5, "arpu": 0.15, "churn": 1.0, "subs": 0.5}[k]
            if last and abs(v - last) / abs(last) > lim: continue           # 비정상 변동은 반영 보류
        if k == "churn" and not (0.05 <= v <= 6): continue
        i = find_label(lst, per["end_y"], per["end_m"])
        if i is not None:
            if lst[i].get("v") is None: lst[i]["v"] = v; applied.append(k)
            continue
        p_last = parse_label(lst[-1].get("label", ""))
        if p_last:
            nl = p_last[2](); pn = parse_label(nl)
            if pn and pn[0] == per["end_y"] and pn[1] == per["end_m"]:
                lst.append({"label": nl, "v": v}); applied.append(k)
    return applied

def validate(f, per, res, cfg, chunks, sec):
    rev, op = _num(res.get("revenue")), _num(res.get("op"))
    if sec: rev, op = sec["revenue"], sec["op"]
    if per["half"] and per["label"].endswith("H2") and (rev is None or op is None):
        fyr, fyo = _num(res.get("fy_revenue")), _num(res.get("fy_op"))
        h1 = (f.get("quarterly") or [])[-1]
        if fyr is not None and fyo is not None and h1.get("revenue") is not None:
            rev, op = fyr - h1["revenue"], fyo - (h1.get("op") or 0)
    if rev is None or op is None: return None, "매출·이익 수치 추출 실패"
    if abs(op) > abs(rev): return None, "이익이 매출보다 큼(단위 오류 의심)"
    if not sec and not _domain_ok(cfg, res.get("source_url"), chunks): return None, "공식 출처 확인 실패"
    q = f.get("quarterly") or []
    n_back = 2 if per["half"] else 4
    ya = None
    yy, ym = per["end_y"] - 1, per["end_m"]
    i = find_label(q, yy, ym)
    if i is not None: ya = q[i].get("revenue")
    yoy = _num(res.get("yoy_revenue"))
    if ya and not sec:
        if yoy is None: return None, "전년 동기 비교치 미추출"
        if abs(yoy - ya) / abs(ya) > 0.03: return None, f"전년 동기 매출 불일치(발표 {yoy:,.1f} vs 저장 {ya:,.1f})"
    elif not sec:
        lastrev = q[-1].get("revenue") if q else None
        if lastrev and abs(rev - lastrev) / abs(lastrev) > 0.5: return None, "직전 분기 대비 매출 변동 과대(단위 오류 의심)"
    return {"revenue": rev, "op": op, **{k: res.get(k) for k in KPI_KEYS}}, ""

def yahoo_quarter(sym, div, op_type, end_y, end_m):
    """Yahoo fundamentals-timeseries에서 해당 분기와 전년 동기 매출·이익 조회 (원 단위 → 저장 단위)"""
    types = ["quarterlyTotalRevenue"] + ([f"quarterly{op_type}"] if op_type else [])
    p1 = int(datetime(end_y - 2, 1, 1).timestamp()); p2 = int(time.time())
    url = (f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}"
           f"?symbol={sym}&type={','.join(types)}&period1={p1}&period2={p2}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as ex:
        DIAG.append(f"Yahoo {sym}: {str(ex)[:80]}")
        return None
    series = {}
    for res in (data.get("timeseries") or {}).get("result") or []:
        t = (res.get("meta") or {}).get("type", [""])[0]
        for row in res.get(t) or []:
            if row and row.get("periodType") == "3M" and row.get("reportedValue"):
                series.setdefault(t, {})[row["asOfDate"]] = row["reportedValue"]["raw"] / float(div)
    end = f"{end_y}-{end_m:02d}-{_last_day(end_y, end_m):02d}"
    yend = f"{end_y - 1}-{end_m:02d}-{_last_day(end_y - 1, end_m):02d}"
    rev = series.get("quarterlyTotalRevenue", {})
    DIAG.append(f"Yahoo {sym}: 분기 {sorted(rev)[-5:]}")
    if end not in rev: return None
    out = {"revenue": rev[end], "yoy_revenue": rev.get(yend)}
    if op_type:
        ops = series.get(f"quarterly{op_type}", {})
        out["op"] = ops.get(end); out["yoy_op"] = ops.get(yend)
    out["url"] = f"https://finance.yahoo.com/quote/{sym}/financials/"
    return out

def check_against_stored(f, per, yoy_rev, yoy_op):
    """전년 동기 값이 저장값과 맞는지(정의·단위 일치 확인). 반환: (매출 일치, 이익 일치 또는 None)"""
    q = f.get("quarterly") or []
    i = find_label(q, per["end_y"] - 1, per["end_m"])
    if i is None: return None, None
    sr, so = q[i].get("revenue"), q[i].get("op")
    rev_ok = (yoy_rev is not None and sr) and abs(yoy_rev - sr) / abs(sr) <= 0.03
    op_ok = None
    if yoy_op is not None and so is not None and sr:
        op_ok = abs(yoy_op - so) <= max(abs(so) * 0.05, abs(sr) * 0.005)
    return bool(rev_ok), op_ok

def run(log=print):
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not os.path.exists(FIN_FILE): return 0
    fin = json.loads(open(FIN_FILE, encoding="utf-8").read())
    state = {}
    if os.path.exists(STATE_FILE):
        try: state = json.loads(open(STATE_FILE, encoding="utf-8").read())
        except Exception: state = {}
    now = datetime.now(KST); today = now.strftime("%Y-%m-%d")
    models = []
    if os.path.exists("gemini_model_nw.txt"):
        m = open("gemini_model_nw.txt", encoding="utf-8").read().strip()
        if m: models.append(m)
    avail = []
    if os.path.exists("gemini_diag.txt"):
        first = open("gemini_diag.txt", encoding="utf-8").read().split("\n")[0]
        if first.startswith("avail:"): avail = [x.strip() for x in first[6:].split(",") if x.strip()]
    full = sorted([x for x in avail if "lite" not in x], reverse=True)    # 신형 상위 모델 우선
    lite = sorted([x for x in avail if "lite" in x], reverse=True)
    models += full + lite + ["gemini-flash-latest", "gemini-flash-lite-latest"]
    models = list(dict.fromkeys(models))
    cool = {}
    if os.path.exists("gemini_cooldown_nw.json"):
        try: cool = json.loads(open("gemini_cooldown_nw.json", encoding="utf-8").read())
        except Exception: cool = {}
    ts = time.time()
    models = [x for x in models if cool.get(x, 0) <= ts] + [x for x in models if cool.get(x, 0) > ts]
    models = models[:6]
    calls, changed, g_blocked = 0, False, False
    for comp, cfg in CFG.items():
        f = fin["companies"].get(comp)
        if not f or f.get("private") or not f.get("quarterly"): continue
        kr = (state.get(comp) or {}).get("kpi_retry")
        if kr and cfg.get("kpi") and not g_blocked and calls < MAX_CALLS_PER_RUN:
            calls += 1
            try:
                text, chunks, gm = _gemini(build_prompt(comp, cfg, kr), key, models)
                res = _parse_json(text) or {}
                if res.get("found") and _domain_ok(cfg, res.get("source_url"), chunks):
                    got = apply_kpi(f, kr, res)
                    log(f"[실적] {comp} {kr['label']} KPI 보완: {got}")
                    state[comp].pop("kpi_retry", None); changed = changed or bool(got)
            except Exception as ex:
                g_blocked = True; log(f"[실적] Gemini 조회 불가 - {str(ex)[:300]}")
        per = next_period(f)
        if not per: continue
        end = datetime(per["end_y"], per["end_m"], _last_day(per["end_y"], per["end_m"]), tzinfo=KST)
        start_after = 45 if per["half"] else START_AFTER_DAYS
        st = state.get(comp, {})
        if st.get("period") != per["label"]: st = {"period": per["label"]}
        due_from = end + timedelta(days=start_after)
        status = {"period": per["label"], "state": "waiting", "due_from": due_from.strftime("%Y-%m-%d"),
                  "checked": st.get("checked", ""), "msg": st.get("msg", "")}
        if now < due_from:
            f["auto_status"] = status; continue
        age_h = 1e9
        if st.get("checked"):
            try: age_h = (now - datetime.strptime(st["checked"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)).total_seconds() / 3600
            except Exception: pass
        interval = RECHECK_FAST_H if now < end + timedelta(days=FAST_UNTIL_DAYS) else RECHECK_SLOW_H
        if st.get("state") == "error": interval = 1
        if age_h < interval:
            status["state"] = st.get("state", "checking"); f["auto_status"] = status; continue
        st["checked"] = now.strftime("%Y-%m-%d %H:%M")
        vals, src, why, used, kp = None, "", "", "", {}
        # ① 미국 SEC 제출사: XBRL 공시값(공식) 그대로 사용
        if cfg.get("cik") and not per["half"]:
            sec = sec_quarter(cfg["cik"], per["end_y"], per["end_m"])
            if sec: vals, src, used = {"revenue": sec["revenue"], "op": sec["op"]}, sec["url"], "SEC XBRL"
        # ② Yahoo 공시 집계: 전년 동기 매출(·이익)이 저장값과 맞을 때만 채택(지표 정의·단위 일치 확인)
        yop = None
        if not vals and comp in YAHOO and not per["half"]:
            sym, div, op_type = YAHOO[comp]
            y = yahoo_quarter(sym, div, op_type, per["end_y"], per["end_m"])
            if y:
                rev_ok, op_ok = check_against_stored(f, per, y.get("yoy_revenue"), y.get("yoy_op"))
                DIAG.append(f"Yahoo {comp}: rev {y.get('revenue')} op {y.get('op')} yoy_rev {y.get('yoy_revenue')} yoy_op {y.get('yoy_op')} → 매출일치 {rev_ok} 이익일치 {op_ok}")
                if rev_ok and op_type and op_ok and y.get("op") is not None:
                    vals, src, used = {"revenue": y["revenue"], "op": y["op"]}, y["url"], "Yahoo Finance(공시 집계)"
                elif rev_ok:
                    yop = y   # 매출은 확인됨 — 이익(회사 고유 지표)은 공식 자료 추출로 보완
                    why = "이익 지표가 회사 고유 정의라 공식 자료 확인 필요"
                else:
                    why = "공시 집계 값이 저장된 전년 동기와 불일치"
        # ③ Gemini + Google 검색(공식 자료): 매출·이익 미확보분과 통신 KPI
        need_g = (vals is None) or bool(cfg.get("kpi"))
        if need_g and not g_blocked and calls < MAX_CALLS_PER_RUN:
            calls += 1
            try:
                text, chunks, gm = _gemini(build_prompt(comp, cfg, per), key, models)
                res = _parse_json(text) or {}
                if res.get("found"):
                    kp = {k: res.get(k) for k in KPI_KEYS}
                    if vals is None:
                        gv, gwhy = validate(f, per, res, cfg, chunks, None)
                        if gv and yop and abs(gv["revenue"] - yop["revenue"]) / abs(yop["revenue"]) > 0.02:
                            gv, gwhy = None, "공식 자료 추출 매출이 공시 집계와 불일치"
                        if gv: vals, src, used = {"revenue": gv["revenue"], "op": gv["op"]}, res.get("source_url") or "", gm
                        else: why = gwhy
                elif vals is None and not yop:
                    why = "아직 발표 자료 미확인"
            except Exception as ex:
                g_blocked = True       # 할당량·권한 문제면 이번 회차의 나머지 Gemini 조회 생략
                gerr = str(ex)[:300]; DIAG.append(f"Gemini: {str(ex)[:600]}")
                log(f"[실적] Gemini 조회 불가 - {gerr}")
                if vals is None: why = why or "공식 자료 조회 불가(Gemini 검색 할당량)"
        if vals is None and need_g and (g_blocked or calls >= MAX_CALLS_PER_RUN) and not why:
            # 공식 자료 조회를 이번 회차에 못 한 경우: 1시간 뒤 재시도
            st.update(state="error", msg="공식 자료 조회 대기(Gemini 검색 할당량·호출 상한)"); state[comp] = st
            status.update(state="checking", checked=st["checked"], msg=st["msg"]); f["auto_status"] = status
            log(f"[실적] {comp} {per['label']} 조회 대기"); continue
        if vals is None:
            stt = "not_yet" if why == "아직 발표 자료 미확인" or (not why and not yop) else "pending"
            if not why: why = "아직 발표 자료 미확인"
            st.update(state=("error" if (stt == "pending" and g_blocked) else stt), msg=why); state[comp] = st
            status.update(state=stt, checked=st["checked"], msg=why, src=(yop or {}).get("url", "")); f["auto_status"] = status
            log(f"[실적] {comp} {per['label']} {stt} - {why}"); continue
        vals.update({k: v for k, v in kp.items() if v is not None})
        kpis = apply_result(f, per, vals, src, today)
        changed = True
        kpi_retry = per if (cfg.get("kpi") and not kpis and not any(v is not None for v in kp.values())) else None
        msg = f"{per['label']} 자동 반영 · {used}" + (f" · KPI {', '.join(kpis)}" if kpis else (" · KPI 미반영" if cfg.get("kpi") else ""))
        state[comp] = {"period": next_period(f)["label"] if next_period(f) else "",
                       "last_applied": {"period": per["label"], "checked": st["checked"], "src": src, "msg": msg}}
        if kpi_retry: state[comp]["kpi_retry"] = kpi_retry
        f["auto_status"] = {"period": per["label"], "state": "applied", "checked": st["checked"], "msg": msg, "src": src}
        log(f"[실적] {comp} {per['label']} 반영({used}): 매출 {vals['revenue']:,.1f} / 이익 {vals['op']:,.1f} {cfg['unit']}"
            + (f" / KPI {kpis}" if kpis else ""))
    fin["auto_checked"] = now.strftime("%Y-%m-%d %H:%M")
    open(FIN_FILE, "w", encoding="utf-8").write(json.dumps(fin, ensure_ascii=False, indent=0))
    open(STATE_FILE, "w", encoding="utf-8").write(json.dumps(state, ensure_ascii=False, indent=1))
    if DIAG: open("earnings_diag.txt", "w", encoding="utf-8").write(now.strftime("%Y-%m-%d %H:%M") + "\n" + "\n".join(DIAG))
    log(f"[실적] 자동 갱신 점검 완료: 조회 {calls}건{' · 반영 있음' if changed else ''}")
    return calls

if __name__ == "__main__":
    run()
