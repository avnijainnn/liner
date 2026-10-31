"""Client report and agency metrics. Rule: always show the denominator."""
from __future__ import annotations

import io
import statistics
from datetime import timedelta

from .db import Database, iso, loads, now, parse_iso

TRANSCRIPT_TYPES = ('forbidden_ask', 'unauthorized_promise', 'empty_promise', 'missed_handoff',
                    'false_success', 'unrequested_booking', 'action_not_allowed', 'wrong_information', 'ungrounded_answer',
                    'duplicate_effect', 'retry_risk')


def client_report(db: Database, client_id: int, days: int = 30) -> dict:
    since = iso(now() - timedelta(days=days))
    client = db.one('SELECT * FROM clients WHERE id=?', client_id)
    calls = db.all("""SELECT c.*, s.coverage, s.manifest_sha256 AS bound_sha, s.binding_status, s.drift_class
                      FROM calls c JOIN call_state s ON s.call_id=c.id
                      WHERE c.client_id=? AND c.source<>'trial' AND c.received_at>=?""", client_id, since)
    ids = [c['id'] for c in calls] or [-1]
    q = ','.join('?' * len(ids))
    fs = db.all(f'SELECT * FROM findings WHERE call_id IN ({q})', *ids)
    bookings = [f for f in fs if f['type'] == 'booking']
    tx = [f for f in fs if f['type'] in TRANSCRIPT_TYPES]
    cs = db.all(f'SELECT * FROM cases WHERE finding_id IN ({",".join("?" * len(fs)) or "-1"})', *([f['id'] for f in fs] or []))
    versions = db.all('SELECT v.*, a.approver_name, a.role, a.method, a.approved_at, e.key_id FROM manifest_versions v '
                      'LEFT JOIN approvals a ON a.manifest_version_id=v.id LEFT JOIN signature_envelopes e ON e.manifest_version_id=v.id '
                      'WHERE v.client_id=? ORDER BY v.version DESC', client_id)
    by_version = []
    for v in versions:
        vc = [c for c in calls if c['bound_sha'] == v['sha256']]
        vf = [f for f in fs if f['manifest_sha256'] == v['sha256']]
        by_version.append({'version': v['version'], 'sha256': v['sha256'], 'approver': v['approver_name'], 'role': v['role'],
                           'method': v['method'], 'approved_at': v['approved_at'], 'agent_version': v['approved_agent_version'],
                           'key_id': v['key_id'], 'calls': len(vc),
                           'violations_confirmed': sum(1 for f in vf if f['type'] in TRANSCRIPT_TYPES and f['state'] == 'confirmed'),
                           'bookings_verified': sum(1 for f in vf if f['type'] == 'booking' and f['state'] == 'verified'),
                           'bookings': sum(1 for f in vf if f['type'] == 'booking')})
    checkable = [b for b in bookings if b['state'] not in ('superseded',)]
    cov = {}
    for c in calls:
        cov[c['coverage'] or 'pending'] = cov.get(c['coverage'] or 'pending', 0) + 1
    return {
        'client': client, 'days': days, 'since': since,
        'calls': len(calls),
        'calls_no_transcript': cov.get('no_transcript', 0),
        'calls_unbound': cov.get('unknown_manifest', 0),
        'calls_checked': sum(v for k, v in cov.items() if k not in ('no_transcript', 'unknown_manifest', 'pending')),
        'coverage': cov,
        'bookings_checkable': len(checkable),
        'bookings_verified': sum(1 for b in checkable if b['state'] == 'verified'),
        'bookings_contradicted': sum(1 for b in checkable if b['state'] == 'contradicted'),
        'bookings_unknown': sum(1 for b in checkable if b['state'] == 'unknown'),
        'bookings_pending': sum(1 for b in checkable if b['state'] == 'pending'),
        'bookings_waived': sum(1 for b in checkable if b['state'] == 'waived'),
        'bookings_superseded': sum(1 for b in bookings if b['state'] == 'superseded'),
        'violations_flagged': len(tx),
        'violations_confirmed': sum(1 for f in tx if f['state'] == 'confirmed'),
        'violations_dismissed': sum(1 for f in tx if f['state'] == 'dismissed'),
        'violations_open': sum(1 for f in tx if f['state'] == 'open'),
        'cases': len(cs), 'cases_fixed': sum(1 for c in cs if c['fixed_at']),
        'cases_open': sum(1 for c in cs if c['status'] in ('open', 'fixed_awaiting_contact')),
        'drift_findings': sum(1 for f in fs if f['type'] == 'agent_drift'),
        'by_version': by_version,
        'confirmed_violations': [f for f in tx if f['state'] == 'confirmed'][:20],
    }


