# -*- coding: utf-8 -*-
"""
scripts/intraday_recheck.py

평일 장중 09:40 / 10:10 / 11:30 / 13:30 / 14:40 KST에 실행
(GitHub Actions, 각 수급 갱신 시점 직후).

아침에 확정한 "오늘의 스윙 후보"(data/eod_snapshot.json — 전일 마감 기준)를
지금 시점의 실시간 수급·기술적 조건·공시·현재가로 다시 검사해서:
  - 더 이상 조건을 못 채우면 -> "제외 후보" (사유 포함)
  - 아침엔 후보가 아니었지만 지금 조건을 채우면 -> "신규 후보"
  - 계속 조건을 채우면 -> "유지"
결과를 data/intraday_status.json 에 저장 (대시보드가 이걸 읽어서 표시).
"""

import json
import os
from datetime import datetime

from common import (
    fetch_investor_ranking, fetch_daily_ohlcv, analyze_technicals,
    check_disclosure_risk, fetch_current_price, KST,
)

CANDIDATE_SCORE_THRESHOLD = 3  # 5개 중 3개 이상 통과해야 "후보" 자격
PRICE_DROP_THRESHOLD = -3.0    # 당일 이 % 이상 하락하면 즉시 제외
SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "eod_snapshot.json")
STATUS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "intraday_status.json")


def load_morning_candidates() -> list[dict]:
    if not os.path.exists(SNAPSHOT_PATH):
        return []
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        snapshot = json.load(f)
    return [r for r in snapshot.get("buy_top10", []) if r.get("passed", 0) >= CANDIDATE_SCORE_THRESHOLD]


def evaluate_stock(code: str, name: str) -> dict:
    df = fetch_daily_ohlcv(code)
    tech = analyze_technicals(df)
    checks = {k: v for k, v in tech.items() if k != "RSI값"}
    passed = sum(1 for v in checks.values() if v is True)
    total = sum(1 for v in checks.values() if v is not None)
    risky = check_disclosure_risk(code)
    price, day_pct = fetch_current_price(code)
    return {"stock_code": code, "stock_name": name, "tech": tech,
            "passed": passed, "total": total, "risky_disclosures": risky,
            "current_price": price, "day_pct": day_pct}


def run():
    morning_candidates = load_morning_candidates()
    morning_codes = {c["stock_code"] for c in morning_candidates}

    # 지금 이 순간의 순매수 상위 10 (실시간에 가까운 최신 랭킹)
    current_buy = fetch_investor_ranking("buy")
    current_codes = {r["stock_code"]: r["stock_name"] for r in current_buy}

    kept, excluded, new_candidates = [], [], []

    # 1) 아침 후보들을 재검사
    for cand in morning_candidates:
        code, name = cand["stock_code"], cand["stock_name"]
        still_in_ranking = code in current_codes
        eval_result = evaluate_stock(code, name)

        if not still_in_ranking:
            excluded.append({**eval_result, "reason": "순매수 상위 10에서 이탈"})
        elif eval_result["day_pct"] is not None and eval_result["day_pct"] <= PRICE_DROP_THRESHOLD:
            excluded.append({**eval_result, "reason": f"당일 가격 {eval_result['day_pct']:.2f}% 하락"})
        elif eval_result["passed"] < CANDIDATE_SCORE_THRESHOLD:
            excluded.append({**eval_result, "reason": f"기술적 점수 하락 ({eval_result['passed']}/5)"})
        elif eval_result["risky_disclosures"]:
            excluded.append({**eval_result, "reason": "장중 주의 공시 발생"})
        else:
            kept.append(eval_result)

    # 2) 아침엔 후보가 아니었지만 지금 새로 조건을 채운 종목
    for code, name in current_codes.items():
        if code in morning_codes:
            continue
        eval_result = evaluate_stock(code, name)
        if (eval_result["passed"] >= CANDIDATE_SCORE_THRESHOLD and not eval_result["risky_disclosures"]
                and (eval_result["day_pct"] is None or eval_result["day_pct"] > PRICE_DROP_THRESHOLD)):
            new_candidates.append(eval_result)

    status = {
        "checked_at":
