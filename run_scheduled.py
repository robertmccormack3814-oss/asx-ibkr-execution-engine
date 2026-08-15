import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent
ENGINE = ROOT / "engine.py"
LOG = ROOT / "scheduler_log.jsonl"
LOCK = ROOT / ".scheduler.lock"
ASX_TZ = ZoneInfo("Australia/Sydney")
START_MINUTES = 9 * 60 + 55
END_MINUTES = 16 * 60 + 15
LOCK_STALE_MINUTES = 30


def log(kind, **payload):
    record = {
        "timestamp": datetime.now(ASX_TZ).isoformat(),
        "type": kind,
        **payload,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    print(json.dumps(record))


def within_asx_window(now):
    if now.weekday() >= 5:
        return False, "weekend"
    minute = now.hour * 60 + now.minute
    if minute < START_MINUTES or minute > END_MINUTES:
        return False, "outside ASX run window"
    return True, None


def acquire_lock(now):
    if LOCK.exists():
        try:
            age = now - datetime.fromtimestamp(LOCK.stat().st_mtime, tz=ASX_TZ)
            if age > timedelta(minutes=LOCK_STALE_MINUTES):
                LOCK.unlink(missing_ok=True)
                log("STALE_LOCK_REMOVED", age_minutes=round(age.total_seconds() / 60, 1))
            else:
                return False
        except Exception:
            return False
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True
    except FileExistsError:
        return False


def main():
    now = datetime.now(ASX_TZ)
    allowed, reason = within_asx_window(now)
    if not allowed:
        log("SCHEDULE_SKIP", reason=reason)
        return 0

    if not acquire_lock(now):
        log("SCHEDULE_SKIP", reason="previous engine run still active")
        return 0

    try:
        log("SCHEDULE_START", engine=str(ENGINE))
        result = subprocess.run(
            [sys.executable, str(ENGINE)],
            cwd=str(ROOT),
            check=False,
        )
        log("SCHEDULE_END", return_code=result.returncode)
        return result.returncode
    except Exception as exc:
        log("SCHEDULE_ERROR", error=str(exc))
        return 1
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
