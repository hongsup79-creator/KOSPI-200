"""
코스피200 정배열 대시보드 데이터 생성 스크립트

실행:  python scripts/update_kospi200.py          # 실제 데이터 (네이버 증권 + 야후 파이낸스)
       python scripts/update_kospi200.py --demo   # 가짜 데이터로 화면 미리보기

결과:  data/kospi200/YYYY-MM-DD.json  (기준일별 결과)
       data/kospi200/dates.json       (선택 가능한 기준일 목록)

정배열 정의 (아래 SECTIONS만 고치면 기준을 바꿀 수 있음)
  1. 단기 정배열 : 5일선 > 10일선 > 20일선
  2. 중기 정배열 : 20일선 > 60일선 > 120일선
  * 완전정배열 배지 : 종가 > 5 > 10 > 20 > 60 > 120 (모두 정렬)
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "kospi200"

SECTIONS = [
    {"id": "short", "title": "단기 정배열", "mas": [5, 10, 20]},
    {"id": "mid", "title": "중기 정배열", "mas": [20, 60, 120]},
]
FULL_MAS = [5, 10, 20, 60, 120]   # 완전정배열 판정용
HISTORY_DAYS = 300                # 가져올 거래일 수 (120일선 + 연속일수 계산 여유분)
SNAPSHOT_DAYS = 20                # 기준일 선택 목록에 남길 최근 거래일 수


# ---------------------------------------------------------------- 데이터 수집
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Referer": "https://m.stock.naver.com/"}
CONSTITUENT_URLS = [
    "https://m.stock.naver.com/api/index/KPI200/enrollStocks?page={page}&pageSize=100",
    "https://m.stock.naver.com/api/index/KPI200/enrollStocks?page={page}&pageSize=50",
]
CODE_KEYS = ("itemCode", "stockCode", "code", "symbolCode")
NAME_KEYS = ("stockName", "itemName", "name", "stockNameKor")
SECTOR_KEYS = ("industryName", "industryCodeName", "sectorName", "upjongName")


def _get_json(url):
    import requests
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            print(f"  재시도 {attempt + 1}: {url} ({e})")
            time.sleep(2)
    raise RuntimeError(f"접속 실패: {url} ({last})")


def _cap_from(d):
    """시가총액(원). '432조 9,104억' 형식 우선, 없으면 marketValue(억 단위)."""
    txt = str(d.get("marketValueHangeul") or "")
    m = re.search(r"(?:([\d,]+)조)?\s*(?:([\d,]+)억)?", txt)
    if txt and m and (m.group(1) or m.group(2)):
        jo = int((m.group(1) or "0").replace(",", ""))
        eok = int((m.group(2) or "0").replace(",", ""))
        return (jo * 10_000 + eok) * 100_000_000
    v = str(d.get("marketValue") or "").replace(",", "")
    return int(float(v)) * 100_000_000 if re.fullmatch(r"[\d.]+", v) else 0


def parse_stock_list(data):
    """JSON 어디에 있든 {종목코드, 종목명}을 가진 항목을 찾아낸다."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            code = next((str(o[k]) for k in CODE_KEYS if k in o), "")
            name = next((str(o[k]) for k in NAME_KEYS if k in o), "")
            if re.fullmatch(r"\d{6}|[0-9A-Z]{6}", code) and name:
                sector = next((str(o[k]) for k in SECTOR_KEYS if o.get(k)), "-")
                found.append((code, name, _cap_from(o), sector))
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return found


def fetch_constituents():
    for tmpl in CONSTITUENT_URLS:
        stocks, seen = [], set()
        try:
            for page in range(1, 11):
                data = _get_json(tmpl.format(page=page))
                rows = [r for r in parse_stock_list(data) if r[0] not in seen]
                if page == 1 and not rows:
                    print(f"  응답 형식 확인 필요: {json.dumps(data, ensure_ascii=False)[:400]}")
                if not rows:
                    break
                seen.update(r[0] for r in rows)
                stocks += rows
                time.sleep(0.2)
        except Exception as e:
            print(f"  구성종목 경로 실패: {e}")
            continue
        if len(stocks) >= 150:
            return stocks
        print(f"  구성종목 {len(stocks)}개뿐 → 다음 경로 시도")
    raise RuntimeError("코스피200 구성종목을 가져오지 못했습니다.")


def fetch_real():
    """구성종목·시가총액: 네이버 증권 / 일별 종가: 야후 파이낸스 (둘 다 로그인 불필요)."""
    import yfinance as yf

    stocks = fetch_constituents()
    print(f"구성종목 {len(stocks)}개 (예: {stocks[0][1]} {stocks[0][0]})")

    tickers = [f"{c}.KS" for c, *_ in stocks]
    raw = yf.download(tickers, period="2y", interval="1d", auto_adjust=True,
                      progress=False, threads=True)
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    close.columns = [str(c).replace(".KS", "") for c in close.columns]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    price = close.sort_index().dropna(how="all").tail(HISTORY_DAYS).replace(0, np.nan)

    missing = [c for c, *_ in stocks if c not in price.columns or price[c].notna().sum() == 0]
    if missing:
        print(f"  시세 없음 {len(missing)}개 제외: {missing[:10]}")
    info = {c: {"name": n, "sector": s, "cap": cap}
            for c, n, cap, s in stocks if c not in missing}
    price = price[list(info)]
    if price.shape[1] < 150:
        raise RuntimeError(f"시세를 받은 종목이 너무 적습니다: {price.shape[1]}개")
    print(f"거래일 {len(price)}일 ({price.index[0]:%Y-%m-%d}~{price.index[-1]:%Y-%m-%d})")
    return price, info


