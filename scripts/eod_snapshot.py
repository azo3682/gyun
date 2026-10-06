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

import csv
import json
import os
from datetime import datetime

from common import (
    fetch_investor_ranking, fetch_daily_ohlcv, analyze_technicals,
    compute_transition_signal, compute_day_return, fetch_valuation_and_risk, is_cheap,
    check_disclosure_risk, fetch_news, KST,
    fetch_financial_ratio, strip_raw, composite_score, fetch_volume_ranking, fetch_price_detail,
)
import signal_tracker

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "eod_snapshot.json")

# '📌 신호 추적' 탭에는 두 종류의 신호를 따로 기록한다 (signal_tracker.py 참고):
#   - 전환신호: 아래에서 순매수 상위 10 종목마다 계산한 transition이 True인 종목
#   - 거래량·수급 동시: 순매수 상위 10 중 거래량 상위에도 오른 종목 (volume_rank가 있는 종목)
# 전환신호는 순매수 상위 10 안에서만 계산한다. 범위를 넓히려면 아래 루프가 도는 종목 목록을 늘려야 한다.


HISTORY_FIELDS = [
    "date", "rank", "stock_code", "stock_name", "transition", "day_pct", "passed", "total",
    "foreign_net", "inst_net", "combined_net", "per", "pbr", "roe", "debt_ratio", "sales_growth",
    "op_growth", "fin_yymm", "score", "market_risk_flags", "risky_disclosure_count",
]


def append_history(snapshot: dict, history_dir: str):
    """오늘의 순매수 상위 10(전환신호 여부 포함)을 data/history/eod_candidates.csv에 누적한다.
    eod_snapshot.json은 매일 덮어쓰이므로, 전환신호가 실제로 통했는지 나중에 검증하려면 따로 쌓아야 한다.
    같은 날 다시 돌리면 그 날짜 행을 지우고 새로 쓴다. 진입 기준 가격은 이 날짜(15:40 마감 후)의 종가로 본다."""
    os.makedirs(history_dir, exist_ok=True)
    path = os.path.join(history_dir, "eod_candidates.csv")
    date = snapshot["date"]
    kept = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            kept = [r for r in csv.DictReader(f) if r.get("date") != date]
    new_rows = []
    for r in snapshot["buy_top10"]:
        v, fund = r.get("valuation") or {}, r.get("fundamentals") or {}
        new_rows.append({
            "date": date, "rank": r["rank"], "stock_code": r["stock_code"], "stock_name": r["stock_name"],
            "transition": int(bool(r["transition"])), "day_pct": r.get("day_pct"),
            "passed": r["passed"], "total": r["total"],
            "foreign_net": r.get("foreign_net"), "inst_net": r.get("inst_net"), "combined_net": r.get("combined_net"),
            "per": v.get("per"), "pbr": v.get("pbr"), "roe": fund.get("roe"), "debt_ratio": fund.get("debt_ratio"),
            "sales_growth": fund.get("sales_growth"), "op_growth": fund.get("op_growth"),
            "fin_yymm": fund.get("stac_yymm"), "score": (r.get("score") or {}).get("score"),
            "market_risk_flags": ";".join(r.get("market_risk_flags") or []),
            "risky_disclosure_count": len(r.get("risky_disclosures") or []),
        })
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        w.writeheader()
        w.writerows(kept)
        w.writerows(new_rows)
    return path, len(new_rows)


