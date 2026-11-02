from __future__ import annotations
import json, os, re, sqlite3, time, hashlib, uuid, secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from contextlib import contextmanager
from fastapi import FastAPI, HTTPException, Query, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
with open(ROOT / 'data' / 'taxonomy.json', encoding='utf-8') as f: TAXONOMY = json.load(f)
with open(ROOT / 'data' / 'rules.json', encoding='utf-8') as f: RULES_RAW = json.load(f)
RULE_ENTRIES: dict[str, list[dict[str, str]]] = {}
for section in RULES_RAW['rules']:
    for point in section.get('points', []):
        if point.get('ref'):
            RULE_ENTRIES.setdefault(point['ref'], []).append({'text': point['text'], 'section': section['category']})
# A simple ref->entry map is retained for existence checks only. Refs are NOT globally unique.
RULES = {ref: entries[0] for ref, entries in RULE_ENTRIES.items()}

# Knowledge centre: unique identifiers include the SOURCE CHAPTER, because
# original paragraph refs such as 1.1 occur across unrelated chapters.
# Keep RULE_ENTRIES untouched: triage citations still use original references.
RULE_CATALOG: list[dict[str, Any]] = []
for chapter in RULES_RAW['rules']:
    chapter_id = int(chapter['id'])
    section_name = chapter['category']
    for ordinal, point in enumerate(chapter.get('points', []), start=1):
        ref = point.get('ref', '').strip()
        if not ref: continue
        RULE_CATALOG.append({
            'id': f'{chapter_id}:{ref}:{ordinal}', 'ref': ref,
            'section_id': chapter_id, 'section': section_name,
            'text': point['text'], 'kind': 'rule',
            'display_ref': f'{section_name} · п. {ref}',
        })
    # The source has an RP glossary with no numbered 'points'. Show its actual
    # bullet definitions without inventing rule numbers or RAG citations.
    if not chapter.get('points') and chapter.get('text'):
        chunks = re.split(r'(?m)^\s*•\s*', chapter['text'])
        for ordinal, chunk in enumerate(chunks):
            cleaned = chunk.strip()
            if not cleaned: continue
            first = cleaned.splitlines()[0]
            term = re.split(r'\s+[—–-]\s+', first, maxsplit=1)[0].strip()
            RULE_CATALOG.append({
                'id': f'{chapter_id}:glossary:{ordinal}', 'ref': term,
                'section_id': chapter_id, 'section': section_name,
                'text': cleaned, 'kind': 'term',
                'display_ref': f'{section_name} · {term}',
            })

CATEGORY_META = {c['code']: c for c in TAXONOMY['categories']}
HUMAN_ALWAYS = set(TAXONOMY.get('needs_human_always', []))

# Optional local ML model. If it is absent or incompatible, the service safely
# falls back to the deterministic keyword baseline below.
MODEL_PATH = ROOT / 'models' / 'complaint_category.joblib'
CATEGORY_MODEL = None
MODEL_METADATA = {}
try:
    import joblib
    if MODEL_PATH.exists():
        _bundle = joblib.load(MODEL_PATH)
        CATEGORY_MODEL = (_bundle['vectorizer'], _bundle['model'])
        _meta_path = ROOT / 'models' / 'model_metadata.json'
        if _meta_path.exists():
            with open(_meta_path, encoding='utf-8') as _mf:
                MODEL_METADATA = json.load(_mf)
except Exception as _model_error:
    CATEGORY_MODEL = None
    MODEL_METADATA = {'load_error': str(_model_error)}


# The additional weak-label priority model augments triage only. Explicit
# taxonomy policy and high-risk escalation always override its predictions.
PRIORITY_MODEL = None
PRIORITY_METADATA = {}
try:
    _priority_path = ROOT / 'models' / 'priority_model.joblib'
    if _priority_path.exists():
        _priority_bundle = joblib.load(_priority_path)
        PRIORITY_MODEL = (_priority_bundle['vectorizer'], _priority_bundle['model'])
        _priority_meta_path = ROOT / 'models' / 'priority_model_metadata.json'
        if _priority_meta_path.exists():
            with open(_priority_meta_path, encoding='utf-8') as _mf:
                PRIORITY_METADATA = json.load(_mf)
except Exception as _priority_error:
    PRIORITY_MODEL = None
    PRIORITY_METADATA = {'load_error': str(_priority_error)}

app = FastAPI(title='Grand Mobile Support Copilot', version='1.1.0', description='Integration-ready triage, rules retrieval and safe reply drafting.')
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173','http://127.0.0.1:5173','http://localhost:4173','http://127.0.0.1:4173'], allow_methods=['GET','POST','PATCH','DELETE'], allow_headers=['Content-Type','Authorization','X-Copilot-Token'])
# Local single-user mode needs no credentials. For non-local deployments set
# COPILOT_API_TOKEN on the backend and enter the token in the interface.
API_TOKEN = os.getenv('COPILOT_API_TOKEN', '').strip()

DB_PATH = os.getenv('COPILOT_DB', str(ROOT / 'copilot.sqlite3'))
# Auth is enabled by default. A local first-run admin can be created in the UI.
# COPILOT_AUTH_ENABLED=off is for isolated development only.
AUTH_MODE = os.getenv('COPILOT_AUTH_ENABLED', 'on').strip().lower()
LOCAL_SETUP_ENABLED = os.getenv('COPILOT_LOCAL_SETUP_ENABLED', '1').strip().lower() not in {'0','false','off'}
SESSION_HOURS = 12
LOGIN_ATTEMPTS: dict[str, tuple[int, float]] = {}
DUMMY_HASH = 'pbkdf2_sha256$390000$' + ('00'*16) + '$' + ('00'*32)


