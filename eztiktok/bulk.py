
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from .config import AppConfig, load_config
from .download import process_one_url

PROFILE_PATTERN = re.compile(r"^@?([\w.-]+)$")
PROFILE_URL_PATTERN = re.compile(
    r"tiktok\.com/@([\w.-]+)(?:/|$|\?)",
    re.IGNORECASE,
)

# Bảo vệ cache dùng chung khi tải nhiều luồng
_CACHE_LOCK = threading.RLock()


def is_profile_input(text: str) -> bool:
    text = text.strip()

    if PROFILE_PATTERN.fullmatch(text):
        return True

    if (
        PROFILE_URL_PATTERN.search(text)
        and "/video/" not in text
        and "/photo/" not in text
    ):
        return True

    return False


def extract_profile_username(text: str) -> Optional[str]:
    text = text.strip()

    match = PROFILE_PATTERN.fullmatch(text)
    if match:
        return match.group(1)

    match = PROFILE_URL_PATTERN.search(text)
    if match:
        return match.group(1)

    return None


def _gallery_dl_command(username: str, cfg: AppConfig) -> list[str]:
    profile_url = f"https://www.tiktok.com/@{username}"

    cmd = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--dump-json",
        profile_url,
    ]

    if cfg.cookies_path and os.path.isfile(cfg.cookies_path):
        cmd[-1:-1] = ["--cookies", cfg.cookies_path]

    return cmd


def fetch_profile_urls(
    username: str,
    cfg: Optional[AppConfig] = None,
) -> list[str]:

    cfg = cfg or load_config()
    cmd = _gallery_dl_command(username, cfg)

    print(f"Đang lấy danh sách video @{username}...")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"Quá thời gian lấy danh sách @{username}")
        return []
    except Exception as error:
        print(f"Không thể lấy danh sách @{username}: {error}")
        return []

    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "").strip()
        print(f"gallery-dl lỗi với @{username}: {error_text[:700]}")
        return []

    urls: list[str] = []
    seen: set[str] = set()

    for line in (result.stdout or "").splitlines():
        line = line.strip()

        if (
            line.startswith("http")
            and "tiktok.com" in line.lower()
            and ("/video/" in line or "/photo/" in line)
            and line not in seen
        ):
            seen.add(line)
            urls.append(line)

    return urls


def process_profile(
    username_or_url: str,
    cache: dict,
    output_dir: str,
    cfg: Optional[AppConfig] = None,
    progress_callback: Optional[
        Callable[[int, Optional[int]], None]
    ] = None,
    workers: int = 5,
) -> int:

    cfg = cfg or load_config()

    username = extract_profile_username(username_or_url)

    if not username:
        print(f"Tên tài khoản không hợp lệ: {username_or_url}")
        return 0

    urls = fetch_profile_urls(username, cfg)

    if not urls:
        print(f"Không tìm thấy video cho @{username}")
        return 0

    print(f"Tìm thấy {len(urls)} bài đăng của @{username}")

    return process_urls(
        urls=urls,
        cache=cache,
        output_dir=output_dir,
        cfg=cfg,
        progress_callback=progress_callback,
        workers=workers,
    )


def process_urls(
    urls: list[str],
    cache: dict,
    output_dir: str,
    cfg: Optional[AppConfig] = None,
    progress_callback: Optional[
        Callable[[int, Optional[int]], None]
    ] = None,
    workers: int = 5,
) -> int:

    cfg = cfg or load_config()

    # Loại bỏ URL trùng
    unique_urls = list(dict.fromkeys(
        url.strip() for url in urls if url.strip()
    ))

    if not unique_urls:
        print("Không có đường dẫn video hợp lệ.")
        return 0

    workers = max(1, min(int(workers), 20, len(unique_urls)))

    print(
        f"Bắt đầu tải {len(unique_urls)} video "
        f"bằng {workers} luồng..."
    )

    completed = 0
    success = 0

    def download_one(url: str) -> bool:
        try:
            # Không cho nhiều luồng sửa cache cùng lúc.
            # Giữ an toàn với hàm tải gốc chưa được kiểm tra.
            with _CACHE_LOCK:
                return bool(
                    process_one_url(
                        url,
                        cache,
                        output_dir,
                        cfg,
                        None,
                    )
                )
        except Exception as error:
            print(f"Lỗi tải {url}: {error}")
            return False

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(download_one, url): url
            for url in unique_urls
        }

        for future in as_completed(futures):
            completed += 1

            try:
                if future.result():
                    success += 1
            except Exception as error:
                print(f"Lỗi tác vụ: {error}")

            if progress_callback:
                try:
                    progress_callback(completed, len(unique_urls))
                except Exception:
                    pass

            print(
                f"Tiến trình: {completed}/{len(unique_urls)} "
                f"| Thành công: {success}"
            )

    print(
        f"Hoàn tất: {success}/{len(unique_urls)} video tải thành công."
    )

    return success


def process_profiles(
    usernames: list[str],
    cache: dict,
    output_dir: str,
    cfg: Optional[AppConfig] = None,
    progress_callback: Optional[
        Callable[[int, Optional[int]], None]
    ] = None,
    workers: int = 5,
) -> int:

    cfg = cfg or load_config()

    all_urls: list[str] = []

    for username in usernames:
        username = username.strip()

        if not username:
            continue

        if not is_profile_input(username):
            print(f"Bỏ qua tên không hợp lệ: {username}")
            continue

        found = fetch_profile_urls(
            extract_profile_username(username) or "",
            cfg,
        )

        print(f"{username}: tìm thấy {len(found)} bài đăng")
        all_urls.extend(found)

    return process_urls(
        urls=all_urls,
        cache=cache,
        output_dir=output_dir,
        cfg=cfg,
        progress_callback=progress_callback,
        workers=workers,
    )
