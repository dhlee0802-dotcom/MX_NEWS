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
회사 이름이 크더라도 단순 언급·제품 소개·인터뷰·시황 전망 기사는 3 이하.
이슈(topic): 기사가 다루는 핵심 사건을 나타내는 짧은 한국어 이슈명. 반드시 "회사명 사건" 형식으로, 회사명을 첫 단어로 동일하게 표기할 것(예: "버라이즌 장애", "에릭슨 수주", "에릭슨 실적" — 회사명 표기는 전부 통일). 영문 매체 기사라도 회사명은 반드시 한국어 표기(에릭슨, 노키아, 화웨이, 버라이즌, 도이치텔레콤 등)로 쓸 것 — 같은 사건을 다룬 한국어·영어 기사가 동일 이슈명으로 묶여야 함. 같은 사건을 다룬 기사는 제목 표현·매체·언어가 달라도 반드시 한 글자도 다르지 않은 동일 이슈명을 부여. 특히 같은 행사·발표·컨퍼런스·국정감사·정책 브리핑에서 파생된 기사들은 세부 주제가 조금씩 달라도 전부 하나의 동일 이슈명으로 묶을 것(예: 국정감사에서 나온 통신 관련 기사 전부 → "과기정통부 국감"). 이슈명이 같으면 중복으로 간주되어 1건만 표시됨.
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
    open(ARCHIVE_FILE, "w", encoding="utf-8").write(json.dumps(arch, ensure_ascii=False))
    print(f"아카이브 갱신: 신규 {added}건 / 보존 {len(arch)}일치")
    return arch

def gemini_brief(items):
    """최근 24시간 중요 기사(4점 이상)를 출근길 음성 브리핑 원고로 변환. 실패 시 단순 연결 원고."""
    if not items:
        return "최근 24시간 사이 중요도 4점 이상 기사가 없습니다. 좋은 하루 되십시오."
    fallback = ("안녕하십니까, 네트워크 뉴스 브리핑입니다. "
                + " ".join(f"{i+1}번째 소식. {a['title']}. {a['summary']}" for i, a in enumerate(items[:8]))
                + " 이상으로 브리핑을 마칩니다.")
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return fallback
    model = "gemini-2.0-flash"
    if os.path.exists("gemini_model_nw.txt"):
        m = open("gemini_model_nw.txt", encoding="utf-8").read().strip()
        if m: model = m
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
    now_kst = datetime.now(KST)
    brief_items = []
    for a in arch_items:
        try:
            dt = datetime.strptime(a["date"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
            if now_kst - dt <= timedelta(hours=24): brief_items.append(a)
        except Exception: pass
    brief_items.sort(key=lambda a: (a["importance"], a["date"]), reverse=True)
    brief = {"generated": now_kst.strftime("%Y-%m-%d %H:%M"), "n": len(brief_items),
             "text": gemini_brief(brief_items)}
    meta = {"generated": datetime.now(KST).strftime("%Y-%m-%d %H:%M") + " · " + engine,
            "sections": [{"id": s[0], "name": s[1]} for s in [next(x for x in SEC_DEFS if x[0]==o) for o in ORDER]]}
    js = ("const NEWS_META = " + json.dumps(meta, ensure_ascii=False) + ";\n"
          + "const NEWS_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n"
          + "const NEWS_LATEST = " + json.dumps(latest, ensure_ascii=False) + ";\n"
          + "const NEWS_ARCHIVE = " + json.dumps(archive, ensure_ascii=False) + ";\n"
          + "const NEWS_BRIEF = " + json.dumps(brief, ensure_ascii=False) + ";\n")
    js = js.replace("</", "<\\/")  # 기사 내용에 </script> 유사 문자열이 있어도 스크립트가 깨지지 않게
    tpl = open("dashboard_network.html", encoding="utf-8").read()
    inj = "<script>\n" + js + "</script>"
    # 자리표시자 주변 공백·따옴표 차이를 허용하는 주입
    out, n = re.subn(r'<script\s+src=["\']news_data\.js["\']\s*>\s*</script>', inj, tpl, count=1)
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

if __name__ == "__main__":
    main()
