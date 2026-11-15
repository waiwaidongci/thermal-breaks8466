import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from sqlalchemy import text,inspect
from redis import Redis
from src.config.database import Base,engine
from src.config.settings import SEED_DEMO,REDIS_URL
from src.models import *
from src.middlewares.errors import ErrorMiddleware
from src.middlewares.request_id import RequestIDMiddleware
from src.routes import auth,batches,runs,notes,audit,uploads

def sync_schema():
 # ORM metadata 负责建新表；已存在的库只补新增列，不做破坏性迁移
 Base.metadata.create_all(engine)
 inspector=inspect(engine)
 columns={c['name'] for c in inspector.get_columns('thermal_batches')}
 additions={'status':"VARCHAR(20) NOT NULL DEFAULT 'published'",
            'source_upload_id':'INTEGER NULL'}
 with engine.begin() as conn:
  for name,ddl in additions.items():
   if name not in columns:
    conn.execute(text(f'ALTER TABLE thermal_batches ADD COLUMN {name} {ddl}'))

@asynccontextmanager
async def lifespan(app):
 sync_schema()
 if SEED_DEMO:
  try:
   from scripts.seed_demo import seed
   seed()
  except Exception:logging.exception('seed failed; startup continues')
 yield
app=FastAPI(title='焰谷热程变点研究档案',lifespan=lifespan)
app.add_middleware(ErrorMiddleware);app.add_middleware(RequestIDMiddleware)
for route in (auth,batches,runs,notes,audit,uploads):app.include_router(route.router)
@app.get('/health')
def health():
 with engine.connect() as c:c.execute(text('select 1'))
 Redis.from_url(REDIS_URL).ping();return {'status':'ok'}
