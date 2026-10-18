#!/usr/bin/env python3
"""
sc_locator.py - Star Citizen coordinate locator

Reads the r_DisplayInfo debug text off the screen (screen capture + OCR only,
no game memory access), converts the planet-frame position into latitude /
longitude / height, and guides you back to saved waypoints with distance,
bearing and a turn hint.

Enable the debug HUD in the game console (~) with:
  r_DisplayInfo 2
The position row it prints looks like:
  Zone: <name> Pos: X Y Z
Coordinates are local to the body's ObjectContainer, so they are unaffected
by the planet's orbit and rotation. The HUD does not print a radius, so it is
looked up from BODY_RADII using the zone name.

Usage:
  python sc_locator.py calibrate [--find] [--delay N] [--region L T R B]
                                [--cut N] [--save]
  python sc_locator.py run [--console]
  python sc_locator.py list
  python sc_locator.py rename OLD NEW
  python sc_locator.py delete NAME
  python sc_locator.py convert X Y Z [--radius 1000000]

Left-click the pin to save, right-click it to open the panel; drag the readout
with the left mouse button to move it.
"""
import argparse
import ctypes
import functools
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:            # the maths lives beside this file
    sys.path.insert(0, str(HERE))
import heightmap                         # noqa: E402
import spin_analysis as spin             # noqa: E402
import stations                          # noqa: E402
import survey                            # noqa: E402
import traces                            # noqa: E402

# A one-file .exe unpacks to a temp folder that is deleted on exit, so data is
# kept beside the .exe instead. BUNDLE is where the packed Tesseract lives.
FROZEN = getattr(sys, "frozen", False)
BUNDLE = Path(getattr(sys, "_MEIPASS", HERE))
if FROZEN:
    HERE = Path(sys.executable).resolve().parent

CONFIG_FILE = HERE / "locator_config.json"
WAYPOINT_FILE = HERE / "waypoints.json"
BODIES_FILE = HERE / "bodies.json"       # what landing on a body taught us
STATIONS_FILE = HERE / "stations.json"   # UEX's station list, re-fetched weekly
HEIGHTMAP_FILE = HERE / "heightmap.json" # ground heights from survey mode
SURVEY_FILE = HERE / "survey.json"       # the last survey square, to resume
TRACES_FILE = HERE / "traces.json"       # railways traced, see traces.py
BACKUP_DIR = HERE / "bak"
BACKUP_KEEP = 30                         # versions of each file kept in bak/
# How often a running survey is written out. Each write also keeps a backup,
# so thirty of them cover the last hour.
HEIGHTMAP_SAVE_SECONDS = 120.0
TRACES_SAVE_SECONDS = 30.0               # and a running trace
# Fastest a survey sample may have moved since the last fix. Flight in
# atmosphere stays well under it; a misread coordinate moves the fix hundreds
# of km in a second.
SURVEY_MAX_SPEED = 3000.0                # m/s
# Fastest a trace point may climb or drop since the last one. The metro
# changed height at ~20 m/s; a misread digit, by kilometres in seconds.
TRACE_MAX_CLIMB = 100.0                  # m/s
# A survey starting this close to a survey waypoint does not add another.
SURVEY_MARK_SPACING_M = 5000.0

TESS_CONFIG = "--psm 6 -c preserve_interword_spaces=1"

DEFAULT_CONFIG = {
    "monitor": 1,                        # mss monitor index (1 = primary)
    # left, top, right, bottom as screen fractions. The HUD block is pinned to
    # the top-right corner, but how far down the CamPos row sits depends on how
    # many rows precede it, which varies by location and graphics settings. So
    # this starts at the very top rather than guessing an offset. Run
    # `calibrate --find` to narrow the sides to your own screen.
    "region": [0.60, 0.00, 1.00, 0.28],
    "scale": 2.0,                        # upscale factor before OCR
    # Text is found by local contrast, not by one brightness cut-off: see
    # preprocess. The radius is the neighbourhood each pixel is compared with,
    # in post-scale pixels, and must be wider than a stroke. The cut is how
    # much brighter than its surroundings a pixel must be to count as ink.
    "contrast_radius": 6,
    "contrast_cut": 28,
    # Retried in order when the configured cut yields no fix; whichever works
    # is promoted to the front. A faint row and a glaring one want different
    # cuts, and both occur within a few seconds of each other.
    "contrast_cuts": [28, 18, 40, 12],
    "interval": 0.5,                     # seconds between captures (OCR time sets the floor)
    # With the game closed there is nothing to read, and each miss costs one
    # Tesseract process per contrast cut in the ladder. Idle polling only looks
    # for the game, which is why it can be this slow without being noticed.
    "idle_interval": 5.0,                # seconds between checks while idle
    "game_process": ["starcitizen.exe"], # any match means the game is up
    # Above this height over the reference sphere you are called to be in
    # space rather than at the body, however far the body's zone reaches. A
    # zone extends Gm beyond its surface -- 55 Gm from Crusader is still
    # inside one -- so being in a zone says almost nothing about where you
    # are. This is the Karman line idea: an arbitrary height that makes the
    # answer useful. It changes what is printed, not where a waypoint is
    # saved; while there is a planet fix, saving stays in the planet frame.
    "space_line_m": 100000.0,
    # And a second line, far out, where "near <body>" stops being honest. Both
    # are arbitrary; this one is placed by measurement. Everything measured
    # that is genuinely in orbit sits far below it -- Everus Harbor 148 km,
    # Grim HEX 373 km -- while the Crusader mining base, which does not turn
    # with the planet (measured 2026-10-05), sits at 85.17 Mm and HUR-L3,
    # whose zone is Crusader's, at 6.67 Gm. 5 Mm leaves more than an order of
    # magnitude of clearance either side. Above it the body is still
    # named, because whose zone you are in is the only bearing there is, but
    # the distance is stated instead of claimed to be small.
    "far_line_m": 5.0e6,
    # Survey samples are only taken below this height over the reference
    # sphere. On a logged climb (microTech 2026-10-14) ElevationUnderCam read
    # right whenever it read at all, up to 25 km, but between 7 and 15 km it
    # never read, and it was seen changing at random there by eye. A wrong
    # value would still satisfy the height identity, so only height can keep
    # it out; below 5 km four frames in five read. Measured from the
    # position, not the HUD's heights, which are the suspect part.
    "survey_max_height_m": 5000.0,
    # A lake survey starts skimming the water, and its level is the camera's
    # height over the reference sphere at that first fix less this much. The
    # HUD cannot give it: over a microTech lake its ground read up to 73 m
    # above the water, neither the bed nor the surface (2026-10-17).
    "water_below_cam_m": 2.0,
    "stale_after": 3.0,                # seconds before a fix is flagged stale
    "default_radius": 1000000.0,         # used when the zone is not in BODY_RADII
    "min_move": 20.0,                    # metres moved before heading is updated
    "space_min_move": 200.0,             # metres moved in deep space before the track updates
    "space_track_age": 30.0,             # seconds a deep-space track stays usable
    "space_min_time": 2.0,               # seconds between the readings a track is measured over
    "mirror": False,                     # flip left/right turn hint if it feels reversed
    "mirror_pitch": False,               # flip up/down in the NOSE and TRK hints
    "tesseract_cmd": None,               # e.g. r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    # The pin is anchored to a corner, not placed at an absolute point, so the
    # panel can grow without walking off the screen. "br" also keeps it clear
    # of the capture region in the top right, which it would otherwise read.
    "overlay_anchor": "br",              # br | bl | tr | tl
    "overlay_margin": [20, 20],          # pixels in from that corner
    "alpha": 0.85,
}

# ----------------------------------------------------------------------------
# JSON helpers (the previous version is kept in bak/ before being overwritten)
# ----------------------------------------------------------------------------


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"warning: could not read {path.name}: {exc}", file=sys.stderr)
    return default


def backup_name(path, stamp=None):
    """Free path in bak/ for a copy of `path`, as '<name>.<stamp>-<n>.bak'.

    Several saves can land in the same second -- double-clicking the readout
    does it -- so the name always carries a counter rather than overwriting
    the copy already there. Both parts are fixed-width and zero-padded so that
    sorting these names sorts them by age, which is what prune_backups relies
    on: an unpadded counter sorts "-10" before "-2" and prunes the wrong
    versions.

    The counter is one past the highest in use, never the lowest free slot.
    Pruning empties the early numbers, and reusing one would make the newest
    backup sort as the oldest and be deleted on the very next save.
    """
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    prefix = f"{path.name}.{stamp}-"
    used = []
    for existing in BACKUP_DIR.glob(f"{prefix}*.bak"):
        tail = existing.name[len(prefix):-len(".bak")]
        if tail.isdigit():
            used.append(int(tail))
    return BACKUP_DIR / f"{prefix}{max(used) + 1 if used else 0:03d}.bak"


def prune_backups(name, keep=BACKUP_KEEP):
    """Drop all but the newest `keep` backups of one file. Timestamped names
    are fixed-width and zero-padded, so sorting them is sorting by age."""
    old = sorted(BACKUP_DIR.glob(f"{name}.*.bak"))
    for stale in old[:max(0, len(old) - keep)]:
        try:
            stale.unlink()
        except OSError:
            pass


def save_json(path, data, indent=2):
    """Write JSON, keeping the previous BACKUP_KEEP versions in bak/.

    Waypoints are collected by flying to each spot, so they cost real time to
    replace and almost nothing to keep copies of. One overwritten .bak gave a
    single chance to notice a bad write before the original was gone.
    """
    if path.exists():
        BACKUP_DIR.mkdir(exist_ok=True)
        shutil.copy2(path, backup_name(path))
        prune_backups(path.name)
    path.write_text(json.dumps(data, indent=indent), encoding="utf-8")


def backfill_systems(wps):
    """Give older waypoints their system. Returns True if any were changed.

    Waypoints saved before systems existed carry only a body, and every body
    the table knows belongs to exactly one system, so this is a lookup rather
    than a guess. A body the table does not know is left alone rather than
    filed under a system it may not be in.
    """
    changed = False
    for w in wps:
        if "system" not in w and w.get("planet") in BODY_SYSTEM:
            w["system"] = BODY_SYSTEM[w["planet"]]
            changed = True
    return changed


# What a waypoint is for: a place you want to find again, or where a survey
# started. Kept apart so the survey markers do not crowd the star map.
WAYPOINT_TYPES = ("poi", "survey")


def backfill_types(wps):
    """Give older waypoints their type. Returns True if any were changed.

    Survey markers used to carry survey=True instead; that becomes the type.
    Everything else was saved by hand, so is a point of interest.
    """
    changed = False
    for w in wps:
        if "survey" in w:
            w["type"] = "survey" if w.pop("survey") else w.get("type", "poi")
            changed = True
        elif "type" not in w:
            w["type"] = "poi"
            changed = True
    return changed


def load_waypoints():
    """Waypoints from disk, with any missing system or type filled in and
    saved back."""
    wps = load_json(WAYPOINT_FILE, [])
    if backfill_systems(wps) | backfill_types(wps):
        save_json(WAYPOINT_FILE, wps)
    return wps


def load_bodies():
    """What past analyses found, keyed by body. Empty if there are none yet."""
    data = load_json(BODIES_FILE, {})
    return data if isinstance(data, dict) else {}


def load_heightmap():
    """Surveyed ground heights, keyed by body. Empty if there are none yet."""
    data = load_json(HEIGHTMAP_FILE, {})
    return data if isinstance(data, dict) else {}


def load_traces():
    """Traced lines, oldest first. Empty if there are none yet."""
    data = load_json(TRACES_FILE, [])
    return data if isinstance(data, list) else []


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_FILE, {}))
    cfg["game_process"] = [n.lower() for n in cfg["game_process"]]
    if not CONFIG_FILE.exists():
        save_json(CONFIG_FILE, cfg)
    return cfg


# ----------------------------------------------------------------------------
# Sphere math
# ----------------------------------------------------------------------------


def to_latlon(x, y, z):
    """Planet-frame metres -> (lat deg, lon deg, distance from centre in m)."""
    r = math.sqrt(x * x + y * y + z * z)
    lat = math.degrees(math.asin(z / r))
    lon = math.degrees(math.atan2(y, x))
    return lat, lon, r


def surface_distance(lat1, lon1, lat2, lon2, radius):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


# Orbital markers sit at the same six points over every body, so which one is
# nearest a waypoint is geometry. Nothing to read off the HUD and nothing to
# store: it falls out of coordinates already saved, which also means every
# waypoint saved before this existed has one.
#
# OM-1 and OM-2 are the poles; the other four sit on the equator a quarter
# turn apart. Longitude here is atan2(y, x) and latitude asin(z/r), the same
# frame to_latlon reports in.
OM_POINTS = (
    ("OM-1", 90.0, 0.0),        # north pole
    ("OM-2", -90.0, 0.0),       # south pole
    ("OM-3", 0.0, 90.0),
    ("OM-4", 0.0, -90.0),
    ("OM-5", 0.0, 0.0),
    ("OM-6", 0.0, 180.0),
)


def nearest_om(lat, lon, radius=1.0):
    """(name, angle in degrees, distance over the ground) of the closest OM.

    The angle says how far off the marker sits from straight above the point;
    the distance is how far there is left to travel after dropping from it.
    """
    best = None
    for name, om_lat, om_lon in OM_POINTS:
        # surface_distance on a unit sphere is the central angle in radians.
        angle = math.degrees(surface_distance(lat, lon, om_lat, om_lon, 1.0))
        if best is None or angle < best[1]:
            best = (name, angle, surface_distance(lat, lon, om_lat, om_lon,
                                                  radius))
    return best


def bearing(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing, degrees clockwise from planet north,
    with east = increasing longitude."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    yb = math.sin(dl) * math.cos(p2)
    xb = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(yb, xb)) + 360.0) % 360.0


def compass(deg):
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int((deg + 22.5) // 45) % 8]


def fmt_dist(m):
    """Metres in the units the game itself uses: m, km, Mm, Gm.

    Every tier earns its place from a real reading. Gm, because distances
    between bodies run to tens of millions of km -- a jump point measured
    54.47 Gm from Crusader. Mm, because without it everything from a mining
    base to a Lagrange station printed as five- and six-digit kilometres:
    61,910.59 km reads as noise where 61.91 Mm reads as a distance.

    The suffixes are the game's own casing, as the HUD prints them on its
    position rows -- `Pos: 22487898.6236km` -- not SHOUTED.
    """
    sign = "-" if m < 0 else ""
    m = abs(m)
    if m >= 1e9:
        return f"{sign}{m / 1e9:.2f} Gm"
    if m >= 1e6:
        return f"{sign}{m / 1e6:.2f} Mm"
    if m >= 1000:
        return f"{sign}{m / 1000:.2f} km"
    return f"{sign}{m:.0f} m"


# ----------------------------------------------------------------------------
# Body radii (metres), keyed on a canonical form of the planet name.
# Only a FALLBACK: the debug HUD prints "Radius:" itself and that value wins.
# Source: starcitizen.tools per-body "Radius" measurement, retrieved 2026-10-03,
# except where a line says otherwise.
#
# The Star Citizen Wiki API (api.star-citizen.wiki/api/locations, "size",
# game version 4.10.1, checked 2026-10-08) is NOT simply the HUD's radius.
# Against radii the HUD's height identity has confirmed to 1 m it matches
# Bloom, Pyro V, Hurston, microTech and Yela, but misses Crusader by 10 m,
# Clio by 3 m and Euterpe by 167 m (API 212,833; Euterpe's waypoints were saved
# with heights against 213,000). So it is used for Pyro only, where it matched
# both bodies checked and the wiki's rounded figures missed both.
# ----------------------------------------------------------------------------

BODY_RADII_KM = {
    # Stanton
    "hurston": 1000.0, "arial": 345.0, "aberdeen": 274.0, "magda": 341.0,
    "ita": 325.0,
    # Crusader is 7450.010, not the wiki's rounded 7450. Measured: the HUD
    # prints 7,450,010 and the identity holds against it to 0.000000 m at
    # 6.67 Gm (HUR-L3) and again at 54.47 Gm (a jump point), while missing the
    # rounded figure by exactly 10 m. Do not "correct" this back to the wiki.
    "crusader": 7450.010, "cellin": 260.0, "daymar": 295.0, "yela": 313.0,
    "arccorp": 800.0, "lyria": 223.0, "wala": 284.0,
    # Clio measured: the HUD's heights agree with 337,170 (log, 2026-10-06).
    "microtech": 1000.0, "calliope": 240.0, "clio": 337.170, "euterpe": 213.0,
    # Pyro, from the Wiki API (see above). Bloom's 413,900 and Pyro V's
    # 7,812,300 are also what the HUD's heights agree with.
    "pyroi": 411.7, "monox": 529.5, "bloom": 413.9, "pyroiv": 321.4,
    "pyrov": 7812.3, "terminus": 285.0, "pyrovi": 285.0,
    "adir": 431.0, "fairo": 392.24, "fuego": 465.67, "ignis": 393.17,
    "vatra": 365.81, "vuur": 452.83,
    # Nyx I: the HUD prints 1,000,000 and the identity holds against it to the
    # millimetre (3,052.095 m both ways). Without an entry the radius was voted
    # on, and a misread 16,066 let fixes 625 km from the centre through
    # (2026-10-16).
    "nyx1": 1000.0,
}
BODY_RADII = {k: v * 1000.0 for k, v in BODY_RADII_KM.items()}

# Longest keys first so "pyroi" can never shadow "pyroiv".
_BODY_KEYS = sorted(BODY_RADII, key=len, reverse=True)

# OCR confuses these in body names: "nyx1" reads as "nyxl" against a bright
# background. Folding both ways to one form keeps a body under a single key, so
# waypoints saved on one pass are still found on the next. Applied to the table
# keys too, so lookups stay consistent.
_CONFUSE = {"l": "1", "i": "1", "o": "0", "s": "5", "b": "8"}


def canon(s):
    """Lowercase, keep only letters and digits: 'Pyro IV' -> 'pyroiv'."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def fold(s):
    """Canonical form with a trailing letter/digit confusion folded.

    Only the trailing run is folded: body names commonly end in a digit
    ("nyx1") and that is where OCR mistakes a letter for one. Folding the whole
    string would turn ordinary names into gibberish ("somewhere" -> "50mewhere")
    and those names are shown on the overlay.
    """
    c = canon(s)
    i = len(c)
    while i and c[i - 1] in _CONFUSE:
        i -= 1
    return c[:i] + "".join(_CONFUSE[ch] for ch in c[i:])


def match_key(s):
    """Aggressive fold for TABLE LOOKUP only: every confusable glyph folded.

    Safe to mangle here because a hit returns the real table key, so nothing
    folded is ever displayed. This catches mid-name confusions such as
    "Micr0Tech" that the trailing-only fold deliberately leaves alone.
    """
    return "".join(_CONFUSE.get(ch, ch) for ch in canon(s))


# aggressively folded form -> real table key
_FOLDED_KEYS = {match_key(k): k for k in BODY_RADII}
# The HUD numbers Pyro's planets with digits ("Planet:pyro5"), the table with
# Roman numerals. Unmatched, the body fell through to the zone row's system
# name and a Pyro V save was filed under OCR junk ("letanvers"). "pyro1"
# needs no alias: it already folds to the same key as "pyroi".
_FOLDED_KEYS.update({match_key(f"pyro{n}"): key for n, key in
                     ((2, "monox"), (3, "bloom"), (4, "pyroiv"),
                      (5, "pyrov"), (6, "pyrovi"))})


# Which system each body belongs to.
#
# The HUD does name the system, in the planet zone row -- "Zone:
# OOC_Stanton_4c_Euterpe" -- and "Stanton" survived OCR in every frame of a
# sample where the Planet row's copy of it came back "Stanten" each time. But
# it is not there for every body: Nyx prints only "Zone: nyx1". So the table
# is primary and the zone row is the fallback, for bodies added in later
# patches rather than for the ones already listed here.
SYSTEM_BODIES = {
    "stanton": ("hurston aberdeen arial ita magda "
                "crusader cellin daymar yela "
                "arccorp lyria wala "
                "microtech calliope clio euterpe").split(),
    "pyro": ("pyroi monox pyroiv pyrov pyrovi "
             "adir fairo fuego ignis terminus vatra vuur bloom").split(),
    "nyx": ["nyx1"],
}
BODY_SYSTEM = {body: name
               for name, bodies in SYSTEM_BODIES.items() for body in bodies}

# The "OOC" prefix is Object Container; the system name follows it. Its letters
# are the round ones OCR swaps freely (OOC, GGC, OGC, GG6C all seen), so the
# prefix is matched loosely and the system name strictly.
_OC_SYSTEM_RE = re.compile(r"(?:^|[\s:])[OG0C6]{2,4}[_ ]+([A-Za-z]{3,14})[_ ]")


# Anchored on "player", which read cleanly in every sampled frame while the
# word after it did not: "logatiogy", "logation", "locazion" were all seen, so
# that word is skipped whole rather than matched.
_LOCATION_RE = re.compile(r"p[li1]ayer\s+\S+\s*[:;]?\s*(.+)", re.I)

# Rows that only the debug HUD prints. Two must be present, so one stray line
# of quoted HUD text elsewhere on screen cannot pass for the HUD being up.
_HUD_MARKERS = ("camdir", "solarsystem", "servertime", "shardid", "fstop")


def parse_location(text):
    """The game's own name for where you are, or None.

    "Current player location : FOB Abandoned Stanton4c FOB2" -- a named place,
    which the system and body cannot give you.

    The row often says the name twice, the second time in brackets:
    "Stanton4 NewBabbage (LZ : Stanton4 NewBabbage)". Only the first is kept.
    The bracketed copy adds nothing and doubles the text OCR can mangle, and
    mangle it does -- twelve frames of one unmoving location produced six
    different strings, differing in a character or two each time.
    """
    for line in text.splitlines():
        m = _LOCATION_RE.search(line)
        if not m:
            continue
        # The HUD block is right-aligned and whatever is behind it reads as
        # junk off the end of the row; a run of spaces is where that starts.
        name = re.split(r"\s{3,}", m.group(1).strip())[0]
        name = name.split("(")[0]
        # A stray leading character was seen on its own ("; Stanton4 ...").
        name = re.sub(r"\s+", " ", name).strip(" ;:.,-")
        if 2 <= len(name) <= 48 and any(c.isalnum() for c in name):
            return name
    return None


def hud_visible(text):
    """Is the debug HUD on screen, even with no planet-local position?

    Without this, "you are between bodies" and "you forgot r_DisplayInfo 2"
    are indistinguishable: both simply produce no fix, and only one of them is
    something to act on.
    """
    low = canon(text)
    return sum(marker in low for marker in _HUD_MARKERS) >= 2


def parse_system(text):
    """System name from the HUD's zone rows, or None.

    The SolarSystem row is skipped deliberately: it carries an instance id
    ("SolarSystem_871500387367"), not a name, and the id differs between
    sessions so it cannot identify a system.
    """
    for line in text.splitlines():
        if "solarsystem" in canon(line):
            continue
        m = _OC_SYSTEM_RE.search(line)
        if m:
            name = canon(m.group(1))
            if name in SYSTEM_BODIES or len(name) >= 4:
                return name
    return None


def resolve_system(body, text=None):
    """Which system a body is in. The table first, the HUD's zone row second."""
    if body in BODY_SYSTEM:
        return BODY_SYSTEM[body]
    found = parse_system(text) if text else None
    return found or "unknown"


def resolve_body(name):
    """Planet name -> canonical body key, or None if it is not in the table.

    Match whole token runs first, longest run first, so "Pyro I" cannot shadow
    "Pyro IV" and a substring scan cannot match "ita" inside "orbital".
    """
    if not name:
        return None
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", name) if t]
    for width in range(len(tokens), 0, -1):
        for i in range(len(tokens) - width + 1):
            key = _FOLDED_KEYS.get(match_key("".join(tokens[i:i + width])))
            if key:
                return key
    joined = match_key(name)
    for key in _BODY_KEYS:
        if match_key(key) in joined:
            return key
    return fuzzy_body(name)


# A name OCR has damaged too far for the table goes to the body whose name it
# holds closely enough, and clearly more closely than any other in its system.
# Of the 770 names in session.log that matched no body (2,631 readings),
# almost all were Pyro V or Hurston: "pyra5", "pyro55", "gocstanton1hurstan".
FUZZY_BODY_MIN = 0.8
FUZZY_BODY_MARGIN = 0.05        # Calliope holds Clio's letters: 0.93 to 0.86
FUZZY_PYRO_MOON_MIN = 0.85

# Pyro's planets are printed by number ("Planet:pyro5"), so their names differ
# in one digit and only the digit tells them apart.
PYRO_BY_NUMBER = dict(zip("123456", ("pyroi", "monox", "bloom", "pyroiv",
                                     "pyrov", "pyrovi")))

# "(Load Complete)" follows the name on the HUD and OCRs into it, where its
# letters matched the moon Adir ("pyra5soadriomptete"). Cut off from here.
_NAME_TAIL_RE = re.compile(r"[l1]?[o0]ad|[o0]mp|p[l1]ete|w[o0]rk")


def _name_system(c):
    """The system a canonical body name is written in, or None: Pyro's start
    "pyro", Stanton's carry "OOC_Stanton_" however damaged."""
    if c.startswith("py") or "pyr" in c[:4]:
        return "pyro"
    k = match_key(c)
    if any(SequenceMatcher(None, "5tant0n", k[i:i + 7]).ratio() >= 0.7
           for i in range(max(1, len(k) - 6))):
        return "stanton"
    return None


@functools.lru_cache(maxsize=1024)
def fuzzy_body(name):
    """The body a damaged name stands for, or None if it is not clearly one.

    Every reading is judged on its own; it takes PLANET_SWITCH_FRAMES of them
    in a row to change body, so an odd wrong match changes nothing.
    """
    c = canon(name)
    tail = _NAME_TAIL_RE.search(c, 2)
    if tail:
        c = c[:tail.start()]
    system = _name_system(c)
    if system is None:
        return None
    k = match_key(c)
    scores = {}
    for form, key in _FOLDED_KEYS.items():
        if len(form) < 4 or BODY_SYSTEM.get(key) != system:
            continue
        for w in (len(form) - 1, len(form), len(form) + 1):
            for i in range(max(1, len(k) - w + 1)):
                r = SequenceMatcher(None, form, k[i:i + w]).ratio()
                scores[key] = max(scores.get(key, 0.0), r)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if system == "pyro":
        moon = next((kv for kv in ranked
                     if kv[0] not in PYRO_BY_NUMBER.values()), None)
        if moon and moon[1] >= FUZZY_PYRO_MOON_MIN:
            return moon[0]
        digit = next((ch for ch in c[2:] if ch in PYRO_BY_NUMBER), None)
        return PYRO_BY_NUMBER.get(digit)
    if not ranked or ranked[0][1] < FUZZY_BODY_MIN:
        return None
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < FUZZY_BODY_MARGIN:
        return None
    return ranked[0][0]


# ----------------------------------------------------------------------------
# Screen capture + OCR + parsing
#
# The r_DisplayInfo HUD prints several position rows in DIFFERENT reference
# frames, for example:
#
#   Zone: ARGO_MOLE_871458443445 Pos: -1.00m 20.13m 0.81m      <- inside the ship
#   Zone: nyx1 Pos: 712.1792km -705.5550km 33.2624km           <- planet, in km
#   Zone: Root Pos: 6494240.6875km ...                         <- system frame
#   CamPos Planet Zone: 712179.247562 -705554.983813 33262.443965
#   Radius: 1000000.000000 ElevationUnderCam: 2073.562500 Altitude 978.532837
#   Planet:nyx1  (Load Complete)
#
# Only the CamPos row is used: it is planet-local and in metres at full
# precision. Reading any other row would silently report a position in the
# wrong frame (the ship row above would place you 20 m from the planet core).
# Radius and the planet name come from the HUD's own rows.
# ----------------------------------------------------------------------------

# Glyph confusions Tesseract makes on the debug HUD font. Applied only to
# tokens that are already mostly digits, so names are never mangled.
_DIGIT_FIXES = str.maketrans(
    {"O": "0", "o": "0", "D": "0", "@": "0", "l": "1", "I": "1", "|": "1",
     "S": "5", "B": "8", "−": "-", "—": "-", "–": "-"}
)

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
# "CamPos", tolerant of the usual OCR slips and of a "CampPos" spelling.
_CAMPOS_RE = re.compile(r"c[a@]m\s*p?\s*p[o0]s", re.I)
_ZONE_KW_RE = re.compile(r"z[o0]ne", re.I)
_PLANET_KW_RE = re.compile(r"pl[a@]net", re.I)
# "Planet:nyx1" or "Planet:OOC Stanton 4 Microtech  (Load Complete)". Names can
# contain spaces, so capture to the trailing parenthetical or end of line.
# The separator is what distinguishes this from the CamPos row's "Planet Zone:",
# and OCR regularly renders the colon as i, l, 1, ; or . against bright
# backgrounds ("Planetinyxl"), so accept any of those.
_PLANET_NAME_RE = re.compile(r"pl[a@]net\s*[:;il1.]\s*([^(]+)", re.I)
# Label only, for corroborating that a row is the Radius row when its digits
# have not survived. See find_region.has_radius_near.
_RADIUS_LABEL_RE = re.compile(r"r[a@]d[il1]us", re.I)
_RADIUS_RE = re.compile(r"r[a@]d[il1]us\s*:?\s*(\d+(?:\.\d+)?)", re.I)

# The Radius row carries both heights too:
#   Radius: 1000000.000000 ElevationUnderCam: 2073.562500 Altitude 978.532837
# ElevationUnderCam is the terrain's height over the reference sphere;
# Altitude is height over that terrain, which is the useful one in flight.
# Altitude carries no colon in the HUD where Elevation does, so neither
# separator is required. The labels are long words and OCR them reliably --
# it is the digits that get mangled, which the identity check in State.update
# is there to catch.
# Beyond any coordinate the game produces. Stanton's bodies sit tens of Gm
# from its star, so this leaves well over an order of magnitude of headroom
# while still catching a reading that is simply garbage.
SYSTEM_EXTENT_M = 1.0e12

# The identity holds exactly in game, so this only absorbs float rounding.
HEIGHT_TOLERANCE = 1.0

# How long a believed height stays on screen without being confirmed again.
# Long enough to ride out the frames that fail the check, short enough that it
# cannot drift far: at the fastest flight seen it is a few km on a reading of
# hundreds, and the alternative is a line that changes shape twice a second.
HEIGHT_HOLD_SECONDS = 3.0

# Frames a new body name must survive before it replaces the current one.
PLANET_SWITCH_FRAMES = 3

# The same, for the place name. Lower: moving between places is far more
# frequent than moving between bodies, so the line should not lag badly.
LOCATION_SWITCH_FRAMES = 2

# Each label is anchored on the part of it that survives OCR, and the number is
# then reached over non-digits only.
#
# Pinning the whole label does not work, because the outer letters are the ones
# that go. Real captures from one session: "Glevationundercan:.",
# "ElevationUnderGam:", "ElevaticnUnderCam:", "ElevaticnUndegCam:" -- E read as
# G, C as G, m as n, and in seven frames of ten the o of "Elevation" as a c.
# Pinned to the full label, elevation went unread on every frame of the sample.
#
# So the anchor is only "levat", which survived every one of them, and the
# number is reached from there. "Altitude" came through entire every time and
# keeps a longer anchor.
#
# Skipping over [^\d] rather than a class of plausible separators also makes
# the old digit-eating bug impossible. A colon OCRs as "1", so a separator
# class containing "1" swallowed the value's first digit and read
# "Altitude 13.710187" as 3.710187; a non-digit class cannot consume a digit.
# The bounds stop a match running past a missing value into the next number.
# Elevation goes negative where the ground is below the reference sphere (a
# frozen sea), so a minus directly before the digits is kept; the lazy skip
# lets the minus be tried before it is passed over as a separator.
_ELEV_RE = re.compile(r"[li1]ev[a@]t[^\d]{0,16}?(-?\d[\d.]*)", re.I)
_ALT_RE = re.compile(r"[li1]t[li1]tude[^\d]{0,8}(\d[\d.]*)", re.I)


# Tesseract ships as a separate binary and its Windows installer does not put
# it on PATH, so look in the usual install locations before giving up.
_TESSERACT_GUESSES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/homebrew/bin/tesseract",
]