def auth_db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        full_name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'operator', active INTEGER NOT NULL DEFAULT 1,
        password_hash TEXT NOT NULL, preferences TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS auth_sessions (
        token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        expires_at REAL NOT NULL, created_at TEXT NOT NULL)''')
    con.execute('CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id)')
    con.commit()
    return con


@contextmanager
def auth_connection():
    con = auth_db()
    try:
        with con:
            yield con
    finally:
        con.close()

def account_count() -> int:
    with auth_connection() as con:
        return con.execute('SELECT COUNT(*) FROM users').fetchone()[0]


def accounts_required() -> bool:
    if AUTH_MODE in {'0', 'false', 'off'}:
        return False
    if AUTH_MODE in {'1', 'true', 'on'}:
        return True
    return account_count() > 0


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 390_000)
    return 'pbkdf2_sha256$390000$' + salt.hex() + '$' + key.hex()


def check_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt, expected = encoded.split('$')
        if scheme != 'pbkdf2_sha256' or not 200_000 <= int(iterations) <= 1_000_000:
            return False
        got = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), int(iterations))
        return secrets.compare_digest(got, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def public_user(row: sqlite3.Row) -> dict:
    return {'id': row['id'], 'username': row['username'], 'full_name': row['full_name'],
            'email': row['email'], 'role': row['role'], 'active': bool(row['active']),
            'preferences': json.loads(row['preferences'] or '{}'), 'created_at': row['created_at']}


def token_user(token: str) -> dict | None:
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    with auth_connection() as con:
        row = con.execute('''SELECT u.* FROM auth_sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>? AND u.active=1''', (digest, time.time())).fetchone()
    return public_user(row) if row else None


def bearer_from(request: Request) -> str:
    raw = request.headers.get('Authorization', '')
    return raw[7:].strip() if raw.lower().startswith('bearer ') else ''


def require_user(request: Request) -> dict:
    user = token_user(bearer_from(request))
    if not user:
        raise HTTPException(401, 'Войди в аккаунт оператора')
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user['role'] != 'admin':
        raise HTTPException(403, 'Это действие доступно только администратору')
    return user


@app.middleware('http')
async def account_protection(request: Request, call_next):
    path = request.url.path
    public = path in {'/health', '/auth/status', '/auth/setup', '/auth/login', '/auth/logout',
                      '/docs', '/openapi.json', '/redoc'}
    actor = 'local'
    if request.method != 'OPTIONS' and not public:
        if accounts_required():
            actor = token_user(bearer_from(request))
            if not actor:
                return JSONResponse({'detail': 'Войди в аккаунт оператора'}, status_code=401)
            if actor['role'] == 'viewer' and request.method not in {'GET', 'HEAD'}:
                return JSONResponse({'detail': 'Режим наблюдателя: доступ только для чтения'}, status_code=403)
            if actor['role'] != 'admin' and request.method == 'DELETE':
                return JSONResponse({'detail': 'Удалять обращения может только администратор'}, status_code=403)
            actor = actor['username']
            # API-key mode can be used only while accounts are not enabled.
        elif API_TOKEN:
            provided = request.headers.get('X-Copilot-Token', '')
            if not secrets.compare_digest(provided, API_TOKEN):
                return JSONResponse({'detail': 'Необходим токен оператора (X-Copilot-Token)'}, status_code=401)
    request.state.actor = actor
    return await call_next(request)


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute('''CREATE TABLE IF NOT EXISTS tickets (id INTEGER PRIMARY KEY, reporter_hash TEXT, fingerprint TEXT, created_at TEXT, category TEXT, priority TEXT, payload TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, ticket_id TEXT, action TEXT, edited_reply TEXT, created_at TEXT)''')
    con.execute('''CREATE TABLE IF NOT EXISTS cases (id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL,
        attachments INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'new', category TEXT,
        priority TEXT, lang TEXT, confidence REAL, needs_human INTEGER, draft_reply TEXT NOT NULL DEFAULT '',
        result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    # Idempotent migrations: keep existing SQLite databases intact on upgrades.
    current_columns = {r[1] for r in con.execute('PRAGMA table_info(cases)')}
    for column, declaration in {
        'assignee': "TEXT NOT NULL DEFAULT ''",
        'resolution_note': "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in current_columns:
            con.execute(f'ALTER TABLE cases ADD COLUMN {column} {declaration}')
    con.execute('CREATE INDEX IF NOT EXISTS idx_cases_status_created ON cases(status,created_at DESC)')
    con.execute('''CREATE TABLE IF NOT EXISTS case_events (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
        action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, actor TEXT NOT NULL DEFAULT 'system')''')
    if 'actor' not in {r[1] for r in con.execute('PRAGMA table_info(case_events)')}:
        con.execute("ALTER TABLE case_events ADD COLUMN actor TEXT NOT NULL DEFAULT 'system'")
    return con

# Account onboarding and management. No public signup after the first admin.
class AccountSetup(BaseModel):
    username: str = Field(min_length=3, max_length=150, pattern=r'^(?:[A-Za-z0-9_.-]{3,32}|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})$')
    full_name: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=12, max_length=128)

class LoginData(BaseModel):
    username: str = Field(min_length=1,max_length=150)
    password: str = Field(min_length=1,max_length=128)

class ProfileData(BaseModel):
    full_name: str = Field(min_length=2, max_length=80)
    email: str = Field(default='', max_length=150)

class PasswordData(BaseModel):
    current_password: str
    new_password: str = Field(min_length=12, max_length=128)

class PreferencesData(BaseModel):
    theme: str = Field(default='dark', pattern='^(dark|light)$')
    compact_queue: bool = False
    auto_refresh: bool = True
    notifications: bool = True

class UserCreate(AccountSetup):
    role: str = Field(default='operator', pattern='^(admin|operator|viewer)$')
    email: str = Field(default='', max_length=150)

class UserEdit(BaseModel):
    role: str | None = Field(default=None, pattern='^(admin|operator|viewer)$')
    active: bool | None = None


def issue_session(con, user_id: str) -> str:
    token = secrets.token_urlsafe(40)
    con.execute('INSERT INTO auth_sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)',
                (hashlib.sha256(token.encode()).hexdigest(), user_id, time.time()+SESSION_HOURS*3600, now_iso()))
    return token


def insert_user(con, data: AccountSetup, role: str = 'admin', email: str = '') -> dict:
    user_id = str(uuid.uuid4())
    stamp = now_iso()
    try:
        con.execute('''INSERT INTO users(id,username,full_name,email,role,active,password_hash,preferences,created_at,updated_at)
            VALUES(?,?,?,?,?,1,?,?,?,?)''', (user_id, data.username.strip(), data.full_name.strip(), email.strip(),
                role, password_hash(data.password), json.dumps({'theme':'dark','compact_queue':False,'auto_refresh':True,'notifications':True}), stamp,stamp))
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'Логин уже занят')
    return public_user(con.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone())


@app.get('/auth/status')
def auth_status():
    total = account_count()
    return {'enabled': accounts_required(), 'setup_required': total == 0 and AUTH_MODE not in {'0','false','off'},
            'local_setup_only': True, 'local_setup_enabled': LOCAL_SETUP_ENABLED, 'accounts': total}


@app.post('/auth/setup')
def setup_first_admin(data: AccountSetup, request: Request):
    # Never allow a remote stranger to race to create the initial administrator.
    # Docker / remote installs can use scripts/create_admin.py from the server.
    remote = (request.client.host if request.client else '')
    if not LOCAL_SETUP_ENABLED:
        raise HTTPException(403, 'Создай первого администратора через серверный скрипт bootstrap_cli.py')
    if remote not in {'127.0.0.1','::1','localhost','testclient'}:
        raise HTTPException(403,'Первичная настройка допускается только локально. Используй скрипт create_admin.py на сервере.')
    with auth_connection() as con:
        # BEGIN IMMEDIATE serializes setup to prevent multiple admins racing.
        con.execute('BEGIN IMMEDIATE')
        if con.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
            raise HTTPException(409, 'Первичный администратор уже создан')
        user = insert_user(con,data)
        token = issue_session(con,user['id'])
        con.commit()
    return {'user': user, 'access_token': token, 'expires_in': SESSION_HOURS*3600}


