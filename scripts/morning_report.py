# -*- coding: utf-8 -*-
"""
scripts/morning_report.py
평일 08:00 KST에 GitHub Actions로 실행.
전일 15:40에 저장된 eod_snapshot.json(오늘의 스윙 후보)을 읽고,
밤사이 다우/나스닥/S&P500 마감 시황을 더해 이메일로 발송한다.
"""

import json
import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import yfinance as yf
from common import KST

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "eod_snapshot.json")
VALUE_SCREEN_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "value_screen.json")

EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")


def fetch_index(ticker: str):
    """(마지막값, 등락률%) 반환. 실패하면 (None, None)."""
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if len(hist) < 2:
            return None, None
        prev, last = hist["Close"].iloc[-2], hist["Close"].iloc[-1]
        return float(last), float((last / prev - 1) * 100)
    except Exception:
        return None, None


def describe_move(pct):
    if pct is None:
        return "데이터 없음"
    if pct >= 1.0:
        return "강세"
    if pct >= 0.2:
        return "상승"
    if pct > -0.2:
        return "보합"
    if pct > -1.0:
        return "하락"
    return "약세"


def build_market_briefing() -> str:
    """수치 목록 + 규칙 기반 한 줄 해설로 구성된 시황 브리핑."""
    kospi_last, kospi_pct = fetch_index("^KS11")
    kosdaq_last, kosdaq_pct = fetch_index("^KQ11")
    dow_last, dow_pct = fetch_index("^DJI")
    nasdaq_last, nasdaq_pct = fetch_index("^IXIC")
    sp500_last, sp500_pct = fetch_index("^GSPC")
    fx_last, fx_pct = fetch_index("KRW=X")       # 원달러 환율
    oil_last, oil_pct = fetch_index("CL=F")       # WTI 유가

    lines = ["[국내 지수 — 전일 마감]"]
    lines.append(f"- 코스피: {kospi_last:,.2f} ({kospi_pct:+.2f}%)" if kospi_last else "- 코스피: 조회 실패")
    lines.append(f"- 코스닥: {kosdaq_last:,.2f} ({kosdaq_pct:+.2f}%)" if kosdaq_last else "- 코스닥: 조회 실패")

    lines.append("\n[밤사이 해외 시황]")
    lines.append(f"- 다우존스: {dow_last:,.2f} ({dow_pct:+.2f}%)" if dow_last else "- 다우존스: 조회 실패")
    lines.append(f"- 나스닥: {nasdaq_last:,.2f} ({nasdaq_pct:+.2f}%)" if nasdaq_last else "- 나스닥: 조회 실패")
    lines.append(f"- S&P500: {sp500_last:,.2f} ({sp500_pct:+.2f}%)" if sp500_last else "- S&P500: 조회 실패")

    lines.append("\n[환율 / 유가]")
    lines.append(f"- 원달러 환율: {fx_last:,.1f}원 ({fx_pct:+.2f}%)" if fx_last else "- 원달러 환율: 조회 실패")
    lines.append(f"- WTI 유가: ${oil_last:,.2f} ({oil_pct:+.2f}%)" if oil_last else "- WTI 유가: 조회 실패")

    # 규칙 기반 한 줄 해설 (수치만으로 구성, 뉴스 텍스트 재구성 아님)
    lines.append("\n[한 줄 요약]")
    kospi_desc = describe_move(kospi_pct)
    us_descs = [d for d in [describe_move(dow_pct), describe_move(nasdaq_pct), describe_move(sp500_pct)]]
    us_tone = "동반 상승" if all(d in ("상승", "강세") for d in us_descs) else \
              "동반 하락" if all(d in ("하락", "약세") for d in us_descs) else "혼조"
    summary = f"코스피는 전일 {kospi_desc} 마감했고, 밤사이 미국 3대 지수는 {us_tone}세로 마감했습니다."
    if oil_last and fx_last:
        summary += f" 국제유가는 배럴당 ${oil_last:,.2f}, 원달러 환율은 {fx_last:,.1f}원 수준입니다."
    lines.append(summary)
    lines.append("(참고: 위 해설은 수치 변화만으로 기계적으로 생성된 요약이며, 뉴스 기반 해설이 아닙니다.)")

    return "\n".join(lines)


# 2026-09-29 추가: 숫자만 봐서는 좋은지 나쁜지 바로 판단하기 어렵다는 피드백으로
# PER/PBR 옆에 붙이는 직관 라벨. 절대적인 '싸다/비싸다' 판정이 아니라 구간 분류일
# 뿐이며, 업종마다 기준이 다르다는 점(금융·건설·해운은 원래 PBR이 낮음)은 그대로 감안할 것.
def per_label(per) -> str:
    if per is None:
        return ""
    if per <= 0:
        return "🔴 나쁨(적자)"
    if per <= 3:
        return "🟡 주의(수치 왜곡 가능성)"
    if per <= 15:
        return "🟢 좋음(저평가권)"
    if per <= 30:
        return "⚪ 보통"
    if per <= 60:
        return "🔴 나쁨(고평가권)"
    return "🔴 매우 나쁨(고평가)"


