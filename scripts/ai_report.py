# -*- coding: utf-8 -*-
"""
scripts/ai_report.py
ChatGPT 같은 외부 AI가 읽고 분석할 수 있는 리포트를 만든다.

출력
  data/ai_report.md        — 분석 지시문 + 종목 표 + 종목별 상세 (AI에게 이 파일 주소를 주거나 내용을 붙여넣는다)
  data/ai_report_short.md  — 지시문 + 표만 (한 번에 읽는 분량이 작은 무료 AI용)
  data/ai_report.json      — 같은 내용을 구조화한 것 (앱의 'AI 분석용' 탭이 읽는다)

어떤 종목을 넣나
  1) 지금 순매수 상위 10   2) 신호 추적에 최근 기록된 종목   3) data/watch_codes.txt 의 관심 종목(선택)
  → 최대 MAX_STOCKS개. 리포트가 너무 길어지면 무료 AI의 입력 한도를 넘을 수 있어 상한을 둔다.

주의
  - 스윙 체크 점수는 대화 중 임의로 정한 기준이다. 백테스트로 검증된 점수가 아니다. 리포트 지시문에도 그렇게 적는다.
  - 매매 일지(비공개 저장소)와 API 키·비밀번호는 절대 넣지 않는다.
  - 조회가 대부분 실패한 날(휴장일, API 장애)에는 직전 리포트를 덮어쓰지 않는다.
"""

import json
import os
import re
import time
from datetime import datetime

import pandas as pd
import requests

