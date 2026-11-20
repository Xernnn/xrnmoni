// Days are the viewer's own: each starts at local midnight, and that moment is what the server is
// sent. The first dashboard took today's date from toISOString(), which is the UTC date, so before
// the UTC midnight (until 1 pm in Auckland in summer) it opened on yesterday.

export const pad = (n) => String(n).padStart(2, "0");

export function dayKey(d) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function parseKey(key) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key || "");
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return dayKey(d) === key ? d : null;
}

// By the calendar, not by 24 hours: a day with a daylight-saving change is 23 or 25 hours long.
export function addDays(d, n) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
}

export function today(now = new Date()) {
  return dayKey(now);
}

export function shiftKey(key, n) {
  return dayKey(addDays(parseKey(key), n));
}

export function dayBounds(key) {
  const d = parseKey(key);
  return [d.getTime() / 1000, addDays(d, 1).getTime() / 1000];
}

// Weeks start on Monday.
export function weekStart(key) {
  const d = parseKey(key);
  return addDays(d, -((d.getDay() + 6) % 7));
}

export function weekDays(key) {
  const start = weekStart(key);
  return Array.from({ length: 7 }, (_, i) => dayKey(addDays(start, i)));
}

export function weekBounds(key) {
  const start = weekStart(key);
  return Array.from({ length: 8 }, (_, i) => addDays(start, i).getTime() / 1000);
}

// The moments of the day's hours that exist, as [hour, epoch seconds]: on a daylight-saving day
// one is missing or comes twice.
export function hourMarks(key, every = 3) {
  const d = parseKey(key);
  const out = [];
  for (let hour = 0; hour <= 24; hour += every) {
    const t = new Date(d.getFullYear(), d.getMonth(), d.getDate(), hour);
    out.push([hour, t.getTime() / 1000]);
  }
  return out;
}
