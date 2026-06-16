#!/usr/bin/env bash
# Установка зависимостей на Raspberry Pi Zero 2W (Bookworm / Bullseye)
set -e

echo "=== Обновление пакетов ==="
sudo apt-get update -qq
sudo apt-get install -y python3-pip python3-venv gpsd gpsd-clients

echo "=== Виртуальное окружение ==="
python3 -m venv /home/pi/uav-env
source /home/pi/uav-env/bin/activate

echo "=== Python-пакеты ==="
pip install --upgrade pip
pip install picamera2 pyserial piexif gpxpy pynmea2 requests

echo "=== Настройка gpsd ==="
sudo tee /etc/default/gpsd > /dev/null << 'EOF'
START_DAEMON="true"
GPSD_OPTIONS="-n"
DEVICES="/dev/ttyS0"
USBAUTO="false"
EOF
sudo systemctl enable gpsd
sudo systemctl start gpsd

echo "=== systemd-сервис захвата ==="
sudo tee /etc/systemd/system/uav-capture.service > /dev/null << 'EOF'
[Unit]
Description=UAV Capture Module
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/uav_capture
Environment=OUTPUT_DIR=/media/uav-sd
Environment=GPS_PORT=/dev/ttyS0
Environment=CAPTURE_INTERVAL=1.0
ExecStart=/home/pi/uav-env/bin/python capture.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable uav-capture
echo "=== Готово! Перезагрузите Pi или: sudo systemctl start uav-capture ==="
