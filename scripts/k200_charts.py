# -*- coding: utf-8 -*-
"""
scripts/k200_charts.py — 판단 보조 화면용 차트·카드 (Vega-Lite 명세를 dict로 만든다. 네트워크·Streamlit 의존 없음)

페이지에서는 st.vega_lite_chart(spec, use_container_width=True) 로 그린다.
한국 증시 관례대로 상승·순매수는 빨강, 하락·순매도는 파랑이다.
"""

from __future__ import annotations

import math

import pandas as pd

import k200_view as V

SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
UP, DOWN = "#d6453d", "#2f6fdb"
GRAY = "#8a93a0"


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if hasattr(v, "item"):
        v = v.item()
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
    return v


def _iso(d: str) -> str:
    d = str(d)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


# ---------------- 가격 차트 ----------------

def price_spec(daily: pd.DataFrame, lv: list[dict], price: float, n: int = 130, level_range_pct: float = 12.0) -> dict:
    ind = V.add_indicators(daily).tail(n)
    vals = []
    for _, r in ind.iterrows():
        vals.append({"date": _iso(r["date"]), "open": _clean(float(r["open"])), "high": _clean(float(r["high"])),
                     "low": _clean(float(r["low"])), "close": _clean(float(r["close"])), "volume": _clean(float(r["volume"])),
                     "sma20": _clean(float(r["sma20"])) if pd.notna(r["sma20"]) else None,
                     "sma60": _clean(float(r["sma60"])) if pd.notna(r["sma60"]) else None,
                     "sma200": _clean(float(r["sma200"])) if pd.notna(r["sma200"]) else None})
    lo = min(v["low"] for v in vals)
    hi = max(v["high"] for v in vals)
    # 현재가에서 가까운 지지·저항만 그린다 (이동평균선과 겹치는 항목은 이름만 다르므로 가격 기준으로 가까운 것 우선)
    near = [x for x in (lv or []) if abs(x["dist_pct"]) <= level_range_pct and not x["name"].endswith("일선")]
    near = sorted(near, key=lambda x: abs(x["dist_pct"]))[:6]
    lo2 = min([lo] + [x["price"] for x in near]) * 0.985
    hi2 = max([hi] + [x["price"] for x in near]) * 1.015
    color = {"condition": {"test": "datum.close >= datum.open", "value": UP}, "value": DOWN}
    x = {"field": "date", "type": "temporal", "axis": {"title": None, "format": "%m/%d", "labelOverlap": True}}
    ysc = {"scale": {"domain": [round(lo2, 0), round(hi2, 0)], "zero": False}, "type": "quantitative", "axis": {"title": None, "format": ",.0f"}}
    layers = [
        {"mark": {"type": "rule"}, "encoding": {"x": x, "y": {"field": "low", **ysc}, "y2": {"field": "high"}, "color": color,
                                                "tooltip": [{"field": "date", "type": "temporal", "title": "날짜"}, {"field": "open", "title": "시가", "format": ",.0f"},
                                                            {"field": "high", "title": "고가", "format": ",.0f"}, {"field": "low", "title": "저가", "format": ",.0f"},
                                                            {"field": "close", "title": "종가", "format": ",.0f"}]}},
        {"mark": {"type": "bar", "size": 4}, "encoding": {"x": x, "y": {"field": "open", **ysc}, "y2": {"field": "close"}, "color": color}},
        {"mark": {"type": "line", "color": "#f0a020", "strokeWidth": 1.6}, "encoding": {"x": x, "y": {"field": "sma20", **ysc}}},
        {"mark": {"type": "line", "color": "#8e5bd6", "strokeWidth": 1.6}, "encoding": {"x": x, "y": {"field": "sma60", **ysc}}},
        {"mark": {"type": "line", "color": "#555e6b", "strokeWidth": 1.4, "strokeDash": [4, 3]}, "encoding": {"x": x, "y": {"field": "sma200", **ysc}}},
    ]
    if near:
        ld = [{"price": round(float(a["price"]), 0), "name": f"{a['name']} {a['price']:,.0f}", "side": a["side"]} for a in near]
        lc = {"field": "side", "type": "nominal", "scale": {"domain": ["저항", "지지"], "range": [UP, DOWN]}, "legend": None}
        layers.append({"data": {"values": ld}, "mark": {"type": "rule", "strokeDash": [6, 4], "opacity": 0.7},
                       "encoding": {"y": {"field": "price", "type": "quantitative", "scale": {"domain": [round(lo2, 0), round(hi2, 0)], "zero": False}}, "color": lc}})
        layers.append({"data": {"values": ld}, "mark": {"type": "text", "align": "left", "dx": 4, "dy": -5, "fontSize": 11},
                       "encoding": {"y": {"field": "price", "type": "quantitative", "scale": {"domain": [round(lo2, 0), round(hi2, 0)], "zero": False}},
                                    "x": {"value": 0}, "text": {"field": "name"}, "color": lc}})
    layers.append({"data": {"values": [{"price": round(float(price), 0)}]}, "mark": {"type": "rule", "color": "#111", "strokeWidth": 1.2},
                   "encoding": {"y": {"field": "price", "type": "quantitative", "scale": {"domain": [round(lo2, 0), round(hi2, 0)], "zero": False}}}})
    volume = {"height": 70, "mark": {"type": "bar", "opacity": 0.55},
              "encoding": {"x": {"field": "date", "type": "temporal", "axis": {"title": None, "format": "%m/%d"}},
                           "y": {"field": "volume", "type": "quantitative", "axis": {"title": "거래량", "format": "~s"}},
                           "color": color}}
    return {"$schema": SCHEMA, "data": {"values": vals}, "vconcat": [{"height": 340, "layer": layers}, volume],
            "config": {"view": {"stroke": None}}, "width": "container"}