def find_tesseract(configured=None):
    """Return a usable tesseract path, or None.

    Order: the configured path, then PATH, then the usual install locations.
    """
    if configured:
        return configured
    bundled = BUNDLE / "tesseract" / "tesseract.exe"
    if FROZEN and bundled.is_file():
        os.environ["TESSDATA_PREFIX"] = str(bundled.parent / "tessdata")
        return str(bundled)
    found = shutil.which("tesseract")
    if found:
        return found
    return next((p for p in _TESSERACT_GUESSES if os.path.isfile(p)), None)


def setup_tesseract(cfg):
    """Point pytesseract at the binary; exit with advice if there isn't one."""
    import pytesseract

    path = find_tesseract(cfg["tesseract_cmd"])
    if path is None:
        sys.exit(
            "tesseract not found. Install it, then either add it to PATH or set\n"
            '"tesseract_cmd" in locator_config.json, e.g.\n'
            r'  "tesseract_cmd": "C:\Program Files\Tesseract-OCR\tesseract.exe"'
        )
    pytesseract.pytesseract.tesseract_cmd = path
    return path


def open_capture():
    """mss renamed mss.mss to mss.MSS; accept either."""
    import mss

    return (getattr(mss, "MSS", None) or mss.mss)()

def grab_region(sct, cfg):
    from PIL import Image

    mons = sct.monitors
    mon = mons[min(cfg["monitor"], len(mons) - 1)]
    l, t, r, b = cfg["region"]
    box = {
        "left": mon["left"] + int(mon["width"] * l),
        "top": mon["top"] + int(mon["height"] * t),
        "width": max(1, int(mon["width"] * (r - l))),
        "height": max(1, int(mon["height"] * (b - t))),
    }
    shot = sct.grab(box)
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def preprocess(img, cfg, cut=None):
    """Isolate the HUD text, as black on white for Tesseract.

    Each pixel is compared with the average of its own neighbourhood rather
    than with one brightness cut-off for the whole frame. A global cut-off
    cannot work here: the HUD is light text, and so is a sunlit planet surface
    behind it, so no single value separates them. On a ten-frame sample taken
    over Euterpe the global cut-off recovered 1 fix, and inverted the bright
    ground into a solid black block that swallowed whole rows.

    Local contrast sidesteps that. Text strokes are brighter than the few
    pixels around them wherever they sit, so background brightness stops
    mattering. `contrast_radius` must be comfortably wider than a stroke, and
    `cut` is how much brighter than its surroundings a pixel must be to count
    as ink.
    """
    from PIL import Image, ImageChops, ImageFilter

    g = img.convert("L")
    s = cfg["scale"]
    g = g.resize((int(g.width * s), int(g.height * s)), Image.LANCZOS)
    local_mean = g.filter(ImageFilter.BoxBlur(cfg["contrast_radius"]))
    # subtract clamps at zero, so anything darker than its surroundings is
    # flattened to nothing and only light-on-dark strokes survive.
    lift = ImageChops.subtract(g, local_mean)
    c = cfg["contrast_cut"] if cut is None else cut
    return lift.point(lambda v: 0 if v > c else 255)


def _repair_number(tok):
    """Repair OCR confusions in a token that is already mostly digits."""
    digits = sum(c.isdigit() for c in tok)
    if digits and digits >= len(tok) - 3:
        tok = tok.translate(_DIGIT_FIXES)
        # thousands separators; this HUD never uses a decimal comma
        tok = re.sub(r"(?<=\d),(?=\d)", "", tok)
    return tok


def restore_decimal(tok, min_digits=9):
    """Reinsert a decimal point Tesseract dropped, if the token must have had one.

    Coordinates and heights always carry six decimals, so a run of digits with
    no point means it vanished: "-261592703280" is "-261592.703280". Nine
    digits already exceeds the largest body radius, so no coordinate reaches
    that length as a whole number and the repair is unambiguous.

    Heights pass a shorter `min_digits`: they share the six decimals but are
    far smaller, so an altitude under 10 m leaves only seven digits
    ("13710187" is 13.710187) and would not trigger the coordinate rule.
    """
    if "." not in tok and sum(c.isdigit() for c in tok) >= min_digits:
        return tok[:-6] + "." + tok[-6:]
    return tok


def extract_numbers(tail):
    """Pull numeric tokens out of a run of HUD text.

    Tesseract sometimes puts a space either side of a decimal point
    ("744846 .976767"), which would otherwise read as two separate values and
    silently truncate a coordinate. Rejoin those before splitting.
    """
    tail = re.sub(r"(?<=\d)\s+(?=[.,]\d)", "", tail)
    tail = re.sub(r"(?<=\d[.,])\s+(?=\d)", "", tail)
    out = []
    for tok in tail.split():
        fixed = restore_decimal(_repair_number(tok))
        m = _NUM_RE.search(fixed)
        # Require a substantial number filling most of the token. A garbled
        # token such as "SO" would otherwise repair to "50" and be accepted as
        # a coordinate, shifting every value after it.
        if m and len(m.group(0)) >= 3 and len(m.group(0)) >= 0.6 * len(fixed):
            out.append(float(m.group(0)))
    return out


def campos_position(line):
    """Planet-local metres from the 'CamPos Planet Zone:' row, else None.

    Identified by carrying both the "Planet" and "Zone" keywords (or a legible
    "CamPos"); no other HUD row does. Numbers are taken after the last keyword
    so the row's own words cannot be read as coordinates.
    """
    ends = [m.end() for m in _CAMPOS_RE.finditer(line)]
    zone = [m.end() for m in _ZONE_KW_RE.finditer(line)]
    planet = [m.end() for m in _PLANET_KW_RE.finditer(line)]
    if not ends and not (zone and planet):
        return None
    nums = extract_numbers(line[max(ends + zone + planet):])
    return tuple(nums[:3]) if len(nums) >= 3 else None


# On foot the HUD can drop the CamPos row altogether (seen 2026-10-11 on
# microTech, walking), while the body's own zone row stays:
#   Zone: OOC_Stanton_4_Microtech Pos: -681.1418km 726.1471km 98.0756km
# Same planet-local frame as CamPos, but in km to four places: 0.1 m rather
# than a micrometre, which is plenty for a waypoint. Its |pos| - radius agreed
# with ElevationUnderCam + Altitude to 0.07 m on that capture.
_ZONE_ROW_RE = re.compile(r"z[o0]ne\s*[:;]?\s*(.+?)\s+p[o0a]s\b\s*[:;]?\s*(.+)",
                          re.I)


def planet_zone_position(zones, planet):
    """Planet-local metres from the body's own Zone row, else None.

    `zones` is (name, values) for every Zone row. The ship, container and
    system rows are told apart by name: only the body's row is named after the
    body. With the Planet row unread, a name the body table knows still counts.
    """
    want = resolve_body(planet) or fold(planet) if planet else None
    for name, tail in zones:
        key = resolve_body(name) or fold(name)
        if key == want or (want is None and resolve_body(name)):
            values = _sys_values(tail)
            if values:
                return values
    return None


# Automatic page segmentation for the find pass. Sparse mode (psm 11) finds the
# words but does not group them into lines, and the find pass needs whole lines
# so it can test each one for a parseable CamPos row.
TESS_FIND_CONFIG = "--psm 3"


