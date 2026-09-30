#!/usr/bin/python3
"""Conservatively reclaim clean Linux page cache during sustained idle periods."""

import argparse
from dataclasses import dataclass
import math
import os
from pathlib import Path
import sys
import time


class ReclaimError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    min_cache_mb: float = 1024
    max_cpu_pct: float = 15
    max_core_cpu_pct: float = 20
    max_load_avg: float = 0.5
    sample_sec: float = 10
    dry_run: bool = False

    @classmethod
    def from_env(cls, env, dry_run=False):
        values = {}
        for name, field, default, upper in (
            ("MIN_CACHE_MB", "min_cache_mb", 1024, None),
            ("MAX_CPU_PCT", "max_cpu_pct", 15, 100),
            ("MAX_CORE_CPU_PCT", "max_core_cpu_pct", 20, 100),
            ("MAX_LOAD_AVG", "max_load_avg", 0.5, None),
            ("SAMPLE_SEC", "sample_sec", 10, 3600),
        ):
            try:
                value = float(env.get(name, default))
            except (ValueError, TypeError):
                raise ReclaimError(f"invalid {name}: expected a positive number") from None
            if not math.isfinite(value) or value <= 0 or (upper and value > upper):
                raise ReclaimError(f"invalid {name}: expected a finite positive number"
                                   + (f" <= {upper}" if upper else ""))
            values[field] = value
        flag = env.get("DRY_RUN", "0")
        if flag not in ("0", "1"):
            raise ReclaimError("invalid DRY_RUN: expected 0 or 1")
        return cls(**values, dry_run=dry_run or flag == "1")


def read_text(root, relative):
    try:
        return (Path(root) / relative).read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ReclaimError(f"cannot read {relative}: {exc}") from exc


def read_cpu(root):
    counters = {}
    try:
        for line in read_text(root, "stat").splitlines():
            fields = line.split()
            if not fields or not (fields[0] == "cpu" or
                                  fields[0].startswith("cpu") and fields[0][3:].isdigit()):
                continue
            # guest and guest_nice are already included in user and nice.
            ticks = tuple(int(value) for value in fields[1:9])
            if len(ticks) != 8 or min(ticks) < 0 or fields[0] in counters:
                raise ValueError("invalid CPU counters")
            counters[fields[0]] = ticks
        if "cpu" not in counters or len(counters) < 2:
            raise ValueError("missing CPU counters")
    except ValueError as exc:
        raise ReclaimError(f"malformed stat: {exc}") from exc
    return counters


def cpu_activity(before, after, config, require_progress=True):
    if before.keys() != after.keys():
        raise ReclaimError("CPU set changed while sampling")
    for name, ticks in after.items():
        delta = tuple(new - old for new, old in zip(ticks, before[name]))
        if min(delta) < 0:
            raise ReclaimError("CPU counters moved backwards")
        total = sum(delta)
        if not total:
            if require_progress:
                raise ReclaimError(f"no CPU sampling progress for {name}")
            continue
        # Only idle is idle: iowait is treated as activity as well.
        busy = 100 * (total - delta[3]) / total
        limit = config.max_cpu_pct if name == "cpu" else config.max_core_cpu_pct
        if busy > limit:
            return f"{name} activity {busy:.1f}% exceeds {limit:g}%"
    return None


def read_memory(root):
    required = {"Cached", "Buffers", "Shmem", "Dirty", "Writeback"}
    values = {}
    try:
        for line in read_text(root, "meminfo").splitlines():
            fields = line.split()
            if fields and fields[0].rstrip(":") in required:
                name = fields[0].rstrip(":")
                if len(fields) != 3 or fields[2] != "kB" or name in values:
                    raise ValueError("invalid memory field")
                values[name] = int(fields[1])
                if values[name] < 0:
                    raise ValueError("negative memory counter")
        if values.keys() != required:
            raise ValueError("missing memory fields")
    except ValueError as exc:
        raise ReclaimError(f"malformed meminfo: {exc}") from exc
    return values