def pbr_label(pbr) -> str:
    if pbr is None or pbr <= 0:
        return ""
    if pbr <= 0.6:
        return "🟢 좋음(저평가권)"
    if pbr <= 1.5:
        return "⚪ 보통"
    if pbr <= 3:
        return "🔴 나쁨(고평가권)"
    return "🔴 매우 나쁨(고평가)"


def fmt_valuation(v) -> str:
    """{'per','pbr'} -> ' · PER 8.0 🟢 좋음(저평가권) · PBR 0.90 ⚪ 보통' (없으면 빈 문자열)"""
    if not v or v.get("per") is None or v.get("pbr") is None:
        return ""
    per_val = v["per"]
    per_str = f"{per_val:.1f} {per_label(per_val)}".strip() if per_val > 0 else "적자 🔴 나쁨(적자)"
    pbr_str = f"{v['pbr']:.2f} {pbr_label(v['pbr'])}".strip()
    return f" · PER {per_str} · PBR {pbr_str}"


def fmt_value_row(r: dict, caution_below: float = 3.0) -> str:
    parts = [f"PER {r['per']:.1f} {per_label(r['per'])}".strip(), f"PBR {r['pbr']:.2f} {pbr_label(r['pbr'])}".strip()]
    if r.get("roe_pct") is not None:
        parts.append(f"ROE(추정) {r['roe_pct']:.0f}%")
    if r.get("mktcap_eok") is not None:
        parts.append(f"시총 {r['mktcap_eok']:,.0f}억")
    if r.get("drawdown_pct") is not None:
        parts.append(f"52주고점대비 {r['drawdown_pct']:+.0f}%")
    line = f"· {r['name']}({r['code']}) — " + " · ".join(parts)
    if r.get("oneoff_suspect"):
        line += "  ⚠ 일회성 이익 의심"
    if r.get("per") is not None and 0 < r["per"] < caution_below:
        line += f"  ⚠ PER {caution_below:g} 미만(이익 지속성 확인)"
    if r.get("risky_disclosures"):
        line += f"  ⚠ 공시: {'; '.join(r['risky_disclosures'])}"
    return line


def build_value_screen_section() -> list:
    """전체 시장 저평가 스캔(value_screen.py) 결과를 이메일용 텍스트로. 없거나 오래됐으면 안내만."""
    if not os.path.exists(VALUE_SCREEN_PATH):
        return ["(저평가 스캔 결과 파일이 아직 없습니다 — Value Screen 워크플로가 한 번도 안 돌았을 수 있습니다)"]
    try:
        with open(VALUE_SCREEN_PATH, "r", encoding="utf-8") as f:
            vs = json.load(f)
        age_days = (datetime.now(KST).date() - datetime.fromisoformat(vs["generated_at"]).date()).days
    except Exception as e:
        return [f"(저평가 스캔 결과를 읽지 못했습니다: {e})"]
    if age_days > 4:
        return [f"(저평가 스캔 결과가 {age_days}일 전 것이라 생략합니다 — 워크플로 상태를 확인하세요)"]

    ms, cr = vs.get("market_stats", {}), vs.get("criteria", {})
    lines = [f"스캔 {vs.get('scanned_ok')}종목 성공 / 필터 통과 {ms.get('eligible_count')}종목 · "
             f"시장 PER 중앙값 {ms.get('median_per')} · PBR 1 미만 비중 {ms.get('pbr_below_1_pct')}%"]
    if vs.get("partial"):
        lines.append("⚠ 시간 초과로 일부 종목만 스캔된 결과입니다.")
    filt = []
    if cr.get("mktcap_filter_applied"):
        filt.append(f"시총 {cr.get('min_mktcap_eok'):.0f}억↑")
    if cr.get("liquidity_filter_applied"):
        filt.append(f"전일 거래대금 {cr.get('min_tr_value_eok')}억↑")
    lines.append(f"(기준: 흑자 종목, {', '.join(filt) if filt else '시총·거래대금 필터 미적용'}, 관리/경고/정지 종목 제외)")

    totals = vs.get("list_totals", {})
    caution = cr.get("per_caution_below", 3.0)

    def total_note(key: str, shown: int) -> str:
        """'통과 N종목 중 상위 K' 문구 (통과 수를 모르는 옛 데이터면 '상위 K'만)."""
        return f"조건 통과 {totals[key]}종목 중 상위 {shown}" if key in totals else f"상위 {shown}"

    if "balanced" in vs:
        lines.append(f"\n[⭐ 균형형 — PER ≤ {cr.get('bal_per_max'):g} & PBR ≤ {cr.get('bal_pbr_max'):g} & "
                     f"ROE(추정) {cr.get('bal_roe_min'):g}~{cr.get('bal_roe_max'):g}%, {total_note('balanced', 10)} — 처음 볼 때 권하는 목록, PER 3 미만은 뒤로]")
        lines += [fmt_value_row(r, caution) for r in vs["balanced"][:10]] or ["(해당 종목 없음)"]
    lines.append(f"\n[저PER 우량 — PER ≤ {cr.get('low_per_max')} & PBR ≤ {cr.get('low_per_pbr_max')}, {total_note('low_per', 5)}]")
    lines += [fmt_value_row(r, caution) for r in vs.get("low_per", [])[:5]] or ["(해당 종목 없음)"]
    lines.append(f"\n[저PBR 자산가치 — PBR ≤ {cr.get('low_pbr_max')} & 흑자, {total_note('low_pbr', 5)}]")
    lines += [fmt_value_row(r, caution) for r in vs.get("low_pbr", [])[:5]] or ["(해당 종목 없음)"]
    lines.append("\n※ PER은 최근 확정된 연간 EPS 기준이고, 이 스크린은 매수 신호로 검증된 게 아니라 관심 종목 후보 풀입니다. "
                 "싸 보이는 데는 이유(실적 악화, 지배구조 등)가 있는 경우가 많으니 공시·뉴스를 꼭 확인하세요.")
    return lines


