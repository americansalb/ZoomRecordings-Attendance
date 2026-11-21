"""How hard the bot's computer is working, read from the container itself.

Added 2026-11-21. Zoom's figures showed the bot's camera picture dropping to
0 to 6 frames a second in a 96-device class while memory sat at half, and
the health page had no way to show the processor. This reads the container's
CPU accounting (cgroup v2 cpu.stat and cpu.max, or v1's cpuacct and cpu.stat)
and says, since the last time anyone asked: how busy the allowed processors
were, and what share of that time the platform held the container back
because it had used its allowance (throttled). Never raises.
"""
import os
import time

_last = {"at": None, "usage_us": None, "throttled_us": None, "periods": None, "throttled_periods": None}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _stat(text):
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def allowed_cpus():
    """How many processors the container may use; None when unlimited or unknown."""
    mx = _read("/sys/fs/cgroup/cpu.max")
    if mx:
        q, _, p = mx.partition(" ")
        if q != "max":
            try:
                return round(int(q) / int(p or 100000), 2)
            except ValueError:
                pass
    q = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    p = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    try:
        if q and p and int(q) > 0:
            return round(int(q) / int(p), 2)
    except ValueError:
        pass
    return None


def read_counters():
    """(usage_us, throttled_us, periods, throttled_periods), any of them None."""
    st = _stat(_read("/sys/fs/cgroup/cpu.stat"))
    if "usage_usec" in st:
        return (st.get("usage_usec"), st.get("throttled_usec"), st.get("nr_periods"), st.get("nr_throttled"))
    v1 = _stat(_read("/sys/fs/cgroup/cpu/cpu.stat"))
    usage = _read("/sys/fs/cgroup/cpuacct/cpuacct.usage") or _read("/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage")
    try:
        usage_us = int(usage) // 1000 if usage else None
    except ValueError:
        usage_us = None
    thr = v1.get("throttled_time")
    return (usage_us, thr // 1000 if thr is not None else None, v1.get("nr_periods"), v1.get("nr_throttled"))


def busy_between(prev, cur, seconds, cpus):
    """Pure: the busy and held-back percentages between two readings."""
    out = {"seconds": round(seconds, 1), "busy_percent": None, "held_back_percent": None}
    if seconds <= 0:
        return out
    pu, pt, pp, ptp = prev
    cu, ct, cp, ctp = cur
    if pu is not None and cu is not None and cu >= pu:
        share = (cu - pu) / (seconds * 1e6) / (cpus or os.cpu_count() or 1)
        out["busy_percent"] = round(min(100.0, share * 100), 1)
    if pp is not None and cp is not None and ptp is not None and ctp is not None and cp > pp:
        out["held_back_percent"] = round(100.0 * (ctp - ptp) / (cp - pp), 1)
    return out


def report():
    """The meter for /healthz. The first call after a start has no interval yet."""
    try:
        now = time.monotonic()
        cur = read_counters()
        cpus = allowed_cpus()
        out = {"cpus": cpus, "load_1min": None}
        try:
            out["load_1min"] = round(os.getloadavg()[0], 2)
        except OSError:
            pass
        if _last["at"] is not None:
            out.update(busy_between((_last["usage_us"], _last["throttled_us"], _last["periods"], _last["throttled_periods"]),
                                   cur, now - _last["at"], cpus))
        _last.update(at=now, usage_us=cur[0], throttled_us=cur[1], periods=cur[2], throttled_periods=cur[3])
        return out
    except Exception as e:  # the meter must never break the health page
        return {"error": str(e)[:120]}
