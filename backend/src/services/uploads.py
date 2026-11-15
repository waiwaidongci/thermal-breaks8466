"""CSV 大文件断点续传服务。

关键保证：
- 分块按字节范围去重：并发重传同段不重复计入；重叠字节内容不一致时保留原进度并报冲突；
- 只按字节顺序喂入连续前缀，解析器/校验器状态持久化，重启与乱序到达都可恢复；
- 暂存内存不随文件增长：每块处理完即落库，只保留当前半条记录的解析状态；
- 只有结束声明、连续序号、时间、温度全部核对通过，批次才在单事务内原子进入底稿；
- 损坏批次标记 quarantined 隔离，不阻塞其他上传/批次。
"""
import base64,binascii,hashlib,math,threading
from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy import select,func
from sqlalchemy.orm.attributes import flag_modified
from src.models import CsvUpload,CsvUploadChunk,CsvStagedPoint,ThermalBatch,TemperaturePoint
from src.utils import csv_stream
from src.utils.serialize import row
from .audit import record

MAX_CSV_POINTS=100000  # CSV 通道的样本上限（JSON 粘贴通道仍受 600 点约束）
# 应用级互斥：串行化同一会话的“检查-落块-消费”临界区。
# 生产 PostgreSQL 另有 SELECT ... FOR UPDATE 行锁兜底，这里覆盖无行锁的部署与测试库。
_session_locks={}
_locks_guard=threading.Lock()

def _session_lock(upload_id):
 with _locks_guard:
  lk=_session_locks.get(upload_id)
  if lk is None:
   lk=threading.RLock();_session_locks[upload_id]=lk
  return lk


# ---------- 会话 ----------

def create_upload(db,data,actor):
 if db.scalar(select(CsvUpload).where(CsvUpload.name==data.name)):
  raise HTTPException(409,'上传批次名称已存在')
 if db.scalar(select(ThermalBatch).where(ThermalBatch.name==data.name)):
  raise HTTPException(409,'批次名称已存在')
 expected=(data.size+data.chunk_size-1)//data.chunk_size
 if data.total_chunks!=expected:
  raise HTTPException(422,f'total_chunks 与 size/chunk_size 不符，应为 {expected}')
 if data.chunk_size*data.total_chunks<data.size:
  raise HTTPException(422,'分块范围无法覆盖整个文件')
 upload=CsvUpload(name=data.name,filename=data.filename,description=data.description,
  size=data.size,sha256=data.sha256,chunk_size=data.chunk_size,total_chunks=data.total_chunks,
  status='open',parser_state=csv_stream.initial_state(),validate_state={},
  created_by=actor)
 db.add(upload);db.commit()
 record(db,actor,'upload.created',{'upload_id':upload.id,'size':data.size,'chunks':data.total_chunks})
 return summary(db,upload)


def list_uploads(db):
 out=[]
 for up in db.scalars(select(CsvUpload).order_by(CsvUpload.id.desc())):
  s=summary(db,up);out.append(s)
 return out


def get_upload(db,upload_id):
 up=db.get(CsvUpload,upload_id)
 if not up:raise HTTPException(404,'上传会话不存在')
 return summary(db,up)


def summary(db,up):
 received={c.index for c in db.scalars(select(CsvUploadChunk).where(CsvUploadChunk.upload_id==up.id))}
 missing=sorted(set(range(up.total_chunks))-received)  # 上限 total_chunks≤20000，全量返回供断点续传
 return {**row(up),'missing_chunks':missing,'missing_count':len(missing),
         'percent':round(100.0*up.received_bytes/up.size,2)}


# ---------- 分块 ----------

def _quarantine(db,up,reason,actor=None):
 up.status='quarantined';up.quarantine_reason=reason[:2000]
 up.quarantined_at=datetime.now(timezone.utc)
 db.commit()
 record(db,actor or up.created_by,'upload.quarantined',{'upload_id':up.id,'reason':reason[:500]})


def _expected_range(up,index):
 start=index*up.chunk_size
 end=min(up.size,start+up.chunk_size)
 return start,end


