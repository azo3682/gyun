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
    for i, item in enumerate(data.get("output", [])[:TOP_N], start=1):
        rows.append({
            "ts": now, "rank_type": rank_type, "rank": i,
            "stock_code": item.get("mksc_shrn_iscd", ""),
            "stock_name": item.get("hts_kor_isnm", ""),
            "foreign_net": float(item.get("frgn_ntby_qty", 0) or 0),
            "inst_net": float(item.get("orgn_ntby_qty", 0) or 0),
            "combined_net": float(item.get("ntby_qty", 0) or 0),
        })
    return rows


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

init_db()

try:
    buy_rows = fetch_investor_ranking("buy")
    sell_rows = fetch_investor_ranking("sell")
    save_rows(buy_rows)
    save_rows(sell_rows)
except Exception as e:
    st.error(f"데이터 조회 실패: {e}")
    st.stop()

col1, col2 = st.columns(2)
with col1:
    st.subheader("순매수 상위 10")
    if buy_rows:
        st.caption(f"기준 시각: {buy_rows[0]['ts']}")
        buy_df = pd.DataFrame(buy_rows).drop(columns=["ts", "rank_type"])
        buy_df = buy_df.rename(columns={
            "rank": "순위", "stock_code": "종목코드", "stock_name": "종목명",
            "foreign_net": "외국인순매수", "inst_net": "기관순매수", "combined_net": "전체순매수",
        })
        st.dataframe(buy_df, use_container_width=True, hide_index=True)
with col2:
    st.subheader("순매도 상위 10")
    if sell_rows:
        st.caption(f"기준 시각: {sell_rows[0]['ts']}")
        sell_df = pd.DataFrame(sell_rows).drop(columns=["ts", "rank_type"])
        sell_df = sell_df.rename(columns={
            "rank": "순위", "stock_code": "종목코드", "stock_name": "종목명",
            "foreign_net": "외국인순매도", "inst_net": "기관순매도", "combined_net": "전체순매도",
        })
        st.dataframe(sell_df, use_container_width=True, hide_index=True)

st.divider()
st.subheader("장중 후보 변동 (제외 / 신규 / 유지)")


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


def trend_label_for(stock_code: str) -> str:
    df = fetch_daily_ohlcv(stock_code)
    label, _, _ = classify_trend_state(df)
    return label


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
            price_str = f" · 현재가 {e['current_price']:,.0f}원 ({e['day_pct']:+.2f}%)" if e.get("current_price") else ""
            trend = trend_label_for(e["stock_code"])
            st.markdown(f"- {e['stock_name']}({e['stock_code']}) — {e['reason']}{price_str} · **국면: {trend}**")
    if new_candidates:
        st.markdown("**🟢 신규 후보** (아침엔 없었으나 지금 조건 충족)")
        for n in new_candidates:
            price_str = f" · 현재가 {n['current_price']:,.0f}원 ({n['day_pct']:+.2f}%)" if n.get("current_price") else ""
            badge = price_status_badge(n.get("day_pct"))
            trend = trend_label_for(n["stock_code"])
            st.markdown(f"- {n['stock_name']}({n['stock_code']}) — {n['passed']}/{n['total']}점{price_str} · {badge} · **국면: {trend}**")
    if kept:
        st.markdown("**⚪ 유지 중** (아침 후보 그대로, 실시간 현재가)")
        kept_rows = [{
            "종목명": k["stock_name"], "종목코드": k["stock_code"],
            "현재가": k.get("current_price"), "당일등락률(%)": k.get("day_pct"),
            "상태": price_status_badge(k.get("day_pct")),
            "국면": trend_label_for(k["stock_code"]),
        } for k in kept]
        st.dataframe(pd.DataFrame(kept_rows), use_container_width=True, hide_index=True)
    if not excluded and not new_candidates and not kept:
        st.info("아침 후보 대비 변동 없음")
else:
    st.caption("아직 장중 재점검 데이터가 없습니다 (첫 재점검은 09:40경 실행됩니다).")

st.divider()
st.subheader("순매수 상위 10 — 스윙 후보 스크리닝 (점수 높은 순 정렬)")

