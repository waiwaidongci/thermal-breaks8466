import base64,hashlib
from src.models import CsvUpload,ThermalBatch
from .conftest import make_chunks,csv_bytes,sample_rows


def _start(client,headers,name,raw,chunk_size):
 total=(len(raw)+chunk_size-1)//chunk_size
 r=client.post('/uploads',headers=headers,json={
  'name':name,'filename':name+'.csv','description':'api',
  'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
  'chunk_size':chunk_size,'total_chunks':total})
 assert r.status_code==200,r.text
 return r.json()


def test_full_api_flow_and_progress(client,auth):
 h=auth('analyst');raw=csv_bytes(sample_rows(25));pieces,_,digest=make_chunks(raw,29)
 up=_start(client,h,'API-完整流程',raw,29)
 # 先传偶数块制造缺口，再补齐，检查 missing 与进度
 for c in pieces[::2]:
  r=client.put(f"/uploads/{up['id']}/chunks/{c['index']}",headers=h,json=c);assert r.status_code==200
 prog=client.get(f"/uploads/{up['id']}",headers=h).json()
 assert prog['missing_count']==len(pieces[1::2]) and 0 < prog['percent'] < 100
 for c in pieces[1::2]:
  r=client.put(f"/uploads/{up['id']}/chunks/{c['index']}",headers=h,json=c);assert r.status_code==200
 r=client.post(f"/uploads/{up['id']}/finish",headers=h,json={'declared_points':25,'sha256':digest})
 assert r.status_code==200 and r.json()['status']=='published'
 # 研究读取面可见
 batches=client.get('/batches',headers=h).json()
 assert any(b['name']=='API-完整流程' and b['status']=='published' for b in batches)


def test_unpublished_and_quarantined_hidden_from_research(client,auth):
 h=auth('analyst');ro=auth('viewer')
 raw=csv_bytes(sample_rows(6));pieces,_,digest=make_chunks(raw,19)
 up=_start(client,h,'API-未发布',raw,19)
 before={b['name'] for b in client.get('/batches',headers=ro).json()}
 assert 'API-未发布' not in before
 for c in pieces[:-1]:client.put(f"/uploads/{up['id']}/chunks/{c['index']}",headers=h,json=c)
 # 观察员对上传只读、不能写
 assert client.post('/uploads',headers=ro,json={}).status_code in (403,422)
 assert client.get('/uploads',headers=ro).status_code==200
 # 发送哈希自洽但字节内容损坏的末块：块级校验通过，但整文件摘要与解析结果必然不符
 span=pieces[-1]['end_byte']-pieces[-1]['start_byte']
 corrupt=b'x'*span
 bad=dict(pieces[-1]);bad['payload']=base64.b64encode(corrupt).decode();bad['sha256']=hashlib.sha256(corrupt).hexdigest()
 r=client.put(f"/uploads/{up['id']}/chunks/{bad['index']}",headers=h,json=bad)
 assert r.status_code==200
 fr=client.post(f"/uploads/{up['id']}/finish",headers=h,
                json={'declared_points':6,'sha256':'0'*64})  # 整文件摘要必然不符
 assert fr.status_code in (422,409)
 detail=client.get(f"/uploads/{up['id']}",headers=h).json()
 assert detail['status']=='quarantined' and detail['quarantine_reason']
 after={b['name'] for b in client.get('/batches',headers=ro).json()}
 assert 'API-未发布' not in after


def test_duplicate_chunk_idempotent_over_http(client,auth):
 h=auth('analyst');raw=csv_bytes(sample_rows(4));pieces,_,digest=make_chunks(raw,13)
 up=_start(client,h,'API-幂等',raw,13)
 for c in pieces:
  r1=client.put(f"/uploads/{up['id']}/chunks/{c['index']}",headers=h,json=c)
  r2=client.put(f"/uploads/{up['id']}/chunks/{c['index']}",headers=h,json=c)
  assert r1.status_code==200 and r2.status_code==200
 client.post(f"/uploads/{up['id']}/finish",headers=h,json={'declared_points':4,'sha256':digest})
 # 已发布后重复 finish 幂等，重复分块被拒
 again=client.post(f"/uploads/{up['id']}/finish",headers=h,json={'declared_points':4,'sha256':digest})
 assert again.status_code==200 and again.json()['status']=='published'
 assert client.put(f"/uploads/{up['id']}/chunks/0",headers=h,json=pieces[0]).status_code==409


def test_resume_with_out_of_order_and_gap_then_publish(client,auth):
 h=auth('analyst')
 # 含引号字段换行与 CRLF 的 CSV，分块故意乱序、留缺口后补齐
 raw=(b'sequence,recorded_at,temperature\r\n'
      +b''.join(f'{i+1},2026-03-01T00:{i:02d}:00+00:00,{150+i*0.5:.2f}\r\n'.encode() for i in range(18)))
 pieces,_,digest=make_chunks(raw,31)
 up=_start(client,h,'API-乱序续传',raw,31)
 order=sorted(range(len(pieces)),key=lambda i:(i%3,i))  # 交错顺序，天然留下前缀缺口
 for i in order[:len(order)-2]:
  r=client.put(f"/uploads/{up['id']}/chunks/{pieces[i]['index']}",headers=h,json=pieces[i]);assert r.status_code==200
 mid=client.get(f"/uploads/{up['id']}",headers=h).json()
 assert mid['status']=='open' and mid['missing_count']>=2 and mid['received_chunks']<=len(pieces)
 # 补齐剩余块
 for i in order[len(order)-2:]:
  r=client.put(f"/uploads/{up['id']}/chunks/{pieces[i]['index']}",headers=h,json=pieces[i]);assert r.status_code==200
 fin=client.post(f"/uploads/{up['id']}/finish",headers=h,json={'declared_points':18,'sha256':digest})
 assert fin.status_code==200 and fin.json()['status']=='published'
 # 研究端读到完整 18 点且序号连续
 all_pts=client.get('/points',headers=h).json()
 mine=[p for p in all_pts if p['batch_id']==fin.json()['batch_id']]
 assert [p['sequence'] for p in mine]==list(range(1,19))


def test_bad_range_and_bad_base64_rejected(client,auth):
 h=auth('analyst');raw=csv_bytes(sample_rows(3));pieces,_,_=make_chunks(raw,17)
 up=_start(client,h,'API-范围校验',raw,17)
 c=dict(pieces[0]);c['start_byte']=999
 assert client.put(f"/uploads/{up['id']}/chunks/0",headers=h,json=c).status_code==422
 c=dict(pieces[0]);c['payload']='not base64!!!'
 assert client.put(f"/uploads/{up['id']}/chunks/0",headers=h,json=c).status_code==422
 assert client.put(f"/uploads/{up['id']}/chunks/99",headers=h,json=pieces[0]).status_code==422
 assert client.get('/uploads/999999',headers=h).status_code==404