def find_region(sct, cfg):
    """OCR the whole screen and return region fractions around the HUD rows.

    Requires a CamPos row with a plausible Radius row within a few lines of it.
    A single CamPos line is not enough: quoted HUD text in a chat window, editor
    or terminal would match, and that text is usually not accompanied by the
    Radius row the game always prints directly underneath.

    Returns [left, top, right, bottom] as monitor fractions, or None.
    """
    import pytesseract

    full = dict(cfg)
    full["region"] = [0.0, 0.0, 1.0, 1.0]
    raw = grab_region(sct, full)
    data = pytesseract.image_to_data(preprocess(raw, full),
                                     config=TESS_FIND_CONFIG,
                                     output_type=pytesseract.Output.DICT)

    rows = {}
    for i, txt in enumerate(data["text"]):
        if txt.strip():
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            rows.setdefault(key, []).append(i)

    def box(idx):
        return (min(data["left"][i] for i in idx),
                min(data["top"][i] for i in idx),
                max(data["left"][i] + data["width"][i] for i in idx),
                max(data["top"][i] + data["height"][i] for i in idx))

    lines = []
    for idx in rows.values():
        order = sorted(idx)
        words = [data["text"][i] for i in order]
        lines.append((order, words, " ".join(words), box(order)))

    heights = sorted(data["height"][i] for idx in rows.values() for i in idx)
    row_h = heights[len(heights) // 2] if heights else 12.0

    def has_radius_near(mid):
        """Is the Radius row within a few lines of this one?

        Corroboration only, so the row is identified by its labels rather than
        by a parseable number. The digits are the first thing to go: a real
        capture of this row read "Radius: 1x@0000.@00000", which no number
        pattern accepts, while every label on it stayed legible. Demanding a
        number here meant find_region located nothing at all.

        The case this guards against is quoted HUD text in a chat window or
        editor, which carries a CamPos line and no Radius row whatsoever, so
        the label alone still rules it out.
        """
        for _, _, text, b in lines:
            if abs((b[1] + b[3]) / 2.0 - mid) > 4.0 * row_h:
                continue
            if not _RADIUS_LABEL_RE.search(text):
                continue
            m = _RADIUS_RE.search(text)
            if (m and float(m.group(1)) >= 1000.0) or _ALT_RE.search(text) \
                    or _ELEV_RE.search(text):
                return True
        return False

    anchor = None
    for order, words, text, b in lines:
        if campos_position(text) is None:
            continue
        if not has_radius_near((b[1] + b[3]) / 2.0):
            continue
        # Tesseract groups text across the whole screen width into one line, so
        # a row can begin with unrelated game content far to the left. Keep only
        # the words from "CamPos" (or its "Planet" keyword) rightward.
        start = next((k for k, w in enumerate(words)
                      if _CAMPOS_RE.search(w) or _PLANET_KW_RE.search(w)), 0)
        anchor = box(order[start:])
        break
    if anchor is None:
        return None

    mid = (anchor[1] + anchor[3]) / 2.0
    # The HUD block is right-aligned; allow neighbouring rows to start a little
    # further left than the CamPos row, but not across the whole screen.
    left_limit = anchor[0] - 0.15 * raw.width * cfg["scale"]

    # Keep the neighbouring rows too: Planet sits just above the CamPos row and
    # Radius/Altitude just below, and all are worth having inside the crop.
    boxes = [anchor]
    for order, _, _, _ in lines:
        kept = [i for i in order if data["left"][i] >= left_limit]
        if not kept:
            continue
        b = box(kept)
        if abs((b[1] + b[3]) / 2.0 - mid) <= 3.0 * row_h:
            boxes.append(b)

    s = cfg["scale"]  # boxes are in upscaled pixels
    l = min(b[0] for b in boxes) / s
    t = min(b[1] for b in boxes) / s
    r = max(b[2] for b in boxes) / s
    b = max(b[3] for b in boxes) / s
    # Pad vertically by ~3 rows: the block shifts as the number of Zone rows
    # changes with how deeply the player is nested in object containers.
    pad_x = 0.02 * raw.width
    pad_y = 3.0 * row_h / s
    right = (r + pad_x) / raw.width
    top = (t - pad_y) / raw.height
    # The HUD block is pinned to the top-right corner of the screen, so snap
    # out to both edges rather than trusting the measured sides. On the right,
    # OCR loses the last glyph or two and truncates the final coordinate. At
    # the top, the number of rows above CamPos changes with location and
    # graphics settings, which slides the whole block up and down - a region
    # measured in one place otherwise stops working in another.
    if right > 0.90:
        right = 1.0
    if top < 0.33:
        top = 0.0
    return [
        max(0.0, (l - pad_x) / raw.width),
        max(0.0, top),
        min(1.0, right),
        min(1.0, (b + pad_y) / raw.height),
    ]


def trailing_zeros(v):
    digits = f"{int(round(v))}"
    return len(digits) - len(digits.rstrip("0"))


def best_radius(votes, r):
    """Pick the most credible radius seen for a body, or None.

    Body radii in game are round numbers (1,000,000 / 295,000 / 7,450,000) and
    OCR noise destroys that roundness, turning zeros into 6s and 8s. So among
    the readings that agree with the current position, prefer the roundest,
    then the most frequent, then the smallest -- misreads inflate digits more
    often than they shrink them.
    """
    cands = [v for v in votes if plausible(r, v, True)]
    if not cands:
        return None
    return max(cands, key=lambda v: (trailing_zeros(v), votes[v], -v))


def above_space_line(r, radius, cfg):
    """Is this high enough above the body to call it space rather than the body?

    Measured over the reference sphere, not over terrain: the sphere height
    can be worked out on every frame, so the answer cannot flicker with
    whether the HUD's height values happened to parse.
    """
    return (r - radius) > cfg["space_line_m"]


def far_from_body(r, radius, cfg):
    """Too far for "near <body>" to mean anything?

    A zone reaches Gm past its surface, so the space line alone leaves one
    wording covering everything from a station in low orbit to a Lagrange
    station four orders of magnitude further out. HUR-L3 reads 6.67 Gm from
    Crusader, whose zone it is in; "In Space near crusader" is a false claim
    at that distance, and the body is still the only bearing available.
    """
    return (r - radius) > cfg["far_line_m"]


def plausible(r, radius, radius_known):
    """Is a distance from this body's centre a possible one?

    Only the floor says anything about the body: you cannot be inside it.

    There is deliberately no proximity ceiling. A body's zone reaches vastly
    further than its surface -- a real reading 55.85 Gm from Crusader, with
    the HUD's own height values agreeing with it exactly, was being thrown
    away by a ceiling of five radii, and the looser branch for an unread
    radius topped out at 50 Gm and threw it away too. Both were guesses about
    how far a zone extends, and both were wrong by orders of magnitude.

    What remains only rejects magnitudes no coordinate in the game reaches.
    Readings far from a body are corroborated instead by the height identity
    in State.update, which is evidence rather than an assumption.
    """
    if not 0.0 < r <= SYSTEM_EXTENT_M:
        return False
    return r >= 0.9 * radius if radius_known else r >= 100_000.0


def parse_heights(text):
    """Return (elevation_under_cam, altitude) in metres, each None if unread.

    Both come off the Radius row. Neither is trusted here: State.update checks
    them against |CamPos| - radius before showing either, because that sum is
    the only handle on whether the digits OCRed correctly.
    """
    elev = alt = None
    for line in text.splitlines():
        if elev is None:
            m = _ELEV_RE.search(line)
            if m:
                elev = _as_height(m.group(1))
        if alt is None:
            m = _ALT_RE.search(line)
            if m:
                alt = _as_height(m.group(1))
    return elev, alt


def _as_height(tok):
    """A height token as a float, or None if it is not a usable number."""
    tok = restore_decimal(tok.rstrip("."), min_digits=7)
    try:
        return float(tok)
    except ValueError:
        return None


def parse_readout(text, default_radius):
    """Return (planet, radius, pos, radius_known) or None if no fix was found.

    `pos` is planet-local metres from the CamPos row. `radius` comes from the
    HUD's Radius row, falling back to BODY_RADII by planet name and then to
    `default_radius`; `radius_known` is False only in that last case.
    """
    pos = planet = radius = None
    zones = []
    for line in text.splitlines():
        if pos is None:
            pos = campos_position(line)
        m = _ZONE_ROW_RE.search(line)
        if m:
            zones.append(m.groups())
        if planet is None:
            m = _PLANET_NAME_RE.search(line)
            if m and m.group(1).strip():
                planet = m.group(1).strip()
        if radius is None:
            m = _RADIUS_RE.search(line)
            if m and float(m.group(1)) >= 1000.0:
                radius = float(m.group(1))
    if pos is None:
        pos = planet_zone_position(zones, planet)
    if pos is None:
        return None
    # Canonical key where possible: the HUD writes the same body as "nyx1" or
    # "OOC Stanton 4 Microtech", and waypoints must group under one name.
    key = resolve_body(planet)
    known = radius is not None
    if radius is None and key:
        radius, known = BODY_RADII[key], True
    if radius is None:
        radius = float(default_radius)
    return (key or fold(planet or "") or "unknown", radius, pos, known)

# ----------------------------------------------------------------------------
# The system frame
#
# Everything else here is planet-local, which exists only inside a body's zone.
# A ship parked between bodies has no such position, so it cannot be marked at
# all without this. The HUD prints a system-frame position on two rows:
#
#   Zone: SolarSystem_871500387367 Pos: 22487898.6236km 37081092.2270km -4126.10m
#   Zone: Root Pos: 22487898.6236km 37081092.2270km -4126.10m
#
# Note the units differ within one row: two values in km, one in m.
#
# The two rows carry the same numbers, which looks like a free cross-check and
# is not one. They are the same glyphs in the same font, so they garble the
# same way: across ten captured frames the rows agreed twice, and both of those
# agreements were on a wrong value ("37681092" for a true "37081092", and a
# truncated "4177km"). Agreement is therefore used only to reject a frame where
# they differ -- it is never taken as evidence that a reading is right.
#
# What does work is time. The true value moved smoothly, 37081092 -> ...091 ->
# ...090 -> ...089, while a digit error jumps by 600,000 km. So a reading is
# believed only once it is consistent with the one before it.
# ----------------------------------------------------------------------------

# Rounding between the two rows; anything larger means one of them is garbled.
SYSTEM_ROW_TOLERANCE_M = 1000.0

# Biggest step between frames still treated as flight rather than a misread.
# Measured flight ran about 2.4 km per frame at the default interval, so this
# allows some forty times that and still rejects what was seen going wrong: a
# frame whose third value read +882 km among neighbours at -77 km slipped
# through a 2,000 km limit and put the position 960 km out.
#
# Quantum travel outruns this and drops the lock, which costs nothing: the
# game stops updating the Root row for the whole jump anyway (Euterpe to
# Hurston, 2026-10-14: the same value for 226 s). The star map takes your
# place during a jump from the planet-frame fix instead.
SYSTEM_LOCK_KM = 100.0

# How still you must be for the position to be worth marking. Marking a parked
# ship is the point, so this only has to be tight enough to exclude a misread.
SYSTEM_STABLE_KM = 10.0

# Frames in a row whose two positions must put a body's star within
# SPIN_AGREE_DEG of each other before its spin anchor is re-checked, and how
# far out the anchor must be to be corrected and saved. Measured parked, the
# angle wandered by 0.02 deg.
SPIN_VOTES = 3
SPIN_AGREE_DEG = 0.1
SPIN_CORRECT_DEG = 0.1

# Agreeing frames needed before a position is believed from nothing. Three
# rather than two, because one digit can be misread the same way twice running
# and a pair of matching errors is indistinguishable from a pair of truths.
SYSTEM_RELOCK_FRAMES = 3

# How far two CamDir readings in a row may differ and still both be believed.
# A dropped minus sign mirrors the direction, which is far more than this.
CAMDIR_AGREE_DEG = 25.0

# How often the believed system position is logged, with how far it has moved
# since the last line. Purely a measurement: the planet frame rotates with its
# body -- 0.021 deg/s on Crusader, which sweeps a motionless ship 18,583 km/s
# at 54 Gm -- and whether the root frame does the same decides which frame a
# waypoint out in space belongs in. Nothing here has ever measured it. Parked
# and still, these lines say: metres over a minute is a static frame,
# thousands of km is not. Twelve lines a minute, which the log can carry.
SYSTEM_LOG_SECONDS = 5.0

# Stands where a body name would for a waypoint out between them, so that
# grouping, naming and the panel need no special case for it.
DEEP_SPACE = "deep-space"

# "Pos" reads as "Pas" often enough to matter, so the vowel is loose.
_SYS_ROW_RE = re.compile(r"\bp[o0a]s\b\s*[:;]?\s*(.+)", re.I)

# The values run together on this row -- "37168944.6349km-~73.0260km" -- and
# whatever sits between them takes the minus with it: "km-~", "km-+" and "km--"
# were all captured for the same negative value. Reading the sign from a single
# character lost it and turned -73 into +73, which then made the two rows look
# like they disagreed and threw the frame away. So the sign comes from a run of
# characters before the digits, and a minus anywhere in that run means negative.
_SYS_VALUE_RE = re.compile(r"([-+~\s]*)(\d[\d.,\s]*?)\s*(km|m)\b", re.I)


def _sys_values(tail):
    """The three numbers off a system-frame row, in metres, or None.

    Each value carries its own unit and they are not the same unit, so the
    suffix has to be read per value rather than once for the row.
    """
    out = []
    for sign, raw, unit in _SYS_VALUE_RE.findall(tail):
        # Tesseract puts spaces and commas inside long numbers:
        # "22487898. 6236km" and "37681092,2270km" both occur.
        cleaned = raw.replace(" ", "").replace(",", ".").rstrip(".")
        if cleaned.count(".") > 1:          # "37,081,092.2270" -> keep the last
            head, _, frac = cleaned.rpartition(".")
            cleaned = head.replace(".", "") + "." + frac
        try:
            value = float(cleaned)
        except ValueError:
            return None
        if "-" in sign:
            value = -abs(value)
        out.append(value * 1000.0 if unit.lower() == "km" else value)
        if len(out) == 3:
            return tuple(out)
    return None


_CAMDIR_RE = re.compile(r"c[a@]m\s*d[il1]r", re.I)
_CAMDIR_NUM_RE = re.compile(r"[-+]?\d*[.,]\d+|[-+]?\d+")


def angles_to_forward(pitch, roll, yaw):
    """Forward unit vector for the engine's XYZ Euler angles, in degrees.

    The HUD's CamDir row is `pitch roll yaw` the way CryEngine prints a
    camera's angles (Ang3::GetAnglesXYZ): rotation about x, then y, then z,
    applied to the camera's forward axis +y, with z up. So forward is
    Rz(yaw) Ry(roll) Rx(pitch) (0, 1, 0). Level and unrolled, yaw alone:
    (-sin yaw, cos yaw, 0); pitch alone lifts it to (0, cos pitch, sin pitch).
    """
    p, r, y = (math.radians(a) for a in (pitch, roll, yaw))
    sp, cp, sr, cr, sy, cy = (math.sin(p), math.cos(p), math.sin(r),
                              math.cos(r), math.sin(y), math.cos(y))
    return (sr * sp * cy - cp * sy,
            sr * sp * sy + cp * cy,
            cr * sp)


def parse_cam_dir(text):
    """Where the camera points, as a unit vector, or None.

    Read from the CamDir row: `CamDir: 2 1 -30 FOV: 60 ...` (Yela,
    2026-10-18), three whole-degree angles, pitch roll yaw, turned into a
    forward vector by angles_to_forward. Whole degrees is all the HUD
    prints, so the nose is known to half a degree, which is plenty.

    Should a build ever print a direction vector instead, three numbers of
    unit length are taken as that. Anything else -- a misread digit that
    makes an angle of 1234 degrees, a value with the minus sign lost is
    sadly not detectable here -- is dropped.
    """
    for line in text.splitlines():
        m = _CAMDIR_RE.search(line)
        if not m:
            continue
        tail = line[m.end():]
        cut = re.search(r"f[o0]v", tail, re.I)
        if cut:
            tail = tail[:cut.start()]
        tail = re.sub(r"(?<=\d)\s+(?=[.,]\d)", "", tail)
        tail = re.sub(r"(?<=\d[.,])\s+(?=\d)", "", tail)
        toks = _CAMDIR_NUM_RE.findall(tail)
        if len(toks) < 3:
            continue
        try:
            v = tuple(float(t.replace(",", ".")) for t in toks[:3])
        except ValueError:
            continue
        n = math.sqrt(sum(c * c for c in v))
        if 0.97 <= n <= 1.03:
            return tuple(c / n for c in v)
        if all(-360.0 <= c <= 360.0 for c in v):
            return angles_to_forward(*v)
    return None


def parse_system_pos(text):
    """System-frame position in metres, or None.

    Read from the Root row, with the SolarSystem row used only to contradict
    it. Where both parse and disagree, the frame is thrown away: one of them
    is garbled and there is no way to tell which.
    """
    root = solar = None
    for line in text.splitlines():
        low = canon(line)
        if "root" not in low and "solarsystem" not in low:
            continue
        m = _SYS_ROW_RE.search(line)
        if not m:
            continue
        values = _sys_values(m.group(1))
        if values is None:
            continue
        if "solarsystem" in low:
            solar = values
        else:
            root = values
    if root and solar and math.dist(root, solar) > SYSTEM_ROW_TOLERANCE_M:
        return None
    return root or solar


# A parked ship on a spinning body moves the system position by metres a second,
# so the same reading for this long is not a ship that has stopped. A 5-minute
# freeze was seen with the HUD visibly changing; this says which part stood still.
FREEZE_LOG_SECONDS = 20.0


class FreezeWatch:
    """Says why the system position stopped changing, once per freeze.

    Fed every frame with a hash of the captured image and the position read from
    it. If the position repeats while the image keeps changing, the game is
    drawing and the row or the reader is stuck; if the image is identical too,
    the capture is returning a stale frame or the HUD stopped redrawing.
    """

    def __init__(self, after=FREEZE_LOG_SECONDS):
        self.after = after
        self.last_hash = None
        self.pos = None
        self.since = None
        self.reads = 0           # frames that parsed to the same position
        self.dropped = 0         # frames whose position did not parse at all
        self.changed = 0
        self.said = False

    def feed(self, now, frame_hash, pos, row=""):
        """Log lines to write for this frame (usually none).

        A frame that parsed to nothing counts too: the held position is simply
        repeated while the reader fails, which looks like a freeze from outside
        and used to go unreported.
        """
        out = []
        changed = self.last_hash is not None and frame_hash != self.last_hash
        self.last_hash = frame_hash
        if pos is not None and pos != self.pos:
            if self.said:
                out.append(f"system row moving again after "
                           f"{now - self.since:.0f} s")
            self.pos, self.since = pos, now
            self.reads, self.dropped, self.changed, self.said = 0, 0, 0, False
            return out
        if self.since is None:
            return out
        if pos is None:
            self.dropped += 1
        else:
            self.reads += 1
        self.changed += changed
        if not self.said and now - self.since >= self.after:
            self.said = True
            if self.dropped > self.reads:
                verdict = ("most frames did not parse, so the reader is "
                           "losing the row" + ("" if row else " (no Root row "
                                               "text seen at all)"))
            elif self.changed == 0:
                verdict = "the capture looks stale or the HUD stopped redrawing"
            else:
                verdict = ("the screen is redrawing, so the row or the reader "
                           "is stuck")
            out.append(f"system pos unchanged for {now - self.since:.0f} s "
                       f"({self.reads} reads, {self.dropped} unparsed); screen "
                       f"image changed {self.changed} times: {verdict}; "
                       f"row {row!r}")
        return out


# ----------------------------------------------------------------------------
# Shared state
# ----------------------------------------------------------------------------


class State:
    def __init__(self, cfg, waypoints):
        self.lock = threading.Lock()
        self.cfg = cfg
        self.waypoints = waypoints
        self.planet = None
        self.radius = cfg["default_radius"]
        self.pos = None
        self.latlon = None
        self.fix_time = 0.0
        self.ref = None
        self.heading = None
        self.heading_time = 0.0
        self.target = None
        self.status = "starting..."
        self.note = ""
        self.note_time = 0.0
        self.hits = 0            # OCR cycles that produced a usable fix
        self.misses = 0
        self.radius_votes = {}   # planet -> {radius reading: times seen}
        self.elev = None         # terrain height over the reference sphere, m
        self.agl = None          # height over that terrain, m; None if unsure
        self.replaced = None     # (planet, record it replaced) until reviewed
        self.heights_bad = 0     # height reads dropped for failing the identity
        self.heights_time = 0.0  # when heights were last believed
        self.heights_conflict = False   # last frame's heights contradicted it
        self.heights_seen = None # (elev, alt, |pos| - radius, radius) when they did
        self.game_up = None      # True / False / None when it cannot be told
        self.planet_misses = 0   # frames whose Planet row could not be read
        self.radius_from_table = 0  # frames whose radius reading was overruled
        self.radius_corrected = 0   # frames where the table lost to the HUD
        self.sys_pos = None      # system-frame position, once it is believed
        self.sys_prev = None     # the one before it, for the stillness test
        self.sys_candidate = None   # a reading waiting to be corroborated
        self.sys_log_time = 0.0  # when the system position was last logged
        self.sys_log_pos = None  # and what it was, to measure drift against
        self.sys_hits = 0        # how many frames have agreed with it
        self.space_ref = None    # (sys_pos, time) the deep-space track is measured from
        self.space_track = None  # unit vector of travel in the system frame
        self.space_speed = None  # metres per second along it
        self.space_speeds = []   # the last few measurements, averaged
        self.space_track_time = 0.0
        self.sys_pos_time = 0.0  # when the believed system position last changed
        self.cam_dir = None      # where the camera points, unit vector, if read
        self.cam_dir_time = 0.0
        self.cam_dir_seen = None    # the last reading, believed or not
        self.cam_dir_logged = False
        self.nose_check_time = 0.0
        self.spin_votes = []     # (body, time, star's longitude) not yet agreed
        self.system = None       # which star system the body is in
        self.location = None     # the game's own name for the place, if read
        self.location_seen = None   # a different name, not yet believed
        self.location_hits = 0
        self.hud_up = False      # debug HUD on screen, fix or no fix
        self.planet_candidate = None      # a different body name, not yet believed
        self.planet_candidate_hits = 0
        self.planet_unmatched = None     # last name that matched no body
        self.analysis = None     # a spin.SpinSession while one is running
        self.bodies = {}         # planet -> what its analysis found
        self.stations = {}       # {"fetched": time, "data": UEX records}
        self.survey = False      # recording ground heights into the height map
        self.survey_marked = set()  # bodies this survey has put a waypoint on
        self.survey_pattern = "free fly"   # the last one started, see survey.py
        self.survey_plan = None  # the course being flown, None for free fly
        self.survey_saved = None  # the last square, flown or not: SURVEY_FILE
        self.survey_guide = None  # what survey.step said at the last fix
        self.heightmap = {}      # body -> surveyed cells, see heightmap.py
        self.heightmap_dirty = False
        self.heightmap_saved = 0.0
        self.traces = []         # traced lines, see traces.py: TRACES_FILE
        self.trace = None        # the one being traced, also in self.traces
        self.trace_last = None   # (pos, time) of its last point
        self.trace_why = None    # why the last fix added no point, or None
        self.traces_dirty = False
        self.traces_saved = 0.0
        self.checked_pos = None  # (planet, pos, time) the heights last backed
        self.log = lambda msg: None

    def flash(self, text):  # call with lock held
        self.note = text
        self.note_time = time.time()

    @staticmethod
    def _heights_back_the_reading(heights, r, radius, radius_known, table):
        """Do the HUD's heights vouch for this frame's radius over the table's?

        True only when the identity holds for one and not the other, so a
        frame that cannot tell them apart leaves the table in charge.
        """
        elev, alt = heights or (None, None)
        if not radius_known or elev is None or alt is None or radius == table:
            return False
        total = elev + alt
        return (abs(total - (r - radius)) <= HEIGHT_TOLERANCE
                and abs(total - (r - table)) > HEIGHT_TOLERANCE)

    def _settle_system_pos(self, pos, now):
        """The system-frame position to believe. Call with the lock held.

        A reading is taken only when it is consistent with one already
        believed. A misread digit moves the position by 600,000 km where
        flight moves it by under one, so consistency over time is what
        separates them -- the two HUD rows cannot, as they garble identically.

        With nothing believed yet, SYSTEM_RELOCK_FRAMES readings have to agree
        before one is. Two would not be enough: the same digit can be misread
        the same way twice running, and a pair of matching errors would take
        the lock exactly as a pair of matching truths would.

        Quantum travel outruns the limit and drops the lock. That is correct:
        the position is simply re-acquired once enough frames agree again.
        """
        if pos is None:
            return self.sys_pos
        limit = SYSTEM_LOCK_KM * 1000.0
        # With a track, the ship can only be about where the track puts it:
        # last position plus speed times the time since. A middle digit
        # misread moves the reading by kilometres, which is inside the 100 km
        # relock limit but far outside this, so it would otherwise be believed
        # and make the distance and the hints jump for a frame. Generous on
        # purpose (four times the expected move, plus a floor) so a boost or
        # a hard turn is not mistaken for a misread.
        if (self.sys_pos is not None and self.space_track is not None
                and self.space_speed is not None
                and now - self.space_track_time < self.cfg["space_track_age"]):
            dt = max(0.0, now - self.sys_pos_time)
            expect = tuple(p + t * self.space_speed * dt
                           for p, t in zip(self.sys_pos, self.space_track))
            slack = 4.0 * self.space_speed * dt + 500.0
            if math.dist(pos, expect) > slack and math.dist(pos, self.sys_pos) <= limit:
                self.log(f"system reading {math.dist(pos, expect):,.0f} m off "
                         f"the track (slack {slack:,.0f} m), held")
                return self.sys_pos
        near = (self.sys_pos is not None
                and math.dist(pos, self.sys_pos) <= limit)
        if self.sys_pos is not None and not near:
            # Only jumps and misreads get here; logged, with sub-second
            # times, as this is what showed the row freezing in a jump.
            self.log(f"system reading {math.dist(pos, self.sys_pos) / 1000:,.0f}"
                     f" km out, held: t={now:.2f} {pos}")
        if near:
            self.sys_candidate = None               # flight, not a misread
            self.sys_hits = 0
            self.sys_prev = self.sys_pos
            return pos
        if (self.sys_candidate is not None
                and math.dist(pos, self.sys_candidate) <= limit):
            self.sys_hits += 1
            self.sys_candidate = pos                # follow the drift
            if self.sys_hits >= SYSTEM_RELOCK_FRAMES:
                self.sys_candidate = None
                self.sys_hits = 0
                self.sys_prev = None
                return pos
            return self.sys_pos
        self.sys_candidate = pos
        self.sys_hits = 1
        return self.sys_pos

    def _track_space(self, now):
        """Keep the deep-space track up to date. Call with the lock held.

        The track is the direction the believed system position moves in,
        measured over at least `space_min_move` metres AND `space_min_time`
        seconds, so neither OCR jitter of a few metres nor the half-second
        jitter in when a frame lands can swing it. The speed is smoothed over
        the last few measurements, because a single one is distance over a
        time that OCR timing makes uncertain by a large fraction. It is your
        direction of travel, not the way the ship points, exactly like the
        planet heading: it only exists once you have moved, and it goes
        stale after `space_track_age` seconds so a parked ship does not keep
        pointing the way it last flew.

        Losing the system lock (a quantum jump, the game stopping) drops the
        track with it, as the next position has nothing in common with the
        last. A relock looks like a jump of more than SYSTEM_LOCK_KM from the
        reference, which starts the measurement over.
        """
        pos = self.sys_pos
        if pos is None:
            self.space_ref = None
            self.space_track = None
            return
        if (self.space_ref is None
                or math.dist(pos, self.space_ref[0]) > SYSTEM_LOCK_KM * 1000.0):
            self.space_ref = (pos, self.sys_pos_time)
            self.space_track = None
            self.space_speeds = []
            return
        ref, t0 = self.space_ref
        if self.sys_pos_time <= t0:
            return                          # same reading as last time
        d = math.dist(pos, ref)
        dt = self.sys_pos_time - t0
        if d < self.cfg["space_min_move"] or dt < self.cfg["space_min_time"]:
            return
        self.space_track = tuple((p - q) / d for p, q in zip(pos, ref))
        self.space_speeds = (self.space_speeds + [d / dt])[-4:]
        self.space_speed = sum(self.space_speeds) / len(self.space_speeds)
        self.space_track_time = now
        self.space_ref = (pos, self.sys_pos_time)

    def system_still(self):
        """Is the system position steady enough to be worth marking?

        Marking a parked ship is the point, so this only has to be tight
        enough to exclude a misread, not to demand perfect stillness.
        """
        with self.lock:
            return (self.sys_pos is not None and self.sys_prev is not None
                    and math.dist(self.sys_pos, self.sys_prev)
                    <= SYSTEM_STABLE_KM * 1000.0)

    def _recheck_spin(self, planet, pos, sys_pos, now):
        """Re-anchor the body's spin on where its star is now, once
        SPIN_VOTES frames in a row agree on it, and save it when the record
        was SPIN_CORRECT_DEG out (see spin.reanchor). Call with the lock held.
        """
        rec = self.bodies.get(planet)
        sub = spin.measured_subsolar(rec, sys_pos, pos)
        if sub is None:
            self.spin_votes = []
            return
        votes = [v for v in self.spin_votes if v[0] == planet]
        omega = rec["omega_deg_s"]
        if any(abs(spin.wrap180(s - omega * (now - t) - sub)) > SPIN_AGREE_DEG
               for _, t, s in votes):
            votes = []                              # start again from this one
        self.spin_votes = votes + [(planet, now, sub)]
        if len(self.spin_votes) < SPIN_VOTES:
            return
        self.spin_votes = []
        new, err = spin.reanchor(rec, sub, now)
        if abs(err) < SPIN_CORRECT_DEG:
            return
        self.bodies[planet] = new
        save_json(BODIES_FILE, self.bodies)
        self.log(f"spin of {planet} re-anchored: record {err:+.3f} deg out, "
                 f"rate {omega:.9f} -> {new['omega_deg_s']:.9f} deg/s")
        self.flash(f"{planet}: spin corrected by {err:+.1f} deg")

    def _settle_location(self, name):
        """The place name to believe this frame. Call with the lock held.

        The row OCRs badly: twelve frames of one unmoving location gave six
        different strings -- "NewBabbage" read as "KewBabbage", "RewBabbage",
        even "Heyfphhrge" -- so taking whatever the latest frame said made the
        line flicker between misreadings of the same name.

        A new name therefore has to repeat before it replaces the one on
        screen, the same rule the body name uses, and for the same reason: a
        garbled name rarely repeats identically, a real move always does.
        """
        if not name or name == self.location:
            self.location_seen = None
            self.location_hits = 0
            return self.location
        if self.location is None:
            return name                   # nothing on screen yet to protect
        if name == self.location_seen:
            self.location_hits += 1
        else:
            self.location_seen = name
            self.location_hits = 1
        if self.location_hits >= LOCATION_SWITCH_FRAMES:
            return name
        return self.location

    def _log_system_drift(self, now):
        """Log the system position and its drift since the last line.

        Raw metres rather than fmt_dist: the question is whether a parked ship
        holds still to centimetres, and the metre tier rounds 8 cm to "0 m".
        """
        if self.sys_pos is None or now - self.sys_log_time < SYSTEM_LOG_SECONDS:
            return
        # The last body fix beside it, with its own time: the star map turns
        # that into a system position (starmap.system_point), and a parked
        # ship is where the two can be checked against each other.
        fix = (f"; planet {self.planet} {self.pos} at t={self.fix_time:.2f}"
               if self.pos is not None else "")
        if self.sys_log_pos is None:
            self.log(f"system pos {self.sys_pos}{fix}")
        else:
            self.log(f"system pos {self.sys_pos} moved "
                     f"{math.dist(self.sys_pos, self.sys_log_pos):,.3f} m in "
                     f"{now - self.sys_log_time:.1f}s{fix}")
        self.sys_log_pos, self.sys_log_time = self.sys_pos, now

    def _settle_planet(self, planet):
        """The body to believe this frame. Call with the lock held.

        Changing body is rare -- it means flying to another planet -- while
        misreading its name happens whenever the view changes. The Planet row
        is separate from CamPos and fails independently of it, and the name
        OCRs badly enough that "Planetinyxl" is normal; the repair table
        catches "nyxl" and "micr0Tech" but not "microTecn" or "rnicroTech".

        Each unrepaired variant is a different body as far as the waypoint
        list is concerned, which emptied the panel mid-flight and resized it.
        A save during one of those frames would have been filed under a body
        that does not exist. So a new name has to repeat before it is
        believed: a garbled one rarely repeats identically, a real one always
        does. Keeping the old body also keeps its agreed radius, and with it
        the height, instead of dropping to default_radius.
        """
        if planet not in BODY_SYSTEM and planet != "unknown":
            # No body by that name, even allowing for OCR damage (see
            # fuzzy_body): a misread, which repeats as long as the screen
            # does not change, so it is never believed. A body missing from
            # the table has to be added to it.
            if planet != self.planet_unmatched:
                self.planet_unmatched = planet
                self.log(f"planet read as {planet}: no such body")
            planet = "unknown"
        if self.planet in (None, "unknown"):
            return planet                      # nothing worth protecting yet
        if planet == self.planet:
            self.planet_candidate = None
            self.planet_candidate_hits = 0
            return planet
        if planet == "unknown":                # row unread; not a body at all
            self.planet_misses += 1
            if self.planet_misses == 1:
                self.log(f"planet row unread - keeping {self.planet}")
            return self.planet
        if planet == self.planet_candidate:
            self.planet_candidate_hits += 1
        else:
            self.planet_candidate = planet
            self.planet_candidate_hits = 1
            self.log(f"planet read as {planet}, holding {self.planet} "
                     f"until it repeats")
        if self.planet_candidate_hits >= PLANET_SWITCH_FRAMES:
            self.log(f"planet changed {self.planet} -> {planet}")
            return planet
        return self.planet

    def set_hud_visible(self, up):
        """Record whether the debug HUD is on screen. Logs each change.

        A fix needs the HUD; the absence of one does not mean the absence of
        the HUD. Knowing which it is separates "between bodies" from "the HUD
        is off", and only the second is something to go and fix.
        """
        with self.lock:
            if up == self.hud_up:
                return
            self.hud_up = up
            self.log("debug HUD visible" if up else "debug HUD not on screen")

    def set_game_up(self, up):
        """Record whether the game is running, and log each change.

        A closed game must not leave its last position on the overlay, so the
        fix is dropped with it: a stale readout that looks live is worse than
        no readout. None means detection failed and is treated as running.
        """
        with self.lock:
            if up is self.game_up:
                return
            self.game_up = up
            if up is False:
                self.pos = self.latlon = None
                self.elev = self.agl = None
                self.heights_conflict = False
                self.location = None
                self.sys_pos = self.sys_prev = self.sys_candidate = None
                self.sys_hits = 0
                self.space_ref = self.space_track = None
                self.analysis = None
                self.status = "game not running"
                self.log("game not running - idling, no capture")
            elif up is True:
                self.log("game detected - capturing")
            else:
                self.log("cannot tell if the game is running - capturing anyway")

    def _take_cam_dir(self, cam_dir, now):
        """Keep the camera direction current. Call with the lock held.

        A reading is believed only when it agrees with the previous one to
        within CAMDIR_AGREE_DEG. The unit-length check in the parser cannot
        catch a dropped minus sign, which mirrors the direction and would
        swing the nose hint by up to 180 degrees for one frame; two frames in
        a row rarely drop the same sign. While you turn, successive readings
        differ by a few degrees at most at the frame rate here, well inside
        the tolerance, so this costs one frame of lag and nothing else.

        Logged once, and then checked against the deep-space track every
        20 s while both exist: if the two frames disagree the angle between
        them will be large and steady while flying straight, which is the
        evidence to go on if the nose hint ever points wrong.
        """
        if cam_dir is None:
            return
        prev = self.cam_dir_seen
        self.cam_dir_seen = cam_dir
        if prev is not None:
            dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(cam_dir, prev))))
            if math.degrees(math.acos(dot)) > CAMDIR_AGREE_DEG:
                return                      # wait for the next frame to agree
        self.cam_dir, self.cam_dir_time = cam_dir, now
        if not self.cam_dir_logged:
            self.cam_dir_logged = True
            self.log(f"CamDir read: {cam_dir}")
        if now - self.nose_check_time < 20:
            return
        notes = []
        if (self.space_track is not None
                and now - self.space_track_time < self.cfg["space_track_age"]):
            dot = max(-1.0, min(1.0, sum(a * b for a, b in
                                         zip(cam_dir, self.space_track))))
            notes.append(f"system track {math.degrees(math.acos(dot)):.1f} deg")
        if (self.heading is not None and now - self.heading_time < 15
                and self.latlon is not None):
            # The nose as a compass bearing, were CamDir in the planet frame.
            lat, lon = (math.radians(a) for a in self.latlon)
            east = (-math.sin(lon), math.cos(lon), 0.0)
            north = (-math.sin(lat) * math.cos(lon),
                     -math.sin(lat) * math.sin(lon), math.cos(lat))
            e = sum(a * b for a, b in zip(cam_dir, east))
            n = sum(a * b for a, b in zip(cam_dir, north))
            brg = (math.degrees(math.atan2(e, n)) + 360.0) % 360.0
            diff = (brg - self.heading + 540.0) % 360.0 - 180.0
            notes.append(f"planet heading {diff:+.1f} deg "
                         f"(nose {brg:03.0f} vs HDG {self.heading:03.0f})")
        if notes:
            self.nose_check_time = now
            self.log("nose vs " + ", ".join(notes)
                     + " -- near 0 for whichever frame CamDir is in, "
                     "when flying straight and looking ahead")

    def update(self, res, heights=None, system=None, location=None,
               sys_pos=None, cam_dir=None):
        now = time.time()
        with self.lock:
            # Tracked whatever else the frame held: it is the only position
            # there is with no body in range, and it is what a waypoint out
            # there is measured against.
            before = self.sys_pos
            self.sys_pos = self._settle_system_pos(sys_pos, now)
            if self.sys_pos is not None and self.sys_pos != before:
                self.sys_pos_time = now
            self._track_space(now)
            self._take_cam_dir(cam_dir, now)
            self._log_system_drift(now)
            # A place with no ground is sampled whether or not a body is in
            # range, and only on a reading this frame produced.
            if (isinstance(self.analysis, spin.SpaceSession)
                    and sys_pos is not None and self.sys_pos == sys_pos):
                self.analysis.add(now, sys_pos)
            if res is None:
                missing = "readout not found - run calibrate"
                if self.status != missing:
                    self.log("fix lost: no CamPos row in the capture"
                             if self.status == "ok" else
                             "no CamPos row in the capture - is the debug HUD "
                             "on? (r_DisplayInfo 2)")
                self.status = missing
                self.misses += 1
                self.elev = self.agl = None
                self.heights_conflict = False
                return
            self.location = self._settle_location(location)
            planet, radius, pos, radius_known = res
            settled = self._settle_planet(planet)
            planet = settled
            # The table wins: it is keyed on the body, which has already been
            # settled, whereas the HUD's own copy of the system name is only
            # there for some bodies. The reading is the fallback for a body
            # added after this table was written.
            self.system = BODY_SYSTEM.get(planet) or system or self.system
            r = math.sqrt(sum(c * c for c in pos))
            # The Radius row OCRs badly (a 0 read as 6 turns 1,000,000 into
            # 1,600,000) and a wrong radius both throws away good positions and
            # corrupts the height. Collect the readings that agree with the
            # position and use the roundest, rather than trusting this frame.
            # Readings are collected either way: for a body the table does not
            # know, and for one whose table entry a patch has made stale, they
            # are the only source there is.
            if radius_known and plausible(r, radius, True):
                votes = self.radius_votes.setdefault(planet, {})
                votes[radius] = votes.get(radius, 0) + 1

            table = BODY_RADII.get(planet)
            if table is not None and self._heights_back_the_reading(
                    heights, r, radius, radius_known, table):
                # The table is exact where a reading is noisy, but it is not
                # beyond being wrong. Where the HUD's own two height values
                # agree with the frame's radius and disagree with the table's,
                # the reading wins: that is evidence, where preferring the
                # table is assumption.
                #
                # Crusader's entry was 10 m out. Small enough to navigate by,
                # large enough to break the identity, so every height reading
                # on Crusader was being thrown away -- and, once a save
                # consulted the identity, every save there too whenever the
                # Radius row failed to parse. That entry is corrected now;
                # this branch stays for the next one a patch makes stale,
                # which will not announce itself either.
                self.radius_corrected += 1
                if self.radius_corrected == 1:
                    self.log(f"table has {table:,.0f} for {planet}, but the "
                             f"HUD's heights agree with {radius:,.0f}")
            elif table is not None:
                # The table is exact where the reading is noisy, and voting on
                # noise only picks the least bad of several wrong answers: one
                # session settled on 213,601 m for Euterpe, whose radius is
                # 213,000 m, and carried that 601 m error into every height.
                # So a body the table knows takes its radius from the table.
                #
                # Applied whether or not the position agrees with it, so that
                # the gate below judges the fix against the body's real size.
                # Conditioning this on agreement left an unreadable Radius row
                # falling through to default_radius, whose window spans
                # 100km-50,000km -- which is how a fix 4,000 km from Clio was
                # once accepted and saved.
                #
                # This is not a staleness check. If a patch changed a body's
                # radius the table would keep winning and every height would
                # be wrong by the difference, with nothing here able to tell.
                # BODY_RADII has to be corrected when the game changes one.
                if radius_known and radius != table:
                    self.radius_from_table += 1
                    if self.radius_from_table == 1:
                        self.log(f"radius read {radius:,.0f}, using the "
                                 f"table's {table:,.0f} for {planet}")
                radius, radius_known = table, True
            else:
                agreed = best_radius(self.radius_votes.get(planet, {}), r)
                if agreed is not None:
                    if agreed != radius:
                        self.log(f"radius read {radius:,.0f}, "
                                 f"using {agreed:,.0f}")
                    radius, radius_known = agreed, True
            if not plausible(r, radius, radius_known):
                self.log(f"fix rejected: r={r:,.0f} radius={radius:,.0f} "
                         f"pos={pos}")
                self.status = f"ignored implausible reading (r={r:,.0f} m)"
                self.misses += 1
                self.elev = self.agl = None
                self.heights_conflict = False
                return
            # Heights are only shown when the HUD's own two values add up to
            # the height the position implies:
            #   |CamPos| - radius == ElevationUnderCam + Altitude
            # verified exact in game. Neither reading has a roundness or table
            # fallback to lean on, so this sum is the only evidence the digits
            # OCRed correctly -- a 0 read as 6 breaks it by thousands. Checked
            # against the agreed radius, since that is the one trusted by now.
            # Held briefly rather than dropped the instant one frame fails.
            # Otherwise the height line alternates between two different
            # quantities several times a second, which reads as the altitude
            # itself jumping around.
            elev, alt = heights or (None, None)
            fresh_elev = None
            if elev is not None and alt is not None:
                if abs((elev + alt) - (r - radius)) <= HEIGHT_TOLERANCE:
                    self.elev, self.agl = elev, alt
                    fresh_elev = elev
                    self.heights_time = now
                    self.heights_conflict = False
                else:
                    # Recorded, not only counted: a save consults this, since
                    # the disagreement is as likely to be the position's fault
                    # as the heights'.
                    self.heights_conflict = True
                    self.heights_seen = (elev, alt, r - radius, radius)
                    self.heights_bad += 1
                    if self.heights_bad == 1:
                        self.log(f"heights dropped: elev {elev:,.1f} + alt "
                                 f"{alt:,.1f} != {r - radius:,.1f}")
            else:
                # Unread heights contradict nothing.
                self.heights_conflict = False
            if now - self.heights_time > HEIGHT_HOLD_SECONDS:
                self.elev = self.agl = None
            lat, lon, _ = to_latlon(*pos)
            if planet != self.planet:
                self.ref = None
                self.heading = None
            if self.ref is None:
                self.ref = (lat, lon)
            else:
                d = surface_distance(self.ref[0], self.ref[1], lat, lon, radius)
                if d >= self.cfg["min_move"]:
                    self.heading = bearing(self.ref[0], self.ref[1], lat, lon)
                    self.heading_time = now
                    self.ref = (lat, lon)
            # Judged against the last position the heights vouched for, not
            # the last one read: a frame with its heights unread carries a
            # position nothing has checked, and measuring from a misread one
            # dropped good frames after it (Harper's Point, 2026-10-14).
            ref = self.checked_pos
            jumped = (ref is not None and ref[0] == planet
                      and too_fast(ref[1], ref[2], pos, now))
            if fresh_elev is not None:
                self.checked_pos = (planet, pos, now)
            self.planet, self.radius, self.pos = planet, radius, pos
            self.latlon = (lat, lon)
            self.fix_time = now
            if self.status != "ok":
                self.log("first fix" if self.misses == 0
                         else f"fix regained after {self.misses} misses")
                self.misses = 0
            self.status = "ok"
            self.hits += 1
            # Only a height this frame read and the identity backed: the one
            # on screen is held for HEIGHT_HOLD_SECONDS, and recording that
            # would copy it onto ground the ship has since flown over. Nothing
            # above survey_max_height_m either, where the HUD's ground is a
            # unreadable or suspect -- and so stations never shape the ground.
            # Nor a position that jumped: a dropped minus sign mirrors the fix
            # to the far side of the body at the same distance from its
            # centre, which the height identity cannot see (microTech,
            # 2026-10-14: longitude 39.7 read as 140.3, 1,093 km away).
            if self.survey:
                why = survey_refusal(elev, alt, fresh_elev, jumped, planet,
                                     r - radius, self.cfg)
                if why is None:
                    heightmap.add_sample(self.heightmap, planet, radius, pos,
                                         fresh_elev)
                    self.heightmap_dirty = True
                    if planet not in self.survey_marked:
                        self.survey_marked.add(planet)
                        mark_survey_start(self)
                # Every frame, kept or not, with the raw readings: where the
                # HUD's ground turns to noise on a climb is read off this.
                self.log(f"survey h {r - radius:,.1f} elev {elev} alt {alt} "
                         f"{why or 'kept'}")
                # Not on a jump either: a misread could pass a corner.
                if self.survey_plan is not None and not jumped:
                    plan = self.survey_plan
                    was = (plan["leg"], plan["joined"], plan["origin"])
                    moving = (self.heading if self.heading is not None
                              and now - self.heading_time < 15 else None)
                    self.survey_guide = survey.step(plan, planet, pos, radius,
                                                    moving)
                    if (plan["leg"], plan["joined"], plan["origin"]) != was:
                        save_json(SURVEY_FILE, plan)  # resumable from here
            if self.trace is not None:
                trace_fix(self, planet, radius, pos, now)
            # A sample is the system position and the planet-frame position
            # from the same frame. Only a reading this frame produced and the
            # settling accepted counts: otherwise a held value would be
            # sampled over and over and read as a ship that has stopped.
            #
            # Not while the HUD's own heights contradict the position: the
            # analysis trusts that one planet-frame position throughout, and a
            # misread digit that repeats on a parked ship (the image does not
            # change, so neither does the misread) turned Clio's spin into
            # "moving".
            if (isinstance(self.analysis, spin.SpinSession)
                    and sys_pos is not None and self.sys_pos == sys_pos
                    and not self.heights_conflict):
                if not self.analysis.add(now, sys_pos, pos):
                    self.log("analysis: ship moved - starting again")
            # The same pairing keeps an analysed body's spin anchor current,
            # on the same conditions, and not from a fix that jumped: a
            # dropped minus sign mirrors it at the same distance and height.
            if (sys_pos is not None and self.sys_pos == sys_pos
                    and not self.heights_conflict and not jumped):
                self._recheck_spin(planet, pos, sys_pos, now)
            # In space but inside a body's zone: whether the place holds still
            # over the body decides how it is found again (see nearest_space).
            if (isinstance(self.analysis, spin.SpaceSession)
                    and not self.heights_conflict):
                self.analysis.add_over(planet, pos)

    def set_error(self, msg):
        with self.lock:
            if not self.status.startswith("error:"):
                self.log(f"error: {msg}")
            self.status = f"error: {msg}"


