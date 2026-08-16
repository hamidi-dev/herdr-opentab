"""Plugin settings, paths, and the environment herdr hands a plugin command.

Nothing here raises on bad input: a typo in config.json must degrade to the
default it replaced, not take down a daemon whose whole job is a sidebar cell.
Every rejected value is reported once through `Config.warnings` so `doctor` can
show it without the daemon having to log on every tick.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from typing import Any

# The report-metadata `--source` this plugin owns. Herdr keys sequencing and
# ownership on it, so it must stay stable across versions.
SOURCE = "opentab"

DEFAULTS: dict[str, Any] = {
    # Sidebar token name: `$cost` in ui.sidebar.agents.rows. Renameable so this
    # can live beside another cost plugin that already claimed the name.
    "token": "cost",
    # Seconds between rounds while any agent is working. opentab reads
    # transcripts off disk; 10s is well under the pace a price actually moves.
    "interval_secs": 10,
    # Rounds while every agent sits idle/blocked/done. A blocked agent burns
    # nothing, so polling it at working speed is pure waste.
    "idle_interval_secs": 60,
    # "project" prices a pane herdr reports no session id for by its directory:
    # that project's most recently active session. Safe because `core.plan`
    # withholds the value from every pane of a project that has more than one
    # agent -- the case where the number would belong to somebody else. "off"
    # leaves every session-less pane blank instead.
    "fallback": "project",
    # Restrict to certain herdr agent labels ("claude", "codex", ...), or null
    # for every agent herdr reports.
    "agents": None,
    "opentab_bin": "opentab",
    # Extra argv for `opentab cost`, e.g. ["--source", "claude"].
    "opentab_args": [],
    # How long a reported price stays valid. Null means "derive one from the
    # interval": the daemon then renews every price it still stands behind, so a
    # daemon that is killed, disabled, or wedged takes its prices down with it
    # instead of leaving plausible-looking numbers in a sidebar nobody is
    # updating. Herdr keeps a token in the *server*, which outlives us. Set 0 to
    # keep the last value until something replaces it.
    "ttl_ms": None,
    # Drop opentab's leading "~" (it marks a price as estimated).
    "strip_approx": False,
    # Pad prices to a common width so their digits line up. Only visible when
    # the price starts its sidebar row; set false if you put it last.
    "align": True,
}

_MIN_INTERVAL = 3
# Herdr's own ceiling for a metadata ttl (`METADATA_TTL_MAX_MS`); it rejects
# anything larger, and a rejected report means no price at all.
MAX_TTL_MS = 86_400_000
# Eight hours: the largest interval whose derived lease (three rounds) still
# fits under that ceiling. A longer one would be capped to less than a single
# round, so every price would lapse before the round that renews it -- and an
# interval that overflows an int (JSON has no limit) would sleep past the heat
# death of the universe.
_MAX_INTERVAL = MAX_TTL_MS // 3000


class Config:
    def __init__(self, values: dict[str, Any], warnings: list[str]) -> None:
        self.token: str = values["token"]
        self.interval_secs: int = values["interval_secs"]
        self.idle_interval_secs: int = values["idle_interval_secs"]
        self.fallback: str = values["fallback"]
        self.agents: list[str] | None = values["agents"]
        self.opentab_bin: str = values["opentab_bin"]
        self.opentab_args: list[str] = values["opentab_args"]
        self.ttl_ms: int | None = values["ttl_ms"]
        self.strip_approx: bool = values["strip_approx"]
        self.align: bool = values["align"]
        self.warnings = warnings

    @property
    def project_fallback(self) -> bool:
        return self.fallback == "project"

    @property
    def lease_ms(self) -> int | None:
        """How long a reported price stays valid; None for "until replaced".

        Long enough that an ordinary round renews it well before it lapses, and
        short enough that a sidebar stops showing prices soon after whatever was
        maintaining them stopped. Capped at herdr's own maximum: a longer one is
        rejected outright (`METADATA_TTL_MAX_MS`, `app/api_helpers.rs`), which
        would leave the sidebar with no price at all rather than a stale one.
        """
        if self.ttl_ms == 0:
            return None
        if self.ttl_ms is not None:
            return min(self.ttl_ms, MAX_TTL_MS)
        return min(MAX_TTL_MS, max(30_000, self.idle_interval_secs * 3 * 1000))

    @property
    def renew_after_secs(self) -> float:
        """Re-report an unchanged price this old, so its lease never lapses."""
        lease = self.lease_ms
        return float("inf") if lease is None else lease / 1000 / 3

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in DEFAULTS}


def _coerce(values: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """Merge `values` over DEFAULTS, dropping anything the daemon can't use."""
    merged = dict(DEFAULTS)
    for key, value in values.items():
        if key not in DEFAULTS:
            warnings.append(f"unknown config key {key!r}")
            continue
        merged[key] = value

    token = merged["token"]
    # Herdr's own rule for a metadata token name: 1-32 ASCII letters, digits,
    # underscores, or hyphens. A rejected name would make every report fail.
    if (
        not isinstance(token, str)
        or not 1 <= len(token) <= 32
        or not all(c.isascii() and (c.isalnum() or c in "_-") for c in token)
    ):
        warnings.append(f"invalid token {token!r}; using {DEFAULTS['token']!r}")
        merged["token"] = DEFAULTS["token"]

    for key in ("interval_secs", "idle_interval_secs"):
        value = merged[key]
        # A float is checked for finiteness because JSON has no infinity but
        # `1e999` parses as one, and int(inf) raises. An int is never infinite --
        # and must not be handed to math.isfinite, which converts to float and
        # raises OverflowError on a 400-digit number. This module is not allowed
        # to raise, whatever is in the file.
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or (isinstance(value, float) and not math.isfinite(value))
            or not _MIN_INTERVAL <= value <= _MAX_INTERVAL
        ):
            warnings.append(f"invalid {key} {value!r}; using {DEFAULTS[key]}")
            merged[key] = DEFAULTS[key]
        else:
            merged[key] = int(value)
    if merged["idle_interval_secs"] < merged["interval_secs"]:
        merged["idle_interval_secs"] = merged["interval_secs"]

    if merged["fallback"] not in ("project", "off"):
        warnings.append(f"invalid fallback {merged['fallback']!r}; using {DEFAULTS['fallback']!r}")
        merged["fallback"] = DEFAULTS["fallback"]

    agents = merged["agents"]
    if agents is not None:
        if isinstance(agents, list) and all(isinstance(a, str) for a in agents):
            merged["agents"] = [a.strip() for a in agents if a.strip()]
        else:
            warnings.append("invalid agents; pricing every agent")
            merged["agents"] = None

    if not isinstance(merged["opentab_bin"], str) or not merged["opentab_bin"].strip():
        warnings.append("invalid opentab_bin; using 'opentab'")
        merged["opentab_bin"] = DEFAULTS["opentab_bin"]

    args = merged["opentab_args"]
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        warnings.append("invalid opentab_args; ignoring")
        merged["opentab_args"] = []

    ttl = merged["ttl_ms"]
    # Herdr accepts 1..86_400_000; 0 is our own "no lease at all", and null means
    # "derive one from the interval" (see Config.lease_ms).
    if ttl is not None and (
        not isinstance(ttl, int) or isinstance(ttl, bool) or not 0 <= ttl <= 86_400_000
    ):
        warnings.append(f"invalid ttl_ms {ttl!r}; deriving one from the interval")
        merged["ttl_ms"] = None
    ttl = merged["ttl_ms"]
    if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl > 0:
        # A lease can only be renewed when a round runs, so one shorter than two
        # rounds lapses on screen: the price blanks and reappears every cycle.
        # The user asked for this number explicitly, so it is honoured -- but
        # `doctor` should be able to say that is where the flicker comes from.
        rounds_ms = merged["idle_interval_secs"] * 2000
        if ttl < rounds_ms:
            warnings.append(
                f"ttl_ms {ttl} is shorter than two idle rounds ({rounds_ms}ms); "
                "prices will blank between updates"
            )

    # Not bool(): "false" and 0 are the shapes a user actually types, and
    # silently reading them as true/false is worse than saying so.
    for key in ("strip_approx", "align"):
        if not isinstance(merged[key], bool):
            warnings.append(f"invalid {key} {merged[key]!r}; using {DEFAULTS[key]}")
            merged[key] = DEFAULTS[key]
    return merged


