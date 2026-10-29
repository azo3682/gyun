# -*- coding: utf-8 -*-
"""
scripts/value_screen.py
평일 아침(07:20 KST)에 GitHub Actions로 실행 — 전체 상장사 저평가 스캔.

왜 이렇게 만들었나
- KIS의 '시장가치 순위' API는 PER 높은 순으로만 응답하고 다음 페이지 조회도 안 돼서
  '가장 싼 종목' 쪽 끝을 가져올 방법이 없다(2026-09-23 확인).
- 대신 이미 검증된 현재가 API(inquire-price)가 종목별 PER/PBR/EPS/BPS/시가총액을 한 번에
  돌려주므로, DART 상장사 목록 전체를 이 API로 훑어서 우리가 직접 걸러낸다.

결과: data/value_screen.json (대시보드 '저평가 후보' 탭과 아침 이메일이 읽음)
  - balanced: 균형형 (PER <= BAL_PER_MAX 이고 PBR <= BAL_PBR_MAX 이면서 ROE BAL_ROE_MIN~MAX)
              — PER·PBR이 둘 다 낮고 수익성도 적당한 종목. 처음 볼 때 권하는 목록.
              ROE는 KIS 재무비율 API의 정식 값(조회 실패 시에만 PBR÷PER 역산 추정으로 대체).
              순서는 종합점수(ROE·부채비율·성장률·PER/PBR) 높은 순. PER이 PER_CAUTION_BELOW 미만은 뒤로(표시는 유지)
  - low_per : 저PER 우량 (0 < PER <= LOW_PER_MAX 이면서 PBR <= LOW_PER_PBR_MAX)
  - low_pbr : 저PBR 자산가치 (0 < PBR <= LOW_PBR_MAX 이면서 흑자)

재무비율(정식 ROE·부채비율·매출/영업이익 증가율)은 전 종목이 아니라 PER/PBR 1차 필터를 통과한
'후보 풀'에만 조회한다 (전 종목에 하면 호출이 2배가 됨). 풀 밖 종목은 종합점수가 없다.

주의: KIS PER은 '가장 최근 확정된 연간 EPS' 기준이라, 실적이 막 좋아지는 회사는 PER이
아직 높게, 막 나빠지는 회사는 낮게 보일 수 있다. 이 스크린은 관심 종목 '풀'을 만들 뿐,
매수 신호로 검증된 게 아니다(백테스트 불가 — 과거 시점 재무데이터가 없음).
"""

import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import common
from common import (
    KST, fetch_price_detail, get_access_token, load_dart_name_map,
    check_disclosure_risk, is_fund_product, record_fail, _to_float,
    fetch_financial_ratio, strip_raw, composite_score, SCORE_WEIGHTS,
)

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "value_screen.json")

# ---- 스크린 기준 (필요하면 숫자만 바꾸면 됨) ----
MIN_MKTCAP_EOK = 500        # 시가총액 최소(억원) — 초소형 잡주 제외
MIN_TR_VALUE_EOK = 1.0      # 전일 거래대금 최소(억원) — 거래 안 되는 종목 제외 (데이터가 있을 때만 적용)
LOW_PER_MAX = 10.0
LOW_PER_PBR_MAX = 1.5
LOW_PBR_MAX = 0.6
# 균형형: PER·PBR이 둘 다 낮고, ROE(추정)가 적당한 종목 (한쪽만 싼 종목, 수익성이 낮아 싼 종목, 일회성 이익 종목을 함께 거른다)
BAL_PER_MAX = 10.0
BAL_PBR_MAX = 1.0
BAL_ROE_MIN = 10.0
BAL_ROE_MAX = 25.0
TOP_N = 50                  # 저PER·저PBR 목록에 저장할 최대 종목 수
BAL_TOP_N = 100             # 균형형은 통과 종목을 사실상 전부 저장 (표 머리글을 눌러 직접 정렬해 볼 수 있게)
RISK_CHECK_N = 20           # 상위 몇 개까지 DART 공시 리스크를 확인할지
PER_CAUTION_BELOW = 3.0     # PER이 이 값 미만이면 '이익 지속성 확인 필요' 표시 (정상 영업이익으로는 드문 수준)
ROE_SUSPECT_PCT = 40.0      # PBR÷PER로 역산한 ROE가 이 값을 넘으면 '일회성 이익 의심' — 목록 뒤로 밀고 표시

