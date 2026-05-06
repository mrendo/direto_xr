# Direto XR Controller

A local web app to control your Elite Direto XR smart trainer over Bluetooth and ANT+.

Features: ERG mode, ANT+ slope simulation, heart rate monitor, power meter, interval workouts, saved workout plans, session recording, and automatic upload to Strava and Garmin Connect.

---

## Requirements

- **Python 3.10 or newer** (3.14 tested and working)
- Elite Direto XR powered on and not connected to another device
- Bluetooth adapter on your PC/Mac
- *(Optional)* ANT+ USB dongle for slope/simulation mode

---

## Installation

### Windows

1. Install Python from https://www.python.org/downloads/ — tick **"Add Python to PATH"**
2. Double-click **`scripts\install.bat`** — creates the virtual environment and installs all dependencies
3. Run **`scripts\start.bat`** to launch — your browser opens automatically at http://localhost:8000

### macOS / Linux

```bash
chmod +x scripts/install.sh scripts/start.sh
./scripts/install.sh
./scripts/start.sh
```

> **macOS**: Grant Bluetooth permission to Terminal via  
> System Settings → Privacy & Security → Bluetooth

> **Linux**: Add your user to the bluetooth group if needed:  
> `sudo usermod -aG bluetooth $USER` (log out and back in after)

---

## ANT+ Dongle Setup (Windows — one-time, required for slope mode)

The Direto XR does **not** support slope simulation over Bluetooth — this is an Elite firmware limitation. Slope control requires an ANT+ USB dongle with a libusb driver installed via Zadig.

1. Download **Zadig** from https://zadig.akeo.ie and run it
2. Plug in your ANT+ USB dongle
3. In Zadig: Options → List All Devices
4. Select your dongle from the dropdown (usually "ANT USB Stick 2" or "ANTUSB-m")
5. Set the driver to **libusb-win32**
6. Click **Replace Driver** — takes about 30 seconds

> ⚠️ After installing the libusb driver, the dongle will not work with Garmin Express or other ANT+ apps until you switch the driver back in Device Manager. The swap is reversible.

Once Zadig is done, use the **Connect ANT+ Dongle** button in the app's left panel.

---

## Usage

### Ride tab

#### Connecting

1. Click **Scan for Trainer** — your Direto XR appears by name
2. Click it to connect over Bluetooth
3. *(Optional)* Click **Scan for HR Monitor** to connect a BLE heart rate strap
4. *(Optional)* Click **Scan for Power Meter** to connect a BLE power meter
5. *(Optional)* Click **Connect ANT+ Dongle** to enable slope control

#### Control modes

**ERG Mode** — set a target power in watts. The trainer adjusts brake resistance to hold that wattage regardless of your cadence or speed. Ideal for structured workouts.

**Free Ride** — no fixed power target. The trainer's flywheel provides natural resistance. Use the session recorder to log the ride.

**ANT+ Slope Mode** — simulates riding a gradient (-10% to +24%). The trainer physically adjusts resistance to match the incline. Requires ANT+ dongle. Works alongside BLE telemetry.

#### Session recording

- Hit **● REC** to start recording (resets timer and distance)
- Hit **■ STOP** to stop recording and trigger an automatic upload based on your Upload tab preference
- The session timer, distance, speed, cadence, power, and heart rate are all tracked live

#### Speed units

- Click the **km/h ⇄ mph** button in the mode bar to toggle between kilometres and miles
- Distance also switches between km and miles automatically
- Tap the Speed dial itself to toggle too

---

### Intervals & Plans

#### Quick intervals (Ride tab — right panel)

- Add steps directly with watts, duration in seconds, and reps
- Hit **▶ Start Plan** — recording starts automatically
- Hit **■ Stop** — intervals cancel, session stops, auto-upload fires
- Load any saved plan into the quick panel with **Load Plan**

#### Plan Builder (Plans tab)

- Create and name unlimited workout plans
- Each step has: label, watts, duration (seconds), reps
- Live bar preview — taller = more watts, wider = longer duration
- Duplicate Last Step button for quickly building interval blocks
- Plans saved to `plans.json` — persist between sessions
- **Load → Ride** sends the plan straight to the Ride tab

When a plan finishes naturally (all steps complete) the session stops and uploads automatically.

---

### Heart Rate Monitor

Any Bluetooth LE heart rate monitor works — Polar H10, Wahoo TICKR, Garmin HRM-Pro, chest straps, arm bands, smartwatches broadcasting HR over BLE.

- Scan and connect from the left panel
- HR shown as a pink dial and pink line on the chart
- Recorded into the session history and included in FIT file uploads (Strava and Garmin show HR zones)
- If you have an ANT+ HR strap paired directly to the Direto XR trainer, the trainer will broadcast it over BLE automatically — the app picks it up as a fallback when no BLE HR monitor is connected

---

### Power Meter

Any BLE Cycling Power Service compatible power meter works — Stages, 4iiii, Assioma, Favero, Quarq, SRM, Pioneer, Rotor, Shimano, SRAM, Wahoo Powrlink, etc.

