# -*- coding: utf-8 -*-
"""
scripts/market_tab.py — "시장 상태" 탭 (코스피 지수 기준)

KODEX 200 판단 엔진(k200_view)을 코스피 지수 일봉에 그대로 적용한다. 새 판단 규칙을 만들지 않는다.
  - 일봉: data/k200/kospi_daily.json (수집기가 지수 일봉 API로 저장) → 없으면 market_flow.json의 지수 시·고·저·종.
  - 지수에는 거래량이 없을 수 있어 거래량 신호는 '계산불가'로 빠진다 (엔진이 volume 0을 그렇게 처리한다).
  - 시·고·저가가 없는 날은 만들어 채우지 않는다. 캔들이 거짓이 되기 때문이다.
순수 함수(build_index_daily)는 네트워크·streamlit 없이 테스트한다.
"""

from __future__ import annotations

import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

LOCAL_DIR = os.path.join(HERE, "..", "data", "k200")
MIN_BARS = 30


def _valid(o, h, l, c) -> bool:
    try:
        o, h, l, c = float(o), float(h), float(l), float(c)
    except Exception:
        return False
    return min(o, h, l, c) > 0 and h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9


def build_index_daily(kospi_rows, flow_rows) -> tuple[pd.DataFrame, dict]:
    """(일봉 DataFrame, 출처 정보). 지수 일봉을 우선 쓰고, 그 뒤 날짜는 수급 파일의 지수 시·고·저·종으로 잇는다.
    OHLC가 일관되지 않은 행은 버리고 개수를 알려 준다."""
    by_date, dropped = {}, 0
    src = {"kospi_daily": 0, "market_flow": 0, "dropped": 0, "last_close_only": None}
    for r in kospi_rows or []:
        d = str(r.get("date") or "")
        if len(d) == 8 and _valid(r.get("open"), r.get("high"), r.get("low"), r.get("close")):
            by_date[d] = {"date": d, "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
                          "close": float(r["close"]), "volume": float(r.get("volume") or 0)}
            src["kospi_daily"] += 1
        else:
            dropped += 1
    last_k = max(by_date) if by_date else ""
    for r in flow_rows or []:
        d = str(r.get("date") or "")
        if len(d) != 8 or d in by_date:
            continue
        o, h, l, c = r.get("idx_open"), r.get("idx_high"), r.get("idx_low"), r.get("idx")
        if None in (o, h, l, c):
            if c is not None and d > last_k:
                src["last_close_only"] = d if not src["last_close_only"] or d > src["last_close_only"] else src["last_close_only"]
            continue
        if _valid(o, h, l, c):
            by_date[d] = {"date": d, "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": 0.0}
            src["market_flow"] += 1
        else:
            dropped += 1
    src["dropped"] = dropped
    if not by_date:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"]), src
    df = pd.DataFrame([by_date[k] for k in sorted(by_date)])
    return df, src