def put_chunk(db,upload_id,data,actor):
 up=db.get(CsvUpload,upload_id)
 if not up:raise HTTPException(404,'上传会话不存在')
 if up.status=='published':raise HTTPException(409,'上传已发布，分块不可更改')
 if up.status=='quarantined':raise HTTPException(409,f'上传已隔离：{up.quarantine_reason}')
 if not 0<=data.index<up.total_chunks:raise HTTPException(422,'分块序号超出范围')
 start,end=_expected_range(up,data.index)
 if data.start_byte!=start or data.end_byte!=end:
  raise HTTPException(422,f'字节范围应为 [{start},{end})')
 try:payload=base64.b64decode(data.payload,validate=True)
 except (binascii.Error,ValueError):raise HTTPException(422,'payload 不是合法 Base64')
 if len(payload)!=end-start:raise HTTPException(422,'分块字节长度与声明范围不符')
 digest=hashlib.sha256(payload).hexdigest()
 if digest!=data.sha256:raise HTTPException(422,'分块 SHA-256 不一致，请重传该块')

 # 临界区：同一会话的并发重传在此串行（应用锁 + PostgreSQL 行锁双保险）
 with _session_lock(up.id):
  db.refresh(up,with_for_update=db.bind.dialect.name!='sqlite')
  if up.status=='quarantined':raise HTTPException(409,f'上传已隔离：{up.quarantine_reason}')
  if up.status=='published':raise HTTPException(409,'上传已发布，分块不可更改')
  existing=db.scalar(select(CsvUploadChunk).where(
   CsvUploadChunk.upload_id==up.id,CsvUploadChunk.index==data.index))
  if existing:
   if existing.sha256==digest and existing.start_byte==start and existing.end_byte==end:
    return summary(db,up)  # 幂等：并发重传同段不重复计入
   # 同段重传但重叠字节不一致：保留原进度并隔离报冲突
   _quarantine(db,up,f'第 {data.index} 块重传内容与已存字节不一致，保留原进度',actor)
   raise HTTPException(409,'分块内容冲突，原进度保留；该上传已隔离')

  # 重叠字节守卫：任何已存块与本段字节区间相交即冲突
  overlap=db.scalar(select(CsvUploadChunk).where(
   CsvUploadChunk.upload_id==up.id,CsvUploadChunk.index!=data.index,
   CsvUploadChunk.start_byte<end,CsvUploadChunk.end_byte>start))
  if overlap:
   _quarantine(db,up,f'第 {data.index} 块与第 {overlap.index} 块字节重叠且不一致，保留原进度',actor)
   raise HTTPException(409,'分块字节范围冲突，原进度保留；该上传已隔离')

  db.add(CsvUploadChunk(upload_id=up.id,index=data.index,start_byte=start,end_byte=end,
                        sha256=digest,payload=payload))
  db.flush()
  try:
   _drain_contiguous(db,up)
  except csv_stream.CsvFormatError as e:
   _quarantine(db,up,f'CSV 结构损坏：{e}',actor);raise HTTPException(422,f'CSV 结构损坏：{e}；该上传已隔离')
  except ValueError as e:
   _quarantine(db,up,f'记录核对失败：{e}',actor);raise HTTPException(422,f'记录核对失败：{e}；该上传已隔离')
  db.commit()
 return summary(db,up)


def _drain_contiguous(db,up):
 """按字节顺序消费所有已到达的连续前缀块，状态持久化在 upload 行上。"""
 while True:
  chunk=db.scalar(select(CsvUploadChunk).where(
   CsvUploadChunk.upload_id==up.id,CsvUploadChunk.index==up.next_expected_chunk))
  if chunk is None:break
  records=csv_stream.feed(up.parser_state,chunk.payload)
  _stage_records(db,up,records)
  flag_modified(up,'parser_state');flag_modified(up,'validate_state')
  up.next_expected_chunk+=1
  up.received_chunks+=1
  up.received_bytes+=chunk.end_byte-chunk.start_byte


# ---------- 记录级核对与暂存 ----------

def _stage_records(db,up,records):
 state=up.validate_state or {}
 last_seq=state.get('last_sequence',0)
 last_time=state.get('last_time_iso')
 last_dt=datetime.fromisoformat(last_time) if last_time else None
 for physical in records:
  # 仅第一条物理记录：首列不是整数则按表头跳过一次；无表头文件不会被误判
  if not up.staged_points and not up.parser_state.get('header_skipped'):
   up.parser_state['header_skipped']=True
   if physical:
    try:int(physical[0].strip())
    except ValueError:
     if len(physical)!=3:raise csv_stream.CsvFormatError('表头列数应为3列（序号,时间,温度）')
     continue
  if len(physical)!=3:raise csv_stream.CsvFormatError(f'记录列数应为3，实际{len(physical)}列')
  seq_raw,time_raw,temp_raw=(x.strip() for x in physical)
  try:seq=int(seq_raw)
  except ValueError:raise ValueError(f'序号必须是整数，收到 {seq_raw!r}')
  if seq!=last_seq+1:
   raise ValueError(f'序号不连续：期望 {last_seq+1}，收到 {seq}')
  try:dt=datetime.fromisoformat(time_raw.replace('Z','+00:00'))
  except ValueError:raise ValueError(f'时间无法解析：{time_raw!r}')
  if dt.tzinfo is None or dt.utcoffset() is None:raise ValueError(f'时间必须包含时区：{time_raw!r}')
  dt=dt.astimezone(timezone.utc)
  if last_dt is not None and dt<=last_dt:raise ValueError(f'时间未按序号严格递增：{time_raw!r}')
  try:temp=float(temp_raw)
  except ValueError:raise ValueError(f'温度必须是数字：{temp_raw!r}')
  if not math.isfinite(temp) or abs(temp)>1e12:raise ValueError(f'温度必须有限且绝对值不超1e12：{temp_raw!r}')
  db.add(CsvStagedPoint(upload_id=up.id,sequence=seq,recorded_at=dt,temperature=temp))
  up.staged_points+=1
  if up.staged_points>MAX_CSV_POINTS:raise ValueError(f'样本数超过 CSV 通道上限 {MAX_CSV_POINTS}')
  last_seq=seq;last_dt=dt
 up.validate_state={'last_sequence':last_seq,
                    'last_time_iso':last_dt.isoformat() if last_dt else None}


