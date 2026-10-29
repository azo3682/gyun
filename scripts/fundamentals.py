# -*- coding: utf-8 -*-
"""
scripts/fundamentals.py
중장기 관점(삼박자: 가치·가격·정보 + 탑다운) 판단용 공용 모듈.
streamlit_app.py / value_screen.py / morning_report.py 가 함께 import 한다.
(streamlit·KIS에 의존하지 않는 순수 함수 위주 — 테스트하기 쉽게 하려는 설계)

※ 중요한 정직성 원칙
- 삼박자(가치+가격+정보)·탑다운이라는 '큰 틀'만 슈퍼개미 이정윤 씨의 방식에서 가져왔다.
  아래의 모든 숫자 임계값(부채비율, ROE, 이동평균 기준 등)은 이정윤 씨가 공개한 기준이 아니라
  일반적으로 쓰이는 재무분석 관행을 바탕으로 내가 정한 값이다. 백테스트로 검증된 값도 아니다.
- 그래서 화면에는 항상 '참고용·미검증'으로 표시한다.
- 정보(뉴스·공시)는 호재/악재를 자동 판정하지 않는다. 사건 종류 태그만 붙인다.
"""

import statistics
from datetime import datetime, timedelta

import requests

# =====================================================================
# 1. PER / PBR / ROE 구간 라벨 (streamlit_app / morning_report 공용)
# =====================================================================
# (상한값, 라벨, 점수 0~2) — 값이 상한 이하인 첫 구간을 택한다. 업종마다 기준이 다르다는 점은 감안해야 한다.
PER_TIERS = [(0, "🔴 나쁨(적자)", 0), (3, "🟡 주의(수치 왜곡 가능성)", 1),
             (15, "🟢 좋음(저평가권)", 2), (30, "⚪ 보통", 1),
             (60, "🔴 나쁨(고평가권)", 0), (float("inf"), "🔴 매우 나쁨(고평가)", 0)]
PBR_TIERS = [(0.6, "🟢 좋음(저평가권)", 2), (1.5, "⚪ 보통", 1),
             (3, "🔴 나쁨(고평가권)", 0), (float("inf"), "🔴 매우 나쁨(고평가)", 0)]
ROE_TIERS = [(0, "🔴 나쁨(적자)", 0), (5, "🔴 나쁨(수익성 낮음)", 0), (10, "⚪ 보통", 1),
             (20, "🟢 좋음", 2), (40, "🟢 매우 좋음", 2), (float("inf"), "🟡 주의(일회성 이익 의심)", 1)]


def _tier_lookup(value, tiers):
    for upper, label, score in tiers:
        if value <= upper:
            return label, score
    return tiers[-1][1], tiers[-1][2]


def per_label(per) -> str:
    return "" if per is None else _tier_lookup(per, PER_TIERS)[0]


def per_score(per):
    return None if per is None else _tier_lookup(per, PER_TIERS)[1]


def pbr_label(pbr) -> str:
    return "" if (pbr is None or pbr <= 0) else _tier_lookup(pbr, PBR_TIERS)[0]


def pbr_score(pbr):
    return None if (pbr is None or pbr <= 0) else _tier_lookup(pbr, PBR_TIERS)[1]


def compute_roe(per, pbr):
    """ROE(%) = PBR/PER*100 (같은 주가 기준이라 수식상 EPS/BPS와 같다)."""
    if per is None or pbr is None or per == 0:
        return None
    return pbr / per * 100


def roe_label(roe) -> str:
    return "" if roe is None else _tier_lookup(roe, ROE_TIERS)[0]


def roe_score(roe):
    return None if roe is None else _tier_lookup(roe, ROE_TIERS)[1]


