#!/usr/bin/env python3
"""
zwiftlights: Philips Hue lights that follow your power zones, Zwift-style.

It reads power straight from the smart trainer over Bluetooth (the standard
Cycling Power Service), averages it over a few seconds, works out your zone
from your FTP, and sets the lights in a Hue room to that zone's Zwift colour.
It only *listens* to the trainer, so Zwift keeps full control of resistance.

Commands:
  python3 zwiftlights.py pair       one-off: pair with the Hue bridge (press its button)
  python3 zwiftlights.py rooms      list the Hue rooms and zones the bridge knows about
  python3 zwiftlights.py scan       list nearby Bluetooth trainers / power meters
  python3 zwiftlights.py zones      print the watt range of each zone for your FTP
  python3 zwiftlights.py test       cycle the lights through every zone colour, then restore
  python3 zwiftlights.py simulate   fake ride (no trainer); add --lights to drive the real lights
  python3 zwiftlights.py run        the main thing; this is what the background service runs
                                    add --dry-run to log zone changes without touching the lights
"""

import argparse
import asyncio
import json
import logging
import ssl
import sys
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    sys.exit("zwiftlights needs Python 3.11 or newer (Raspberry Pi OS Bookworm or later).")

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.toml"
KEY_FILE = HERE / "hue_key.json"

log = logging.getLogger("zwiftlights")


# --------------------------------------------------------------------------
# Zones. Boundaries match Zwift's power zones (fraction of FTP where each
# zone starts). Colours are RGB; None means neutral white.
# Tweak colours / brightness here if you want a different look.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Zone:
    name: str
    starts_at: float          # fraction of FTP
    rgb: tuple | None         # None = white
    brightness: int           # percent


ZONES = [
    Zone("Z1 Recovery",  0.00, None,          35),   # Zwift grey -> soft white
    Zone("Z2 Endurance", 0.60, (0, 110, 255), 70),   # blue
    Zone("Z3 Tempo",     0.76, (0, 200, 60),  70),   # green
    Zone("Z4 Threshold", 0.90, (255, 190, 0), 80),   # yellow
    Zone("Z5 VO2max",    1.05, (255, 90, 0),  90),   # orange
    Zone("Z6 Anaerobic", 1.19, (255, 0, 0),   100),  # red, full blast
]

WHITE_XY = (0.3227, 0.3290)


def rgb_to_xy(rgb):
    """sRGB (0-255) -> CIE 1931 xy, which is what Hue wants."""
    if rgb is None:
        return WHITE_XY

    def lin(c):
        c /= 255
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = (lin(c) for c in rgb)
    X = r * 0.4124 + g * 0.3576 + b * 0.1805
    Y = r * 0.2126 + g * 0.7152 + b * 0.0722
    Z = r * 0.0193 + g * 0.1192 + b * 0.9505
    s = X + Y + Z
    return (round(X / s, 4), round(Y / s, 4)) if s else WHITE_XY


def zone_index(watts, ftp):
    idx = 0
    for i, z in enumerate(ZONES):
        if watts >= z.starts_at * ftp:
            idx = i
    return idx


def zone_lower_watts(i, ftp):
    return round(ZONES[i].starts_at * ftp)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
DEFAULTS = {
    "ftp": 220,
    "trainer_name": "KICKR",
    "hue_room": "Zwift Cave",
    "bridge_ip": "",
    "smoothing_seconds": 3.0,
    "hysteresis_percent": 2.0,
    "min_zone_seconds": 1.5,
    "idle_after_seconds": 90,
    "when_idle": "restore",
    "fade_ms": 400,
}


def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "rb") as f:
            cfg.update(tomllib.load(f))
    if cfg["when_idle"] not in ("restore", "off", "leave"):
        sys.exit('config.toml: when_idle must be "restore", "off" or "leave"')
    return cfg


def load_key():
    if not KEY_FILE.exists():
        return None
    return json.loads(KEY_FILE.read_text())


