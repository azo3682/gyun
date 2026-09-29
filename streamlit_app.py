# -*- coding: utf-8 -*-
"""
streamlit_app.py (v2)

기존 순매수/순매도 상위 10 랭킹에 더해, 각 종목에 대해
- 기술적 스크리닝 (정배열/거래량급증/모멘텀/RSI/VCP)
- DART 공시 리스크 체크
- 관련 뉴스 헤드라인
을 보여주는 스윙 후보 스크리닝 도구.

주의: 이건 "사라/팔아라" 결론을 내려주는 게 아니라, 여러 조건의
통과 여부를 투명하게 보여주는 체크리스트입니다. 최종 판단은
직접 하셔야 합니다. 투자 자문이 아닙니다.

필요한 Secrets (Streamlit Cloud > Advanced settings > Secrets):
    KIS_APP_KEY = "..."
    KIS_APP_SECRET = "..."
    DART_API_KEY = "..."
    NAVER_CLIENT_ID = "..."
    NAVER_CLIENT_SECRET = "..."
"""

import io
import json
import os
import re
import sqlite3
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

# ============================================================
# CONFIG
# ============================================================

KST = ZoneInfo("Asia/Seoul")
IS_REAL_ACCOUNT = True

REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
BASE_URL = REAL_BASE_URL if IS_REAL_ACCOUNT else PAPER_BASE_URL

APP_KEY = st.secrets.get("KIS_APP_KEY", "")
APP_SECRET = st.secrets.get("KIS_APP_SECRET", "")
DART_API_KEY = st.secrets.get("DART_API_KEY", "")
NAVER_CLIENT_ID = st.secrets.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = st.secrets.get("NAVER_CLIENT_SECRET", "")

RANKING_API_PATH = "/uapi/domestic-stock/v1/quotations/foreign-institution-total"
RANKING_TR_ID = "FHPTJ04400000"

VOLUME_RANK_API_PATH = "/uapi/domestic-stock/v1/quotations/volume-rank"
VOLUME_RANK_TR_ID = "FHPST01710000"


DAILY_CHART_API_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
DAILY_CHART_TR_ID = "FHKST03010100"

CURRENT_PRICE_API_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
CURRENT_PRICE_TR_ID = "FHKST01010100"

TOP_N = 10
DB_PATH = "investor_flow.db"

RISK_KEYWORDS = ["유상증자", "무상감자", "감자", "관리종목", "상장폐지",
                  "횡령", "배임", "불성실공시", "자본잠식", "거래정지"]


# ============================================================
# 인증
# ============================================================

@st.cache_resource
def _token_holder():
    return {"token": None, "expires_at": 0}


def get_access_token() -> str:
    holder = _token_holder()
    if holder["token"] and holder["expires_at"] > time.time() + 300:
        return holder["token"]

    resp = requests.post(
        f"{BASE_URL}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    holder["token"] = data["access_token"]
    holder["expires_at"] = time.time() + int(data.get("expires_in", 86400))
    return holder["token"]


def kis_headers(tr_id: str) -> dict:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {get_access_token()}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }


ETF_NAME_KEYWORDS = [
    "KODEX", "TIGER", "ACE", "KINDEX", "RISE", "KBSTAR", "SOL", "ARIRANG",
    "HANARO", "KOSEF", "TIMEFOLIO", "PLUS", "마이다스", "히어로즈", "WOORI",
    "레버리지", "인버스", "ETN", "스팩",
]


def is_fund_product(name: str) -> bool:
    """ETF/ETN/레버리지/인버스 상품인지 이름으로 판별 (종목코드로는 구분 불가)."""
    name_upper = name.upper()
    return any(kw.upper() in name_upper for kw in ETF_NAME_KEYWORDS)


# ============================================================
# 순매수/순매도 랭킹 (기존 기능)
# ============================================================

@st.cache_data(ttl=300)
def fetch_investor_ranking(rank_type: str) -> list[dict]:
    url = f"{BASE_URL}{RANKING_API_PATH}"
    params = {
        "FID_COND_MRKT_DIV_CODE": "V",
        "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",
        "FID_RANK_SORT_CLS_CODE": "0" if rank_type == "buy" else "1",
        "FID_ETC_CLS_CODE": "0",
    }
    resp = requests.get(url, headers=kis_headers(RANKING_TR_ID), params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"KIS API 오류: {data.get('msg1')}")

    now = datetime.now(KST).isoformat(timespec="seconds")
    rows = []
    for item in data.get("output", []):
        name = item.get("hts_kor_isnm", "")
        if is_fund_product(name):
            continue
        rows.append({
            "ts": now, "rank_type": rank_type, "rank": len(rows) + 1,
            "stock_code": item.get("mksc_shrn_iscd", ""),
            "stock_name": name,
            "foreign_net": float(item.get("frgn_ntby_qty", 0) or 0),
            "inst_net": float(item.get("orgn_ntby_qty", 0) or 0),
            "combined_net": float(item.get("ntby_qty", 0) or 0),
        })
        if len(rows) >= TOP_N:
            break
    return rows


@st.cache_data(ttl=60)
def fetch_volume_rank() -> tuple[list[dict], dict]:
    """(순위 리스트, 원본 응답 첫 항목) 반환. 필드명 검증용으로 원본도 같이 반환."""
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",
        "FID_BLNG_CLS_CODE": "0",
        "FID_TRGT_CLS_CODE": "111111111",
        "FID_TRGT_EXLS_CLS_CODE": "0000000000",
        "FID_INPUT_PRICE_1": "0",
        "FID_INPUT_PRICE_2": "0",
        "FID_VOL_CNT": "0",
        "FID_INPUT_DATE_1": "",
    }
    resp = requests.get(f"{BASE_URL}{VOLUME_RANK_API_PATH}", headers=kis_headers(VOLUME_RANK_TR_ID),
                         params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"KIS API 오류: {data.get('msg1')}")

    output = data.get("output", [])
    raw_sample = output[0] if output else {}
    rows = []
    for item in output:
        name = item.get("hts_kor_isnm", "")
        if is_fund_product(name):
            continue
        rows.append({
            "rank": len(rows) + 1,
            "stock_code": item.get("mksc_shrn_iscd", ""),
            "stock_name": name,
            "volume": item.get("acml_vol", ""),
            "day_pct": float(item.get("prdy_ctrt", 0) or 0),
        })
        if len(rows) >= TOP_N:
            break
    return rows, raw_sample


# ============================================================
# 일봉 데이터 + 기술적 분석
# ============================================================

