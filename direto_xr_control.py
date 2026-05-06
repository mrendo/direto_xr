"""
Elite Direto XR - Python BLE Controller
Requires: pip install bleak
Tested with Python 3.8+

The Direto XR uses standard BLE FTMS (Fitness Machine Service) for control
and Cycling Power Service for power readings.
"""

import asyncio
import struct
from bleak import BleakClient, BleakScanner

# ── BLE UUIDs ──────────────────────────────────────────────────────────────────
FTMS_SERVICE_UUID               = "00001826-0000-1000-8000-00805f9b34fb"
FTMS_CONTROL_POINT_UUID         = "00002ad9-0000-1000-8000-00805f9b34fb"
FTMS_STATUS_UUID                = "00002ada-0000-1000-8000-00805f9b34fb"
INDOOR_BIKE_DATA_UUID           = "00002ad2-0000-1000-8000-00805f9b34fb"
CYCLING_POWER_MEASUREMENT_UUID  = "00002a63-0000-1000-8000-00805f9b34fb"

# ── FTMS Control Point opcodes ─────────────────────────────────────────────────
OP_REQUEST_CONTROL  = 0x00
OP_RESET            = 0x01
OP_SET_TARGET_POWER = 0x05   # ERG mode — set watts
OP_SET_RESISTANCE   = 0x04   # 0–100 (percentage of max resistance)
OP_START_RESUME     = 0x07
OP_STOP_PAUSE       = 0x08

RESPONSE_SUCCESS    = 0x01


# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_indoor_bike_data(data: bytearray) -> dict:
    """Parse FTMS Indoor Bike Data characteristic (0x2AD2)."""
    flags = struct.unpack_from("<H", data, 0)[0]
    offset = 2
    result = {}

    # Instantaneous Speed (always present unless "More Data" flag set)
    if not (flags & 0x0001):
        result["speed_kmh"] = struct.unpack_from("<H", data, offset)[0] * 0.01
        offset += 2

    if flags & 0x0004:  # Average Speed
        result["avg_speed_kmh"] = struct.unpack_from("<H", data, offset)[0] * 0.01
        offset += 2

    if flags & 0x0008:  # Instantaneous Cadence
        result["cadence_rpm"] = struct.unpack_from("<H", data, offset)[0] * 0.5
        offset += 2

    if flags & 0x0020:  # Instantaneous Power
        result["power_w"] = struct.unpack_from("<h", data, offset)[0]  # signed
        offset += 2

    return result


async def find_direto(name_hint: str = "Direto") -> str | None:
    """Scan for the trainer and return its BLE address."""
    print("Scanning for Elite Direto XR…")
    devices = await BleakScanner.discover(timeout=10.0)
    for d in devices:
        if d.name and name_hint.lower() in d.name.lower():
            print(f"  Found: {d.name}  [{d.address}]")
            return d.address
    print("  Not found. Make sure the trainer is powered on and not connected elsewhere.")
    return None


# ── Core controller ────────────────────────────────────────────────────────────

class DiреtoXR:
    def __init__(self, address: str):
        self.address = address
        self.client = BleakClient(address)
        self._latest_data: dict = {}

    async def connect(self):
        await self.client.connect()
        print(f"Connected to {self.address}")

        # Subscribe to indoor bike data notifications
        await self.client.start_notify(
            INDOOR_BIKE_DATA_UUID, self._on_bike_data
        )

        # Request FTMS control
        await self._write_control_point(bytes([OP_REQUEST_CONTROL]))
        print("FTMS control granted.")

    async def disconnect(self):
        await self._write_control_point(bytes([OP_RESET]))
        await self.client.stop_notify(INDOOR_BIKE_DATA_UUID)
        await self.client.disconnect()
        print("Disconnected.")

    async def set_target_power(self, watts: int):
        """ERG mode: hold a specific power target (0–4000 W)."""
        watts = max(0, min(4000, watts))
        payload = struct.pack("<Bh", OP_SET_TARGET_POWER, watts)
        await self._write_control_point(payload)
        print(f"  → Target power set to {watts} W")

    async def set_resistance(self, level: int):
        """Resistance mode: 0 (none) to 100 (max)."""
        level = max(0, min(100, level))
        payload = struct.pack("<BB", OP_SET_RESISTANCE, level)
        await self._write_control_point(payload)
        print(f"  → Resistance set to {level}%")

    async def start(self):
        await self._write_control_point(bytes([OP_START_RESUME]))
        print("  → Training started/resumed.")

    async def stop(self):
        await self._write_control_point(bytes([OP_STOP_PAUSE, 0x01]))  # 0x01 = stop
        print("  → Training stopped.")

    @property
    def latest_data(self) -> dict:
        return self._latest_data

    def _on_bike_data(self, _sender, data: bytearray):
        self._latest_data = parse_indoor_bike_data(data)

    async def _write_control_point(self, payload: bytes):
        await self.client.write_gatt_char(
            FTMS_CONTROL_POINT_UUID, payload, response=True
        )


# ── Example workout ────────────────────────────────────────────────────────────

async def example_erg_workout(address: str):
    """
    Simple ERG workout:
      3 min warm-up  @ 100 W
      5 min interval @ 250 W
      2 min cooldown @ 80 W
    """
    trainer = DiреtoXR(address)
    await trainer.connect()
    await trainer.start()

    plan = [
        (100, 180, "Warm-up"),
        (250, 300, "Interval"),
        (80,  120, "Cooldown"),
    ]

    try:
        for watts, duration, label in plan:
            await trainer.set_target_power(watts)
            print(f"\n[{label}] {watts} W for {duration}s")

            for elapsed in range(duration):
                await asyncio.sleep(1)
                d = trainer.latest_data
                power   = d.get("power_w", "—")
                cadence = d.get("cadence_rpm", "—")
                speed   = d.get("speed_kmh", "—")
                print(
                    f"  {elapsed+1:>3}s  "
                    f"Power: {power:>4} W  "
                    f"Cadence: {cadence:>5} rpm  "
                    f"Speed: {speed:>5} km/h",
                    end="\r"
                )
            print()  # newline after segment

    finally:
        await trainer.stop()
        await trainer.disconnect()


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    address = await find_direto("Direto")
    if address is None:
        return

    await example_erg_workout(address)


if __name__ == "__main__":
    asyncio.run(main())
