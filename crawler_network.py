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

    ("Mobile World Live", "https://www.mobileworldlive.com/feed/"),
    ("SDxCentral", "https://www.sdxcentral.com/feed/"),
    ("Telecom Paper", "https://www.telecompaper.com/rss/news"),
    # ── 종합·경제지 ──
    ("WSJ Tech", "https://feeds.a.dj.com/rss/RSSWSJD.xml"),
    ("Financial Times Tech", "https://www.ft.com/technology?format=rss"),
    ("Nikkei Asia", "https://asia.nikkei.com/rss/feed/nar"),
    # ── 일본 ──
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

    ("exec", ["사장","부사장","ceo","임원","인사","교체","appoint","resign","executive","조직개편"]),
    ("policy", ["정책","규제","제재","tariff","regulation","ban","spectrum","주파수","auction","경매","fcc"]),
]
CRIT = ["outage","통신장애","nationwide","전국","대규모","수주","contract win","제재","ban","breach","hack"]
HIGH = ["삼성","samsung","ericsson","nokia","huawei","5g","6g","주파수","spectrum","starlink","open ran"]

def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()

def norm_key(title):
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

    return []

def crawl():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    exclude = load_lines("exclude_network.txt")
    print(f"제외어 {len(exclude)}개 로드: {', '.join(exclude) if exclude else '없음 (exclude_network.txt 미발견 또는 비어있음)'}")
    use_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    pool, seen = [], set()
    excl_n = [0]

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
        if any(x in bl for x in exclude):
            excl_n[0] += 1; return
        sid = section_id(blob)
        if not sid:
            if use_gemini: sid = "unknown"
            else: return
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
섹션(sec):
competitor = 글로벌 대형 통신장비 경쟁사(Ericsson, Nokia, Huawei, ZTE, Mavenir, Rakuten Symphony)의 수주·기술·실적·전략. 국내 중소 통신장비·부품업체(우리넷, 알에프텍, 쏠리드 등) 기사는 competitor가 아니라 none
carrier = 통신사 동향. 대상: 한국 SKT·KT·LG유플러스 / 미국 Verizon·AT&T·T-Mobile·EchoStar·Viaero·US Cellular·Charter / 일본 NTT DOCOMO·KDDI·SoftBank·Rakuten Mobile / 인도 Reliance Jio·Bharti Airtel·Vodafone Idea / 캐나다 TELUS·Videotron·SaskTel / 유럽 Vodafone·Deutsche Telekom·Orange·Telefónica. 단, 네트워크 투자·장비 조달·주파수·실적·경영 전략 관련만 해당
satellite = 위성통신(Starlink, AST SpaceMobile, Amazon Leo/Kuiper, Direct-to-Cell, 저궤도 위성) 및 6G(기술·표준화·연구개발·상용화 준비)
policy = 통신 정책·규제(FCC, 과기정통부, Digital Networks Act, EU Cybersecurity Act, 망중립성, 주파수 경매 등)
outage = 위에 나열한 이동통신사의 통신망(무선망·유선 인터넷) 장애 발생·확산·복구 보도만 해당
none = 삼성전자 네트워크사업과 무관 → 제외. 특히 스마트폰 단말·요금제 프로모션·소비자 마케팅·연예 기사는 none. 원자력·건설·에너지·조선·바이오 등 통신과 무관한 산업 기사는 회사명이 겹쳐도 none — 삼성물산·삼성SDI·삼성중공업 등 삼성전자가 아닌 삼성 계열사 기사, 그리고 삼성전자 기사라도 반도체·가전·스마트폰 등 네트워크사업 외 분야 기사는 모두 none

    avail = []
    try:
        ml = gemini_call(f"https://generativelanguage.googleapis.com/v1beta/models?key={key}&pageSize=200", timeout=30)
        for m in ml.get("models", []):
            name = m.get("name","").replace("models/","")
            if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in name \
               and not re.search(r"preview|exp|image|tts|live|audio|embedding|thinking|omni", name):
                avail.append(name)
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
    pool, engine = crawl()
    data = {}
    for sid, name, _ in SEC_DEFS:
        items = [a for a in pool if a["sid"] == sid]
        items.sort(key=lambda a: (a["importance"], a["date"]), reverse=True)  # 중요도 -> 최신순
        items = dedupe_topics(items)
        cap = 60 if sid == "carrier" else 30   # 통신사는 지역 필터용으로 넉넉히 보관
        data[sid] = [{k: a[k] for k in ("title","summary","source","date","url","category","importance","wl","topic")} for a in items[:cap]]
    tpl = open("dashboard_network.html", encoding="utf-8").read()
    out = tpl.replace('<script src="news_data.js"></script>', "<script>\n" + js + "</script>")
    os.makedirs("docs", exist_ok=True)
    open("docs/network.html","w",encoding="utf-8").write(out)
    print("완료 -> docs/network.html 생성")

if __name__ == "__main__":
    main()
