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
]
GN_QUERIES = [
    "삼성 갤럭시", "Samsung Galaxy", "iPhone OR iPad OR Apple Watch",
    "DRAM NAND memory price", "메모리 가격 스마트폰",
    "Xiaomi OR OPPO OR vivo OR Honor smartphone shipment",
    "smart glasses OR XR headset", "Samsung Wallet OR Samsung Pay",
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
