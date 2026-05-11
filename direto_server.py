"""
Elite Direto XR - Local Web Controller Backend
================================================
Install:  pip install fastapi uvicorn bleak websockets httpx garminconnect cloudscraper
Run:      python direto_server.py
Then open: http://localhost:8000

Garmin setup: click "Log in to Garmin" in the Upload tab.
Session token cached in garmin_session/ — password never stored on disk.
"""

import asyncio, json, math, random, struct, time, pathlib, io
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx, uvicorn
import logging
from bleak import BleakClient, BleakScanner
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# ANT+ (optional — only used if dongle present)
try:
    from openant.easy.node import Node
    from openant.devices.fitness_equipment import FitnessEquipment
    from openant.devices import ANTPLUS_NETWORK_KEY
    ANT_AVAILABLE = True
except ImportError:
    ANT_AVAILABLE = False
    logger_ant = None

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("direto")
# Quieten noisy libraries
logging.getLogger("bleak").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openant.base.ant").setLevel(logging.ERROR)   # suppress USB timeout spam
logging.getLogger("openant.base.driver").setLevel(logging.WARNING)
logging.getLogger("openant.easy.filter").setLevel(logging.WARNING)
logging.getLogger("openant.easy.node").setLevel(logging.WARNING)
logging.getLogger("openant.devices").setLevel(logging.WARNING)

BASE          = pathlib.Path(__file__).parent
PLANS_FILE    = BASE / "plans.json"
CONFIG_FILE   = BASE / "strava_config.json"
TOKEN_FILE    = BASE / "strava_token.json"
GARMIN_DIR    = BASE / "garmin_session"
SETTINGS_FILE = BASE / "settings.json"

# ── BLE UUIDs ──────────────────────────────────────────────────────────────────
FTMS_CONTROL_POINT_UUID = "00002ad9-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA_UUID   = "00002ad2-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID     = "00002a37-0000-1000-8000-00805f9b34fb"   # standard GATT HR
HR_SERVICE_UUID         = "0000180d-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_UUID      = "00002a63-0000-1000-8000-00805f9b34fb"   # Cycling Power Measurement
CYCLING_POWER_SVC_UUID  = "00001818-0000-1000-8000-00805f9b34fb"   # Cycling Power Service

OP_REQUEST_CONTROL  = 0x00
OP_RESET            = 0x01
OP_SET_TARGET_POWER = 0x05
OP_SET_RESISTANCE   = 0x04
OP_START_RESUME          = 0x07
OP_STOP_PAUSE            = 0x08
OP_SET_INDOOR_SIMULATION = 0x11  # Indoor Bike Simulation (slope)

STRAVA_AUTH_URL   = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL  = "https://www.strava.com/oauth/token"
STRAVA_UPLOAD_URL = "https://www.strava.com/api/v3/uploads"


# ── Persist helpers ────────────────────────────────────────────────────────────

def load_plans() -> dict:
    if PLANS_FILE.exists(): return json.loads(PLANS_FILE.read_text())
    return {}

def save_plans(p: dict): PLANS_FILE.write_text(json.dumps(p, indent=2))

def load_config() -> dict:
    if CONFIG_FILE.exists(): return json.loads(CONFIG_FILE.read_text())
    default = {"client_id": "", "client_secret": ""}
    CONFIG_FILE.write_text(json.dumps(default, indent=2))
    return default

def load_token() -> Optional[dict]:
    return json.loads(TOKEN_FILE.read_text()) if TOKEN_FILE.exists() else None

def save_token(t: dict): TOKEN_FILE.write_text(json.dumps(t, indent=2))

def load_settings() -> dict:
    if SETTINGS_FILE.exists(): return json.loads(SETTINGS_FILE.read_text())
    return {"auto_upload": "none", "default_name_template": "Indoor Ride"}

def save_settings(s: dict): SETTINGS_FILE.write_text(json.dumps(s, indent=2))


# ── FIT file builder ───────────────────────────────────────────────────────────

def _fit_crc(data: bytes) -> int:
    tbl = [0x0000,0xCC01,0xD801,0x1400,0xF001,0x3C00,0x2800,0xE401,
           0xA001,0x6C00,0x7800,0xB401,0x5000,0x9C01,0x8801,0x4400]
    crc = 0
    for b in data:
        t = tbl[crc & 0xF]; crc = (crc >> 4) & 0x0FFF; crc ^= t ^ tbl[b & 0xF]
        t = tbl[crc & 0xF]; crc = (crc >> 4) & 0x0FFF; crc ^= t ^ tbl[(b >> 4) & 0xF]
    return crc

def build_fit(history: list, session_start: float) -> bytes:
    """
    Build a FIT activity file using fit-tool — a proper SDK-based library.
    This replaces the hand-crafted byte approach which had field number errors
    that caused Garmin to silently reject uploads.
    """
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.activity_message import ActivityMessage
    from fit_tool.profile.messages.session_message import SessionMessage
    from fit_tool.profile.messages.lap_message import LapMessage
    from fit_tool.profile.messages.record_message import RecordMessage
    from fit_tool.profile.messages.event_message import EventMessage
    from fit_tool.profile.profile_type import (
        FileType, Manufacturer, Sport, SubSport, Event, EventType, Activity
    )

    has_hr      = any(pt.get("hr", 0) > 0 for pt in history)
    total_s     = int(history[-1]["t"] - history[0]["t"]) if len(history) > 1 else 0
    avg_power   = int(sum(p.get("power",   0) for p in history) / max(len(history), 1))
    avg_cad     = int(sum(p.get("cadence", 0) for p in history) / max(len(history), 1))
    avg_spd_ms  = sum(p.get("speed", 0) / 3.6 for p in history) / max(len(history), 1)
    avg_hr      = int(sum(p.get("hr", 0) for p in history) / max(len(history), 1)) if has_hr else 0
    max_hr      = int(max((p.get("hr", 0) for p in history), default=0))

    # Convert session_start to milliseconds (fit-tool uses ms)
    start_ms = int(session_start * 1000)
    end_ms   = start_ms + total_s * 1000

    builder = FitFileBuilder(auto_define=True, min_string_size=50)

    # file_id — Wahoo Fitness manufacturer accepted by Garmin for trainer files
    fid = FileIdMessage()
    fid.type          = FileType.ACTIVITY
    fid.manufacturer  = Manufacturer.WAHOO_FITNESS
    fid.product       = 4894   # KICKR
    fid.serial_number = 1
    fid.time_created  = start_ms
    builder.add(fid)

    # event: start
    ev_start            = EventMessage()
    ev_start.event      = Event.TIMER
    ev_start.event_type = EventType.START
    ev_start.timestamp  = start_ms
    builder.add(ev_start)

    # records
    for pt in history:
        rec           = RecordMessage()
        rec.timestamp = start_ms + int(pt["t"] * 1000)
        rec.power     = max(0, min(65534, int(pt.get("power",   0))))
        rec.cadence   = max(0, min(254,   int(pt.get("cadence", 0))))
        rec.speed     = max(0.0, pt.get("speed", 0) / 3.6)  # m/s
        if has_hr:
            rec.heart_rate = max(0, min(254, int(pt.get("hr", 0))))
        builder.add(rec)

    # event: stop
    ev_stop            = EventMessage()
    ev_stop.event      = Event.TIMER
    ev_stop.event_type = EventType.STOP_ALL
    ev_stop.timestamp  = end_ms
    builder.add(ev_stop)

    # lap
    lap                   = LapMessage()
    lap.timestamp         = end_ms
    lap.start_time        = start_ms
    lap.total_elapsed_time = total_s
    lap.total_timer_time   = total_s
    lap.total_distance    = avg_spd_ms * total_s  # metres
    lap.avg_speed         = avg_spd_ms
    lap.avg_power         = avg_power
    lap.avg_cadence       = avg_cad
    lap.sport             = Sport.CYCLING
    lap.sub_sport         = SubSport.INDOOR_CYCLING
    if has_hr:
        lap.avg_heart_rate = avg_hr
        lap.max_heart_rate = max_hr
    builder.add(lap)

    # session
    ses                    = SessionMessage()
    ses.timestamp          = end_ms
    ses.start_time         = start_ms
    ses.total_elapsed_time = total_s
    ses.total_timer_time   = total_s
    ses.total_distance     = avg_spd_ms * total_s
    ses.avg_speed          = avg_spd_ms
    ses.avg_power          = avg_power
    ses.avg_cadence        = avg_cad
    ses.sport              = Sport.CYCLING
    ses.sub_sport          = SubSport.INDOOR_CYCLING
    ses.first_lap_index    = 0
    ses.num_laps           = 1
    if has_hr:
        ses.avg_heart_rate = avg_hr
        ses.max_heart_rate = max_hr
    builder.add(ses)

    # activity
    act               = ActivityMessage()
    act.timestamp     = end_ms
    act.num_sessions  = 1
    act.type          = Activity.MANUAL
    # local_timestamp tells Garmin the local wall-clock time so it displays correctly.
    # Must be in FIT epoch seconds (not Unix seconds, not ms).
    # time.timezone is negative for east-of-UTC (e.g. UTC+1 BST gives -3600).
    FIT_EPOCH         = 631065600
    local_offset_sec  = -time.timezone  # e.g. BST = +3600, UTC = 0
    act.local_timestamp = int(session_start + total_s) - FIT_EPOCH + local_offset_sec
    builder.add(act)

    return builder.build().to_bytes()


