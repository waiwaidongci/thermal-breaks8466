import os,base64,hashlib,tempfile

_tmp=tempfile.NamedTemporaryFile(suffix='.db',delete=False)
_tmp.close()
os.environ.setdefault('DATABASE_URL',f'sqlite:///{_tmp.name}')
os.environ.setdefault('JWT_SECRET','test-secret-not-for-deployment')
os.environ.setdefault('SEED_DEMO','false')
os.environ.setdefault('REDIS_URL','redis://localhost:6379/0')

import pytest
from sqlalchemy import event
from fastapi.testclient import TestClient
from src.config.database import Base,engine,SessionLocal
from src.main import app,sync_schema
from src.models import User
from src.utils.security import hash_password,issue_token


# SQLite 测试库：写操作串行并等待锁，模拟多线程下的并发提交（生产为 PostgreSQL 行锁）
@event.listens_for(engine,'connect')
def _sqlite_pragmas(dbapi_conn,_):
 if engine.url.get_backend_name()=='sqlite':
  cur=dbapi_conn.cursor()
  cur.execute('PRAGMA journal_mode=WAL');cur.execute('PRAGMA busy_timeout=30000');cur.close()


@pytest.fixture(scope='session',autouse=True)
def _schema():
 sync_schema()
 yield
 engine.dispose()
 try:os.unlink(_tmp.name)
 except OSError:pass


@pytest.fixture
def db():
 session=SessionLocal()
 try:yield session
 finally:session.close()


@pytest.fixture(scope='session')
def users():
 with SessionLocal() as db:
  made={}
  for role,email in [('admin','admin@test.local'),('analyst','analyst@test.local'),('viewer','viewer@test.local')]:
   u=db.query(User).filter_by(email=email).one_or_none()
   if not u:
    u=User(email=email,name=role,role=role,password_hash=hash_password('pw123456'));db.add(u);db.commit();db.refresh(u)
   made[role]=u
  return made


@pytest.fixture
def client():
 with TestClient(app) as c:yield c


@pytest.fixture
def auth(users):
 def _make(role='analyst'):
  return {'Authorization':'Bearer '+issue_token(users[role])}
 return _make


def make_chunks(raw:bytes,chunk_size:int):
 """返回 (chunk_inputs, size, sha256)，每个元素可直接作为 PUT 正文。"""
 size=len(raw);digest=hashlib.sha256(raw).hexdigest()
 out=[]
 for i in range(0,size,chunk_size):
  payload=raw[i:i+chunk_size]
  out.append({'index':len(out),'start_byte':i,'end_byte':i+len(payload),
              'sha256':hashlib.sha256(payload).hexdigest(),
              'payload':base64.b64encode(payload).decode()})
 return out,size,digest


def csv_bytes(rows,header=('sequence','recorded_at','temperature')):
 import io,csv
 buf=io.StringIO()
 w=csv.writer(buf)
 if header:w.writerow(header)
 for r in rows:w.writerow(r)
 return buf.getvalue().encode('utf-8')


def sample_rows(n=30):
 from datetime import datetime,timedelta,timezone
 t0=datetime(2026,1,1,tzinfo=timezone.utc)
 return [[i+1,(t0+timedelta(seconds=30*i)).isoformat(),160+i*0.1] for i in range(n)]
