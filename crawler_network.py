# -*- coding: utf-8 -*-
"""
Network 뉴스 크롤러 - GitHub Actions판 (MX crawler_actions.py 기반 이식)
매체 RSS(통신 전문지 포함) + Google News + 네이버 수집, 전역 중복제거,
섹션 분류(경쟁사/통신사/위성/정책/Outage), Gemini 배치 판정, 최신 20건.
결과: docs/network.html (데이터 내장 단일 파일, GitHub Pages로 서빙)
필요 환경변수: GEMINI_API_KEY (없으면 키워드 분류로 동작), NAVER_CLIENT_ID/SECRET(선택)
"""
import feedparser, json, re, html, os, sys
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

HOURS, PER, LATEST_N = 48, 5, 20
KST = timezone(timedelta(hours=9))

FEEDS = [
    # ── 통신 전문매체 ──
    ("Light Reading", "https://www.lightreading.com/rss.xml"),
    ("Fierce Network", "https://www.fierce-network.com/rss/xml"),
    ("RCR Wireless", "https://www.rcrwireless.com/feed"),
    ("Mobile World Live", "https://www.mobileworldlive.com/feed/"),
    ("SDxCentral", "https://www.sdxcentral.com/feed/"),
    ("Telecom Paper", "https://www.telecompaper.com/rss/news"),
    # ── 종합·경제지 ──
    ("WSJ Tech", "https://feeds.a.dj.com/rss/RSSWSJD.xml"),
    ("Financial Times Tech", "https://www.ft.com/technology?format=rss"),
    ("Nikkei Asia", "https://asia.nikkei.com/rss/feed/nar"),
    # ── 국내 ──
    ("전자신문", "https://rss.etnews.com/Section901.xml"),
    ("ZDNet Korea", "https://feeds.feedburner.com/zdkorea"),
    ("한국경제 IT", "https://rss.hankyung.com/feed/it.xml"),
    ("매일경제 IT", "https://www.mk.co.kr/rss/50300009/"),
]
GN_QUERIES = [
    # 경쟁사
    "Ericsson OR Nokia telecom",
    "Huawei OR ZTE network equipment",
    "Mavenir OR Rakuten Symphony OR Open RAN",
    "Samsung Networks 5G",
    "삼성전자 네트워크 OR 5G 장비",
    # 통신사 (지역별 묶음)
    "Verizon OR AT&T OR T-Mobile network",
    "EchoStar OR Viaero OR US Cellular OR Charter spectrum",
    "NTT DOCOMO OR KDDI OR SoftBank OR Rakuten Mobile network",
    "Reliance Jio OR Bharti Airtel OR Vodafone Idea",
    "TELUS OR Videotron OR SaskTel network",
    "Vodafone OR Deutsche Telekom OR Orange OR Telefonica network",
    "SKT OR KT OR LG유플러스",
    # 위성
    "Starlink OR AST SpaceMobile direct to cell",
    "Amazon Leo OR Kuiper satellite",
    "저궤도 위성통신 OR 스타링크",
    # 정책
    "FCC OR spectrum auction",
    "net neutrality OR Digital Networks Act OR EU Cybersecurity Act",
    "과기정통부 OR 주파수 경매",
    # Outage 전용
    "Verizon OR AT&T OR T-Mobile outage",
    "Vodafone OR Orange OR Telefonica OR Deutsche Telekom outage",
    "DOCOMO OR KDDI OR SoftBank OR Jio OR Airtel outage",
    "SKT OR KT OR LG유플러스 통신 장애",
]
NAVER_QUERIES = [
    "삼성전자 네트워크", "5G 장비", "6G 기술", "주파수 할당", "통신 장애",
    "과기정통부 통신", "에릭슨 노키아", "화웨이 통신장비", "스타링크", "저궤도 위성",
]