# ----------------------------------------------------------------------------
# Is the game running?
#
# With no game there is no HUD, and OCRing an empty desktop costs one Tesseract
# process per contrast cut in the ladder, every interval -- some 9,500 of them
# across one idle session, which is the likeliest cause of the heap corruption
# Tesseract was reported to die of. So look for the process first and skip the
# capture entirely when it is absent.
#
# Fails soft: an unreadable process list returns None, meaning "cannot tell",
# and None is treated as running so a detection problem can never stop the
# program working. Only an explicit False idles it.
# ----------------------------------------------------------------------------

_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = ctypes.c_void_p(-1).value

# Spelled out rather than taken from ctypes.wintypes, which does not import off
# Windows and would make this module unimportable there.
_DWORD, _HANDLE, _BOOL = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int32


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", _DWORD), ("cntUsage", _DWORD),
                ("th32ProcessID", _DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", _DWORD),
                ("cntThreads", _DWORD),
                ("th32ParentProcessID", _DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", _DWORD),
                ("szExeFile", ctypes.c_char * 260)]


def _kernel32():
    """kernel32 with argtypes set. A HANDLE returned as a default int would be
    truncated on 64-bit, so the signatures are not optional here."""
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.argtypes = [_DWORD, _DWORD]
    k.CreateToolhelp32Snapshot.restype = _HANDLE
    for fn in (k.Process32First, k.Process32Next):
        fn.argtypes = [_HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
        fn.restype = _BOOL
    k.CloseHandle.argtypes = [_HANDLE]
    k.CloseHandle.restype = _BOOL
    return k


def running_processes():
    """Lowercased names of every running process, or None if they cannot be listed."""
    if sys.platform != "win32":
        return None
    try:
        k = _kernel32()
        snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE:
            return None
        try:
            ent = _PROCESSENTRY32()
            ent.dwSize = ctypes.sizeof(ent)
            names = set()
            ok = k.Process32First(snap, ctypes.byref(ent))
            while ok:
                names.add(ent.szExeFile.decode("latin-1", "replace").lower())
                ok = k.Process32Next(snap, ctypes.byref(ent))
            return names
        finally:
            k.CloseHandle(snap)
    except OSError:
        return None


_ERROR_ALREADY_EXISTS = 183

# Held for the life of the process. A mutex handle closes when the last
# reference to it goes, which would release the claim while still running.
_instance_claim = None


def claim_single_instance(name="sc-locator-overlay"):
    """Claim the one-overlay-per-session lock. True if it is ours to take.

    Two copies ruin each other quietly. Both draw in the same corner, so the
    overlay ghosts and shows two readouts at once. Neither symptom points anywhere near the cause -- it cost a round of
    confused screenshots to notice a forgotten instance was still running.

    Fails open: if the claim cannot be made or tested, the answer is yes. A
    second overlay is a nuisance, refusing to start is worse.
    """
    global _instance_claim
    if sys.platform != "win32":
        return True
    try:
        k = ctypes.windll.kernel32
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.CreateMutexW.restype = _HANDLE
        handle = k.CreateMutexW(None, 1, "Local\\" + name)
        if not handle:
            return True
        if ctypes.GetLastError() == _ERROR_ALREADY_EXISTS:
            k.CloseHandle(handle)
            return False
        _instance_claim = handle
        return True
    except OSError:
        return True


def game_running(wanted, listing=None):
    """True / False / None ("cannot tell") for whether the game is up."""
    names = running_processes() if listing is None else listing
    if names is None:
        return None
    return any(n.lower() in names for n in wanted)


def make_logger(path):
    """Append-only diagnostic log, or a no-op when --log was not given."""
    if not path:
        return lambda msg: None
    lock = threading.Lock()

    def log(msg):
        line = "%s %s\n" % (time.strftime("%H:%M:%S"), msg)
        with lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)

    log("--- run started ---")
    return log


def on_hotkey(state, label, fn):
    """Record every button press before acting on it.

    Tk only prints an exception raised inside a callback, so without this a
    failing save would look exactly like a click that never arrived.
    """
    def handler():
        state.log("key %s" % label)
        try:
            fn(state)
        except Exception as exc:
            with state.lock:
                state.flash("%s failed: %s" % (label, exc))
            state.log("key %s FAILED: %r" % (label, exc))

    return handler


# ----------------------------------------------------------------------------
# Waypoint edits
#
# Plain functions over a list of waypoint dicts: no I/O, no Tk, no state. The
# CLI loads a list, calls one of these, then saves; the overlay calls the same
# function against the list it already holds in memory. Both paths therefore
# enforce identical rules, and the overlay no longer has to duplicate them.
# Each raises ValueError carrying a message fit to show the user directly.
# ----------------------------------------------------------------------------


def auto_name(wps, planet):
    """Next free '<planet>-NNN', counting the waypoints already on that body."""
    existing = {w["name"] for w in wps}
    n = sum(1 for w in wps if w["planet"] == planet) + 1
    while f"{planet}-{n:03d}" in existing:
        n += 1
    return f"{planet}-{n:03d}"


def add_waypoint(wps, planet, pos, radius, name=None, elev=None, agl=None,
                 system=None, frame="planet", wtype="poi"):
    """Append a waypoint in place and return its name. None auto-names it.

    `elev` is the terrain's height over the reference sphere at this spot and
    `agl` how far above that terrain you were. Both are recorded only when the
    HUD agreed with itself (see State.update), and left out otherwise, because
    height over the reference sphere is not a number a player can use: a
    hangar on microTech reads 1,771 m there while standing on the ground.
    """
    if name is None:
        name = auto_name(wps, planet)
    else:
        name = name.strip()
        if not name:
            raise ValueError("name cannot be empty")
        if any(w["name"] == name for w in wps):
            raise ValueError(f"'{name}' already exists")
    x, y, z = pos
    entry = {
        "name": name, "planet": planet,
        "system": system or BODY_SYSTEM.get(planet, "unknown"),
        # Which frame x, y and z are in. "planet" is planet-local metres, as
        # every waypoint was before deep space; "system" is the system frame,
        # which is the only one that exists with no body in range.
        "frame": frame,
        "type": wtype,
        "x": x, "y": y, "z": z,
        "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if radius is not None:
        entry["radius"] = radius
    if elev is not None:
        entry["elev"] = elev
        entry["agl"] = agl
    wps.append(entry)
    return name


def rename_waypoint(wps, old, new):
    """Rename in place. Renaming to the current name is allowed and is a no-op."""
    new = new.strip()
    if not new:
        raise ValueError("name cannot be empty")
    target = next((w for w in wps if w["name"] == old), None)
    if target is None:
        raise ValueError(f"no waypoint named '{old}'")
    if new != old and any(w["name"] == new for w in wps):
        raise ValueError(f"'{new}' already exists")
    target["name"] = new


def delete_waypoint(wps, name):
    """Remove a waypoint in place."""
    for i, w in enumerate(wps):
        if w["name"] == name:
            del wps[i]
            return
    raise ValueError(f"no waypoint named '{name}'")


def set_waypoint_note(wps, name, note):
    """Set a waypoint's note in place. An empty note removes it."""
    target = next((w for w in wps if w["name"] == name), None)
    if target is None:
        raise ValueError(f"no waypoint named '{name}'")
    note = note.strip()
    if note:
        target["note"] = note
    else:
        target.pop("note", None)


def set_waypoint_parent(wps, name, parent):
    """Which body a deep-space waypoint is shown around on the star map.

    A body key attaches it there, "none" keeps it off every body, and None
    hands it back to distance (see starmap.attached_body).
    """
    target = next((w for w in wps if w["name"] == name), None)
    if target is None:
        raise ValueError(f"no waypoint named '{name}'")
    if target.get("frame") != "system":
        raise ValueError(f"'{name}' is on a body already")
    if parent is None:
        target.pop("parent", None)
    else:
        target["parent"] = parent


def filter_waypoints(wps, system, body, wtype="all"):
    """What the waypoint list shows: (shown, systems, bodies).

    `system` and `body` are a name or "all"; a name nothing carries any more
    counts as "all". `wtype` is one of WAYPOINT_TYPES or "all". `systems` are
    those of every waypoint of that type, `bodies` only those in the chosen
    system, both sorted, for the filter's choices.
    """
    def sys_of(w):
        return w.get("system", "unknown")

    wps = [w for w in wps if wtype == "all" or w.get("type", "poi") == wtype]
    systems = sorted({sys_of(w) for w in wps})
    in_sys = [w for w in wps if system not in systems or sys_of(w) == system]
    bodies = sorted({w["planet"] for w in in_sys})
    shown = [w for w in in_sys if body not in bodies or w["planet"] == body]
    return shown, systems, bodies


TYPE_NAMES = {"poi": "point of interest", "survey": "survey"}


def waypoint_text(w, around=None, bold=True):
    """A waypoint as a few lines to paste into a chat such as Discord.

    `around` is the body a deep-space waypoint is shown around, if any: out
    there it is the only place a reader can find on their own map. `bold`
    marks the name up as Discord bold.
    """
    kind = TYPE_NAMES.get(w.get("type", "poi"))
    name = f"**{w['name']}**" if bold else w["name"]
    lines = [name + (f" — {kind}" if kind else "")]
    system = w.get("system", "unknown").title()
    if w.get("frame") == "system":
        lines.append(f"{system} › deep space"
                     + (f", near {around.title()}" if around else ""))
    else:
        lat, lon, _ = to_latlon(w["x"], w["y"], w["z"])
        lines.append(f"{system} › {w['planet'].title()}")
        lines.append(f"lat {lat:+.3f}  lon {lon:+.3f}  · "
                     + " ".join(describe_height(w).split()))
    note = (w.get("note") or "").strip()
    if note:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


def export_json(path, data):
    """Write waypoints or reports to `path` in the format of their own file."""
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


IMPORT_NEAR_M = 1000.0   # an imported waypoint this close to one of ours asks
# What an imported waypoint may carry; anything else in the file is dropped.
IMPORT_FIELDS = ("name", "planet", "system", "frame", "type", "x", "y", "z",
                 "radius", "elev", "agl", "parent", "note")


def _is_number(v):
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def check_import_record(w):
    """What stops a waypoint from a shared file being used; [] if nothing.

    Every field a waypoint is placed by is required. Not every waypoint has
    every field, though: radius is only on the planet frame, elev and agl only
    where the HUD agreed with itself, and frame is missing on the oldest
    (which are all planet frame). Those are checked only when present.
    """
    if not isinstance(w, dict):
        return ["not a waypoint"]
    problems = []
    for key in ("name", "planet", "system"):
        if not isinstance(w.get(key), str) or not w[key].strip():
            problems.append(f"{key} missing" if key not in w
                            else f"{key} is not a name")
    if "type" not in w:
        problems.append("type missing")
    elif w["type"] not in WAYPOINT_TYPES:
        problems.append(f"type '{w['type']}' unknown")
    for key in ("x", "y", "z"):
        if key not in w:
            problems.append(f"{key} missing")
        elif not _is_number(w[key]):
            problems.append(f"{key} is not a number")
    frame = w.get("frame", "planet")
    if frame not in ("planet", "system"):
        problems.append(f"frame '{frame}' unknown")
    elif frame == "planet":
        if "radius" not in w:
            problems.append("radius missing")
        elif not _is_number(w["radius"]) or w["radius"] <= 0:
            problems.append("radius is not a size")
    for key in ("elev", "agl"):
        if w.get(key) is not None and not _is_number(w[key]):
            problems.append(f"{key} is not a number")
    for key in ("note", "parent"):
        if key in w and not isinstance(w[key], str):
            problems.append(f"{key} is not text")
    return problems


def read_import_file(path):
    """(good, bad) from a shared waypoints file.

    `bad` is [(label, problems)] for the records that cannot be used. A file
    that is not a list of records at all raises ValueError.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"not a waypoints file: {exc}") from None
    if not isinstance(data, list):
        raise ValueError("not a waypoints file: expected a list of waypoints")
    good, bad = [], []
    for i, w in enumerate(data, 1):
        problems = check_import_record(w)
        if problems:
            name = w.get("name") if isinstance(w, dict) else None
            bad.append((f"'{name}'" if isinstance(name, str) and name.strip()
                        else f"record {i}", problems))
        else:
            good.append(w)
    return good, bad


def prepare_import(w, now):
    """A checked record as our waypoint: saved now, with the note saying so."""
    out = {k: w[k] for k in IMPORT_FIELDS if k in w}
    out["name"] = out["name"].strip()
    out["frame"] = out.get("frame", "planet")
    out["saved"] = now
    note = (w.get("note") or "").strip()
    out["note"] = f"Imported on : {now}" + (f"\n{note}" if note else "")
    return out


def waypoint_gap(a, b):
    """Metres between two waypoints, or None where their coordinates cannot be
    compared: planet-frame ones only on the same body, system-frame ones only
    in the same system."""
    frame = a.get("frame", "planet")
    if frame != b.get("frame", "planet"):
        return None
    if a.get("system") != b.get("system"):
        return None
    if frame == "planet" and a["planet"] != b["planet"]:
        return None
    return math.dist((a["x"], a["y"], a["z"]), (b["x"], b["y"], b["z"]))


def import_conflicts(w, wps, near_m=IMPORT_NEAR_M):
    """(same_name, nearby) for an imported waypoint against ours.

    `same_name` is our waypoint with its name, or None. `nearby` is
    [(metres, waypoint)] for our others within near_m, nearest first.
    """
    same = next((q for q in wps if q["name"] == w["name"]), None)
    nearby = []
    for q in wps:
        if q is same:
            continue
        gap = waypoint_gap(w, q)
        if gap is not None and gap <= near_m:
            nearby.append((gap, q))
    nearby.sort(key=lambda p: p[0])
    return same, nearby


def conflict_text(w, same, nearby):
    """The warning shown before an imported waypoint that clashes is added."""
    def block(q):
        lines = waypoint_text(q, bold=False).splitlines()
        if q.get("saved"):
            lines.insert(1, f"saved {q['saved']}")
        return "\n".join("    " + line for line in lines)

    out = [f"'{w['name']}' in the file clashes with your waypoints.", "",
           "Imported:", block(w)]
    if same is not None:
        gap = waypoint_gap(w, same)
        out += ["", "Has the same name as yours"
                + (f" ({fmt_dist(gap)} away):" if gap is not None else ":"),
                block(same)]
    if nearby:
        out += ["", f"Is within {IMPORT_NEAR_M:,.0f} m of yours:"]
        for gap, q in nearby:
            out += [f"  {fmt_dist(gap)} away:", block(q)]
    target = (same or nearby[0][1])["name"]
    out += ["", "Yes adds it as well"
            + (" under a new name" if same is not None else "")
            + f", No leaves it out, Replace puts it in place of '{target}'."]
    return "\n".join(out)


def free_name(wps, name):
    """`name`, or the first of 'name (2)', 'name (3)', ... nobody has."""
    taken = {w["name"] for w in wps}
    n, out = 2, name
    while out in taken:
        out, n = f"{name} ({n})", n + 1
    return out


def import_waypoint(wps, w, replace=None):
    """Add an imported waypoint in place, in place of the one named `replace`.

    Checked before anything is removed, so a refused import loses nothing.
    """
    if replace is not None and not any(q["name"] == replace for q in wps):
        raise ValueError(f"no waypoint named '{replace}'")
    if any(q["name"] == w["name"] and q["name"] != replace for q in wps):
        raise ValueError(f"'{w['name']}' already exists")
    if replace is not None:
        delete_waypoint(wps, replace)
    wps.append(w)


def import_summary(source, added, replaced, declined, bad):
    """What an import did, for the message at its end."""
    out = [f"From {source}:",
           f"  {len(added)} added",
           f"  {len(replaced)} replaced one of yours",
           f"  {len(declined)} left out by you",
           f"  {len(bad)} skipped with problems"]
    if bad:
        out.append("")
        out += [f"{label}: {', '.join(problems)}" for label, problems in bad]
    return "\n".join(out)


def set_report_notes(bodies, key, notes):
    """Set the notes on an analysis report in place. Empty removes them."""
    rec = bodies.get(key)
    if rec is None:
        raise ValueError(f"no report for '{key}'")
    notes = notes.strip()
    if notes:
        rec["notes"] = notes
    else:
        rec.pop("notes", None)


def stored_report_lines(rec):
    """A report as the viewer shows it for a place you are not at."""
    lines = spin.report_lines(rec)
    if rec.get("station"):
        lines += stations.report_lines(rec["station"], READOUT_COLS)
    return lines


def report_text(title, rec, lines):
    """A report to paste into a chat such as Discord: the report itself in a
    code block, so its columns stay lined up, then the notes."""
    out = [f"**{title}** — {rec.get('system', 'unknown').title()}",
           "```", *lines, "```"]
    notes = (rec.get("notes") or "").strip()
    if notes:
        out.append(f"Notes: {notes}")
    return "\n".join(out)


# What each kind of report is placed and drawn by, from what the analysis
# writes (spin.make_record, spin.make_space_record) and the report reads.
REPORT_COMMON = {"body": "planet system radius_m kind site samples duration_s",
                 "space": "system kind site_sys samples duration_s"}
REPORT_NEEDS = {
    ("body", "rotating"): "rho_m rho_local_m omega_deg_s period_s sigma "
                          "arc_deg axis axis_tilt_deg centre star_distance_m "
                          "declination_deg t_ref subsolar_lon_ref",
    ("body", "moving"): "rho_m rho_local_m",
    ("body", "static"): "period_longer_than_s",
    ("space", "fixed"): "",
    ("space", "moving"): "speed_mps direction",
}
_VECTORS = {"axis", "centre", "site_sys", "site_over", "direction"}
_TEXTS = {"planet", "system", "kind", "label", "notes", "over", "analysed"}


def _field_problem(key, v):
    """What is wrong with one report field's value, or None."""
    if key in _VECTORS:
        ok = (isinstance(v, list) and len(v) == 3
              and all(_is_number(c) for c in v))
        return None if ok else f"{key} is not a position"
    if key == "site":
        ok = isinstance(v, dict) and all(_is_number(v.get(c))
                                         for c in ("lat", "lon"))
        return None if ok else "site is not a lat/lon"
    if key == "station":
        ok = isinstance(v, dict) and isinstance(v.get("name"), str)
        return None if ok else "station has no name"
    if key in _TEXTS:
        return None if isinstance(v, str) else f"{key} is not text"
    return None if _is_number(v) else f"{key} is not a number"


def check_report_record(key, rec):
    """What stops a report from a shared file being used; [] if nothing.

    The fields required depend on what the report is: a body (rotating,
    static or moving) or a place in space (fixed or moving). Label, notes,
    station and the analysed date are optional; the date is replaced anyway.
    """
    if not isinstance(rec, dict):
        return ["not a report"]
    scope = "space" if rec.get("scope") == "space" else "body"
    if "scope" in rec and rec["scope"] != "space":
        return [f"scope '{rec['scope']}' unknown"]
    kind = rec.get("kind")
    if kind is None:
        return ["kind missing"]
    if (scope, kind) not in REPORT_NEEDS:
        return [f"kind '{kind}' unknown for a {scope} report"]
    need = (REPORT_COMMON[scope] + " " + REPORT_NEEDS[scope, kind]).split()
    if "over" in rec or "site_over" in rec:
        need += ["over", "site_over"]
    problems = []
    for field in need:
        if field not in rec:
            problems.append(f"{field} missing")
        elif _field_problem(field, rec[field]):
            problems.append(_field_problem(field, rec[field]))
    for field in ("label", "notes", "station", "analysed"):
        if field in rec and _field_problem(field, rec[field]):
            problems.append(_field_problem(field, rec[field]))
    if scope == "body" and isinstance(rec.get("planet"), str) \
            and key != rec["planet"]:
        problems.append(f"filed under '{key}', not its body '{rec['planet']}'")
    if scope == "space" and not key.startswith("space "):
        problems.append(f"'{key}' is not a place name")
    return problems


def read_report_file(path):
    """(good, bad) from a shared reports file.

    `good` is [(key, record)]; `bad` is [(key, problems)] for the records
    that cannot be used. A file that is not a set of reports raises
    ValueError.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"not a reports file: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("not a reports file: expected reports by place")
    good, bad = [], []
    for key, rec in data.items():
        problems = check_report_record(key, rec)
        if problems:
            bad.append((f"'{key}'", problems))
        else:
            good.append((key, rec))
    return good, bad


def prepare_report_import(rec, now):
    """A checked report as ours: dated now, with the notes saying so."""
    out = dict(rec)
    out["analysed"] = now
    notes = (rec.get("notes") or "").strip()
    out["notes"] = f"Imported on : {now}" + (f"\n{notes}" if notes else "")
    return out


def place_gap(a, b):
    """Metres between two places in space, or None where they cannot be
    compared. As spin.nearest_space matches them: a place that turns with a
    body only by its position over that same body, any other by its system
    position in the same system."""
    if a.get("system") != b.get("system"):
        return None
    if a.get("site_over") is not None or b.get("site_over") is not None:
        if a.get("over") != b.get("over") or None in (a.get("site_over"),
                                                     b.get("site_over")):
            return None
        return math.dist(a["site_over"], b["site_over"])
    return math.dist(a["site_sys"], b["site_sys"])


def report_conflicts(key, rec, bodies):
    """(same, nearby) for an imported report against ours.

    `same` is our report under the same key, or None. `nearby` is
    [(metres, key, report)] for our other places in space close enough to
    count as the same place, nearest first.
    """
    same = bodies.get(key)
    nearby = []
    if rec.get("scope") == "space":
        for k, q in bodies.items():
            if k == key or not isinstance(q, dict) \
                    or q.get("scope") != "space":
                continue
            gap = place_gap(rec, q)
            if gap is not None and gap <= spin.SPACE_MATCH_M:
                nearby.append((gap, k, q))
        nearby.sort(key=lambda p: p[0])
    return same, nearby


def report_conflict_text(key, rec, same, nearby):
    """The warning shown before an imported report that clashes is added.
    With `same`, only No and Replace are offered: one key holds one report."""
    def block(r):
        lines = stored_report_lines(r)
        notes = (r.get("notes") or "").strip()
        if notes:
            lines += [f"Notes    {line}" if i == 0 else f"         {line}"
                      for i, line in enumerate(notes.splitlines())]
        return "\n".join("    " + line for line in lines)

    out = [f"'{rec.get('label') or key}' in the file clashes with your "
           "reports.", "", "Imported:", block(rec)]
    if same is not None:
        out += ["", "Yours for the same "
                + ("place:" if rec.get("scope") == "space" else "body:"),
                block(same)]
    if nearby:
        out += ["", f"Within {fmt_dist(spin.SPACE_MATCH_M)} of yours, which "
                "the app counts as the same place:"]
        for gap, _, q in nearby:
            out += [f"  {fmt_dist(gap)} away:", block(q)]
    if same is not None:
        out += ["", "Only one report is kept here: No keeps yours, Replace "
                "takes theirs."]
    else:
        target = nearby[0][2].get("label") or nearby[0][1]
        out += ["", "Yes adds it as well, No leaves it out, Replace puts it "
                f"in place of '{target}'."]
    return "\n".join(out)


def import_report(bodies, key, rec, replace=None):
    """Add an imported report in place, in place of the one under `replace`.

    Checked before anything is removed, so a refused import loses nothing.
    """
    if replace is not None and replace not in bodies:
        raise ValueError(f"no report for '{replace}'")
    if key in bodies and key != replace:
        raise ValueError(f"'{key}' already has a report")
    if replace is not None:
        del bodies[replace]
    bodies[key] = rec


def save_waypoint(state, name=None):
    with state.lock:
        if state.pos is None:
            return _save_in_system_frame(state, name)
        age = time.time() - state.fix_time
        if age > state.cfg["stale_after"]:
            state.flash(f"fix is {age:.0f}s old - not saved")
            state.log(f"save refused: fix {age:.1f}s old")
            return
        # Above the far line the planet frame is not a place. It rotates with
        # its body -- 0.021 deg/s on Crusader -- so only something turning
        # with the body holds still in it. The ground does, and a synchronous
        # station does, which is why everything at or below 373 km measured
        # here is stored that way and stays correct. A jump point does not: at
        # 54.47 Gm the same spin sweeps it 18,583 km/s, and the waypoint saved
        # there was wrong seconds after it was written.
        #
        # The system frame is static -- measured parked at a jump point, nine
        # readings over 52 s, 0.000 m -- and planets do not orbit, so a point
        # out there keeps its coordinates indefinitely. Checked after the
        # staleness gate so a fresh fix decides this, and before the height
        # check because the heights gate what a planet-frame save stores and
        # have no bearing on a system-frame one.
        r = math.sqrt(sum(c * c for c in state.pos))
        if far_from_body(r, state.radius, state.cfg):
            state.log(f"{fmt_dist(r - state.radius)} over {state.planet} - "
                      f"saving in the system frame")
            return _save_in_system_frame(state, name)
        # |CamPos| - radius == ElevationUnderCam + Altitude holds exactly in
        # game, so a frame where it fails has mangled digits on one side or the
        # other, and nothing says which. Dropping only the heights kept the
        # position, which is the half a waypoint is made of: Everus Harbor
        # saved 117 km from where it stands that way, and the record looked no
        # different from a good one. A refused save costs a second.
        if state.heights_conflict:
            state.flash("HUD heights disagree with the position - not saved")
            elev, alt, h, radius = state.heights_seen
            state.log(f"save refused: heights contradict pos={state.pos}: "
                      f"elev {elev:,.1f} + alt {alt:,.1f} != {h:,.1f} "
                      f"(radius {radius:,.0f})")
            return
        try:
            name = add_waypoint(state.waypoints, state.planet, state.pos,
                                state.radius, name, state.elev, state.agl,
                                state.system)
        except ValueError as exc:
            state.flash(str(exc))
            state.log(f"save refused: {exc}")
            return
        save_json(WAYPOINT_FILE, state.waypoints)
        state.flash(f"saved {name}")
        state.log(f"saved {name} at {state.pos} on {state.planet}")


def _save_in_system_frame(state, name):
    """Mark a point in the static system frame. Call with the lock held.

    Two callers. A ship parked between bodies has no planet-local position at
    all, so without this it cannot be marked. And anything above the far line
    has one that is useless: the planet frame turns with its body, so a point
    not turning with it sweeps away at thousands of km a second.

    The position has to have settled first. A misread digit moves it by
    600,000 km, and unlike a planet fix there is no radius or terrain height
    to contradict one -- the only thing that can is the position holding still
    across frames, so that is what is required before anything is written.
    """
    if state.sys_pos is None:
        state.flash("no fix yet - not saved")
        state.log("save refused: no fix, and no system position either")
        return
    if (state.sys_prev is None
            or math.dist(state.sys_pos, state.sys_prev)
            > SYSTEM_STABLE_KM * 1000.0):
        state.flash("position still settling - not saved")
        state.log("save refused: system position has not settled")
        return
    try:
        name = add_waypoint(state.waypoints, DEEP_SPACE, state.sys_pos, None,
                            name, system=state.system or "unknown",
                            frame="system")
    except ValueError as exc:
        state.flash(str(exc))
        state.log(f"save refused: {exc}")
        return
    save_json(WAYPOINT_FILE, state.waypoints)
    state.flash(f"saved {name} in deep space")
    state.log(f"saved {name} at system {state.sys_pos}")


# ----------------------------------------------------------------------------
# Body analysis
#
# Landed on a body you turn with it, so the system-frame position sweeps a
# circle about the spin axis. The maths is in spin_analysis; this is only the
# plumbing: start a session, let State.update feed it, and write the result
# down when it is as good as it will get.
# ----------------------------------------------------------------------------


def in_space(state):
    """Is the ship somewhere with no ground under it? Call with the lock held.

    Between bodies there is no planet-frame position at all; near one, above
    the space line is a station or a Lagrange point, not the surface.
    """
    if state.pos is None:
        return True
    r = math.sqrt(sum(c * c for c in state.pos))
    return above_space_line(r, state.radius, state.cfg)


def current_place(state):
    """(key, record) for where the ship is, or (None, None). Lock held."""
    if in_space(state):
        return spin.nearest_space(state.bodies, state.sys_pos, state.planet,
                                  state.pos)
    return state.planet, state.bodies.get(state.planet)


def start_analysis(state):
    """Begin sampling the body the ship is on, or the place it is parked at."""
    with state.lock:
        if state.analysis is not None:
            state.flash("already analysing")
            return
        if state.pos is None and state.sys_pos is None:
            state.flash("no fix - cannot analyse")
            return
        if state.sys_pos is None:
            state.flash("no system position yet - wait a few seconds")
            return
        if in_space(state):
            state.analysis = spin.SpaceSession(time.time())
            state.flash(f"analysing this place - {ANALYSIS_ADVICE}")
            state.log("analysis started in space")
            return
        if state.planet in (None, "unknown"):
            state.flash("no fix on a body - cannot analyse")
            return
        state.analysis = spin.SpinSession(state.planet, time.time())
        state.flash(f"analysing {state.planet} - {ANALYSIS_ADVICE}")
        state.log(f"analysis started on {state.planet}")


def finish_analysis(state, why="stopped"):
    """Close the session and keep what it found. Returns the record or None.

    A run that said nothing writes nothing: an earlier, better record is not
    to be replaced by an empty one because a second try was cut short.
    """
    with state.lock:
        session, state.analysis = state.analysis, None
        if session is None:
            return None
        stamp = time.strftime("%Y-%m-%d %H:%M")
        state.replaced = None
        if isinstance(session, spin.SpaceSession):
            record = spin.make_space_record(session, state.system or "unknown",
                                            stamp)
            key = None
            if record is not None:
                key = (spin.nearest_space(state.bodies, record["site_sys"],
                                          record.get("over"),
                                          record.get("site_over"))[0]
                       or spin.space_key(record["site_sys"]))
        else:
            # The name the session started under can be an OCR misread that
            # the reader later corrected; the held name is the better one.
            if state.planet not in (None, "unknown"):
                session.planet = state.planet
            key = session.planet
            record = spin.make_record(session, session.fit(), session.planet,
                                      state.system or "unknown", state.radius,
                                      stamp)
        if record is None:
            state.flash("not enough to say - hold still longer")
            state.log(f"analysis ended ({why}): no result")
            return None
        old = state.bodies.get(key)
        for kept in ("label", "station", "notes"):  # a new run keeps these
            if old and old.get(kept):
                record[kept] = old[kept]
        state.replaced = (key, old)
        state.bodies[key] = record
        save_json(BODIES_FILE, state.bodies)
        state.flash(f"{key}: {record['kind']}")
        state.log(f"analysis of {key} ended ({why}): {record['kind']}"
                  f", {record['samples']} samples")
        return record


def poll_analysis(state, now=None):
    """Finish the session if it has what it needs. True if it just finished."""
    now = time.time() if now is None else now
    with state.lock:
        session = state.analysis
        if session is None:
            return False
        done, why = session.verdict(now)
    if done:
        finish_analysis(state, why)
    return done


def analysis_progress(state, now=None):
    """One line on how a running analysis is getting on, or ''."""
    now = time.time() if now is None else now
    with state.lock:
        return state.analysis.progress(now) if state.analysis else ""


def body_report(state, now=None):
    """The report lines for the body the ship is on, sun figures live."""
    now = time.time() if now is None else now
    with state.lock:
        record = current_place(state)[1]
        here = state.latlon if state.pos is not None else None
    if record is None or record.get("kind") != "rotating" or here is None:
        lines = spin.report_lines(record)
    else:
        lines = spin.report_lines(record, here[0], here[1], now)
    if record is not None and record.get("station"):
        lines += stations.report_lines(record["station"], READOUT_COLS)
    return lines


def too_fast(prev, prev_time, pos, now, limit=SURVEY_MAX_SPEED):
    """Did the fix move faster than `limit` m/s since the previous one?

    A misread the heights cannot see (a dropped minus sign keeps the distance
    from the centre) becomes the reference, so the frame after it is dropped
    too and the one after that is clean again. Two samples lost, against one
    cell on the wrong side of the body.
    """
    d = math.sqrt(sum((a - b) ** 2 for a, b in zip(pos, prev)))
    return d > limit * max(now - prev_time, 1.0)


# The pilot's reminder while surveying: speed against sample spacing. Measured
# at Harper's Point (microTech, 2026-10-14): 33 m/s gave a sample every 92 m,
# 107 m/s every 450 m, 180 m/s every 365 m -- a frame every 1.5-2.1 s, and a
# quarter to a half of them dropped. A line crossing 70-100 m cells at an
# angle needs closer than that, so 33 m/s still left gaps. Flown low at just
# under 20 m/s (17.7 m/s): a sample every ~34 m, and 18 of 22 steps between
# new cells went to the neighbouring cell.
SURVEY_SPEEDS = [
    "   20 m/s   ~35 m apart   almost no gaps",
    "   30 m/s   ~90 m apart   small gaps",
    "  100 m/s  ~300 m apart   overview",
    "  200 m/s  ~400 m apart   rough shapes",
]


def survey_refusal(elev, alt, fresh_elev, jumped, planet, height, cfg):
    """Why this frame gives no survey sample, or None to keep it.

    `elev`, `alt` are this frame's raw readings, `fresh_elev` the elevation if
    it passed the identity, `height` the fix's height over the sphere.
    """
    if elev is None or alt is None:
        return "dropped: heights unread"
    if fresh_elev is None:
        return "dropped: heights fail the identity"
    if height > cfg["survey_max_height_m"]:
        return "dropped: above survey_max_height_m"
    if jumped:
        return "dropped: position jumped"
    if planet not in BODY_RADII:
        return "dropped: unknown body"
    return None


def toggle_survey(state):
    """Start or stop recording ground heights. Stopping saves them; starting
    flies the pattern started last."""
    with state.lock:
        on = state.survey
    if on:
        stop_survey(state)
    else:
        start_survey(state, state.survey_pattern)


def start_survey(state, pattern):
    """Start recording ground heights, flying `pattern` (survey.PATTERNS).

    A square starts at the targeted waypoint, so a point of interest is at
    its middle; with nothing targeted, where you are at the next fix.
    """
    with state.lock:
        wp = next((w for w in state.waypoints if w["name"] == state.target),
                  None)
        if wp is not None and wp.get("frame", "planet") != "planet":
            wp = None                   # deep space: no ground to start on
        state.survey, state.survey_marked = True, set()
        state.survey_pattern = pattern
        plan = survey.new_plan(
            pattern, wp and wp["planet"], wp and (wp["x"], wp["y"], wp["z"]))
        state.survey_plan, state.survey_guide = plan, None
        if plan is not None:            # a new square replaces the last one
            if wp is not None:
                plan["start"] = wp["name"]
            state.survey_saved = plan
            save_json(SURVEY_FILE, plan)
        state.flash(f"survey on: {pattern}")
    state.log(f"survey on: {pattern}")


def resume_survey(state, skip=False):
    """Fly the last square on, from the start of the leg it stopped on, or
    with `skip` from the first leg past those already flown."""
    with state.lock:
        plan = state.survey_saved
        if plan is None:
            state.flash("no survey square to resume")
            return
        if skip:
            plan["leg"] = resume_leg(state, plan)
        plan["joined"] = False
        state.survey, state.survey_marked = True, set()
        state.survey_pattern = plan["pattern"]
        state.survey_plan, state.survey_guide = plan, None
        save_json(SURVEY_FILE, plan)
        state.flash(f"survey resumed at LEG {plan['leg'] + 1}")
    state.log(f"survey resumed at leg {plan['leg'] + 1}: {plan['pattern']}")


def resume_leg(state, plan):
    """The leg skipping ahead would resume `plan` at: survey.skip_to over the
    cells measured round its start. Call with the lock held."""
    rec = state.heightmap.get(plan["planet"])
    if rec is None or plan["origin"] is None:
        return plan["leg"]
    frame = heightmap.local_frame(plan["origin"])
    # 30 x 30 km: at 100 m/s a 500 m square that size is 2.5 hours flying.
    reach = 15_000.0
    cells = heightmap.local_cells(rec, rec["radius"], frame, reach, reach)
    return survey.skip_to(plan, [(x, y) for x, y, _ in cells])


def stop_survey(state):
    """Stop recording ground heights and save them. A square stays saved,
    at the leg it stopped on, for resume_survey."""
    with state.lock:
        state.survey = False
        state.survey_plan = state.survey_guide = None
        if state.survey_saved is not None:
            save_json(SURVEY_FILE, state.survey_saved)
        state.flash("survey off")
    state.log("survey off")
    save_heightmap(state, force=True)


def start_trace(state, kind):
    """Start tracing a line of `kind` (traces.KINDS) from the next fix."""
    with state.lock:
        name = traces.trace_name(state.traces, kind)
        state.trace = traces.new_trace(kind, name, time.time())
        state.traces.append(state.trace)
        state.trace_last = state.trace_why = None
        state.flash(f"tracing {name}")
    state.log(f"trace on: {name}")


def trace_fix(state, planet, radius, pos, now):
    """Add this fix to the trace being flown. Call with the lock held.

    A lake's first point also sets its level: the camera's height over the
    reference sphere less water_below_cam_m, as it starts skimming the water.

    The HUD's heights are not asked to vouch for the position, as survey
    mode does: on foot they are often not shown, and on the metro they
    contradicted good positions on 45 of 107 fixes (New Babbage, 2026-10-17).
    The position is judged against the last point instead. One whose height
    over the centre changed faster than TRACE_MAX_CLIMB is a misread: a 0
    read as 6 moves the fix ~6 km and, at New Babbage, ~3 km up or down. One
    that moved faster than SURVEY_MAX_SPEED is too (a dropped minus sign
    keeps the height).
    """
    climb = None
    if state.trace_last is not None:
        last, then = state.trace_last
        climb = abs(math.dist(pos, (0, 0, 0)) - math.dist(last, (0, 0, 0)))
    if climb is not None and climb > TRACE_MAX_CLIMB * max(now - then, 1.0):
        why = f"dropped: height jumped {climb:,.0f} m"
    elif climb is not None and too_fast(last, then, pos, now):
        why = "dropped: position jumped"
    else:
        why = traces.add_point(state.trace, planet, pos)
        if why is None:
            state.trace_last = (pos, now)
            state.traces_dirty = True
            if state.trace["kind"] == "lake" and "level" not in state.trace:
                state.trace["level"] = round(
                    math.dist(pos, (0, 0, 0)) - radius
                    - state.cfg["water_below_cam_m"], 1)
    state.trace_why = why
    state.log(f"trace {state.trace['name']}: {why or 'added'}")


def stop_trace(state):
    """Stop tracing and save. A trace of fewer points than its kind needs
    (traces.LEAST: two for a line, three for a lake) is dropped."""
    with state.lock:
        t, state.trace = state.trace, None
        if t is None:
            return
        if len(t["points"]) < traces.LEAST[t["kind"]]:
            state.traces.remove(t)
            state.flash(f"{t['name']} dropped: too short")
        else:
            state.flash(f"saved {t['name']}: "
                        f"{fmt_dist(traces.length(t))}")
        state.traces_dirty = True
    state.log(f"trace off: {t['name']}, {len(t['points'])} points")
    save_traces(state, force=True)


def save_traces(state, force=False, now=None):
    """Write the traces if they changed and TRACES_SAVE_SECONDS have passed
    since the last write, or at once with `force`. True if written."""
    now = time.time() if now is None else now
    with state.lock:
        if not state.traces_dirty or (
                not force and now - state.traces_saved < TRACES_SAVE_SECONDS):
            return False
        save_json(TRACES_FILE, state.traces, indent=None)
        state.traces_dirty = False
        state.traces_saved = now
    return True


def survey_name(wps):
    """First free 'survey-NNN'."""
    existing = {w["name"] for w in wps}
    n = 1
    while f"survey-{n:03d}" in existing:
        n += 1
    return f"survey-{n:03d}"


def mark_survey_start(state):
    """A waypoint where a survey first recorded the ground on this body, so
    the height map can always be found again from the waypoint list. None
    when one is already within SURVEY_MARK_SPACING_M: a survey paused and
    resumed would otherwise leave a cluster of them. Call with the lock held,
    on a frame the survey kept."""
    for w in state.waypoints:
        if (w.get("type") == "survey" and w["planet"] == state.planet
                and math.dist((w["x"], w["y"], w["z"]), state.pos)
                <= SURVEY_MARK_SPACING_M):
            return
    name = add_waypoint(state.waypoints, state.planet, state.pos,
                        state.radius, survey_name(state.waypoints),
                        state.elev, state.agl, state.system,
                        wtype="survey")
    state.waypoints[-1]["note"] = "start of a survey"
    save_json(WAYPOINT_FILE, state.waypoints)
    state.flash(f"survey started: saved {name}")
    state.log(f"saved {name} at {state.pos} on {state.planet}")


def save_heightmap(state, force=False, now=None):
    """Write the height map if it changed and HEIGHTMAP_SAVE_SECONDS have
    passed since the last write, or at once with `force`. True if written."""
    now = time.time() if now is None else now
    with state.lock:
        if not state.heightmap_dirty or (
                not force and now - state.heightmap_saved
                < HEIGHTMAP_SAVE_SECONDS):
            return False
        # Compact: indented, every cell would take four lines.
        save_json(HEIGHTMAP_FILE, state.heightmap, indent=None)
        state.heightmap_dirty = False
        state.heightmap_saved = now
    return True


def load_stations(state):
    """The saved station list, fetched again if missing or a week old.

    Runs on its own thread: the fetch can take seconds and the overlay must
    not wait for it. A failed fetch keeps whatever list was saved before.
    """
    cache = load_json(STATIONS_FILE, {})
    with state.lock:
        state.stations = cache
    if not stations.stale(cache):
        return
    try:
        data = stations.fetch()
    except Exception as exc:
        state.log(f"station list not fetched: {exc!r}")
        return
    cache = {"fetched": time.time(), "data": data}
    # Downloaded, not collected: no backups, unlike the files in bak/.
    STATIONS_FILE.write_text(json.dumps(cache), encoding="utf-8")
    with state.lock:
        state.stations = cache
    state.log(f"station list fetched: {len(data)} stations")


def station_choices(state):
    """Names for the station menu: this system's, this body's first. Lock held."""
    return stations.choices(state.stations.get("data", []), state.system,
                            state.planet, resolve_body)


def link_station(state, name):
    """Link the report for this place to station `name`, or unlink on None.

    Only a place in space: a station is something the ship is parked at, and
    a planet's report is about the planet.
    """
    with state.lock:
        key, rec = current_place(state)
        if rec is None or rec.get("scope") != "space":
            state.flash("analyse this place in space first")
            return
        if name is None:
            rec.pop("station", None)
            state.flash("station unlinked")
        else:
            found = next((s for s in state.stations.get("data", [])
                          if s["name"] == name), None)
            if found is None:
                state.flash(f"{name} is not in the station list")
                return
            rec["station"] = stations.summary(
                found, state.stations.get("fetched", time.time()))
            state.flash(f"linked {name}")
        save_json(BODIES_FILE, state.bodies)
        state.log(f"{key}: station {name or 'unlinked'}")


def health(state):
    """"ok" | "stale" | "error" | "off" | "nohud" | "space".

    Takes the lock itself, so it must not be called from inside render();
    render reports the same thing through health_locked.
    """
    with state.lock:
        return health_locked(state)


def health_locked(state):
    """As health, but for a caller that already holds the lock.

    "off" and "nohud" need the game detection to have reported; when it has
    not (game_up is None, as in a bare State) the older three are returned, so
    a caller that knows nothing about the game still gets a sensible answer.
    """
    if state.game_up is False:
        return "off"
    if state.status.startswith("error:"):
        return "error"
    if state.pos is None:
        if not state.game_up:
            return "stale"
        # The HUD is up and still no position: you are between bodies, which
        # is a normal place to be and not something to go and fix.
        return "space" if state.hud_up else "nohud"
    if time.time() - state.fix_time > state.cfg["stale_after"]:
        return "stale"
    return "ok" if state.status == "ok" else "stale"


def render_status(state):
    """The health line, alone: the panel puts it above the location block."""
    with state.lock:
        return f"Status: {STATUS_TEXT[health_locked(state)]}"


def render_location(state, with_status=True):
    """Where you are: the top block of the readout.

    Split from the target block so the panel can rule a line between them.
    """
    with state.lock:
        status = f"Status: {STATUS_TEXT[health_locked(state)]}"
        if state.pos is None:
            # Being between bodies is a place, not a failure, so it is reported
            # as one. Anything else here is the HUD or the game being absent,
            # and the status line says which.
            where = "In Space" if state.game_up and state.hud_up else "unknown"
            lines = [f"Current location: {where}"]
            if with_status:
                lines.append(status)
            if state.location:
                lines.append(state.location)
            return "\n".join(lines)
        lat, lon = state.latlon
        r = math.sqrt(sum(c * c for c in state.pos))
        # A stale fix is reported on the status line rather than as a marker on
        # the location, which is about where you are and not how fresh the
        # reading of it is.
        # Being inside a body's zone is not the same as being at the body.
        # "In Space, 1000.00 Gm from microtech" is the widest this line gets,
        # and is what READOUT_COLS is sized for.
        if far_from_body(r, state.radius, state.cfg):
            where = f"In Space, {fmt_dist(r - state.radius)} from {state.planet}"
        elif above_space_line(r, state.radius, state.cfg):
            where = f"In Space near {state.planet}"
        else:
            where = f"{state.system or '?'}, {state.planet}"
        lines = [f"Current location: {where}"]
        if with_status:
            lines.append(status)
        # The game's own name for where you are. Neither the system nor the
        # body can give you that, and it is the name you would actually use.
        if state.location:
            lines.append(state.location)
        lines.append(f"LAT {lat:+9.3f}")
        lines.append(f"LON {lon:+9.3f}")
        # Height over terrain is the useful one in flight, and is only shown
        # when the HUD's own two values agreed with the position -- see the
        # identity check in State.update.
        # Height above the ground leads, because that is the one that means
        # anything wherever you are. Leading with the terrain's own elevation
        # put a 55 m figure in front of someone 955 km above it.
        if state.agl is not None:
            lines.append(f"H   {fmt_dist(state.agl)} up"
                         f"   (terrain at {fmt_dist(state.elev)})")
        else:
            lines.append(f"H   {fmt_dist(r - state.radius)} "
                         f"over reference sphere")
        return "\n".join(lines)


def render_survey(state):
    """The survey block: cells so far, the ceiling, and the speed reminder,
    or on a square the course to fly. Empty while not surveying. Its own
    block, as the location block is full."""
    return "\n".join(p for p in survey_parts(state)[:3] if p)


def survey_parts(state):
    """The survey block as (top line, middle two, bottom two, colour).

    On a square the middle two are what to fly, and `colour` is the one the
    panel draws them in, bold, to stand out from the rest of the HUD text:
    TURN_COLOUR as a corner comes up, else DIRECTIONS. Free fly has none
    (None): the middle carries the speed table on. All "" while not
    surveying.
    """
    with state.lock:
        if state.trace is not None:
            return trace_parts(state.trace, state.trace_why)
        if not state.survey:
            return "", "", "", None
        cells = heightmap.cell_count(state.heightmap, state.planet)
        top = fmt_dist(state.cfg["survey_max_height_m"])
        plan, g = state.survey_plan, state.survey_guide
    head = f"SURVEY {cells:,} cell{'' if cells == 1 else 's'}"
    if plan is None:
        return (f"{head} - records below {top}",
                "\n".join(SURVEY_SPEEDS[:2]), "\n".join(SURVEY_SPEEDS[2:]),
                None)
    course = course_lines(plan, g)
    speed = (g or {}).get("speed") or survey.speed_for(plan["leg"],
                                                       plan["spacing"])
    legs = "short" if speed == survey.SLOW_MS else "long"
    return (f"{head} - {plan['pattern']}", "\n".join(course[:2]),
            "\n".join(course[2:] + [f"fly {speed:.0f} m/s on {legs} legs"
                                    f" - stay below {top}"]),
            TURN_COLOUR if (g or {}).get("turn") else DIRECTIONS)


def trace_parts(trace, why):
    """survey_parts while tracing: how far it has come, and how to fly it."""
    n = len(trace["points"])
    top = (f" {trace['name']} - {fmt_dist(traces.length(trace))}, "
           f"{n:,} point{'' if n == 1 else 's'}")
    if trace["kind"] == "lake":
        level = trace.get("level")
        return ("LAKE" + top,
                "skim the water: the first fix sets its level"
                if level is None else f"water level {level:,.0f} m\n"
                "fly right along the waterline, all round",
                f"{why or 'last fix added'}\n"
                "END LAKE closes it back to the start",
                None)
    return ("TRACE" + top,
            "fly along it, right over it\n"
            "about 30 m/s: a point every ~60 m",
            f"{why or 'last fix added'}\n"
            "END TRACE when you reach its end",
            None)


def course_lines(plan, g):
    """Up to three lines on what to fly next, from survey.step's `g`."""
    # Cut to fit "square round " in the block's width.
    start = (plan.get("start") or "")[:READOUT_COLS - 13]
    round_ = [f"square round {start}"] if start else []
    if g is None:
        if plan["origin"] is None:
            return ["the square starts where you are", "at the next fix"]
        return [f"fly to LEG {plan['leg'] + 1} start", "waiting for a fix"] \
            + round_
    if "away" in g:
        return [f"the square is on {g['away']}", "fly there first"]
    if "to_join" in g:
        brg = "" if g["bearing"] is None else f", bearing {g['bearing']:03.0f}"
        return [f"fly to LEG {g['leg']} start: {fmt_dist(g['to_join'])}{brg}",
                f"then {g['heading']:03d} {g['name']} - joins within "
                f"{survey.START_M:.0f} m"] + round_
    if abs(g["off"]) < survey.ON_LINE_M:
        line = "on the line"
    else:
        side = "right" if g["off"] > 0 else "left"
        line = (f"{fmt_dist(abs(g['off']))} {side} of the line"
                f" - steer {g['steer']:03.0f}")
    w, h = g["size"]
    fly = f"LEG {g['leg']}  FLY {g['heading']:03d} {g['name']}  "
    then = f"{g['next_heading']:03d} {g['next_name']}"
    if g["turn"] == "late":             # past the corner, not round yet
        fly = f"LEG {g['leg']}  TURN NOW to {g['heading']:03d} {g['name']}"
    elif g["turn"] == "now":
        fly = f"LEG {g['leg']}  TURN NOW to {then}"
    elif g["turn"] == "soon":
        fly += f"TURN {then} in {fmt_dist(g['to_go'])}"
    else:
        fly += f"{fmt_dist(g['to_go'])} to go, then {then}"
    return [fly,
            line,
            f"covered {w / 1000:.1f} x {h / 1000:.1f} km"
            f"  ({w * h / 1e6:.1f} km2)"]


def render_target(state):
    """Where you are going: the lower block of the readout.

    Never empty. The panel gives this block a fixed height and rules a line
    above it either way, so returning nothing left a blank gap that read as
    something broken rather than as nothing to show.

    The orbital marker is shown whether or not there is a fix. It is a
    property of the target, not a reading, so it is exactly what can be
    planned from orbit before any position is available.
    """
    now = time.time()
    with state.lock:
        wp = next((w for w in state.waypoints if w["name"] == state.target),
                  None)
        if wp is None:
            return "no target - select one and press TARGET"

        lines = [f"TARGET {wp['name']}"]

        if wp.get("frame") == "system":
            # Out between bodies there is no surface to measure over and no
            # north to bear from. What there is: the straight-line distance,
            # where the camera points (NOSE, from the HUD's CamDir row, so it
            # works standing still) and, once you have moved, your track
            # through the system frame (TRK).
            lines.append(f"DEEP SPACE in {wp.get('system', 'unknown')}")
            if state.sys_pos is None:
                lines.append("no system position yet")
                return chr(10).join(lines)
            here, est = _predicted_sys_pos(state, now)
            gap = math.dist(here, (wp["x"], wp["y"], wp["z"]))
            lines.append(f"DIST {fmt_dist(gap)} direct" + ("  (est)" if est else ""))
            lines.append(_steer_line("NOSE", _nose_guidance(state, wp, now),
                                     "(no CamDir on HUD)"))
            g = _space_guidance(state, wp, now)
            lines.append(_steer_line("TRK ", g, "(move to get track)"))
            if g is not None:
                spd = f"SPD  {g['speed']:,.0f} m/s"
                if g["closing"] > 0:
                    spd += f"   closing   ETA {spin.fmt_duration(g['eta'])}"
                elif g["closing"] < 0:
                    spd += "   OPENING - turn around"
                lines.append(spd)
            return chr(10).join(lines)

        tlat, tlon, tr = to_latlon(wp["x"], wp["y"], wp["z"])
        # A target in orbit is reached directly, so the orbital marker and the
        # distance over the ground below it are legs nobody flies.
        in_orbit = above_space_line(tr, wp.get("radius", 1e6), state.cfg)
        if not in_orbit:
            om, _, over_ground = nearest_om(tlat, tlon, wp.get("radius", 1e6))
            lines.append(f"VIA  {om}   then {fmt_dist(over_ground)} over ground")

        # Distances are planet-local, so they mean nothing without a fix on
        # the same body. The marker above still does.
        if state.pos is None or wp.get("planet") != state.planet:
            lines.append("no fix here - distance unavailable")
            return chr(10).join(lines)

        lat, lon = state.latlon
        R = state.radius
        r = math.sqrt(sum(c * c for c in state.pos))
        surf = surface_distance(lat, lon, tlat, tlon, R)
        straight = math.dist(state.pos, (wp["x"], wp["y"], wp["z"]))
        brg = bearing(lat, lon, tlat, tlon)
        if in_orbit:
            lines.append(f"DIST {fmt_dist(straight)} direct")
        else:
            lines.append(f"DIST {fmt_dist(surf)} surface / "
                         f"{fmt_dist(straight)} direct")
        line = f"BRG  {brg:03.0f} {compass(brg)}"
        if state.heading is not None and now - state.heading_time < 15:
            rel = (brg - state.heading + 540.0) % 360.0 - 180.0
            if state.cfg["mirror"]:
                rel = -rel
            side = "RIGHT" if rel > 0 else "LEFT"
            line += f"   HDG {state.heading:03.0f}   turn {side} {abs(rel):.0f}"
        else:
            line += "   (move to get heading)"
        lines.append(line)
        lines.append(_steer_line("NOSE", _nose_guidance(state, wp, now),
                                 "(no CamDir on HUD)"))
        lines.append(f"ALT  {fmt_dist(tr - r)} vs you")
        return chr(10).join(lines)


def _steer(fwd, to, up_hint, mirror):
    """Yaw and pitch from a forward vector to a target direction.

    `fwd` and `to` are unit vectors in the same frame; `up_hint` says which
    way is up in that frame. Returns (off, yaw, pitch) in degrees: `off` is
    the whole angle between them, `yaw` is + right and - left, `pitch` is + up
    and - down. right = fwd x up, up = right x fwd. A forward straight along
    the hint has no "right"; the x axis stands in for it.
    """
    cross = lambda a, b: (a[1] * b[2] - a[2] * b[1],
                          a[2] * b[0] - a[0] * b[2],
                          a[0] * b[1] - a[1] * b[0])
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))
    # The game's frame is the opposite hand to the textbook one: with the
    # textbook right = fwd x up, both the turn and the pitch came out
    # reversed in flight (deep space, 2026-10-18). up x fwd is the game's
    # right, which flips both at once. `mirror` is a (yaw, pitch) pair of
    # flags on top of that, should a frame ever disagree.
    right = cross(up_hint, fwd)
    rn = math.sqrt(dot(right, right))
    if rn < 1e-6:
        right, rn = (1.0, 0.0, 0.0), 1.0
    right = tuple(c / rn for c in right)
    up = cross(right, fwd)
    f, r, u = dot(to, fwd), dot(to, right), dot(to, up)
    yaw = math.degrees(math.atan2(r, f))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, u))))
    off = math.degrees(math.acos(max(-1.0, min(1.0, f))))
    flip_yaw, flip_pitch = mirror
    return off, (-yaw if flip_yaw else yaw), (-pitch if flip_pitch else pitch)


