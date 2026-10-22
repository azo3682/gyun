# -*- coding: utf-8 -*-
"""
pages/KODEX200.py — KODEX 200 매매 분석기 (Streamlit 멀티페이지)

기존 streamlit_app.py는 건드리지 않는다. 이 파일은 scripts/k200_core.py의 계산 결과만 보여준다.
데이터는 GitHub Actions(K200 Collect)가 data/k200/ 에 쌓은 파일을 읽는다.
주의: 새 문구에는 물결표(tilde)를 쓰지 않는다 (마크다운 취소선으로 깨진다).
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import k200_core as K  # noqa: E402
import k200_view as V  # noqa: E402

st.set_page_config(page_title="KODEX 200 매매 분석기", layout="wide")

RAW_BASE = str(st.secrets.get("AI_REPORT_RAW_BASE", "https://raw.githubusercontent.com/azo3682/gyun/main/data")
               if hasattr(st, "secrets") else "https://raw.githubusercontent.com/azo3682/gyun/main/data")
LOCAL_DIR = os.path.join(HERE, "..", "data", "k200")


# ---------------- 데이터 읽기 ----------------

def _read_remote(rel):
    try:
        r = requests.get(f"{RAW_BASE}/k200/{rel}", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _read_local(rel):
    try:
        with open(os.path.join(LOCAL_DIR, rel), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


@st.cache_data(ttl=600, show_spinner=False)
def load_json(rel):
    return _read_remote(rel) or _read_local(rel)


@st.cache_data(ttl=3600, show_spinner=False)
def load_minute_days(dates: tuple):
    def one(d):
        obj = _read_local(f"minute/{d}.json") or _read_remote(f"minute/{d}.json")
        if not obj or not obj.get("bars"):
            return None
        df = pd.DataFrame(obj["bars"], columns=["time", "open", "high", "low", "close", "volume"])
        issues = obj.get("issues") or []
        if any(("장중" in x) or ("혼입" in x) for x in issues):
            return None          # 미완성·오염 의심인 날은 검증에서 제외한다
        bars = K.norm_minute(df)
        if K.flat_fill_ratio(bars) > K.FLAT_FILL_MAX:
            return None          # 선채움(가짜 봉)이 섞인 날은 검증에서 제외한다
        return {"date": d, "bars": bars, "issues": issues}
    with ThreadPoolExecutor(max_workers=8) as ex:
        res = list(ex.map(one, dates))
    return [r for r in res if r]


def fmt(x, nd=1, suffix=""):
    if x is None or (isinstance(x, float) and (pd.isna(x))):
        return "-"
    if isinstance(x, float) and x == float("inf"):
        return "∞"
    return f"{x:,.{nd}f}{suffix}"


def verdict_table(block: dict) -> pd.DataFrame:
    rows = []
    for name, r in block.items():
        f, o = r["full"], r["out_of_sample"]
        rows.append({"규칙": name, "거래 수": f.get("n", 0), "승률 %": fmt(f.get("win_pct"), 0),
                     "손익비": fmt(f.get("payoff"), 2), "기대값 bp": fmt(f.get("expectancy_bps"), 1),
                     "t값": fmt(f.get("t"), 2), "PF": fmt(f.get("pf"), 2), "최대낙폭 %": fmt(f.get("mdd_pct"), 1),
                     "검증구간 기대값 bp": fmt(o.get("expectancy_bps"), 1) if o.get("n") else "-", "판정": r["verdict"]})
    return pd.DataFrame(rows)


def trades_frame(trades):
    if not trades:
        return pd.DataFrame()
    df = pd.DataFrame(trades)
    keep = [c for c in ["entry", "exit", "entry_px", "exit_px", "bars", "why", "gross_bps", "net_bps"] if c in df.columns]
    return df[keep].rename(columns={"entry": "진입일", "exit": "청산일", "entry_px": "진입가", "exit_px": "청산가",
                                    "bars": "보유봉", "why": "사유", "gross_bps": "비용 전 bp", "net_bps": "비용 후 bp"})


# ---------------- 화면 ----------------

st.title("KODEX 200 판단 보조")
st.caption("지금 사도 되는지, 저점인지 고점인지, 수급은 어떤지를 지표로 보여줍니다. 매수·매도를 대신 결정하지 않고 주문 기능도 없습니다. "
           "규칙 검증과 비용 계산은 뒤쪽 탭으로 옮겼습니다.")

with st.sidebar:
    st.header("비용 가정")
    tick = st.number_input("호가 단위 (원)", min_value=1.0, value=K.TICK_DEFAULT, step=1.0,
                           help="ETF 호가 단위. 증권사 화면에서 확인하세요.")
    slip = st.number_input("편도 슬리피지 (틱, 스프레드 포함)", min_value=0.0, value=1.0, step=0.5,
                           help="시장가로 사고팔 때 한쪽에서 밀리는 틱 수. 지정가 위주면 낮출 수 있지만 체결 안 될 위험이 생깁니다.")
    comm = st.number_input("편도 수수료 (bp)", min_value=0.0, value=0.0, step=0.1,
                           help="증권사 수수료. 이벤트 무료라면 0, 기간이 끝나면 올라갑니다. 1bp는 0.01%입니다.")
    cost = K.CostModel(tick=tick, slip_ticks=slip, commission_bps=comm)
    max_days = st.slider("분봉 불러올 최근 일수", 20, 250, 120, step=10)
    if st.button("데이터 새로고침"):
        st.cache_data.clear()
        st.rerun()

status = load_json("status.json") or {}
daily_obj = load_json("daily.json") or {}
nav_log = load_json("nav_log.json") or {}
daily = pd.DataFrame(daily_obj.get("rows", []))
minute_dates = tuple((status.get("minute_dates") or [])[-max_days:])
days = load_minute_days(minute_dates) if minute_dates else []

c1, c2, c3, c4 = st.columns(4)
c1.metric("일봉", f"{len(daily):,}개" if len(daily) else "없음",
          f"{daily['date'].iloc[0]} 부터 {daily['date'].iloc[-1]}" if len(daily) else None, delta_color="off")
c2.metric("분봉 누적", f"{len(days)}일", f"{minute_dates[0]} 부터" if minute_dates else None, delta_color="off")
if nav_log:
    last_nav = nav_log[sorted(nav_log)[-1]]
    c3.metric("NAV 괴리율", fmt(last_nav.get("premium_pct"), 2, "%"), sorted(nav_log)[-1], delta_color="off")
else:
    c3.metric("NAV 괴리율", "-")
c4.metric("수집 시각", (status.get("run_at") or "-")[:16].replace("T", " "))

if daily.empty:
    st.warning("아직 수집된 데이터가 없습니다. GitHub의 Actions 탭에서 'K200 Collect'를 Run workflow로 한 번 실행하세요. "
               "분봉 과거분을 채우려면 backfill_days에 일수를 넣어 실행하고, 결과는 data/k200/status.json의 backfill 항목에서 확인합니다.")
    st.stop()

if status.get("minute_today", {}).get("issues"):
    st.warning("가장 최근 분봉 수집에 문제가 있었습니다: " + ", ".join(status["minute_today"]["issues"]))

daily_n = K.norm_daily(daily)
ev = K.evaluate_all(daily_n, days if len(days) else None, cost)

tab_now, tab_rules, tab_day, tab_method = st.tabs(["판단 보조", "규칙 검증 (일봉)", "단타·스켈핑 진단 (분봉)", "방법과 한계"])

PANEL_ORDER = V.GROUPS


def _live_quote():
    """KIS에서 지금 값을 받아온다. 실패하면 (None, 사유). 수집기 함수를 그대로 쓴다."""
    try:
        sec = st.secrets
        os.environ["KIS_APP_KEY"] = str(sec["KIS_APP_KEY"])
        os.environ["KIS_APP_SECRET"] = str(sec["KIS_APP_SECRET"])
    except Exception:
        return None, "Streamlit secrets에 KIS_APP_KEY, KIS_APP_SECRET이 없습니다."
    try:
        import k200_collect as C
        from datetime import datetime
        now = datetime.now(C.KST)
        today = now.strftime("%Y%m%d")
        data, err = C.kis_get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                              {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": C.CODE})
        if data is None:
            return None, f"현재가 조회 실패: {err}"
        o = data.get("output") or {}
        f = lambda k: float(str(o.get(k, "0")).replace(",", "") or 0)
        q = {"today": today, "open": f("stck_oprc"), "high": f("stck_hgpr"), "low": f("stck_lwpr"),
             "close": f("stck_prpr"), "volume": f("acml_vol")}
        rows, _ = C.fetch_minutes_today(today, upper=now.strftime("%H%M") + "00")
        rows = [r for r in rows if r["time"] <= now.strftime("%H%M") + "00"]
        q["minutes"] = rows
        mdata, _e = C.kis_get(C.MKT_TIME_PATH, C.MKT_TIME_TR, {"FID_INPUT_ISCD": "KSP", "FID_INPUT_ISCD_2": "0001"})
        mo = (mdata or {}).get("output") or []
        if isinstance(mo, dict):
            mo = [mo]
        if mo:
            q["mkt_flow"] = {k: C._iv(mo[0].get(f)) for k, f in C.MKT_AMT.items()}
        q["fetched_at"] = now.strftime("%H:%M:%S")
        if q["close"] <= 0:
            return None, "현재가가 0으로 왔습니다(장 시작 전이거나 응답 이상)."
        return q, None
    except Exception as e:
        return None, f"라이브 조회 오류: {str(e)[:120]}"


# ---------- 판단 보조 ----------
with tab_now:
    base_daily = daily_n.copy()
    last_date = str(base_daily["date"].iloc[-1])
    live = st.session_state.get("k200_live")
    use_live = bool(live)
    cols = st.columns([1, 1, 3])
    if cols[0].button("지금 값으로 다시 계산 (장중)"):
        q, err = _live_quote()
        if q:
            st.session_state["k200_live"] = q
            st.rerun()
        else:
            st.error(err)
    if live and cols[1].button("라이브 해제"):
        st.session_state.pop("k200_live", None)
        st.rerun()

    work = base_daily
    partial = False
    if use_live:
        work = V.with_live_bar(base_daily, live["today"], live["open"], live["high"], live["low"], live["close"], live["volume"])
        partial = True
        st.warning(f"{live['fetched_at']} 현재가 {fmt(live['close'], 0)}원을 오늘 일봉으로 가정해 계산했습니다. "
                   "장 마감 전 값이라 종가가 달라지면 지표도 바뀝니다.")
    elif last_date == pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y%m%d") and pd.Timestamp.now(tz="Asia/Seoul").strftime("%H%M") < "1535":
        st.warning("저장된 오늘 일봉이 장중 값일 수 있습니다. 확정 전 값이면 위 버튼으로 지금 값을 반영하거나 15:50 이후 보세요.")

    view = V.build_view(work)
    if not view:
        st.info("일봉이 30개 미만이라 지표를 계산할 수 없습니다.")
    else:
        m, cards, stt = view["m"], view["cards"], view["state"]
        st.subheader(f"지금 상태: {stt['state']}")
        st.markdown(f"**{stt['headline']}**")
        a, b, c, d = st.columns(4)
        a.metric(f"종가 ({m['date']})", fmt(m["close"], 0, "원"), fmt(m["day_ret_pct"], 2, "%"))
        b.metric("RSI(14) / RSI(2)", f"{fmt(m['rsi14'], 0)} / {fmt(m['rsi2'], 0)}")
        c.metric("20일 고점 대비", fmt(m["dd20_pct"], 1, "%"))
        d.metric("52주 범위 내 위치", fmt(m["pos52_pct"], 0, "%"), f"고점 대비 {fmt(m['dd52_pct'], 1)}%", delta_color="off")
        sc = stt["score"]
        st.markdown(" · ".join(f"**{g}** {sc[g]['on']}/{sc[g]['total']}" for g in PANEL_ORDER))
        for r in stt["reasons"]:
            st.markdown(f"- {r}")
        st.info("참고 가이드: " + stt["guide"])
        st.markdown(f"**무효화 기준 가격 약 {fmt(stt['invalidation_price'], 0)}원** ({stt['invalidation_note']}). 이 아래로 마감하면 위 판독의 근거가 깨집니다.")
        if stt["pending_confirmations"]:
            st.markdown("**아직 안 켜진 반등 확인 신호:** " + ", ".join(stt["pending_confirmations"]))

        st.subheader("신호 점검표")
        for g in PANEL_ORDER:
            with st.expander(f"{g} ({sc[g]['on']}/{sc[g]['total']} 충족)", expanded=(g in PANEL_ORDER[1:3])):
                rows = [{"상태": "켜짐" if x["on"] else ("꺼짐" if x["on"] is False else "계산불가"), "항목": x["name"],
                         "현재 값": x["value"], "의미": x["note"]} for x in cards if x["group"] == g]
                st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

        st.subheader("과거에 비슷했던 때 이후 성과")
        an = V.analog_stats(work)
        if not an["rows"]:
            st.info("일봉이 260개 미만이라 계산하지 않습니다. Actions에서 daily_years를 12로 올려 다시 수집하세요.")
        else:
            st.caption(f"{an['first']} 부터 {an['last']} 까지 {an['n_bars']}거래일. 같은 사건이 이어지면 첫날만 세고(10거래일 간격), 오늘은 제외했습니다. "
                       "사건 수가 적으면 우연일 수 있습니다. 비교 기준은 아무 날이나 샀을 때입니다.")
            tab_rows = []
            for r in an["rows"]:
                row = {"상황": r["name"], "오늘 해당": "예" if r["active_today"] else "-", "사건 수": r["episodes"]}
                for h in V.HORIZONS:
                    s_ = r["stats"][h]
                    row[f"{h}일 후 승률/평균/최악"] = (f"{s_['win_pct']:.0f}% / {s_['mean']:+.1f}% / {s_['worst']:+.1f}%  (n={s_['n']})" if s_.get("n") else "-")
                tab_rows.append(row)
            row = {"상황": "(기준) 아무 날이나 매수", "오늘 해당": "-", "사건 수": an["baseline"][5].get("n", 0)}
            for h in V.HORIZONS:
                s_ = an["baseline"][h]
                row[f"{h}일 후 승률/평균/최악"] = f"{s_['win_pct']:.0f}% / {s_['mean']:+.1f}% / {s_['worst']:+.1f}%  (n={s_['n']})"
            tab_rows.append(row)
            st.dataframe(pd.DataFrame(tab_rows), hide_index=True, use_container_width=True)
            mae = [f"{r['name'].split(' (')[0]} {r['stats'][10]['mae_median']:+.1f}%" for r in an["rows"] if r["stats"][10].get("n")]
            if mae:
                st.caption("10일 안에 겪은 중앙값 최대 평가손실: " + " / ".join(mae))

        st.subheader("가격 지도 (지지·저항)")
        lv = V.levels(work, price=m["close"], last_is_partial=partial)
        if lv:
            near = sorted(lv, key=lambda x: abs(x["dist_pct"]))[:12]
            near = sorted(near, key=lambda x: x["price"], reverse=True)
            st.dataframe(pd.DataFrame([{"구분": x["side"], "기준": x["name"], "가격": f"{x['price']:,.0f}", "현재가 대비": f"{x['dist_pct']:+.1f}%"} for x in near]),
                         hide_index=True, use_container_width=True)
            st.caption("피벗은 직전 완성 일봉 기준. 이동평균선과 최근 고저점은 여러 개가 가까이 몰린 가격일수록 의미가 큽니다.")

        st.subheader("오늘 장중 위치")
        mins = None
        if use_live and live.get("minutes"):
            mins = pd.DataFrame(live["minutes"])
        elif days:
            mins = None
        if mins is not None and len(mins):
            ir = V.intraday_read(K.norm_minute(mins[["time", "open", "high", "low", "close", "volume"]]), prev_close=m["prev_close"])
        else:
            ir = {}
        if ir:
            x1, x2, x3, x4 = st.columns(4)
            x1.metric("VWAP 대비", fmt(ir["vs_vwap_pct"], 2, "%"))
            x2.metric("시가 대비", fmt(ir["vs_open_pct"], 2, "%"))
            x3.metric("당일 범위 내 위치", fmt(ir["range_pos_pct"], 0, "%"), "0이 저가, 100이 고가", delta_color="off")
            x4.metric("최근 30분", fmt(ir["last30_pct"], 2, "%"))
            st.caption(f"{ir['time']} 기준. 시초 갭 {fmt(ir.get('gap_pct'), 2, '%')}, 고점에서 {fmt(ir['from_high_pct'], 2, '%')}, 저점에서 {fmt(ir['from_low_pct'], 2, '%')}.")
        else:
            st.caption("장중 위치는 위 버튼으로 지금 값을 불러왔을 때만 표시합니다.")

        st.subheader("수급")
        # (1) 시장 전체(코스피)
        mk_obj = load_json("market_flow.json") or {}
        mk_rows = mk_obj.get("rows") or []
        if mk_rows:
            mdf = pd.DataFrame(mk_rows).sort_values("date", ascending=False).reset_index(drop=True)
            eok = lambda x: None if x is None or pd.isna(x) else x / 100.0       # 백만원 -> 억원
            last = mdf.iloc[0]
            st.markdown(f"**코스피 전체 투자자별 순매수 (확정치, 기준일 {last['date']}, 지수 {fmt(last.get('idx'), 2)} / {fmt(last.get('idx_chg_pct'), 2, '%')})**")
            names = [("frgn", "외국인"), ("orgn", "기관 합계"), ("prsn", "개인"), ("etc", "기타법인"),
                     ("scrt", "  금융투자"), ("ivtr", "  투신"), ("pe", "  사모펀드"), ("insu", "  보험"), ("bank", "  은행"), ("fund", "  연기금 등")]
            trs = []
            for k, nm in names:
                col = f"{k}_amt"
                if col not in mdf.columns:
                    continue
                ser = mdf[col]
                tot = lambda n: ser.head(n).sum(min_count=1)
                streak = V._streak(list(ser.head(30)))
                trs.append({"구분": nm, "당일(억원)": fmt(eok(ser.iloc[0]), 0), "3일 합": fmt(eok(tot(3)), 0), "5일 합": fmt(eok(tot(5)), 0),
                            "20일 합": fmt(eok(tot(20)), 0),
                            "연속": (f"{streak}일 순매수" if streak > 0 else f"{-streak}일 순매도" if streak < 0 else "-")})
            st.dataframe(pd.DataFrame(trs), hide_index=True, use_container_width=True)
            hist = mdf.head(10)[["date", "idx_chg_pct", "frgn_amt", "orgn_amt", "prsn_amt"]].copy()
            for c_ in ("frgn_amt", "orgn_amt", "prsn_amt"):
                hist[c_] = (hist[c_] / 100.0).round(0)
            hist.columns = ["날짜", "지수 등락 %", "외국인 억", "기관 억", "개인 억"]
            st.caption("최근 10거래일 흐름 (지수 등락과 함께)")
            st.dataframe(hist, hide_index=True, use_container_width=True)
            st.caption("금액은 억원. 수량(주)과 금액의 부호가 다를 수 있습니다(싼 종목을 팔고 비싼 종목을 샀을 때). 판단에는 금액을 기준으로 봅니다. 기관 합계는 아래 세부 항목의 합입니다.")
        else:
            st.info("코스피 전체 수급이 아직 없습니다. 수집기를 새 버전으로 실행하면 market_flow.json이 생깁니다.")
        mf = live.get("mkt_flow") if use_live else None
        if mf:
            st.markdown(f"**코스피 장중 누적 ({live['fetched_at']} 기준, 잠정치, 억원)**: 외국인 {fmt(mf['frgn'] / 100 if mf.get('frgn') is not None else None, 0)}, "
                        f"기관 {fmt(mf['orgn'] / 100 if mf.get('orgn') is not None else None, 0)}, 개인 {fmt(mf['prsn'] / 100 if mf.get('prsn') is not None else None, 0)}, "
                        f"금융투자 {fmt(mf['scrt'] / 100 if mf.get('scrt') is not None else None, 0)}")
        # (2) 개별 종목
        flow_obj = load_json("flow.json") or {}
        fr = V.flow_read(flow_obj.get("series") or {})
        if fr["rows"]:
            st.markdown("**개별 종목 투자자별 순매수 (확정치)**")
            for ln in fr["lines"]:
                st.markdown(f"- {ln.replace('5일 합 ', '5일 합 ')}")
            trows = []
            for r in fr["rows"]:
                trows.append({"종목": r["name"], "투자자": r["investor"], "기준일": r["last_date"],
                              "당일(억원)": fmt(None if r["last_amt"] is None else r["last_amt"] / 100, 0),
                              "5일 합": fmt(None if r["amt5"] is None else r["amt5"] / 100, 0),
                              "20일 합": fmt(None if r["amt20"] is None else r["amt20"] / 100, 0),
                              "연속": (f"{r['streak']}일 순매수" if r["streak"] > 0 else f"{-r['streak']}일 순매도" if r["streak"] < 0 else "-"),
                              "비고": r["flip"] or ""})
            st.dataframe(pd.DataFrame(trows), hide_index=True, use_container_width=True)
            st.caption("연속 일수는 수량 기준, 합계는 금액(억원) 기준입니다. 프로그램 매매는 API 확인 후 추가 예정입니다.")
        else:
            st.info("종목별 수급 데이터가 없습니다.")

# ---------- 규칙 검증 ----------
with tab_rules:
    if not ev["swing"]:
        st.info("일봉이 120개 미만이라 검증하지 않습니다.")
    else:
        thr = K.bonferroni_t(ev["n_rules"])
        st.markdown(f"비교한 규칙 수 {ev['n_rules']}개 → 우연을 걸러내려고 t값 {thr:.2f} 이상을 요구합니다. "
                    f"거래 {K.MIN_TRADES}건 미만이면 판정하지 않습니다. 기대값은 거래 한 번당 평균 순수익(비용 후, bp)입니다.")
        st.dataframe(verdict_table(ev["swing"]), hide_index=True, use_container_width=True)
        for name, r in ev["swing"].items():
            with st.expander(f"{name} — 거래 내역과 누적 곡선"):
                st.caption(K.SWING_RULES.get(name, ""))
                tf = trades_frame(r["trades"])
                if tf.empty:
                    st.write("거래 없음")
                    continue
                eq = (1 + pd.Series([t["net_bps"] for t in r["trades"]]) / 1e4).cumprod() * 100
                eq.index = [t["exit"] for t in r["trades"]]
                st.line_chart(eq.rename("누적 (시작 100)"))
                st.dataframe(tf.sort_values("진입일", ascending=False).head(60), hide_index=True, use_container_width=True)

# ---------- 분봉 ----------
with tab_day:
    need = 30
    if len(days) < 5:
        st.info(f"분봉이 {len(days)}일치뿐입니다. 수집기가 매일 15:50에 하루씩 쌓습니다. "
                f"규칙 검증은 최소 {need}일 이상 쌓인 뒤에 의미가 생기고, 지금은 아래 진단만 참고용입니다. "
                "과거 분봉 채우기는 Actions에서 backfill_days로 시도할 수 있습니다.")
    cm = ev.get("cost_move") or {}
    if cm:
        a, b, c, d = st.columns(4)
        a.metric("1분 변동폭 중앙값", fmt(cm["median_abs_bps"], 1, "bp"))
        b.metric("1분 변동폭 상위 10%", fmt(cm["p90_abs_bps"], 1, "bp"))
        c.metric("왕복 비용", fmt(cm["round_trip_cost_bps"], 1, "bp"))
        d.metric("비용 / 1분 변동폭", fmt(cm["cost_to_median_move"], 2, "배"))
        st.caption("비용이 1분 변동폭의 큰 몫을 차지할수록 스켈핑은 구조적으로 불리합니다. 이 숫자가 1배를 넘으면 한 번의 평균적인 움직임으로는 비용도 못 냅니다.")
    if ev["day"]:
        st.subheader("당일 단타 규칙")
        if len(days) < need:
            st.warning(f"분봉 {len(days)}일 / 권장 {need}일 이상. 아래 결과는 표본이 적어 판정이 '표본부족'으로 나옵니다.")
        st.dataframe(verdict_table(ev["day"]), hide_index=True, use_container_width=True)
        for name, text in K.DAY_RULES.items():
            st.caption(f"{name}: {text}")
    if ev["scalp"]:
        st.subheader("1분봉 해상도에서 본 스켈핑 진단")
        sc = pd.DataFrame(ev["scalp"])
        sc = sc[sc["n"] > 0].rename(columns={"horizon_min": "보유(분)", "group": "상황", "n": "표본", "gross_bps": "비용 전 bp",
                                              "net_bps": "비용 후 bp", "t": "t값", "win_pct": "승률 %"})
        st.dataframe(sc[["보유(분)", "상황", "표본", "비용 전 bp", "비용 후 bp", "t값", "승률 %"]].round(2),
                     hide_index=True, use_container_width=True)
        st.caption("롱만 가정. '큰 상승/하락 직후'는 직전 1분 변동이 전체의 상위 10%일 때입니다. 수초 단위 스켈핑은 1분봉으로 검증할 수 없습니다.")

# ---------- 방법 ----------
with tab_method:
    st.markdown("""