SEC_DEFS = [  # 검사 순서 = 분류 우선순위 (outage 최우선)
    ("outage", "[Alert] Outage", ["outage","통신장애","통신 장애","먹통","서비스 중단","network down","service disruption","대규모 장애","전국 장애"]),
    ("satellite", "[Sat] 위성", ["starlink","스타링크","ast spacemobile","kuiper","amazon leo","저궤도","leo satellite","direct-to-cell","direct to cell","위성통신","위성 통신","non-terrestrial","ntn"]),
    ("competitor", "[Comp] 경쟁사", ["ericsson","에릭슨","nokia","노키아","huawei","화웨이","zte","mavenir","마베니어","rakuten symphony","라쿠텐 심포니","open ran","오픈랜","vran","ai-ran"]),
    ("policy", "[Policy] 정책·규제", ["fcc","과기정통부","spectrum","주파수","net neutrality","망중립성","digital networks act","cybersecurity act","spectrum auction","주파수 경매","통신 정책","통신 규제"]),
    ("carrier", "[Telco] 통신사", ["verizon","at&t","t-mobile","echostar","viaero","us cellular","charter","docomo","도코모","kddi","softbank","소프트뱅크","rakuten mobile","reliance jio","jio","airtel","vodafone","보다폰","telus","videotron","sasktel","deutsche telekom","도이치텔레콤","orange","telefonica","telefónica","skt","sk텔레콤","lg유플러스","lgu+","케이티","이동통신사","통신사"]),
]
ORDER = ["competitor","carrier","satellite","policy","outage"]
VALID_IDS = [s[0] for s in SEC_DEFS]

CATS = [
    ("outage", ["outage","장애","먹통","복구","disruption","restore","서비스 중단"]),
    ("contract", ["수주","계약","공급","선정","contract","deal","supply","vendor","공급사"]),
    ("tech", ["6g","open ran","vran","ai-ran","trial","시연","실증","상용화","표준","standard","mou"]),
    ("earnings", ["실적","earnings","revenue","guidance","분기","매출","순이익"]),
    ("exec", ["사장","부사장","ceo","임원","인사","교체","appoint","resign","executive","조직개편"]),
    ("policy", ["정책","규제","제재","tariff","regulation","ban","spectrum","주파수","auction","경매","fcc"]),
]
CRIT = ["outage","통신장애","nationwide","전국","대규모","수주","contract win","제재","ban","breach","hack"]
HIGH = ["삼성","samsung","ericsson","nokia","huawei","5g","6g","주파수","spectrum","starlink","open ran"]

def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()

def norm_key(title):
    return re.sub(r"[^a-z0-9가-힣]", "", title.lower())[:40]

def section_id(text):
    # '장애인' 복지·요금제 기사가 outage로 오분류되지 않도록 단어 자체를 제거 후 검사
    t = text.lower().replace("장애인", "")
    for sid, _, kws in SEC_DEFS:
        if any(k.lower() in t for k in kws):
            return sid
    return None

def category(text):
    t = text.lower()
    for c, kws in CATS:
        if any(k.lower() in t for k in kws):
            return c
    return "other"

def score(text):
    # 키워드 폴백은 최대 4점 — 5점은 Gemini가 사업 영향을 확인한 경우에만 부여
    t = text.lower(); s = 2
    if any(k.lower() in t for k in CRIT): s += 1
    if any(k.lower() in t for k in HIGH): s += 1
    return min(4, s)

