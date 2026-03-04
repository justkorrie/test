#!/usr/bin/env python3
"""
=============================================================
  Portable Pentest Menu — Raspberry Pi Zero 2W
  Waveshare 1.44" LCD HAT (128x128px)
=============================================================
  Installeer dependencies eerst:
    sudo apt install -y aircrack-ng mdk4 hostapd dnsmasq
                        hcxtools bettercap python3-pip
                        python3-pil python3-rpi.gpio
                        python3-spidev fonts-dejavu-core
    pip3 install RPi.GPIO spidev pillow --break-system-packages

  Clone ook de Waveshare library:
    git clone https://github.com/waveshare/1.44inch-LCD-HAT.git ~/waveshare_hat

  Run als root (nodig voor airmon, hostapd etc.):
    sudo python3 pentest_menu.py
=============================================================
"""

import os
import sys
import time
import subprocess
import threading
import signal
import socket
import re

# ── Waveshare library pad toevoegen ──────────────────────────────────────────
HAT_LIB_PATHS = [
    os.path.expanduser("~/waveshare_hat/python/lib/"),
    os.path.expanduser("~/1.44inch-LCD-HAT/python/lib/"),
    "/home/pi/waveshare_hat/python/lib/",
    "/home/pi/1.44inch-LCD-HAT/python/lib/",
]
for p in HAT_LIB_PATHS:
    if os.path.exists(p):
        sys.path.insert(0, p)
        break

try:
    import RPi.GPIO as GPIO
    import spidev
    from PIL import Image, ImageDraw, ImageFont
    HARDWARE = True
except ImportError:
    HARDWARE = False
    print("[WARN] Hardware libs niet gevonden — simulatiemodus (geen display/GPIO)")

# ── GPIO Pin definities (Waveshare 1.44" HAT) ────────────────────────────────
PIN_KEY1      = 21   # Knop boven (KEY1)
PIN_KEY2      = 20   # Knop midden (KEY2)  → BACK / ANNULEER
PIN_KEY3      = 26   # Knop onder (KEY3)   → niet gebruikt hier
PIN_JOY_UP    = 6
PIN_JOY_DOWN  = 19
PIN_JOY_LEFT  = 5
PIN_JOY_RIGHT = 26   # gedeeld met KEY3 op sommige versies
PIN_JOY_PRESS = 13   # Joystick indrukken → SELECT / ENTER

# Display
LCD_WIDTH  = 128
LCD_HEIGHT = 128

# ── Kleuren (BGR voor Waveshare, maar PIL gebruikt RGB) ───────────────────────
C_BG        = (10,  12,  20)   # Bijna zwart
C_ACCENT    = (0,   200, 100)  # Groen — hacker-stijl
C_ACCENT2   = (0,   140, 255)  # Blauw
C_WARN      = (255, 170,  30)  # Oranje
C_DANGER    = (220,  50,  50)  # Rood
C_TEXT      = (220, 230, 240)  # Lichtgrijs/wit
C_DIM       = (80,   90, 110)  # Dimgrijs
C_SEL_BG    = (0,    60,  30)  # Geselecteerde rij achtergrond
C_HEADER_BG = (15,   20,  35)  # Header achtergrond
C_BORDER    = (0,   200, 100)  # Rand

# ── Fonts ─────────────────────────────────────────────────────────────────────
def load_fonts():
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
    ]
    mono = None
    for c in candidates:
        if os.path.exists(c):
            mono = c
            break
    try:
        f_title  = ImageFont.truetype(mono, 9)  if mono else ImageFont.load_default()
        f_menu   = ImageFont.truetype(mono, 8)  if mono else ImageFont.load_default()
        f_small  = ImageFont.truetype(mono, 7)  if mono else ImageFont.load_default()
        f_status = ImageFont.truetype(mono, 7)  if mono else ImageFont.load_default()
    except Exception:
        f_title = f_menu = f_small = f_status = ImageFont.load_default()
    return f_title, f_menu, f_small, f_status