# --------------------------------------------------------------------------
# Hue bridge (local CLIP v2 API)
# --------------------------------------------------------------------------
# The bridge uses a self-signed certificate on your LAN, so we skip
# certificate verification for it (and only for it).
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE


class HueError(Exception):
    pass


class Hue:
    def __init__(self, ip, key=None):
        self.ip = ip
        self.key = key

    def _req(self, method, path, body=None, auth=True):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"https://{self.ip}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("hue-application-key", self.key)
        try:
            with urllib.request.urlopen(req, context=_SSL, timeout=5) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise HueError(f"{method} {path}: HTTP {e.code} {e.read()[:200]!r}") from None
        except OSError as e:
            raise HueError(f"can't reach bridge at {self.ip}: {e}") from None
        return json.loads(raw) if raw else {}

    def get(self, rtype, rid=""):
        path = f"/clip/v2/resource/{rtype}" + (f"/{rid}" if rid else "")
        return self._req("GET", path).get("data", [])

    def put_light(self, light_id, body):
        self._req("PUT", f"/clip/v2/resource/light/{light_id}", body)

    def try_pair(self):
        """Returns the app key once the link button has been pressed, else None."""
        res = self._req("POST", "/api",
                        {"devicetype": "zwiftlights#raspberrypi", "generateclientkey": True},
                        auth=False)
        item = res[0] if isinstance(res, list) and res else {}
        if "success" in item:
            return item["success"]["username"]
        err = item.get("error", {})
        if err.get("type") == 101:  # link button not pressed
            return None
        raise HueError(f"pairing failed: {err or res}")

    def groups(self):
        """Rooms and zones as {name: [light ids]}."""
        devices = {d["id"]: d for d in self.get("device")}
        out = {}
        for rtype in ("room", "zone"):
            for g in self.get(rtype):
                lights = []
                for child in g.get("children", []):
                    if child["rtype"] == "light":
                        lights.append(child["rid"])
                    elif child["rtype"] == "device":
                        dev = devices.get(child["rid"], {})
                        lights += [s["rid"] for s in dev.get("services", []) if s["rtype"] == "light"]
                out[g["metadata"]["name"]] = {"type": rtype, "lights": lights}
        return out


def discover_bridge_ip():
    """Ask Philips' discovery service for the bridge on this network."""
    try:
        with urllib.request.urlopen("https://discovery.meethue.com/", timeout=10) as r:
            found = json.loads(r.read())
        if found:
            return found[0]["internalipaddress"]
    except Exception as e:  # noqa: BLE001
        log.debug("discovery failed: %s", e)
    return None


def connect_hue(cfg):
    saved = load_key()
    if not saved:
        sys.exit("Not paired with the Hue bridge yet. Run:  python3 zwiftlights.py pair")
    ip = cfg["bridge_ip"] or saved.get("bridge_ip")
    return Hue(ip, saved["key"])


def light_ids_for_room(hue, room_name):
    groups = hue.groups()
    for name, g in groups.items():
        if name.lower() == room_name.lower():
            if not g["lights"]:
                sys.exit(f'Hue {g["type"]} "{name}" has no lights in it.')
            return g["lights"]
    names = ", ".join(f'"{n}"' for n in groups) or "(none)"
    sys.exit(f'No Hue room or zone called "{room_name}". Found: {names}\n'
             f'Set hue_room in config.toml to one of these.')