def agency_metrics(db: Database, agency_id: int, days: int = 30) -> dict:
    since = iso(now() - timedelta(days=days))
    fs = db.all("""SELECT f.* FROM findings f JOIN calls c ON c.id=f.call_id
                   WHERE f.agency_id=? AND c.source<>'trial' AND f.created_at>=?""", agency_id, since)
    tx = [f for f in fs if f['type'] in TRANSCRIPT_TYPES]
    reviewed = [f for f in tx if f['state'] in ('confirmed', 'dismissed')]
    bookings = [f for f in fs if f['type'] == 'booking' and f['state'] != 'superseded']
    decided = [b for b in bookings if b['state'] in ('verified', 'contradicted')]
    cs = db.all("SELECT k.*, c.ended_at FROM cases k JOIN findings f ON f.id=k.finding_id JOIN calls c ON c.id=f.call_id "
                "WHERE k.agency_id=? AND c.source<>'trial' AND k.created_at>=?", agency_id, since)
    fix_hours = [(parse_iso(c['fixed_at']) - parse_iso(c['ended_at'])).total_seconds() / 3600 for c in cs if c['fixed_at'] and c['ended_at']]
    trials = db.all("SELECT * FROM trials WHERE agency_id=? AND status='done' AND created_at>=?", agency_id, since)
    yielded = sum(1 for t in trials if loads(t['summary'], {}).get('failed') or loads(t['summary'], {}).get('flaky'))
    appr = db.all('SELECT role FROM approvals WHERE agency_id=?', agency_id)
    disputed = db.val("SELECT COUNT(DISTINCT case_id) FROM case_events WHERE agency_id=? AND action='disputed'", agency_id) or 0
    real_cases = len([c for c in cs]) or 0
    return {
        'precision': {'num': sum(1 for f in reviewed if f['state'] == 'confirmed') + max(real_cases - disputed, 0),
                      'den': len(reviewed) + real_cases},
        'coverage': {'num': len(decided), 'den': len(bookings)},
        'time_to_fix_h': round(statistics.median(fix_hours), 1) if fix_hours else None,
        'fixed': len(fix_hours),
        'trial_yield': {'num': yielded, 'den': len(trials)},
        'client_approved': {'num': sum(1 for a in appr if a['role'] == 'client'), 'den': len(appr)},
        'unauthorized_writes': 0,
        'broken_found_fixed': sum(1 for c in cs if c['status'] in ('closed_verified', 'fixed_awaiting_contact')),
    }


def pct(m: dict) -> str:
    return f"{round(100 * m['num'] / m['den'])}%" if m['den'] else '—'


def report_pdf(rep: dict, agency_name: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Vaada report — {rep['client']['name']}")
    ss = getSampleStyleSheet()
    h = ParagraphStyle('h', parent=ss['Title'], fontName='Times-Roman', fontSize=24, leading=28, alignment=0, spaceAfter=4)
    sub = ParagraphStyle('s', parent=ss['Normal'], textColor=colors.HexColor('#6b665c'), fontSize=9.5)
    h2 = ParagraphStyle('h2', parent=ss['Heading2'], fontName='Times-Roman', fontSize=14, spaceBefore=10)
    body = ParagraphStyle('b', parent=ss['Normal'], fontSize=9.5, leading=13)
    c = rep['client']
    el = [Paragraph(f"{c['name']}", h),
          Paragraph(f"AI phone agent report · last {rep['days']} days · prepared by {agency_name} with Vaada", sub), Spacer(1, 8)]
    rows = [
        ['Calls received', str(rep['calls'])],
        ['Calls checked', f"{rep['calls_checked']} of {rep['calls']}  ({rep['calls_no_transcript']} had no usable transcript, {rep['calls_unbound']} had no approved manifest in force)"],
        ['Bookings verified in the calendar', f"{rep['bookings_verified']} of {rep['bookings_checkable']} checkable"],
        ['Bookings found wrong', f"{rep['bookings_contradicted']}   ·   unknown: {rep['bookings_unknown']}   ·   still checking: {rep['bookings_pending']}"],
        ['Rule violations confirmed', f"{rep['violations_confirmed']} of {rep['violations_flagged']} flagged ({rep['violations_dismissed']} dismissed after review)"],
        ['Cases fixed and re-verified', f"{rep['cases_fixed']} of {rep['cases']}"],
        ['Agent changed since approval', f"{rep['drift_findings']} drift finding(s)"],
    ]
    t = Table(rows, colWidths=[58 * mm, 116 * mm])
    t.setStyle(TableStyle([('FONT', (0, 0), (-1, -1), 'Helvetica', 9.5), ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#6b665c')),
                           ('LINEBELOW', (0, 0), (-1, -1), 0.4, colors.HexColor('#e4dfd3')), ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                           ('BOTTOMPADDING', (0, 0), (-1, -1), 6), ('TOPPADDING', (0, 0), (-1, -1), 6)]))
    el += [t, Paragraph('Manifest versions and who approved them', h2)]
    vr = [['Version', 'Approved by', 'Agent', 'Calls', 'Bookings verified', 'Hash']]
    for v in rep['by_version']:
        vr.append([f"v{v['version']}", f"{v['approver']} ({'client' if v['role'] == 'client' else 'agency on client’s behalf'})",
                   f"v{v['agent_version']}", str(v['calls']), f"{v['bookings_verified']} / {v['bookings']}", v['sha256'][:12] + '…'])
    t2 = Table(vr, colWidths=[16 * mm, 62 * mm, 16 * mm, 14 * mm, 30 * mm, 36 * mm])
    t2.setStyle(TableStyle([('FONT', (0, 0), (-1, -1), 'Helvetica', 8.5), ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8.5),
                            ('LINEBELOW', (0, 0), (-1, -1), 0.4, colors.HexColor('#e4dfd3'))]))
    el += [t2, Spacer(1, 10)]
    if rep['confirmed_violations']:
        el.append(Paragraph('Confirmed rule violations', h2))
        for f in rep['confirmed_violations']:
            q = (loads(f['span'], {}) or {}).get('quote', '')
            el.append(Paragraph(f"<b>{f['type'].replace('_', ' ').title()}</b> — manifest v{f['manifest_version']}: “{q[:180]}”", body))
    el += [Spacer(1, 12), Paragraph('Hashes and approvals give tamper evidence inside Vaada; they are not an independently auditable public log. '
                                     'Unknown means Vaada did not have enough evidence to decide, never that a booking was verified.', sub)]
    doc.build(el)
    return buf.getvalue()
