# -*- coding: utf-8 -*-
"""
scripts/k200_collect.py — KODEX 200(069500) 데이터 수집기 (GitHub Actions, 평일 15:50 KST)

저장 위치(data/k200/):
  daily.json         일봉(최근 약 3년, 실행할 때마다 합쳐서 갱신)
  minute/YYYYMMDD.json  그날의 1분봉 (수집한 날부터 쌓인다)
  nav_log.json       ETF 현재가 응답의 NAV·괴리율 스냅샷 (날짜별)
  status.json        이번 실행의 진단(성공/실패 건수, 오류 코드, 데이터 범위)

환경변수:
  K200_BACKFILL_DAYS  과거 분봉을 이 일수만큼 시도해서 채운다(기본 0). KIS '일별분봉' 조회가 되는지는
                      처음 실행해 보기 전에는 알 수 없으므로, status.json의 backfill 항목으로 결과를 확인한다.
  K200_DAILY_YEARS    일봉 수집 기간(년, 기본 3)

주의: API 응답 필드명은 KIS 공개 문서 기준이며, 이 파일을 쓴 시점에는 실제 호출로 검증하지 못했다.
      파싱 함수는 필드가 없으면 건너뛰도록 만들었고, 실패는 status.json에 사유 코드로 남긴다.
"""

import json
import os
import time
from datetime import datetime, timedelta

import requests

from common import BASE_URL, KST, kis_headers, FAIL_REASONS, record_fail

CODE = "069500"
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "k200")
MIN_DIR = os.path.join(DATA_DIR, "minute")

DAILY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
DAILY_TR = "FHKST03010100"
MIN_TODAY_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
MIN_TODAY_TR = "FHKST03010200"
MIN_HIST_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
MIN_HIST_TR = "FHKST03010230"
ETF_PRICE_PATH = "/uapi/etfetn/v1/quotations/inquire-price"
ETF_PRICE_TR = "FHPST02400000"

MAX_MIN_PAGES = 25


# ---------------- 공통 호출 ----------------

def kis_get(path, tr_id, params, retries=3):
    """(data, 오류코드). 초당 호출 제한(EGW00201)이면 쉬었다가 재시도한다."""
    last = ("EXCEPTION", "")
    for attempt in range(retries + 1):
        try:
            resp = requests.get(f"{BASE_URL}{path}", headers=kis_headers(tr_id), params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            last = ("EXCEPTION", str(e)[:80])
            time.sleep(0.6 * (attempt + 1))
            continue
        if data.get("rt_cd") == "0":
            return data, None
        code, msg = data.get("msg_cd", "UNKNOWN"), (data.get("msg1") or "")[:80]
        if code == "EGW00201":
            last = ("RATE_LIMIT_EXHAUSTED", msg)
            time.sleep(1.0 * (attempt + 1))
            continue
        record_fail(f"{tr_id}:{code}", msg)
        return None, code
    record_fail(f"{tr_id}:{last[0]}", last[1])
    return None, last[0]


def _f(v):
    try:
        x = float(str(v).replace(",", ""))
        return x
    except Exception:
        return None


# ---------------- 파싱 (네트워크 없음, 테스트 대상) ----------------

def parse_daily_rows(output2):
    rows = []
    for r in output2 or []:
        if not isinstance(r, dict):
            continue
        d = str(r.get("stck_bsop_date") or "").strip()
        o, h, l, c, v = (_f(r.get(k)) for k in ("stck_oprc", "stck_hgpr", "stck_lwpr", "stck_clpr", "acml_vol"))
        if len(d) != 8 or None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        rows.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v or 0.0})
    return rows


def parse_minute_rows(output2):
    rows = []
    for r in output2 or []:
        if not isinstance(r, dict):
            continue
        t = str(r.get("stck_cntg_hour") or "").strip()
        d = str(r.get("stck_bsop_date") or "").strip()
        o, h, l, c, v = (_f(r.get(k)) for k in ("stck_oprc", "stck_hgpr", "stck_lwpr", "stck_prpr", "cntg_vol"))
        if len(t) != 6 or None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        rows.append({"date": d, "time": t, "open": o, "high": h, "low": l, "close": c, "volume": v or 0.0})
    return rows


def parse_etf_price(output):
    if isinstance(output, list):
        output = output[0] if output else {}
    if not isinstance(output, dict):
        return {}
    keys = {"price": "stck_prpr", "nav": "nav", "nav_diff": "nav_prdy_vrss", "nav_chg_pct": "nav_prdy_ctrt",
            "premium_pct": "dprt", "tracking_err": "trc_errt", "volume": "acml_vol"}
    out = {k: _f(output.get(v)) for k, v in keys.items()}
    return {k: v for k, v in out.items() if v is not None}