# ── LCD Driver (minimale inline implementatie als fallback) ───────────────────
class LCD_Driver:
    """Waveshare 1.44" LCD HAT — ST7735S driver"""
    RST = 27; DC = 25; BL = 24; CS = 8

    # ST7735S commando's
    NOP=0x00; SWRESET=0x01; SLPOUT=0x11; NORON=0x13
    INVOFF=0x20; INVON=0x21; DISPON=0x29; CASET=0x2A
    RASET=0x2B; RAMWR=0x2C; MADCTL=0x36; COLMOD=0x3A
    FRMCTR1=0xB1; FRMCTR2=0xB2; FRMCTR3=0xB3; INVCTR=0xB4
    PWCTR1=0xC0; PWCTR2=0xC1; PWCTR3=0xC2; PWCTR4=0xC3
    PWCTR5=0xC4; VMCTR1=0xC5; GMCTRP1=0xE0; GMCTRN1=0xE1

    def __init__(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in [self.RST, self.DC, self.BL, self.CS]:
            GPIO.setup(pin, GPIO.OUT)
        GPIO.output(self.BL, GPIO.HIGH)
        self.spi = spidev.SpiDev()
        self.spi.open(0, 0)
        self.spi.max_speed_hz = 40000000
        self.spi.mode = 0
        self._init_display()

    def _cmd(self, cmd):
        GPIO.output(self.DC, GPIO.LOW)
        GPIO.output(self.CS, GPIO.LOW)
        self.spi.writebytes([cmd])
        GPIO.output(self.CS, GPIO.HIGH)

    def _data(self, data):
        GPIO.output(self.DC, GPIO.HIGH)
        GPIO.output(self.CS, GPIO.LOW)
        if isinstance(data, int):
            self.spi.writebytes([data])
        else:
            # Stuur in chunks van 4096 bytes
            for i in range(0, len(data), 4096):
                self.spi.writebytes(list(data[i:i+4096]))
        GPIO.output(self.CS, GPIO.HIGH)

    def _reset(self):
        GPIO.output(self.RST, GPIO.HIGH); time.sleep(0.05)
        GPIO.output(self.RST, GPIO.LOW);  time.sleep(0.05)
        GPIO.output(self.RST, GPIO.HIGH); time.sleep(0.05)

    def _init_display(self):
        self._reset()
        self._cmd(self.SWRESET); time.sleep(0.15)
        self._cmd(self.SLPOUT);  time.sleep(0.5)
        self._cmd(self.FRMCTR1); self._data([0x01,0x2C,0x2D])
        self._cmd(self.FRMCTR2); self._data([0x01,0x2C,0x2D])
        self._cmd(self.FRMCTR3); self._data([0x01,0x2C,0x2D,0x01,0x2C,0x2D])
        self._cmd(self.INVCTR);  self._data(0x07)
        self._cmd(self.PWCTR1);  self._data([0xA2,0x02,0x84])
        self._cmd(self.PWCTR2);  self._data(0xC5)
        self._cmd(self.PWCTR3);  self._data([0x0A,0x00])
        self._cmd(self.PWCTR4);  self._data([0x8A,0x2A])
        self._cmd(self.PWCTR5);  self._data([0x8A,0xEE])
        self._cmd(self.VMCTR1);  self._data(0x0E)
        self._cmd(self.INVON)
        # BGR, rij/kolom richting voor 128x128
        self._cmd(self.MADCTL);  self._data(0xC8)
        self._cmd(self.COLMOD);  self._data(0x05)   # 16-bit kleur
        self._cmd(self.CASET);   self._data([0x00,0x02,0x00,0x81])  # offset +2
        self._cmd(self.RASET);   self._data([0x00,0x01,0x00,0x80])  # offset +1
        self._cmd(self.GMCTRP1); self._data([0x02,0x1C,0x07,0x12,0x37,0x32,0x29,0x2D,
                                              0x29,0x25,0x2B,0x39,0x00,0x01,0x03,0x10])
        self._cmd(self.GMCTRN1); self._data([0x03,0x1D,0x07,0x06,0x2E,0x2C,0x29,0x2D,
                                              0x2E,0x2E,0x37,0x3F,0x00,0x00,0x02,0x10])
        self._cmd(self.NORON);   time.sleep(0.1)
        self._cmd(self.DISPON);  time.sleep(0.1)

    def show_image(self, img):
        """Stuur PIL Image (RGB, 128x128) naar display"""
        img = img.convert("RGB")
        pixels = []
        for r, g, b in img.getdata():
            color = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            pixels.append((color >> 8) & 0xFF)
            pixels.append(color & 0xFF)
        self._cmd(self.CASET); self._data([0x00,0x02,0x00,0x81])
        self._cmd(self.RASET); self._data([0x00,0x01,0x00,0x80])
        self._cmd(self.RAMWR); self._data(pixels)

    def clear(self, color=(0,0,0)):
        img = Image.new("RGB", (128,128), color)
        self.show_image(img)

    def backlight(self, on=True):
        GPIO.output(self.BL, GPIO.HIGH if on else GPIO.LOW)

    def cleanup(self):
        self.backlight(False)
        self.spi.close()

# ── Input handler ─────────────────────────────────────────────────────────────
class ButtonHandler:
    def __init__(self):
        self._events = []
        self._lock   = threading.Lock()
        if HARDWARE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            pins = {
                PIN_KEY1:      "KEY1",
                PIN_KEY2:      "BACK",
                PIN_JOY_UP:    "UP",
                PIN_JOY_DOWN:  "DOWN",
                PIN_JOY_PRESS: "SELECT",
            }
            for pin, name in pins.items():
                GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                GPIO.add_event_detect(
                    pin, GPIO.FALLING,
                    callback=lambda ch, n=name: self._cb(n),
                    bouncetime=200
                )

    def _cb(self, name):
        with self._lock:
            self._events.append(name)

    def get(self):
        with self._lock:
            if self._events:
                return self._events.pop(0)
        return None

    def cleanup(self):
        if HARDWARE:
            GPIO.cleanup()

# ── Proces beheer ─────────────────────────────────────────────────────────────
class ProcManager:
    def __init__(self):
        self._procs = {}

    def start(self, name, cmd, shell=True):
        self.stop(name)
        try:
            proc = subprocess.Popen(
                cmd, shell=shell,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            self._procs[name] = proc
            return True
        except Exception as e:
            return False

    def stop(self, name):
        if name in self._procs:
            try:
                os.killpg(os.getpgid(self._procs[name].pid), signal.SIGTERM)
            except Exception:
                pass
            del self._procs[name]

    def stop_all(self):
        for name in list(self._procs):
            self.stop(name)

    def is_running(self, name):
        if name not in self._procs:
            return False
        return self._procs[name].poll() is None

    def run_output(self, cmd):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            return r.stdout.strip()
        except Exception:
            return ""

# ── Hulpfuncties ──────────────────────────────────────────────────────────────
def get_interfaces():
    """Geef lijst van WiFi interfaces terug"""
    out = subprocess.run("iwconfig 2>/dev/null | grep -oP '^\\S+'",
                         shell=True, capture_output=True, text=True).stdout
    return [i.strip() for i in out.split('\n') if i.strip()]

def get_monitor_iface():
    ifaces = get_interfaces()
    for i in ifaces:
        if "mon" in i:
            return i
    return None

def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "geen IP"

def truncate(text, maxlen):
    return text if len(text) <= maxlen else text[:maxlen-1] + "…"

# ── Renderer ──────────────────────────────────────────────────────────────────
class Renderer:
    def __init__(self, lcd):
        self.lcd = lcd
        self.f_title, self.f_menu, self.f_small, self.f_status = load_fonts()
        self._last_hash = None

    def _new_canvas(self):
        img = Image.new("RGB", (LCD_WIDTH, LCD_HEIGHT), C_BG)
        draw = ImageDraw.Draw(img)
        return img, draw

    def _text_size(self, draw, text, font):
        try:
            bbox = draw.textbbox((0,0), text, font=font)
            return bbox[2]-bbox[0], bbox[3]-bbox[1]
        except Exception:
            return 8*len(text), 10

    def draw_header(self, draw, title, line2=""):
        # Header achtergrond
        draw.rectangle([0,0,LCD_WIDTH,20], fill=C_HEADER_BG)
        draw.line([0,20,LCD_WIDTH,20], fill=C_BORDER, width=1)
        # Titel
        tw, _ = self._text_size(draw, title, self.f_title)
        x = (LCD_WIDTH - tw) // 2
        draw.text((x, 3), title, font=self.f_title, fill=C_ACCENT)
        if line2:
            tw2, _ = self._text_size(draw, line2, self.f_small)
            x2 = (LCD_WIDTH - tw2) // 2
            draw.text((x2, 13), line2, font=self.f_small, fill=C_DIM)

    def draw_footer(self, draw, left="", right=""):
        draw.rectangle([0,LCD_HEIGHT-12,LCD_WIDTH,LCD_HEIGHT], fill=C_HEADER_BG)
        draw.line([0,LCD_HEIGHT-12,LCD_WIDTH,LCD_HEIGHT-12], fill=C_BORDER, width=1)
        if left:
            draw.text((3, LCD_HEIGHT-10), left, font=self.f_small, fill=C_DIM)
        if right:
            rw, _ = self._text_size(draw, right, self.f_small)
            draw.text((LCD_WIDTH-rw-3, LCD_HEIGHT-10), right, font=self.f_small, fill=C_DIM)

    def draw_menu(self, title, items, selected, status="", scroll_offset=0):
        img, draw = self._new_canvas()
        self.draw_header(draw, title)

        ITEM_H  = 14
        START_Y = 24
        VISIBLE = 6  # max items zichtbaar

        for i, (icon, label, state) in enumerate(items[scroll_offset:scroll_offset+VISIBLE]):
            y    = START_Y + i * ITEM_H
            idx  = i + scroll_offset
            is_sel = (idx == selected)

            if is_sel:
                draw.rectangle([2, y-1, LCD_WIDTH-3, y+ITEM_H-2], fill=C_SEL_BG)
                draw.rectangle([2, y-1, LCD_WIDTH-3, y+ITEM_H-2], outline=C_ACCENT, width=1)
                txt_col = C_ACCENT
            else:
                txt_col = C_TEXT

            # Indicator links
            indicator = "▶" if is_sel else " "
            draw.text((4, y+1), indicator, font=self.f_menu, fill=C_ACCENT)
            # Icon
            draw.text((14, y+1), icon, font=self.f_menu, fill=C_ACCENT2 if is_sel else C_DIM)
            # Label
            draw.text((26, y+1), truncate(label, 13), font=self.f_menu, fill=txt_col)
            # State badge rechts
            if state:
                sc = C_ACCENT if state == "ON" else (C_DANGER if state == "OFF" else C_WARN)
                sw, _ = self._text_size(draw, state, self.f_small)
                draw.text((LCD_WIDTH-sw-4, y+2), state, font=self.f_small, fill=sc)

        # Scroll indicator
        total = len(items)
        if total > VISIBLE:
            bar_h   = max(8, int(VISIBLE/total*(LCD_HEIGHT-36)))
            bar_y   = int(scroll_offset/total*(LCD_HEIGHT-36)) + 24
            draw.rectangle([LCD_WIDTH-3, 24, LCD_WIDTH-1, LCD_HEIGHT-13], fill=C_HEADER_BG)
            draw.rectangle([LCD_WIDTH-3, bar_y, LCD_WIDTH-1, bar_y+bar_h], fill=C_DIM)

        # Status onderaan
        self.draw_footer(draw, status, f"{selected+1}/{total}")

        self._send(img)

    def draw_action(self, title, lines, color=C_TEXT, blink_dot=False):
        """Actie/output scherm"""
        img, draw = self._new_canvas()
        self.draw_header(draw, title)

        y = 24
        for line in lines[:7]:
            col = color
            if line.startswith("[OK]"):   col = C_ACCENT
            elif line.startswith("[!!]"): col = C_DANGER
            elif line.startswith("[>>]"): col = C_ACCENT2
            elif line.startswith("[**]"): col = C_WARN
            draw.text((4, y), truncate(line, 18), font=self.f_small, fill=col)
            y += 13

        if blink_dot:
            dot_x = LCD_WIDTH-8
            dot_y = 24
            draw.ellipse([dot_x,dot_y,dot_x+5,dot_y+5], fill=C_ACCENT)

        self.draw_footer(draw, "KEY2=terug")
        self._send(img)

    def draw_confirm(self, title, question, sub=""):
        img, draw = self._new_canvas()
        self.draw_header(draw, title)
        draw.text((4, 28), question, font=self.f_menu, fill=C_WARN)
        if sub:
            draw.text((4, 42), truncate(sub,18), font=self.f_small, fill=C_DIM)
        # Knoppen
        draw.rectangle([8,  80, 58, 96],  fill=(0,80,0),   outline=C_ACCENT)
        draw.rectangle([68, 80, 118, 96], fill=(80,0,0),   outline=C_DANGER)
        draw.text((15, 83), "JOY=JA",  font=self.f_small, fill=C_ACCENT)
        draw.text((75, 83), "KEY2=NEE", font=self.f_small, fill=C_DANGER)
        self.draw_footer(draw, "Bevestig actie")
        self._send(img)

    def draw_splash(self):
        img, draw = self._new_canvas()
        # Border
        draw.rectangle([1,1,LCD_WIDTH-2,LCD_HEIGHT-2], outline=C_ACCENT, width=1)
        draw.rectangle([3,3,LCD_WIDTH-4,LCD_HEIGHT-4], outline=C_ACCENT2, width=1)
        # ASCII skull
        skull = [
            " .--.  ",
            " |o o| ",
            " | ^ | ",
            "  \\_/  ",
        ]
        y = 14
        for line in skull:
            tw, _ = self._text_size(draw, line, self.f_menu)
            draw.text(((LCD_WIDTH-tw)//2, y), line, font=self.f_menu, fill=C_ACCENT)
            y += 11
        draw.text((10, 62), "PENTEST DEVICE", font=self.f_title, fill=C_ACCENT)
        draw.text((18, 74), "RPi Zero 2W", font=self.f_small, fill=C_ACCENT2)
        draw.line([10,84,118,84], fill=C_BORDER)
        ip = get_ip()
        draw.text((4, 88), f"IP: {ip}", font=self.f_small, fill=C_DIM)
        ifaces = get_interfaces()
        draw.text((4, 98), f"IF: {', '.join(ifaces[:2])}", font=self.f_small, fill=C_DIM)
        draw.text((4, 108), "Alleen eigen netwerk!", font=self.f_small, fill=C_WARN)
        draw.text((20,118), "JOY = start", font=self.f_small, fill=C_DIM)
        self._send(img)

    def draw_scan_result(self, title, networks):
        """Toon gescande WiFi netwerken"""
        img, draw = self._new_canvas()
        self.draw_header(draw, title)
        if not networks:
            draw.text((4, 40), "Geen netwerken", font=self.f_menu, fill=C_WARN)
            draw.text((4, 55), "gevonden...", font=self.f_menu, fill=C_DIM)
        else:
            y = 24
            for net in networks[:6]:
                draw.text((4, y), truncate(net, 19), font=self.f_small, fill=C_TEXT)
                y += 12
        self.draw_footer(draw, "KEY2=terug")
        self._send(img)

    def _send(self, img):
        if HARDWARE and self.lcd:
            self.lcd.show_image(img)
        else:
            pass  # Simulatiemodus: geen output

# ═════════════════════════════════════════════════════════════════════════════
#  HOOFD APPLICATIE
# ═════════════════════════════════════════════════════════════════════════════

class PentestMenu:
    def __init__(self):
        self.pm      = ProcManager()
        self.buttons = ButtonHandler()
        self.lcd     = LCD_Driver() if HARDWARE else None
        self.rend    = Renderer(self.lcd)

        # App state
        self.running          = True
        self.screen           = "splash"
        self.menu_sel         = 0
        self.menu_scroll      = 0
        self.sub_sel          = 0
        self.sub_scroll       = 0
        self.current_menu     = "main"
        self.action_lines     = []
        self.action_title     = ""
        self.confirm_action   = None
        self.confirm_title    = ""
        self.confirm_q        = ""
        self.confirm_sub      = ""
        self.wifi_iface       = "wlan1"
        self.mon_iface        = "wlan1mon"
        self.target_bssid     = ""
        self.target_ch        = "6"
        self.blink_state      = False

        # Status tracking
        self.status = {
            "monitor":  False,
            "handshake": False,
            "deauth":   False,
            "beacon":   False,
            "portal":   False,
            "ble":      False,
        }

        # Hoofd menu items: (icon, label, dynamische state func)
        self.main_items = [
            ("W", "WiFi Tools",    self._state_wifi),
            ("B", "BLE Sniffing",  self._state_ble),
            ("P", "Evil Portal",   self._state_portal),
            ("S", "Systeem Info",  None),
            ("X", "Stop Alles",    None),
            ("!", "Afsluiten",     None),
        ]

        # Sub-menu's
        self.sub_menus = {
            "WiFi Tools": [
                ("~", "Monitor AAN",       "wifi_mon_on"),
                ("~", "Monitor UIT",       "wifi_mon_off"),
                ("-", "Scan Netwerken",    "wifi_scan"),
                ("H", "Handshake Capture", "handshake"),
                ("D", "Deauth Attack",     "deauth_start"),
                ("d", "Deauth STOP",       "deauth_stop"),
                ("*", "Beacon Spam AAN",   "beacon_start"),
                ("*", "Beacon Spam UIT",   "beacon_stop"),
                ("E", "EAPOL Capture",     "eapol"),
                ("<", "TERUG",             "back"),
            ],
            "BLE Sniffing": [
                (">", "BLE Scan START",   "ble_start"),
                (">", "BLE Scan STOP",    "ble_stop"),
                ("L", "Toon Gevonden",    "ble_show"),
                ("<", "TERUG",            "back"),
            ],
            "Evil Portal": [
                ("+", "Portal START",     "portal_start"),
                ("-", "Portal STOP",      "portal_stop"),
                ("?", "Toon Status",      "portal_status"),
                ("<", "TERUG",            "back"),
            ],
            "Systeem Info": [
                ("i", "IP Adres",         "sys_ip"),
                ("m", "Geheugen",         "sys_mem"),
                ("c", "CPU Temp",         "sys_temp"),
                ("I", "Interfaces",       "sys_ifaces"),
                ("<", "TERUG",            "back"),
            ],
        }

        self.ble_found = []

    # ── State helpers ─────────────────────────────────────────────────────────
    def _state_wifi(self):
        return "ON" if self.status["monitor"] else "OFF"
    def _state_ble(self):
        return "ON" if self.status["ble"] else ""
    def _state_portal(self):
        return "ON" if self.status["portal"] else ""

    def _item_state(self, item):
        icon, label, state_fn = item
        if callable(state_fn):
            return state_fn()
        return ""

    def _get_mon_iface(self):
        ifaces = get_interfaces()
        for i in ifaces:
            if "mon" in i:
                return i
        return self.mon_iface

    # ── Acties ────────────────────────────────────────────────────────────────

    def _action_wifi_mon_on(self):
        self.action_title = "Monitor Mode"
        self.action_lines = ["[>>] Monitor aanzetten..."]
        self._show_action()
        out = self.pm.run_output("sudo airmon-ng check kill 2>&1")
        out2= self.pm.run_output(f"sudo airmon-ng start {self.wifi_iface} 2>&1")
        mon = self._get_mon_iface()
        if mon:
            self.status["monitor"] = True
            self.mon_iface = mon
            self.action_lines += [f"[OK] Gestart: {mon}", "[OK] Klaar voor gebruik"]
        else:
            self.action_lines += ["[!!] Mislukt!", "[**] Check wlan1 aanwezig?"]
        self._show_action()

    def _action_wifi_mon_off(self):
        self.action_title = "Monitor UIT"
        self.action_lines = ["[>>] Stoppen..."]
        self._show_action()
        mon = self._get_mon_iface()
        self.pm.run_output(f"sudo airmon-ng stop {mon}")
        self.pm.run_output("sudo systemctl restart networking 2>/dev/null")
        self.status["monitor"] = False
        self.action_lines += ["[OK] Monitor gestopt", "[OK] WiFi hersteld"]
        self._show_action()

    def _action_wifi_scan(self):
        self.action_title = "WiFi Scan"
        self.action_lines = ["[>>] Scannen (10s)..."]
        self._show_action()
        mon = self._get_mon_iface() if self.status["monitor"] else self.wifi_iface
        out = self.pm.run_output(
            f"sudo timeout 10 airodump-ng --output-format csv "
            f"-w /tmp/scan_out {mon} 2>/dev/null; "
            f"cat /tmp/scan_out-01.csv 2>/dev/null | head -20"
        )
        networks = []
        for line in out.split('\n'):
            parts = line.split(',')
            if len(parts) > 13 and len(parts[0].strip()) == 17:
                ssid = parts[13].strip()
                ch   = parts[3].strip()
                if ssid:
                    networks.append(f"ch{ch} {ssid}")
        if networks:
            self.action_lines = [f"[OK] {len(networks)} gevonden:"] + networks[:6]
        else:
            self.action_lines = ["[!!] Geen netwerken", "[**] Monitor mode aan?"]
        self._show_action()

    def _action_handshake(self):
        self.action_title = "Handshake"
        if not self.status["monitor"]:
            self.action_lines = ["[!!] Monitor mode uit!", "[**] Zet eerst monitor aan"]
            self._show_action(); return
        self.action_lines = [
            "[>>] Capture gestart",
            f"[>>] Iface: {self.mon_iface}",
            "[>>] Wachten op client...",
            "[**] KEY2 = stoppen",
            "[OK] Opgeslagen in:",
            "     /tmp/capture",
        ]
        self.pm.start("handshake",
            f"sudo airodump-ng -w /tmp/capture --output-format pcap {self.mon_iface}")
        self.status["handshake"] = True
        self._show_action()

    def _action_deauth_start(self):
        self.action_title = "Deauth"
        if not self.status["monitor"]:
            self.action_lines = ["[!!] Monitor mode uit!"]
            self._show_action(); return
        # Gebruik broadcast als geen target ingesteld
        bssid = self.target_bssid if self.target_bssid else "FF:FF:FF:FF:FF:FF"
        self.pm.start("deauth",
            f"sudo aireplay-ng --deauth 0 -a {bssid} {self.mon_iface}")
        self.status["deauth"] = True
        self.action_lines = [
            "[OK] Deauth gestart!",
            f"[>>] Target: {bssid}",
            f"[>>] Iface:  {self.mon_iface}",
            "[**] KEY2 = stoppen",
            "",
            "[**] Alleen eigen AP!",
        ]
        self._show_action()

    def _action_deauth_stop(self):
        self.pm.stop("deauth")
        self.status["deauth"] = False
        self.action_title = "Deauth"
        self.action_lines = ["[OK] Deauth gestopt"]
        self._show_action()

    def _action_beacon_start(self):
        self.action_title = "Beacon Spam"
        if not self.status["monitor"]:
            self.action_lines = ["[!!] Monitor mode uit!"]
            self._show_action(); return
        # Genereer random SSID lijst
        ssids = ["FreeWiFi","McDonald's Free","Telenet-Guest",
                 "Proximus_Public","Airport_Free","Hotel_Guest",
                 "LinksysXXXXXX","NETGEAR_5G","iPhone van Jan"]
        with open("/tmp/ssid_list.txt","w") as f:
            f.write('\n'.join(ssids))
        self.pm.start("beacon",
            f"sudo mdk4 {self.mon_iface} b -f /tmp/ssid_list.txt -c 6")
        self.status["beacon"] = True
        self.action_lines = [
            "[OK] Beacon spam AAN!",
            f"[>>] Iface: {self.mon_iface}",
            f"[>>] SSIDs: {len(ssids)}",
            "[**] KEY2 = stoppen",
        ]
        self._show_action()

    def _action_beacon_stop(self):
        self.pm.stop("beacon")
        self.status["beacon"] = False
        self.action_title = "Beacon Spam"
        self.action_lines = ["[OK] Beacon spam gestopt"]
        self._show_action()

    def _action_eapol(self):
        self.action_title = "EAPOL/PMKID"
        if not self.status["monitor"]:
            self.action_lines = ["[!!] Monitor mode uit!"]
            self._show_action(); return
        self.pm.start("eapol",
            f"sudo hcxdumptool -i {self.mon_iface} "
            f"-o /tmp/eapol_capture.pcapng --enable_status=1")
        self.action_lines = [
            "[OK] EAPOL capture AAN",
            f"[>>] Iface: {self.mon_iface}",
            "[>>] Output: /tmp/eapol_",
            "     capture.pcapng",
            "[**] KEY2 = stoppen",
            "[>>] Converteer met:",
            "  hcxpcapngtool",
        ]
        self.status["handshake"] = True
        self._show_action()

    def _action_ble_start(self):
        self.action_title = "BLE Sniffing"
        self.ble_found = []
        self.pm.start("ble",
            "sudo bettercap -eval 'ble.recon on; events.stream on' 2>&1 | "
            "tee /tmp/ble_output.txt")
        self.status["ble"] = True
        self.action_lines = [
            "[OK] BLE scan gestart!",
            "[>>] Via bettercap",
            "[>>] Output: /tmp/",
            "     ble_output.txt",
            "[**] KEY2 = stoppen",
        ]
        self._show_action()

    def _action_ble_stop(self):
        self.pm.stop("ble")
        self.status["ble"] = False
        self.action_title = "BLE Scan"
        self.action_lines = ["[OK] BLE scan gestopt"]
        # Probeer gevonden devices te lezen
        try:
            with open("/tmp/ble_output.txt") as f:
                content = f.read()
            macs = re.findall(r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})', content)
            unique = list(set(macs))[:5]
            if unique:
                self.action_lines += [f"[>>] {len(unique)} devices:"] + unique
            self.ble_found = unique
        except Exception:
            pass
        self._show_action()

    def _action_ble_show(self):
        self.action_title = "BLE Gevonden"
        if not self.ble_found:
            try:
                with open("/tmp/ble_output.txt") as f:
                    content = f.read()
                macs = re.findall(r'([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})', content)
                self.ble_found = list(set(macs))[:5]
            except Exception:
                pass
        if self.ble_found:
            self.action_lines = [f"[OK] {len(self.ble_found)} devices:"] + self.ble_found
        else:
            self.action_lines = ["[**] Nog niets gevonden", "[>>] Scan eerst starten"]
        self._show_action()

    def _action_portal_start(self):
        self.action_title = "Evil Portal"
        # Maak simpele portal pagina
        portal_html = """<!DOCTYPE html><html><head>
<title>WiFi Login</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<style>body{font-family:Arial;background:#f0f0f0;display:flex;
justify-content:center;align-items:center;height:100vh;margin:0}
.box{background:white;padding:30px;border-radius:8px;
box-shadow:0 2px 10px rgba(0,0,0,.2);min-width:280px}
h2{margin:0 0 20px;color:#333}
input{width:100%;padding:10px;margin:8px 0;
border:1px solid #ddd;border-radius:4px;box-sizing:border-box}
button{width:100%;padding:12px;background:#0078d4;
color:white;border:none;border-radius:4px;cursor:pointer;font-size:16px}
</style></head><body><div class="box">
<h2>&#x1F4F6; WiFi Toegang</h2>
<p>Voer uw gegevens in om verbinding te maken.</p>
<input type="text" placeholder="Gebruikersnaam" id="u">
<input type="password" placeholder="Wachtwoord" id="p">
<button onclick="alert('Verbinden...')">Verbinden</button>
</div></body></html>"""
        os.makedirs("/tmp/portal", exist_ok=True)
        with open("/tmp/portal/index.html", "w") as f:
            f.write(portal_html)

        # Stel wlan1 IP in
        self.pm.run_output(f"sudo ip addr add 192.168.10.1/24 dev {self.wifi_iface} 2>/dev/null")

        # hostapd config
        hostapd_conf = f"""interface={self.wifi_iface}
driver=nl80211
ssid=FreeWiFi_Public
hw_mode=g
channel=6
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
"""
        with open("/tmp/hostapd.conf","w") as f:
            f.write(hostapd_conf)

        # dnsmasq config
        dnsmasq_conf = f"""interface={self.wifi_iface}
dhcp-range=192.168.10.10,192.168.10.100,255.255.255.0,12h
dhcp-option=3,192.168.10.1
dhcp-option=6,192.168.10.1
address=/#/192.168.10.1
"""
        with open("/tmp/dnsmasq_portal.conf","w") as f:
            f.write(dnsmasq_conf)

        self.pm.start("hostapd",  "sudo hostapd /tmp/hostapd.conf")
        time.sleep(1)
        self.pm.start("dnsmasq",  "sudo dnsmasq -C /tmp/dnsmasq_portal.conf --no-daemon")
        self.pm.start("webserver","cd /tmp/portal && sudo python3 -m http.server 80")

        self.status["portal"] = True
        self.action_lines = [
            "[OK] Evil Portal AAN!",
            "[>>] SSID: FreeWiFi_Public",
            "[>>] IP:   192.168.10.1",
            "[>>] Web:  :80",
            "[**] KEY2 = stoppen",
            "[**] Alleen eigen test!",
        ]
        self._show_action()

    def _action_portal_stop(self):
        for p in ["hostapd","dnsmasq","webserver"]:
            self.pm.stop(p)
        self.status["portal"] = False
        self.action_title = "Evil Portal"
        self.action_lines = ["[OK] Portal gestopt", "[OK] AP uitgeschakeld"]
        self._show_action()

    def _action_portal_status(self):
        self.action_title = "Portal Status"
        running = self.status["portal"]
        self.action_lines = [
            f"[{'OK' if running else '!!'}] Status: {'AAN' if running else 'UIT'}",
            f"[>>] SSID: FreeWiFi_Public",
            f"[>>] IP:   192.168.10.1",
            f"[>>] hostapd: {'ja' if self.pm.is_running('hostapd') else 'nee'}",
            f"[>>] dnsmasq: {'ja' if self.pm.is_running('dnsmasq') else 'nee'}",
        ]
        self._show_action()

    def _action_sys_ip(self):
        self.action_title = "IP Adres"
        ip = get_ip()
        ifaces = get_interfaces()
        self.action_lines = [
            f"[OK] IP: {ip}",
            "[>>] Interfaces:",
        ] + [f"     {i}" for i in ifaces]
        self._show_action()

    def _action_sys_mem(self):
        self.action_title = "Geheugen"
        out = self.pm.run_output("free -h | head -3")
        lines = ["[>>] " + l for l in out.split('\n') if l.strip()]
        self.action_lines = lines or ["[!!] Fout"]
        self._show_action()

    def _action_sys_temp(self):
        self.action_title = "CPU Temp"
        temp = self.pm.run_output("vcgencmd measure_temp 2>/dev/null || cat /sys/class/thermal/thermal_zone0/temp")
        if temp.isdigit():
            temp = f"{int(temp)/1000:.1f}°C"
        cpu  = self.pm.run_output("top -bn1 | grep 'Cpu' | awk '{print $2}'")
        self.action_lines = [
            f"[OK] Temp: {temp}",
            f"[>>] CPU:  {cpu}%",
        ]
        self._show_action()

    def _action_sys_ifaces(self):
        self.action_title = "Interfaces"
        out = self.pm.run_output("iwconfig 2>/dev/null | grep -E '^\\w'")
        lines = [l.split()[0] for l in out.split('\n') if l.strip()]
        self.action_lines = ["[>>] WiFi interfaces:"] + [f"     {l}" for l in lines]
        self._show_action()

    def _action_stop_all(self):
        self.action_title = "Stop Alles"
        self.action_lines = ["[>>] Alles stoppen..."]
        self._show_action()
        self.pm.stop_all()
        for k in self.status:
            self.status[k] = False
        self.pm.run_output(f"sudo airmon-ng stop {self.mon_iface} 2>/dev/null")
        self.pm.run_output("sudo systemctl restart networking 2>/dev/null")
        self.action_lines = [
            "[OK] Alle processen gestopt",
            "[OK] Monitor mode uit",
            "[OK] Netwerk hersteld",
        ]
        self._show_action()

    # ── Actie dispatcher ──────────────────────────────────────────────────────
    ACTION_MAP = {
        "wifi_mon_on":   "_action_wifi_mon_on",
        "wifi_mon_off":  "_action_wifi_mon_off",
        "wifi_scan":     "_action_wifi_scan",
        "handshake":     "_action_handshake",
        "deauth_start":  "_action_deauth_start",
        "deauth_stop":   "_action_deauth_stop",
        "beacon_start":  "_action_beacon_start",
        "beacon_stop":   "_action_beacon_stop",
        "eapol":         "_action_eapol",
        "ble_start":     "_action_ble_start",
        "ble_stop":      "_action_ble_stop",
        "ble_show":      "_action_ble_show",
        "portal_start":  "_action_portal_start",
        "portal_stop":   "_action_portal_stop",
        "portal_status": "_action_portal_status",
        "sys_ip":        "_action_sys_ip",
        "sys_mem":       "_action_sys_mem",
        "sys_temp":      "_action_sys_temp",
        "sys_ifaces":    "_action_sys_ifaces",
    }

    # ── Scherm helpers ────────────────────────────────────────────────────────
    def _show_action(self):
        self.rend.draw_action(
            self.action_title,
            self.action_lines,
            blink_dot=True
        )
        time.sleep(0.1)

    def _build_main_items(self):
        result = []
        for icon, label, state_fn in self.main_items:
            state = state_fn() if callable(state_fn) else ""
            result.append((icon, label, state))
        return result

    def _build_sub_items(self, menu_name):
        raw = self.sub_menus.get(menu_name, [])
        return [(icon, label, "") for icon, label, _ in raw]

    # ── Hoofd loop ────────────────────────────────────────────────────────────
    def run(self):
        # Splash
        self.rend.draw_splash()
        # Wacht op knop of timeout
        t = time.time()
        while time.time() - t < 3:
            if self.buttons.get():
                break
            time.sleep(0.05)

        self.screen       = "main"
        self.current_menu = "main"
        self.menu_sel     = 0
        self.menu_scroll  = 0

        blink_timer = time.time()

        try:
            while self.running:
                self.blink_state = (time.time() % 1) > 0.5

                ev = self.buttons.get()

                # ── SPLASH ────────────────────────────────────────────────
                if self.screen == "splash":
                    if ev:
                        self.screen = "main"

                # ── HOOFD MENU ────────────────────────────────────────────
                elif self.screen == "main":
                    items = self._build_main_items()
                    VISIBLE = 6

                    if ev == "UP":
                        self.menu_sel = max(0, self.menu_sel - 1)
                        if self.menu_sel < self.menu_scroll:
                            self.menu_scroll = self.menu_sel
                    elif ev == "DOWN":
                        self.menu_sel = min(len(items)-1, self.menu_sel + 1)
                        if self.menu_sel >= self.menu_scroll + VISIBLE:
                            self.menu_scroll += 1
                    elif ev in ("SELECT", "KEY1"):
                        label = self.main_items[self.menu_sel][1]
                        if label == "Afsluiten":
                            self.confirm_title  = "Afsluiten"
                            self.confirm_q      = "Pi afsluiten?"
                            self.confirm_sub    = "Alles wordt gestopt"
                            self.confirm_action = "shutdown"
                            self.screen         = "confirm"
                        elif label == "Stop Alles":
                            self.confirm_title  = "Stop Alles"
                            self.confirm_q      = "Alles stoppen?"
                            self.confirm_sub    = "Monitor, portal, BLE..."
                            self.confirm_action = "stop_all"
                            self.screen         = "confirm"
                        elif label in self.sub_menus:
                            self.current_menu = label
                            self.sub_sel      = 0
                            self.sub_scroll   = 0
                            self.screen       = "sub"
                        elif label == "Systeem Info":
                            self.current_menu = "Systeem Info"
                            self.sub_sel      = 0
                            self.sub_scroll   = 0
                            self.screen       = "sub"

                    # Render
                    status_str = f"{'MON' if self.status['monitor'] else '   '} {'BLE' if self.status['ble'] else '   '} {'PTL' if self.status['portal'] else '   '}"
                    self.rend.draw_menu("[ PENTEST ]", items, self.menu_sel,
                                        status=status_str, scroll_offset=self.menu_scroll)

                # ── SUB MENU ──────────────────────────────────────────────
                elif self.screen == "sub":
                    raw_items = self.sub_menus.get(self.current_menu, [])
                    items     = [(i, l, "") for i,l,_ in raw_items]
                    VISIBLE   = 6

                    if ev == "UP":
                        self.sub_sel = max(0, self.sub_sel - 1)
                        if self.sub_sel < self.sub_scroll:
                            self.sub_scroll = self.sub_sel
                    elif ev == "DOWN":
                        self.sub_sel = min(len(items)-1, self.sub_sel + 1)
                        if self.sub_sel >= self.sub_scroll + VISIBLE:
                            self.sub_scroll += 1
                    elif ev in ("SELECT", "KEY1"):
                        _, label, action = raw_items[self.sub_sel]
                        if action == "back":
                            self.screen = "main"
                        elif action in self.ACTION_MAP:
                            meth = getattr(self, self.ACTION_MAP[action])
                            self.screen = "action"
                            meth()
                    elif ev == "BACK":
                        self.screen = "main"

                    if self.screen == "sub":
                        self.rend.draw_menu(
                            self.current_menu[:12], items, self.sub_sel,
                            scroll_offset=self.sub_scroll
                        )

                # ── ACTIE SCHERM ──────────────────────────────────────────
                elif self.screen == "action":
                    if ev == "BACK":
                        self.pm.stop("handshake")
                        self.pm.stop("eapol")
                        self.status["handshake"] = False
                        self.screen = "sub"
                    else:
                        # Herrender met blink
                        self.rend.draw_action(
                            self.action_title,
                            self.action_lines,
                            blink_dot=self.blink_state
                        )

                # ── BEVESTIGING ───────────────────────────────────────────
                elif self.screen == "confirm":
                    self.rend.draw_confirm(self.confirm_title,
                                           self.confirm_q, self.confirm_sub)
                    if ev in ("SELECT", "KEY1"):
                        if self.confirm_action == "shutdown":
                            self._shutdown()
                        elif self.confirm_action == "stop_all":
                            self._action_stop_all()
                            self.screen = "action"
                    elif ev == "BACK":
                        self.screen = "main"

                time.sleep(0.05)

        except KeyboardInterrupt:
            pass
        finally:
            self.cleanup()

    def _shutdown(self):
        self.action_title = "Afsluiten"
        self.action_lines = ["[>>] Stoppen...", "[>>] Tot ziens!"]
        self._show_action()
        self.pm.stop_all()
        time.sleep(1)
        if HARDWARE and self.lcd:
            self.lcd.clear()
            self.lcd.backlight(False)
        os.system("sudo shutdown -h now")
        sys.exit(0)

    def cleanup(self):
        self.pm.stop_all()
        if HARDWARE:
            if self.lcd:
                self.lcd.clear((0,0,0))
                self.lcd.backlight(False)
                self.lcd.cleanup()
            self.buttons.cleanup()
        print("\n[OK] Afgesloten.")

# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if os.geteuid() != 0:
        print("⚠️  Dit programma heeft root nodig. Start met: sudo python3 pentest_menu.py")
        sys.exit(1)
    app = PentestMenu()
    app.run()
