#!/usr/bin/env python3
"""Peatix 新規購入者ポーラー。
5分ごとに実行し、支払済みになった参加者を検知して Slack に通知する。

last_poll_time 方式: list_sales HTML の data-datetime（UTC）をカーソルとして
前回ポーリング以降に支払済みになった参加者だけ通知する。

使い方:
  python3 poll_peatix.py          # 通常実行
  python3 poll_peatix.py --dry-run # Slack 送信せずログのみ確認
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from peatix_api import PeatixAPIError, PeatixAuthError, PeatixClient
from slack_notify import load_env, send_slack

BASE_DIR   = Path(__file__).parent
ENV_FILE   = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / "state.json"

SLACK_RYO = "<@U08FS2E4B>"


# ── ユーティリティ ────────────────────────────────────────────

def parse_mapping(raw: str) -> dict[str, str]:
    """'key1:val1,key2:val2' 形式の文字列を dict に変換する。"""
    result = {}
    for item in raw.split(","):
        item = item.strip()
        if ":" in item:
            k, v = item.split(":", 1)
            result[k.strip()] = v.strip()
    return result


# ── 状態管理 ──────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            print("state.json 破損のため空状態で起動します", file=sys.stderr)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── 日時パース ────────────────────────────────────────────────

def parse_dt(dt_str: str) -> datetime | None:
    """Peatix data-datetime "2026-05-08 06:55:14 +0000" → UTC aware datetime。"""
    if not dt_str:
        return None
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


# ── Slack メッセージ構築 ──────────────────────────────────────

def build_message(sale: dict, event_name: str) -> str:
    name   = sale.get("buyer_name") or "(名前なし)"
    price  = sale.get("amount_paid") or 0
    dt     = parse_dt(sale.get("created", ""))
    dt_jst = (dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M JST") if dt else ""
    attendances = sale.get("attendances") or []
    ticket = attendances[0].get("ticket_name", "") if attendances else ""

    lines = [f"🎫 *{event_name} チケット購入（Peatix）* {SLACK_RYO}", ""]
    lines.append(f"• 参加者:   {name}")
    if ticket:
        lines.append(f"• チケット: {ticket}")
    if price:
        lines.append(f"• 金額:     ¥{int(price):,}")
    if dt_jst:
        lines.append(f"• 購入日時: {dt_jst}")
    return "\n".join(lines)


# ── イベント単体のポーリング ──────────────────────────────────

def poll_one_event(
    client: PeatixClient,
    state: dict,
    event_id: str,
    token: str,
    channel: str,
    event_name: str,
    dry_run: bool,
) -> None:
    state_key = f"peatix_{event_id}"
    entry     = state.get(state_key, {})
    is_first  = state_key not in state
    last_str  = entry.get("last_poll_time", "")
    last_dt   = datetime.fromisoformat(last_str.replace("Z", "+00:00")) if last_str else None

    print(f"[Peatix:{event_id}] ポーリング開始（last_poll_time: {last_str or '(初回)'}）")

    try:
        attendees = client.get_attendees(event_id)
    except PeatixAuthError as e:
        print(f"[Peatix:{event_id}] 認証エラー: {e}", file=sys.stderr)
        sys.exit(1)
    except PeatixAPIError as e:
        print(f"[Peatix:{event_id}] API エラー: {e} — cursor 更新せずスキップ", file=sys.stderr)
        return

    # 支払済み・未キャンセルのみ通知対象
    paid = [s for s in attendees if s.get("is_paid") == 1 and s.get("is_canceled") == 0]
    print(f"[Peatix:{event_id}] 全注文: {len(attendees)}件, 支払済み: {len(paid)}件")

    # 最新 created を cursor 更新用に収集（全注文対象）
    all_dts = [parse_dt(s["created"]) for s in attendees if s.get("created")]
    max_dt  = max((d for d in all_dts if d), default=None)

    if is_first:
        # 初回: 現在の最新日時を cursor として登録、通知なし
        new_cursor = (
            (max_dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if max_dt else
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        state[state_key] = {"last_poll_time": new_cursor}
        print(f"[Peatix:{event_id}] 初回起動 — cursor を {new_cursor} に設定。今回は通知なし。")
        return

    # last_dt 以降に作成された支払済み注文を通知
    new_paid = sorted(
        [s for s in paid
         if (dt := parse_dt(s.get("created", ""))) and last_dt and dt > last_dt],
        key=lambda s: s.get("created", ""),
    )
    print(f"[Peatix:{event_id}] 新規購入者: {len(new_paid)}件")

    for s in new_paid:
        msg = build_message(s, event_name)
        print(f"  通知: {s.get('buyer_name') or '(不明)'} @ {s.get('created', '')}")
        if dry_run:
            print("[DRY-RUN] Slack 送信スキップ:\n" + msg)
        else:
            send_slack(token, channel, msg)

    # cursor を更新（max_dt + 1s）
    if max_dt:
        new_cursor = (max_dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if new_cursor != last_str:
            state[state_key] = {"last_poll_time": new_cursor}
            print(f"[Peatix:{event_id}] cursor 更新: {last_str} → {new_cursor}")

    print(f"[Peatix:{event_id}] 完了")


# ── メイン ───────────────────────────────────────────────────

def main() -> None:
    dry_run = "--dry-run" in sys.argv

    env               = load_env(str(ENV_FILE))
    email             = os.environ.get("PEATIX_EMAIL")          or env.get("PEATIX_EMAIL")
    password          = os.environ.get("PEATIX_PASSWORD")       or env.get("PEATIX_PASSWORD")
    event_ids_raw     = os.environ.get("PEATIX_EVENT_IDS")      or env.get("PEATIX_EVENT_IDS", "")
    slack_token       = os.environ.get("SLACK_TOKEN")           or env.get("SLACK_TOKEN")
    default_channel   = os.environ.get("SLACK_CHANNEL")         or env.get("SLACK_CHANNEL", "#ryosakai_notify")
    channels_raw      = os.environ.get("PEATIX_EVENT_CHANNELS") or env.get("PEATIX_EVENT_CHANNELS", "")
    names_raw         = os.environ.get("PEATIX_EVENT_NAMES")    or env.get("PEATIX_EVENT_NAMES", "")

    missing = [k for k, v in [
        ("PEATIX_EMAIL",     email),
        ("PEATIX_PASSWORD",  password),
        ("PEATIX_EVENT_IDS", event_ids_raw),
        ("SLACK_TOKEN",      slack_token),
    ] if not v]
    if missing:
        print(f"必須環境変数が未設定: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    channel_map = parse_mapping(channels_raw)   # {event_id: channel}
    name_map    = parse_mapping(names_raw)       # {event_id: event_name}
    event_ids   = [i.strip() for i in event_ids_raw.split(",") if i.strip()]
    client      = PeatixClient(email, password)
    state       = load_state()

    for event_id in event_ids:
        channel    = channel_map.get(event_id, default_channel)
        event_name = name_map.get(event_id, f"イベント {event_id}")
        poll_one_event(client, state, event_id, slack_token, channel, event_name, dry_run)

    save_state(state)
    print("state.json 保存完了")


if __name__ == "__main__":
    main()
