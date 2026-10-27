"""Five-field cron evaluation with owner-approved DST handling."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter


def validate_cron_expression(expression: str) -> str:
    if len(expression.split()) != 5 or not croniter.is_valid(expression):
        raise ValueError("cron_expression must be a valid five-field cron expression")
    return expression


def next_fire_at(expression: str, timezone_name: str, after: datetime) -> datetime:
    validate_cron_expression(expression)
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be an installed IANA timezone") from exc

    after_utc = after.astimezone(timezone.utc)
    local_base = after_utc.astimezone(zone).replace(tzinfo=None)
    iterator = croniter(expression, local_base)
    for _ in range(10000):
        wall_time = iterator.get_next(datetime)
        possible_instants: set[datetime] = set()
        for fold in (0, 1):
            local_candidate = wall_time.replace(tzinfo=zone, fold=fold)
            utc_candidate = local_candidate.astimezone(timezone.utc)
            round_trip = utc_candidate.astimezone(zone)
            if round_trip.replace(tzinfo=None) == wall_time:
                possible_instants.add(utc_candidate)
        if not possible_instants:
            # A DST gap has no valid instant for this scheduled local wall time.
            continue
        # A DST fold can map one wall time to two instants. Choose the earlier
        # instant once; if it is already past, skip the repeated wall time.
        chosen = min(possible_instants)
        if chosen > after_utc:
            return chosen
    raise ValueError("no next execution time found within the cron search window")
