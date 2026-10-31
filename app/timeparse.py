"""Time handling that fails closed.

* DST gaps and folds raise AmbiguousTime instead of guessing.
* Spoken dates/times are parsed into candidates with their source span, so the
  extractor can record exactly which words produced a commitment.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


class AmbiguousTime(ValueError):
    pass


def unique_local_wall(naive: datetime, tzname: str) -> datetime:
    """Resolve a business-local wall time, or raise on DST gap/fold."""
    if naive.tzinfo is not None:
        raise ValueError('expected naive local datetime')
    zone = ZoneInfo(tzname)
    valid = []
    for fold in (0, 1):
        cand = naive.replace(tzinfo=zone, fold=fold)
        back = cand.astimezone(timezone.utc).astimezone(zone)
        if back.replace(tzinfo=None) == naive:
            valid.append(cand)
    if not valid:
        raise AmbiguousTime('nonexistent local time (DST gap)')
    if len(valid) == 2 and valid[0].utcoffset() != valid[1].utcoffset():
        raise AmbiguousTime('repeated local time (DST fold)')
    return valid[0]


def to_utc(value, tzname: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = unique_local_wall(dt, tzname)
        return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise ValueError('missing timestamp')
    s = value.strip().replace('Z', '+00:00')
    if re.fullmatch(r'\d{10,13}', s):
        return to_utc(int(s), tzname)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = unique_local_wall(dt, tzname)
    return dt.astimezone(timezone.utc)


def local(dt: datetime, tzname: str) -> datetime:
    return dt.astimezone(ZoneInfo(tzname))


# ---------------------------------------------------------------- spoken forms
MONTHS = {m: i for i, m in enumerate(['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
WEEKDAYS = {d: i for i, d in enumerate(['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'])}
NUMWORDS = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
            'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12}
ORD = r'(\d{1,2})(?:st|nd|rd|th)?'

_DATE_MD = re.compile(r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+' + ORD + r'(?:,?\s*(20\d\d))?\b', re.I)
_DATE_DM = re.compile(r'\b(?:the\s+)?' + ORD + r'\s+of\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b', re.I)
_DATE_NUM = re.compile(r'\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b')
_DATE_THE = re.compile(r'\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b', re.I)
_DATE_REL = re.compile(r'\b(today|tonight|tomorrow|day after tomorrow)\b', re.I)
_DATE_WD = re.compile(r'\b(?:(this|next|coming|on)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', re.I)

_TIME = re.compile(
    r'\b(?:(noon|midday|midnight)|'
    r'(\d{1,2})(?::([0-5]\d))?\s*(a\.?\s?m\.?|p\.?\s?m\.?|o\'?clock)?|'
    r'(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)(?:\s+(thirty|fifteen|forty[- ]five))?\s*(a\.?\s?m\.?|p\.?\s?m\.?|o\'?clock)?)'
    r'(?=\W|$)', re.I)
_TIME_QUARTER = re.compile(r'\bquarter\s+(past|after|to|till|til|before)\s+(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b', re.I)
_PERIOD_AFTER = re.compile(r"\s*(?:o'?\s?clock\s+)?(?:in\s+the\s+(morning|afternoon|evening)|(?:at\s+)?(night)|(tonight))\b", re.I)
_PERIOD_BEFORE = re.compile(r'\b(morning|afternoon|evening|night)\s*(?:at\s+|,\s*)?$', re.I)
_TIME_HALF = re.compile(r'\bhalf\s+past\s+(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b', re.I)


@dataclass
class DateCand:
    value: date
    kind: str      # explicit | relative | weekday | ordinal
    span: tuple
    text: str


@dataclass
class TimeCand:
    hour: int      # 0-23 if meridiem known, else 1-12
    minute: int
    meridiem: bool
    span: tuple
    text: str

    def resolved(self, hint_pm: bool | None = None) -> tuple[int, int, str] | None:
        """(hour24, minute, how). Without am/pm uses a hint or business-hours rule."""
        if self.meridiem:
            return self.hour, self.minute, 'explicit'
        h = self.hour % 12
        if hint_pm is not None:
            return h + (12 if hint_pm else 0), self.minute, 'inherited'
        # business-hours convention: 7-11 morning, 12-6 afternoon. Marked inferred.
        if 7 <= self.hour <= 11:
            return h, self.minute, 'inferred'
        if self.hour == 12 or 1 <= self.hour <= 6:
            return h + 12, self.minute, 'inferred'
        return None


def _year_for(month: int, day: int, ref: date) -> date:
    d = date(ref.year, month, day)
    if d < ref - timedelta(days=1):
        d = date(ref.year + 1, month, day)
    return d


def find_dates(text: str, ref: date) -> list[DateCand]:
    out: list[DateCand] = []
    taken: list[tuple] = []

    def add(d, kind, m):
        if any(not (m.end() <= a or m.start() >= b) for a, b in taken):
            return
        taken.append(m.span())
        out.append(DateCand(d, kind, m.span(), m.group(0)))

    for m in _DATE_MD.finditer(text):
        try:
            mon = MONTHS[m.group(1).lower()[:3]]
            day = int(m.group(2))
            d = date(int(m.group(3)), mon, day) if m.group(3) else _year_for(mon, day, ref)
            add(d, 'explicit', m)
        except ValueError:
            pass
    for m in _DATE_DM.finditer(text):
        try:
            add(_year_for(MONTHS[m.group(2).lower()[:3]], int(m.group(1)), ref), 'explicit', m)
        except ValueError:
            pass
    for m in _DATE_NUM.finditer(text):
        try:
            mon, day = int(m.group(1)), int(m.group(2))
            if m.group(3):
                y = int(m.group(3))
                d = date(y + 2000 if y < 100 else y, mon, day)
            else:
                d = _year_for(mon, day, ref)
            add(d, 'explicit', m)
        except ValueError:
            pass
    for m in _DATE_REL.finditer(text):
        w = m.group(1).lower()
        off = {'today': 0, 'tonight': 0, 'tomorrow': 1, 'day after tomorrow': 2}[w]
        add(ref + timedelta(days=off), 'relative', m)
    for m in _DATE_WD.finditer(text):
        mod = (m.group(1) or '').lower()
        wd = WEEKDAYS[m.group(2).lower()]
        delta = (wd - ref.weekday()) % 7
        if mod == 'next':
            # "next Friday" = the Friday in next calendar week (Mon-start). Ambiguous in speech,
            # so kind='weekday_next' lets the extractor require an explicit read-back.
            start_next_week = ref + timedelta(days=7 - ref.weekday())
            d = start_next_week + timedelta(days=wd)
            add(d, 'weekday_next', m)
        else:
            if delta == 0 and mod not in ('this', 'on'):
                delta = 7 if mod == 'coming' else 0
            add(ref + timedelta(days=delta), 'weekday', m)
    for m in _DATE_THE.finditer(text):
        day = int(m.group(1))
        try:
            d = date(ref.year, ref.month, day)
            if d < ref:
                nm = ref.month % 12 + 1
                d = date(ref.year + (1 if nm == 1 else 0), nm, day)
            add(d, 'ordinal', m)
        except ValueError:
            pass
    out.sort(key=lambda c: c.span[0])
    return out


def find_times(text: str, exclude: list[tuple] = ()) -> list[TimeCand]:
    out: list[TimeCand] = []
    for m in _TIME_HALF.finditer(text):
        h = m.group(1)
        hour = NUMWORDS.get(h.lower(), None) if not h.isdigit() else int(h)
        if hour and 1 <= hour <= 12:
            out.append(TimeCand(hour, 30, False, m.span(), m.group(0)))
    for m in _TIME_QUARTER.finditer(text):
        h = m.group(2)
        hour = NUMWORDS.get(h.lower(), None) if not h.isdigit() else int(h)
        if hour and 1 <= hour <= 12:
            if m.group(1).lower() in ('past', 'after'):
                out.append(TimeCand(hour, 15, False, m.span(), m.group(0)))
            else:
                out.append(TimeCand(12 if hour == 1 else hour - 1, 45, False, m.span(), m.group(0)))
    for m in _TIME.finditer(text):
        if any(not (m.end() <= a or m.start() >= b) for a, b in list(exclude) + [c.span for c in out]):
            continue
        g = m.groups()
        if g[0]:
            w = g[0].lower()
            out.append(TimeCand(0 if w == 'midnight' else 12, 0, True, m.span(), m.group(0)))
            continue
        if g[1]:
            hour, minute, mer = int(g[1]), int(g[2] or 0), (g[3] or '').lower().replace('.', '').replace(' ', '')
            # bare numbers need a time context: "at 3", "3 o'clock", "make it 10", or minutes
            pre = text[max(0, m.start() - 12):m.start()].lower()
            if not mer and not g[2] and not re.search(r'(at|it|for|to|around|about|by|or|and|not|morning|afternoon|evening|night|,)\s*$', pre):
                continue
            if mer in ('am', 'pm'):
                if not 1 <= hour <= 12:
                    continue
                hour = hour % 12 + (12 if mer == 'pm' else 0)
                out.append(TimeCand(hour, minute, True, m.span(), m.group(0)))
            elif hour > 12 and hour <= 23 and g[2]:
                out.append(TimeCand(hour, minute, True, m.span(), m.group(0)))
            elif 1 <= hour <= 12:
                out.append(TimeCand(hour, minute, False, m.span(), m.group(0)))
            continue
        if g[4]:
            hour = NUMWORDS[g[4].lower()]
            minute = {'thirty': 30, 'fifteen': 15}.get((g[5] or '').lower(), 45 if g[5] else 0)
            mer = (g[6] or '').lower().replace('.', '').replace(' ', '')
            pre = text[max(0, m.start() - 12):m.start()].lower()
            if not mer and not g[5] and not re.search(r'(at|it|for|to|around|about|by|or|and|not|morning|afternoon|evening|night|,)\s*$', pre):
                continue
            if mer in ('am', 'pm'):
                out.append(TimeCand(hour % 12 + (12 if mer == 'pm' else 0), minute, True, m.span(), m.group(0)))
            else:
                out.append(TimeCand(hour, minute, False, m.span(), m.group(0)))
    # spoken day periods fix the meridiem: "4:45 in the evening", "evening 4:45", "2 o'clock in the afternoon"
    fixed = []
    for c in out:
        if not c.meridiem and 1 <= c.hour <= 12:
            ma = _PERIOD_AFTER.match(text, c.span[1])
            mb = _PERIOD_BEFORE.search(text[max(0, c.span[0] - 16):c.span[0]])
            per = ((ma and (ma.group(1) or ma.group(2) or ma.group(3))) or (mb and mb.group(1)) or '').lower()
            if per == 'morning':
                c = TimeCand(c.hour % 12, c.minute, True, c.span, c.text)
            elif per in ('afternoon', 'evening', 'night', 'tonight'):
                c = TimeCand(c.hour % 12 + 12, c.minute, True, c.span, c.text)
        fixed.append(c)
    fixed.sort(key=lambda c: c.span[0])
    return fixed


def combine(d: date, hour: int, minute: int, tzname: str) -> datetime:
    return unique_local_wall(datetime.combine(d, time(hour, minute)), tzname)