# ---- 재무비율(정식 ROE·부채비율·성장률) 조회 설정 ----
FIN_POOL_MAX = 600          # 재무비율을 조회할 후보 풀 최대 종목 수 (PER/PBR이 낮은 순으로 자름)
FIN_DEADLINE_SEC = 10 * 60  # 재무비율 조회 단계 제한 시간 — 넘기면 지금까지 모은 것만 반영
BAL_MIN_SCORE = None        # 균형형에 최소 종합점수를 걸고 싶으면 숫자로 (예: 55). None이면 점수로는 거르지 않고 정렬에만 씀

# ---- 호출 설정 ----
CALLS_PER_SEC = 9.0         # KIS 실전 계좌 제한(초당 20건)보다 넉넉히 낮게
WORKERS = 8
DEADLINE_SEC = 25 * 60      # 이 시간을 넘기면 지금까지 모은 것만 저장(partial)

# ---- 제외 기준 ----
EXTRA_EXCLUDE_NAME_KEYWORDS = ["리츠", "인프라투융자"]   # 스팩/ETF 계열은 is_fund_product가 처리
# 종목상태구분코드(iscd_stat_cls_code): 51 관리종목, 52 투자위험, 53 투자경고, 58 거래정지
EXCLUDE_STAT_CODES = {"51", "52", "53", "58"}
# 시장경고구분코드(mrkt_warn_cls_code): 02 투자경고, 03 투자위험
EXCLUDE_WARN_CODES = {"02", "03"}


class RateLimiter:
    """여러 스레드가 공유하는 초당 호출 수 제한기."""

    def __init__(self, per_sec: float):
        self.interval = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next_time)
            self.next_time = t + self.interval
        delay = t - now
        if delay > 0:
            time.sleep(delay)


def parse_row(code: str, name: str, out: dict):
    """현재가 API 응답(output)에서 필요한 값만 뽑는다. 현재가가 없으면 None."""
    price = _to_float(out.get("stck_prpr"))
    if not price:
        return None

    mktcap = _to_float(out.get("hts_avls"))                # 억원
    if mktcap is None:
        shares = _to_float(out.get("lstn_stcn"))
        mktcap = price * shares / 1e8 if shares else None
    tr_value = _to_float(out.get("acml_tr_pbmn"))          # 원
    w52_high = _to_float(out.get("w52_hgpr"))
    per, pbr = _to_float(out.get("per")), _to_float(out.get("pbr"))
    # ROE = EPS/BPS = PBR/PER. 40%를 넘으면 지속 가능한 수익성이라기보다 일회성 이익(자산 매각 등)일 가능성이 높다
    roe_pct = round(100 * pbr / per, 1) if per and pbr and per > 0 and pbr > 0 else None

    return {
        "code": code,
        "name": name,
        "market": out.get("rprs_mrkt_kor_name") or "",
        "sector": out.get("bstp_kor_isnm") or "",
        "price": price,
        "day_pct": _to_float(out.get("prdy_ctrt")),
        "per": per,
        "pbr": pbr,
        "roe_pct": roe_pct,
        "oneoff_suspect": bool(roe_pct is not None and roe_pct > ROE_SUSPECT_PCT),
        "eps": _to_float(out.get("eps")),
        "bps": _to_float(out.get("bps")),
        "mktcap_eok": round(mktcap, 1) if mktcap is not None else None,
        "tr_value_eok": round(tr_value / 1e8, 2) if tr_value is not None else None,
        "drawdown_pct": round((price / w52_high - 1) * 100, 1) if w52_high else None,
        "stat_code": str(out.get("iscd_stat_cls_code") or ""),
        "warn_code": str(out.get("mrkt_warn_cls_code") or ""),
        "halted": str(out.get("temp_stop_yn") or "") == "Y",
    }