# ── Fake ride generator ────────────────────────────────────────────────────────

def generate_fake_ride(duration_s: int = 1800, ftp: int = 220) -> tuple[list, float]:
    """
    Generate a realistic-looking 30-min indoor ride with warmup, intervals, cooldown.
    Returns (history, session_start).
    """
    session_start = time.time() - duration_s
    history = []

    # Ride profile: list of (end_t, target_power_fraction, base_hr)
    warmup_end   = int(duration_s * 0.15)
    block1_end   = int(duration_s * 0.30)
    recover1_end = int(duration_s * 0.40)
    block2_end   = int(duration_s * 0.55)
    recover2_end = int(duration_s * 0.65)
    block3_end   = int(duration_s * 0.80)
    cooldown_end = duration_s

    def profile(t):
        if   t < warmup_end:   return 0.55 + 0.30 * (t / warmup_end), 120 + int(20 * t / warmup_end)
        elif t < block1_end:   return 0.88, 148
        elif t < recover1_end: return 0.55, 132
        elif t < block2_end:   return 0.95, 158
        elif t < recover2_end: return 0.52, 130
        elif t < block3_end:   return 0.90, 153
        else:                   return 0.50 - 0.20*((t-block3_end)/(cooldown_end-block3_end)), 125

    hr_current = 100.0
    speed_current = 20.0

    for t in range(0, duration_s + 1, 2):  # 2-second samples, inclusive of end
        frac, target_hr = profile(t)
        target_power = frac * ftp

        # Add natural variation
        noise_p = random.gauss(0, ftp * 0.03)
        noise_c = random.gauss(0, 2)
        noise_s = random.gauss(0, 0.3)

        power   = max(0, round(target_power + noise_p))
        cadence = max(0, round(85 + noise_c + (frac - 0.7) * 10))
        speed   = max(0, round(speed_current + noise_s, 1))

        # HR lags power with slow response
        hr_lag  = 0.05
        hr_current += hr_lag * (target_hr - hr_current) + random.gauss(0, 1)
        hr = max(60, min(200, round(hr_current)))

        # Speed drifts toward a power-based estimate
        target_speed = 15 + frac * 18
        speed_current += 0.1 * (target_speed - speed_current)

        history.append({"t": float(t), "power": power, "cadence": cadence,
                        "speed": round(speed_current, 1), "hr": hr})

    return history, session_start


# ── State ──────────────────────────────────────────────────────────────────────

class TrainerState:
    def __init__(self):
        self.connected      = False
        self.address        = None
        self.name           = None
        self.client         = None
        self.mode           = "free"
        self.target_power   = 0
        self.resistance     = 0
        self.latest         = {"power_w": 0, "cadence_rpm": 0, "speed_kmh": 0, "hr": 0}
        self.history        = []
        self.session_start  = None
        self.interval_plan  = []
        self.interval_index = 0
        self.interval_elapsed = 0
        self.interval_task  = None
        # HR monitor
        self.hr_connected   = False
        self.hr_address     = None
        self.hr_name        = None
        self.hr_client      = None
        self.current_hr     = 0
        self.hr_last_seen   = 0.0  # timestamp of last HR reading
        # Power meter
        self.pm_connected   = False
        self.pm_address     = None
        self.pm_name        = None
        self.pm_client      = None
        self.pm_power       = 0    # most recent power reading (W)
        self.pm_cadence     = 0    # most recent cadence (rpm) if available
        self.use_pm         = False # True = record from power meter not trainer
        # BLE device cache — stores BLEDevice objects from scans so we can
        # connect using the object directly (avoids Windows random address rotation)
        self.scan_cache: dict = {}
        # ANT+ FE-C
        self.ant_node        = None   # openant Node
        self.ant_device      = None   # FEC channel
        self.ant_connected   = False
        self._ant_connecting = False  # guard against concurrent connect attempts
        self.ant_device_id   = 0      # 0 = wildcard (find any trainer)
        self.ant_grade       = 0.0    # current slope %
        self.ant_hr_channel  = None   # separate ANT+ HR monitor channel
        # Session recording
        self.recording      = False   # True = actively recording a session
        self.ride_name      = ""      # name to use on upload

state   = TrainerState()
clients : list[WebSocket] = []
plans   = load_plans()


# ── BLE — Trainer ──────────────────────────────────────────────────────────────

def parse_bike_data(data: bytearray) -> dict:
    """
    Parse FTMS Indoor Bike Data (0x2AD2).
    Field sizes (all skipped unless we need the value):
      speed:          uint16  (2 bytes)  0.01 km/h
      avg speed:      uint16  (2 bytes)
      inst cadence:   uint16  (2 bytes)  0.5 rpm
      avg cadence:    uint16  (2 bytes)
      total distance: uint24  (3 bytes)  metres
      resistance:     sint16  (2 bytes)
      inst power:     sint16  (2 bytes)  watts
      avg power:      sint16  (2 bytes)
      expended energy:uint24+uint16+uint8 = (5 bytes total: total kcal, per hour kcal, per min kcal)
      heart rate:     uint8   (1 byte)
      metabolic eq:   uint8   (1 byte)
      elapsed time:   uint16  (2 bytes)
      remaining time: uint16  (2 bytes)
    """
    if len(data) < 4:
        return {}
    flags  = struct.unpack_from("<H", data, 0)[0]
    offset = 2
    result = {}

    # Bit 0 = 0 → Instantaneous Speed present (uint16, 0.01 km/h)
    if not (flags & 0x0001):
        if offset + 2 <= len(data):
            result["speed_kmh"] = round(struct.unpack_from("<H", data, offset)[0] * 0.01, 1)
        offset += 2

    # Bit 1 → Average Speed (skip, uint16)
    if flags & 0x0002: offset += 2

    # Bit 2 → Instantaneous Cadence (uint16, 0.5 rpm)
    if flags & 0x0004:
        if offset + 2 <= len(data):
            result["cadence_rpm"] = round(struct.unpack_from("<H", data, offset)[0] * 0.5, 0)
        offset += 2

    # Bit 3 → Average Cadence (skip, uint16)
    if flags & 0x0008: offset += 2

    # Bit 4 → Total Distance (skip, uint24 = 3 bytes)
    if flags & 0x0010: offset += 3

    # Bit 5 → Resistance Level (skip, sint16)
    if flags & 0x0020: offset += 2

    # Bit 6 → Instantaneous Power (sint16, watts)
    if flags & 0x0040:
        if offset + 2 <= len(data):
            result["power_w"] = struct.unpack_from("<h", data, offset)[0]
        offset += 2

    # Bit 7 → Average Power (skip, sint16)
    if flags & 0x0080: offset += 2

    # Bit 8 → Expended Energy (skip, uint16 + uint16 + uint8 = 5 bytes)
    if flags & 0x0100: offset += 5

    # Bit 9 → Heart Rate (uint8)
    if flags & 0x0200:
        if offset + 1 <= len(data):
            result["hr_from_trainer"] = data[offset]
        offset += 1

    # Bit 10 → Metabolic Equivalent (skip, uint8)
    if flags & 0x0400: offset += 1

    # Bit 11 → Elapsed Time (skip, uint16)
    if flags & 0x0800: offset += 2

    # Bit 12 → Remaining Time (skip, uint16)
    if flags & 0x1000: offset += 2

    return result