# --------------------------------------------------------------------------
# Lights
# --------------------------------------------------------------------------
class Lights:
    def __init__(self, hue, light_ids, fade_ms, dry_run=False):
        self.hue = hue
        self.ids = light_ids
        self.fade_ms = fade_ms
        self.dry_run = dry_run
        self.saved = None  # pre-ride state, for restoring afterwards

    def _put_all(self, body):
        if self.dry_run:
            return
        for lid in self.ids:
            try:
                self.hue.put_light(lid, body)
            except HueError as e:
                log.warning("Hue: %s", e)

    def snapshot(self):
        if self.dry_run or self.saved is not None:
            return
        saved = {}
        for lid in self.ids:
            try:
                d = self.hue.get("light", lid)[0]
            except (HueError, IndexError) as e:
                log.warning("Couldn't read light state (%s); won't restore it.", e)
                continue
            state = {"on": {"on": d["on"]["on"]}}
            if "dimming" in d:
                state["dimming"] = {"brightness": d["dimming"]["brightness"]}
            ct = d.get("color_temperature", {})
            if ct.get("mirek_valid") and ct.get("mirek"):
                state["color_temperature"] = {"mirek": ct["mirek"]}
            elif "color" in d:
                state["color"] = {"xy": d["color"]["xy"]}
            saved[lid] = state
        self.saved = saved

    def show_zone(self, i):
        z = ZONES[i]
        x, y = rgb_to_xy(z.rgb)
        self._put_all({
            "on": {"on": True},
            "dimming": {"brightness": z.brightness},
            "color": {"xy": {"x": x, "y": y}},
            "dynamics": {"duration": self.fade_ms},
        })

    def go_idle(self, mode):
        if mode == "leave" or self.dry_run:
            self.saved = None
            return
        if mode == "off" or not self.saved:
            self._put_all({"on": {"on": False}, "dynamics": {"duration": 2000}})
        else:
            for lid, state in self.saved.items():
                try:
                    self.hue.put_light(lid, {**state, "dynamics": {"duration": 2000}})
                except HueError as e:
                    log.warning("Hue: %s", e)
        self.saved = None


# --------------------------------------------------------------------------
# Power -> zone logic (no Bluetooth or Hue in here, so it's easy to test)
# --------------------------------------------------------------------------
class ZoneTracker:
    """Smooths power and decides when the zone has really changed.

    - averages power over `smoothing_seconds`
    - hysteresis: to drop a zone you must fall a little *below* the boundary,
      so riding right on a boundary doesn't make the lights flicker
    - a new zone must hold for `min_zone_seconds` before the lights change
    """

    def __init__(self, ftp, smoothing_seconds=3.0, hysteresis_percent=2.0, min_zone_seconds=1.5):
        self.ftp = ftp
        self.window = smoothing_seconds
        self.hyst = hysteresis_percent / 100 * ftp
        self.min_hold = min_zone_seconds
        self.samples = deque()
        self.current = None
        self.pending = None
        self.pending_since = 0.0
        self.last_pedal = 0.0

    def add(self, watts, now):
        self.samples.append((now, watts))
        if watts > 0:
            self.last_pedal = now

    def average(self, now):
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        if not self.samples:
            return 0.0
        return sum(w for _, w in self.samples) / len(self.samples)

    def update(self, now):
        """Returns the new zone index when the zone changes, else None."""
        avg = self.average(now)
        cand = zone_index(avg, self.ftp)
        if (self.current is not None and cand < self.current
                and avg >= ZONES[self.current].starts_at * self.ftp - self.hyst):
            cand = self.current  # inside the hysteresis band: stay put
        if cand == self.current:
            self.pending = None
            return None
        if self.pending != cand:
            self.pending, self.pending_since = cand, now
            if self.current is not None:
                return None
        if self.current is None or now - self.pending_since >= self.min_hold:
            self.current, self.pending = cand, None
            return cand
        return None

    def reset(self):
        self.samples.clear()
        self.current = self.pending = None


