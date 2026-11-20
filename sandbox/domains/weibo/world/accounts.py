"""账户 fixture 加载。

账户是“当前授权用户”的内容：本人资料、本人发的微博、别人 @ 他的微博、他微博下的评论、点赞记录。
"""
from __future__ import annotations

import json
from pathlib import Path

ACCOUNT_DIR = Path(__file__).resolve().parent.parent / "accounts"
DEFAULT_ACCOUNT = "alice"


class AccountError(ValueError):
    pass


def list_accounts() -> list[str]:
    return sorted(p.stem for p in ACCOUNT_DIR.glob("*.json"))


def load_account(account_id: str) -> dict:
    if account_id not in list_accounts():
        raise AccountError(f"账户不存在: {account_id}")
    return json.loads((ACCOUNT_DIR / f"{account_id}.json").read_text(encoding="utf-8"))
