/** Time helpers. India has no daylight saving, so IST is always UTC+5:30. */
const IST_OFFSET_SECONDS = 5.5 * 3600;
const DAY = 86_400;

function dayLabel(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toISOString().slice(0, 10);
}

/** The calendar day (YYYY-MM-DD) in India. The free daily question cap resets at its midnight. */
export function istDay(nowSeconds: number): string {
  return dayLabel(nowSeconds + IST_OFFSET_SECONDS);
}

/** The calendar day in UTC. Cloudflare's free limits reset at 00:00 UTC (05:30 IST). */
export function utcDay(nowSeconds: number): string {
  return dayLabel(nowSeconds);
}

/**
 * First day (YYYY-MM-DD) of the current score week. Weeks start on Sunday at 20:00 IST, the
 * same moment as the weekly leaderboard of the Python bot.
 */
export function istWeekStart(nowSeconds: number): string {
  const local = nowSeconds + IST_OFFSET_SECONDS;
  const dayStart = Math.floor(local / DAY) * DAY;
  const weekday = new Date(dayStart * 1000).getUTCDay(); // 0 = Sunday
  let sunday = dayStart - weekday * DAY;
  if (local < sunday + 20 * 3600) sunday -= 7 * DAY;
  return dayLabel(sunday);
}

/** Seconds from `nowSeconds` until the next midnight in India. */
export function secondsUntilIstMidnight(nowSeconds: number): number {
  const local = nowSeconds + IST_OFFSET_SECONDS;
  return DAY - (local % DAY);
}

/** "27 Oct 2026, 18:30 IST" for messages. */
export function formatIst(nowSeconds: number): string {
  const d = new Date((nowSeconds + IST_OFFSET_SECONDS) * 1000);
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${d.getUTCDate()} ${months[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${hh}:${mm} IST`;
}