if not (DART_API_KEY and NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
    st.warning("DART / 네이버 뉴스 Secrets이 없어 공시·뉴스 정보는 생략됩니다. "
               "기술적 체크리스트만 표시합니다.")

# 먼저 전 종목의 기술적 체크 결과를 계산해서 점수순으로 정렬
scored_rows = []
for row in buy_rows:
    code = row["stock_code"]
    df = fetch_daily_ohlcv(code)
    if df.empty:
        time.sleep(0.5)
        df = fetch_daily_ohlcv.__wrapped__(code)  # 캐시 우회 재시도
    tech = analyze_technicals(df)
    checks = {k: v for k, v in tech.items() if k != "RSI값"}
    passed = sum(1 for v in checks.values() if v is True)
    total = sum(1 for v in checks.values() if v is not None)
    scored_rows.append({**row, "_tech": tech, "_checks": checks, "_passed": passed, "_total": total})

# 통과 개수 내림차순, 동점이면 원래 순매수 순위(오름차순)로 정렬
scored_rows.sort(key=lambda r: (-r["_passed"], r["rank"]))

for row in scored_rows:
    code, name = row["stock_code"], row["stock_name"]
    tech, checks, passed, total = row["_tech"], row["_checks"], row["_passed"], row["_total"]
    with st.expander(f"점수 {passed}/{total} · 원순위 {row['rank']}위 · {name} ({code})"):
        rsi_note = f" (RSI: {tech['RSI값']})" if tech["RSI값"] is not None else ""
        st.markdown(f"**기술적 체크: {passed}/{total} 통과**{rsi_note}")

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

st.divider()
st.subheader("종목별 수급 추이 (이 앱이 켜져 있던 동안만)")
code_input = st.text_input("종목코드 (예: 005930)")
if code_input:
    conn = sqlite3.connect(DB_PATH)
    hist = pd.read_sql_query(
        "SELECT ts, foreign_net, inst_net, combined_net FROM investor_ranking WHERE stock_code = ? ORDER BY ts ASC",
        conn, params=(code_input,))
    conn.close()
    if hist.empty:
        st.info("아직 이 종목의 이력이 없습니다.")
    else:
        st.line_chart(hist.set_index("ts")[["foreign_net", "inst_net", "combined_net"]])

st.divider()
st.subheader("보유·관심 종목 직접 조회")
st.caption("순매수 상위 10에 없는 종목도 조회 가능합니다 (예: 보유 중인 종목 점검용). 매수/매도 신호가 아니라 참고용 체크리스트입니다.")
lookup_code = st.text_input("종목코드 입력 (예: 009150)", key="lookup_code")
if lookup_code:
    with st.spinner("조회 중..."):
        lookup_df = fetch_daily_ohlcv(lookup_code)
        lookup_tech = analyze_technicals(lookup_df)
        lookup_price, lookup_pct = fetch_current_price(lookup_code)
        lookup_risky = check_disclosure_risk(lookup_code)

    if lookup_price is not None:
        st.metric("현재가", f"{lookup_price:,.0f}원", f"{lookup_pct:+.2f}%")
        st.markdown(f"**상태: {price_status_badge(lookup_pct)}**")
    else:
        st.warning("현재가 조회 실패 — 종목코드를 확인해주세요.")

    lookup_checks = {k: v for k, v in lookup_tech.items() if k != "RSI값"}
    lookup_passed = sum(1 for v in lookup_checks.values() if v is True)
    lookup_total = sum(1 for v in lookup_checks.values() if v is not None)
    if lookup_total == 0:
        st.caption("일봉 데이터를 가져오지 못했습니다 (상장 60거래일 미만이거나 코드 오류일 수 있습니다).")
    else:
        rsi_note = f" (RSI: {lookup_tech['RSI값']})" if lookup_tech["RSI값"] is not None else ""
        st.markdown(f"**기술적 체크: {lookup_passed}/{lookup_total} 통과**{rsi_note}")
        cols = st.columns(len(lookup_checks))
        for c, (label, val) in zip(cols, lookup_checks.items()):
            icon = "✅" if val is True else ("❌" if val is False else "—")
            c.metric(label, icon)

        st.markdown("**추세 판단 (규칙 기반 참고 의견)**")
        state_label, state_desc, state_fn = classify_trend_state(lookup_df)
        state_fn(f"**{state_label}** — {state_desc}")
        st.caption("이동평균선(5/20/60일) 배열과 RSI 흐름만으로 판단한 규칙 기반 해석이며, 매수/매도 신호가 아닙니다.")

    if lookup_risky:
        st.error("⚠️ 최근 30일 내 주의 공시 발견:\n" + "\n".join(f"- {r}" for r in lookup_risky))
    elif DART_API_KEY:
        st.success("최근 30일 내 주의 공시 없음")

if st.button("지금 새로고침"):
    st.cache_data.clear()
    st.rerun()
