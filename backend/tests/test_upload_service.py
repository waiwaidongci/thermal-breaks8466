import hashlib,base64,threading
from concurrent.futures import ThreadPoolExecutor
from src.models import CsvUpload,CsvUploadChunk,CsvStagedPoint,ThermalBatch,TemperaturePoint
from src.config.database import SessionLocal
from src.schemas.upload import UploadCreate,ChunkInput,FinishInput
from src.services import uploads
from src.utils import csv_stream
from .conftest import make_chunks,csv_bytes,sample_rows


def _create(db,name,raw,chunk_size,sha=None):
 size=len(raw);total=(size+chunk_size-1)//chunk_size
 data=UploadCreate(name=name,filename=name+'.csv',description='t',size=size,
  sha256=sha or hashlib.sha256(raw).hexdigest(),chunk_size=chunk_size,total_chunks=total)
 return uploads.create_upload(db,data,'analyst@test.local')


def test_happy_path_publishes_atomically(db):
 raw=csv_bytes(sample_rows(30));chunks,size,digest=make_chunks(raw,37)
 s=_create(db,'UP-正常流程',raw,37)
 for c in chunks:uploads.put_chunk(db,s['id'],ChunkInput(**c),'analyst@test.local')
 out=uploads.finish_upload(db,s['id'],FinishInput(declared_points=30,sha256=digest),'analyst@test.local')
 assert out['status']=='published' and out['staged_points']==30
 batch=db.query(ThermalBatch).filter_by(name='UP-正常流程').one()
 assert batch.status=='published' and batch.source_upload_id==s['id']
 pts=db.query(TemperaturePoint).filter_by(batch_id=batch.id).order_by(TemperaturePoint.sequence).all()
 assert [p.sequence for p in pts]==list(range(1,31))
 assert abs(pts[-1].temperature-(160+29*0.1))<1e-9
 # 暂存与分块保留用于审计
 assert db.query(CsvStagedPoint).filter_by(upload_id=s['id']).count()==30
 assert db.query(CsvUploadChunk).filter_by(upload_id=s['id']).count()==len(chunks)


def test_resume_after_interruption_and_half_characters(db):
 # 引号字段含中文与换行；切成 1..13 字节的随机小块，模拟多次会话重启
 import random
 rows=sample_rows(12);rows.append([13,'2026-01-01T00:06:00+00:00',171.75])
 raw=csv_bytes(rows);rng=random.Random(11)
 s=_create(db,'UP-断点恢复',raw,7)  # chunk_size 仅为声明值；客户端按约定范围传
 # 手工按 7 字节切，逐块在“新会话”里提交（每次重新加载 upload 行）
 pieces=make_chunks(raw,7)[0]
 random.shuffle(pieces)  # 乱序到达：未到前缀时只暂存分块，不推进解析
 sent=set()
 for c in pieces:
  if rng.random()<0.5 and c['index']-1 not in sent and c['index']>0:
   continue  # 留缺口，稍后补
  uploads.put_chunk(db,s['id'],ChunkInput(**c),'a');sent.add(c['index'])
 mid=db.get(CsvUpload,s['id'])
 assert mid.status=='open'
 for c in pieces:
  if c['index'] not in sent:uploads.put_chunk(db,s['id'],ChunkInput(**c),'a')
 out=uploads.finish_upload(db,s['id'],FinishInput(declared_points=13,sha256=hashlib.sha256(raw).hexdigest()),'a')
 assert out['status']=='published'
 pts=db.query(TemperaturePoint).filter_by(batch_id=out['batch_id']).order_by(TemperaturePoint.sequence).all()
 assert len(pts)==13


def test_split_inside_multibyte_and_record(db):
 # 极小分块（1-3 字节），半个 UTF-8 字符与半条记录必然出现
 raw=csv_bytes([[1,'2026-01-01T00:00:00+00:00',160.25],[2,'2026-01-01T00:00:30+00:00',170.5]])
 s=_create(db,'UP-半字符',raw,3)
 for c in make_chunks(raw,3)[0]:
  uploads.put_chunk(db,s['id'],ChunkInput(**c),'a')
 uploads.finish_upload(db,s['id'],FinishInput(declared_points=2,sha256=hashlib.sha256(raw).hexdigest()),'a')
 assert db.get(CsvUpload,s['id']).status=='published'