# ---------------- 게이지 ----------------

def gauge_rows(m: dict) -> list[dict]:
    def row(name, val, lo, hi, order):
        return {"name": name, "value": None if val is None else float(max(0, min(100, val))), "raw": val, "lo": lo, "hi": hi, "order": order}
    return [
        row("RSI(14)", m.get("rsi14"), 30, 70, 0),
        row("RSI(2)", m.get("rsi2"), 10, 90, 1),
        row("스토캐스틱 K", m.get("stoch_k"), 20, 80, 2),
        row("볼린저 %B", None if m.get("bb_pctb") is None else m["bb_pctb"] * 100, 5, 95, 3),
        row("52주 범위 내 위치", m.get("pos52_pct"), 0, 100, 4),
    ]


def gauge_spec(rows: list[dict]) -> dict:
    vals = []
    for r in rows:
        if r["value"] is None:
            continue
        vals.append({"name": r["name"], "value": r["value"], "label": f"{r['raw']:.0f}", "lo": r["lo"], "hi": r["hi"], "order": r["order"]})
    y = {"field": "name", "type": "nominal", "sort": {"field": "order"}, "axis": {"title": None, "labelFontSize": 12}}
    xs = {"scale": {"domain": [0, 100]}, "axis": {"title": None, "values": [0, 20, 30, 50, 70, 80, 100]}}
    layers = [
        {"mark": {"type": "bar", "color": "#e9edf2", "height": {"band": 0.55}}, "encoding": {"y": y, "x": {"datum": 0, "type": "quantitative", **xs}, "x2": {"datum": 100}}},
        {"mark": {"type": "bar", "color": "#bcd5ff", "height": {"band": 0.55}}, "encoding": {"y": y, "x": {"datum": 0, "type": "quantitative", **xs}, "x2": {"field": "lo"}}},
        {"mark": {"type": "bar", "color": "#ffc9c2", "height": {"band": 0.55}}, "encoding": {"y": y, "x": {"field": "hi", "type": "quantitative", **xs}, "x2": {"datum": 100}}},
        {"mark": {"type": "tick", "color": "#111", "thickness": 3, "height": {"band": 0.8}}, "encoding": {"y": y, "x": {"field": "value", "type": "quantitative", **xs}}},
        {"mark": {"type": "text", "align": "left", "dx": 7, "fontSize": 12, "fontWeight": "bold"},
         "encoding": {"y": y, "x": {"field": "value", "type": "quantitative", **xs}, "text": {"field": "label"}}},
    ]
    return {"$schema": SCHEMA, "data": {"values": vals}, "layer": layers, "width": "container",
            "height": max(40, 34 * len(vals)), "config": {"view": {"stroke": None}}}


# ---------------- 수급 ----------------

