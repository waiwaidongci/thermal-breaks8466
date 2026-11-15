from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy.orm import Session
from src.config.database import get_db
from src.controllers.dependencies import current_user,writers
from src.schemas.upload import UploadCreate,ChunkInput,FinishInput
from src.services import uploads
router=APIRouter()

@router.get('/uploads')
def list_uploads(db:Session=Depends(get_db),user=Depends(current_user)):
 return uploads.list_uploads(db)

@router.get('/uploads/{upload_id}')
def one(upload_id:int,db:Session=Depends(get_db),user=Depends(current_user)):
 return uploads.get_upload(db,upload_id)

@router.post('/uploads')
def create(data:UploadCreate,db:Session=Depends(get_db),user=Depends(writers)):
 return uploads.create_upload(db,data,user.email)

@router.put('/uploads/{upload_id}/chunks/{index}')
def chunk(upload_id:int,index:int,data:ChunkInput,db:Session=Depends(get_db),user=Depends(writers)):
 if data.index!=index:raise HTTPException(422,'路径与正文分块序号不一致')
 return uploads.put_chunk(db,upload_id,data,user.email)

@router.post('/uploads/{upload_id}/finish')
def finish(upload_id:int,data:FinishInput,db:Session=Depends(get_db),user=Depends(writers)):
 return uploads.finish_upload(db,upload_id,data,user.email)