- Scan and connect from the left panel
- Once connected a **Use power meter as source** toggle appears
- When enabled, power and cadence readings from the meter override the Direto's built-in sensors in both the live stats and the recorded session
- Can be toggled mid-ride to compare readings
- Cadence is extracted from crank revolution data if your meter broadcasts it

---

### Upload tab

#### Auto-upload on stop

Set your preference once and forget it:

- **Don't auto-upload** — manual only
- **Upload to Strava** — fires when you hit ■ STOP or a plan completes
- **Upload to Garmin Connect** — same trigger
- **Upload to both** — uploads to both simultaneously

A toast notification appears bottom-right confirming the upload or showing the error.

#### Strava setup (one-time)

1. Go to https://www.strava.com/settings/api and create an app (any name)
2. Set *Authorization Callback Domain* to `localhost`
3. Open `strava_config.json` (created next to `direto_server.py`) and add:
   ```json
   {
     "client_id":     "YOUR_CLIENT_ID",
     "client_secret": "YOUR_CLIENT_SECRET"
   }
   ```
4. Restart the server
5. Click **Connect Strava Account** in the Upload tab — authenticates via browser popup

#### Garmin Connect setup (one-time)

1. Click **Log in to Garmin** in the Upload tab
2. Enter your Garmin Connect email and password
3. Your session token is saved to `garmin_session/` — your password is never stored on disk
4. Done — subsequent uploads use the cached token and auto-refresh it

> **Note on Garmin 2FA**: If your account has two-factor authentication, temporarily disable it, log in to cache the token, then re-enable it. Once the token is cached you won't need to log in again.

#### Manual upload & FIT download

- **Upload Ride to Strava / Garmin** — uploads your most recent session
- **⬇ Download FIT file** — saves the session as a `.fit` file for manual import anywhere
- **Test Upload / Fake Ride Generator** — generates a realistic fake ride (warmup, intervals, cooldown) with simulated HR. Useful for testing your Strava/Garmin connections. Set duration and FTP before generating.

---

## Files

| File | Purpose |
|---|---|
| `direto_server.py` | FastAPI backend — BLE, ANT+, WebSocket, uploads |
| `direto_ui.html` | Frontend — served at http://localhost:8000 |
| `direto_xr_control.py` | Standalone Python script (no web UI) |
| `requirements.txt` | Python dependencies |
| `plans.json` | Saved workout plans (auto-created) |
| `settings.json` | Auto-upload preference and ride name template (auto-created) |
| `strava_config.json` | Your Strava API credentials (auto-created) |
| `strava_token.json` | Strava OAuth token (auto-created after auth) |
| `garmin_session/` | Garmin Connect session token folder (auto-created after login) |
| `debug_strava_last.fit` | Last FIT file sent to Strava (for debugging) |
| `debug_garmin_last.fit` | Last FIT file sent to Garmin (for debugging) |

---

## Accessing from your phone

The app is mobile-friendly. To open it on your phone while the server runs on your laptop:

1. Make sure both devices are on the same Wi-Fi network
2. Find your laptop's local IP: run `ipconfig` (Windows) or check Wi-Fi settings
3. In `direto_server.py`, change the last line from `host="127.0.0.1"` to `host="0.0.0.0"`
4. Open `http://<your-laptop-ip>:8000` on your phone

---

## Troubleshooting

**Trainer not found during scan**  
Make sure the Direto XR is powered on and not connected to the Elite app, Zwift, or any other device — BLE only allows one connection at a time. On Linux, check Bluetooth group membership.

**"Device not found" when connecting**  
Windows BLE uses randomised MAC addresses that rotate periodically. The app caches the device object from the scan to work around this, but if it persists try scanning again immediately before connecting.

**Connection fails with 0x81 error**  
This is an FTMS control point error. The app retries automatically up to 3 times. If it keeps failing, power-cycle the Direto XR (unplug the power cable for 10 seconds) and try again.

**Power and cadence showing zero**  
Start pedalling — the trainer only broadcasts non-zero values when moving. If still zero after pedalling, check the terminal for `[BLE] Indoor Bike Data flags=` lines and paste them here.

**ANT+ dongle not found**  
Make sure you ran Zadig and replaced the driver with libusb-win32. Unplug and replug the dongle. Garmin Express must not be running — it takes exclusive ownership of ANT+ dongles.

**Slope not responding**  
Confirm the ANT+ status dot in the header is orange (connected). The Direto XR applies the slope command immediately but you won't feel it until you're pedalling against it.

**Garmin upload fails with 400 error**  
The FIT file structure may be invalid. Check `debug_garmin_last.fit` by dragging it to `connect.garmin.com/modern/import-data` manually — this confirms whether it's a file issue or an API issue.

**Strava upload accepted but not appearing**  
Strava processes FIT files asynchronously — it typically takes 1–5 minutes to appear in your activity feed. Check the upload status at `https://www.strava.com/api/v3/uploads/<upload_id>`.

**Garmin session expired**  
Click **Log in to Garmin** again in the Upload tab. The `garmin_session/` folder can be deleted to force a fresh login.

**Port 8000 already in use**  
Edit the last line of `direto_server.py` and change `port=8000` to another port (e.g. `8080`), then update the WebSocket URL in `direto_ui.html` from `ws://localhost:8000/ws` to match.
