#!/usr/bin/env python3
"""EventHub + Peatix 日次進捗レポート。
チケット別の登録者数×金額を集計し Slack に送信する。

使い方:
  python3 daily_report.py          # 通常実行
  python3 daily_report.py --dry-run # Slack 送信せずメッセージ確認
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from eventhub_api import EventHubAPIError, EventHubClient, EventHubRateLimitError
from peatix_api import PeatixAPIError, PeatixAuthError, PeatixClient
from slack_notify import load_env, send_slack

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"

CATEGORIES = ["事業会社", "スタートアップ", "VC/CVC", "アドバイザー"]

_CAT_ENV_KEYS = {
    "全体":           "全体",
    "事業会社":       "事業会社",
    "スタートアップ": "スタートアップ",
    "VC/CVC":         "VC_CVC",
    "アドバイザー":   "アドバイザー",
}


# ── カテゴリ抽出 ──────────────────────────────────────────────

def extract_category(title: str) -> str:
    """EventHub チケットタイトルからカテゴリを抽出。
    "メインチケット｜VC/CVC_早割(2026/5/15まで)" → "VC/CVC"
    """
    if "｜" not in title:
        return "その他"
    part = title.split("｜", 1)[1]
    for sep in ("_", "("):
        if sep in part:
            part = part.split(sep)[0]
    return part.strip()




# ── 集計 ─────────────────────────────────────────────────────

def aggregate_by_ticket(client: EventHubClient, event_key: str) -> dict:
    tickets = client.get_tickets(event_key)
    cat_count: dict[str, int] = defaultdict(int)
    cat_amount: dict[str, int] = defaultdict(int)

    for ticket in tickets:
        tid = ticket["ticketId"]
        price = int(ticket.get("price", 0))
        cat = extract_category(ticket.get("title", ""))
        count = client.get_ticket_user_count(event_key, tid)
        cat_count[cat] += count
        cat_amount[cat] += count * price

    return _build_agg(cat_count, cat_amount)


def aggregate_peatix(client: PeatixClient, event_id: str) -> dict:
    """Peatix /saleses API（認証あり）からチケット別 count・amount を集計する。
    支払済み・未キャンセルのみ対象。
    1注文に複数 attendances がある場合は人数として個別カウントする。
    """
    attendees = client.get_attendees(event_id)
    paid = [s for s in attendees if s.get("is_paid") == 1 and s.get("is_canceled") == 0]

    cat_count: dict[str, int] = defaultdict(int)
    cat_amount: dict[str, int] = defaultdict(int)

    for sale in paid:
        attendances = sale.get("attendances") or []
        if not attendances:
            continue
        ticket_name = attendances[0].get("ticket_name", "")
        cat = extract_category(ticket_name)
        cat_count[cat] += len(attendances)
        cat_amount[cat] += int(sale.get("amount_paid") or 0)

    return _build_agg(cat_count, cat_amount)


def _build_agg(cat_count: dict, cat_amount: dict) -> dict:
    """count/amount の defaultdict → 標準集計 dict に変換する共通処理。"""
    total_count = sum(cat_count.values())
    total_amount = sum(cat_amount.values())
    result: dict[str, dict] = {"全体": {"count": total_count, "amount": total_amount}}
    for cat in CATEGORIES:
        result[cat] = {"count": cat_count.get(cat, 0), "amount": cat_amount.get(cat, 0)}
    for cat, cnt in cat_count.items():
        if cat not in CATEGORIES:
            result[cat] = {"count": cnt, "amount": cat_amount.get(cat, 0)}
    return result


def merge_agg(agg1: dict, agg2: dict) -> dict:
    """2つの集計（EventHub と Peatix）をカテゴリ別に合算する。"""
    all_cats = set(agg1) | set(agg2)
    return {
        cat: {
            "count":  agg1.get(cat, {}).get("count",  0) + agg2.get(cat, {}).get("count",  0),
            "amount": agg1.get(cat, {}).get("amount", 0) + agg2.get(cat, {}).get("amount", 0),
        }
        for cat in all_cats
    }


# ── 目標値 ────────────────────────────────────────────────────

def load_targets(env: dict) -> tuple[dict, dict]:
    def get_int(key: str) -> int | None:
        v = env.get(key, "").strip()
        return int(v) if v.isdigit() else None

    tc = {cat: get_int(f"TARGET_COUNT_{key}") for cat, key in _CAT_ENV_KEYS.items()}
    ta = {cat: get_int(f"TARGET_AMOUNT_{key}") for cat, key in _CAT_ENV_KEYS.items()}
    return tc, ta


# ── ビジュアル部品 ────────────────────────────────────────────

BAR_WIDTH = 16

CATEGORY_COLORS: dict[str, str] = {
    "全体":           "⬛",
    "事業会社":       "🟦",
    "スタートアップ": "🟪",
    "VC/CVC":         "🟥",
    "アドバイザー":   "🟨",
}


def _color(cat: str) -> str:
    return CATEGORY_COLORS.get(cat, "🔲")


def _bar(actual: int, target: int | None) -> str:
    if not target:
        return "─" * BAR_WIDTH
    filled = round(min(actual / target, 1.0) * BAR_WIDTH)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def _pct(actual: int, target: int | None) -> str:
    return f"{actual / target * 100:.1f}%" if target else "—"


def _diff_str(actual: int, target: int | None, unit: str) -> str:
    if not target:
        return ""
    diff = target - actual
    if diff > 0:
        return f"残り {diff:,}{unit}"
    return "✅ 達成！" if diff == 0 else f"超過 +{-diff:,}{unit}"


def _progress_line(label: str, actual: int, target: int | None, val_str: str, tgt_str: str, diff_unit: str, indent: bool) -> str:
    ind = "　　" if indent else ""
    b = "" if indent else "*"
    diff = f"  {_diff_str(actual, target, diff_unit)}" if target else ""
    return (
        f"{ind}{_color(label)} {b}{label}{b}\n"
        f"{ind}　`{_bar(actual, target)}`  "
        f"{val_str} / {tgt_str}  {_pct(actual, target)}{diff}"
    )


def _count_line(label: str, count: int, target: int | None, indent: bool = False) -> str:
    return _progress_line(label, count, target,
        f"{count:,}名", f"{target:,}名" if target else "—", "名", indent)


def _amount_line(label: str, amount: int, target: int | None, indent: bool = False) -> str:
    return _progress_line(label, amount, target,
        f"¥{amount:,}", f"¥{target:,}" if target else "—", "円", indent)


def _agg_to_blocks(agg: dict, targets_count: dict, targets_amount: dict, source_label: str) -> list:
    """1つの集計データ（EventHub or Peatix）を Block Kit ブロックに変換する。"""
    display_cats = ["全体"] + CATEGORIES
    ordered = [c for c in display_cats + [k for k in agg if k not in display_cats and agg[k]["count"] > 0] if c in agg]

    count_parts = [f"*👥 人数進捗*", ""]
    amount_parts = [f"*💴 金額進捗*", ""]
    for cat in ordered:
        ind = cat != "全体"
        count_parts  += [_count_line(cat,  agg[cat]["count"],  targets_count.get(cat),  indent=ind), ""]
        amount_parts += [_amount_line(cat, agg[cat]["amount"], targets_amount.get(cat), indent=ind), ""]

    return [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{source_label}*"}]},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(count_parts).rstrip()}},
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(amount_parts).rstrip()}},
    ]


# ── Block Kit メッセージ構築 ──────────────────────────────────

def build_daily_blocks(
    eh_agg: dict,
    targets_count: dict,
    targets_amount: dict,
    event_name: str,
    peatix_agg: dict | None = None,
) -> tuple[list, str]:
    """Slack Block Kit のブロックリストとフォールバックテキストを返す。
    Peatix データがある場合は EventHub と合算して1つのセクションで表示する。
    """
    today_jst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d")

    # EventHub + Peatix を合算（Peatix データがあれば）
    agg   = merge_agg(eh_agg, peatix_agg) if peatix_agg is not None else eh_agg
    label = "📌 EventHub + Peatix 合算" if peatix_agg is not None else "📌 EventHub"

    blocks: list = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 進捗レポート（{today_jst}）"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{event_name}*"}]},
        {"type": "divider"},
    ]
    blocks += _agg_to_blocks(agg, targets_count, targets_amount, label)

    return blocks, f"進捗レポート（{today_jst}）— {event_name}"


# ── メイン ───────────────────────────────────────────────────

def main() -> None:
    dry_run = "--dry-run" in sys.argv

    env = load_env(str(ENV_FILE))
    api_key             = os.environ.get("EVENTHUB_API_KEY")    or env.get("EVENTHUB_API_KEY")
    slack_token         = os.environ.get("SLACK_TOKEN")         or env.get("SLACK_TOKEN")
    event_keys_raw      = os.environ.get("EVENTHUB_EVENT_KEYS") or env.get("EVENTHUB_EVENT_KEYS", "")
    peatix_email        = os.environ.get("PEATIX_EMAIL")        or env.get("PEATIX_EMAIL")
    peatix_password     = os.environ.get("PEATIX_PASSWORD")     or env.get("PEATIX_PASSWORD")
    peatix_event_ids_raw = os.environ.get("PEATIX_EVENT_IDS")   or env.get("PEATIX_EVENT_IDS", "")
    channel             = os.environ.get("SLACK_CHANNEL")       or env.get("SLACK_CHANNEL", "#ryosakai_notify")

    missing = [k for k, v in [("EVENTHUB_API_KEY", api_key), ("SLACK_TOKEN", slack_token), ("EVENTHUB_EVENT_KEYS", event_keys_raw)] if not v]
    if missing:
        print(f"必須環境変数が未設定: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    targets_count, targets_amount = load_targets(env)
    client = EventHubClient(api_key)

    # Peatix クライアント（認証情報があれば作成）
    peatix_client = None
    peatix_event_ids = [i.strip() for i in peatix_event_ids_raw.split(",") if i.strip()]
    if peatix_email and peatix_password and peatix_event_ids:
        peatix_client = PeatixClient(peatix_email, peatix_password)

    for event_key in [k.strip() for k in event_keys_raw.split(",") if k.strip()]:
        print(f"[EventHub:{event_key}] チケット別集計中...")
        try:
            eh_agg = aggregate_by_ticket(client, event_key)
        except EventHubRateLimitError:
            print(f"[EventHub:{event_key}] レート制限 (429) — スキップ", file=sys.stderr)
            continue
        except EventHubAPIError as e:
            print(f"[EventHub:{event_key}] API エラー: {e}", file=sys.stderr)
            if e.status // 100 == 4:
                sys.exit(1)
            continue

        event_name = client.get_event_name(event_key) or event_key

        # Peatix 集計（最初の event_id を MAP 2026 として対応づける）
        peatix_agg = None
        if peatix_client:
            event_id = peatix_event_ids[0]
            print(f"[Peatix:{event_id}] チケット別集計中...")
            try:
                peatix_agg = aggregate_peatix(peatix_client, event_id)
            except (PeatixAPIError, PeatixAuthError) as e:
                print(f"[Peatix:{event_id}] API エラー: {e} — Peatix セクションをスキップ", file=sys.stderr)

        blocks, fallback = build_daily_blocks(eh_agg, targets_count, targets_amount, event_name, peatix_agg)

        if dry_run:
            print("[DRY-RUN] blocks:\n" + json.dumps(blocks, ensure_ascii=False, indent=2))
        elif send_slack(slack_token, channel, fallback, blocks=blocks):
            print(f"[{event_key}] Slack 送信完了")


if __name__ == "__main__":
    main()
