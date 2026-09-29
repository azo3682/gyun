# -*- coding: utf-8 -*-
"""
scripts/signal_tracker.py
두 종류의 신호를 각각 따로 기록하고, 신호일 종가 옆에 그 뒤 매 거래일 종가를 이어서 적는다.
(대시보드 '📌 신호 추적' 탭이 읽는다.)

  - transition    : 전환신호(VCP 눌림 후 거래량급증+상승)가 뜬 종목
  - volume_supply : 순매수 상위 10과 거래량 상위에 같은 날 함께 오른 종목 ('동시 등장' 탭과 같은 정의)

두 신호는 서로의 조건을 요구하지 않는다. 한 종목이 같은 날 둘 다 해당하면 각각 따로 기록하고
conditions.overlap=True로 표시한다(나중에 '겹친 경우가 더 잘 올랐나'를 보려는 용도).

eod_snapshot.py가 평일 15:40 KST에 이 모듈을 호출한다. 결과: data/signal_tracker.json

- 신호일 종가: 신호가 뜬 날(일봉 마지막 행 날짜)의 종가. (종목, 신호일, 신호 종류)가 같으면 한 번만 기록한다.
- 매 실행마다 진행 중인 신호의 일봉을 다시 읽어 신호일 이후 종가를 전부 채운다.
  → 하루 실행이 실패해도 다음 실행에서 빠진 날이 메워진다.
- 일봉은 수정주가라서, 신호 이후 액면분할·무상증자 등이 있으면 과거 종가가 소급 조정된다.
  신호일 종가도 같은 계열로 매번 갱신해 '신호일 대비' 비교가 어긋나지 않게 하고,
  맨 처음 기록한 값은 signal_close_first에 남긴다.
- TRACK_DAYS 거래일치 종가가 쌓이면 그 신호는 추적을 끝낸다(active=False, 기록은 그대로 남음).

성과 계산 (신호마다 perf에 저장, 매 실행마다 다시 계산):
  - 진입가 = 신호 다음 거래일의 시가(entry_open). 신호는 15:40 마감 후에야 확인되므로 현실적으로 살 수 있는 가장 이른 가격이다.
    실제 체결은 이 가격과 다를 수 있다(시가 단일가 체결 가정).
  - gap_pct = 신호일 종가 → 다음 날 시가 변동률. 갭이 크면 신호일 종가는 현실에서 살 수 없는 가격이다.
  - D+n: 신호일이 D+0, 다음 거래일이 D+1(진입일). D+n 수익률 = D+n 종가 / 진입가 - 1 (D+1은 진입일 당일 시가→종가).
    참고용으로 신호일 종가 대비 수익률(ret_from_close)도 같이 저장한다.
  - 지수 대비 초과수익 = 종목 수익률 - 같은 기간 시장 지수 수익률(%p). 종목이 코스피면 코스피, 코스닥이면 코스닥과 비교한다.
    지수 기준가는 진입일 지수 시가(없으면 신호일 지수 종가로 대체하고 idx_basis에 표시).
  - 지수는 yfinance(^KS11, ^KQ11)로 받아 data/index_history.json에 날짜별로 쌓는다. 비공식 데이터라 실패할 수 있고,
    실패한 날은 기존 값을 그대로 두고 다음 실행에서 채운다. 오늘자 지수 종가는 장 마감 직후엔 지연·미확정일 수 있어 다음 실행에서 덮어써진다.

주의: 전환신호는 2026-09-29에 '상승' 조건을 추가해 9/21 백테스트 이후 재검증되지 않았고,
'거래량·수급 동시'는 매수 신호로 검증된 적이 없다(관심이 쏠렸다는 뜻일 뿐). 이 기록은 검증용 데이터를 쌓는 용도다.
"""

import json
import os
from datetime import datetime

from common import KST

TRACKER_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "signal_tracker.json")
INDEX_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "index_history.json")
TRACK_DAYS = 40            # 신호 이후 이만큼의 거래일 종가를 쌓으면 추적 종료
HORIZONS = (1, 3, 5, 10)   # 성과를 계산할 보유 거래일 수 (D+n)
INDEX_TICKERS = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11"}
INDEX_KEEP_DAYS = 250      # 지수 이력에 남길 최대 일수

