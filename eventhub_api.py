#!/usr/bin/env python3
"""EventHub API クライアント。stdlib のみ使用（pip 不要）。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = "https://api.eventhub.jp/v1"


class EventHubAPIError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


class EventHubRateLimitError(EventHubAPIError):
    pass


class EventHubClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{BASE_URL}{path}"
        data = json.dumps(body, ensure_ascii=False).encode() if body else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "X-API-KEY": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            if e.code == 429:
                raise EventHubRateLimitError(e.code, body_text)
            raise EventHubAPIError(e.code, body_text)

    def get_new_users(self, event_key: str, created_on_or_after: str) -> list[dict]:
        """createdOnOrAfter 以降の新規ユーザーを全件返す。
        実際のレスポンスフィールド: userId, email, lastName, firstName,
          affiliation, department, position, createdAt, updatedAt, registrationType
        """
        users = []
        offset = 0
        limit = 100
        while True:
            resp = self._request("POST", f"/users/{event_key}", {
                "createdOnOrAfter": created_on_or_after,
                "offset": offset,
                "limit": limit,
            })
            page = resp.get("users") or []
            users.extend(page)
            total = resp.get("userCount", 0)
            if offset + len(page) >= total or len(page) < limit:
                break
            offset += limit
        return users

    def get_tickets(self, event_key: str) -> list[dict]:
        """チケット一覧を返す。各チケットに ticketId, title, price, publishStatus。"""
        resp = self._request("GET", f"/tickets/{event_key}")
        return resp.get("tickets") or []

    def get_ticket_user_count(self, event_key: str, ticket_id: str) -> int:
        """チケット別の登録者数を返す（1リクエストで完結）。"""
        resp = self._request("POST", f"/users/{event_key}", {
            "ticketId": ticket_id,
            "offset": 0,
            "limit": 1,
        })
        return resp.get("userCount", 0)

    def get_event_name(self, event_key: str) -> str:
        """イベント名を返す。取得失敗時は空文字。"""
        try:
            resp = self._request("GET", f"/events/{event_key}")
            return resp.get("name") or resp.get("title") or resp.get("eventName") or ""
        except EventHubAPIError:
            return ""


# ── タイムスタンプ ユーティリティ ─────────────────────────────

def _parse_iso(iso: str) -> datetime:
    """ミリ秒あり/なし両方の ISO 8601 UTC 文字列をパースする。
    例: "2026-05-07T07:35:07.200Z" / "2026-05-07T07:35:07Z"
    """
    s = iso.rstrip("Z")
    if "." in s:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_seconds(iso: str, seconds: int) -> str:
    """ISO 8601 UTC 文字列に秒数を加算して返す（ミリ秒対応）。"""
    return (_parse_iso(iso) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def max_created_at(users: list[dict]) -> str | None:
    """ユーザーリストの最大 createdAt を返す。"""
    times = [u.get("createdAt", "") for u in users if u.get("createdAt")]
    return max(times) if times else None