def _unit_to(frm, to):
    d = [t - p for t, p in zip(to, frm)]
    n = math.sqrt(sum(c * c for c in d))
    return (None, 0.0) if n <= 0 else ([c / n for c in d], n)


def _predicted_sys_pos(state, now):
    """The system position now, carried forward along the track since the
    last read. Call with the lock held. Returns (pos, estimated).

    OCR gives a position about once a second and the Root row less often
    than that; between reads the ship keeps moving, so the distance would
    otherwise tick down in steps. The prediction is only ever a few seconds
    of dead reckoning and is dropped as soon as the track goes stale.
    """
    pos = state.sys_pos
    if (state.space_track is None or state.space_speed is None
            or now - state.space_track_time >= state.cfg["space_track_age"]):
        return pos, False
    dt = now - state.sys_pos_time
    if dt <= 0 or dt > state.cfg["space_track_age"]:
        return pos, False
    step = state.space_speed * dt
    return tuple(p + t * step for p, t in zip(pos, state.space_track)), True


def _nose_guidance(state, wp, now):
    """How to steer for the target from where the camera points, or None.

    Call with the lock held. Needs a CamDir read in the last few seconds and
    a position in the target's frame: the system position for a deep-space
    target, the planet-local one for a target on a body. CamDir is taken to
    be in that same frame, which the "nose vs track" log line is there to
    confirm. Up is the system z axis in deep space and straight up from the
    body's centre on a planet. Returns a dict like _space_guidance, without
    the speed fields.
    """
    if state.cam_dir is None or now - state.cam_dir_time >= 5.0:
        return None
    if wp.get("frame") == "system":
        if state.sys_pos is None:
            return None
        here, _ = _predicted_sys_pos(state, now)
        up = (0.0, 0.0, 1.0)
    else:
        if state.pos is None or wp.get("planet") != state.planet:
            return None
        here = state.pos
        rn = math.sqrt(sum(c * c for c in here))
        up = tuple(c / rn for c in here) if rn > 0 else (0.0, 0.0, 1.0)
    to, gap = _unit_to(here, (wp["x"], wp["y"], wp["z"]))
    if to is None:
        return None
    off, yaw, pitch = _steer(state.cam_dir, to, up,
                             (state.cfg["mirror"], state.cfg["mirror_pitch"]))
    return {"off": off, "yaw": yaw, "pitch": pitch, "gap": gap}