def _availability(rows: list, key: str) -> float:
    return sum(1 for r in rows if r.get(key) is not None) / len(rows) if rows else 0.0


def _eligible(rows: list):
    """시총·거래대금·거래소 상태 기준을 통과한 종목과, 시총/거래대금 필터 적용 여부를 반환."""
    # 시총·거래대금 필드가 아예 안 내려오는 경우(필드명 불일치 등)엔 그 필터를 끄고 기록한다
    use_mktcap = _availability(rows, "mktcap_eok") >= 0.5
    use_liquidity = _availability(rows, "tr_value_eok") >= 0.5 and \
        sum(1 for r in rows if (r.get("tr_value_eok") or 0) > 0) / max(len(rows), 1) >= 0.3

    def base_ok(r):
        if "KONEX" in r["market"].upper():
            return False
        if r["halted"] or r["stat_code"] in EXCLUDE_STAT_CODES or r["warn_code"] in EXCLUDE_WARN_CODES:
            return False
        if use_mktcap and (r["mktcap_eok"] is None or r["mktcap_eok"] < MIN_MKTCAP_EOK):
            return False
        if use_liquidity and (r["tr_value_eok"] is None or r["tr_value_eok"] < MIN_TR_VALUE_EOK):
            return False
        return True

    return [r for r in rows if base_ok(r)], use_mktcap, use_liquidity


def select_fin_pool(rows: list, max_n: int = FIN_POOL_MAX) -> list:
    """재무비율을 조회할 종목코드 목록: 세 목록(균형형·저PER·저PBR)의 PER/PBR 조건을 하나라도 만족하는 종목.
    (세 목록은 모두 이 풀의 부분집합이라, 풀 안에서는 전부 종합점수가 계산된다.)"""
    eligible, _, _ = _eligible(rows)
    pool = [r for r in eligible
            if r["per"] is not None and r["pbr"] is not None and r["per"] > 0 and r["pbr"] > 0
            and ((r["per"] <= LOW_PER_MAX and r["pbr"] <= LOW_PER_PBR_MAX)
                 or r["pbr"] <= LOW_PBR_MAX
                 or (r["per"] <= BAL_PER_MAX and r["pbr"] <= BAL_PBR_MAX))]
    pool.sort(key=lambda r: r["per"] / max(LOW_PER_MAX, 1e-9) + r["pbr"] / max(BAL_PBR_MAX, 1e-9))
    return [r["code"] for r in pool[:max_n]]


def attach_fundamentals(r: dict, fin: dict | None):
    """행에 정식 재무비율과 종합점수를 붙인다. fin이 None이면 재무 항목은 비우고 점수도 계산하지 않는다
    (PER/PBR만으로 계산한 점수가 다른 종목의 점수와 섞여 보이는 걸 막기 위해)."""
    r["roe"] = fin.get("roe") if fin else None
    r["debt_ratio"] = fin.get("debt_ratio") if fin else None
    r["sales_growth"] = fin.get("sales_growth") if fin else None
    r["op_growth"] = fin.get("op_growth") if fin else None
    r["ni_growth"] = fin.get("ni_growth") if fin else None
    r["fin_yymm"] = fin.get("stac_yymm") if fin else None
    # ROE: 정식 값 우선, 없으면 PBR÷PER 역산 추정
    roe_used = r["roe"] if r["roe"] is not None else r.get("roe_pct")
    r["roe_used"] = roe_used
    r["roe_source"] = "KIS" if r["roe"] is not None else ("추정" if r.get("roe_pct") is not None else "")
    r["oneoff_suspect"] = bool(roe_used is not None and roe_used > ROE_SUSPECT_PCT)
    if fin:
        sc = composite_score(r["per"], r["pbr"], fin)
        r["score"], r["score_parts"] = sc["score"], sc["parts"]
        r["score_coverage"], r["score_notes"] = sc["coverage"], sc["notes"]
    else:
        r["score"], r["score_parts"], r["score_coverage"], r["score_notes"] = None, {}, 0.0, []


