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
    batch = pool[:250]
    lines = [f"{i} | {b['title']} | {b['summary'][:220]} | {b['source']}" for i, b in enumerate(batch)]
    prompt = f"""당신은 삼성전자 MX사업부 경쟁정보(CI) 분석가입니다. 아래 기사 목록(번호|제목|요약|매체)을 각각 판정하세요.

섹션(sec): phone(스마트폰) tablet(태블릿) pc(노트북/PC) watch(스마트워치) tws(무선이어폰) glasses(XR/AI글래스) wallet(결제/월렛) health(디지털헬스) memchina(메모리 가격·중국 스마트폰 제조사 동향) none(MX사업과 무관→제외)
카테고리(cat): quality launch price exec policy community other
중요도(imp): 5=삼성 MX에 즉각 대응 필요한 긴급, 4=경영진 보고 필요, 3=주시, 2=참고, 1=단순 정보
이슈(topic): 기사가 다루는 핵심 사건을 나타내는 짧은 한국어 이슈명. 반드시 "제품명 사건" 형식으로, 제품명을 첫 단어로 동일하게 표기할 것(예: "북6 출시", "북6 리뷰", "북6 가격" — 제품명 표기는 전부 통일). 같은 사건을 다룬 기사는 제목 표현·매체·언어가 달라도 반드시 한 글자도 다르지 않은 동일 이슈명을 부여. 같은 제품의 출시·공개·발표·리뷰 보도는 원칙적으로 하나의 이슈로 묶을 것. 이슈명이 같으면 중복으로 간주되어 1건만 표시됨.
요약(sum): 반드시 한국어로만 작성. 외국어 기사도 한국어로 번역 요약. 4~5문장 300자 내외로, 핵심 사실 → 배경·수치 → 경쟁 구도 → 사업적 의미 순으로 충실히 작성. 제공된 제목·요약 범위 내에서만 작성하고 추측 금지. 제공 정보가 제목뿐이면 억지로 늘리지 말고 짧게 유지.

모든 기사에 대해 JSON 배열만 출력: [{{"i":0,"sec":"phone","cat":"launch","imp":3,"topic":"언팩 초청장","sum":"..."}}]

기사 목록:
{chr(10).join(lines)}"""
    payload = {"contents":[{"parts":[{"text":prompt}]}],
               "generationConfig":{"response_mime_type":"application/json","temperature":0}}

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
        for attempt in range(2):
            try:
                print(f"Gemini 분류 요청... ({model})")
                r = gemini_call(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}", payload)
                raw = r["candidates"][0]["content"]["parts"][0]["text"]
                try:
                    judged = json.loads(raw)
                except Exception:
                    judged = []
                    for m in re.finditer(r"\{[^{}]*\}", raw):
                        try: judged.append(json.loads(m.group()))
                        except Exception: pass
                    print(f"  일부 형식 오류 -> 복구 파싱 {len(judged)}건")
                if not judged: raise ValueError("판정 결과 파싱 실패")
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
                print(f"Gemini 판정 적용: {applied}건")
                open("gemini_model.txt","w",encoding="utf-8").write(model)
                return f"Gemini 분류 ({model})"
            except urllib.error.HTTPError as ex:
                if ex.code == 404:
                    print(f"  {model} 사용 불가(404) -> 다음 모델"); break
                if ex.code == 429:
                    if attempt == 0:
                        print(f"  {model} 429 -> 30초 대기 후 재시도"); time.sleep(30)
                    else:
                        print(f"  {model} 재시도도 429 -> 다음 모델")
                else:
                    print(f"경고: {model} 실패 - HTTP {ex.code} -> 다음 모델"); break
            except Exception as ex:
                print(f"경고: {model} 실패 - {ex} -> 다음 모델"); break
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

def main():
    pool, engine = crawl()
    data = {}
    for sid, name, _ in SEC_DEFS:
        items = [a for a in pool if a["sid"] == sid]
        items.sort(key=lambda a: (a["wl"], a["importance"], a["date"]), reverse=True)
        items = dedupe_topics(items)
        data[sid] = [{k: a[k] for k in ("title","summary","source","date","url","category","importance","wl","topic")} for a in items[:30]]
        print(f"{name}: {len(items)}건 -> {len(data[sid])}건")
    latest = sorted(pool, key=lambda a: a["date"], reverse=True)
    latest = dedupe_topics(latest)[:LATEST_N]
    latest = [{k: a[k] for k in ("title","summary","source","date","url","category","importance","sid","topic")} for a in latest]
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
