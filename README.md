# Direto XR Controller

A local web app to control your Elite Direto XR smart trainer. Connect via Bluetooth for ERG mode and telemetry, and via ANT+ for slope simulation. Record sessions and upload automatically to Strava and Garmin Connect.

---

## Requirements

- **Python 3.10 or newer**
- Elite Direto XR (or compatible FTMS trainer) powered on and not connected to another app
- Bluetooth adapter
- *(Optional)* ANT+ USB dongle for slope/gradient simulation

---

## Installation

### Windows

1. Install Python from https://www.python.org — tick **"Add Python to PATH"**
2. Double-click **`scripts\install.bat`**
3. Double-click **`scripts\start.bat`** — browser opens at http://localhost:8000

### macOS / Linux

```bash
chmod +x scripts/install.sh scripts/start.sh
./scripts/install.sh
./scripts/start.sh
```

> **macOS:** System Settings → Privacy & Security → Bluetooth → allow Terminal  
> **Linux:** `sudo usermod -aG bluetooth $USER` then log out and back in

---

## ANT+ Setup (Windows — one-time, required for slope mode)

The Direto XR does not support slope simulation over Bluetooth. An ANT+ USB dongle with a libusb driver is required.

1. Download **Zadig** from https://zadig.akeo.ie
2. Plug in your ANT+ USB dongle
3. In Zadig: Options → List All Devices
4. Select your dongle (usually "ANT USB Stick 2" or "ANTUSB-m")
5. Set driver to **libusb-win32** → click **Replace Driver**

> ⚠️ After installing the libusb driver the dongle won't work with Garmin Express until you switch back in Device Manager.

---

## Tabs

### Ride

The main screen. Left panel contains all controls:

**ERG Mode** — set target power in watts. Large display shows current target with −25/−5/+5/+25 nudge buttons for easy adjustment while riding.

**Slope** — set gradient from −10% to +24%. Requires ANT+ trainer connected. Nudge buttons: −5%/−1%/+1%/+5%.

**Quick Intervals** — build an ad-hoc workout on the fly. Each row has watts, duration (seconds), reps, and optional grade %. Tap + Step to add rows.

**Bottom buttons (pinned):**
- **Load Plan** — loads a saved plan from the Plans tab into Quick Intervals
- **▶ Start Plan / ■ Stop Plan** — runs the Quick Intervals as a structured workout, recording automatically
- **▶ Start Free Ride / ■ Stop Free Ride** — records an unstructured ride

The centre shows:
- **Mode bar** — FREE RIDE / ERG MODE / INTERVAL with current step info during intervals
- **3×2 stat grid** — Power, Heart Rate, Cadence, Speed, Distance, Time
- **Interval progress bar** — step progress, dots, and time remaining (visible during intervals)
- **Live chart** — power, HR, and cadence over time with dual Y-axis

Speed and distance default to **mph/mi** — tap the Speed or Distance block to toggle to km.

---

### Connect

Connection hub for all devices. Four cards:

**BLE Trainer** — scan and connect to the Direto XR over Bluetooth. Shows the device address for identification.

**ANT+ Dongle** — connect the USB dongle (requires Zadig driver). Once connected, ANT+ HR scanning becomes available.

**ANT+ Trainer** — scan for the trainer on ANT+ FEC channel. Required for slope control.

**Heart Rate Monitor** — two sections:
- *BLE* — scan for Bluetooth HR straps (Polar H10, Wahoo TICKR, etc.)
- *ANT+* — scan for ANT+ HR straps (requires dongle)

**Power Meter** — scan and connect a BLE power meter. Toggle "Use power meter as source" to override the trainer's built-in power and cadence readings.

A **Connection Log** at the bottom shows live connection activity.

---

### Plans

Create and manage structured workout plans using the plan editor. Each step has a label, watts, duration, reps, and optional grade %. A live bar preview shows the workout shape.

- **New Plan** — start a fresh plan
- **Duplicate Last Step** — quickly build interval blocks
- **Load → Ride** — sends the plan to the Quick Intervals on the Ride tab
- Plans are saved automatically to `plans.json`