def price_metric_score(per, pbr, roe):
    """PER·PBR·ROE 3개만으로 매긴 '가격지표 점수'(0~6). ROE=PBR/PER 이라 세 값이 독립적이지 않으므로
    '종합점수'라 부르지 않는다 — 가격 대비 싼지만 보는 점수일 뿐이다. 하나라도 계산 불가하면 (None, '—')."""
    scores = [per_score(per), pbr_score(pbr), roe_score(roe)]
    if any(s is None for s in scores):
        return None, "—"
    total = sum(scores)
    if total >= 5:
        return total, "🟢 가격지표 좋음"
    if total >= 3:
        return total, "⚪ 가격지표 보통"
    return total, "🔴 가격지표 나쁨"


# =====================================================================
# 2. 업종 상대평가 (금융·건설·해운 등은 원래 PBR이 낮아서 절대 기준만으로는 왜곡된다)
# =====================================================================
SECTOR_MIN_N = 5            # 업종 표본이 이보다 적으면 비교하지 않는다
REL_CHEAP_RATIO = 0.7       # 업종 중앙값의 70% 이하 → 업종 대비 저평가
REL_EXPENSIVE_RATIO = 1.3   # 130% 이상 → 업종 대비 고평가


def compute_sector_stats(rows: list, min_n: int = SECTOR_MIN_N) -> dict:
    """{업종명: {n, median_per, median_pbr}} — PER은 흑자(>0) 종목만, PBR은 >0 종목만 중앙값에 포함."""
    by_sector = {}
    for r in rows:
        s = r.get("sector") or ""
        if s:
            by_sector.setdefault(s, []).append(r)
    stats = {}
    for s, items in by_sector.items():
        if len(items) < min_n:
            continue
        pers = [x["per"] for x in items if x.get("per") is not None and x["per"] > 0]
        pbrs = [x["pbr"] for x in items if x.get("pbr") is not None and x["pbr"] > 0]
        stats[s] = {
            "n": len(items),
            "median_per": round(statistics.median(pers), 2) if len(pers) >= min_n else None,
            "median_pbr": round(statistics.median(pbrs), 2) if len(pbrs) >= min_n else None,
        }
    return stats


def relative_valuation(per, pbr, sector: str, sector_stats: dict | None):
    """업종 중앙값 대비 비율로 판정. 반환 {label, per_ratio, pbr_ratio, sector, n} 또는 None(비교 불가)."""
    st = (sector_stats or {}).get(sector or "")
    if not st:
        return None
    ratios = []
    per_ratio = pbr_ratio = None
    if per is not None and per > 0 and st.get("median_per"):
        per_ratio = round(per / st["median_per"], 2)
        ratios.append(per_ratio)
    if pbr is not None and pbr > 0 and st.get("median_pbr"):
        pbr_ratio = round(pbr / st["median_pbr"], 2)
        ratios.append(pbr_ratio)
    if not ratios:
        return None
    avg = sum(ratios) / len(ratios)
    if avg <= REL_CHEAP_RATIO:
        label = "🟢 업종 대비 저평가"
    elif avg >= REL_EXPENSIVE_RATIO:
        label = "🔴 업종 대비 고평가"
    else:
        label = "⚪ 업종 평균 수준"
    return {"label": label, "per_ratio": per_ratio, "pbr_ratio": pbr_ratio,
            "sector": sector, "n": st["n"], "avg_ratio": round(avg, 2)}


# =====================================================================
# 3. DART 주요 재무지표 (fnlttSinglIndx) — 가치 축
# =====================================================================
DART_INDX_URL = "https://opendart.fss.or.kr/api/fnlttSinglIndx.json"
IDX_CLASSES = {"profit": "M210000", "stability": "M220000", "growth": "M230000"}
# (연도 오프셋, 보고서코드) 최신순 시도 순서 — 오늘 날짜에 맞춰 build_period_candidates()가 만든다
REPRT_Q1, REPRT_HALF, REPRT_Q3, REPRT_ANNUAL = "11013", "11012", "11014", "11011"