class Controller:
    def __init__(self, cfg, lights):
        self.cfg = cfg
        self.lights = lights
        self.tracker = ZoneTracker(cfg["ftp"], cfg["smoothing_seconds"],
                                   cfg["hysteresis_percent"], cfg["min_zone_seconds"])
        self.riding = False

    def on_power(self, watts, now=None):
        now = time.monotonic() if now is None else now
        self.tracker.add(watts, now)
        if watts > 0 and not self.riding:
            self.riding = True
            log.info("Pedalling detected, taking over the lights.")
            self.lights.snapshot()

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if not self.riding:
            return
        if now - self.tracker.last_pedal > self.cfg["idle_after_seconds"]:
            log.info("No pedalling for %ss, ride over (%s lights).",
                     self.cfg["idle_after_seconds"], self.cfg["when_idle"])
            self.riding = False
            self.tracker.reset()
            self.lights.go_idle(self.cfg["when_idle"])
            return
        new = self.tracker.update(now)
        if new is not None:
            log.info("%-13s avg %4.0f W", ZONES[new].name, self.tracker.average(now))
            self.lights.show_zone(new)


# --------------------------------------------------------------------------
# Bluetooth (Cycling Power Service)
# --------------------------------------------------------------------------
CPS_SERVICE = "00001818-0000-1000-8000-00805f9b34fb"
CPS_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"


def parse_cps(data):
    """Cycling Power Measurement: 2 bytes flags, then instantaneous power (sint16, W)."""
    return int.from_bytes(data[2:4], "little", signed=True)


async def ble_loop(cfg, controller):
    from bleak import BleakClient, BleakScanner

    want = cfg["trainer_name"].lower()

    def matches(dev, adv):
        return want in (dev.name or adv.local_name or "").lower()

    announced_missing = False
    while True:
        dev = await BleakScanner.find_device_by_filter(matches, timeout=15)
        if dev is None:
            if not announced_missing:
                log.info('Waiting for "%s" (is the trainer awake?)', cfg["trainer_name"])
                announced_missing = True
            await asyncio.sleep(5)
            continue
        announced_missing = False

        gone = asyncio.Event()
        try:
            async with BleakClient(dev, disconnected_callback=lambda _c: gone.set()) as client:
                await client.start_notify(
                    CPS_MEASUREMENT, lambda _h, data: controller.on_power(parse_cps(data)))
                log.info("Connected to %s, listening for power.", dev.name)
                await gone.wait()
                log.info("Trainer disconnected.")
        except Exception as e:  # noqa: BLE001 (keep the service alive whatever happens)
            log.warning("Bluetooth: %s", e)
        await asyncio.sleep(3)


async def tick_loop(controller):
    while True:
        controller.tick()
        await asyncio.sleep(0.5)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_pair(cfg, _args):
    ip = cfg["bridge_ip"] or discover_bridge_ip()
    if not ip:
        sys.exit("Couldn't find the bridge automatically. Look up its IP in the Hue app\n"
                 "(Settings > Bridges > (i)) and put it in config.toml as bridge_ip.")
    hue = Hue(ip)
    print(f"Found Hue bridge at {ip}.")
    print("Press the button on top of the bridge now. Waiting up to 60 seconds...")
    deadline = time.time() + 60
    while time.time() < deadline:
        key = hue.try_pair()
        if key:
            KEY_FILE.write_text(json.dumps({"bridge_ip": ip, "key": key}, indent=2))
            KEY_FILE.chmod(0o600)
            print(f"Paired! Key saved to {KEY_FILE.name}.")
            print("Next:  python3 zwiftlights.py rooms")
            return
        time.sleep(2)
    sys.exit("Timed out. Run pair again and press the bridge button within a minute.")


def cmd_rooms(cfg, _args):
    hue = connect_hue(cfg)
    for name, g in hue.groups().items():
        marker = "  <- hue_room" if name.lower() == cfg["hue_room"].lower() else ""
        print(f'{g["type"]:5}  "{name}"  ({len(g["lights"])} lights){marker}')


