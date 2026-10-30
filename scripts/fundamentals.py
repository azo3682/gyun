# -*- coding: utf-8 -*-
"""
scripts/fundamentals.py
중장기 관점(삼박자: 가치·가격·정보 + 탑다운) 보조 판단 모듈 — 순수 함수만 있다(네트워크·streamlit·secrets 의존 없음).
value_screen.py / morning_report.py / streamlit_app.py 가 함께 import 한다.

이미 있는 것은 새로 만들지 않고 재사용한다:
  - 가치: common.composite_score(ROE·부채비율·성장률·PER/PBR 종합점수) — 여기서는 그 점수를 받아 구간만 나눈다.
  - 정보: 공시 리스크(check_disclosure_risk)와 거래소 지정 상태(market_risk_flags) — 여기서는 받아서 반영만 한다.
  - 시장 지수: signal_tracker 가 쌓는 data/index_history.json (새로 내려받지 않는다).
이 모듈이 새로 하는 일: 업종 상대평가 · 장기 추세(60/120일선) · 중장기 뱃지 · 시장 국면(탑다운).

※ 정직성 원칙: 삼박자·탑다운이라는 '큰 틀'만 이정윤 씨 방식에서 가져왔다. 아래 모든 숫자 임계값은
  그가 공개한 기준이 아니라 일반적인 관행을 바탕으로 내가 정한 값이고 백테스트로 검증되지 않았다.
  그래서 화면에는 항상 '참고용·미검증'으로 표시한다. 뉴스·공시의 호재/악재는 자동 판정하지 않는다.
"""

import statistics

# =====================================================================
# 1. 업종 상대평가 (금융·건설·해운 등은 원래 PBR이 낮아서 절대 기준만으로는 왜곡된다)
# =====================================================================
SECTOR_MIN_N = 5            # 업종 표본이 이보다 적으면 비교하지 않는다
REL_CHEAP_RATIO = 0.7       # 업종 중앙값의 70% 이하 → 업종 대비 저평가
REL_EXPENSIVE_RATIO = 1.3   # 130% 이상 → 업종 대비 고평가


def compute_sector_stats(rows: list, min_n: int = SECTOR_MIN_N) -> dict:
    """{업종명: {n, median_per, median_pbr}} — PER은 흑자(>0) 종목만, PBR은 >0 종목만 중앙값에 포함."""
    by_sector = {}
    for r in rows:
        s = r.get("sector") or ""
        if s:
            by_sector.setdefault(s, []).append(r)
    stats = {}
    for s, items in by_sector.items():
        if len(items) < min_n:
            continue
        pers = [x["per"] for x in items if x.get("per") is not None and x["per"] > 0]
        pbrs = [x["pbr"] for x in items if x.get("pbr") is not None and x["pbr"] > 0]
        stats[s] = {
            "n": len(items),
            "median_per": round(statistics.median(pers), 2) if len(pers) >= min_n else None,
            "median_pbr": round(statistics.median(pbrs), 2) if len(pbrs) >= min_n else None,
        }
    return stats


def relative_valuation(per, pbr, sector: str, sector_stats: dict | None):
    """업종 중앙값 대비 비율로 판정. 반환 {label, per_ratio, pbr_ratio, avg_ratio, sector, n} 또는 None(비교 불가)."""
    st = (sector_stats or {}).get(sector or "")
    if not st:
        return None
    ratios, per_ratio, pbr_ratio = [], None, None
    if per is not None and per > 0 and st.get("median_per"):
        per_ratio = round(per / st["median_per"], 2)
        ratios.append(per_ratio)
    if pbr is not None and pbr > 0 and st.get("median_pbr"):
        pbr_ratio = round(pbr / st["median_pbr"], 2)
        ratios.append(pbr_ratio)
    if not ratios:
        return None
    avg = sum(ratios) / len(ratios)
    if avg <= REL_CHEAP_RATIO:
        label = "🟢 업종 대비 저평가"
    elif avg >= REL_EXPENSIVE_RATIO:
        label = "🔴 업종 대비 고평가"
    else:
        label = "⚪ 업종 평균 수준"
    return {"label": label, "per_ratio": per_ratio, "pbr_ratio": pbr_ratio,
            "avg_ratio": round(avg, 2), "sector": sector, "n": st["n"]}


# =====================================================================
# 2. 가치 축 — 기존 종합점수(0~100)를 구간으로 나눈다
# =====================================================================
VALUE_GOOD_MIN = 75.0       # common.score_label 의 '우수' 기준과 같다
VALUE_OK_MIN = 55.0         # '보통' 기준과 같다
VALUE_MIN_COVERAGE = 0.8    # 종합점수에 반영된 가중치 비율이 이보다 낮으면 점수를 믿지 않고 '데이터 부족'