def build_report_body() -> str:
    if not os.path.exists(SNAPSHOT_PATH):
        return "전일 마감 스냅샷 파일이 없습니다. eod_snapshot.py가 정상 실행됐는지 확인이 필요합니다."

    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    lines = [f"[{datetime.now(KST).strftime('%Y-%m-%d')}] 아침 스윙 후보 리포트",
              f"(기준: 전일 {snapshot.get('date', '?')} 마감 데이터)", ""]

    lines.append("=== 시황 브리핑 ===")
    lines.append(build_market_briefing())
    lines.append("")

    lines.append("=== 오늘의 스윙 후보 (전환신호: VCP 눌림 후 거래량급증+상승 — 2026-09-29 방향 조건 추가로 "
                 "9/21 백테스트 재검증 전, 참고용) ===")
    candidates = [r for r in snapshot.get("buy_top10", []) if r.get("transition") is True]
    if not candidates:
        lines.append("(전환신호가 뜬 후보가 없습니다)")
    for c in candidates:
        checks_str = ", ".join(f"{k}:{'O' if v else 'X'}" for k, v in c["tech"].items() if k != "RSI값")
        day_pct = c.get("day_pct")
        if day_pct is not None and day_pct >= 0.07:
            day_pct_str = f" (당일 {day_pct*100:+.2f}% ⚠️이미 급등, 추격 주의)"
        elif day_pct is not None:
            day_pct_str = f" (당일 {day_pct*100:+.2f}%)"
        else:
            day_pct_str = ""
        lines.append(f"\n· {c['stock_name']}({c['stock_code']}) — 전환신호 ✅{day_pct_str} (참고점수 {c['passed']}/{c['total']}){fmt_valuation(c.get('valuation'))}")
        lines.append(f"  참고지표: {checks_str}")
        if c.get("risky_disclosures"):
            lines.append(f"  ⚠ 주의 공시: {'; '.join(c['risky_disclosures'])}")
        if c.get("news"):
            lines.append("  관련 뉴스:")
            for n in c["news"][:2]:
                lines.append(f"    - {n['title']} ({n['link']})")

    value_overlap = snapshot.get("value_overlap", [])
    if value_overlap:
        lines.append("\n=== 🎯💰 전환신호 + 저평가(PER ≤ 15 & PBR ≤ 1.5) 동시 충족 ===")
        for v in value_overlap:
            lines.append(f"· {v['stock_name']}({v['stock_code']}) — PER {v['per']:.1f}배 · PBR {v['pbr']:.2f}배")

    lines.append("\n=== 💰 전체 시장 저평가 스캔 (전 종목 PER/PBR 자동 조회) ===")
    lines += build_value_screen_section()

    lines.append("\n\n※ 이 리포트는 투자 자문이 아니며, 참고용 스크리닝 결과입니다.")
    lines.append("※ 장중 조건 변화(제외/신규 후보)는 대시보드에서 실시간으로 확인하세요.")
    return "\n".join(lines)


def send_email(body: str):
    if not (EMAIL_ADDRESS and EMAIL_APP_PASSWORD and EMAIL_TO):
        print("이메일 설정(Secrets)이 없어 발송을 건너뜁니다.")
        print(body)
        return

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = f"[스윙 후보 리포트] {datetime.now(KST).strftime('%Y-%m-%d')}"
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_TO

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        server.send_message(msg)
    print("이메일 발송 완료")


if __name__ == "__main__":
    body = build_report_body()
    send_email(body)
