# -*- coding: utf-8 -*-
"""
scripts/eod_snapshot.py
평일 15:40 KST에 GitHub Actions로 실행.
오늘의 최종 순매수/순매도 상위10 + 기술적 점수 + 공시 + 뉴스를 계산해서
data/eod_snapshot.json 에 저장한다. (다음날 아침 리포트가 이 파일을 읽음)
"""

import json
import os
from datetime import datetime

from common import (
    fetch_investor_ranking, fetch_daily_ohlcv, analyze_technicals,
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
        checks = {k: v for k, v in tech.items() if k != "RSI값"}
        passed = sum(1 for v in checks.values() if v is True)
        total = sum(1 for v in checks.values() if v is not None)
        risky = check_disclosure_risk(code)
        news = fetch_news(name)
        enriched.append({
            **row, "tech": tech, "passed": passed, "total": total,
            "risky_disclosures": risky, "news": news,
        })

    enriched.sort(key=lambda r: (-r["passed"], r["rank"]))

    snapshot = {
        "date": datetime.now(KST).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "buy_top10": enriched,
        "sell_top10": sell_rows,
    }
    return snapshot


if __name__ == "__main__":
    snapshot = build_snapshot()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"저장 완료: {OUT_PATH}")