async def ble_notify_handler(_sender, data: bytearray):
    parsed = parse_bike_data(data)
    state.latest.update(parsed)
    # Use trainer's built-in HR field if no HR monitor connected
    if not state.hr_connected and parsed.get("hr_from_trainer", 0) > 0:
        state.current_hr = parsed["hr_from_trainer"]
    # Clear stale HR if monitor hasn't sent data for 5 seconds
    if state.hr_last_seen > 0 and (time.time() - state.hr_last_seen) > 5:
        state.current_hr   = 0
        state.hr_last_seen = 0.0
    state.latest["hr"] = state.current_hr
    # If power meter connected and enabled, override trainer power/cadence
    if state.use_pm and state.pm_connected:
        state.latest["power_w"]    = state.pm_power
        if state.pm_cadence > 0:
            state.latest["cadence_rpm"] = state.pm_cadence
    if state.session_start:
        t = round(time.time() - state.session_start, 1)
        state.history.append({
            "t": t,
            "power":   state.latest.get("power_w", 0),
            "cadence": state.latest.get("cadence_rpm", 0),
            "speed":   state.latest.get("speed_kmh", 0),
            "hr":      state.current_hr,
        })
        if len(state.history) > 36000: state.history = state.history[-36000:]
    await broadcast({"type": "telemetry", "data": state.latest,
                     "elapsed": round(time.time() - state.session_start, 0) if state.session_start else 0,
                     "history": state.history[-120:]})

async def write_cp(payload: bytes):
    """Write to FTMS Control Point and wait for response indication."""
    if state.client and state.connected:
        await state.client.write_gatt_char(FTMS_CONTROL_POINT_UUID, payload, response=True)

async def request_ftms_control() -> bool:
    """
    Properly request FTMS control with retries.
    The Direto XR requires:
      1. Subscribe to CP notifications so it can send back the response code
      2. Small settle delay after BLE connect
      3. Send OP_REQUEST_CONTROL (0x00) and wait for 0x80 0x00 0x01 response
    Returns True if control was granted.
    """
    for attempt in range(3):
        try:
            await asyncio.sleep(0.5 + attempt * 0.5)  # settle time, grows on retry
            await write_cp(bytes([OP_REQUEST_CONTROL]))
            await asyncio.sleep(0.3)
            logger.info(f"[BLE] FTMS control requested (attempt {attempt+1})")
            return True
        except Exception as e:
            logger.warning(f"[BLE] Control request attempt {attempt+1} failed: {e}")
            if attempt == 2:
                return False
    return False


# ── BLE — Heart Rate Monitor ───────────────────────────────────────────────────

def parse_hr_measurement(data: bytearray) -> int:
    """Parse BLE Heart Rate Measurement characteristic (0x2A37)."""
    flags = data[0]
    # Bit 0: 0 = HR is uint8, 1 = HR is uint16
    if flags & 0x01:
        return struct.unpack_from("<H", data, 1)[0]
    else:
        return data[1]

async def hr_notify_handler(_sender, data: bytearray):
    hr = parse_hr_measurement(data)
    if hr > 0:
        state.current_hr   = hr
        state.hr_last_seen = time.time()
        state.latest["hr"] = hr
        await broadcast({"type": "hr_update", "hr": hr})

async def connect_hr_monitor(device, name: str):
    try:
        state.hr_client = BleakClient(device)  # device = BLEDevice or address string
        await state.hr_client.connect()
        state.hr_connected = True
        state.hr_address   = address
        state.hr_name      = name
        await state.hr_client.start_notify(HR_MEASUREMENT_UUID, hr_notify_handler)
        await broadcast({"type": "hr_connected", "name": name})
    except Exception as e:
        await broadcast({"type": "error", "msg": f"HR monitor: {e}"})

async def disconnect_hr_monitor():
    if state.hr_client and state.hr_connected:
        try:
            await state.hr_client.stop_notify(HR_MEASUREMENT_UUID)
            await state.hr_client.disconnect()
        except Exception:
            pass
    state.hr_connected = False
    state.hr_address   = None
    state.hr_name      = None
    state.hr_client    = None
    state.current_hr   = 0
    await broadcast({"type": "hr_disconnected"})


# ── BLE — Power Meter ─────────────────────────────────────────────────────────

def parse_cycling_power(data: bytearray) -> dict:
    """Parse Cycling Power Measurement characteristic (0x2A63).
    Returns dict with power_w and optionally cadence_rpm."""
    flags  = struct.unpack_from("<H", data, 0)[0]
    offset = 2
    result = {}
    # Instantaneous power is always present (sint16 at offset 2)
    result["power_w"] = struct.unpack_from("<h", data, offset)[0]
    offset += 2
    # Bit 4: Wheel Revolution Data present (skip if set)
    if flags & 0x0010: offset += 6
    # Bit 5: Crank Revolution Data present — gives cadence
    if flags & 0x0020:
        crank_revs  = struct.unpack_from("<H", data, offset)[0]
        crank_time  = struct.unpack_from("<H", data, offset + 2)[0]  # 1/1024 s units
        offset += 4
        if hasattr(parse_cycling_power, "_last_crank_revs"):
            d_revs = (crank_revs - parse_cycling_power._last_crank_revs) & 0xFFFF
            d_time = (crank_time - parse_cycling_power._last_crank_time) & 0xFFFF
            if d_time > 0:
                result["cadence_rpm"] = round(d_revs * 1024 * 60 / d_time, 0)
        parse_cycling_power._last_crank_revs = crank_revs
        parse_cycling_power._last_crank_time = crank_time
    return result

async def pm_notify_handler(_sender, data: bytearray):
    parsed = parse_cycling_power(data)
    state.pm_power = parsed.get("power_w", state.pm_power)
    if "cadence_rpm" in parsed:
        state.pm_cadence = parsed["cadence_rpm"]
    # If we're using power meter as source, push update immediately
    if state.use_pm:
        state.latest["power_w"] = state.pm_power
        if state.pm_cadence > 0:
            state.latest["cadence_rpm"] = state.pm_cadence
        await broadcast({"type": "pm_update", "power": state.pm_power,
                         "cadence": state.pm_cadence})

async def connect_power_meter(device, name: str):
    try:
        state.pm_client = BleakClient(device)
        await state.pm_client.connect()
        state.pm_connected = True
        state.pm_address   = device if isinstance(device, str) else getattr(device, 'address', str(device))
        state.pm_name      = name
        await state.pm_client.start_notify(CYCLING_POWER_UUID, pm_notify_handler)
        await broadcast({"type": "pm_connected", "name": name})
        logger.info(f"Power meter connected: {name}")
    except Exception as e:
        await broadcast({"type": "error", "msg": f"Power meter: {e}"})

async def disconnect_power_meter():
    if state.pm_client and state.pm_connected:
        try:
            await state.pm_client.stop_notify(CYCLING_POWER_UUID)
            await state.pm_client.disconnect()
        except Exception:
            pass
    state.pm_connected = False
    state.pm_address   = None
    state.pm_name      = None
    state.pm_client    = None
    state.pm_power     = 0
    state.pm_cadence   = 0
    state.use_pm       = False
    await broadcast({"type": "pm_disconnected"})


# ── ANT+ FE-C ─────────────────────────────────────────────────────────────────

async def ant_connect(device_id: int = 0) -> bool:
    """Connect via raw ANT+ channel — bypasses FitnessEquipment pairing bugs on Windows."""
    if not ANT_AVAILABLE:
        await broadcast({"type": "error", "msg": "openant not installed — run: pip install openant"})
        return False
    if state.ant_connected or state._ant_connecting:
        logger.warning("[ANT+] Already connecting — ignoring")
        await broadcast({"type": "log", "msg": "ANT+ already connecting — please wait…"})
        return False
    state._ant_connecting = True
    loop = asyncio.get_event_loop()

    def _connect():
        import time as _t
        try:
            node = Node()
            node.ant.reset_system()
            _t.sleep(0.8)
            logger.info("[ANT+] Dongle reset OK")
            node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)

            # Start node — FEC/HR channels opened separately on demand
            state.ant_node        = node
            state.ant_device      = None
            state.ant_connected   = True
            state._ant_connecting = False
            asyncio.run_coroutine_threadsafe(
                broadcast({"type": "ant_connected", "device_id": 0}), loop)

            logger.info("[ANT+] Node loop starting…")
            node.start()

        except Exception as e:
            logger.error(f"[ANT+] Connection failed: {e}")
            state.ant_connected = state._ant_connecting = False
            asyncio.run_coroutine_threadsafe(
                broadcast({"type": "error", "msg": f"ANT+ error: {e}"}), loop)
        finally:
            if state.ant_connected:
                state.ant_connected = False
                asyncio.run_coroutine_threadsafe(
                    broadcast({"type": "ant_disconnected"}), loop)

    loop.run_in_executor(None, _connect)
    return True