def _space_guidance(state, wp, now):
    """How to steer for a deep-space target from the current track, or None.

    Call with the lock held. The track is where the system position has been
    moving, so this needs a believed position, a recent track and a target in
    the system frame. Returns a dict:

      off      degrees between the track and the target, 0 = dead on
      yaw      degrees to turn, + right and - left
      pitch    degrees to pitch, + up and - down
      speed    metres per second along the track
      closing  metres per second the gap shrinks by (negative = opening)
      eta      seconds to arrive at the closing rate, or None

    The system frame has no cockpit in it, so "right" and "up" need an
    agreed horizon. Every body measured so far spins about the system z axis,
    so z is taken as up and the xy plane as level, the way the star map and
    most flying between bodies already treat it. Rolled well off level, left
    and right swap with up and down; `mirror` covers a plain reversal.
    """
    if (state.sys_pos is None or state.space_track is None
            or now - state.space_track_time >= state.cfg["space_track_age"]):
        return None
    here, _ = _predicted_sys_pos(state, now)
    to, gap = _unit_to(here, (wp["x"], wp["y"], wp["z"]))
    if to is None:
        return None
    off, yaw, pitch = _steer(state.space_track, to, (0.0, 0.0, 1.0),
                             (state.cfg["mirror"], state.cfg["mirror_pitch"]))
    f = sum(a * b for a, b in zip(to, state.space_track))
    closing = f * state.space_speed
    eta = gap / closing if closing > 0 else None
    return {"off": off, "yaw": yaw, "pitch": pitch, "speed": state.space_speed,
            "closing": closing, "eta": eta, "gap": gap}


def _steer_line(label, g, missing):
    """One readout line for a guidance dict: `label  off 23  turn RIGHT 20
    pitch UP 11`, `label  on course` under 2 degrees, or `label  missing`."""
    if g is None:
        return f"{label} {missing}"
    if g["off"] < 2:
        return f"{label} on course"
    line = f"{label} off {g['off']:.0f}"
    if abs(g["yaw"]) >= 1:
        line += f"  turn {'RIGHT' if g['yaw'] > 0 else 'LEFT'} {abs(g['yaw']):.0f}"
    if abs(g["pitch"]) >= 1:
        line += f"  pitch {'UP' if g['pitch'] > 0 else 'DOWN'} {abs(g['pitch']):.0f}"
    return line


def target_arrow(state, now=None):
    """(turn, off, pitch) for the compass arrow, or None.

    `turn` is degrees to turn, + right and - left; `off` is how far off the
    target you point in all, which sets the colour; `pitch` is + up and
    - down, None when there is no pitch to show.

    The nose (CamDir) is used whenever it is available: it answers at once
    and while standing still. Failing that, the deep-space track, or on a
    body the heading, which both need you to have moved.
    """
    now = time.time() if now is None else now
    with state.lock:
        wp = next((w for w in state.waypoints if w["name"] == state.target),
                  None)
        if wp is None:
            return None
        g = _nose_guidance(state, wp, now)
        if g is not None:
            return g["yaw"], g["off"], g["pitch"]
        if wp.get("frame") == "system":
            g = _space_guidance(state, wp, now)
            if g is None:
                return None
            return g["yaw"], g["off"], g["pitch"]
        if (state.pos is None or wp.get("planet") != state.planet
                or state.heading is None or now - state.heading_time >= 15):
            return None
        tlat, tlon, _ = to_latlon(wp["x"], wp["y"], wp["z"])
        lat, lon = state.latlon
        rel = (bearing(lat, lon, tlat, tlon) - state.heading + 540.0) % 360.0 - 180.0
        rel = -rel if state.cfg["mirror"] else rel
        return rel, abs(rel), None


def target_turn(state, now=None):
    """Degrees to turn for the target, + right and - left, or None."""
    arrow = target_arrow(state, now)
    return None if arrow is None else arrow[0]


def arrow_polygon(rel, cx, cy, size):
    """Flat x,y list for an arrow turned `rel` degrees clockwise from up."""
    a = math.radians(rel)
    sin, cos = math.sin(a), math.cos(a)
    pts = []
    for x, y in ((0, -1), (0.55, 0.6), (0, 0.25), (-0.55, 0.6)):
        pts += [cx + size * (x * cos - y * sin), cy + size * (x * sin + y * cos)]
    return pts


def arrow_colour(rel):
    """Green on course, amber off it, red well off; grey with nothing to show."""
    if rel is None:
        return "#3a4654"
    off = abs(rel)
    return STATUS_COLOURS["ok" if off < 10 else "stale" if off < 45 else "error"]


def render(state, notes=True):
    """The whole readout, for console mode and for tests.

    The panel draws the two blocks separately with a rule between them, and
    turns `notes` off because it has its own line for those.
    """
    parts = [render_location(state), render_survey(state),
             render_target(state)]
    with state.lock:
        if notes and state.status != "ok":
            parts.append(f"!! {state.status}")
        if notes and time.time() - state.note_time < 4:
            parts.append(f">> {state.note}")
    return "\n".join(p for p in parts if p)


# ----------------------------------------------------------------------------
# Workers and UI
# ----------------------------------------------------------------------------


def ocr_worker(state, cfg, stop):
    try:
        import pytesseract
        setup_tesseract(cfg)
    except Exception as exc:
        state.set_error(f"missing dependency: {exc}")
        return
    # The HUD is near-white, but so is bright terrain and sky. One fixed cut-off
    # cannot suit every background, so retry the same frame at other cut-offs
    # before giving up, and keep whichever worked at the front of the list.
    ladder = [cfg["contrast_cut"]] + [c for c in cfg["contrast_cuts"]
                                      if c != cfg["contrast_cut"]]
    watch = FreezeWatch()
    with open_capture() as sct:  # mss handles are per-thread, so create it here
        while not stop.is_set():
            t0 = time.time()
            # No game means no HUD, and a miss costs one Tesseract process per
            # contrast cut in the ladder. Skip the capture entirely rather than
            # OCRing an empty desktop several times a second.
            state.set_game_up(game_running(cfg["game_process"]))
            if state.game_up is False:
                stop.wait(max(0.05, cfg["idle_interval"] - (time.time() - t0)))
                continue
            try:
                raw = grab_region(sct, cfg)
                res = sys_pos = cam_dir = None
                with state.lock:
                    deep = state.pos is None and state.sys_pos is not None
                for i, thr in enumerate(ladder):
                    text = pytesseract.image_to_string(
                        preprocess(raw, cfg, thr), config=TESS_CONFIG)
                    # From the first cut that reads it.
                    sys_pos = sys_pos or parse_system_pos(text)
                    cam_dir = cam_dir or parse_cam_dir(text)
                    res = parse_readout(text, cfg["default_radius"])
                    if res is not None:
                        if i:
                            state.log(f"contrast cut {ladder[0]} failed, "
                                      f"{thr} worked - switching")
                            ladder.insert(0, ladder.pop(i))
                        break
                    # Out between bodies there is no planet readout to find,
                    # so once the Root row is read the remaining cuts would
                    # only cost time: each is a Tesseract run, and the frame
                    # rate out there is what the guidance lives on.
                    if deep and sys_pos is not None and cam_dir is not None:
                        break
                state.set_hud_visible(hud_visible(text))
                row = next((ln for ln in text.splitlines()
                            if "root" in canon(ln)), "")
                for line in watch.feed(time.time(),
                                       hashlib.md5(raw.tobytes()).digest(),
                                       sys_pos, row):
                    state.log(line)
                state.update(res,
                             parse_heights(text) if res else None,
                             parse_system(text) if res else None,
                             parse_location(text),
                             sys_pos, cam_dir)
            except Exception as exc:
                state.set_error(str(exc))
            stop.wait(max(0.05, cfg["interval"] - (time.time() - t0)))