def fetch_demo():
    """네트워크 없이 화면을 확인하기 위한 가짜 데이터."""
    rng = np.random.default_rng(7)
    sample = [
        ("005930", "삼성전자", "반도체"), ("000660", "SK하이닉스", "반도체"),
        ("373220", "LG에너지솔루션", "2차전지"), ("207940", "삼성바이오로직스", "바이오"),
        ("005380", "현대차", "자동차"), ("000270", "기아", "자동차"),
        ("068270", "셀트리온", "바이오"), ("105560", "KB금융", "은행"),
        ("055550", "신한지주", "은행"), ("035420", "NAVER", "인터넷"),
        ("011200", "HMM", "해운"), ("028260", "삼성물산", "지주"),
        ("096770", "SK이노베이션", "에너지"), ("036460", "한국가스공사", "유틸리티"),
        ("015760", "한국전력", "유틸리티"), ("021240", "코웨이", "가전"),
        ("012330", "현대모비스", "자동차부품"), ("034730", "SK", "지주"),
        ("010130", "고려아연", "비철금속"), ("051910", "LG화학", "화학"),
    ]
    days = pd.bdate_range(end=datetime.now(KST).date(), periods=HISTORY_DAYS)
    price, info = {}, {}
    for code, name, sector in sample:
        drift = rng.normal(0.0004, 0.0012)
        r = rng.normal(drift, 0.018, len(days))
        price[code] = 50000 * np.exp(np.cumsum(r))
        info[code] = {"name": name, "sector": sector,
                      "cap": int(rng.uniform(1e12, 4e14))}
    return pd.DataFrame(price, index=days), info


# ---------------------------------------------------------------- 계산
def streak(mask: pd.DataFrame) -> pd.DataFrame:
    """각 날짜까지 True가 몇 거래일 연속인지."""
    arr = mask.to_numpy(dtype=bool)
    out = np.zeros(arr.shape, dtype=int)
    for i in range(len(arr)):
        out[i] = np.where(arr[i], (out[i - 1] if i else 0) + 1, 0)
    return pd.DataFrame(out, index=mask.index, columns=mask.columns)


def aligned(ma: dict, periods: list) -> pd.DataFrame:
    cond = None
    for a, b in zip(periods, periods[1:]):
        c = ma[a] > ma[b]
        cond = c if cond is None else (cond & c)
    return cond


def build(price: pd.DataFrame, info: dict, demo: bool):
    all_p = sorted({p for s in SECTIONS for p in s["mas"]} | set(FULL_MAS))
    ma = {p: price.rolling(p, min_periods=p).mean() for p in all_p}
    ma[0] = price  # 0 = 종가

    full = aligned(ma, [0] + FULL_MAS).fillna(False)
    sec_mask = {s["id"]: aligned(ma, s["mas"]).fillna(False) for s in SECTIONS}
    sec_streak = {k: streak(v) for k, v in sec_mask.items()}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dates = list(price.index[-SNAPSHOT_DAYS:])
    updated = datetime.now(KST).isoformat(timespec="seconds")

    for i, d in enumerate(dates):
        prev = price.index[price.index.get_loc(d) - 1]
        sections = []
        for s in SECTIONS:
            today_mask, st = sec_mask[s["id"]].loc[d], sec_streak[s["id"]].loc[d]
            prev_mask, prev_st = sec_mask[s["id"]].loc[prev], sec_streak[s["id"]].loc[prev]
            items = []
            for t in today_mask[today_mask].index:
                items.append({
                    "code": t, **info[t],
                    "days": int(st[t]),
                    "full": bool(full.loc[d, t]),
                    "close": round(float(price.loc[d, t]), 2),
                })
            dropped = [{"code": t, "name": info[t]["name"], "days": int(prev_st[t])}
                       for t in prev_mask[prev_mask & ~today_mask].index]
            sections.append({
                "id": s["id"], "title": s["title"], "mas": s["mas"],
                "items": sorted(items, key=lambda x: (-x["days"], -x["cap"])),
                "dropped": dropped,
            })
        out = {
            "date": d.strftime("%Y-%m-%d"), "updated": updated,
            "universe": len(info), "demo": demo, "sections": sections,
        }
        path = OUT_DIR / f"{out['date']}.json"
        path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    # 보관 기간이 지난 파일 정리
    keep = {d.strftime("%Y-%m-%d") for d in dates}
    for f in OUT_DIR.glob("????-??-??.json"):
        if f.stem not in keep:
            f.unlink()

    meta = {"dates": sorted(keep, reverse=True), "updated": updated, "demo": demo}
    (OUT_DIR / "dates.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    print(f"저장 완료: {OUT_DIR} (기준일 {len(keep)}개, 최신 {meta['dates'][0]})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="가짜 데이터로 실행")
    args = ap.parse_args()
    try:
        price, info = fetch_demo() if args.demo else fetch_real()
    except Exception as e:
        print(f"데이터 수집 실패: {e}", file=sys.stderr)
        sys.exit(1)
    build(price, info, args.demo)


if __name__ == "__main__":
    main()