async def ant_disconnect():
    """Stop the ANT+ node cleanly — gives the USB device 2s to settle after close."""
    import asyncio as _asyncio

    def _stop():
        import time as _t
        if state.ant_device:
            try:
                state.ant_device.close()
                _t.sleep(0.3)
            except Exception as e:
                logger.debug(f"[ANT+] channel close: {e}")
        if state.ant_hr_channel:
            try:
                state.ant_hr_channel.close()
                _t.sleep(0.1)
            except Exception as e:
                logger.debug(f"[ANT+] HR channel close: {e}")
        if state.ant_node:
            try:
                state.ant_node.stop()
            except Exception as e:
                logger.debug(f"[ANT+] node stop: {e}")
        _t.sleep(1.0)

    if state.ant_node or state.ant_device:
        await asyncio.get_event_loop().run_in_executor(None, _stop)

    state.ant_node        = None
    state.ant_device      = None
    state.ant_connected   = False
    state._ant_connecting = False
    await broadcast({"type": "ant_disconnected"})




# ── Session stop helper ───────────────────────────────────────────────────────

async def _do_session_stop(auto_upload: str = "none", ride_name: str = ""):
    """Stop recording, broadcast session_stopped, and handle auto-upload."""
    if not state.recording:
        logger.info(f"[SESSION] _do_session_stop called but not recording — skipping (auto_upload={auto_upload})")
        return
    state.recording = False
    history       = state.history[:]
    session_start = state.session_start or time.time()
    settings      = load_settings()
    # Use passed auto_upload, fall back to saved preference
    if auto_upload == "none":
        auto_upload = settings.get("auto_upload", "none")
    if not ride_name:
        ride_name = settings.get("default_name_template", "")

    logger.info(f"[SESSION] Stopped — {len(history)} data points, auto_upload={auto_upload}")
    await broadcast({"type": "session_stopped",
                     "history": history, "session_start": session_start})

    if not history:
        logger.warning("[SESSION] No data recorded — nothing to upload (was trainer connected and pedalling?)")
        await broadcast({"type": "log", "msg": "Session stopped — no ride data recorded, nothing to upload"})
        return

    from datetime import datetime
    dt   = datetime.fromtimestamp(session_start).strftime("%Y-%m-%d %H:%M")
    name = f"{ride_name} — {dt}" if ride_name else f"Indoor Ride — {dt}"
    fit_bytes = build_fit(history, session_start)

    for platform in (["strava"] if auto_upload == "strava"
                     else ["garmin"] if auto_upload == "garmin"
                     else ["strava", "garmin"] if auto_upload == "both"
                     else []):
        label  = "Strava" if platform == "strava" else "Garmin Connect"
        logger.info(f"[SESSION] Auto-uploading to {label}…")
        await broadcast({"type": "log", "msg": f"Uploading to {label}…"})
        result = (await upload_to_strava(fit_bytes, name) if platform == "strava"
                  else await upload_to_garmin(fit_bytes, name))
        ok = result.get("ok", False)
        if ok:
            logger.info(f"[SESSION] {label} OK — id={result.get('upload_id')}")
        else:
            logger.error(f"[SESSION] {label} failed: {result.get('error')}")
        await broadcast({"type": "auto_upload_result", "platform": platform,
                         "ok": ok, "upload_id": result.get("upload_id", ""),
                         "error": result.get("error", "")})


# ── Broadcast ──────────────────────────────────────────────────────────────────

async def broadcast(msg: dict):
    dead = []
    for ws in clients:
        try: await ws.send_text(json.dumps(msg))
        except: dead.append(ws)
    for ws in dead: clients.remove(ws)


# ── Interval runner ────────────────────────────────────────────────────────────

async def _send_step_commands(step: dict):
    """Send both ERG power (BLE) and/or grade (ANT+) for an interval step."""
    watts = step.get("watts", 0)
    grade = step.get("grade", None)   # None means no grade set for this step

    # ERG mode via BLE — only if watts > 0
    if watts and watts > 0:
        try:
            await write_cp(struct.pack("<Bh", OP_SET_TARGET_POWER, watts))
        except Exception as e:
            logger.warning(f"[INTERVAL] ERG command failed: {e}")

    # Slope via ANT+ — only if grade is set and ANT+ connected
    if grade is not None and state.ant_connected and state.ant_device:
        def _send():
            import time as _t
            try:
                grade_raw = int(grade * 100) + 20000
                grade_raw = max(0, min(0xFFFF, grade_raw))
                init  = [0x32, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00]
                slope = [0x33, 0xFF, 0xFF, 0xFF, 0xFF,
                         grade_raw & 0xFF, (grade_raw >> 8) & 0xFF, 40]
                state.ant_device.send_acknowledged_data(init)
                _t.sleep(0.2)
                state.ant_device.send_acknowledged_data(slope)
                logger.info(f"[INTERVAL] Grade {grade}% sent via ANT+")
            except Exception as e:
                logger.warning(f"[INTERVAL] ANT+ grade failed: {e}")
        await asyncio.get_event_loop().run_in_executor(None, _send)


async def run_intervals():
    for i, step in enumerate(state.interval_plan):
        if state.mode != "interval": break
        state.interval_index = i; state.interval_elapsed = 0
        for rep in range(step.get("reps", 1)):
            if state.mode != "interval": return
            await _send_step_commands(step)
            await broadcast({"type": "interval_step", "index": i, "step": step,
                             "total": len(state.interval_plan), "rep": rep+1, "reps": step.get("reps",1)})
            for _ in range(step["duration"]):
                if state.mode != "interval": return
                await asyncio.sleep(1)
                state.interval_elapsed += 1
                await broadcast({"type": "interval_tick", "index": i,
                                 "elapsed": state.interval_elapsed, "duration": step["duration"]})
    state.mode = "free"
    await broadcast({"type": "interval_done"})
    # Auto-stop the session when the plan completes
    if state.recording:
        logger.info("[SESSION] Interval plan complete — auto-stopping session")
        await _do_session_stop()


# ── Debug helpers ─────────────────────────────────────────────────────────────




# ── Strava ────────────────────────────────────────────────────────────────────

async def refresh_token(cfg, tok):
    async with httpx.AsyncClient() as h:
        r = await h.post(STRAVA_TOKEN_URL, data={"client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"], "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"]})
    if r.status_code == 200: save_token(r.json()); return r.json()
    return None

async def upload_to_strava(fit_bytes: bytes, name: str) -> dict:
    logger.info(f"[STRAVA] Starting upload — name={name!r}, fit_size={len(fit_bytes)} bytes")

    cfg = load_config()
    if not cfg.get("client_id"):
        logger.error("[STRAVA] Not configured — strava_config.json missing client_id")
        return {"error": "Strava not configured — edit strava_config.json"}

    tok = load_token()
    if not tok:
        logger.error("[STRAVA] No token found — user not authenticated")
        return {"error": "Not authenticated — visit http://localhost:8000/strava/auth"}

    logger.debug(f"[STRAVA] Token expires_at={tok.get('expires_at')}, now={time.time():.0f}")
    if time.time() > tok.get("expires_at", 0) - 60:
        logger.info("[STRAVA] Token expired, refreshing…")
        tok = await refresh_token(cfg, tok)
        if not tok:
            logger.error("[STRAVA] Token refresh failed")
            return {"error": "Token refresh failed — re-authenticate"}
        logger.info("[STRAVA] Token refreshed OK")

    logger.info(f"[STRAVA] POSTing to {STRAVA_UPLOAD_URL}")
    async with httpx.AsyncClient() as h:
        r = await h.post(STRAVA_UPLOAD_URL,
            headers={"Authorization": f"Bearer {tok['access_token']}"},
            data={"data_type": "fit", "name": name, "sport_type": "VirtualRide"},
            files={"file": ("ride.fit", fit_bytes, "application/octet-stream")},
            timeout=30)

    logger.info(f"[STRAVA] Response status={r.status_code}")
    logger.debug(f"[STRAVA] Response body={r.text[:500]}")

    if r.status_code in (200, 201):
        j = r.json()
        logger.info(f"[STRAVA] Upload accepted — id={j.get('id')}, status={j.get('status')}")
        return {"ok": True, "upload_id": j.get("id"), "status": j.get("status", "processing")}

    logger.error(f"[STRAVA] Upload failed {r.status_code}: {r.text[:300]}")
    return {"error": f"Strava {r.status_code}: {r.text[:300]}"}


# ── Garmin Connect (python-garminconnect) ─────────────────────────────────────
# The new API: client.login(token_dir) saves tokens on first login,
# then loads + auto-refreshes them on subsequent calls — no re-login needed.

GARMIN_TOKEN_FILE = GARMIN_DIR / "garmin_tokens.json"

