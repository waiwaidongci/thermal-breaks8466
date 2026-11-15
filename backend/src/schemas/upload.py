from pydantic import BaseModel,Field

class UploadCreate(BaseModel):
 name:str=Field(min_length=2,max_length=150)
 filename:str=Field(min_length=2,max_length=180)
 description:str=Field(default='',max_length=3000)
 size:int=Field(gt=0,le=200*1024*1024)  # 单文件 200MiB 上限
 sha256:str=Field(min_length=64,max_length=64,pattern=r'^[0-9a-f]{64}$')
 chunk_size:int=Field(gt=0,le=8*1024*1024)
 total_chunks:int=Field(gt=0,le=20000)

class ChunkInput(BaseModel):
 index:int=Field(ge=0)
 start_byte:int=Field(ge=0)
 end_byte:int=Field(ge=1)
 sha256:str=Field(min_length=64,max_length=64,pattern=r'^[0-9a-f]{64}$')
 # Base64 原始字节；JSON 传输，前端可 FileReader 分块读取，不需要 multipart 缓冲整文件
 payload:str=Field(min_length=1)

class FinishInput(BaseModel):
 # 结束记录声明：点数与摘要（整文件 SHA-256），与创建会话时的声明一致
 declared_points:int=Field(ge=0)
 sha256:str=Field(min_length=64,max_length=64,pattern=r'^[0-9a-f]{64}$')
