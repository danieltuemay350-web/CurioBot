"""UTC timestamp and local scheduling helpers."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

UTC_ZONE = UTC
LOCAL_ZONE = ZoneInfo("Africa/Addis_Ababa")


def utc_now() -> datetime:
    return datetime.now(UTC)


def dump_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Naive datetimes cannot be persisted")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def load_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Persisted datetime must include a timezone")
    return parsed.astimezone(UTC)


def local_day_bounds(day: date | None = None, timezone_name: str = "Africa/Addis_Ababa") -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    local_day = day or utc_now().astimezone(zone).date()
    start = datetime.combine(local_day, time.min, zone).astimezone(UTC)
    end = datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(UTC)
    return start, end
