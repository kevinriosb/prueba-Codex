from datetime import datetime
from zoneinfo import ZoneInfo


def server_time(timezone: str) -> dict:
    now = datetime.now(ZoneInfo(timezone))
    return {"datetime": now.isoformat(timespec="seconds"), "timezone": timezone}
