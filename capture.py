"""
UAV Capture Module — бортовой скрипт для Raspberry Pi Zero 2W
Снимает JPEG, записывает GPS-координаты в EXIF, сохраняет GPX-трек.
Никакого ML на борту — только сбор данных.

Зависимости:
  pip install picamera2 pyserial piexif gpxpy pynmea2
"""

import os
import time
import serial
import threading
import logging
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone

import piexif
import gpxpy
import gpxpy.gpx
import pynmea2
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput

# ── Конфигурация ──────────────────────────────────────────────────────────────

MISSION_ID      = os.environ.get("MISSION_ID", datetime.now().strftime("%Y%m%d_%H%M%S"))
OUTPUT_DIR      = Path(os.environ.get("OUTPUT_DIR", f"/media/uav-sd/{MISSION_ID}"))
GPS_PORT        = os.environ.get("GPS_PORT", "/dev/ttyS0")
GPS_BAUD        = int(os.environ.get("GPS_BAUD", "9600"))
CAPTURE_INTERVAL = float(os.environ.get("CAPTURE_INTERVAL", "1.0"))  # секунд между кадрами
JPEG_QUALITY    = int(os.environ.get("JPEG_QUALITY", "92"))
RESOLUTION      = (4056, 3040)   # IMX219 полное разрешение; (1920,1080) для скорости
GPS_TIMEOUT     = 60             # сек ожидания первого фикса
MIN_SATELLITES  = 4              # минимум спутников для записи

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(OUTPUT_DIR / "capture.log") if OUTPUT_DIR.exists() else logging.StreamHandler(),
    ],
)
log = logging.getLogger("uav_capture")

# ── Глобальное состояние GPS ──────────────────────────────────────────────────