def build_output(rows: list, meta: dict, fin_map: dict | None = None) -> dict:
    """파싱된 전 종목 데이터(rows)에서 저평가 목록과 시장 통계를 만든다. (순수 함수 — 테스트 가능)
    fin_map: {종목코드: fetch_financial_ratio 결과}. 없으면 예전처럼 PBR÷PER 역산 ROE로만 동작한다."""
    eligible, use_mktcap, use_liquidity = _eligible(rows)
    fin_map = fin_map or {}
    for r in eligible:
        attach_fundamentals(r, fin_map.get(r["code"]))

    stat_counts = {}
    for r in rows:
        stat_counts[r["stat_code"]] = stat_counts.get(r["stat_code"], 0) + 1

    low_per_all = sorted(
        [r for r in eligible if r["per"] is not None and r["pbr"] is not None
         and 0 < r["per"] <= LOW_PER_MAX and 0 < r["pbr"] <= LOW_PER_PBR_MAX],
        key=lambda r: (r["oneoff_suspect"], r["per"]))
    low_pbr_all = sorted(
        [r for r in eligible if r["per"] is not None and r["pbr"] is not None
         and 0 < r["pbr"] <= LOW_PBR_MAX and r["per"] > 0],
        key=lambda r: (r["oneoff_suspect"], r["pbr"]))

    # 균형형: 각 상한 대비 비율의 합이 작은 순 (PER·PBR을 똑같이 중요하게 본다)
    # PER이 PER_CAUTION_BELOW 미만인 종목은 이익 지속성이 가장 의심스러운 극단값이라 목록 뒤로 보낸다
    # (점수가 가장 낮은, 즉 가장 극단적인 종목이 맨 위에 오는 것을 막기 위함. 표시는 그대로 남는다)
    # 정렬: PER<caution은 뒤로 → 종합점수 높은 순(점수 없는 종목은 맨 뒤) → 동점이면 PER·PBR 상한 대비 합이 작은 순
    balanced_all = sorted(
        [r for r in eligible if r["per"] is not None and r["pbr"] is not None and r["roe_used"] is not None
         and 0 < r["per"] <= BAL_PER_MAX and 0 < r["pbr"] <= BAL_PBR_MAX
         and BAL_ROE_MIN <= r["roe_used"] <= BAL_ROE_MAX
         and (BAL_MIN_SCORE is None or (r["score"] is not None and r["score"] >= BAL_MIN_SCORE))],
        key=lambda r: (r["per"] < PER_CAUTION_BELOW,
                       -(r["score"] if r["score"] is not None else -1),
                       r["per"] / BAL_PER_MAX + r["pbr"] / BAL_PBR_MAX))
    # 목록별 상위 N개만 저장하지만, 실제로 몇 개가 조건을 통과했는지는 따로 기록한다
    list_totals = {"balanced": len(balanced_all), "low_per": len(low_per_all), "low_pbr": len(low_pbr_all)}
    balanced, low_per, low_pbr = balanced_all[:BAL_TOP_N], low_per_all[:TOP_N], low_pbr_all[:TOP_N]

    positive_per = [r["per"] for r in eligible if r["per"] is not None and r["per"] > 0]
    pbr_values = [r["pbr"] for r in eligible if r["pbr"] is not None and r["pbr"] > 0]
    market_stats = {
        "eligible_count": len(eligible),
        "median_per": round(statistics.median(positive_per), 1) if positive_per else None,
        "median_pbr": round(statistics.median(pbr_values), 2) if pbr_values else None,
        "pbr_below_1_pct": round(100 * sum(1 for v in pbr_values if v < 1) / len(pbr_values), 1) if pbr_values else None,
        "loss_making_pct": round(100 * sum(1 for r in eligible if r["per"] is not None and r["per"] <= 0) / len(eligible), 1) if eligible else None,
    }

    return {
        **meta,
        "criteria": {
            "min_mktcap_eok": MIN_MKTCAP_EOK if use_mktcap else None,
            "min_tr_value_eok": MIN_TR_VALUE_EOK if use_liquidity else None,
            "low_per_max": LOW_PER_MAX, "low_per_pbr_max": LOW_PER_PBR_MAX, "low_pbr_max": LOW_PBR_MAX,
            "bal_per_max": BAL_PER_MAX, "bal_pbr_max": BAL_PBR_MAX,
            "bal_roe_min": BAL_ROE_MIN, "bal_roe_max": BAL_ROE_MAX,
            "per_caution_below": PER_CAUTION_BELOW, "top_n": TOP_N, "bal_top_n": BAL_TOP_N,
            "bal_min_score": BAL_MIN_SCORE, "score_weights": dict(SCORE_WEIGHTS),
            "mktcap_filter_applied": use_mktcap, "liquidity_filter_applied": use_liquidity,
        },
        "market_stats": market_stats,
        "list_totals": list_totals,
        "stat_code_counts": stat_counts,
        "balanced": balanced,
        "low_per": low_per,
        "low_pbr": low_pbr,
    }


