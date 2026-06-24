"""FH6 "Data Out" UDP packet parser.

Corroboration, kept separate for the two distinct claims:
  * OFFSET LAYOUT (PositionX@244, Speed@256, TireTemp@268, base 323 = FM7 dash 311
    + 12-byte Horizon insert): official FH6 doc; github.com/TheBanHammer/fh6-tel
    parser.rs; richstokes/Forza-data-tools FH4_packetformat.dat (the Horizon line
    has carried a 12-byte insert after NumCylinders since FH4 - CarCategory(4) +
    HorizonUnknown1(4) + HorizonUnknown2(4) - so FH4's downstream offsets match
    FH6's field-for-field); and a first-principles walk (below).
  * FIELD SEMANTICS of bytes 232-243 (that they are CarGroup/SmashableVelDiff/
    SmashableMass, not FH4's CarCategory/unknowns): the official FH6 doc and
    fh6-tel only - those FH6 names are absent from the FH4 file.

Layout summary (little-endian):

  * Bytes 0..231   : the classic "Sled" block, unchanged from FM7/FH4/FH5.
  * Bytes 232..243 : THREE FH6-only fields inserted after NumCylinders -
                     CarGroup (s32), SmashableVelDiff (f32), SmashableMass (f32).
                     This 12-byte insert is the #1 thing that breaks naive FH5
                     parsers: everything Dash-side is shifted +12 vs FH5.
  * Bytes 244..322 : the "Dash" extension (Position, Speed, Power, TireTemp...).
  * Bytes 323..338 : OPTIONAL Horizon tyre-wear block (4x f32), may be absent.

Two facts the original brief got wrong, corrected here from the real parser:
  * Tyre temps are sent in FAHRENHEIT - we convert to Celsius so the analyzer's
    Celsius thresholds are valid.
  * The base packet is 323 bytes, not "~324".
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, asdict, field
from typing import Optional

# --- packet sizes -----------------------------------------------------------
# The FIXED field block every analyzer value lives in is bytes 0..322 (323 bytes).
# Trailing bytes after that are OPTIONAL and version-dependent:
#   +0  -> 323: older/truncated stream (ends before HorizonTrailingUnknown)
#   +1  -> 324: NORMAL live FH6 packet (one HorizonTrailingUnknown byte at 323)
#   +16 -> 339: a 16-byte tyre-wear block (4x f32) is present at offset 323
# We never reject on length >= 323; trailing bytes are interpreted by how many
# there are, and are never required to parse the core packet.
SLED_LEN = 232
FH6_BASE_LEN = 323          # fixed field block (Sled + 12-byte FH6 insert + Dash)
FH6_NORMAL_LEN = 324        # the real live FH6 size: base + 1 trailing byte
FH6_WITH_WEAR_LEN = 339     # base + 4x f32 tyre wear
WEAR_BLOCK_LEN = 16

# Each entry: (attr_name, struct_format, absolute_byte_offset)
# struct formats: i=s32, I=u32, f=f32, H=u16, B=u8, b=s8
_FIELDS = [
    ("is_race_on",                "i", 0),
    ("timestamp_ms",              "I", 4),
    ("engine_max_rpm",            "f", 8),
    ("engine_idle_rpm",           "f", 12),
    ("current_engine_rpm",        "f", 16),
    ("accel_x",                   "f", 20),   # lateral-ish in car frame
    ("accel_y",                   "f", 24),
    ("accel_z",                   "f", 28),
    ("vel_x",                     "f", 32),
    ("vel_y",                     "f", 36),
    ("vel_z",                     "f", 40),
    ("ang_vel_x",                 "f", 44),
    ("ang_vel_y",                 "f", 48),
    ("ang_vel_z",                 "f", 52),
    ("yaw",                       "f", 56),
    ("pitch",                     "f", 60),
    ("roll",                      "f", 64),
    # NormalizedSuspensionTravel: 0..1, near 0 = bottoming, near 1 = topping
    ("susp_norm_fl",              "f", 68),
    ("susp_norm_fr",              "f", 72),
    ("susp_norm_rl",              "f", 76),
    ("susp_norm_rr",              "f", 80),
    ("tire_slip_ratio_fl",        "f", 84),
    ("tire_slip_ratio_fr",        "f", 88),
    ("tire_slip_ratio_rl",        "f", 92),
    ("tire_slip_ratio_rr",        "f", 96),
    # WheelRotationSpeed (rad/s) per corner: radius-free LOCKUP signal - a wheel at
    # ~0 rad/s while the car is moving is locked, even when slip-ratio is noisy.
    ("wheel_rot_fl",              "f", 100),
    ("wheel_rot_fr",              "f", 104),
    ("wheel_rot_rl",              "f", 108),
    ("wheel_rot_rr",              "f", 112),
    # 116..131 WheelOnRumbleStrip x4   (skipped)
    # 132..147 WheelInPuddleDepth x4   (skipped)
    # 148..163 SurfaceRumble x4        (skipped)
    ("tire_slip_angle_fl",        "f", 164),
    ("tire_slip_angle_fr",        "f", 168),
    ("tire_slip_angle_rl",        "f", 172),
    ("tire_slip_angle_rr",        "f", 176),
    ("tire_combined_slip_fl",     "f", 180),
    ("tire_combined_slip_fr",     "f", 184),
    ("tire_combined_slip_rl",     "f", 188),
    ("tire_combined_slip_rr",     "f", 192),
    ("susp_travel_m_fl",          "f", 196),
    ("susp_travel_m_fr",          "f", 200),
    ("susp_travel_m_rl",          "f", 204),
    ("susp_travel_m_rr",          "f", 208),
    ("car_ordinal",               "i", 212),
    ("car_class",                 "i", 216),
    ("car_pi",                    "i", 220),
    ("drivetrain_type",           "i", 224),
    ("num_cylinders",             "i", 228),
    # --- FH6-only insert (232..243) ---
    ("car_group",                 "i", 232),
    ("smashable_vel_diff",        "f", 236),
    ("smashable_mass",            "f", 240),
    # --- Dash extension (FH6 offsets) ---
    ("position_x",                "f", 244),
    ("position_y",                "f", 248),
    ("position_z",                "f", 252),
    ("speed",                     "f", 256),   # m/s
    ("power",                     "f", 260),   # W
    ("torque",                    "f", 264),   # Nm
    ("tire_temp_fl_f",            "f", 268),   # FAHRENHEIT (converted below)
    ("tire_temp_fr_f",            "f", 272),
    ("tire_temp_rl_f",            "f", 276),
    ("tire_temp_rr_f",            "f", 280),
    ("boost",                     "f", 284),
    ("fuel",                      "f", 288),
    ("distance_traveled",         "f", 292),
    ("best_lap",                  "f", 296),
    ("last_lap",                  "f", 300),
    ("current_lap",               "f", 304),
    ("current_race_time",         "f", 308),
    ("lap_number",                "H", 312),
    ("race_position",             "B", 314),
    ("accel",                     "B", 315),   # throttle 0..255
    ("brake",                     "B", 316),   # 0..255
    ("clutch",                    "B", 317),
    ("handbrake",                 "B", 318),
    ("gear",                      "B", 319),
    ("steer",                     "b", 320),   # -127..127
    ("normalized_driving_line",   "b", 321),
    ("normalized_ai_brake_diff",  "b", 322),
]

# Pre-build per-field struct objects (little-endian) for speed.
_COMPILED = [(name, struct.Struct("<" + fmt), off) for name, fmt, off in _FIELDS]

# Optional trailing tyre-wear block.
_WEAR = struct.Struct("<4f")
_WEAR_OFFSET = 323


@dataclass
class Packet:
    """One decoded FH6 Data Out frame. Temps already converted to Celsius."""
    is_race_on: int = 0
    timestamp_ms: int = 0
    engine_max_rpm: float = 0.0
    engine_idle_rpm: float = 0.0
    current_engine_rpm: float = 0.0
    accel_x: float = 0.0
    accel_y: float = 0.0
    accel_z: float = 0.0
    vel_x: float = 0.0
    vel_y: float = 0.0
    vel_z: float = 0.0
    ang_vel_x: float = 0.0
    ang_vel_y: float = 0.0
    ang_vel_z: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    susp_norm_fl: float = 0.0
    susp_norm_fr: float = 0.0
    susp_norm_rl: float = 0.0
    susp_norm_rr: float = 0.0
    tire_slip_ratio_fl: float = 0.0
    tire_slip_ratio_fr: float = 0.0
    tire_slip_ratio_rl: float = 0.0
    tire_slip_ratio_rr: float = 0.0
    wheel_rot_fl: float = 0.0
    wheel_rot_fr: float = 0.0
    wheel_rot_rl: float = 0.0
    wheel_rot_rr: float = 0.0
    tire_slip_angle_fl: float = 0.0
    tire_slip_angle_fr: float = 0.0
    tire_slip_angle_rl: float = 0.0
    tire_slip_angle_rr: float = 0.0
    tire_combined_slip_fl: float = 0.0
    tire_combined_slip_fr: float = 0.0
    tire_combined_slip_rl: float = 0.0
    tire_combined_slip_rr: float = 0.0
    susp_travel_m_fl: float = 0.0
    susp_travel_m_fr: float = 0.0
    susp_travel_m_rl: float = 0.0
    susp_travel_m_rr: float = 0.0
    car_ordinal: int = 0
    car_class: int = 0
    car_pi: int = 0
    drivetrain_type: int = 0       # 0=FWD, 1=RWD, 2=AWD
    num_cylinders: int = 0
    car_group: int = 0
    smashable_vel_diff: float = 0.0
    smashable_mass: float = 0.0
    position_x: float = 0.0
    position_y: float = 0.0
    position_z: float = 0.0
    speed: float = 0.0
    power: float = 0.0
    torque: float = 0.0
    tire_temp_fl_f: float = 0.0
    tire_temp_fr_f: float = 0.0
    tire_temp_rl_f: float = 0.0
    tire_temp_rr_f: float = 0.0
    boost: float = 0.0
    fuel: float = 0.0
    distance_traveled: float = 0.0
    best_lap: float = 0.0
    last_lap: float = 0.0
    current_lap: float = 0.0
    current_race_time: float = 0.0
    lap_number: int = 0
    race_position: int = 0
    accel: int = 0
    brake: int = 0
    clutch: int = 0
    handbrake: int = 0
    gear: int = 0
    steer: int = 0
    normalized_driving_line: int = 0
    normalized_ai_brake_diff: int = 0
    # Celsius tyre temps (derived in __post_init__)
    tire_temp_fl: float = 0.0
    tire_temp_fr: float = 0.0
    tire_temp_rl: float = 0.0
    tire_temp_rr: float = 0.0
    # Optional tyre wear (None if not present in packet)
    tire_wear_fl: Optional[float] = None
    tire_wear_fr: Optional[float] = None
    tire_wear_rl: Optional[float] = None
    tire_wear_rr: Optional[float] = None
    # observed datagram length (324 = normal live FH6)
    packet_len: int = 0

    def __post_init__(self) -> None:
        self.tire_temp_fl = _f_to_c(self.tire_temp_fl_f)
        self.tire_temp_fr = _f_to_c(self.tire_temp_fr_f)
        self.tire_temp_rl = _f_to_c(self.tire_temp_rl_f)
        self.tire_temp_rr = _f_to_c(self.tire_temp_rr_f)

    # convenience views ------------------------------------------------------
    @property
    def speed_mph(self) -> float:
        return self.speed * 2.236936

    @property
    def speed_kmh(self) -> float:
        return self.speed * 3.6

    @property
    def rpm_fraction(self) -> float:
        return self.current_engine_rpm / self.engine_max_rpm if self.engine_max_rpm else 0.0

    @property
    def lateral_g(self) -> float:
        """Lateral cornering load in g (accel_x is the car-frame lateral axis)."""
        return self.accel_x / 9.80665

    @property
    def drivetrain_name(self) -> str:
        return {0: "FWD", 1: "RWD", 2: "AWD"}.get(self.drivetrain_type, "?")

    def as_dict(self) -> dict:
        return asdict(self)


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


class ParseError(ValueError):
    pass


def parse(buf: bytes) -> Packet:
    """Decode a raw FH6 Data Out datagram into a :class:`Packet`.

    Raises :class:`ParseError` if the buffer is too short for the FH6 layout.
    """
    n = len(buf)
    if n < FH6_BASE_LEN:
        raise ParseError(
            f"packet too short: {n} bytes (need >= {FH6_BASE_LEN}). The live FH6 "
            f"packet is normally {FH6_NORMAL_LEN} bytes (base {FH6_BASE_LEN} + 1 "
            "trailing byte); a shorter datagram is truncated or a different format."
        )
    # NOTE: FH6 base 323 = FM7 dash 311 + the 12-byte Horizon insert. The real
    # live stream is 324 (one HorizonTrailingUnknown byte). Every analyzer field
    # is inside bytes 0..322, so we parse that fixed block and treat ALL trailing
    # bytes as optional - length >= 323 is accepted, never rejected.
    values = {}
    for name, s, off in _COMPILED:
        (values[name],) = s.unpack_from(buf, off)

    pkt = Packet(**values)
    pkt.packet_len = n

    # Interpret the optional trailing region by its size.
    trailing = n - FH6_BASE_LEN
    if trailing >= WEAR_BLOCK_LEN:
        # 16-byte tyre-wear block (4x f32) at offset 323.
        fl, fr, rl, rr = _WEAR.unpack_from(buf, _WEAR_OFFSET)
        pkt.tire_wear_fl, pkt.tire_wear_fr = fl, fr
        pkt.tire_wear_rl, pkt.tire_wear_rr = rl, rr
    # trailing in 1..15 (incl. the normal 324 case) = HorizonTrailingUnknown /
    # padding: ignored. No analyzer field depends on it.

    return pkt
