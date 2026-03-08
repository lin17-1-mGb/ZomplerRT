# ZomplerRT
Hardware and software setup for the Zompler RT on a Raspberry Pi Zero 2W with Pimoroni Pirate Audio HAT, Waveshare UPS HAT C, and RT kernel for low-latency MIDI performance.

---

## Hardware Required

- Raspberry Pi Zero 2W
- Pimoroni Pirate Audio HAT (HifiBerry DAC + 240x240 ST7789 display + 4 buttons)
- Waveshare UPS HAT C (I2C battery management)
- 3.7V 1000mAh Li-ion battery
- MicroSD card (16GB+ recommended if using large soundfonts)
- USB-A to Micro-USB OTG adapter (for MIDI keyboard)
- Works well with bluetooth midi controllers such as Wavy Industries Monkey, CME WIDI
---
## compressed .img file 
https://nextcloud.englishup.me/s/s3996AY23pAerSn

## 1. Flash Raspberry Pi OS

Use **Raspberry Pi Imager** and select:

- **OS:** Raspberry Pi OS Lite (64-bit) — Debian Bookworm
- **Storage:** your MicroSD card

In Imager's settings (gear icon) configure:
- Hostname (e.g. `raspberry`)
- Username/password (default username is `pi`)
- WiFi credentials
- Enable SSH

---

## 2. First Boot & System Update

```bash
sudo apt-get update && sudo apt-get upgrade -y
```

---

## 3. Enable Hardware Interfaces

```bash
sudo raspi-config
```

- **Interface Options → SPI → Enable**
- **Interface Options → I2C → Enable**
- Finish → Reboot

---

## 4. Install RT Kernel

```bash
sudo apt-get install linux-image-6.12.47+rpt-rpi-v8-rt
```

> **Note:** Replace `6.12.47` with the version matching your current kernel (`uname -r`). Check available versions with `apt-cache search linux-image-rpi-v8-rt`.

Tell the Pi to boot the RT kernel by adding this to `/boot/firmware/config.txt`:

```bash
sudo nano /boot/firmware/config.txt
```

Add at the bottom:
```
kernel=kernel8_rt.img
```

Reboot and verify:
```bash
sudo reboot
uname -r   # Should show -rt suffix e.g. 6.12.47+rpt-rpi-v8-rt
```

---

## 5. Configure `/boot/firmware/config.txt`

Replace the contents with:

```
dtparam=i2c_arm=on
dtparam=spi=on
dtparam=audio=off
auto_initramfs=1
disable_fw_kms_setup=1
arm_64bit=1
disable_overscan=1
arm_boost=1
max_framebuffers=2
boot_delay=0
disable_splash=1
camera_auto_detect=0
display_auto_detect=0

[all]
dtoverlay=hifiberry-dac
dtoverlay=vc4-kms-v3d,noaudio
dtoverlay=dwc2,dr_mode=host
kernel=kernel8_rt.img
core_freq=250
core_freq_min=250
```

---

## 6. Configure `/boot/firmware/cmdline.txt`

Edit the file (all on one line, do not add line breaks):

```bash
sudo nano /boot/firmware/cmdline.txt
```

Replace contents with (keeping your own `PARTUUID` and `cfg80211` values):

```
console=tty3 root=PARTUUID=YOUR-PARTUUID-HERE rootfstype=ext4 fsck.mode=skip rootwait cfg80211.ieee80211_regdom=XX quiet loglevel=3 logo.nologo vt.global_cursor_default=0 dwc_otg.lpm_enable=0
```

---

## 7. Install System Dependencies

```bash
sudo apt-get install -y \
  python3-pip \
  python3-smbus \
  i2c-tools \
  fluidsynth \
  libfluidsynth-dev \
  fonts-dejavu
```

---

## 8. Install Python Packages

```bash
sudo pip3 install \
  st7789 \
  pillow \
  mido \
  python-rtmidi \
  bleak \
  pyfluidsynth \
  sf2utils \
  --break-system-packages
```

---

## 9. RT Priority Permissions (Optional)

Allows FluidSynth to set its own RT thread priority, eliminating startup warnings:

```bash
sudo nano /etc/security/limits.conf
```