def scan(codes_names: list, fetcher=fetch_price_detail, calls_per_sec=CALLS_PER_SEC,
         workers=WORKERS, deadline_sec=DEADLINE_SEC):
    """상장사 전체를 훑어 (파싱된 행 리스트, 실패 종목 수, 원본 샘플, 중단 여부)를 반환."""
    limiter = RateLimiter(calls_per_sec)
    deadline = time.monotonic() + deadline_sec
    rows, failed, raw_sample, timed_out = [], 0, {}, False
    lock = threading.Lock()
    done = {"n": 0}

    def work(item):
        code, name = item
        if time.monotonic() > deadline:
            return "timeout", code, name, None
        limiter.wait()
        out = fetcher(code)
        return "ok" if out is not None else "fail", code, name, out

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for status, code, name, out in pool.map(work, codes_names):
            with lock:
                done["n"] += 1
                if done["n"] % 300 == 0:
                    print(f"  진행 {done['n']}/{len(codes_names)}")
            if status == "timeout":
                timed_out = True
                continue
            if status == "fail":
                failed += 1
                continue
            row = parse_row(code, name, out)
            if row is None:
                record_fail("NO_PRICE", "응답에 현재가가 없음")
                failed += 1
                continue
            if not raw_sample:
                raw_sample = out
            rows.append(row)
    return rows, failed, raw_sample, timed_out


def scan_financials(codes: list, fetcher=fetch_financial_ratio, calls_per_sec=CALLS_PER_SEC,
                    workers=WORKERS, deadline_sec=FIN_DEADLINE_SEC):
    """후보 풀 종목의 재무비율을 조회해 (fin_map, 실패 수, 중단 여부, 원본 샘플)을 반환."""
    limiter = RateLimiter(calls_per_sec)
    deadline = time.monotonic() + deadline_sec
    fin_map, failed, timed_out, raw_sample = {}, 0, False, {}

    def work(code):
        if time.monotonic() > deadline:
            return "timeout", code, None
        limiter.wait()
        fin = fetcher(code)
        return ("ok" if fin else "fail"), code, fin

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for status, code, fin in pool.map(work, codes):
            if status == "timeout":
                timed_out = True
            elif status == "fail":
                failed += 1
            else:
                if not raw_sample and fin.get("raw"):
                    raw_sample = fin["raw"]
                fin_map[code] = strip_raw(fin)
    return fin_map, failed, timed_out, raw_sample


