# -*- coding: utf-8 -*-
"""
scripts/eod_snapshot.py
평일 15:40 KST에 GitHub Actions로 실행.

1) 오늘의 순매수 상위10 + 전환신호(검증된 유일한 신호) + 참고 기술지표 +
   공시 + 뉴스를 계산해서 data/eod_snapshot.json 에 저장한다.
2) "관찰 후보" 관리: 전환신호가 떴지만 당일 이미 +7% 이상 급등해서
   추격매수가 부담스러운 종목은 즉시 후보로 넣지 않고 data/watchlist.json에
   등록해두고, 이후 며칠 안에 다시 VCP(눌림) 상태로 돌아오면 그날
   "재진입 후보"로 승격시킨다. 15거래일 안에 눌림이 안 오면 관찰 목록에서
   자동 제외한다.

2026-09-21 결정 배경: 당일 급등형 vs 완만형 전환신호를 비교했지만
표본 부족(75개)으로 통계적 결론을 못 냈음. 필터로 걸러내는 대신,
"급등형은 관찰 후보로 미루고 눌림을 기다린다"는 원칙으로 대응하기로 함.
"""

import json
import os
from datetime import datetime

from common import (
    fetch_investor_ranking, fetch_daily_ohlcv, analyze_technicals,
    compute_transition_signal, compute_day_return, fetch_valuation_rank,
    check_disclosure_risk, fetch_news, KST,
)

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "eod_snapshot.json")
WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "watchlist.json")

CHASE_THRESHOLD = 0.07   # 당일 이 이상 오르면 "이미 급등" -> 관찰 후보로 미룸
MAX_WATCH_DAYS = 15      # 이 기간 안에 눌림이 안 오면 관찰 목록에서 제외


def load_watchlist() -> dict:
    if not os.path.exists(WATCHLIST_PATH):
        return {}
    try:
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_watchlist(watchlist: dict):
    os.makedirs(os.path.dirname(WATCHLIST_PATH), exist_ok=True)
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)


def process_watchlist(watchlist: dict) -> list:
    """기존 관찰 후보들을 재검사. 눌림 오면 재진입 후보로 반환하고 목록에서 제거.
    기간 초과면 제거만. 나머지는 days_watched +1 해서 유지."""
    reentry_candidates = []
    still_watching = {}

    for code, entry in watchlist.items():
        df = fetch_daily_ohlcv(code)
        tech = analyze_technicals(df)
        is_vcp_now = tech.get("변동성수축(VCP)") is True

        if is_vcp_now:
            reentry_candidates.append({
                "stock_code": code, "stock_name": entry["stock_name"],
                "initial_pct": entry["initial_pct"], "days_watched": entry["days_watched"],
                "tech": tech,
            })
            continue  # 목록에서 제거 (재진입 후보로 승격, still_watching에 안 넣음)

        new_days = entry["days_watched"] + 1
        if new_days > MAX_WATCH_DAYS:
            continue  # 기간 초과로 제외

        still_watching[code] = {**entry, "days_watched": new_days}

    return reentry_candidates, still_watching


def build_snapshot():
    buy_rows = fetch_investor_ranking("buy")
    sell_rows = fetch_investor_ranking("sell")

    watchlist = load_watchlist()
    reentry_candidates, watchlist = process_watchlist(watchlist)
    reentry_codes = {r["stock_code"] for r in reentry_candidates}

    enriched = []
    for row in buy_rows:
        code, name = row["stock_code"], row["stock_name"]
        df = fetch_daily_ohlcv(code)
        tech = analyze_technicals(df)
        transition = compute_transition_signal(df)
        day_pct = compute_day_return(df)
        checks = {k: v for k, v in tech.items() if k != "RSI값"}
        passed = sum(1 for v in checks.values() if v is True)
        total = sum(1 for v in checks.values() if v is not None)
        risky = check_disclosure_risk(code)
        news = fetch_news(name)

        deferred = False
        if transition and day_pct is not None and day_pct >= CHASE_THRESHOLD and code not in watchlist:
            # 이미 급등한 전환신호 -> 즉시 후보 대신 관찰 목록으로
            watchlist[code] = {"stock_name": name, "initial_pct": day_pct, "days_watched": 0}
            deferred = True

        enriched.append({
            **row, "tech": tech, "transition": transition, "day_pct": day_pct,
            "deferred_to_watchlist": deferred,
            "passed": passed, "total": total,
            "risky_disclosures": risky, "news": news,
        })

    # 전환신호(관찰 목록으로 안 미뤄진 것) 우선, 그다음 참고점수, 그다음 원래 순위
    enriched.sort(key=lambda r: (not (r["transition"] and not r["deferred_to_watchlist"]),
                                   -r["passed"], r["rank"]))

    save_watchlist(watchlist)

    # 저평가(PER 낮은 순) + 오늘 전환신호가 겹치는 종목 — 가장 근거가 탄탄한 조합
    value_overlap = []
    try:
        value_rows = fetch_valuation_rank(sort_code="23", top_n=30)
        transition_codes = {r["stock_code"]: r for r in enriched if r["transition"] and not r["deferred_to_watchlist"]}
        for v in value_rows:
            if v["stock_code"] in transition_codes:
                value_overlap.append({**v, "matched_via": "전환신호"})
    except Exception as e:
        print(f"저평가 순위 조회 실패(건너뜀): {e}")

    snapshot = {
        "date": datetime.now(KST).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "buy_top10": enriched,
        "sell_top10": sell_rows,
        "reentry_candidates": reentry_candidates,
        "watchlist_size": len(watchlist),
        "value_overlap": value_overlap,
    }
    return snapshot


if __name__ == "__main__":
    snapshot = build_snapshot()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    n_transition = sum(1 for r in snapshot["buy_top10"] if r["transition"] and not r["deferred_to_watchlist"])
    n_deferred = sum(1 for r in snapshot["buy_top10"] if r["deferred_to_watchlist"])
    print(f"저장 완료: {OUT_PATH}")
    print(f"전환신호(즉시후보) {n_transition}개 / 관찰목록 신규편입 {n_deferred}개 / "
          f"재진입후보 {len(snapshot['reentry_candidates'])}개 / 관찰목록 총 {snapshot['watchlist_size']}개 / "
          f"저평가+전환신호 겹침 {len(snapshot['value_overlap'])}개")
