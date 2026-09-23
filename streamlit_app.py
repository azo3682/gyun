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

VALUATION_RANK_API_PATH = "/uapi/domestic-stock/v1/ranking/market-value"
VALUATION_RANK_TR_ID = "FHPST01790000"
VALUATION_FISCAL_YEAR = "2025"  # 회계연도(결산 기준) — 매년 갱신 필요

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


@st.cache_data(ttl=1800)
def fetch_valuation_rank(sort_code: str = "23", top_n: int = 30, per_max: float = 50.0):
    """전체 시장 PER/PBR 순위. (순위 리스트, 원본 응답 첫 항목) 반환.
    API가 반환한 순서(오름/내림 여부 미확인)를 신뢰하지 않고, 받은 데이터를
    직접 PER 오름차순으로 재정렬하고 상식적인 범위(0 < PER <= per_max)만 남긴다
    — EPS가 0에 가까운 종목은 PER이 수천 배로 튀는 경우가 있어 그런 값은 제외.
    재무비율은 하루에도 거의 안 바뀌어 캐시를 길게 둠."""
    params = {
        "fid_trgt_cls_code": "0",
        "fid_cond_mrkt_div_code": "J",
        "fid_cond_scr_div_code": "20179",
        "fid_input_iscd": "0000",
        "fid_div_cls_code": "6",  # 보통주만 (우선주 제외)
        "fid_input_price_1": "0",
        "fid_input_price_2": "0",
        "fid_vol_cnt": "0",
        "fid_input_option_1": VALUATION_FISCAL_YEAR,
        "fid_input_option_2": "3",  # 결산(연간)
        "fid_rank_sort_cls_code": sort_code,
        "fid_blng_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
    }
    resp = requests.get(f"{BASE_URL}{VALUATION_RANK_API_PATH}", headers=kis_headers(VALUATION_RANK_TR_ID),
                         params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"KIS API 오류: {data.get('msg1')}")

    output = data.get("output", [])
    raw_sample = output[0] if output else {}
    candidates = []
    for item in output:
        name = item.get("hts_kor_isnm", "")
        if is_fund_product(name):
            continue
        try:
            per = float(item.get("per", "") or 0)
            pbr = float(item.get("pbr", "") or 0)
        except (TypeError, ValueError):
            continue
        if not (0 < per <= per_max):  # 적자·EPS 0에 가까운 이상치 제외
            continue
        candidates.append({
            "stock_code": item.get("mksc_shrn_iscd", ""),
            "stock_name": name,
            "price": item.get("stck_prpr", ""),
            "day_pct": float(item.get("prdy_ctrt", 0) or 0),
            "per": per,
            "pbr": pbr,
        })

    candidates.sort(key=lambda r: r["per"])  # 낮은 PER부터 — API 원본 순서는 신뢰 안 함
    rows = []
    for i, c in enumerate(candidates[:top_n], start=1):
        rows.append({"rank": i, **c})
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