# 지표 이름 매칭용 키워드 (공백·괄호 제거 후 부분 일치). 실제 응답 이름을 샘플 없이 만들었으므로
# 매칭 실패에 대비해 화면에 원본 응답을 펼쳐볼 수 있게 한다.
INDICATOR_KEYS = {
    "debt_ratio": ["부채비율"],
    "current_ratio": ["유동비율"],
    "roe": ["ROE", "자기자본이익률", "자기자본순이익률", "자기자본수익률"],
    "op_margin": ["영업이익률", "매출액영업이익률"],
    "net_margin": ["순이익률", "당기순이익률", "매출액순이익률"],
    "sales_growth": ["매출액증가율", "매출증가율"],
    "op_growth": ["영업이익증가율"],
    "ni_growth": ["순이익증가율", "당기순이익증가율"],
}


def build_period_candidates(today: datetime | None = None) -> list:
    """오늘 기준으로 공시되었을 가능성이 높은 순서대로 (사업연도, 보고서코드) 후보를 만든다."""
    today = today or datetime.now()
    y = today.year
    return [(y, REPRT_Q3), (y, REPRT_HALF), (y, REPRT_Q1),
            (y - 1, REPRT_ANNUAL), (y - 1, REPRT_Q3), (y - 1, REPRT_HALF)]


def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "") if ch not in " ()[]·,/-_").upper()


def _to_num(v):
    try:
        s = str(v).replace(",", "").replace("%", "").strip()
        return float(s) if s not in ("", "-") else None
    except (TypeError, ValueError):
        return None


def parse_indicators(items: list) -> dict:
    """DART 응답 list(dict) → {키: 값(원본 단위)}. 같은 키가 여러 번 나오면 먼저 나온 값을 쓴다."""
    out = {}
    for it in items or []:
        nm = _norm(it.get("idx_nm", ""))
        val = _to_num(it.get("idx_val"))
        if val is None:
            continue
        for key, kws in INDICATOR_KEYS.items():
            if key in out:
                continue
            if any(_norm(k) in nm for k in kws):
                # '영업이익률' 이 '영업이익증가율' 에 매칭되지 않도록 증가율 계열은 이름에 '증가' 가 있을 때만
                if key in ("op_margin", "net_margin", "roe", "debt_ratio", "current_ratio") and "증가" in nm:
                    continue
                # '총자산순이익률(ROA)' 이 순이익률로 잘못 잡히지 않게 (자기자본순이익률은 roe 가 먼저 가져감)
                if key == "net_margin" and "자산" in nm:
                    continue
                out[key] = val
                break
    return out


def detect_unit_scale(values: list) -> tuple:
    """(배율, 메모). 비율 지표들이 전부 작은 소수(예: 0.12)로 내려오면 퍼센트로 환산(×100).
    샘플 없이 만든 보정이라 확정이 아니며, 판단 근거는 화면의 원본 응답으로 사람이 재확인한다."""
    vals = [abs(v) for v in values if v is not None]
    if len(vals) >= 3 and max(vals) <= 5 and statistics.median(vals) <= 2:
        return 100.0, "값이 모두 작아 '비율(0.12=12%)'로 보고 ×100 환산함 — 원본 확인 필요"
    return 1.0, "퍼센트 단위로 해석"