def flow_spec(mk_rows: list[dict], n: int = 20) -> dict | None:
    rows = sorted(mk_rows or [], key=lambda r: r["date"])[-n:]
    if not rows:
        return None
    vals = []
    for r in rows:
        vals.append({"date": _iso(r["date"]), "idx": _clean(r.get("idx")), "idx_chg": _clean(r.get("idx_chg_pct")),
                     "frgn": None if r.get("frgn_amt") is None else round(r["frgn_amt"] / 100.0, 0),
                     "orgn": None if r.get("orgn_amt") is None else round(r["orgn_amt"] / 100.0, 0),
                     "prsn": None if r.get("prsn_amt") is None else round(r["prsn_amt"] / 100.0, 0)})
    xo = {"field": "date", "type": "ordinal", "axis": {"title": None, "labelExpr": "slice(datum.value, 5)", "labelAngle": -45}}

    def bar(field, title):
        return {"height": 90, "title": {"text": title, "anchor": "start", "fontSize": 12},
                "mark": {"type": "bar"},
                "encoding": {"x": {**xo, "axis": {**xo["axis"], "labels": False, "ticks": False}} if field != "prsn" else xo,
                             "y": {"field": field, "type": "quantitative", "axis": {"title": None, "format": ",.0f"}},
                             "color": {"condition": {"test": f"datum.{field} >= 0", "value": UP}, "value": DOWN},
                             "tooltip": [{"field": "date", "type": "ordinal", "title": "날짜"}, {"field": field, "title": "억원", "format": ",.0f"}]}}

    idx = {"height": 110, "title": {"text": "코스피 지수", "anchor": "start", "fontSize": 12}, "mark": {"type": "line", "point": True, "color": "#333"},
           "encoding": {"x": {**xo, "axis": {**xo["axis"], "labels": False, "ticks": False}},
                        "y": {"field": "idx", "type": "quantitative", "scale": {"zero": False}, "axis": {"title": None, "format": ",.0f"}},
                        "tooltip": [{"field": "date", "type": "ordinal", "title": "날짜"}, {"field": "idx", "title": "지수", "format": ",.2f"},
                                    {"field": "idx_chg", "title": "등락 %", "format": "+.2f"}]}}
    return {"$schema": SCHEMA, "data": {"values": vals}, "vconcat": [idx, bar("frgn", "외국인 순매수 (억원)"), bar("orgn", "기관 순매수 (억원)"), bar("prsn", "개인 순매수 (억원)")],
            "width": "container", "config": {"view": {"stroke": None}}}


# ---------------- 유사 상황 ----------------

def analog_spec(an: dict, horizon: int = 10) -> dict | None:
    if not an or not an.get("rows"):
        return None
    vals = []
    for r in an["rows"]:
        s = r["stats"].get(horizon) or {}
        if not s.get("n"):
            continue
        vals.append({"name": r["name"].split(" (")[0], "win": round(s["win_pct"], 0), "n": s["n"], "mean": round(s["mean"], 1),
                     "worst": round(s["worst"], 1), "active": "오늘 해당" if r["active_today"] else "해당 아님",
                     "label": f"{s['win_pct']:.0f}%  평균 {s['mean']:+.1f}%  (사건 {s['n']}건)",
                     "small": s["n"] < 8})
    if not vals:
        return None
    base = (an.get("baseline") or {}).get(horizon) or {}
    y = {"field": "name", "type": "nominal", "sort": None, "axis": {"title": None, "labelLimit": 260}}
    layers = [
        {"mark": {"type": "bar", "height": {"band": 0.6}},
         "encoding": {"y": y, "x": {"field": "win", "type": "quantitative", "scale": {"domain": [0, 100]}, "axis": {"title": f"{horizon}일 후 상승 비율 (%)"}},
                      "color": {"field": "small", "type": "nominal", "scale": {"domain": [False, True], "range": ["#2a9d8f", "#c3c9d0"]}, "legend": None},
                      "tooltip": [{"field": "name", "title": "상황"}, {"field": "win", "title": "상승 비율 %"}, {"field": "mean", "title": "평균 %"},
                                  {"field": "worst", "title": "최악 %"}, {"field": "n", "title": "사건 수"}, {"field": "active", "title": "오늘"}]}},
        {"mark": {"type": "text", "align": "left", "dx": 5, "fontSize": 11},
         "encoding": {"y": y, "x": {"field": "win", "type": "quantitative"}, "text": {"field": "label"}}},
    ]
    if base.get("n"):
        layers.append({"data": {"values": [{"b": round(base["win_pct"], 0)}]}, "mark": {"type": "rule", "color": "#111", "strokeDash": [5, 4], "strokeWidth": 1.5},
                       "encoding": {"x": {"field": "b", "type": "quantitative"}}})
    return {"$schema": SCHEMA, "data": {"values": vals}, "layer": layers, "width": "container", "height": 44 * len(vals) + 30,
            "config": {"view": {"stroke": None}}}


# ---------------- 신호등 카드 ----------------