def build_snapshot():
    buy_rows = fetch_investor_ranking("buy")
    sell_rows = fetch_investor_ranking("sell")

    # 거래량 상위 순위 (신호 추적용). 실패하면 volume_rank_ok=False로 남기고, 그 날은 신호를 기록하지 않는다.
    try:
        volume_rows = fetch_volume_ranking()
        volume_ok = True
    except Exception as e:
        print(f"거래량 순위 조회 실패: {e}")
        volume_rows, volume_ok = [], False
    volume_rank_map = {v["stock_code"]: v["rank"] for v in volume_rows}

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
        fin = fetch_financial_ratio(code)                          # 정식 ROE·부채비율·성장률 (실패하면 None)
        score = (composite_score((valuation or {}).get("per"), (valuation or {}).get("pbr"), fin)
                 if fin else None)
        # 관리종목·투자위험 등 거래소 지정 상태면 '거래량급증'이 매수세가 아니라 투매일 수 있어
        # 검증된 신호로 인정하지 않는다 (백테스트 표본에 이런 상태의 종목은 없었다)
        transition = raw_transition and not market_flags

        last_date = str(df["stck_bsop_date"].iloc[-1]) if not df.empty else None
        last_close = float(df["stck_clpr"].iloc[-1]) if not df.empty else None

        enriched.append({
            **row, "tech": tech, "transition": transition, "day_pct": day_pct,
            "volume_rank": volume_rank_map.get(code), "last_date": last_date, "last_close": last_close,
            "valuation": valuation, "market_risk_flags": market_flags,
            "fundamentals": strip_raw(fin), "score": score,
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
                "score": (r.get("score") or {}).get("score"),
            })

    snapshot = {
        "date": datetime.now(KST).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "buy_top10": enriched,
        "sell_top10": sell_rows,
        "volume_rank_ok": volume_ok,
        "volume_top": [{"rank": v["rank"], "stock_code": v["stock_code"], "stock_name": v["stock_name"]}
                       for v in volume_rows],
        "value_overlap": value_overlap,
    }
    return snapshot


if __name__ == "__main__":
    snapshot = build_snapshot()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    try:
        hist_path, hist_n = append_history(snapshot, os.path.join(os.path.dirname(OUT_PATH), "history"))
        print(f"이력 누적: {hist_path} (오늘 {hist_n}행)")
    except Exception as e:
        print(f"이력 누적 실패(스냅샷에는 영향 없음): {e}")
    # 신호 추적: 오늘 신호를 기록하고, 진행 중인 신호의 종가를 갱신한다 (실패해도 스냅샷에는 영향 없음)
    try:
        res = signal_tracker.run(
            snapshot, fetcher=fetch_daily_ohlcv,
            market_lookup=lambda code: (fetch_price_detail(code) or {}).get("rprs_mrkt_kor_name"),
            market_probe=signal_tracker.probe_market_yf)
        for t, label in signal_tracker.SIGNAL_TYPES.items():
            print(f"신호 추적[{label}]: 오늘 {res['found'][t]}개(신규 기록 {res['added'][t]}개) / "
                  f"진행 중 {res['active'][t]}개 / 누적 {res['total'][t]}개")
        idx_msg = ", ".join(f"{k} {'OK' if v else '실패'}" for k, v in res["index_status"].items()) or "신호 없어 생략"
        print(f"신호 추적: 종가 갱신 {res['updated']}건, 조회 실패 {res['failed']}건, 다음 날 시가 기록된 신호 {res['with_entry']}건, 지수 이력({idx_msg})"
              + ("" if res["volume_ok"] else " — 거래량 순위 조회 실패로 오늘은 '거래량·수급 동시' 신호를 기록하지 않음"))
        b = res["baseline"]
        print(f"대조군(순매수 상위 10 전체): 오늘 신규 {b['added']}개 + 히스토리 소급 {b['backfilled']}개 / 진행 중 {b['active']}개 / 누적 {b['total']}개 / "
              f"다음 날 시가 기록된 항목 {b['with_entry']}개 (종가 갱신 {b['updated']}건, 조회 실패 {b['failed']}건)")
        if res["unresolved_markets"]:      # 코스피/코스닥을 못 가린 종목: KIS가 준 원본 시장명을 남긴다 (지수 대비 비교가 비는 원인 확인용)
            print("시장 판별 실패(지수 대비 비교 불가): " + ", ".join(f"{c} {n} (KIS 시장명={raw!r})" for c, n, raw in res["unresolved_markets"][:15]))
    except Exception as e:
        print(f"신호 추적 실패(스냅샷에는 영향 없음): {e}")
    n_transition = sum(1 for r in snapshot["buy_top10"] if r["transition"])
    n_chased = sum(1 for r in snapshot["buy_top10"]
                   if r["transition"] and r["day_pct"] is not None and r["day_pct"] >= 0.07)
    print(f"저장 완료: {OUT_PATH}")
    print(f"전환신호 {n_transition}개 (그중 당일+7%↑ 이미 급등 {n_chased}개) / "
          f"저평가+전환신호 겹침 {len(snapshot['value_overlap'])}개")