def garmin_is_authed() -> bool:
    return GARMIN_TOKEN_FILE.exists()

async def garmin_login(email: str, password: str) -> dict:
    """Log in fresh with credentials and save DI OAuth tokens."""
    def _do_login():
        from garminconnect import Garmin
        GARMIN_DIR.mkdir(exist_ok=True)
        client = Garmin(email=email, password=password)
        client.login(str(GARMIN_DIR))          # saves garmin_tokens.json
        return getattr(client, "display_name", email) or email
    try:
        display = await asyncio.get_event_loop().run_in_executor(None, _do_login)
        return {"ok": True, "athlete": display}
    except Exception as e:
        err = str(e)
        if "429" in err:
            return {"error": "Garmin is rate-limiting logins (429). Wait 15–30 minutes then try again. This happens when login is attempted repeatedly — it won't recur once your session is cached."}
        if "401" in err or "Invalid" in err or "AUTHENTICATION" in err.upper():
            return {"error": "Invalid email or password"}
        if "MFA" in err.upper() or "2FA" in err.upper() or "factor" in err.lower():
            return {"error": "MFA/2FA detected — not yet supported in web UI. Disable 2FA temporarily, log in here to cache the token, then re-enable it."}
        return {"error": err[:300]}

def _garmin_client_from_cache() -> "Garmin":
    """Return a logged-in Garmin client using cached tokens — no password needed."""
    from garminconnect import Garmin
    client = Garmin()
    client.login(str(GARMIN_DIR))   # loads token, refreshes if needed
    return client

async def garmin_display_name() -> str:
    try:
        def _get():
            return getattr(_garmin_client_from_cache(), "display_name", "") or ""
        return await asyncio.get_event_loop().run_in_executor(None, _get)
    except Exception:
        return ""

async def upload_to_garmin(fit_bytes: bytes, name: str) -> dict:
    logger.info(f"[GARMIN] Starting upload — name={name!r}, fit_size={len(fit_bytes)} bytes")

    if not garmin_is_authed():
        logger.error("[GARMIN] Not authenticated — no token file found")
        return {"error": "Not authenticated — log in via the Upload tab first"}

    def _upload():
        import tempfile, os, re, json as _json
        logger.info("[GARMIN] Building client from cached token")
        client = _garmin_client_from_cache()
        logger.info(f"[GARMIN] Client ready, display_name={getattr(client, 'display_name', '?')}")

        with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as tmp:
            tmp.write(fit_bytes)
            tmp_path = tmp.name
        logger.info(f"[GARMIN] Temp file written: {tmp_path} ({len(fit_bytes)} bytes)")

        try:
            logger.info("[GARMIN] Calling upload_activity()")
            result = client.upload_activity(tmp_path)
            logger.info(f"[GARMIN] upload_activity() returned: {result}")
            return result
        except Exception as e:
            err = str(e)
            logger.warning(f"[GARMIN] upload_activity() raised: {err[:500]}")
            # Garmin sometimes raises on 415 but the body shows success
            if "415" in err:
                logger.info("[GARMIN] 415 received — attempting to parse body from exception")
                m = re.search(r'(\{.*\})', err, re.DOTALL)
                if m:
                    try:
                        parsed = _json.loads(m.group(1))
                        logger.info(f"[GARMIN] Parsed body from 415: {parsed}")
                        return parsed
                    except Exception as pe:
                        logger.error(f"[GARMIN] Failed to parse 415 body: {pe}")
                        raise
                else:
                    logger.error("[GARMIN] 415 but no JSON body found in exception")
            raise
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    try:
        result   = await asyncio.get_event_loop().run_in_executor(None, _upload)
        logger.info(f"[GARMIN] Raw result: {result}")
        detail   = result.get("detailedImportResult", result) if isinstance(result, dict) else {}
        failures = detail.get("failures", [])
        uid      = detail.get("uploadId", "")
        logger.info(f"[GARMIN] uploadId={uid!r}, failures={failures}")
        if failures:
            logger.error(f"[GARMIN] Garmin reported failures: {failures}")
            return {"error": f"Garmin upload failures: {failures}"}
        if not uid:
            logger.warning("[GARMIN] uploadId is empty — Garmin may have silently rejected the file")
        logger.info(f"[GARMIN] Upload success — uploadId={uid}")
        return {"ok": True, "upload_id": uid, "status": "processing"}
    except Exception as e:
        err = str(e)
        logger.error(f"[GARMIN] Exception during upload: {err[:500]}")
        if "401" in err or "403" in err:
            return {"error": "Garmin session expired — please log in again"}
        if "409" in err or "Conflict" in err:
            return {"error": "Activity already exists on Garmin Connect (duplicate)"}
        return {"error": err[:300]}



@asynccontextmanager
async def lifespan(app):
    yield
    if state.client and state.connected: await state.client.disconnect()
    if state.hr_client and state.hr_connected: await state.hr_client.disconnect()
    if state.pm_client and state.pm_connected: await state.pm_client.disconnect()
    if state.ant_connected: await ant_disconnect()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root(): return HTMLResponse((BASE / "direto_ui.html").read_text(encoding="utf-8"))

# Plans
@app.get("/plans")
async def get_plans(): return JSONResponse(plans)

@app.post("/plans/{name}")
async def save_plan(name: str, request: Request):
    plans[name] = await request.json(); save_plans(plans); return {"ok": True}

@app.delete("/plans/{name}")
async def del_plan(name: str):
    plans.pop(name, None); save_plans(plans); return {"ok": True}

# Settings
@app.get("/settings")
async def get_settings(): return JSONResponse(load_settings())

@app.post("/settings")
async def post_settings(request: Request):
    body = await request.json()
    s = load_settings(); s.update(body); save_settings(s); return {"ok": True}

# Strava OAuth
@app.get("/strava/status")
async def strava_status():
    cfg = load_config(); tok = load_token()
    return {"configured": bool(cfg.get("client_id")),
            "authed": tok is not None,
            "athlete": (tok or {}).get("athlete", {}).get("firstname", "")}

@app.get("/strava/auth")
async def strava_auth():
    cfg = load_config()
    if not cfg.get("client_id"):
        return JSONResponse({"error": "Add client_id/client_secret to strava_config.json"}, status_code=400)
    url = (f"{STRAVA_AUTH_URL}?client_id={cfg['client_id']}"
           f"&redirect_uri=http://localhost:8000/strava/callback"
           f"&response_type=code&scope=activity:write,read&approval_prompt=auto")
    return RedirectResponse(url)

@app.get("/strava/callback")
async def strava_callback(code: str = "", error: str = ""):
    if error: return HTMLResponse(f"<p>Auth error: {error}</p>")
    cfg = load_config()
    async with httpx.AsyncClient() as h:
        r = await h.post(STRAVA_TOKEN_URL, data={"client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"], "code": code, "grant_type": "authorization_code"})
    if r.status_code == 200:
        save_token(r.json())
        return HTMLResponse("<script>window.close();if(window.opener)window.opener.location.reload();</script>"
                            "<p>Strava connected! Close this window.</p>")
    return HTMLResponse(f"<p>Token exchange failed: {r.text}</p>")

# Upload real ride
@app.post("/upload/strava")
async def upload_strava(request: Request):
    body    = await request.json()
    history = body.get("history", state.history)
    if not history: return JSONResponse({"error": "No ride data"}, status_code=400)
    ss   = body.get("session_start", state.session_start or time.time())
    name = body.get("name") or f"Indoor Ride — {datetime.fromtimestamp(ss).strftime('%Y-%m-%d %H:%M')}"
    return JSONResponse(await upload_to_strava(build_fit(history, ss), name))

# Upload fake test ride to Strava
@app.post("/upload/strava/fake")
async def upload_fake(request: Request):
    body     = await request.json()
    duration = int(body.get("duration", 1800))
    ftp      = int(body.get("ftp", 220))
    name     = body.get("name") or f"Test Ride — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    history, session_start = generate_fake_ride(duration, ftp)
    return JSONResponse(await upload_to_strava(build_fit(history, session_start), name))

# ── Garmin routes ──────────────────────────────────────────────────────────────

@app.get("/garmin/status")
async def garmin_status():
    authed  = garmin_is_authed()
    athlete = await garmin_display_name() if authed else ""
    return {"authed": authed, "athlete": athlete}

@app.post("/garmin/login")
async def garmin_login_route(request: Request):
    body = await request.json()
    email    = body.get("email", "").strip()
    password = body.get("password", "")
    if not email or not password:
        return JSONResponse({"error": "Email and password required"}, status_code=400)
    return JSONResponse(await garmin_login(email, password))

