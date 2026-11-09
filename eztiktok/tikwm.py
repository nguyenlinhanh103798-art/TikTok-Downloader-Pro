
from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from .cache import (
    CACHE_LOCK,
    get_item_lock,
    load_cache,
    save_cache,
)
from .config import AppConfig, load_config
from .tikwm import (
    TIKWM_HEADERS,
    content_type_from_url,
    extract_media_id_from_url,
    extract_username_from_url,
    load_sessionid,
    resolve_tiktok_redirect,
    sanitize_filename,
    submit_tikwm_task,
)

ProgressCallback = Optional[Callable[[int, Optional[int]], None]]

# Tránh hai tác vụ ghi cùng một đường dẫn
_FILE_LOCKS: dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def get_file_lock(filepath: str) -> threading.Lock:
    key = os.path.abspath(filepath)

    with _FILE_LOCKS_GUARD:
        if key not in _FILE_LOCKS:
            _FILE_LOCKS[key] = threading.Lock()

        return _FILE_LOCKS[key]


def build_date_str(create_time: Optional[int]) -> str:
    if create_time is not None:
        try:
            return datetime.fromtimestamp(
                int(create_time),
                tz=timezone.utc,
            ).strftime("%y-%m-%d")
        except (ValueError, TypeError, OSError):
            pass

    return datetime.now(timezone.utc).strftime("%y-%m-%d")


def build_filename(
    entry: dict,
    ext: str = ".mp4",
    cfg: Optional[AppConfig] = None,
) -> str:

    cfg = cfg or load_config()

    username = sanitize_filename(
        str(entry.get("username") or "unknown")
    )

    date_str = build_date_str(entry.get("create_time"))

    profile_uid = entry.get("profile_uid") or "unknown"

    if isinstance(profile_uid, (int, float)):
        profile_uid = str(int(profile_uid))

    video_id = sanitize_filename(
        str(entry.get("video_id") or "unknown")
    )

    return cfg.naming_template.format(
        date=date_str,
        profile_uid=profile_uid,
        username=username,
        video_id=video_id,
        ext=ext,
    )


def subfolder_for_content_type(content_type: str) -> str:
    if content_type == "story":
        return "story"

    if content_type == "highlight":
        return "highlight"

    return ""


def count_images_in_folder(folder: str) -> int:
    if not os.path.isdir(folder):
        return 0

    return sum(
        1
        for name in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, name))
        and name.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp")
        )
    )


def download_to_file(
    url: str,
    filepath: str,
    headers: Optional[dict] = None,
    cfg: Optional[AppConfig] = None,
    progress_callback: ProgressCallback = None,
) -> bool:

    cfg = cfg or load_config()

    if not url:
        return False

    folder = os.path.dirname(filepath) or "."
    os.makedirs(folder, exist_ok=True)

    lock = get_file_lock(filepath)

    with lock:
        if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
            return True

        temp_path = filepath + ".part"

        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)

            response = requests.get(
                url,
                headers=headers or TIKWM_HEADERS,
                timeout=cfg.request_timeout,
                stream=True,
            )

            response.raise_for_status()

            total = int(
                response.headers.get("Content-Length", 0)
            ) or None

            downloaded = 0

            with response, open(temp_path, "wb") as file:
                for chunk in response.iter_content(
                    chunk_size=64 * 1024
                ):
                    if not chunk:
                        continue

                    file.write(chunk)
                    downloaded += len(chunk)

                    if progress_callback:
                        try:
                            progress_callback(downloaded, total)
                        except Exception:
                            pass

                file.flush()
                os.fsync(file.fileno())

            if not os.path.isfile(temp_path):
                return False

            if os.path.getsize(temp_path) <= 0:
                return False

            os.replace(temp_path, filepath)
            return True

        except Exception as error:
            print(f"Download error: {error}")
            return False

        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


