# -*- coding: utf-8 -*-
"""
scripts/delete_signals.py
앱의 '📌 신호 추적' 탭에서 지우기를 요청하면 GitHub Actions('Delete signals' 워크플로)가 실행하는 스크립트.

지우는 대상: data/signal_tracker.json(전환신호·거래량수급 동시)과 data/top10_baseline.json(대조군)의 기록.
건드리지 않는 것: 매매 일지(별도 비공개 저장소), eod_snapshot.json, history CSV, AI 리포트 파일.

동작
  1. 입력(환경변수)을 엄격히 검증한다 — 종목코드 6자리, 날짜 YYYY-MM-DD, 종류 3가지 중 선택.
  2. 조건에 맞는 기록을 찾는다. 종목코드·날짜 중 하나는 반드시 있어야 하고, 둘 다 없으면 실행하지 않는다.
  3. 지운 기록 전체를 data/deleted_archive.json에 보관한다 (복구용).
  4. 지운 (종목코드, 신호일, 종류)를 data/deleted_signals.json에 적어 둔다 — EOD 실행이 같은 조합을 다시 기록하지 않게 하는 '삭제 목록'.
     이미 있던 그 건만 막고, 같은 종목의 앞으로의 신호는 막지 않는다.

입력 환경변수: DEL_CODE(선택), DEL_DATE(선택), DEL_TYPES(쉼표 구분, 비우면 전체), DEL_DRY_RUN(true면 미리보기만)
"""

import json
import os
import re
import sys
from datetime import datetime

import signal_tracker as st_

KST = st_.KST
BASE = os.path.join(os.path.dirname(__file__), "..", "data")
ARCHIVE_PATH = os.path.join(BASE, "deleted_archive.json")
ALL_TYPES = (*st_.SIGNAL_TYPES.keys(), st_.BASELINE_TYPE)      # transition, volume_supply, top10
MAX_DELETE = 300                                               # 한 번에 이보다 많이 지우려 하면 중단한다 (실수 방지)

CODE_RE = re.compile(r"[0-9A-Z]{6}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class InputError(ValueError):
    pass


def validate_inputs(code: str, date: str, types: str):
    """검증된 (code, date, types튜플)을 반환한다. 잘못되면 InputError. 쉘에 넘기지 않고 파이썬 안에서만 쓴다."""
    code, date, types = (code or "").strip(), (date or "").strip(), (types or "").strip()
    if code and not CODE_RE.fullmatch(code):
        raise InputError(f"종목코드 형식이 올바르지 않습니다: {code[:20]!r}")
    if date:
        if not DATE_RE.fullmatch(date):
            raise InputError(f"날짜 형식이 올바르지 않습니다(YYYY-MM-DD): {date[:20]!r}")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise InputError(f"존재하지 않는 날짜입니다: {date}")
    if not code and not date:
        raise InputError("종목코드와 날짜 중 최소 하나는 필요합니다 (전체 삭제는 허용하지 않습니다).")
    if types:
        chosen = tuple(dict.fromkeys(t.strip() for t in types.split(",") if t.strip()))
        bad = [t for t in chosen if t not in ALL_TYPES]
        if bad or not chosen:
            raise InputError(f"알 수 없는 종류: {bad[:3]} (가능: {', '.join(ALL_TYPES)})")
    else:
        chosen = ALL_TYPES
    return code, date, chosen


def matches(entry: dict, code: str, date: str, types: tuple) -> bool:
    return ((not code or entry.get("code") == code) and (not date or entry.get("signal_date") == date)
            and entry.get("type") in types)


def _load_json_list(path: str, key: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = json.load(f).get(key)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _save_json(path: str, data: dict):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def delete_signals(code: str, date: str, types: tuple, dry_run: bool = False, tracker_path: str = st_.TRACKER_PATH,
                   baseline_path: str = st_.BASELINE_PATH, deleted_path: str = st_.DELETED_PATH,
                   archive_path: str = ARCHIVE_PATH, now: datetime | None = None) -> dict:
    """조건에 맞는 기록을 지우고 요약을 반환한다. dry_run이면 아무 파일도 바꾸지 않는다."""
    now = now or datetime.now(KST)
    tracker = st_.load_tracker(tracker_path)
    base = st_.load_tracker(baseline_path, types={st_.BASELINE_TYPE: "순매수 상위 10"}, legacy_split=False)
    hit_t = [s for s in tracker["signals"] if matches(s, code, date, types)]
    hit_b = [s for s in base["signals"] if matches(s, code, date, types)]
    found = hit_t + hit_b
    summary = {"matched": len(found), "from_tracker": len(hit_t), "from_baseline": len(hit_b), "dry_run": dry_run,
               "items": [{"code": s["code"], "name": s.get("name"), "date": s["signal_date"], "type": s["type"]} for s in found]}
    if len(found) > MAX_DELETE:
        raise InputError(f"{len(found)}건이 걸렸습니다. 한 번에 {MAX_DELETE}건까지만 지울 수 있습니다 — 조건을 좁혀 주세요.")
    if dry_run or not found:
        return summary

    ids_t, ids_b = {id(s) for s in hit_t}, {id(s) for s in hit_b}
    tracker["signals"] = [s for s in tracker["signals"] if id(s) not in ids_t]
    base["signals"] = [s for s in base["signals"] if id(s) not in ids_b]

    stamp = now.isoformat(timespec="seconds")
    archive = _load_json_list(archive_path, "archived")
    archive.extend({"deleted_at": stamp, "entry": s} for s in found)                    # 지운 기록을 통째로 보관
    tombs = _load_json_list(deleted_path, "deleted")
    have = {(d.get("code"), d.get("date"), d.get("type")) for d in tombs if isinstance(d, dict)}
    for s in found:
        key = (s["code"], s["signal_date"], s["type"])
        if key not in have:
            tombs.append({"code": s["code"], "name": s.get("name"), "date": s["signal_date"], "type": s["type"], "deleted_at": stamp})
            have.add(key)

    if hit_t:
        st_.save_tracker(tracker, tracker_path)
    if hit_b:
        st_.save_tracker(base, baseline_path)
    _save_json(archive_path, {"archived": archive, "updated_at": stamp})
    _save_json(deleted_path, {"deleted": tombs, "updated_at": stamp})
    return summary


def main():
    try:
        code, date, types = validate_inputs(os.environ.get("DEL_CODE", ""), os.environ.get("DEL_DATE", ""), os.environ.get("DEL_TYPES", ""))
        dry = os.environ.get("DEL_DRY_RUN", "").strip().lower() == "true"
        res = delete_signals(code, date, types, dry_run=dry)
    except InputError as e:
        print(f"요청 거부: {e}")
        sys.exit(1)
    label = "미리보기(변경 없음)" if res["dry_run"] else "삭제 완료"
    print(f"{label}: 조건 code={code or '(전체)'} date={date or '(전체)'} types={','.join(types)}")
    print(f"해당 {res['matched']}건 (신호 추적 {res['from_tracker']} / 대조군 {res['from_baseline']})")
    for it in res["items"][:50]:
        print(f"  - {it['date']} {it['code']} {it['name']} [{it['type']}]")
    if res["matched"] == 0:
        print("조건에 맞는 기록이 없습니다. 아무것도 바꾸지 않았습니다.")


if __name__ == "__main__":
    main()
