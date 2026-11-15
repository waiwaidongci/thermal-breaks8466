import {useEffect,useMemo,useState} from 'react';
import {uploadApi} from '../api/catalog';
import {useResumableUpload,readJobs,type UploadProgress} from '../hooks/useResumableUpload';
import {UPLOAD_STATUS} from '../constants/enums';
import {useAsync} from '../hooks/useAsync';
import {date} from '../utils/format';
import type {UploadSummary} from '../types/domain';

const mb=(n:number)=>(n/1048576).toFixed(2)+' MiB';

export function UploadPanel({user,onPublished}:{user:any;onPublished:()=>void}){
 const [uploads,setUploads]=useState<UploadSummary[]>([]);
 const [file,setFile]=useState<File|null>(null);
 const [name,setName]=useState('');const [desc,setDesc]=useState('');
 const state=useAsync();
 const {progress,upload}=useResumableUpload(reload);
 const jobs=useMemo(()=>readJobs(),[progress?.phase]);

 async function reload(){
  try{setUploads(await uploadApi.list());}catch(e:any){state.act(async()=>{throw e;},'');}
 }
 useEffect(()=>{reload();},[]);

 if(user.role==='viewer'){
  return <section className="input-sheet"><h2>CSV 大文件续传</h2>
   <p>观察员可查看上传进度与隔离原因；导入由分析员处理。</p>
   <UploadList uploads={uploads} jobs={jobs}/></section>;
 }

 async function start(){
  if(!file)return state.act(async()=>{throw new Error('请先选择 CSV 文件');},'');
  if(name.trim().length<2)return state.act(async()=>{throw new Error('请填写至少 2 个字符的批次名称');},'');
  await state.act(()=>upload(file,{name:name.trim(),description:desc.trim()}),'上传任务已结束');
  reload();onPublished();
 }
 async function resumeOne(up:UploadSummary){
  // 续传：重新选择同一文件（浏览器不允许脚本自动读盘），靠名称+大小+摘要匹配回会话
  const input=document.createElement('input');input.type='file';input.accept='.csv,text/csv';
  input.onchange=async()=>{
   const f=input.files?.[0];if(!f)return;
   await state.act(async()=>{
    if(f.size!==up.size)throw new Error('文件大小与原会话不一致，无法续传');
    await upload(f,{name:up.name,description:up.description},512*1024,3,up.id);
   },'续传任务已结束');reload();onPublished();
  };
  input.click();
 }

 return <section className="input-sheet upload-panel">
  <h2>CSV 大文件断点续传</h2>
  <p>逗号分隔（RFC 4180，引号字段可含换行）。分块并发上传，断网或重启后按服务端缺失块续传；
   只有结束声明（点数 + 整文件 SHA-256）、连续序号、时间、温度全部核对通过，批次才原子进入底稿。</p>
  <form onSubmit={e=>{e.preventDefault();start();}}>
   <label>批次名称<input value={name} onChange={e=>setName(e.target.value)} placeholder="如 YG-2412-A 焙程 CSV" maxLength={150}/></label>
   <label>说明<textarea value={desc} onChange={e=>setDesc(e.target.value)} rows={2} maxLength={3000}/></label>
   <label>CSV 文件<input type="file" accept=".csv,text/csv" onChange={e=>{const f=e.target.files?.[0]||null;setFile(f);if(f&&!name)setName(f.name.replace(/\.[^.]+$/,'').slice(0,150));}}/></label>
   {file&&<small>{file.name} · {mb(file.size)}</small>}
   <button className="primary" disabled={state.busy}>{state.busy?'传输中…':'开始 / 续传上传 →'}</button>
  </form>
  {progress&&<ProgressBox p={progress}/>}
  {state.error&&<p className="fire" role="alert">{state.error}</p>}
  {state.message&&<p>{state.message}</p>}
  <UploadList uploads={uploads} jobs={jobs} onResume={resumeOne} busy={state.busy}/>
 </section>;
}

function ProgressBox({p}:{p:UploadProgress}){
 const label={hashing:'摘要计算',uploading:'分块传输',finishing:'结束核对',done:'完成',error:'部分失败',idle:'待机'}[p.phase];
 return <div className="upload-progress">
  <div className="upload-bar"><span style={{width:`${p.percent}%`}}/></div>
  <small>{label} · {p.done}/{p.total} 块 · {p.percent}%{p.failed?` · ${p.failed} 块失败`:''} — {p.message}</small>
 </div>;
}

function UploadList({uploads,jobs,onResume,busy}:{uploads:UploadSummary[];jobs:Record<number,unknown>;onResume?:(u:UploadSummary)=>void;busy?:boolean}){
 if(!uploads.length)return <p className="empty">暂无上传会话。</p>;
 return <div className="upload-list">
  {uploads.map(u=><div key={u.id} className={'upload-row status-'+u.status}>
   <div className="upload-row-head">
    <strong>{u.name}</strong>
    <span className={'badge upload-badge '+u.status}>{UPLOAD_STATUS[u.status]}</span>
   </div>
   <small>{u.filename} · {mb(u.size)} · 块 {u.received_chunks}/{u.total_chunks} · {u.percent}% · {date(u.updated_at)}</small>
   {u.status==='open'&&<>
     <div className="upload-bar slim"><span style={{width:`${u.percent}%`}}/></div>
     <small>缺 {u.missing_count} 块{jobs[u.id]?' · 本机有未完成任务，可续传':''}；暂存 {u.staged_points} 点（未发布，研究端不可见）</small>
     {onResume&&<button disabled={busy} onClick={()=>onResume(u)}>选择同一文件续传 ↗</button>}
   </>}
   {u.status==='quarantined'&&<p className="fire">隔离原因：{u.quarantine_reason}<br/><small>原进度保留且不影响其他批次；请修正文件后新建上传。</small></p>}
   {u.status==='published'&&<small>已发布为底稿批次 #{u.batch_id}，共 {u.staged_points} 点 · {date(u.published_at||'')}</small>}
  </div>)}
 </div>;
}
