from __future__ import annotations
import os, re, sqlite3, secrets, time, math, unicodedata, threading
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from flask import Blueprint, request, jsonify
from zoneinfo import ZoneInfo
from demo_assistant import answer as conversation_answer

PARIS = ZoneInfo("Europe/Paris")
def now_paris(): return datetime.now(PARIS)

portfolio_demo = Blueprint('portfolio_demo', __name__)
DB = Path(os.environ.get('YC_PORTFOLIO_DEMO_DB_PATH', Path(__file__).parent / 'portfolio-demo.sqlite'))
CAPACITY = 8
_rate = defaultdict(list)
_rate_lock = threading.Lock()

@contextmanager
def connection():
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, token TEXT NOT NULL, client_key TEXT UNIQUE NOT NULL, day TEXT NOT NULL, slot TEXT NOT NULL, party INTEGER NOT NULL, first_name TEXT NOT NULL, created REAL NOT NULL)')
    conn.execute('CREATE INDEX IF NOT EXISTS reservations_slot ON reservations(day,slot)')
    conn.execute('DELETE FROM reservations WHERE created < ?', (time.time()-86400,))
    try:
        yield conn
    finally:
        conn.close()

def validate(day, party, service):
    if type(party) is not int or not 1 <= party <= 6 or service not in ('lunch','dinner'):
        raise ValueError('Choisissez entre 1 et 6 convives et un service valide.')
    if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
        raise ValueError('Date invalide.')
    parsed = date.fromisoformat(day)
    if parsed < now_paris().date() or parsed > now_paris().date()+timedelta(days=30):
        raise ValueError('Choisissez un jour dans les 30 prochains jours.')
    return parsed

def slots_for(day, party, service, conn):
    parsed=validate(day,party,service)
    if parsed.weekday() in (0,6): return []
    times=('12:00','12:30','13:00') if service=='lunch' else ('19:00','19:30','20:00','20:30')
    used=dict(conn.execute('SELECT slot, SUM(party) FROM reservations WHERE day=? GROUP BY slot',(day,)).fetchall())
    result=[]
    for i,slot in enumerate(times):
        left=max(0,CAPACITY-used.get(slot,0))
        blocked=(parsed.day+i)%4==0 or (party>4 and i==1) or (parsed==now_paris().date() and slot<=now_paris().strftime('%H:%M'))
        result.append({'time':slot,'available':not blocked and left>=party,'remaining':0 if blocked else left})
    return result

def guard():
    origin=request.headers.get('Origin')
    if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
        return jsonify(error='Origine non autorisée.'),403
    with _rate_lock:
        now=time.time(); key=request.remote_addr or 'local'
        stale=[ip for ip,times in _rate.items() if not times or times[-1]<now-60]
        for ip in stale: _rate.pop(ip,None)
        if key not in _rate and len(_rate)>=1024:
            return jsonify(error='Le guide est très sollicité. Réessayez dans une minute.'),429
        _rate[key]=[t for t in _rate[key] if t>now-60]
        if len(_rate[key])>=20: return jsonify(error='Merci d’attendre une minute avant de réessayer.'),429
        _rate[key].append(now)
    return None

@portfolio_demo.get('/api/portfolio-demo/slots')
def slots():
    try:
        with connection() as conn:
            rows=slots_for(request.args.get('day',''),int(request.args.get('party','2')),request.args.get('service','dinner'),conn)
        return jsonify(slots=rows,demo=True,storage='Base de démonstration sur ce serveur')
    except (ValueError,TypeError): return jsonify(error='Date, service ou nombre de convives invalide.'),400
    except sqlite3.Error: return jsonify(error='Le calendrier de démonstration est momentanément indisponible.'),503

@portfolio_demo.post('/api/portfolio-demo/reservations')
def create():
    blocked=guard()
    if blocked: return blocked
    body=request.get_json(silent=True)
    if not isinstance(body,dict): return jsonify(error='Demande invalide.'),400
    day,slot,party,service=body.get('day'),body.get('slot'),body.get('party'),body.get('service')
    name=body.get('first_name'); client_key=body.get('client_key')
    if body.get('demo_ack') is not True or not isinstance(name,str) or not 2 <= len(name.strip()) <= 40 or not isinstance(client_key,str) or not re.fullmatch(r'[A-Za-z0-9-]{20,80}',client_key):
        return jsonify(error='Utilisez un prénom fictif et confirmez la démonstration.'),400
    try:
        validate(day,party,service)
        with connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous=conn.execute('SELECT * FROM reservations WHERE client_key=?',(client_key,)).fetchone()
            if previous:
                if (previous['day'],previous['slot'],previous['party']) != (day,slot,party):
                    conn.execute('ROLLBACK');return jsonify(error='Ce récapitulatif a déjà été utilisé. Recommencez le parcours.'),409
                conn.execute('COMMIT');return jsonify(id=previous['id'],token=previous['token'],day=day,slot=slot,party=party,demo=True,reused=True)
            if not any(row['time']==slot and row['available'] for row in slots_for(day,party,service,conn)):
                conn.execute('ROLLBACK');return jsonify(error='Ce créneau ne peut plus accueillir ce groupe. Choisissez un autre moment.'),409
            identifier=secrets.token_hex(8);token=secrets.token_urlsafe(24)
            conn.execute('INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?)',(identifier,token,client_key,day,slot,party,name.strip(),time.time()))
            conn.execute('COMMIT')
        return jsonify(id=identifier,token=token,day=day,slot=slot,party=party,demo=True),201
    except (ValueError,TypeError): return jsonify(error='Vérifiez la date, le service et les convives.'),400
    except sqlite3.Error: return jsonify(error='La base de démonstration est momentanément indisponible.'),503

@portfolio_demo.delete('/api/portfolio-demo/reservations/<identifier>')
def cancel(identifier):
    blocked=guard()
    if blocked: return blocked
    body=request.get_json(silent=True) or {}
    token=body.get('token') if isinstance(body,dict) else None
    if not isinstance(token,str):return jsonify(error='Confirmation manquante.'),400
    try:
        with connection() as conn:
            row=conn.execute('SELECT token FROM reservations WHERE id=?',(identifier,)).fetchone()
            if not row or not secrets.compare_digest(row['token'],token):return jsonify(error='Réservation introuvable.'),404
            conn.execute('DELETE FROM reservations WHERE id=?',(identifier,))
        return jsonify(cancelled=True,demo=True)
    except sqlite3.Error:
        return jsonify(error='La base de démonstration est momentanément indisponible.'),503

def faq_answer(question, context=None, state=None):
    return conversation_answer(question, context, state, today=now_paris().date())

@portfolio_demo.post('/api/portfolio-demo/assistant')
def assistant():
    blocked=guard()
    if blocked:return blocked
    if request.content_length and request.content_length>8192:return jsonify(error='Message trop volumineux.'),413
    body=request.get_json(silent=True)
    if not isinstance(body,dict) or not isinstance(body.get('question'),str) or not 2<=len(body['question'].strip())<=500:return jsonify(error='Posez une question entre 2 et 500 caractères.'),400
    return jsonify(faq_answer(body['question'].strip(), body.get('context'), body.get('state')))