def do_download(
    entry: dict,
    output_dir: str,
    cfg: Optional[AppConfig] = None,
    progress_callback: ProgressCallback = None,
) -> bool:

    cfg = cfg or load_config()

    username = sanitize_filename(
        str(entry.get("username") or "unknown")
    )

    video_id = str(entry.get("video_id") or "unknown")

    play_url = entry.get("play_url")
    images = entry.get("images") or []

    content_type = entry.get("content_type") or "video"

    sub = subfolder_for_content_type(content_type)

    folder = (
        os.path.join(output_dir, username, sub)
        if sub
        else os.path.join(output_dir, username)
    )

    os.makedirs(folder, exist_ok=True)

    # Tải ảnh/slideshow
    if images and (not play_url or content_type == "photo"):

        base_name = build_filename(entry, "", cfg)
        success = 0

        for index, item in enumerate(images):

            image_url = (
                item
                if isinstance(item, str)
                else (
                    item.get("url")
                    or item.get("image_url")
                    or ""
                )
            )

            if not image_url:
                continue

            clean_url = image_url.split("?")[0]

            ext = ".jpg"

            if "." in clean_url:
                candidate = "." + clean_url.rsplit(".", 1)[-1].lower()

                if candidate in (
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".webp",
                ):
                    ext = candidate

            filepath = os.path.join(
                folder,
                f"{base_name}_img_{index + 1}{ext}",
            )

            if download_to_file(
                image_url,
                filepath,
                cfg=cfg,
                progress_callback=progress_callback,
            ):
                success += 1

        if success:
            print(
                f"Downloaded {success} image(s) -> {folder}"
            )

        return success > 0

    # Tải video
    if not play_url:
        return False

    if play_url.startswith("//"):
        play_url = "https:" + play_url

    filename = build_filename(entry, ".mp4", cfg)

    filepath = os.path.join(folder, filename)

    headers = {
        **TIKWM_HEADERS,
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
    }

    if download_to_file(
        play_url,
        filepath,
        headers=headers,
        cfg=cfg,
        progress_callback=progress_callback,
    ):
        print(f"Downloaded: {filepath}")
        return True

    return False


def process_one_url(
    tiktok_url: str,
    cache: dict,
    output_dir: str,
    cfg: Optional[AppConfig] = None,
    progress_callback: ProgressCallback = None,
) -> bool:

    cfg = cfg or load_config()

    tiktok_url = tiktok_url.strip()

    if (
        "tiktok.com" not in tiktok_url
        and "douyin.com" not in tiktok_url
    ):
        print(f"Skipped (not TikTok): {tiktok_url[:60]}...")
        return False

    tiktok_url = resolve_tiktok_redirect(
        tiktok_url,
        cfg,
    )

    video_id = (
        extract_media_id_from_url(tiktok_url)
        or "unknown"
    )

    username = (
        extract_username_from_url(tiktok_url)
        or "unknown"
    )

    content_type = content_type_from_url(tiktok_url)

    # Mỗi video xử lý độc lập
    with get_item_lock(video_id):

        with CACHE_LOCK:
            cached_entry = cache.get(video_id)
            ent = (
                dict(cached_entry)
                if isinstance(cached_entry, dict)
                else None
            )

        if ent:
            ent.setdefault("video_id", video_id)
            ent.setdefault("username", username)
            ent.setdefault("content_type", content_type)
            ent.setdefault("create_time", None)
            ent.setdefault("profile_uid", "unknown")

            if ent.get("play_url") or ent.get("images"):
                print(f"[Cache] Using cached link: {video_id}")

                return do_download(
                    ent,
                    output_dir,
                    cfg,
                    progress_callback,
                )

        print(f"Extracting link for {video_id}...")

        result = submit_tikwm_task(
            tiktok_url,
            cfg,
        )

        if not result:

            if content_type == "photo":
                print(
                    f"TikWM failed for photo {video_id}; "
                    "trying gallery-dl fallback..."
                )

                return gallery_dl_photo_fallback(
                    tiktok_url,
                    output_dir,
                    username,
                    cfg,
                )

            print(f"Extraction failed for {video_id}")
            return False

        entry = {
            "username": result.get("username") or username,
            "play_url": result.get("play_url"),
            "video_id": result.get("video_id") or video_id,
            "images": result.get("images") or [],
            "create_time": result.get("create_time"),
            "profile_uid": result.get("profile_uid") or "unknown",
            "content_type": content_type,
        }

        with CACHE_LOCK:
            cache[video_id] = entry
            snapshot = dict(cache)

        save_cache(snapshot, cfg.cache_path)

        return do_download(
            entry,
            output_dir,
            cfg,
            progress_callback,
        )


def gallery_dl_photo_fallback(
    tiktok_url: str,
    output_dir: str,
    username: str,
    cfg: Optional[AppConfig] = None,
) -> bool:

    cfg = cfg or load_config()

    folder = os.path.join(
        output_dir,
        sanitize_filename(username or "unknown"),
        "photo",
    )

    os.makedirs(folder, exist_ok=True)

    before = count_images_in_folder(folder)

    cmd = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--no-skip",
        "-D",
        folder,
    ]

    if cfg.cookies_path and os.path.isfile(cfg.cookies_path):
        cmd.extend([
            "--cookies",
            cfg.cookies_path,
        ])

    cmd.append(tiktok_url)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    except Exception as error:
        print(f"gallery-dl error: {error}")
        return False

    if result.returncode != 0:
        message = (
            result.stderr or result.stdout or ""
        ).strip()

        if message:
            print(message[:500])

        return False

    after = count_images_in_folder(folder)

    if after <= before:
        return False

    print(
        f"Downloaded {after - before} image(s) -> {folder}"
    )

    return True