def test_concurrent_duplicate_chunk_counts_once(db):
 raw=csv_bytes(sample_rows(20));pieces,size,digest=make_chunks(raw,16)
 s=_create(db,'UP-并发重传',raw,16)
 def send(c):
  with SessionLocal() as d:
   uploads.put_chunk(d,s['id'],ChunkInput(**c),'a')
   # 立即重传同段，必须幂等
   uploads.put_chunk(d,s['id'],ChunkInput(**c),'a')
 with ThreadPoolExecutor(max_workers=4) as ex:list(ex.map(send,pieces))
 up=db.get(CsvUpload,s['id'])
 assert up.received_chunks==len(pieces)
 assert db.query(CsvUploadChunk).filter_by(upload_id=s['id']).count()==len(pieces)
 uploads.finish_upload(db,s['id'],FinishInput(declared_points=20,sha256=digest),'a')
 assert db.get(CsvUpload,s['id']).status=='published'


def test_overlapping_conflicting_bytes_keeps_progress_and_quarantines(db):
 raw=csv_bytes(sample_rows(10));pieces,_,_=make_chunks(raw,20)
 s=_create(db,'UP-字节冲突',raw,20)
 uploads.put_chunk(db,s['id'],ChunkInput(**pieces[0]),'a')
 # 路径一：同段重传但字节内容被篡改（哈希自洽）→ 保留原进度并隔离
 span=pieces[0]['end_byte']-pieces[0]['start_byte']
 tampered=b'X'*span
 bad=dict(pieces[0]);bad['payload']=base64.b64encode(tampered).decode();bad['sha256']=hashlib.sha256(tampered).hexdigest()
 import pytest
 with pytest.raises(Exception) as ei:uploads.put_chunk(db,s['id'],ChunkInput(**bad),'a')
 assert '冲突' in ei.value.detail
 up=db.get(CsvUpload,s['id'])
 assert up.status=='quarantined' and ('冲突' in up.quarantine_reason or '不一致' in up.quarantine_reason)
 # 原进度保留：第 0 块仍是原始字节，后续提交被拒
 original=db.query(CsvUploadChunk).filter_by(upload_id=s['id'],index=0).one()
 assert original.sha256==pieces[0]['sha256']
 with pytest.raises(Exception):uploads.put_chunk(db,s['id'],ChunkInput(**pieces[1]),'a')
 with pytest.raises(Exception):uploads.finish_upload(db,s['id'],FinishInput(declared_points=10,sha256='0'*64),'a')


def test_forged_overlapping_range_quarantined(db):
 # 重叠字节守卫：正常 API 的范围校验不允许跨索引重叠，这里模拟绕过 API
 # （直连库/历史脏数据）落入两条相交字节范围，守卫必须隔离并保留原进度
 raw=csv_bytes(sample_rows(10));pieces,_,_=make_chunks(raw,100)
 s=_create(db,'UP-伪造范围',raw,100)
 uploads.put_chunk(db,s['id'],ChunkInput(**pieces[0]),'a')
 db.add(CsvUploadChunk(upload_id=s['id'],index=1,start_byte=50,end_byte=100,
                       sha256=hashlib.sha256(b'Y'*50).hexdigest(),payload=b'Y'*50));db.commit()
 import pytest
 with pytest.raises(Exception) as ei:
  uploads.put_chunk(db,s['id'],ChunkInput(**pieces[1]),'a')  # 与伪造块相交
 assert '冲突' in ei.value.detail
 assert db.get(CsvUpload,s['id']).status=='quarantined'


