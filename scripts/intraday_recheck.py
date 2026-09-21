# -*- coding: utf-8 -*-
"""
scripts/common.py
GitHub Actions에서 실행되는 스크립트들이 공유하는 데이터 조회 함수.
streamlit_app.py와 로직은 동일하나, st.cache 데코레이터 없이 순수 함수로 작성.
환경변수(GitHub Actions Secrets)에서 키를 읽는다.
"""

import io
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

KST = ZoneInfo("Asia/Seoul")

APP_KEY = os.environ.get("KIS_APP_KEY", "")
APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
DART_API_KEY = os.environ.get("DART_API_KEY", "")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

BASE_URL = "https://openapi.koreainvestment.com:9443"
RANKING_API_PATH = "/uapi/domestic-stock/v1/quotations/foreign-institution-total"
RANKING_TR_ID = "FHPTJ04400000"
DAILY_CHART_API_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
DAILY_CHART_TR_ID = "FHKST03010100"
CURRENT_PRICE_API_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
CURRENT_PRICE_TR_ID = "FHKST01010100"

RISK_KEYWORDS = ["유상증자", "무상감자", "감자", "관리종목", "상장폐지",
                  "횡령", "배임", "불성실공시", "자본잠식", "거래정지"]

_token_cache = {"token": None, "expires_at": 0}


def get_access_token() -> str:
    if _token_cache["token"] and _token_cache["expires_at"] > time.time() + 300:
        return _token_cache["token"]
    resp = requests.post(
        f"{BASE_URL}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 86400))
    return _token_cache["token"]


def kis_headers(tr_id: str) -> dict:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {get_access_token()}",
        "appkey": APP_KEY, "appsecret": APP_SECRET,
        "tr_id": tr_id, "custtype": "P",
    }


def fetch_investor_ranking(rank_type: str, top_n: int = 10) -> list[dict]:
    params = {
        "FID_COND_MRKT_DIV_CODE": "V", "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0",
        "FID_RANK_SORT_CLS_CODE": "0" if rank_type == "buy" else "1",
        "FID_ETC_CLS_CODE": "0",
    }
    resp = requests.get(f"{BASE_URL}{RANKING_API_PATH}", headers=kis_headers(RANKING_TR_ID),
                         params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"KIS API 오류: {data.get('msg1')}")
    now = datetime.now(KST).isoformat(timespec="seconds")
    rows = []
    for i, item in enumerate(data.get("output", [])[:top_n], start=1):
        rows.append({
            "ts": now, "rank_type": rank_type, "rank": i,
            "stock_code": item.get("mksc_shrn_iscd", ""), "stock_name": item.get("hts_kor_isnm", ""),
            "foreign_net": float(item.get("frgn_ntby_qty", 0) or 0),
            "inst_net": float(item.get("orgn_ntby_qty", 0) or 0),
            "combined_net": float(item.get("ntby_qty", 0) or 0),
        })
    return rows


