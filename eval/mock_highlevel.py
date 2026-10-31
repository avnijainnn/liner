"""A local stand-in for the HighLevel v2 API, for connector tests. Not part of the product.

Serves only the read endpoints Vaada's HighLevelCRM calls, with the same paths, auth (Bearer token +
Version header) and response envelopes. Data comes from the JSON file in MOCK_HL_DATA, re-read on every
request so a test harness can change records between reads (delayed writes, fixes).

  uvicorn eval.mock_highlevel:app --port 8799
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

TOKEN = os.environ.get('MOCK_HL_TOKEN', 'hl-test-token')
DATA = Path(os.environ.get('MOCK_HL_DATA', 'mock_hl.json'))
LOG = []


def world():
    try:
        return json.loads(DATA.read_text())
    except (OSError, ValueError):
        return {'locations': {}, 'contacts': {}, 'appointments': {}, 'tasks': {}, 'calendars': []}


def guard(request: Request):
    LOG.append(request.url.path)
    if request.headers.get('authorization') != f'Bearer {TOKEN}':
        return JSONResponse({'message': 'Invalid JWT'}, 401)
    if not request.headers.get('version'):
        return JSONResponse({'message': 'Version header is required'}, 400)
    return None


def appointment(request: Request):
    if (e := guard(request)):
        return e
    a = world()['appointments'].get(request.path_params['id'])
    return JSONResponse({'appointment': a}) if a else JSONResponse({'message': 'Appointment not found'}, 404)


def contact(request: Request):
    if (e := guard(request)):
        return e
    c = world()['contacts'].get(request.path_params['id'])
    return JSONResponse({'contact': c}) if c else JSONResponse({'message': 'Contact not found'}, 404)


def contact_appointments(request: Request):
    if (e := guard(request)):
        return e
    cid = request.path_params['id']
    return JSONResponse({'events': [a for a in world()['appointments'].values() if a.get('contactId') == cid]})


def contact_tasks(request: Request):
    if (e := guard(request)):
        return e
    return JSONResponse({'tasks': world().get('tasks', {}).get(request.path_params['id'], [])})


def search_contacts(request: Request):
    if (e := guard(request)):
        return e
    q = ''.join(ch for ch in request.query_params.get('query', '') if ch.isdigit())
    loc = request.query_params.get('locationId')
    out = [c for c in world()['contacts'].values() if c.get('locationId') == loc and q and q in ''.join(ch for ch in c.get('phone', '') if ch.isdigit())]
    return JSONResponse({'contacts': out})


def conversations(request: Request):
    if (e := guard(request)):
        return e
    return JSONResponse({'conversations': []})


def location(request: Request):
    if (e := guard(request)):
        return e
    loc = world()['locations'].get(request.path_params['id'])
    return JSONResponse({'location': loc}) if loc else JSONResponse({'message': 'Location not found'}, 404)


def calendars(request: Request):
    if (e := guard(request)):
        return e
    loc = request.query_params.get('locationId')
    return JSONResponse({'calendars': [c for c in world()['calendars'] if c.get('locationId') == loc]})


def stats(request: Request):
    return JSONResponse({'requests': len(LOG), 'paths': sorted(set(p.rsplit('/', 1)[0] for p in LOG))})


app = Starlette(routes=[
    Route('/calendars/events/appointments/{id}', appointment),
    Route('/calendars/', calendars),
    Route('/contacts/', search_contacts),
    Route('/contacts/{id}/appointments', contact_appointments),
    Route('/contacts/{id}/tasks', contact_tasks),
    Route('/contacts/{id}', contact),
    Route('/conversations/search', conversations),
    Route('/locations/{id}', location),
    Route('/__stats', stats),
])