@app.post('/auth/login')
def auth_login(data: LoginData, request: Request):
    ip = request.client.host if request.client else 'unknown'
    key = ip + ':' + data.username.casefold()
    failures, until = LOGIN_ATTEMPTS.get(key, (0,0.0))
    if failures >= 5 and time.time() < until:
        raise HTTPException(429, 'Слишком много попыток. Попробуй позже.')
    with auth_connection() as con:
        user_row = con.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE',(data.username.strip(),)).fetchone()
        # Constant work even for non-existent usernames.
        valid = check_password(data.password, user_row['password_hash'] if user_row else DUMMY_HASH)
        if not user_row or not valid or not user_row['active']:
            failures += 1
            LOGIN_ATTEMPTS[key] = (failures, time.time()+900 if failures >= 5 else 0)
            raise HTTPException(401, 'Неверный логин или пароль')
        LOGIN_ATTEMPTS.pop(key, None)
        con.execute('DELETE FROM auth_sessions WHERE expires_at<?',(time.time(),))
        token = issue_session(con,user_row['id'])
        con.commit()
        return {'user': public_user(user_row), 'access_token':token, 'expires_in':SESSION_HOURS*3600}


@app.post('/auth/logout')
def auth_logout(request: Request):
    token = bearer_from(request)
    if token:
        with auth_connection() as con:
            con.execute('DELETE FROM auth_sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
            con.commit()
    return {'ok': True}


@app.get('/auth/me')
def auth_me(user: dict = Depends(require_user)):
    return {'user': user}


@app.get('/auth/activity')
def auth_activity(user: dict = Depends(require_user), limit: int = Query(default=12,ge=1,le=30)):
    con=db();con.row_factory=sqlite3.Row
    rows=con.execute('''SELECT case_id,action,details,created_at FROM case_events
        WHERE actor=? ORDER BY id DESC LIMIT ?''',(user['username'],limit)).fetchall()
    con.close()
    return {'events':[dict(x) for x in rows]}


@app.post('/auth/logout-all')
def auth_logout_all(user: dict = Depends(require_user)):
    with auth_connection() as con:
        deleted=con.execute('DELETE FROM auth_sessions WHERE user_id=?',(user['id'],)).rowcount
        con.commit()
    return {'ok':True,'sessions_revoked':deleted}


@app.patch('/auth/profile')
def auth_profile(payload: ProfileData, user: dict = Depends(require_user)):
    if payload.email and (not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', payload.email) or len(payload.email)>150):
        raise HTTPException(422, 'Некорректный email')
    with auth_connection() as con:
        con.execute('UPDATE users SET full_name=?,email=?,updated_at=? WHERE id=?',
                    (payload.full_name.strip(),payload.email.strip(),now_iso(),user['id']))
        con.commit()
        row = con.execute('SELECT * FROM users WHERE id=?',(user['id'],)).fetchone()
    return {'user':public_user(row)}


@app.patch('/auth/preferences')
def auth_preferences(payload: PreferencesData, user: dict = Depends(require_user)):
    with auth_connection() as con:
        con.execute('UPDATE users SET preferences=?,updated_at=? WHERE id=?',
                    (payload.model_dump_json(),now_iso(),user['id']))
        con.commit()
        row = con.execute('SELECT * FROM users WHERE id=?',(user['id'],)).fetchone()
    return {'user':public_user(row)}


@app.post('/auth/change-password')
def auth_change_password(payload: PasswordData, user: dict = Depends(require_user)):
    with auth_connection() as con:
        row = con.execute('SELECT password_hash FROM users WHERE id=?',(user['id'],)).fetchone()
        if not row or not check_password(payload.current_password,row['password_hash']):
            raise HTTPException(401, 'Неверный текущий пароль')
        if payload.current_password == payload.new_password:
            raise HTTPException(400, 'Новый пароль должен отличаться от текущего')
        con.execute('UPDATE users SET password_hash=?,updated_at=? WHERE id=?',
                    (password_hash(payload.new_password),now_iso(),user['id']))
        con.execute('DELETE FROM auth_sessions WHERE user_id=?',(user['id'],))
        con.commit()
    return {'ok':True,'reauth_required':True}


@app.get('/auth/users')
def auth_users(admin: dict = Depends(require_admin)):
    with auth_connection() as con:
        return {'users':[public_user(r) for r in con.execute('SELECT * FROM users ORDER BY created_at').fetchall()]}


@app.post('/auth/users')
def auth_create_user(payload: UserCreate, admin: dict = Depends(require_admin)):
    with auth_connection() as con:
        user=insert_user(con,payload,payload.role,payload.email)
        con.commit()
    return {'user':user}


@app.patch('/auth/users/{user_id}')
def auth_edit_user(user_id: str, payload: UserEdit, admin: dict = Depends(require_admin)):
    with auth_connection() as con:
        row=con.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
        if not row: raise HTTPException(404, 'Пользователь не найден')
        role = payload.role or row['role']
        active = bool(row['active']) if payload.active is None else payload.active
        if user_id == admin['id'] and (role != 'admin' or not active):
            raise HTTPException(400, 'Нельзя отключить собственную учётную запись администратора')
        if row['role']=='admin' and row['active'] and (role!='admin' or not active):
            other=con.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",(user_id,)).fetchone()[0]
            if other==0:raise HTTPException(400, 'Должен остаться хотя бы один активный администратор')
        con.execute('UPDATE users SET role=?, active=?,updated_at=? WHERE id=?',
                    (role,int(active),now_iso(),user_id))
        if not active: con.execute('DELETE FROM auth_sessions WHERE user_id=?',(user_id,))
        con.commit()
        row=con.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
    return {'user':public_user(row)}


class Message(BaseModel):
    role: str = 'player'
    text: str = ''
    attachments: int | None = None
    ts: str | None = None

class TriageRequest(BaseModel):
    ticket_id: str | None = None
    kind: str = 'report'
    messages: list[Message] = Field(default_factory=list)
    created_at: str | None = None
    reporter_hash: str | None = None
    reported_hash: str | None = None
    reported_nick: str | None = None
    rule_refs_raw: str | None = None
    images_count: int | None = None
    region: str | None = None
    server_lang: str | None = None
    lang: str | None = None
    save_to_queue: bool = False

class BatchRequest(BaseModel):
    items: list[TriageRequest] = Field(min_length=1,max_length=100)

class FeedbackRequest(BaseModel):
    ticket_id: str
    action: str
    edited_reply: str | None = None

class CaseUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None
    draft_reply: str | None = Field(default=None, max_length=12000)
    assignee: str | None = Field(default=None, max_length=80)
    resolution_note: str | None = Field(default=None, max_length=1500)

class CaseNote(BaseModel):
    text: str = Field(min_length=2, max_length=1500)

class BulkAction(BaseModel):
    case_ids: list[str] = Field(min_length=1, max_length=100)
    status: str | None = None
    assignee: str | None = Field(default=None, max_length=80)

VALID_STATUSES = {'new', 'in_progress', 'escalated', 'resolved'}
VALID_PRIORITIES = {'p0', 'p1', 'p2', 'p3'}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def case_dict(row):
    result = dict(row)
    result['needs_human'] = bool(result['needs_human'])
    result['result'] = json.loads(result.pop('result_json'))
    return result

KZ_MAP = str.maketrans({'ќ':'қ','ґ':'ғ','ѕ':'ң','є':'ә','ј':'ө','ў':'ұ','ї':'ү','Ќ':'Қ','Ґ':'Ғ','Ѕ':'Ң','Є':'Ә','Ј':'Ө','Ў':'Ұ','Ї':'Ү'})

def normalize(s: str) -> str:
    s = s.translate(KZ_MAP).lower().replace('ё','е')
    s = re.sub(r'https?://\S+|www\.\S+', ' URL ', s)
    s = re.sub(r'[^\w\s.]+', ' ', s, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', s).strip()

def collect_text(req: TriageRequest) -> str:
    return ' '.join(m.text for m in req.messages if m.role in ('player','reported_player') and m.text).strip()

def detect_lang(text: str, fallback: str | None = None) -> str:
    t = normalize(text)
    if fallback in {'kk','uz'}: return fallback
    if re.search(r'[әіңүұөһ]', t): return 'kk'
    if re.search(r'[ўҳҷӯӣ]', t): return 'uz'
    if re.search(r'[қғ]', t): return 'kk'
    if re.search(r'[а-я]', t): return 'ru'
    # Uzbek, Spanish and Portuguese are commonly written with Latin letters;
    # a trusted server/dataset language hint disambiguates them from English.
    if re.search(r'[a-z]', t) and fallback in {'ru', 'kk', 'uz', 'en', 'es', 'pt'}: return fallback
    if re.search(r'[a-z]', t): return 'en'
    return fallback if fallback in {'ru','kk','uz','en','es','pt'} else 'und'

KEYWORDS = {
 'complaint.insult': ['оскорб','мат','токсич','расизм','унижал','обзывал','insult','haqorat'],
 'complaint.cheat': ['чит','аим','aimbot','wallhack','софт','читы','flyhack','читы'],
 'complaint.dm': ['дм','dm','убил без','убийств','стрелял без','зеленой зоне','зеленой зоны','gz'],
 'complaint.scam': ['обман','мошенн','скам','не вернул','не отдал','украл','вымог'],
 'complaint.rmt_ads': ['реал','продам аккаунт','продает аккаунт','за деньги','донат за','rmt','реклама'],
 'complaint.admin': ['админ','администратор','модератор'],
 'complaint.invalid': ['как купить','как устроиться','помогите','ау','привет','вопрос','жб'],
 'report.question': ['как ','где ','сколько','устроиться','лиценз','работ','купить'],
 'report.help': ['застрял','застряла','текстур','телепорт','баг','вылетел','не могу двиг'],
 'report.complaint': ['оскорб','дм','убил','чит','мешает','наруш'],
 'report.account': ['взлом','угнали аккаунт','пропал','пропала','потерял доступ','не могу войти','аккаунт'],
 'report.payment': ['оплат','донат','не приш','покупк','платеж','платёж','голд','gold'],
 'report.offtopic': ['ау','привет','помогите','ку','йо']
}

def classify(kind: str, text: str) -> tuple[str,float,list[str]]:
    t = normalize(text)
    if kind == 'complaint' and CATEGORY_MODEL and t:
        try:
            vectorizer, model = CATEGORY_MODEL
            features = vectorizer.transform([t])
            predicted = str(model.predict(features)[0])
            if predicted in CATEGORY_META and CATEGORY_META[predicted].get('kind') == 'complaint':
                decision = model.decision_function(features)
                values = decision[0] if getattr(decision, 'ndim', 1) > 1 else [float(decision[0])]
                ranked = sorted((float(v) for v in values), reverse=True)
                gap = ranked[0] - ranked[1] if len(ranked) > 1 else max(0.0, ranked[0])
                confidence = min(.91, .52 + .40 * (1 - __import__('math').exp(-max(0.0, gap))))
                return predicted, confidence, [
                    'локальная ML-модель TF-IDF + LinearSVC',
                    'категории обучены на слабой разметке из violation, rule_refs_raw и verdict; требуется проверка на ручной эталонной выборке'
                ]
        except Exception:
            pass
    scores: dict[str,int] = {}
    for code, words in KEYWORDS.items():
        if CATEGORY_META.get(code,{}).get('kind') != kind: continue
        scores[code] = sum(2 if len(w) > 4 else 1 for w in words if w in t)
    if kind == 'complaint' and not t: return 'complaint.invalid', .42, ['пустой текст жалобы']
    if kind == 'report' and not t: return 'report.offtopic', .4, ['пустое сообщение']
    if not scores or max(scores.values()) == 0:
        return ('complaint.other' if kind == 'complaint' else 'report.question'), .48, ['неоднозначное содержание; требуется проверка']
    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    confidence = min(.94, .58 + .08 * ranked[0] + (.08 if len(ranked)==1 or ranked[0]>ranked[1] else 0))
    reasons = [f'ключевые признаки категории: {best}']
    if len(ranked)>1 and ranked[0]==ranked[1]: reasons.append('есть пересечение признаков нескольких категорий')
    return best, confidence, reasons

# Signs of critical-risk requests. Do not depend on the ML classifier.
CRITICAL_RE = re.compile(
    r"взлом|угнал.{0,15}аккаунт|потерял.{0,12}доступ|не могу войти|"
    r"оплатил|списал.{0,12}деньг|не приш.{0,15}(донат|голд)|"
    r"(донат|платеж|платёж|покупка).{0,20}не приш|"
    r"массов.{0,10}баг|угрож.{0,20}(вне игр|реальн)|"
    r"account (hacked|stolen)|lost access|payment (failed|missing)|"
    r"akkaunt.{0,16}(buz|o.gir)|to.lov.{0,16}(kelm|o.tm)",
    re.I
)
PAYMENT_RE = re.compile(
    r"донат|платеж|платёж|оплат|покупк.{0,16}(голд|game|gold)|"
    r"списал.{0,15}деньг|donat|payment|top.up|to.lov|pul.{0,10}yech", re.I
)
ACCOUNT_RE = re.compile(
    r"взлом|угнали аккаунт|потерял доступ|не могу войти|"
    r"восстанов.{0,15}аккаунт|account hacked|lost access|"
    r"akkaunt.{0,16}(buz|o.gir)", re.I
)
APPEAL_RE = re.compile(
    r"апелляц|разбан|снять бан|обжал|бан выдали|"
    r"несправедлив.{0,12}бан|unban|ban appeal|ban ochib", re.I
)

def override_report_category(kind: str, text: str) -> str | None:
    """Precision-first report overrides; avoid training noise for risky cases."""
    if kind != 'report': return None
    t = normalize(text)
    if ACCOUNT_RE.search(t):
        return 'report.account'
    if PAYMENT_RE.search(t):
        return 'report.payment'
    return None

def predicted_priority(kind: str, text: str, evidence: bool) -> str | None:
    if PRIORITY_MODEL is None or not text.strip(): return None
    try:
        vectorizer, model = PRIORITY_MODEL
        features = f"kind_{kind} evidence_{int(evidence)} " + normalize(text)
        pred = str(model.predict(vectorizer.transform([features]))[0])
        return pred if pred in {'p0','p1','p2','p3'} else None
    except Exception:
        return None

def priority_for(category: str, text: str, evidence: bool, kind: str = 'report') -> str:
    """ML-assisted, policy-first priority; never downgrade critical or high-risk cases."""
    t=normalize(text)
    if category in {'report.account','report.payment'} or CRITICAL_RE.search(t):
        return 'p0'
    if category in {'complaint.cheat','complaint.scam','complaint.rmt_ads'}:
        return 'p1'
    if category.startswith('complaint.') and evidence:
        return 'p1'
    if category in {'complaint.insult','complaint.dm','complaint.admin'}:
        return 'p2'
    candidate=predicted_priority(kind,text,evidence)
    # Weak-label predictions are used for the noncritical p2/p3 distinction.
    if candidate in {'p2','p3'}:
        return candidate
    if kind == 'report' and category == 'report.complaint':return 'p2'
    return 'p3'

def priority_reason(category: str, text: str, evidence: bool, kind: str = 'report') -> str:
    t=normalize(text)
    if category in {'report.account','report.payment'} or CRITICAL_RE.search(t):
        return 'Критический приоритет по обязательной политике: доступ, оплата или угроза.'
    if category in {'complaint.cheat','complaint.scam','complaint.rmt_ads'}:
        return 'Высокий приоритет по типу предполагаемого нарушения (правило приоритета).'
    if category.startswith('complaint.') and evidence:
        return 'Высокий приоритет: жалоба содержит вложения (правило приоритета).'
    if category in {'complaint.insult','complaint.dm','complaint.admin'}:
        return 'Обычный приоритет по типу обращения (правило приоритета).'
    if predicted_priority(kind,text,evidence) in {'p2','p3'}:
        return 'Оценка локальной ML-модели, обученной на слабой разметке; требует проверки.'
    return 'Резервная детерминированная оценка для некритичного обращения.'


def extract_refs(text: str, raw: str | None = None) -> list[str]:
    found = set(re.findall(r'(?<!\d)(\d+\.\d+(?:\.\d+)?)(?!\d)', (text or '')+' '+(raw or '')))
    return sorted(x for x in found if x in RULES)

def section_scope(category: str, text: str = '') -> str | None:
    # Taxonomy references are shorthand; select the intended chapter instead of
    # accidentally citing an identically numbered rule from another chapter.
    normalized = normalize(text)
    if category == 'complaint.dm' and any(x in normalized for x in ['зеленой зоне', 'зеленая зона', 'green zone', 'gz']):
        return 'Правила нахождения в Зелёной Зоне'
    if category == 'complaint.invalid': return 'Правила жалоб'
    if category.startswith('complaint.') or category.startswith('report.'):
        return 'Общие правила игры'
    return None

def resolve_rule(ref: str, category: str, text: str = '') -> dict[str, str] | None:
    entries = RULE_ENTRIES.get(ref, [])
    if not entries: return None
    scope = section_scope(category, text)
    if scope:
        match = next((entry for entry in entries if entry['section'] == scope), None)
        if match: return {'ref': ref, **match}
    # If an ID occurs in several chapters and no taxonomy scope resolves it,
    # omit it rather than attach a potentially false citation.
    if len(entries) == 1: return {'ref': ref, **entries[0]}
    return None

def retrieve_rules(text: str, refs: list[str], category: str, limit: int=3) -> list[dict[str,str]]:
    selected = []
    for ref in refs:
        rule = resolve_rule(ref, category, text)
        if rule and all(x['ref'] != ref for x in selected): selected.append(rule)
    if selected: return selected[:limit]
    meta = CATEGORY_META.get(category,{})
    for raw_ref in meta.get('rules',[]):
        ref = raw_ref.split()[-1] if raw_ref.startswith('ЖБ ') else raw_ref
        rule = resolve_rule(ref, category, text)
        if rule and all(x['ref'] != ref for x in selected): selected.append(rule)
    if selected: return selected[:limit]
    query_tokens = normalize(text).split()
    if not query_tokens:
        return []
    scope = section_scope(category, text)
    corpus = []
    for ref, entries in RULE_ENTRIES.items():
        for entry in entries:
            if scope and entry['section'] != scope:
                continue
            if len(entries) > 1 and not scope:
                continue
            tokens = normalize(entry['text']).split()
            if tokens:
                corpus.append((ref, entry, tokens))
    # Small dependency-free BM25 implementation. It ranks rule text by term
    # relevance while keeping ambiguous duplicate refs out of the result.
    if not corpus:
        return []
    from collections import Counter
    import math
    n_docs = len(corpus)
    avg_len = sum(len(tokens) for _, _, tokens in corpus) / n_docs
    df = Counter(token for _, _, tokens in corpus for token in set(tokens))
    query_counts = Counter(query_tokens)
    k1, b = 1.5, 0.75
    scored = []
    for ref, entry, tokens in corpus:
        tf = Counter(tokens)
        score = 0.0
        for token, qf in query_counts.items():
            if token not in tf:
                continue
            idf = math.log(1 + (n_docs - df[token] + 0.5) / (df[token] + 0.5))
            denom = tf[token] + k1 * (1 - b + b * len(tokens) / max(avg_len, 1))
            score += idf * (tf[token] * (k1 + 1) / denom) * min(qf, 2)
        if score > 0:
            scored.append((score, ref, entry))
    scored.sort(key=lambda item: item[0], reverse=True)
    seen = set()
    for _, ref, entry in scored:
        if ref not in seen:
            selected.append({'ref': ref, **entry})
            seen.add(ref)
        if len(selected) >= limit:
            break
    return selected[:limit]

def fingerprint(text: str) -> str:
    words = sorted(set(w for w in normalize(text).split() if len(w)>2))
    return hashlib.sha1(' '.join(words).encode()).hexdigest()

def token_signature(text: str) -> list[str]:
    # Hash tokens rather than storing raw ticket text or nicknames in the dedupe index.
    tokens = {w for w in normalize(text).split() if len(w) > 2}
    return sorted(hashlib.sha256(w.encode('utf-8')).hexdigest()[:12] for w in tokens)


def duplicate_check(req: TriageRequest, text: str) -> bool:
    # Same reporter + same/similar issue + 5 submissions within one hour.
    if not req.reporter_hash or not text:
        return False
    fp = fingerprint(text)
    current_tokens = set(token_signature(text))
    now = datetime.now(timezone.utc)
    if req.created_at:
        try:
            now = datetime.fromisoformat(req.created_at.replace('Z', '+00:00'))
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    con = db()
    rows = con.execute('SELECT fingerprint, created_at, payload FROM tickets WHERE reporter_hash=? ORDER BY id DESC LIMIT 30', (req.reporter_hash,)).fetchall()
    con.close()
    repeats = 0
    for old_fp, created, payload in rows:
        try:
            stamp = datetime.fromisoformat(created.replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if abs((now - stamp).total_seconds()) > 3600:
                continue
            old_tokens = set(json.loads(payload or '{}').get('token_sig', []))
            union = current_tokens | old_tokens
            similarity = len(current_tokens & old_tokens) / len(union) if union else 0.0
            if old_fp == fp or similarity >= 0.78:
                repeats += 1
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return repeats >= 4


def save_ticket(req: TriageRequest, text: str, category: str, priority: str):
    payload = json.dumps({'kind': req.kind, 'token_sig': token_signature(text)})
    con = db()
    con.execute('INSERT INTO tickets(reporter_hash,fingerprint,created_at,category,priority,payload) VALUES(?,?,?,?,?,?)',
                (req.reporter_hash, fingerprint(text), req.created_at or datetime.now(timezone.utc).isoformat(), category, priority, payload))
    con.commit()
    con.close()

def make_draft(lang: str, category: str, rules: list[dict[str,str]], nickname: str | None, human: bool, evidence: bool) -> dict[str,Any]:
    cited=[r['ref'] for r in rules]
    if lang == 'kk':
        if human: txt='Сәлеметсіз бе! Өтінішіңіз қабылданды және қосымша тексеру үшін маманға жіберіледі. Қосымша ақпарат қажет болса, сізбен байланысамыз.'
        else: txt='Сәлеметсіз бе! Өтінішіңіз қабылданды. Мамандар оны қолданыстағы ойын ережелеріне сәйкес тексереді.'
    elif lang == 'uz':
        if human: txt='Assalomu alaykum! Murojaatingiz qabul qilindi va qo‘shimcha tekshiruv uchun mutaxassisga yuborildi. Zarur bo‘lsa, sizdan qo‘shimcha ma’lumot so‘raymiz.'
        else: txt='Assalomu alaykum! Murojaatingiz qabul qilindi. Mutaxassislar vaziyatni amaldagi o‘yin qoidalariga muvofiq tekshiradi.'
    elif lang == 'en':
        if human: txt='Hello! Your request has been received and forwarded to a specialist for further review. We may contact you if more information is needed.'
        else: txt='Hello! Your request has been received. Our team will review the situation under the current game rules.'
    elif lang == 'es':
        if human: txt='¡Hola! Hemos recibido tu solicitud y la hemos enviado a un especialista para una revisión adicional. Si hace falta, te pediremos más información.'
        else: txt='¡Hola! Hemos recibido tu solicitud. Nuestro equipo revisará la situación de acuerdo con las reglas actuales del juego.'
    elif lang == 'pt':
        if human: txt='Olá! Sua solicitação foi recebida e encaminhada a um especialista para análise. Se necessário, pediremos informações adicionais.'
        else: txt='Olá! Sua solicitação foi recebida. Nossa equipe analisará a situação de acordo com as regras atuais do jogo.'
    else:
        if human: txt='Здравствуйте! Обращение принято и передано специалисту для дополнительной проверки. Решение по ситуации может принять только администратор.'
        elif category in {'report.question','report.help','report.offtopic'}: txt='Здравствуйте! Обращение принято. Уточните, пожалуйста, детали ситуации, чтобы мы могли помочь точнее.'
        elif category=='complaint.invalid' and not evidence: txt='Здравствуйте! Для рассмотрения жалобы нужны объективные доказательства нарушения. Пожалуйста, приложите скриншоты или видео, на которых видны обстоятельства и участники ситуации.'
        else: txt='Здравствуйте! Обращение принято. Ситуация будет проверена по действующим правилам игры. Окончательное решение принимает администрация.'
    refs=', '.join(cited)
    if rules:
        if lang == 'kk':
            suffix = f' Тексеру кезінде мына ережелер қолданылуы мүмкін: {refs}. Бұл алдын ала сілтеме, бұзушылық анықталды дегенді білдірмейді.' if not human else f' Тексеру үшін мына ережелер пайдалы болуы мүмкін: {refs}. Бұл бұзушылық анықталды дегенді білдірмейді.'
        elif lang == 'uz':
            suffix = f' Tekshiruvda quyidagi qoidalar qo‘llanishi mumkin: {refs}. Bu dastlabki havola bo‘lib, qoidabuzarlik tasdiqlandi degani emas.' if not human else f' Tekshiruv uchun quyidagi qoidalar foydali bo‘lishi mumkin: {refs}. Bu qoidabuzarlik tasdiqlandi degani emas.'
        elif lang == 'en':
            suffix = f' Rules that may be relevant: {refs}. This is a preliminary reference, not a finding of misconduct.'
        elif lang == 'es':
            suffix = f' Las reglas que podrían ser relevantes: {refs}. Esta referencia es preliminar y no confirma una infracción.'
        elif lang == 'pt':
            suffix = f' Regras possivelmente relevantes: {refs}. Esta referência é preliminar e não confirma uma infração.'
        else:
            suffix = (f' При проверке могут быть применимы пункты правил: {refs}. Это предварительная ссылка, а не подтверждение нарушения.' if not human else f' Для проверки могут быть полезны пункты правил: {refs}. Ссылка не означает, что нарушение уже установлено.')
        txt += suffix
    return {'lang':lang,'text':txt,'cited':cited}

@app.get('/health')
def health(): return {'status':'ok','service':'grand-support-copilot','version':'1.1.0','rules_loaded':len(RULES),'ml_model_loaded':CATEGORY_MODEL is not None,'priority_model_loaded':PRIORITY_MODEL is not None}

@app.get('/meta')
def meta(): return {'categories':TAXONOMY['categories'],'priorities':TAXONOMY['priorities'],'rules_count':len(RULES),'human_always':sorted(HUMAN_ALWAYS),'ml_model_loaded':CATEGORY_MODEL is not None,'priority_model_loaded':PRIORITY_MODEL is not None,'model_note':MODEL_METADATA.get('label_quality_note'),'priority_model_note':PRIORITY_METADATA.get('label_quality_note'),'model_metrics':{'weak_label_accuracy':MODEL_METADATA.get('weak_label_accuracy'),'weak_label_macro_f1':MODEL_METADATA.get('weak_label_macro_f1'),'training_rows':MODEL_METADATA.get('training_rows')},'priority_model_metrics':{'weak_label_accuracy':PRIORITY_METADATA.get('weak_label_accuracy'),'weak_label_macro_f1':PRIORITY_METADATA.get('weak_label_macro_f1'),'training_rows':PRIORITY_METADATA.get('training_rows')}}

def audit_actor(request: Request | None) -> str:
    return str(getattr(getattr(request, 'state', None), 'actor', 'local'))

@app.post('/triage')
def triage(req: TriageRequest, request: Request = None):
    started=time.perf_counter(); kind=req.kind if req.kind in {'complaint','report'} else 'report'
    text=collect_text(req); lang=detect_lang(text, req.lang if req.lang not in {'lat', 'und'} else req.server_lang)
    category, confidence, reasons=classify(kind,text)
    forced_category=override_report_category(kind,text)
    if forced_category:
        category=forced_category
        confidence=max(confidence,.95)
        reasons.append('обязательное правило маршрутизации: платёж или безопасность аккаунта')
    evidence=(req.images_count or 0)>0 or any((m.attachments or 0)>0 for m in req.messages)
    refs=extract_refs(text,req.rule_refs_raw)
    rules=[] if category in {'report.account','report.payment'} else retrieve_rules(text,refs,category)
    priority=priority_for(category,text,evidence,kind)
    duplicate=duplicate_check(req,text)
    if duplicate: reasons.append('похожее обращение уже поступало от этого заявителя')
    low_conf=confidence<.68
    is_appeal=bool(APPEAL_RE.search(normalize(text)))
    is_hacked=bool(ACCOUNT_RE.search(normalize(text)))
    human=category in HUMAN_ALWAYS or category in {'report.account','report.payment'} or is_appeal or is_hacked or bool(CRITICAL_RE.search(normalize(text))) or bool(PAYMENT_RE.search(normalize(text))) or duplicate or low_conf or (kind=='complaint' and category!='complaint.invalid')
    if human: reasons.append('требуется проверка администратора; автоматическое решение не выносится')
    if category in {'report.account','report.payment','complaint.admin'}: reasons.append('категория всегда направляется человеку согласно таксономии')
    if is_appeal: reasons.append('апелляции и запросы на разбан всегда эскалируются')
    if is_hacked: reasons.append('подозрение на взлом аккаунта требует ручной проверки')
    nickname=req.reported_nick
    if not nickname:
        m=re.search(r'\b([A-ZА-Я][\w]{2,}_[A-ZА-Я][\w]{2,})\b', text)
        nickname=m.group(1) if m else None
    draft=make_draft(lang,category,rules,nickname,human,evidence)
    if duplicate: draft['text']='Здравствуйте! Похоже, вы уже отправляли обращение по этой ситуации. Мы объединим повторные сообщения, чтобы проверка не дублировалась.'
    save_ticket(req,text,category,priority)
    elapsed=round((time.perf_counter()-started)*1000,2)
    out = {'lang':lang,'category':category,'priority':priority,'priority_reason':priority_reason(category,text,evidence,kind),'is_spam_or_duplicate':duplicate,'extracted':{'nickname':nickname,'rule_refs':refs or [r['ref'] for r in rules],'has_evidence':evidence,'server':req.region},'draft_reply':draft,'needs_human':human,'confidence':round(confidence,2),'reasons':reasons,'retrieved_rules':[{'ref':r['ref'],'section':r['section'],'text':r['text'][:600]} for r in rules],'latency_ms':elapsed}
    if req.save_to_queue:
        case_id = req.ticket_id or 'GM-' + uuid.uuid4().hex[:10].upper()
        stamp = now_iso()
        con = db()
        if con.execute('SELECT 1 FROM cases WHERE id=?', (case_id,)).fetchone():
            con.close()
            raise HTTPException(status_code=409, detail='Case ID already exists')
        con.execute('''INSERT INTO cases(id,kind,text,attachments,status,category,priority,lang,confidence,needs_human,
                draft_reply,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (case_id,kind,text[:12000], min(max(req.images_count or 0,0),100),'new',category,priority,lang,confidence,int(human),draft['text'],
             json.dumps(out,ensure_ascii=False),stamp,stamp))
        con.execute('INSERT INTO case_events(case_id,action,details,created_at,actor) VALUES(?,?,?,?,?)',
                    (case_id,'created','AI triage',stamp,audit_actor(request)))
        con.commit(); con.close()
        out['case_id'] = case_id
    return out

@app.post('/cases/batch')
def batch_triage(req: BatchRequest, request: Request):
    results=[]
    for item in req.items:
        # Do not mutate the validated caller payload.
        res=triage(item.model_copy(update={'ticket_id':None,'save_to_queue':True}),request)
        results.append({'case_id':res['case_id'],'category':res['category'],'priority':res['priority'],
                        'needs_human':res['needs_human']})
    return {'created':len(results),'results':results}

@app.get('/cases')
def list_cases(status: str | None = None, q: str = '', limit: int = Query(default=100,ge=1,le=500)):
    if status and status not in VALID_STATUSES:
        raise HTTPException(400, 'Invalid status')
    con=db();con.row_factory=sqlite3.Row
    sql='SELECT * FROM cases WHERE 1=1'; params=[]
    if status: sql+=' AND status=?'; params.append(status)
    if q.strip():
        sql+=' AND (text LIKE ? OR id LIKE ? OR category LIKE ?)'; params.extend([f'%{q.strip()}%']*3)
    rows=con.execute(sql+" ORDER BY CASE priority WHEN 'p0' THEN 0 WHEN 'p1' THEN 1 WHEN 'p2' THEN 2 ELSE 3 END, created_at DESC LIMIT ?",(*params,limit)).fetchall()
    con.close()
    return {'cases':[case_dict(row) for row in rows], 'count':len(rows)}

@app.get('/cases/{case_id}')
def get_case(case_id: str):
    con=db();con.row_factory=sqlite3.Row
    row=con.execute('SELECT * FROM cases WHERE id=?',(case_id,)).fetchone()
    events=con.execute('SELECT action,details,created_at,actor FROM case_events WHERE case_id=? ORDER BY id DESC LIMIT 40',(case_id,)).fetchall()
    con.close()
    if row is None: raise HTTPException(404,'Case not found')
    return {'case':case_dict(row),'events':[dict(e) for e in events]}

@app.patch('/cases/{case_id}')
def update_case(case_id: str, update: CaseUpdate, request: Request):
    changes=update.model_dump(exclude_unset=True)
    if not changes: raise HTTPException(400,'No changes supplied')
    if 'status' in changes and changes['status'] not in VALID_STATUSES: raise HTTPException(400,'Invalid status')
    if 'priority' in changes and changes['priority'] not in VALID_PRIORITIES: raise HTTPException(400,'Invalid priority')
    if 'assignee' in changes and changes['assignee'] is None:
        raise HTTPException(400,'Assignee must not be null')
    if 'resolution_note' in changes and changes['resolution_note'] is None:
        raise HTTPException(400,'Resolution note must not be null')
    if 'draft_reply' in changes and (changes['draft_reply'] is None or not changes['draft_reply'].strip()):
        raise HTTPException(400,'Draft cannot be empty')
    con=db();con.row_factory=sqlite3.Row
    row=con.execute('SELECT * FROM cases WHERE id=?',(case_id,)).fetchone()
    if row is None: con.close();raise HTTPException(404,'Case not found')
    # Human-required tickets may be closed only with a recorded operator decision.
    if changes.get('status') == 'resolved' and row['needs_human'] and not (changes.get('resolution_note') or row['resolution_note']):
        con.close(); raise HTTPException(400, 'Укажи результат ручной проверки перед закрытием обращения')
    # Operator may raise urgency, but must never downgrade safety-critical P0.
    if 'priority' in changes and row['priority']=='p0' and changes['priority']!='p0' and row['needs_human']:
        con.close();raise HTTPException(400,'Cannot downgrade critical escalation')
    stamp=now_iso()
    changes['updated_at']=stamp
    con.execute('UPDATE cases SET '+','.join(k+'=?' for k in changes)+' WHERE id=?',(*changes.values(),case_id))
    for action,value in changes.items():
        if action=='updated_at':continue
        con.execute('INSERT INTO case_events(case_id,action,details,created_at,actor) VALUES(?,?,?,?,?)',
                    (case_id,action, str(value)[:200] if action not in {'draft_reply','resolution_note'} else ('Draft edited' if action=='draft_reply' else 'Результат ручной проверки записан'),stamp,audit_actor(request)))
    con.commit()
    updated=con.execute('SELECT * FROM cases WHERE id=?',(case_id,)).fetchone()
    con.close()
    return {'case':case_dict(updated),'saved':True}

@app.delete('/cases/{case_id}')
def delete_case(case_id: str):
    con=db()
    cur=con.execute('DELETE FROM cases WHERE id=?',(case_id,))
    con.execute('DELETE FROM case_events WHERE case_id=?',(case_id,))
    con.commit();con.close()
    if not cur.rowcount:raise HTTPException(404,'Case not found')
    return {'deleted':True}

@app.post('/cases/{case_id}/notes')
def add_case_note(case_id: str, note: CaseNote, request: Request):
    con=db()
    if not con.execute('SELECT 1 FROM cases WHERE id=?',(case_id,)).fetchone():
        con.close(); raise HTTPException(404,'Case not found')
    stamp=now_iso()
    con.execute('INSERT INTO case_events(case_id,action,details,created_at,actor) VALUES(?,?,?,?,?)',
                (case_id,'note',note.text.strip(),stamp,audit_actor(request)))
    con.execute('UPDATE cases SET updated_at=? WHERE id=?',(stamp,case_id))
    con.commit();con.close()
    return {'saved':True,'created_at':stamp}

@app.get('/cases/{case_id}/similar')
def similar_cases(case_id: str, limit: int = Query(default=5, ge=1, le=20)):
    con=db();con.row_factory=sqlite3.Row
    source=con.execute('SELECT id,text,category,status FROM cases WHERE id=?',(case_id,)).fetchone()
    if not source:
        con.close();raise HTTPException(404,'Case not found')
    candidates=con.execute('SELECT id,text,category,status FROM cases WHERE id<>? ORDER BY updated_at DESC LIMIT 500', (case_id,)).fetchall()
    con.close()
    source_tokens={w for w in normalize(source['text']).split() if len(w)>2}
    ranked=[]
    for candidate in candidates:
        tokens={w for w in normalize(candidate['text']).split() if len(w)>2}
        union=source_tokens | tokens
        overlap=len(source_tokens & tokens)/len(union) if union else 0
        # Category is a tie-breaker, never enough to claim similarity alone.
        score=overlap + (0.05 if candidate['category']==source['category'] and overlap>=0.15 else 0)
        if score>=0.18:
            ranked.append({'id':candidate['id'],'category':candidate['category'],
                           'status':candidate['status'],'similarity':round(min(score,1.0),3),
                           'preview':candidate['text'][:180]})
    ranked.sort(key=lambda r:r['similarity'],reverse=True)
    return {'matches':ranked[:limit], 'method':'token_overlap_heuristic', 'scope':'last_500_cases'}

@app.post('/cases/bulk')
def bulk_update(action: BulkAction, request: Request):
    if (action.status is None) == (action.assignee is None):
        raise HTTPException(400,'Choose exactly one field: status or assignee')
    if action.status is not None and action.status not in VALID_STATUSES:
        raise HTTPException(400,'Invalid status')
    con=db();con.row_factory=sqlite3.Row
    ids=list(dict.fromkeys(action.case_ids))
    rows=con.execute('SELECT id,needs_human,resolution_note FROM cases WHERE id IN ('+','.join('?' for _ in ids)+')',ids).fetchall()
    if len(rows)!=len(ids):
        con.close();raise HTTPException(404,'Some cases were not found')
    if action.status=='resolved' and any(row['needs_human'] and not row['resolution_note'] for row in rows):
        con.close();raise HTTPException(400,'Для закрытия обращений с эскалацией нужна запись о ручной проверке')
    column='status' if action.status is not None else 'assignee'
    value=action.status if action.status is not None else action.assignee.strip()
    stamp=now_iso()
    for item in rows:
        con.execute(f'UPDATE cases SET {column}=?,updated_at=? WHERE id=?',(value,stamp,item['id']))
        con.execute('INSERT INTO case_events(case_id,action,details,created_at,actor) VALUES(?,?,?,?,?)',
                    (item['id'],column,str(value)[:200],stamp,audit_actor(request)))
    con.commit();con.close()
    return {'updated':len(rows),'field':column,'value':value}

@app.get('/cases/export/jsonl')
def export_cases_jsonl(status: str | None = None):
    if status and status not in VALID_STATUSES:
        raise HTTPException(400,'Invalid status')
    con=db();con.row_factory=sqlite3.Row
    rows=con.execute('SELECT * FROM cases'+(' WHERE status=?' if status else '')+' ORDER BY created_at DESC',
                     (status,) if status else ()).fetchall()
    con.close()
    def stream():
        for row in rows:
            yield json.dumps(case_dict(row),ensure_ascii=False)+'\n'
    return StreamingResponse(stream(),media_type='application/x-ndjson',
                             headers={'Content-Disposition':'attachment; filename="copilot-cases.jsonl"'})

@app.get('/rules')
def search_rules(q: str = '', section: str = '', limit: int = Query(default=350,ge=1,le=500)):
    needle=q.strip().casefold()
    selected=[item for item in RULE_CATALOG if (not section or section == item['section'])
              and (not needle or needle in (item['ref']+' '+item['text']+' '+item['section']+' '+item['display_ref']).casefold())]
    sections=[chapter['category'] for chapter in RULES_RAW['rules']]
    return {'rules':selected[:limit], 'count':len(selected), 'total':len(RULE_CATALOG),
            'sections':sections,
            'section_counts':{name:sum(entry['section']==name for entry in RULE_CATALOG) for name in sections}}

@app.post('/feedback')
def feedback(req: FeedbackRequest):
    con=db(); con.execute('INSERT INTO feedback(ticket_id,action,edited_reply,created_at) VALUES(?,?,?,?)',(req.ticket_id,req.action,req.edited_reply,datetime.now(timezone.utc).isoformat())); con.commit(); con.close()
    return {'saved':True,'message':'Feedback stored for future review.'}

@app.get('/stats')
def stats():
    con=db(); total=con.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]
    bycat=con.execute('SELECT category,COUNT(*) FROM tickets GROUP BY category ORDER BY COUNT(*) DESC').fetchall()
    feedback=con.execute('SELECT COUNT(*) FROM feedback').fetchone()[0]
    bystatus=con.execute('SELECT status,COUNT(*) FROM cases GROUP BY status').fetchall()
    bypriority=con.execute('SELECT priority,COUNT(*) FROM cases GROUP BY priority').fetchall()
    cases_total=con.execute('SELECT COUNT(*) FROM cases').fetchone()[0]
    human_pending=con.execute("SELECT COUNT(*) FROM cases WHERE needs_human=1 AND status!='resolved'").fetchone()[0]
    by_assignee=con.execute('SELECT assignee,COUNT(*) FROM cases GROUP BY assignee ORDER BY COUNT(*) DESC').fetchall()
    con.close()
    return {'processed':total,'feedback_count':feedback,'cases_total':cases_total,
            'by_category':[{'category':x[0],'count':x[1]} for x in bycat],
            'by_status':{x[0]:x[1] for x in bystatus},'by_priority':{x[0]:x[1] for x in bypriority},
            'human_pending':human_pending,'by_assignee':{(x[0] or 'Не назначено'):x[1] for x in by_assignee}}
