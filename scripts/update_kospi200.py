"""
정배열 대시보드 데이터 생성 스크립트 (코스피200 · 코스닥150 공통)
※ 파일 이름은 자동 실행 설정(update.yml)과 맞추기 위해 그대로 둡니다.

실행:  python scripts/update_kospi200.py          # 실제 데이터
       python scripts/update_kospi200.py --demo   # 가짜 데이터로 화면 미리보기

데이터 출처 (모두 로그인 불필요)
  - 구성종목·시가총액 : 네이버 증권 모바일 API
  - 일별 종가         : 야후 파이낸스 (yfinance)
  - 업종(섹터)        : 한국거래소 KIND 상장법인 목록

결과
  data/kospi200/YYYY-MM-DD.json, data/kospi200/dates.json
  data/kosdaq150/YYYY-MM-DD.json, data/kosdaq150/dates.json
  data/sectors.json   (섹터 흐름 탭용)
  data/kr_trend.json, data/us_trend.json  (국내 ETF·미국 탭용, 묶음은 config/groups.json)

정배열 정의 (SECTIONS만 고치면 기준 변경)
  1. 단기 정배열 : 5일선 > 10일선 > 20일선
  2. 중기 정배열 : 20일선 > 60일선 > 120일선
  * 완전정배열 배지 : 종가 > 5 > 10 > 20 > 60 > 120
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
DATA = ROOT / "data"

SECTIONS = [
    {"id": "short", "title": "단기 정배열", "mas": [5, 10, 20]},
    {"id": "mid", "title": "중기 정배열", "mas": [20, 60, 120]},
]
FULL_MAS = [5, 10, 20, 60, 120]
HISTORY_DAYS = 300
SNAPSHOT_DAYS = 20

INDEXES = [
    {"id": "kospi200", "name": "코스피200", "suffix": ".KS", "min": 150,
     "naver": ["KPI200"], "fallback_market": "KOSPI", "fallback_n": 200},
    {"id": "kosdaq150", "name": "코스닥150", "suffix": ".KQ", "min": 110,
     "naver": ["KSQ150", "KQ150", "KOSDAQ150", "KPQ150"],
     "fallback_market": "KOSDAQ", "fallback_n": 150},
]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Referer": "https://m.stock.naver.com/"}
CODE_KEYS = ("itemCode", "stockCode", "code", "symbolCode")
NAME_KEYS = ("stockName", "itemName", "name", "stockNameKor")


def warn(msg):
    print(f"::warning::{msg}")


# ---------------------------------------------------------------- 수집 도구
def _get(url, as_json=True):
    import requests
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            return r.json() if as_json else r
        except Exception as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"접속 실패: {url} ({last})")


def _cap_from(d):
    """시가총액(원). '432조 9,104억' 우선, 없으면 marketValue(억 단위)."""
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
            if re.fullmatch(r"[0-9A-Z]{6}", code) and name:
                found.append((code, name, _cap_from(o)))
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return found


def _paged(url_tmpl, max_pages=10):
    stocks, seen = [], set()
    for page in range(1, max_pages + 1):
        data = _get(url_tmpl.format(page=page))
        rows = [r for r in parse_stock_list(data) if r[0] not in seen]
        if page == 1 and not rows:
            print(f"  응답 형식: {json.dumps(data, ensure_ascii=False)[:300]}")
        if not rows:
            break
        seen.update(r[0] for r in rows)
        stocks += rows
        time.sleep(0.2)
    return stocks


def fetch_constituents(ix):
    # 네이버는 한 번에 받을 수 있는 개수(pageSize)에 상한이 있어 50 → 20 순으로 시도
    for code in ix["naver"]:
        for size in (50, 20):
            url = (f"https://m.stock.naver.com/api/index/{code}/enrollStocks"
                   f"?page={{page}}&pageSize={size}")
            try:
                stocks = _paged(url, max_pages=12)
            except Exception as e:
                print(f"  {code} (pageSize={size}) 실패: {str(e)[:120]}")
                continue
            if len(stocks) >= ix["min"]:
                print(f"  구성종목 출처: 네이버 {code} ({len(stocks)}개)")
                return stocks, "official"
            print(f"  {code} (pageSize={size}): {len(stocks)}개 → 다음 후보")
            break
    # 대체: 시가총액 상위 N종목
    for size in (50, 20):
        url = (f"https://m.stock.naver.com/api/stocks/marketValue/{ix['fallback_market']}"
               f"?page={{page}}&pageSize={size}")
        try:
            stocks = _paged(url, max_pages=10)[: ix["fallback_n"]]
        except Exception as e:
            print(f"  시가총액 목록 (pageSize={size}) 실패: {str(e)[:120]}")
            continue
        if len(stocks) >= ix["min"]:
            warn(f"{ix['name']} 공식 구성종목을 못 찾아 시가총액 상위 {len(stocks)}종목으로 대체했습니다.")
            return stocks, "marketcap"
    raise RuntimeError(f"{ix['name']} 구성종목을 가져오지 못했습니다.")


def fetch_sectors():
    """KIND 상장법인 목록 → {종목코드: 업종}"""
    try:
        r = _get("https://kind.krx.co.kr/corpgeneral/corpList.do?method=download", as_json=False)
        r.encoding = "euc-kr"
        from io import StringIO
        df = pd.read_html(StringIO(r.text), header=0)[0]
        df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
        sec = df.set_index("종목코드")["업종"].astype(str)
        sec = sec.str.replace(r"\s*제조업$", "", regex=True).str.strip()
        print(f"  업종 정보 {len(sec)}개")
        return sec.to_dict()
    except Exception as e:
        warn(f"업종 정보를 가져오지 못했습니다: {e}")
        return {}


def fetch_prices(codes, suffix):
    import yfinance as yf
    raw = yf.download([c + suffix for c in codes], period="2y", interval="1d",
                      auto_adjust=True, progress=False, threads=True)
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    close.columns = [str(c).split(".")[0] for c in close.columns]
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.sort_index().dropna(how="all").tail(HISTORY_DAYS).replace(0, np.nan)


def fetch_real(ix, sectors):
    stocks, source = fetch_constituents(ix)
    price = fetch_prices([c for c, *_ in stocks], ix["suffix"])
    missing = [c for c, *_ in stocks if c not in price.columns or price[c].notna().sum() == 0]
    if missing:
        print(f"  시세 없음 {len(missing)}개 제외: {missing[:10]}")
    info = {c: {"name": n, "sector": sectors.get(c, "-"), "cap": cap}
            for c, n, cap in stocks if c not in missing}
    price = price[list(info)]
    if price.shape[1] < ix["min"] * 0.8:
        raise RuntimeError(f"시세를 받은 종목이 너무 적습니다: {price.shape[1]}개")
    print(f"  거래일 {len(price)}일 ({price.index[0]:%Y-%m-%d}~{price.index[-1]:%Y-%m-%d})")
    return price, info, source


def fetch_demo(ix, _sectors):
    rng = np.random.default_rng(7 if ix["id"] == "kospi200" else 11)
    secs = ["반도체", "자동차", "화학", "은행", "바이오", "해운", "유틸리티", "인터넷", "2차전지", "소프트웨어"]
    n = 40 if ix["id"] == "kospi200" else 30
    days = pd.bdate_range(end=datetime.now(KST).date(), periods=HISTORY_DAYS)
    price, info = {}, {}
    for i in range(n):
        code = f"{(i + 1) * 1371 % 999999:06d}"
        r = rng.normal(rng.normal(0.0004, 0.0012), 0.018, len(days))
        price[code] = 50000 * np.exp(np.cumsum(r))
        info[code] = {"name": f"{ix['name']} 샘플{i + 1}", "sector": secs[i % len(secs)],
                      "cap": int(rng.uniform(1e11, 4e14))}
    return pd.DataFrame(price, index=days), info, "demo"


# ---------------------------------------------------------------- 계산
def streak(mask):
    arr = mask.to_numpy(dtype=bool)
    out = np.zeros(arr.shape, dtype=int)
    for i in range(len(arr)):
        out[i] = np.where(arr[i], (out[i - 1] if i else 0) + 1, 0)
    return pd.DataFrame(out, index=mask.index, columns=mask.columns)


def aligned(ma, periods):
    cond = None
    for a, b in zip(periods, periods[1:]):
        c = ma[a] > ma[b]
        cond = c if cond is None else (cond & c)
    return cond


def build(ix, price, info, source, demo, updated):
    out_dir = DATA / ix["id"]
    all_p = sorted({p for s in SECTIONS for p in s["mas"]} | set(FULL_MAS))
    ma = {p: price.rolling(p, min_periods=p).mean() for p in all_p}
    ma[0] = price
    full = aligned(ma, [0] + FULL_MAS).fillna(False)
    sec_mask = {s["id"]: aligned(ma, s["mas"]).fillna(False) for s in SECTIONS}
    sec_streak = {k: streak(v) for k, v in sec_mask.items()}

    out_dir.mkdir(parents=True, exist_ok=True)
    dates = list(price.index[-SNAPSHOT_DAYS:])
    flow = {s["id"]: {} for s in SECTIONS}  # {section: {sector: [count per date]}}

    for di, d in enumerate(dates):
        prev = price.index[price.index.get_loc(d) - 1]
        sections = []
        for s in SECTIONS:
            m_today, st = sec_mask[s["id"]].loc[d], sec_streak[s["id"]].loc[d]
            m_prev, st_prev = sec_mask[s["id"]].loc[prev], sec_streak[s["id"]].loc[prev]
            items = [{"code": t, **info[t], "days": int(st[t]), "full": bool(full.loc[d, t])}
                     for t in m_today[m_today].index]
            dropped = [{"code": t, "name": info[t]["name"], "days": int(st_prev[t])}
                       for t in m_prev[m_prev & ~m_today].index]
            sections.append({"id": s["id"], "title": s["title"], "mas": s["mas"],
                              "items": sorted(items, key=lambda x: (-x["days"], -x["cap"])),
                              "dropped": dropped})
            for it in items:
                flow[s["id"]].setdefault(it["sector"], [0] * len(dates))[di] += 1
        out = {"date": d.strftime("%Y-%m-%d"), "updated": updated, "index": ix["name"],
               "universe": len(info), "source": source, "demo": demo, "sections": sections}
        (out_dir / f"{out['date']}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")

    keep = {d.strftime("%Y-%m-%d") for d in dates}
    for f in out_dir.glob("????-??-??.json"):
        if f.stem not in keep:
            f.unlink()
    meta = {"dates": sorted(keep, reverse=True), "updated": updated, "demo": demo, "source": source}
    (out_dir / "dates.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    universe = {}
    for t in info:
        universe[info[t]["sector"]] = universe.get(info[t]["sector"], 0) + 1
    print(f"  저장 완료: {out_dir.name} (최신 {meta['dates'][0]})")
    return {"name": ix["name"], "dates": [d.strftime("%Y-%m-%d") for d in dates],
            "universe": universe, "flow": flow}


# ---------------------------------------------------------------- ETF·미국 추이
CONFIG = ROOT / "config" / "groups.json"
TREND_DAYS = 500   # 약 2년


def _yf_close(tickers):
    import yfinance as yf
    raw = yf.download(tickers, period="2y", interval="1d", auto_adjust=True,
                      progress=False, threads=True)
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if not isinstance(raw.columns, pd.MultiIndex):
        close.columns = tickers
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    return close.sort_index()


def build_trend(market, groups, demo, updated):
    """market: 'kr' | 'us'  →  data/{market}_trend.json"""
    def ticker(code):
        return code + ".KS" if market == "kr" else code

    codes = [it["code"] for g in groups for it in g["items"]]
    if demo:
        rng = np.random.default_rng(3 if market == "kr" else 5)
        days = pd.bdate_range(end=datetime.now(KST).date(), periods=TREND_DAYS)
        close = pd.DataFrame({ticker(c): 10000 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, len(days))))
                              for c in codes}, index=days)
    else:
        close = _yf_close([ticker(c) for c in codes])
        # 국내 코스피 시세가 없으면 코스닥(.KQ)으로 재시도
        if market == "kr":
            miss = [c for c in codes if ticker(c) not in close or close[ticker(c)].notna().sum() == 0]
            if miss:
                extra = _yf_close([c + ".KQ" for c in miss])
                for c in miss:
                    if c + ".KQ" in extra:
                        close[ticker(c)] = extra[c + ".KQ"]
    close = close.tail(TREND_DAYS)

    out_groups = []
    for g in groups:
        series = []
        for it in g["items"]:
            col = ticker(it["code"])
            if col not in close or close[col].notna().sum() < 5:
                warn(f"{it['name']}({it['code']}) 시세 없음 — groups.json 코드를 확인하세요.")
                continue
            v = close[col].ffill()
            series.append({"code": it["code"], "name": it["name"],
                           "close": [None if pd.isna(x) else round(float(x), 4) for x in v]})
        if series:
            out_groups.append({"name": g["name"], "series": series})
    out = {"updated": updated, "demo": demo, "market": market,
           "dates": [d.strftime("%Y-%m-%d") for d in close.index], "groups": out_groups}
    DATA.mkdir(exist_ok=True)
    (DATA / f"{market}_trend.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"  저장 완료: {market}_trend.json (묶음 {len(out_groups)}개, 최신 {out['dates'][-1]})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    updated = datetime.now(KST).isoformat(timespec="seconds")
    sectors = {} if args.demo else fetch_sectors()

    sector_file = DATA / "sectors.json"
    try:
        sector_out = json.loads(sector_file.read_text(encoding="utf-8"))
    except Exception:
        sector_out = {}

    ok = 0
    for ix in INDEXES:
        print(f"[{ix['name']}]")
        try:
            price, info, source = (fetch_demo if args.demo else fetch_real)(ix, sectors)
            sector_out[ix["id"]] = build(ix, price, info, source, args.demo, updated)
            ok += 1
        except Exception as e:
            warn(f"{ix['name']} 실패: {e}")

    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        for market, label in (("kr", "국내 ETF"), ("us", "미국 주식/ETF")):
            print(f"[{label}]")
            try:
                build_trend(market, cfg.get(market, []), args.demo, updated)
                ok += 1
            except Exception as e:
                warn(f"{label} 실패: {e}")
    except Exception as e:
        warn(f"config/groups.json 읽기 실패: {e}")

    if ok:
        sector_out["updated"] = updated
        sector_out["demo"] = args.demo
        DATA.mkdir(exist_ok=True)
        sector_file.write_text(json.dumps(sector_out, ensure_ascii=False), encoding="utf-8")
    if ok < len(INDEXES) + 2:
        print(f"일부 실패: {ok}/{len(INDEXES) + 2} 성공", file=sys.stderr)
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