def fetch_dart_indicators(api_key: str, corp_code: str, today: datetime | None = None,
                          session=None, timeout: int = 10) -> dict | None:
    """최신 공시 기준 주요 재무지표 {values, period, unit_note, raw}. 못 구하면 None.
    (XBRL 제출 기업·2023 3분기 이후 자료만 존재 — 비어 있으면 '재무데이터 부족'으로 처리)"""
    if not api_key or not corp_code:
        return None
    get = (session or requests).get

    def call(year, reprt, idx_cl):
        try:
            resp = get(DART_INDX_URL, params={"crtfc_key": api_key, "corp_code": corp_code,
                                              "bsns_year": str(year), "reprt_code": reprt,
                                              "idx_cl_code": idx_cl}, timeout=timeout)
            data = resp.json()
        except Exception:
            return None
        if data.get("status") != "000":
            return None
        return data.get("list") or None

    for year, reprt in build_period_candidates(today):
        first = call(year, reprt, IDX_CLASSES["profit"])
        if not first:
            continue
        raw = {"profit": first}
        for k in ("stability", "growth"):
            raw[k] = call(year, reprt, IDX_CLASSES[k]) or []
        merged = []
        for lst in raw.values():
            merged.extend(lst)
        parsed = parse_indicators(merged)
        if not parsed:
            return {"values": {}, "period": f"{year}/{reprt}", "unit_note": "매칭된 지표 없음", "raw": raw}
        pct_keys = ("roe", "op_margin", "net_margin", "sales_growth", "op_growth", "ni_growth")
        scale, note = detect_unit_scale([parsed.get(k) for k in pct_keys])
        if scale != 1.0:
            for k in pct_keys + ("debt_ratio", "current_ratio"):
                if k in parsed:
                    parsed[k] = round(parsed[k] * scale, 2)
        return {"values": parsed, "period": f"{year}/{reprt}", "unit_note": note, "raw": raw}
    return None


# --- 가치 축 판정 (임계값은 모두 내가 정한 일반 관행 기준) ---
DEBT_GOOD, DEBT_BAD = 100.0, 200.0
ROE_GOOD, ROE_BAD = 10.0, 5.0
OPM_GOOD = 5.0


def judge_value(ind: dict | None, per=None, pbr=None, rel: dict | None = None) -> dict:
    """가치 축. DART 지표가 없으면 판단 보류(level=None) — PER/PBR만으로 '가치 양호'를 말하지 않는다.
    반환 {level: good|ok|poor|None, score, reasons[], missing[]}"""
    vals = (ind or {}).get("values") or {}
    if not vals:
        return {"level": None, "score": None, "reasons": [], "missing": ["DART 재무지표"]}
    score, reasons, missing = 0, [], []

    d = vals.get("debt_ratio")
    if d is None:
        missing.append("부채비율")
    elif d <= DEBT_GOOD:
        score += 1; reasons.append(f"부채비율 {d:.0f}% (양호)")
    elif d >= DEBT_BAD:
        score -= 1; reasons.append(f"부채비율 {d:.0f}% (부담)")
    else:
        reasons.append(f"부채비율 {d:.0f}% (보통)")

    roe = vals.get("roe")
    if roe is None:
        roe = compute_roe(per, pbr)
    if roe is None:
        missing.append("ROE")
    elif roe >= ROE_GOOD:
        score += 1; reasons.append(f"ROE {roe:.1f}% (양호)")
    elif roe < ROE_BAD:
        score -= 1; reasons.append(f"ROE {roe:.1f}% (낮음)")
    else:
        reasons.append(f"ROE {roe:.1f}% (보통)")

    m = vals.get("op_margin")
    if m is None:
        missing.append("영업이익률")
    elif m <= 0:
        score -= 2; reasons.append(f"영업이익률 {m:.1f}% (영업적자)")
    elif m >= OPM_GOOD:
        score += 1; reasons.append(f"영업이익률 {m:.1f}% (양호)")
    else:
        reasons.append(f"영업이익률 {m:.1f}% (낮음)")

    # 성장성은 가중치 2 — 지금 시장에서는 성장주를 선호한다는 방향을 반영(내 판단)
    sg, og = vals.get("sales_growth"), vals.get("op_growth")
    if sg is None and og is None:
        missing.append("성장률")
    else:
        pos = [g > 0 for g in (sg, og) if g is not None]
        gtxt = ", ".join(f"{n} {g:+.1f}%" for n, g in (("매출", sg), ("영업이익", og)) if g is not None)
        if all(pos):
            score += 2; reasons.append(f"성장 {gtxt} (성장)")
        elif not any(pos):
            score -= 2; reasons.append(f"성장 {gtxt} (역성장)")
        else:
            reasons.append(f"성장 {gtxt} (엇갈림)")

    if rel:
        if rel["label"].startswith("🟢"):
            score += 1; reasons.append(f"{rel['label'][2:]} (업종 중앙값 대비 {rel['avg_ratio']:.2f}배)")
        elif rel["label"].startswith("🔴"):
            score -= 1; reasons.append(f"{rel['label'][2:]} (업종 중앙값 대비 {rel['avg_ratio']:.2f}배)")

    level = "good" if score >= 3 else ("poor" if score <= 0 else "ok")
    return {"level": level, "score": score, "reasons": reasons, "missing": missing}