from common import (
    BASE_URL, KST, kis_headers, fetch_investor_ranking, fetch_volume_ranking, fetch_price_detail, market_risk_flags,
    _to_float, fetch_financial_ratio, strip_raw, composite_score, fetch_daily_ohlcv, analyze_technicals,
    compute_transition_signal, check_disclosure_risk,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
REPORT_MD_PATH = os.path.join(DATA_DIR, "ai_report.md")
REPORT_JSON_PATH = os.path.join(DATA_DIR, "ai_report.json")
REPORT_SHORT_PATH = os.path.join(DATA_DIR, "ai_report_short.md")
WATCH_PATH = os.path.join(DATA_DIR, "watch_codes.txt")
TRACKER_PATH = os.path.join(DATA_DIR, "signal_tracker.json")

MAX_STOCKS = 14
RECENT_SIGNAL_DATES = 3        # 신호 추적에서 최근 몇 개 신호일의 종목을 넣을지
MIN_OK_STOCKS = 3              # 이보다 적게 조회되면 직전 리포트를 유지한다

INVESTOR_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"
INVESTOR_DAILY_TR_ID = "FHKST01010900"
INVESTOR_EST_PATH = "/uapi/domestic-stock/v1/quotations/investor-trend-estimate"
INVESTOR_EST_TR_ID = "HHPTJ04160200"
EST_SLOT_LABELS = {"1": "09:30", "2": "10:00", "3": "11:20", "4": "13:20", "5": "14:30"}

PROMPT = """## 분석 지시문 (이 부분부터 읽고 따라 주세요)
당신은 한국 주식 스윙 매매(며칠~몇 주 보유) 후보를 점검하는 분석 보조입니다. 아래 데이터는 제가 만든 대시보드가 한국투자증권 API 등으로 자동 수집한 것입니다.

규칙
1. 이 문서에 있는 값만 근거로 삼으세요. 없는 정보(뉴스, 실적 전망, 업종 이슈)는 제가 주지 않았으니 아는 것처럼 단정하지 말고 "데이터 없음"이라고 말하세요.
2. '스윙 체크 점수'와 '앱 종합점수'는 제가 임의로 정한 기준이고 백테스트로 검증되지 않았습니다. 점수가 높다고 오를 가능성이 높다는 뜻이 아닙니다. 점수보다 구성 항목을 보고 판단하세요.
3. 투자 자문이 아닙니다. 매수·매도를 지시하지 말고, 어떤 조건이면 근거가 강해지고 약해지는지 조건부로 설명하세요. 최종 판단은 제가 합니다.
4. 같은 날 여러 종목이 함께 급등했다면 시장 전체 효과일 수 있음을 함께 고려하세요.
5. 각 데이터의 기준 시점(아래 '데이터 기준')을 확인하고, 장중 값은 잠정치라는 점을 반영하세요.

요청
- 종목별로 (a) 근거가 되는 강점 (b) 약점·위험 (c) 더 확인이 필요한 것을 각 2~3줄로 정리해 주세요.
- 후보를 비교하는 표(과열 정도, 수급 지속성, 재무, 추세)와, 점수가 아닌 이유 중심의 근거 강도 순서를 보여 주세요.
- 진입을 생각한다면 제가 미리 정해야 할 항목(손절 기준, 투입 금액, 보유 기간, 재진입 규칙)의 체크리스트를 주세요. 값은 제가 정합니다.
- 제가 따로 질문을 하면 그 질문을 우선하세요.
"""

FIELD_GUIDE = """## 필드 설명
- 등락: 전일 종가 대비 %. 장중이면 실시간, 마감 후면 오늘 확정.
- 순매수(외국인/기관): 오늘 장중 가집계 순매수 수량(단위 만주). 외국인은 09:30·11:20·13:20·14:30, 기관은 10:00·11:20·13:20·14:30에만 갱신되는 증권사 집계라 그 사이에는 값이 그대로이고, 기관은 10:00 전에는 0으로 보일 수 있습니다.
- 수급 흐름: 일별 확정 데이터 기준 연속 순매수(+) / 순매도(-) 일수와 최근 5일 누적(단위 만주). 오늘 값은 장 종료 후에야 들어오므로 보통 '어제까지' 기준입니다.
- 전환신호: 눌림(VCP) 이후 거래량이 20일 평균의 1.5배 이상으로 터지면서 실제로 오른 날. 백테스트로 재검증되지 않은 신호입니다.
- RSI: 70 이상 과열, 80 이상 강한 과열. 볼린저(20일): 상단은 평균+2표준편차. '상단까지'는 현재가에서 상단까지 남은 % (음수면 이미 돌파).
- 재무: 한국투자증권 재무비율의 최근 결산(기준월 표시). 6월 기준이면 반기 누적이고 ROE는 연환산입니다. 영업이익 증가율 0은 적자 지속·흑자전환·적자전환일 수 있어 점수에서 제외합니다.
- 스윙 체크 점수(100): 수급 20(순매수 상위 10에 있으면 10, 거래량 상위에도 있으면 +10) · 신호 20 · 추세 20(정배열 20, 상승추세 속 조정 14, 그 외 0) · 가격부담 20(오늘 +7% -6, +10% -10 / RSI 70 -5, 80 -10 / 볼린저 상단 1.5% 이내·돌파 -5) · 재무 20(매출·영업이익 증가율 플러스, ROE 10% 이상, 부채비율 150% 이하 각 5). 재무 값이 비어 있으면 재무 없이 80점 만점으로 표시합니다.
- 앱 종합점수(100): ROE·부채비율·성장률·PER/PBR 가중합. 재무 값이 비면 신뢰도가 낮다고 표시합니다.
"""


# ---------------------------------------------------------------- 유틸
def _retry(fn, *args, attempts: int = 2, delay: float = 0.7):
    for i in range(attempts):
        try:
            return fn(*args)
        except Exception:
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    return None


def _to_int(v):
    try:
        t = str(v).replace(",", "").strip()
        return int(float(t)) if t not in ("", "None") else None
    except (TypeError, ValueError):
        return None


def fmt_man(v) -> str:
    """주 → 만주 문자열(부호 포함). 없으면 '—'."""
    if v is None:
        return "—"
    return f"{v / 10000:+.1f}만" if abs(v) >= 1000 else f"{v:+,.0f}주"


def fmt_num(v, nd: int = 0, plus: bool = False) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    return f"{v:+,.{nd}f}" if plus else f"{v:,.{nd}f}"


def market_phase(now: datetime) -> str:
    """리포트를 만든 시각의 장 상태 설명. 공휴일은 반영하지 못한다."""
    if now.weekday() >= 5:
        return "휴장일(주말) — 가장 최근 거래일 기준 데이터"
    t = now.hour * 60 + now.minute
    if t < 9 * 60:
        return "장 시작 전 — 전일 마감 기준 데이터 (공휴일이면 직전 거래일)"
    if t < 15 * 60 + 20:
        return "장중 — 가격은 실시간, 수급은 정해진 시각에만 갱신되는 잠정 값"
    if t < 15 * 60 + 30:
        return "종가 동시호가 — 가격·수급 모두 잠정"
    if t < 15 * 60 + 40:
        return "장 마감 직후 — 가격은 확정, 수급은 확정 전"
    return "장 마감 후 — 오늘 가격 확정 (일별 수급은 증권사 제공 시점에 따라 아직 어제까지일 수 있음)"


# ---------------------------------------------------------------- 지표 계산 (앱의 계산을 옮겨 옴)
def rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain, avg_loss = gain.rolling(period).mean(), loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def classify_trend(df: pd.DataFrame) -> str:
    """streamlit_app.py의 classify_trend_state와 같은 규칙. 상태 라벨만 반환."""
    if df is None or df.empty or len(df) < 60:
        return "데이터 부족"
    close = df["stck_clpr"]
    ma5, ma20, ma60 = close.rolling(5).mean(), close.rolling(20).mean(), close.rolling(60).mean()
    rsi = rsi_series(close)
    ma5_now, ma20_now, ma60_now = ma5.iloc[-1], ma20.iloc[-1], ma60.iloc[-1]
    ma5_prev = ma5.iloc[-6] if len(ma5) > 6 else ma5.iloc[0]
    rsi_now = rsi.iloc[-1] if pd.notna(rsi.iloc[-1]) else None
    rsi_prev = rsi.iloc[-6] if len(rsi) > 6 and pd.notna(rsi.iloc[-6]) else None
    price_up_5d = close.iloc[-1] > (close.iloc[-6] if len(close) > 6 else close.iloc[0])
    rsi_rising = rsi_now is not None and rsi_prev is not None and rsi_now > rsi_prev
    if ma5_now > ma20_now > ma60_now:
        return "상승추세 · 단기 과열" if (rsi_now is not None and rsi_now >= 70) else "상승추세 진행중"
    if ma5_now < ma20_now < ma60_now:
        if price_up_5d and ma5_now > ma5_prev:
            return "하락추세 속 기술적 반등"
        if rsi_now is not None and rsi_now <= 35 and rsi_rising:
            return "하락추세 · 반등 준비 구간"
        return "추세적 하락 지속"
    if ma5_now > ma20_now and ma20_now < ma60_now:
        return "하락추세 속 단기 반등 시도"
    if ma5_now < ma20_now and ma20_now > ma60_now:
        return "상승추세 속 단기 조정"
    return "방향성 탐색 구간"


def bollinger(df: pd.DataFrame, period: int = 20, num_std: float = 2.0):
    """(상단, 중심선, 하단). 데이터 부족하면 (None, None, None)."""
    if df is None or df.empty or len(df) < period:
        return None, None, None
    close = df["stck_clpr"]
    mid, std = close.rolling(period).mean().iloc[-1], close.rolling(period).std().iloc[-1]
    return mid + num_std * std, mid, mid - num_std * std


# ---------------------------------------------------------------- 수급 (주식현재가 투자자 / 외인기관 추정가집계)
def parse_investor_daily(output) -> list:
    """output 배열 → [{date, prsn, frgn, orgn}] 최신순 (날짜 없는 행 제외). 수량(주), 음수는 순매도."""
    if isinstance(output, dict):
        output = [output]
    rows = []
    for r in output or []:
        if not isinstance(r, dict):
            continue
        d = str(r.get("stck_bsop_date") or "").strip()
        if len(d) != 8:
            continue
        rows.append({"date": d, "prsn": _to_int(r.get("prsn_ntby_qty")), "frgn": _to_int(r.get("frgn_ntby_qty")),
                     "orgn": _to_int(r.get("orgn_ntby_qty"))})
    rows.sort(key=lambda x: x["date"], reverse=True)
    return rows


def parse_investor_estimate(output2) -> list:
    if isinstance(output2, dict):
        output2 = [output2]
    rows = []
    for r in output2 or []:
        if not isinstance(r, dict):
            continue
        gb = str(r.get("bsop_hour_gb") or "").strip()
        if gb in EST_SLOT_LABELS:
            rows.append({"time": EST_SLOT_LABELS[gb], "frgn": _to_int(r.get("frgn_fake_ntby_qty")),
                         "orgn": _to_int(r.get("orgn_fake_ntby_qty")), "slot": gb})
    rows.sort(key=lambda x: x["slot"])
    return rows


def _investor_daily_once(code: str) -> list:
    resp = requests.get(f"{BASE_URL}{INVESTOR_DAILY_PATH}", headers=kis_headers(INVESTOR_DAILY_TR_ID),
                        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1") or "수급 조회 실패")
    rows = parse_investor_daily(data.get("output"))
    if not rows:
        raise RuntimeError("수급 데이터가 비어 있음")
    return rows


def _investor_estimate_once(code: str) -> list:
    resp = requests.get(f"{BASE_URL}{INVESTOR_EST_PATH}", headers=kis_headers(INVESTOR_EST_TR_ID),
                        params={"MKSC_SHRN_ISCD": code}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1") or "장중 추정 수급 조회 실패")
    return parse_investor_estimate(data.get("output2"))


def fetch_investor_daily(code: str):
    return _retry(_investor_daily_once, code)


def fetch_investor_estimate(code: str):
    return _retry(_investor_estimate_once, code)


def streak(rows: list, key: str) -> int:
    """rows(최신순)에서 연속 순매수(+n)/순매도(-n) 일수. 값이 없거나 0이면 0."""
    n = 0
    for r in rows:
        v = r.get(key)
        if v is None or v == 0:
            break
        sign = 1 if v > 0 else -1
        if n == 0:
            n = sign
        elif (n > 0) == (sign > 0):
            n += sign
        else:
            break
    return n


def summarize_flow(daily, estimate, today: str) -> dict:
    """일별 수급(오늘 미확정 행 제외)과 오늘 장중 추정을 요약."""
    out = {"daily_asof": None, "streak": {}, "sum5": {}, "estimate": estimate, "daily_ok": daily is not None,
           "estimate_ok": estimate is not None}
    if daily:
        valid = [r for r in daily if not (r["date"] == today and all(r.get(k) in (None, 0) for k in ("prsn", "frgn", "orgn")))]
        if valid:
            out["daily_asof"] = f"{valid[0]['date'][:4]}-{valid[0]['date'][4:6]}-{valid[0]['date'][6:]}"
            for k, name in (("frgn", "외국인"), ("orgn", "기관"), ("prsn", "개인")):
                out["streak"][name] = streak(valid, k)
                vals = [r[k] for r in valid[:5] if r.get(k) is not None]
                out["sum5"][name] = sum(vals) if vals else None
    return out


# ---------------------------------------------------------------- 점수
def fin_is_empty(fin) -> bool:
    """KIS가 재무값을 0으로 채워 보낸 경우(예: HPSP)를 걸러낸다. ROE·부채비율·매출증가율이 전부 0이면 비어 있는 것으로 본다."""
    if not fin:
        return True
    keys = ("roe", "debt_ratio", "sales_growth")
    return all((fin.get(k) in (None, 0, 0.0)) for k in keys)


TREND_SCORE = {"상승추세 진행중": 20, "상승추세 · 단기 과열": 20, "상승추세 속 단기 조정": 14}


def swing_check_score(*, in_top10: bool, in_volume: bool, transition: bool, trend: str, day_pct, rsi, price, bb_upper, fin) -> dict:
    """스윙 체크 점수(임의 기준, 검증 안 됨). 재무 값이 비어 있으면 재무를 빼고 80점 만점으로 계산한다."""
    supply = (10 if in_top10 else 0) + (10 if in_volume else 0)
    signal = 20 if transition else 0
    trend_pts = TREND_SCORE.get(trend, 0)
    pen_day = 10 if (day_pct is not None and day_pct >= 10) else 6 if (day_pct is not None and day_pct >= 7) else 0
    pen_rsi = 10 if (rsi is not None and rsi >= 80) else 5 if (rsi is not None and rsi >= 70) else 0
    pen_bb = 5 if (price and bb_upper and price >= bb_upper * 0.985) else 0
    burden = max(0, 20 - pen_day - pen_rsi - pen_bb)
    parts = {"수급": supply, "신호": signal, "추세": trend_pts, "가격부담": burden}
    notes = []
    if fin_is_empty(fin):
        parts["재무"] = None
        notes.append("재무 데이터가 비어 있어 재무 항목을 점수에서 제외 (80점 만점)")
        max_score = 80
    else:
        f = 0
        f += 5 if (fin.get("sales_growth") or 0) > 0 else 0
        f += 5 if (fin.get("op_growth") or 0) > 0 else 0       # 0(적자 관련 가능)이나 None은 점수 없음
        f += 5 if (fin.get("roe") or -1) >= 10 else 0
        f += 5 if (fin.get("debt_ratio") is not None and fin["debt_ratio"] <= 150) else 0
        parts["재무"] = f
        if fin.get("op_growth") == 0:
            notes.append("영업이익 증가율 0: 적자 관련 상태일 수 있어 점수 제외")
        max_score = 100
    total = sum(v for v in parts.values() if v is not None)
    return {"total": total, "max": max_score, "parts": parts, "penalties": {"등락": pen_day, "RSI": pen_rsi, "볼린저": pen_bb},
            "notes": notes}


# ---------------------------------------------------------------- 대상 종목 고르기
def load_watch_codes(path: str = WATCH_PATH) -> list:
    """한 줄에 '종목코드 [메모]'. # 이후는 주석. 6자리 숫자가 아닌 줄은 무시."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            m = re.match(r"^(\d{6})\b\s*(.*)$", line)
            if m:
                out.append({"code": m.group(1), "name": m.group(2).strip()})
    return out


def recent_signal_stocks(tracker: dict, n_dates: int = RECENT_SIGNAL_DATES) -> list:
    sigs = (tracker or {}).get("signals") or []
    dates = sorted({s.get("signal_date") for s in sigs if s.get("signal_date")}, reverse=True)[:n_dates]
    out = []
    for s in sorted(sigs, key=lambda x: (x.get("signal_date", ""), x.get("code", "")), reverse=True):
        if s.get("signal_date") in dates:
            out.append({"code": s["code"], "name": s.get("name", ""), "signal_date": s["signal_date"],
                        "type": {"transition": "전환신호", "volume_supply": "거래량·수급 동시"}.get(s.get("type"), s.get("type"))})
    return out


def select_universe(top10: list, volume_rows: list, signals: list, watch: list, max_n: int = MAX_STOCKS) -> list:
    """순매수 상위 → 최근 신호 종목 → 관심 종목 순으로, 중복 없이 최대 max_n개. 각 항목에 출처(sources)를 붙인다."""
    vol_rank = {v["stock_code"]: v["rank"] for v in volume_rows or []}
    buy_rank = {r["stock_code"]: r for r in top10 or []}
    order, seen = [], {}

    def add(code, name, source):
        if code in seen:
            seen[code]["sources"].append(source)
            return
        if len(order) >= max_n:
            return
        item = {"code": code, "name": name, "sources": [source]}
        seen[code] = item
        order.append(item)

    for r in top10 or []:
        add(r["stock_code"], r["stock_name"], f"순매수 상위 {r['rank']}위")
    for s in signals or []:
        add(s["code"], s["name"], f"신호 추적({s['signal_date']} {s['type']})")
    for w in watch or []:
        add(w["code"], w["name"], "관심 종목 파일")
    for item in order:
        item["buy_rank"] = buy_rank.get(item["code"], {}).get("rank")
        item["foreign_net"] = buy_rank.get(item["code"], {}).get("foreign_net")
        item["inst_net"] = buy_rank.get(item["code"], {}).get("inst_net")
        item["volume_rank"] = vol_rank.get(item["code"])
        if item["volume_rank"]:
            item["sources"].append(f"거래량 상위 {item['volume_rank']}위")
    return order


# ---------------------------------------------------------------- 종목 하나 수집
def collect_stock(item: dict, now: datetime) -> dict:
    code, name = item["code"], item["name"]
    issues = []
    detail = fetch_price_detail(code)
    if detail is None:
        issues.append("현재가 조회 실패")
        detail = {}
    price = _to_float(detail.get("stck_prpr"))
    day_pct = _to_float(detail.get("prdy_ctrt"))
    name = name or detail.get("hts_kor_isnm") or code
    flags = market_risk_flags(str(detail.get("iscd_stat_cls_code") or ""), str(detail.get("mrkt_warn_cls_code") or ""),
                              str(detail.get("temp_stop_yn") or "") == "Y", _to_float(detail.get("hts_avls"))) if detail else []
    fin = strip_raw(fetch_financial_ratio(code))
    if fin is None:
        issues.append("재무비율 조회 실패")
    elif fin_is_empty(fin):
        issues.append("재무비율이 비어 있음(KIS가 0을 돌려줌) — 재무 판단 불가")
    df = fetch_daily_ohlcv(code)
    if df.empty:
        issues.append("일봉 조회 실패")
    tech = analyze_technicals(df)
    transition = bool(compute_transition_signal(df)) and not flags      # 거래소 위험 상태 종목은 신호로 인정하지 않는다(EOD와 동일)
    trend = classify_trend(df)
    up, mid, low = bollinger(df)
    daily, est = fetch_investor_daily(code), fetch_investor_estimate(code)
    if daily is None:
        issues.append("일별 수급 조회 실패")
    if est is None:
        issues.append("장중 추정 수급 조회 실패")
    flow = summarize_flow(daily, est, now.strftime("%Y%m%d"))
    try:
        disclosures = check_disclosure_risk(code) or []
    except Exception:
        disclosures = []
        issues.append("공시 조회 실패")
    per, pbr = _to_float(detail.get("per")), _to_float(detail.get("pbr"))
    app_score = composite_score(per, pbr, fin) if not fin_is_empty(fin) else {"score": None, "parts": {}, "coverage": 0, "notes": []}
    rsi = tech.get("RSI값")
    swing = swing_check_score(in_top10=item.get("buy_rank") is not None, in_volume=item.get("volume_rank") is not None,
                              transition=transition, trend=trend, day_pct=day_pct, rsi=rsi, price=price, bb_upper=up, fin=fin)
    return {
        "code": code, "name": name, "sources": item.get("sources", []),
        "ranking": {"buy_rank": item.get("buy_rank"), "foreign_net": item.get("foreign_net"), "inst_net": item.get("inst_net"),
                    "volume_rank": item.get("volume_rank")},
        "price": {"current": price, "change_pct": day_pct, "per": per, "pbr": pbr, "eps": _to_float(detail.get("eps")),
                  "bps": _to_float(detail.get("bps")), "mktcap_eok": _to_float(detail.get("hts_avls")),
                  "w52_high": _to_float(detail.get("w52_hgpr")), "w52_low": _to_float(detail.get("w52_lwpr"))},
        "risk_flags": flags,
        "technical": {"transition": transition, "trend": trend, "rsi": rsi, "aligned": tech.get("정배열"),
                      "volume_surge": tech.get("거래량급증"), "momentum_20d": tech.get("20일모멘텀"), "vcp": tech.get("변동성수축(VCP)"),
                      "bb_upper": up, "bb_mid": mid, "bb_lower": low,
                      "to_upper_pct": ((up / price - 1) * 100) if (up and price) else None,
                      "vs_mid_pct": ((price / mid - 1) * 100) if (mid and price) else None},
        "financial": fin, "flow": flow, "disclosures": disclosures,
        "scores": {"app_composite": app_score.get("score"), "app_notes": app_score.get("notes", []), "swing_check": swing},
        "data_issues": issues,
    }


# ---------------------------------------------------------------- 리포트 만들기
def _flow_text(s: dict) -> str:
    fl = s["flow"]
    if not fl.get("daily_ok"):
        return "일별 수급 조회 실패"
    if not fl.get("daily_asof"):
        return "일별 수급 데이터 없음"
    parts = []
    for name in ("외국인", "기관", "개인"):
        n = fl["streak"].get(name, 0)
        txt = "—" if not n else f"{abs(n)}일 연속 {'순매수' if n > 0 else '순매도'}"
        parts.append(f"{name} {txt}(5일 누적 {fmt_man(fl['sum5'].get(name))})")
    return " / ".join(parts) + f" [일별 기준일 {fl['daily_asof']}]"


def _est_text(s: dict) -> str:
    est = s["flow"].get("estimate")
    if est is None:
        return "장중 추정 조회 실패"
    if not est:
        return "아직 입력 없음"
    return " → ".join(f"{e['time']} 외국인 {fmt_man(e['frgn'])}·기관 {fmt_man(e['orgn'])}" for e in est)


def _score_text(sc: dict) -> str:
    p = sc["parts"]
    seg = " / ".join(f"{k} {v if v is not None else 'N/A'}" for k, v in p.items())
    return f"{sc['total']}/{sc['max']} ({seg})"


def _table_row(s: dict) -> str:
    pr, tc, fin = s["price"], s["technical"], s["financial"]
    flow = s["flow"]["streak"]
    fl = lambda n: "—" if not n else f"{'+' if n > 0 else '-'}{abs(n)}"
    rk = s["ranking"]
    cells = [
        s["name"], s["code"], fmt_num(pr["current"]), fmt_num(pr["change_pct"], 2, True) + "%" if pr["change_pct"] is not None else "—",
        f"{rk['buy_rank']}위" if rk["buy_rank"] else "—",
        f"{fmt_man(rk['foreign_net'])}/{fmt_man(rk['inst_net'])}" if rk["buy_rank"] else "—",
        f"{rk['volume_rank']}위" if rk["volume_rank"] else "—",
        "O" if tc["transition"] else "X",
        fmt_num(tc["rsi"], 1), fmt_num(tc["to_upper_pct"], 1, True) + "%" if tc["to_upper_pct"] is not None else "—",
        tc["trend"],
        f"{fl(flow.get('외국인'))}/{fl(flow.get('기관'))}/{fl(flow.get('개인'))}",
        (f"{fmt_num(fin.get('roe'), 1)}/{fmt_num(fin.get('debt_ratio'), 0)}/{fmt_num(fin.get('sales_growth'), 1, True)}/{fmt_num(fin.get('op_growth'), 1, True)}"
         if fin and not fin_is_empty(fin) else "없음"),
        fmt_num(s["scores"]["app_composite"]) if s["scores"]["app_composite"] is not None else "N/A",
        f"{s['scores']['swing_check']['total']}/{s['scores']['swing_check']['max']}",
    ]
    return "| " + " | ".join(str(c) for c in cells) + " |"


TABLE_HEADER = ("| 종목 | 코드 | 현재가 | 등락 | 순매수 순위 | 외국인/기관 순매수 | 거래량 순위 | 전환신호 | RSI | 볼린저 상단까지 | 추세 | "
                "연속(외/기/개) | 재무(ROE/부채/매출/영업이익 증가율) | 앱 종합 | 스윙 체크 |\n"
                "|" + "---|" * 15)


def _detail_block(s: dict) -> str:
    pr, tc, fin = s["price"], s["technical"], s["financial"]
    sc = s["scores"]["swing_check"]
    lines = [f"### {s['name']} ({s['code']}) — {', '.join(s['sources'])}"]
    lines.append(f"- 가격: 현재가 {fmt_num(pr['current'])}원 ({fmt_num(pr['change_pct'], 2, True)}%), PER {fmt_num(pr['per'], 1)} / PBR {fmt_num(pr['pbr'], 2)} "
                 f"(EPS {fmt_num(pr['eps'])}원, BPS {fmt_num(pr['bps'])}원), 시총 {fmt_num(pr['mktcap_eok'])}억원, "
                 f"52주 {fmt_num(pr['w52_low'])}~{fmt_num(pr['w52_high'])}원")
    lines.append(f"- 기술: 추세 '{tc['trend']}', 전환신호 {'O' if tc['transition'] else 'X'}, 정배열 {tc['aligned']}, 거래량급증 {tc['volume_surge']}, "
                 f"20일모멘텀 {tc['momentum_20d']}, RSI {fmt_num(tc['rsi'], 1)}, 볼린저 하/중/상 {fmt_num(tc['bb_lower'])}/{fmt_num(tc['bb_mid'])}/{fmt_num(tc['bb_upper'])} "
                 f"(중심선 대비 {fmt_num(tc['vs_mid_pct'], 1, True)}%)")
    lines.append(f"- 수급: {_flow_text(s)}")
    lines.append(f"- 오늘 장중 추정: {_est_text(s)}")
    if fin and not fin_is_empty(fin):
        lines.append(f"- 재무(기준 {fin.get('stac_yymm')}): ROE {fmt_num(fin.get('roe'), 1)}%, 부채비율 {fmt_num(fin.get('debt_ratio'), 0)}%, "
                     f"매출 증가율 {fmt_num(fin.get('sales_growth'), 1, True)}%, 영업이익 증가율 {fmt_num(fin.get('op_growth'), 1, True)}%, "
                     f"순이익 증가율 {fmt_num(fin.get('ni_growth'), 1, True)}%")
    else:
        lines.append("- 재무: 데이터 없음")
    lines.append(f"- 점수: 스윙 체크 {_score_text(sc)}; 앱 종합 {s['scores']['app_composite'] if s['scores']['app_composite'] is not None else 'N/A'}")
    extra = list(sc["notes"]) + list(s["scores"]["app_notes"]) + list(s["data_issues"])
    if s["risk_flags"]:
        extra.append("거래소 위험 표시: " + ", ".join(s["risk_flags"]))
    if s["disclosures"]:
        extra.append("주의 공시: " + "; ".join(s["disclosures"]))
    if extra:
        lines.append("- 유의: " + " · ".join(extra))
    return "\n".join(lines)


def _split_stocks(stocks: list) -> tuple:
    top = [s for s in stocks if s["ranking"]["buy_rank"]]
    top.sort(key=lambda s: s["ranking"]["buy_rank"])
    return top, [s for s in stocks if not s["ranking"]["buy_rank"]]


def _header_and_tables(stocks: list, meta: dict, title: str) -> list:
    top, others = _split_stocks(stocks)
    md = [title, "",
          f"- 생성 시각: {meta['generated_at']} (KST)",
          f"- 데이터 기준: {meta['phase']}",
          f"- 포함 종목: {len(stocks)}개 (순매수 상위 {len(top)}개 + 신호 추적·관심 종목 {len(others)}개)",
          "- 출처: 한국투자증권 Open API(시세·수급·재무), 대시보드 자체 계산(신호·점수). 뉴스·사업 내용은 포함하지 않았습니다.",
          "- 이 리포트는 투자 자문이 아니며 매매일지 등 개인 정보와 API 키는 포함하지 않습니다.", ""]
    if meta.get("notes"):
        md += [f"- 유의: {n}" for n in meta["notes"]] + [""]
    md += [PROMPT, FIELD_GUIDE, "## 1. 오늘 순매수 상위 종목", TABLE_HEADER]
    md += [_table_row(s) for s in top] or ["| (순매수 상위 데이터 없음 — 장 시작 전이거나 조회 실패) |"]
    md += ["", "## 2. 최근 신호 · 관심 종목", TABLE_HEADER]
    md += [_table_row(s) for s in others] or ["| (해당 종목 없음) |"]
    return md


LIMITS = ["## 4. 데이터 한계", "- 수급 가집계는 하루 5번만 갱신되고, 일별 확정 수급은 보통 어제까지입니다.",
          "- 점수는 임의 기준이며 검증되지 않았습니다. 신호 추적 표본이 아직 적어 신호의 성과도 입증되지 않았습니다.",
          "- 같은 날 시장 전체가 오른 경우 개별 종목 신호와 구분하기 어렵습니다.", ""]


def build_report(stocks: list, meta: dict) -> tuple:
    """(전체 markdown 문자열, json 가능한 dict)."""
    top, others = _split_stocks(stocks)
    md = _header_and_tables(stocks, meta, "# 스윙 후보 AI 분석용 리포트")
    md += ["", "## 3. 종목별 상세", ""]
    md += [_detail_block(s) + "\n" for s in top + others]
    md += LIMITS
    return "\n".join(md), {"meta": meta, "stocks": stocks}


def build_report_short(stocks: list, meta: dict) -> str:
    """지시문 + 표만 있는 짧은 버전 (종목별 상세 생략)."""
    md = _header_and_tables(stocks, meta, "# 스윙 후보 AI 분석용 리포트 (짧은 버전)")
    md += ["", "※ 종목별 상세(재무 기준월, 볼린저 가격대, 장중 추정 수급 등)는 전체 리포트에 있습니다. 더 자세한 분석이 필요하면 알려 주세요.", ""]
    md += LIMITS
    return "\n".join(md)


# ---------------------------------------------------------------- 실행
def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_atomic(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def run(now: datetime | None = None, md_path: str = REPORT_MD_PATH, json_path: str = REPORT_JSON_PATH,
        watch_path: str = WATCH_PATH, tracker_path: str = TRACKER_PATH, max_n: int = MAX_STOCKS, short_path: str | None = None) -> dict:
    now = now or datetime.now(KST)
    short_path = short_path or (REPORT_SHORT_PATH if md_path == REPORT_MD_PATH else md_path[:-3] + "_short.md")
    notes = []
    try:
        top10 = fetch_investor_ranking("buy") or []
    except Exception as e:
        top10 = []
        notes.append(f"순매수 상위 조회 실패({str(e)[:40]}) — 신호 추적·관심 종목만 포함")
    try:
        volume_rows = fetch_volume_ranking() or []
    except Exception:
        volume_rows = []
        notes.append("거래량 순위 조회 실패 — 거래량 상위 항목은 비어 있음")
    if not top10 and not notes:
        notes.append("순매수 상위가 비어 있음(장 시작 전·휴장일이거나 조회 실패)")
    universe = select_universe(top10, volume_rows, recent_signal_stocks(_load_json(tracker_path)), load_watch_codes(watch_path), max_n)
    stocks = []
    for item in universe:
        try:
            stocks.append(collect_stock(item, now))
        except Exception as e:
            notes.append(f"{item['name'] or item['code']} 수집 실패({str(e)[:40]})")
    ok = [s for s in stocks if "현재가 조회 실패" not in s["data_issues"]]
    meta = {"generated_at": now.strftime("%Y-%m-%d %H:%M"), "phase": market_phase(now), "notes": notes,
            "counts": {"universe": len(universe), "ok": len(ok)}}
    if len(ok) < MIN_OK_STOCKS:
        print(f"조회 성공 종목이 {len(ok)}개뿐이라 직전 리포트를 유지합니다 (대상 {len(universe)}개)")
        return {"written": False, "meta": meta}
    md, payload = build_report(stocks, meta)
    short = build_report_short(stocks, meta)
    write_atomic(md_path, md)
    write_atomic(short_path, short)
    write_atomic(json_path, json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"AI 리포트 저장: {md_path} ({len(stocks)}종목, 전체 {len(md):,}자 / 짧은 버전 {len(short):,}자) / {meta['phase']}")
    return {"written": True, "meta": meta, "chars": len(md), "short_chars": len(short)}


if __name__ == "__main__":
    run()
