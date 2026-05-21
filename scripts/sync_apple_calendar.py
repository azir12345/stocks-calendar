#!/usr/bin/env python3
"""Optionally sync the generated ICS feed into macOS Apple Calendar."""

from __future__ import annotations

import argparse
import datetime as dt
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_ICS_PATH = "public/earnings.ics"
DEFAULT_CALENDAR_NAME = "Stocks Calendar"
SYNC_MARKER_PREFIX = "[stocks-calendar uid:"


@dataclass(frozen=True)
class IcsEvent:
    uid: str
    summary: str
    description: str
    start: dt.date | dt.datetime
    end: dt.date | dt.datetime
    all_day: bool
    url: str | None = None
    reminder_days_before: int | None = None


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync public/earnings.ics into macOS Apple Calendar.")
    parser.add_argument("--ics", default=DEFAULT_ICS_PATH, help="Path to the generated .ics file")
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR_NAME, help="Apple Calendar name to create/use")
    parser.add_argument("--apply", action="store_true", help="Actually write to Apple Calendar. Default is dry-run.")
    args = parser.parse_args()

    events = parse_ics(Path(args.ics))
    print(f"Loaded {len(events)} events from {args.ics}")
    print(f"Target Apple Calendar: {args.calendar}")
    if not args.apply:
        print("Dry-run only. Re-run with --apply to write to Apple Calendar.")
        for event in events:
            print(f"- {event.summary} | {event.start} -> {event.end}")
        return 0

    if platform.system() != "Darwin":
        print("ERROR: Apple Calendar sync is only supported on macOS.", file=sys.stderr)
        return 2

    script = build_applescript(args.calendar, events)
    subprocess.run(["osascript", "-"], input=script, text=True, check=True)
    print(f"Synced {len(events)} events into Apple Calendar '{args.calendar}'.")
    return 0


def parse_ics(path: Path) -> list[IcsEvent]:
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    lines = unfold_ics_lines(raw_lines)
    events: list[IcsEvent] = []
    current: dict[str, str] | None = None
    alarm_trigger: str | None = None
    in_alarm = False

    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
            alarm_trigger = None
            in_alarm = False
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(event_from_properties(current, alarm_trigger))
            current = None
            continue
        if current is None:
            continue
        if line == "BEGIN:VALARM":
            in_alarm = True
            continue
        if line == "END:VALARM":
            in_alarm = False
            continue
        name, value = split_ics_property(line)
        if in_alarm and name == "TRIGGER":
            alarm_trigger = value
        elif not in_alarm:
            current[name] = value

    return events


def unfold_ics_lines(lines: list[str]) -> list[str]:
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def split_ics_property(line: str) -> tuple[str, str]:
    if ":" not in line:
        return line, ""
    key, value = line.split(":", 1)
    name = key.split(";", 1)[0].upper()
    return name, unescape_ics_text(value)


def event_from_properties(properties: dict[str, str], alarm_trigger: str | None) -> IcsEvent:
    uid = properties.get("UID", "").removesuffix("@stocks-calendar")
    start, all_day = parse_ics_datetime(properties, "DTSTART")
    end, _ = parse_ics_datetime(properties, "DTEND")
    return IcsEvent(
        uid=uid,
        summary=properties.get("SUMMARY", "Stocks Calendar Event"),
        description=properties.get("DESCRIPTION", ""),
        start=start,
        end=end,
        all_day=all_day,
        url=properties.get("URL") or None,
        reminder_days_before=parse_alarm_days(alarm_trigger),
    )


def parse_ics_datetime(properties: dict[str, str], name: str) -> tuple[dt.date | dt.datetime, bool]:
    raw_value = properties.get(name)
    if raw_value is None:
        raise ValueError(f"ICS event is missing {name}")
    if re.fullmatch(r"\d{8}", raw_value):
        return dt.date(int(raw_value[:4]), int(raw_value[4:6]), int(raw_value[6:8])), True
    match = re.fullmatch(r"(\d{8})T(\d{6})Z?", raw_value)
    if not match:
        raise ValueError(f"Unsupported {name} value: {raw_value}")
    date_part, time_part = match.groups()
    return (
        dt.datetime(
            int(date_part[:4]),
            int(date_part[4:6]),
            int(date_part[6:8]),
            int(time_part[:2]),
            int(time_part[2:4]),
            int(time_part[4:6]),
        ),
        False,
    )


def parse_alarm_days(trigger: str | None) -> int | None:
    if not trigger:
        return None
    match = re.fullmatch(r"-P(\d+)D", trigger)
    if not match:
        return None
    return int(match.group(1))


def unescape_ics_text(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt in ("n", "N"):
                result.append("\n")
            else:
                result.append(nxt)
            index += 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


def build_applescript(calendar_name: str, events: list[IcsEvent]) -> str:
    if not events:
        return 'display notification "No stocks-calendar events to sync"\n'
    window_start = min(event_date(event.start) for event in events)
    window_end = max(event_date(event.end) for event in events) + dt.timedelta(days=1)

    lines = [
        "on makeDate(theYear, theMonth, theDay, theSeconds)",
        "  set theDate to current date",
        "  set year of theDate to theYear",
        "  set month of theDate to theMonth",
        "  set day of theDate to theDay",
        "  set time of theDate to theSeconds",
        "  return theDate",
        "end makeDate",
        "",
        'tell application "Calendar"',
        f"  set calendarName to {as_applescript_string(calendar_name)}",
        "  if not (exists calendar calendarName) then",
        "    make new calendar with properties {name:calendarName}",
        "  end if",
        "  set targetCalendar to calendar calendarName",
        f"  set cleanupStart to {applescript_date(dt.datetime.combine(window_start, dt.time.min))}",
        f"  set cleanupEnd to {applescript_date(dt.datetime.combine(window_end, dt.time.min))}",
        f"  delete (every event of targetCalendar whose description contains {as_applescript_string(SYNC_MARKER_PREFIX)} and start date >= cleanupStart and start date < cleanupEnd)",
    ]
    for event in events:
        lines.extend(applescript_create_event(event))
    lines.append("end tell")
    return "\n".join(lines) + "\n"


def applescript_create_event(event: IcsEvent) -> list[str]:
    description = f"{event.description}\n\n{SYNC_MARKER_PREFIX}{event.uid}]".strip()
    properties = [
        f"summary:{as_applescript_string(event.summary)}",
        f"start date:{applescript_date(event.start)}",
        f"end date:{applescript_date(event.end)}",
        f"description:{as_applescript_string(description)}",
        f"allday event:{'true' if event.all_day else 'false'}",
    ]
    if event.url:
        properties.append(f"url:{as_applescript_string(event.url)}")
    lines = [
        f"  set newEvent to make new event at end of events of targetCalendar with properties {{{', '.join(properties)}}}",
    ]
    if event.reminder_days_before is not None:
        minutes = -event.reminder_days_before * 24 * 60
        lines.append(f"  make new display alarm at end of display alarms of newEvent with properties {{trigger interval:{minutes}}}")
    return lines


def applescript_date(value: dt.date | dt.datetime) -> str:
    if isinstance(value, dt.datetime):
        date_value = value.date()
        seconds = value.hour * 3600 + value.minute * 60 + value.second
    else:
        date_value = value
        seconds = 0
    month_name = (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    )[date_value.month - 1]
    return f"my makeDate({date_value.year}, {month_name}, {date_value.day}, {seconds})"


def event_date(value: dt.date | dt.datetime) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    return value


def as_applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


if __name__ == "__main__":
    raise SystemExit(main())