@app.post("/garmin/logout")
async def garmin_logout():
    import shutil
    if GARMIN_DIR.exists():
        shutil.rmtree(GARMIN_DIR)
    return {"ok": True}

@app.post("/upload/garmin")
async def upload_garmin(request: Request):
    body    = await request.json()
    history = body.get("history", state.history)
    if not history:
        return JSONResponse({"error": "No ride data"}, status_code=400)
    ss   = body.get("session_start", state.session_start or time.time())
    name = body.get("name") or f"Indoor Ride — {datetime.fromtimestamp(ss).strftime('%Y-%m-%d %H:%M')}"
    return JSONResponse(await upload_to_garmin(build_fit(history, ss), name))

@app.post("/upload/garmin/fake")
async def upload_garmin_fake(request: Request):
    body     = await request.json()
    duration = int(body.get("duration", 1800))
    ftp      = int(body.get("ftp", 220))
    name     = body.get("name") or f"Test Ride — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    history, session_start = generate_fake_ride(duration, ftp)
    return JSONResponse(await upload_to_garmin(build_fit(history, session_start), name))

# ── FIT download ───────────────────────────────────────────────────────────────

@app.post("/download/fit")
async def download_fit(request: Request):
    """Generate and return a FIT file for direct download."""
    body    = await request.json()
    history = body.get("history", state.history)
    fake    = body.get("fake", False)
    if fake:
        duration = int(body.get("duration", 1800))
        ftp      = int(body.get("ftp", 220))
        history, session_start = generate_fake_ride(duration, ftp)
    else:
        if not history:
            return JSONResponse({"error": "No ride data"}, status_code=400)
        session_start = body.get("session_start", state.session_start or time.time())
    dt       = datetime.fromtimestamp(session_start).strftime("%Y-%m-%d_%H-%M")
    filename = f"ride_{dt}.fit"
    fit_data = build_fit(history, session_start)
    return Response(
        content=fit_data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

# WebSocket
@app.websocket("/ws")
async def ws_ep(ws: WebSocket):
    await ws.accept(); clients.append(ws)
    await ws.send_text(json.dumps({
        "type": "status", "connected": state.connected, "name": state.name,
        "mode": state.mode, "target_power": state.target_power, "resistance": state.resistance,
        "hr_connected": state.hr_connected, "hr_name": state.hr_name,
        "pm_connected": state.pm_connected, "pm_name": state.pm_name, "use_pm": state.use_pm,
        "ant_connected": state.ant_connected, "ant_device_id": state.ant_device_id,
        "ant_available": ANT_AVAILABLE,
    }))
    try:
        while True: await handle_message(json.loads(await ws.receive_text()))
    except WebSocketDisconnect:
        if ws in clients: clients.remove(ws)

async def handle_message(msg: dict):
    a = msg.get("action")

    if a == "scan":
        await broadcast({"type": "log", "msg": "Scanning for trainer…"})
        devs = await BleakScanner.discover(timeout=4.0)
        found = []
        for d in devs:
            if d.name and "direto" in d.name.lower():
                state.scan_cache[d.address] = d   # cache BLEDevice object
                found.append({"name": d.name, "address": d.address})
        await broadcast({"type": "scan_results", "devices": found})

    elif a == "scan_hr":
        await broadcast({"type": "log", "msg": "Scanning for heart rate monitors…"})
        devs = await BleakScanner.discover(timeout=4.0)
        hr_keywords = ["heart", "hr ", "polar", "garmin", "wahoo", "tickr", "scosche",
                       "hrm", "chest", "band", "sense", "vantage", "rhythm"]
        found = []
        for d in devs:
            if not d.name: continue
            name_l = d.name.lower()
            has_hr_svc  = HR_SERVICE_UUID.lower() in [str(s).lower() for s in []]
            has_hr_name = any(k in name_l for k in hr_keywords)
            if has_hr_svc or has_hr_name:
                state.scan_cache[d.address] = d
                found.append({"name": d.name, "address": d.address})
        await broadcast({"type": "hr_scan_results", "devices": found})

    elif a == "connect_hr":
        addr = msg.get("address"); name = msg.get("name", addr)
        if addr:
            dev = state.scan_cache.get(addr, addr)  # use BLEDevice if cached
            await connect_hr_monitor(dev, name)

    elif a == "disconnect_hr":
        await disconnect_hr_monitor()

    elif a == "scan_pm":
        await broadcast({"type": "log", "msg": "Scanning for power meters…"})
        devs = await BleakScanner.discover(timeout=4.0)
        pm_keywords = ["power", "stages", "4iiii", "assioma", "favero", "vector",
                       "powertap", "srm", "pioneer", "quarq", "rotor", "infocrank",
                       "xcadey", "sram", "shimano", "bepro", "dfour", "p2m"]
        found = []
        for d in devs:
            if not d.name: continue
            name_l = d.name.lower()
            has_pm_svc  = CYCLING_POWER_SVC_UUID.lower() in [str(s).lower() for s in []]
            has_pm_name = any(k in name_l for k in pm_keywords)
            if has_pm_svc or has_pm_name:
                state.scan_cache[d.address] = d
                found.append({"name": d.name, "address": d.address})
        await broadcast({"type": "pm_scan_results", "devices": found})

    elif a == "connect_pm":
        addr = msg.get("address"); name = msg.get("name", addr)
        if addr:
            dev = state.scan_cache.get(addr, addr)
            await connect_power_meter(dev, name)

    elif a == "disconnect_pm":
        await disconnect_power_meter()

    elif a == "set_use_pm":
        state.use_pm = bool(msg.get("enabled", False))
        logger.info(f"Power source: {'power meter' if state.use_pm else 'trainer'}")
        await broadcast({"type": "pm_source_changed", "use_pm": state.use_pm})

    elif a == "connect":
        addr = msg.get("address")
        if not addr: return
        # Use cached BLEDevice object — avoids "device not found" on Windows
        # where BLE addresses are randomised and may rotate between scan and connect
        dev = state.scan_cache.get(addr, addr)
        logger.info(f"[BLE] Connecting to {msg.get('name', addr)} using {'BLEDevice' if dev is not addr else 'address string'}")
        try:
            state.client = BleakClient(dev)
            await state.client.connect()
            logger.info(f"[BLE] Connected, discovering services…")
            await asyncio.sleep(1.0)

            # Services are available directly as a property in bleak >= 1.0
            try:
                services = state.client.services
            except AttributeError:
                services = await state.client.get_services()
            logger.info(f"[BLE] Services: {[str(s.uuid) for s in services]}")

            # Find Indoor Bike Data characteristic — log all characteristics to help debug
            ibd_char = None
            for svc in services:
                for char in svc.characteristics:
                    logger.debug(f"[BLE] Char: {char.uuid} props={char.properties}")
                    if char.uuid.lower() == INDOOR_BIKE_DATA_UUID.lower():
                        ibd_char = char
            if ibd_char:
                logger.info(f"[BLE] Found Indoor Bike Data characteristic")
                await state.client.start_notify(INDOOR_BIKE_DATA_UUID, ble_notify_handler)
            else:
                # Some devices use a short UUID — try anyway and log
                logger.debug(f"[BLE] Indoor Bike Data UUID not in service list — subscribing directly")
                await state.client.start_notify(INDOOR_BIKE_DATA_UUID, ble_notify_handler)
            logger.info("[BLE] Subscribed to Indoor Bike Data")

            # Subscribe to FTMS Control Point indications so the trainer can
            # send back response codes — required before sending any CP commands
            def cp_response_handler(sender, data: bytearray):
                # Response format: 0x80, request_opcode, result_code
                if len(data) >= 3 and data[0] == 0x80:
                    codes = {0x01:"Success", 0x02:"Op Code not supported",
                             0x03:"Invalid Parameter", 0x04:"Operation Failed",
                             0x05:"Control Not Permitted"}
                    result = codes.get(data[2], f"0x{data[2]:02X}")
                    opcode = f"0x{data[1]:02X}"
                    level  = logging.INFO if data[2] == 0x01 else logging.WARNING
                    logger.log(level, f"[BLE] CP response opcode={opcode} result={result} raw={data.hex()}")
                else:
                    logger.debug(f"[BLE] CP notification: {data.hex()}")
            try:
                await state.client.start_notify(FTMS_CONTROL_POINT_UUID, cp_response_handler)
                logger.info("[BLE] Subscribed to CP indications")
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.warning(f"[BLE] CP notification subscribe failed (non-fatal): {e}")

            # Read Fitness Machine Features to log what the trainer supports
            FTMS_FEATURE_UUID = "00002acc-0000-1000-8000-00805f9b34fb"
            try:
                feat_data = await state.client.read_gatt_char(FTMS_FEATURE_UUID)
                fm_feat   = struct.unpack_from("<I", feat_data, 0)[0]
                tm_feat   = struct.unpack_from("<I", feat_data, 4)[0] if len(feat_data) >= 8 else 0
                features  = []
                if fm_feat & 0x0002: features.append("Cadence")
                if fm_feat & 0x0040: features.append("Resistance")
                if fm_feat & 0x0200: features.append("Power")
                if tm_feat & 0x0004: features.append("TargetResistance")
                if tm_feat & 0x0008: features.append("TargetPower(ERG)")
                if tm_feat & 0x0080: features.append("IndoorBikeSimulation(Slope)")
                logger.info(f"[BLE] Trainer features: FM=0x{fm_feat:08X} TM=0x{tm_feat:08X} → {features}")
                if not (tm_feat & 0x0080):
                    logger.warning("[BLE] Trainer does NOT advertise IndoorBikeSimulation support — slope mode may not work")
                    await broadcast({"type": "log", "msg": "⚠ Trainer does not advertise slope support — ERG mode recommended"})
            except Exception as e:
                logger.warning(f"[BLE] Could not read features characteristic: {e}")

            # Request control with retries
            ok = await request_ftms_control()
            if not ok:
                raise Exception("Trainer rejected FTMS control request after 3 attempts. "
                                "Try power-cycling the trainer.")

            # Start the session
            try:
                await write_cp(bytes([OP_START_RESUME]))
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.warning(f"[BLE] START_RESUME failed (non-fatal): {e}")

            state.connected = True; state.address = addr; state.name = msg.get("name", addr)
            state.session_start = time.time(); state.history = []
            await broadcast({"type": "connected", "name": state.name})
            logger.info(f"[BLE] Session started: {state.name}")

        except Exception as e:
            logger.error(f"[BLE] Connect failed: {e}")
            try:
                if state.client: await state.client.disconnect()
            except Exception:
                pass
            state.connected = False; state.client = None
            err = str(e)
            # Don't surface internal BLE characteristic lookup failures to UI
            if "was not found" not in err and "Characteristic" not in err:
                await broadcast({"type": "error", "msg": err})

    elif a == "disconnect":
        if state.client and state.connected:
            if state.interval_task: state.interval_task.cancel()
            await write_cp(bytes([OP_STOP_PAUSE, 0x01])); await write_cp(bytes([OP_RESET]))
            await state.client.disconnect()
        state.connected = False; state.mode = "free"
        await broadcast({"type": "disconnected", "history": state.history,
                         "session_start": state.session_start})

    elif a == "set_power":
        w = int(msg.get("watts", 100)); state.mode = "erg"; state.target_power = w
        try:
            await write_cp(struct.pack("<Bh", OP_SET_TARGET_POWER, w))
        except Exception as e:
            if "0x81" in str(e) or "Application" in str(e):
                logger.warning("[BLE] 0x81 on set_power — re-requesting control")
                await request_ftms_control()
                await write_cp(struct.pack("<Bh", OP_SET_TARGET_POWER, w))
            else:
                raise
        await broadcast({"type": "mode_update", "mode": "erg", "target_power": w})

    elif a == "set_resistance":
        # FTMS Set Target Resistance Level (opcode 0x04)
        # Parameter: uint16 LE, value = percentage * 10 (so 50% = 500, resolution 0.1)
        # NOTE: The Direto XR may not support resistance mode over BLE FTMS —
        # it primarily supports ERG (target power) mode. If this errors, use ERG instead.
        l = max(0, min(100, int(msg.get("level", 0))))
        state.mode = "free"; state.resistance = l
        try:
            await write_cp(struct.pack("<BH", OP_SET_RESISTANCE, l * 10))
            logger.info(f"[BLE] Resistance set to {l}% (raw={l*10})")
        except Exception as e:
            logger.warning(f"[BLE] Resistance command failed: {e} — Direto XR may only support ERG mode over BLE")
            await broadcast({"type": "log", "msg": f"Resistance mode may not be supported — try ERG power mode instead"})
        await broadcast({"type": "mode_update", "mode": "free", "resistance": l})

    elif a == "session_start":
        # Auto-start also kicks off intervals if a plan was queued
        state.history       = []
        state.session_start = time.time()
        state.recording     = True
        state.ride_name     = msg.get("name", "")
        logger.info("[SESSION] Recording started")
        await broadcast({"type": "session_started", "session_start": state.session_start})

    elif a == "session_stop":
        # Also cancel any running intervals
        if state.interval_task:
            state.interval_task.cancel()
            state.mode = "free"
            await broadcast({"type": "interval_stopped"})
        auto_upload = msg.get("auto_upload", "none")
        ride_name   = msg.get("name", "") or state.ride_name or ""
        await _do_session_stop(auto_upload=auto_upload, ride_name=ride_name)

    elif a == "zero_pm":
        # Send zero offset calibration to power meter via Cycling Power Control Point
        # Opcode 0x01 = Set Cumulative Value (resets accumulated energy, not offset)
        # Most meters use proprietary calibration but the standard CPCP zero offset is 0x00
        CYCLING_POWER_CP_UUID = "00002a66-0000-1000-8000-00805f9b34fb"
        if not state.pm_connected or not state.pm_client:
            await broadcast({"type": "error", "msg": "Power meter not connected"})
            return
        try:
            await state.pm_client.write_gatt_char(
                CYCLING_POWER_CP_UUID,
                bytes([0x00]),   # Request sampling of supported sensor calibration
                response=True
            )
            await broadcast({"type": "log", "msg": "Power meter: zero calibration sent"})
            logger.info("[PM] Zero calibration command sent")
        except Exception as e:
            await broadcast({"type": "error", "msg": f"Zero calibration failed: {e}"})
            logger.warning(f"[PM] Zero calibration error: {e}")

    elif a == "reset_app":
        """Disconnect all devices and reset state without stopping the server."""
        logger.info("[RESET] Resetting all connections…")
        if state.recording: await _do_session_stop()
        if state.connected and state.client:
            try: await state.client.disconnect()
            except: pass
        if state.hr_connected and state.hr_client:
            try: await state.hr_client.disconnect()
            except: pass
        if state.pm_connected and state.pm_client:
            try: await state.pm_client.disconnect()
            except: pass
        if state.ant_connected: await ant_disconnect()
        # Reset state
        state.connected = False; state.client = None; state.name = ""
        state.hr_connected = False; state.hr_client = None
        state.pm_connected = False; state.pm_client = None
        state.ant_device_id = 0
        await broadcast({"type": "reset"})
        await broadcast({"type": "log", "msg": "App reset — all devices disconnected"})

    elif a == "shutdown":
        logger.info("[SERVER] Shutdown requested — disconnecting and stopping…")
        await broadcast({"type": "log", "msg": "Shutting down…"})
        # Disconnect BLE
        if state.connected and state.client:
            try: await state.client.disconnect()
            except: pass
        if state.hr_connected and state.hr_client:
            try: await state.hr_client.disconnect()
            except: pass
        if state.pm_connected and state.pm_client:
            try: await state.pm_client.disconnect()
            except: pass
        # Disconnect ANT+
        if state.ant_connected: await ant_disconnect()
        await asyncio.sleep(0.5)
        import os, signal
        os.kill(os.getpid(), signal.SIGTERM)

    elif a == "ant_connect":
        device_id = int(msg.get("device_id", 0))
        await broadcast({"type": "log", "msg": f"Connecting ANT+ dongle…"})
        await ant_connect(device_id)

    elif a == "ant_scan_trainer":
        # Open the FEC channel on an already-running node
        if not state.ant_node:
            await broadcast({"type": "error", "msg": "Connect ANT+ dongle first"})
            return
        loop_t = asyncio.get_event_loop()
        def _scan_trainer():
            import time as _t
            try:
                channel = state.ant_node.new_channel(0x00, network_number=0x00)
                channel.set_id(0, 17, 0)
                channel.set_period(8192)
                channel.set_rf_freq(57)
                channel.set_search_timeout(255)
                channel.enable_extended_messages(True)
                channel.open()
                logger.info("[ANT+] FEC channel open, searching for trainer…")
                state.ant_device    = channel
                state.ant_connected = True
                # Send controller registration
                _t.sleep(1.0)
                try:
                    channel.send_acknowledged_data([0x32,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0x00])
                    logger.info("[ANT+] Controller registration sent")
                except Exception as e:
                    logger.warning(f"[ANT+] Controller init: {e}")
                def on_broadcast(data):
                    if not data: return
                    page = data[0]
                    if page in (0x10, 0x19) and len(data) > 8 and state.ant_device_id == 0:
                        dev_id = data[9] + (data[10] << 8)
                        if dev_id > 0:
                            state.ant_device_id = dev_id
                            logger.info(f"[ANT+] Trainer found: #{dev_id:05}")
                            asyncio.run_coroutine_threadsafe(
                                broadcast({"type": "ant_trainer_connected", "device_id": dev_id}),
                                loop_t)
                channel.on_broadcast_data   = on_broadcast
                channel.on_acknowledge_data = on_broadcast
                asyncio.run_coroutine_threadsafe(
                    broadcast({"type": "ant_trainer_connected", "device_id": 0}), loop_t)
            except Exception as e:
                logger.error(f"[ANT+] Trainer scan failed: {e}")
                asyncio.run_coroutine_threadsafe(
                    broadcast({"type": "error", "msg": f"ANT+ trainer scan: {e}"}), loop_t)
        asyncio.get_event_loop().run_in_executor(None, _scan_trainer)
    elif a == "scan_ant_hr":
        # ANT+ HR monitor — works independently of trainer connection
        # Uses existing node if connected to trainer, otherwise needs dongle connected
        if not state.ant_node:
            await broadcast({"type": "error", "msg": "Connect ANT+ dongle first"})
            return
        loop2 = asyncio.get_event_loop()
        def _open_hr():
            import time as _t
            try:
                logger.info("[ANT+] Opening HR channel…")
                ch = state.ant_node.new_channel(0x00, network_number=0x00)
                logger.info("[ANT+] HR channel created, setting ID…")
                ch.set_id(0, 120, 0)       # device type 120 = HR monitor, 0 = any
                logger.info("[ANT+] HR set_id OK, setting period…")
                ch.set_period(8070)
                logger.info("[ANT+] HR period OK, setting rf_freq…")
                ch.set_rf_freq(57)
                logger.info("[ANT+] HR rf_freq OK, setting timeout…")
                ch.set_search_timeout(255)  # search indefinitely
                logger.info("[ANT+] HR timeout OK, enabling ext messages…")
                ch.enable_extended_messages(True)
                logger.info("[ANT+] HR ext OK, opening…")

                def on_hr_data(data):
                    if not data or len(data) < 5: return
                    hr = data[7] if len(data) > 7 else data[6]  # byte 7 = computed HR in ANT+ HR profile
                    if 30 < hr < 220:
                        state.current_hr   = hr
                        state.hr_last_seen = time.time()
                        state.latest["hr"] = hr
                        asyncio.run_coroutine_threadsafe(
                            broadcast({"type": "hr_update", "hr": hr}), loop2)

                def on_hr_found(data):
                    if not data: return
                    if len(data) > 8 and not state.hr_connected:
                        dev_id = data[9] + (data[10] << 8)
                        state.hr_connected = True
                        state.hr_name      = f"ANT+ HR #{dev_id:05}"
                        logger.info(f"[ANT+] HR strap found: #{dev_id:05}")
                        asyncio.run_coroutine_threadsafe(
                            broadcast({"type": "hr_connected", "name": state.hr_name}), loop2)
                    on_hr_data(data)

                ch.on_broadcast_data   = on_hr_found
                ch.on_acknowledge_data = on_hr_data
                ch.open()
                state.ant_hr_channel = ch
                logger.info("[ANT+] HR channel open — searching for strap…")
                asyncio.run_coroutine_threadsafe(
                    broadcast({"type": "log", "msg": "ANT+ HR: searching… (wet the strap contacts)"}), loop2)
            except Exception as e:
                err = str(e)
                logger.error(f"[ANT+] HR open failed at: {err}")
                if "Timed out" in err or "timeout" in err.lower():
                    msg = "ANT+ HR: channel setup timed out — try disconnecting and reconnecting the ANT+ dongle"
                else:
                    msg = f"ANT+ HR: {err}"
                asyncio.run_coroutine_threadsafe(
                    broadcast({"type": "error", "msg": msg}), loop2)
        asyncio.get_event_loop().run_in_executor(None, _open_hr)

    elif a == "ant_disconnect":
        await ant_disconnect()
        await broadcast({"type": "log", "msg": "ANT+ disconnected"})

    elif a == "set_slope":
        # Sends slope via BLE (FTMS Indoor Bike Simulation) AND ANT+ (page 51)
        # BLE works when pedalling on the Direto XR
        # ANT+ is sent as a backup for trainers that support it
        grade = float(msg.get("grade", 0.0))
        state.ant_grade = grade

        sent_ble = False
        sent_ant = False

        # ── BLE: FTMS Indoor Bike Simulation (opcode 0x11) ─────────────────
        if state.connected and state.client:
            try:
                grade_raw = int(grade * 100)   # 0.01% units, no offset for BLE
                # Parameters: wind_speed(sint16, 0.001 m/s), grade(sint16, 0.01%),
                #             crr(uint8, 0.0001), cw(uint8, 0.01 kg/m)
                payload = struct.pack("<BhhBB",
                    OP_SET_INDOOR_SIMULATION,
                    0,           # wind speed: 0
                    grade_raw,   # grade
                    40,          # crr 0.004
                    51)          # cw 0.51 kg/m
                await write_cp(payload)
                logger.info(f"[BLE] Slope sent via FTMS: {grade}% (raw={grade_raw})")
                sent_ble = True
            except Exception as e:
                logger.warning(f"[BLE] Slope failed: {e}")

        # ── ANT+: FEC page 51 ───────────────────────────────────────────────
        if state.ant_connected and state.ant_device:
            def _send_ant_slope():
                import time as _t
                try:
                    grade_raw_ant = int(grade * 100) + 20000  # ANT+ offset +200%
                    grade_raw_ant = max(0, min(0xFFFF, grade_raw_ant))
                    state.ant_device.send_acknowledged_data(
                        [0x32, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])
                    _t.sleep(0.2)
                    state.ant_device.send_acknowledged_data([
                        0x33, 0xFF, 0xFF, 0xFF, 0xFF,
                        grade_raw_ant & 0xFF, (grade_raw_ant >> 8) & 0xFF, 40])
                    logger.info(f"[ANT+] Slope sent via FEC: {grade}%")
                except Exception as e:
                    logger.warning(f"[ANT+] Slope failed: {e}")
            await asyncio.get_event_loop().run_in_executor(None, _send_ant_slope)
            sent_ant = True

        if not sent_ble and not sent_ant:
            await broadcast({"type": "error",
                "msg": "No trainer connected — connect via BLE or ANT+ first"})
            return

        await broadcast({"type": "mode_update", "mode": "slope", "grade": grade})

    elif a == "ant_set_power":
        # ANT+ FEC page 0x31 — Target Power ERG mode via ANT+
        w = max(0, min(4000, int(msg.get("watts", 100))))
        if state.ant_connected and state.ant_device:
            def _send_power():
                power_raw = w * 4  # 0.25W units
                data = [0x31, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
                        power_raw & 0xFF, (power_raw >> 8) & 0xFF]
                state.ant_device.send_acknowledged_data(data)
                logger.info(f"[ANT+] ERG power set to {w}W")
            await asyncio.get_event_loop().run_in_executor(None, _send_power)
            await broadcast({"type": "mode_update", "mode": "erg", "target_power": w})

    elif a == "start_intervals":
        plan = msg.get("plan", [])
        if not plan: return
        # Auto-start recording if not already recording
        if not state.recording:
            settings = load_settings()
            state.history       = []
            state.session_start = time.time()
            state.recording     = True
            state.ride_name     = msg.get("name", "") or settings.get("default_name_template", "")
            logger.info("[SESSION] Auto-started recording with interval plan")
            await broadcast({"type": "session_started", "session_start": state.session_start})
        state.interval_plan = plan; state.mode = "interval"
        if state.interval_task: state.interval_task.cancel()
        state.interval_task = asyncio.create_task(run_intervals())
        await broadcast({"type": "interval_started", "plan": plan})

    elif a == "stop_intervals":
        state.mode = "free"
        if state.interval_task: state.interval_task.cancel()
        await broadcast({"type": "interval_stopped"})
        if state.recording:
            await _do_session_stop(
                auto_upload=msg.get("auto_upload", "none"),
                ride_name=msg.get("name", "")
            )


if __name__ == "__main__":
    uvicorn.run("direto_server:app", host="127.0.0.1", port=8000, reload=False)