Add at the bottom:
```
pi soft rtprio 95
pi hard rtprio 95
pi soft memlock unlimited
pi hard memlock unlimited
```

---

## 10. Disable Unnecessary Services

Reduces boot time significantly:

```bash
sudo systemctl disable NetworkManager-wait-online
sudo systemctl disable ModemManager
sudo systemctl disable avahi-daemon
sudo systemctl disable e2scrub_reap
sudo systemctl disable dphys-swapfile
systemctl --user mask fluidsynth
```

---

## 11. Copy Script and Sound Files

Create the required directories:

```bash
mkdir -p /home/pi/midifileplayer
mkdir -p /home/pi/sf2
mkdir -p /home/pi/midifiles
```

Copy your files to the Pi (from your PC):

```bash
scp monkey_script_master.py pi@raspberry.local:/home/pi/midifileplayer/
scp your_soundfont.sf2 pi@raspberry.local:/home/pi/sf2/
```

---

## 12. Create Splash Screen Script

```bash
sudo nano /home/pi/splash.py
```

```python
#!/usr/bin/env python3
import sys
sys.path.insert(0, '/usr/local/lib/python3.11/dist-packages/st7789')
import st7789
from PIL import Image, ImageDraw, ImageFont

disp = st7789.ST7789(width=240, height=240, rotation=90, port=0,
                     cs=st7789.BG_SPI_CS_FRONT, dc=9, backlight=13,
                     spi_speed_hz=24_000_000)
disp.begin()
img = Image.new("RGB", (240, 240), (0, 0, 0))
draw = ImageDraw.Draw(img)
try:
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
    font_tiny = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
except:
    font = ImageFont.load_default()
    font_tiny = ImageFont.load_default()
draw.text((50, 80), "ZOMPLER", font=font, fill=(255, 255, 0))
draw.text((30, 115), "Real time kernel", font=font, fill=(255, 255, 255))
draw.text((55, 160), "Loading...", font=font_tiny, fill=(100, 100, 100))
disp.display(img)
```

---

## 13. Create systemd Services

### Splash Service (runs early at boot)

```bash
sudo nano /etc/systemd/system/splash.service
```

```ini
[Unit]
Description=Boot Splash Screen
DefaultDependencies=no
After=dev-spidev0.0.device
Before=monkey.service

[Service]
Type=oneshot
User=pi
ExecStart=/usr/bin/python3 /home/pi/splash.py
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
```

### Main App Service

```bash
sudo nano /etc/systemd/system/monkey.service
```

```ini
[Unit]
Description=Monkey MIDI Player
After=sound.target bluetooth.target
Wants=bluetooth.target

[Service]
User=pi
WorkingDirectory=/home/pi
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -u /home/pi/midifileplayer/monkey_script_master.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Enable Both Services

```bash
sudo systemctl daemon-reload
sudo systemctl enable splash
sudo systemctl enable monkey
```

---

## 14. Reboot & Test

```bash
sudo reboot
```

Expected boot sequence:
1. Splash screen appears (~5-8 seconds)
2. Main menu appears (~23 seconds total)

Check service status if something is wrong:
```bash
sudo systemctl status monkey
sudo journalctl -u monkey --no-pager -o cat | tail -20
```

---

## Troubleshooting

**No sound:** Check HifiBerry DAC is card 0 with `aplay -l`. If HDMI grabbed card 0, verify `dtparam=audio=off` and `dtoverlay=vc4-kms-v3d,noaudio` are in `config.txt`.

**Display not working:** Verify SPI is enabled (`ls /dev/spidev*`) and the `pi` user is in the `spi` group (`groups pi`).

**MIDI double notes:** Check `aconnect -l` for duplicate connections. Disable any auto-connection daemons (`sudo systemctl mask amidiminder` if installed).

**RT kernel not loading:** Verify `kernel=kernel8_rt.img` is in `config.txt` and the file exists at `/boot/firmware/kernel8_rt.img`.

**Mixer shows no preset names:** Install `sf2utils` — `sudo pip3 install sf2utils --break-system-packages`.

**FluidSynth priority warnings:** Add RT priority limits to `/etc/security/limits.conf` (see Step 9).
