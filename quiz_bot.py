def load_schedule() -> List[ScheduleEntry]:
    """Read schedule.json as strict JSON.

    Comments and trailing commas are intentionally rejected so schedule.json stays
    easy to audit and cannot silently hide malformed data.

    Bad entries are skipped with a warning.

    Raises:
        RuntimeError: if the file itself is missing or not a JSON list.
    """
    try:
        data = read_json(SCHEDULE_FILE, allow_comments=False)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot load schedule.json: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("schedule.json must contain a JSON list")
