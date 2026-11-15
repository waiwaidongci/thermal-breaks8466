import {useCallback,useRef,useState} from 'react';
import {uploadApi} from '../api/catalog';
import type {UploadSummary} from '../types/domain';

// 浏览器原生 SubtleCrypto 计算 SHA-256（整文件与每块都用同一算法，和后端 hexdigest 对齐）
export async function sha256Hex(buf:ArrayBuffer):Promise<string>{
 const out=await crypto.subtle.digest('SHA-256',buf);
 return Array.from(new Uint8Array(out)).map(b=>b.toString(16).padStart(2,'0')).join('');
}
export function base64FromBytes(bytes:Uint8Array):string{
 let bin='';const step=0x8000;
 for(let i=0;i<bytes.length;i+=step)bin+=String.fromCharCode(...bytes.subarray(i,i+step));
 return btoa(bin);
}
// 解析本地 CSV 物理记录数（结束声明用）；引号字段可含换行，需状态机扫描
export function countCsvRecords(text:string):number{
 let records=0,firstField='',inQuotes=false,recordStarted=false,firstDone=false;
 for(let i=0;i<text.length;i++){
  const ch=text[i];
  if(inQuotes){if(ch==='"'){if(text[i+1]==='"')i++;else inQuotes=false;}continue;}
  if(ch==='"'){inQuotes=true;recordStarted=true;if(!firstDone)firstField+='"';}
  else if(ch===','){if(!firstDone)firstDone=true;}
  else if(ch==='\n'||ch==='\r'){
   if(recordStarted){records++;if(ch==='\r'&&text[i+1]==='\n')i++;}
   firstDone=true;recordStarted=false;
  }else{recordStarted=true;if(!firstDone)firstField+=ch;}
 }
 if(recordStarted)records++;
 // 与后端一致：仅首条记录首列不是整数时才按表头扣除一行
 const hasHeader=records>0&&!/^[+-]?\d+$/.test(firstField.trim());
 return hasHeader?records-1:records;
}

export type UploadPhase='idle'|'hashing'|'uploading'|'finishing'|'done'|'error';
export type UploadProgress={uploadId:number;total:number;done:number;failed:number;percent:number;phase:UploadPhase;message:string};

const STORAGE_KEY='yangu-csv-upload-v1';
type SavedJob={uploadId:number;name:string;chunkSize:number;total:number;size:number;sha256:string;declared:number};

function saveJob(job:SavedJob){const all=readJobs();all[job.uploadId]=job;localStorage.setItem(STORAGE_KEY,JSON.stringify(all));}
export function readJobs():Record<number,SavedJob>{try{return JSON.parse(localStorage.getItem(STORAGE_KEY)||'{}');}catch{return {};}}
function clearJob(id:number){const all=readJobs();delete all[id];localStorage.setItem(STORAGE_KEY,JSON.stringify(all));}

export function useResumableUpload(onChange:()=>void){
 const [progress,setProgress]=useState<UploadProgress|null>(null);
 const running=useRef(false);

 const update=useCallback((p:UploadProgress)=>setProgress({...p}),[]);

 const upload=useCallback(async(file:File,meta:{name:string;description:string},
   chunkSizeArg=512*1024,concurrency=3,existingId=0)=>{
  if(running.current)throw new Error('已有上传在进行');
  running.current=true;
  try{
   let chunkSize=chunkSizeArg;
   const size=file.size;
   update({uploadId:0,total:0,done:0,failed:0,percent:0,phase:'hashing',message:'计算整文件 SHA-256…'});
   const wholeBuf=await file.arrayBuffer();
   const sha256=await sha256Hex(wholeBuf);
   const declared=countCsvRecords(new TextDecoder().decode(wholeBuf));
   // 优先用显式传入的会话（跨浏览器/清缓存后续传）；否则按本地同名同摘要任务复用
   const jobs=readJobs();
   let uploadId=existingId;
   if(!uploadId)for(const j of Object.values(jobs))if(j.name===meta.name&&j.size===size&&j.sha256===sha256){
    uploadId=j.uploadId;chunkSize=j.chunkSize;break;
   }
   let total=Math.max(1,Math.ceil(size/chunkSize));
   if(!uploadId){
    const created=await uploadApi.create({name:meta.name,filename:file.name,description:meta.description,
     size,sha256,chunk_size:chunkSize,total_chunks:total});
    uploadId=created.id;saveJob({uploadId,name:meta.name,chunkSize,total,size,sha256,declared});
   }
   // 以服务端缺失块为准，实现跨标签页/重启后的断点续传
   const fresh=await uploadApi.one(uploadId);
   if(fresh.status==='quarantined')throw new Error('该上传已隔离：'+fresh.quarantine_reason);
   if(uploadId){ // 续传既有会话：块大小/块数以服务端为权威
    chunkSize=fresh.chunk_size;total=fresh.total_chunks;
    saveJob({uploadId,name:fresh.name,chunkSize,total,size,sha256,declared});
   }
   if(fresh.size!==size||fresh.sha256!==sha256)
    throw new Error('文件大小或 SHA-256 与原会话不一致，无法续传');
   const pending:number[]=fresh.status==='open'?fresh.missing_chunks.slice():[];
   let doneChunks=total-pending.length;let failed=0;
   update({uploadId,total,done:doneChunks,failed,percent:Math.round(100*doneChunks/total),phase:'uploading',message:`分块上传中（并发 ${concurrency}）…`});

   let cursor=0;
   const worker=async()=>{
    while(cursor<pending.length){
     const index=pending[cursor++];
     const start=index*chunkSize;const end=Math.min(size,start+chunkSize);
     const slice=wholeBuf.slice(start,end);
     const bytes=new Uint8Array(slice);
     const payload=base64FromBytes(bytes);
     const digest=await sha256Hex(slice);
     try{
      await uploadApi.putChunk(uploadId,index,{index,start_byte:start,end_byte:end,sha256:digest,payload});
      doneChunks++;
      update({uploadId,total,done:doneChunks,failed,percent:Math.round(100*doneChunks/total),phase:'uploading',message:`分块 ${index+1}/${total} 已确认`});
     }catch(e:any){
      failed++;
      update({uploadId,total,done:doneChunks,failed,percent:Math.round(100*doneChunks/total),phase:'error',message:`分块 ${index+1} 失败：${e.message}；可点续传重试`});
      throw e;
     }
    }
   };
   await Promise.all(Array.from({length:Math.min(concurrency,pending.length||1)},()=>worker()));

   update({uploadId,total,done:total,failed:0,percent:100,phase:'finishing',message:'提交结束声明并核对…'});
   await uploadApi.finish(uploadId,declared,sha256);
   clearJob(uploadId);
   update({uploadId,total,done:total,failed:0,percent:100,phase:'done',message:'核对通过，批次已原子进入底稿'});
   onChange();
  }finally{running.current=false;}
 },[onChange,update]);

 return {progress,upload,running};
}
