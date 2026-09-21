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
CANDIDATE_SCORE_THRESHOLD = 3

EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS", "")
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")


def fetch_global_indices() -> str:
    indices = {"다우존스": "^DJI", "나스닥": "^IXIC", "S&P500": "^GSPC"}
    lines = []
    for name, ticker in indices.items():
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if len(hist) < 2:
                lines.append(f"- {name}: 데이터 없음")
                continue
            prev, last = hist["Close"].iloc[-2], hist["Close"].iloc[-1]
            pct = (last / prev - 1) * 100
            lines.append(f"- {name}: {last:,.2f} ({pct:+.2f}%)")
        except Exception:
            lines.append(f"- {name}: 조회 실패")
    return "\n".join(lines)


def build_report_body() -> str:
    if not os.path.exists(SNAPSHOT_PATH):
        return "전일 마감 스냅샷 파일이 없습니다. eod_snapshot.py가 정상 실행됐는지 확인이 필요합니다."

    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    lines = [f"[{datetime.now(KST).strftime('%Y-%m-%d')}] 아침 스윙 후보 리포트",
              f"(기준: 전일 {snapshot.get('date', '?')} 마감 데이터)", ""]

    lines.append("=== 밤사이 해외증시 마감 시황 ===")
    lines.append(fetch_global_indices())
    lines.append("")

    lines.append(f"=== 오늘의 스윙 후보 (기술적 점수 {CANDIDATE_SCORE_THRESHOLD}/5 이상) ===")
    candidates = [r for r in snapshot.get("buy_top10", []) if r.get("passed", 0) >= CANDIDATE_SCORE_THRESHOLD]
    if not candidates:
        lines.append("(조건을 충족한 후보가 없습니다)")
    for c in candidates:
        checks_str = ", ".join(f"{k}:{'O' if v else 'X'}" for k, v in c["tech"].items() if k != "RSI값")
        lines.append(f"\n· {c['stock_name']}({c['stock_code']}) — {c['passed']}/{c['total']}점")
        lines.append(f"  {checks_str}")
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
