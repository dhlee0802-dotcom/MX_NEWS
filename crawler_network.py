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
ARCHIVE_FILE, ARCHIVE_DAYS, ARCHIVE_MIN_IMP = "network_archive.json", 30, 4
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
    # ── 일본 ──
    ("ケータイ Watch", "https://k-tai.watch.impress.co.jp/data/rss/1.0/ktw/feed.rdf"),
    ("ITmedia Mobile", "https://rss.itmedia.co.jp/rss/2.0/mobile.xml"),
    # ── 인도 ──
    ("ET Telecom", "https://telecom.economictimes.indiatimes.com/rss/topstories"),
    ("Business Standard", "https://www.business-standard.com/rss/technology-108.rss"),
    ("Mint", "https://www.livemint.com/rss/industry"),
    ("Fortune India", "https://www.fortuneindia.com/rss"),
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
    "ドコモ OR KDDI OR ソフトバンク 基地局",
    "楽天モバイル OR 通信障害",
    "Reliance Jio OR Bharti Airtel OR Vodafone Idea",
    "TELUS OR Videotron OR SaskTel network",
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
    ("satellite", "[Sat] 위성·6G", ["starlink","스타링크","ast spacemobile","kuiper","amazon leo","저궤도","leo satellite","direct-to-cell","direct to cell","위성통신","위성 통신","non-terrestrial","ntn","6g","6세대 이동통신"]),
    ("competitor", "[Comp] 경쟁사", ["ericsson","에릭슨","nokia","노키아","huawei","화웨이","zte","mavenir","마베니어","rakuten symphony","라쿠텐 심포니","open ran","오픈랜","vran","ai-ran"]),
    ("policy", "[Policy] 정책·규제", ["fcc","과기정통부","spectrum","주파수","net neutrality","망중립성","digital networks act","cybersecurity act","spectrum auction","주파수 경매","통신 정책","통신 규제"]),
    ("carrier", "[Telco] 통신사", ["verizon","at&t","t-mobile","echostar","viaero","us cellular","charter","docomo","도코모","kddi","softbank","소프트뱅크","rakuten mobile","reliance jio","jio","airtel","vodafone","보다폰","telus","videotron","sasktel","deutsche telekom","도이치텔레콤","orange","telefonica","telefónica","skt","sk텔레콤","lg유플러스","lgu+","케이티","이동통신사","통신사"]),
]
ORDER = ["competitor","carrier","satellite","policy","outage"]
VALID_IDS = [s[0] for s in SEC_DEFS]
# outage 키워드 폴백 범위 제한: 명단의 이동통신사 이름이 직접 언급된 경우에만 outage로 분류
# (ChatGPT·클라우드 장애, 통신장애를 제도·책임 문맥에서 언급만 하는 기사 차단)
OUTAGE_SCOPE = [k for k in next(s[2] for s in SEC_DEFS if s[0] == "carrier")
                if not (k.isascii() and len(k) <= 3)] + ["이동통신사","통신3사","통신 3사"]
OUTAGE_SCOPE_RE = re.compile(r"(?<![a-z0-9])(kt|skt)(?![a-z0-9])")  # 짧은 약칭은 단어 경계로만 검사 (desktop 등 오탐 방지)

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
    print(f"제외어 {len(exclude)}개 로드: {', '.join(exclude) if exclude else '없음 (exclude_network.txt 미발견 또는 비어있음)'}")
    use_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    pool, seen = [], set()
    excl_n = [0]

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
    print(f"수집 완료 / 후보 풀: {len(pool)}건 / 제외어 필터 {excl_n[0]}건 제외")

    engine = "키워드 분류"
    if use_gemini and pool:
        engine = gemini_judge(pool) or engine
    # 2차 필터: Gemini가 만든 한국어 요약에도 제외어 재검사
    # (영어 원문엔 없던 제외어가 번역 요약에서 드러나는 경우 대응)
    if exclude:
        before = len(pool)
        pool[:] = [a for a in pool if not any(x in (a["title"] + " " + a["summary"]).lower() for x in exclude)]
        if before - len(pool):
            print(f"제외어 2차 필터(요약 기준): {before - len(pool)}건 제외")
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
competitor = 글로벌 대형 통신장비 경쟁사(Ericsson, Nokia, Huawei, ZTE, Mavenir, Rakuten Symphony)의 수주·기술·실적·전략. 국내 중소 통신장비·부품업체(우리넷, 알에프텍, 쏠리드 등) 기사는 competitor가 아니라 none
carrier = 통신사 동향. 대상: 한국 SKT·KT·LG유플러스 / 미국 Verizon·AT&T·T-Mobile·EchoStar·Viaero·US Cellular·Charter / 일본 NTT DOCOMO·KDDI·SoftBank·Rakuten Mobile / 인도 Reliance Jio·Bharti Airtel·Vodafone Idea / 캐나다 TELUS·Videotron·SaskTel / 유럽 Vodafone·Deutsche Telekom·Orange·Telefónica. 단, 네트워크 투자·장비 조달·주파수·실적·경영 전략 관련만 해당
satellite = 위성통신(Starlink, AST SpaceMobile, Amazon Leo/Kuiper, Direct-to-Cell, 저궤도 위성) 및 6G(기술·표준화·연구개발·상용화 준비)
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