# ----------------------------------------------------------------------------
# Overlay
#
# One window at two sizes. Collapsed it is a status pin, which is all that is
# wanted while flying; right-clicking expands it into a panel for naming,
# retargeting and deleting waypoints.
#
# The pin sits bottom-right for two reasons: the game draws little there, and
# the capture region is top-right -- an overlay inside it reads its own text
# back as a fix.
#
# Left-click on the pin saves, because that is the one thing worth doing
# without opening anything first. Collapsed there is no room for a readout, so
# the result of a save appears beside the pin for a few seconds: without it a
# click gives no sign of whether it worked, and that silence has cost whole
# debugging rounds before.
# ----------------------------------------------------------------------------

PIN_W, PIN_H = 30, 40

# The readout's box, in characters. Large enough for the longest line it can
# produce and the most lines it can produce, so that neither ever changes the
# window's size. The widest is the location line carrying the stale marker,
# "Current location: stanton, microtech   ** STALE **"; the tallest is a fix
# with a named place and a target set. A test asserts the worst case fits,
# because Tesseract-style silent clipping is what Tk does here too.
# The readout's two blocks, in characters. Each has a fixed size so that an
# ordinary frame cannot resize the window; a test asserts the worst case fits,
# because Tk clips silently. The location block is at most "Current location",
# "Status", the place name, LAT, LON and height; the target block is TARGET,
# the orbital marker, DIST, BRG and ALT.
READOUT_COLS = 53
ARROW_COLS = 9          # the compass arrow beside the target block
TARGET_COLS = READOUT_COLS - ARROW_COLS
ARROW_PX = 96
LOCATION_ROWS = 6
SURVEY_ROWS = 5
DIRECTIONS = "#FFFFFF"  # the survey's what-to-fly lines: apart from all else
TURN_COLOUR = "#FFE45C"  # and as a corner comes up: catches the eye
TARGET_ROWS = 6
REPORT_ROWS = 14        # the body analysis, in its block under the status
NO_LABEL = "(not labelled)"
NO_STATION = "(no station)"
# The HUD is read off the screen, so a bright light behind the CamPos row (a
# cockpit lamp, sunlit ground) garbles it. Shown while an analysis runs.
ANALYSIS_ADVICE = "hold still, look at a dark area"
PLACE_LABELS = ["planet", "moon", "orbital station", "Lagrange station",
                "jump point", "mining base", "PIO", "other"]

STATUS_COLOURS = {"ok": "#7CFFB2", "stale": "#FFC861", "error": "#FF6B6B",
                  "nohud": "#FFC861", "off": "#6A7785", "space": "#8FB8FF"}
STATUS_TEXT = {
    "ok": "fix live",
    "stale": "fix going stale",
    "error": "capture error - see the log",
    "nohud": "no HUD: press ~ then r_DisplayInfo 2",
    "off": "game not running",
    "space": "no body in range",
}


def monitor_box(cfg):
    """(left, top, width, height) of the monitor the capture reads, else None.

    Taken from mss rather than Tk so the overlay lands on the same screen the
    region is cropped from; Tk only knows about the primary one.
    """
    try:
        with open_capture() as sct:
            mons = sct.monitors
            mon = mons[min(cfg["monitor"], len(mons) - 1)]
            return mon["left"], mon["top"], mon["width"], mon["height"]
    except Exception:
        return None


def waypoint_rows(state):
    """(name, surface distance, bearing) per waypoint on the current planet.

    Distance and bearing are None when there is no fix to measure from, so the
    panel can still list and edit waypoints with the game closed.
    """
    with state.lock:
        planet, pos, R = state.planet, state.pos, state.radius
        rows = []
        for w in state.waypoints:
            # With no body named yet, list everything rather than nothing: an
            # empty panel reads as "no waypoints", which would be a lie.
            if planet not in (None, "unknown") and w["planet"] != planet:
                continue
            if pos is None:
                rows.append((w["name"], None, None))
                continue
            lat, lon = state.latlon
            tlat, tlon, _ = to_latlon(w["x"], w["y"], w["z"])
            rows.append((w["name"],
                         surface_distance(lat, lon, tlat, tlon, R),
                         bearing(lat, lon, tlat, tlon)))
        return rows


def waypoint_groups(state):
    """[(system, body, rows), ...] over every waypoint, nearest context first.

    Distance and bearing only mean anything within one body: coordinates are
    planet-local, so a waypoint on another moon has no computable distance from
    here. Those rows carry None rather than a number that would be fiction.

    The body you are on comes first, then the rest of its system, so the list
    you can act on is at the top without hiding everything else.
    """
    with state.lock:
        here_body, here_system = state.planet, state.system
        pos, radius, latlon = state.pos, state.radius, state.latlon
        sys_pos = state.sys_pos
        waypoints = list(state.waypoints)

    groups = {}
    for w in waypoints:
        groups.setdefault((w.get("system", "unknown"),
                           w.get("planet", "unknown")), []).append(w)

    out = []
    for (system, body), members in groups.items():
        rows = []
        for w in sorted(members, key=lambda q: q["name"].lower()):
            if w.get("frame") == "system":
                # Measured from the system position, which exists wherever you
                # are, so a parked ship stays reachable from a planet too.
                # No LAT/LON: there is no body to take them on.
                rows.append((w["name"],
                             None if sys_pos is None
                             else math.dist(sys_pos, (w["x"], w["y"], w["z"])),
                             None, None))
                continue
            # LAT/LON are the waypoint's own, so every body has them, fix or not.
            tlat, tlon, tr = to_latlon(w["x"], w["y"], w["z"])
            if body == here_body and pos is not None and latlon is not None:
                # Over the ground for two points on it. For a ship parked in
                # orbit, or when you are up there yourself, the ground track
                # is not the trip: two ships 13 km apart 330 km up have
                # ground tracks 340 m apart (Yela, 2026-10-18). Straight line
                # then, like the target block.
                r_here = math.sqrt(sum(c * c for c in pos))
                in_space = (above_space_line(r_here, radius, state.cfg)
                            or above_space_line(tr, radius, state.cfg))
                dist = (math.dist(pos, (w["x"], w["y"], w["z"])) if in_space
                        else surface_distance(latlon[0], latlon[1], tlat, tlon,
                                              radius))
                rows.append((w["name"], dist,
                             bearing(latlon[0], latlon[1], tlat, tlon),
                             (tlat, tlon)))
            else:
                rows.append((w["name"], None, None, (tlat, tlon)))
        out.append((system, body, rows))
    out.sort(key=lambda g: (g[1] != here_body, g[0] != here_system, g[0], g[1]))
    return out


def format_row(name, dist, brg, latlon=None, width=16):
    """One waypoint as a fixed-width line for the panel's list.

    A deep-space waypoint has a distance but no bearing: there is no north out
    there to take one from. Nor does it have LON/LAT, which follow at the
    location readout's precision when there are any. Columns as in
    list_header.
    """
    if dist is None:
        row = f"{name[:width]:<{width}}         --"
    elif brg is None:
        row = f"{name[:width]:<{width}} {fmt_dist(dist):>9}    "
    else:
        row = f"{name[:width]:<{width}} {fmt_dist(dist):>9} {brg:03.0f}"
    if latlon is None:
        return row
    # Padded first: the no-fix row is shorter, and the columns must line up.
    return f"{row:<{width + 14}}  {latlon[1]:+8.3f} {latlon[0]:+7.3f}"


def list_header(width=16):
    """Column titles over the list, at format_row's widths."""
    return f"{'Location':<{width}} {'Distance':>9} HDG  {'LON':>8} {'LAT':>7}"


def anchor_position(box, size, anchor, margin, corner=None):
    """Top-left to give a window of `size` so its anchored corner stays put.

    `box` is (left, top, width, height) of the monitor, `anchor` names the
    corner to pin ("br", "bl", "tr", "tl"), and `corner` overrides the
    computed corner once the window has been dragged somewhere.

    Anchoring a corner rather than the top-left is what lets the panel grow up
    and to the left: Tk grows a window right and down from its top-left, so a
    readout one line longer would otherwise walk it off the screen.
    """
    w, h = size
    left, top, mw, mh = box
    if corner:
        cx, cy = corner
    else:
        cx = left + mw - margin[0] if "r" in anchor else left + margin[0]
        cy = top + mh - margin[1] if "b" in anchor else top + margin[1]
    return (int(cx - w) if "r" in anchor else int(cx),
            int(cy - h) if "b" in anchor else int(cy))


def set_no_activate(root, on):
    """Stop clicks on the overlay taking focus from the game, or allow it again.

    Windows makes any window that is clicked the active one, so a click on the
    pin or the open panel left the game without keyboard input. With
    WS_EX_NOACTIVATE the click still reaches the widget but focus stays where
    it was. Tk leaves the bit alone through -topmost, -transparentcolor and
    geometry changes (checked), so setting it once holds.
    """
    if os.name != "nt":
        return
    u = ctypes.windll.user32
    u.GetAncestor.restype = ctypes.c_void_p
    u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
    root.update_idletasks()
    hwnd = u.GetAncestor(root.winfo_id(), 2)    # GA_ROOT: Tk's outer window
    style = u.GetWindowLongW(hwnd, -20)         # GWL_EXSTYLE
    u.SetWindowLongW(hwnd, -20, (style | 0x08000000) if on
                     else (style & ~0x08000000))


def keep_on_top(root):
    """Put the overlay back on top, and back on screen if it was minimized.

    -topmost is set once, but other topmost windows (Discord, Steam and GPU
    overlays, Task Manager) and the game's own display-mode changes on alt-tab
    can end up above it, and Win+D minimizes it. With no taskbar button or
    alt-tab entry there is then no way to bring it back by hand. The game
    keeps the keyboard throughout.
    """
    if os.name != "nt":
        return
    u = ctypes.windll.user32
    u.GetAncestor.restype = ctypes.c_void_p
    u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.IsIconic.argtypes = [ctypes.c_void_p]
    u.IsWindowVisible.argtypes = [ctypes.c_void_p]
    u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_uint]
    u.FindWindowW.restype = ctypes.c_void_p
    # An open dropdown is a separate topmost window; raising the overlay now
    # would put the panel over its own menu.
    menu = u.FindWindowW("#32768", None)        # the popup-menu window class
    if menu and u.IsWindowVisible(menu):
        return
    hwnd = u.GetAncestor(root.winfo_id(), 2)    # GA_ROOT: Tk's outer window
    if u.IsIconic(hwnd) or not u.IsWindowVisible(hwnd):
        # Restoring from minimized activates the window even with
        # SW_SHOWNOACTIVATE and WS_EX_NOACTIVATE (tested 2026-10-11), so
        # focus is handed straight back.
        had_focus = foreground_handle()
        u.ShowWindow(hwnd, 4)                   # SW_SHOWNOACTIVATE
        give_focus_to(had_focus)
    # HWND_TOPMOST; SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    u.SetWindowPos(hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0013)


def foreground_handle():
    """The window that has focus now, to give it back later. None off Windows."""
    if os.name != "nt":
        return None
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = ctypes.c_void_p
    return u.GetForegroundWindow()


def give_focus_to(hwnd):
    """Hand focus back after a dropdown pick.

    A dropdown works in the no-activate overlay, but picking from it makes the
    overlay the active window regardless (tested 2026-10-08), which takes the
    game's keyboard away exactly as a plain click used to.
    """
    if hwnd and os.name == "nt":
        u = ctypes.windll.user32
        u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u.SetForegroundWindow(hwnd)