def traffic_cards(view: dict, mk_rows: list[dict] | None, provisional: bool = False) -> list[dict]:
    m, stt = view["m"], view["state"]
    sc = stt["score"]
    t_on, t_all = sc[V.GROUPS[0]]["on"], sc[V.GROUPS[0]]["total"]
    os_on, os_all = sc[V.GROUPS[1]]["on"], sc[V.GROUPS[1]]["total"]
    rc_on, rc_all = sc[V.GROUPS[2]]["on"], sc[V.GROUPS[2]]["total"]
    oh_on = sc[V.GROUPS[3]]["on"]
    cards = []
    up_state = "상승" in stt["state"]
    lvl = "good" if up_state else ("bad" if t_on >= max(3, t_all - 1) else ("mid" if t_on >= 1 else "good"))
    cards.append({"title": "추세", "level": lvl, "headline": ("상승 추세" if up_state else ("하락 추세" if lvl == "bad" else ("혼조" if lvl == "mid" else "하락 조건 없음"))),
                  "sub": f"하락 조건 {t_on}/{t_all} · 20일선 대비 {m['gap20_pct']:+.1f}%" if m.get("gap20_pct") is not None else f"하락 조건 {t_on}/{t_all}"})
    if oh_on >= 2:
        cards.append({"title": "저점·고점", "level": "bad", "headline": f"과열 신호 {oh_on}개", "sub": "추격 매수에 불리한 구간"})
    elif os_on >= 3:
        cards.append({"title": "저점·고점", "level": "good", "headline": f"저점 신호 {os_on}/{os_all}", "sub": f"RSI14 {m['rsi14']:.0f} · 스토캐스틱 {m['stoch_k']:.0f}"})
    elif os_on >= 1:
        cards.append({"title": "저점·고점", "level": "mid", "headline": f"저점 신호 {os_on}/{os_all}", "sub": f"RSI14 {m['rsi14']:.0f} · 스토캐스틱 {m['stoch_k']:.0f}"})
    else:
        cards.append({"title": "저점·고점", "level": "na", "headline": "중간 구간", "sub": f"RSI14 {m['rsi14']:.0f}" if m.get("rsi14") is not None else ""})
    cards.append({"title": "반등 확인", "level": "good" if rc_on >= 5 else ("mid" if rc_on >= 3 else "bad"),
                  "headline": f"{rc_on}/{rc_all} 켜짐" + (" (확인)" if rc_on >= 5 else (" (진행 중)" if rc_on >= 3 else " (부족)")),
                  "sub": f"5일 저점에서 {m['bounce_from_low5_pct']:+.1f}%"})
    rows = sorted(mk_rows or [], key=lambda r: r["date"], reverse=True)
    if rows and rows[0].get("frgn_amt") is not None:
        last = rows[0]["frgn_amt"] / 100.0
        s5 = sum((r.get("frgn_amt") or 0) for r in rows[:5]) / 100.0
        lv = "good" if (last > 0 and s5 > 0) else ("bad" if (last < 0 and s5 < 0) else "mid")
        cards.append({"title": "수급 (코스피 외국인)", "level": lv, "headline": f"당일 {last:+,.0f}억 · 5일 {s5:+,.0f}억",
                      "sub": f"기준 {rows[0]['date'][4:6]}/{rows[0]['date'][6:]} 확정치" + (" · 장중은 별도" if provisional else "")})
    else:
        cards.append({"title": "수급 (코스피 외국인)", "level": "na", "headline": "데이터 없음", "sub": "수집기 실행 후 표시"})
    return cards


LEVEL_STYLE = {"good": ("#2a9d5c", "유리"), "mid": ("#d9a21b", "중립"), "bad": ("#d6453d", "불리"), "na": ("#8a93a0", "해당 없음")}


def cards_html(cards: list[dict]) -> str:
    items = []
    for c in cards:
        col, tag = LEVEL_STYLE[c["level"]]
        items.append(
            f'<div style="flex:1;min-width:180px;border-left:6px solid {col};background:{col}22;border-radius:6px;padding:10px 14px;">'
            f'<div style="font-size:12px;opacity:.75">{c["title"]}</div>'
            f'<div style="font-size:18px;font-weight:700;margin:2px 0">{c["headline"]}</div>'
            f'<div style="font-size:12px;opacity:.8">{c["sub"]}</div>'
            f'<div style="font-size:11px;margin-top:4px;color:{col};font-weight:700">매수 관점 {tag}</div></div>')
    return '<div style="display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 14px">' + "".join(items) + "</div>"
