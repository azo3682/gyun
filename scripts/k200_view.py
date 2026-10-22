# -*- coding: utf-8 -*-
"""
scripts/k200_view.py — KODEX 200 판단 보조 엔진 (네트워크 없음, 순수 계산)

"지금 살지 말지"를 대신 결정하지 않는다. 지표를 같은 기준으로 계산해서
  1) 상태 판독(하락 추세/과매도/기술적 반등/눌림/과열 중 어디인지)과 그 근거,
  2) 저점·반등·과열 신호 점검표,
  3) 과거에 비슷한 상태였던 때의 이후 성과(유사 상황 통계),
  4) 지지·저항 가격, 5) 수급 흐름 요약
을 보여 준다. 임계값은 널리 쓰이는 값(RSI 30/70, 볼린저 2σ 등)으로 고정했고 데이터를 보고 맞추지 않았다.
지표가 미래를 예측한다는 보증은 없다. 유사 상황 통계는 '과거에 이랬다'는 기록이다.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from k200_core import norm_daily, rsi, sma

# ============================================================
# 지표
# ============================================================


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def stochastic(df: pd.DataFrame, n: int = 14, k: int = 3, d: int = 3):
    ll = df["low"].rolling(n, min_periods=n).min()
    hh = df["high"].rolling(n, min_periods=n).max()
    raw = (df["close"] - ll) / (hh - ll).replace(0, np.nan) * 100
    kk = raw.rolling(k, min_periods=k).mean()
    return kk, kk.rolling(d, min_periods=d).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    line = ema(close, fast) - ema(close, slow)
    signal = line.ewm(span=sig, adjust=False, min_periods=sig).mean()
    return line, signal, line - signal


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = norm_daily(df)
    c = d["close"]
    out = d.copy()
    for n in (5, 20, 60, 120, 200):
        out[f"sma{n}"] = sma(c, n)
    out["rsi14"], out["rsi2"] = rsi(c, 14), rsi(c, 2)
    out["stoch_k"], out["stoch_d"] = stochastic(d)
    out["macd"], out["macd_sig"], out["macd_hist"] = macd(c)
    mid, sd = sma(c, 20), c.rolling(20, min_periods=20).std()
    out["bb_pctb"] = (c - (mid - 2 * sd)) / (4 * sd).replace(0, np.nan)
    out["bb_width"] = 4 * sd / mid * 100
    out["atr14"] = atr(d)
    out["gap20"] = (c / out["sma20"] - 1) * 100
    out["hi20"] = c.rolling(20, min_periods=20).max()
    out["dd20"] = (c / out["hi20"] - 1) * 100
    out["vol_avg20"] = d["volume"].shift(1).rolling(20, min_periods=10).mean()
    return out


def _num(x):
    try:
        v = float(x)
    except Exception:
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


def _pct_rank(series: pd.Series, value) -> float | None:
    s = series.dropna()
    if value is None or len(s) < 60:
        return None
    return float((s < value).mean() * 100)


def _bullish_divergence(ind: pd.DataFrame, lookback: int = 40) -> bool:
    c, r = ind["close"].to_numpy(float), ind["rsi14"].to_numpy(float)
    n = len(c)
    if n < lookback + 5:
        return False
    piv = []
    for i in range(n - lookback, n - 2):
        if i < 2 or i + 2 >= n:
            continue
        if c[i] < min(c[i - 2], c[i - 1], c[i + 1], c[i + 2]) and not math.isnan(r[i]):
            piv.append(i)
    if len(piv) < 2:
        return False
    i2, i1 = piv[-1], piv[-2]
    if i2 - i1 < 5:
        return False
    return bool(c[i2] < c[i1] and r[i2] > r[i1])


def snapshot(df: pd.DataFrame) -> dict:
    """마지막 봉 기준 지표값 모음. 데이터가 30개 미만이면 빈 dict."""
    ind = add_indicators(df)
    n = len(ind)
    if n < 30:
        return {}
    last, prev = ind.iloc[-1], ind.iloc[-2]
    c = ind["close"]
    look = ind.tail(min(252, n))
    hi52, lo52 = float(look["high"].max()), float(look["low"].min())
    close = float(last["close"])
    rng = float(last["high"] - last["low"])
    body = abs(float(last["close"] - last["open"]))
    lower = float(min(last["open"], last["close"]) - last["low"])
    upper = float(last["high"] - max(last["open"], last["close"]))
    atr_v = _num(last["atr14"])
    pos_in_range = (close - float(last["low"])) / rng if rng > 0 else 0.5
    hammer = rng > 0 and body > 0 and lower >= 2 * body and upper <= body and pos_in_range >= 0.6
    long_bull = close > float(last["open"]) and rng > 0 and body >= 0.6 * rng and (atr_v is not None and rng >= 1.2 * atr_v)
    long_bear = close < float(last["open"]) and rng > 0 and body >= 0.6 * rng and (atr_v is not None and rng >= 1.2 * atr_v)
    down_run = 0
    for d in c.diff().iloc[::-1]:
        if d < 0:
            down_run += 1
        else:
            break
    low5 = float(ind["low"].tail(5).min())
    prior_low = float(ind["low"].iloc[-20:-5].min()) if n >= 21 else None
    low60 = float(ind["low"].tail(60).min())
    high60 = float(ind["high"].tail(60).max())
    vol_ratio = None
    if _num(last["vol_avg20"]) and last["volume"] > 0:
        vol_ratio = float(last["volume"] / last["vol_avg20"])
    # 최근 5거래일 안에 거래량 2배 이상 + 하락 -2% 이하였던 날이 있는가 (투매 흔적)
    chg = c.pct_change() * 100
    climax = False
    for i in range(max(1, n - 5), n):
        va = _num(ind["vol_avg20"].iloc[i])
        if va and ind["volume"].iloc[i] >= 2 * va and chg.iloc[i] <= -2:
            climax = True
    h = ind["macd_hist"]
    hist_rising2 = bool(len(h.dropna()) >= 3 and h.iloc[-1] > h.iloc[-2] > h.iloc[-3])
    k, d_ = ind["stoch_k"], ind["stoch_d"]
    cross_up = False
    for i in range(max(1, n - 3), n):
        if not (math.isnan(k.iloc[i]) or math.isnan(d_.iloc[i]) or math.isnan(k.iloc[i - 1]) or math.isnan(d_.iloc[i - 1])):
            if k.iloc[i - 1] <= d_.iloc[i - 1] and k.iloc[i] > d_.iloc[i] and k.iloc[i] < 50:
                cross_up = True
    s5 = ind["sma5"]
    m = {
        "date": str(last["date"]), "close": close, "open": float(last["open"]), "high": float(last["high"]), "low": float(last["low"]),
        "prev_close": float(prev["close"]), "prev_high": float(prev["high"]), "prev_low": float(prev["low"]),
        "day_ret_pct": (close / float(prev["close"]) - 1) * 100,
        "gap_pct": (float(last["open"]) / float(prev["close"]) - 1) * 100,
        "sma5": _num(last["sma5"]), "sma20": _num(last["sma20"]), "sma60": _num(last["sma60"]),
        "sma120": _num(last["sma120"]), "sma200": _num(last["sma200"]),
        "sma5_rising": bool(len(s5.dropna()) >= 2 and s5.iloc[-1] > s5.iloc[-2]),
        "sma20_slope5_pct": _num((ind["sma20"].iloc[-1] / ind["sma20"].iloc[-6] - 1) * 100) if n >= 26 and _num(ind["sma20"].iloc[-6]) else None,
        "rsi14": _num(last["rsi14"]), "rsi2": _num(last["rsi2"]),
        "stoch_k": _num(last["stoch_k"]), "stoch_d": _num(last["stoch_d"]), "stoch_cross_up": cross_up,
        "macd": _num(last["macd"]), "macd_sig": _num(last["macd_sig"]), "macd_hist": _num(last["macd_hist"]),
        "macd_hist_rising2": hist_rising2,
        "bb_pctb": _num(last["bb_pctb"]), "bb_width": _num(last["bb_width"]),
        "atr14": atr_v, "atr14_pct": (atr_v / close * 100) if atr_v else None,
        "vol20_ann_pct": _num(c.pct_change().tail(20).std() * math.sqrt(252) * 100),
        "gap20_pct": _num(last["gap20"]), "gap20_rank": _pct_rank(ind["gap20"], _num(last["gap20"])),
        "dd20_pct": _num(last["dd20"]),
        "dd52_pct": (close / hi52 - 1) * 100, "hi52": hi52, "lo52": lo52,
        "pos52_pct": (close - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else None,
        "fib_retrace_pct": (close - low60) / (high60 - low60) * 100 if high60 > low60 else None,
        "low60": low60, "high60": high60,
        "down_run": down_run, "low5": low5,
        "bounce_from_low5_pct": (close / low5 - 1) * 100,
        "higher_low": bool(prior_low is not None and low5 > prior_low),
        "vol_ratio": vol_ratio, "climax_recent": climax,
        "hammer": bool(hammer), "long_bull": bool(long_bull), "long_bear": bool(long_bear),
        "bull_div": _bullish_divergence(ind),
        "n": n,
    }
    return m


# ============================================================
# 신호 점검표
# ============================================================

GROUPS = ("하락 추세 조건", "저점 신호 (과매도)", "반등 확인 신호", "과열 신호")


def _fv(x, nd=1, suffix=""):
    return "-" if x is None else f"{x:,.{nd}f}{suffix}"


def signal_cards(m: dict) -> list[dict]:
    """각 항목: group, name, value, on(조건 충족 여부, None=계산 불가), note."""
    if not m:
        return []
    c = m["close"]
    cards = []

    def add(group, name, value, on, note):
        cards.append({"group": group, "name": name, "value": value, "on": on, "note": note})

    def lt(a, b):
        return None if a is None or b is None else a < b

    g = GROUPS[0]
    add(g, "종가가 20일선 아래", f"{_fv(m['gap20_pct'], 1, '%')}", lt(c, m["sma20"]), "단기 추세가 아래쪽")
    add(g, "20일선이 60일선 아래 (역배열)", f"{_fv(m['sma20'], 0)} / {_fv(m['sma60'], 0)}", lt(m["sma20"], m["sma60"]), "중기 추세가 아래쪽")
    add(g, "종가가 200일선 아래", f"{_fv((c / m['sma200'] - 1) * 100 if m['sma200'] else None, 1, '%')}", lt(c, m["sma200"]), "장기 추세가 아래쪽")
    add(g, "20일선 기울기 하락 (5일)", _fv(m["sma20_slope5_pct"], 2, "%"), None if m["sma20_slope5_pct"] is None else m["sma20_slope5_pct"] < 0, "이동평균이 아직 내려가는 중")

    g = GROUPS[1]
    add(g, "RSI(14) 30 이하", _fv(m["rsi14"], 0), None if m["rsi14"] is None else m["rsi14"] <= 30, "14일 기준 과매도")
    add(g, "RSI(2) 10 이하", _fv(m["rsi2"], 0), None if m["rsi2"] is None else m["rsi2"] <= 10, "단기(2일) 과매도")
    add(g, "스토캐스틱 K 20 이하", _fv(m["stoch_k"], 0), None if m["stoch_k"] is None else m["stoch_k"] <= 20, "최근 14일 가격대 하단")
    add(g, "볼린저 하단 이탈 (%B 5% 이하)", _fv(None if m["bb_pctb"] is None else m["bb_pctb"] * 100, 0, "%"),
        None if m["bb_pctb"] is None else m["bb_pctb"] <= 0.05, "20일 밴드 하단 근처")
    add(g, "20일 고점 대비 -15% 이하", _fv(m["dd20_pct"], 1, "%"), None if m["dd20_pct"] is None else m["dd20_pct"] <= -15, "단기간 큰 낙폭")
    add(g, "20일선 이격이 3년 중 하위 5%", _fv(m["gap20_rank"], 0, "분위"), None if m["gap20_rank"] is None else m["gap20_rank"] <= 5, "평균에서 비정상적으로 멀어짐")
    add(g, "투매 흔적 (최근 5일 내 거래량 2배+ 급락)", "있음" if m["climax_recent"] else "없음", bool(m["climax_recent"]), "항복 매도 뒤 바닥이 나오는 경우가 많음")

    g = GROUPS[2]
    add(g, "오늘 종가가 전일보다 상승", _fv(m["day_ret_pct"], 2, "%"), m["day_ret_pct"] > 0, "반등 1일차")
    add(g, "종가가 전일 고가 위", f"{_fv(c, 0)} / {_fv(m['prev_high'], 0)}", c > m["prev_high"], "하락 흐름을 끊는 봉")
    add(g, "MACD 히스토그램 2일 연속 증가", _fv(m["macd_hist"], 0), m["macd_hist_rising2"], "하락 모멘텀 둔화")
    add(g, "스토캐스틱 K가 D를 상향 돌파 (50 미만)", f"{_fv(m['stoch_k'], 0)} / {_fv(m['stoch_d'], 0)}", m["stoch_cross_up"], "과매도권 골든크로스")
    add(g, "최근 5일 저점이 직전 구간 저점보다 높음", _fv(m["low5"], 0), m["higher_low"], "저점을 높이는 중")
    add(g, "종가가 5일선 위 + 5일선 상승", _fv(m["sma5"], 0), (m["sma5"] is not None and c > m["sma5"] and m["sma5_rising"]), "초단기 회복")
    add(g, "RSI 상승 다이버전스", "있음" if m["bull_div"] else "없음", bool(m["bull_div"]), "가격은 저점을 낮췄는데 RSI는 높아짐")
    add(g, "망치형 또는 장대양봉", "망치형" if m["hammer"] else ("장대양봉" if m["long_bull"] else "없음"), bool(m["hammer"] or m["long_bull"]), "저가 매수 유입 흔적")
    add(g, "종가가 20일선 회복", _fv(m["gap20_pct"], 1, "%"), None if m["sma20"] is None else c > m["sma20"], "추세 전환의 첫 관문")

    g = GROUPS[3]
    add(g, "RSI(14) 70 이상", _fv(m["rsi14"], 0), None if m["rsi14"] is None else m["rsi14"] >= 70, "과열")
    add(g, "볼린저 상단 돌파 (%B 100% 이상)", _fv(None if m["bb_pctb"] is None else m["bb_pctb"] * 100, 0, "%"), None if m["bb_pctb"] is None else m["bb_pctb"] >= 1.0, "20일 밴드 상단 이탈")
    add(g, "20일선 이격이 3년 중 상위 5%", _fv(m["gap20_rank"], 0, "분위"), None if m["gap20_rank"] is None else m["gap20_rank"] >= 95, "평균에서 위로 비정상적으로 멀어짐")
    add(g, "52주 고점 대비 -3% 이내", _fv(m["dd52_pct"], 1, "%"), m["dd52_pct"] >= -3, "고점 부근")
    return cards


def score_board(cards: list[dict]) -> dict:
    out = {}
    for g in GROUPS:
        items = [x for x in cards if x["group"] == g]
        valid = [x for x in items if x["on"] is not None]
        out[g] = {"on": sum(1 for x in valid if x["on"]), "total": len(valid)}
    return out


# ============================================================
# 상태 판독
# ============================================================


def classify(m: dict, cards: list[dict] | None = None) -> dict:
    """상태 라벨과 근거, 참고 가이드, 확인 조건, 무효화 가격."""
    if not m:
        return {}
    cards = cards or signal_cards(m)
    sb = score_board(cards)
    os_n, rc_n, oh_n = sb[GROUPS[1]]["on"], sb[GROUPS[2]]["on"], sb[GROUPS[3]]["on"]
    c, s20, s60 = m["close"], m["sma20"], m["sma60"]
    downtrend = bool(s20 and s60 and c < s20 and c < s60 and s20 < s60)
    uptrend = bool(s20 and s60 and c > s20 and s20 > s60)
    atr_v = m["atr14"] or 0.0
    stop = m["low5"] - 0.5 * atr_v

    pending = [x["name"] for x in cards if x["group"] == GROUPS[2] and x["on"] is False][:4]
    reasons = []
    if m["dd20_pct"] is not None:
        reasons.append(f"20일 고점 대비 {m['dd20_pct']:.1f}%, 52주 범위의 {m['pos52_pct']:.0f}% 지점" if m["pos52_pct"] is not None else f"20일 고점 대비 {m['dd20_pct']:.1f}%")
    if m["rsi14"] is not None and m["rsi2"] is not None:
        reasons.append(f"RSI(14) {m['rsi14']:.0f}, RSI(2) {m['rsi2']:.0f}")
    reasons.append(f"저점 신호 {os_n}/{sb[GROUPS[1]]['total']}개, 반등 확인 {rc_n}/{sb[GROUPS[2]]['total']}개, 과열 {oh_n}개")
    if m["gap20_pct"] is not None:
        reasons.append(f"20일선 대비 {m['gap20_pct']:+.1f}%" + (f", 200일선 대비 {(c / m['sma200'] - 1) * 100:+.1f}%" if m["sma200"] else ""))

    if oh_n >= 2:
        state, head = "상승 과열", "과열 신호가 겹쳐 있습니다. 신규 추격 매수는 불리한 구간입니다."
        guide = "보유 중이면 이익 보호선을 올리는 쪽이 유리합니다. 신규 진입은 눌림(20일선 근처)을 기다리는 편이 낫습니다."
    elif uptrend:
        if (m["rsi14"] is not None and m["rsi14"] < 50) or (m["gap20_pct"] is not None and m["gap20_pct"] < 2):
            state, head = "상승 추세 눌림", "상승 추세 안에서 쉬어가는 구간입니다."
            guide = "20일선 지지와 거래량 감소를 확인하며 분할로 접근하는 구간입니다."
        else:
            state, head = "상승 추세", "정배열 상승 추세입니다."
            guide = "추세를 따르는 구간이지만 20일선에서 멀어지면 눌림을 기다리세요."
    elif downtrend:
        if m["bounce_from_low5_pct"] >= 2 and rc_n >= 3:
            state, head = "하락 추세 속 기술적 반등", "저점에서 반등이 나오고 있으나 20일선 아래라 추세 전환으로 보기는 이릅니다."
            guide = "반등 구간의 단기 매매는 가능하지만 20일선 회복 전에는 비중을 작게 유지하는 구간입니다."
        elif os_n >= 2 and rc_n <= 2:
            state, head = "하락 추세 + 과매도, 반등 미확인", "많이 빠졌지만 아직 바닥 확인 신호는 부족합니다."
            guide = "떨어지는 칼날 구간입니다. 소액 분할 이상은 반등 확인 신호가 3개 이상 켜진 뒤로 미루는 쪽이 안전합니다."
        elif os_n >= 2:
            state, head = "하락 추세 + 과매도, 반등 신호 출현", "과매도에서 반등 신호가 일부 나왔으나 추세는 아직 하락입니다."
            guide = "소액 분할이 가능한 구간입니다. 최근 저점 이탈 시 중단 기준을 두세요."
        else:
            state, head = "하락 추세 지속 (과매도 아님)", "추세는 아래인데 과매도도 아닙니다. 매수 근거가 약합니다."
            guide = "신규 매수 근거가 부족합니다. 저점 신호가 겹칠 때까지 관망이 유리합니다."
    else:
        if m["dd20_pct"] is not None and m["dd20_pct"] <= -10 and m["higher_low"] and rc_n >= 3:
            state, head = "바닥 다지기 후보", "급락 뒤 저점을 높이며 회복 신호가 쌓이고 있습니다."
            guide = "분할 진입을 검토할 만한 구간입니다. 20일선 회복과 거래량 동반을 확인하세요."
        else:
            state, head = "혼조 구간", "추세 신호가 엇갈립니다."
            guide = "방향이 확인될 때까지 비중을 작게 유지하세요."
    return {"state": state, "headline": head, "reasons": reasons, "guide": guide,
            "pending_confirmations": pending, "invalidation_price": float(stop), "invalidation_note": "최근 5일 저점에서 0.5 ATR 아래",
            "score": sb}


# ============================================================
# 유사 상황 통계 (과거에 비슷했던 때 이후 성과)
# ============================================================

ANALOGS = {
    "급락 과매도 (20일 고점 -12% 이하 + RSI14 35 이하)": lambda i: (i["dd20"] <= -12) & (i["rsi14"] <= 35),
    "과매도 후 반등 1일차 (RSI14 40 이하에서 2일 하락 뒤 상승)":
        lambda i: (i["close"] > i["close"].shift(1)) & (i["close"].shift(1) < i["close"].shift(2)) &
                  (i["close"].shift(2) < i["close"].shift(3)) & (i["rsi14"].shift(1) <= 40),
    "RSI(2) 10 이하": lambda i: i["rsi2"] <= 10,
    "200일선 아래 + RSI(2) 10 이하": lambda i: (i["close"] < i["sma200"]) & (i["rsi2"] <= 10),
    "상승 추세 눌림 (60일선 위 + 20일선>60일선 + RSI14 45 이하)":
        lambda i: (i["close"] > i["sma60"]) & (i["sma20"] > i["sma60"]) & (i["rsi14"] <= 45),
}
HORIZONS = (5, 10, 20)
EPISODE_GAP = 10     # 같은 사건으로 묶는 간격(거래일)


def _episodes(mask: np.ndarray, gap: int = EPISODE_GAP) -> list[int]:
    idx, last = [], -10 ** 9
    for i in np.flatnonzero(mask):
        if i - last >= gap:
            idx.append(int(i))
        last = i      # 연속 구간은 첫날만 기록
    return idx


def _fwd_stats(ind: pd.DataFrame, idxs: list[int], h: int) -> dict:
    c = ind["close"].to_numpy(float)
    lo = ind["low"].to_numpy(float)
    rets, maes = [], []
    for i in idxs:
        if i + h >= len(c):
            continue
        rets.append((c[i + h] / c[i] - 1) * 100)
        maes.append((lo[i + 1:i + h + 1].min() / c[i] - 1) * 100)
    if not rets:
        return {"n": 0}
    a = np.array(rets)
    return {"n": int(len(a)), "win_pct": float((a > 0).mean() * 100), "mean": float(a.mean()), "median": float(np.median(a)),
            "worst": float(a.min()), "best": float(a.max()), "mae_median": float(np.median(maes))}


def analog_stats(df: pd.DataFrame) -> dict:
    """조건별 사건 수·이후 성과 + 오늘 켜져 있는지 + 전체 기간 기준선."""
    ind = add_indicators(df)
    n = len(ind)
    if n < 260:
        return {"rows": [], "baseline": {}, "n_bars": n}
    base = {}
    allidx = list(range(60, n))
    for h in HORIZONS:
        base[h] = _fwd_stats(ind, allidx, h)
    rows = []
    for name, fn in ANALOGS.items():
        mask = fn(ind).fillna(False).to_numpy(bool)
        active = bool(mask[-1])
        ep = _episodes(mask[:-1] if False else mask)
        ep_hist = [i for i in ep if i < n - 1]
        row = {"name": name, "active_today": active, "episodes": len(ep_hist), "stats": {h: _fwd_stats(ind, ep_hist, h) for h in HORIZONS}}
        rows.append(row)
    return {"rows": rows, "baseline": base, "n_bars": n, "first": str(ind["date"].iloc[0]), "last": str(ind["date"].iloc[-1])}


# ============================================================
# 지지·저항
# ============================================================


def levels(df: pd.DataFrame, price: float | None = None, last_is_partial: bool = False) -> list[dict]:
    """가까운 지지·저항 가격 목록. last_is_partial이면 마지막 봉(오늘, 미완성)을 빼고 전일 기준으로 계산."""
    d = norm_daily(df)
    if len(d) < 25:
        return []
    ref = d.iloc[:-1] if last_is_partial else d
    r = ref.iloc[-1]
    px = float(price if price is not None else d["close"].iloc[-1])
    p = (r["high"] + r["low"] + r["close"]) / 3
    rng = r["high"] - r["low"]
    out = [
        ("피벗", p), ("저항1 (R1)", 2 * p - r["low"]), ("지지1 (S1)", 2 * p - r["high"]),
        ("저항2 (R2)", p + rng), ("지지2 (S2)", p - rng),
        ("전일 고가", r["high"]), ("전일 저가", r["low"]), ("전일 종가", r["close"]),
        ("최근 20일 고가", ref["high"].tail(20).max()), ("최근 20일 저가", ref["low"].tail(20).min()),
        ("최근 60일 고가", ref["high"].tail(60).max()), ("최근 60일 저가", ref["low"].tail(60).min()),
    ]
    c = ref["close"]
    for n in (5, 20, 60, 120, 200):
        if len(c) >= n:
            out.append((f"{n}일선", c.tail(n).mean()))
    res = [{"name": k, "price": float(v), "dist_pct": (float(v) / px - 1) * 100,
            "side": "저항" if v > px else "지지"} for k, v in out]
    return sorted(res, key=lambda x: x["price"], reverse=True)


# ============================================================
# 장중 상태 (오늘 분봉 기준)
# ============================================================


def intraday_read(bars: pd.DataFrame, prev_close: float | None = None) -> dict:
    """정리된 오늘 분봉(미래 봉 제외)으로 현재 위치 요약. 봉이 10개 미만이면 빈 dict."""
    from k200_core import norm_minute, vwap_series
    b = bars if "mins" in bars.columns else norm_minute(bars)
    if len(b) < 10:
        return {}
    vw = vwap_series(b)
    last = float(b["close"].iloc[-1])
    hi, lo, op = float(b["high"].max()), float(b["low"].min()), float(b["open"].iloc[0])
    tail = b.tail(30)
    slope = (float(tail["close"].iloc[-1]) / float(tail["close"].iloc[0]) - 1) * 100
    out = {
        "time": str(b["time"].iloc[-1]), "last": last, "open": op, "high": hi, "low": lo, "vwap": float(vw.iloc[-1]),
        "vs_vwap_pct": (last / float(vw.iloc[-1]) - 1) * 100, "vs_open_pct": (last / op - 1) * 100,
        "from_high_pct": (last / hi - 1) * 100, "from_low_pct": (last / lo - 1) * 100,
        "range_pos_pct": (last - lo) / (hi - lo) * 100 if hi > lo else 50.0,
        "last30_pct": slope, "bars": int(len(b)), "volume": float(b["volume"].sum()),
    }
    if prev_close:
        out["gap_pct"] = (op / prev_close - 1) * 100
        out["vs_prev_close_pct"] = (last / prev_close - 1) * 100
    return out


# ============================================================
# 수급 요약
# ============================================================

INVESTORS = (("frgn", "외국인"), ("orgn", "기관"), ("prsn", "개인"))


def _streak(vals: list) -> int:
    """최신순 값 목록에서 같은 부호가 이어진 일수 (+면 순매수, -면 순매도 연속)."""
    s = 0
    sign = 0
    for v in vals:
        if v is None or v == 0:
            break
        sg = 1 if v > 0 else -1
        if sign == 0:
            sign = sg
        if sg != sign:
            break
        s += 1
    return sign * s


def flow_summary(series: dict) -> list[dict]:
    """series: {code: {"name":..., "rows":[{date, frgn, orgn, prsn, frgn_amt?, orgn_amt?, prsn_amt?}, ...]}} → 표 행."""
    rows_out = []
    for code, s in (series or {}).items():
        rows = sorted((s.get("rows") or []), key=lambda r: r["date"], reverse=True)
        if not rows:
            continue
        for key, label in INVESTORS:
            qty = [r.get(key) for r in rows]
            amt = [r.get(f"{key}_amt") for r in rows]
            def tot(arr, n):
                v = [x for x in arr[:n] if x is not None]
                return float(sum(v)) if v else None
            streak = _streak(qty)
            prev_run = _streak(qty[abs(streak):]) if streak != 0 else 0
            flip = None
            if streak > 0 and streak <= 2 and prev_run <= -3:
                flip = "순매도 후 순매수 전환"
            rows_out.append({
                "code": code, "name": s.get("name", code), "investor": label, "last_date": rows[0]["date"],
                "last_qty": qty[0], "qty5": tot(qty, 5), "qty20": tot(qty, 20),
                "last_amt": amt[0], "amt5": tot(amt, 5), "amt20": tot(amt, 20),
                "streak": streak, "flip": flip, "n_rows": len(rows),
            })
    return rows_out


def flow_read(series: dict) -> dict:
    """수급을 한 줄로: 대형주(삼성전자·SK하이닉스) 외국인 흐름과 KODEX 200 자체 흐름."""
    rows = flow_summary(series)
    lines = []
    for r in rows:
        if r["investor"] != "외국인":
            continue
        st = r["streak"]
        txt = f"{r['name']} 외국인 " + (f"{st}일 연속 순매수" if st > 0 else f"{-st}일 연속 순매도" if st < 0 else "방향 없음")
        if r["amt5"] is not None:
            txt += f", 5일 합 {r['amt5'] / 100:+,.0f}억 원"
        elif r["qty5"] is not None:
            txt += f", 5일 합 {r['qty5']:+,.0f}주"
        if r["flip"]:
            txt += f" ({r['flip']})"
        lines.append(txt)
    return {"rows": rows, "lines": lines}


# ============================================================
# 라이브(장중) 반영: 오늘 일봉을 현재가로 갱신
# ============================================================


def with_live_bar(df: pd.DataFrame, today: str, o: float, h: float, l: float, c: float, v: float) -> pd.DataFrame:
    """오늘 일봉(장중 미완성)을 현재 값으로 넣거나 교체한다."""
    d = norm_daily(df)
    d = d[d["date"] != today]
    add = pd.DataFrame([{"date": today, "open": o, "high": h, "low": l, "close": c, "volume": v}])
    return pd.concat([d, add], ignore_index=True)


def build_view(daily: pd.DataFrame) -> dict:
    """대시보드가 쓰는 한 번에 계산."""
    m = snapshot(daily)
    if not m:
        return {}
    cards = signal_cards(m)
    return {"m": m, "cards": cards, "state": classify(m, cards)}