SIGNAL_TYPES = {"transition": "전환신호", "volume_supply": "거래량·수급 동시"}


def load_tracker(path: str = TRACKER_PATH) -> dict:
    """저장된 추적 파일을 읽는다. 없거나 깨졌으면 빈 추적기.
    종류(type)가 없는 예전 기록(세 조건을 모두 만족하던 버전)은 두 종류 모두에 해당하므로 각각 하나씩으로 나눈다."""
    if not os.path.exists(path):
        return {"signals": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        signals = data.get("signals")
        if not isinstance(signals, list):
            return {"signals": []}
    except Exception:
        return {"signals": []}
    migrated = []
    for s in signals:
        if s.get("type") in SIGNAL_TYPES:
            migrated.append(s)
        else:
            for t in SIGNAL_TYPES:
                copy = {**s, "type": t, "closes": dict(s.get("closes") or {})}
                copy["conditions"] = {**(s.get("conditions") or {}), "overlap": True}
                migrated.append(copy)
    data["signals"] = migrated
    return data


def save_tracker(tracker: dict, path: str = TRACKER_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tracker["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)


def _fmt_date(yyyymmdd: str) -> str:
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def _num(v):
    """가격은 정수면 정수로, 아니면 float로 (JSON을 깔끔하게)."""
    v = float(v)
    return int(v) if v.is_integer() else v


def find_signals(buy_rows: list, volume_ok: bool = True) -> list:
    """스냅샷의 순매수 상위 행에서 두 종류의 신호를 각각 찾는다.
    - transition: r['transition']이 True (거래소 위험 상태 종목은 eod_snapshot이 이미 걸러 False로 만든다)
    - volume_supply: 거래량 상위에도 올라 있음(r['volume_rank']). 거래량 순위 조회가 실패한 날은 판단할 수 없어 기록하지 않는다.
    한 종목이 둘 다 해당하면 종류별로 하나씩, 둘 다 overlap=True로 반환한다."""
    out = []
    for r in buy_rows:
        if not r.get("last_date") or not r.get("last_close"):
            continue
        is_trans = r.get("transition") is True
        is_vs = bool(volume_ok and r.get("volume_rank"))
        if not (is_trans or is_vs):
            continue
        day_pct = r.get("day_pct")
        base = {
            "code": r["stock_code"], "name": r["stock_name"],
            "signal_date": _fmt_date(r["last_date"]), "signal_close": _num(r["last_close"]),
            "day_pct": round(day_pct * 100, 2) if day_pct is not None else None,
        }
        cond = {
            "buy_rank": r.get("rank"), "volume_rank": r.get("volume_rank"),
            "overlap": is_trans and is_vs, "risk_flags": list(r.get("market_risk_flags") or []),
        }
        if is_trans:
            out.append({**base, "type": "transition", "conditions": dict(cond)})
        if is_vs:
            out.append({**base, "type": "volume_supply", "conditions": dict(cond)})
    return out


def record_signals(tracker: dict, signals: list) -> int:
    """새 신호를 추가하고 추가된 개수를 반환. (종목코드, 신호일, 종류)가 같으면 건너뛴다."""
    seen = {(s["code"], s["signal_date"], s["type"]) for s in tracker["signals"]}
    added = 0
    now = datetime.now(KST).isoformat(timespec="seconds")
    for s in signals:
        key = (s["code"], s["signal_date"], s["type"])
        if key in seen:
            continue
        tracker["signals"].append({
            **s, "signal_close_first": s["signal_close"], "closes": {}, "active": True, "recorded_at": now,
        })
        seen.add(key)
        added += 1
    return added


def update_closes(tracker: dict, fetcher, track_days: int = TRACK_DAYS):
    """진행 중인 신호마다 일봉을 읽어 신호일 이후 종가를 채운다. (갱신 수, 조회 실패 수) 반환.
    fetcher(code) -> 일봉 DataFrame (stck_bsop_date, stck_clpr 컬럼). 비어 있으면 실패로 센다.
    같은 종목이 두 종류에 다 있어도 일봉은 한 번만 조회한다."""
    cache = {}

    def get(code):
        if code not in cache:
            cache[code] = fetcher(code)
        return cache[code]

    updated = failed = 0
    for s in tracker["signals"]:
        if not s.get("active", True):
            continue
        df = get(s["code"])
        if df is None or df.empty or "stck_clpr" not in df.columns:
            failed += 1
            continue
        closes = dict(s.get("closes") or {})
        opens = {}
        open_col = df["stck_oprc"] if "stck_oprc" in df.columns else [None] * len(df)
        for d_raw, c, o in zip(df["stck_bsop_date"], df["stck_clpr"], open_col):
            d = _fmt_date(d_raw)
            if c != c or c is None:          # NaN
                continue
            if d == s["signal_date"]:
                s["signal_close"] = _num(c)   # 수정주가 계열로 맞춰 갱신
            elif d > s["signal_date"]:
                closes[d] = _num(c)
                if o is not None and o == o and float(o) > 0:
                    opens[d] = _num(o)
        s["closes"] = dict(sorted(closes.items()))
        if s["closes"]:
            # 진입일 = 신호 다음 거래일. 시가는 이번에 받은 일봉에 있으면 갱신(수정주가 계열 유지), 없으면 기존 값 유지
            s["entry_date"] = min(s["closes"])
            if s["entry_date"] in opens:
                s["entry_open"] = opens[s["entry_date"]]
        s["last_updated"] = datetime.now(KST).strftime("%Y-%m-%d")
        if len(s["closes"]) >= track_days:
            s["active"] = False
        updated += 1
    return updated, failed


def index_key_from_market(market_name) -> str:
    """KIS 현재가 응답의 시장명(rprs_mrkt_kor_name)을 지수 키로. 알 수 없으면 ''(초과수익 비교 안 함)."""
    m = str(market_name or "").upper()
    if "KOSDAQ" in m or "코스닥" in m:
        return "KOSDAQ"
    if "KOSPI" in m or "코스피" in m or "유가증권" in m:
        return "KOSPI"
    return ""


def ensure_index_keys(tracker: dict, market_lookup) -> int:
    """지수 키가 없는 신호에 시장(코스피/코스닥)을 채운다. market_lookup(code) -> 시장명. 종목당 한 번만 조회."""
    if market_lookup is None:
        return 0
    cache, filled = {}, 0
    for s in tracker["signals"]:
        if s.get("index_key"):
            continue
        code = s["code"]
        if code not in cache:
            try:
                cache[code] = index_key_from_market(market_lookup(code))
            except Exception:
                cache[code] = ""
        if cache[code]:
            s["index_key"] = cache[code]
            filled += 1
    return filled


def fetch_index_yf(key: str, period: str = "4mo") -> dict:
    """yfinance로 지수 일봉을 받아 {날짜: {"open","close"}}로. 실패하면 예외를 던진다."""
    import yfinance as yf
    hist = yf.Ticker(INDEX_TICKERS[key]).history(period=period)
    out = {}
    for ts, row in hist.iterrows():
        o, c = float(row["Open"]), float(row["Close"])
        out[ts.strftime("%Y-%m-%d")] = {"open": o if (o == o and o > 0) else None,
                                        "close": c if (c == c and c > 0) else None}
    return out


def load_index_history(path: str = INDEX_PATH) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def update_index_history(path: str = INDEX_PATH, fetcher=None, keys=None):
    """지수 이력 파일을 갱신하고 (이력 dict, {키: 성공 여부})를 반환. 실패한 지수는 기존 값을 그대로 둔다."""
    fetcher = fetcher or fetch_index_yf
    hist = load_index_history(path)
    status = {}
    for key in (keys or INDEX_TICKERS):
        try:
            fresh = fetcher(key)
            if not fresh:
                raise ValueError("빈 응답")
            merged = dict(hist.get(key) or {})
            merged.update(fresh)                 # 같은 날짜는 최신 값으로 덮어쓴다 (오늘자 미확정 값 교정)
            hist[key] = dict(sorted(merged.items())[-INDEX_KEEP_DAYS:])
            status[key] = True
        except Exception as e:
            print(f"지수 이력 갱신 실패({key}): {e}")
            status[key] = False
    if any(status.values()):             # 하나도 못 받았으면 파일을 새로 만들지 않는다
        hist["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False)
    return hist, status


def compute_performance(s: dict, index_history: dict) -> dict:
    """신호 하나의 성과. 계산할 수 없는 항목은 키 자체가 없다.
    ret[n]: 진입가(다음 날 시가) 대비 D+n 종가 수익률(%), ret_from_close[n]: 신호일 종가 대비,
    idx_ret[n]: 같은 기간 지수 수익률(%), excess[n]: ret - idx_ret (%p)."""
    closes = s.get("closes") or {}
    dates = sorted(closes)                     # dates[0] = D+1(진입일), dates[n-1] = D+n
    base_close, entry_open, entry_date = s.get("signal_close"), s.get("entry_open"), s.get("entry_date")
    perf = {"ret": {}, "ret_from_close": {}, "idx_ret": {}, "excess": {}}
    if base_close and entry_open:
        perf["gap_pct"] = round((entry_open / base_close - 1) * 100, 2)
    idx = (index_history or {}).get(s.get("index_key") or "", {}) or {}
    idx_open = (idx.get(entry_date) or {}).get("open") if entry_date else None
    idx_base, basis = (idx_open, "open") if idx_open else ((idx.get(s["signal_date"]) or {}).get("close"), "prev_close")
    for n in HORIZONS:
        if len(dates) < n or not base_close:
            continue
        d = dates[n - 1]
        c = closes[d]
        perf["ret_from_close"][str(n)] = round((c / base_close - 1) * 100, 2)
        if not entry_open:
            continue
        ret = (c / entry_open - 1) * 100
        perf["ret"][str(n)] = round(ret, 2)
        idx_close = (idx.get(d) or {}).get("close")
        if idx_base and idx_close:
            idx_ret = (idx_close / idx_base - 1) * 100
            perf["idx_ret"][str(n)] = round(idx_ret, 2)
            perf["excess"][str(n)] = round(ret - idx_ret, 2)
            perf["idx_basis"] = basis
    return perf


def run(snapshot: dict, fetcher, path: str = TRACKER_PATH, track_days: int = TRACK_DAYS,
        market_lookup=None, index_fetcher=None, index_path: str = INDEX_PATH) -> dict:
    """스냅샷에서 두 종류의 신호를 찾아 기록하고, 진행 중인 신호의 종가를 갱신해 저장한다. 요약 dict 반환."""
    tracker = load_tracker(path)
    tracker["criteria"] = {
        "transition": "전환신호(VCP 눌림 후 거래량급증+상승)가 뜬 종목 (순매수 상위 10 안에서 계산)",
        "volume_supply": "순매수 상위 10과 거래량 상위에 같은 날 함께 오른 종목",
        "track_days": track_days,
    }
    volume_ok = snapshot.get("volume_rank_ok", True)
    signals = find_signals(snapshot["buy_top10"], volume_ok)
    added_before = {t: 0 for t in SIGNAL_TYPES}
    seen_before = {(s["code"], s["signal_date"], s["type"]) for s in tracker["signals"]}
    for s in signals:
        if (s["code"], s["signal_date"], s["type"]) not in seen_before:
            added_before[s["type"]] += 1
    record_signals(tracker, signals)
    updated, failed = update_closes(tracker, fetcher, track_days)
    ensure_index_keys(tracker, market_lookup)
    index_status = {}
    index_hist = load_index_history(index_path)
    if tracker["signals"]:               # 신호가 하나도 없으면 지수를 받을 이유가 없다
        index_hist, index_status = update_index_history(index_path, index_fetcher)
    for s in tracker["signals"]:
        s["perf"] = compute_performance(s, index_hist)
    save_tracker(tracker, path)
    by_type = lambda f: {t: sum(1 for s in tracker["signals"] if s["type"] == t and f(s)) for t in SIGNAL_TYPES}
    return {
        "found": {t: sum(1 for s in signals if s["type"] == t) for t in SIGNAL_TYPES},
        "added": added_before, "active": by_type(lambda s: s.get("active", True)), "total": by_type(lambda s: True),
        "updated": updated, "failed": failed, "volume_ok": volume_ok, "index_status": index_status,
        "with_entry": sum(1 for s in tracker["signals"] if s.get("entry_open")),
    }