@st.cache_data(ttl=1800)
def fetch_daily_ohlcv(stock_code: str) -> pd.DataFrame:
    """최근 약 4개월 일봉 데이터. 실패하면 빈 DataFrame.

    [확인 필요] output2 필드 구조는 여러 공개 예제에서 일관되게
    확인했지만(stck_bsop_date/stck_clpr/stck_oprc/stck_hgpr/stck_lwpr/acml_vol),
    KIS 계정으로 직접 테스트하진 못했습니다. 화면에 빈 값만 나오면
    알려주세요 — 실제 응답 구조를 같이 확인하겠습니다.
    """
    end = datetime.now(KST).strftime("%Y%m%d")
    start = (datetime.now(KST) - timedelta(days=130)).strftime("%Y%m%d")
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd": stock_code,
        "fid_input_date_1": start,
        "fid_input_date_2": end,
        "fid_period_div_code": "D",
        "fid_org_adj_prc": "0",
    }
    time.sleep(0.15)  # 초당 호출 제한 방지용 간격
    try:
        resp = requests.get(
            f"{BASE_URL}{DAILY_CHART_API_PATH}",
            headers=kis_headers(DAILY_CHART_TR_ID),
            params=params, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("output2", [])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df = df[df["stck_bsop_date"] != ""]
        for col in ["stck_clpr", "stck_oprc", "stck_hgpr", "stck_lwpr", "acml_vol"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("stck_bsop_date").reset_index(drop=True)
        return df
    except Exception:
        return pd.DataFrame()


# 거래소가 실시간으로 매기는 종목상태코드 — DART 공시보다 먼저, 더 확실하게 위험을 알려준다
# (예: '관리종목 지정 우려' 같은 거래소 시장조치 안내는 DART 기업공시로 안 올라오는 경우가 있다)
MARKET_STAT_LABELS = {"51": "🚨 관리종목", "52": "🚨 투자위험", "53": "⚠️ 투자경고",
                       "54": "⚠️ 투자주의", "58": "🚨 거래정지", "59": "⚠️ 정리매매"}
MARKET_WARN_LABELS = {"01": "⚠️ 투자주의", "02": "⚠️ 투자경고", "03": "🚨 투자위험"}
# 관리종목 지정 요건(시가총액 200억 미만)은 '우려 안내' 단계에서는 종목상태코드에 아직 안 잡힌다.
# (거래소가 확정 지정하기 전까지는 상태코드가 정상으로 남아있음 — 2026-09-29 엔젠바이오 사례로 확인)
# 그래서 시가총액 자체도 별도로 확인한다.
MKTCAP_RISK_THRESHOLD_EOK = 300  # 관리종목 기준(200억)보다 여유를 둔 경고선


def market_risk_flags(stat_code: str, warn_code: str, halted: bool, mktcap_eok: float | None = None) -> list[str]:
    flags = []
    if halted:
        flags.append("🚨 거래정지")
    if stat_code in MARKET_STAT_LABELS and MARKET_STAT_LABELS[stat_code] not in flags:
        flags.append(MARKET_STAT_LABELS[stat_code])
    if warn_code in MARKET_WARN_LABELS and MARKET_WARN_LABELS[warn_code] not in flags:
        flags.append(MARKET_WARN_LABELS[warn_code])
    if mktcap_eok is not None and mktcap_eok < MKTCAP_RISK_THRESHOLD_EOK:
        flags.append(f"⚠️ 시가총액 {mktcap_eok:,.0f}억(관리종목 요건 200억에 근접/미달)")
    return flags


@st.cache_data(ttl=30)
def fetch_current_price(stock_code: str):
    """(현재가, 전일대비 등락률%, 거래소 위험 플래그 리스트) 반환. 실패하면 (None, None, [])."""
    try:
        resp = requests.get(
            f"{BASE_URL}{CURRENT_PRICE_API_PATH}",
            headers=kis_headers(CURRENT_PRICE_TR_ID),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            return None, None, []
        output = data.get("output", {})
        price = float(output.get("stck_prpr", 0) or 0)
        pct = float(output.get("prdy_ctrt", 0) or 0)
        mktcap_eok = None
        try:
            if output.get("hts_avls") not in (None, ""):
                mktcap_eok = float(output["hts_avls"])
        except (TypeError, ValueError):
            pass
        flags = market_risk_flags(
            str(output.get("iscd_stat_cls_code") or ""),
            str(output.get("mrkt_warn_cls_code") or ""),
            str(output.get("temp_stop_yn") or "") == "Y",
            mktcap_eok,
        )
        return price, pct, flags
    except Exception:
        return None, None, []


@st.cache_data(ttl=30)
def fetch_valuation(stock_code: str):
    """(PER, PBR, EPS, BPS, 원본응답) 반환. 실패하면 전부 None / {}.
    같은 현재가 조회 API 안에 들어있는 값이라 별도 엔드포인트 승인 없이 바로 씀.
    필드명이 실제와 다를 수 있어 원본 응답도 같이 반환 — 화면에서 검증용으로 보여줌."""
    try:
        resp = requests.get(
            f"{BASE_URL}{CURRENT_PRICE_API_PATH}",
            headers=kis_headers(CURRENT_PRICE_TR_ID),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            return None, None, None, None, {}
        output = data.get("output", {})

        def to_float(key):
            v = output.get(key)
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        per = to_float("per")
        pbr = to_float("pbr")
        eps = to_float("eps")
        bps = to_float("bps")
        return per, pbr, eps, bps, output
    except Exception:
        return None, None, None, None, {}


@st.cache_data(ttl=6 * 3600)
def fetch_financial_ratio(stock_code: str, period: str = "0"):
    """KIS 국내주식 재무비율(v1_국내주식-080, 실전 전용) — 최근 결산 기준 정식 재무비율 dict, 실패하면 None.
    ROE(roe_val)·부채비율(lblt_rate)·매출/영업이익/순이익 증가율. 결산 데이터라 6시간 캐시.
    output은 결산년월별 배열이라 이번 달 이하 중 가장 최근 결산을 고른다. 'raw'는 필드명 검증용.
    영업이익 증가율(bsop_prfi_inrt)은 적자지속/흑자전환/적자전환이면 0으로 오므로 0을 '성장 없음'으로 보면 안 된다."""
    try:
        resp = requests.get(
            f"{BASE_URL}/uapi/domestic-stock/v1/finance/financial-ratio",
            headers=kis_headers("FHKST66430300"),
            params={"FID_DIV_CLS_CODE": period, "fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            return None
        rows = data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]
        rows = [r for r in rows if isinstance(r, dict) and r.get("stac_yymm")]
        if not rows:
            return None
        cutoff = datetime.now(KST).strftime("%Y%m")
        latest = max([r for r in rows if r["stac_yymm"] <= cutoff] or rows, key=lambda r: r["stac_yymm"])

        def f(key):
            v = latest.get(key)
            try:
                return float(v) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        return {"stac_yymm": latest.get("stac_yymm"), "roe": f("roe_val"), "debt_ratio": f("lblt_rate"),
                "sales_growth": f("grs"), "op_growth": f("bsop_prfi_inrt"), "ni_growth": f("ntin_inrt"),
                "eps": f("eps"), "bps": f("bps"), "rsrv_rate": f("rsrv_rate"), "raw": latest}
    except Exception:
        return None


def compute_rsi(closes: pd.Series, period: int = 14):
    series = compute_rsi_series(closes, period)
    return float(series.iloc[-1]) if not series.empty and pd.notna(series.iloc[-1]) else None


def compute_rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def classify_trend_state(df: pd.DataFrame):
    """(상태 라벨, 설명, streamlit 표시함수) 반환."""
    if df.empty or len(df) < 60:
        return "데이터 부족", "일봉 데이터가 충분하지 않아 추세를 판단할 수 없습니다.", st.caption

    close = df["stck_clpr"]
    ma5, ma20, ma60 = close.rolling(5).mean(), close.rolling(20).mean(), close.rolling(60).mean()
    rsi_series = compute_rsi_series(close)

    ma5_now, ma20_now, ma60_now = ma5.iloc[-1], ma20.iloc[-1], ma60.iloc[-1]
    ma5_prev = ma5.iloc[-6] if len(ma5) > 6 else ma5.iloc[0]
    rsi_now = rsi_series.iloc[-1] if pd.notna(rsi_series.iloc[-1]) else None
    rsi_prev = rsi_series.iloc[-6] if len(rsi_series) > 6 and pd.notna(rsi_series.iloc[-6]) else None
    close_now, close_5ago = close.iloc[-1], close.iloc[-6] if len(close) > 6 else close.iloc[0]

    ma5_rising = ma5_now > ma5_prev
    rsi_rising = (rsi_now is not None and rsi_prev is not None and rsi_now > rsi_prev)
    price_up_5d = close_now > close_5ago
    uptrend_long = ma20_now > ma60_now
    downtrend_long = ma20_now < ma60_now

    if ma5_now > ma20_now > ma60_now:
        if rsi_now is not None and rsi_now >= 70:
            return ("상승추세 · 단기 과열",
                    "정배열 상태지만 RSI가 과열 구간에 들어와, 추가 상승보다 단기 조정 가능성도 염두에 둬야 하는 구간입니다.",
                    st.warning)
        return ("상승추세 진행중",
                "단기·중기·장기 이동평균선이 순서대로 위에 있는 정배열 상태로, 추세가 살아있는 구간입니다.",
                st.success)

    if ma5_now < ma20_now < ma60_now:
        if price_up_5d and ma5_rising:
            return ("하락추세 속 기술적 반등",
                    "중장기 추세는 아직 하락이지만, 최근 며칠 단기적으로 반등이 나오고 있는 구간입니다. 추세 전환인지 일시적 되돌림인지는 며칠 더 지켜봐야 합니다.",
                    st.info)
        if rsi_now is not None and rsi_now <= 35 and rsi_rising:
            return ("하락추세 · 반등 준비 구간",
                    "과매도 영역에서 RSI가 바닥을 다지는 움직임이 보이지만, 아직 가격이나 이동평균선상 뚜렷한 반등 신호는 아닙니다.",
                    st.info)
        return ("추세적 하락 지속",
                "단기·중기·장기 이동평균선이 모두 역순으로 배열된 뚜렷한 하락추세이며, 반등 조짐도 약한 상태입니다.",
                st.error)

    if ma5_now > ma20_now and downtrend_long:
        return ("하락추세 속 단기 반등 시도",
                "중장기 추세는 아직 하락(20일선<60일선)이지만, 단기 이동평균선이 중기선 위로 올라서며 반등을 시도하는 모습입니다.",
                st.info)
    if ma5_now < ma20_now and uptrend_long:
        return ("상승추세 속 단기 조정",
                "중장기 추세는 상승(20일선>60일선)이지만, 단기적으로 눌림/조정을 받는 구간입니다.",
                st.warning)

    return ("방향성 탐색 구간",
            "이동평균선들이 뚜렷한 배열 없이 얽혀 있어, 추세 전환기이거나 단순 횡보 구간일 가능성이 있습니다.",
            st.info)


def compute_bollinger(df: pd.DataFrame, period: int = 20, num_std: float = 2.0):
    """(상단, 중심선, 하단, 현재가 위치 설명) 반환. 데이터 부족 시 전부 None."""
    if df.empty or len(df) < period:
        return None, None, None, None
    close = df["stck_clpr"]
    mid = close.rolling(period).mean().iloc[-1]
    std = close.rolling(period).std().iloc[-1]
    upper = mid + num_std * std
    lower = mid - num_std * std
    price = close.iloc[-1]

    if upper == lower:
        position = "데이터 부족"
    elif price >= upper:
        position = "상단 돌파/근접 — 단기 과매수 구간일 수 있음"
    elif price <= lower:
        position = "하단 돌파/근접 — 단기 과매도 구간일 수 있음"
    elif price >= mid:
        position = "중심선~상단 사이 (중심선 위)"
    else:
        position = "중심선~하단 사이 (중심선 아래)"
    return upper, mid, lower, position


def analyze_technicals(df: pd.DataFrame) -> dict:
    """체크리스트 결과를 딕셔너리로 반환. 데이터 부족하면 각 항목 None."""
    result = {"정배열": None, "거래량급증": None, "20일모멘텀": None,
               "RSI과열아님": None, "변동성수축(VCP)": None, "RSI값": None}
    if df.empty or len(df) < 60:
        return result

    close = df["stck_clpr"]
    vol = df["acml_vol"]

    ma5 = close.rolling(5).mean().iloc[-1]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    result["정배열"] = bool(ma5 > ma20 > ma60)

    vol_recent = vol.iloc[-1]
    vol_avg20 = vol.rolling(20).mean().iloc[-2]
    result["거래량급증"] = bool(vol_avg20 and vol_recent >= vol_avg20 * 1.5)

    if len(close) >= 21:
        result["20일모멘텀"] = bool(close.iloc[-1] > close.iloc[-21])

    rsi = compute_rsi(close)
    result["RSI값"] = round(rsi, 1) if rsi is not None else None
    result["RSI과열아님"] = bool(rsi < 70) if rsi is not None else None

    daily_range = (df["stck_hgpr"] - df["stck_lwpr"]) / df["stck_clpr"]
    if len(daily_range) >= 20:
        recent5 = daily_range.iloc[-5:].std()
        prior15 = daily_range.iloc[-20:-5].std()
        result["변동성수축(VCP)"] = bool(prior15 and recent5 < prior15 * 0.8)

    return result


def compute_transition_signal(df: pd.DataFrame) -> bool:
    """VCP(눌림) 상태였다가 거래량급증 + 실제 상승이 함께 뜬 경우만 True.

    2026-09-21 백테스트(코스닥 성장주 38종목, 5년)에서 근사t값 2.3~2.5로
    반복 확인된 신호는 원래 '거래량급증'만 조건이었다(상승/하락 무관). 그런데
    2026-09-29 실전에서 폭락하며 거래량이 터진 날(투매)도 똑같이 잡히는 오작동이
    2건(엔젠바이오 -24.9%, 퀀텀레일 -22.4%) 확인돼, 그날 실제로 올랐는지
    (종가 > 전일종가) 조건을 추가했다.
    이 조건 추가로 신호의 정의가 원래 백테스트와 달라졌다 — 즉 지금 이 버전은
    9/21 백테스트로 재검증된 게 아니다. 논리적으로는 더 타당하지만
    통계적 근거는 아직 없는 상태이니, 화면에 '검증된 신호'라고 표시하지 말 것.
    정배열/20일모멘텀/RSI/VCP단독/볼린저밴드 4종은 여전히 효과 미확인 참고용.
    """
    if df.empty or len(df) < 60:
        return False
    close, vol = df["stck_clpr"], df["acml_vol"]
    high, low = df["stck_hgpr"], df["stck_lwpr"]

    vol_avg20 = vol.rolling(20).mean().shift(1)
    vol_surge = vol >= vol_avg20 * 1.5
    up_day = close > close.shift(1)   # 거래량 급증한 그날 실제로 올랐는지

    daily_range = (high - low) / close
    recent5 = daily_range.rolling(5).std()
    prior15 = daily_range.rolling(15).std().shift(5)
    vcp = recent5 < prior15 * 0.8

    recent_vcp = vcp.shift(1).rolling(3, min_periods=1).max().fillna(0).astype(bool)
    transition = (vol_surge.fillna(False).astype(bool) & recent_vcp
                  & up_day.fillna(False).astype(bool))
    return bool(transition.iloc[-1]) if not transition.empty else False


# ============================================================
# DART 공시 리스크
# ============================================================

@st.cache_resource
def load_dart_corp_code_map() -> dict:
    """stock_code -> corp_code 매핑. 앱 실행 중 한 번만 다운로드."""
    if not DART_API_KEY:
        return {}
    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/corpCode.xml",
            params={"crtfc_key": DART_API_KEY}, timeout=30,
        )
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_bytes = zf.read(zf.namelist()[0])
        root = ET.fromstring(xml_bytes)
        mapping = {}
        for node in root.findall("list"):
            stock_code = (node.findtext("stock_code") or "").strip()
            corp_code = (node.findtext("corp_code") or "").strip()
            if stock_code and len(stock_code) == 6:
                mapping[stock_code] = corp_code
        return mapping
    except Exception:
        return {}


@st.cache_data(ttl=3600)
def check_disclosure_risk(stock_code: str) -> list[str]:
    """최근 30일 내 위험 키워드가 포함된 공시 제목 리스트 반환."""
    corp_map = load_dart_corp_code_map()
    corp_code = corp_map.get(stock_code)
    if not corp_code or not DART_API_KEY:
        return []

    end = datetime.now(KST).strftime("%Y%m%d")
    start = (datetime.now(KST) - timedelta(days=30)).strftime("%Y%m%d")
    try:
        resp = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={
                "crtfc_key": DART_API_KEY, "corp_code": corp_code,
                "bgn_de": start, "end_de": end,
                "page_no": 1, "page_count": 50,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "000":
            return []
        risky = []
        for item in data.get("list", []):
            title = item.get("report_nm", "")
            if any(kw in title for kw in RISK_KEYWORDS):
                risky.append(f"{item.get('rcept_dt', '')} {title}")
        return risky
    except Exception:
        return []


# ============================================================
# 네이버 뉴스 (NAVER API HUB)
# ============================================================

@st.cache_data(ttl=1800)
def fetch_news(stock_name: str, count: int = 3):
    """반환: (뉴스 리스트, 디버그용 에러 메시지 또는 None)"""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return [], "NAVER_CLIENT_ID/SECRET이 Secrets에 없음"
    try:
        resp = requests.get(
            "https://naverapihub.apigw.ntruss.com/search/v1/news",
            headers={
                "X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID,
                "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET,
            },
            params={"query": stock_name, "display": count, "sort": "date"},
            timeout=10,
        )
        if resp.status_code != 200:
            return [], f"HTTP {resp.status_code}: {resp.text[:300]}"
        items = resp.json().get("items", [])
        out = []
        for it in items:
            title = re.sub("<[^>]+>", "", it.get("title", ""))
            out.append({"title": title, "link": it.get("link", ""), "pubDate": it.get("pubDate", "")})
        return out, None
    except Exception as e:
        return [], f"예외 발생: {e}"


# ============================================================
# 히스토리 DB (기존 기능)
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS investor_ranking (
            ts TEXT NOT NULL, rank_type TEXT NOT NULL, rank INTEGER NOT NULL,
            stock_code TEXT NOT NULL, stock_name TEXT NOT NULL,
            foreign_net REAL NOT NULL, inst_net REAL NOT NULL, combined_net REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_rows(rows: list[dict]):
    if not rows:
        return
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute(
        "SELECT 1 FROM investor_ranking WHERE ts = ? AND rank_type = ? LIMIT 1",
        (rows[0]["ts"], rows[0]["rank_type"]),
    ).fetchone()
    if not existing:
        conn.executemany("""
            INSERT INTO investor_ranking
            (ts, rank_type, rank, stock_code, stock_name, foreign_net, inst_net, combined_net)
            VALUES (:ts, :rank_type, :rank, :stock_code, :stock_name, :foreign_net, :inst_net, :combined_net)
        """, rows)
        conn.commit()
    conn.close()


# ============================================================
# 화면
# ============================================================

st.set_page_config(page_title="스윙 후보 스크리닝 대시보드", layout="wide")
st.title("투자자별 매매현황 + 스윙 후보 스크리닝")
st.caption("아래 체크리스트는 참고용 스크리닝 결과이며, 투자 자문이 아닙니다. 최종 판단은 직접 하셔야 합니다.")

if not APP_KEY or not APP_SECRET:
    st.error("Secrets에 KIS_APP_KEY / KIS_APP_SECRET이 설정되지 않았습니다.")
    st.stop()

try:
    buy_rows = fetch_investor_ranking("buy")
except Exception as e:
    st.error(f"데이터 조회 실패: {e}")
    st.stop()

try:
    volume_rows, volume_raw_sample = fetch_volume_rank()
    volume_error = None
except Exception as e:
    volume_rows, volume_raw_sample, volume_error = [], {}, str(e)


def price_status_badge(day_pct):
    if day_pct is None:
        return "—"
    if day_pct >= 15:
        return "🔥🔥 급등 마감권 (추격 매우 위험)"
    if day_pct >= 7:
        return "🔥 이미 상승 (추격 주의)"
    if day_pct <= -3:
        return "🔻 하락 중"
    return "➖ 보합 (신선한 구간)"


# 2026-09-29 추가: PER/PBR 숫자만 봐서는 좋은지 나쁜지 바로 판단하기 어렵다는 피드백으로
# 직관 라벨을 붙인다. 절대적인 '싸다/비싸다' 판정이 아니라 구간 분류일 뿐이며, 업종마다
# 기준이 다르다는 점(금융·건설·해운은 원래 PBR이 낮음)은 그대로 감안해야 한다.
def per_label(per) -> str:
    if per is None:
        return ""
    if per <= 0:
        return "🔴 나쁨(적자)"
    if per <= 3:
        return "🟡 주의(수치 왜곡 가능성)"
    if per <= 15:
        return "🟢 좋음(저평가권)"
    if per <= 30:
        return "⚪ 보통"
    if per <= 60:
        return "🔴 나쁨(고평가권)"
    return "🔴 매우 나쁨(고평가)"


def pbr_label(pbr) -> str:
    if pbr is None or pbr <= 0:
        return ""
    if pbr <= 0.6:
        return "🟢 좋음(저평가권)"
    if pbr <= 1.5:
        return "⚪ 보통"
    if pbr <= 3:
        return "🔴 나쁨(고평가권)"
    return "🔴 매우 나쁨(고평가)"


# ============================================================
# 종합점수 (ROE · 부채비율 · 성장률 · PER/PBR)
#   - 각 항목을 0~100점 구간 점수로 바꾼 뒤 가중합. 값이 없는 항목은 빼고 나머지 가중치로 재정규화.
#   - 절대적인 '좋은 종목' 판정이 아니라 구간 분류일 뿐이며, 업종 특성(금융업 부채비율 등)은 감안해야 한다.
#   - scripts/common.py의 '종합점수' 블록과 같은 코드다 (이 파일은 st.secrets를 쓰므로 common을 import하지 않고 복사해 둠).
#     한쪽을 고치면 다른 쪽도 같이 고칠 것.
# ============================================================
SCORE_WEIGHTS = {
    "roe": 0.30,            # 수익성
    "debt": 0.20,           # 안정성
    "sales_growth": 0.10,   # 성장성
    "op_growth": 0.10,
    "per": 0.15,            # 밸류에이션
    "pbr": 0.15,
}
SCORE_PART_NAMES = {"roe": "ROE", "debt": "부채비율", "sales_growth": "매출증가율",
                    "op_growth": "영업이익증가율", "per": "PER", "pbr": "PBR"}


def _band(x, bands):
    """bands: [(상한, 점수), ...] 오름차순. x <= 상한인 첫 구간의 점수. 상한 None은 '그 이상 전부'."""
    for limit, pts in bands:
        if limit is None or x <= limit:
            return pts
    return bands[-1][1]


def score_roe(roe):
    if roe is None:
        return None
    if roe > 40:
        return 50            # 일회성 이익 가능성 — 만점을 주지 않는다
    return _band(roe, [(0, 0), (5, 20), (10, 50), (15, 75), (25, 100), (40, 85)])


def score_debt(debt):        # 낮을수록 좋음
    if debt is None:
        return None
    return _band(debt, [(50, 100), (100, 80), (200, 50), (300, 25), (None, 0)])


def score_sales_growth(g):
    if g is None:
        return None
    return _band(g, [(-10, 0), (0, 30), (5, 50), (15, 75), (None, 100)])


def score_op_growth(g):
    # 0은 '적자지속/흑자전환/적자전환' 표시와 구분이 안 되므로 점수에서 제외(None)
    if g is None or g == 0:
        return None
    return _band(g, [(-20, 0), (0, 30), (10, 55), (30, 80), (None, 100)])


def score_per(per):
    if per is None:
        return None
    if per <= 0:
        return 0
    if per <= 3:
        return 50            # 수치 왜곡 가능성 (per_label과 같은 기준)
    return _band(per, [(8, 100), (15, 80), (30, 50), (60, 20), (None, 0)])


def score_pbr(pbr):
    if pbr is None or pbr <= 0:
        return None
    return _band(pbr, [(0.6, 100), (1.0, 85), (1.5, 65), (3.0, 30), (None, 0)])


def composite_score(per, pbr, fin, weights=None) -> dict:
    """반환: {"score": 0~100 또는 None, "parts": {항목: 점수|None}, "coverage": 반영된 가중치 비율(0~1), "notes": [...]}
    fin은 fetch_financial_ratio 결과(dict) 또는 None."""
    weights = weights or SCORE_WEIGHTS
    fin = fin or {}
    parts = {
        "roe": score_roe(fin.get("roe")),
        "debt": score_debt(fin.get("debt_ratio")),
        "sales_growth": score_sales_growth(fin.get("sales_growth")),
        "op_growth": score_op_growth(fin.get("op_growth")),
        "per": score_per(per),
        "pbr": score_pbr(pbr),
    }
    used = {k: v for k, v in parts.items() if v is not None}
    total_w = sum(weights.values())
    used_w = sum(weights[k] for k in used)
    notes = []
    if fin.get("op_growth") == 0:
        notes.append("영업이익 증가율 0: 적자 관련 상태일 수 있어 점수 제외")
    if fin.get("roe") is not None and fin["roe"] > 40:
        notes.append("ROE 40% 초과: 일회성 이익 의심")
    if fin.get("debt_ratio") is not None and fin["debt_ratio"] > 500:
        notes.append("부채비율 500% 초과: 금융업이면 구조적으로 높을 수 있음")
    coverage = round(used_w / total_w, 2) if total_w else 0.0
    if used_w and coverage < 0.8:
        notes.append(f"일부 항목 미반영(가중치 {coverage:.0%}만 반영): 점수 신뢰도 낮음")
    score = round(sum(weights[k] * v for k, v in used.items()) / used_w, 1) if used_w else None
    return {"score": score, "parts": parts, "coverage": coverage, "notes": notes}


def score_label(score) -> str:
    if score is None:
        return "—"
    if score >= 75:
        return "🟢 우수"
    if score >= 55:
        return "⚪ 보통"
    return "🔴 미흡"


# PER/PBR 라벨(per_label/pbr_label)과 같은 방식의 직관 라벨
def roe_label(roe) -> str:
    if roe is None:
        return ""
    if roe <= 0:
        return "🔴 나쁨(적자)"
    if roe < 5:
        return "🔴 나쁨(낮음)"
    if roe < 10:
        return "⚪ 보통"
    if roe <= 25:
        return "🟢 좋음"
    if roe <= 40:
        return "🟢 좋음(높음)"
    return "🟡 주의(일회성 이익 의심)"


def debt_label(debt) -> str:
    if debt is None:
        return ""
    if debt <= 100:
        return "🟢 좋음(안정)"
    if debt <= 200:
        return "⚪ 보통"
    if debt <= 300:
        return "🔴 나쁨(부담)"
    return "🔴 매우 나쁨(과다)"


def growth_label(g) -> str:
    if g is None:
        return ""
    if g < 0:
        return "🔴 역성장"
    if g < 5:
        return "⚪ 정체"
    if g < 15:
        return "🟢 성장"
    return "🟢 고성장"



def fmt_fin_base(yymm) -> str:
    """결산년월 '202606' -> '2026.06' (없으면 '—'). 12월이 아니면 반기·분기 자료라는 걸 표에서 바로 알 수 있게 한다."""
    y = str(yymm or "")
    return f"{y[:4]}.{y[4:]}" if len(y) == 6 and y.isdigit() else "—"


def fmt_fin_line(fin, score_info=None) -> str:
    """재무 한 줄 요약 (ROE·부채비율·성장률·종합점수). fin이 None이면 빈 문자열."""
    if not fin:
        return ""
    parts = []
    if fin.get("roe") is not None:
        parts.append(f"ROE {fin['roe']:.1f}% {roe_label(fin['roe'])}".strip())
    if fin.get("debt_ratio") is not None:
        parts.append(f"부채비율 {fin['debt_ratio']:.0f}% {debt_label(fin['debt_ratio'])}".strip())
    if fin.get("sales_growth") is not None:
        parts.append(f"매출증가 {fin['sales_growth']:+.1f}% {growth_label(fin['sales_growth'])}".strip())
    op = fin.get("op_growth")
    if op is not None:
        parts.append("영업이익 증가율 0(적자 관련 상태일 수 있음)" if op == 0
                     else f"영업이익증가 {op:+.1f}% {growth_label(op)}".strip())
    if score_info and score_info.get("score") is not None:
        parts.append(f"종합점수 {score_info['score']:.0f} {score_label(score_info['score'])}")
    yymm = f" (결산 {fin['stac_yymm']})" if fin.get("stac_yymm") else ""
    return " · ".join(parts) + yymm


def fmt_shares(value) -> str:
    """1만주 이상이면 '만주' 단위로, 부호 포함 표시."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "+" if v > 0 else ""
    if abs(v) >= 10000:
        return f"{sign}{v/10000:,.1f}만주"
    return f"{sign}{v:,.0f}주"


def fmt_shares_plain(value) -> str:
    """부호 없이, 1만주 이상이면 '만주' 단위로 표시."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) >= 10000:
        return f"{v/10000:,.1f}만주"
    return f"{v:,.0f}주"


def colored_pct_html(value, suffix: str = "%") -> str:
    """국내 증권사 관행: 양수 빨강, 음수 파랑, 0/None은 회색."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    color = "#e03131" if v > 0 else ("#1971c2" if v < 0 else "#868e96")
    sign = "+" if v > 0 else ""
    if suffix == "%":
        text = f"{sign}{v:,.2f}%"
    else:
        text = fmt_shares(v)
    return f"<span style='color:{color}; font-weight:700;'>{text}</span>"


def style_signed(df: pd.DataFrame, cols: list[str], plain_cols: dict | None = None):
    """signed cols: 양수 빨강/음수 파랑 + '만주'/부호 단위 표시. plain_cols: {컬럼명: 포맷함수}로 단위만 적용.
    주의: Styler.format을 dict로 여러 번 부르면 앞서 지정한 컬럼 포맷이 초기화되므로 컬럼별 subset으로 지정한다."""
    def _color(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return ""
        if v > 0:
            return "color: #e03131; font-weight: 600;"
        if v < 0:
            return "color: #1971c2; font-weight: 600;"
        return ""

    def _fmt_pct(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return str(v)
        if v != v:  # NaN
            return "—"
        return f"{'+' if v > 0 else ''}{v:,.2f}%"

    existing = [c for c in cols if c in df.columns]
    styler = df.style
    if existing:
        styler = styler.map(_color, subset=existing)
        for c in existing:
            fmt = _fmt_pct if c in ("등락률(%)", "당일등락률(%)") else fmt_shares
            styler = styler.format(fmt, subset=[c])
    for c, fmt in (plain_cols or {}).items():
        if c in df.columns:
            styler = styler.format(fmt, subset=[c])
    return styler


def trend_label_for(stock_code: str) -> str:
    df = fetch_daily_ohlcv(stock_code)
    label, _, _ = classify_trend_state(df)
    return label


tab_supply, tab_volume, tab_value, tab_overlap, tab_intraday, tab_screen, tab_reversal, tab_lookup = st.tabs([
    "📊 순매수 상위", "📈 거래량 상위", "💰 저평가 후보", "🔥 동시 등장", "⏱ 장중 변동", "✅ 스윙 후보 스크리닝", "🔄 반등 후보", "🔍 종목 조회",
])

# ---------------- 📊 순매수 상위 ----------------
with tab_supply:
    st.subheader("순매수 상위 10")
    if buy_rows:
        st.caption(f"기준 시각: {buy_rows[0]['ts']}")
        buy_df = pd.DataFrame(buy_rows).drop(columns=["ts", "rank_type"])
        buy_df = buy_df.rename(columns={
            "rank": "순위", "stock_code": "종목코드", "stock_name": "종목명",
            "foreign_net": "외국인순매수", "inst_net": "기관순매수", "combined_net": "전체순매수",
        })
        st.dataframe(style_signed(buy_df, ["외국인순매수", "기관순매수", "전체순매수"]),
                     use_container_width=True, hide_index=True)

# ---------------- 📈 거래량 상위 ----------------
with tab_volume:
    st.subheader("거래량 상위 10")
    if volume_error:
        st.warning(f"거래량 상위 조회 실패: {volume_error}")

    if volume_rows:
        vol_df = pd.DataFrame(volume_rows).rename(columns={
            "rank": "순위", "stock_code": "종목코드", "stock_name": "종목명",
            "volume": "거래량", "day_pct": "등락률(%)",
        })
        st.dataframe(style_signed(vol_df, ["등락률(%)"], plain_cols={"거래량": fmt_shares_plain}),
                     use_container_width=True, hide_index=True)
        with st.expander("원본 응답 확인 (필드명 검증용)"):
            st.json(volume_raw_sample)

        st.markdown("**거래량 상위 종목 기술적 스크리닝**")
        st.caption("순매수 상위와 달리 개인 투기 수급이 섞일 수 있는 목록입니다 — 아래 체크와 함께 반드시 같이 보세요.")
        for row in volume_rows:
            code, name = row["stock_code"], row["stock_name"]
            if not code:
                continue
            with st.expander(f"{row['rank']}위 · {name} ({code}) · 거래량 {fmt_shares_plain(row['volume'])}"):
                vdf = fetch_daily_ohlcv(code)
                vtech = analyze_technicals(vdf)
                vchecks = {k: v for k, v in vtech.items() if k != "RSI값"}
                vpassed = sum(1 for v in vchecks.values() if v is True)
                vtotal = sum(1 for v in vchecks.values() if v is not None)
                if vtotal == 0:
                    st.caption("일봉 데이터를 가져오지 못했습니다.")
                else:
                    vrsi_note = f" (RSI: {vtech['RSI값']})" if vtech["RSI값"] is not None else ""
                    st.markdown(f"**기술적 체크: {vpassed}/{vtotal} 통과**{vrsi_note}")
                    vcols = st.columns(len(vchecks))
                    for c, (label, val) in zip(vcols, vchecks.items()):
                        icon = "✅" if val is True else ("❌" if val is False else "—")
                        c.metric(label, icon)
                    vstate_label, vstate_desc, vstate_fn = classify_trend_state(vdf)
                    vstate_fn(f"**{vstate_label}** — {vstate_desc}")
                vrisky = check_disclosure_risk(code)
                if vrisky:
                    st.error("⚠️ 최근 30일 내 주의 공시 발견:\n" + "\n".join(f"- {r}" for r in vrisky))
                elif DART_API_KEY:
                    st.success("최근 30일 내 주의 공시 없음")

# ---------------- 💰 저평가 후보 ----------------
VALUE_SCREEN_PATH = "data/value_screen.json"


def render_value_table(rows: list, total: int | None = None, caution_below: float = 3.0):
    """저평가 스캔 결과 목록을 표로 보여준다. total: 조건을 통과한 전체 종목 수(저장된 건 상위 일부)."""
    if not rows:
        st.info("조건에 맞는 종목이 없습니다.")
        return
    if total is not None:
        st.caption(f"조건을 통과한 종목 {total:,}개 중 상위 {len(rows)}개를 표시합니다." if total > len(rows)
                   else f"조건을 통과한 종목 {total:,}개를 모두 표시합니다.")
    table = []
    for i, r in enumerate(rows, start=1):
        risky = r.get("risky_disclosures")
        table.append({
            "순위": i, "종목코드": r["code"], "종목명": r["name"], "업종": r.get("sector", ""),
            "현재가": r.get("price"), "당일등락률(%)": r.get("day_pct"),
            "종합점수": r.get("score"), "등급": score_label(r.get("score")),
            "PER": r.get("per"), "PER평가": per_label(r.get("per")),
            "PBR": r.get("pbr"), "PBR평가": pbr_label(r.get("pbr")),
            "ROE(%)": r.get("roe_used", r.get("roe_pct")), "ROE평가": roe_label(r.get("roe_used", r.get("roe_pct"))),
            "부채비율(%)": r.get("debt_ratio"), "매출증가율(%)": r.get("sales_growth"),
            "영업이익증가율(%)": r.get("op_growth"), "재무기준": fmt_fin_base(r.get("fin_yymm")),
            "시총(억)": r.get("mktcap_eok"), "52주고점대비(%)": r.get("drawdown_pct"),
            "공시": ("⚠ " + "; ".join(risky)) if risky else ("이상 없음" if risky == [] else "미확인"),
            "비고": " / ".join(filter(None, [
                "ROE는 PBR÷PER 역산 추정(재무비율 조회 실패)" if r.get("roe_source") == "추정" else "",
                "⚠ 일회성 이익 의심" if r.get("oneoff_suspect") else "",
                *[f"⚠ {n}" for n in (r.get("score_notes") or [])],
                f"PER {caution_below:g} 미만: 이익 지속성 확인 필요"
                if (r.get("per") is not None and 0 < r["per"] < caution_below) else "",
            ])),
        })
    df = pd.DataFrame(table)
    nan_dash = lambda f: (lambda v: f(v) if pd.notna(v) else "—")
    st.dataframe(
        style_signed(df, ["당일등락률(%)"], plain_cols={
            "현재가": nan_dash(lambda v: f"{v:,.0f}"),
            "PER": nan_dash(lambda v: f"{v:.1f}"),
            "PBR": nan_dash(lambda v: f"{v:.2f}"),
            "종합점수": nan_dash(lambda v: f"{v:.0f}"),
            "ROE(%)": nan_dash(lambda v: f"{v:.1f}"),
            "부채비율(%)": nan_dash(lambda v: f"{v:.0f}"),
            "매출증가율(%)": nan_dash(lambda v: f"{v:+.1f}"),
            "영업이익증가율(%)": nan_dash(lambda v: f"{v:+.1f}"),
            "시총(억)": nan_dash(lambda v: f"{v:,.0f}"),
            "52주고점대비(%)": nan_dash(lambda v: f"{v:+.1f}%"),
        }),
        use_container_width=True, hide_index=True)


with tab_value:
    st.subheader("저평가 후보 (전체 상장사 PER·PBR 자동 스캔)")

    if not os.path.exists(VALUE_SCREEN_PATH):
        st.info("아직 스캔 결과가 없습니다. GitHub Actions의 'Value Screen' 워크플로를 한 번 실행해주세요 "
                "(이후에는 평일 07:20에 자동 실행됩니다).")
    else:
        with open(VALUE_SCREEN_PATH, "r", encoding="utf-8") as f:
            value_screen = json.load(f)
        vs_stats = value_screen.get("market_stats", {})
        vs_crit = value_screen.get("criteria", {})

        st.caption(
            f"스캔 시각 {value_screen.get('generated_at')} · 조회 성공 {value_screen.get('scanned_ok', 0):,}"
            f" / 실패 {value_screen.get('scanned_failed', 0):,} · 필터 통과 {vs_stats.get('eligible_count', 0):,}종목"
            f" · 시장 PER 중앙값 {vs_stats.get('median_per')} · PBR 1 미만 비중 {vs_stats.get('pbr_below_1_pct')}%")
        if value_screen.get("partial"):
            st.warning("시간 초과로 일부 종목만 스캔된 결과입니다.")

        filt = ["흑자 종목만", "관리·경고·정지 종목 제외"]
        if vs_crit.get("mktcap_filter_applied"):
            filt.insert(1, f"시총 {vs_crit.get('min_mktcap_eok'):.0f}억 이상")
        if vs_crit.get("liquidity_filter_applied"):
            filt.insert(2, f"전일 거래대금 {vs_crit.get('min_tr_value_eok')}억 이상")
        st.caption("적용 기준: " + ", ".join(filt) + ". PER은 KIS가 제공하는 '최근 확정 연간 EPS' 기준이라 "
                   "실적이 막 좋아지는 회사는 아직 비싸 보이고, 막 나빠지는 회사는 싸 보일 수 있습니다. "
                   "ROE·부채비율·매출/영업이익 증가율은 KIS 재무비율 API의 최근 결산 값입니다(PER/PBR 1차 필터를 통과한 후보에만 조회하며, "
                   "조회에 실패한 종목은 ROE만 PBR÷PER 역산 추정으로 대체하고 종합점수는 비웁니다). "
                   "'재무기준'은 그 값의 결산 시점이며, 12월이 아니면(예: 2026.06) 반기·분기 자료라 ROE는 연환산 값으로 보이고 "
                   "PER/PBR(작년 확정 연간 EPS 기준)과 기준 시점이 다릅니다. "
                   "종합점수는 ROE·부채비율·성장률·PER/PBR을 0~100점 구간으로 바꿔 가중합산한 참고용 점수라 금융업 부채비율 같은 업종 특성은 반영되지 않습니다. "
                   "영업이익 증가율 0은 적자지속·흑자전환·적자전환일 수 있어 점수에서 제외합니다. "
                   "ROE가 40%를 넘으면 자산 매각 같은 일회성 이익일 가능성이 높아 '일회성 이익 의심'으로 표시하고 목록 뒤로 보냅니다. "
                   "PER이 3 미만인 종목도 정상 영업이익으로는 드문 수준이라 '이익 지속성 확인 필요'를 붙이고, "
                   "균형형에서는 이런 종목을 목록 뒤로 보냅니다(다른 두 목록은 순서 그대로). "
                   "이 목록은 관심 종목 후보 풀일 뿐 매수 신호로 검증된 게 아니며, 싼 데에는 이유(실적 악화, "
                   "지배구조 등)가 있는 경우가 많으니 공시·뉴스를 꼭 같이 확인하세요.")

        vs_totals = value_screen.get("list_totals", {})
        cnt = lambda k: f" · {vs_totals[k]:,}종목" if k in vs_totals else ""
        caution_below = vs_crit.get("per_caution_below", 3.0)
        sub_bal, sub_per, sub_pbr = st.tabs([
            f"⭐ 균형형 (PER ≤ {vs_crit.get('bal_per_max', 10):g} & PBR ≤ {vs_crit.get('bal_pbr_max', 1):g} & "
            f"ROE {vs_crit.get('bal_roe_min', 10):g}~{vs_crit.get('bal_roe_max', 25):g}%){cnt('balanced')}",
            f"저PER 우량 (PER ≤ {vs_crit.get('low_per_max')} & PBR ≤ {vs_crit.get('low_per_pbr_max')}){cnt('low_per')}",
            f"저PBR 자산가치 (PBR ≤ {vs_crit.get('low_pbr_max')} & 흑자){cnt('low_pbr')}",
        ])
        with sub_bal:
            if "balanced" not in value_screen:
                st.info("균형형 목록은 다음 스캔부터 표시됩니다. GitHub Actions의 'Value Screen'을 한 번 실행해주세요.")
            else:
                st.caption("처음 볼 때 권하는 목록입니다. PER·PBR이 둘 다 낮고 ROE가 적당한 종목만 남겨서, "
                           "한쪽만 싼 종목·수익성이 낮아서 싼 종목·일회성 이익 종목을 함께 걸러냅니다. "
                           "순서는 종합점수가 높은 순이고(동점이면 PER·PBR 상한 대비 합이 작은 순), "
                           "PER 3 미만은 맨 뒤로 보냅니다. 표 머리글을 누르면 종합점수·PBR·시총·ROE 등 원하는 기준으로 "
                           "다시 정렬할 수 있습니다. 업종 특성(금융·건설·해운은 원래 PBR이 낮음)과 "
                           "최근 분기 실적은 직접 확인하세요.")
                render_value_table(value_screen.get("balanced", []), vs_totals.get("balanced"), caution_below)
        with sub_per:
            render_value_table(value_screen.get("low_per", []), vs_totals.get("low_per"), caution_below)
        with sub_pbr:
            render_value_table(value_screen.get("low_pbr", []), vs_totals.get("low_pbr"), caution_below)

        st.markdown("**🎯💰 오늘 순매수·거래량 상위와 겹치는 저평가 종목**")
        st.caption("수급/거래량 관심과 저평가가 동시에 나타난 종목입니다.")
        today_codes = {r["stock_code"] for r in buy_rows}
        if volume_rows:
            today_codes |= {r["stock_code"] for r in volume_rows}
        balanced_codes = {r["code"] for r in value_screen.get("balanced", [])}
        seen, overlap = set(), []
        for r in (value_screen.get("balanced", []) + value_screen.get("low_per", []) + value_screen.get("low_pbr", [])):
            if r["code"] in today_codes and r["code"] not in seen:
                seen.add(r["code"])
                overlap.append(r)
        if overlap:
            for r in overlap:
                st.success(f"{'⭐ ' if r['code'] in balanced_codes else ''}{r['name']}({r['code']}) — PER {r['per']:.1f} · PBR {r['pbr']:.2f}"
                           + (f" · 시총 {r['mktcap_eok']:,.0f}억" if r.get("mktcap_eok") is not None else ""))
        else:
            st.info("오늘 순매수·거래량 상위에 오른 종목 중 저평가 목록과 겹치는 종목이 없습니다.")

        with st.expander("스캔 원본 응답·종목상태코드 분포 확인 (필드 검증용)"):
            st.caption("PER/PBR·시총 필드가 실제 응답에서 기대한 이름으로 오는지, 종목상태코드 해석이 맞는지 확인하는 용도입니다.")
            st.json(value_screen.get("raw_sample", {}))
            st.write("종목상태코드 분포:", value_screen.get("stat_code_counts", {}))
            st.write("조회 실패 사유(코드별 건수·예시 메시지):", value_screen.get("fail_reasons", {}))
        st.write(f"재무비율 조회 — 후보 풀 {value_screen.get('fin_pool_size', '—')} / 성공 {value_screen.get('fin_ok', '—')}"
                 f" / 실패 {value_screen.get('fin_failed', '—')}" + (" (시간 초과로 일부 생략)" if value_screen.get("fin_partial") else ""))
        st.caption("재무비율 API 원본 응답 한 건 — roe_val·lblt_rate·grs·bsop_prfi_inrt 필드가 기대한 값으로 오는지 확인하는 용도입니다.")
        st.json(value_screen.get("fin_raw_sample", {}))

# ---------------- 🔥 동시 등장 ----------------
with tab_overlap:
    st.subheader("동시 등장 종목 (순매수 상위 + 거래량 상위)")
    st.caption("두 리스트는 성격이 달라 합산 점수를 매기지 않습니다. 대신 둘 다에 오른 종목만 따로 골라 보여드립니다 — 큰손 매수와 시장 관심이 동시에 쏠린 종목입니다.")

    buy_codes_map = {r["stock_code"]: r for r in buy_rows}
    volume_codes_map = {r["stock_code"]: r for r in volume_rows} if volume_rows else {}
    overlap_codes = set(buy_codes_map) & set(volume_codes_map)

    if not overlap_codes:
        st.info("현재 두 리스트에 동시에 오른 종목이 없습니다.")
    else:
        for code in overlap_codes:
            b, v = buy_codes_map[code], volume_codes_map[code]
            with st.expander(f"{b['stock_name']}({code}) · 순매수 {b['rank']}위 · 거래량 {v['rank']}위"):
                st.markdown(f"- 외국인순매수: {colored_pct_html(b['foreign_net'], '')} / "
                            f"기관순매수: {colored_pct_html(b['inst_net'], '')}", unsafe_allow_html=True)
                st.markdown(f"- 거래량: {fmt_shares_plain(v['volume'])} / 등락률: {colored_pct_html(v['day_pct'])}", unsafe_allow_html=True)
                odf = fetch_daily_ohlcv(code)
                otech = analyze_technicals(odf)
                ochecks = {k: val for k, val in otech.items() if k != "RSI값"}
                opassed = sum(1 for val in ochecks.values() if val is True)
                ototal = sum(1 for val in ochecks.values() if val is not None)
                if ototal > 0:
                    orsi_note = f" (RSI: {otech['RSI값']})" if otech["RSI값"] is not None else ""
                    st.markdown(f"**기술적 체크: {opassed}/{ototal} 통과**{orsi_note}")
                    ocols = st.columns(len(ochecks))
                    for c, (label, val) in zip(ocols, ochecks.items()):
                        icon = "✅" if val is True else ("❌" if val is False else "—")
                        c.metric(label, icon)
                    ostate_label, ostate_desc, ostate_fn = classify_trend_state(odf)
                    ostate_fn(f"**{ostate_label}** — {ostate_desc}")
                orisky = check_disclosure_risk(code)
                if orisky:
                    st.error("⚠️ 최근 30일 내 주의 공시 발견:\n" + "\n".join(f"- {r}" for r in orisky))
                elif DART_API_KEY:
                    st.success("최근 30일 내 주의 공시 없음")

# ---------------- ⏱ 장중 변동 ----------------
with tab_intraday:
    st.subheader("장중 후보 변동 (제외 / 신규 / 유지)")
    st.caption("후보 기준: 전환신호(VCP 눌림 후 거래량급증+상승). 2026-09-29에 '상승' 조건을 "
               "추가해 9/21 백테스트와 정의가 달라졌습니다 — 재검증 전까지 참고용입니다.")

    INTRADAY_STATUS_PATH = "data/intraday_status.json"
    if os.path.exists(INTRADAY_STATUS_PATH):
        with open(INTRADAY_STATUS_PATH, "r", encoding="utf-8") as f:
            intraday = json.load(f)
        st.caption(f"마지막 재점검: {intraday.get('checked_at', '?')}")

        excluded = intraday.get("excluded", [])
        new_candidates = intraday.get("new_candidates", [])
        kept = intraday.get("kept", [])

        if excluded:
            st.markdown("**🔴 제외 후보** (아침엔 후보였으나 조건 이탈)")
            for e in excluded:
                price_str = f" · 현재가 {e['current_price']:,.0f}원 ({colored_pct_html(e['day_pct'])})" if e.get("current_price") else ""
                trend = trend_label_for(e["stock_code"])
                st.markdown(f"- {e['stock_name']}({e['stock_code']}) — {e['reason']}{price_str} · **국면: {trend}**", unsafe_allow_html=True)
        if new_candidates:
            st.markdown("**🟢 신규 후보** (아침엔 없었으나 지금 조건 충족)")
            for n in new_candidates:
                price_str = f" · 현재가 {n['current_price']:,.0f}원 ({colored_pct_html(n['day_pct'])})" if n.get("current_price") else ""
                badge = price_status_badge(n.get("day_pct"))
                trend = trend_label_for(n["stock_code"])
                st.markdown(f"- {n['stock_name']}({n['stock_code']}) — {n['passed']}/{n['total']}점{price_str} · {badge} · **국면: {trend}**", unsafe_allow_html=True)
        if kept:
            st.markdown("**⚪ 유지 중** (아침 후보 그대로, 실시간 현재가)")
            kept_rows = [{
                "종목명": k["stock_name"], "종목코드": k["stock_code"],
                "현재가": k.get("current_price"), "당일등락률(%)": k.get("day_pct"),
                "상태": price_status_badge(k.get("day_pct")),
                "국면": trend_label_for(k["stock_code"]),
            } for k in kept]
            st.dataframe(
                style_signed(pd.DataFrame(kept_rows), ["당일등락률(%)"],
                             plain_cols={"현재가": lambda v: f"{v:,.0f}원" if pd.notna(v) else "—"}),
                use_container_width=True, hide_index=True)
        if not excluded and not new_candidates and not kept:
            st.info("아침 후보 대비 변동 없음")
    else:
        st.caption("아직 장중 재점검 데이터가 없습니다 (첫 재점검은 09:40경 실행됩니다).")

# ---------------- ✅ 스윙 후보 스크리닝 ----------------
with tab_screen:
    st.subheader("순매수 상위 10 — 스윙 후보 스크리닝")
    st.caption("2026-09-21 백테스트(코스닥 성장주 38종목, 5년)에서 유일하게 통계적 근거가 있던 "
               "신호는 전환신호(눌림 후 거래량급증)였습니다. 다만 2026-09-29에 그날 실제로 올랐는지 "
               "조건을 추가했습니다(폭락하며 거래량이 터진 날도 신호로 잡히는 오작동 발견 — 엔젠바이오·"
               "퀀텀레일 사례). 조건이 바뀌어 위 백테스트가 지금 버전을 검증하진 않으니, 재검증 전까지 "
               "**참고용**으로 봐주세요. 정배열·20일모멘텀·RSI·VCP단독·볼린저밴드는 원래도 효과 미확인.")

    if not (DART_API_KEY and NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        st.warning("DART / 네이버 뉴스 Secrets이 없어 공시·뉴스 정보는 생략됩니다.")

    scored_rows = []
    for row in buy_rows:
        code = row["stock_code"]
        df = fetch_daily_ohlcv(code)
        if df.empty:
            time.sleep(0.5)
            df = fetch_daily_ohlcv.__wrapped__(code)  # 캐시 우회 재시도
        tech = analyze_technicals(df)
        raw_transition = compute_transition_signal(df)
        price, pct, mflags = fetch_current_price(code)
        per, pbr, _, _, _ = fetch_valuation(code)
        fin = fetch_financial_ratio(code)
        fscore = composite_score(per, pbr, fin) if fin else None
        trend_label, _, _ = classify_trend_state(df)  # 이미 받아온 df 재사용 (중복 조회 방지)
        # 관리종목·투자위험 등 거래소 지정 상태면 '거래량급증'이 매수세가 아니라 투매일 수 있어
        # 검증된 신호로 인정하지 않는다 (백테스트 표본에 이런 상태의 종목은 없었다)
        transition = raw_transition and not mflags
        checks = {k: v for k, v in tech.items() if k != "RSI값"}
        passed = sum(1 for v in checks.values() if v is True)
        total = sum(1 for v in checks.values() if v is not None)
        scored_rows.append({**row, "_tech": tech, "_checks": checks, "_passed": passed,
                             "_total": total, "_transition": transition, "_mflags": mflags,
                             "_price": price, "_pct": pct, "_per": per, "_pbr": pbr,
                             "_trend": trend_label, "_fin": fin, "_score": fscore})

    # 전환신호 있는 종목을 최상단으로, 그다음은 참고점수순
    scored_rows.sort(key=lambda r: (not r["_transition"], -r["_passed"], r["rank"]))

    n_transition = sum(1 for r in scored_rows if r["_transition"])
    n_transition_risky = sum(1 for r in scored_rows if r["_transition"] and r["_mflags"])
    n_transition_chased = sum(1 for r in scored_rows if r["_transition"] and r["_pct"] is not None and r["_pct"] >= 7)
    if n_transition:
        st.success(f"🎯 전환신호 종목 {n_transition}개 발견")
        if n_transition_risky:
            st.error(f"⚠️ 그중 {n_transition_risky}개는 관리종목·투자위험 등 거래소 지정 상태입니다 — "
                     "거래량급증이 매수세가 아니라 투매일 수 있으니 근거로 쓰지 마세요.")
        if n_transition_chased:
            st.warning(f"🔥 그중 {n_transition_chased}개는 오늘 이미 +7% 이상 올라 추격 부담이 있습니다 "
                       "(아래 표의 '상태' 열 참고 — 2026-09-29 백테스트로 '며칠 기다렸다 재진입'은 "
                       "효과가 없다고 확인돼, 따로 관찰 목록으로 미루지 않고 그대로 표시합니다).")
    else:
        st.info("오늘은 전환신호가 뜬 종목이 없습니다. 아래는 전부 참고용입니다.")

    st.markdown("**한눈에 비교**")
    st.caption("표 머리글을 누르면 정렬됩니다. 자세한 뉴스·공시는 아래 종목별 펼치기에서 확인하세요.")
    table_rows = []
    for r in scored_rows:
        table_rows.append({
            "순위": r["rank"], "종목코드": r["stock_code"], "종목명": r["stock_name"],
            "전환신호": "🎯 확인" if r["_transition"] else ("🚨 위험상태" if r["_mflags"] else "—"),
            "현재가": r["_price"], "당일등락률(%)": r["_pct"], "상태": price_status_badge(r["_pct"]),
            "PER": r["_per"], "PER평가": per_label(r["_per"]),
            "PBR": r["_pbr"], "PBR평가": pbr_label(r["_pbr"]),
            "ROE(%)": (r["_fin"] or {}).get("roe"), "부채비율(%)": (r["_fin"] or {}).get("debt_ratio"),
            "매출증가율(%)": (r["_fin"] or {}).get("sales_growth"), "영업이익증가율(%)": (r["_fin"] or {}).get("op_growth"),
            "재무기준": fmt_fin_base((r["_fin"] or {}).get("stac_yymm")),
            "종합점수": (r["_score"] or {}).get("score"), "등급": score_label((r["_score"] or {}).get("score")),
            "국면": r["_trend"], "참고지표": f"{r['_passed']}/{r['_total']}",
        })
    summary_df = pd.DataFrame(table_rows)
    nan_dash = lambda f: (lambda v: f(v) if pd.notna(v) else "—")
    st.dataframe(
        style_signed(summary_df, ["당일등락률(%)"], plain_cols={
            "현재가": nan_dash(lambda v: f"{v:,.0f}"),
            "PER": nan_dash(lambda v: f"{v:.2f}"),
            "PBR": nan_dash(lambda v: f"{v:.2f}"),
            "ROE(%)": nan_dash(lambda v: f"{v:.1f}"),
            "부채비율(%)": nan_dash(lambda v: f"{v:.0f}"),
            "매출증가율(%)": nan_dash(lambda v: f"{v:+.1f}"),
            "영업이익증가율(%)": nan_dash(lambda v: f"{v:+.1f}"),
            "종합점수": nan_dash(lambda v: f"{v:.0f}"),
        }),
        use_container_width=True, hide_index=True)

    for row in scored_rows:
        code, name = row["stock_code"], row["stock_name"]
        tech, checks, passed, total = row["_tech"], row["_checks"], row["_passed"], row["_total"]
        transition, mflags = row["_transition"], row["_mflags"]
        price, pct = row["_price"], row["_pct"]
        price_str = f" · {price:,.0f}원 ({pct:+.2f}%)" if price is not None else ""
        title_prefix = ("🚨 " if mflags else "") + ("🎯 전환신호 " if transition else "참고 ")
        with st.expander(f"{title_prefix}· 원순위 {row['rank']}위 · {name} ({code}){price_str}"):
            if mflags:
                st.error("🚨 거래소 지정 상태: " + " · ".join(mflags) +
                          " — 거래소 위험 상태에서는 신호 자체를 인정하지 않습니다.")
            if transition:
                st.success("🎯 **전환신호 확인** — VCP(눌림) 이후 거래량급증+상승. "
                           "(2026-09-29 방향 조건 추가로 9/21 백테스트 재검증 전 — 참고용)")
                if pct is not None and pct >= 7:
                    st.warning(f"🔥 오늘 이미 {pct:+.2f}% 상승 — 추격 매수 부담이 있는 구간입니다.")
            else:
                st.caption("전환신호 없음 (조건 미충족)")

            st.caption(f"국면 판단: **{row['_trend']}**")
            fin_line = fmt_fin_line(row["_fin"], row["_score"])
            if fin_line:
                st.markdown(f"**재무**: {fin_line}")
                for n in (row["_score"] or {}).get("notes", []):
                    st.caption(f"⚠ {n}")
            else:
                st.caption("재무비율을 가져오지 못했습니다 (ETF·신규상장 등은 제공되지 않을 수 있습니다).")

            rsi_note = f" (RSI: {tech['RSI값']})" if tech["RSI값"] is not None else ""
            st.markdown(f"**참고지표: {passed}/{total} 통과**{rsi_note} — 아래는 효과가 "
                        "확인되지 않은 지표들이라 판단 근거로 쓰지 마세요.")

            if total == 0:
                st.caption("일봉 데이터를 가져오지 못했습니다.")
            else:
                cols = st.columns(len(checks))
                for c, (label, val) in zip(cols, checks.items()):
                    icon = "✅" if val is True else ("❌" if val is False else "—")
                    c.metric(label, icon)

            risky = check_disclosure_risk(code)
            if risky:
                st.error("⚠️ 최근 30일 내 주의 공시 발견:\n" + "\n".join(f"- {r}" for r in risky))
            elif DART_API_KEY:
                st.success("최근 30일 내 주의 공시 없음")

            news, news_err = fetch_news(name)
            if news:
                st.markdown("**관련 뉴스**")
                for n in news:
                    st.markdown(f"- [{n['title']}]({n['link']})")

# ---------------- 🔄 반등 후보 ----------------
REVERSAL_LABELS = {"하락추세 속 기술적 반등", "하락추세 · 반등 준비 구간", "하락추세 속 단기 반등 시도"}

with tab_reversal:
    st.subheader("하락추세 반등 후보")
    st.caption("순매수 상위·거래량 상위 후보군 안에서, 국면 판단이 '하락추세 속 반등'류로 나온 종목만 골라 보여드립니다. "
               "전환신호도 지금은 재검증 전 참고용이고, 이 분류는 그보다도 더 느슨한 규칙 기반 참고용입니다 (코스피/코스닥 전체를 매번 스캔할 수는 없어, "
               "이미 화면에 있는 후보군 안에서만 찾습니다).")

    candidate_pool = {}
    for r in buy_rows:
        candidate_pool[r["stock_code"]] = r["stock_name"]
    if volume_rows:
        for r in volume_rows:
            candidate_pool[r["stock_code"]] = r["stock_name"]

    reversal_found = []
    for code, name in candidate_pool.items():
        if not code:
            continue
        rdf = fetch_daily_ohlcv(code)
        rlabel, rdesc, rfn = classify_trend_state(rdf)
        if rlabel in REVERSAL_LABELS:
            rprice, rpct, rflags = fetch_current_price(code)
            reversal_found.append({"code": code, "name": name, "label": rlabel, "desc": rdesc,
                                     "fn": rfn, "price": rprice, "pct": rpct, "flags": rflags})

    if not reversal_found:
        st.info("현재 후보군(순매수·거래량 상위) 안에는 하락추세 반등 패턴이 없습니다.")
    else:
        for item in reversal_found:
            title = f"{'🚨 ' if item['flags'] else ''}{item['name']}({item['code']}) — {item['label']}"
            with st.expander(title):
                if item["flags"]:
                    st.error("거래소 지정 상태: " + " · ".join(item["flags"]) +
                             " — 기술적 반등 모양이어도 이 상태면 통상적인 매매 판단이 적용되지 않습니다.")
                if item["price"] is not None:
                    st.markdown(f"현재가 {item['price']:,.0f}원 &nbsp; {colored_pct_html(item['pct'])}", unsafe_allow_html=True)
                item["fn"](f"**{item['label']}** — {item['desc']}")

                rrisky = check_disclosure_risk(item["code"])
                if rrisky:
                    st.error("⚠️ 최근 30일 내 주의 공시 발견:\n" + "\n".join(f"- {r}" for r in rrisky))
                elif DART_API_KEY:
                    st.success("최근 30일 내 주의 공시 없음")

                rnews, _ = fetch_news(item["name"])
                if rnews:
                    st.markdown("**관련 뉴스**")
                    for n in rnews:
                        st.markdown(f"- [{n['title']}]({n['link']})")

# ---------------- 🔍 종목 조회 ----------------
with tab_lookup:
    st.subheader("보유·관심 종목 직접 조회")
    st.caption("순매수 상위 10에 없는 종목도 조회 가능합니다 (예: 보유 중인 종목 점검용). 매수/매도 신호가 아니라 참고용 체크리스트입니다.")
    lookup_code = st.text_input("종목코드 입력 (예: 009150)", key="lookup_code")
    if lookup_code:
        with st.spinner("조회 중..."):
            lookup_df = fetch_daily_ohlcv(lookup_code)
            lookup_tech = analyze_technicals(lookup_df)
            lookup_price, lookup_pct, lookup_flags = fetch_current_price(lookup_code)
            lookup_risky = check_disclosure_risk(lookup_code)
            lookup_per, lookup_pbr, lookup_eps, lookup_bps, lookup_val_raw = fetch_valuation(lookup_code)
            lookup_fin = fetch_financial_ratio(lookup_code)

        if lookup_price is not None:
            st.markdown(f"### {lookup_price:,.0f}원 &nbsp; {colored_pct_html(lookup_pct)}", unsafe_allow_html=True)
            st.markdown(f"**상태: {price_status_badge(lookup_pct)}**")
            if lookup_flags:
                st.error("🚨 거래소 지정 상태: " + " · ".join(lookup_flags) +
                         " — 아래 전환신호·국면 판단은 이 상태를 반영하지 않으니 근거로 쓰지 마세요.")
        else:
            st.warning("현재가 조회 실패 — 종목코드를 확인해주세요.")

        st.markdown("**밸류에이션 (PER · PBR)**")
        if lookup_per is not None or lookup_pbr is not None:
            val_cols = st.columns(4)
            val_cols[0].metric("PER", f"{lookup_per:.2f}배" if lookup_per is not None else "N/A",
                                delta=(per_label(lookup_per) or None), delta_color="off")
            val_cols[1].metric("PBR", f"{lookup_pbr:.2f}배" if lookup_pbr is not None else "N/A",
                                delta=(pbr_label(lookup_pbr) or None), delta_color="off")
            val_cols[2].metric("EPS", f"{lookup_eps:,.0f}원" if lookup_eps is not None else "N/A")
            val_cols[3].metric("BPS", f"{lookup_bps:,.0f}원" if lookup_bps is not None else "N/A")
            st.caption("PBR 1배 미만이면 장부가치보다 싸게 거래 중이라는 뜻입니다. 다만 이것만으로 '저평가'라 단정할 순 없고, "
                       "동종업계 평균과 비교하거나 왜 싼지(실적 부진 등) 같이 확인하셔야 합니다. 옆 라벨은 일반적인 구간 "
                       "분류일 뿐이라 업종 특성(금융·건설·해운은 원래 PBR이 낮음)은 별도로 감안하세요.")
        else:
            st.caption("PER/PBR 조회 실패 (적자 기업은 PER이 제공되지 않을 수 있습니다).")
        with st.expander("원본 응답 확인 (필드명 검증용)"):
            st.json(lookup_val_raw)

        st.markdown("**재무 지표 (KIS 재무비율 · 최근 결산)**")
        if lookup_fin:
            lookup_score = composite_score(lookup_per, lookup_pbr, lookup_fin)
            fin_cols = st.columns(5)
            fin_cols[0].metric("ROE", f"{lookup_fin['roe']:.1f}%" if lookup_fin["roe"] is not None else "N/A",
                                delta=(roe_label(lookup_fin["roe"]) or None), delta_color="off")
            fin_cols[1].metric("부채비율", f"{lookup_fin['debt_ratio']:.0f}%" if lookup_fin["debt_ratio"] is not None else "N/A",
                                delta=(debt_label(lookup_fin["debt_ratio"]) or None), delta_color="off")
            fin_cols[2].metric("매출 증가율", f"{lookup_fin['sales_growth']:+.1f}%" if lookup_fin["sales_growth"] is not None else "N/A",
                                delta=(growth_label(lookup_fin["sales_growth"]) or None), delta_color="off")
            op_g = lookup_fin["op_growth"]
            fin_cols[3].metric("영업이익 증가율",
                                "0 (적자 관련?)" if op_g == 0 else (f"{op_g:+.1f}%" if op_g is not None else "N/A"),
                                delta=(growth_label(op_g) if op_g not in (None, 0) else None), delta_color="off")
            fin_cols[4].metric("종합점수", f"{lookup_score['score']:.0f}" if lookup_score["score"] is not None else "N/A",
                                delta=(score_label(lookup_score["score"]) if lookup_score["score"] is not None else None),
                                delta_color="off")
            st.caption(f"결산 {lookup_fin['stac_yymm']} 기준. 종합점수는 ROE·부채비율·성장률·PER/PBR을 구간 점수로 바꿔 가중합산한 "
                       "참고용 점수입니다(금융업 부채비율 같은 업종 특성은 반영되지 않음). 영업이익 증가율 0은 적자지속·흑자전환·"
                       "적자전환일 수 있어 점수에서 제외됩니다.")
            for n in lookup_score["notes"]:
                st.caption(f"⚠ {n}")
            with st.expander("종합점수 항목별 점수 / 재무비율 원본 응답 (검증용)"):
                st.write({SCORE_PART_NAMES[k]: v for k, v in lookup_score["parts"].items()})
                st.json(lookup_fin.get("raw", {}))
        else:
            st.caption("재무비율 조회 실패 (ETF·신규상장·결산 미공시 종목은 제공되지 않을 수 있습니다).")

        lookup_transition = compute_transition_signal(lookup_df)
        if lookup_transition:
            st.success("🎯 **전환신호 확인** — VCP(눌림) 이후 거래량급증+상승. "
                       "(2026-09-29 방향 조건 추가로 9/21 백테스트 재검증 전 — 참고용)")
        else:
            st.info("전환신호 없음 (조건 미충족 — 아래 지표는 참고용입니다)")

        lookup_checks = {k: v for k, v in lookup_tech.items() if k != "RSI값"}
        lookup_passed = sum(1 for v in lookup_checks.values() if v is True)
        lookup_total = sum(1 for v in lookup_checks.values() if v is not None)
        if lookup_total == 0:
            st.caption("일봉 데이터를 가져오지 못했습니다 (상장 60거래일 미만이거나 코드 오류일 수 있습니다).")
        else:
            rsi_note = f" (RSI: {lookup_tech['RSI값']})" if lookup_tech["RSI값"] is not None else ""
            st.markdown(f"**참고지표: {lookup_passed}/{lookup_total} 통과**{rsi_note} "
                        "— 효과가 확인되지 않은 지표들이라 판단 근거로 쓰지 마세요.")
            cols = st.columns(len(lookup_checks))
            for c, (label, val) in zip(cols, lookup_checks.items()):
                icon = "✅" if val is True else ("❌" if val is False else "—")
                c.metric(label, icon)

            st.markdown("**추세 판단 (규칙 기반 참고 의견)**")
            state_label, state_desc, state_fn = classify_trend_state(lookup_df)
            state_fn(f"**{state_label}** — {state_desc}")
            st.caption("이동평균선(5/20/60일) 배열과 RSI 흐름만으로 판단한 규칙 기반 해석이며, 매수/매도 신호가 아닙니다.")

            st.markdown("**볼린저밴드 (20일, ±2표준편차)**")
            bb_upper, bb_mid, bb_lower, bb_position = compute_bollinger(lookup_df)
            if bb_mid is not None:
                bb_cols = st.columns(3)
                bb_cols[0].metric("상단", f"{bb_upper:,.0f}원")
                bb_cols[1].metric("중심선", f"{bb_mid:,.0f}원")
                bb_cols[2].metric("하단", f"{bb_lower:,.0f}원")
                dist_to_mid = (lookup_price / bb_mid - 1) * 100 if lookup_price else None
                dist_str = f" (중심선 대비 {dist_to_mid:+.1f}%)" if dist_to_mid is not None else ""
                st.markdown(f"현재가 위치: **{bb_position}**{dist_str}")
                st.caption("일반적으로 중심선 지지 후 반등하면 상승 재개, 중심선을 하향 이탈하면 추가 조정 가능성으로 해석하는 경우가 많습니다 (참고용 해석입니다).")
            else:
                st.caption("데이터가 부족해 볼린저밴드를 계산할 수 없습니다.")

        if lookup_risky:
            st.error("⚠️ 최근 30일 내 주의 공시 발견:\n" + "\n".join(f"- {r}" for r in lookup_risky))
        elif DART_API_KEY:
            st.success("최근 30일 내 주의 공시 없음")

st.divider()
if st.button("지금 새로고침"):
    st.cache_data.clear()
    st.rerun()