def config_dir() -> str:
    """Where user-editable settings live. Herdr creates this directory."""
    return os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "herdr", "plugins", SOURCE
    )


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def state_dir() -> str:
    """Runtime state: the daemon lock, its wake file, and its log.

    Scoped to one herdr *server*, not one plugin. Herdr hands every plugin the
    same HERDR_PLUGIN_STATE_DIR (`plugin_paths.rs`: state_dir/plugins/<id>) but
    gives each named session its own socket (`session.rs`:
    api_socket_path_for), so a single lock under that directory would let the
    first server's daemon lock every other session out of its own prices.

    Never HERDR_PLUGIN_ROOT — a GitHub-installed root is a managed checkout that
    reinstalling replaces.
    """
    override = os.environ.get("HERDR_OPENTAB_STATE_DIR")
    if override:
        return override
    from_herdr = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    base = from_herdr or os.path.join(tempfile.gettempdir(), f"herdr-opentab-{os.getuid()}")
    return os.path.join(base, server_key())


def server_key() -> str:
    """A short stable name for the herdr server this process talks to."""
    socket_path = os.environ.get("HERDR_SOCKET_PATH") or os.environ.get("HERDR_SESSION") or ""
    if not socket_path:
        return "default"
    return hashlib.sha256(socket_path.encode("utf-8")).hexdigest()[:12]


def load() -> Config:
    """Read config.json, falling back to defaults for anything unusable."""
    warnings: list[str] = []
    values: dict[str, Any] = {}
    path = config_path()
    try:
        with open(path, encoding="utf-8") as handle:
            parsed = json.load(handle)
        if isinstance(parsed, dict):
            values = parsed
        else:
            warnings.append(f"{path}: expected a JSON object")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        warnings.append(f"{path}: {error}")

    merged = _coerce(values, warnings)

    # Env overrides exist for debugging a live daemon without editing the file
    # herdr may reload from underneath it.
    interval = os.environ.get("HERDR_OPENTAB_INTERVAL")
    if interval:
        try:
            merged["interval_secs"] = max(_MIN_INTERVAL, int(interval))
            merged["idle_interval_secs"] = max(
                merged["interval_secs"], merged["idle_interval_secs"]
            )
        except ValueError:
            warnings.append(f"HERDR_OPENTAB_INTERVAL={interval!r} is not a number")
    bin_override = os.environ.get("HERDR_OPENTAB_BIN")
    if bin_override:
        merged["opentab_bin"] = bin_override

    return Config(merged, warnings)


def herdr_bin() -> str:
    """The running herdr binary. Portable across unix sockets and named pipes."""
    return os.environ.get("HERDR_BIN_PATH") or "herdr"
