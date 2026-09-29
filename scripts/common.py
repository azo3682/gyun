# -*- coding: utf-8 -*-
"""
scripts/common.py
GitHub Actions에서 실행되는 스크립트들이 공유하는 데이터 조회 함수.
streamlit_app.py와 로직은 동일하나, st.cache 데코레이터 없이 순수 함수로 작성.
환경변수(GitHub Actions Secrets)에서 키를 읽는다.
"""

import hashlib
import io
import json
import os
import re
import threading
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

# 워크플로가 KIS_TOKEN_CACHE(파일 경로)를 지정하면, 잡끼리 토큰을 파일로 공유해 재발급을 줄인다.
# (KIS API는 토큰만으로는 호출이 안 되고 앱키/시크릿 헤더가 같이 필요하다.)
TOKEN_CACHE_PATH = os.environ.get("KIS_TOKEN_CACHE", "")


def _app_fingerprint() -> str:
    return hashlib.sha256(APP_KEY.encode("utf-8")).hexdigest()[:16]


def _read_token_file():
    """캐시 파일에 아직 유효한 토큰이 있으면 (token, expires_at), 없으면 None."""
    if not TOKEN_CACHE_PATH or not os.path.exists(TOKEN_CACHE_PATH):
        return None
    try:
        with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("fp") != _app_fingerprint():
            return None                      # 다른 앱키로 발급된 토큰
        if d["expires_at"] > time.time() + 300:
            return d["token"], d["expires_at"]
    except Exception:
        pass
    return None


def _write_token_file(token: str, expires_at: float):
    if not TOKEN_CACHE_PATH:
        return
    try:
        os.makedirs(os.path.dirname(TOKEN_CACHE_PATH), exist_ok=True)
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"token": token, "expires_at": expires_at, "fp": _app_fingerprint()}, f)
        os.chmod(TOKEN_CACHE_PATH, 0o600)
    except Exception:
        pass


def get_access_token() -> str:
    if _token_cache["token"] and _token_cache["expires_at"] > time.time() + 300:
        return _token_cache["token"]
    cached = _read_token_file()
    if cached:
        _token_cache["token"], _token_cache["expires_at"] = cached
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
    _write_token_file(_token_cache["token"], _token_cache["expires_at"])
    return _token_cache["token"]


def kis_headers(tr_id: str) -> dict:
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {get_access_token()}",
        "appkey": APP_KEY, "appsecret": APP_SECRET,
        "tr_id": tr_id, "custtype": "P",
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
    for item in data.get("output", []):
        name = item.get("hts_kor_isnm", "")
        if is_fund_product(name):
            continue
        rows.append({
            "ts": now, "rank_type": rank_type, "rank": len(rows) + 1,
            "stock_code": item.get("mksc_shrn_iscd", ""), "stock_name": name,
            "foreign_net": float(item.get("frgn_ntby_qty", 0) or 0),
            "inst_net": float(item.get("orgn_ntby_qty", 0) or 0),
            "combined_net": float(item.get("ntby_qty", 0) or 0),
        })
        if len(rows) >= top_n:
            break
    return rows


FAIL_REASONS = {}          # {사유코드: {"count": n, "msg": 예시 메시지}} — 어떤 이유로 실패했는지 진단용
_fail_lock = threading.Lock()


def record_fail(reason: str, msg: str = ""):
    with _fail_lock:
        entry = FAIL_REASONS.setdefault(reason, {"count": 0, "msg": msg})
        entry["count"] += 1


def fetch_price_detail(stock_code: str, retries: int = 3):
    """현재가 조회 API(inquire-price)의 output 전체를 dict로 반환. 실패하면 None.
    PER/PBR/EPS/BPS, 시가총액, 52주 고저 등이 이 응답 하나에 들어있다.
    초당 호출 제한(EGW00201)에 걸리면 잠깐 쉬고 재시도한다. 실패 사유는 FAIL_REASONS에 기록."""
    last_reason, last_msg = "EXCEPTION", ""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                f"{BASE_URL}{CURRENT_PRICE_API_PATH}",
                headers=kis_headers(CURRENT_PRICE_TR_ID),
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code},
                timeout=10,
            )
            data = resp.json()
        except Exception as e:
            last_reason, last_msg = "EXCEPTION", str(e)[:80]
            time.sleep(0.5 * (attempt + 1))
            continue
        if data.get("rt_cd") == "0":
            return data.get("output", {}) or {}
        msg_cd, msg1 = data.get("msg_cd", "UNKNOWN"), (data.get("msg1") or "")[:80]
        if msg_cd == "EGW00201":  # 초당 거래건수 초과
            last_reason, last_msg = "RATE_LIMIT_EXHAUSTED", msg1
            time.sleep(1.0 * (attempt + 1))
            continue
        record_fail(msg_cd, msg1)   # 종목 없음 등 재시도해도 소용없는 오류
        return None
    record_fail(last_reason, last_msg)
    return None