def update_archive(items):
    """중요 기사(4점 이상)를 발행일 기준으로 network_archive.json에 누적. 30일 보존."""
    arch = {}
    if os.path.exists(ARCHIVE_FILE):
        try: arch = json.loads(open(ARCHIVE_FILE, encoding="utf-8").read())
        except Exception: arch = {}
    added = 0
    for a in items:
        day = a["date"][:10]
        lst = arch.setdefault(day, [])
        key = norm_key(a["title"]); tp = a.get("topic","")
        if any(norm_key(x.get("title","")) == key or (tp and x.get("topic") == tp) for x in lst):
            continue  # 같은 날짜에 같은 제목·이슈명 기사는 1건만
        lst.append({k: a[k] for k in ("title","summary","source","date","url","category","importance","sid","topic")})
        added += 1
    cutoff_day = (datetime.now(KST) - timedelta(days=ARCHIVE_DAYS)).strftime("%Y-%m-%d")
    arch = {d: v for d, v in arch.items() if d >= cutoff_day}
    for d in arch:
        arch[d].sort(key=lambda x: (x.get("importance",0), x.get("date","")), reverse=True)
    open(ARCHIVE_FILE, "w", encoding="utf-8").write(json.dumps(arch, ensure_ascii=False))
    print(f"아카이브 갱신: 신규 {added}건 / 보존 {len(arch)}일치")
    return arch

def main():
    pool, engine = crawl()
    data = {}
    for sid, name, _ in SEC_DEFS:
        items = [a for a in pool if a["sid"] == sid]
        items.sort(key=lambda a: (a["importance"], a["date"]), reverse=True)  # 중요도 -> 최신순
        items = dedupe_topics(items)
        cap = 60 if sid == "carrier" else 30   # 통신사는 지역 필터용으로 넉넉히 보관
        data[sid] = [{k: a[k] for k in ("title","summary","source","date","url","category","importance","wl","topic")} for a in items[:cap]]
        print(f"{name}: {len(items)}건 -> {len(data[sid])}건")
    # 최신 탭: 소규모 장애(outage & imp<5)는 제외 — 대규모 장애(imp=5)만 주요·최신에 노출
    latest = sorted([a for a in pool if not (a["sid"] == "outage" and a["importance"] < 5)],
                    key=lambda a: a["date"], reverse=True)
    latest = dedupe_topics(latest)[:LATEST_N]
    latest = [{k: a[k] for k in ("title","summary","source","date","url","category","importance","sid","topic")} for a in latest]
    arch_items = [{k: a[k] for k in ("title","summary","source","date","url","category","importance","sid","topic")}
                  for a in pool if a["importance"] >= ARCHIVE_MIN_IMP]
    shown = [a for arr in data.values() for a in arr] + latest + arch_items
    resolve_google_links(shown)
    archive = update_archive(arch_items)
    meta = {"generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M") + " · " + engine,
            "sections": [{"id": s[0], "name": s[1]} for s in [next(x for x in SEC_DEFS if x[0]==o) for o in ORDER]]}
    js = ("const NEWS_META = " + json.dumps(meta, ensure_ascii=False) + ";\n"
          + "const NEWS_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n"
          + "const NEWS_LATEST = " + json.dumps(latest, ensure_ascii=False) + ";\n"
          + "const NEWS_ARCHIVE = " + json.dumps(archive, ensure_ascii=False) + ";\n")
    js = js.replace("</", "<\\/")  # 기사 내용에 </script> 유사 문자열이 있어도 스크립트가 깨지지 않게
    tpl = open("dashboard_network.html", encoding="utf-8").read()
    out = tpl.replace('<script src="news_data.js"></script>', "<script>\n" + js + "</script>")
    os.makedirs("docs", exist_ok=True)
    open("docs/network.html","w",encoding="utf-8").write(out)
    print("완료 -> docs/network.html 생성")

if __name__ == "__main__":
    main()
