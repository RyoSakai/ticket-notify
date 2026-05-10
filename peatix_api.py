#!/usr/bin/env python3
"""Peatix クライアント。
- get_view_data(): 認証不要。チケット別 seatsSold/price を取得（日次集計用）
- PeatixClient: 2ステップログイン + /saleses JSON API（新着通知用）
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar


class PeatixAPIError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


class PeatixAuthError(PeatixAPIError):
    pass


# ── 認証不要 ──────────────────────────────────────────────────

def get_view_data(event_slug: str) -> dict:
    """認証不要。https://{slug}.peatix.com/get_view_data を取得して返す。

    レスポンス例:
      json_data.event.tickets[n]: {id, name, price, seatsSold, seatsMax}
      json_data.event.seatsSold: 全体の販売数
    """
    url = f"https://{event_slug}.peatix.com/get_view_data"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise PeatixAPIError(e.code, e.read().decode(errors="replace"))


# ── 認証あり（2ステップログイン + /saleses JSON API）───────────

class PeatixClient:
    """2ステップ認証でセッション Cookie を取得し /saleses JSON API を叩く。

    /saleses レスポンス（json_data.saleses[n]）の主要フィールド:
      id           : int   注文 ID
      buyer_name   : str   購入者名（カタカナ）
      created      : str   作成日時 UTC "YYYY-MM-DD HH:MM:SS +0000"
      is_paid      : int   1=支払済み
      is_canceled  : int   1=キャンセル済み
      amount_paid  : int   支払金額（JPY）
      attendances  : list  [{ticket_name, ticket_price, quantity, ...}]
    """

    _SIGNIN_PAGE = "https://peatix.com/signin"
    _SIGNIN_POST = "https://peatix.com/user/signin"
    _UA          = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

    def __init__(self, email: str, password: str):
        self.email    = email
        self.password = password
        self._jar     = CookieJar()
        self._opener  = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._logged_in = False

    # ── 内部ヘルパー ─────────────────────────────────────────

    def _fetch(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        extra_headers: dict | None = None,
    ) -> tuple[int, str]:
        headers = {"User-Agent": self._UA}
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=30) as resp:
                return resp.status, resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")

    @staticmethod
    def _extract_form_token(html: str) -> str:
        for pat in (
            r'name="form_token"\s+value="([^"]+)"',
            r'value="([^"]+)"\s+name="form_token"',
        ):
            m = re.search(pat, html)
            if m:
                return m.group(1)
        raise PeatixAuthError(0, "form_token が見つかりません（ログインページ構造変化の可能性）")

    # ── 2ステップログイン ─────────────────────────────────────

    def _login(self) -> None:
        """2ステップログインでセッション Cookie を取得する。

        Step 1: GET /signin → form_token 取得
        Step 2: POST /user/signin (username のみ) → パスワードフォーム + 新 form_token
        Step 3: POST /user/signin (password) → JSON {"json_data": {"redirect_to": "..."}}
        """
        post_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer":      self._SIGNIN_PAGE,
            "Origin":       "https://peatix.com",
        }

        # Step 1: CSRF トークン取得
        _, html1 = self._fetch("GET", self._SIGNIN_PAGE)
        token1   = self._extract_form_token(html1)

        # Step 2: email 送信 → パスワードフォーム取得
        _, html2 = self._fetch(
            "POST", self._SIGNIN_POST,
            data=urllib.parse.urlencode({"form_token": token1, "username": self.email}).encode(),
            extra_headers=post_headers,
        )
        if "password" not in html2:
            raise PeatixAuthError(0, "パスワードフォームが表示されませんでした（メールアドレスが未登録の可能性）")
        token2 = self._extract_form_token(html2)

        # Step 3: パスワード送信 → HTTP 200 + JSON {"json_data": {"redirect_to": "..."}}
        _, body3 = self._fetch(
            "POST", self._SIGNIN_POST,
            data=urllib.parse.urlencode({
                "username":        self.email,
                "form_token":      token2,
                "new_signin_flow": "1",
                "password":        self.password,
                "signin_Peatix":   "Agree and sign in",
            }).encode(),
            extra_headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer":      self._SIGNIN_POST,
                "Origin":       "https://peatix.com",
            },
        )
        try:
            resp_json = json.loads(body3)
            redirect  = resp_json.get("json_data", {}).get("redirect_to", "")
            if not redirect or "signin" in redirect:
                raise PeatixAuthError(401, f"ログイン失敗（パスワードが間違っている可能性）: {body3[:200]}")
        except json.JSONDecodeError:
            raise PeatixAuthError(401, f"ログイン失敗（パスワードエラーの可能性）: {body3[:200]}")

        self._logged_in = True

    def ensure_session(self) -> None:
        if not self._logged_in:
            self._login()

    # ── 注文リスト取得 ────────────────────────────────────────

    def get_attendees(self, event_id: str) -> list[dict]:
        """GET /event/{id}/saleses で注文リストを全件返す（ページネーション対応）。

        返却: json_data.saleses の各要素をそのまま返す。
        主要フィールド: id, buyer_name, created, is_paid, is_canceled, amount_paid, attendances
        """
        self.ensure_session()
        all_sales: list[dict] = []
        page = 1

        while True:
            url = f"https://peatix.com/event/{event_id}/saleses?d=descend&page={page}"
            status, body = self._fetch("GET", url, extra_headers={"Accept": "application/json"})

            if status in (401, 403):
                self._logged_in = False
                self.ensure_session()
                status, body = self._fetch("GET", url, extra_headers={"Accept": "application/json"})

            if status not in (200, 201):
                raise PeatixAPIError(status, body[:300])

            try:
                data = json.loads(body).get("json_data", {})
            except json.JSONDecodeError:
                raise PeatixAPIError(status, f"JSON パース失敗: {body[:300]}")

            saleses  = data.get("saleses", [])
            page_max = int(data.get("page_max", 1))
            all_sales.extend(saleses)

            if page >= page_max:
                break
            page += 1

        return all_sales
