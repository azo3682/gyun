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

import base64
import hmac
import io
import json
import os
import re
import sqlite3
import time
import uuid
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

def _retry(fn, *args, attempts: int = 2, delay: float = 0.7, default=None):
    """fn(*args)를 시도해 성공하면 그 값을 돌려주고, 예외가 나면 잠깐 쉬었다 다시 시도한다. 끝내 실패하면 default.
    st.cache_data는 예외가 난 호출을 캐시하지 않는다 — 그래서 '실패'를 예외로 알리는 함수를 캐시에 넣고 이 헬퍼로 감싸면
    일시적인 조회 실패(호출 한도 초과 등)가 몇 분~몇 시간 동안 화면에 그대로 남는 일이 없다."""
    for i in range(attempts):
        try:
            return fn(*args)
        except Exception:
            if i < attempts - 1:
                time.sleep(delay)
    return default


def _fetch_daily_ohlcv_raw(stock_code: str) -> pd.DataFrame:
    """최근 약 4개월 일봉 데이터. 실패하면(빈 응답 포함) 예외를 던진다 — 캐시에 실패가 남지 않게 하려는 것.

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
    resp = requests.get(
        f"{BASE_URL}{DAILY_CHART_API_PATH}",
        headers=kis_headers(DAILY_CHART_TR_ID),
        params=params, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") not in (None, "0"):
        raise RuntimeError(data.get("msg1") or "일봉 조회 실패")
    rows = data.get("output2", [])
    if not rows:
        raise RuntimeError("일봉 응답이 비어 있음")
    df = pd.DataFrame(rows)
    df = df[df["stck_bsop_date"] != ""]
    for col in ["stck_clpr", "stck_oprc", "stck_hgpr", "stck_lwpr", "acml_vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_values("stck_bsop_date").reset_index(drop=True)


_fetch_daily_ohlcv_cached = st.cache_data(ttl=1800)(_fetch_daily_ohlcv_raw)


def fetch_daily_ohlcv(stock_code: str) -> pd.DataFrame:
    """최근 약 4개월 일봉. 성공한 결과만 30분 캐시하고 실패는 한 번 더 시도한다. 끝내 실패하면 빈 DataFrame."""
    return _retry(_fetch_daily_ohlcv_cached, stock_code, default=pd.DataFrame())


def _fetch_daily_ohlcv_uncached(stock_code: str) -> pd.DataFrame:
    try:
        return _fetch_daily_ohlcv_raw(stock_code)
    except Exception:
        return pd.DataFrame()


fetch_daily_ohlcv.__wrapped__ = _fetch_daily_ohlcv_uncached   # 호출부의 '캐시 우회 재시도'(스윙 후보 스크리닝)가 그대로 동작하도록


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


def _fetch_current_price_raw(stock_code: str):
    """(현재가, 전일대비 등락률%, 거래소 위험 플래그 리스트). 실패하거나 응답이 비어 있으면 예외를 던진다."""
    resp = requests.get(
        f"{BASE_URL}{CURRENT_PRICE_API_PATH}",
        headers=kis_headers(CURRENT_PRICE_TR_ID),
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1") or "현재가 조회 실패")
    output = data.get("output") or {}
    price = float(output.get("stck_prpr", 0) or 0)
    if price <= 0:
        # 값이 전부 0으로 오는 응답을 정상으로 취급하면 '시가총액 0억' 같은 가짜 관리종목 경고가 뜬다
        raise RuntimeError("현재가가 비어 있음")
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


_fetch_current_price_cached = st.cache_data(ttl=30)(_fetch_current_price_raw)


def fetch_current_price(stock_code: str):
    """(현재가, 전일대비 등락률%, 거래소 위험 플래그 리스트) 반환. 실패하면 (None, None, []). 실패는 캐시하지 않는다."""
    return _retry(_fetch_current_price_cached, stock_code, default=(None, None, []))


def _fetch_valuation_raw(stock_code: str):
    """(PER, PBR, EPS, BPS, 원본응답). 실패하거나 응답이 비어 있으면 예외를 던진다.
    같은 현재가 조회 API 안에 들어있는 값이라 별도 엔드포인트 승인 없이 바로 씀.
    필드명이 실제와 다를 수 있어 원본 응답도 같이 반환 — 화면에서 검증용으로 보여줌."""
    resp = requests.get(
        f"{BASE_URL}{CURRENT_PRICE_API_PATH}",
        headers=kis_headers(CURRENT_PRICE_TR_ID),
        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1") or "밸류에이션 조회 실패")
    output = data.get("output") or {}
    if not output:
        raise RuntimeError("응답이 비어 있음")

    def to_float(key):
        v = output.get(key)
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return to_float("per"), to_float("pbr"), to_float("eps"), to_float("bps"), output


_fetch_valuation_cached = st.cache_data(ttl=30)(_fetch_valuation_raw)


def fetch_valuation(stock_code: str):
    """(PER, PBR, EPS, BPS, 원본응답) 반환. 실패하면 전부 None / {}. 실패는 캐시하지 않는다."""
    return _retry(_fetch_valuation_cached, stock_code, default=(None, None, None, None, {}))


def _fetch_financial_ratio_raw(stock_code: str, period: str = "0"):
    """KIS 국내주식 재무비율(v1_국내주식-080, 실전 전용) — 최근 결산 기준 정식 재무비율 dict.
    실패하거나 데이터가 없으면 예외를 던진다(캐시에 실패가 6시간 남지 않게). ETF·신규상장은 데이터가 없어 이 경로로 빠진다.
    ROE(roe_val)·부채비율(lblt_rate)·매출/영업이익/순이익 증가율. 결산 데이터라 성공한 값은 6시간 캐시.
    output은 결산년월별 배열이라 이번 달 이하 중 가장 최근 결산을 고른다. 'raw'는 필드명 검증용.
    영업이익 증가율(bsop_prfi_inrt)은 적자지속/흑자전환/적자전환이면 0으로 오므로 0을 '성장 없음'으로 보면 안 된다."""
    resp = requests.get(
        f"{BASE_URL}/uapi/domestic-stock/v1/finance/financial-ratio",
        headers=kis_headers("FHKST66430300"),
        params={"FID_DIV_CLS_CODE": period, "fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1") or "재무비율 조회 실패")
    rows = data.get("output") or []
    if isinstance(rows, dict):
        rows = [rows]
    rows = [r for r in rows if isinstance(r, dict) and r.get("stac_yymm")]
    if not rows:
        raise RuntimeError("재무비율 데이터 없음")
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


_fetch_financial_ratio_cached = st.cache_data(ttl=6 * 3600)(_fetch_financial_ratio_raw)


def fetch_financial_ratio(stock_code: str, period: str = "0"):
    """최근 결산 기준 정식 재무비율 dict, 실패하거나 데이터가 없으면 None. 실패는 캐시하지 않는다."""
    return _retry(_fetch_financial_ratio_cached, stock_code, period, default=None)


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


# ============================================================
# 📌 신호 추적 (전환신호 + 거래량 상위 + 순매수 상위가 같은 날 겹친 종목의 신호일 종가 이후 추이)
#   기록은 scripts/signal_tracker.py가 매 거래일 15:40에 data/signal_tracker.json에 쌓는다.
# ============================================================
TRACKER_PATH = "data/signal_tracker.json"
_UP_STYLE = "background-color: rgba(224,49,49,0.16); color: #e03131; font-weight: 600;"
_DOWN_STYLE = "background-color: rgba(25,113,194,0.16); color: #1971c2; font-weight: 600;"


def build_tracker_table(signals: list, n_days: int):
    """행=신호 종목, 열=신호일 종가 + 최근 n_days 거래일의 종가. (DataFrame, 날짜 컬럼 이름 리스트) 반환.
    날짜 컬럼은 모든 종목이 같은 달력을 공유해서, 세로로 보면 '그날 종목들이 어땠는지'가 보인다."""
    all_dates = sorted({d for s in signals for d in (s.get("closes") or {})} | {s["signal_date"] for s in signals})
    shown = all_dates[-n_days:]
    labels = [d[5:] for d in shown]                     # 'MM-DD'
    rows = []
    for s in sorted(signals, key=lambda x: (x["signal_date"], x["code"]), reverse=True):
        base = s["signal_close"]
        closes = s.get("closes") or {}
        row = {"종목명": s["name"], "종목코드": s["code"], "신호일": s["signal_date"], "신호일종가": base,
               "다음날시가": s.get("entry_open"), "갭(%)": (s.get("perf") or {}).get("gap_pct")}
        for d, lab in zip(shown, labels):
            row[lab] = closes.get(d)                    # 신호일 이전·당일은 빈칸 (당일 종가는 '신호일종가' 열)
        rets = [(c / base - 1) * 100 for c in closes.values()] if base else []
        last = closes[max(closes)] if closes else None
        row["최근종가"] = last
        row["수익률(%)"] = (last / base - 1) * 100 if (last and base) else None
        row["최고(%)"] = max(rets) if rets else None
        row["최저(%)"] = min(rets) if rets else None
        row["경과(거래일)"] = len(closes)
        row["상태"] = "추적 중" if s.get("active", True) else "추적 종료"
        row["두 신호 겹침"] = "✔" if (s.get("conditions") or {}).get("overlap") else ""
        rows.append(row)
    df = pd.DataFrame(rows)
    num_cols = ["신호일종가", "다음날시가", "갭(%)", "최근종가", "수익률(%)", "최고(%)", "최저(%)"] + labels
    df[num_cols] = df[num_cols].apply(pd.to_numeric, errors="coerce")
    return df, labels


def style_tracker_table(df: pd.DataFrame, date_cols: list):
    """날짜별 종가 칸을 신호일 종가와 비교해 오르면 빨강, 내리면 파랑 배경으로 칠한다(한국식: 상승=빨강)."""
    def _row_style(row):
        base = row["신호일종가"]
        out = []
        for col in row.index:
            v = row[col]
            if col in date_cols and pd.notna(v) and pd.notna(base) and base:
                out.append(_UP_STYLE if v > base else _DOWN_STYLE if v < base else "")
            else:
                out.append("")
        return out

    def _ret_color(v):
        if pd.isna(v):
            return ""
        return "color: #e03131; font-weight: 600;" if v > 0 else "color: #1971c2; font-weight: 600;" if v < 0 else ""

    ret_cols = ["수익률(%)", "최고(%)", "최저(%)", "갭(%)"]
    styler = df.style.apply(_row_style, axis=1).map(_ret_color, subset=ret_cols)
    styler = styler.format(lambda v: "" if pd.isna(v) else f"{v:,.0f}", subset=["신호일종가", "다음날시가", "최근종가"] + date_cols)
    for c in ret_cols:
        styler = styler.format(lambda v: "—" if pd.isna(v) else f"{v:+.2f}%", subset=[c])
    return styler


# ---- 성과 (진입가 = 신호 다음 거래일 시가 기준, D+n = 신호일이 D+0일 때 n번째 거래일 종가) ----
PERF_HORIZONS = (1, 3, 5, 10)


def _signed_style(v):
    if pd.isna(v):
        return ""
    return "color: #e03131; font-weight: 600;" if v > 0 else "color: #1971c2; font-weight: 600;" if v < 0 else ""


def build_perf_table(signals: list) -> pd.DataFrame:
    """종목별 성과 표: 진입가, 갭, D+n 수익률(진입가 대비), D+n 지수 대비 초과수익(%p). 아직 오지 않은 날은 빈칸."""
    rows = []
    for s in sorted(signals, key=lambda x: (x["signal_date"], x["code"]), reverse=True):
        perf = s.get("perf") or {}
        ret, exc = perf.get("ret") or {}, perf.get("excess") or {}
        row = {"종목명": s["name"], "종목코드": s["code"], "신호일": s["signal_date"], "시장": s.get("index_key") or "—",
               "진입일": s.get("entry_date") or "", "진입가(다음날 시가)": s.get("entry_open"), "갭(%)": perf.get("gap_pct")}
        for n in PERF_HORIZONS:
            row[f"D+{n}(%)"] = ret.get(str(n))
        for n in PERF_HORIZONS:
            row[f"초과 D+{n}(%p)"] = exc.get(str(n))
        rows.append(row)
    df = pd.DataFrame(rows)
    num = [c for c in df.columns if c not in ("종목명", "종목코드", "신호일", "시장", "진입일")]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df


def style_perf_table(df: pd.DataFrame):
    signed = [c for c in df.columns if c.startswith("D+") or c.startswith("초과") or c == "갭(%)"]
    styler = df.style.map(_signed_style, subset=signed)
    styler = styler.format(lambda v: "" if pd.isna(v) else f"{v:,.0f}", subset=["진입가(다음날 시가)"])
    return styler.format(lambda v: "—" if pd.isna(v) else f"{v:+.2f}", subset=signed)


def summarize_performance(signals: list) -> pd.DataFrame:
    """D+n별 통계: 표본 수, 평균·중앙값, 승률, 평균 이익/손실, 손익비, 지수 대비 평균 초과수익과 지수를 이긴 비율."""
    rows = []
    for n in PERF_HORIZONS:
        key = str(n)
        rets = [(s.get("perf") or {}).get("ret", {}).get(key) for s in signals]
        rets = [r for r in rets if r is not None]
        exc = [(s.get("perf") or {}).get("excess", {}).get(key) for s in signals]
        exc = [e for e in exc if e is not None]
        wins, losses = [r for r in rets if r > 0], [r for r in rets if r < 0]
        avg_w = sum(wins) / len(wins) if wins else None
        avg_l = sum(losses) / len(losses) if losses else None
        rows.append({
            "보유기간": f"D+{n}", "표본 수": len(rets),
            "평균 수익률(%)": sum(rets) / len(rets) if rets else None,
            "중앙값(%)": float(pd.Series(rets).median()) if rets else None,
            "승률(%)": 100 * len(wins) / len(rets) if rets else None,
            "평균 이익(%)": avg_w, "평균 손실(%)": avg_l,
            "손익비": (avg_w / abs(avg_l)) if (avg_w is not None and avg_l) else None,
            "지수 비교 표본": len(exc),
            "평균 초과수익(%p)": sum(exc) / len(exc) if exc else None,
            "지수 이긴 비율(%)": 100 * sum(1 for e in exc if e > 0) / len(exc) if exc else None,
        })
    df = pd.DataFrame(rows)
    df[[c for c in df.columns if c != "보유기간"]] = df[[c for c in df.columns if c != "보유기간"]].apply(pd.to_numeric, errors="coerce")
    return df


def style_summary(df: pd.DataFrame):
    signed = ["평균 수익률(%)", "중앙값(%)", "평균 이익(%)", "평균 손실(%)", "평균 초과수익(%p)"]
    styler = df.style.map(_signed_style, subset=signed)
    styler = styler.format(lambda v: "—" if pd.isna(v) else f"{v:+.2f}", subset=signed)
    styler = styler.format(lambda v: "—" if pd.isna(v) else f"{v:.0f}", subset=["승률(%)", "지수 이긴 비율(%)"])
    styler = styler.format(lambda v: "—" if pd.isna(v) else f"{v:.2f}", subset=["손익비"])
    return styler.format(lambda v: f"{int(v)}", subset=["표본 수", "지수 비교 표본"])


# ============================================================
# 수급 (외국인 · 기관 · 개인) — 종목 조회 탭
#   - 일별: KIS '주식현재가 투자자'(v1_국내주식-012, FHKST01010900) — 개인·외국인·기관 순매수 수량(주) 일별.
#     명세: 당일 데이터는 장 종료 후 제공. 외국인 = 외국인(투자등록 고유번호가 있는 경우) + 기타 외국인.
#   - 장중: KIS '종목별 외인기관 추정가집계'(v1_국내주식-046, HHPTJ04160200, 실전 전용) — 외국인·기관만(개인 없음).
#     증권사 직원이 장중에 집계·입력한 값의 단순 누계. 입력 시각은 외국인 09:30·11:20·13:20·14:30, 기관 10:00·11:20·13:20·14:30.
#     응답에 날짜가 없다.
# ============================================================
INVESTOR_DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"
INVESTOR_DAILY_TR_ID = "FHKST01010900"
INVESTOR_EST_PATH = "/uapi/domestic-stock/v1/quotations/investor-trend-estimate"
INVESTOR_EST_TR_ID = "HHPTJ04160200"
EST_SLOT_LABELS = {"1": "09:30", "2": "10:00", "3": "11:20", "4": "13:20", "5": "14:30"}


def _to_int(v):
    try:
        t = str(v).replace(",", "").strip()
        return int(float(t)) if t not in ("", "None") else None
    except (TypeError, ValueError):
        return None


def parse_investor_daily(output) -> list:
    """주식현재가 투자자 응답의 output 배열 → [{date, close, prsn, frgn, orgn}] 최신순. 날짜가 없는 행은 버린다.
    prsn=개인, frgn=외국인, orgn=기관계 순매수 수량(주, 음수면 순매도)."""
    if isinstance(output, dict):
        output = [output]
    rows = []
    for r in output or []:
        if not isinstance(r, dict):
            continue
        d = str(r.get("stck_bsop_date") or "").strip()
        if len(d) != 8:
            continue
        rows.append({"date": d, "close": _to_int(r.get("stck_clpr")), "prsn": _to_int(r.get("prsn_ntby_qty")),
                     "frgn": _to_int(r.get("frgn_ntby_qty")), "orgn": _to_int(r.get("orgn_ntby_qty"))})
    rows.sort(key=lambda x: x["date"], reverse=True)
    return rows


def parse_investor_estimate(output2) -> list:
    """추정가집계 응답의 output2 배열 → [{slot, time, frgn, orgn, sum}] 입력 순서대로(09:30→14:30)."""
    if isinstance(output2, dict):
        output2 = [output2]
    rows = []
    for r in output2 or []:
        if not isinstance(r, dict):
            continue
        gb = str(r.get("bsop_hour_gb") or "").strip()
        if gb not in EST_SLOT_LABELS:
            continue
        rows.append({"slot": gb, "time": EST_SLOT_LABELS[gb], "frgn": _to_int(r.get("frgn_fake_ntby_qty")),
                     "orgn": _to_int(r.get("orgn_fake_ntby_qty")), "sum": _to_int(r.get("sum_fake_ntby_qty"))})
    rows.sort(key=lambda x: x["slot"])
    return rows


def investor_row_pending(row: dict, today: str) -> bool:
    """오늘 행인데 세 투자자 값이 모두 비었거나 0이면 아직 마감 후 제공 전인 행으로 본다."""
    return row["date"] == today and all(row.get(k) in (None, 0) for k in ("prsn", "frgn", "orgn"))


def investor_streak(rows: list, key: str) -> int:
    """rows(최신순, 미확정 행 제외)에서 key 투자자의 연속 순매수(+n) / 순매도(-n) 일수. 값이 없거나 0이면 0."""
    streak = 0
    for r in rows:
        v = r.get(key)
        if v is None or v == 0:
            break
        sign = 1 if v > 0 else -1
        if streak == 0:
            streak = sign
        elif (streak > 0) == (sign > 0):
            streak += sign
        else:
            break
    return streak


def investor_recent_sum(rows: list, key: str, n: int = 5):
    vals = [r[key] for r in rows[:n] if r.get(key) is not None]
    return sum(vals) if vals else None


def _investor_daily_raw(stock_code: str) -> list:
    resp = requests.get(f"{BASE_URL}{INVESTOR_DAILY_PATH}", headers=kis_headers(INVESTOR_DAILY_TR_ID),
                        params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1") or "수급 조회 실패")
    rows = parse_investor_daily(data.get("output"))
    if not rows:
        raise RuntimeError("수급 데이터가 비어 있음")
    return rows


def _investor_estimate_raw(stock_code: str) -> list:
    resp = requests.get(f"{BASE_URL}{INVESTOR_EST_PATH}", headers=kis_headers(INVESTOR_EST_TR_ID),
                        params={"MKSC_SHRN_ISCD": stock_code}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(data.get("msg1") or "장중 추정 수급 조회 실패")
    return parse_investor_estimate(data.get("output2"))      # 아직 입력 전이면 빈 목록(정상)


_investor_daily_cached = st.cache_data(ttl=300)(_investor_daily_raw)
_investor_estimate_cached = st.cache_data(ttl=120)(_investor_estimate_raw)


def fetch_investor_daily(stock_code: str):
    """일별 개인·외국인·기관 순매수 목록(최신순). 실패하면 None. 실패는 캐시하지 않는다."""
    return _retry(_investor_daily_cached, stock_code, default=None)


def fetch_investor_estimate(stock_code: str):
    """장중 외국인·기관 추정 집계 목록(입력 순서). 아직 입력이 없으면 [], 실패하면 None."""
    return _retry(_investor_estimate_cached, stock_code, default=None)


def _streak_text(n: int) -> str:
    return "—" if not n else f"{abs(n)}일 연속 {'순매수' if n > 0 else '순매도'}"


def _qty_style(df: pd.DataFrame, cols: list):
    styler = df.style.map(_signed_style, subset=cols)
    return styler.format(lambda v: "—" if pd.isna(v) else fmt_shares(v), subset=cols)


def render_investor_section(est, inv):
    """종목 조회 탭의 수급 섹션. est=fetch_investor_estimate 결과, inv=fetch_investor_daily 결과."""
    now = datetime.now(KST)
    today = now.strftime("%Y%m%d")
    st.markdown("**수급 (외국인 · 기관 · 개인)**")

    # ---- 오늘 장중 추정 (외국인·기관) ----
    st.markdown("오늘 장중 추정 — 외국인·기관")
    if est is None:
        st.caption("장중 추정 수급을 가져오지 못했습니다 (일시적인 조회 실패일 수 있어요 — 잠시 후 다시 시도하세요).")
    elif not est:
        st.caption("아직 입력된 추정 집계가 없습니다 (첫 입력은 09:30 무렵).")
    else:
        edf = pd.DataFrame([{"입력 시각": r["time"], "외국인(주)": r["frgn"], "기관(주)": r["orgn"], "합산(주)": r["sum"]}
                            for r in est])
        edf[["외국인(주)", "기관(주)", "합산(주)"]] = edf[["외국인(주)", "기관(주)", "합산(주)"]].apply(pd.to_numeric, errors="coerce")
        st.dataframe(_qty_style(edf, ["외국인(주)", "기관(주)", "합산(주)"]), use_container_width=True, hide_index=True)
    st.caption("증권사 직원이 장중에 집계·입력한 값을 단순 누계한 추정치입니다. 입력 시각은 외국인 09:30·11:20·13:20·14:30, "
               "기관 10:00·11:20·13:20·14:30이고 사정에 따라 바뀔 수 있어요. 개인은 제공되지 않습니다. "
               "응답에 날짜가 없어서 이른 시간에는 전일 값이 보일 수 있고, 0은 아직 입력 전일 수 있습니다.")

    # ---- 일별 (개인·외국인·기관) ----
    st.markdown("일별 순매수 — 개인·외국인·기관")
    if inv is None:
        st.caption("일별 수급을 가져오지 못했습니다 (일시적인 조회 실패일 수 있어요 — 잠시 후 다시 시도하세요).")
        return
    valid = [r for r in inv if not investor_row_pending(r, today)]
    pending_today = any(investor_row_pending(r, today) for r in inv)
    m = st.columns(5)
    m[0].metric("외국인", _streak_text(investor_streak(valid, "frgn")))
    m[1].metric("기관", _streak_text(investor_streak(valid, "orgn")))
    m[2].metric("개인", _streak_text(investor_streak(valid, "prsn")))
    n_recent = min(5, len(valid))
    for col, label, key in ((m[3], "외국인", "frgn"), (m[4], "기관", "orgn")):
        total = investor_recent_sum(valid, key, 5)
        col.metric(f"{label} 최근 {n_recent}일 누적", fmt_shares(total) if total is not None else "—")
    rows = []
    for r in inv[:10]:
        pend = investor_row_pending(r, today)
        d = f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:]}"
        rows.append({"일자": d + (" (마감 후 제공)" if pend else ""), "종가": r["close"],
                     "개인(주)": None if pend else r["prsn"], "외국인(주)": None if pend else r["frgn"],
                     "기관(주)": None if pend else r["orgn"]})
    ddf = pd.DataFrame(rows)
    ddf[["종가", "개인(주)", "외국인(주)", "기관(주)"]] = ddf[["종가", "개인(주)", "외국인(주)", "기관(주)"]].apply(pd.to_numeric, errors="coerce")
    styler = _qty_style(ddf, ["개인(주)", "외국인(주)", "기관(주)"]).format(
        lambda v: "" if pd.isna(v) else f"{v:,.0f}", subset=["종가"])
    st.dataframe(styler, use_container_width=True, hide_index=True)
    st.caption("KIS '주식현재가 투자자' 기준 순매수 수량(주)이며, 빨강은 순매수·파랑은 순매도입니다. 외국인은 외국인(투자등록 고유번호가 있는 경우)과 "
               "기타 외국인을 합친 값이고, 연속 일수는 마감이 확정된 날만 셉니다. "
               + ("오늘 값은 장 종료 후 제공돼서 지금은 어제까지의 값입니다." if pending_today else
                  "당일 데이터는 장 종료 후 제공됩니다."))


def render_tracker_section(signals: list, n_days: int, only_active: bool, empty_msg: str):
    """신호 추적 표 한 벌(요약 지표 + 종가 표 + 발생 당시 조건)을 그린다. 종류별 하위 탭에서 각각 호출한다."""
    if not signals:
        st.info(empty_msg)
        return
    shown_signals = [s for s in signals if s.get("active", True)] if only_active else signals
    if not shown_signals:
        st.info("추적 중인 신호가 없습니다.")
        return
    tdf, tdate_cols = build_tracker_table(shown_signals, n_days)
    measured = tdf["수익률(%)"].dropna()
    m = st.columns(5)
    m[0].metric("기록된 신호", f"{len(tdf)}건")
    m[1].metric("신호일 종가보다 위", f"{int((measured > 0).sum())}건")
    m[2].metric("신호일 종가보다 아래", f"{int((measured < 0).sum())}건")
    m[3].metric("평균 수익률", f"{measured.mean():+.2f}%" if len(measured) else "—")
    m[4].metric("마지막 종가 기록일", max((max(s["closes"]) for s in shown_signals if s.get("closes")), default="—"))
    st.dataframe(style_tracker_table(tdf, tdate_cols), use_container_width=True, hide_index=True)
    st.caption("이 표의 수익률·최고·최저는 신호일 종가 대비이며 수수료·세금·슬리피지는 반영하지 않았습니다. "
               "'다음날시가'는 신호 다음 거래일의 시가(현실적인 진입가)이고 '갭'은 신호일 종가 대비 그 시가의 변동률입니다. "
               "신호일 종가는 수정주가 기준으로 갱신될 수 있어 최초 기록값과 다를 수 있습니다(액면분할 등). "
               "'두 신호 겹침'은 같은 날 다른 종류의 신호에도 해당했다는 표시입니다.")

    # ---- 성과 요약: 진입가(다음날 시가) 기준. 추적 중만 보기와 상관없이 이 종류의 전체 신호로 계산 ----
    st.markdown("##### 📊 성과 요약 (진입가 = 신호 다음 거래일 시가)")
    summ = summarize_performance(signals)
    if int(summ["표본 수"].sum()) == 0:
        st.info("아직 신호 다음 거래일이 지난 신호가 없어 성과 통계가 비어 있습니다. 다음 거래일 15:40 이후부터 채워집니다.")
    else:
        st.dataframe(style_summary(summ), use_container_width=True, hide_index=True)
        best_n = int(summ["표본 수"].max())
        if best_n < 30:
            st.warning(f"표본이 가장 많은 구간도 {best_n}건뿐입니다. 30건 미만이면 평균·승률은 우연에 크게 좌우되므로 "
                       "결론으로 삼지 마세요(신호가 몇 번 통했다/안 통했다는 이야기일 뿐입니다).")
        gaps = [(s.get("perf") or {}).get("gap_pct") for s in signals]
        gaps = [g for g in gaps if g is not None]
        if gaps:
            st.caption(f"신호일 종가 → 다음날 시가 갭: 평균 {sum(gaps) / len(gaps):+.2f}% "
                       f"(갭상승 {sum(1 for g in gaps if g > 0)}건 / 갭하락 {sum(1 for g in gaps if g < 0)}건 / "
                       f"보합 {sum(1 for g in gaps if g == 0)}건, 표본 {len(gaps)}건). "
                       "갭이 크면 신호일 종가는 현실에서 살 수 없는 가격이었다는 뜻입니다.")
    st.caption("D+n은 신호일을 D+0으로 봤을 때 n번째 거래일 종가이며, 수익률은 진입가(D+1 시가) 대비입니다(D+1은 진입일 당일 시가→종가). "
               "'지수 대비 초과수익'은 같은 기간 종목이 속한 시장(코스피/코스닥) 지수의 수익률을 뺀 값(%p)이고, 지수는 야후 파이낸스(yfinance) 비공식 "
               "데이터라 받지 못한 날은 비어 있을 수 있습니다(지수 시가가 없으면 신호일 지수 종가를 기준으로 대체). "
               "승률은 진입가 대비 수익률이 0보다 큰 비율입니다. 수수료·세금·슬리피지, 시가에 실제로 체결되는지는 반영하지 않았습니다.")
    st.markdown("##### 🧾 종목별 성과 (D+n · 진입가 기준)")
    st.dataframe(style_perf_table(build_perf_table(shown_signals)), use_container_width=True, hide_index=True)
    with st.expander("신호 발생 당시 조건 상세"):
        detail = pd.DataFrame([{
            "종목명": s["name"], "종목코드": s["code"], "신호일": s["signal_date"],
            "순매수 순위": (s.get("conditions") or {}).get("buy_rank"),
            "거래량 순위": (s.get("conditions") or {}).get("volume_rank"),
            "신호일 등락률(%)": s.get("day_pct"),
            "최초 기록 종가": s.get("signal_close_first"),
            "거래소 위험 표시": ", ".join((s.get("conditions") or {}).get("risk_flags") or []),
        } for s in sorted(shown_signals, key=lambda x: (x["signal_date"], x["code"]), reverse=True)])
        st.dataframe(detail, use_container_width=True, hide_index=True)


# ============================================================
# 📒 매매 일지 — 입력·계산·통계는 앱에서, 저장은 '비공개' GitHub 저장소의 journal.json에 한다.
#   (배포된 앱의 로컬 파일은 재시작하면 사라지고, 이 코드 저장소는 공개라서 일지를 여기에 두면 안 된다.)
#   필요한 Secrets: JOURNAL_GITHUB_TOKEN(그 저장소 한 곳에만 Contents 읽기/쓰기 권한), JOURNAL_REPO('계정/저장소'),
#                    JOURNAL_PASSWORD(탭을 여는 비밀번호). 선택: JOURNAL_PATH(기본 journal.json), JOURNAL_BRANCH.
# ============================================================
JOURNAL_SIGNAL_OPTIONS = ["순매수 상위", "거래량 상위", "동시 등장", "전환신호", "저평가 후보", "외국인 지속 매수", "기타"]
JOURNAL_EXIT_TYPES = ["(선택 안 함)", "목표 도달", "손절", "신호 소멸·수급 이탈", "추세 판단(눌림 예상 등)", "기타"]
GITHUB_API = "https://api.github.com"


def _secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default) or default)
    except Exception:
        return default


def journal_settings() -> dict:
    return {"repo": _secret("JOURNAL_REPO"), "token": _secret("JOURNAL_GITHUB_TOKEN"),
            "password": _secret("JOURNAL_PASSWORD"), "path": _secret("JOURNAL_PATH", "journal.json"),
            "branch": _secret("JOURNAL_BRANCH") or None}


# ---- 입력 파싱 ----
def _parse_journal_date(tok: str, today) -> str | None:
    """'2026-09-28', '2026.9.28', '20260928', '9.28', '9/28' → 'YYYY-MM-DD'. 연도가 없으면 올해(오늘보다 미래면 작년). 실패하면 None."""
    t = tok.strip()
    m = re.fullmatch(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", t) or re.fullmatch(r"(\d{4})(\d{2})(\d{2})", t)
    try:
        if m:
            y, mo, d = map(int, m.groups())
            return datetime(y, mo, d).date().isoformat()
        m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})", t)
        if not m:
            return None
        mo, d = map(int, m.groups())
        cand = datetime(today.year, mo, d).date()
        if cand > today + timedelta(days=1):
            cand = datetime(today.year - 1, mo, d).date()
        return cand.isoformat()
    except ValueError:
        return None


def _to_number(tok: str):
    t = re.sub(r"[,\s]", "", tok).rstrip("원주")
    return float(t) if re.fullmatch(r"\d+(\.\d+)?", t) else None


def parse_trade_lines(text: str, today=None):
    """한 줄에 '날짜 가격 수량'(공백 구분). 가격의 쉼표·'원', 수량의 '주'는 무시한다. (rows, errors) 반환."""
    today = today or datetime.now(KST).date()
    rows, errors = [], []
    for i, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3:
            errors.append(f"{i}번째 줄: '날짜 가격 수량' 세 값이 필요합니다 → {line}")
            continue
        d, price, qty = _parse_journal_date(parts[0], today), _to_number(parts[1]), _to_number(parts[2])
        if d is None:
            errors.append(f"{i}번째 줄: 날짜를 읽지 못했습니다 → {parts[0]}")
        elif not price or price <= 0:
            errors.append(f"{i}번째 줄: 가격이 올바르지 않습니다 → {parts[1]}")
        elif not qty or qty <= 0 or qty != int(qty):
            errors.append(f"{i}번째 줄: 수량은 1 이상의 정수여야 합니다 → {parts[2]}")
        else:
            rows.append({"date": d, "price": int(price) if price == int(price) else price, "qty": int(qty)})
    return rows, errors


def lines_from_rows(rows: list) -> str:
    return "\n".join(f"{r['date']} {r['price']} {r['qty']}" for r in rows)


# ---- 계산 ----
def compute_trade(t: dict, today=None) -> dict:
    """평균 매수/매도가, 실현손익(수수료·세금 반영), 수익률, 보유 기간, 상태.
    실현손익 = 매도금액 - 평균 매수가 × 매도 수량 - 수수료·세금(입력한 전액).
    수익률 = 실현손익 ÷ (평균 매수가 × 매도 수량). 일부만 매도했으면 매도한 수량 기준이다."""
    today = today or datetime.now(KST).date()
    buys, sells = t.get("buys") or [], t.get("sells") or []
    buy_qty = sum(b["qty"] for b in buys)
    buy_cost = sum(b["price"] * b["qty"] for b in buys)
    sell_qty = sum(s["qty"] for s in sells)
    sell_amt = sum(s["price"] * s["qty"] for s in sells)
    avg_buy = buy_cost / buy_qty if buy_qty else None
    avg_sell = sell_amt / sell_qty if sell_qty else None
    costs = float(t.get("costs") or 0)
    first_buy = min((b["date"] for b in buys), default=None)
    last_sell = max((s["date"] for s in sells), default=None)
    out = {"avg_buy": avg_buy, "buy_qty": buy_qty, "avg_sell": avg_sell, "sell_qty": sell_qty,
           "remaining": buy_qty - sell_qty, "costs": costs, "first_buy": first_buy, "last_sell": last_sell,
           "gross_pl": None, "net_pl": None, "ret_pct": None, "hold_days": None}
    if not sells:
        out["status"] = "보유 중"
    else:
        out["status"] = "마감" if out["remaining"] <= 0 else "일부 매도"
    if sell_qty and avg_buy:
        sold_cost = avg_buy * sell_qty
        out["gross_pl"] = sell_amt - sold_cost
        out["net_pl"] = out["gross_pl"] - costs
        out["ret_pct"] = out["net_pl"] / sold_cost * 100
    if first_buy:
        end = datetime.fromisoformat(last_sell).date() if (last_sell and out["status"] == "마감") else today
        out["hold_days"] = (end - datetime.fromisoformat(first_buy).date()).days
    return out


def summarize_trades(trades: list) -> dict:
    """마감된(전량 매도) 매매만의 통계."""
    rows = [c for c in (compute_trade(t) for t in trades) if c["status"] == "마감" and c["ret_pct"] is not None]
    rets = [c["ret_pct"] for c in rows]
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r < 0]
    avg_w = sum(wins) / len(wins) if wins else None
    avg_l = sum(losses) / len(losses) if losses else None
    return {"n": len(rows), "win_rate": 100 * len(wins) / len(rets) if rets else None,
            "avg_ret": sum(rets) / len(rets) if rets else None,
            "median_ret": float(pd.Series(rets).median()) if rets else None,
            "avg_win": avg_w, "avg_loss": avg_l, "pl_ratio": (avg_w / abs(avg_l)) if (avg_w is not None and avg_l) else None,
            "total_net_pl": sum(c["net_pl"] for c in rows) if rows else None}


def summarize_journal_by(trades: list, field: str) -> pd.DataFrame:
    """마감된 매매를 field(signals: 태그 목록 / exit_type: 문자열)별로 묶은 통계. 태그가 여러 개면 각각에 센다."""
    groups = {}
    for t in trades:
        c = compute_trade(t)
        if c["status"] != "마감" or c["ret_pct"] is None:
            continue
        v = t.get(field)
        keys = v if isinstance(v, list) else ([v] if v and v != JOURNAL_EXIT_TYPES[0] else [])
        for k in keys:
            groups.setdefault(k, []).append(c)
    rows = []
    for k, cs in groups.items():
        rets = [c["ret_pct"] for c in cs]
        rows.append({"구분": k, "매매 수": len(cs), "승률(%)": 100 * sum(1 for r in rets if r > 0) / len(rets),
                     "평균 수익률(%)": sum(rets) / len(rets), "총 실현손익(원)": sum(c["net_pl"] for c in cs)})
    return pd.DataFrame(rows, columns=["구분", "매매 수", "승률(%)", "평균 수익률(%)", "총 실현손익(원)"]).sort_values("매매 수", ascending=False, ignore_index=True)


def build_journal_table(trades: list) -> pd.DataFrame:
    rows = []
    for t in sorted(trades, key=lambda x: min((b["date"] for b in x.get("buys") or []), default=""), reverse=True):
        c = compute_trade(t)
        rows.append({"종목명": t["name"], "상태": c["status"], "매수일": c["first_buy"], "평균매수가": c["avg_buy"],
                     "매수수량": c["buy_qty"], "매도일": c["last_sell"], "평균매도가": c["avg_sell"],
                     "매도수량": c["sell_qty"], "남은수량": c["remaining"], "실현손익(원)": c["net_pl"],
                     "수익률(%)": c["ret_pct"], "보유(일)": c["hold_days"],
                     "매수 근거 신호": ", ".join(t.get("signals") or []), "청산 유형": t.get("exit_type") or ""})
    df = pd.DataFrame(rows)
    if len(df):
        num = ["평균매수가", "매수수량", "평균매도가", "매도수량", "남은수량", "실현손익(원)", "수익률(%)", "보유(일)"]
        df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df


# ---- 일지 ↔ 신호 추적 연결 ----
# 일지의 매매 하나에 신호 추적의 신호 기록(종목코드 + 신호 종류 + 신호일)을 연결해 두면,
# 신호 다음 거래일 시가·D+n 성과와 내가 실제로 한 매매를 나란히 비교할 수 있다.
SIGNAL_TYPE_LABELS = {"transition": "전환신호", "volume_supply": "거래량·수급 동시"}
SIGNAL_TAG_BY_TYPE = {"transition": "전환신호", "volume_supply": "동시 등장"}     # 연결한 신호 종류 → '매수 근거 신호' 태그


def signal_ref_key(code, typ, date) -> str:
    return f"{code}|{typ}|{date}"


def parse_signal_ref_key(key):
    parts = str(key).split("|")
    if len(parts) != 3 or not all(parts):
        return None
    return {"code": parts[0], "type": parts[1], "signal_date": parts[2]}


@st.cache_data(ttl=60)
def load_tracker_signals() -> list:
    """신호 추적 파일(data/signal_tracker.json)의 신호 목록. 파일이 없거나 깨졌으면 빈 목록."""
    if not os.path.exists(TRACKER_PATH):
        return []
    try:
        with open(TRACKER_PATH, "r", encoding="utf-8") as f:
            sigs = json.load(f).get("signals")
        return sigs if isinstance(sigs, list) else []
    except Exception:
        return []


def signal_candidates(signals: list, name: str = "", code: str = "", limit: int = 30) -> list:
    """연결할 수 있는 신호 후보. 종목코드가 맞는 기록을 먼저 보고, 없으면 종목명으로 찾는다.
    코드·종목명이 둘 다 비었으면 최근 신호를 보여 준다. 신호일 최신순."""
    code, name = (code or "").strip(), (name or "").strip()
    rows = [s for s in signals if code and s.get("code") == code]
    if not rows and name:
        rows = [s for s in signals if s.get("name") == name]
    if not rows and not code and not name:
        rows = list(signals)
    rows.sort(key=lambda s: (s.get("signal_date", ""), s.get("type", "")), reverse=True)
    return rows[:limit]


def find_signal(signals: list, ref: dict):
    for s in signals:
        if (s.get("code") == ref.get("code") and s.get("type") == ref.get("type")
                and s.get("signal_date") == ref.get("signal_date")):
            return s
    return None


def _pct_change(a, b):
    return (a / b - 1) * 100 if (a and b) else None


def compare_trade_with_signal(t: dict, sig, today=None) -> dict:
    """내 매매 하나를 연결된 신호 기록과 비교한다. 계산할 수 없는 값은 None.
    - buy_vs_open / buy_vs_close: 내 평균 매수가가 신호 다음날 시가 / 신호일 종가보다 몇 % 높았나(음수면 더 싸게 산 것)
    - my_ret: 내 실현 수익률(수수료·세금 반영, 매도한 수량 기준)
    - sig_ret_exit: 신호 다음날 시가에 사서 '내가 마지막으로 판 날'의 종가에 팔았다면의 수익률(비용 미반영)
    - diff: my_ret - sig_ret_exit (%p). 내 매도가는 장중 체결가라 종가와 다를 수 있고, 두 수익률의 비용 처리도 달라 근사 비교다."""
    c = compute_trade(t, today)
    out = {"name": t.get("name", ""), "status": c["status"], "my_avg_buy": c["avg_buy"], "my_ret": c["ret_pct"],
           "signal_found": sig is not None, "type_label": "", "signal_date": "", "timing": "", "signal_close": None,
           "entry_open": None, "buy_vs_open": None, "buy_vs_close": None, "sig_ret_exit": None, "diff": None,
           "sig_latest_ret": None, "d1": None, "d3": None, "d5": None, "d10": None, "note": ""}
    if sig is None:
        out["note"] = "신호 추적에 기록 없음"
        return out
    sdate, entry_date = sig.get("signal_date", ""), sig.get("entry_date")
    entry_open, closes = sig.get("entry_open"), sig.get("closes") or {}
    out.update(type_label=SIGNAL_TYPE_LABELS.get(sig.get("type"), sig.get("type", "")), signal_date=sdate,
               signal_close=sig.get("signal_close"), entry_open=entry_open)
    fb = c["first_buy"]
    if fb:
        out["timing"] = ("신호 전" if fb < sdate else "신호일" if fb == sdate else
                         "다음 거래일" if (entry_date and fb == entry_date) else "그 이후")
    out["buy_vs_open"] = _pct_change(c["avg_buy"], entry_open)
    out["buy_vs_close"] = _pct_change(c["avg_buy"], sig.get("signal_close"))
    if entry_open and c["last_sell"] and c["last_sell"] in closes:
        out["sig_ret_exit"] = _pct_change(closes[c["last_sell"]], entry_open)
        if out["my_ret"] is not None and out["sig_ret_exit"] is not None:
            out["diff"] = out["my_ret"] - out["sig_ret_exit"]
    if entry_open and closes:
        out["sig_latest_ret"] = _pct_change(closes[max(closes)], entry_open)
    perf_ret = (sig.get("perf") or {}).get("ret") or {}
    for n in (1, 3, 5, 10):
        out[f"d{n}"] = perf_ret.get(str(n))
    notes = []
    if not entry_open:
        notes.append("다음날 시가 대기 중 (신호 다음 거래일 15:40 이후 채워져요)")
    elif c["status"] == "보유 중":
        notes.append("아직 매도 전" + (f" · 신호 기준 현재 {out['sig_latest_ret']:+.2f}%" if out["sig_latest_ret"] is not None else ""))
    elif c["last_sell"] and out["sig_ret_exit"] is None:
        notes.append("내 매도일 종가 대기 중 (15:40 이후 채워져요)")
    out["note"] = " · ".join(notes)
    return out


def build_signal_compare_rows(trades: list, signals: list, today=None) -> list:
    """신호를 연결한 매매마다, 연결한 신호 하나당 한 줄. 최근 매수일 순."""
    rows = []
    for t in sorted(trades, key=lambda x: min((b["date"] for b in x.get("buys") or []), default=""), reverse=True):
        for ref in t.get("signal_refs") or []:
            rows.append(compare_trade_with_signal(t, find_signal(signals, ref), today))
            if rows[-1]["signal_date"] == "":
                rows[-1]["signal_date"] = ref.get("signal_date", "")
                rows[-1]["type_label"] = SIGNAL_TYPE_LABELS.get(ref.get("type"), ref.get("type", ""))
    return rows


def summarize_signal_comparison(rows: list) -> dict:
    avg = lambda xs: (sum(xs) / len(xs)) if xs else None
    both = [r for r in rows if r["diff"] is not None]
    return {
        "n_rows": len(rows), "n_signal_found": sum(1 for r in rows if r["signal_found"]),
        "avg_buy_vs_open": avg([r["buy_vs_open"] for r in rows if r["buy_vs_open"] is not None]),
        "n_both": len(both), "avg_my_ret": avg([r["my_ret"] for r in both]),
        "avg_sig_ret": avg([r["sig_ret_exit"] for r in both]), "avg_diff": avg([r["diff"] for r in both]),
        "n_better": sum(1 for r in both if r["diff"] > 0),
    }


# ---- 내 매매 이후 가격 추이 ----
# 일지에서 매매를 고르면 내 매수가·매도가와, 첫 매수일부터 이후 영업일별 가격(시가·고가·저가·종가)을 한 표로 보여 준다.
WEEKDAYS_KO = "월화수목금토일"


def resolve_trade_code(trade: dict, trades: list, signals: list) -> str:
    """종목코드 찾기: 일지에 적은 코드 → 연결한 신호의 코드 → 같은 이름의 다른 매매에 적은 코드 → 신호 추적의 같은 이름 기록."""
    code = (trade.get("code") or "").strip()
    if code:
        return code
    for ref in trade.get("signal_refs") or []:
        if ref.get("code"):
            return ref["code"]
    name = trade.get("name", "")
    for other in trades:
        if other.get("name") == name and (other.get("code") or "").strip():
            return other["code"].strip()
    for s in signals:
        if s.get("name") == name and s.get("code"):
            return s["code"]
    return ""


def _nz(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def ohlcv_rows_from_df(df) -> list:
    """일봉 DataFrame(stck_bsop_date·stck_clpr 등) → [{date, open, high, low, close}] 날짜 오름차순. 비어 있으면 []."""
    if df is None or len(df) == 0 or "stck_bsop_date" not in df.columns:
        return []
    rows = []
    for _, r in df.iterrows():
        d = str(r["stck_bsop_date"]).strip()
        if len(d) != 8:
            continue
        rows.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "open": _nz(r.get("stck_oprc")), "high": _nz(r.get("stck_hgpr")),
                     "low": _nz(r.get("stck_lwpr")), "close": _nz(r.get("stck_clpr"))})
    rows.sort(key=lambda x: x["date"])
    return rows


def trade_price_path(trade: dict, ohlcv: list, now=None) -> dict:
    """선택한 매매의 첫 매수일부터 이후 영업일별 가격 표와 요약.
    - rows: 영업일마다 시가·고가·저가·종가, 전일대비, 종가↔내 평균매수가(%), 종가↔내 평균매도가(%, 첫 매도일부터), 그날 내 체결
    - after_buy: 첫 매수일 이후 종가의 최고·최저(내 평균매수가 대비)
    - after_sell: 마지막 매도일 이후(그날 제외)의 최고·최저·최근 종가(내 평균매도가 대비), 매도가보다 종가가 높았던 날 수
    - data_gap: 첫 매수일이 받은 일봉 범위보다 앞이면 True (앞부분이 비어 있다는 뜻)"""
    now = now or datetime.now(KST)
    c = compute_trade(trade, now.date())
    avg_buy, avg_sell, first_buy, last_sell = c["avg_buy"], c["avg_sell"], c["first_buy"], c["last_sell"]
    first_sell = min((s["date"] for s in trade.get("sells") or []), default=None)
    events = {}
    for kind, items in (("매수", trade.get("buys") or []), ("매도", trade.get("sells") or [])):
        for it in items:
            events.setdefault(it["date"], []).append(f"{kind} {it['price']:,.0f}원 × {it['qty']:,}주")
    rows = []
    for i, r in enumerate(ohlcv):
        d = r["date"]
        if not first_buy or d < first_buy:
            continue
        ev = events.get(d, [])
        kinds = {e.split()[0] for e in ev}
        rows.append({
            "date": d, "label": f"{d[5:]}({WEEKDAYS_KO[datetime.fromisoformat(d).weekday()]})",
            "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
            "day_pct": _pct_change(r["close"], ohlcv[i - 1]["close"]) if i > 0 else None,
            "vs_buy": _pct_change(r["close"], avg_buy),
            "vs_sell": _pct_change(r["close"], avg_sell) if (avg_sell and first_sell and d >= first_sell) else None,
            "events": " · ".join(ev), "kind": "매수+매도" if len(kinds) == 2 else (next(iter(kinds)) if kinds else ""),
            "partial": d == now.date().isoformat() and (now.hour, now.minute) < (15, 40),
        })
    priced = [r for r in rows if r["close"]]

    def extremes(rs, base):
        if not rs or not base:
            return None
        hi, lo = max(rs, key=lambda x: x["close"]), min(rs, key=lambda x: x["close"])
        return {"hi_date": hi["date"], "hi_close": hi["close"], "hi_pct": _pct_change(hi["close"], base),
                "lo_date": lo["date"], "lo_close": lo["close"], "lo_pct": _pct_change(lo["close"], base)}

    after_buy = extremes(priced, avg_buy)
    after_sell = None
    if last_sell and c["status"] != "보유 중":
        later = [r for r in priced if r["date"] > last_sell]
        if later:
            after_sell = extremes(later, avg_sell)
            after_sell.update(n=len(later), latest_close=later[-1]["close"], latest_pct=_pct_change(later[-1]["close"], avg_sell),
                              days_higher=sum(1 for r in later if r["close"] > avg_sell))
    return {"rows": rows, "avg_buy": avg_buy, "avg_sell": avg_sell, "status": c["status"], "ret_pct": c["ret_pct"],
            "buy_qty": c["buy_qty"], "sell_qty": c["sell_qty"], "after_buy": after_buy, "after_sell": after_sell,
            "latest": priced[-1] if priced else None, "first_buy": first_buy, "last_sell": last_sell,
            "data_start": ohlcv[0]["date"] if ohlcv else None,
            "data_gap": bool(first_buy and ohlcv and first_buy < ohlcv[0]["date"])}


PATH_TABLE_COLUMNS = [("label", "날짜"), ("open", "시가"), ("high", "고가"), ("low", "저가"), ("close", "종가"),
                      ("day_pct", "전일대비(%)"), ("vs_buy", "종가↔내 평균매수가(%)"), ("vs_sell", "종가↔내 평균매도가(%)"),
                      ("events", "내 체결")]


def path_dataframe(rows: list) -> pd.DataFrame:
    df = pd.DataFrame([{label: (r["label"] + (" 장중" if r.get("partial") else "") if key == "label" else r.get(key))
                        for key, label in PATH_TABLE_COLUMNS} for r in rows], columns=[label for _, label in PATH_TABLE_COLUMNS])
    num = [label for key, label in PATH_TABLE_COLUMNS if key not in ("label", "events")]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df


# ---- 실현 손익 (월별 / 당월) ----
# 매도 한 건(lot)마다 실현손익을 계산해 '매도한 날이 속한 달'에 넣는다. 같은 매매를 두 달에 걸쳐 나눠 팔았으면 각 달에 따로 들어간다.
# gross = (매도가 - 그 매매의 평균 매수가) × 수량, 수수료·세금은 입력한 합계를 매도 수량 비율로 나눠 배분한다.
# → 모든 달을 더하면 compute_trade의 실현손익(net_pl)과 같다. 보유 중인 수량의 미실현 손익은 포함하지 않는다.
def current_month(now=None) -> str:
    return (now or datetime.now(KST)).strftime("%Y-%m")


def realized_lots(trades: list) -> list:
    out = []
    for t in trades:
        c = compute_trade(t)
        if not c["sell_qty"] or not c["avg_buy"]:
            continue
        for s in t.get("sells") or []:
            gross = (s["price"] - c["avg_buy"]) * s["qty"]
            cost = c["costs"] * s["qty"] / c["sell_qty"]
            out.append({"trade_id": t.get("id") or t.get("name"), "name": t.get("name", ""), "date": s["date"], "month": s["date"][:7],
                        "price": s["price"], "qty": s["qty"], "avg_buy": c["avg_buy"], "gross": gross, "cost": cost,
                        "net": gross - cost, "basis": c["avg_buy"] * s["qty"], "proceeds": s["price"] * s["qty"]})
    out.sort(key=lambda x: (x["date"], x["name"]))
    return out


def month_summary(lots: list, month: str) -> dict:
    ml = [l for l in lots if l["month"] == month]
    per_trade = {}
    for l in ml:
        per_trade[l["trade_id"]] = per_trade.get(l["trade_id"], 0.0) + l["net"]
    net, basis = sum(l["net"] for l in ml), sum(l["basis"] for l in ml)
    wins = sum(1 for v in per_trade.values() if v > 0)
    return {"month": month, "net": net, "ret_pct": (net / basis * 100) if basis else None, "n_lots": len(ml),
            "n_trades": len(per_trade), "wins": wins, "losses": sum(1 for v in per_trade.values() if v < 0),
            "win_rate": (100 * wins / len(per_trade)) if per_trade else None,
            "proceeds": sum(l["proceeds"] for l in ml), "cost": sum(l["cost"] for l in ml)}


MONTHLY_COLUMNS = ["월", "실현손익(원)", "수익률(%)", "매도금액(원)", "수수료·세금(원)", "매매 수", "수익 매매", "손실 매매", "승률(%)", "누적 실현손익(원)"]


def monthly_realized(lots: list) -> pd.DataFrame:
    """월별 요약(오래된 달부터). 수익률은 그 달에 판 수량의 매수 원가 대비, 승률은 그 달 실현손익이 +인 매매의 비율."""
    rows, cum = [], 0.0
    for m in sorted({l["month"] for l in lots}):
        s = month_summary(lots, m)
        cum += s["net"]
        rows.append({"월": m, "실현손익(원)": s["net"], "수익률(%)": s["ret_pct"], "매도금액(원)": s["proceeds"],
                     "수수료·세금(원)": s["cost"], "매매 수": s["n_trades"], "수익 매매": s["wins"], "손실 매매": s["losses"],
                     "승률(%)": s["win_rate"], "누적 실현손익(원)": cum})
    return pd.DataFrame(rows, columns=MONTHLY_COLUMNS)


MONTH_SUMMARY_COLUMNS = ["종목명", "매도일", "체결 수(건)", "총 수량(주)", "평균 매수가", "평균 매도가", "매도가 범위", "실현손익(원)", "수익률(%)"]


def month_trade_summary(lots: list, month: str, sort: str = "pnl_desc") -> pd.DataFrame:
    """그 달 실현 손익을 종목(이름)별로 합친 요약표 + 마지막에 '합계' 행. 체결(분할 매도) 여러 건은 한 줄로 합친다.
    평균 매수가·매도가는 수량 가중 평균, 수익률은 합친 실현 손익 ÷ 합친 매수 원가. 같은 종목을 매매 여러 건으로 나눠 기록했어도 합쳐진다.
    합계 행의 수량·평균가는 종목이 섞여 의미가 없어 비운다. sort: pnl_desc(실현손익 큰 순) / pnl_asc / date_desc(매도일 최신순)."""
    groups = {}
    for l in lots:
        if l["month"] == month:
            groups.setdefault(l["name"], []).append(l)
    rows = []
    for name, ls in groups.items():
        qty, basis = sum(l["qty"] for l in ls), sum(l["basis"] for l in ls)
        net, proceeds = sum(l["net"] for l in ls), sum(l["proceeds"] for l in ls)
        dates, prices = sorted(l["date"] for l in ls), [l["price"] for l in ls]
        rows.append({"종목명": name, "매도일": dates[0][5:] if dates[0] == dates[-1] else f"{dates[0][5:]}~{dates[-1][5:]}",
                     "체결 수(건)": len(ls), "총 수량(주)": qty, "평균 매수가": basis / qty, "평균 매도가": proceeds / qty,
                     "매도가 범위": f"{min(prices):,.0f}" if min(prices) == max(prices) else f"{min(prices):,.0f}~{max(prices):,.0f}",
                     "실현손익(원)": net, "수익률(%)": (net / basis * 100) if basis else None, "_last": dates[-1]})
    if sort == "pnl_asc":
        rows.sort(key=lambda r: (r["실현손익(원)"], r["종목명"]))
    elif sort == "date_desc":
        rows.sort(key=lambda r: (r["_last"], r["실현손익(원)"]), reverse=True)
    else:
        rows.sort(key=lambda r: (-r["실현손익(원)"], r["종목명"]))
    ml = [l for l in lots if l["month"] == month]
    tb, tn = sum(l["basis"] for l in ml), sum(l["net"] for l in ml)
    if rows:
        rows.append({"종목명": "합계", "매도일": None, "체결 수(건)": len(ml), "총 수량(주)": None, "평균 매수가": None,
                     "평균 매도가": None, "매도가 범위": None, "실현손익(원)": tn, "수익률(%)": (tn / tb * 100) if tb else None, "_last": ""})
    df = pd.DataFrame(rows, columns=MONTH_SUMMARY_COLUMNS + ["_last"]).drop(columns="_last")
    num = ["체결 수(건)", "총 수량(주)", "평균 매수가", "평균 매도가", "실현손익(원)", "수익률(%)"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df


MONTH_LOTS_COLUMNS = ["종목명", "매도일", "매도가", "수량", "수량 비중(%)", "내 평균매수가", "실현손익(원)", "수익률(%)"]


def month_lots_dataframe(lots: list, month: str, name_order=None) -> pd.DataFrame:
    """체결(분할 매도) 한 건씩의 상세. 종목별로 묶어 매도일·매도가 순. 수량 비중 = 그 종목의 그 달 매도 수량 중 이 체결의 몫."""
    ml = [l for l in lots if l["month"] == month]
    totals = {}
    for l in ml:
        totals[l["name"]] = totals.get(l["name"], 0) + l["qty"]
    order = {n: i for i, n in enumerate(name_order or [])}
    ml.sort(key=lambda l: (order.get(l["name"], len(order)), l["name"], l["date"], -l["price"]))
    rows = [{"종목명": l["name"], "매도일": l["date"], "매도가": l["price"], "수량": l["qty"],
             "수량 비중(%)": l["qty"] / totals[l["name"]] * 100, "내 평균매수가": l["avg_buy"], "실현손익(원)": l["net"],
             "수익률(%)": (l["net"] / l["basis"] * 100) if l["basis"] else None} for l in ml]
    return pd.DataFrame(rows, columns=MONTH_LOTS_COLUMNS)


# ---- 매매 목록 월별 보기 ----
def trade_months(t: dict) -> set:
    """매수일·매도일이 속한 달('YYYY-MM') 모음."""
    return {x["date"][:7] for x in (t.get("buys") or []) + (t.get("sells") or []) if x.get("date")}


def trade_month_options(trades: list) -> list:
    """일지에 나오는 달 목록(최근 달 먼저)."""
    return sorted({m for t in trades for m in trade_months(t)}, reverse=True)


def filter_trades_by_month(trades: list, month: str) -> list:
    """그 달에 매수나 매도가 하나라도 있는 매매. 두 달에 걸친 매매는 양쪽 달에 모두 나온다."""
    return [t for t in trades if month in trade_months(t)]


SIGNAL_COMPARE_COLUMNS = [
    ("name", "종목명"), ("type_label", "신호"), ("signal_date", "신호일"), ("timing", "내 매수 시점"), ("status", "상태"),
    ("signal_close", "신호일종가"), ("entry_open", "다음날시가"), ("my_avg_buy", "내 평균매수가"),
    ("buy_vs_open", "매수가↔다음날시가(%)"), ("buy_vs_close", "매수가↔신호일종가(%)"),
    ("my_ret", "내 수익률(%)"), ("sig_ret_exit", "신호 기준 수익률(%)"), ("diff", "차이(%p)"),
    ("d1", "신호 D+1(%)"), ("d3", "신호 D+3(%)"), ("d5", "신호 D+5(%)"), ("d10", "신호 D+10(%)"), ("note", "비고"),
]


def signal_compare_dataframe(rows: list) -> pd.DataFrame:
    df = pd.DataFrame([{label: r.get(key) for key, label in SIGNAL_COMPARE_COLUMNS} for r in rows],
                      columns=[label for _, label in SIGNAL_COMPARE_COLUMNS])
    num = [label for key, label in SIGNAL_COMPARE_COLUMNS if key not in ("name", "type_label", "signal_date", "timing", "status", "note")]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    return df


def safe_styler(df: pd.DataFrame, formats: dict, signed_cols=(), row_styles=None, col_style_fns=None):
    """값이 없는 칸이 화면에서 'None'으로 보이지 않게, 표시용 문자열 표를 따로 만들고 색은 숫자 값으로 계산해서 입힌다.
    (Streamlit은 Styler.format으로 바꾼 값이 아니라 빈 값을 'None'으로 그린다.)
    formats: {열 이름: 값 → 문자열}. 포맷이 없는 열은 그대로 문자열로 바꾼다. signed_cols: 양수 빨강·음수 파랑으로 칠할 열.
    row_styles: 행마다 모든 칸에 덧입힐 CSS 목록(길이 = 행 수, 없으면 빈 문자열).
    col_style_fns: {열 이름: 값 → CSS 문자열}. 열마다 셀 값에 따라 색을 입힌다(예: 점수 높낮이)."""
    disp = pd.DataFrame(index=df.index)
    styles = pd.DataFrame("", index=df.index, columns=df.columns)
    for col in df.columns:
        fmt = formats.get(col)
        disp[col] = ["—" if (v is None or (not isinstance(v, str) and pd.isna(v)) or v == "") else (fmt(v) if fmt else str(v))
                     for v in df[col]]
    for col in signed_cols:
        styles[col] = [_signed_style(v) for v in df[col]]
    if row_styles:
        for col in df.columns:
            styles[col] = [(a + " " + b).strip() for a, b in zip(styles[col], row_styles)]
    for col, fn in (col_style_fns or {}).items():
        if col in df.columns:
            styles[col] = [(a + " " + fn(v)).strip() for a, v in zip(styles[col], df[col])]
    return disp.style.apply(lambda _: styles, axis=None)


# ---- GitHub 저장 (Contents API) ----
class JournalConflict(RuntimeError):
    pass


def _gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _gh_error(resp) -> str:
    """오류 메시지를 만든다. 토큰이 들어갈 수 있는 요청 헤더는 절대 포함하지 않는다."""
    try:
        msg = (resp.json().get("message") or "")[:120]
    except Exception:
        msg = ""
    hint = {401: "토큰이 잘못됐거나 만료됐습니다", 403: "권한이 부족하거나 호출 한도에 걸렸습니다"}.get(resp.status_code, "")
    return f"GitHub {resp.status_code}: {msg} {('— ' + hint) if hint else ''}".strip()


def gh_read_journal(repo: str, path: str, token: str, branch=None):
    """(일지 dict, sha). 파일이 아직 없으면 ({'trades': []}, None). 저장소를 못 찾거나 권한이 없으면 RuntimeError."""
    resp = requests.get(f"{GITHUB_API}/repos/{repo}/contents/{path}", headers=_gh_headers(token),
                        params={"ref": branch} if branch else None, timeout=15)
    if resp.status_code == 404:
        # 404는 '파일 없음'과 '저장소 없음/권한 없음'이 같이 온다 → 저장소 접근으로 구분
        r2 = requests.get(f"{GITHUB_API}/repos/{repo}", headers=_gh_headers(token), timeout=15)
        if r2.status_code == 200:
            return {"trades": []}, None
        raise RuntimeError("저장소를 찾을 수 없거나 토큰에 접근 권한이 없습니다 (JOURNAL_REPO 이름과 토큰의 저장소 권한을 확인하세요)")
    if resp.status_code != 200:
        raise RuntimeError(_gh_error(resp))
    data = resp.json()
    text = base64.b64decode(data.get("content", "")).decode("utf-8")
    journal = json.loads(text) if text.strip() else {"trades": []}
    if not isinstance(journal.get("trades"), list):
        journal["trades"] = []
    return journal, data["sha"]


def gh_write_journal(repo: str, path: str, token: str, journal: dict, sha, message: str, branch=None) -> str:
    body = {"message": message,
            "content": base64.b64encode(json.dumps(journal, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    if branch:
        body["branch"] = branch
    resp = requests.put(f"{GITHUB_API}/repos/{repo}/contents/{path}", headers=_gh_headers(token), json=body, timeout=15)
    if resp.status_code in (200, 201):
        return resp.json()["content"]["sha"]
    if resp.status_code == 409 or (resp.status_code == 422 and "sha" in (resp.text or "").lower()):
        raise JournalConflict("다른 곳에서 일지가 먼저 바뀌었습니다")
    raise RuntimeError(_gh_error(resp))


def journal_mutate(mutator, message: str):
    """일지를 읽어 mutator로 바꾼 뒤 저장한다. 그 사이 다른 곳에서 바뀌었으면(충돌) 한 번 다시 읽어 재시도."""
    cfg = journal_settings()
    for attempt in range(2):
        journal, sha = gh_read_journal(cfg["repo"], cfg["path"], cfg["token"], cfg["branch"])
        journal = mutator(journal)
        try:
            gh_write_journal(cfg["repo"], cfg["path"], cfg["token"], journal, sha, message, cfg["branch"])
            return journal
        except JournalConflict:
            if attempt == 1:
                raise


def journal_upsert(trade: dict):
    def _m(j):
        trades = j["trades"]
        for i, t in enumerate(trades):
            if t.get("id") == trade["id"]:
                trades[i] = trade
                break
        else:
            trades.append(trade)
        return j
    return journal_mutate(_m, f"journal: {trade['name']}")


def journal_delete(trade_id: str):
    def _m(j):
        j["trades"] = [t for t in j["trades"] if t.get("id") != trade_id]
        return j
    return journal_mutate(_m, "journal: delete")


@st.cache_data(ttl=60)
def load_journal() -> dict:
    cfg = journal_settings()
    return gh_read_journal(cfg["repo"], cfg["path"], cfg["token"], cfg["branch"])[0]


# ---- 화면 ----
def style_journal_table(df: pd.DataFrame):
    styler = df.style.map(_signed_style, subset=["실현손익(원)", "수익률(%)"])
    styler = styler.format(lambda v: "" if pd.isna(v) else f"{v:,.0f}",
                           subset=["평균매수가", "평균매도가", "매수수량", "매도수량", "남은수량"])
    styler = styler.format(lambda v: "—" if pd.isna(v) else f"{v:+,.0f}", subset=["실현손익(원)"])
    styler = styler.format(lambda v: "—" if pd.isna(v) else f"{v:+.2f}", subset=["수익률(%)"])
    return styler.format(lambda v: "" if pd.isna(v) else f"{v:.0f}", subset=["보유(일)"])


def style_journal_summary(df: pd.DataFrame):
    styler = df.style.map(_signed_style, subset=["평균 수익률(%)", "총 실현손익(원)"])
    styler = styler.format(lambda v: f"{v:.0f}", subset=["승률(%)"])
    styler = styler.format(lambda v: f"{v:+.2f}", subset=["평균 수익률(%)"])
    return styler.format(lambda v: f"{v:+,.0f}", subset=["총 실현손익(원)"])


def render_journal_tab():
    """비밀번호를 통과한 뒤에만 호출된다. 저장소에서 일지를 읽어 통계·목록·입력 폼을 그린다."""
    flash = st.session_state.pop("journal_flash", None)
    if flash:
        st.success(flash)
    if st.button("🔒 잠그기"):
        st.session_state["journal_ok"] = False
        st.rerun()
    try:
        journal = load_journal()
    except Exception as ex:
        st.error(f"일지를 불러오지 못했습니다: {ex}")
        return
    trades = journal.get("trades", [])
    cfg = journal_settings()
    st.caption(f"기록은 비공개 저장소 {cfg['repo']}의 {cfg['path']}에 저장됩니다. 화면의 값은 최대 1분 전 내용일 수 있습니다. "
               "수익률은 입력한 수수료·세금까지 뺀 실현손익 기준입니다.")

    # ---- 실현 손익 (월별 / 당월) ----
    st.markdown("##### 💰 실현 손익")
    pnl_lots = realized_lots(trades)
    this_month = current_month()
    if not pnl_lots:
        st.info("아직 매도 내역이 없어서 실현 손익이 없습니다. 매도까지 기록하면 월별·당월 실현 손익을 이곳에서 볼 수 있어요.")
    else:
        past_months = [m for m in sorted({l["month"] for l in pnl_lots}, reverse=True) if m != this_month]
        pnl_keys = ["__this__", "__all__"] + past_months
        pnl_pick = st.selectbox(
            "실현 손익 기간", pnl_keys, key=f"jn_pnl_pick_{st.session_state.get('journal_nonce', 0)}",
            format_func=lambda k: f"당월 ({this_month})" if k == "__this__" else "월별 전체" if k == "__all__" else k)
        money = lambda v: f"{v:+,.0f}"
        pct1 = lambda v: f"{v:+.2f}"
        if pnl_pick == "__all__":
            mdf = monthly_realized(pnl_lots)
            total = mdf["실현손익(원)"].sum()
            mm = st.columns(3)
            mm[0].metric("누적 실현 손익", f"{total:+,.0f}원")
            mm[1].metric("수익 낸 달", f"{int((mdf['실현손익(원)'] > 0).sum())} / {len(mdf)}개월")
            mm[2].metric("월 평균 실현 손익", f"{mdf['실현손익(원)'].mean():+,.0f}원")
            shown = mdf.iloc[::-1].reset_index(drop=True)                     # 최근 달이 위로
            st.dataframe(
                safe_styler(shown, {"실현손익(원)": money, "수익률(%)": pct1, "매도금액(원)": lambda v: f"{v:,.0f}",
                                    "수수료·세금(원)": lambda v: f"{v:,.0f}", "매매 수": lambda v: f"{int(v)}",
                                    "수익 매매": lambda v: f"{int(v)}", "손실 매매": lambda v: f"{int(v)}",
                                    "승률(%)": lambda v: f"{v:.0f}", "누적 실현손익(원)": money},
                            signed_cols=["실현손익(원)", "수익률(%)", "누적 실현손익(원)"]),
                use_container_width=True, hide_index=True)
            st.bar_chart(mdf.set_index("월")["실현손익(원)"])
        else:
            month = this_month if pnl_pick == "__this__" else pnl_pick
            ms = month_summary(pnl_lots, month)
            if ms["n_lots"] == 0:
                st.info(f"{month}에는 매도 내역이 없습니다.")
            else:
                mm = st.columns(4)
                mm[0].metric(f"{month} 실현 손익", f"{ms['net']:+,.0f}원")
                mm[1].metric("수익률 (매수 원가 기준)", f"{ms['ret_pct']:+.2f}%" if ms["ret_pct"] is not None else "—")
                mm[2].metric("매도 건수", f"{ms['n_lots']}건", delta=f"{ms['n_trades']}개 매매", delta_color="off")
                mm[3].metric("승률", f"{ms['win_rate']:.0f}%", delta=f"수익 {ms['wins']} · 손실 {ms['losses']}", delta_color="off")
                price1 = lambda v: f"{v:,.0f}"
                sort_labels = {"pnl_desc": "실현손익 큰 순", "pnl_asc": "실현손익 작은 순", "date_desc": "매도일 최신순"}
                sort_pick = st.selectbox("종목 정렬", list(sort_labels), format_func=lambda k: sort_labels[k],
                                         key=f"jn_pnl_sort_{st.session_state.get('journal_nonce', 0)}")
                sdf = month_trade_summary(pnl_lots, month, sort_pick)
                st.dataframe(
                    safe_styler(sdf, {"체결 수(건)": lambda v: f"{int(v)}", "총 수량(주)": lambda v: f"{int(v):,}", "평균 매수가": price1,
                                      "평균 매도가": price1, "실현손익(원)": money, "수익률(%)": pct1},
                                signed_cols=["실현손익(원)", "수익률(%)"],
                                row_styles=[""] * (len(sdf) - 1) + ["font-weight: 700; background-color: rgba(148, 163, 184, 0.18);"]),
                    use_container_width=True, hide_index=True)
                st.caption("같은 종목의 체결(분할 매도)은 한 줄로 합쳤어요. 평균 매수가·매도가는 수량 가중 평균, 수익률은 합친 실현 손익 ÷ 합친 매수 원가이고, "
                           "'매도가 범위'는 가장 낮은 체결가~가장 높은 체결가예요.")
                with st.expander("체결 상세 보기 (분할 매도 한 건씩)"):
                    ldf = month_lots_dataframe(pnl_lots, month, [n for n in sdf["종목명"] if n != "합계"])
                    zebra, prev_name, group = [], None, -1
                    for nm in ldf["종목명"]:                  # 종목이 바뀔 때마다 배경을 번갈아 바꿔 묶음이 보이게 한다
                        if nm != prev_name:
                            group, prev_name = group + 1, nm
                        zebra.append("background-color: rgba(148, 163, 184, 0.10);" if group % 2 else "")
                    st.dataframe(
                        safe_styler(ldf, {"매도가": price1, "수량": lambda v: f"{int(v):,}", "수량 비중(%)": lambda v: f"{v:.0f}",
                                          "내 평균매수가": price1, "실현손익(원)": money, "수익률(%)": pct1},
                                    signed_cols=["실현손익(원)", "수익률(%)"], row_styles=zebra),
                        use_container_width=True, hide_index=True)
                    st.caption("'수량 비중'은 그 종목의 이 달 매도 수량 중 이 체결이 차지하는 몫이에요. 큰 수량이 낮은 가격에 체결됐는지 같은 분포를 볼 때 쓰세요.")
        st.caption("실현 손익은 매도한 날이 속한 달에 넣어요(같은 매매를 두 달에 나눠 팔았으면 각 달에 따로). 매도 한 건의 손익 = (매도가 − 그 매매의 "
                   "평균 매수가) × 수량이고, 입력한 수수료·세금 합계는 매도 수량 비율로 나눠 뺍니다(매수 때 낸 수수료도 합계에 들어 있을 수 있어요). "
                   "수익률은 그 기간에 판 수량의 매수 원가 대비, 승률은 그 기간 실현 손익이 +인 매매의 비율이에요. 아직 팔지 않은 보유 수량의 "
                   "미실현 손익은 포함하지 않습니다.")

    # ---- 통계 (마감된 매매만) ----
    st.markdown("##### 📊 마감된 매매 통계")
    stats = summarize_trades(trades)
    if stats["n"] == 0:
        st.info("아직 전량 매도까지 끝난 매매가 없습니다.")
    else:
        m = st.columns(5)
        m[0].metric("마감 매매", f"{stats['n']}건")
        m[1].metric("승률", f"{stats['win_rate']:.0f}%")
        m[2].metric("평균 수익률", f"{stats['avg_ret']:+.2f}%")
        m[3].metric("손익비", f"{stats['pl_ratio']:.2f}" if stats["pl_ratio"] is not None else "—")
        m[4].metric("총 실현손익", f"{stats['total_net_pl']:+,.0f}원")
        if stats["n"] < 30:
            st.caption("⚠ 마감 매매가 30건 미만이면 승률·평균은 우연에 크게 좌우됩니다. 결론이 아니라 복기용 기록으로 보세요.")
        for title, field in (("매수 근거 신호별 (여러 신호를 골랐으면 각각에 집계)", "signals"), ("청산 유형별", "exit_type")):
            by = summarize_journal_by(trades, field)
            if len(by):
                st.markdown(f"**{title}**")
                st.dataframe(style_journal_summary(by), use_container_width=True, hide_index=True)

    # ---- 목록 ----
    st.markdown("##### 🧾 매매 목록")
    if not trades:
        st.info("아직 기록이 없습니다. 아래에서 첫 매매를 기록해 보세요.")
    else:
        list_keys = ["__all__", "__this__"] + [m for m in trade_month_options(trades) if m != this_month]
        list_pick = st.selectbox(
            "매매 목록 기간", list_keys, key=f"jn_list_pick_{st.session_state.get('journal_nonce', 0)}",
            format_func=lambda k: "전체" if k == "__all__" else f"당월 ({this_month})" if k == "__this__" else k)
        list_trades = trades if list_pick == "__all__" else filter_trades_by_month(trades, this_month if list_pick == "__this__" else list_pick)
        if not list_trades:
            st.info("이 기간에는 매수나 매도 기록이 없습니다.")
        else:
            st.caption(f"{len(list_trades)}건 표시 (전체 {len(trades)}건). 선택한 달에 매수나 매도가 있는 매매를 보여줘요 — "
                       "두 달에 걸친 매매는 양쪽 달에 모두 나옵니다.")
            st.dataframe(style_journal_table(build_journal_table(list_trades)), use_container_width=True, hide_index=True)
        with st.expander("매수·매도 이유와 메모 보기"):
            for t in sorted(list_trades, key=lambda x: compute_trade(x)["first_buy"] or "", reverse=True):
                c = compute_trade(t)
                ret = f"{c['ret_pct']:+.2f}%" if c["ret_pct"] is not None else "미정"
                st.markdown(f"**{t['name']}** · {c['first_buy']} · {c['status']} · {ret}")
                st.markdown(f"- 매수 이유: {t.get('buy_reason') or '—'}\n- 매도 이유: {t.get('sell_reason') or '—'}"
                            + (f"\n- 메모: {t['memo']}" if t.get("memo") else ""))

    # ---- 내 매매 이후 가격 추이 ----
    st.markdown("##### 📈 내 매매 이후 가격 추이")
    if not trades:
        st.info("아직 기록이 없습니다. 매매를 기록하면 이곳에서 선택해 매수·매도가와 이후 영업일별 가격을 볼 수 있어요.")
    else:
        _nonce = st.session_state.get("journal_nonce", 0)
        _order = sorted(range(len(trades)), key=lambda i: compute_trade(trades[i])["first_buy"] or "", reverse=True)
        pi = st.selectbox(
            "추이를 볼 매매 선택", _order, key=f"jn_path_pick_{_nonce}",
            format_func=lambda i: f"{trades[i]['name']} · {compute_trade(trades[i])['first_buy']} · {compute_trade(trades[i])['status']}")
        pt = trades[pi]
        fills = ([{"구분": "매수", "날짜": b["date"], "가격(원)": b["price"], "수량(주)": b["qty"], "금액(원)": b["price"] * b["qty"]}
                  for b in pt.get("buys") or []] +
                 [{"구분": "매도", "날짜": x["date"], "가격(원)": x["price"], "수량(주)": x["qty"], "금액(원)": x["price"] * x["qty"]}
                  for x in pt.get("sells") or []])
        fdf = pd.DataFrame(fills).sort_values(["날짜", "구분"], ascending=[True, True], ignore_index=True)
        pcode = resolve_trade_code(pt, trades, load_tracker_signals())
        ohlcv = ohlcv_rows_from_df(fetch_daily_ohlcv(pcode)) if pcode else []
        path = trade_price_path(pt, ohlcv)
        pm = st.columns(5)
        pm[0].metric("평균 매수가", f"{path['avg_buy']:,.0f}원", delta=f"{path['buy_qty']:,}주", delta_color="off")
        pm[1].metric("평균 매도가", f"{path['avg_sell']:,.0f}원" if path["avg_sell"] else "보유 중",
                     delta=(f"{path['sell_qty']:,}주" if path["avg_sell"] else None), delta_color="off")
        pm[2].metric("실현 수익률", f"{path['ret_pct']:+.2f}%" if path["ret_pct"] is not None else "—",
                     delta="수수료·세금 반영", delta_color="off")
        asl, ab = path["after_sell"], path["after_buy"]
        if asl:
            pm[3].metric(f"매도 후 {asl['n']}영업일 최고 종가", f"{asl['hi_close']:,.0f}원",
                         delta=f"내 매도가 대비 {asl['hi_pct']:+.2f}%", delta_color="off")
            pm[4].metric("최근 종가 (내 매도가 대비)", f"{asl['latest_close']:,.0f}원",
                         delta=f"{asl['latest_pct']:+.2f}% · 종가가 매도가보다 높았던 날 {asl['days_higher']}/{asl['n']}일", delta_color="off")
        elif path["latest"] and ab:
            pm[3].metric("첫 매수 후 최고 종가", f"{ab['hi_close']:,.0f}원", delta=f"내 매수가 대비 {ab['hi_pct']:+.2f}%", delta_color="off")
            pm[4].metric("최근 종가 (내 매수가 대비)", f"{path['latest']['close']:,.0f}원",
                         delta=f"{_pct_change(path['latest']['close'], path['avg_buy']):+.2f}%", delta_color="off")
        st.markdown("**내 체결 내역**")
        st.dataframe(safe_styler(fdf, {"가격(원)": lambda v: f"{v:,.0f}", "수량(주)": lambda v: f"{v:,.0f}", "금액(원)": lambda v: f"{v:,.0f}"}),
                     use_container_width=True, hide_index=True)
        st.markdown("**이후 영업일별 가격**")
        if not pcode:
            st.info("이 매매의 종목코드를 알 수 없어서 가격을 불러오지 못했습니다. 아래 입력 폼에서 이 매매를 골라 종목코드를 넣고 저장해 주세요.")
        elif not ohlcv:
            st.warning(f"{pcode}의 일봉을 가져오지 못했습니다 (일시적인 조회 실패일 수 있어요 — 잠시 후 다시 시도하세요).")
        elif not path["rows"]:
            st.info("받은 일봉 범위(최근 약 4개월)에 이 매매의 매수일 이후 데이터가 없습니다.")
        else:
            if path["data_gap"]:
                st.caption(f"⚠ 첫 매수일({path['first_buy']})이 받은 일봉 범위(최근 약 4개월, {path['data_start']}부터)보다 앞이라 앞부분이 비어 있어요.")
            pdf = path_dataframe(path["rows"])
            pct = lambda v: f"{v:+.2f}"
            price = lambda v: f"{v:,.0f}"
            hl = ["background-color: rgba(250, 204, 21, 0.16);" if r["kind"] else "" for r in path["rows"]]
            st.dataframe(
                safe_styler(pdf, {"시가": price, "고가": price, "저가": price, "종가": price, "전일대비(%)": pct,
                                  "종가↔내 평균매수가(%)": pct, "종가↔내 평균매도가(%)": pct},
                            signed_cols=["전일대비(%)", "종가↔내 평균매수가(%)", "종가↔내 평균매도가(%)"], row_styles=hl),
                use_container_width=True, hide_index=True)
            if asl:
                st.caption(f"매도 후 {asl['n']}영업일 동안 종가는 최고 {asl['hi_close']:,.0f}원({asl['hi_date']}, 내 매도가 대비 {asl['hi_pct']:+.2f}%), "
                           f"최저 {asl['lo_close']:,.0f}원({asl['lo_date']}, {asl['lo_pct']:+.2f}%)였어요. 내 매도가보다 종가가 높았던 날은 "
                           f"{asl['days_higher']}/{asl['n']}일입니다. 파랑(마이너스)이면 판 뒤에 내려간 것, 빨강이면 판 뒤에 더 오른 거예요.")
            if ab:
                st.caption(f"첫 매수 이후 종가는 최고 {ab['hi_close']:,.0f}원({ab['hi_date']}, 내 평균 매수가 대비 {ab['hi_pct']:+.2f}%), "
                           f"최저 {ab['lo_close']:,.0f}원({ab['lo_date']}, {ab['lo_pct']:+.2f}%)였어요.")
            st.caption("노란 배경은 내가 체결한 날이에요. 내 체결가는 장중 가격이라 그날 종가와 다르고, 일봉은 수정주가 기준이라 액면분할 등이 있으면 "
                       "과거 값이 조정될 수 있어요. 오늘 행이 '장중'이면 현재가이고 마감(15:30) 뒤에 종가로 바뀝니다. 같은 값을 신호 기록과 "
                       "비교하려면 아래 '신호 vs 내 매매'를 보세요.")

    # ---- 신호 vs 내 매매 ----
    st.markdown("##### 🔗 신호 vs 내 매매")
    linked = [t for t in trades if t.get("signal_refs")]
    if not linked:
        st.info("아직 신호를 연결한 매매가 없습니다. 아래 입력 폼의 '연결할 신호 기록'에서 연결하면, 신호 다음 날 시가·D+n 성과와 "
                "내 매매를 이곳에서 비교할 수 있어요.")
    else:
        cmp_rows = build_signal_compare_rows(linked, load_tracker_signals())
        sm = summarize_signal_comparison(cmp_rows)
        mc = st.columns(5)
        mc[0].metric("연결한 신호", f"{sm['n_rows']}건", delta=f"기록 확인 {sm['n_signal_found']}건", delta_color="off")
        mc[1].metric("내 매수가 vs 다음날 시가", f"{sm['avg_buy_vs_open']:+.2f}%" if sm["avg_buy_vs_open"] is not None else "—")
        mc[2].metric("내 평균 수익률", f"{sm['avg_my_ret']:+.2f}%" if sm["avg_my_ret"] is not None else "—")
        mc[3].metric("신호 기준 평균 수익률", f"{sm['avg_sig_ret']:+.2f}%" if sm["avg_sig_ret"] is not None else "—")
        mc[4].metric("평균 차이(내 − 신호)", f"{sm['avg_diff']:+.2f}%p" if sm["avg_diff"] is not None else "—",
                     delta=(f"내가 나은 건 {sm['n_better']}/{sm['n_both']}건" if sm["n_both"] else None), delta_color="off")
        pct = lambda v: f"{v:+.2f}"
        price = lambda v: f"{v:,.0f}"
        cdf = signal_compare_dataframe(cmp_rows)
        st.dataframe(
            safe_styler(cdf, {"신호일종가": price, "다음날시가": price, "내 평균매수가": price, "매수가↔다음날시가(%)": pct,
                              "매수가↔신호일종가(%)": pct, "내 수익률(%)": pct, "신호 기준 수익률(%)": pct, "차이(%p)": pct,
                              "신호 D+1(%)": pct, "신호 D+3(%)": pct, "신호 D+5(%)": pct, "신호 D+10(%)": pct},
                        signed_cols=["내 수익률(%)", "신호 기준 수익률(%)", "차이(%p)", "신호 D+1(%)", "신호 D+3(%)",
                                     "신호 D+5(%)", "신호 D+10(%)"]),
            use_container_width=True, hide_index=True)
        if sm["n_both"] == 0:
            st.info("아직 내 매도일의 종가까지 채워진 연결 신호가 없어 수익률 비교는 비어 있습니다. 신호 추적은 매 거래일 15:40 이후 갱신돼요.")
        elif sm["n_both"] < 30:
            st.warning(f"비교할 수 있는 매매가 {sm['n_both']}건뿐입니다. 30건 미만이면 평균과 '내가 나았다/못했다'는 우연에 크게 좌우돼요.")
        st.caption("'신호 기준 수익률'은 신호 다음 거래일 시가에 사서 내가 마지막으로 판 날의 종가에 팔았다면의 수익률이에요. "
                   "내 매도가는 장중 체결가라 종가와 다를 수 있고, 내 수익률은 수수료·세금을 뺀 값인데 신호 기준 값은 비용을 빼지 않아서 "
                   "정확히 같은 조건의 비교는 아니에요(근사치). '매수가↔다음날시가'가 음수면 신호 다음 날 시가보다 싸게 산 거예요. "
                   "같은 종목을 재진입으로 여러 번 기록했다면 매매마다 같은 신호에 연결해 한 줄씩 비교됩니다.")

    # ---- 입력 / 수정 ----
    st.markdown("##### ✍️ 매매 기록하기 / 수정하기")
    nonce = st.session_state.get("journal_nonce", 0)
    order = sorted(range(len(trades)), key=lambda i: compute_trade(trades[i])["first_buy"] or "", reverse=True)
    pick = st.selectbox(
        "입력 대상", [-1] + order, key=f"jn_target_{nonce}",
        format_func=lambda i: "새 매매 기록" if i == -1 else
        f"{trades[i]['name']} · {compute_trade(trades[i])['first_buy']} · {compute_trade(trades[i])['status']}")
    existing = trades[pick] if pick != -1 else None
    e = existing or {}
    ks = f"{nonce}_{e.get('id', 'new')}"     # 대상이 바뀌거나 저장한 뒤에는 입력칸 초기값이 새로 적용되도록 key에 넣는다
    name = st.text_input("종목명", value=e.get("name", ""), key=f"jn_name_{ks}")
    code = st.text_input("종목코드 (선택)", value=e.get("code", ""), key=f"jn_code_{ks}")
    buys_txt = st.text_area("매수 내역 — 한 줄에 하나, '날짜 가격 수량'", value=lines_from_rows(e.get("buys", [])),
                            key=f"jn_buys_{ks}", height=110, placeholder="2026-09-28 36000 14\n9.29 36500 4")
    sells_txt = st.text_area("매도 내역 — 아직 안 팔았으면 비워 두세요 (나중에 이 매매를 골라 추가)",
                             value=lines_from_rows(e.get("sells", [])), key=f"jn_sells_{ks}", height=110,
                             placeholder="2026-09-30 38050 9\n9.30 38750 9")
    st.caption("날짜는 2026-09-28 · 2026.9.28 · 9.28 · 9/28 형식이 모두 됩니다. 연도를 빼면 올해로 봅니다. 쉼표·'원'·'주'는 있어도 됩니다.")
    costs = st.number_input("수수료·세금 합계 (원, 증권사 체결내역에서 확인)", min_value=0.0, value=float(e.get("costs") or 0),
                            step=100.0, key=f"jn_costs_{ks}")
    signals = st.multiselect("매수 근거가 된 앱 신호", JOURNAL_SIGNAL_OPTIONS,
                             default=[s for s in e.get("signals", []) if s in JOURNAL_SIGNAL_OPTIONS], key=f"jn_sig_{ks}")
    tracker_signals = load_tracker_signals()
    sig_by_key = {signal_ref_key(x.get("code"), x.get("type"), x.get("signal_date")): x for x in tracker_signals}
    default_keys = [signal_ref_key(r.get("code"), r.get("type"), r.get("signal_date")) for r in e.get("signal_refs") or []]
    cand_keys = [signal_ref_key(x.get("code"), x.get("type"), x.get("signal_date")) for x in signal_candidates(tracker_signals, name, code)]
    # 이미 고른 값이 후보에서 빠져도 선택이 사라지거나 오류가 나지 않게 옵션에 함께 넣는다
    ref_options = list(dict.fromkeys(cand_keys + default_keys + list(st.session_state.get(f"jn_sigref_{ks}") or [])))

    def _fmt_ref(k):
        x = sig_by_key.get(k)
        if not x:
            return f"{k} (신호 추적에 기록 없음)"
        return (f"{x.get('name')}({x.get('code')}) · {SIGNAL_TYPE_LABELS.get(x.get('type'), x.get('type'))} · "
                f"신호일 {x.get('signal_date')} · 신호일 종가 {x.get('signal_close', 0):,.0f}원")

    if ref_options:
        sig_ref_sel = st.multiselect("연결할 신호 기록 (신호 추적)", ref_options, format_func=_fmt_ref, key=f"jn_sigref_{ks}",
                                     default=[k for k in default_keys if k in ref_options])
    else:
        sig_ref_sel = []
        st.caption("연결할 신호 기록이 없습니다 — 종목코드(또는 종목명)를 입력하면 신호 추적에 기록된 그 종목의 신호가 나옵니다. "
                   "신호는 매 거래일 15:40 이후 기록돼요.")
    st.caption("연결하면 아래 '🔗 신호 vs 내 매매' 표에서 신호 다음 날 시가·D+n 성과와 내 매매를 비교할 수 있어요. "
               "연결한 신호의 종류는 위 '매수 근거 신호' 태그에도 자동으로 반영됩니다.")
    exit_type = st.selectbox("청산 유형", JOURNAL_EXIT_TYPES, key=f"jn_exit_{ks}",
                             index=JOURNAL_EXIT_TYPES.index(e["exit_type"]) if e.get("exit_type") in JOURNAL_EXIT_TYPES else 0)
    buy_reason = st.text_area("매수 이유", value=e.get("buy_reason", ""), key=f"jn_br_{ks}", height=80)
    sell_reason = st.text_area("매도 이유", value=e.get("sell_reason", ""), key=f"jn_sr_{ks}", height=80)
    memo = st.text_area("메모 (복기할 점 등)", value=e.get("memo", ""), key=f"jn_memo_{ks}", height=80)

    buys, err_b = parse_trade_lines(buys_txt)
    sells, err_s = parse_trade_lines(sells_txt)
    errors = list(err_b) + list(err_s)
    typed = bool(name.strip() or buys_txt.strip() or sells_txt.strip())
    if typed:
        if not name.strip():
            errors.append("종목명을 입력하세요.")
        if not buys and not err_b:
            errors.append("매수 내역을 한 줄 이상 입력하세요.")
        if buys and sells:
            if sum(s["qty"] for s in sells) > sum(b["qty"] for b in buys):
                errors.append("매도 수량이 매수 수량보다 많습니다.")
            if min(s["date"] for s in sells) < min(b["date"] for b in buys):
                errors.append("매도일이 첫 매수일보다 빠릅니다.")
    draft = {"buys": buys, "sells": sells, "costs": costs}
    if typed and buys and not errors:
        c = compute_trade(draft)
        parts = [f"평균 매수가 {c['avg_buy']:,.0f}원 × {c['buy_qty']}주"]
        if c["avg_sell"]:
            parts.append(f"평균 매도가 {c['avg_sell']:,.0f}원 × {c['sell_qty']}주 → 실현손익 {c['net_pl']:+,.0f}원 ({c['ret_pct']:+.2f}%)")
        if c["remaining"] > 0:
            parts.append(f"남은 수량 {c['remaining']}주")
        st.info("미리보기: " + " · ".join(parts))
    for msg in errors:
        st.warning(msg)

    if st.button("💾 저장", key=f"jn_save_{ks}", disabled=(not typed or bool(errors))):
        now = datetime.now(KST).isoformat(timespec="seconds")
        refs = [r for r in (parse_signal_ref_key(k) for k in sig_ref_sel) if r]
        tags = list(signals)
        for r in refs:                                   # 연결한 신호 종류를 '매수 근거 신호' 태그에도 반영
            tag = SIGNAL_TAG_BY_TYPE.get(r["type"])
            if tag and tag not in tags:
                tags.append(tag)
        trade = {
            "id": e.get("id") or f"{datetime.now(KST):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:6]}",
            "name": name.strip(), "code": code.strip() or (refs[0]["code"] if refs else ""), "signals": tags,
            "signal_refs": refs, "buys": buys, "sells": sells,
            "costs": costs, "exit_type": "" if exit_type == JOURNAL_EXIT_TYPES[0] else exit_type,
            "buy_reason": buy_reason.strip(), "sell_reason": sell_reason.strip(), "memo": memo.strip(),
            "created_at": e.get("created_at") or now, "updated_at": now,
        }
        try:
            journal_upsert(trade)
        except Exception as ex:
            st.error(f"저장하지 못했습니다: {ex}")
        else:
            load_journal.clear()
            st.session_state["journal_nonce"] = nonce + 1
            st.session_state["journal_flash"] = "저장했습니다 ✅"
            st.rerun()

    if existing:
        with st.expander("이 매매 삭제"):
            confirm = st.checkbox("정말 삭제합니다 (되돌릴 수 없습니다)", key=f"jn_del_ok_{ks}")
            if st.button("🗑 삭제", key=f"jn_del_{ks}", disabled=not confirm):
                try:
                    journal_delete(existing["id"])
                except Exception as ex:
                    st.error(f"삭제하지 못했습니다: {ex}")
                else:
                    load_journal.clear()
                    st.session_state["journal_nonce"] = nonce + 1
                    st.session_state["journal_flash"] = "삭제했습니다."
                    st.rerun()


SETUP_JOURNAL_GUIDE = """**매매 일지를 쓰려면 먼저 설정이 필요합니다** (일지는 이 코드 저장소가 아니라 **비공개 저장소**에 저장됩니다).