def read_guards(root, config):
    try:
        loads = read_text(root, "loadavg").split()
        if len(loads) < 3:
            raise ValueError("missing load averages")
        load = [float(value) for value in loads[:3]]
        if any(not math.isfinite(value) or value < 0 for value in load):
            raise ValueError("invalid load average")
    except ValueError as exc:
        raise ReclaimError(f"malformed loadavg: {exc}") from exc
    if load[0] > config.max_load_avg:
        return f"load {load[0]:g} exceeds {config.max_load_avg:g}", None
    psi = {}
    try:
        for line in read_text(root, "pressure/memory").splitlines():
            fields = line.split()
            if not fields or fields[0] not in ("some", "full"):
                raise ValueError("invalid PSI row")
            name = fields[0]
            metrics = dict(item.split("=", 1) for item in fields[1:])
            value = float(metrics["avg10"])
            if name in psi or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError("invalid PSI avg10")
            psi[name] = value
        if psi.keys() != {"some", "full"}:
            raise ValueError("missing PSI rows")
    except (ValueError, KeyError) as exc:
        raise ReclaimError(f"malformed pressure/memory: {exc}") from exc
    if psi["some"] > 0.5 or psi["full"] > 0.1:
        return "memory pressure is elevated", None
    memory = read_memory(root)
    if memory["Dirty"] + memory["Writeback"] > 16 * 1024:
        return "dirty/writeback memory exceeds 16 MiB", None
    cache_mb = max(0, memory["Cached"] + memory["Buffers"] - memory["Shmem"]) / 1024
    if cache_mb < config.min_cache_mb:
        return f"disposable cache {cache_mb:.1f} MiB is below {config.min_cache_mb:g} MiB", cache_mb
    return None, cache_mb


def write_drop(path):
    with path.open("w", encoding="ascii") as stream:
        stream.write("1\n")


def run(config, proc_root=Path("/proc"), sleeper=time.sleep,
        time_fn=time.monotonic, writer=write_drop, geteuid=os.geteuid):
    reason, _ = read_guards(proc_root, config)
    if reason:
        return "skip: " + reason
    before = read_cpu(proc_root)
    start = time_fn()
    sleeper(config.sample_sec)
    elapsed = time_fn() - start
    if not math.isfinite(elapsed) or elapsed <= 0:
        raise ReclaimError("invalid sampling elapsed time")
    after = read_cpu(proc_root)
    reason = cpu_activity(before, after, config)
    if reason:
        return "skip: " + reason
    reason, _ = read_guards(proc_root, config)
    if reason:
        return "skip: " + reason
    # Close the sampling-to-write gap with fresh CPU, load, PSI and cache data.
    final = read_cpu(proc_root)
    reason = cpu_activity(after, final, config, require_progress=False)
    if reason:
        return "skip: " + reason
    reason, cache_mb = read_guards(proc_root, config)
    if reason:
        return "skip: " + reason
    if config.dry_run:
        return f"would-drop: clean page cache (estimated {cache_mb:.1f} MiB disposable)"
    if geteuid() != 0:
        raise ReclaimError("root is required to write drop_caches; use --dry-run")
    try:
        writer(Path(proc_root) / "sys/vm/drop_caches")
    except OSError as exc:
        raise ReclaimError(f"cannot write drop_caches: {exc}") from exc
    return f"dropped: clean page cache (estimated {cache_mb:.1f} MiB disposable)"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Environment: MIN_CACHE_MB=1024, MAX_CPU_PCT=15, MAX_CORE_CPU_PCT=20, "
        "MAX_LOAD_AVG=0.5, SAMPLE_SEC=10 (maximum 3600), DRY_RUN=0 or 1. "
        "CPU limits are percentages in (0,100]; other thresholds must be positive."))
    parser.add_argument("--dry-run", action="store_true", help="report without writing drop_caches")
    args = parser.parse_args(argv)
    try:
        print(run(Config.from_env(os.environ, args.dry_run)))
    except (ReclaimError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