def _read_json(rel):
    try:
        with open(os.path.join(LOCAL_DIR, rel), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _fmt(x, nd=1, suffix=""):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "-"
    return f"{x:,.{nd}f}{suffix}"


def _draw(st, spec):
    if not spec:
        return
    try:
        st.vega_lite_chart(spec, use_container_width=True)
    except Exception as e:      # noqa: BLE001
        st.caption(f"차트를 그리지 못했습니다: {str(e)[:100]}")


def render_market_state(st, load_json=None):
    """시장 상태 탭 본문. load_json(rel)은 data/k200 아래 파일을 읽는 함수(원격 우선). 없으면 로컬만 읽는다."""
    import k200_view as V
    import k200_charts as CH

    load = load_json or _read_json
    kospi = (load("kospi_daily.json") or {}).get("rows") or []
    mk_obj = load("market_flow.json") or {}
    mk_rows = mk_obj.get("rows") or []
    status = load("status.json") or {}

    daily, src = build_index_daily(kospi, mk_rows)

    st.subheader("코스피 시장 상태")
    st.caption("코스피 지수 일봉에 KODEX 200 판단 엔진과 같은 지표를 적용한 결과입니다. 매수·매도를 대신 결정하지 않고, "
               "지금 시장이 하락 추세인지 반등 중인지 과열인지를 같은 기준으로 보여 줍니다. 임계값은 널리 쓰는 값으로 고정했고 이 데이터에 맞춰 조정하지 않았습니다.")

    if len(daily) < MIN_BARS:
        st.warning(
            f"코스피 지수 일봉이 {len(daily)}개뿐이라 판독할 수 없습니다(최소 {MIN_BARS}개). "
            "GitHub Actions의 'K200 Collect'를 Run workflow로 실행하세요. 12년치를 받으려면 daily_years에 12를 넣습니다. "
            "수집 결과는 data/k200/status.json의 kospi_daily 항목에서 확인합니다.")
        kd = (status or {}).get("kospi_daily") or {}
        if kd.get("error"):
            st.error(f"지수 일봉 수집 오류 코드: {kd['error']}")
        if src.get("last_close_only"):
            st.caption("수급 파일에는 지수 종가만 있고 시가·고가·저가가 없는 날이 있어 캔들을 만들 수 없습니다. 수집기를 새 버전으로 실행하면 채워집니다.")
        _flow_section(st, V, CH, mk_rows)
        return

    view = V.build_view(daily)
    if not view:
        st.info("지표를 계산할 수 없습니다.")
        return
    m, stt = view["m"], view["state"]
    last_date = str(daily["date"].iloc[-1])

    c1, c2, c3 = st.columns(3)
    c1.metric("지수 일봉", f"{len(daily):,}개", f"{daily['date'].iloc[0]} 부터 {last_date}", delta_color="off")
    c2.metric("출처", "지수 일봉 API" if src["kospi_daily"] else "수급 파일 내 지수",
              f"일봉 {src['kospi_daily']} + 수급파일 {src['market_flow']}", delta_color="off")
    c3.metric("수집 시각", (status.get("run_at") or "-")[:16].replace("T", " "))
    if src["dropped"]:
        st.caption(f"시·고·저·종이 앞뒤가 맞지 않는 {src['dropped']}개 행은 버렸습니다.")
    if len(daily) < 260:
        st.caption("일봉이 260개 미만이라 아래 '과거 유사 상황' 통계는 만들지 않습니다.")

    st.markdown(f"### 지금 상태: {stt['state']}")
    st.markdown(f"**{stt['headline']}**")
    st.markdown(CH.cards_html(CH.traffic_cards(view, mk_rows, provisional=False)), unsafe_allow_html=True)

    a, b, c, d = st.columns(4)
    a.metric(f"지수 ({m['date']})", _fmt(m["close"], 2), _fmt(m["day_ret_pct"], 2, "%"))
    b.metric("RSI(14) / RSI(2)", f"{_fmt(m['rsi14'], 0)} / {_fmt(m['rsi2'], 0)}")
    c.metric("20일 고점 대비", _fmt(m["dd20_pct"], 1, "%"))
    d.metric("52주 범위 내 위치", _fmt(m["pos52_pct"], 0, "%"), f"고점 대비 {_fmt(m['dd52_pct'], 1)}%", delta_color="off")

    sc = stt["score"]
    st.markdown(" · ".join(f"**{g}** {sc[g]['on']}/{sc[g]['total']}" for g in V.GROUPS))
    for r in stt["reasons"]:
        st.markdown(f"- {r}")
    st.info("참고 가이드: " + stt["guide"])
    st.markdown(f"**무효화 기준 지수 약 {_fmt(stt['invalidation_price'], 0)}** ({stt['invalidation_note']}). 이 아래로 마감하면 위 판독의 근거가 깨집니다.")
    if stt["pending_confirmations"]:
        st.markdown("**아직 안 켜진 반등 확인 신호:** " + ", ".join(stt["pending_confirmations"]))

    st.markdown("#### 지수 차트")
    lv = V.levels(daily, price=m["close"], last_is_partial=False)
    _draw(st, CH.price_spec(daily, lv, m["close"]))
    st.caption("빨강은 상승 봉, 파랑은 하락 봉. 주황선 20일선, 보라선 60일선, 회색 점선 200일선. 점선 가로선은 가까운 지지(파랑)·저항(빨강), 검은 선은 마지막 종가입니다. "
               "지수는 거래량 자료가 없어 아래 거래량 칸이 비어 있을 수 있습니다.")

    st.markdown("#### 지표 게이지")
    _draw(st, CH.gauge_spec(CH.gauge_rows(m)))

    with st.expander("신호 점검표 (켜짐/꺼짐 항목 전체)"):
        for g in V.GROUPS:
            st.markdown(f"**{g}** ({sc[g]['on']}/{sc[g]['total']} 충족)")
            rows = [{"상태": "켜짐" if x["on"] else ("꺼짐" if x["on"] is False else "계산불가"), "항목": x["name"],
                     "현재 값": x["value"], "의미": x["note"]} for x in view["cards"] if x["group"] == g]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    st.markdown("#### 과거에 비슷했던 때 이후 코스피 성과")
    an = V.analog_stats(daily)
    if not an["rows"]:
        st.info("일봉이 260개 미만이라 계산하지 않습니다. K200 Collect를 daily_years 12로 실행하면 채워집니다.")
    else:
        _draw(st, CH.analog_spec(an, 10))
        st.caption(f"{an['first']} 부터 {an['last']} 까지 {an['n_bars']}거래일. 같은 사건이 이어지면 첫날만 세고, 오늘은 제외했습니다. "
                   "사건 수가 8건 미만이면 신뢰하기 어렵습니다. 비교 기준은 아무 날이나 샀을 때입니다.")
        tab_rows = []
        for r in an["rows"]:
            row = {"상황": r["name"], "오늘 해당": "예" if r["active_today"] else "-", "사건 수": r["episodes"]}
            for h in V.HORIZONS:
                s_ = r["stats"][h]
                row[f"{h}일 후 승률/평균/최악"] = (f"{s_['win_pct']:.0f}% / {s_['mean']:+.1f}% / {s_['worst']:+.1f}%  (n={s_['n']})" if s_.get("n") else "-")
            tab_rows.append(row)
        row = {"상황": "(기준) 아무 날이나 매수", "오늘 해당": "-", "사건 수": an["baseline"][5].get("n", 0)}
        for h in V.HORIZONS:
            s_ = an["baseline"][h]
            row[f"{h}일 후 승률/평균/최악"] = f"{s_['win_pct']:.0f}% / {s_['mean']:+.1f}% / {s_['worst']:+.1f}%  (n={s_['n']})"
        tab_rows.append(row)
        st.dataframe(pd.DataFrame(tab_rows), hide_index=True, use_container_width=True)

    st.markdown("#### 지수 가격 지도 (지지·저항)")
    if lv:
        near = sorted(lv, key=lambda x: abs(x["dist_pct"]))[:10]
        near = sorted(near, key=lambda x: x["price"], reverse=True)
        st.dataframe(pd.DataFrame([{"구분": x["side"], "기준": x["name"], "지수": f"{x['price']:,.0f}", "현재 대비": f"{x['dist_pct']:+.1f}%"} for x in near]),
                     hide_index=True, use_container_width=True)

    _flow_section(st, V, CH, mk_rows)


def _flow_section(st, V, CH, mk_rows):
    st.markdown("#### 코스피 투자자별 수급 (확정치)")
    if not mk_rows:
        st.info("코스피 전체 수급이 아직 없습니다. K200 Collect를 실행하면 market_flow.json이 생깁니다.")
        return
    mdf = pd.DataFrame(mk_rows).sort_values("date", ascending=False).reset_index(drop=True)
    eok = lambda x: None if x is None or pd.isna(x) else x / 100.0       # 백만원 -> 억원
    last = mdf.iloc[0]
    st.markdown(f"**기준일 {last['date']}, 지수 {_fmt(last.get('idx'), 2)} / {_fmt(last.get('idx_chg_pct'), 2, '%')}**")
    names = [("frgn", "외국인"), ("orgn", "기관 합계"), ("prsn", "개인"), ("etc", "기타법인"),
             ("scrt", "  금융투자"), ("ivtr", "  투신"), ("pe", "  사모펀드"), ("insu", "  보험"), ("bank", "  은행"), ("fund", "  연기금 등")]
    trs = []
    for k, nm in names:
        col = f"{k}_amt"
        if col not in mdf.columns:
            continue
        ser = mdf[col]
        tot = lambda n: ser.head(n).sum(min_count=1)
        streak = V._streak(list(ser.head(30)))
        trs.append({"구분": nm, "당일(억원)": _fmt(eok(ser.iloc[0]), 0), "3일 합": _fmt(eok(tot(3)), 0), "5일 합": _fmt(eok(tot(5)), 0),
                    "20일 합": _fmt(eok(tot(20)), 0),
                    "연속": (f"{streak}일 순매수" if streak > 0 else f"{-streak}일 순매도" if streak < 0 else "-")})
    _draw(st, CH.flow_spec(mk_rows, 20))
    st.caption("막대는 일별 순매수 금액(억원), 빨강은 순매수, 파랑은 순매도. 맨 위 선은 코스피 지수입니다. 장중 값은 포함하지 않습니다.")
    st.dataframe(pd.DataFrame(trs), hide_index=True, use_container_width=True)
    hist = mdf.head(10)[["date", "idx_chg_pct", "frgn_amt", "orgn_amt", "prsn_amt"]].copy()
    for c_ in ("frgn_amt", "orgn_amt", "prsn_amt"):
        hist[c_] = (hist[c_] / 100.0).round(0)
    hist.columns = ["날짜", "지수 등락 %", "외국인 억", "기관 억", "개인 억"]
    st.caption("최근 10거래일 흐름 (지수 등락과 함께)")
    st.dataframe(hist, hide_index=True, use_container_width=True)
    st.caption("금액은 억원. 수량(주)과 금액의 부호가 다를 수 있어 판단에는 금액을 기준으로 봅니다. 기관 합계는 세부 항목의 합입니다.")
