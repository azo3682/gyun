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

대조군(top10): 신호가 실제로 효과가 있는지 보려면 '신호가 없던 종목'과 비교해야 한다. 그래서 같은 날 순매수 상위 10 전체를
신호 여부와 상관없이 data/top10_baseline.json에 따로 기록한다(type="top10", 신호 파일과 섞지 않는다).
  - 진입가·D+n·지수 대비 계산은 신호와 똑같다. conditions에 is_transition / is_volume_supply를 남겨 앱이 집단별로 나눈다.
  - 최대 BASELINE_TRACK_DAYS(10)거래일치만 추적한다 (HORIZONS의 최대가 D+10).
  - 과거 날짜는 data/history/eod_candidates.csv(순매수 상위 10 + 전환신호 여부)로 소급해서 채운다. 이 CSV에는 거래량 순위가 없어서
    소급분의 is_volume_supply는 None(알 수 없음)이다.

장중 실행 규칙 (2026-10-06 추가): 장중에 수동으로 돌리면 오늘 날짜의 순위·신호·종가가 모두 잠정값이다.
  - FINAL_TIME(15:40) 전에는 '오늘 날짜'로 새 신호·대조군을 기록하지 않는다. (어제 이전 날짜 기록과 종가 갱신은 그대로 한다.)
  - 이미 장중에 기록된 오늘자 항목(recorded_at이 오늘 15:40 전)은 다음 실행 때 제거하고, 15:40 이후 실행이 확정 값으로 다시 기록한다.
    (같은 (종목, 날짜)는 건너뛰는 규칙 때문에 그냥 두면 장중 값이 영구히 남는다.)
  - 장중 실행은 '추적 종료(active=False)' 판정도 하지 않는다. 오늘 종가가 잠정이라, 그 값으로 끝내면 확정 종가로 못 고친다.
  - 신호일·D+n 종가는 다음 거래일 실행에서 공식 종가로 조금 바뀔 수 있다(2026-10 관측: 15:40 값이 최대 약 1.5% 달랐다). 최신 날짜 값은 잠정이다.

