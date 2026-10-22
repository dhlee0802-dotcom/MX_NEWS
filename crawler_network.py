# -*- coding: utf-8 -*-
"""
Network 뉴스 크롤러 - GitHub Actions판 (MX crawler_actions.py 기반 이식)
매체 RSS(통신 전문지 포함) + Google News + 네이버 수집, 전역 중복제거,
섹션 분류(경쟁사/통신사/위성/정책/Outage), Gemini 배치 판정, 최신 20건.
결과: docs/network.html (데이터 내장 단일 파일, GitHub Pages로 서빙)
필요 환경변수: GEMINI_API_KEY (없으면 키워드 분류로 동작), NAVER_CLIENT_ID/SECRET(선택)
"""
import feedparser, json, re, html, os, sys
import socket
socket.setdefaulttimeout(20)   # 느린 피드·사이트가 실행을 오래 붙잡지 않도록 전역 타임아웃
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
    "1Finity OR Fujitsu network 5G",
    "Samsung Networks 5G",
    "삼성전자 네트워크 OR 5G 장비",
    # 통신사 (지역별 묶음)
    "Verizon OR AT&T OR T-Mobile network",
    "EchoStar OR Viaero OR US Cellular OR Charter spectrum",
    "NTT DOCOMO OR KDDI OR SoftBank OR Rakuten Mobile network",
    "ドコモ OR KDDI OR ソフトバンク 基地局",
    "楽天モバイル OR 通信障害",
    "Reliance Jio OR Bharti Airtel OR Vodafone Idea",
    "TELUS OR Videotron OR SaskTel OR Rogers OR Bell Canada",
    "Vodafone OR Deutsche Telekom OR Orange OR Telefonica network",
    "SKT OR KT OR LG유플러스",
    # 위성
    "Starlink OR AST SpaceMobile direct to cell",
    "Amazon Leo OR Kuiper satellite",
    "저궤도 위성통신 OR 스타링크",
    "6G technology OR 6G standard",
    "6G 상용화 OR 6G 표준",
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
    ("satellite", "[Sat] 위성·6G", ["starlink","스타링크","ast spacemobile","kuiper","amazon leo","저궤도","leo satellite","direct-to-cell","direct to cell","위성통신","위성 통신","non-terrestrial","ntn","6g","6세대 이동통신"]),
    ("competitor", "[Comp] 경쟁사", ["ericsson","에릭슨","nokia","노키아","huawei","화웨이","zte","mavenir","마베니어","1finity","원피니티","fujitsu","후지쯔","후지츠","rakuten symphony","라쿠텐 심포니","open ran","오픈랜","vran","ai-ran"]),
    ("policy", "[Policy] 정책·규제", ["fcc","과기정통부","spectrum","주파수","net neutrality","망중립성","digital networks act","cybersecurity act","spectrum auction","주파수 경매","통신 정책","통신 규제"]),
    ("carrier", "[Telco] 통신사", ["verizon","at&t","t-mobile","echostar","viaero","us cellular","charter","docomo","도코모","kddi","softbank","소프트뱅크","rakuten mobile","reliance jio","jio","airtel","vodafone","보다폰","telus","videotron","sasktel","rogers","로저스","bell canada","deutsche telekom","도이치텔레콤","orange","telefonica","telefónica","skt","sk텔레콤","lg유플러스","lgu+","케이티","이동통신사","통신사"]),
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
    return re.sub(r"\s+", " ", s).strip()

def norm_key(title):
    return re.sub(r"[^a-z0-9가-힣]", "", title.lower())[:40]

def section_id(text):
    # '장애인' 복지·요금제 기사가 outage로 오분류되지 않도록 단어 자체를 제거 후 검사
    t = text.lower().replace("장애인", "")
    for sid, _, kws in SEC_DEFS:
        if any(k.lower() in t for k in kws):
            if sid == "outage" and not (any(h.lower() in t for h in OUTAGE_SCOPE) or OUTAGE_SCOPE_RE.search(t)):
                continue  # 명단의 이동통신사 언급 없는 장애 기사는 outage 아님 -> 다음 섹션 검사
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

TOPIC_SEEN_FILE, TOPIC_TTL_H, TOPIC_KEEP_DAYS = "topics_seen_network.json", 48, 14