def fetch_daily_ohlcv(stock_code: str) -> pd.DataFrame:
    end = datetime.now(KST).strftime("%Y%m%d")
    start = (datetime.now(KST) - timedelta(days=130)).strftime("%Y%m%d")
    params = {
        "fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code,
        "fid_input_date_1": start, "fid_input_date_2": end,
        "fid_period_div_code": "D", "fid_org_adj_prc": "0",
    }
    time.sleep(0.15)
    try:
        resp = requests.get(f"{BASE_URL}{DAILY_CHART_API_PATH}", headers=kis_headers(DAILY_CHART_TR_ID),
                             params=params, timeout=10)
        resp.raise_for_status()
        rows = resp.json().get("output2", [])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df = df[df["stck_bsop_date"] != ""]
        for col in ["stck_clpr", "stck_oprc", "stck_hgpr", "stck_lwpr", "acml_vol"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.sort_values("stck_bsop_date").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def compute_rsi(closes: pd.Series, period: int = 14):
    delta = closes.diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain, avg_loss = gain.rolling(period).mean(), loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty and pd.notna(rsi.iloc[-1]) else None


def analyze_technicals(df: pd.DataFrame) -> dict:
    result = {"정배열": None, "거래량급증": None, "20일모멘텀": None,
               "RSI과열아님": None, "변동성수축(VCP)": None, "RSI값": None}
    if df.empty or len(df) < 60:
        return result
    close, vol = df["stck_clpr"], df["acml_vol"]
    ma5, ma20, ma60 = close.rolling(5).mean().iloc[-1], close.rolling(20).mean().iloc[-1], close.rolling(60).mean().iloc[-1]
    result["정배열"] = bool(ma5 > ma20 > ma60)
    vol_avg20 = vol.rolling(20).mean().iloc[-2]
    result["거래량급증"] = bool(vol_avg20 and vol.iloc[-1] >= vol_avg20 * 1.5)
    if len(close) >= 21:
        result["20일모멘텀"] = bool(close.iloc[-1] > close.iloc[-21])
    rsi = compute_rsi(close)
    result["RSI값"] = round(rsi, 1) if rsi is not None else None
    result["RSI과열아님"] = bool(rsi < 70) if rsi is not None else None
    daily_range = (df["stck_hgpr"] - df["stck_lwpr"]) / df["stck_clpr"]
    if len(daily_range) >= 20:
        recent5, prior15 = daily_range.iloc[-5:].std(), daily_range.iloc[-20:-5].std()
        result["변동성수축(VCP)"] = bool(prior15 and recent5 < prior15 * 0.8)
    return result


_dart_corp_map_cache = None


def load_dart_corp_code_map() -> dict:
    global _dart_corp_map_cache
    if _dart_corp_map_cache is not None:
        return _dart_corp_map_cache
    if not DART_API_KEY:
        return {}
    try:
        resp = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                             params={"crtfc_key": DART_API_KEY}, timeout=30)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_bytes = zf.read(zf.namelist()[0])
        root = ET.fromstring(xml_bytes)
        mapping = {}
        for node in root.findall("list"):
            sc, cc = (node.findtext("stock_code") or "").strip(), (node.findtext("corp_code") or "").strip()
            if sc and len(sc) == 6:
                mapping[sc] = cc
        _dart_corp_map_cache = mapping
        return mapping
    except Exception:
        return {}


def check_disclosure_risk(stock_code: str) -> list[str]:
    corp_map = load_dart_corp_code_map()
    corp_code = corp_map.get(stock_code)
    if not corp_code or not DART_API_KEY:
        return []
    end = datetime.now(KST).strftime("%Y%m%d")
    start = (datetime.now(KST) - timedelta(days=30)).strftime("%Y%m%d")
    try:
        resp = requests.get("https://opendart.fss.or.kr/api/list.json",
                             params={"crtfc_key": DART_API_KEY, "corp_code": corp_code,
                                      "bgn_de": start, "end_de": end, "page_no": 1, "page_count": 50},
                             timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "000":
            return []
        return [f"{it.get('rcept_dt', '')} {it.get('report_nm', '')}"
                for it in data.get("list", [])
                if any(kw in it.get("report_nm", "") for kw in RISK_KEYWORDS)]
    except Exception:
        return []


def fetch_news(stock_name: str, count: int = 3) -> list[dict]:
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        return []
    try:
        resp = requests.get(
            "https://naverapihub.apigw.ntruss.com/search/v1/news",
            headers={"X-NCP-APIGW-API-KEY-ID": NAVER_CLIENT_ID, "X-NCP-APIGW-API-KEY": NAVER_CLIENT_SECRET},
            params={"query": stock_name, "display": count, "sort": "date"}, timeout=10,
        )
        if resp.status_code != 200:
            return []
        out = []
        for it in resp.json().get("items", []):
            title = re.sub("<[^>]+>", "", it.get("title", ""))
            out.append({"title": title, "link": it.get("link", "")})
        return out
    except Exception:
        return []


def fetch_current_price(stock_code: str):
    """(현재가, 전일대비 등락률%) 반환. 실패하면 (None, None).
    장중 급락/급등 감지용 — 일봉 데이터와 달리 실시간에 가깝게 갱신됨."""
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
