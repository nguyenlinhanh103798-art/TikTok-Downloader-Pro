
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 1

ENTRY_FIELDS = frozenset({
    "play_url",
    "images",
    "username",
    "video_id",
    "create_time",
    "profile_uid",
    "content_type",
})

# Khóa dùng chung giữa các luồng tải
CACHE_LOCK = threading.RLock()

# Mỗi video có một khóa riêng để tránh xử lý trùng
_ITEM_LOCKS: dict[str, threading.RLock] = {}
_ITEM_LOCKS_GUARD = threading.Lock()


def get_item_lock(video_id: str) -> threading.RLock:
    key = str(video_id)

    with _ITEM_LOCKS_GUARD:
        lock = _ITEM_LOCKS.get(key)

        if lock is None:
            lock = threading.RLock()
            _ITEM_LOCKS[key] = lock

        return lock


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _is_entry(value: Any) -> bool:
    if not isinstance(value, dict):
        return False

    images = value.get("images") or []

    return bool(
        value.get("play_url")
        or (
            isinstance(images, list)
            and len(images) > 0
        )
    )


def _normalize_entry(entry: dict) -> dict:
    out = {
        key: value
        for key, value in entry.items()
        if key in ENTRY_FIELDS
    }

    out.setdefault("content_type", "video")
    return out


def _parse_raw(data: Any) -> dict[str, dict]:
    if not isinstance(data, dict):
        return {}

    if "schema_version" in data and "entries" in data:
        entries = data.get("entries") or {}

        if isinstance(entries, dict):
            return {
                str(key): _normalize_entry(value)
                for key, value in entries.items()
                if _is_entry(value)
            }

        return {}

    # Hỗ trợ cache định dạng cũ
    if any(
        _is_entry(value)
        for value in data.values()
        if isinstance(value, dict)
    ):
        return {
            str(key): _normalize_entry(value)
            for key, value in data.items()
            if isinstance(value, dict) and _is_entry(value)
        }

    return {}


def load_cache(path: str) -> dict[str, dict]:
    if not os.path.isfile(path):
        return {}

    try:
        with CACHE_LOCK:
            with open(path, "r", encoding="utf-8") as file:
                return _parse_raw(json.load(file))
    except Exception as error:
        print(f"Warning: could not load cache: {error}")
        return {}


def save_cache(entries: dict[str, dict], path: str) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)

    # Tạo bản chụp dữ liệu trong lúc khóa cache
    with CACHE_LOCK:
        snapshot = {
            str(key): _normalize_entry(value)
            for key, value in entries.items()
            if isinstance(value, dict)
        }

        wrapper = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _utc_now_iso(),
            "app": "ez-tiktok-downloader",
            "entries": snapshot,
        }

        temp_path = None

        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=".cache_",
                suffix=".tmp",
                dir=folder,
            )

            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(
                    wrapper,
                    file,
                    indent=2,
                    ensure_ascii=False,
                )
                file.flush()
                os.fsync(file.fileno())

            # Thay file cũ bằng file mới sau khi ghi hoàn tất
            os.replace(temp_path, path)
            temp_path = None

        except Exception as error:
            print(f"Warning: could not save cache: {error}")

        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


def merge_entries(
    base: dict[str, dict],
    incoming: dict[str, dict],
) -> dict[str, dict]:

    with CACHE_LOCK:
        merged = dict(base)

        for video_id, entry in incoming.items():
            if not isinstance(entry, dict):
                continue

            new = _normalize_entry(entry)
            old = merged.get(video_id)

            if old is None:
                merged[video_id] = new
                continue

            old = _normalize_entry(old)

            if not old.get("play_url") and new.get("play_url"):
                merged[video_id] = new

            elif not old.get("images") and new.get("images"):
                merged[video_id] = new

        return merged


def export_cache_file(
    entries: dict[str, dict],
    out_path: str,
) -> None:

    save_cache(entries, out_path)
    print(
        f"Cache exported to {out_path} "
        f"({len(entries)} entries, schema v1)"
    )


def import_cache_file(
    path: str,
    merge: bool = True,
) -> dict[str, dict]:

    incoming = load_cache(path)
    print(f"Imported {len(incoming)} entries from {path}")
    return incoming


def load_cache_from_any(path: str) -> dict[str, dict]:
    return load_cache(path)
