#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

_BLANK = {"", "なし", "na", "n/a", "-", "—", "無し", "null", "none"}


def load_env(path: str) -> dict:
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _to_jst(iso_utc: str) -> str:
    try:
        s = iso_utc.rstrip("Z")
        fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in s else "%Y-%m-%dT%H:%M:%S"
        dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        return (dt + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M JST")
    except Exception:
        return iso_utc


def build_message(user: dict, event_key: str, event_name: str) -> str:
    ts = user.get("createdAt") or user.get("updatedAt") or ""
    last = user.get("lastName") or ""
    first = user.get("firstName") or ""
    name = (last + " " + first).strip() or user.get("email") or "(名前なし)"
    email = user.get("email") or "(メールなし)"

    department = user.get("department") or ""
    position = user.get("position") or ""
    department = "" if department.strip().lower() in _BLANK else department
    position = "" if position.strip().lower() in _BLANK else position

    affiliation = user.get("affiliation") or ""
    org = affiliation
    if department or position:
        org += f"（{' / '.join(p for p in [department, position] if p)}）"

    lines = ["🎫 *EventHub チケット購入*", "",
             f"• イベント: {event_name or event_key}",
             f"• 参加者:   {name}（{email}）"]
    if org:
        lines.append(f"• 所属:     {org}")
    lines.append(f"• 登録日時: {_to_jst(ts)}")
    return "\n".join(lines)


def send_slack(token: str, channel: str, message: str, blocks: list | None = None) -> bool:
    body: dict = {"channel": channel, "text": message}
    if blocks:
        body["blocks"] = blocks
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        print(f"Slack 送信エラー: {e}", file=sys.stderr)
        return False
    if result.get("ok"):
        print(f"Slack 送信完了: ts={result.get('ts')}")
        return True
    print(f"Slack 送信失敗: {result.get('error')}", file=sys.stderr)
    return False
