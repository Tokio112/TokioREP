"""
Простое персистентное хранилище на JSON-файле.
Каждый пользователь бота хранится по своему chat_id.
"""

import json
import os
from threading import Lock

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
_lock = Lock()


def _load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def get_user(chat_id) -> dict | None:
    with _lock:
        return _load()["users"].get(str(chat_id))


def save_user(chat_id, user_data: dict) -> None:
    with _lock:
        data = _load()
        data["users"][str(chat_id)] = user_data
        _save(data)


def update_user(chat_id, **kwargs) -> dict:
    with _lock:
        data = _load()
        user = data["users"].setdefault(str(chat_id), {})
        user.update(kwargs)
        _save(data)
        return user


def all_users() -> dict:
    with _lock:
        return _load()["users"]


def delete_user(chat_id) -> None:
    with _lock:
        data = _load()
        data["users"].pop(str(chat_id), None)
        _save(data)


def add_plan(chat_id, date_str: str, text: str) -> None:
    with _lock:
        data = _load()
        user = data["users"].setdefault(str(chat_id), {})
        plans = user.setdefault("plans", {})
        plans.setdefault(date_str, []).append(text)
        _save(data)


def get_plans(chat_id, date_str: str) -> list:
    user = get_user(chat_id) or {}
    return user.get("plans", {}).get(date_str, [])


def clear_plans(chat_id, date_str: str) -> None:
    with _lock:
        data = _load()
        user = data["users"].get(str(chat_id))
        if user and date_str in user.get("plans", {}):
            del user["plans"][date_str]
            _save(data)