**평가 방식**
- 모든 규칙은 미리 정한 숫자로만 돌립니다. 결과를 보고 숫자를 바꾸면 검증이 무의미해집니다.
- 신호는 종가(또는 봉 마감)에 확정되고 체결은 다음 시가로 가정합니다. 손절은 장중에 닿으면 손절가, 갭이면 시가입니다.
- 비용은 편도 슬리피지(스프레드 포함 틱 수)와 수수료를 왕복으로 차감합니다. ETF는 증권거래세가 없는 것으로 알고 있으나 확인이 필요합니다.
- 판정: 거래 30건 미만이면 표본부족, 기대값이 0 이하이면 음의 기대값, 양수여도 t값이 본페로니 기준 미만이거나 뒤쪽 40퍼센트 구간(검증구간)에서 양수가 아니면 근거 없음, 둘 다 넘으면 후보입니다.

**한계**
- 후보 판정은 돈 번다는 보증이 아닙니다. 과거 구간에서 우연히 설명되지 않는다는 정도의 근거입니다.
- 분봉은 수집을 시작한 날부터만 있습니다. 스켈핑(수초)은 이 데이터로 검증할 수 없습니다.
- 슬리피지는 가정값입니다. 실제 체결내역으로 보정하기 전까지는 낙관적일 수 있습니다.
- 현재 구조(GitHub Actions 수집, Streamlit 조회)는 실시간 신호 알림용이 아닙니다.
""")
