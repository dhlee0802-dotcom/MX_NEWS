# -*- coding: utf-8 -*-
"""
MX 뉴스 크롤러 - GitHub Actions판
로컬 PowerShell v3와 동일 기능: 매체 RSS + Google News 수집, 전역 중복제거,
섹션 분류, exclude.txt 제외, Gemini 배치 판정(분류·중요도·한국어 요약), 최신 20건.
결과: docs/index.html (데이터 내장 단일 파일, GitHub Pages로 서빙)
필요 환경변수: GEMINI_API_KEY (없으면 키워드 분류로 동작)
"""
import feedparser, json, re, html, os, sys
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

HOURS, PER, LATEST_N = 48, 5, 20
KST = timezone(timedelta(hours=9))

FEEDS = [
    ("WSJ Tech", "https://feeds.a.dj.com/rss/RSSWSJD.xml"),
    ("Financial Times Tech", "https://www.ft.com/technology?format=rss"),
    ("Nikkei Asia", "https://asia.nikkei.com/rss/feed/nar"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("9to5Mac", "https://9to5mac.com/feed/"),
    ("MacRumors", "https://feeds.macrumors.com/MacRumors-All"),
    ("Android Authority", "https://www.androidauthority.com/feed/"),
    ("ZDNet", "https://www.zdnet.com/news/rss.xml"),
    ("CNET", "https://www.cnet.com/rss/news/"),
    ("전자신문", "https://rss.etnews.com/Section901.xml"),
    ("ZDNet Korea", "https://feeds.feedburner.com/zdkorea"),
    ("한국경제 IT", "https://rss.hankyung.com/feed/it.xml"),
    ("매일경제 IT", "https://www.mk.co.kr/rss/50300009/"),
]
GN_QUERIES = [
    "삼성 갤럭시", "Samsung Galaxy", "iPhone OR iPad OR Apple Watch",
    "DRAM NAND memory price", "메모리 가격 스마트폰",
    "Xiaomi OR OPPO OR vivo OR Honor smartphone shipment",
    "smart glasses OR XR headset", "Samsung Wallet OR Samsung Pay",
    "갤럭시 워치 OR 갤럭시 버즈", "갤럭시 북 OR 갤럭시 탭",
    "삼성월렛 OR 삼성페이 OR 삼성헬스", "중국 스마트폰 판매량 OR 출하량",
    "스마트 글래스 OR XR 헤드셋",
]
NAVER_QUERIES = [
    "갤럭시 스마트폰", "갤럭시 워치", "갤럭시 버즈", "갤럭시 탭", "갤럭시 북",
    "삼성월렛", "삼성헬스", "메모리 가격", "중국 스마트폰 판매", "스마트 글래스 XR",
]

SEC_DEFS = [  # 검사 순서 = 분류 우선순위
    ("memchina", "[CN] 메모리·中폰", ["dram","nand","메모리 가격","memory price","memory chip","낸드","d램"]),
    ("glasses", "[XR] AI Glasses", ["glasses","글래스","스마트 안경","xr","vision pro","quest","헤드셋","headset"]),
    ("watch", "[Watch] 워치", ["watch","워치","smartwatch","갤럭시 워치"]),
    ("tws", "[TWS] TWS", ["buds","airpods","버즈","이어폰","earbud","headphone","tws"]),
    ("tablet", "[Tab] 태블릿", ["ipad","tablet","태블릿","갤럭시 탭","galaxy tab"]),
    ("pc", "[PC] PC", ["laptop","notebook","노트북","macbook","갤럭시 북","galaxy book","chromebook","맥북"]),
    ("wallet", "[Pay] 삼성월렛", ["월렛","wallet","삼성페이","samsung pay","apple pay","google pay","간편결제"]),
    ("health", "[Health] 헬스", ["삼성헬스","samsung health","apple health","fitbit","oura","헬스케어","health app","피트니스","fitness"]),
    ("phone", "[Phone] 스마트폰", ["스마트폰","smartphone","갤럭시 s","갤럭시 z","galaxy s","galaxy z","galaxy a","iphone","아이폰","pixel","폴더블","foldable","휴대폰","단말기"]),
]
ORDER = ["phone","tablet","pc","watch","tws","glasses","wallet","health","memchina"]
VALID_IDS = [s[0] for s in SEC_DEFS]

CATS = [
    ("quality", ["결함","리콜","불량","발화","recall","defect","lawsuit","소송","취약점","vulnerability","버그","bug","hack","오류","issue"]),
    ("launch", ["출시","공개","발표","unveil","launch","release","announce","신제품","new model","첫 공개"]),
    ("price", ["가격","인상","인하","판매량","출하량","점유율","price","hike","shipment","sales","market share","할인","discount"]),
    ("exec", ["사장","부사장","ceo","임원","인사","교체","appoint","resign","executive","조직개편"]),
    ("policy", ["정책","규제","관세","제재","tariff","regulation","ban","정부","policy","약관","업데이트 종료"]),
    ("community", ["reddit","루리웹","커뮤니티","여론"]),
]
CRIT = ["리콜","발화","recall","fire","exploding","전량","소송","lawsuit","금지","ban"]
HIGH = ["삼성","samsung","galaxy","갤럭시","인상","hike","ceo","관세","tariff","점유율"]

CN_MAKERS = ["xiaomi","oppo","vivo","honor","화웨이","huawei","샤오미","중국 스마트폰"]
CN_VOL = ["shipment","출하","판매량","점유율","market share","sales"]

def clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    return re.sub(r"\s+", " ", s).strip()

def norm_key(title):
    return re.sub(r"[^a-z0-9가-힣]", "", title.lower())[:40]

def section_id(text):
    t = text.lower()
    if any(k in t for k in CN_MAKERS) and any(k in t for k in CN_VOL):
        return "memchina"
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
    t = text.lower(); s = 2
    if any(k.lower() in t for k in CRIT): s += 2
    if any(k.lower() in t for k in HIGH): s += 1
    return min(5, s)

def load_lines(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return [l.strip().lower() for l in f if l.strip()]
    return []

def crawl():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    exclude = load_lines("exclude.txt")
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
            "url": link, "category": category(blob), "importance": score(blob), "wl": wl, "topic": "", "alert": False,
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
    # 후보 전체를 150건씩 묶어 전부 판정 (최대 600건). 250건 초과분이 판정 없이 남던 문제 해결
    chunks = [pool[i:i+150] for i in range(0, min(len(pool), 600), 150)]

    def build_payload(batch):
        lines = [f"{i} | {b['title']} | {b['summary'][:220]} | {b['source']}" for i, b in enumerate(batch)]
        prompt = f"""당신은 삼성전자 MX사업부 경쟁정보(CI) 분석가입니다. 아래 기사 목록(번호|제목|요약|매체)을 각각 판정하세요.

섹션(sec): phone(스마트폰) tablet(태블릿) pc(노트북/PC) watch(스마트워치) tws(무선이어폰) glasses(XR/AI글래스) wallet(결제/월렛) health(디지털헬스) memchina(메모리 가격·중국 스마트폰 제조사 동향) none(MX사업과 무관→제외)
카테고리(cat): quality launch price exec policy community other
중요도(imp): 5=삼성 MX에 즉각 대응 필요한 긴급, 4=경영진 보고 필요, 3=주시, 2=참고, 1=단순 정보
알람(alert): true/false. 다음 세 조건을 모두 충족할 때만 true — ① 루머·전망·유출이 아닌 실제 발생 사건(공식 발표·판결·사고·조치), ② 삼성 MX사업에 직접 영향(당사 제품 품질·안전 사고/리콜, 당사 대상 소송·규제·판매금지, 당사 제품·서비스 보안사고, 관세·수출규제 등 정부 조치 확정), ③ 당일 인지가 필요한 시급성. 신제품 유출·루머·리뷰·전망·점유율 통계·예고된 이벤트는 중요도가 높아도 반드시 false. 확실하지 않으면 false.
이슈(topic): 기사가 다루는 핵심 사건을 나타내는 짧은 한국어 이슈명. 반드시 "제품명 사건" 형식으로, 제품명을 첫 단어로 동일하게 표기할 것(예: "북6 출시", "북6 리뷰", "북6 가격" — 제품명 표기는 전부 통일). 같은 사건을 다룬 기사는 제목 표현·매체·언어가 달라도 반드시 한 글자도 다르지 않은 동일 이슈명을 부여. 같은 제품의 출시·공개·발표·리뷰 보도는 원칙적으로 하나의 이슈로 묶을 것. 이슈명이 같으면 중복으로 간주되어 1건만 표시됨.
요약(sum): 반드시 100% 한국어로만 작성 — 영어 문장이나 영어 원문 요약을 그대로 넣는 것은 오답이며, 외국어 기사는 한국어로 번역해 요약. 4~5문장 300자 내외로, 핵심 사실 → 배경·수치 → 경쟁 구도 → 사업적 의미 순으로 충실히 작성. 제공된 제목·요약 범위 내에서만 작성하고 추측 금지. 제공 정보가 제목뿐이면 억지로 늘리지 말고 짧게 유지.

모든 기사에 대해 빠짐없이 JSON 배열만 출력: [{{"i":0,"sec":"phone","cat":"launch","imp":3,"alert":false,"topic":"언팩 초청장","sum":"..."}}]

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
            item["alert"] = (j.get("alert") is True)
            applied += 1
        return applied

    last_good = None
    if os.path.exists("gemini_model.txt"):
        last_good = open("gemini_model.txt", encoding="utf-8").read().strip()
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
            open("gemini_model.txt","w",encoding="utf-8").write(model)
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
    """1차: 동일 이슈명 1건만. 2차: 이슈명 단어가 50% 이상 겹치면 같은 사건으로 간주.
       3차: 제목 단어가 55% 이상 겹치면 중복. (한/영 혼재·묶음 분할로 이슈명이 갈리는 경우 대비)"""
    out, seen_topics, kept_topic_toks, kept_title_toks = [], set(), [], []
    for a in items:
        t = a.get("topic","")
        if t and t in seen_topics: continue
        ttk = _tokens(t) if t else set()
        dup = False
        if ttk:
            for x in kept_topic_toks:
                if x and len(ttk & x) / max(1, min(len(ttk), len(x))) >= 0.6: dup = True; break
        if not dup:
            tk = _tokens(a.get("title",""))
            for x in kept_title_toks:
                if len(tk & x) / max(1, len(tk | x)) >= 0.55: dup = True; break
        if dup: continue
        if t: seen_topics.add(t)
        kept_topic_toks.append(ttk)
        kept_title_toks.append(_tokens(a.get("title","")))
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

def notify_urgent(pool):
    """중요도 5 신규 기사를 ntfy 푸시로 알림 (NTFY_TOPIC 미설정 시 생략, 회차당 최대 3건)"""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic: return
    urgent = [a for a in pool if a.get("alert") and a.get("sid") in VALID_IDS]
    if not urgent: return
    notified = set()
    if os.path.exists("notified.txt"):
        with open("notified.txt", encoding="utf-8") as f:
            notified = set(l.strip() for l in f if l.strip())
    sent = 0
    with open("notified.txt", "a", encoding="utf-8") as f:
        for a in urgent:
            key = norm_key(a["title"])
            if key in notified: continue
            f.write(key + "\n"); notified.add(key)
            if sent >= 3: continue   # 알림 폭주 방지 (기록은 남기되 전송은 3건까지)
            try:
                body = f"🚨 [긴급] {a['title']}\n{a.get('summary','')[:120]}\n{a['source']} · {a['date']}\n{a['url']}"
                req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), method="POST")
                req.add_header("Priority", "high")
                urllib.request.urlopen(req, timeout=15)
                sent += 1
            except Exception as ex:
                print(f"경고: 알림 전송 실패 - {ex}")
    if sent: print(f"긴급 알림 전송: {sent}건")

def semantic_dedupe(data, latest):
    """화면 표시 대상 기사 제목을 Gemini에 보내 같은 사건끼리 그룹핑 -> 목록별로 그룹당 1건만 유지"""
    key = os.environ.get("GEMINI_API_KEY")
    if not key: return
    lists = list(data.values()) + [latest]
    title_idx, order = {}, []
    for lst in lists:
        for a in lst:
            t = a["title"]
            if t not in title_idx:
                title_idx[t] = len(order); order.append(t)
    if len(order) < 2: return
    prompt = ("아래 뉴스 제목 목록에서 같은 사건·발표·조치를 다룬 기사끼리 동일한 그룹 번호(g)를 부여하세요. "
              "언어(한국어/영어)나 표현이 달라도 같은 사건이면 반드시 같은 그룹. 확실히 같은 사건일 때만 묶고 애매하면 다른 그룹. "
              "모든 번호에 대해 JSON 배열만 출력: [{\"i\":0,\"g\":1},{\"i\":1,\"g\":1},{\"i\":2,\"g\":2}]\n\n"
              + "\n".join(f"{i} | {t}" for i, t in enumerate(order)))
    payload = {"contents":[{"parts":[{"text":prompt}]}],
               "generationConfig":{"response_mime_type":"application/json","temperature":0}}
    model = "gemini-2.5-flash-lite"
    if os.path.exists("gemini_model.txt"):
        model = open("gemini_model.txt", encoding="utf-8").read().strip() or model
    try:
        r = gemini_call(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}", payload, timeout=90)
        raw = r["candidates"][0]["content"]["parts"][0]["text"]
        try: judged = json.loads(raw)
        except Exception:
            judged = []
            for m in re.finditer(r"\{[^{}]*\}", raw):
                try: judged.append(json.loads(m.group()))
                except Exception: pass
        groups = {}
        for j in judged:
            try: groups[int(j["i"])] = j["g"]
            except Exception: continue
        removed = 0
        for lst in lists:
            seen_g, keep = set(), []
            for a in lst:
                g = groups.get(title_idx.get(a["title"], -1))
                if g is not None and g in seen_g:
                    removed += 1; continue
                if g is not None: seen_g.add(g)
                keep.append(a)
            lst[:] = keep
        print(f"의미 기반 중복 제거: {removed}건 제거")
    except Exception as ex:
        print(f"경고: 의미 기반 중복 제거 생략 - {ex}")

def main():
    pool, engine = crawl()
    data = {}
    for sid, name, _ in SEC_DEFS:
        items = [a for a in pool if a["sid"] == sid]
        items.sort(key=lambda a: (a["wl"], a["importance"], a["date"]), reverse=True)
        items = dedupe_topics(items)
        data[sid] = [{k: a[k] for k in ("title","summary","source","date","url","category","importance","wl","topic","alert")} for a in items[:30]]
        print(f"{name}: {len(items)}건 -> {len(data[sid])}건")
    latest = sorted(pool, key=lambda a: a["date"], reverse=True)
    latest = dedupe_topics(latest)[:LATEST_N]
    latest = [{k: a[k] for k in ("title","summary","source","date","url","category","importance","sid","topic","alert")} for a in latest]
    semantic_dedupe(data, latest)
    shown = [a for arr in data.values() for a in arr] + latest
    resolve_google_links(shown)
    notify_urgent(pool)
    meta = {"generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M") + " · " + engine,
            "sections": [{"id": s[0], "name": s[1]} for s in [next(x for x in SEC_DEFS if x[0]==o) for o in ORDER]]}
    js = ("const NEWS_META = " + json.dumps(meta, ensure_ascii=False) + ";\n"
          + "const NEWS_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n"
          + "const NEWS_LATEST = " + json.dumps(latest, ensure_ascii=False) + ";\n")
    tpl = open("dashboard_local.html", encoding="utf-8").read()
    out = tpl.replace('<script src="news_data.js"></script>', "<script>\n" + js + "</script>")
    os.makedirs("docs", exist_ok=True)
    open("docs/index.html","w",encoding="utf-8").write(out)
    print("완료 -> docs/index.html 생성")

if __name__ == "__main__":
    main()