def load_lines(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return [l.strip().lower() for l in f if l.strip()]
    return []

def crawl():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    exclude = load_lines("exclude_network.txt")
    use_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    pool, seen = [], set()

    def add(title, summary, src, pub, link, wl):
        if pub < cutoff: return
        title = clean(title)
        if not title: return
        key = norm_key(title)
        if key in seen: return
        summary = clean(summary)
        if summary and len(title) >= 15 and title[:15] in summary:
            summary = ""
        blob = (title + " " + summary)
        bl = blob.lower()
        if any(x in bl for x in exclude): return
        sid = section_id(blob)
        if not sid:
            if use_gemini: sid = "unknown"
            else: return
        seen.add(key)
        pool.append({
            "sid": sid, "title": title[:90],
            "summary": (summary[:220] + "…") if len(summary) > 220 else summary,
            "source": src or "Google News",
            "date": pub.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
            "url": link, "category": category(blob), "importance": score(blob), "wl": wl, "topic": "",
        })

    for src, url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                try: pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    try: pub = datetime(*e.updated_parsed[:6], tzinfo=timezone.utc)
                    except Exception: continue
                summ = getattr(e, "summary", "") or getattr(e, "description", "")
                add(e.get("title",""), summ, src, pub, e.get("link",""), True)
            print(f"{src} 수집 완료")
        except Exception as ex:
            print(f"경고: {src} 실패 - {ex}")

    for q in GN_QUERIES:
        ko = bool(re.search(r"[가-힣]", q))
        hl, gl, ceid = ("ko","KR","KR:ko") if ko else ("en-US","US","US:en")
        url = f"https://news.google.com/rss/search?q={quote(q+' when:2d')}&hl={hl}&gl={gl}&ceid={ceid}"
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                try: pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                except Exception: continue
                src = ""
                if hasattr(e, "source"): src = clean(e.source.get("title",""))
                add(e.get("title",""), e.get("description",""), src, pub, e.get("link",""), False)
        except Exception as ex:
            print(f"경고: Google News 실패({q}) - {ex}")
    # ── 네이버 뉴스 검색 (NAVER API HUB, 키 등록 시에만 동작) ──
    nv_id = os.environ.get("NAVER_CLIENT_ID"); nv_secret = os.environ.get("NAVER_CLIENT_SECRET")
    if nv_id and nv_secret:
        from email.utils import parsedate_to_datetime
        from urllib.parse import urlparse
        for q in NAVER_QUERIES:
            try:
                req = urllib.request.Request(
                    f"https://naverapihub.apigw.ntruss.com/search/v1/news?query={quote(q)}&display=30&sort=date&format=json")
                req.add_header("X-NCP-APIGW-API-KEY-ID", nv_id)
                req.add_header("X-NCP-APIGW-API-KEY", nv_secret)
                with urllib.request.urlopen(req, timeout=25) as r:
                    res = json.loads(r.read().decode("utf-8"))
                for it in res.get("items", []):
                    try: pub = parsedate_to_datetime(it["pubDate"]).astimezone(timezone.utc)
                    except Exception: continue
                    link = it.get("originallink") or it.get("link","")
                    src = urlparse(link).netloc.replace("www.","") if link else "네이버뉴스"
                    add(it.get("title",""), it.get("description",""), src, pub, link, False)
            except Exception as ex:
                print(f"경고: 네이버 API 실패({q}) - {ex}")
        print("네이버 뉴스 수집 완료")
    else:
        print("안내: NAVER_CLIENT_ID/SECRET 미등록 - 네이버 수집 생략")
    print(f"수집 완료 / 후보 풀: {len(pool)}건")

    engine = "키워드 분류"
    if use_gemini and pool:
        engine = gemini_judge(pool) or engine
    pool[:] = [a for a in pool if a["sid"] not in ("unknown","drop")]
    return pool, engine

def gemini_call(url, payload=None, timeout=120):
    req = urllib.request.Request(url, method="POST" if payload else "GET")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    data = json.dumps(payload).encode("utf-8") if payload else None
    with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def gemini_judge(pool):
    key = os.environ["GEMINI_API_KEY"]
    # 후보 전체를 150건씩 묶어 전부 판정 (최대 600건)
    chunks = [pool[i:i+150] for i in range(0, min(len(pool), 600), 150)]

    def build_payload(batch):
        lines = [f"{i} | {b['title']} | {b['summary'][:220]} | {b['source']}" for i, b in enumerate(batch)]
        prompt = f"""당신은 삼성전자 네트워크사업부 경쟁정보(CI) 분석가입니다. 아래 기사 목록(번호|제목|요약|매체)을 각각 판정하세요.

섹션(sec):
competitor = 통신장비 경쟁사(Ericsson, Nokia, Huawei, ZTE, Mavenir, Rakuten Symphony)의 수주·기술·실적·전략
carrier = 통신사 동향. 대상: 한국 SKT·KT·LG유플러스 / 미국 Verizon·AT&T·T-Mobile·EchoStar·Viaero·US Cellular·Charter / 일본 NTT DOCOMO·KDDI·SoftBank·Rakuten Mobile / 인도 Reliance Jio·Bharti Airtel·Vodafone Idea / 캐나다 TELUS·Videotron·SaskTel / 유럽 Vodafone·Deutsche Telekom·Orange·Telefónica. 단, 네트워크 투자·장비 조달·주파수·실적·경영 전략 관련만 해당
satellite = 위성통신(Starlink, AST SpaceMobile, Amazon Leo/Kuiper, Direct-to-Cell, 저궤도 위성)
policy = 통신 정책·규제(FCC, 과기정통부, Digital Networks Act, EU Cybersecurity Act, 망중립성, 주파수 경매 등)
outage = 위 통신사의 통신망 장애 발생·확산·복구 보도
none = 삼성전자 네트워크사업과 무관 → 제외. 특히 스마트폰 단말·요금제 프로모션·소비자 마케팅·연예 기사는 none

판정 규칙: 실제로 발생한 통신망 장애·복구 보도만 sec=outage에 해당하며, 이 경우 통신사 이름이 있어도 반드시 outage로 분류. 다음은 outage가 절대 아님 — 장애인(disability) 복지·요금제·접근성 기사(무관하면 none), 축제·행사·재난 대비 통신 지원이나 트래픽 증설 기사(carrier 또는 none), 장애 예방 훈련·점검·모의훈련 기사. 장애 기사의 중요도는 둘 중 하나만 가능 — 전국 단위 대규모 장애(전국 규모 또는 수백만 가입자, 수 시간 이상 지속)면 imp=5, 그 외 지역·일부 서비스·경미한 장애는 imp=3 이하. 장애 기사에 imp=4는 부여 금지.

카테고리(cat): outage contract tech earnings exec policy other
중요도(imp) — 보수적으로 판정하고 5점은 아래 유형에 해당할 때만 부여:
5 = ①전국 단위 대규모 통신망 장애 ②삼성전자 네트워크사업이 당사자인 대형 수주·실주·제재 확정 ③주요 통신사·장비사의 인수합병 및 시장 구도를 바꾸는 대형 딜(대형 M&A, 수조 원대 장비 공급계약) ④경쟁사(Ericsson·Nokia·Huawei·ZTE·Mavenir 등)의 주요 신제품·신기술 출시, 생산·R&D 거점 이전, CEO 등 최고경영진 교체 ⑤중국산 통신장비(Huawei·ZTE)에 대한 각국의 제재·퇴출·반입 금지. 단계 요건: ①②는 실제 발생·확정 보도만 5점, ③④⑤는 루머·검토·협상·추진 단계 보도라도 5점 부여. 다만 구체적 사실 근거 없이 시황 전망·애널리스트 의견만 담은 기사는 5 불가
4 = 경영진 보고 가치: 주요 수주전 진행 상황, 주파수 경매 결과, 규제 확정, 경쟁사의 대형 발표
3 = 주시할 업계 동향
2 = 참고 수준
1 = 단순 정보·홍보성
회사 이름이 크더라도 단순 언급·제품 소개·인터뷰·시황 전망 기사는 3 이하.
이슈(topic): 기사가 다루는 핵심 사건을 나타내는 짧은 한국어 이슈명. 반드시 "회사명 사건" 형식으로, 회사명을 첫 단어로 동일하게 표기할 것(예: "버라이즌 장애", "에릭슨 수주", "에릭슨 실적" — 회사명 표기는 전부 통일). 같은 사건을 다룬 기사는 제목 표현·매체·언어가 달라도 반드시 한 글자도 다르지 않은 동일 이슈명을 부여. 이슈명이 같으면 중복으로 간주되어 1건만 표시됨.
요약(sum): 반드시 100% 한국어로만 작성 — 영어 문장이나 영어 원문 요약을 그대로 넣는 것은 오답이며, 외국어 기사는 한국어로 번역해 요약. 4~5문장 300자 내외로, 핵심 사실 → 배경·수치 → 경쟁 구도 → 사업적 의미 순으로 충실히 작성. 제공된 제목·요약 범위 내에서만 작성하고 추측 금지. 제공 정보가 제목뿐이면 억지로 늘리지 말고 짧게 유지.

모든 기사에 대해 빠짐없이 JSON 배열만 출력: [{{"i":0,"sec":"carrier","cat":"contract","imp":3,"topic":"버라이즌 수주","sum":"..."}}]

기사 목록:
{chr(10).join(lines)}"""
        return {"contents":[{"parts":[{"text":prompt}]}],
                "generationConfig":{"response_mime_type":"application/json","temperature":0}}

    def parse_judged(raw):
        try:
            return json.loads(raw)
        except Exception:
            out = []
            for m in re.finditer(r"\{[^{}]*\}", raw):
                try: out.append(json.loads(m.group()))
                except Exception: pass
            if out: print(f"  일부 형식 오류 -> 복구 파싱 {len(out)}건")
            return out

    def apply_judged(batch, judged):
        applied = 0
        for j in judged:
            try: idx = int(j["i"])
            except Exception: continue
            if not (0 <= idx < len(batch)): continue
            item = batch[idx]
            sec = j.get("sec")
            if sec == "none" or sec not in VALID_IDS:
                item["sid"] = "drop"; continue
            item["sid"] = sec
            if j.get("cat"): item["category"] = j["cat"]
            try:
                imp = int(j.get("imp", 0))
                if 1 <= imp <= 5: item["importance"] = imp
            except Exception: pass
            if j.get("sum"): item["summary"] = str(j["sum"])
            if j.get("topic"): item["topic"] = re.sub(r"\s+"," ",str(j["topic"])).strip().lower()
            applied += 1
        return applied

    last_good = None
    if os.path.exists("gemini_model_nw.txt"):
        last_good = open("gemini_model_nw.txt", encoding="utf-8").read().strip()
    avail = []
    try:
        ml = gemini_call(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}&pageSize=200", timeout=30)
        for m in ml.get("models", []):
            name = m.get("name","").replace("models/","")
            if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in name \
               and not re.search(r"preview|exp|image|tts|live|audio|embedding|thinking|omni", name):
                avail.append(name)
        if avail: print("사용 가능 모델:", ", ".join(avail))
    except Exception as ex:
        print(f"경고: 모델 목록 조회 실패 - {ex}")
    prefer = ["gemini-2.5-flash","gemini-2.5-flash-lite","gemini-2.0-flash","gemini-2.0-flash-lite","gemini-1.5-flash","gemini-flash-latest"]
    cands = ([last_good] if last_good else []) + [m for m in prefer if m in avail] + avail + prefer
    cands = list(dict.fromkeys(c for c in cands if c))

    import time
    for model in cands:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        try:
            total = 0
            for ci, batch in enumerate(chunks):
                payload = build_payload(batch)
                ok = False
                for attempt in range(2):
                    try:
                        print(f"Gemini 분류 요청... ({model}, {ci+1}/{len(chunks)}묶음 {len(batch)}건)")
                        r = gemini_call(url, payload)
                        raw = r["candidates"][0]["content"]["parts"][0]["text"]
                        judged = parse_judged(raw)
                        if not judged: raise ValueError("판정 결과 파싱 실패")
                        total += apply_judged(batch, judged)
                        ok = True; break
                    except urllib.error.HTTPError as ex:
                        if ex.code == 429 and attempt == 0:
                            print(f"  {model} 429 -> 30초 대기 후 재시도"); time.sleep(30); continue
                        raise
                if not ok: raise ValueError("묶음 처리 실패")
                if ci < len(chunks) - 1: time.sleep(5)   # 분당 호출 제한 배려
            print(f"Gemini 판정 적용: 총 {total}건 / {len(chunks)}묶음")
            open("gemini_model_nw.txt","w",encoding="utf-8").write(model)
            return f"Gemini 분류 ({model})"
        except urllib.error.HTTPError as ex:
            if ex.code == 404: print(f"  {model} 사용 불가(404) -> 다음 모델")
            else: print(f"경고: {model} 실패 - HTTP {ex.code} -> 다음 모델")
        except Exception as ex:
            print(f"경고: {model} 실패 - {ex} -> 다음 모델")
    print("경고: 모든 Gemini 모델 실패 - 키워드 분류로 대체")
    return None

def _tokens(t):
    return set(re.findall(r"[a-z0-9가-힣]+", t.lower()))

def dedupe_topics(items):
    """1차: 같은 이슈명은 1건만. 2차: 이슈명이 달라도 제목 단어가 55% 이상 겹치면 중복으로 간주"""
    out, seen_topics, kept_tokens = [], set(), []
    for a in items:
        t = a.get("topic","")
        if t:
            if t in seen_topics: continue
        tk = _tokens(a.get("title",""))
        dup = False
        for x in kept_tokens:
            inter = len(tk & x); union = len(tk | x)
            if union and inter / union >= 0.55: dup = True; break
        if dup: continue
        if t: seen_topics.add(t)
        kept_tokens.append(tk)
        out.append(a)
    return out

def resolve_google_links(all_items):
    """news.google.com 중계 주소를 원문 기사 주소로 변환 (변환 실패 시 원래 링크 유지)"""
    targets = [a for a in all_items if "news.google.com" in a.get("url","")]
    if not targets: return
    try:
        from googlenewsdecoder import gnewsdecoder
    except Exception:
        print("안내: googlenewsdecoder 미설치 - 구글 링크 원본 변환 생략"); return
    cache, n = {}, 0
    for a in targets:
        u = a["url"]
        if u in cache:
            a["url"] = cache[u]; continue
        try:
            r = gnewsdecoder(u, interval=1)
            if isinstance(r, dict) and r.get("status") and r.get("decoded_url"):
                cache[u] = r["decoded_url"]; a["url"] = cache[u]; n += 1
        except Exception:
            pass
    print(f"구글 뉴스 링크 원본 변환: {n}/{len(targets)}건")

def main():
    pool, engine = crawl()
    data = {}
    for sid, name, _ in SEC_DEFS:
        items = [a for a in pool if a["sid"] == sid]
        items.sort(key=lambda a: (a["importance"], a["date"]), reverse=True)  # 중요도 -> 최신순
        items = dedupe_topics(items)
        data[sid] = [{k: a[k] for k in ("title","summary","source","date","url","category","importance","wl","topic")} for a in items[:30]]
        print(f"{name}: {len(items)}건 -> {len(data[sid])}건")
    # 최신 탭: 소규모 장애(outage & imp<5)는 제외 — 대규모 장애(imp=5)만 주요·최신에 노출
    latest = sorted([a for a in pool if not (a["sid"] == "outage" and a["importance"] < 5)],
                    key=lambda a: a["date"], reverse=True)
    latest = dedupe_topics(latest)[:LATEST_N]
    latest = [{k: a[k] for k in ("title","summary","source","date","url","category","importance","sid","topic")} for a in latest]
    shown = [a for arr in data.values() for a in arr] + latest
    resolve_google_links(shown)
    meta = {"generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M") + " · " + engine,
            "sections": [{"id": s[0], "name": s[1]} for s in [next(x for x in SEC_DEFS if x[0]==o) for o in ORDER]]}
    js = ("const NEWS_META = " + json.dumps(meta, ensure_ascii=False) + ";\n"
          + "const NEWS_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n"
          + "const NEWS_LATEST = " + json.dumps(latest, ensure_ascii=False) + ";\n")
    tpl = open("dashboard_network.html", encoding="utf-8").read()
    out = tpl.replace('<script src="news_data.js"></script>', "<script>\n" + js + "</script>")
    os.makedirs("docs", exist_ok=True)
    open("docs/network.html","w",encoding="utf-8").write(out)
    print("완료 -> docs/network.html 생성")

if __name__ == "__main__":
    main()