# =====================================================================
# 4. 가격 축 — 장기 이동평균 (60일/120일)
# =====================================================================
def judge_price_axis(df) -> dict:
    """df: 'stck_clpr' 컬럼의 일봉(오래된 순). 120일선까지 계산되려면 약 140행 이상 필요.
    반환 {state: up|mixed|down|None, label, detail, partial}"""
    if df is None or len(df) < 80 or "stck_clpr" not in df:
        return {"state": None, "label": "❔ 장기추세 판단불가(데이터 부족)", "detail": "", "partial": True}
    close = df["stck_clpr"].astype(float)
    ma60 = close.rolling(60).mean()
    last = float(close.iloc[-1])
    ma60_now, ma60_prev = ma60.iloc[-1], ma60.iloc[-21]
    slope60 = ma60_now > ma60_prev
    if len(close) >= 140:
        ma120 = close.rolling(120).mean()
        ma120_now, ma120_prev = ma120.iloc[-1], ma120.iloc[-21]
        slope120 = ma120_now > ma120_prev
        above = last > ma120_now and ma60_now > ma120_now
        if above and slope120 and slope60:
            return {"state": "up", "label": "🟢 장기 상승추세", "partial": False,
                    "detail": "현재가>120일선, 60일선>120일선, 60·120일선 모두 상승"}
        if last < ma120_now and not slope120:
            return {"state": "down", "label": "🔴 장기 하락추세", "partial": False,
                    "detail": "현재가<120일선이고 120일선 하락"}
        return {"state": "mixed", "label": "🟡 장기 추세 불분명", "partial": False,
                "detail": "120일선 기준 상승·하락 조건을 모두 충족하지 않음"}
    # 120일치가 없으면 60일선만으로 (정확도 낮음)
    if last > ma60_now and slope60:
        return {"state": "up", "label": "🟢 중기 상승추세(60일선 기준)", "partial": True,
                "detail": "현재가>60일선, 60일선 상승 (120일선은 데이터 부족으로 미확인)"}
    if last < ma60_now and not slope60:
        return {"state": "down", "label": "🔴 중기 하락추세(60일선 기준)", "partial": True,
                "detail": "현재가<60일선, 60일선 하락 (120일선은 데이터 부족으로 미확인)"}
    return {"state": "mixed", "label": "🟡 중기 추세 불분명(60일선 기준)", "partial": True,
            "detail": "60일선 기준 조건 미충족 (120일선은 데이터 부족으로 미확인)"}


# =====================================================================
# 5. 정보 축 — 공시 '사건 종류' 태그만 (호재/악재 자동판정 안 함)
# =====================================================================
# (키워드, 태그, 위험여부) — 위험(True)이면 중장기 뱃지를 '부적합'으로 내린다
EVENT_RULES = [
    ("유상증자", "⚠️ 유상증자", True),
    ("감자", "⚠️ 감자", True),
    ("관리종목", "🚨 관리종목", True),
    ("상장폐지", "🚨 상장폐지", True),
    ("횡령", "🚨 횡령·배임", True),
    ("배임", "🚨 횡령·배임", True),
    ("자본잠식", "🚨 자본잠식", True),
    ("거래정지", "🚨 거래정지", True),
    ("불성실공시", "⚠️ 불성실공시", True),
    ("전환사채", "🟡 전환사채(CB)", False),
    ("신주인수권부사채", "🟡 신주인수권부사채(BW)", False),
    ("합병", "🔀 합병", False),
    ("분할", "🔀 분할·물적분할 검토", False),
    ("자기주식취득", "💡 자기주식 취득", False),
    ("자기주식처분", "🟡 자기주식 처분", False),
    ("배당", "💰 배당", False),
    ("단일판매", "📝 수주·공급계약", False),
    ("공급계약", "📝 수주·공급계약", False),
    ("최대주주", "👤 최대주주 변경", False),
    ("타법인주식", "🏢 타법인 주식 취득·처분", False),
]