---

### Upload

**Auto-upload** — set your preference (none / Strava / Garmin / both). Fires automatically when a ride or plan stops.

**Strava setup:**
1. Create an app at https://www.strava.com/settings/api — set callback domain to `localhost`
2. Add your client ID and secret to `strava_config.json`
3. Click **Connect Strava Account** — authenticates via browser

**Garmin setup:**
1. Click **Log in to Garmin** — enter your Garmin Connect credentials
2. Session token saved to `garmin_session/` — password never stored

**Manual upload** — upload the most recent session to Strava or Garmin, or download as a `.fit` file.

**Test ride generator** — generates a realistic fake ride (warmup → intervals → cooldown) with simulated HR. Set duration and FTP. Useful for testing upload connections without riding.

---

### Settings

**Shut Down** — disconnects all BLE and ANT+ devices cleanly, then stops the server. The browser tab shows "Server stopped" when complete.

**Log** — full activity log for the session. All connection events, recording starts/stops, upload results, and errors.

---

## Header Status Dots

Five indicators across the top right, always visible:

| Dot | Colour | Meaning |
|-----|--------|---------|
| ANT+ Dongle | Orange | Dongle connected and node running |
| ANT+ Trainer | Orange | FEC trainer channel open |
| No Power Meter | Orange | BLE power meter connected |
| HR Monitor | Pink | Heart rate monitor connected (BLE or ANT+) |
| BLE Trainer | Green | Trainer connected over Bluetooth |

On mobile, labels hide and only dots show.

---

## Files

| File | Purpose |
|------|---------|
| `direto_server.py` | FastAPI backend — BLE, ANT+, WebSocket, uploads |
| `direto_ui.html` | Frontend — served at http://localhost:8000 |
| `requirements.txt` | Python dependencies |
| `plans.json` | Saved workout plans *(auto-created, gitignored)* |
| `settings.json` | Auto-upload preference and ride name *(auto-created, gitignored)* |
| `strava_config.json` | Strava API credentials *(gitignored — add manually)* |
| `strava_token.json` | Strava OAuth token *(auto-created after auth, gitignored)* |
| `garmin_session/` | Garmin Connect session token *(auto-created after login, gitignored)* |

---

## Mobile Access

1. Find your laptop's local IP: run `ipconfig` (Windows) or check Wi-Fi settings
2. In `direto_server.py`, change the last line from `host="127.0.0.1"` to `host="0.0.0.0"`
3. Open `http://<your-laptop-ip>:8000` on your phone

---

## Troubleshooting

**Trainer not found during scan**  
Make sure the Direto XR is powered on and not connected to the Elite app, Zwift, or any other device — BLE allows only one connection at a time.

**Connection fails or drops**  
Power-cycle the trainer (unplug for 10 seconds). On Windows, try scanning again immediately after power-on before BLE addresses rotate.

**Slope not responding**  
Confirm the ANT+ Trainer dot is orange in the header. The Direto XR only applies slope when pedalling — you won't feel it at standstill. Make sure you clicked **Scan for ANT+ Trainer** after connecting the dongle.

**ANT+ dongle errors on connect**  
Unplug and replug the dongle after installing the Zadig driver. Only click Connect ANT+ Dongle once — multiple rapid clicks cause channel state errors.

**HR shows stale value after removing strap**  
HR clears automatically after 5 seconds of no signal.

**Garmin upload fails**  
Click **Log in to Garmin** again in the Upload tab. Delete `garmin_session/` to force a fresh login if needed.

**Strava upload accepted but not appearing**  
Strava processes FIT files asynchronously — allow 1–5 minutes. Check activity feed or https://www.strava.com/athlete/training.

**Port 8000 already in use**  
Change `port=8000` at the bottom of `direto_server.py` to another port (e.g. `8080`) and update the WebSocket URL in `direto_ui.html` from `ws://localhost:8000/ws` to match.

**"No ride data recorded" on stop**  
The trainer must be connected over BLE and you need to be pedalling before recording starts — data points only arrive when the trainer is broadcasting.