def run_overlay(state, cfg):
    import tkinter as tk

    BG, DIM, ACCENT, FIELD = "#05080c", "#8FA0B4", "#7CFFB2", "#0d141c"

    root = tk.Tk()
    root.title("SC Locator")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", cfg["alpha"])
    root.configure(bg=BG)

    def set_transparent(on):
        """Make the window background vanish, or bring it back.

        Collapsed, only the pin should be visible: a dark card around it is
        exactly the clutter the corner is meant to avoid. Windows also makes
        the transparent pixels click-through, so the pin stays clickable while
        the space around it belongs to the game again.

        Expanded this must be off, or the panel's text floats unreadably over
        whatever is behind it. Unsupported elsewhere, hence the guard.
        """
        try:
            root.attributes("-transparentcolor", BG if on else "")
        except tk.TclError:
            pass

    box = monitor_box(cfg) or (0, 0, root.winfo_screenwidth(),
                               root.winfo_screenheight())
    anchor, margin = cfg["overlay_anchor"], cfg["overlay_margin"]
    dragged = {"corner": None}

    def reposition():
        root.update_idletasks()
        size = (root.winfo_reqwidth(), root.winfo_reqheight())
        x, y = anchor_position(box, size, anchor, margin, dragged["corner"])
        root.geometry(f"+{x}+{y}")
        # Moved now, not when Tk next idles. keep_on_top runs before that, and
        # its SetWindowPos reports the old position back to Tk, which then
        # keeps the old top: a block showing up grew the panel down past its
        # corner instead of up.
        root.update_idletasks()

    def button(parent, text, fn, width=7, bg=ACCENT):
        b = tk.Button(parent, text=text, font=("Consolas", 10, "bold"),
                      width=width, pady=3, fg=BG, bg=bg,
                      activebackground="#B8FFD8", relief="flat",
                      borderwidth=0, cursor="hand2")
        # Press, not release, and not the command option. A Tk button fires
        # only once it sees press AND release inside the widget, and over a
        # fullscreen game the release goes astray: the press lands and the
        # button never fires.
        b.bind("<ButtonPress-1>", lambda e: fn())
        return b

    # --- collapsed: the pin, with room for a short result beside it ---------
    collapsed = tk.Frame(root, bg=BG)
    note = tk.Label(collapsed, text="", font=("Consolas", 10), fg=DIM, bg=BG,
                    anchor="e", justify="right")
    note.pack(side="left", padx=(8, 6))
    pin = tk.Canvas(collapsed, width=PIN_W, height=PIN_H, bg=BG,
                    highlightthickness=0, cursor="hand2")
    pin.pack(side="left")

    def draw_pin(colour):
        pin.delete("all")
        pin.create_oval(3, 2, PIN_W - 3, PIN_W - 2, fill=colour, outline="")
        pin.create_polygon(PIN_W * 0.30, PIN_W * 0.76,
                           PIN_W * 0.70, PIN_W * 0.76,
                           PIN_W * 0.50, PIN_H - 2, fill=colour, outline="")
        # FIELD, not BG: BG is the transparent colour, so a hole painted in it
        # would be a click-through hole in the middle of the pin -- which is
        # exactly where the pin gets clicked.
        pin.create_oval(PIN_W * 0.37, PIN_W * 0.30,
                        PIN_W * 0.63, PIN_W * 0.56, fill=FIELD, outline="")

    # --- expanded: the waypoint panel ---------------------------------------
    panel = tk.Frame(root, bg=BG)

    # Fixed width and height in characters. A Label sizes itself to its text,
    # so a readout that gains a line when a target is set, or widens when a
    # distance grows a digit, would resize the window on an ordinary frame.
    def rule(parent=panel):
        """A hairline across the panel, separating one block from the next."""
        line = tk.Frame(parent, height=1, bg="#243040")
        line.pack(fill="x", padx=10, pady=5)
        return line

    # The health line, which a save, an edit or a running analysis takes over
    # as a toast while it has something to say. Two rows so a long message
    # wraps rather than resizing the window.
    health_lbl = tk.Label(panel, text="", justify="left", anchor="nw",
                          width=READOUT_COLS, height=2, wraplength=420,
                          font=("Consolas", 11), fg=ACCENT, bg=BG)
    health_lbl.pack(fill="x", padx=10, pady=(8, 0))

    top_rule = rule()

    # The body analysis sits under the status like the target block sits under
    # the location: shown on request, with the waypoint list and the buttons
    # staying where they are and the window growing to make room.
    report_block = tk.Frame(panel, bg=BG)
    block_rule = rule(report_block)

    # One row shorter than LOCATION_ROWS: the status line is above, not in it.
    fix_lbl = tk.Label(panel, text="", justify="left", anchor="nw",
                       width=READOUT_COLS, height=LOCATION_ROWS - 1,
                       font=("Consolas", 11), fg=ACCENT, bg=BG)
    fix_lbl.pack(fill="x", padx=10, pady=(0, 0))

    # Shown only while surveying; refresh packs and forgets it.
    survey_block = tk.Frame(panel, bg=BG)
    rule(survey_block)
    # Three labels so the directions can be bold and their own colours; the
    # rows add up to SURVEY_ROWS, so the block never changes height.
    survey_lbls = [tk.Label(survey_block, text="", justify="left", anchor="nw",
                            width=READOUT_COLS, height=rows,
                            font=("Consolas", 11), fg=ACCENT, bg=BG)
                   for rows in (1, 2, SURVEY_ROWS - 3)]
    for lbl in survey_lbls:
        lbl.pack(fill="x", padx=10)

    # Shown only while there is a target; refresh packs and forgets it.
    target_block = tk.Frame(panel, bg=BG)
    rule(target_block)
    target_row = tk.Frame(target_block, bg=BG)
    target_row.pack(fill="x", padx=10)
    target_lbl = tk.Label(target_row, text="", justify="left", anchor="nw",
                          width=TARGET_COLS, height=TARGET_ROWS,
                          font=("Consolas", 11), fg=ACCENT, bg=BG)
    target_lbl.pack(side="left")
    # FIELD, not BG, for the same reason as the pin: BG is click-through.
    compass = tk.Canvas(target_row, width=ARROW_PX, height=ARROW_PX, bg=FIELD,
                        highlightthickness=0)
    compass.pack(side="right", anchor="n")

    def draw_compass(arrow):
        """`arrow` is target_arrow's (turn, off, pitch), or None.

        A target ball, the way a game HUD marks something off-screen: the
        dot sits where the target is relative to your nose, right of centre
        means turn right, above means pitch up, and flying is "put the dot
        in the middle". The dot's distance from the centre is the angle off,
        90 degrees at the ring, so a target straight ahead is a dot in the
        crosshair. Past 90 degrees the target is behind you: the dot stays
        on the ring, hollow, and the ring itself turns red. A target on a
        body with only a heading to go on (no CamDir) is drawn on the
        horizontal line alone.
        """
        compass.delete("all")
        c, r = ARROW_PX / 2, ARROW_PX / 2 - 3
        turn, off, pitch = arrow if arrow is not None else (None, None, None)
        colour = arrow_colour(off)
        behind = off is not None and off > 90
        ring = STATUS_COLOURS["error"] if behind else "#243040"
        compass.create_oval(c - r, c - r, c + r, c + r, outline=ring,
                            width=2 if behind else 1)
        # The crosshair, with a gap in the middle so the dot reads cleanly.
        for a, b in ((c - r, c - 6), (c + 6, c + r)):
            compass.create_line(a, c, b, c, fill="#243040")
            compass.create_line(c, a, c, b, fill="#243040")
        compass.create_line(c, c - r - 2, c, c - r + 5, fill=DIM)
        if off is None:
            compass.create_oval(c - 3, c - 3, c + 3, c + 3, outline=colour)
            return
        # Place the dot: the angle off sets the distance from the centre,
        # the turn/pitch split sets the direction. A 30-degree error is a
        # third of the way out, which is what makes small corrections
        # readable; the old arrow swung its full travel for any error.
        yaw = turn or 0.0
        pt = pitch or 0.0
        ang = math.atan2(-pt, yaw) if (yaw or pt) else 0.0
        dist = r * min(1.0, off / 90.0)
        x, y = c + dist * math.cos(ang), c + dist * math.sin(ang)
        if off >= 2:
            compass.create_line(c, c, x, y, fill=colour, width=2)
        if behind:
            compass.create_oval(x - 5, y - 5, x + 5, y + 5, outline=colour,
                                width=2)
        else:
            compass.create_oval(x - 5, y - 5, x + 5, y + 5, fill=colour,
                                outline="")

    rule()

    # Only shown while a waypoint is being renamed (RENAME opens it, OK or
    # Enter accepts, X or Escape cancels). SAVE auto-names, like the pin.
    name_row = tk.Frame(panel, bg=BG)
    tk.Label(name_row, text="name", font=("Consolas", 10), fg=DIM,
             bg=BG).pack(side="left")
    name_var = tk.StringVar()
    name_entry = tk.Entry(name_row, textvariable=name_var, width=17,
                          font=("Consolas", 11), bg=FIELD, fg="#DCE6F0",
                          insertbackground="#DCE6F0", relief="flat")
    name_entry.pack(side="left", padx=6)
    ok_btn = button(name_row, "OK", lambda: fire("rename", do_rename)(),
                    width=3)
    ok_btn.pack(side="left")
    # Cancelling has to be possible with the mouse alone: Escape needs the
    # keyboard, and typing needs F held for a cursor.
    button(name_row, "X", lambda: end_rename(), width=2,
           bg="#6A7785").pack(side="left", padx=(6, 0))

    # Grouped by body, so the list is longer than the waypoint count and a
    # scrollbar is the only sign that there is more below the fold.
    # Column titles outside the list, so they stay put when it scrolls and
    # cannot be selected. The two spaces match the indent on every row, and
    # padx=11 is the list's 10 plus its 1 px border, so the text lines up.
    tk.Label(panel, text="  " + list_header(), font=("Consolas", 10), fg=DIM,
             bg=BG, anchor="w", padx=0, borderwidth=0).pack(
                 fill="x", padx=11, pady=(8, 0))
    list_frame = tk.Frame(panel, bg=BG)
    list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))
    listbox = tk.Listbox(list_frame, height=9, font=("Consolas", 10), bg=FIELD,
                         fg="#DCE6F0", selectbackground="#1d4f3a",
                         selectforeground="#FFFFFF", highlightthickness=0,
                         relief="flat", activestyle="none",
                         exportselection=False)
    scroll = tk.Scrollbar(list_frame, orient="vertical", command=listbox.yview,
                          width=10, relief="flat", borderwidth=0,
                          highlightthickness=0, troughcolor=FIELD,
                          bg="#2b3847", activebackground=DIM)
    listbox.config(yscrollcommand=scroll.set)
    listbox.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    # The body analysis takes the list's place on request, rather than adding a
    # second window over a game that is already fighting for the screen.
    report_box = tk.Text(report_block, height=REPORT_ROWS, width=READOUT_COLS,
                         font=("Consolas", 10), bg=FIELD, fg="#DCE6F0",
                         relief="flat", wrap="none", highlightthickness=0,
                         state="disabled", cursor="arrow")
    report_box.pack(fill="x", padx=10, pady=(0, 6), before=block_rule)

    # What the place is, which the measurement cannot say: a jump point, a
    # mining base and a Lagrange station all just sit still. Picking one
    # writes it straight into the stored record.
    # A pick from either dropdown activates the overlay; give_focus_to hands
    # the game back the focus it had when the menu opened.
    menu_from = {"hwnd": None}

    def menu_opened():
        menu_from["hwnd"] = foreground_handle()

    def picked(label, fn):
        fire(label, fn)()
        give_focus_to(menu_from["hwnd"])

    label_row = tk.Frame(report_block, bg=BG)
    tk.Label(label_row, text="label", font=("Consolas", 10), fg=DIM,
             bg=BG).pack(side="left")
    label_var = tk.StringVar(value=NO_LABEL)
    label_menu = tk.OptionMenu(
        label_row, label_var, *PLACE_LABELS,
        command=lambda v: picked("label", lambda st: do_label(st, v)))
    label_menu.config(font=("Consolas", 10), bg=FIELD, fg="#DCE6F0",
                      activebackground="#1d4f3a", activeforeground="#FFFFFF",
                      highlightthickness=0, relief="flat", width=18,
                      anchor="w")
    label_menu["menu"].config(font=("Consolas", 10), bg=FIELD, fg="#DCE6F0",
                              activebackground="#1d4f3a",
                              postcommand=menu_opened)
    label_menu.pack(side="left", padx=6)
    label_row.pack(fill="x", padx=10, pady=(0, 6), before=block_rule)

    # Which station this is, from UEX's list. The menu is rebuilt each time it
    # opens: the list arrives on another thread and depends on where you are.
    station_row = tk.Frame(report_block, bg=BG)
    tk.Label(station_row, text="station", font=("Consolas", 10), fg=DIM,
             bg=BG).pack(side="left")
    station_var = tk.StringVar(value=NO_STATION)
    station_btn = tk.Menubutton(
        station_row, textvariable=station_var, font=("Consolas", 10),
        bg=FIELD, fg="#DCE6F0", activebackground="#1d4f3a",
        activeforeground="#FFFFFF", highlightthickness=0, relief="flat",
        width=24, anchor="w", indicatoron=True)
    station_menu = tk.Menu(station_btn, tearoff=False, font=("Consolas", 10),
                           bg=FIELD, fg="#DCE6F0", activebackground="#1d4f3a")
    station_btn["menu"] = station_menu

    def fill_station_menu():
        menu_opened()
        station_menu.delete(0, "end")
        with state.lock:
            names = station_choices(state)
        if not names:
            station_menu.add_command(label="(no station list yet)",
                                     state="disabled")
            return
        station_menu.add_command(
            label=NO_STATION,
            command=lambda: picked("station", lambda st: (
                link_station(st, None), refresh_report())))
        for name in names:
            station_menu.add_command(
                label=name, command=lambda n=name: picked(
                    "station", lambda st: (link_station(st, n),
                                           refresh_report())))

    station_menu.config(postcommand=fill_station_menu)
    station_btn.pack(side="left", padx=6)
    station_row.pack(fill="x", padx=10, pady=(0, 6), before=block_rule)

    # Under the report of a fresh analysis. Either button returns to the list;
    # the result is already saved, so KEEP only closes the report.
    review_row = tk.Frame(report_block, bg=BG)
    tk.Label(review_row, text="new analysis", font=("Consolas", 10), fg=DIM,
             bg=BG).pack(side="left")
    button(review_row, "KEEP", lambda: fire("keep", lambda st: hide_report())(),
           width=7).pack(side="left", padx=(8, 0))
    button(review_row, "DISCARD", lambda: fire("discard", do_discard)(),
           width=9, bg="#FF9B9B").pack(side="left", padx=(6, 0))

    # Two rows, by what they act on: waypoints, then the body and the window.
    btn_row = tk.Frame(panel, bg=BG)
    btn_row.pack(fill="x", padx=10)
    rule()
    btn_row2 = tk.Frame(panel, bg=BG)
    btn_row2.pack(fill="x", padx=10, pady=(0, 8))

    shown = []            # (kind, name) per visible line; kind is row or head

    def selected_name():
        """The waypoint on the selected line, or None on a heading or nothing."""
        sel = listbox.curselection()
        if sel and sel[0] < len(shown) and shown[sel[0]][0] == "row":
            return shown[sel[0]][1]
        return None

    # --- panel actions ------------------------------------------------------
    # Each takes the state so it can go through on_hotkey, which logs the press
    # and reports an exception rather than letting Tk swallow it silently.

    def do_save_quick(st):
        """The pin's save. Deliberately ignores the name field.

        Star Citizen only shows a mouse cursor while F is held, so clicking the
        pin means holding F -- and the click moves focus here, which sends
        those held keystrokes straight into the name field. A quick save that
        read the field named a waypoint "fffffffff". The pin auto-names; the
        panel is where a name is chosen on purpose.
        """
        save_waypoint(st)
        refresh_list()

    editing = {"old": None}     # the waypoint whose name the field is editing

    def end_rename():
        name_row.pack_forget()
        editing["old"] = None
        name_var.set("")
        listbox.focus_set()
        set_no_activate(root, True)

    def do_start_rename(st):
        chosen = selected_name()
        if chosen is None:
            with st.lock:
                st.flash("select a waypoint first")
            return
        editing["old"] = chosen
        name_var.set(chosen)
        name_row.pack(fill="x", padx=10, pady=(0, 6), before=btn_row)
        # The only place anything is typed, so the only time the overlay
        # takes focus; end_rename gives the game its clicks back.
        set_no_activate(root, False)
        root.focus_force()
        name_entry.focus_set()
        name_entry.select_range(0, "end")
        name_entry.icursor("end")

    def do_rename(st):
        chosen, new = editing["old"], name_var.get().strip()
        with st.lock:
            if chosen is None:
                st.flash("select a waypoint first")
                return
            if not new:
                st.flash("type the new name first")
                return
            try:
                rename_waypoint(st.waypoints, chosen, new)
            except ValueError as exc:
                st.flash(str(exc))
                return
            if st.target == chosen:      # the target follows its waypoint
                st.target = new
            save_json(WAYPOINT_FILE, st.waypoints)
            st.flash(f"renamed to {new}")
        end_rename()
        refresh_list()

    def do_delete(st):
        chosen = selected_name()
        with st.lock:
            if chosen is None:
                st.flash("select a waypoint first")
                return
            try:
                delete_waypoint(st.waypoints, chosen)
            except ValueError as exc:
                st.flash(str(exc))
                return
            if st.target == chosen:
                st.target = None
            save_json(WAYPOINT_FILE, st.waypoints)
            st.flash(f"deleted {chosen}")
        refresh_list()

    def do_target(st):
        """Set the selected waypoint as the target, or clear it if it already is.

        A toggle rather than a second button: untargeting is the same thought
        as targeting, and the panel has enough controls. The label says which
        it will do, so nothing is hidden behind the gesture.
        """
        chosen = selected_name()
        with st.lock:
            if chosen is None:
                st.flash("select a waypoint first")
                return
            if st.target == chosen:
                st.target = None
                st.flash(f"target cleared")
            else:
                st.target = chosen
                st.flash(f"target: {chosen}")

    view = {"report": False, "was_analysing": False}

    def do_label(st, value):
        with st.lock:
            rec = current_place(st)[1]
            if rec is None:
                st.flash("analyse this place first")
                return
            rec["label"] = value
            save_json(BODIES_FILE, st.bodies)
            st.flash(f"labelled {value}")
        refresh_report()

    def refresh_report():
        with state.lock:
            rec = current_place(state)[1]
        label_var.set((rec or {}).get("label") or NO_LABEL)
        station_var.set(((rec or {}).get("station") or {}).get("name")
                        or NO_STATION)
        # A running analysis reports its progress here, not in the status row,
        # so the status stays put at the top.
        running = analysis_progress(state)
        text = (f"{running}{chr(10)}{ANALYSIS_ADVICE}" if running
                else chr(10).join(body_report(state)))
        if text == report_box.get("1.0", "end-1c"):
            return
        report_box.config(state="normal")
        report_box.delete("1.0", "end")
        report_box.insert("end", text)
        report_box.config(state="disabled")

    def show_report():
        if view["report"]:
            refresh_report()
            return
        report_block.pack(fill="x", after=top_rule)
        report_btn.config(text="HIDE")
        view["report"] = True
        refresh_report()

    def hide_report():
        if not view["report"]:
            return
        review_row.pack_forget()
        with state.lock:
            state.replaced = None       # leaving the report keeps the result
        report_block.pack_forget()
        report_btn.config(text="REPORT")
        view["report"] = False

    def do_report(st):
        hide_report() if view["report"] else show_report()

    def show_review():
        """A just-finished analysis: the report, with KEEP / DISCARD under it."""
        show_report()
        with state.lock:
            fresh_result = state.replaced is not None
        if fresh_result:
            review_row.pack(fill="x", padx=10, pady=(0, 6), before=block_rule)

    def do_discard(st):
        """Drop the result just taken, putting back the record it replaced."""
        with st.lock:
            if st.replaced is not None:
                planet, old = st.replaced
                if old is None:
                    st.bodies.pop(planet, None)
                else:
                    st.bodies[planet] = old
                save_json(BODIES_FILE, st.bodies)
                st.flash(f"{planet}: analysis discarded")
        hide_report()

    def do_analyse(st):
        """Start sampling the spin, or stop and keep what there is so far."""
        if st.analysis is None:
            start_analysis(st)
        else:
            finish_analysis(st, "stopped")
            show_review()

    def fire(label, fn):
        return lambda: on_hotkey(state, label, fn)()

    button(btn_row, "SAVE", fire("save", do_save_quick)).pack(side="left")
    # Wide enough for "UNTARGET" too, so swapping the label cannot resize it.
    target_btn = button(btn_row, "TARGET", fire("target", do_target), width=9)
    target_btn.pack(side="left", padx=(6, 0))
    button(btn_row, "RENAME", fire("rename", do_start_rename)).pack(
        side="left", padx=(6, 0))
    button(btn_row, "DELETE", fire("delete", do_delete),
           bg="#FF9B9B").pack(side="left", padx=(6, 0))
    # The body's own controls on the left, the window's on the right.
    analyse_btn = button(btn_row2, "ANALYSE", fire("analyse", do_analyse),
                         width=8)
    analyse_btn.pack(side="left")
    report_btn = button(btn_row2, "REPORT", fire("report", do_report), width=7)
    report_btn.pack(side="left", padx=(6, 0))
    # SURVEY asks which pattern to fly; END SURVEY just ends it.
    survey_menu = tk.Menu(root, tearoff=False, font=("Consolas", 10),
                          bg=FIELD, fg="#DCE6F0", activebackground="#1d4f3a")

    def fill_survey_menu():
        """The patterns, then the last square to resume, if there is one."""
        survey_menu.delete(0, "end")
        for pattern in survey.PATTERNS:
            survey_menu.add_command(
                label=pattern, command=lambda p=pattern: picked(
                    "survey", lambda st: start_survey(st, p)))
        survey_menu.add_separator()
        for kind, label in traces.KINDS.items():
            survey_menu.add_command(
                label=label, command=lambda k=kind: picked(
                    "trace", lambda st: start_trace(st, k)))
        with state.lock:
            plan = state.survey_saved
            if plan is None:
                return
            ahead = resume_leg(state, plan)
        size = f"{plan['spacing']:g} m square"
        survey_menu.add_separator()
        survey_menu.add_command(
            label=f"resume {size} at LEG {plan['leg'] + 1}",
            command=lambda: picked("survey", resume_survey))
        if ahead > plan["leg"]:
            survey_menu.add_command(
                label=f"resume {size}, skip ahead to LEG {ahead + 1}",
                command=lambda: picked(
                    "survey", lambda st: resume_survey(st, skip=True)))

    def on_survey():
        if state.trace is not None:
            fire("trace", stop_trace)()
            return
        if state.survey:
            fire("survey", stop_survey)()
            return
        fill_survey_menu()
        menu_opened()
        survey_menu.tk_popup(survey_btn.winfo_rootx(),
                             survey_btn.winfo_rooty()
                             + survey_btn.winfo_height())

    # Wide enough for "END SURVEY", so swapping the label cannot resize it.
    survey_btn = button(btn_row2, "SURVEY", on_survey, width=10)
    survey_btn.pack(side="left", padx=(6, 0))
    button(btn_row2, "QUIT", lambda: root.destroy(), width=5,
           bg="#6A7785").pack(side="right")
    button(btn_row2, "CLOSE", lambda: collapse()).pack(side="right",
                                                       padx=(0, 6))

    # --- collapse / expand --------------------------------------------------
    showing = {"panel": False}

    def expand():
        if showing["panel"]:
            return
        collapsed.pack_forget()
        panel.pack(fill="both", expand=True)
        showing["panel"] = True
        set_transparent(False)
        refresh_list()
        reposition()
        # No focus_force here: the panel is often left open while playing, and
        # taking focus would take it off the game. RENAME takes it when needed.
        listbox.focus_set()
        state.log("panel opened")

    def collapse():
        if not showing["panel"]:
            return
        panel.pack_forget()
        collapsed.pack()
        showing["panel"] = False
        # Anything typed by accident goes with it, and focus leaves the field
        # so a held key cannot keep filling it while the pin is all that shows.
        end_rename()
        set_transparent(True)
        reposition()
        state.log("panel closed")

    pin.bind("<ButtonPress-1>", lambda e: fire("save", do_save_quick)())
    pin.bind("<ButtonPress-3>", lambda e: expand())
    panel.bind("<ButtonPress-3>", lambda e: collapse())
    fix_lbl.bind("<ButtonPress-3>", lambda e: collapse())
    name_entry.bind("<Return>", lambda e: fire("rename", do_rename)())
    name_entry.bind("<Escape>", lambda e: end_rename())

    # Dragging moves the window by its readout, not by the pin: the pin's left
    # button is the save, so a drag started there would save before it moved.
    drag = {"x": 0, "y": 0}
    fix_lbl.bind("<ButtonPress-1>", lambda e: drag.update(x=e.x, y=e.y))

    def on_drag(event):
        nx = root.winfo_x() + event.x - drag["x"]
        ny = root.winfo_y() + event.y - drag["y"]
        root.geometry(f"+{nx}+{ny}")
        w, h = root.winfo_width(), root.winfo_height()
        dragged["corner"] = (nx + w if "r" in anchor else nx,
                             ny + h if "b" in anchor else ny)

    fix_lbl.bind("<B1-Motion>", on_drag)

    # Every click is logged, so one that lands beside a button is
    # distinguishable from one that never reached the window at all.
    # A click in a combobox's drop-down list arrives as a path string, not a
    # widget, so there is no class to ask for.
    root.bind_all("<Button-1>", lambda e: state.log(
        f"click at ({e.x},{e.y}) on "
        f"{getattr(e.widget, 'winfo_class', lambda: e.widget)()}"), add="+")

    def refresh_list():
        """Rebuild the list, keeping the selection on the same waypoint.

        Distances move every second, so the rows are rewritten rather than
        patched; restoring the selection by name is what stops that rewrite
        from stealing it out from under an edit.
        """
        chosen = selected_name()
        top = listbox.yview()[0]      # delete() resets the scroll to the top
        groups = waypoint_groups(state)
        shown[:] = []
        listbox.delete(0, "end")
        if not groups:
            # An empty box looks identical to a fault, so say which it is.
            # Recorded as a heading, so it can never be selected and acted on.
            shown.append(("head", None))
            listbox.insert("end", "  no waypoints saved yet")
        for system, body, rows in groups:
            shown.append(("head", None))
            listbox.insert("end", f"{system} / {body}")
            listbox.itemconfig("end", foreground=DIM)
            for row in rows:
                shown.append(("row", row[0]))
                listbox.insert("end", "  " + format_row(*row))
        if chosen is not None:
            for i, (kind, name) in enumerate(shown):
                if kind == "row" and name == chosen:
                    listbox.selection_set(i)
                    break
        listbox.yview_moveto(top)

    ticks = {"n": 0}
    last_size = {"wh": None}

    def reposition_if_resized():
        """Re-anchor whenever the content changes the window's size.

        Tk grows a window right and down from its top-left, so a readout that
        gets one line longer walks the panel off the right of the screen
        unless the anchored corner is reapplied.
        """
        root.update_idletasks()
        wh = (root.winfo_reqwidth(), root.winfo_reqheight())
        if wh != last_size["wh"]:
            last_size["wh"] = wh
            reposition()

    def refresh():
        if poll_analysis(state) and showing["panel"]:
            show_review()           # it finished by itself: show what it found
        state_health = health(state)
        colour = STATUS_COLOURS[state_health]
        draw_pin(colour)
        fresh = time.time() - state.note_time < 4
        if showing["panel"]:
            fix_lbl.config(text=render_location(state, with_status=False),
                           fg=colour)
            busy = state.survey or state.trace is not None
            if busy != bool(survey_block.winfo_manager()):
                if busy:
                    survey_block.pack(fill="x", after=fix_lbl)
                else:
                    survey_block.pack_forget()
            *texts, directions = survey_parts(state)
            for lbl, text in zip(survey_lbls, texts):
                lbl.config(text=text, fg=colour, font=("Consolas", 11))
            if directions:
                survey_lbls[1].config(fg=directions,
                                      font=("Consolas", 11, "bold"))
            if (state.target is not None) != bool(target_block.winfo_manager()):
                if state.target is not None:
                    target_block.pack(fill="x", after=fix_lbl)
                else:
                    target_block.pack_forget()
            target_lbl.config(text=render_target(state), fg=colour)
            draw_compass(target_arrow(state))
            chosen = selected_name()
            with state.lock:
                targeted = chosen is not None and chosen == state.target
            target_btn.config(text="UNTARGET" if targeted else "TARGET")
            analysing = analysis_progress(state)
            if analysing and not view["was_analysing"]:
                show_report()               # a run just began: open its block
            view["was_analysing"] = bool(analysing)
            analyse_btn.config(text="STOP" if analysing else "ANALYSE")
            tracing = state.trace       # once: END can clear it meanwhile
            survey_btn.config(
                text=("END LAKE" if tracing["kind"] == "lake" else "END TRACE")
                if tracing is not None
                else "END SURVEY" if state.survey else "SURVEY")
            # Toast: what a save or an edit just reported replaces the health
            # line until it fades.
            toast = f">> {state.note}" if fresh else ""
            health_lbl.config(text=toast or render_status(state), fg=colour)
            ticks["n"] += 1
            if ticks["n"] % 4 == 0:          # once a second, not every frame
                refresh_list()
                if view["report"]:
                    refresh_report()
        else:
            # Collapsed, this note is the only feedback a click produces.
            analysing = analysis_progress(state)
            note.config(text=analysing or (state.note if fresh else ""),
                        fg=colour if (fresh or analysing) else DIM)
        save_heightmap(state)
        save_traces(state)
        reposition_if_resized()
        if hud["on"]:               # hidden on purpose: keep_on_top shows it
            keep_on_top(root)
        root.after(250, refresh)

    hud = {"on": True}

    def set_hud(on):
        """Show or hide the overlay; the main window's HUD switch."""
        hud["on"] = on
        if on:
            root.deiconify()
        else:
            root.withdraw()
        state.log(f"HUD {'on' if on else 'off'}")

    collapsed.pack()
    set_transparent(True)
    set_no_activate(root, True)
    reposition()
    refresh()
    import main_window
    # The module itself is handed over, not imported there: a second import of
    # this file would carry its own file paths and its own copy of the data.
    main_window.open_main(root, state, sys.modules[__name__], set_hud)
    root.mainloop()


def run_console(state):
    try:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            print(render(state))
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------


def cmd_run(args):
    if not args.allow_multiple and not claim_single_instance():
        sys.exit(
            "sc_locator is already running.\n"
            "Both overlays draw in the same corner, so the readout ghosts.\n"
            "Close the other one first, or pass --allow-multiple."
        )
    cfg = load_config()
    state = State(cfg, load_waypoints())
    state.bodies = load_bodies()
    state.heightmap = load_heightmap()
    state.traces = load_traces()
    state.survey_saved = load_json(SURVEY_FILE, None)
    state.log = make_logger(args.log)
    stop = threading.Event()
    threading.Thread(target=ocr_worker, args=(state, cfg, stop), daemon=True).start()
    threading.Thread(target=load_stations, args=(state,), daemon=True).start()

    if args.log:
        print(f"logging to {args.log}")
    try:
        if args.console:
            run_console(state)
        else:
            run_overlay(state, cfg)
    finally:
        stop.set()
        save_heightmap(state, force=True)
        save_traces(state, force=True)


def cmd_calibrate(args):
    cfg = load_config()
    if args.region:
        cfg["region"] = args.region
    if args.cut is not None:
        cfg["contrast_cut"] = args.cut
    import pytesseract

    print(f"tesseract: {setup_tesseract(cfg)}")
    if args.delay:
        print("switch to the game - capturing in %ds" % args.delay, flush=True)
        time.sleep(args.delay)
    if args.find:
        with open_capture() as sct:
            found = find_region(sct, cfg)
        if found is None:
            sys.exit("could not find the HUD rows on screen - is 'r_DisplayInfo 2' on?")
        cfg["region"] = [round(v, 4) for v in found]
        area = (found[2] - found[0]) * (found[3] - found[1])
        print(f"found region: {cfg['region']}  ({area * 100:.1f}% of the screen)")
    with open_capture() as sct:
        raw = grab_region(sct, cfg)
    processed = preprocess(raw, cfg)
    raw.save(HERE / "calib_raw.png")
    processed.save(HERE / "calib_processed.png")
    text = pytesseract.image_to_string(processed, config=TESS_CONFIG)
    print("---- OCR text ----")
    print(text)
    print("---- parsed ----")
    res = parse_readout(text, cfg["default_radius"])
    if res is None:
        print("No 'CamPos Planet Zone' row found. Check calib_raw.png /")
        print("calib_processed.png, then adjust --region and --cut.")
        print("The debug HUD needs 'r_DisplayInfo 2' in the console (~).")
    else:
        planet, radius, pos, radius_known = res
        lat, lon, r = to_latlon(*pos)
        src = "HUD" if radius_known else "fallback"
        print(f"planet={planet} radius={radius:,.0f} ({src}) pos={pos}")
        print(f"lat={lat:+.4f} lon={lon:+.4f} height={r - radius:,.1f} m")
        if not plausible(r, radius, radius_known):
            print()
            print(f"WARNING: {r:,.0f} m from the centre is {r / radius:.2f}x the")
            print("radius, so 'run' would discard this reading. Either a")
            print("coordinate was misread - check the CamPos row above for")
            print("garbled digits or a clipped right-hand edge - or you are not")
            print("near a planet surface.")
    if args.save:
        save_json(CONFIG_FILE, cfg)
        print("saved region/cut to locator_config.json")


def describe_height(w):
    """How high this waypoint is, in terms a player can use.

    Height over the reference sphere is not one: a hangar standing on the
    ground at microTech reads 1,771 m. Where the terrain height was recorded,
    report the ground's own elevation and how far above it the waypoint sits.

    Height above the surface is reported plainly and not flagged. Stations in
    low orbit, asteroid bases and other structures are ordinary places to mark
    -- Port Thresler sits 124 km up -- so a large altitude says what kind of
    place it is, not that the reading went wrong.

    Waypoints saved before the terrain values were stored fall back to the old
    figure, labelled so it is not mistaken for a height above ground.
    """
    if w.get("frame") == "system":
        return "deep space"
    if "elev" not in w:
        _, _, r = to_latlon(w["x"], w["y"], w["z"])
        # Through fmt_dist like every other distance here: these run to Gm
        # once a waypoint is out at a Lagrange station, and 61,910,589 m is
        # not a figure anyone reads.
        # Under 10 km it stays in metres: ground waypoints sit here, and 1.77
        # km throws away the resolution that 1,771 m had.
        over = r - w.get("radius", 1e6)
        shown = f"{over:,.0f} m" if abs(over) < 10_000 else fmt_dist(over)
        return f"sphere {shown:>11}"
    agl = w.get("agl") or 0.0
    if agl < 25.0:
        return f"ground {w['elev']:>9,.0f} m  on the surface"
    return f"ground {w['elev']:>9,.0f} m  {fmt_dist(agl):>10} up"


def cmd_list(_args):
    wps = load_waypoints()
    if not wps:
        print("no waypoints yet")
    for w in sorted(wps, key=lambda q: (q.get("system", "unknown"),
                                        q.get("planet", ""), q["name"])):
        if w.get("frame") == "system":
            # No latitude, longitude or orbital marker out there to print.
            where = "  ".join(f"{v / 1000:,.1f} km" for v in
                              (w["x"], w["y"], w["z"]))
            print(f"{w['name']:<16} {w.get('system', 'unknown'):<9} "
                  f"{w['planet']:<10} {where:<42} "
                  f"{describe_height(w):<14} {w.get('saved', '')}")
            continue
        lat, lon, _ = to_latlon(w["x"], w["y"], w["z"])
        om, _, _ = nearest_om(lat, lon)
        print(f"{w['name']:<16} {w.get('system', 'unknown'):<9} {w['planet']:<10} "
              f"{om:<6} lat {lat:+8.3f} lon {lon:+8.3f} "
              f"{describe_height(w):<32} {w.get('saved', '')}")


def cmd_rename(args):
    wps = load_waypoints()
    try:
        rename_waypoint(wps, args.old, args.new)
    except ValueError as exc:
        sys.exit(str(exc))
    save_json(WAYPOINT_FILE, wps)
    print(f"renamed {args.old} -> {args.new}")


def cmd_delete(args):
    wps = load_waypoints()
    try:
        delete_waypoint(wps, args.name)
    except ValueError as exc:
        sys.exit(str(exc))
    save_json(WAYPOINT_FILE, wps)
    print(f"deleted {args.name}")


def cmd_convert(args):
    lat, lon, r = to_latlon(args.x, args.y, args.z)
    print(f"lat {lat:+.4f}  lon {lon:+.4f}  distance from centre {r:,.1f} m  "
          f"height over {args.radius:,.0f} m sphere {r - args.radius:,.1f} m")


def main():
    ap = argparse.ArgumentParser(description="Star Citizen coordinate locator")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run")
    p.add_argument("--console", action="store_true", help="text output instead of overlay")
    p.add_argument("--log", metavar="FILE", help="write a diagnostic log")
    p.add_argument("--allow-multiple", action="store_true",
                   help="start even if another overlay is already running")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("calibrate")
    p.add_argument("--region", nargs=4, type=float, metavar=("L", "T", "R", "B"))
    p.add_argument("--cut", type=int,
                   help="local-contrast cut-off; see preprocess")
    p.add_argument("--find", action="store_true",
                   help="OCR the whole screen to locate the HUD rows")
    p.add_argument("--delay", type=int, default=0, metavar="SECONDS",
                   help="wait before capturing, to switch to the game first")
    p.add_argument("--save", action="store_true")
    p.set_defaults(fn=cmd_calibrate)

    sub.add_parser("list").set_defaults(fn=cmd_list)

    p = sub.add_parser("rename")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(fn=cmd_rename)

    p = sub.add_parser("delete")
    p.add_argument("name")
    p.set_defaults(fn=cmd_delete)

    p = sub.add_parser("convert")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.add_argument("z", type=float)
    p.add_argument("--radius", type=float, default=1000000.0)
    p.set_defaults(fn=cmd_convert)

    # No arguments, as from a double-click, means run the overlay.
    args = ap.parse_args(sys.argv[1:] or ["run"])
    args.fn(args)


if __name__ == "__main__":
    main()