def judge_value(score, coverage, rel: dict | None = None, oneoff_suspect: bool = False) -> dict:
    """{level: good|ok|poor|None, reasons[]}. 점수가 없거나 반영 비율이 낮으면 판단 보류(None).
    업종 대비 고평가면 good → ok 로 한 단계 낮춘다(업종 특성 반영). 일회성 이익 의심이면 good → ok."""
    if score is None or (coverage is not None and coverage < VALUE_MIN_COVERAGE):
        why = "재무비율 조회 실패" if score is None else f"종합점수 반영 항목 부족(가중치 {coverage:.0%}만 반영)"
        return {"level": None, "reasons": [why]}
    reasons = [f"종합점수 {score:.0f}점"]
    if score >= VALUE_GOOD_MIN:
        level = "good"
    elif score >= VALUE_OK_MIN:
        level = "ok"
    else:
        level = "poor"
    if level == "good" and rel and rel["label"].startswith("🔴"):
        level = "ok"
        reasons.append(f"업종 대비 고평가(업종 중앙값의 {rel['avg_ratio']:.2f}배)라 한 단계 낮춤")
    if level == "good" and oneoff_suspect:
        level = "ok"
        reasons.append("ROE가 너무 높아 일회성 이익 의심 — 한 단계 낮춤")
    if rel and rel["label"].startswith("🟢"):
        reasons.append(f"업종 대비 저평가(업종 중앙값의 {rel['avg_ratio']:.2f}배)")
    return {"level": level, "reasons": reasons}


# =====================================================================
# 3. 가격 축 — 장기 이동평균 (60일/120일)
# =====================================================================
def judge_price_axis(df) -> dict:
    """df: 'stck_clpr' 컬럼의 일봉(오래된 순). 120일선까지 계산되려면 약 140행 이상 필요.
    반환 {state: up|mixed|down|None, label, detail, partial}"""
    if df is None or len(df) < 80 or "stck_clpr" not in df:
        return {"state": None, "label": "❔ 장기추세 판단불가(데이터 부족)", "detail": "", "partial": True}
    close = df["stck_clpr"].astype(float)
    ma60 = close.rolling(60).mean()
    last = float(close.iloc[-1])
    ma60_now, ma60_prev = ma60.iloc[-1], ma60.iloc[-21]
    slope60 = ma60_now > ma60_prev
    if len(close) >= 140:
        ma120 = close.rolling(120).mean()
        ma120_now, ma120_prev = ma120.iloc[-1], ma120.iloc[-21]
        slope120 = ma120_now > ma120_prev
        if last > ma120_now and ma60_now > ma120_now and slope120 and slope60:
            return {"state": "up", "label": "🟢 장기 상승추세", "partial": False,
                    "detail": "현재가>120일선, 60일선>120일선, 60·120일선 모두 상승"}
        if last < ma120_now and not slope120:
            return {"state": "down", "label": "🔴 장기 하락추세", "partial": False,
                    "detail": "현재가<120일선이고 120일선 하락"}
        return {"state": "mixed", "label": "🟡 장기 추세 불분명", "partial": False,
                "detail": "120일선 기준 상승·하락 조건을 모두 충족하지 않음"}
    if last > ma60_now and slope60:
        return {"state": "up", "label": "🟢 중기 상승추세(60일선 기준)", "partial": True,
                "detail": "현재가>60일선, 60일선 상승 (120일선은 데이터 부족으로 미확인)"}
    if last < ma60_now and not slope60:
        return {"state": "down", "label": "🔴 중기 하락추세(60일선 기준)", "partial": True,
                "detail": "현재가<60일선, 60일선 하락 (120일선은 데이터 부족으로 미확인)"}
    return {"state": "mixed", "label": "🟡 중기 추세 불분명(60일선 기준)", "partial": True,
            "detail": "60일선 기준 조건 미충족 (120일선은 데이터 부족으로 미확인)"}


# =====================================================================
# 4. 중장기 뱃지 (가치 × 가격 × 리스크) — 단기 🎯 전환신호와 별개
# =====================================================================
BADGE_CAVEAT = "참고용·미검증 (임계값은 일반 관행 기준으로 직접 정한 값이며 백테스트로 검증되지 않았습니다)"


def mid_long_badge(value: dict, price: dict, risks: list | None = None) -> str:
    """risks: 거래소 지정 상태 플래그·주의 공시 제목 등 '중장기 부적합' 사유 목록. 하나라도 있으면 🚫."""
    if risks:
        return "🚫 중장기 부적합 (리스크 이벤트)"
    lv = (value or {}).get("level")
    if lv is None:
        return "❔ 재무데이터 부족 (판단 보류)"
    ps = (price or {}).get("state")
    if lv == "poor":
        return "🚫 중장기 메리트 부족"
    if lv == "good" and ps == "up":
        return "🌱 중장기 메리트 있음"
    if lv == "good":
        return "👀 가치 양호 · 추세 확인 대기"
    if ps == "up":
        return "⚪ 보통 · 추세는 양호"
    return "⚪ 보통"