def _to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def fetch_valuation(stock_code: str):
    """{'per','pbr','eps','bps'} 반환 (값이 없으면 None). 조회 실패 시 None."""
    out = fetch_price_detail(stock_code)
    if out is None:
        return None
    return {k: _to_float(out.get(k)) for k in ("per", "pbr", "eps", "bps")}


# 거래소가 실시간으로 매기는 종목상태코드 — DART 공시보다 먼저, 더 확실하게 위험을 알려준다
# (예: '관리종목 지정 우려' 같은 거래소 시장조치 안내는 DART 기업공시로 안 올라오는 경우가 있다)
MARKET_STAT_LABELS = {"51": "🚨 관리종목", "52": "🚨 투자위험", "53": "⚠️ 투자경고",
                       "54": "⚠️ 투자주의", "58": "🚨 거래정지", "59": "⚠️ 정리매매"}
MARKET_WARN_LABELS = {"01": "⚠️ 투자주의", "02": "⚠️ 투자경고", "03": "🚨 투자위험"}
# 관리종목 지정 요건(시가총액 200억 미만)은 '우려 안내' 단계에서는 종목상태코드에 아직 안 잡힌다.
# (거래소가 확정 지정하기 전까지는 상태코드가 정상으로 남아있음 — 2026-09-29 엔젠바이오 사례로 확인)
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


def fetch_valuation_and_risk(stock_code: str):
    """(밸류에이션 dict|None, 거래소 위험 플래그 리스트) — 현재가 조회 한 번으로 둘 다 얻는다."""
    out = fetch_price_detail(stock_code)
    if out is None:
        return None, []
    valuation = {k: _to_float(out.get(k)) for k in ("per", "pbr", "eps", "bps")}
    flags = market_risk_flags(
        str(out.get("iscd_stat_cls_code") or ""),
        str(out.get("mrkt_warn_cls_code") or ""),
        str(out.get("temp_stop_yn") or "") == "Y",
        _to_float(out.get("hts_avls")),
    )
    return valuation, flags


def is_cheap(val: dict | None, per_max: float = 15.0, pbr_max: float = 1.5) -> bool:
    """흑자(PER>0)이면서 PER/PBR이 기준 이하인지."""
    if not val or val.get("per") is None or val.get("pbr") is None:
        return False
    return 0 < val["per"] <= per_max and 0 < val["pbr"] <= pbr_max


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


def compute_day_return(df: pd.DataFrame):
    """오늘(마지막 행) 하루 등락률. 데이터 부족 시 None."""
    if df.empty or len(df) < 2:
        return None
    close = df["stck_clpr"]
    return float(close.iloc[-1] / close.iloc[-2] - 1)


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


_dart_corp_map_cache = None
_dart_name_map_cache = None


def _load_dart_lists():
    """DART 상장사 목록을 한 번만 내려받아 (종목코드->corp_code, 종목코드->회사명) 두 매핑을 채운다."""
    global _dart_corp_map_cache, _dart_name_map_cache
    if _dart_corp_map_cache is not None:
        return
    if not DART_API_KEY:
        _dart_corp_map_cache, _dart_name_map_cache = {}, {}
        return
    try:
        resp = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                             params={"crtfc_key": DART_API_KEY}, timeout=30)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_bytes = zf.read(zf.namelist()[0])
        root = ET.fromstring(xml_bytes)
        corp_map, name_map = {}, {}
        for node in root.findall("list"):
            sc = (node.findtext("stock_code") or "").strip()
            cc = (node.findtext("corp_code") or "").strip()
            nm = (node.findtext("corp_name") or "").strip()
            if sc and len(sc) == 6:
                corp_map[sc] = cc
                name_map[sc] = nm
        _dart_corp_map_cache, _dart_name_map_cache = corp_map, name_map
    except Exception:
        _dart_corp_map_cache, _dart_name_map_cache = {}, {}


def load_dart_corp_code_map() -> dict:
    _load_dart_lists()
    return _dart_corp_map_cache


def load_dart_name_map() -> dict:
    """{종목코드: 회사명} — 상장사 전체 유니버스로도 쓴다."""
    _load_dart_lists()
    return _dart_name_map_cache


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