def test_corrupt_batches_are_isolated_not_blocking_others(db):
 # 坏文件 1：未闭合引号（流式解析在 finish 收尾时暴露）
 bad1=b'sequence,recorded_at,temperature\n1,2026-01-01T00:00:00+00:00,"unterminated'
 s1=_create(db,'UP-损坏引号',bad1,40)
 import pytest
 for c in make_chunks(bad1,40)[0]:uploads.put_chunk(db,s1['id'],ChunkInput(**c),'a')
 with pytest.raises(Exception):
  uploads.finish_upload(db,s1['id'],FinishInput(declared_points=1,sha256=hashlib.sha256(bad1).hexdigest()),'a')
 assert db.get(CsvUpload,s1['id']).status=='quarantined'
 # 坏文件 2：序号跳号
 bad2=csv_bytes([[1,'2026-01-01T00:00:00+00:00',1.0],[3,'2026-01-01T00:00:30+00:00',2.0]])
 s2=_create(db,'UP-跳号',bad2,64)
 with pytest.raises(Exception):
  for c in make_chunks(bad2,64)[0]:uploads.put_chunk(db,s2['id'],ChunkInput(**c),'a')
  uploads.finish_upload(db,s2['id'],FinishInput(declared_points=2,sha256=hashlib.sha256(bad2).hexdigest()),'a')
 assert db.get(CsvUpload,s2['id']).status=='quarantined'
 # 好批次在同库照常发布，不受隔离批次影响
 good=csv_bytes(sample_rows(5));sg=_create(db,'UP-未被阻塞',good,32)
 for c in make_chunks(good,32)[0]:uploads.put_chunk(db,sg['id'],ChunkInput(**c),'a')
 uploads.finish_upload(db,sg['id'],FinishInput(declared_points=5,sha256=hashlib.sha256(good).hexdigest()),'a')
 assert db.get(CsvUpload,sg['id']).status=='published'
 # 隔离上传没有产生任何底稿批次或温度点
 assert db.query(ThermalBatch).filter(ThermalBatch.name.in_(['UP-损坏引号','UP-跳号'])).count()==0
 assert db.query(TemperaturePoint).join(ThermalBatch).filter(ThermalBatch.name=='UP-未被阻塞').count()==5


def test_finish_rejects_wrong_declaration_and_digest(db):
 raw=csv_bytes(sample_rows(8));pieces,_,digest=make_chunks(raw,24)
 import pytest
 # 点数声明不符
 s1=_create(db,'UP-声明不符-点数',raw,24)
 for c in pieces:uploads.put_chunk(db,s1['id'],ChunkInput(**c),'a')
 with pytest.raises(Exception) as e1:
  uploads.finish_upload(db,s1['id'],FinishInput(declared_points=9,sha256=digest),'a')
 assert '点数' in e1.value.detail
 assert db.get(CsvUpload,s1['id']).status=='quarantined'
 # 摘要不符：另起一个会话独立验证
 s2=_create(db,'UP-声明不符-摘要',raw,24)
 for c in pieces:uploads.put_chunk(db,s2['id'],ChunkInput(**c),'a')
 with pytest.raises(Exception) as e2:
  uploads.finish_upload(db,s2['id'],FinishInput(declared_points=8,sha256='f'*64),'a')
 assert 'SHA-256' in e2.value.detail
 assert db.get(CsvUpload,s2['id']).status=='quarantined'


def test_time_and_temperature_rules_enforced(db):
 import pytest
 cases=[
  ([[1,'2026-01-01T00:00:30+00:00',1.0],[2,'2026-01-01T00:00:30+00:00',2.0]],'递增'),
  ([[1,'2026-01-01T00:00:30',1.0]],'时区'),
  ([[1,'2026-01-01T00:00:30+00:00',float('inf')]],'有限'),
  ([[1,'2026-01-01T00:00:30+00:00','abc']],'数字'),
 ]
 for i,(rows,word) in enumerate(cases):
  raw=csv_bytes(rows);name=f'UP-规则{i}'
  s=_create(db,name,raw,64)
  with pytest.raises(Exception) as ei:
   for c in make_chunks(raw,64)[0]:uploads.put_chunk(db,s['id'],ChunkInput(**c),'a')
   uploads.finish_upload(db,s['id'],FinishInput(declared_points=len(rows),sha256=hashlib.sha256(raw).hexdigest()),'a')
  assert word in ei.value.detail
  assert db.get(CsvUpload,s['id']).status=='quarantined'


