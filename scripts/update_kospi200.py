"""
정배열 대시보드 데이터 생성 스크립트 (코스피200 · 코스닥150 공통)
※ 파일 이름은 자동 실행 설정(update.yml)과 맞추기 위해 그대로 둡니다.

실행:  python scripts/update_kospi200.py          # 실제 데이터
       python scripts/update_kospi200.py --demo   # 가짜 데이터로 화면 미리보기

데이터 출처 (모두 로그인 불필요)
  (참고: jblee1031/ETF_trend 와 같은 출처)
  - 구성종목          : 지수 추종 ETF 보유종목 (네이버 wisereport)
                        코스피200 = KODEX 200(069500), 코스닥150 = KODEX 코스닥150(229200)
  - 국내 일별 종가    : 네이버 차트 (fchart.stock.naver.com)
  - 미국 일별 종가    : 야후 파이낸스
  - 업종(섹터)        : 네이버 업종 분류 (실패 시 한국거래소 KIND)

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
HISTORY_DAYS = 500   # 약 2년 (120일선 + 신호 성적표 검증 기간)
SNAPSHOT_DAYS = 20

INDEXES = [
    {"id": "kospi200", "name": "코스피200", "suffix": ".KS", "min": 150,
     "naver": ["KPI200"], "etf": "069500", "market_type": "KOSPI",
     "fallback_market": "KOSPI", "fallback_n": 200},
    # 네이버에 코스닥150 구성종목 목록이 없어, 지수를 그대로 복제하는
    # KODEX 코스닥150(229200)의 실제 보유종목을 구성종목으로 사용
    {"id": "kosdaq150", "name": "코스닥150", "suffix": ".KQ", "min": 110,
     "naver": [], "etf": "229200", "market_type": "KOSDAQ",
     "fallback_market": "KOSDAQ", "fallback_n": 150},
]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
      "Referer": "https://m.stock.naver.com/"}
CODE_KEYS = ("itemCode", "stockCode", "code", "symbolCode")
NAME_KEYS = ("stockName", "itemName", "name", "stockNameKor")


def warn(msg):
    print(f"::warning::{msg}")


CACHE = DATA / "cache"
WORKERS = 8          # 동시에 받는 개수 (너무 크면 네이버가 막을 수 있음)


def pmap(fn, items):
    """여러 건을 동시에 처리 (순서 유지)"""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(fn, items))


def load_cache(name, max_age_days=None):
    f = CACHE / f"{name}.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    if max_age_days is not None:
        saved = datetime.fromisoformat(d.get("_saved", "2000-01-01T00:00:00+09:00"))
        if datetime.now(KST) - saved > timedelta(days=max_age_days):
            return None
    return d


def save_cache(name, d):
    CACHE.mkdir(parents=True, exist_ok=True)
    d["_saved"] = datetime.now(KST).isoformat(timespec="seconds")
    (CACHE / f"{name}.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _norm(name):
    return re.sub(r"\s+", "", str(name))


# ---------------------------------------------------------------- 수집 도구
def _get(url, as_json=True):
    import requests
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=15)
            if 400 <= r.status_code < 500:      # 요청 자체가 거부된 경우는 재시도해도 소용없음
                raise RuntimeError(f"{r.status_code} 거부")
            r.raise_for_status()
            return r.json() if as_json else r
        except RuntimeError:
            raise
        except Exception as e:
            last = e
            time.sleep(1.5)
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
        time.sleep(0.05)
    return stocks


def universe_from_etf(etf_code, market_type, caps, name_map=None):
    """지수 추종 ETF의 보유종목(wisereport) → [(코드, 이름, 시가총액)]"""
    r = _get(f"https://navercomp.wisereport.co.kr/v2/ETF/index.aspx?cmp_cd={etf_code}", as_json=False)
    m = re.search(r"var CU_data = (\{.*?\});", r.text, re.S)
    if not m:
        raise RuntimeError("ETF 보유종목 데이터를 찾지 못했습니다.")
    rows = json.loads(m.group(1)).get("grid_data", [])
    known = (load_cache("codes") or {}).get("map", {})     # 이전에 확인한 이름→코드
    name_map = name_map or {}
    names, codes = [], {}
    for row in rows:
        name = str(row.get("STK_NM_KOR", "")).strip()
        if not name or name in ("원화현금", "현금"):
            continue
        names.append(name)
        code = next((re.sub(r"^A", "", str(v)) for v in row.values()
                     if re.fullmatch(r"A?\d{6}", str(v))), None)
        codes[name] = code or known.get(name) or name_map.get(_norm(name))
    todo = [n for n in names if not codes[n]]
    if todo:                                                 # 남은 것만 검색 (동시에)
        for n, c in zip(todo, pmap(lambda n: _code_by_name(n, market_type), todo)):
            codes[n] = c
    known.update({n: c for n, c in codes.items() if c})
    save_cache("codes", {"map": known})
    out = [(codes[n], n, caps.get(codes[n], 0)) for n in names if codes[n]]
    unresolved = [n for n in names if not codes[n]]
    if unresolved:
        print(f"  종목코드 못 찾음 {len(unresolved)}개: {unresolved[:8]}")
    print(f"  이름→코드: 저장분·업종목록 {len(names) - len(todo)}개, 새로 검색 {len(todo)}개")
    return out


def _code_by_name(name, market_type):
    import urllib.parse
    try:
        data = _get(f"https://ac.stock.naver.com/ac?q={urllib.parse.quote(name)}&target=stock")
    except Exception:
        return None
    hit = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("name") == name and str(o.get("typeCode", "")).upper() == market_type \
                    and re.fullmatch(r"\d{6}", str(o.get("code", ""))):
                hit.append(str(o["code"]))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return hit[0] if hit else None


def fetch_industry():
    """네이버 업종 분류 → ({코드: 업종}, {코드: 시가총액}, {이름: 코드}). 업종은 7일마다 새로 받음"""
    cached = load_cache("industry", max_age_days=7)
    if cached:
        print(f"  업종: 저장분 사용 ({cached['_saved'][:10]}, 종목 {len(cached['sectors'])}개)")
        return cached["sectors"], cached["caps"], cached["names"]
    groups, seen = [], set()
    for page in range(1, 6):
        try:
            data = _get(f"https://m.stock.naver.com/api/stocks/industry?page={page}")
        except Exception:
            break
        found = []

        def walk(o):
            if isinstance(o, dict):
                no = next((o[k] for k in ("no", "industryCode", "upjongNo") if k in o), None)
                nm = next((o[k] for k in ("name", "industryName", "upjongName") if k in o), None)
                if no is not None and nm and str(no) not in seen:
                    found.append((str(no), str(nm)))
                    return
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        walk(data)
        if not found:
            break
        seen.update(g[0] for g in found)
        groups += found

    def one(g):
        no, _ = g
        for size in (50, 20):
            try:
                return _paged(f"https://m.stock.naver.com/api/stocks/industry/{no}?page={{page}}&pageSize={size}",
                              max_pages=10)
            except Exception:
                continue
        return []

    sectors, caps, names = {}, {}, {}
    for (no, nm), rows in zip(groups, pmap(one, groups)):
        for code, name, cap in rows:
            sectors[code] = nm
            names[_norm(name)] = code
            if cap:
                caps[code] = cap
    print(f"  네이버 업종 {len(groups)}개, 종목 {len(sectors)}개")
    if len(sectors) > 1000:
        save_cache("industry", {"sectors": sectors, "caps": caps, "names": names})
    return sectors, caps, names


def fetch_constituents(ix, caps=None, name_map=None):
    # 1순위: 지수 추종 ETF 보유종목 (참고 대시보드와 같은 방식)
    if ix.get("etf"):
        try:
            stocks = universe_from_etf(ix["etf"], ix["market_type"], caps or {}, name_map)
            if len(stocks) >= ix["min"]:
                print(f"  구성종목 출처: {ix['etf']} ETF 보유종목 ({len(stocks)}개)")
                return stocks, "etf"
            print(f"  ETF 보유종목 {len(stocks)}개뿐 → 다음 방법")
        except Exception as e:
            print(f"  ETF 보유종목 실패: {str(e)[:120]}")
    # 2순위: 네이버 지수 구성종목 목록 (pageSize 상한 때문에 50 → 20)
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
    # 3순위: 시가총액 상위 N종목
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


def naver_history(code, count):
    """네이버 차트(fchart) 일봉 → DataFrame[close, vol] (참고 대시보드와 같은 출처)"""
    url = (f"https://fchart.stock.naver.com/sise.nhn?symbol={code}"
           f"&timeframe=day&count={count}&requestType=0")
    r = _get(url, as_json=False)
    text = r.content.decode("euc-kr", errors="replace")
    rows = {}
    for d, c, v in re.findall(r'data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|([\d.]*)', text):
        rows[pd.Timestamp(d)] = (float(c), float(v) if v else np.nan)
    df = pd.DataFrame.from_dict(rows, orient="index", columns=["close", "vol"])
    return df.sort_index()


def fetch_prices(codes, count=HISTORY_DAYS, with_volume=False):
    def one(c):
        try:
            return naver_history(c, count)
        except Exception:
            return None
    closes, vols, failed = {}, {}, []
    for c, df in zip(codes, pmap(one, codes)):
        if df is not None and len(df):
            closes[c], vols[c] = df["close"], df["vol"]
        else:
            failed.append(c)
    print(f"  시세 {len(closes)}/{len(codes)}개 수집")
    if failed:
        print(f"  시세 실패 {len(failed)}개: {failed[:10]}")
    price = pd.DataFrame(closes).sort_index().dropna(how="all").tail(count).replace(0, np.nan)
    if not with_volume:
        return price
    vol = pd.DataFrame(vols).reindex(index=price.index, columns=price.columns)
    return price, vol


def fetch_real(ix, sectors, caps=None, name_map=None):
    stocks, source = fetch_constituents(ix, caps, name_map)
    caps = caps or {}
    stocks = [(c, n, cap or caps.get(c, 0)) for c, n, cap in stocks]
    price, vol = fetch_prices([c for c, *_ in stocks], with_volume=True)
    missing = [c for c, *_ in stocks if c not in price.columns or price[c].notna().sum() == 0]
    if missing:
        print(f"  시세 없음 {len(missing)}개 제외: {missing[:10]}")
    info = {c: {"name": n, "sector": sectors.get(c, "-"), "cap": cap}
            for c, n, cap in stocks if c not in missing}
    price, vol = price[list(info)], vol[list(info)]
    if price.shape[1] < ix["min"] * 0.8:
        raise RuntimeError(f"시세를 받은 종목이 너무 적습니다: {price.shape[1]}개")
    print(f"  거래일 {len(price)}일 ({price.index[0]:%Y-%m-%d}~{price.index[-1]:%Y-%m-%d})")
    return price, vol, info, source


def fetch_demo(ix, _sectors, _caps=None, _names=None):
    rng = np.random.default_rng(7 if ix["id"] == "kospi200" else 11)
    secs = ["반도체", "자동차", "화학", "은행", "바이오", "해운", "유틸리티", "인터넷", "2차전지", "소프트웨어"]
    n = 40 if ix["id"] == "kospi200" else 30
    days = pd.bdate_range(end=datetime.now(KST).date(), periods=HISTORY_DAYS)
    price, vol, info = {}, {}, {}
    for i in range(n):
        code = f"{(i + 1) * 1371 % 999999:06d}"
        r = rng.normal(rng.normal(0.0004, 0.0012), 0.018, len(days))
        price[code] = 50000 * np.exp(np.cumsum(r))
        vol[code] = rng.lognormal(12, 0.5, len(days))
        info[code] = {"name": f"{ix['name']} 샘플{i + 1}", "sector": secs[i % len(secs)],
                      "cap": int(rng.uniform(1e11, 4e14))}
    return pd.DataFrame(price, index=days), pd.DataFrame(vol, index=days), info, "demo"


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


def _r(v, nd=1):
    return None if v is None or pd.isna(v) else round(float(v), nd)


BREADTH_DAYS = 120


def breadth(price, sec_mask, full):
    """시장 체온계: 날짜별 정배열 종목 비율(%)"""
    valid = price.notna().sum(axis=1).replace(0, np.nan)
    out = {"dates": [d.strftime("%Y-%m-%d") for d in price.index[-BREADTH_DAYS:]]}
    for k, m in list(sec_mask.items()) + [("full", full)]:
        pct = (m.sum(axis=1) / valid * 100).iloc[-BREADTH_DAYS:]
        out[k] = [_r(v) for v in pct]
    return out


HORIZONS = [5, 10, 20]


def scorecard(price, sec_streak):
    """신호 성적표: 정배열에 새로 들어온 날 종가에 샀다고 가정한 이후 수익률"""
    res = {}
    fwd = {h: (price.shift(-h) / price - 1) * 100 for h in HORIZONS}
    bench = {h: fwd[h].mean(axis=1) for h in HORIZONS}   # 같은 기간 구성종목 평균
    for k, st in sec_streak.items():
        entry = (st == 1)
        # 이평선이 처음 계산되는 구간의 가짜 신호 제외 (최소 120일 이후만)
        entry.iloc[:130] = False
        row = {}
        for h in HORIZONS:
            r = fwd[h][entry].stack()
            if r.empty:
                row[str(h)] = None
                continue
            ex = fwd[h][entry].sub(bench[h], axis=0).stack()
            row[str(h)] = {"n": int(r.size), "avg": _r(r.mean(), 2), "win": _r((r > 0).mean() * 100),
                           "excess": _r(ex.mean(), 2), "beat": _r((ex > 0).mean() * 100)}
        first = price.index[130] if len(price) > 130 else price.index[0]
        row["from"] = first.strftime("%Y-%m-%d")
        res[k] = row
    return res


def build(ix, price, vol, info, source, demo, updated):
    out_dir = DATA / ix["id"]
    all_p = sorted({p for s in SECTIONS for p in s["mas"]} | set(FULL_MAS))
    ma = {p: price.rolling(p, min_periods=p).mean() for p in all_p}
    ma[0] = price
    full = aligned(ma, [0] + FULL_MAS).fillna(False)
    sec_mask = {s["id"]: aligned(ma, s["mas"]).fillna(False) for s in SECTIONS}
    sec_streak = {k: streak(v) for k, v in sec_mask.items()}

    # --- 과열(이격도)·품질 점수 재료
    gap20 = (price / ma[20] - 1) * 100                              # 20일선 대비 이격도(%)
    spread = (ma[5] / ma[20] - 1) * 100                             # 이평선 간격
    slope20 = (ma[20] / ma[20].shift(5) - 1) * 100                  # 20일선 5일 기울기
    volr = vol.rolling(5, min_periods=3).mean() / vol.rolling(20, min_periods=10).mean()  # 거래량 증가
    hi52 = price / price.rolling(250, min_periods=120).max()        # 52주 고점 대비
    parts = [spread, slope20, volr, hi52]
    score = sum(p.rank(axis=1, pct=True) for p in parts) / len(parts) * 100

    out_dir.mkdir(parents=True, exist_ok=True)
    dates = list(price.index[-SNAPSHOT_DAYS:])
    flow = {s["id"]: {} for s in SECTIONS}  # {section: {sector: [count per date]}}

    for di, d in enumerate(dates):
        prev = price.index[price.index.get_loc(d) - 1]
        sections = []
        for s in SECTIONS:
            m_today, st = sec_mask[s["id"]].loc[d], sec_streak[s["id"]].loc[d]
            m_prev, st_prev = sec_mask[s["id"]].loc[prev], sec_streak[s["id"]].loc[prev]
            items = [{"code": t, **info[t], "days": int(st[t]), "full": bool(full.loc[d, t]),
                      "gap": _r(gap20.loc[d, t]), "score": _r(score.loc[d, t], 0),
                      "volr": _r(volr.loc[d, t], 2)}
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
    meta = {"dates": sorted(keep, reverse=True), "updated": updated, "demo": demo, "source": source,
            "breadth": breadth(price, sec_mask, full), "scorecard": scorecard(price, sec_streak)}
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
    elif market == "kr":
        # 국내: 네이버 차트 (참고 대시보드와 같은 출처)
        close = fetch_prices(codes, count=TREND_DAYS)
        close.columns = [ticker(c) for c in close.columns]
    else:
        # 미국: 야후 파이낸스 (참고 대시보드와 같은 출처)
        close = _yf_close([ticker(c) for c in codes])
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
    sectors, caps, name_map = {}, {}, {}
    if not args.demo:
        t_ind = time.time()
        nav_sec, caps, name_map = fetch_industry()
        print(f"  업종 수집 {time.time() - t_ind:.0f}초")
        kind = fetch_sectors() if len(nav_sec) < 1000 else {}
        sectors = {**kind, **nav_sec}   # 네이버 업종 우선, 없으면 KIND

    sector_file = DATA / "sectors.json"
    try:
        sector_out = json.loads(sector_file.read_text(encoding="utf-8"))
    except Exception:
        sector_out = {}

    ok = 0
    t0 = time.time()
    for ix in INDEXES:
        print(f"[{ix['name']}]  (+{time.time() - t0:.0f}초)")
        try:
            price, vol, info, source = (fetch_demo if args.demo else fetch_real)(ix, sectors, caps, name_map)
            sector_out[ix["id"]] = build(ix, price, vol, info, source, args.demo, updated)
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
    print(f"총 소요 {time.time() - t0:.0f}초")
    if ok < len(INDEXES) + 2:
        print(f"일부 실패: {ok}/{len(INDEXES) + 2} 성공", file=sys.stderr)
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
