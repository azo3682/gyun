# -*- coding: utf-8 -*-
"""
scripts/k200_core.py — KODEX 200 매매 분석 엔진 (네트워크 없음, 순수 계산)

목적: "어떤 매매 규칙이 비용을 빼고도 돈이 되는가"를 데이터로 가려낸다.
  - 스윙(일봉), 당일 단타(1분봉), 스켈핑 해상도 진단(1분봉)을 같은 기준으로 평가한다.
  - 규칙의 파라미터는 미리 고정해 둔다(데이터를 보고 맞추지 않는다). 규칙 개수만큼 통계 기준을 높인다(본페로니).
  - 모든 결과는 비용(슬리피지·스프레드·수수료) 차감 후 bps(1bp = 0.01%) 단위.

한계(코드가 아니라 데이터/구조의 한계):
  - 1분봉으로는 수초 단위 스켈핑을 검증할 수 없다. 여기서는 '1~5분 보유' 수준까지만 본다.
  - 분봉 이력은 수집기가 쌓은 날짜만큼만 있다. 표본이 모자라면 판정은 '표본부족'이다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
import pandas as pd

BPS = 1e4
TICK_DEFAULT = 5.0          # ETF 호가 단위(원). 실제 값은 확인 필요 — 사이드바에서 바꿀 수 있게 한다.
ORB_MINUTES = 15            # 시초 범위(오프닝 레인지) 길이
CUTOFF_MIN = 15 * 60 + 19   # 15:19 이후 신규 진입·보유 금지 (15:20부터 종가 단일가)
OPEN_MIN = 9 * 60
MIN_TRADES = 30             # 이보다 적으면 판정하지 않는다


# ============================================================
# 비용 모델
# ============================================================

@dataclass
class CostModel:
    """편도 비용 = 슬리피지(스프레드 포함, 틱 단위) + 수수료. 왕복은 2배.
    ETF는 증권거래세가 없다고 알고 있으나 증권사 수수료는 사용자가 직접 입력한다."""
    tick: float = TICK_DEFAULT
    slip_ticks: float = 1.0
    commission_bps: float = 0.0

    def one_way_bps(self, price: float) -> float:
        if not price or price <= 0:
            return float("nan")
        return self.slip_ticks * self.tick / price * BPS + self.commission_bps

    def round_trip_bps(self, price: float) -> float:
        return 2.0 * self.one_way_bps(price)


# ============================================================
# 데이터 정리
# ============================================================

def norm_daily(df: pd.DataFrame) -> pd.DataFrame:
    """date/open/high/low/close/volume 열을 가진 일봉을 오름차순으로 정리한다."""
    cols = ["date", "open", "high", "low", "close", "volume"]
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=cols)
    out = df[cols].copy()
    out["date"] = out["date"].astype(str)
    for c in cols[1:]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[(out[["open", "high", "low", "close"]] > 0).all(axis=1)]
    return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def _hhmm_to_min(t) -> int:
    s = str(t).strip().replace(":", "")
    s = s.zfill(6) if len(s) > 4 else s.zfill(4)
    return int(s[:2]) * 60 + int(s[2:4])


def norm_minute(df: pd.DataFrame) -> pd.DataFrame:
    """한 거래일의 1분봉. time(HHMM 또는 HHMMSS)/open/high/low/close/volume → mins 열 추가, 시간 오름차순."""
    cols = ["time", "open", "high", "low", "close", "volume"]
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=cols + ["mins"])
    out = df[cols].copy()
    for c in cols[1:]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[(out[["open", "high", "low", "close"]] > 0).all(axis=1)]
    out["mins"] = out["time"].map(_hhmm_to_min)
    out = out.drop_duplicates("mins").sort_values("mins").reset_index(drop=True)
    return out


FLAT_FILL_MAX = 0.10   # 정규장 봉 중 '거래 없는 평평한 봉'이 이 비율을 넘으면 선채움(가짜 봉)으로 본다


def flat_fill_ratio(bars: pd.DataFrame) -> float:
    """09:00–15:19 봉 중 거래량 0이고 시고저종이 같은 봉의 비율. KIS가 아직 오지 않은 시각을 마지막 가격으로
    채워 보내는 것이 실제 확인되어(2026-10-22), 그런 날을 걸러내는 데 쓴다."""
    if bars is None or len(bars) == 0:
        return 0.0
    b = bars if "mins" in bars.columns else norm_minute(bars)
    sess = b[(b["mins"] >= OPEN_MIN) & (b["mins"] <= CUTOFF_MIN)]
    if len(sess) == 0:
        return 0.0
    flat = (sess["volume"].fillna(0) == 0) & (sess["open"] == sess["high"]) & (sess["high"] == sess["low"]) & (sess["low"] == sess["close"])
    return float(flat.mean())


# ============================================================
# 지표
# ============================================================

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out[(dn == 0) & (up > 0)] = 100.0
    return out


def vwap_series(day: pd.DataFrame) -> pd.Series:
    tp = (day["high"] + day["low"] + day["close"]) / 3.0
    vol = day["volume"].fillna(0)
    pv = (tp * vol).cumsum()
    cv = vol.cumsum()
    v = pv / cv.replace(0, np.nan)
    return v.ffill().fillna(day["close"])


# ============================================================
# 거래 시뮬레이션 (일봉, 롱 온리)
# 신호는 종가에 확정 → 체결은 다음 거래일 시가. 손절은 당일 장중에 닿으면 손절가(갭이면 시가).
# ============================================================

def run_long_only(df: pd.DataFrame, enter, exit_, max_hold: int | None = None,
                  stop_pct: float | None = None) -> list[dict]:
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    d = df["date"].to_numpy()
    n = len(df)
    enter = np.asarray(enter, dtype=bool)
    exit_ = np.asarray(exit_, dtype=bool)
    trades: list[dict] = []
    i = 0
    while i < n - 1:
        if not enter[i]:
            i += 1
            continue
        ei = i + 1
        ep = o[ei]
        stop = ep * (1 - stop_pct) if stop_pct else None
        k = ei
        while True:
            if stop is not None and l[k] <= stop:
                xp, xi, why = min(stop, o[k]), k, "손절"
                break
            if k == n - 1:
                xp, xi, why = c[k], k, "기간종료"
                break
            held = k - ei + 1
            if exit_[k]:
                xp, xi, why = o[k + 1], k + 1, "청산신호"
                break
            if max_hold and held >= max_hold:
                xp, xi, why = o[k + 1], k + 1, "보유기간"
                break
            k += 1
        trades.append({"entry": str(d[ei]), "exit": str(d[xi]), "entry_px": float(ep), "exit_px": float(xp),
                       "bars": int(xi - ei + 1), "why": why, "gross_bps": (xp / ep - 1) * BPS})
        i = xi
    return trades


# ============================================================
# 스윙 규칙 (일봉) — 파라미터는 고정. 바꾸려면 이 파일에서 바꾸고, 바꾼 횟수를 기록할 것.
# ============================================================

SWING_RULES = {
    "S1 추세추종": "종가>20일선 & 20일선>60일선이면 매수, 종가가 20일선 아래로 내려가면 청산",
    "S2 RSI(2) 눌림": "종가>200일선 상태에서 RSI(2)<10이면 매수, RSI(2)>70 또는 10일 경과 시 청산",
    "S3 급락 반등": "20일 고점 대비 -7% 이하면 매수, 10일 경과 또는 -5% 손절",
    "S0 단순 보유": "첫날 시가 매수, 마지막 종가 청산 (비교 기준)",
}


def swing_signals(df: pd.DataFrame) -> dict:
    """각 규칙의 (enter, exit_, kwargs)를 만든다."""
    c = df["close"]
    s20, s60, s200 = sma(c, 20), sma(c, 60), sma(c, 200)
    r2 = rsi(c, 2)
    hi20 = c.rolling(20, min_periods=20).max()
    n = len(df)
    sigs = {
        "S1 추세추종": ((c > s20) & (s20 > s60), (c < s20), {}),
        "S2 RSI(2) 눌림": ((c > s200) & (r2 < 10), (r2 > 70), {"max_hold": 10}),
        "S3 급락 반등": ((c <= hi20 * 0.93), pd.Series(False, index=df.index), {"max_hold": 10, "stop_pct": 0.05}),
    }
    out = {}
    for k, (e, x, kw) in sigs.items():
        out[k] = (e.fillna(False).to_numpy(bool), x.fillna(False).to_numpy(bool), kw)
    e0 = np.zeros(n, dtype=bool)
    if n >= 2:
        e0[0] = True
    out["S0 단순 보유"] = (e0, np.zeros(n, dtype=bool), {})
    return out


def eval_swing(df: pd.DataFrame, cost: CostModel) -> dict:
    """규칙별 거래 목록(비용 반영 net_bps 포함)."""
    res = {}
    for name, (e, x, kw) in swing_signals(df).items():
        tr = run_long_only(df, e, x, **kw)
        for t in tr:
            t["net_bps"] = t["gross_bps"] - cost.round_trip_bps(t["entry_px"])
        res[name] = tr
    return res


def swing_today(df: pd.DataFrame) -> dict:
    """마지막 봉 종가 기준, 규칙별 오늘 진입/청산 조건 충족 여부와 현재 지표."""
    if len(df) < 2:
        return {}
    sigs = swing_signals(df)
    c = df["close"]
    out = {"rules": {}}
    for name, (e, x, _) in sigs.items():
        if name == "S0 단순 보유":
            continue
        out["rules"][name] = {"enter": bool(e[-1]), "exit": bool(x[-1])}
    r2, r14 = rsi(c, 2).iloc[-1], rsi(c, 14).iloc[-1]
    s20, s60, s200 = (sma(c, n).iloc[-1] for n in (20, 60, 200))
    hi20 = c.rolling(20, min_periods=20).max().iloc[-1]
    ret = c.pct_change()
    last = float(c.iloc[-1])
    out["metrics"] = {
        "date": str(df["date"].iloc[-1]), "close": last,
        "day_ret_pct": float(ret.iloc[-1] * 100) if len(ret) > 1 else None,
        "rsi2": None if pd.isna(r2) else float(r2), "rsi14": None if pd.isna(r14) else float(r14),
        "vs_sma20_pct": None if pd.isna(s20) else float((last / s20 - 1) * 100),
        "vs_sma60_pct": None if pd.isna(s60) else float((last / s60 - 1) * 100),
        "vs_sma200_pct": None if pd.isna(s200) else float((last / s200 - 1) * 100),
        "from_20d_high_pct": None if pd.isna(hi20) else float((last / hi20 - 1) * 100),
        "vol20_ann_pct": None if ret.tail(20).isna().all() else float(ret.tail(20).std() * math.sqrt(252) * 100),
    }
    return out


# ============================================================
# 당일 단타 규칙 (1분봉) — 하루 단위 리스트 days = [{"date": "YYYYMMDD", "bars": DataFrame}, ...] 오름차순
# ============================================================

DAY_RULES = {
    "D1 시초 범위 돌파": f"첫 {ORB_MINUTES}분 고가를 종가로 돌파하면 다음 봉 시가 매수, 범위 저가 이탈 시 손절, 15:19 청산",
    "D2 VWAP 이탈 반등": "VWAP 대비 괴리가 최근 30분 표준편차의 -2배 아래면 매수, VWAP 회복·30분 경과·-30bp 손절·15:19 중 먼저 오는 쪽에서 청산",
    "D3 갭하락 반등": "시가가 전일 종가 대비 -0.5% 이하면 시가 매수, 15:19 청산",
}


def _day_bars(day) -> pd.DataFrame:
    b = day["bars"]
    return b if "mins" in b.columns else norm_minute(b)


def day_orb(day, orb_minutes: int = ORB_MINUTES) -> list[dict]:
    b = _day_bars(day)
    if len(b) < orb_minutes + 5:
        return []
    start = int(b["mins"].iloc[0])
    orb = b[b["mins"] < start + orb_minutes]
    if len(orb) < max(5, orb_minutes // 2):
        return []
    orh, orl = float(orb["high"].max()), float(orb["low"].min())
    rest = b[b["mins"] >= start + orb_minutes].reset_index(drop=True)
    for j in range(len(rest) - 1):
        if rest["close"].iloc[j] > orh and rest["mins"].iloc[j + 1] <= CUTOFF_MIN:
            ep = float(rest["open"].iloc[j + 1])
            if ep <= orl:      # 갭으로 이미 범위 아래면 진입하지 않는다
                return []
            for k in range(j + 1, len(rest)):
                if rest["mins"].iloc[k] > CUTOFF_MIN:
                    k -= 1
                    return [_trade(day, ep, float(rest["close"].iloc[k]), "시간청산", k - j)]
                if rest["low"].iloc[k] <= orl:
                    return [_trade(day, ep, min(orl, float(rest["open"].iloc[k])), "손절", k - j + 1)]
            return [_trade(day, ep, float(rest["close"].iloc[-1]), "시간청산", len(rest) - j)]
    return []


def day_vwap_revert(day, k_sigma: float = 2.0, win: int = 30, stop_bps: float = 30.0, max_hold: int = 30) -> list[dict]:
    b = _day_bars(day)
    if len(b) < win + 5:
        return []
    vw = vwap_series(b)
    dev = b["close"] - vw
    sd = dev.rolling(win, min_periods=20).std()
    trades, j = [], 0
    n = len(b)
    while j < n - 1:
        m = int(b["mins"].iloc[j])
        ok = (OPEN_MIN + 30 <= m <= 14 * 60 + 30) and not pd.isna(sd.iloc[j]) and sd.iloc[j] > 0
        if not (ok and dev.iloc[j] < -k_sigma * sd.iloc[j]):
            j += 1
            continue
        ei = j + 1
        ep = float(b["open"].iloc[ei])
        stop = ep * (1 - stop_bps / BPS)
        k, xp, why = ei, None, None
        while True:
            if b["low"].iloc[k] <= stop:
                xp, why = min(stop, float(b["open"].iloc[k])), "손절"
                break
            if int(b["mins"].iloc[k]) >= CUTOFF_MIN or k == n - 1:
                xp, why = float(b["close"].iloc[k]), "시간청산"
                break
            if b["close"].iloc[k] >= vw.iloc[k]:
                xp, why = float(b["open"].iloc[k + 1]), "VWAP회복"
                k += 1
                break
            if k - ei + 1 >= max_hold:
                xp, why = float(b["open"].iloc[k + 1]), "보유시간"
                k += 1
                break
            k += 1
        trades.append(_trade(day, ep, xp, why, k - ei + 1))
        j = k
    return trades


def day_gap_fade(day, prev_close: float | None, gap_pct: float = -0.5) -> list[dict]:
    b = _day_bars(day)
    if prev_close is None or prev_close <= 0 or len(b) < 10:
        return []
    ep = float(b["open"].iloc[0])
    if (ep / prev_close - 1) * 100 > gap_pct:
        return []
    late = b[b["mins"] <= CUTOFF_MIN]
    if len(late) < 5:
        return []
    return [_trade(day, ep, float(late["close"].iloc[-1]), "시간청산", len(late))]


def _trade(day, ep, xp, why, bars) -> dict:
    return {"entry": str(day["date"]), "exit": str(day["date"]), "entry_px": float(ep), "exit_px": float(xp),
            "bars": int(bars), "why": why, "gross_bps": (xp / ep - 1) * BPS}


def eval_day(days: list[dict], cost: CostModel) -> dict:
    days = sorted(days, key=lambda d: str(d["date"]))
    res = {"D1 시초 범위 돌파": [], "D2 VWAP 이탈 반등": [], "D3 갭하락 반등": []}
    prev_close = None
    for d in days:
        b = _day_bars(d)
        d = {"date": d["date"], "bars": b}
        res["D1 시초 범위 돌파"] += day_orb(d)
        res["D2 VWAP 이탈 반등"] += day_vwap_revert(d)
        res["D3 갭하락 반등"] += day_gap_fade(d, prev_close)
        if len(b):
            prev_close = float(b["close"].iloc[-1])
    for tr in res.values():
        for t in tr:
            t["net_bps"] = t["gross_bps"] - cost.round_trip_bps(t["entry_px"])
    return res


# ============================================================
# 스켈핑 해상도 진단 (1분봉으로 볼 수 있는 한계까지)
# ============================================================

def cost_vs_move(days: list[dict], cost: CostModel) -> dict:
    """1분 변동폭(절대값)과 왕복 비용의 비교. 비용이 변동폭의 큰 부분이면 스켈핑은 구조적으로 불리하다."""
    moves, prices = [], []
    for d in days:
        b = _day_bars(d)
        if len(b) < 10:
            continue
        r = (b["close"].pct_change().abs() * BPS).dropna()
        moves += r.tolist()
        prices.append(float(b["close"].median()))
    if not moves:
        return {}
    px = float(np.median(prices))
    rt = cost.round_trip_bps(px)
    m = np.array(moves)
    return {"bars": int(len(m)), "median_abs_bps": float(np.median(m)), "mean_abs_bps": float(m.mean()),
            "p90_abs_bps": float(np.percentile(m, 90)), "round_trip_cost_bps": float(rt),
            "cost_to_median_move": float(rt / np.median(m)) if np.median(m) > 0 else None, "ref_price": px}


def scalp_horizon_table(days: list[dict], cost: CostModel, horizons=(1, 3, 5), q_pct: float = 90.0) -> list[dict]:
    """직전 1분봉이 크게 오르거나(상위 q%) 내린 뒤, h분 보유 롱의 평균 수익(비용 후).
    진입은 신호 봉의 다음 봉 시가, 청산은 h봉 뒤 종가. 같은 날 안에서 겹치지 않게 표본을 솎는다."""
    prepared = []
    allabs = []
    for d in days:
        b = _day_bars(d)
        if len(b) < 20:
            continue
        r1 = b["close"].pct_change() * BPS
        prepared.append((b, r1))
        allabs += r1.abs().dropna().tolist()
    if not allabs:
        return []
    q = float(np.percentile(allabs, q_pct))
    rows = []
    for h in horizons:
        groups = {"전체": [], "큰 상승 직후": [], "큰 하락 직후": []}
        for b, r1 in prepared:
            last_taken = {"전체": -10**9, "큰 상승 직후": -10**9, "큰 하락 직후": -10**9}
            n = len(b)
            for t in range(5, n - h - 1):
                if not (OPEN_MIN + 5 <= int(b["mins"].iloc[t]) <= CUTOFF_MIN - h - 1):
                    continue
                ep = float(b["open"].iloc[t + 1])
                xp = float(b["close"].iloc[t + h])
                g = (xp / ep - 1) * BPS
                net = g - cost.round_trip_bps(ep)
                x = r1.iloc[t]
                tags = ["전체"]
                if x >= q:
                    tags.append("큰 상승 직후")
                if x <= -q:
                    tags.append("큰 하락 직후")
                for tg in tags:
                    if t - last_taken[tg] >= h:
                        groups[tg].append((g, net))
                        last_taken[tg] = t
        for tg, vals in groups.items():
            if not vals:
                rows.append({"horizon_min": h, "group": tg, "n": 0})
                continue
            a = np.array(vals)
            gross, net = a[:, 0], a[:, 1]
            sd = net.std(ddof=1) if len(net) > 1 else float("nan")
            t_stat = net.mean() / (sd / math.sqrt(len(net))) if len(net) > 1 and sd > 0 else float("nan")
            rows.append({"horizon_min": h, "group": tg, "n": int(len(net)), "gross_bps": float(gross.mean()),
                         "net_bps": float(net.mean()), "t": float(t_stat), "win_pct": float((net > 0).mean() * 100),
                         "q_bps": q})
    return rows


# ============================================================
# 통계·판정
# ============================================================

def summarize(net_bps) -> dict:
    a = np.asarray([x for x in net_bps if x is not None and not pd.isna(x)], dtype=float)
    n = len(a)
    if n == 0:
        return {"n": 0}
    wins, losses = a[a > 0], a[a <= 0]
    mean = float(a.mean())
    sd = float(a.std(ddof=1)) if n > 1 else float("nan")
    t = mean / (sd / math.sqrt(n)) if n > 1 and sd > 0 else float("nan")
    gl = float(losses.sum())
    pf = float(wins.sum() / abs(gl)) if gl < 0 else (float("inf") if wins.sum() > 0 else float("nan"))
    eq = np.cumprod(1 + a / BPS)
    peak = np.maximum.accumulate(eq)
    avg_w = float(wins.mean()) if len(wins) else 0.0
    avg_l = float(losses.mean()) if len(losses) else 0.0
    return {"n": n, "win_pct": float(len(wins) / n * 100), "avg_win_bps": avg_w, "avg_loss_bps": avg_l,
            "payoff": float(avg_w / abs(avg_l)) if avg_l < 0 else float("nan"),
            "expectancy_bps": mean, "t": float(t), "pf": pf,
            "mdd_pct": float(((eq / peak) - 1).min() * 100), "total_pct": float((eq[-1] - 1) * 100)}


def bonferroni_t(k: int, alpha: float = 0.05) -> float:
    return NormalDist().inv_cdf(1 - alpha / (2 * max(int(k), 1)))


def split_by_time(trades: list[dict], frac: float = 0.6):
    tr = sorted(trades, key=lambda t: (t["entry"], t["exit"]))
    cut = int(len(tr) * frac)
    return tr[:cut], tr[cut:]


def verdict(trades: list[dict], k_rules: int, min_n: int = MIN_TRADES) -> dict:
    net = [t["net_bps"] for t in trades]
    full = summarize(net)
    ins, oos = split_by_time(trades)
    s_ins, s_oos = summarize([t["net_bps"] for t in ins]), summarize([t["net_bps"] for t in oos])
    thr = bonferroni_t(k_rules)
    if full["n"] < min_n:
        v = "표본부족"
    elif full["expectancy_bps"] <= 0:
        v = "음의 기대값"
    elif s_oos["n"] >= 10 and s_oos["expectancy_bps"] > 0 and full["t"] >= thr:
        v = "후보 (추가 검증 필요)"
    else:
        v = "근거 없음 (양의 값이나 우연과 구분 불가)"
    return {"verdict": v, "full": full, "in_sample": s_ins, "out_of_sample": s_oos, "t_threshold": thr}


def evaluate_all(daily: pd.DataFrame | None, days: list[dict] | None, cost: CostModel) -> dict:
    """대시보드가 쓰는 한 번에 평가: 규칙별 판정표 + 스캘프 진단."""
    out = {"swing": {}, "day": {}, "scalp": [], "cost_move": {}, "n_rules": 0}
    swing = eval_swing(norm_daily(daily), cost) if daily is not None and len(daily) >= 120 else {}
    dayres = eval_day(days, cost) if days else {}
    k = len([x for x in swing if not x.startswith("S0")]) + len(dayres)
    out["n_rules"] = k
    for name, tr in swing.items():
        out["swing"][name] = {"trades": tr, **verdict(tr, k if not name.startswith("S0") else 1)}
    for name, tr in dayres.items():
        out["day"][name] = {"trades": tr, **verdict(tr, k)}
    if days:
        out["scalp"] = scalp_horizon_table(days, cost)
        out["cost_move"] = cost_vs_move(days, cost)
    return out
