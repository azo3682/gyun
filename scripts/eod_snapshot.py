# -*- coding: utf-8 -*-
"""
scripts/eod_snapshot.py
평일 15:40 KST에 GitHub Actions로 실행.

오늘의 순매수 상위10 + 전환신호(2026-09-29부터 방향 조건 추가, 재검증 전 참고용) +
참고 기술지표 + 공시 + 뉴스를 계산해서 data/eod_snapshot.json 에 저장한다.

2026-09-29 변경: "관찰 후보/재진입 후보"(당일 급등한 전환신호를 며칠 미뤘다가 눌림 오면
다시 후보로 올리는 로직)를 완전히 제거했다. 같은 날 백테스트로 확인한 결과, 급등 후
15거래일 안에 눌림이 오는 비율이 98.2%(사실상 전부)였고, 눌린 뒤 수익률도 아무 날에나
매수한 경우(기준선)와 통계적으로 다르지 않았다 — 즉 이 필터가 실제로 걸러내는 게 없었다.
대신 전환신호가 당일 이미 크게 오른 경우, 후보 목록에 그대로 두되 그 사실(당일수익률)만
같이 보여주고 판단은 보는 사람이 하도록 바꿨다.
"""

import json
import os
from datetime import datetime

from common import (
    fetch_investor_ranking, fetch_daily_ohlcv, analyze_technicals,
    compute_transition_signal, compute_day_return, fetch_valuation_and_risk, is_cheap,
    check_disclosure_risk, fetch_news, KST,
)

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "eod_snapshot.json")


def build_snapshot():
    buy_rows = fetch_investor_ranking("buy")
    sell_rows = fetch_investor_ranking("sell")

    enriched = []
    for row in buy_rows:
        code, name = row["stock_code"], row["stock_name"]
        df = fetch_daily_ohlcv(code)
        tech = analyze_technicals(df)
        raw_transition = compute_transition_signal(df)
        day_pct = compute_day_return(df)
        checks = {k: v for k, v in tech.items() if k != "RSI값"}
        passed = sum(1 for v in checks.values() if v is True)
        total = sum(1 for v in checks.values() if v is not None)
        risky = check_disclosure_risk(code)
        news = fetch_news(name)
        valuation, market_flags = fetch_valuation_and_risk(code)   # PER/PBR과 거래소 위험 상태를 한 번에
        # 관리종목·투자위험 등 거래소 지정 상태면 '거래량급증'이 매수세가 아니라 투매일 수 있어
        # 검증된 신호로 인정하지 않는다 (백테스트 표본에 이런 상태의 종목은 없었다)
        transition = raw_transition and not market_flags

        enriched.append({
            **row, "tech": tech, "transition": transition, "day_pct": day_pct,
            "valuation": valuation, "market_risk_flags": market_flags,
            "passed": passed, "total": total,
            "risky_disclosures": risky, "news": news,
        })

    # 전환신호 우선, 그다음 참고점수, 그다음 원래 순위
    enriched.sort(key=lambda r: (not r["transition"], -r["passed"], r["rank"]))

    # 오늘 전환신호이면서 저평가 조건(0<PER<=15, PBR<=1.5)도 충족하는 종목
    value_overlap = []
    for r in enriched:
        if r["transition"] and is_cheap(r.get("valuation")):
            v = r["valuation"]
            value_overlap.append({
                "stock_code": r["stock_code"], "stock_name": r["stock_name"],
                "per": v["per"], "pbr": v["pbr"], "matched_via": "전환신호",
            })

    snapshot = {
        "date": datetime.now(KST).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "buy_top10": enriched,
        "sell_top10": sell_rows,
        "value_overlap": value_overlap,
    }
    return snapshot


if __name__ == "__main__":
    snapshot = build_snapshot()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    n_transition = sum(1 for r in snapshot["buy_top10"] if r["transition"])
    n_chased = sum(1 for r in snapshot["buy_top10"]
                   if r["transition"] and r["day_pct"] is not None and r["day_pct"] >= 0.07)
    print(f"저장 완료: {OUT_PATH}")
    print(f"전환신호 {n_transition}개 (그중 당일+7%↑ 이미 급등 {n_chased}개) / "
          f"저평가+전환신호 겹침 {len(snapshot['value_overlap'])}개")