def fetch_dart_titles(api_key: str, corp_code: str, days: int = 60, session=None, timeout: int = 10) -> list:
    """최근 days일 DART 공시 제목 목록 ['YYYYMMDD 제목', ...]. 실패하면 []."""
    if not api_key or not corp_code:
        return []
    get = (session or requests).get
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    try:
        data = get("https://opendart.fss.or.kr/api/list.json",
                   params={"crtfc_key": api_key, "corp_code": corp_code, "bgn_de": start,
                           "end_de": end, "page_no": 1, "page_count": 100}, timeout=timeout).json()
        if data.get("status") != "000":
            return []
        return [f"{it.get('rcept_dt', '')} {it.get('report_nm', '')}" for it in data.get("list", [])]
    except Exception:
        return []


def tag_events(titles: list) -> dict:
    """공시 제목들에서 사건 태그 추출. {tags[], risk_tags[], examples{tag: 제목}}"""
    tags, risk_tags, examples = [], [], {}
    for t in titles or []:
        for kw, tag, risky in EVENT_RULES:
            if kw in t:
                if tag not in tags:
                    tags.append(tag)
                    examples[tag] = t
                if risky and tag not in risk_tags:
                    risk_tags.append(tag)
    return {"tags": tags, "risk_tags": risk_tags, "examples": examples}


# =====================================================================
# 6. 중장기 뱃지 (가치 × 가격 × 리스크) — 단기 🎯 전환신호와 별개
# =====================================================================
BADGE_CAVEAT = "참고용·미검증 (임계값은 일반 관행 기준으로 직접 정한 값이며 백테스트로 검증되지 않았습니다)"


def mid_long_badge(value: dict, price: dict, event: dict | None = None, exchange_flags: list | None = None) -> str:
    risk = bool((event or {}).get("risk_tags")) or bool(exchange_flags)
    if risk:
        return "🚫 중장기 부적합 (리스크 이벤트)"
    lv = (value or {}).get("level")
    if lv is None:
        return "❔ 재무데이터 부족 (판단 보류)"
    ps = (price or {}).get("state")
    if lv == "poor":
        return "🚫 중장기 메리트 부족"
    if lv == "good" and ps == "up":
        return "🌱 중장기 메리트 있음"
    if lv == "good":
        return "👀 가치 양호 · 추세 확인 대기"
    if ps == "up":
        return "⚪ 보통 · 추세는 양호"
    return "⚪ 보통"


def build_mid_long(row: dict, sector_stats: dict | None, indicators: dict | None, df_long,
                   titles: list | None, exchange_flags: list | None = None) -> dict:
    """한 종목의 중장기 판단 묶음 (JSON 저장 가능한 dict). row는 value_screen의 parse_row 결과 형식."""
    per, pbr = row.get("per"), row.get("pbr")
    rel = relative_valuation(per, pbr, row.get("sector", ""), sector_stats)
    value = judge_value(indicators, per, pbr, rel)
    price = judge_price_axis(df_long)
    event = tag_events(titles)
    badge = mid_long_badge(value, price, event, exchange_flags)
    ind = indicators or {}
    return {
        "badge": badge, "caveat": BADGE_CAVEAT,
        "value": value, "price": price, "event": event, "relative": rel,
        "dart_period": ind.get("period"), "dart_unit_note": ind.get("unit_note"),
        "dart_values": ind.get("values"),
    }


