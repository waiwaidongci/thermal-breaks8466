"""RFC 4180 流式 CSV 解析器（字节级，状态可持久化）。

设计目标：
- 每次只喂一个分块（原始字节），解析器状态可 JSON 持久化到数据库。进程或网络在
  半个 UTF-8 字符、半个引号字段、半条记录处中断，重启后用同样的字节序列即可继续；
- 暂存额外内存只与“当前半条记录”有关，不随文件总大小增长（上限 MAX_RECORD_CHARS）；
- 引号字段可含换行、逗号与转义双引号；CRLF、LF、CR 三种行结束都支持；
- 解析失败抛出 CsvFormatError，由服务层隔离上传，不影响其他批次。
"""

MAX_RECORD_CHARS=65536  # 单条记录字符上限，截断的巨型记录在此被拒绝而不是持续吃内存


class CsvFormatError(ValueError):
 def __init__(self,message,line=None):
  super().__init__(message if line is None else f'第{line}行：{message}')
  self.message=message
  self.line=line


def initial_state():
 # 全部是 JSON 原生类型，可直接存 JSON 列
 return {'mode':'normal','field':'','fields':[],'line':1,'record_open':False,
         'utf8_pending':[],'crlf_pending':False,'saw_any':False,'header_skipped':False,
         'bom_skipped':False}


def _char_len(first):
 if first<0x80:return 1
 if first>>5==0b110:return 2
 if first>>4==0b1110:return 3
 if first>>3==0b11110:return 4
 raise ValueError('非法 UTF-8 首字节')


def _iter_chars(pending,chunk):
 """把上一块残字节与本块拼成完整字符。返回 (字符列表, 新残字节)。"""
 data=bytes(pending)+chunk;out=[];i=0
 while i<len(data):
  try:n=_char_len(data[i])
  except ValueError as e:raise CsvFormatError(str(e)) from e
  if i+n>len(data):break  # 截在多字节字符中间，等下一块
  seq=data[i:i+n]
  try:out.append(seq.decode('utf-8'))
  except UnicodeDecodeError as e:raise CsvFormatError('UTF-8 字节序列损坏') from e
  i+=n
 return out,list(data[i:])


def _emit_record(state):
 fields=state['fields']
 fields.append(state['field'])
 state['fields']=[]
 state['field']=''
 state['record_open']=False
 return fields


def _check_size(state):
 if len(state['field'])>MAX_RECORD_CHARS or sum(len(f) for f in state['fields'])>MAX_RECORD_CHARS:
  raise CsvFormatError(f'单条记录超过 {MAX_RECORD_CHARS} 字符上限',state['line'])


def feed(state,chunk:bytes):
 """喂入一段原始字节，返回完整记录列表，每条记录是 list[str]。

 未结束的半条记录、半个字符留在 state；调用方持久化 state 即可断点续传。
 """
 if not state['bom_skipped']:
  state['bom_skipped']=True
  head=bytes(state['utf8_pending'])+chunk
  if head.startswith(b'\xef\xbb\xbf'):
   head=head[3:]  # 文件起始的 UTF-8 BOM，即使跨前两块也能识别
  state['utf8_pending']=[];chunk=head
 chars,pending=_iter_chars(state['utf8_pending'],chunk)
 state['utf8_pending']=pending
 records=[]
 for ch in chars:
  if state['crlf_pending'] and ch!='\n':
   state['crlf_pending']=False  # 单独的 CR 已作为换行处理
  mode=state['mode']
  if mode=='normal':
   if ch==',':
    state['fields'].append(state['field']);state['field']='';state['record_open']=True
   elif ch=='"':
    if state['field']:raise CsvFormatError('引号只能出现在字段开头',state['line'])
    state['mode']='quoted';state['record_open']=True
   elif ch=='\n':
    if state['record_open'] or state['fields'] or state['field']:
     records.append(_emit_record(state))
    state['line']+=1
   elif ch=='\r':
    if state['record_open'] or state['fields'] or state['field']:
     records.append(_emit_record(state))
    state['line']+=1;state['crlf_pending']=True
   else:
    state['field']+=ch;state['record_open']=True;state['saw_any']=True
  elif mode=='quoted':
   if ch=='"':state['mode']='quote_escape'
   else:
    # 引号内换行合法（含 CRLF/CR），行号仍随物理换行推进
    if ch=='\n':state['line']+=1
    elif ch=='\r':state['line']+=1
    state['field']+=ch;state['record_open']=True
  else:  # quote_escape：引号后的字符
   if ch=='"':state['field']+='"';state['mode']='quoted'
   elif ch==',':state['fields'].append(state['field']);state['field']='';state['mode']='normal'
   elif ch=='\n':records.append(_emit_record(state));state['mode']='normal';state['line']+=1
   elif ch=='\r':records.append(_emit_record(state));state['mode']='normal';state['line']+=1;state['crlf_pending']=True
   else:raise CsvFormatError('引号后只允许逗号、换行或转义引号',state['line'])
  _check_size(state)
 return records


def finish(state):
 """声明文件结束。返回最后一条未换行的记录（若有），并校验引号/字节残片。"""
 if state['utf8_pending']:
  raise CsvFormatError('文件结束在 UTF-8 多字节字符中间',state['line'])
 if state['mode']=='quoted':
  raise CsvFormatError('引号字段没有闭合',state['line'])
 if state['mode']=='quote_escape':
  state['mode']='normal'  # 结尾恰好是闭合引号
 records=[_emit_record(state)] if (state['record_open'] or state['fields'] or state['field']) else []
 state['crlf_pending']=False
 return records