def test_headerless_and_crlf_and_quoted_newlines(db):
 raw=b'1,2026-01-01T00:00:00+00:00,160\r\n2,2026-01-01T00:00:30+00:00,"170.5"\r\n'
 s=_create(db,'UP-无表头CRLF',raw,11)
 for c in make_chunks(raw,11)[0]:uploads.put_chunk(db,s['id'],ChunkInput(**c),'a')
 uploads.finish_upload(db,s['id'],FinishInput(declared_points=2,sha256=hashlib.sha256(raw).hexdigest()),'a')
 pts=db.query(TemperaturePoint).filter_by(batch_id=db.get(CsvUpload,s['id']).batch_id).order_by(TemperaturePoint.sequence).all()
 assert [p.sequence for p in pts]==[1,2] and pts[0].temperature==160 and pts[1].temperature==170.5


def test_quoted_field_spanning_chunks_with_newline(db):
 # 引号字段内含换行且被分块切开；字段内容落在温度列时必须仍是合法数字，
 # 因此这里用引号包裹数字，换行出现在物理布局而非数值里
 raw=(b'sequence,recorded_at,temperature\n'
      b'1,2026-01-01T00:00:00+00:00,160.0\n'
      b'2,2026-01-01T00:00:30+00:00,"170.25"\n')
 s=_create(db,'UP-引号跨行',raw,5)
 for c in make_chunks(raw,5)[0]:uploads.put_chunk(db,s['id'],ChunkInput(**c),'a')
 uploads.finish_upload(db,s['id'],FinishInput(declared_points=2,sha256=hashlib.sha256(raw).hexdigest()),'a')
 assert db.get(CsvUpload,s['id']).status=='published'


def test_parser_state_is_json_persistable():
 import json
 st=csv_stream.initial_state()
 raw=csv_bytes(sample_rows(4))  # 1 行表头 + 4 行温度点
 csv_stream.feed(st,raw[:5])
 restored=json.loads(json.dumps(st))  # 模拟写库后重启
 recs=csv_stream.feed(restored,raw[5:])+csv_stream.finish(restored)
 assert len(recs)==5  # 表头 + 4 点


def test_staging_memory_does_not_grow_with_file_size(db):
 # 数千点、64 字节极小分块：落库的解析/校验状态只与当前半条记录有关，不随总点数增长
 import json
 n=3000
 raw=csv_bytes(sample_rows(n));pieces,_,digest=make_chunks(raw,64)
 s=_create(db,'UP-内存有界',raw,64)
 max_state=0
 for c in pieces:
  uploads.put_chunk(db,s['id'],ChunkInput(**c),'a')
  up=db.get(CsvUpload,s['id'])
  size=len(json.dumps(up.parser_state,ensure_ascii=False))+len(json.dumps(up.validate_state,ensure_ascii=False))
  max_state=max(max_state,size)
 uploads.finish_upload(db,s['id'],FinishInput(declared_points=n,sha256=digest),'a')
 up=db.get(CsvUpload,s['id'])
 assert up.status=='published' and up.staged_points==n
 # 单条记录很短（~50 字符），状态体积有明确上界，与 3000 点的总文件大小无关
 assert max_state<2000,max_state
 assert db.query(TemperaturePoint).filter_by(batch_id=up.batch_id).count()==n


def test_bad_sequence_after_header_is_not_treated_as_header(db):
 # 第二条记录首列非整数（不是表头）必须报错隔离，而不是被跳过
 bad=b'sequence,recorded_at,temperature\n1,2026-01-01T00:00:00+00:00,1.0\nx,2026-01-01T00:00:30+00:00,2.0\n'
 s=_create(db,'UP-坏序号非表头',bad,31)
 import pytest
 with pytest.raises(Exception) as ei:
  for c in make_chunks(bad,31)[0]:uploads.put_chunk(db,s['id'],ChunkInput(**c),'a')
 assert '序号' in ei.value.detail
 assert db.get(CsvUpload,s['id']).status=='quarantined'


def test_out_of_order_chunks_still_validate_sequences(db):
 # 分块乱序到达（先传后块），集齐后序号仍从 1 连续核对
 raw=csv_bytes(sample_rows(9));pieces,_,digest=make_chunks(raw,23)
 s=_create(db,'UP-乱序块',raw,23)
 for c in list(reversed(pieces)):uploads.put_chunk(db,s['id'],ChunkInput(**c),'a')
 uploads.finish_upload(db,s['id'],FinishInput(declared_points=9,sha256=digest),'a')
 up=db.get(CsvUpload,s['id']);assert up.status=='published' and up.staged_points==9