def build_mid_long(per, pbr, sector: str, score, coverage, sector_stats: dict | None, df_long,
                   risks: list | None = None, oneoff_suspect: bool = False) -> dict:
    """한 종목의 중장기 판단 묶음 (JSON 저장 가능). score/coverage 는 common.composite_score 결과."""
    rel = relative_valuation(per, pbr, sector, sector_stats)
    value = judge_value(score, coverage, rel, oneoff_suspect)
    price = judge_price_axis(df_long)
    return {"badge": mid_long_badge(value, price, risks), "caveat": BADGE_CAVEAT,
            "value": value, "price": price, "relative": rel, "risks": list(risks or [])}


# =====================================================================
# 5. 시장 국면 (탑다운 1단계) — 지수 종가 시계열로 판정
# =====================================================================
def classify_market_regime(closes) -> dict:
    """closes: 지수 종가 Series(오래된 순, 80개 이상). 120개 이상이면 120일선도 본다.
    규칙(내가 정한 것): 지수>60일선, 60일선 상승(20일 전 대비), 20일선>60일선, 지수>120일선 각 1점."""
    if closes is None or len(closes) < 80:
        return {"state": None, "label": "❔ 시장국면 판단불가(데이터 부족)", "score": None, "detail": [], "advice": ""}
    c = closes.astype(float).reset_index(drop=True)
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    last = float(c.iloc[-1])
    checks = [(last > ma60.iloc[-1], "지수>60일선"),
              (ma60.iloc[-1] > ma60.iloc[-21], "60일선 상승"),
              (ma20.iloc[-1] > ma60.iloc[-1], "20일선>60일선")]
    if len(c) >= 120:
        checks.append((last > c.rolling(120).mean().iloc[-1], "지수>120일선"))
    score = sum(int(bool(ok)) for ok, _ in checks)
    detail = [("✅ " if ok else "❌ ") + txt for ok, txt in checks]
    hi = float(c.iloc[-min(len(c), 252):].max())
    dd = (last / hi - 1) * 100
    detail.append(f"기간 내 고점 대비 {dd:+.1f}%")
    ratio = score / len(checks)
    if ratio >= 0.75:
        state, label = "bull", "🟢 상승 국면"
        advice = "시장 추세가 우호적입니다. 개별 종목 신호를 평소 기준대로 참고할 수 있습니다."
    elif ratio >= 0.5:
        state, label = "mixed", "🟡 혼조 국면"
        advice = "방향이 불분명합니다. 신규 매수 비중을 줄이고 손절선을 엄격히 지키는 쪽이 일반적 원칙입니다."
    else:
        state, label = "bear", "🔴 약세 국면"
        advice = "시장 추세가 약합니다. 신규 매수를 줄이고 현금 비중을 높이는 것을 검토하는 것이 일반적 원칙입니다."
    return {"state": state, "label": label, "score": f"{score}/{len(checks)}", "detail": detail,
            "advice": advice, "last": round(last, 2), "drawdown_pct": round(dd, 1), "n_days": len(c)}


def closes_from_history(hist_for_index: dict):
    """index_history.json 의 한 지수({'YYYY-MM-DD': {'open':..,'close':..}}) → 종가 Series (날짜 오름차순). 없으면 None."""
    import pandas as pd
    if not hist_for_index:
        return None
    try:
        items = sorted((d, v["close"]) for d, v in hist_for_index.items() if isinstance(v, dict) and v.get("close"))
    except Exception:
        return None
    return pd.Series([x[1] for x in items], index=[x[0] for x in items]) if items else None


def regime_from_history(index_history: dict | None) -> dict:
    """{kospi, kosdaq, overall_label, overall_advice, note, data_through}"""
    ih = index_history or {}
    out = {}
    for key, name in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        out[key] = classify_market_regime(closes_from_history(ih.get(name)))
    states = [out[k]["state"] for k in ("kospi", "kosdaq") if out[k]["state"]]
    if not states:
        out["overall_label"], out["overall_advice"] = "❔ 시장국면 판단불가", ""
    elif "bear" in states:
        out["overall_label"] = "🔴 약세 국면 포함 — 신규 매수 주의"
        out["overall_advice"] = "코스피·코스닥 중 한쪽 이상이 약세입니다. 신규 진입은 비중을 줄이는 쪽을 권합니다."
    elif all(s == "bull" for s in states):
        out["overall_label"], out["overall_advice"] = "🟢 상승 국면", "시장 환경이 우호적입니다."
    else:
        out["overall_label"], out["overall_advice"] = "🟡 혼조 국면", "방향이 불분명합니다. 비중 조절을 권합니다."
    dates = sorted(d for d in (ih.get("KOSPI") or {}) if isinstance(d, str))
    out["data_through"] = dates[-1] if dates else None
    out["note"] = "지수 이동평균 기반의 규칙형 판정(내가 정한 기준, 미검증). 지수 이력은 신호 추적 기록(index_history.json)을 사용"
    return out