# =====================================================================
# 7. 시장 국면 (탑다운 1단계) — 지수 종가 시계열로 판정
# =====================================================================
def classify_market_regime(closes) -> dict:
    """closes: 지수 종가 Series(오래된 순, 120개 이상 권장). 반환 {state, label, score, detail[], advice}
    규칙(내가 정한 것): 현재>60일선, 60일선 상승(20일 전 대비), 20일선>60일선, 현재>120일선 각 1점."""
    if closes is None or len(closes) < 80:
        return {"state": None, "label": "❔ 시장국면 판단불가(데이터 부족)", "score": None, "detail": [], "advice": ""}
    c = closes.astype(float).reset_index(drop=True)
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    last = float(c.iloc[-1])
    detail, score = [], 0
    checks = [
        (last > ma60.iloc[-1], "지수>60일선"),
        (ma60.iloc[-1] > ma60.iloc[-21], "60일선 상승"),
        (ma20.iloc[-1] > ma60.iloc[-1], "20일선>60일선"),
    ]
    total = 3
    if len(c) >= 120:
        checks.append((last > c.rolling(120).mean().iloc[-1], "지수>120일선"))
        total = 4
    for ok, txt in checks:
        score += int(bool(ok))
        detail.append(("✅ " if ok else "❌ ") + txt)
    hi = float(c.iloc[-min(len(c), 252):].max())
    dd = (last / hi - 1) * 100
    detail.append(f"고점 대비 {dd:+.1f}%")
    ratio = score / total
    if ratio >= 0.75:
        state, label = "bull", "🟢 상승 국면"
        advice = "시장 추세가 우호적입니다. 개별 종목 신호를 평소 기준대로 참고할 수 있습니다."
    elif ratio >= 0.5:
        state, label = "mixed", "🟡 혼조 국면"
        advice = "방향이 불분명합니다. 신규 매수 비중을 줄이고 손절선을 엄격히 지키는 쪽이 일반적 원칙입니다."
    else:
        state, label = "bear", "🔴 약세 국면"
        advice = "시장 추세가 약합니다. 신규 매수를 줄이고 현금 비중을 높이는 것을 검토하는 것이 일반적 원칙입니다."
    return {"state": state, "label": label, "score": f"{score}/{total}", "detail": detail,
            "advice": advice, "last": round(last, 2), "drawdown_pct": round(dd, 1)}


def fetch_index_closes(ticker: str, period: str = "1y"):
    """yfinance로 지수 종가 Series. 실패하면 None. (GitHub Actions 환경용 — 대시보드 서버에는 설치돼 있지 않다)"""
    try:
        import yfinance as yf
        df = yf.download(ticker, period=period, progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        col = df["Close"]
        if hasattr(col, "columns"):
            col = col.iloc[:, 0]
        return col.dropna()
    except Exception:
        return None


def market_regime_snapshot(fetch=fetch_index_closes) -> dict:
    """{kospi: 국면, kosdaq: 국면, overall_label, overall_advice}"""
    out = {}
    for key, tk in (("kospi", "^KS11"), ("kosdaq", "^KQ11")):
        out[key] = classify_market_regime(fetch(tk))
    states = [out[k]["state"] for k in ("kospi", "kosdaq") if out[k]["state"]]
    if not states:
        out["overall_label"], out["overall_advice"] = "❔ 시장국면 판단불가", ""
    elif "bear" in states:
        out["overall_label"] = "🔴 약세 국면 포함 — 신규 매수 주의"
        out["overall_advice"] = "코스피·코스닥 중 한쪽 이상이 약세입니다. 신규 진입은 비중을 줄이는 쪽을 권합니다."
    elif all(s == "bull" for s in states):
        out["overall_label"], out["overall_advice"] = "🟢 상승 국면", "시장 환경이 우호적입니다."
    else:
        out["overall_label"], out["overall_advice"] = "🟡 혼조 국면", "방향이 불분명합니다. 비중 조절을 권합니다."
    out["note"] = "지수 이동평균 기반의 규칙형 판정(내가 정한 기준, 미검증)"
    return out
