    if dry_run:
        valid, skipped = filter_valid(questions)
        logger.info("%s: %d valid question(s), %d invalid.", quiz_id, len(valid), skipped)
        return 0 if valid and not skipped else 1

    # Manual runs: use a predefined 10-second timer (so manual runs behave
    # consistently independent of schedule/defaults). Create a minimal
    # ScheduleEntry carrying only the timer so run_quiz picks it up.
    logger.info("Manual run: posting %s now (schedule, sent record, weekly scores and "
                "question statistics are unchanged). Using 10s timer for this manual run.",
                quiz_id)
    now = datetime.now(timezone.utc)
    manual_entry = ScheduleEntry(
        when=now,
        quiz_id=quiz_id,
        key=f"{quiz_id}@manual",
        pick=None,
        strategy="",
        timer=10,
        no_repeat_days=None,
    )
    outcome = run_quiz(questions, token, chat_id, quiz_id, entry=manual_entry)
    log_outcome(quiz_id, outcome)
    return 0 if outcome.sent > 0 and outcome.failed == 0 else 1
