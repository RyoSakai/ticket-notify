#!/usr/bin/env python3
"""
EventHub チケット購入ポーラー。
5分ごとに実行し、新規購入者を検知して Slack に通知する。

通知フィールド: イベント名, 参加者名, 所属, メール, チケット種別, 金額, 注文番号, 購入日時

使い方:
  python3 poll_eventhub.py          # 通常実行
  python3 poll_eventhub.py --dry-run # Slack 送信せずログだけ確認
"""
import json
import os
import sys
from pathlib import Path

from eventhub_api import (
    EventHubAPIError,
    EventHubClient,
    EventHubRateLimitError,
    add_seconds,
    max_created_at,
    now_utc_iso,
)
from slack_notify import build_message, load_env, send_slack

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
STATE_FILE = BASE_DIR / "state.json"


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


# ── イベント単体のポーリング ──────────────────────────────────

def poll_one_event(
    client: EventHubClient,
    state: dict,
    event_key: str,
    token: str,
    channel: str,
    dry_run: bool,
) -> None:
    cursor = state.get(event_key, {}).get("last_poll_time")

    if cursor is None:
        baseline = now_utc_iso()
        state[event_key] = {"last_poll_time": baseline}
        print(f"[{event_key}] 初回起動 — ベースライン設定: {baseline}。今回は通知なし。")
        return

    print(f"[{event_key}] ポーリング開始 (cursor={cursor})")

    try:
        users = client.get_new_users(event_key, cursor)
    except EventHubRateLimitError:
        print(f"[{event_key}] レート制限 (429) — cursor 更新せず次サイクルで再試行", file=sys.stderr)
        return
    except EventHubAPIError as e:
        if e.status // 100 == 4:
            print(f"[{event_key}] 認証エラー (HTTP {e.status}) — API キーまたは eventKey を確認してください", file=sys.stderr)
            sys.exit(1)
        print(f"[{event_key}] API エラー: {e} — cursor 更新せず次サイクルで再試行", file=sys.stderr)
        return

    print(f"[{event_key}] 新規登録: {len(users)}件")

    if users:
        # イベント名を取得（失敗時は event_key をそのまま表示）
        event_name = client.get_event_name(event_key)

        # 購入日時昇順に並べて通知（古い順に Slack へ流す）
        sorted_users = sorted(
            users,
            key=lambda u: u.get("createdAt") or u.get("purchasedAt") or "",
        )
        for user in sorted_users:
            msg = build_message(user, event_key, event_name)
            print(f"  通知: {user.get('name') or user.get('email') or '(不明)'}")

            if dry_run:
                print("  [DRY-RUN] Slack 送信スキップ\n" + msg)
            else:
                send_slack(token, channel, msg)

        # カーソルを max(createdAt) + 1秒 に進める
        latest = max_created_at(users)
        if latest:
            state[event_key]["last_poll_time"] = add_seconds(latest, 1)
    else:
        # 新規なし: カーソルを現在時刻に更新してギャップ縮小
        state[event_key]["last_poll_time"] = add_seconds(now_utc_iso(), -10)

    print(f"[{event_key}] 完了 → 次の cursor={state[event_key]['last_poll_time']}")


# ── メイン ───────────────────────────────────────────────────

def main() -> None:
    dry_run = "--dry-run" in sys.argv

    env = load_env(str(ENV_FILE))
    # 環境変数が設定されていれば優先（LaunchAgent 経由での起動時など）
    api_key = os.environ.get("EVENTHUB_API_KEY") or env.get("EVENTHUB_API_KEY")
    slack_token = os.environ.get("SLACK_TOKEN") or env.get("SLACK_TOKEN")
    event_keys_raw = os.environ.get("EVENTHUB_EVENT_KEYS") or env.get("EVENTHUB_EVENT_KEYS", "")
    channel = os.environ.get("SLACK_CHANNEL") or env.get("SLACK_CHANNEL", "#ryosakai_notify")

    missing = [k for k, v in [
        ("EVENTHUB_API_KEY", api_key),
        ("SLACK_TOKEN", slack_token),
        ("EVENTHUB_EVENT_KEYS", event_keys_raw),
    ] if not v]
    if missing:
        print(f"必須環境変数が未設定です: {', '.join(missing)}\n.env ファイルを確認してください。", file=sys.stderr)
        sys.exit(1)

    event_keys = [k.strip() for k in event_keys_raw.split(",") if k.strip()]
    client = EventHubClient(api_key)
    state = load_state()

    for event_key in event_keys:
        poll_one_event(client, state, event_key, slack_token, channel, dry_run)

    save_state(state)
    print("state.json 保存完了")


if __name__ == "__main__":
    main()
