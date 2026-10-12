#!/usr/bin/env python3
"""Email Metro disruption summaries to recipients subscribed to train lines. Stdlib only."""
import argparse
import html
from html.parser import HTMLParser
import textwrap
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

API = 'https://www.metrotrains.com.au/api?op=get_healthboard_alerts'
TZ = ZoneInfo('Australia/Melbourne')
SERVICE_WORKS = {'bus-replacement', 'service-changes'}
DAY_NAMES = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
DEFAULT_DAYS = DAY_NAMES[:5]
# Don't change this unless you know what you're doing,
# the SMTP call set up is by-and-large mail server agnostic,
# but depending on the service you may need to modify how
# it authenticates
EMAIL_SERVER = 'smtp.gmail.com'


class MetroHTMLFormatter(HTMLParser):
    """Convert Metro's paragraph/list HTML to readable email-friendly text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.current = []
        self.in_item = False

    def flush(self):
        value = ' '.join(''.join(self.current).split())
        if value:
            prefix = '  • ' if self.in_item else ''
            indent = '    ' if self.in_item else ''
            self.lines.append(textwrap.fill(value, width=76,
                                            initial_indent=prefix,
                                            subsequent_indent=indent,
                                            break_long_words=False))
        self.current = []

    def handle_starttag(self, tag, attrs):
        if tag in {'p', 'li', 'div', 'br'}:
            self.flush()
        if tag == 'li':
            self.in_item = True

    def handle_endtag(self, tag):
        if tag in {'p', 'li', 'div'}:
            self.flush()
        if tag == 'li':
            self.in_item = False

    def handle_data(self, data):
        self.current.append(data)

    def result(self):
        self.flush()
        return '\n'.join(self.lines).strip()


def plain(value):
    parser = MetroHTMLFormatter()
    parser.feed(str(value or ''))
    return parser.result()


def format_alert(alert):
    kind = str(alert.get('alert_type', 'alert')).replace('_', ' ').upper()
    description = plain(alert.get('alert_text')) or 'No further information provided by Metro.'
    return f'  [LIVE] {kind}\n\n{indent_body(description)}'


def indent_body(message):
    return '\n'.join('  ' + line if line else '' for line in message.splitlines())


def format_line_section(name, entries):
    heading = name.upper()
    return f'{heading}\n{'-' * min(len(heading), 32)}\n\n' + '\n\n'.join(entries)


def timestamp(value):
    try:
        return datetime.fromtimestamp(float(value), TZ)
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise ValueError(f'Invalid Metro timestamp: {value!r}') from exc


def overlaps(item, start, end):
    begins = item['from_date'] if 'from_date' in item else item['start_date']
    finishes = item['to_date'] if 'to_date' in item else item['end_date']
    return timestamp(begins) <= end and timestamp(finishes) >= start


def fetch_data(fixture=None):
    if fixture:
        return json.loads(Path(fixture).read_text(encoding='utf-8'))
    req = Request(API, headers={'Accept': 'application/json', 'User-Agent': 'metro-commute-alert/1.0'})
    with urlopen(req, timeout=20) as response:  # follows redirects
        return json.load(response)


def index_lines(data):
    if not isinstance(data, dict):
        raise ValueError('Metro response is not a JSON object')
    lines = {v['line_name'].strip().casefold(): v for v in data.values()
             if isinstance(v, dict) and isinstance(v.get('line_name'), str)}
    if not lines:
        raise ValueError('No Metro train lines found in response')
    return lines


def summaries(line, start, end, include_works=True):
    results = []
    alerts = line.get('alerts')
    if isinstance(alerts, list):
        for alert in alerts:
            if not isinstance(alert, dict):
                raise ValueError('Unexpected live alert item')
            if overlaps(alert, start, end):
                results.append(format_alert(alert))
    elif isinstance(alerts, str):
        if 'good service' not in alerts.casefold():
            results.append('  [LINE STATUS]\n\n' + indent_body(alerts))
    else:
        raise ValueError('Unexpected/missing alerts field')

    works = line.get('planned_works_list', [])
    if not isinstance(works, list):
        raise ValueError('Unexpected planned works format')
    if include_works:
        seen = set()
        for work in works:
            if not isinstance(work, dict):
                raise ValueError('Unexpected planned works entry')
            if work.get('type') not in SERVICE_WORKS or not overlaps(work, start, end):
                continue
            if work.get('id') in seen:
                continue
            seen.add(work.get('id'))
            begin = timestamp(work['start_date']).strftime('%a %d %b %I:%M %p')
            finish = timestamp(work['end_date']).strftime('%a %d %b %I:%M %p')
            results.append(f"  [PLANNED SERVICE CHANGE]\n\n"
                           f"  {work.get('title', 'Service change')}\n"
                           f"  {begin} – {finish}\n"
                           f"  {work.get('link', '')}")
    return results


def read_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8'))
    recipients = config.get('recipients')
    if not isinstance(recipients, dict) or not recipients:
        raise ValueError('Config must contain a non-empty "recipients" object')
    for email, schedules in recipients.items():
        if not isinstance(email, str) or '@' not in email:
            raise ValueError(f'Invalid recipient address: {email!r}')
        if not isinstance(schedules, list) or not schedules:
            raise ValueError(f'{email}: supply a non-empty list of time/lines schedules')
        seen_times = set()
        for schedule in schedules:
            if not isinstance(schedule, dict) or not {'time', 'lines'} <= set(schedule) or set(schedule) - {'time', 'lines', 'days'}:
                raise ValueError(f'{email}: each schedule needs time and lines (optional days)')
            days = schedule.get('days', DEFAULT_DAYS)
            if (not isinstance(days, list) and not isinstance(days, tuple)) or not days or any(
                    not isinstance(day, str) or day not in DAY_NAMES for day in days) or len(set(days)) != len(days):
                raise ValueError(f'{email}: days must be unique three-letter lowercase names: {", ".join(DAY_NAMES)}')
            at = schedule['time']
            try:
                parsed = datetime.strptime(at, '%H:%M')
            except (TypeError, ValueError) as exc:
                raise ValueError(f'{email}: invalid 24-hour time {at!r}') from exc
            if parsed.strftime('%H:%M') != at or parsed.minute not in (0, 30):
                raise ValueError(f'{email}: time must be HH:00 or HH:30 (cron runs each half-hour)')
            # The same time can be used for different day groups. Reject only
            # schedules that would overlap on the same day.
            day_set = set(days)
            if any(existing_at == at and day_set & existing_days for existing_at, existing_days in seen_times):
                raise ValueError(f'{email}: overlapping schedules at {at} for the same day')
            seen_times.add((at, frozenset(day_set)))
            lines = schedule['lines']
            if not isinstance(lines, list) or not lines or any(
                    not isinstance(line, str) or not line.strip() for line in lines):
                raise ValueError(f'{email} at {at}: lines must be a non-empty list')
    minutes = config.get('minutes', 120)
    if type(minutes) is not int or minutes <= 0:
        raise ValueError('"minutes" must be a positive integer')
    if type(config.get('include_planned_works', True)) is not bool:
        raise ValueError('"include_planned_works" must be true or false')
    return config


def due_recipients(config, now, grace_minutes=30):
    """Find subscriptions due in the previous grace period (not future slots).

    Each recipient maps to (scheduled HH:MM, deduplicated lines). For a recipient
    with more than one eligible slot, select the most recent one.
    """
    selected = {}
    for email, schedules in config['recipients'].items():
        eligible = []
        for schedule in schedules:
            hour, minute = map(int, schedule['time'].split(':'))
            scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if scheduled > now:
                scheduled -= timedelta(days=1)
            late_by = now - scheduled
            # Evaluate weekday from the actual scheduled slot, not today's date.
            # This also works across midnight for a delayed cron run.
            allowed_days = schedule.get('days', DEFAULT_DAYS)
            if DAY_NAMES[scheduled.weekday()] not in allowed_days:
                continue
            if timedelta(0) <= late_by < timedelta(minutes=grace_minutes):
                eligible.append((scheduled, schedule))
        if eligible:
            scheduled, match = max(eligible, key=lambda pair: pair[0])
            selected[email] = (match['time'], list(dict.fromkeys(
                line.strip() for line in match['lines'])))
    return selected


def html_from_entry(entry):
    """Render one existing alert entry without changing its selection or wording."""
    lines = entry.strip().splitlines()
    label = ''
    if lines and lines[0].strip().startswith('['):
        label = lines.pop(0).strip()
    # Paragraph/list handling uses the existing plain-text formatter output.
    # Continuation lines produced by textwrap are joined back into their bullet.
    paragraphs = []
    bullets = []
    def flush_bullets():
        if bullets:
            paragraphs.append('<ul style="margin:8px 0 12px;padding-left:22px">' +
                              ''.join('<li style="margin:4px 0">' + html.escape(b) + '</li>' for b in bullets) + '</ul>')
            bullets.clear()
    current = []
    def flush_paragraph():
        if current:
            paragraphs.append('<p style="margin:8px 0;line-height:1.5">' +
                              html.escape(' '.join(current)) + '</p>')
            current.clear()
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            flush_paragraph()
            flush_bullets()
        elif stripped.startswith(('• ', '- ')):
            flush_paragraph()
            bullets.append(stripped[2:])
        elif bullets and raw.startswith('      '):
            bullets[-1] += ' ' + stripped
        else:
            flush_bullets()
            current.append(stripped)
    flush_paragraph()
    flush_bullets()
    if label:
        alert_kind = label.replace('[LIVE]', '').strip()
        accent = '#b45309' if alert_kind in {'MINOR', 'CANCELLATION'} else '#b91c1c' if alert_kind in {'MAJOR', 'SUSPENDED'} else '#475569'
        label_html = f'<div style="font-size:12px;font-weight:700;letter-spacing:.05em;color:{accent};margin-bottom:8px">{html.escape(label.replace("[", "").replace("]", ""))}</div>'
    else:
        label_html = ''
    return ('<div style="padding:14px 0;border-bottom:1px solid #e5e7eb">' +
            label_html + ''.join(paragraphs) + '</div>')


def build_html_email(now, end, sections):
    """Gmail-friendly HTML with inline styles and no Unicode box characters."""
    content = []
    for line_name, entries in sections:
        content.append('<section style="margin:24px 0">'
                       f'<h2 style="font:700 17px Arial,sans-serif;margin:0;padding:0 0 10px;border-bottom:2px solid #1e293b;color:#0f172a">{html.escape(line_name.upper())}</h2>')
        for entry in entries:
            content.append(html_from_entry(entry))
        content.append('</section>')
        # TODO: Rip this out of here so we can have configurable email bodies
    return ('<!doctype html><html><body style="margin:0;background:#f8fafc;color:#1f2937;font-family:Arial,Helvetica,sans-serif">'
            '<div style="max-width:640px;margin:20px auto;background:white;padding:26px;border:1px solid #e5e7eb;border-radius:10px">'
            '<h1 style="margin:0 0 6px;font:700 22px Arial,sans-serif;color:#0f172a">Metro train alerts</h1>'
            f'<p style="margin:0;color:#64748b;font-size:13px">{html.escape(now.strftime("%A %d %B %Y"))}</p>'
            f'<p style="margin:10px 0 24px;font-size:14px"><strong>Travel window:</strong> {html.escape(now.strftime("%I:%M %p"))} – {html.escape(end.strftime("%I:%M %p"))} (Melbourne)</p>'
            + ''.join(content)
            + '<p style="margin-top:26px;padding-top:14px;border-top:1px solid #e5e7eb;color:#64748b;font-size:12px">Source: '
            + f'<a href="{html.escape(API, quote=True)}" style="color:#2563eb">Metro Trains Melbourne</a></p>'
            '</div></body></html>')


def send_email(sender, recipient, password, subject, body, html_body=None):
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.set_content(body)
    if html_body is not None:
        msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL(EMAIL_SERVER, 465, context=ssl.create_default_context(), timeout=20) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def load_dotenv(path=None):
    """Load simple KEY=VALUE settings from a .env beside this script.

    Existing process environment variables take precedence. No dependencies.
    """
    env_path = Path(path) if path else Path(__file__).resolve().parent / '.env'
    if not env_path.is_file():
        return
    for number, raw in enumerate(env_path.read_text(encoding='utf-8-sig').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, sep, value = line.partition('=')
        key = key.strip()
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError(f'{env_path}:{number}: expected KEY=VALUE')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Path to subscribers JSON')
    parser.add_argument('--dry-run', action='store_true', help='Print messages without emailing')
    parser.add_argument('--test-email', action='store_true', help='Send test message to every recipient')
    parser.add_argument('--fixture', help='Read a saved Metro JSON response (for testing)')
    parser.add_argument('--at', metavar='HH:MM', help='Simulate Melbourne clock time for testing, including late runs')
    args = parser.parse_args()
    try:
        load_dotenv()
    except (OSError, ValueError) as exc:
        print(f'Invalid .env: {exc}', file=sys.stderr)
        return 2
    try:
        config = read_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'Invalid configuration: {exc}', file=sys.stderr)
        return 2

    now = datetime.now(TZ)
    if args.at is not None:
        try:
            parsed = datetime.strptime(args.at, '%H:%M')
            if parsed.strftime('%H:%M') != args.at:
                raise ValueError()
        except (TypeError, ValueError):
            print('--at must be HH:MM', file=sys.stderr)
            return 2
        now = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)

    due = due_recipients(config, now)
    if not due:
        print(f'{now:%H:%M} Melbourne: no recipients scheduled within the last 30 minutes; skipping Metro API')
        return 0
    print(f'{now:%H:%M} Melbourne: checking {len(due)} scheduled recipient(s)')

    sender = os.environ.get('METRO_GMAIL_SENDER', '').strip()
    password = os.environ.get('METRO_GMAIL_PASSWORD', '').strip()
    if not args.dry_run and (not sender or not password):
        print('Set METRO_GMAIL_SENDER and METRO_GMAIL_PASSWORD in .env', file=sys.stderr)
        return 2

    end = now + timedelta(minutes=config.get('minutes', 120))
    error = None
    line_index = {}
    if not args.test_email:
        try:
            line_index = index_lines(fetch_data(args.fixture))  # one request per cron run
        except Exception as exc:
            error = f'Could not fetch or parse Metro line status: {exc}'
            print(error, file=sys.stderr)

    had_failures = False
    for recipient, (slot, subscribed_lines) in due.items():
        # Only send an email when at least one line has an alert or an error.
        names = list(dict.fromkeys(name.strip() for name in subscribed_lines))
        subject = f"Your {datetime.strptime(slot, '%H:%M').strftime('%I:%M %p').lstrip('0')} train alerts for {now.strftime('%d-%m-%Y')}"
        blocks = []
        html_sections = []

        if args.test_email:
            entries = ['Email delivery test — no Metro status checked.']
            blocks.append(format_line_section('TEST', entries))
            html_sections.append(('TEST', entries))
        else:
            for name in names:
                line = line_index.get(name.casefold()) if not error else None
                display_name = line['line_name'] if line else name
                if error:
                    entries = [f"Status unavailable: {error}"]
                    had_failures = True
                elif line is None:
                    entries = [(f"Configuration error: '{name}' is not a recognised Metro train line.\n"
                                "Please correct this line in your subscription configuration.\n"
                                "Available lines: "
                                + ', '.join(sorted(v['line_name'] for v in line_index.values())))]
                    had_failures = True
                else:
                    try:
                        entries = summaries(line, now, end, config.get('include_planned_works', True))
                    except Exception as exc:
                        entries = [f"Status unavailable: could not interpret Metro data for this line ({exc})."]
                        had_failures = True
                if entries:
                    blocks.append(format_line_section(display_name, entries))
                    html_sections.append((display_name, entries))

        if not blocks:
            print(f'{recipient}: no relevant alerts; no email sent')
            continue

        body = (
            "METRO TRAIN ALERTS\n"
            "==================\n"
            f"{now:%A %d %B %Y}\n"
            f"Travel window: {now:%I:%M %p} – {end:%I:%M %p} (Melbourne)\n\n"
            + ("\n\n" + "=" * 42 + "\n\n").join(blocks)
            + "\n\n" + "-" * 42
            + "\nSource: Metro Trains Melbourne\n" + API + "\n"
        )
        html_body = build_html_email(now, end, html_sections)
        if args.dry_run:
            print(f'\n--- To: {recipient} ---\nSubject: {subject}\n\n{body}\n')
        else:
            try:
                send_email(sender, recipient, password, subject, body, html_body)
                print(f'{recipient}: sent {subject}')
            except Exception as exc:
                had_failures = True
                print(f'{recipient}: email failed: {exc}', file=sys.stderr)

    return 1 if had_failures else 0


if __name__ == '__main__':
    sys.exit(main())
