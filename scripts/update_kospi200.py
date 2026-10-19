"""
코스피200 정배열 대시보드 데이터 생성 스크립트

실행:  python scripts/update_kospi200.py          # 실제 데이터 (pykrx)
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
def fetch_real():
    """pykrx로 코스피200 구성종목과 일별 종가를 가져온다.
    pykrx 1.2.x부터 KRX 로그인이 필요: 환경변수 KRX_ID, KRX_PW."""
    import os
    if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):
        raise RuntimeError("KRX_ID / KRX_PW 환경변수가 없습니다. GitHub Secrets 등록을 확인하세요.")
    from pykrx import stock

    today = datetime.now(KST).strftime("%Y%m%d")
    start = (datetime.now(KST) - timedelta(days=int(HISTORY_DAYS * 1.6))).strftime("%Y%m%d")

    # 1) 거래일 목록 (코스피200 지수 일봉의 날짜 = 거래일)
    idx = stock.get_index_ohlcv(start, today, "1028")
    days = [d.strftime("%Y%m%d") for d in idx.index][-HISTORY_DAYS:]
    if not days:
        raise RuntimeError("거래일 목록을 가져오지 못했습니다.")
    last = days[-1]

    # 2) 구성종목
    tickers = list(stock.get_index_portfolio_deposit_file("1028", last))
    if len(tickers) < 150:
        raise RuntimeError(f"구성종목 수가 비정상입니다: {len(tickers)}")
    print(f"구성종목 {len(tickers)}개, 거래일 {len(days)}일 ({days[0]}~{last})")

    # 3) 날짜별 전 종목 종가 (종목별 200회 대신 날짜별 1회씩 호출해 빠르게)
    closes = {}
    for i, d in enumerate(days, 1):
        for attempt in range(3):
            try:
                df = stock.get_market_ohlcv(d, market="KOSPI")
                closes[d] = df["종가"]
                break
            except Exception as e:  # 일시적 오류는 재시도
                print(f"  {d} 재시도 {attempt + 1}: {e}")
                time.sleep(2)
        if i % 50 == 0:
            print(f"  시세 수집 {i}/{len(days)}")
        time.sleep(0.2)  # 서버 부담 줄이기
    price = pd.DataFrame(closes).T
    price.index = pd.to_datetime(price.index)
    price = price.reindex(columns=tickers).replace(0, np.nan)

    # 4) 종목명·시가총액
    cap = stock.get_market_cap(last, market="KOSPI")["시가총액"]
    names = {t: stock.get_market_ticker_name(t) for t in tickers}

    # 5) 섹터 (FinanceDataReader의 업종 정보, 실패해도 계속 진행)
    sectors = {}
    try:
        import FinanceDataReader as fdr
        desc = fdr.StockListing("KRX-DESC").set_index("Code")
        sectors = desc["Sector"].dropna().to_dict()
    except Exception as e:
        print(f"섹터 정보 생략: {e}")

    info = {
        t: {"name": names.get(t, t), "sector": sectors.get(t, "-") or "-",
            "cap": int(cap.get(t, 0) or 0)}
        for t in tickers
    }
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
