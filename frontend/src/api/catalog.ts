import {request,post} from './client';
export const catalogApi={
 batches:()=>request('/batches'),
 points:()=>request('/points'),
 create:(data:any)=>post('/batches',data),
 edit:(id:number,temperature:number)=>request('/points/'+id,{method:'PATCH',body:JSON.stringify({temperature})}),
};
export const uploadApi={
 list:()=>request('/uploads'),
 one:(id:number)=>request('/uploads/'+id),
 create:(data:any)=>post('/uploads',data),
 putChunk:(id:number,index:number,data:any)=>request('/uploads/'+id+'/chunks/'+index,{method:'PUT',body:JSON.stringify(data)}),
 finish:(id:number,declaredPoints:number,sha256:string)=>post('/uploads/'+id+'/finish',{declared_points:declaredPoints,sha256}),
};
