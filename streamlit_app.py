# -*- coding: utf-8 -*-
"""
streamlit_app.py

Streamlit Community Cloud에서 실행되는 통합 버전입니다.
(기존 collector.py + dashboard.py를 하나로 합침 — 개인 PC가 필요 없습니다)

- 앱키/시크릿은 코드에 넣지 않고 Streamlit Cloud의 "Secrets"에서 읽어옵니다.
- 5분 캐시(TTL)로 KIS API를 호출해, 과도한 호출을 막습니다.
  (어차피 이 데이터는 하루 4번만 갱신되므로 5분 캐시로 충분합니다)
- 히스토리(추이 차트)는 앱이 켜져 있는 동안에만 쌓입니다. 클라우드가
  앱을 재시작하면(장시간 미접속 등) 히스토리는 초기화됩니다 — 로컬 PC에
  파일로 영구 저장하는 것과 다른 점입니다.
"""

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
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

# Streamlit Cloud의 "Secrets"에서 읽어옵니다. (로컬 테스트 시에는
# .streamlit/secrets.toml 파일에 같은 키로 넣어두면 됩니다)
APP_KEY = st.secrets.get("KIS_APP_KEY", "")
APP_SECRET = st.secrets.get("KIS_APP_SECRET", "")

RANKING_API_PATH = "/uapi/domestic-stock/v1/quotations/foreign-institution-total"
RANKING_TR_ID_REAL = "FHPTJ04400000"

TOP_N = 10
DB_PATH = "investor_flow.db"  # 클라우드 컨테이너 안 임시 파일 (재시작 시 초기화됨)


# ============================================================
# 인증 (앱이 실행되는 동안은 캐시해서 재사용 — 5분당 1회 제한 준수)
# ============================================================

@st.cache_resource
def _token_holder():
    """앱 프로세스가 살아있는 동안 유지되는 토큰 저장소."""
    return {"token": None, "expires_at": 0}


def get_access_token() -> str:
    holder = _token_holder()
    if holder["token"] and holder["expires_at"] > time.time() + 300:
        return holder["token"]

    url = f"{BASE_URL}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }
    resp = requests.post(url, json=body, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    holder["token"] = data["access_token"]
    holder["expires_at"] = time.time() + int(data.get("expires_in", 86400))
    return holder["token"]


# ============================================================
# 데이터 모델 / 조회
# ============================================================

@dataclass
class RankRow:
    ts: str
    rank_type: str
    rank: int
    stock_code: str
    stock_name: str
    foreign_net: float
    inst_net: float
    combined_net: float


@st.cache_data(ttl=300)  # 5분 캐시 — 실제 데이터도 5분 단위보다 자주 안 바뀜
def fetch_investor_ranking(rank_type: str) -> list[dict]:
    token = get_access_token()
    url = f"{BASE_URL}{RANKING_API_PATH}"

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": RANKING_TR_ID_REAL,
        "custtype": "P",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "V",
        "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": "0000",
        "FID_DIV_CLS_CODE": "0",
        "FID_RANK_SORT_CLS_CODE": "0" if rank_type == "buy" else "1",
        "FID_ETC_CLS_CODE": "0",
    }

    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("rt_cd") != "0":
        raise RuntimeError(f"KIS API 오류: {data.get('msg1')}")

    now = datetime.now(KST).isoformat(timespec="seconds")
    rows = []
    for i, item in enumerate(data.get("output", [])[:TOP_N], start=1):
        rows.append(
            {
                "ts": now,
                "rank_type": rank_type,
                "rank": i,
                "stock_code": item.get("mksc_shrn_iscd", ""),
                "stock_name": item.get("hts_kor_isnm", ""),
                "foreign_net": float(item.get("frgn_ntby_qty", 0) or 0),
                "inst_net": float(item.get("orgn_ntby_qty", 0) or 0),
                "combined_net": float(item.get("ntby_qty", 0) or 0),
            }
        )
    return rows


# ============================================================
# 히스토리 저장 (앱 실행 중에만 유지되는 임시 DB)
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS investor_ranking (
            ts TEXT NOT NULL, rank_type TEXT NOT NULL, rank INTEGER NOT NULL,
            stock_code TEXT NOT NULL, stock_name TEXT NOT NULL,
            foreign_net REAL NOT NULL, inst_net REAL NOT NULL, combined_net REAL NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def save_rows(rows: list[dict]):
    if not rows:
        return
    conn = sqlite3.connect(DB_PATH)
    # 같은 ts는 중복 저장하지 않음
    existing = conn.execute(
        "SELECT 1 FROM investor_ranking WHERE ts = ? AND rank_type = ? LIMIT 1",
        (rows[0]["ts"], rows[0]["rank_type"]),
    ).fetchone()
    if not existing:
        conn.executemany(
            """
            INSERT INTO investor_ranking
            (ts, rank_type, rank, stock_code, stock_name, foreign_net, inst_net, combined_net)
            VALUES (:ts, :rank_type, :rank, :stock_code, :stock_name, :foreign_net, :inst_net, :combined_net)
            """,
            rows,
        )
        conn.commit()
    conn.close()


def load_history(stock_code: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT ts, foreign_net, inst_net, combined_net FROM investor_ranking WHERE stock_code = ? ORDER BY ts ASC",
        conn,
        params=(stock_code,),
    )
    conn.close()
    return df


# ============================================================
# 화면
# ============================================================

st.set_page_config(page_title="투자자별 매매현황 대시보드", layout="wide")
st.title("투자자별(외국인·기관) 매매현황 — 장중 순매수/순매도 상위")

if not APP_KEY or not APP_SECRET:
    st.error("Secrets에 KIS_APP_KEY / KIS_APP_SECRET이 설정되지 않았습니다. "
             "앱 설정 > Secrets에서 등록해주세요.")
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
        st.dataframe(pd.DataFrame(buy_rows).drop(columns=["ts", "rank_type"]),
                     use_container_width=True, hide_index=True)

with col2:
    st.subheader("순매도 상위 10")
    if sell_rows:
        st.caption(f"기준 시각: {sell_rows[0]['ts']}")
        st.dataframe(pd.DataFrame(sell_rows).drop(columns=["ts", "rank_type"]),
                     use_container_width=True, hide_index=True)

st.divider()
st.subheader("종목별 수급 추이 (이 앱이 켜져 있던 동안만)")
code = st.text_input("종목코드 (예: 005930)")
if code:
    hist = load_history(code)
    if hist.empty:
        st.info("아직 이 종목의 이력이 없습니다.")
    else:
        st.line_chart(hist.set_index("ts")[["foreign_net", "inst_net", "combined_net"]])

st.caption("5분 캐시로 갱신됩니다. 새로고침 버튼을 누르거나 페이지를 다시 열면 최신 데이터를 확인할 수 있습니다.")
if st.button("지금 새로고침"):
    st.cache_data.clear()
    st.rerun()