def run(universe: dict | None = None, fetcher=fetch_price_detail, out_path: str = OUT_PATH,
        fin_fetcher=None, **scan_kwargs):
    started = time.monotonic()
    common.FAIL_REASONS.clear()
    universe = universe if universe is not None else load_dart_name_map()
    if not universe:
        raise SystemExit("DART 상장사 목록을 불러오지 못했습니다 (DART_API_KEY / 네트워크 확인)")

    codes_names = [
        (code, name) for code, name in sorted(universe.items())
        if not is_fund_product(name) and not any(k in name for k in EXTRA_EXCLUDE_NAME_KEYWORDS)
    ]
    print(f"스캔 대상 {len(codes_names)}개 (DART 상장사 {len(universe)}개 중 펀드/리츠/스팩 제외)")

    if fetcher is fetch_price_detail:
        get_access_token()   # 스레드 시작 전에 토큰을 한 번만 발급

    rows, failed, raw_sample, timed_out = scan(codes_names, fetcher=fetcher, **scan_kwargs)
    if not rows:
        raise SystemExit("한 종목도 조회하지 못했습니다 (KIS 키/네트워크 확인)")

    # 재무비율은 PER/PBR 1차 필터를 통과한 후보 풀에만 조회한다.
    # fin_fetcher를 따로 주지 않았을 때는 진짜 KIS 조회기를 쓰되, 테스트용 가짜 fetcher로 돌릴 땐 건너뛴다.
    if fin_fetcher is None and fetcher is fetch_price_detail:
        fin_fetcher = fetch_financial_ratio
    fin_map, fin_failed, fin_timed_out, fin_raw_sample = {}, 0, False, {}
    pool_codes = select_fin_pool(rows) if fin_fetcher else []
    if pool_codes:
        print(f"재무비율 조회 대상 {len(pool_codes)}개 (PER/PBR 1차 필터 통과 후보 풀)")
        fin_map, fin_failed, fin_timed_out, fin_raw_sample = scan_financials(pool_codes, fetcher=fin_fetcher)
        print(f"재무비율 조회 성공 {len(fin_map)} / 실패 {fin_failed}" + (" (시간 초과로 일부 생략)" if fin_timed_out else ""))

    meta = {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "universe_size": len(codes_names),
        "scanned_ok": len(rows),
        "scanned_failed": failed,
        "partial": timed_out,
        "fin_pool_size": len(pool_codes),
        "fin_ok": len(fin_map),
        "fin_failed": fin_failed,
        "fin_partial": fin_timed_out,
        "fail_reasons": dict(common.FAIL_REASONS),
        "elapsed_sec": round(time.monotonic() - started),
        "raw_sample": raw_sample,
        "fin_raw_sample": fin_raw_sample,
    }
    result = build_output(rows, meta, fin_map)

    # 상위 후보에만 DART 공시 리스크를 붙인다 (전 종목에 하면 호출이 너무 많음)
    lists = ("balanced", "low_per", "low_pbr")
    checked = {}
    for key in lists:
        for r in result[key][:RISK_CHECK_N]:
            if r["code"] not in checked:
                checked[r["code"]] = check_disclosure_risk(r["code"])
    for key in lists:
        for r in result[key]:
            r["risky_disclosures"] = checked.get(r["code"])   # 조회하지 않은 종목은 None(미확인)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


if __name__ == "__main__":
    result = run()
    ms = result["market_stats"]
    print(f"저장 완료: {OUT_PATH}")
    print(f"조회 성공 {result['scanned_ok']} / 실패 {result['scanned_failed']} "
          f"(소요 {result['elapsed_sec']}초, 중단여부 {result['partial']})")
    print(f"필터 통과 {ms['eligible_count']}종목 · 시장 PER 중앙값 {ms['median_per']} · PBR<1 비중 {ms['pbr_below_1_pct']}%")
    lt = result["list_totals"]
    print(f"조건 통과 종목 수 — 균형형 {lt['balanced']} / 저PER 우량 {lt['low_per']} / 저PBR 자산가치 {lt['low_pbr']} (저장은 균형형 상위 {BAL_TOP_N}개, 나머지 목록은 상위 {TOP_N}개까지)")
    print(f"적용된 필터: {result['criteria']}")
    print(f"종목상태코드 분포: {result['stat_code_counts']}")
    print(f"실패 사유: {result['fail_reasons']}")
    print(f"재무비율 — 후보 풀 {result['fin_pool_size']} / 성공 {result['fin_ok']} / 실패 {result['fin_failed']}"
          f" (중단여부 {result['fin_partial']})")
    scored = [r for r in result['balanced'] if r.get('score') is not None]
    print(f"균형형 {len(result['balanced'])}개 중 종합점수 산출 {len(scored)}개"
          + (f", 최고 {max(r['score'] for r in scored):.0f}점" if scored else ""))
    n_sus = sum(1 for r in result['low_per'] if r['oneoff_suspect'])
    print(f"저PER 목록 중 일회성 이익 의심(ROE>{ROE_SUSPECT_PCT:.0f}%) {n_sus}개")