# ---------- 结束声明与原子发布 ----------

def finish_upload(db,upload_id,data,actor):
 up=db.get(CsvUpload,upload_id)
 if not up:raise HTTPException(404,'上传会话不存在')
 if up.status=='published':return summary(db,up)  # 幂等：重复结束直接返回已发布结果
 if up.status=='quarantined':raise HTTPException(409,f'上传已隔离：{up.quarantine_reason}')
 with _session_lock(up.id):
  db.refresh(up,with_for_update=db.bind.dialect.name!='sqlite')
  if up.status=='quarantined':raise HTTPException(409,f'上传已隔离：{up.quarantine_reason}')
  try:
   _verify_and_publish(db,up,data,actor)
  except HTTPException:
   raise
  except csv_stream.CsvFormatError as e:
   _quarantine(db,up,f'CSV 结构损坏：{e}',actor);raise HTTPException(422,f'CSV 结构损坏：{e}；该上传已隔离')
  except ValueError as e:
   _quarantine(db,up,f'结束核对失败：{e}',actor);raise HTTPException(422,f'结束核对失败：{e}；该上传已隔离')
 return summary(db,up)


def _verify_and_publish(db,up,data,actor):
 # 1) 分块完整性
 if up.next_expected_chunk!=up.total_chunks or up.received_chunks!=up.total_chunks:
  missing=summary(db,up)['missing_count']
  raise ValueError(f'分块未集齐：缺 {missing} 块')
 # 2) 流式核对连续字节范围并重算整文件摘要：逐块游标读取，不把全文件载入内存
 h=hashlib.sha256();cursor=0;count=0
 stream=db.scalars(select(CsvUploadChunk).where(
  CsvUploadChunk.upload_id==up.id).order_by(CsvUploadChunk.index).execution_options(yield_per=100))
 for c in stream:
  if c.index!=count or c.start_byte!=cursor:raise ValueError('分块字节范围不连续')
  h.update(c.payload);cursor=c.end_byte;count+=1
  db.expunge(c)  # 立即释放该块的会话引用
 if count!=up.total_chunks or cursor!=up.size:
  raise ValueError('分块拼接长度与文件声明大小不符')
 whole=h.hexdigest()
 if whole!=up.sha256 or whole!=data.sha256:
  raise ValueError('整文件 SHA-256 与结束声明不一致')
 # 3) 解析收尾：半条记录/半个字符/未闭合引号在此暴露
 tail=csv_stream.finish(up.parser_state)
 _stage_records(db,up,tail)
 flag_modified(up,'parser_state');flag_modified(up,'validate_state')
 # 4) 结束声明的点数
 if up.staged_points==0:raise ValueError('文件不含任何温度记录')
 if up.staged_points!=data.declared_points:
  raise ValueError(f'结束声明点数 {data.declared_points} 与实际解析 {up.staged_points} 不符')
 staged=db.scalar(select(func.count()).select_from(CsvStagedPoint).where(CsvStagedPoint.upload_id==up.id))
 if staged!=up.staged_points:raise ValueError('暂存点数与解析计数不一致')
 # 5) 名称唯一（创建后可能有同名 JSON 批次插入）
 if db.scalar(select(ThermalBatch).where(ThermalBatch.name==up.name)):
  raise ValueError('同名温度批次已存在，无法发布')

 # 6) 单事务原子发布：批次与全部温度点在同一事务内可见，分批游标搬运避免一次性载入
 batch=ThermalBatch(name=up.name,filename=up.filename,description=up.description,
                    status='published',source_upload_id=up.id)
 db.add(batch);db.flush()
 point_stream=db.scalars(select(CsvStagedPoint).where(
  CsvStagedPoint.upload_id==up.id).order_by(CsvStagedPoint.sequence).execution_options(yield_per=500))
 published=0
 for p in point_stream:
  db.add(TemperaturePoint(batch_id=batch.id,sequence=p.sequence,
                          recorded_at=p.recorded_at,temperature=p.temperature))
  published+=1
  if published%500==0:db.flush()
 if published!=up.staged_points:raise ValueError('发布点数与暂存计数不一致')
 up.status='published';up.batch_id=batch.id;up.published_at=datetime.now(timezone.utc)
 db.commit()
 record(db,actor,'upload.published',
        {'upload_id':up.id,'batch_id':batch.id,'points':published,'sha256':whole})