def merge_daily(old, new):
    m = {r["date"]: r for r in old or []}
    for r in new or []:
        m[r["date"]] = r
    return [m[k] for k in sorted(m)]


def validate_minute_day(rows, date):
    """하루치 분봉이 쓸 만한지 검사한다. 문제가 있으면 사유 목록을 돌려준다(저장은 하되 표시)."""
    issues = []
    if len(rows) < 300:
        issues.append(f"봉 수 부족({len(rows)})")
    if rows:
        ts = sorted(r["time"] for r in rows)
        if ts[0] > "090500":
            issues.append(f"시초 누락(첫 봉 {ts[0]})")
        if ts[-1] < "151500":
            issues.append(f"마감 누락(마지막 봉 {ts[-1]})")
        if any(r["date"] and r["date"] != date for r in rows):
            issues.append("다른 날짜 혼입")
    return issues


# ---------------- 수집 ----------------

def fetch_daily_history(years=3, today=None):
    """일봉을 100개 안팎씩 거슬러 올라가며 모은다."""
    today = today or datetime.now(KST)
    start_limit = (today - timedelta(days=int(365 * years))).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    rows, pages = [], 0
    while pages < 20:
        time.sleep(0.2)
        data, err = kis_get(DAILY_PATH, DAILY_TR, {
            "fid_cond_mrkt_div_code": "J", "fid_input_iscd": CODE,
            "fid_input_date_1": start_limit, "fid_input_date_2": end,
            "fid_period_div_code": "D", "fid_org_adj_prc": "0"})
        pages += 1
        if data is None:
            return rows, err
        got = parse_daily_rows(data.get("output2"))
        if not got:
            break
        rows += got
        earliest = min(r["date"] for r in got)
        if earliest <= start_limit or len(got) < 5:
            break
        end = (datetime.strptime(earliest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    return merge_daily([], rows), None


def fetch_minutes_today(date, upper="153000"):
    """당일 분봉을 30개씩 거슬러 올라가며 모은다. (rows, 진단)"""
    rows, pages, diag = {}, 0, {"pages": 0, "errors": []}
    cursor = upper
    incl = "N"
    while pages < MAX_MIN_PAGES:
        time.sleep(0.2)
        data, err = kis_get(MIN_TODAY_PATH, MIN_TODAY_TR, {
            "FID_ETC_CLS_CODE": "", "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": CODE,
            "FID_INPUT_HOUR_1": cursor, "FID_PW_DATA_INCU_YN": incl})
        pages += 1
        if data is None:
            diag["errors"].append(err)
            break
        got = [r for r in parse_minute_rows(data.get("output2")) if not r["date"] or r["date"] == date]
        if not got:
            if incl == "N" and not rows:
                incl = "Y"          # 'N'에서 비어 있으면 'Y'로 한 번 더 시도
                continue
            break
        new = 0
        for r in got:
            if r["time"] not in rows:
                rows[r["time"]] = r
                new += 1
        earliest = min(r["time"] for r in got)
        if new == 0 or earliest <= "090000":
            break
        hh, mm = int(earliest[:2]), int(earliest[2:4])
        tot = hh * 60 + mm - 1
        cursor = f"{tot // 60:02d}{tot % 60:02d}00"
    diag["pages"] = pages
    out = [rows[k] for k in sorted(rows)]
    return out, diag


def fetch_minutes_history(date):
    """과거 하루치 분봉(일별분봉 조회). 이 API가 계정에서 되는지는 첫 실행으로 확인해야 한다."""
    rows, pages, diag = {}, 0, {"pages": 0, "errors": []}
    cursor = "153000"
    while pages < MAX_MIN_PAGES:
        time.sleep(0.2)
        data, err = kis_get(MIN_HIST_PATH, MIN_HIST_TR, {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": CODE, "FID_INPUT_HOUR_1": cursor,
            "FID_INPUT_DATE_1": date, "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": ""})
        pages += 1
        if data is None:
            diag["errors"].append(err)
            break
        got = [r for r in parse_minute_rows(data.get("output2")) if r["date"] == date]
        if not got:
            break
        new = 0
        for r in got:
            if r["time"] not in rows:
                rows[r["time"]] = r
                new += 1
        earliest = min(r["time"] for r in got)
        if new == 0 or earliest <= "090000":
            break
        tot = int(earliest[:2]) * 60 + int(earliest[2:4]) - 1
        cursor = f"{tot // 60:02d}{tot % 60:02d}00"
    diag["pages"] = pages
    return [rows[k] for k in sorted(rows)], diag


def fetch_etf_snapshot():
    data, err = kis_get(ETF_PRICE_PATH, ETF_PRICE_TR, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": CODE})
    if data is None:
        return {}, err
    return parse_etf_price(data.get("output")), None


# ---------------- 저장 ----------------

def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def save_minute_day(date, rows, issues):
    bars = [[r["time"], r["open"], r["high"], r["low"], r["close"], r["volume"]] for r in rows]
    _save(os.path.join(MIN_DIR, f"{date}.json"), {"date": date, "code": CODE, "issues": issues, "bars": bars})


def list_minute_dates():
    try:
        return sorted(f[:8] for f in os.listdir(MIN_DIR) if f.endswith(".json") and f[:8].isdigit())
    except Exception:
        return []


def main():
    now = datetime.now(KST)
    today = now.strftime("%Y%m%d")
    status = {"run_at": now.isoformat(timespec="seconds"), "code": CODE, "daily": {}, "minute_today": {},
              "etf_snapshot": {}, "backfill": {}}

    # 1) 일봉
    years = float(os.environ.get("K200_DAILY_YEARS", "3") or 3)
    new_rows, err = fetch_daily_history(years, now)
    old = _load(os.path.join(DATA_DIR, "daily.json"), {}).get("rows", [])
    merged = merge_daily(old, new_rows)
    if merged:
        _save(os.path.join(DATA_DIR, "daily.json"), {"code": CODE, "updated": now.isoformat(timespec="seconds"), "rows": merged})
    status["daily"] = {"fetched": len(new_rows), "total": len(merged), "error": err,
                       "first": merged[0]["date"] if merged else None, "last": merged[-1]["date"] if merged else None}

    # 2) 오늘 분봉 (일봉에 오늘 날짜가 있어야 거래일이다)
    is_trading_day = bool(merged) and merged[-1]["date"] == today
    if is_trading_day:
        rows, diag = fetch_minutes_today(today)
        issues = validate_minute_day(rows, today) if rows else ["수집 0건"]
        if rows:
            save_minute_day(today, rows, issues)
        status["minute_today"] = {"bars": len(rows), "issues": issues, **diag}
    else:
        status["minute_today"] = {"skipped": "오늘 일봉이 없어 거래일로 보지 않음(휴장일이거나 일봉 조회 실패)"}

    # 3) NAV·괴리율 스냅샷
    snap, err = fetch_etf_snapshot()
    status["etf_snapshot"] = {"ok": bool(snap), "error": err, "fields": sorted(snap)}
    if snap:
        log = _load(os.path.join(DATA_DIR, "nav_log.json"), {})
        log[today] = snap
        _save(os.path.join(DATA_DIR, "nav_log.json"), log)

    # 4) 과거 분봉 채우기 (선택)
    n_back = int(os.environ.get("K200_BACKFILL_DAYS", "0") or 0)
    if n_back > 0 and merged:
        have = set(list_minute_dates())
        cands = [r["date"] for r in merged[-(n_back + 1):] if r["date"] != today and r["date"] not in have]
        ok = fail = 0
        sample = None
        for d in cands:
            rows, diag = fetch_minutes_history(d)
            issues = validate_minute_day(rows, d) if rows else ["수집 0건"]
            if rows:
                save_minute_day(d, rows, issues)
                ok += 1
            else:
                fail += 1
            sample = sample or {"date": d, "bars": len(rows), "issues": issues, **diag}
        status["backfill"] = {"tried": len(cands), "ok": ok, "fail": fail, "first_attempt": sample}

    dates = list_minute_dates()
    status["minute_days_total"] = len(dates)
    status["minute_first"], status["minute_last"] = (dates[0], dates[-1]) if dates else (None, None)
    status["minute_dates"] = dates        # 대시보드가 원격 파일을 읽을 때 목록으로 쓴다
    status["fail_reasons"] = FAIL_REASONS
    _save(os.path.join(DATA_DIR, "status.json"), status)

    print(f"일봉 {status['daily']}")
    print(f"오늘 분봉 {status['minute_today']}")
    print(f"ETF 스냅샷 {status['etf_snapshot']}")
    print(f"백필 {status['backfill']}")
    print(f"분봉 누적 {len(dates)}일, 실패 사유 {FAIL_REASONS}")
    if not merged:
        raise SystemExit("일봉을 하나도 받지 못했습니다 — 키/네트워크/응답 형식을 확인하세요.")


if __name__ == "__main__":
    main()