1. GitHub에서 비공개(Private) 저장소를 새로 만듭니다 (예: `gyun-journal`, README 추가 체크).
2. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens**에서 새 토큰을 만들되, 저장소는 방금 만든 그 저장소 하나만 선택하고 권한은 **Contents: Read and write**로 둡니다.
3. Streamlit 앱 설정 → Secrets에 아래 세 줄을 추가합니다 (기존 값은 그대로 두세요).

```
JOURNAL_GITHUB_TOKEN = "github_pat_..."
JOURNAL_REPO = "내계정/gyun-journal"
JOURNAL_PASSWORD = "정한 비밀번호"
```
"""


# ============================================================
# 🤖 AI 분석용 리포트 — scripts/ai_report.py(GitHub Actions)가 평일에 7번 만들어 data/에 올리는 파일을 보여 주고,
#   ChatGPT 같은 AI에 붙여넣거나 파일로 올릴 수 있게 복사·다운로드를 제공한다. (매매 일지·API 키는 리포트에 들어가지 않는다)
# ============================================================
AI_REPORT_MD_PATH = "data/ai_report.md"
AI_REPORT_SHORT_PATH = "data/ai_report_short.md"
AI_REPORT_JSON_PATH = "data/ai_report.json"
AI_REPORT_STATUS_PATH = "data/ai_report_status.json"
AI_REPORT_BLIND_PATH = "data/ai_report_blind.md"
AI_REPORT_RAW_BASE = _secret("AI_REPORT_RAW_BASE", "https://raw.githubusercontent.com/azo3682/gyun/main/data")


@st.cache_data(ttl=60)
def load_ai_report() -> dict:
    """{'md', 'short', 'json', 'status'}: 파일이 없거나 깨졌으면 해당 값은 None. status = 마지막 실행 결과(성공/실패 사유)."""
    out = {"md": None, "short": None, "json": None, "status": None, "blind": None}
    try:
        with open(AI_REPORT_STATUS_PATH, "r", encoding="utf-8") as f:
            out["status"] = json.load(f)
    except Exception:
        pass
    for key, path in (("md", AI_REPORT_MD_PATH), ("short", AI_REPORT_SHORT_PATH), ("blind", AI_REPORT_BLIND_PATH)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                out[key] = f.read()
        except OSError:
            pass
    try:
        with open(AI_REPORT_JSON_PATH, "r", encoding="utf-8") as f:
            out["json"] = json.load(f)
    except Exception:
        pass
    return out


def ai_report_table(payload: dict) -> pd.DataFrame:
    rows = []
    for s in (payload or {}).get("stocks", []):
        sc, pr, tc = s["scores"]["swing_check"], s["price"], s["technical"]
        rows.append({"종목명": s["name"], "코드": s["code"], "순매수 순위": s["ranking"].get("buy_rank"),
                     "현재가": pr.get("current"), "등락(%)": pr.get("change_pct"), "전환신호": "O" if tc.get("transition") else "X",
                     "RSI": tc.get("rsi"), "추세": tc.get("trend"), "스윙 체크": f"{sc['total']}/{sc['max']}",
                     "앱 종합": s["scores"].get("app_composite")})
    df = pd.DataFrame(rows, columns=["종목명", "코드", "순매수 순위", "현재가", "등락(%)", "전환신호", "RSI", "추세", "스윙 체크", "앱 종합"])
    df[["순매수 순위", "현재가", "등락(%)", "RSI", "앱 종합"]] = df[["순매수 순위", "현재가", "등락(%)", "RSI", "앱 종합"]].apply(pd.to_numeric, errors="coerce")
    return df


# ============================================================
# 📊 AI 점수 비교 — 같은 '점수 매기기용 리포트'를 여러 AI에게 주고 받은 JSON 점수를 나란히 놓고 비교한다.
#   목적은 서로를 비판하게 하는 게 아니라, 같은 기준(수급·신호·추세·가격부담·재무 각 0~20점)으로 매긴 결과를 한눈에 비교하는 것이다.
#   일치한다고 맞는 게 아니다(같은 데이터에 같은 식으로 반응했을 수 있다). 규칙 기반 점수는 기준선으로만 보여 준다.
# ============================================================
# ---- AI 점수 비교 helpers begin
SCORE_DIMS = ["수급", "신호", "추세", "가격부담", "재무"]
DISAGREE_SCORE_GAP = 20      # AI들의 총점 차이(최대-최소)가 이 값 이상이면 '의견 갈림' (임의 기준)
DISAGREE_RANK_GAP = 4        # 순위 차이가 이 값 이상이어도 '의견 갈림' (임의 기준)
BASELINE_NAME = "규칙 기반"
RESERVED_NAMES = {"종목명", "코드", BASELINE_NAME, "AI 평균", "점수 편차", "평균 순위", "순위 차", "판정"}


def extract_json_object(text):
    """AI 답변에서 'scores' 목록이 있는 JSON 객체를 꺼낸다. 코드 블록이나 앞뒤 설명 문장이 있어도 된다. 없으면 None."""
    src = (text or "").replace("\u201c", '"').replace("\u201d", '"')
    dec, i = json.JSONDecoder(), src.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(src[i:])
            if isinstance(obj, dict) and isinstance(obj.get("scores"), list):
                return obj
        except ValueError:
            pass
        i = src.find("{", i + 1)
    return None


def _score_num(v):
    try:
        x = float(str(v).replace(",", "").strip())
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def parse_ai_scores(text, universe) -> dict:
    """AI가 돌려준 JSON을 검증해서 읽는다. universe = [{'code','name'}, ...](리포트 순서).
    항목 점수는 0~20으로 맞추고, 총점은 항목 합으로 다시 계산한다(AI가 적은 총점이 다르면 경고). 문제는 warnings에 모은다."""
    out = {"ok": False, "error": None, "ai": None, "as_of": None, "scores": {}, "warnings": []}
    obj = extract_json_object(text)
    if obj is None:
        out["error"] = "JSON을 찾지 못했어요. AI가 출력한 JSON 전체({ 로 시작해서 } 로 끝나는 부분)를 붙여넣어 주세요."
        return out
    by_code, by_name = {u["code"]: u for u in universe}, {u["name"]: u for u in universe}
    out["ai"], out["as_of"] = obj.get("ai"), obj.get("as_of")
    for item in obj["scores"]:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code", "")).strip()
        code = code.zfill(6) if code.isdigit() else code
        u = by_code.get(code) or by_name.get(str(item.get("name", "")).strip())
        if not u:
            out["warnings"].append(f"리포트에 없는 종목은 무시했어요: {item.get('name') or code}")
            continue
        rec = {}
        for d in SCORE_DIMS:
            v = _score_num(item.get(d))
            if v is None:
                out["warnings"].append(f"{u['name']}: '{d}' 점수가 없거나 숫자가 아니에요")
                rec[d] = None
                continue
            if v < 0 or v > 20:
                out["warnings"].append(f"{u['name']}: '{d}' {v:g}점이 0~20 범위를 벗어나 {min(max(v, 0), 20):g}점으로 바꿨어요")
                v = min(max(v, 0), 20)
            rec[d] = v
        dims, given = [rec[d] for d in SCORE_DIMS], _score_num(item.get("총점"))
        if all(x is not None for x in dims):
            total = sum(dims)
            if given is not None and abs(given - total) > 1:
                out["warnings"].append(f"{u['name']}: 총점({given:g})이 항목 합({total:g})과 달라 항목 합으로 계산했어요")
        else:
            total = min(max(given, 0), 100) if given is not None else None
        conf = _score_num(item.get("확신도"))
        rec.update(total=total, conf=int(min(max(conf, 1), 5)) if conf is not None else None, note=str(item.get("한줄", ""))[:80])
        if u["code"] in out["scores"]:
            out["warnings"].append(f"{u['name']}: 같은 종목이 두 번 나와 뒤의 값을 썼어요")
        out["scores"][u["code"]] = rec
    missing = [u["name"] for u in universe if u["code"] not in out["scores"]]
    if missing:
        out["warnings"].append("점수가 빠진 종목: " + ", ".join(missing))
    out["ok"] = bool(out["scores"])
    if not out["ok"]:
        out["error"] = out["error"] or "리포트의 종목과 맞는 점수가 하나도 없어요."
    return out


def baseline_scores(stocks) -> dict:
    """리포트 JSON의 규칙 기반 스윙 체크 점수 → {code: {항목..., total(100점 환산)}}. 재무가 비어 80점 만점인 종목은 100점으로 환산한다."""
    out = {}
    for s_ in stocks or []:
        sc = s_["scores"]["swing_check"]
        rec = {d: sc["parts"].get(d) for d in SCORE_DIMS}
        rec["total"] = sc["total"] / sc["max"] * 100 if sc.get("max") else None
        out[s_["code"]] = rec
    return out


def _rank_map(values: dict) -> dict:
    """{code: 점수} → {code: 순위}. 1이 가장 높은 점수, 동점은 같은 순위."""
    ser = pd.Series({k: v for k, v in values.items() if v is not None}, dtype=float)
    return ser.rank(ascending=False, method="min").astype(int).to_dict() if len(ser) else {}


def compare_scores(universe, baseline: dict, ais: dict) -> pd.DataFrame:
    """종목마다 AI별 총점·순위, 규칙 기반 점수, AI 평균·편차·평균 순위·순위 차, 판정(일치/의견 갈림).
    ais = {AI 이름: parse_ai_scores 결과}. 편차·순위 차는 AI가 둘 이상 점수를 매긴 종목에만 계산한다."""
    names = list(ais)
    totals = {n: {c: r["total"] for c, r in ais[n]["scores"].items()} for n in names}
    ranks = {n: _rank_map(totals[n]) for n in names}
    rows = []
    for u in universe:
        c = u["code"]
        row = {"종목명": u["name"], "코드": c}
        vals, rks = [], []
        for n in names:
            v = totals[n].get(c)
            row[n], row[f"{n} 순위"] = v, ranks[n].get(c)
            if v is not None:
                vals.append(v)
            if ranks[n].get(c) is not None:
                rks.append(ranks[n][c])
        row[BASELINE_NAME] = (baseline or {}).get(c, {}).get("total")
        row["AI 평균"] = sum(vals) / len(vals) if vals else None
        row["점수 편차"] = (max(vals) - min(vals)) if len(vals) >= 2 else None
        row["평균 순위"] = sum(rks) / len(rks) if rks else None
        row["순위 차"] = (max(rks) - min(rks)) if len(rks) >= 2 else None
        split = (row["점수 편차"] is not None and row["점수 편차"] >= DISAGREE_SCORE_GAP) or (row["순위 차"] is not None and row["순위 차"] >= DISAGREE_RANK_GAP)
        row["판정"] = "⚠ 의견 갈림" if split else ("일치" if len(vals) >= 2 else "—")
        rows.append(row)
    cols = (["종목명", "코드"] + names + [BASELINE_NAME, "AI 평균", "점수 편차"] + [f"{n} 순위" for n in names] + ["평균 순위", "순위 차", "판정"])
    return pd.DataFrame(rows, columns=cols)


def agreement_summary(ais: dict, top_n: int = 3) -> list:
    """AI 쌍마다 순위 상관(스피어먼, -1~1)과 상위 N개 겹침. 공통 종목이 3개 미만이면 상관은 None."""
    names, rows = list(ais), []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            ta = {c: r["total"] for c, r in ais[a]["scores"].items() if r["total"] is not None}
            tb = {c: r["total"] for c, r in ais[b]["scores"].items() if r["total"] is not None}
            common = [c for c in ta if c in tb]
            rho = None
            if len(common) >= 3:
                ra = pd.Series({c: ta[c] for c in common}).rank(ascending=False, method="average")
                rb = pd.Series({c: tb[c] for c in common}).rank(ascending=False, method="average")
                r_ = ra.corr(rb)
                rho = None if pd.isna(r_) else float(r_)
            overlap = None
            if len(common) >= top_n:
                overlap = len(set(sorted(common, key=lambda c: -ta[c])[:top_n]) & set(sorted(common, key=lambda c: -tb[c])[:top_n]))
            rows.append({"AI 쌍": f"{a} ↔ {b}", "공통 종목": len(common), "순위 상관(-1~1)": rho, f"상위 {top_n} 겹침": overlap})
    return rows


def dimension_frame(code: str, baseline: dict, ais: dict) -> pd.DataFrame:
    """한 종목의 항목별 점수. 행 = 수급·신호·추세·가격부담·재무, 열 = AI들 + 규칙 기반."""
    data = {n: [ais[n]["scores"].get(code, {}).get(d) for d in SCORE_DIMS] for n in ais}
    data[BASELINE_NAME] = [(baseline or {}).get(code, {}).get(d) for d in SCORE_DIMS]
    return pd.DataFrame(data, index=SCORE_DIMS, dtype=float)


def score_cell_style(v) -> str:
    """총점(0~100) 높낮이 색: 50을 기준으로 높으면 빨강, 낮으면 파랑(앱의 다른 표와 같은 색 규칙), 멀수록 진하게."""
    if v is None or (isinstance(v, float) and v != v):
        return ""
    t = max(-1.0, min(1.0, (v - 50) / 50))
    rgb = "224, 49, 49" if t >= 0 else "25, 113, 194"
    return f"background-color: rgba({rgb}, {0.10 + 0.40 * abs(t):.2f});"


def split_style(v) -> str:
    return "background-color: rgba(250, 204, 21, 0.25); font-weight: 700;" if isinstance(v, str) and v.startswith("⚠") else ""
# ---- AI 점수 비교 helpers end


# ============================================================
# ⚖️ 신호 vs 대조군 — '📌 신호 추적' 탭의 하위 탭.
#   대조군 = 같은 날 순매수 상위 10 전체(신호 여부와 무관). data/top10_baseline.json에 signal_tracker.py가 기록한다.
#   신호가 실제로 효과가 있는지는 '신호가 없던 종목'과 비교해야 알 수 있다. 같은 날끼리 짝지으면 그날 시장 전체의 움직임이 양쪽에 똑같이 들어가 덜어진다.
# ============================================================
# ---- 대조군 비교 helpers begin
BASELINE_PATH = "data/top10_baseline.json"
BASELINE_GROUPS = [
    ("상위 10 전체", lambda c: True),
    ("전환신호 있음", lambda c: c.get("is_transition") is True),
    ("전환신호 없음", lambda c: c.get("is_transition") is False),
    ("동시 등장 있음", lambda c: c.get("is_volume_supply") is True),
    ("동시 등장 없음", lambda c: c.get("is_volume_supply") is False),
]
BASELINE_FLAGS = {"전환신호": "is_transition", "거래량·수급 동시": "is_volume_supply"}


@st.cache_data(ttl=60)
def load_baseline_entries() -> list:
    """대조군 파일의 항목 목록. 없거나 깨졌으면 빈 목록."""
    if not os.path.exists(BASELINE_PATH):
        return []
    try:
        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            sigs = json.load(f).get("signals")
        return sigs if isinstance(sigs, list) else []
    except Exception:
        return []


def _entry_ret(e: dict, n: int, key: str = "ret"):
    return ((e.get("perf") or {}).get(key) or {}).get(str(n))


def baseline_group_stats(entries: list, horizon: int) -> pd.DataFrame:
    """집단(상위 10 전체 / 전환신호 있음·없음 / 동시 등장 있음·없음)별 D+horizon 통계. 신호 탭의 성과 요약과 같은 열."""
    rows = []
    for label, pred in BASELINE_GROUPS:
        sel = [e for e in entries if pred(e.get("conditions") or {})]
        rets = [r for r in (_entry_ret(e, horizon) for e in sel) if r is not None]
        exc = [x for x in (_entry_ret(e, horizon, "excess") for e in sel) if x is not None]
        wins, losses = [r for r in rets if r > 0], [r for r in rets if r < 0]
        avg_w = sum(wins) / len(wins) if wins else None
        avg_l = sum(losses) / len(losses) if losses else None
        rows.append({"집단": label, "표본 수": len(rets),
                     "평균 수익률(%)": sum(rets) / len(rets) if rets else None,
                     "중앙값(%)": float(pd.Series(rets).median()) if rets else None,
                     "승률(%)": 100 * len(wins) / len(rets) if rets else None,
                     "평균 이익(%)": avg_w, "평균 손실(%)": avg_l,
                     "손익비": (avg_w / abs(avg_l)) if (avg_w is not None and avg_l) else None,
                     "지수 비교 표본": len(exc), "평균 초과수익(%p)": sum(exc) / len(exc) if exc else None})
    df = pd.DataFrame(rows)
    cols = [c for c in df.columns if c != "집단"]
    df[cols] = df[cols].apply(pd.to_numeric, errors="coerce")
    return df


def baseline_daily_pairs(entries: list, horizon: int, flag: str = "is_transition") -> pd.DataFrame:
    """같은 날끼리 짝지은 비교: 그날 flag가 True인 종목들의 평균 D+horizon 수익률 vs False인 종목들의 평균.
    flag를 알 수 없는 항목(None)과 아직 수익률이 없는 항목은 뺀다. 두 집단이 모두 있는 날만 나온다. 최근 날짜 먼저."""
    by_date = {}
    for e in entries:
        r, f = _entry_ret(e, horizon), (e.get("conditions") or {}).get(flag)
        if r is None or f is None:
            continue
        by_date.setdefault(e["signal_date"], {True: [], False: []})[bool(f)].append(r)
    rows = []
    for d, g in sorted(by_date.items(), reverse=True):
        if g[True] and g[False]:
            m1, m0 = sum(g[True]) / len(g[True]), sum(g[False]) / len(g[False])
            rows.append({"신호일": d, "있음 종목 수": len(g[True]), "없음 종목 수": len(g[False]),
                         "있음 평균(%)": m1, "없음 평균(%)": m0, "차이(%p)": m1 - m0})
    return pd.DataFrame(rows, columns=["신호일", "있음 종목 수", "없음 종목 수", "있음 평균(%)", "없음 평균(%)", "차이(%p)"])


def pair_summary(pairs: pd.DataFrame) -> dict:
    if pairs is None or pairs.empty:
        return {"n_days": 0, "mean_diff": None, "n_better": 0}
    return {"n_days": len(pairs), "mean_diff": float(pairs["차이(%p)"].mean()), "n_better": int((pairs["차이(%p)"] > 0).sum())}


def baseline_coverage(entries: list) -> dict:
    """대조군 기록 현황: 항목 수, 날짜 수, 기간, 소급 항목 수, D+1 수익률이 계산된 항목 수."""
    dates = sorted({e["signal_date"] for e in entries})
    return {"n": len(entries), "n_dates": len(dates), "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
            "n_backfilled": sum(1 for e in entries if e.get("backfilled")), "n_with_d1": sum(1 for e in entries if _entry_ret(e, 1) is not None),
            "n_vs_unknown": sum(1 for e in entries if (e.get("conditions") or {}).get("is_volume_supply") is None)}
# ---- 대조군 비교 helpers end


# ---- 신호 추적 잠정값 안내 helpers begin
def tracker_provisional_note(updated_at, last_close_date):
    """추적 파일이 장중(평일 15:40 전)에 마지막으로 갱신됐고 가장 최근 종가 기록일이 바로 그날이면 안내 문구, 아니면 None.
    그 날짜 열의 값은 종가가 아니라 갱신 시각의 가격이고, 요약(평균 수익률 등)에도 섞여 있다."""
    try:
        dt = datetime.fromisoformat(str(updated_at))
    except (TypeError, ValueError):
        return None
    if not last_close_date or last_close_date != dt.strftime("%Y-%m-%d") or dt.weekday() >= 5 or (dt.hour, dt.minute) >= (15, 40):
        return None
    return (f"마지막 갱신이 {dt:%m-%d %H:%M}(장중, 15:40 전)이라 '{last_close_date}' 열의 값은 종가가 아니라 그 시각의 가격이에요. "
            "평균 수익률 같은 요약에도 이 잠정값이 섞여 있고, 15:40 이후 실행에서 확정 값으로 바뀝니다. 오늘 날짜의 새 신호·대조군은 15:40 이후에만 기록돼요.")
# ---- 신호 추적 잠정값 안내 helpers end


tab_supply, tab_volume, tab_value, tab_overlap, tab_tracker, tab_intraday, tab_screen, tab_reversal, tab_lookup, tab_journal, tab_ai, tab_cmp = st.tabs([
    "📊 순매수 상위", "📈 거래량 상위", "💰 저평가 후보", "🔥 동시 등장", "📌 신호 추적", "⏱ 장중 변동", "✅ 스윙 후보 스크리닝", "🔄 반등 후보", "🔍 종목 조회", "📒 매매 일지", "🤖 AI 분석용", "📊 AI 점수 비교",
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

# ---------------- 📌 신호 추적 ----------------
with tab_tracker:
    st.subheader("신호 추적 (신호일 종가 이후 추이)")
    tracker = None
    if os.path.exists(TRACKER_PATH):
        try:
            with open(TRACKER_PATH, "r", encoding="utf-8") as f:
                tracker = json.load(f)
        except Exception as e:
            st.warning(f"신호 추적 파일을 읽지 못했습니다: {e}")
    crit = (tracker or {}).get("criteria", {})
    st.caption(
        "매 거래일 15:40(장 마감 후)에 신호가 뜬 종목을 그날 종가와 함께 기록하고, 이후 거래일마다 종가를 옆에 이어서 적습니다. "
        "두 종류의 신호를 서로 따로 기록합니다. 종가 칸은 신호일 종가보다 높으면 빨강, 낮으면 파랑입니다. "
        f"신호 후 {crit.get('track_days', 40)}거래일치가 쌓이면 추적을 끝냅니다."
    )
    st.caption("⚠ 두 신호 모두 매수 신호로 검증된 적이 없습니다(전환신호는 2026-09-29 조건 변경 후 재검증 전, "
               "거래량·수급 동시는 관심이 쏠렸다는 뜻일 뿐). 이 탭은 '실제로 올랐는지'를 지켜보기 위한 기록이며, "
               "몇 건 안 되는 표본으로 결론을 내리면 안 됩니다. 종가는 매 거래일 15:40 이후 자동 갱신되고 실시간이 아닙니다.")
    all_signals = (tracker or {}).get("signals", [])
    _last_close = max((d for s_ in all_signals for d in (s_.get("closes") or {})), default=None)
    _prov = tracker_provisional_note((tracker or {}).get("updated_at"), _last_close)
    if _prov:
        st.warning(_prov)
    st.caption("가장 최근 거래일의 종가와 D+n은 다음 거래일 실행에서 공식 종가로 조금 바뀔 수 있어요(최근 관측: 최대 약 1.5%). 최신 날짜 값은 잠정으로 보세요.")
    opt_cols = st.columns([1, 1, 3])
    n_days = opt_cols[0].selectbox("표시할 최근 거래일 수", [10, 20, 40], index=1)
    only_active = opt_cols[1].checkbox("추적 중만 보기", value=False)
    sub_trans, sub_vs, sub_ctrl = st.tabs(["🔀 전환신호", "🔥 거래량·수급 동시", "⚖️ 신호 vs 대조군"])
    with sub_trans:
        st.caption(crit.get("transition", "전환신호(VCP 눌림 후 거래량급증+상승)가 뜬 종목 (순매수 상위 10 안에서 계산)")
                   + " — 순매수·거래량 순위는 요구하지 않습니다.")
        render_tracker_section([x for x in all_signals if x.get("type") == "transition"], n_days, only_active,
                               "아직 기록된 전환신호가 없습니다. 신호가 뜨면 그날 15:40 이후 이 탭에 나타납니다.")
    with sub_vs:
        st.caption(crit.get("volume_supply", "순매수 상위 10과 거래량 상위에 같은 날 함께 오른 종목")
                   + " — '🔥 동시 등장' 탭과 같은 정의이고, 전환신호는 요구하지 않습니다.")
        render_tracker_section([x for x in all_signals if x.get("type") == "volume_supply"], n_days, only_active,
                               "아직 기록된 종목이 없습니다. 두 순위에 함께 오른 종목이 생기면 그날 15:40 이후 이 탭에 나타납니다.")

    with sub_ctrl:
        _bents = load_baseline_entries()
        st.caption("대조군 = 같은 날 **순매수 상위 10 전체**(신호 여부와 무관)예요. 진입가(다음 거래일 시가)·D+n·지수 대비 계산은 신호와 똑같아서, "
                   "'신호가 있던 종목'이 '신호가 없던 상위 종목'보다 나았는지를 볼 수 있어요. 신호가 수급 상위만 보는 것보다 실제로 더했는지 확인하려는 용도예요.")
        if not _bents:
            st.info("대조군 기록이 아직 없습니다. 다음 거래일 15:40 이후 'EOD Snapshot' 실행에서 기록이 시작되고, 과거 날짜는 히스토리(eod_candidates.csv)로 소급해서 채워져요.")
        else:
            _cov = baseline_coverage(_bents)
            _hzs = [n for n in PERF_HORIZONS if any(_entry_ret(e, n) is not None for e in _bents)] or [1]
            _hz = st.selectbox("보유기간", _hzs, format_func=lambda n: f"D+{n}", key="ctrl_hz")
            cm = st.columns(4)
            cm[0].metric("대조군 기록", f"{_cov['n']}건", delta=f"{_cov['n_dates']}일치", delta_color="off")
            cm[1].metric("기간", f"{_cov['first']} ~ {_cov['last']}")
            cm[2].metric("소급해서 채운 항목", f"{_cov['n_backfilled']}건")
            cm[3].metric("D+1 계산된 항목", f"{_cov['n_with_d1']}건")
            st.markdown(f"##### 집단별 D+{_hz} 성과 (진입가 = 다음 거래일 시가)")
            _g = baseline_group_stats(_bents, _hz)
            _signed = ["평균 수익률(%)", "중앙값(%)", "평균 이익(%)", "평균 손실(%)", "평균 초과수익(%p)"]
            _gf = {c: (lambda v: f"{v:+.2f}") for c in _signed}
            _gf.update({"승률(%)": lambda v: f"{v:.0f}", "손익비": lambda v: f"{v:.2f}", "표본 수": lambda v: f"{int(v)}", "지수 비교 표본": lambda v: f"{int(v)}"})
            st.dataframe(safe_styler(_g, _gf, signed_cols=_signed), use_container_width=True, hide_index=True)
            st.markdown(f"##### 같은 날끼리 비교 (D+{_hz})")
            _flag_label = st.radio("무엇이 있는 종목 vs 없는 종목?", list(BASELINE_FLAGS), horizontal=True, key="ctrl_flag")
            _pairs = baseline_daily_pairs(_bents, _hz, BASELINE_FLAGS[_flag_label])
            _ps = pair_summary(_pairs)
            pm = st.columns(3)
            pm[0].metric("비교할 수 있는 날", f"{_ps['n_days']}일")
            pm[1].metric(f"평균 차이 ({_flag_label} 있음 − 없음)", f"{_ps['mean_diff']:+.2f}%p" if _ps["mean_diff"] is not None else "—")
            pm[2].metric(f"{_flag_label} 쪽이 나았던 날", f"{_ps['n_better']}/{_ps['n_days']}일" if _ps["n_days"] else "—")
            if _ps["n_days"]:
                st.dataframe(safe_styler(_pairs, {"있음 종목 수": lambda v: f"{int(v)}", "없음 종목 수": lambda v: f"{int(v)}", "있음 평균(%)": lambda v: f"{v:+.2f}",
                                                  "없음 평균(%)": lambda v: f"{v:+.2f}", "차이(%p)": lambda v: f"{v:+.2f}"},
                                         signed_cols=["있음 평균(%)", "없음 평균(%)", "차이(%p)"]), use_container_width=True, hide_index=True)
            else:
                st.info(f"D+{_hz} 수익률이 나온 날 중에 '{_flag_label} 있음'과 '없음' 종목이 같이 있는 날이 아직 없습니다.")
            if _hz == 1 and _ps["n_days"] and _ps["n_days"] < 30:
                st.warning(f"비교할 수 있는 날이 {_ps['n_days']}일뿐입니다. 하루에 몇 종목씩이라 우연의 영향이 크고, 30일 미만이면 결론으로 삼지 마세요.")
            st.caption("**읽는 법**: '같은 날끼리 비교'는 신호가 있던 종목과 없던 종목의 평균 수익률 차이예요. 그날 시장이 전체적으로 오르내린 영향은 두 쪽에 똑같이 들어가서 덜어져요. "
                       "차이가 플러스로 일관되게 나와야 신호가 부가 효과가 있다고 볼 수 있어요. 한두 날의 차이는 우연일 수 있어요. "
                       f"**한계**: 대조군은 과거 날짜를 히스토리 CSV로 소급해서 채웠고, 거기에는 거래량 순위가 없어서 소급분 {_cov['n_vs_unknown']}건은 '동시 등장' 여부를 알 수 없어 그 비교에서 빠져요. "
                       "수수료·세금·슬리피지는 반영하지 않았고, 순매수 상위 10 안에서만 비교해서 수급이 안 몰린 종목과의 비교는 아니에요. "
                       "코스닥 종목은 지수 비교가 비어 있을 수 있어요(시장 구분이 안 잡힌 경우).")

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
            lookup_est = fetch_investor_estimate(lookup_code)
            lookup_inv = fetch_investor_daily(lookup_code)

        if lookup_price is not None:
            st.markdown(f"### {lookup_price:,.0f}원 &nbsp; {colored_pct_html(lookup_pct)}", unsafe_allow_html=True)
            st.markdown(f"**상태: {price_status_badge(lookup_pct)}**")
            if lookup_flags:
                st.error("🚨 거래소 지정 상태: " + " · ".join(lookup_flags) +
                         " — 아래 전환신호·국면 판단은 이 상태를 반영하지 않으니 근거로 쓰지 마세요.")
        else:
            st.warning("현재가 조회 실패 — 종목코드를 확인하거나 잠시 후 다시 시도해 주세요 (일시적인 조회 실패일 수 있습니다).")

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

        render_investor_section(lookup_est, lookup_inv)

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

# ---------------- 📒 매매 일지 ----------------
with tab_journal:
    st.subheader("📒 매매 일지")
    _jcfg = journal_settings()
    _missing = [n for n, k in (("JOURNAL_GITHUB_TOKEN", "token"), ("JOURNAL_REPO", "repo"), ("JOURNAL_PASSWORD", "password")) if not _jcfg[k]]
    if _missing:
        st.info(SETUP_JOURNAL_GUIDE)
        st.caption("아직 없는 설정: " + ", ".join(_missing))
    elif not st.session_state.get("journal_ok"):
        _pw = st.text_input("비밀번호", type="password", key="journal_pw")
        if _pw:
            if hmac.compare_digest(_pw.encode("utf-8"), _jcfg["password"].encode("utf-8")):
                st.session_state["journal_ok"] = True
                st.rerun()
            else:
                time.sleep(1)
                st.error("비밀번호가 맞지 않습니다.")
        st.caption("매매 일지는 비밀번호를 입력해야 볼 수 있습니다.")
    else:
        render_journal_tab()

# ---------------- 🤖 AI 분석용 ----------------
with tab_ai:
    st.subheader("🤖 AI 분석용 리포트")
    _rep = load_ai_report()
    _status = _rep.get("status") or {}

    def _status_text(stt: dict) -> str:
        why = ", ".join(f"{k}×{v.get('count')}({v.get('msg')})" for k, v in (stt.get("fail_reasons") or {}).items())
        return f"마지막 실행 {stt.get('at', '—')} — {stt.get('message', '—')}" + (f" [실패 사유 코드: {why}]" if why and not stt.get("ok") else "")

    if not _rep["md"]:
        st.info("아직 리포트가 없습니다. GitHub의 Actions 탭에서 'AI Report'를 한 번 실행(Run workflow)하면 만들어져요. "
                "이후에는 평일 08:30 · 09:50 · 10:20 · 11:40 · 13:40 · 14:50 · 15:55에 자동으로 갱신됩니다.")
        if _status:
            (st.warning if not _status.get("ok") else st.caption)(_status_text(_status))
            if not _status.get("ok"):
                st.caption("증권사 연결이 안 되는 시간대(점검 등)였거나 일시적인 오류일 수 있어요. 장중에 한 번 더 실행해 보세요.")
    else:
        if _status and not _status.get("ok"):
            st.warning("가장 최근 실행이 실패해서 이전 리포트를 보여 주고 있어요. " + _status_text(_status))
        _payload = _rep["json"] or {}
        _meta = _payload.get("meta", {})
        _stocks = _payload.get("stocks", [])
        _gen = _meta.get("generated_at")
        mc = st.columns(3)
        mc[0].metric("생성 시각", _gen or "—")
        mc[1].metric("포함 종목", f"{len(_stocks)}개")
        mc[2].metric("분량", f"짧은 {len(_rep['short'] or ''):,}자 / 전체 {len(_rep['md']):,}자")
        try:
            _age_h = (datetime.now(KST) - datetime.strptime(_gen, "%Y-%m-%d %H:%M").replace(tzinfo=KST)).total_seconds() / 3600
            st.caption(f"데이터 기준: {_meta.get('phase', '—')} · 생성된 지 약 {_age_h:.0f}시간 지났어요.")
            if _age_h > 24:
                st.warning("리포트가 하루 넘게 갱신되지 않았습니다. 휴장일이 아니라면 Actions의 'AI Report' 실행 기록을 확인하세요.")
        except Exception:
            st.caption(f"데이터 기준: {_meta.get('phase', '—')}")
        for _n in _meta.get("notes", []):
            st.warning(_n)
        if _stocks:
            _pct = lambda v: f"{v:+.2f}"
            st.dataframe(
                safe_styler(ai_report_table(_payload), {"순매수 순위": lambda v: f"{int(v)}위", "현재가": lambda v: f"{v:,.0f}", "등락(%)": _pct,
                                                        "RSI": lambda v: f"{v:.1f}", "앱 종합": lambda v: f"{v:.0f}"}, signed_cols=["등락(%)"]),
                use_container_width=True, hide_index=True)
        st.markdown("##### ChatGPT에 쓰는 방법")
        st.markdown("1. **가장 확실한 방법 — 붙여넣기**: 아래 '짧은 버전'의 복사 버튼을 눌러 ChatGPT 입력창에 붙여넣고 보내세요. 맨 위에 분석 지시문이 들어 있어 그대로 분석을 시작해요.\n"
                    "2. **주소로 읽히기**: 아래 주소를 알려 주면 ChatGPT가 열 수 있는 환경에서는 스스로 읽어요. 열 수 있는지는 환경마다 달라서, "
                    "먼저 \"이 주소 내용의 첫 부분을 알려 줘\"로 확인하고 이상하면 1번을 쓰세요. 새로 올라간 파일이 주소에 반영되기까지 몇 분 걸릴 수 있어요.\n"
                    "3. **파일로 올리기**: 파일 첨부가 되는 환경이면 아래 다운로드 버튼으로 받아 올리세요.")
        st.code(f"{AI_REPORT_RAW_BASE}/ai_report_short.md\n{AI_REPORT_RAW_BASE}/ai_report.md", language="text")
        if _rep["short"]:
            with st.expander("짧은 버전 (지시문 + 표) — 복사용"):
                st.code(_rep["short"], language="markdown")
            st.download_button("⬇ 짧은 버전 (.md)", _rep["short"], file_name="ai_report_short.md", mime="text/markdown", key="ai_dl_short")
        with st.expander("전체 버전 (종목별 상세 포함) — 복사용"):
            st.code(_rep["md"], language="markdown")
        st.download_button("⬇ 전체 버전 (.md)", _rep["md"], file_name="ai_report.md", mime="text/markdown", key="ai_dl_full")
        if _rep["json"]:
            st.download_button("⬇ 구조화 데이터 (.json)", json.dumps(_rep["json"], ensure_ascii=False, indent=1), file_name="ai_report.json",
                               mime="application/json", key="ai_dl_json")
        st.caption("이 주소와 리포트는 공개 저장소에 올라가므로 누구나 볼 수 있어요. 시세·수급·점수만 들어 있고 매매 일지, API 키, 비밀번호는 들어 있지 않습니다. "
                   "스윙 체크 점수와 앱 종합점수는 임의 기준이라 검증되지 않았고, 투자 자문이 아닙니다. 관심 종목을 넣으려면 저장소의 data/watch_codes.txt에 "
                   "한 줄에 종목코드 하나씩 적어 두세요(예: 098460 고영).")

# ---------------- 📊 AI 점수 비교 ----------------
with tab_cmp:
    st.subheader("📊 AI 점수 비교")
    _crep = load_ai_report()
    _cpay = _crep.get("json") or {}
    _cstocks = _cpay.get("stocks", [])
    if not _cstocks or not _crep.get("blind"):
        st.info("점수 매기기용 리포트가 아직 없습니다. 'AI Report' 워크플로가 한 번 실행되면 만들어져요 (🤖 AI 분석용 탭 안내 참고).")
    else:
        _cmeta = _cpay.get("meta", {})
        _gen = _cmeta.get("generated_at", "—")
        _universe = [{"code": x["code"], "name": x["name"]} for x in _cstocks]
        st.markdown("같은 리포트를 여러 AI에게 주고, 돌려받은 점수를 나란히 비교해 보는 화면이에요. 서로를 비판시키지 않고 **같은 기준으로 매긴 결과**만 봅니다.\n\n"
                    "1. 아래 '점수 매기기용 리포트'를 복사해서 **AI마다 새 대화에, 같은 내용으로** 보내세요. (다른 AI의 점수는 보여주지 마세요 — 독립 채점이어야 비교가 의미 있어요.)\n"
                    "2. AI가 돌려준 JSON을 아래 칸에 붙여넣으세요. 이 대화의 Claude 결과도 같은 방식으로 붙여넣으면 돼요.\n"
                    "3. 점수표, 의견이 갈린 종목, 항목별 그래프를 보고 사용자님이 판단하세요.")
        st.caption(f"리포트 생성 시각: {_gen} · 이 리포트에는 규칙 기반 점수(스윙 체크·앱 종합)가 들어 있지 않아요. AI들이 그 점수를 따라 쓰는 걸 막으려는 거예요.")
        with st.expander("① 점수 매기기용 리포트 — 복사용"):
            st.code(_crep["blind"], language="markdown")
        st.download_button("⬇ 점수 매기기용 리포트 (.md)", _crep["blind"], file_name="ai_report_blind.md", mime="text/markdown", key="cmp_dl_blind")
        st.markdown("##### ② AI 결과 붙여넣기")
        _defaults, _slots = ["Claude", "GPT", ""], []
        for _i, _col in enumerate(st.columns(3)):
            with _col:
                _nm = st.text_input(f"AI {_i + 1} 이름", value=_defaults[_i], key=f"cmp_name_{_i}")
                _tx = st.text_area(f"AI {_i + 1} 결과 JSON", value="", height=170, key=f"cmp_text_{_i}", placeholder='{"ai": "...", "scores": [...]}')
            _slots.append((_nm.strip(), _tx))
        _ais, _used = {}, set(RESERVED_NAMES)
        for _i, (_nm, _tx) in enumerate(_slots):
            if not _tx.strip():
                continue
            _res = parse_ai_scores(_tx, _universe)
            _label = _nm or _res.get("ai") or f"AI {_i + 1}"
            while _label in _used:
                _label += " (2)"
            _used.add(_label)
            if not _res["ok"]:
                st.error(f"{_label}: {_res['error']}")
                continue
            _ais[_label] = _res
            _warns = list(_res["warnings"])
            if _res.get("as_of") and _gen != "—" and _gen not in str(_res["as_of"]):
                _warns.insert(0, f"리포트 생성 시각({_gen})과 다른 시각({_res['as_of']}) 기준으로 매긴 점수예요. 같은 리포트로 매겼는지 확인하세요.")
            st.success(f"{_label}: {len(_res['scores'])}/{len(_universe)}종목 점수를 읽었어요" + (f" (확인할 점 {len(_warns)}개)" if _warns else ""))
            if _warns:
                with st.expander(f"{_label} — 확인할 점"):
                    for _w in _warns:
                        st.write("- " + _w)
        if _ais:
            _base = baseline_scores(_cstocks)
            _cmp = compare_scores(_universe, _base, _ais)
            _sort_labels = {"avg": "AI 평균 높은 순", "split": "의견 갈림 큰 순", "order": "리포트 순서"}
            _sk = st.selectbox("정렬", list(_sort_labels), format_func=lambda k: _sort_labels[k], key="cmp_sort")
            if _sk == "avg":
                _cmp = _cmp.sort_values("AI 평균", ascending=False, na_position="last", ignore_index=True)
            elif _sk == "split":
                _cmp = _cmp.sort_values("점수 편차", ascending=False, na_position="last", ignore_index=True)
            _n_split = int(_cmp["판정"].astype(str).str.startswith("⚠").sum())
            _top = _cmp.dropna(subset=["AI 평균"]).sort_values("AI 평균", ascending=False)
            mc = st.columns(3)
            mc[0].metric("비교한 AI", f"{len(_ais)}개")
            mc[1].metric("의견 갈린 종목", f"{_n_split}개" if len(_ais) >= 2 else "—")
            mc[2].metric("AI 평균 1위", _top.iloc[0]["종목명"] if len(_top) else "—",
                         delta=(f"{_top.iloc[0]['AI 평균']:.0f}점" if len(_top) else None), delta_color="off")
            _rank_cols = [f"{n} 순위" for n in _ais]
            _score_cols = list(_ais) + [BASELINE_NAME, "AI 평균"]
            _fmt = {c: (lambda v: f"{v:.0f}") for c in _score_cols}
            _fmt.update({"점수 편차": lambda v: f"{v:.0f}", "평균 순위": lambda v: f"{v:.1f}", "순위 차": lambda v: f"{int(v)}"})
            _fmt.update({c: (lambda v: f"{int(v)}위") for c in _rank_cols})
            st.dataframe(safe_styler(_cmp, _fmt, col_style_fns={**{c: score_cell_style for c in _score_cols}, "판정": split_style}),
                         use_container_width=True, hide_index=True)
            st.caption(f"색: 빨강은 높은 점수, 파랑은 낮은 점수예요(50점 기준). '{BASELINE_NAME}'은 앱의 규칙 기반 스윙 체크 점수를 100점으로 환산한 기준선이에요(검증 안 됨). "
                       f"'의견 갈림'은 AI 간 총점 차이가 {DISAGREE_SCORE_GAP}점 이상이거나 순위 차이가 {DISAGREE_RANK_GAP} 이상일 때 표시해요(임의 기준). "
                       "AI마다 후하고 박한 정도가 달라서 절대 점수보다 순위와 항목별 차이를 보는 게 더 의미 있어요.")
            st.markdown("##### 총점 비교")
            st.bar_chart(_cmp.set_index("종목명")[list(_ais) + [BASELINE_NAME]])
            if len(_ais) >= 2:
                st.markdown("##### AI 간 일치도")
                _agree = pd.DataFrame(agreement_summary(_ais))
                st.dataframe(safe_styler(_agree, {"공통 종목": lambda v: f"{int(v)}", "순위 상관(-1~1)": lambda v: f"{v:+.2f}", "상위 3 겹침": lambda v: f"{int(v)}/3"}),
                             use_container_width=True, hide_index=True)
                st.caption("순위 상관이 1에 가까우면 두 AI가 종목 순서를 비슷하게 매긴 거고, 0 근처면 거의 무관해요. 일치한다고 맞는 건 아니에요 — 같은 데이터에 같은 식으로 반응했을 수 있어요.")
            st.markdown("##### 항목별로 보기")
            _pick = st.selectbox("종목", [u["code"] for u in _universe], format_func=lambda c: next(u["name"] for u in _universe if u["code"] == c), key="cmp_dim_pick")
            _dim = dimension_frame(_pick, _base, _ais)
            st.bar_chart(_dim)
            _notes = [{"AI": n, "총점": r["scores"].get(_pick, {}).get("total"), "확신도": r["scores"].get(_pick, {}).get("conf"),
                       "한줄": r["scores"].get(_pick, {}).get("note", "")} for n, r in _ais.items()]
            st.dataframe(safe_styler(pd.DataFrame(_notes), {"총점": lambda v: f"{v:.0f}", "확신도": lambda v: f"{int(v)}"}),
                         use_container_width=True, hide_index=True)
            st.download_button("⬇ 비교표 (.csv)", _cmp.to_csv(index=False).encode("utf-8-sig"), file_name="ai_score_compare.csv", mime="text/csv", key="cmp_dl_csv")
        st.caption("붙여넣은 내용은 이 브라우저 세션에만 있고 저장되지 않아요. 점수는 모두 검증되지 않은 참고용이고, 투자 자문이 아닙니다.")

st.divider()
if st.button("지금 새로고침"):
    st.cache_data.clear()
    st.rerun()