def cmd_scan(_cfg, _args):
    from bleak import BleakScanner

    async def go():
        print("Scanning Bluetooth for 10 seconds (pedal the trainer to wake it)...")
        found = await BleakScanner.discover(timeout=10, return_adv=True)
        rows = []
        for dev, adv in found.values():
            name = dev.name or adv.local_name
            if not name:
                continue
            power = CPS_SERVICE in [u.lower() for u in adv.service_uuids]
            rows.append((not power, name, dev.address, power))
        for _, name, addr, power in sorted(rows):
            print(f'{"POWER " if power else "      "} {name:30} {addr}')
        print("\nPut the name of your trainer (or part of it) in config.toml as trainer_name.")

    asyncio.run(go())


def cmd_zones(cfg, _args):
    ftp = cfg["ftp"]
    print(f"FTP {ftp} W\n")
    for i, z in enumerate(ZONES):
        lo = zone_lower_watts(i, ftp)
        hi = f"{zone_lower_watts(i + 1, ftp) - 1:>4} W" if i + 1 < len(ZONES) else "   up"
        colour = "white" if z.rgb is None else f"rgb{z.rgb}"
        print(f"{z.name:13} {lo:>4} - {hi}   {colour}, {z.brightness}% brightness")


def cmd_test(cfg, _args):
    hue = connect_hue(cfg)
    lights = Lights(hue, light_ids_for_room(hue, cfg["hue_room"]), cfg["fade_ms"])
    lights.snapshot()
    try:
        for i, z in enumerate(ZONES):
            print(z.name)
            lights.show_zone(i)
            time.sleep(2.5)
    finally:
        print("Restoring lights.")
        lights.go_idle("restore")


def cmd_simulate(cfg, args):
    """A fake ride: warm-up, intervals, sprint, cool-down. Runs in real time (~1 min)."""
    if args.lights:
        hue = connect_hue(cfg)
        lights = Lights(hue, light_ids_for_room(hue, cfg["hue_room"]), cfg["fade_ms"])
    else:
        lights = Lights(None, [], cfg["fade_ms"], dry_run=True)
    sim_cfg = {**cfg, "idle_after_seconds": 5}
    ctl = Controller(sim_cfg, lights)
    ftp = cfg["ftp"]
    ride = [(0.50, 6), (0.70, 6), (0.82, 6), (0.97, 6), (0.91, 4), (1.10, 6),
            (1.40, 5), (0.65, 6), (0.0, 8)]
    print(f"Simulated ride at FTP {ftp} W ({'real lights' if args.lights else 'dry run'})\n")
    for frac, secs in ride:
        for _ in range(secs * 4):
            ctl.on_power(round(frac * ftp))
            ctl.tick()
            time.sleep(0.25)


def cmd_run(cfg, args):
    if args.dry_run:
        lights = Lights(None, [], cfg["fade_ms"], dry_run=True)
        log.info("Dry run: zone changes are logged, lights are left alone.")
    else:
        hue = connect_hue(cfg)
        ids = light_ids_for_room(hue, cfg["hue_room"])
        lights = Lights(hue, ids, cfg["fade_ms"])
        log.info('Hue room "%s": %d lights.', cfg["hue_room"], len(ids))
    log.info("FTP %d W, trainer \"%s\".", cfg["ftp"], cfg["trainer_name"])
    controller = Controller(cfg, lights)

    async def main():
        await asyncio.gather(ble_loop(cfg, controller), tick_loop(controller))

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if controller.riding:
            lights.go_idle(cfg["when_idle"])


def main():
    p = argparse.ArgumentParser(description="Hue lights that follow your Zwift power zones.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("pair", "rooms", "scan", "zones", "test"):
        sub.add_parser(name)
    s = sub.add_parser("simulate")
    s.add_argument("--lights", action="store_true", help="drive the real Hue lights")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="log zone changes, don't touch lights")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    cfg = load_config()
    {"pair": cmd_pair, "rooms": cmd_rooms, "scan": cmd_scan, "zones": cmd_zones,
     "test": cmd_test, "simulate": cmd_simulate, "run": cmd_run}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
