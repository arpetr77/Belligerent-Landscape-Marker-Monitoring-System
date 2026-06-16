"""
Upload script — запускается после посадки (Wi-Fi / USB).
Сканирует OUTPUT_DIR, отправляет фото + frames.csv + track.gpx на сервер.
"""

import os
import sys
import time
import logging
import hashlib
import requests
from pathlib import Path

SERVER_URL   = os.environ.get("SERVER_URL", "http://your-vps:8000")
API_KEY      = os.environ.get("API_KEY", "changeme")
OUTPUT_DIR   = Path(os.environ.get("OUTPUT_DIR", "/media/uav-sd"))
MISSION_ID   = os.environ.get("MISSION_ID", "")
CHUNK_SIZE   = 5 * 1024 * 1024  # 5 МБ за раз
RETRY_MAX    = 3
RETRY_DELAY  = 5

log = logging.getLogger("uav_upload")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

HEADERS = {"X-Api-Key": API_KEY}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_file(url: str, path: Path, extra_fields: dict | None = None):
    for attempt in range(1, RETRY_MAX + 1):
        try:
            with open(path, "rb") as f:
                files = {"file": (path.name, f, "application/octet-stream")}
                data = {"sha256": sha256(path), **(extra_fields or {})}
                r = requests.post(url, headers=HEADERS, files=files, data=data, timeout=60)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"Attempt {attempt}/{RETRY_MAX} failed for {path.name}: {e}")
            if attempt < RETRY_MAX:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"Upload failed: {path}")


def main():
    mission_dir = OUTPUT_DIR / MISSION_ID if MISSION_ID else OUTPUT_DIR
    photos_dir  = mission_dir / "photos"
    gpx_path    = mission_dir / "track.gpx"
    csv_path    = mission_dir / "frames.csv"

    log.info(f"Upload from: {mission_dir}")
    log.info(f"Server: {SERVER_URL}")

    # 1. Создаём миссию на сервере
    r = requests.post(
        f"{SERVER_URL}/api/missions",
        headers=HEADERS,
        json={"name": MISSION_ID, "local_path": str(mission_dir)},
        timeout=15,
    )
    r.raise_for_status()
    server_mission_id = r.json()["id"]
    log.info(f"Mission registered: {server_mission_id}")

    # 2. Загружаем GPX-трек
    if gpx_path.exists():
        upload_file(f"{SERVER_URL}/api/missions/{server_mission_id}/gpx", gpx_path)
        log.info("GPX uploaded")

    # 3. Загружаем CSV метаданных кадров
    if csv_path.exists():
        upload_file(f"{SERVER_URL}/api/missions/{server_mission_id}/frames_csv", csv_path)
        log.info("frames.csv uploaded")

    # 4. Загружаем фотографии
    photos = sorted(photos_dir.glob("*.jpg"))
    log.info(f"Uploading {len(photos)} photos…")
    ok = err = 0
    for i, photo in enumerate(photos, 1):
        try:
            upload_file(
                f"{SERVER_URL}/api/missions/{server_mission_id}/images",
                photo,
                extra_fields={"mission_id": server_mission_id},
            )
            ok += 1
            if i % 20 == 0:
                log.info(f"  {i}/{len(photos)} uploaded…")
        except RuntimeError as e:
            log.error(str(e))
            err += 1

    # 5. Финализируем миссию → сервер ставит задачи в очередь YOLOv8
    requests.post(
        f"{SERVER_URL}/api/missions/{server_mission_id}/finalize",
        headers=HEADERS, timeout=15,
    )
    log.info(f"Done. OK={ok}  ERR={err}. Processing queued on server.")


if __name__ == "__main__":
    main()
