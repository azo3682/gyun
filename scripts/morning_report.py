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

    lines.append("=== 오늘의 스윙 후보 (전환신호: VCP 눌림 후 거래량급증 — 백테스트로 검증된 유일한 신호) ===")
    candidates = [r for r in snapshot.get("buy_top10", []) if r.get("transition") is True]
    if not candidates:
        lines.append("(전환신호가 뜬 후보가 없습니다)")
    for c in candidates:
        checks_str = ", ".join(f"{k}:{'O' if v else 'X'}" for k, v in c["tech"].items() if k != "RSI값")
        lines.append(f"\n· {c['stock_name']}({c['stock_code']}) — 전환신호 ✅ (참고점수 {c['passed']}/{c['total']})")
        lines.append(f"  참고지표: {checks_str}")
        if c.get("risky_disclosures"):
            lines.append(f"  ⚠ 주의 공시: {'; '.join(c['risky_disclosures'])}")
        if c.get("news"):
            lines.append("  관련 뉴스:")
            for n in c["news"][:2]:
                lines.append(f"    - {n['title']} ({n['link']})")

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