주의: 전환신호는 2026-09-29에 '상승' 조건을 추가해 9/21 백테스트 이후 재검증되지 않았고,
'거래량·수급 동시'는 매수 신호로 검증된 적이 없다(관심이 쏠렸다는 뜻일 뿐). 이 기록은 검증용 데이터를 쌓는 용도다.
"""

import csv
import json
import os
from datetime import datetime

from common import KST

TRACKER_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "signal_tracker.json")
INDEX_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "index_history.json")
BASELINE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "top10_baseline.json")
HISTORY_CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "history", "eod_candidates.csv")
DELETED_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "deleted_signals.json")
FINAL_TIME = (15, 40)      # 이 시각 이후의 실행만 '오늘 날짜'의 신호·대조군을 확정 기록한다
BASELINE_TYPE = "top10"
BASELINE_TRACK_DAYS = 10   # 대조군은 D+10까지만 필요하다
TRACK_DAYS = 40            # 신호 이후 이만큼의 거래일 종가를 쌓으면 추적 종료
HORIZONS = (1, 3, 5, 10)   # 성과를 계산할 보유 거래일 수 (D+n)
INDEX_TICKERS = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11"}
INDEX_KEEP_DAYS = 250      # 지수 이력에 남길 최대 일수

SIGNAL_TYPES = {"transition": "전환신호", "volume_supply": "거래량·수급 동시"}


def load_tracker(path: str = TRACKER_PATH, types=None, legacy_split: bool = True) -> dict:
    """저장된 추적 파일을 읽는다. 없거나 깨졌으면 빈 추적기.
    types: 인정할 종류(기본 SIGNAL_TYPES). 종류(type)가 없는 예전 기록(세 조건을 모두 만족하던 버전)은
    legacy_split=True일 때 두 종류 모두에 해당하므로 각각 하나씩으로 나눈다. 대조군 파일은 legacy_split=False로 읽는다."""
    types = types or SIGNAL_TYPES
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
        if s.get("type") in types:
            migrated.append(s)
        elif legacy_split:
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


def is_provisional_day(date_str, now: datetime) -> bool:
    """date_str(YYYYMMDD 또는 YYYY-MM-DD)가 오늘이고 아직 FINAL_TIME 전이면 True — 그 날의 순위·신호·종가는 장중 값이라 확정 기록하면 안 된다.
    어제 이전 날짜(장 시작 전·휴장일에 읽은 직전 거래일 데이터)는 False라서 정상적으로 기록된다."""
    return _fmt_date(date_str) == now.strftime("%Y-%m-%d") and (now.hour, now.minute) < FINAL_TIME


def purge_provisional(tracker: dict, now: datetime) -> int:
    """오늘 날짜인데 오늘 FINAL_TIME 전에 기록된(=장중 값으로 기록된) 항목을 지운다. 지운 개수 반환.
    같은 (종목, 날짜)는 다시 기록하지 않는 규칙 때문에, 지우지 않으면 장중 값이 영구히 남는다. 15:40 이후 실행이 확정 값으로 다시 기록한다."""
    today, cutoff = now.strftime("%Y-%m-%d"), f"{FINAL_TIME[0]:02d}:{FINAL_TIME[1]:02d}"
    keep = []
    for s in tracker["signals"]:
        ra = str(s.get("recorded_at") or "")
        if s.get("signal_date") == today and ra[:10] == today and ra[11:16] < cutoff:
            continue
        keep.append(s)
    removed = len(tracker["signals"]) - len(keep)
    tracker["signals"] = keep
    return removed


def find_signals(buy_rows: list, volume_ok: bool = True, now: datetime | None = None) -> list:
    """스냅샷의 순매수 상위 행에서 두 종류의 신호를 각각 찾는다.
    - transition: r['transition']이 True (거래소 위험 상태 종목은 eod_snapshot이 이미 걸러 False로 만든다)
    - volume_supply: 거래량 상위에도 올라 있음(r['volume_rank']). 거래량 순위 조회가 실패한 날은 판단할 수 없어 기록하지 않는다.
    한 종목이 둘 다 해당하면 종류별로 하나씩, 둘 다 overlap=True로 반환한다."""
    out = []
    for r in buy_rows:
        if not r.get("last_date") or not r.get("last_close"):
            continue
        if now is not None and is_provisional_day(r["last_date"], now):    # 장중 값은 기록하지 않는다
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


def load_deleted(path: str | None = DELETED_PATH) -> set:
    """사용자가 앱에서 지운 (종목코드, 신호일, 종류) 목록(data/deleted_signals.json). 이 조합은 다시 기록하지 않는다.
    파일이 없거나 깨졌으면 빈 집합. 지운 '그 건'만 막고 같은 종목의 앞으로의 신호는 막지 않는다."""
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f).get("deleted") or []
        return {(d["code"], d["date"], d["type"]) for d in items if isinstance(d, dict) and d.get("code") and d.get("date") and d.get("type")}
    except Exception:
        return set()


def record_signals(tracker: dict, signals: list, now: datetime | None = None, deleted: set | None = None) -> int:
    """새 신호를 추가하고 추가된 개수를 반환. (종목코드, 신호일, 종류)가 같으면 건너뛴다. recorded_at은 실행 시각(now)으로 찍는다.
    deleted: 사용자가 지운 조합(load_deleted) — 이 조합도 건너뛴다."""
    seen = {(s["code"], s["signal_date"], s["type"]) for s in tracker["signals"]} | set(deleted or ())
    added = 0
    stamp = (now or datetime.now(KST)).isoformat(timespec="seconds")
    for s in signals:
        key = (s["code"], s["signal_date"], s["type"])
        if key in seen:
            continue
        tracker["signals"].append({
            **s, "signal_close_first": s["signal_close"], "closes": {}, "active": True, "recorded_at": stamp,
        })
        seen.add(key)
        added += 1
    return added


def find_top10_entries(buy_rows: list, volume_ok: bool = True, now: datetime | None = None) -> list:
    """오늘의 순매수 상위 10 전체를 대조군으로 기록할 항목으로 바꾼다(신호 여부와 무관).
    conditions.is_transition: 전환신호가 떴나 / is_volume_supply: 거래량 상위에도 올랐나(거래량 순위 조회가 실패한 날은 None=알 수 없음)."""
    out = []
    for r in buy_rows or []:
        if not r.get("last_date") or not r.get("last_close"):
            continue
        if now is not None and is_provisional_day(r["last_date"], now):    # 장중 값은 기록하지 않는다
            continue
        day_pct = r.get("day_pct")
        out.append({
            "code": r["stock_code"], "name": r["stock_name"], "type": BASELINE_TYPE,
            "signal_date": _fmt_date(r["last_date"]), "signal_close": _num(r["last_close"]),
            "day_pct": round(day_pct * 100, 2) if day_pct is not None else None,
            "conditions": {
                "buy_rank": r.get("rank"), "volume_rank": r.get("volume_rank") if volume_ok else None,
                "is_transition": r.get("transition") is True,
                "is_volume_supply": bool(r.get("volume_rank")) if volume_ok else None,
                "risk_flags": list(r.get("market_risk_flags") or []),
            },
        })
    return out


def backfill_top10_from_history(tracker: dict, csv_path, exclude_date: str | None = None, now: datetime | None = None,
                                deleted: set | None = None) -> int:
    """data/history/eod_candidates.csv(날짜별 순매수 상위 10 + 전환신호 여부)로 과거 날짜의 대조군을 채운다.
    신호일 종가는 비워 두고, update_closes가 일봉에서 그 날짜 종가를 찾아 채운다(일봉 범위 밖이면 계속 빈다).
    이 CSV에는 거래량 순위가 없어서 is_volume_supply는 None이다. 이미 있는 (종목, 날짜)는 건너뛴다. 추가한 개수를 반환.
    exclude_date: 이 날짜의 행은 건너뛴다(장중 실행에서 오늘 날짜의 잠정 행을 소급하지 않기 위함)."""
    if not csv_path or not os.path.exists(csv_path):
        return 0
    seen = {(s["code"], s["signal_date"], s["type"]) for s in tracker["signals"]} | set(deleted or ())
    stamp = (now or datetime.now(KST)).isoformat(timespec="seconds")
    added = 0
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return 0
    for r in rows:
        date, code, name = (r.get("date") or "").strip(), (r.get("stock_code") or "").strip(), (r.get("stock_name") or "").strip()
        if not (date and code and name) or (code, date, BASELINE_TYPE) in seen or date == exclude_date:
            continue
        try:
            day_pct = round(float(r["day_pct"]) * 100, 2) if r.get("day_pct") not in (None, "") else None
        except ValueError:
            day_pct = None
        try:
            rank = int(float(r.get("rank")))
        except (TypeError, ValueError):
            rank = None
        tracker["signals"].append({
            "code": code, "name": name, "type": BASELINE_TYPE, "signal_date": date, "signal_close": None, "signal_close_first": None,
            "day_pct": day_pct, "closes": {}, "active": True, "recorded_at": stamp, "backfilled": True,
            "conditions": {"buy_rank": rank, "volume_rank": None, "is_transition": str(r.get("transition")) == "1",
                           "is_volume_supply": None,
                           "risk_flags": [x for x in (r.get("market_risk_flags") or "").split(";") if x]},
        })
        seen.add((code, date, BASELINE_TYPE))
        added += 1
    return added


def update_closes(tracker: dict, fetcher, track_days: int = TRACK_DAYS, deactivate: bool = True):
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
        s["closes"] = dict(sorted(closes.items())[:track_days])     # track_days개까지만 저장 (소급·갭 이후 한꺼번에 쌓이는 것 방지)
        all_opens = {**(s.get("opens") or {}), **opens}              # 날짜별 시가도 보관 — 눌림 진입(D+2 시가) 계산에 쓴다
        s["opens"] = dict(sorted((d, o) for d, o in all_opens.items() if d in s["closes"])[:track_days])
        if s["closes"]:
            # 진입일 = 신호 다음 거래일. 시가는 이번에 받은 일봉에 있으면 갱신(수정주가 계열 유지), 없으면 기존 값 유지
            s["entry_date"] = min(s["closes"])
            if s["entry_date"] in opens:
                s["entry_open"] = opens[s["entry_date"]]
        s["last_updated"] = datetime.now(KST).strftime("%Y-%m-%d")
        if deactivate and len(s["closes"]) >= track_days:   # 장중 실행(deactivate=False)에선 오늘 종가가 잠정이라 끝내지 않는다
            s["active"] = False
        updated += 1
    return updated, failed


KOSDAQ_TOKENS = ("KOSDAQ", "코스닥", "KSQ", "KQ")
KOSPI_TOKENS = ("KOSPI", "코스피", "유가증권", "KSP", "STK")


def index_key_from_market(market_name) -> str:
    """KIS 현재가 응답의 시장명(rprs_mrkt_kor_name)을 지수 키로. 알 수 없으면 ''(초과수익 비교 안 함).
    코스닥 종목이 '—'로 남던 문제(2026-10)를 겪어, 코스닥 표기를 넓게 받는다(KOSDAQ GLOBAL, 코스닥150, KSQ 등)."""
    m = str(market_name or "").upper()
    if any(t in m for t in KOSDAQ_TOKENS):
        return "KOSDAQ"
    if any(t in m for t in KOSPI_TOKENS):
        return "KOSPI"
    return ""


def probe_market_yf(code: str) -> str:
    """KIS 시장명으로 못 가린 종목의 대체 판별. yfinance에서 코드.KS(코스피)·코드.KQ(코스닥) 중 시세가 나오는 쪽을 시장으로 본다.
    비공식 데이터라 실패할 수 있다 — 실패하면 ''(다음 실행에서 다시 시도)."""
    import yfinance as yf
    for suffix, key in ((".KS", "KOSPI"), (".KQ", "KOSDAQ")):
        try:
            hist = yf.Ticker(code + suffix).history(period="5d")
        except Exception:
            continue
        if hist is not None and len(hist) > 0:
            return key
    return ""


def ensure_index_keys(tracker: dict, market_lookup, market_probe=None) -> int:
    """지수 키가 없는 신호에 시장(코스피/코스닥)을 채운다. market_lookup(code) -> 시장명, 종목당 한 번만 조회.
    시장명으로 못 가리면 market_probe(code)(예: probe_market_yf)로 한 번 더 시도한다.
    KIS가 준 원본 시장명은 market_raw에 남겨서, 못 가린 종목의 원인을 나중에 볼 수 있게 한다."""
    if market_lookup is None:
        return 0
    cache, filled = {}, 0
    for s in tracker["signals"]:
        if s.get("index_key"):
            continue
        code = s["code"]
        if code not in cache:
            raw, key = None, ""
            try:
                raw = market_lookup(code)
                key = index_key_from_market(raw)
            except Exception:
                key = ""
            if not key and market_probe is not None:
                try:
                    key = market_probe(code) or ""
                except Exception:
                    key = ""
            cache[code] = (key, raw)
        key, raw = cache[code]
        if raw is not None:
            s["market_raw"] = str(raw)[:40]
        if key:
            s["index_key"] = key
            filled += 1
    return filled


def unresolved_markets(tracker: dict) -> list:
    """시장을 못 가린 신호의 (종목코드, 종목명, KIS 원본 시장명) 목록(종목당 한 줄)."""
    seen, out = set(), []
    for s in tracker["signals"]:
        if not s.get("index_key") and s["code"] not in seen:
            seen.add(s["code"])
            out.append((s["code"], s["name"], s.get("market_raw")))
    return out


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
    perf["pullback"] = compute_pullback(s, closes, dates)
    if perf["pullback"] is None:
        del perf["pullback"]
    return perf


PULLBACK_HORIZONS = (3, 5, 10)    # 눌림 진입은 D+2 시가라서 청산은 D+3 종가부터 비교한다


def compute_pullback(s: dict, closes: dict, dates: list):
    """'눌림 대기 진입'을 '다음 날 시가 진입'과 같은 청산일 기준으로 비교한다. 일봉만으로 계산하는 보수적 근사다.
    규칙: 신호 다음 거래일(D+1) 종가가 신호일 종가보다 낮으면 '눌림 발생'으로 보고, 그 다음 거래일(D+2) 시가에 진입한다.
    (D+1 종가를 본 뒤에 D+2 시가에 사는 것이라 미래 정보를 쓰지 않는다. 장중에 눌림을 보고 바로 사는 실제 방식과는 다르다.)
    반환: {"triggered": 눌림 발생 여부, "entry_date", "entry_open"(D+2 시가, 아직 없으면 None), "ret": {n: 눌림 진입 수익률%},
           "imm_ret": {n: 같은 청산일의 다음 날 시가 진입 수익률%}}. D+1 종가가 아직 없거나 신호일 종가가 없으면 None."""
    base_close = s.get("signal_close")
    if not base_close or not dates:
        return None
    triggered = closes[dates[0]] < base_close
    out = {"triggered": bool(triggered), "ret": {}, "imm_ret": {}}
    entry_open, entry_open_date = s.get("entry_open"), s.get("entry_date")
    if not triggered or len(dates) < 2:
        if triggered:
            out["entry_open"], out["entry_date"] = None, None
        # 눌림이 안 온 신호도 같은 청산일의 즉시 진입 수익률을 남겨 '놓친 상승'을 볼 수 있게 한다
        if entry_open:
            for n in PULLBACK_HORIZONS:
                if len(dates) >= n:
                    out["imm_ret"][str(n)] = round((closes[dates[n - 1]] / entry_open - 1) * 100, 2)
        return out
    d2 = dates[1]
    pb_open = (s.get("opens") or {}).get(d2)
    out["entry_date"], out["entry_open"] = d2, pb_open
    for n in PULLBACK_HORIZONS:
        if len(dates) < n:
            continue
        c = closes[dates[n - 1]]
        if pb_open:
            out["ret"][str(n)] = round((c / pb_open - 1) * 100, 2)
        if entry_open:
            out["imm_ret"][str(n)] = round((c / entry_open - 1) * 100, 2)
    return out


def run(snapshot: dict, fetcher, path: str = TRACKER_PATH, track_days: int = TRACK_DAYS,
        market_lookup=None, index_fetcher=None, index_path: str = INDEX_PATH,
        baseline_path=None, history_csv=None, market_probe=None, now: datetime | None = None,
        deleted_path=None) -> dict:
    """스냅샷에서 두 종류의 신호를 찾아 기록하고, 진행 중인 신호의 종가를 갱신해 저장한다. 요약 dict 반환.
    같은 실행에서 순매수 상위 10 전체를 대조군(data/top10_baseline.json)으로도 기록한다.
    baseline_path/history_csv를 안 주면, 기본 경로(TRACKER_PATH)일 때만 저장소의 대조군·히스토리 파일을 쓰고
    다른 경로(테스트 등)일 때는 추적 파일 이름에 맞춘 별도 대조군 파일(<추적파일>_top10_baseline.json)을 쓰고 히스토리 소급은 하지 않는다.
    일봉·시장 조회는 두 추적기가 한 번만 호출하도록 이 실행 안에서 공유한다."""
    now = now or datetime.now(KST)
    final_run = (now.hour, now.minute) >= FINAL_TIME       # False = 장중(15:40 전) 실행: 오늘 날짜는 기록하지 않는다
    default_paths = os.path.abspath(path) == os.path.abspath(TRACKER_PATH)
    baseline_path = baseline_path or (BASELINE_PATH if default_paths else os.path.splitext(os.path.abspath(path))[0] + "_top10_baseline.json")
    history_csv = history_csv if history_csv is not None else (HISTORY_CSV_PATH if default_paths else None)
    deleted = load_deleted(deleted_path if deleted_path is not None else (DELETED_PATH if default_paths else None))   # 사용자가 지운 항목은 다시 기록하지 않는다
    _memo, _market_memo = {}, {}

    def shared_fetcher(code):
        if code not in _memo:
            _memo[code] = fetcher(code)
        return _memo[code]

    def shared_market(code):
        if code not in _market_memo:
            _market_memo[code] = market_lookup(code)
        return _market_memo[code]

    shared_lookup = shared_market if market_lookup is not None else None
    tracker = load_tracker(path)
    tracker["criteria"] = {
        "transition": "전환신호(VCP 눌림 후 거래량급증+상승)가 뜬 종목 (순매수 상위 10 안에서 계산)",
        "volume_supply": "순매수 상위 10과 거래량 상위에 같은 날 함께 오른 종목",
        "track_days": track_days,
    }
    volume_ok = snapshot.get("volume_rank_ok", True)
    purged = purge_provisional(tracker, now)
    signals = find_signals(snapshot["buy_top10"], volume_ok, now)
    added_before = {t: 0 for t in SIGNAL_TYPES}
    seen_before = {(s["code"], s["signal_date"], s["type"]) for s in tracker["signals"]} | deleted
    for s in signals:
        if (s["code"], s["signal_date"], s["type"]) not in seen_before:
            added_before[s["type"]] += 1
    record_signals(tracker, signals, now, deleted)
    updated, failed = update_closes(tracker, shared_fetcher, track_days, deactivate=final_run)
    ensure_index_keys(tracker, shared_lookup, market_probe)

    # 대조군: 순매수 상위 10 전체 (신호 여부 무관). 오늘 스냅샷을 먼저 기록하고, 그 뒤 히스토리 CSV로 과거 날짜를 소급한다.
    base = load_tracker(baseline_path, types={BASELINE_TYPE: "순매수 상위 10"}, legacy_split=False)
    base["criteria"] = {"top10": "순매수 상위 10 전체(신호 여부와 무관) — 신호가 실제로 효과가 있는지 비교하는 대조군", "track_days": BASELINE_TRACK_DAYS}
    purged += purge_provisional(base, now)
    base_added = record_signals(base, find_top10_entries(snapshot["buy_top10"], volume_ok, now), now, deleted)
    base_backfilled = backfill_top10_from_history(base, history_csv, None if final_run else now.strftime("%Y-%m-%d"), now, deleted)
    base_updated, base_failed = update_closes(base, shared_fetcher, BASELINE_TRACK_DAYS, deactivate=final_run)
    ensure_index_keys(base, shared_lookup, market_probe)

    index_status = {}
    index_hist = load_index_history(index_path)
    if tracker["signals"] or base["signals"]:   # 추적할 게 하나도 없으면 지수를 받을 이유가 없다
        index_hist, index_status = update_index_history(index_path, index_fetcher)
    for s in tracker["signals"] + base["signals"]:
        s["perf"] = compute_performance(s, index_hist)
    save_tracker(tracker, path)
    save_tracker(base, baseline_path)
    by_type = lambda f: {t: sum(1 for s in tracker["signals"] if s["type"] == t and f(s)) for t in SIGNAL_TYPES}
    return {
        "found": {t: sum(1 for s in signals if s["type"] == t) for t in SIGNAL_TYPES},
        "added": added_before, "active": by_type(lambda s: s.get("active", True)), "total": by_type(lambda s: True),
        "updated": updated, "failed": failed, "volume_ok": volume_ok, "index_status": index_status,
        "final": final_run, "purged": purged,
        "with_entry": sum(1 for s in tracker["signals"] if s.get("entry_open")),
        "baseline": {"added": base_added, "backfilled": base_backfilled, "total": len(base["signals"]),
                     "active": sum(1 for s in base["signals"] if s.get("active", True)),
                     "with_entry": sum(1 for s in base["signals"] if s.get("entry_open")),
                     "updated": base_updated, "failed": base_failed},
        "unresolved_markets": unresolved_markets(tracker) + [u for u in unresolved_markets(base) if u[0] not in {x[0] for x in unresolved_markets(tracker)}],
    }