def filter_stale_topics(pool):
    """이슈명별 최초 보도 시각을 기록하고, 최초 보도 후 TOPIC_TTL_H시간이 지난 이슈의 기사는
    새로 발행된 기사라도 제외 (같은 이슈가 며칠씩 대시보드에 머무는 피로도 방지)"""
    seen = {}
    if os.path.exists(TOPIC_SEEN_FILE):
        try: seen = json.loads(open(TOPIC_SEEN_FILE, encoding="utf-8").read())
        except Exception: seen = {}
    now = datetime.now(KST)
    # 1) 이번 수집분의 이슈별 최초 보도 시각 기록 (기사 발행 시각 기준, 더 이른 시각으로 갱신)
    for a in pool:
        t = issue_key(a)
        if not t: continue
        if t not in seen or a["date"] < seen[t]:
            seen[t] = a["date"]
    # 2) 최초 보도 후 TTL 경과한 이슈의 기사 제외
    kept, dropped = [], 0
    for a in pool:
        t = issue_key(a)
        if t and t in seen:
            try:
                fdt = datetime.strptime(seen[t], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
                if now - fdt > timedelta(hours=TOPIC_TTL_H):
                    dropped += 1; continue
            except Exception: pass
        kept.append(a)
    # 3) 보존기간 지난 이슈 정리 후 저장
    cutoff = (now - timedelta(days=TOPIC_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    open(TOPIC_SEEN_FILE, "w", encoding="utf-8").write(json.dumps(seen, ensure_ascii=False))
    if dropped: print(f"이슈 노출기한 필터: {dropped}건 제외 (최초 보도 후 {TOPIC_TTL_H}시간 경과 이슈)")
    pool[:] = kept

def crawl():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    # 내장 제외어: 삼성전자 외 삼성 계열사(비통신)는 Gemini 판정과 무관하게 수집 단계에서 제외
    BUILTIN_EXCLUDE = ["삼성물산","삼성중공업","삼성sdi","삼성바이오","삼성생명","삼성화재","삼성카드",
                       "삼성증권","삼성엔지니어링","삼성이앤에이","samsung c&t","samsung heavy",
                       "samsung sdi","samsung biologics","samsung e&a"]
    exclude = load_lines("exclude_network.txt") + BUILTIN_EXCLUDE
    print(f"제외어 {len(exclude)}개 로드 (내장 {len(BUILTIN_EXCLUDE)}개 포함): "
          f"{', '.join(load_lines('exclude_network.txt')) or 'exclude_network.txt 없음/비어있음'}")
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
        seen.add(key)
        pool.append({
            "sid": sid, "title": title[:90],
            "summary": (summary[:220] + "…") if len(summary) > 220 else summary,
            "source": src or "Google News",
            "date": pub.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
            "url": link, "category": category(blob), "importance": score(blob), "wl": wl,
            "topic": "", "subj": "", "evt": "", "tgt": "", "kw": "",
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

    # ── Bing News (원문 직링크 제공) — Google News보다 먼저 수집해 같은 기사면 직링크가 우선되게 함 ──
    from urllib.parse import urlparse, parse_qs, unquote
    bing_n = 0
    for q in GN_QUERIES:
        ko = bool(re.search(r"[가-힣]", q))
        ja = bool(re.search(r"[ぁ-んァ-ヶ一-龯]", q))
        mkt = "ko-KR" if ko else "ja-JP" if ja else "en-US"
        url = f"https://www.bing.com/news/search?q={quote(q)}&format=RSS&mkt={mkt}&qft=interval%3d%228%22"
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                try: pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                except Exception: continue
                link = e.get("link","")
                if "apiclick" in link:  # Bing 중계형 링크면 url 파라미터에서 원문 추출
                    try:
                        qs = parse_qs(urlparse(link).query)
                        if qs.get("url"): link = unquote(qs["url"][0])
                    except Exception: pass
                if "bing.com" in link: continue   # 원문 추출 실패분은 버림 (중계 링크 유입 방지)
                src = urlparse(link).netloc.replace("www.","") if link else ""
                before = len(pool)
                add(e.get("title",""), e.get("description",""), src, pub, link, False)
                bing_n += len(pool) - before
        except Exception as ex:
            print(f"경고: Bing News 실패({q}) - {str(ex)[:80]}")
    print(f"Bing News 수집 완료: {bing_n}건 (원문 직링크)")

    for q in GN_QUERIES:
        ko = bool(re.search(r"[가-힣]", q))
        ja = bool(re.search(r"[ぁ-んァ-ヶ一-龯]", q))
        hl, gl, ceid = ("ko","KR","KR:ko") if ko else ("ja","JP","JP:ja") if ja else ("en-US","US","US:en")
        url = f"https://news.google.com/rss/search?q={quote(q+' when:2d')}&hl={hl}&gl={gl}&ceid={ceid}"
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                try: pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                except Exception: continue
                src = ""
                if hasattr(e, "source"): src = clean(e.source.get("title",""))
                t = e.get("title","")
                if src and t.endswith(" - " + src): t = t[:-(len(src)+3)]  # GN 제목 꼬리(- 매체명) 제거
                add(t, e.get("description",""), src, pub, e.get("link",""), False)
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
    for a in pool:   # 100점 점수 미부여(키워드 분류·누락) 기사는 등급 중앙값으로 보정
        if not isinstance(a.get("score"), int): a["score"] = default_score(a["importance"])
    filter_stale_topics(pool)
    return pool, engine

SCORE_BAND = {5: (90, 100), 4: (75, 89), 3: (50, 74), 2: (30, 49), 1: (0, 29)}
def clamp_score(imp, sc):
    """100점 점수를 1~5등급 구간 안으로 고정 (등급·점수 불일치 방지)"""
    lo, hi = SCORE_BAND.get(int(imp or 3), (50, 74))
    return max(lo, min(hi, int(sc)))
def default_score(imp):
    lo, hi = SCORE_BAND.get(int(imp or 3), (50, 74))
    return (lo + hi) // 2

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
competitor = 글로벌 대형 통신장비 경쟁사(Ericsson, Nokia, Huawei, ZTE, Mavenir, 1Finity, Fujitsu, Rakuten Symphony)의 수주·기술·실적·전략. 국내 중소 통신장비·부품업체(우리넷, 알에프텍, 쏠리드 등) 기사는 competitor가 아니라 none
carrier = 통신사 동향. 대상: 한국 SKT·KT·LG유플러스 / 미국 Verizon·AT&T·T-Mobile·EchoStar·Viaero·US Cellular·Charter / 일본 NTT DOCOMO·KDDI·SoftBank·Rakuten Mobile / 인도 Reliance Jio·Bharti Airtel·Vodafone Idea / 캐나다 TELUS·Videotron·SaskTel·Rogers·Bell / 유럽 Vodafone·Deutsche Telekom·Orange·Telefónica. 단, 네트워크 투자·장비 조달·주파수·실적·경영 전략 관련만 해당
satellite = 위성통신(Starlink, AST SpaceMobile, Amazon Leo/Kuiper, Direct-to-Cell, 저궤도 위성) 및 6G(기술·표준화·연구개발·상용화 준비)
policy = 통신 정책·규제(FCC, 과기정통부, Digital Networks Act, EU Cybersecurity Act, 망중립성, 주파수 경매 등)
outage = 위에 나열한 이동통신사의 통신망(무선망·유선 인터넷) 장애 발생·확산·복구 보도만 해당
none = 삼성전자 네트워크사업과 명백히 무관할 때만 → 제외. 판단이 애매한 통신·네트워크 관련 기사는 none으로 버리지 말고 가장 가까운 섹션에 imp 2로 분류할 것. 특히 스마트폰 단말·요금제 프로모션·소비자 마케팅·연예 기사는 none. 원자력·건설·에너지·조선·바이오 등 통신과 무관한 산업 기사는 회사명이 겹쳐도 none — 삼성물산·삼성SDI·삼성중공업 등 삼성전자가 아닌 삼성 계열사 기사, 그리고 삼성전자 기사라도 반도체·가전·스마트폰 등 네트워크사업 외 분야 기사는 모두 none

판정 규칙: sec=outage는 위 명단의 특정 이동통신사가 운영하는 통신망에서 실제로 발생한 장애의 발생·확산·복구 보도만 해당하며, 이 경우 통신사 이름이 있어도 반드시 outage로 분류. 통신장애를 언급만 하는 기사는 outage가 아님 — 통신장애 관련 제도·법령·책임 범위·보상 기준 논의, 의료·재난·행정 등 다른 주제의 기사에서 통신장애를 부수적으로 언급하는 경우, 과거 장애의 회고·통계 기사는 실제 장애 발생 보도가 아니므로 outage 금지(내용에 따라 policy·carrier 또는 none). 통신사가 아닌 서비스의 장애 — ChatGPT 등 AI 서비스, AWS·Azure·Google Cloud·Cloudflare 등 클라우드/CDN, 앱·플랫폼·게임·금융·SNS 서비스 장애 — 는 outage가 절대 아니며 none으로 제외("outage"라는 단어가 있어도 통신사 통신망 장애가 아니면 제외). 해킹·보안사고 기사는 그로 인해 통신 서비스 중단·장애가 실제 발생한 경우에만 outage이며, 서비스 영향이 없는 단순 해킹·개인정보 유출·보안 취약점 기사는 none으로 제외. 다음도 outage가 절대 아님 — 장애인(disability) 복지·요금제·접근성 기사(무관하면 none), 축제·행사·재난 대비 통신 지원이나 트래픽 증설 기사(carrier 또는 none), 장애 예방 훈련·점검·모의훈련 기사. 장애 기사의 중요도는 둘 중 하나만 가능 — 전국 단위 대규모 장애(전국 규모 또는 수백만 가입자, 수 시간 이상 지속)면 imp=5, 그 외 지역·일부 서비스·경미한 장애는 imp=3 이하. 장애 기사에 imp=4는 부여 금지.

카테고리(cat): outage contract tech earnings exec policy other
중요도(imp) — 보수적으로 판정하고 5점은 아래 유형에 해당할 때만 부여:
5 = ①전국 단위 대규모 통신망 장애 ②삼성전자 네트워크사업부가 당사자인 통신장비·네트워크 관련 대형 수주·실주·제재 확정(다른 삼성 계열사나 비통신 분야 수주는 해당 없음) ③업계 판도를 바꾸는 수준의 대형 M&A·딜(주요 통신사·장비사 간 인수합병, 수조 원대 장비 공급계약 — 소규모 기업·스타트업 인수나 지분 일부 투자는 4 이하) ④경쟁사(Ericsson·Nokia·Huawei·ZTE·Mavenir 등)의 플래그십급 신제품·차세대 기술의 공식 출시, 생산·R&D 거점의 국가 간 이전, CEO·회장급 최고경영진 교체(부분 업그레이드·로드맵 발표·임원급 인사는 4 이하) ⑤주요국 정부 차원의 중국산 통신장비(Huawei·ZTE) 제재·퇴출·반입 금지(개별 기업·지방정부 차원 조치는 4 이하). 단계 요건: ①②는 실제 발생·확정 보도만 5점, ③④⑤는 루머·검토·협상·추진 단계 보도라도 5점 가능. 판단이 애매하면 5가 아닌 4를 부여할 것 — 한 판정 묶음에서 5점은 많아야 2~3건이어야 정상. 구체적 사실 근거 없이 시황 전망·애널리스트 의견만 담은 기사는 5 불가
4 = 경영진 보고 가치: 주요 수주전 진행 상황, 주파수 경매 결과, 규제 확정, 경쟁사의 대형 발표
3 = 주시할 업계 동향
2 = 참고 수준
1 = 단순 정보·홍보성
회사 이름이 크더라도 단순 언급·제품 소개·인터뷰·시황 전망 기사는 3 이하.
점수(score): 0~100 정수로 imp 등급 안에서 세분화. 반드시 등급 구간을 지킬 것 — imp5: 90~100, imp4: 75~89, imp3: 50~74, imp2: 30~49, imp1: 0~29. 구간 안에서는 ①삼성전자 네트워크사업과의 직접 관련성(당사 고객사·경쟁사 당사자 여부) ②사업 영향 규모(금액·가입자·시장 범위) ③확정성(확정 보도 > 협상·검토 > 루머·전망) ④시급성(즉시 대응 필요 여부)이 높을수록 높은 점수. 모든 기사에 반드시 기재.
주체(subj): 사건의 주체를 아래 표준 표기 중에서만 골라 기재 (다른 표기·번역·영문 절대 금지):
에릭슨, 노키아, 화웨이, ZTE, 마베니어, 원피니티, 후지쯔, 라쿠텐심포니, 삼성, 버라이즌, AT&T, T모바일, 에코스타, US셀룰러, 차터, SKT, KT, LG유플러스, 도코모, KDDI, 소프트뱅크, 라쿠텐모바일, 지오, 에어텔, 보다폰아이디어, 텔러스, 비디오트론, 사스크텔, 로저스, 벨, 보다폰, 도이치텔레콤, 오렌지, 텔레포니카, 스타링크, AST, 아마존, FCC, 과기정통부, EU
어떤 언어의 기사든 같은 회사·기관이면 반드시 같은 표준 표기를 쓸 것(Ericsson도 エリクソン도 전부 "에릭슨"). 목록에 없는 주체(특정 공항, 지역, 타 정부기관 등)는 짧은 한국어 명사 하나로 통일해 기재(예: "미국공항", "인도정부").
사건(evt): 반드시 다음 중 하나만: 장애 신제품 수주공급 인수합병 투자 실적 경영진거점 규제주파수 위성 요금제 기타. 기준 — 설비투자·데이터센터·지분 투자는 "투자", CEO·임원 인사와 생산·R&D 거점 이전·감원은 "경영진거점", 주파수 할당·경매·규제·제재는 "규제주파수", 위성 발사·D2C 제휴·위성 서비스 개시는 "위성", 요금제 개편·인상·인하는 "요금제"
대상(tgt): 사건의 상대방(수주공급의 발주 통신사, 인수합병의 피인수사, 제재의 대상국 등)이 기사에 명시된 경우에만 위 표준 표기(또는 국가명 한 단어)로 기재하고, 없거나 불분명하면 빈 문자열 "". 같은 주체+사건이라도 대상이 다르면 별개 사건으로 구분하기 위한 필드임.
과거(past): 기사가 보도하는 핵심 사건이 이미 발생한 지 수개월~수년이 지난 과거 사건이면 1, 최근 발생했거나 진행 중인 사건이면 0. 회고·기념·연재·분석 기사, 그리고 스팸성 사이트가 과거의 유명 기사를 최신 날짜로 재발행한 경우(발행일은 최신이어도 내용은 옛 사건)는 반드시 1. 특히 과거의 대규모 통신장애(예: 2021년 NTT 도코모 200만 명 장애, 2022년 KDDI 장애, 2018년 KT 아현 화재)를 다룬 기사를 현재 발생한 장애로 오인하지 말 것 — 기사 속 연도, "당시", "그해", 이미 종결된 사건의 사과·복구 완료 서술 등이 단서. past=1 기사는 전량 자동 제외됨. 현재 장애 기사가 과거 사례를 비교·인용만 하는 경우는 0.
이슈(topic): 기사가 다루는 핵심 사건을 나타내는 짧은 한국어 이슈명. 반드시 "회사명 사건" 형식으로, 회사명을 첫 단어로 동일하게 표기할 것(예: "버라이즌 장애", "에릭슨 수주", "에릭슨 실적" — 회사명 표기는 전부 통일). 영어·일본어·중국어 등 어떤 언어의 기사라도 이슈명은 반드시 한국어 표기(에릭슨, 노키아, 화웨이, 버라이즌, 도이치텔레콤 등)로 쓸 것 — 같은 사건을 다룬 다국어 기사가 전부 동일 이슈명으로 묶여야 함. 특정 회사가 주체가 아닌 사건(공항·지역 통신장애, 정책 발표 등)은 "지역/기관 사건" 형식으로 통일(예: 같은 공항 통신장애를 다룬 한국어·영어·일본어 기사 전부 → "미국공항 통신장애" 하나로). 같은 사건을 다룬 기사는 제목 표현·매체·언어가 달라도 반드시 한 글자도 다르지 않은 동일 이슈명을 부여. 특히 같은 행사·발표·컨퍼런스·국정감사·정책 브리핑에서 파생된 기사들은 세부 주제가 조금씩 달라도 전부 하나의 동일 이슈명으로 묶을 것(예: 국정감사에서 나온 통신 관련 기사 전부 → "과기정통부 국감"). 이슈명이 같으면 중복으로 간주되어 1건만 표시됨.
키워드(kw): 문장이 아닌 순수 키워드만, 최대 12자. 조사·서술어·수식어 금지, 2~3어절 명사 나열 (예: "CEO 교체", "2Q 매출 -6%", "美 제재 확대", "D2C 계약", "R&D 거점 이전", "6G 시연"). 회사명 생략. 모든 기사에 반드시 기재.
번역제목(kt): 기사 제목의 자연스러운 한국어 번역. 이미 한국어 제목이면 그대로 기재. 원제목의 의미를 유지하고 과장 금지, 매체명·불필요한 수식어는 제거, 80자 이내. 모든 기사에 반드시 기재.
요약(sum): 반드시 100% 한국어로만 작성 — 영어 문장을 그대로 넣는 것은 오답이며, 외국어 기사는 한국어로 번역해 요약. 메신저 보고에 바로 붙여 쓸 수 있는 개조식으로 작성: "- "로 시작하는 불릿 2~4개, 불릿 사이는 줄바꿈(\\n), 각 불릿은 명사형 종결(예: 계약 체결, 출시 발표, 대응 필요). 순서는 핵심 사실 → 수치·배경 → 경쟁 구도·사업적 의미. 기사에 인용된 수치·금액·비율·규모는 최대한 그대로 포함할 것. 제공된 제목·요약 범위 내에서만 작성하고 추측 금지. 제공 정보가 제목뿐이면 불릿 1개만 작성.

모든 기사에 대해 빠짐없이 JSON 배열만 출력: [{{"i":0,"sec":"carrier","cat":"contract","imp":3,"score":62,"subj":"에릭슨","evt":"수주공급","tgt":"버라이즌","past":0,"topic":"에릭슨 수주","kw":"버라이즌 5G 계약 체결","kt":"에릭슨, 버라이즌과 5G 장비 공급 계약 체결","sum":"..."}}]

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
            try:
                if int(j.get("past", 0)) == 1:   # 과거 사건 회고·재게재 기사는 섹션 무관 전량 제외
                    item["sid"] = "drop"; applied += 1; continue
            except Exception: pass
            sec = j.get("sec")
            if sec == "none" or sec not in VALID_IDS:
                item["sid"] = "drop"; continue
            item["sid"] = sec
            if j.get("cat"): item["category"] = j["cat"]
            try:
                imp = int(j.get("imp", 0))
                if 1 <= imp <= 5: item["importance"] = imp
            except Exception: pass
            try:
                sc = int(float(j.get("score", -1)))
                if 0 <= sc <= 100: item["score"] = clamp_score(item["importance"], sc)
            except Exception: pass
            if j.get("sum"): item["summary"] = str(j["sum"])
            if j.get("topic"): item["topic"] = re.sub(r"\s+"," ",str(j["topic"])).strip().lower()
            kw = re.sub(r"\s+", " ", str(j.get("kw",""))).strip()
            if kw: item["kw"] = kw[:24]
            kt = re.sub(r"\s+", " ", str(j.get("kt",""))).strip()
            if len(kt) >= 5:  # 한국어 번역 제목으로 교체 (원제목은 otitle에 보존)
                item["otitle"] = item["title"]; item["title"] = kt[:90]
            for f in ("subj","evt","tgt"):  # 구조화 키: 공백 제거·소문자 정규화로 표기 흔들림 최소화
                if j.get(f): item[f] = re.sub(r"\s+","",str(j[f])).strip().lower()
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
    prefer = ["gemini-2.5-flash","gemini-flash-latest","gemini-2.0-flash","gemini-1.5-flash"]
    # 모델명 변경에 흔들리지 않도록: lite가 아닌 모델을 항상 lite보다 우선 (판정 품질 확보)
    full  = [m for m in avail if "lite" not in m]
    lite  = [m for m in avail if "lite" in m]
    cands = [m for m in prefer if m in full] + full             + ([last_good] if last_good and "lite" not in last_good else [])             + lite + prefer + ["gemini-2.5-flash-lite","gemini-flash-lite-latest"]
    cands = list(dict.fromkeys(c for c in cands if c))

    import time
    diag = ["avail: " + ", ".join(avail)]
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
                        r = gemini_call(url, payload, timeout=240)   # 신형 상위 모델은 응답이 느려 여유 확보
                        raw = r["candidates"][0]["content"]["parts"][0]["text"]
                        judged = parse_judged(raw)
                        if not judged: raise ValueError("판정 결과 파싱 실패")
                        total += apply_judged(batch, judged)
                        ok = True; break
                    except urllib.error.HTTPError as ex:
                        if ex.code in (429, 503) and attempt == 0:   # 혼잡·한도는 잠시 후 재시도
                            print(f"  {model} HTTP {ex.code} -> 20초 대기 후 재시도"); time.sleep(20); continue
                        raise
                    except (TimeoutError, OSError) as ex:
                        if attempt == 0:                              # 응답 지연도 1회 재시도
                            print(f"  {model} 응답 지연 -> 20초 대기 후 재시도"); time.sleep(20); continue
                        raise
                if not ok: raise ValueError("묶음 처리 실패")
                if ci < len(chunks) - 1: time.sleep(5)   # 분당 호출 제한 배려
            print(f"Gemini 판정 적용: 총 {total}건 / {len(chunks)}묶음")
            open("gemini_model_nw.txt","w",encoding="utf-8").write(model)
            diag.append(f"선택: {model}")
            open("gemini_diag.txt","w",encoding="utf-8").write("\n".join(diag))
            return f"Gemini 분류 ({model})"
        except urllib.error.HTTPError as ex:
            diag.append(f"{model}: HTTP {ex.code}")
            if ex.code == 404: print(f"  {model} 사용 불가(404) -> 다음 모델")
            else: print(f"경고: {model} 실패 - HTTP {ex.code} -> 다음 모델")
        except Exception as ex:
            diag.append(f"{model}: {str(ex)[:80]}")
            print(f"경고: {model} 실패 - {ex} -> 다음 모델")
    diag.append("선택: 없음(키워드 분류)")
    open("gemini_diag.txt","w",encoding="utf-8").write("\n".join(diag))
    print("경고: 모든 Gemini 모델 실패 - 키워드 분류로 대체")
    return None

def _tokens(t):
    return set(re.findall(r"[a-z0-9가-힣]+", t.lower()))

def event_key(a):
    """주체|사건 구조화 키 (Gemini가 표준 표기로 부여). 둘 다 있어야 유효"""
    s = a.get("subj",""); e = a.get("evt","")
    return (s + "|" + e) if (s and e) else ""

def event_cmp(a, b):
    """구조화 키 비교: True=같은 사건, False=확실히 별개(같은 키·다른 대상), None=판단 유보(폴백 검사로)"""
    ka, kb = event_key(a), event_key(b)
    if not (ka and kb) or ka != kb: return None
    ta, tb = a.get("tgt",""), b.get("tgt","")
    if ta and tb and ta != tb: return False  # 예: 둘 다 에릭슨|수주공급 이지만 대상이 버라이즌/텔러스로 다름
    return True

def issue_key(a):
    """노출기한 장부용 키: 구조화 키(+대상) 우선, 없으면 이슈명"""
    k = event_key(a)
    if k:
        return k + ("|" + a["tgt"] if a.get("tgt","") else "")
    return a.get("topic","")

def is_dup_pair(a, b):
    """대시보드 중복 판정: 1차 구조화 키(주체+사건, 대상 다르면 별개) → 2차 폴백(이슈명·제목 유사도)"""
    v = event_cmp(a, b)
    if v is not None: return v
    ta, tb = a.get("topic",""), b.get("topic","")
    if ta and ta == tb: return True
    x, y = _tokens(a.get("title","")), _tokens(b.get("title",""))
    if x and y and len(x & y) / len(x | y) >= 0.55: return True
    return False

def dedupe_topics(items):
    """같은 사건은 대표 1건만 남기고, 대표에 관련 보도 수(rel)를 기록"""
    out = []
    for a in items:
        rep = next((b for b in out if is_dup_pair(a, b)), None)
        if rep is not None:
            rep["rel"] = rep.get("rel", 1) + 1
            if "news.google.com" in rep.get("url","") and a.get("url") and "news.google.com" not in a["url"]:
                rep["url"] = a["url"]; rep["source"] = a.get("source", rep.get("source",""))  # 중계 대신 직링크 채택
            continue
        a["rel"] = a.get("rel", 1)
        out.append(a)
    return out

GN_CACHE_FILE, GN_CACHE_MAX = "gn_url_cache_network.json", 3000
GN_DECODE_BUDGET = 15    # 실행당 신규 변환 상한 (구글이 Actions발 변환을 차단 중 — 연속 5회 실패 시 조기 중단)

def resolve_google_links(all_items):
    """news.google.com 중계 주소를 원문 기사 주소로 변환 (변환 실패 시 원래 링크 유지).
    성공분은 파일 캐시에 저장해 다음 실행에서 재변환 없이 즉시 적용 — 매 실행 변환량을 줄여
    구글 rate limit 실패를 최소화하고, 실패분은 다음 실행에서 자연 재시도됨."""
    targets = [a for a in all_items if "news.google.com" in a.get("url","")]
    if not targets: return
    cache = {}
    if os.path.exists(GN_CACHE_FILE):
        try: cache = json.loads(open(GN_CACHE_FILE, encoding="utf-8").read())
        except Exception: cache = {}
    # 1) 캐시 적중분 먼저 적용
    pend = []
    for a in targets:
        u = a["url"]
        if u in cache: a["url"] = cache[u]
        else: pend.append(a)
    hit = len(targets) - len(pend)
    if not pend:
        print(f"구글 뉴스 링크 원본 변환: 캐시 {hit}건 / 신규 0건")
        return
    try:
        from googlenewsdecoder import gnewsdecoder
    except Exception:
        print("안내: googlenewsdecoder 미설치 - 구글 링크 원본 변환 생략"); return
    import time
    n, fail, first_err, skipped = 0, 0, "", 0
    run, seen_urls = {}, set()
    for a in pend:
        u = a["url"]
        if u in run:
            a["url"] = run[u]; continue
        if u in seen_urls: continue   # 이번 실행에서 이미 실패한 주소는 재시도 생략
        if n + fail >= GN_DECODE_BUDGET or (n == 0 and fail >= 5):
            skipped += 1; continue    # 상한 초과 또는 연속 실패(차단 감지) 시 이번 실행은 중단
        ok = False
        try:
            r = gnewsdecoder(u, interval=1)
            if isinstance(r, dict) and r.get("status") and r.get("decoded_url"):
                run[u] = r["decoded_url"]; a["url"] = run[u]; n += 1; ok = True
            elif not first_err and isinstance(r, dict): first_err = str(r.get("message",""))[:120]
        except Exception as ex:
            if not first_err: first_err = str(ex)[:120]
        if not ok:
            fail += 1; seen_urls.add(u)
    cache.update(run)
    if len(cache) > GN_CACHE_MAX:     # 캐시 크기 상한: 오래된 항목부터 정리
        cache = dict(list(cache.items())[-GN_CACHE_MAX:])
    try:
        open(GN_CACHE_FILE, "w", encoding="utf-8").write(json.dumps(cache, ensure_ascii=False))
    except Exception: pass
    msg = f"구글 뉴스 링크 원본 변환: 캐시 {hit}건 / 신규 성공 {n}건 / 실패 {fail}건"
    if skipped: msg += f" / 다음 실행 이월 {skipped}건"
    if fail and first_err: msg += f" (첫 실패 사유: {first_err})"
    print(msg)

def _arch_dup(a, b):
    """아카이브용 중복 판정: 1차 구조화 키(주체+사건+대상) → 2차 이슈명 일치/유사·제목 일치/유사"""
    v = event_cmp(a, b)
    if v is not None: return v
    ta, tb = a.get("topic",""), b.get("topic","")
    if ta and ta == tb: return True
    if norm_key(a.get("title","")) == norm_key(b.get("title","")): return True
    if ta and tb:
        na = re.sub(r"[^a-z0-9가-힣]", "", ta.lower())
        nb = re.sub(r"[^a-z0-9가-힣]", "", tb.lower())
        if na and nb:
            if na == nb: return True                                   # 띄어쓰기만 다른 경우
            if min(len(na), len(nb)) >= 5 and (na in nb or nb in na):  # "공항통신장애" ⊂ "미국공항통신장애"
                return True
            if min(len(na), len(nb)) >= 6:                             # 짧은 약칭(KT/SKT) 오병합 방지 가드
                A = {na[i:i+2] for i in range(len(na)-1)}
                B = {nb[i:i+2] for i in range(len(nb)-1)}
                if A and B and len(A & B) / len(A | B) >= 0.6: return True
    x, y = _tokens(a.get("title","")), _tokens(b.get("title",""))
    if x and y and len(x & y) / len(x | y) >= 0.55: return True
    return False

def update_archive(items):
    """중요 기사(4점 이상)를 발행일 기준으로 network_archive.json에 누적. 30일 보존.
    저장 전 이슈명·제목 유사도 기반 중복제거를 신규 기사와 기존 저장분 모두에 적용."""
    arch = {}
    if os.path.exists(ARCHIVE_FILE):
        try: arch = json.loads(open(ARCHIVE_FILE, encoding="utf-8").read())
        except Exception: arch = {}
    # 기존 저장분 소급 정리 (같은 날 + 전날 기사와 교차 비교, 중요도 높은 기사 우선 보존)
    days_sorted = sorted(arch.keys())
    for i, d in enumerate(days_sorted):
        prev = arch.get(days_sorted[i-1], []) if i > 0 else []
        cleaned = []
        for x in sorted(arch[d], key=lambda v: (v.get("importance",0), v.get("date","")), reverse=True):
            rep = next((y for y in cleaned if _arch_dup(x, y)), None) \
                  or next((y for y in prev if _arch_dup(x, y)), None)   # 전날 이미 실린 사건 포함
            if rep is not None:
                rep["rel"] = rep.get("rel", 1) + x.get("rel", 1)
                continue
            cleaned.append(x)
        arch[d] = cleaned
    added = 0
    for a in items:
        day = a["date"][:10]
        lst = arch.setdefault(day, [])
        try: prev_day = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception: prev_day = ""
        rep = next((x for x in lst if _arch_dup(a, x)), None) \
              or next((x for x in arch.get(prev_day, []) if _arch_dup(a, x)), None)
        if rep is not None:
            rep["rel"] = rep.get("rel", 1) + 1   # 같은 날짜·전날의 같은 사건은 대표에 관련 보도 수만 누적
            if "news.google.com" in rep.get("url","") and a.get("url") and "news.google.com" not in a["url"]:
                rep["url"] = a["url"]; rep["source"] = a.get("source", rep.get("source",""))
            continue
        rec = {k: a.get(k, "") for k in ("title","otitle","summary","source","date","url","category","importance","score","impact","sid","topic","subj","evt","tgt")}
        rec["rel"] = 1
        lst.append(rec)
        added += 1
    cutoff_day = (datetime.now(KST) - timedelta(days=ARCHIVE_DAYS)).strftime("%Y-%m-%d")
    arch = {d: v for d, v in arch.items() if d >= cutoff_day}
    for d in arch:
        arch[d].sort(key=lambda x: (x.get("importance",0), x.get("date","")), reverse=True)
    open(ARCHIVE_FILE, "w", encoding="utf-8").write(json.dumps(arch, ensure_ascii=False))
    print(f"아카이브 갱신: 신규 {added}건 / 보존 {len(arch)}일치")
    return arch

def gemini_brief(items):
    """중요 기사들을 화면용 개조식 브리핑(text)과 음성용 구어체 원고(script)로 변환"""
    if not items:
        none_msg = "최근 수집 구간에 중요도 4점 이상 기사가 없습니다."
        return {"text": none_msg, "script": none_msg + " 좋은 하루 되십시오."}
    flat = lambda s: re.sub(r"\s+", " ", str(s or "").replace("- ", "")).strip()
    fb_text = "\n".join(
        "- " + a["title"] + ((" — " + flat(a["summary"]).split(". ")[0].rstrip(".") + ".") if a.get("summary") else "")
        for a in items[:8])
    fb_script = ("안녕하십니까, 네트워크 뉴스 브리핑입니다. "
                 + " ".join(f"{i+1}번째 소식. {a['title']}. {flat(a['summary'])}" for i, a in enumerate(items[:8]))
                 + " 이상으로 브리핑을 마칩니다.")
    fallback = {"text": fb_text, "script": fb_script}
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return fallback
    model = "gemini-2.0-flash"
    if os.path.exists("gemini_model_nw.txt"):
        m = open("gemini_model_nw.txt", encoding="utf-8").read().strip()
        if m: model = m
    lines = [f"- [{a['importance']}점][{a['sid']}] {a['title']} :: {a['summary']}" for a in items[:15]]
    prompt = f"""당신은 삼성전자 네트워크사업부 임원 대상 뉴스 브리핑 작성자입니다. 아래 기간 내 중요 기사들로 두 가지 버전의 브리핑을 작성하세요.

브리핑에 포함할 기사 선별 기준 — 아래에 해당하는 기사만 다루고, 해당하지 않으면 중요도가 높아도 과감히 제외:
1. 경쟁사(에릭슨, 화웨이, 노키아, 마베니어, 원피니티, 후지쯔)의 신제품·신기술 발표
2. 주요 통신사의 인수합병, 경영진 교체, 인력 조정
3. 당사 고객사(버라이즌, SKT, KT, LG유플러스, KDDI, 도코모, 라쿠텐, 지오, 에어텔, 보다폰 아이디어, 텔러스, 비디오트론, 사스크텔, 보다폰)와 목표 고객사(AT&T, 티모바일, 소프트뱅크, 로저스, 벨, 텔레포니카, 도이치텔레콤, 오렌지)의 주요 전략 변화, 경쟁사와의 협력·장비 도입 발표
4. 한국, 미국, 일본, 인도, 유럽의 통신 정책 변화
5. 위성통신 시장의 주요 변화
선별 결과 다룰 기사가 없으면 text와 script 모두 경영진 보고 대상의 주요 소식이 없다는 취지의 한 문장으로 작성하세요.

반드시 아래 JSON 형식으로만 출력하고 다른 텍스트는 금지:
{{"text": "화면용 개조식 브리핑", "script": "음성용 구어체 원고"}}

text 작성 규칙 (화면에서 빠르게 읽는 문어체 개조식):
- 첫 줄은 "핵심: "으로 시작하는 한 줄 총평
- 이후 소식마다 한 항목씩 줄바꿈으로 구분: "- 회사명, 핵심 사실, 사업적 의미" 순서로 1~2줄, 명사형 종결(예: 출시 발표, 계약 체결, 대응 검토 필요)
- 전체 400자 내외, 중요도 높은 순, 마크다운·이모지 금지

script 작성 규칙 (음성 재생용 구어체 원고):
- 전체 700자 내외, 음성으로 약 2분 분량
- 뉴스 앵커의 차분하고 격식 있는 구어체 존댓말. 문장은 짧게 끊고 소식 사이에는 "다음 소식입니다", "이어서" 같은 전환어 사용
- 구성: 인사 및 기사 건수 한 문장, 중요도 높은 순으로 소식 전달(기사마다 핵심 사실과 사업적 의미를 2~3문장), 마무리 한 문장
- 순수한 문장만: 특수문자, 괄호, 불릿, 이모지 금지. 영문 회사명·약어는 한글 발음으로(에릭슨, 버라이즌, 에프씨씨), 숫자·단위도 읽히는 대로
- 두 버전 모두 제공된 기사 내용 범위에서만 작성하고 추측 금지

기사 목록:
{chr(10).join(lines)}"""
    payload = {"contents":[{"parts":[{"text":prompt}]}],
               "generationConfig":{"response_mime_type":"application/json","temperature":0.3}}
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        r = gemini_call(url, payload)
        raw = r["candidates"][0]["content"]["parts"][0]["text"].strip()
        j = json.loads(raw)
        text = str(j.get("text","")).strip()
        script = re.sub(r"\s+", " ", str(j.get("script","")).strip())
        if len(text) > 30 and len(script) > 30:
            print(f"브리핑 원고 생성 완료 (개조식 {len(text)}자 / 구어체 {len(script)}자)")
            return {"text": text, "script": script}
    except Exception as ex:
        print(f"경고: 브리핑 생성 실패 - {ex} -> 단순 원고로 대체")
    return fallback

# ── 기사별 사업 영향 분석 (대시보드에서 기사 클릭 시 표시) ──
IMPACT_FILE, IMPACT_MIN_IMP, IMPACT_MAX_PER_RUN, IMPACT_TTL_DAYS = "impact_cache_network.json", 3, 40, 14
IMPACT_AREAS = ["수주·영업", "제품·기술", "가격·수익성", "공급망·제재", "고객관계", "규제·정책", "경쟁구도", "위성·신사업"]

def _impact_key(a):
    return (a.get("url") or a.get("title") or "")[:300]

def gemini_impact(groups):
    """중요도 3점(50점) 이상 기사에 대해 '왜 사업적 영향이 있는지 / 어느 분야에 어떻게 / 필요한 조치'를 생성.
    결과는 URL 기준으로 캐시(14일)하여 매 크롤마다 같은 기사를 다시 분석하지 않음.
    groups: 기사 dict 리스트들의 리스트 (같은 기사의 복제본이 여러 목록에 있어도 캐시로 모두 채움)"""
    cache = {}
    if os.path.exists(IMPACT_FILE):
        try: cache = json.loads(open(IMPACT_FILE, encoding="utf-8").read())
        except Exception: cache = {}
    now = datetime.now(KST)
    cutoff = (now - timedelta(days=IMPACT_TTL_DAYS)).strftime("%Y-%m-%d")
    cache = {k: v for k, v in cache.items() if v.get("t", "") >= cutoff}

    todo, seen = [], set()
    for lst in groups:
        for a in lst:
            k = _impact_key(a)
            if not k or k in cache or k in seen: continue
            if a.get("importance", 0) < IMPACT_MIN_IMP: continue
            seen.add(k); todo.append(a)
    todo.sort(key=lambda a: (a.get("score", 0), a.get("date", "")), reverse=True)
    todo = todo[:IMPACT_MAX_PER_RUN]

    key = os.environ.get("GEMINI_API_KEY")
    if todo and key:
        model = "gemini-2.0-flash"
        if os.path.exists("gemini_model_nw.txt"):
            m = open("gemini_model_nw.txt", encoding="utf-8").read().strip()
            if m: model = m
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        done = 0
        for ci in range(0, len(todo), 10):
            batch = todo[ci:ci+10]
            flat = lambda s: re.sub(r"\s+", " ", str(s or "")).strip()[:400]
            lines = [f"{i} | [{a.get('score',0)}점/{a.get('sid','')}] {a['title']} | {flat(a.get('summary',''))}"
                     for i, a in enumerate(batch)]
            prompt = f"""당신은 삼성전자 네트워크사업부(기지국·vRAN·Open RAN·5G/6G 장비, 주요 고객: 버라이즌·SKT·KT·LG유플러스·KDDI·도코모·라쿠텐·지오·에어텔·보다폰·텔러스 등, 주요 경쟁사: 에릭슨·노키아·화웨이·ZTE·마베니어) CFO 조직의 경쟁정보(CI) 분석가입니다.
아래 각 기사(번호 | 점수/섹션 | 제목 | 요약)에 대해 삼성전자 네트워크사업 관점의 사업 영향 분석을 작성하세요. 100% 한국어, 개조식(명사형 종결), 추측은 "가능성"·"예상"으로 표시하고 기사 범위를 벗어난 단정 금지.

각 기사별 출력 필드:
- why: 이 기사가 당사에 사업적으로 중요한 이유. 불릿 2~3개, 각 60자 이내, "- "로 시작하고 줄바꿈(\\n)으로 구분. 기사 속 수치·금액·규모를 포함.
- areas: 영향 받는 분야 2~4개. 분야명(a)은 반드시 다음 중에서만: {", ".join(IMPACT_AREAS)}. 방향(d)은 "+"(당사에 기회), "-"(당사에 위협/부정), "±"(혼재·관찰 필요) 중 하나. 설명(t)은 그 분야에 구체적으로 어떤 영향이 어떻게 예상되는지 한 줄(70자 이내).
- actions: 당사가 취해야 할 조치 2~3개, 각 60자 이내. 담당 조직 관점이 드러나게 구체적으로(예: "버라이즌 vRAN 2차 물량 협상 시 가격 방어 논리 준비", "에릭슨 신제품 스펙 비교표 작성 후 영업 공유"). 단순 "모니터링"만으로 끝내지 말 것.
- horizon: 영향 시점 — "즉시", "단기(3개월)", "중기(1년)", "장기" 중 하나.
- conf: 분석 확신도 — "높음", "중간", "낮음" 중 하나(기사 정보가 제목뿐이거나 루머 단계면 낮음).

JSON 배열만 출력: [{{"i":0,"why":"- ...\\n- ...","areas":[{{"a":"수주·영업","d":"-","t":"..."}}],"actions":["...","..."],"horizon":"단기(3개월)","conf":"중간"}}]

기사 목록:
{chr(10).join(lines)}"""
            payload = {"contents":[{"parts":[{"text":prompt}]}],
                       "generationConfig":{"response_mime_type":"application/json","temperature":0.2}}
            try:
                r = gemini_call(url, payload, timeout=180)
                raw = r["candidates"][0]["content"]["parts"][0]["text"]
                try: judged = json.loads(raw)
                except Exception:
                    judged = []
                    for mm in re.finditer(r"\{(?:[^{}]|\{[^{}]*\})*\}", raw):
                        try: judged.append(json.loads(mm.group()))
                        except Exception: pass
                for j in judged:
                    try: idx = int(j["i"])
                    except Exception: continue
                    if not (0 <= idx < len(batch)): continue
                    why = str(j.get("why", "")).strip()
                    areas = []
                    for ar in (j.get("areas") or [])[:4]:
                        if not isinstance(ar, dict): continue
                        nm = str(ar.get("a", "")).strip()
                        if nm not in IMPACT_AREAS:   # 유사 표기 보정
                            nm = next((x for x in IMPACT_AREAS if nm and (nm in x or x.split("·")[0] in nm)), nm[:8])
                        d = str(ar.get("d", "±")).strip()
                        if d not in ("+", "-", "±"): d = "±"
                        areas.append({"a": nm, "d": d, "t": str(ar.get("t", "")).strip()[:120]})
                    actions = [str(x).strip()[:120] for x in (j.get("actions") or []) if str(x).strip()][:3]
                    if len(why) < 10 and not areas: continue
                    cache[_impact_key(batch[idx])] = {
                        "t": now.strftime("%Y-%m-%d"), "why": why[:600], "areas": areas, "actions": actions,
                        "horizon": str(j.get("horizon", "")).strip()[:12], "conf": str(j.get("conf", "")).strip()[:4]}
                    done += 1
            except Exception as ex:
                print(f"경고: 영향 분석 실패({model}, {ci//10+1}묶음) - {ex}")
                break
            if ci + 10 < len(todo):
                import time; time.sleep(3)
        print(f"영향 분석 생성: {done}/{len(todo)}건 (캐시 보유 {len(cache)}건)")
    elif todo:
        print("안내: GEMINI_API_KEY 없음 - 영향 분석 생략")

    filled = 0
    for lst in groups:
        for a in lst:
            c = cache.get(_impact_key(a))
            if c:
                a["impact"] = {k: c[k] for k in ("why","areas","actions","horizon","conf") if k in c}; filled += 1
    try: open(IMPACT_FILE, "w", encoding="utf-8").write(json.dumps(cache, ensure_ascii=False))
    except Exception as ex: print(f"경고: 영향 분석 캐시 저장 실패 - {ex}")
    return filled

# ── 기업별 히스토리 누적 (company.html용) ──
HIST_FILE, HIST_DAYS, HIST_MIN_IMP, HIST_PER_CAP = "company_history.json", 400, 3, 150
HIST_GROUPS = {
    "경쟁사": ["에릭슨","노키아","화웨이","zte"],
    "위성·유관": ["스타링크","ast","아마존"],
    "고객사": ["버라이즌","kddi","도코모","skt","kt","lg유플러스","지오"],
}
HIST_COMPANIES = {c for v in HIST_GROUPS.values() for c in v}

def update_company_history(items):
    """대상 기업의 중요도 3점 이상 기사를 회사별로 400일 누적 (같은 사건은 1건만)"""
    hist = {}
    if os.path.exists(HIST_FILE):
        try: hist = json.loads(open(HIST_FILE, encoding="utf-8").read())
        except Exception: hist = {}
    def day_diff(d1, d2):
        try:
            a = datetime.strptime(d1[:10], "%Y-%m-%d"); b = datetime.strptime(d2[:10], "%Y-%m-%d")
            return abs((a - b).days)
        except Exception: return 999
    def dup(e, x):
        if e["evt"] and e["evt"] == x.get("evt","") and day_diff(e["date"], x.get("date","")) <= 2:
            ta, tb = e.get("tgt",""), x.get("tgt","")
            if not (ta and tb and ta != tb): return True   # 같은 사건 유형·같은 시기·대상 미상충
        if day_diff(e["date"], x.get("date","")) <= 7:
            a, b = _tokens(e["title"]), _tokens(x.get("title",""))
            if a and b and len(a & b) / len(a | b) >= 0.55: return True
        return False
    added = 0
    for a in items:
        c = a.get("subj","")
        if c not in HIST_COMPANIES: continue
        e = {"date": a["date"], "title": a["title"][:90], "otitle": a.get("otitle",""), "url": a.get("url",""),
             "source": a.get("source","")[:40], "evt": a.get("evt","") or "기타",
             "tgt": a.get("tgt",""), "imp": a.get("importance",3), "kw": a.get("kw",""),
             "sum": re.sub(r"\s+"," ", a.get("summary",""))[:160]}
        lst = hist.setdefault(c, [])
        rep = next((x for x in lst if dup(e, x)), None)
        if rep is not None:
            if e["imp"] > rep.get("imp",0):  # 같은 사건의 더 중요한 보도면 교체 (날짜·시드 표식은 유지)
                keep = {k: rep[k] for k in ("date","seed") if k in rep}
                rep.update(e); rep.update(keep)
            continue
        lst.append(e); added += 1
    cutoff = (datetime.now(KST) - timedelta(days=HIST_DAYS)).strftime("%Y-%m-%d")
    for c in list(hist.keys()):
        lst = [x for x in hist[c] if x.get("date","")[:10] >= cutoff]
        lst.sort(key=lambda x: x.get("date",""), reverse=True)
        if len(lst) > HIST_PER_CAP:   # 상한 초과 시 오래된 것 중 중요도 낮은 순으로 정리
            keep = sorted(lst, key=lambda x: (x.get("imp",0), x.get("date","")), reverse=True)[:HIST_PER_CAP]
            lst = sorted(keep, key=lambda x: x.get("date",""), reverse=True)
        hist[c] = lst
    open(HIST_FILE, "w", encoding="utf-8").write(json.dumps(hist, ensure_ascii=False))
    print(f"기업 히스토리 갱신: 신규 {added}건 / {sum(len(v) for v in hist.values())}건 보존")
    return hist

# ── 상장사 주가 수집 (기업 페이지 이벤트 차트용) ──
STOCK_FILE, STOCK_TTL_H = "stock_prices.json", 20
STOCK_TICKERS = {   # subj 키 → (야후 티커, 기본 통화)
    "에릭슨": ("ERIC","USD"), "노키아": ("NOK","USD"), "zte": ("0763.HK","HKD"),
    "버라이즌": ("VZ","USD"), "kddi": ("9433.T","JPY"), "ast": ("ASTS","USD"),
    "skt": ("017670.KS","KRW"), "kt": ("030200.KS","KRW"), "lg유플러스": ("032640.KS","KRW"),
}
STOOQ_MAP = {"ERIC":"eric.us","NOK":"nok.us","VZ":"vz.us","ASTS":"asts.us","9433.T":"9433.jp","0763.HK":"0763.hk"}

def update_stock_prices():
    """상장 9개사의 1년 일별 종가를 하루 1회 갱신 (Yahoo 1차, Stooq 2차). 실패 시 기존 캐시 유지"""
    data = {"fetched_at": "", "series": {}}
    if os.path.exists(STOCK_FILE):
        try: data = json.loads(open(STOCK_FILE, encoding="utf-8").read())
        except Exception: pass
    try:
        last = datetime.strptime(data.get("fetched_at",""), "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        if datetime.now(KST) - last < timedelta(hours=STOCK_TTL_H) and data.get("series"):
            return data
    except Exception: pass
    import time
    ok, fail = 0, 0
    for comp, (tk, curr) in STOCK_TICKERS.items():
        series = None
        try:  # 1차: Yahoo Finance chart API
            req = urllib.request.Request(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(tk)}?range=1y&interval=1d",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=25) as r:
                res = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]
            ts = res.get("timestamp") or []
            cl = res["indicators"]["quote"][0].get("close") or []
            pts = [(datetime.fromtimestamp(t, tz=KST).strftime("%Y-%m-%d"), round(c, 3))
                   for t, c in zip(ts, cl) if c is not None]
            if len(pts) >= 30:
                series = {"ticker": tk, "currency": res.get("meta", {}).get("currency") or curr,
                          "dates": [p[0] for p in pts], "closes": [p[1] for p in pts]}
        except Exception as ex:
            print(f"  주가 Yahoo 실패({tk}): {str(ex)[:80]}")
        if series is None and tk in STOOQ_MAP:
            try:  # 2차: Stooq CSV
                req = urllib.request.Request(f"https://stooq.com/q/d/l/?s={STOOQ_MAP[tk]}&i=d",
                                             headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    lines = r.read().decode("utf-8").strip().splitlines()[1:]
                pts = []
                for ln in lines[-260:]:
                    f = ln.split(",")
                    if len(f) >= 5 and f[4]: pts.append((f[0], round(float(f[4]), 3)))
                if len(pts) >= 30:
                    series = {"ticker": tk, "currency": curr,
                              "dates": [p[0] for p in pts], "closes": [p[1] for p in pts]}
            except Exception as ex:
                print(f"  주가 Stooq 실패({tk}): {str(ex)[:80]}")
        if series:
            series["dates"] = series["dates"][-260:]; series["closes"] = series["closes"][-260:]
            data.setdefault("series", {})[comp] = series; ok += 1
        else:
            fail += 1  # 실패 시 기존 캐시 항목 유지
        time.sleep(1)
    data["fetched_at"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    try:
        open(STOCK_FILE, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
    except Exception: pass
    print(f"주가 갱신: 성공 {ok} / 실패 {fail} (캐시 보존)")
    return data

BRIEF_FILE, BRIEF_KEEP = "briefings_network.json", 28  # 하루 2회 x 14일

def update_briefings(pool):
    """06시·17시 첫 크롤에서 그 사이 수집된 4점 이상 기사로 텍스트 브리핑 회차 생성·누적"""
    briefs = []
    if os.path.exists(BRIEF_FILE):
        try: briefs = json.loads(open(BRIEF_FILE, encoding="utf-8").read())
        except Exception: briefs = []
    now = datetime.now(KST)
    # 캐치업 방식: 가장 최근에 지난 발행 시각(06시/17시)의 회차가 아직 없으면,
    # 크롤이 몇 시에 돌든 그 회차를 소급 생성 (스케줄 지연으로 창을 놓쳐도 결번 없음)
    t6 = now.replace(hour=6, minute=0, second=0, microsecond=0)
    t17 = now.replace(hour=17, minute=0, second=0, microsecond=0)
    if now >= t17:
        slot = ("이브닝", t17, t6)
    elif now >= t6:
        slot = ("모닝", t6, t17 - timedelta(days=1))
    else:
        slot = ("이브닝", t17 - timedelta(days=1), t6 - timedelta(days=1))
    if not briefs and slot is None:
        slot = ("초기", now, now - timedelta(hours=24))
    label, end_dt, start_dt = slot
    bid = end_dt.strftime("%Y-%m-%d %H:%M") + " " + label
    if any(b.get("id") == bid for b in briefs): return briefs
    items = []
    for a in pool:
        if a["importance"] < ARCHIVE_MIN_IMP: continue
        try: dt = datetime.strptime(a["date"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except Exception: continue
        if start_dt <= dt <= end_dt + timedelta(minutes=40): items.append(a)
    items.sort(key=lambda a: (a["importance"], a["date"]), reverse=True)
    res = gemini_brief(items)
    briefs.insert(0, {"id": bid, "label": end_dt.strftime("%m.%d") + " " + label,
                      "window": start_dt.strftime("%m.%d %H:%M") + " ~ " + end_dt.strftime("%m.%d %H:%M"),
                      "generated": now.strftime("%Y-%m-%d %H:%M"), "n": len(items),
                      "text": res["text"], "script": res["script"]})
    del briefs[BRIEF_KEEP:]
    open(BRIEF_FILE, "w", encoding="utf-8").write(json.dumps(briefs, ensure_ascii=False))
    print(f"브리핑 신규 생성: {bid} (대상 {len(items)}건)")
    return briefs

def main():
    pool, engine = crawl()
    data = {}
    for sid, name, _ in SEC_DEFS:
        items = [a for a in pool if a["sid"] == sid]
        items.sort(key=lambda a: (a["importance"], a["date"]), reverse=True)  # 중요도 -> 최신순
        items = dedupe_topics(items)
        cap = 60 if sid == "carrier" else 30   # 통신사는 지역 필터용으로 넉넉히 보관
        data[sid] = [{k: a.get(k, "") for k in ("title","otitle","summary","source","date","url","category","importance","score","impact","wl","topic","subj","evt","tgt","rel")} for a in items[:cap]]
        print(f"{name}: {len(items)}건 -> {len(data[sid])}건")
    # 최신 탭: 소규모 장애(outage & imp<5)는 제외 — 대규모 장애(imp=5)만 주요·최신에 노출
    latest = sorted([a for a in pool if not (a["sid"] == "outage" and a["importance"] < 5)],
                    key=lambda a: a["date"], reverse=True)
    latest = dedupe_topics(latest)[:LATEST_N]
    latest = [{k: a.get(k, "") for k in ("title","otitle","summary","source","date","url","category","importance","score","impact","sid","topic","subj","evt","tgt","rel")} for a in latest]
    arch_items = [{k: a.get(k, "") for k in ("title","otitle","summary","source","date","url","category","importance","score","impact","sid","topic","subj","evt","tgt")}
                  for a in pool if a["importance"] >= ARCHIVE_MIN_IMP]
    hist_items = [{k: a.get(k, "") for k in ("title","otitle","summary","source","date","url","importance","subj","evt","tgt","kw")}
                  for a in pool if a.get("subj","") in HIST_COMPANIES and a["importance"] >= HIST_MIN_IMP]
    shown = [a for arr in data.values() for a in arr] + latest + arch_items + hist_items
    resolve_google_links(shown)
    company_hist = update_company_history(hist_items)
    archive = update_archive(arch_items)
    # 기사별 사업 영향 분석: 현재 노출 기사 + 일자별 아카이브(캐시 기준으로 채움, 신규 분석은 1회 40건 한도)
    gemini_impact(list(data.values()) + [latest] + list(archive.values()))
    for d in archive:   # 아카이브 레코드에 score가 없던 구형 저장분 보정
        for a in archive[d]:
            if not isinstance(a.get("score"), int): a["score"] = default_score(a.get("importance", 3))
    try: open(ARCHIVE_FILE, "w", encoding="utf-8").write(json.dumps(archive, ensure_ascii=False))
    except Exception: pass
    # 과거 저장분(일자별 탭)에 남은 미변환 구글 링크도 소급 변환 후 재저장
    arch_flat = [a for arr in archive.values() for a in arr]
    if any("news.google.com" in a.get("url","") for a in arch_flat):
        resolve_google_links(arch_flat)
        try:
            open(ARCHIVE_FILE, "w", encoding="utf-8").write(json.dumps(archive, ensure_ascii=False))
        except Exception: pass
    briefs = update_briefings(pool)
    meta = {"generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M") + " · " + engine,
            "sections": [{"id": s[0], "name": s[1]} for s in [next(x for x in SEC_DEFS if x[0]==o) for o in ORDER]]}
    js = ("const NEWS_META = " + json.dumps(meta, ensure_ascii=False) + ";\n"
          + "const NEWS_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n"
          + "const NEWS_LATEST = " + json.dumps(latest, ensure_ascii=False) + ";\n"
          + "const NEWS_ARCHIVE = " + json.dumps(archive, ensure_ascii=False) + ";\n")
    js = js.replace("</", "<\\/")  # 기사 내용에 </script> 유사 문자열이 있어도 스크립트가 깨지지 않게
    js = js.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")  # 기사 속 특수 줄구분 문자로 스크립트가 깨지는 것 방지
    tpl = open("dashboard_network.html", encoding="utf-8").read()
    inj = "<script>\n" + js + "</script>"
    # 자리표시자 주변 공백·따옴표 차이를 허용하는 주입
    out, n = re.subn(r'<script\s+src=["\']news_data\.js["\']\s*>\s*</script>', lambda m: inj, tpl, count=1)
    if n == 0:
        # 자리표시자가 손상된 경우에도 데이터가 뜨도록 </head> 앞에 강제 주입
        if "</head>" in tpl:
            out = tpl.replace("</head>", inj + "\n</head>", 1)
        else:
            out = inj + tpl
        print("경고: news_data.js 자리표시자를 찾지 못해 head에 데이터를 강제 주입 (dashboard_network.html 확인 필요)")
    else:
        print("데이터 주입 완료")
    os.makedirs("docs", exist_ok=True)
    open("docs/network.html","w",encoding="utf-8").write(out)
    print("완료 -> docs/network.html 생성")
    # ── 브리핑 페이지 생성 ──
    bjs = ("const NEWS_BRIEFINGS = " + json.dumps(briefs, ensure_ascii=False) + ";\n").replace("</", "<\\/")
    bjs = bjs.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    binj = "<script>\n" + bjs + "</script>"
    btpl = open("dashboard_briefing.html", encoding="utf-8").read()
    bout, bn = re.subn(r'<script\s+src=["\']news_data\.js["\']\s*>\s*</script>', lambda m: binj, btpl, count=1)
    if bn == 0:
        bout = btpl.replace("</head>", binj + "\n</head>", 1) if "</head>" in btpl else binj + btpl
        print("경고: briefing 자리표시자를 찾지 못해 head에 강제 주입")
    open("docs/briefing.html","w",encoding="utf-8").write(bout)
    print("완료 -> docs/briefing.html 생성")
    # ── 기업 히스토리 페이지 생성 ──
    cmeta = {"generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"), "groups": HIST_GROUPS}
    cfin = {}
    if os.path.exists("company_financials.json"):
        try: cfin = json.loads(open("company_financials.json", encoding="utf-8").read())
        except Exception: print("경고: company_financials.json 파싱 실패")
    cstock = update_stock_prices()
    cjs = ("const COMPANY_META = " + json.dumps(cmeta, ensure_ascii=False) + ";\n"
           + "const COMPANY_DATA = " + json.dumps(company_hist, ensure_ascii=False) + ";\n"
           + "const COMPANY_FIN = " + json.dumps(cfin, ensure_ascii=False) + ";\n"
           + "const COMPANY_STOCK = " + json.dumps(cstock, ensure_ascii=False) + ";\n").replace("</", "<\\/")
    cjs = cjs.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    cinj = "<script>\n" + cjs + "</script>"
    ctpl = open("dashboard_company.html", encoding="utf-8").read()
    cout, cn = re.subn(r'<script\s+src=["\']news_data\.js["\']\s*>\s*</script>', lambda m: cinj, ctpl, count=1)
    if cn == 0:
        cout = ctpl.replace("</head>", cinj + "\n</head>", 1) if "</head>" in ctpl else cinj + ctpl
        print("경고: company 자리표시자를 찾지 못해 head에 강제 주입")
    open("docs/company.html","w",encoding="utf-8").write(cout)
    print("완료 -> docs/company.html 생성")

if __name__ == "__main__":
    main()