@st.cache_data(ttl=30)
def fetch_current_price(stock_code: str):
    """(현재가, 전일대비 등락률%) 반환. 실패하면 (None, None)."""
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
            return None, None
        output = data.get("output", {})
        price = float(output.get("stck_prpr", 0) or 0)
        pct = float(output.get("prdy_ctrt", 0) or 0)
        return price, pct
    except Exception:
        return None, None


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
    """VCP(눌림) 상태였다가 거래량급증이 뜬 경우만 True.

    2026-09-21 백테스트(코스닥 성장주 38종목, 5년)에서 근사t값 2.3~2.5로
    반복 확인된, 유일하게 통계적 근거가 있는 신호. 정배열/20일모멘텀/RSI/
    VCP단독/볼린저밴드 4종은 전부 효과가 확인되지 않아 참고용으로만 남김.
    """
    if df.empty or len(df) < 60:
        return False
    close, vol = df["stck_clpr"], df["acml_vol"]
    high, low = df["stck_hgpr"], df["stck_lwpr"]

    vol_avg20 = vol.rolling(20).mean().shift(1)
    vol_surge = vol >= vol_avg20 * 1.5

    daily_range = (high - low) / close
    recent5 = daily_range.rolling(5).std()
    prior15 = daily_range.rolling(15).std().shift(5)
    vcp = recent5 < prior15 * 0.8

    recent_vcp = vcp.shift(1).rolling(3, min_periods=1).max().fillna(0).astype(bool)
    transition = vol_surge.fillna(False).astype(bool) & recent_vcp
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
    """signed cols: 양수 빨강/음수 파랑 + '만주'/부호 단위 표시. plain_cols: {컬럼명: 포맷함수}로 단위만 적용."""
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
        return f"{'+' if v > 0 else ''}{v:,.2f}%"

    existing = [c for c in cols if c in df.columns]
    styler = df.style
    if existing:
        styler = styler.map(_color, subset=existing)
        fmt_map = {c: (fmt_shares if c != "등락률(%)" and c != "당일등락률(%)" else _fmt_pct) for c in existing}
        styler = styler.format(fmt_map)
    if plain_cols:
        styler = styler.format({c: f for c, f in plain_cols.items() if c in df.columns})
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
with tab_value:
    st.subheader("저평가 후보 (전체 시장 PER 낮은 순)")
    st.caption(f"회계연도 {VALUATION_FISCAL_YEAR} 결산 기준, 코스피/코스닥 보통주 중 PER이 0~50배 범위인 종목만 낮은 순으로 정렬합니다 "
               "(EPS가 0에 가까워 PER이 수천 배로 튀는 이상치는 제외). API가 반환한 원래 순서는 신뢰할 수 없어 직접 재정렬한 것이라, "
               "이 목록이 '전체 시장에서 진짜 가장 싼 30개'라는 보장은 아직 없습니다 — 참고용으로만 봐주세요. "
               "PER이 낮다고 매수 신호도 아닙니다. 관리종목·투자위험 등 문제가 있어서 싼 경우도 섞여 있으니, "
               "아래 공시 확인을 꼭 같이 보세요.")

    try:
        value_rows, value_raw_sample = fetch_valuation_rank(sort_code="23", top_n=30)
        value_error = None
    except Exception as e:
        value_rows, value_raw_sample, value_error = [], {}, str(e)

    if value_error:
        st.warning(f"저평가 순위 조회 실패: {value_error}")

    if value_rows:
        value_df = pd.DataFrame(value_rows).rename(columns={
            "rank": "순위", "stock_code": "종목코드", "stock_name": "종목명",
            "price": "현재가", "day_pct": "당일등락률(%)", "per": "PER", "pbr": "PBR",
        })
        st.dataframe(style_signed(value_df, ["당일등락률(%)"]), use_container_width=True, hide_index=True)
        with st.expander("원본 응답 확인 (필드명 검증용)"):
            st.json(value_raw_sample)

        st.markdown("**🎯💰 오늘 순매수·거래량 상위와 동시에 저PER인 종목**")
        st.caption("검증된 수급/거래량 신호와 저평가가 겹치는, 가장 근거가 탄탄한 조합입니다.")
        candidate_codes = {r["stock_code"] for r in buy_rows}
        if volume_rows:
            candidate_codes |= {r["stock_code"] for r in volume_rows}
        overlap = [r for r in value_rows if r["stock_code"] in candidate_codes]
        if overlap:
            for r in overlap:
                with st.expander(f"{r['stock_name']}({r['stock_code']}) · PER {r['per']:.1f}배 · PBR {r['pbr']:.1f}배"):
                    orisky = check_disclosure_risk(r["stock_code"])
                    if orisky:
                        st.error("⚠️ 최근 30일 내 주의 공시 발견:\n" + "\n".join(f"- {x}" for x in orisky))
                    elif DART_API_KEY:
                        st.success("최근 30일 내 주의 공시 없음")
        else:
            st.info("오늘 순매수·거래량 상위 후보 중에는 저PER 상위 30위 안에 든 종목이 없습니다.")
    else:
        st.info("저평가 후보 데이터를 가져오지 못했습니다.")

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
    st.caption("후보 기준: 전환신호(VCP 눌림 후 거래량급증) — 백테스트로 검증된 신호입니다.")

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
    st.caption("2026-09-21 백테스트(코스닥 성장주 38종목, 5년) 결과, 검증된 신호는 "
               "**전환신호(눌림 후 거래량급증) 하나뿐**입니다. 정배열·20일모멘텀·RSI·VCP단독·"
               "볼린저밴드는 효과가 확인되지 않아 참고 정보로만 표시합니다.")

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
        transition = compute_transition_signal(df)
        checks = {k: v for k, v in tech.items() if k != "RSI값"}
        passed = sum(1 for v in checks.values() if v is True)
        total = sum(1 for v in checks.values() if v is not None)
        scored_rows.append({**row, "_tech": tech, "_checks": checks, "_passed": passed,
                             "_total": total, "_transition": transition})

    # 전환신호 있는 종목을 최상단으로, 그다음은 참고점수순
    scored_rows.sort(key=lambda r: (not r["_transition"], -r["_passed"], r["rank"]))

    n_transition = sum(1 for r in scored_rows if r["_transition"])
    if n_transition:
        st.success(f"🎯 전환신호 종목 {n_transition}개 발견")
    else:
        st.info("오늘은 전환신호(검증된 신호)가 뜬 종목이 없습니다. 아래는 전부 참고용입니다.")

    # 재진입 후보 / 관찰 목록 (전일 마감 배치가 생성한 데이터, GitHub Actions가 커밋)
    EOD_SNAPSHOT_PATH = "data/eod_snapshot.json"
    WATCHLIST_PATH = "data/watchlist.json"
    if os.path.exists(EOD_SNAPSHOT_PATH):
        with open(EOD_SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            eod_snapshot = json.load(f)
        reentry = eod_snapshot.get("reentry_candidates", [])
        if reentry:
            st.markdown("**🎯 재진입 후보** (급등 후 관찰 중이던 종목이 눌림 상태로 복귀)")
            for r in reentry:
                st.markdown(f"- {r['stock_name']}({r['stock_code']}) — {r['days_watched']}거래일 관찰 후 눌림 확인 "
                            f"(최초 급등 +{r['initial_pct']*100:.2f}%)")

    if os.path.exists(WATCHLIST_PATH):
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            watchlist = json.load(f)
        if watchlist:
            with st.expander(f"👀 관찰 목록 ({len(watchlist)}개 — 급등 후 눌림 대기 중)"):
                watch_rows = [{"종목명": v["stock_name"], "종목코드": k,
                               "최초급등률(%)": v["initial_pct"] * 100,
                               "관찰경과(거래일)": v["days_watched"]}
                              for k, v in watchlist.items()]
                st.dataframe(pd.DataFrame(watch_rows), use_container_width=True, hide_index=True)
                st.caption("이 종목들은 당일 +7% 이상 급등해서 추격 대신 눌림을 기다리는 중입니다. "
                           "눌림이 오면 위 '재진입 후보'로 자동 승격됩니다 (최대 15거래일 대기).")

    for row in scored_rows:
        code, name = row["stock_code"], row["stock_name"]
        tech, checks, passed, total = row["_tech"], row["_checks"], row["_passed"], row["_total"]
        transition = row["_transition"]
        title_prefix = "🎯 전환신호 " if transition else "참고 "
        with st.expander(f"{title_prefix}· 원순위 {row['rank']}위 · {name} ({code})"):
            if transition:
                st.success("🎯 **전환신호 확인** — VCP(눌림) 이후 거래량급증. 검증된 신호입니다.")
            else:
                st.caption("전환신호 없음 (검증된 신호 기준 미충족)")

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
               "전환신호와 달리 이 분류 자체는 아직 검증된 신호가 아니라 참고용입니다 (코스피/코스닥 전체를 매번 스캔할 수는 없어, "
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
            rprice, rpct = fetch_current_price(code)
            reversal_found.append({"code": code, "name": name, "label": rlabel, "desc": rdesc,
                                     "fn": rfn, "price": rprice, "pct": rpct})

    if not reversal_found:
        st.info("현재 후보군(순매수·거래량 상위) 안에는 하락추세 반등 패턴이 없습니다.")
    else:
        for item in reversal_found:
            with st.expander(f"{item['name']}({item['code']}) — {item['label']}"):
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
            lookup_price, lookup_pct = fetch_current_price(lookup_code)
            lookup_risky = check_disclosure_risk(lookup_code)
            lookup_per, lookup_pbr, lookup_eps, lookup_bps, lookup_val_raw = fetch_valuation(lookup_code)

        if lookup_price is not None:
            st.markdown(f"### {lookup_price:,.0f}원 &nbsp; {colored_pct_html(lookup_pct)}", unsafe_allow_html=True)
            st.markdown(f"**상태: {price_status_badge(lookup_pct)}**")
        else:
            st.warning("현재가 조회 실패 — 종목코드를 확인해주세요.")

        st.markdown("**밸류에이션 (PER · PBR)**")
        if lookup_per is not None or lookup_pbr is not None:
            val_cols = st.columns(4)
            val_cols[0].metric("PER", f"{lookup_per:.2f}배" if lookup_per is not None else "N/A")
            val_cols[1].metric("PBR", f"{lookup_pbr:.2f}배" if lookup_pbr is not None else "N/A")
            val_cols[2].metric("EPS", f"{lookup_eps:,.0f}원" if lookup_eps is not None else "N/A")
            val_cols[3].metric("BPS", f"{lookup_bps:,.0f}원" if lookup_bps is not None else "N/A")
            st.caption("PBR 1배 미만이면 장부가치보다 싸게 거래 중이라는 뜻입니다. 다만 이것만으로 '저평가'라 단정할 순 없고, "
                       "동종업계 평균과 비교하거나 왜 싼지(실적 부진 등) 같이 확인하셔야 합니다.")
        else:
            st.caption("PER/PBR 조회 실패 (적자 기업은 PER이 제공되지 않을 수 있습니다).")
        with st.expander("원본 응답 확인 (필드명 검증용)"):
            st.json(lookup_val_raw)

        lookup_transition = compute_transition_signal(lookup_df)
        if lookup_transition:
            st.success("🎯 **전환신호 확인** — VCP(눌림) 이후 거래량급증. 백테스트로 검증된 신호입니다.")
        else:
            st.info("전환신호 없음 (검증된 신호 기준 미충족 — 아래 지표는 참고용입니다)")

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