class GpsFix:
    """Потокобезопасный контейнер последнего GPS-фикса."""
    def __init__(self):
        self._lock = threading.Lock()
        self.lat: float | None = None
        self.lon: float | None = None
        self.alt: float | None = None
        self.speed_kmh: float | None = None
        self.heading: float | None = None
        self.satellites: int = 0
        self.fix_quality: int = 0
        self.utc_time: datetime | None = None
        self.hdop: float | None = None

    def update(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    @property
    def valid(self) -> bool:
        with self._lock:
            return (
                self.lat is not None
                and self.lon is not None
                and self.fix_quality > 0
                and self.satellites >= MIN_SATELLITES
            )


gps = GpsFix()
_stop_event = threading.Event()

# ── GPS-поток ─────────────────────────────────────────────────────────────────

def _dd_to_dms_exif(dd: float) -> tuple:
    """Десятичные градусы → EXIF-рациональные (градусы, минуты, секунды)."""
    dd = abs(dd)
    d = int(dd)
    m = int((dd - d) * 60)
    s = round(((dd - d) * 60 - m) * 60 * 1000)  # тысячные долей секунды
    return ((d, 1), (m, 1), (s, 1000))


def gps_reader():
    """Фоновый поток: читает NMEA из BN220, обновляет GpsFix."""
    log.info(f"GPS reader started on {GPS_PORT} @ {GPS_BAUD}")
    while not _stop_event.is_set():
        try:
            with serial.Serial(GPS_PORT, GPS_BAUD, timeout=2.0) as ser:
                while not _stop_event.is_set():
                    raw = ser.readline()
                    if not raw:
                        continue
                    try:
                        line = raw.decode("ascii", errors="replace").strip()
                    except Exception:
                        continue
                    if not line.startswith("$"):
                        continue
                    try:
                        msg = pynmea2.parse(line)
                    except pynmea2.ParseError:
                        continue

                    if isinstance(msg, pynmea2.types.talker.GGA):
                        # Основной фикс
                        if msg.latitude and msg.longitude:
                            gps.update(
                                lat=msg.latitude,
                                lon=msg.longitude,
                                alt=float(msg.altitude) if msg.altitude else None,
                                satellites=int(msg.num_sats) if msg.num_sats else 0,
                                fix_quality=int(msg.gps_qual) if msg.gps_qual else 0,
                                hdop=float(msg.horizontal_dil) if msg.horizontal_dil else None,
                            )
                    elif isinstance(msg, pynmea2.types.talker.RMC):
                        # Скорость, курс, время
                        if msg.status == "A":
                            dt = None
                            if msg.datetime:
                                dt = msg.datetime.replace(tzinfo=timezone.utc)
                            gps.update(
                                speed_kmh=float(msg.spd_over_grnd) * 1.852 if msg.spd_over_grnd else None,
                                heading=float(msg.true_course) if msg.true_course else None,
                                utc_time=dt,
                            )
        except serial.SerialException as e:
            log.warning(f"GPS serial error: {e}. Retry in 3s…")
            time.sleep(3)
        except Exception as e:
            log.error(f"GPS reader exception: {e}", exc_info=True)
            time.sleep(3)


# ── EXIF-запись ───────────────────────────────────────────────────────────────

def build_gps_exif(fix: dict) -> dict:
    """Строит GPS-секцию EXIF из снимка фикса."""
    lat, lon, alt = fix["lat"], fix["lon"], fix["alt"]
    gps_ifd = {
        piexif.GPSIFD.GPSVersionID: (2, 3, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef:  b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude:     _dd_to_dms_exif(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude:    _dd_to_dms_exif(lon),
        piexif.GPSIFD.GPSMeasureMode:  b"3",   # 3D
        piexif.GPSIFD.GPSSatellites:   str(fix["satellites"]).encode(),
    }
    if alt is not None:
        gps_ifd[piexif.GPSIFD.GPSAltitudeRef] = 0
        gps_ifd[piexif.GPSIFD.GPSAltitude] = (int(alt * 100), 100)
    if fix.get("heading") is not None:
        gps_ifd[piexif.GPSIFD.GPSImgDirectionRef] = b"T"
        gps_ifd[piexif.GPSIFD.GPSImgDirection] = (int(fix["heading"] * 100), 100)
    if fix.get("speed_kmh") is not None:
        gps_ifd[piexif.GPSIFD.GPSSpeedRef] = b"K"
        gps_ifd[piexif.GPSIFD.GPSSpeed] = (int(fix["speed_kmh"] * 100), 100)
    if fix.get("utc_time") is not None:
        t = fix["utc_time"]
        gps_ifd[piexif.GPSIFD.GPSDateStamp] = t.strftime("%Y:%m:%d").encode()
        gps_ifd[piexif.GPSIFD.GPSTimeStamp] = (
            (t.hour, 1), (t.minute, 1), (t.second, 1)
        )
    return gps_ifd


def inject_exif(jpeg_path: Path, fix: dict):
    """Читает JPEG, вставляет GPS EXIF, перезаписывает файл."""
    try:
        exif_dict = piexif.load(str(jpeg_path))
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

    exif_dict["GPS"] = build_gps_exif(fix)

    # Дополнительные Exif-поля
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = (
        fix["utc_time"].strftime("%Y:%m:%d %H:%M:%S").encode()
        if fix.get("utc_time") else datetime.utcnow().strftime("%Y:%m:%d %H:%M:%S").encode()
    )
    exif_dict["0th"][piexif.ImageIFD.Make]  = b"Raspberry Pi"
    exif_dict["0th"][piexif.ImageIFD.Model] = b"IMX219"

    piexif.insert(piexif.dump(exif_dict), str(jpeg_path))


# ── GPX-трек ──────────────────────────────────────────────────────────────────

class GpxTrack:
    """Накапливает точки трека и сбрасывает в файл при завершении."""
    def __init__(self, path: Path):
        self.path = path
        self.gpx = gpxpy.gpx.GPX()
        track = gpxpy.gpx.GPXTrack(name=MISSION_ID)
        self.gpx.tracks.append(track)
        self.segment = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(self.segment)
        self._lock = threading.Lock()

    def add_point(self, fix: dict):
        pt = gpxpy.gpx.GPXTrackPoint(
            latitude=fix["lat"],
            longitude=fix["lon"],
            elevation=fix.get("alt"),
            time=fix.get("utc_time"),
        )
        if fix.get("speed_kmh") is not None:
            pt.speed = fix["speed_kmh"] / 3.6  # м/с
        with self._lock:
            self.segment.points.append(pt)

    def save(self):
        with self._lock:
            xml = self.gpx.to_xml()
        self.path.write_text(xml, encoding="utf-8")
        log.info(f"GPX saved → {self.path}  ({len(self.segment.points)} points)")


# ── Лог кадров (CSV) ─────────────────────────────────────────────────────────

class FrameLog:
    """CSV с метаданными каждого кадра для сервера."""
    HEADER = "filename,lat,lon,alt_m,heading_deg,speed_kmh,satellites,hdop,utc_time\n"

    def __init__(self, path: Path):
        self.path = path
        self._f = open(path, "w", encoding="utf-8", buffering=1)
        self._f.write(self.HEADER)

    def write(self, filename: str, fix: dict):
        row = ",".join([
            filename,
            f"{fix['lat']:.7f}",
            f"{fix['lon']:.7f}",
            f"{fix.get('alt', ''):.1f}" if fix.get("alt") is not None else "",
            f"{fix.get('heading', ''):.1f}" if fix.get("heading") is not None else "",
            f"{fix.get('speed_kmh', ''):.1f}" if fix.get("speed_kmh") is not None else "",
            str(fix.get("satellites", "")),
            f"{fix.get('hdop', ''):.1f}" if fix.get("hdop") is not None else "",
            fix["utc_time"].isoformat() if fix.get("utc_time") else "",
        ])
        self._f.write(row + "\n")

    def close(self):
        self._f.close()


# ── Главный цикл ──────────────────────────────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    photos_dir = OUTPUT_DIR / "photos"
    photos_dir.mkdir(exist_ok=True)

    log.info(f"Mission: {MISSION_ID}")
    log.info(f"Output:  {OUTPUT_DIR}")

    gpx_track = GpxTrack(OUTPUT_DIR / "track.gpx")
    frame_log = FrameLog(OUTPUT_DIR / "frames.csv")

    # Запускаем GPS в фоне
    gps_thread = threading.Thread(target=gps_reader, daemon=True, name="gps")
    gps_thread.start()

    # Ожидаем GPS-фикс
    log.info("Waiting for GPS fix…")
    deadline = time.monotonic() + GPS_TIMEOUT
    while not gps.valid:
        if time.monotonic() > deadline:
            log.warning(f"No GPS fix after {GPS_TIMEOUT}s — continuing WITHOUT geotag")
            break
        log.info(f"  sats={gps.satellites}  fix_quality={gps.fix_quality}")
        time.sleep(2)

    if gps.valid:
        fix0 = gps.snapshot()
        log.info(f"GPS fix acquired: {fix0['lat']:.6f}, {fix0['lon']:.6f}  sats={fix0['satellites']}")

    # Инициализация камеры
    cam = Picamera2()
    config = cam.create_still_configuration(
        main={"size": RESOLUTION, "format": "RGB888"},
        lores={"size": (640, 480)},
        display="lores",
    )
    cam.configure(config)
    cam.set_controls({
        "AwbEnable":       True,
        "AeEnable":        True,
        "ExposureTime":    0,     # авто
        "AnalogueGain":    0,     # авто
        "Sharpness":       1.5,
    })
    cam.start()
    time.sleep(2)  # прогрев AE/AWB

    frame_idx = 0
    log.info("Capture loop started. Ctrl+C to stop.")

    def _shutdown(sig, frame):
        log.info("Shutdown signal received.")
        _stop_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while not _stop_event.is_set():
            t0 = time.monotonic()

            fix = gps.snapshot()
            has_fix = gps.valid

            filename = f"{MISSION_ID}_{frame_idx:05d}.jpg"
            out_path = photos_dir / filename

            # Захват кадра
            cam.capture_file(str(out_path))

            if has_fix:
                inject_exif(out_path, fix)
                gpx_track.add_point(fix)
                frame_log.write(filename, fix)
                log.info(
                    f"[{frame_idx:05d}] {filename}  "
                    f"lat={fix['lat']:.5f} lon={fix['lon']:.5f} "
                    f"alt={fix.get('alt','?')}m  sats={fix['satellites']}"
                )
            else:
                log.warning(f"[{frame_idx:05d}] {filename}  NO GPS FIX")

            frame_idx += 1

            # Точная пауза с учётом времени захвата
            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, CAPTURE_INTERVAL - elapsed)
            _stop_event.wait(sleep_for)

    finally:
        cam.stop()
        gpx_track.save()
        frame_log.close()

        # Итоговая статистика
        total = frame_idx
        geotagged = sum(1 for _ in (photos_dir).glob("*.jpg"))
        log.info(f"Done. Frames captured: {total}, photos: {geotagged}")
        log.info(f"Output: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
