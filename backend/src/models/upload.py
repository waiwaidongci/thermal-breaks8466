from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,Text,Integer,BigInteger,DateTime,ForeignKey,UniqueConstraint,Index,JSON,LargeBinary,Float
from datetime import datetime,timezone
from src.config.database import Base

class CsvUpload(Base):
 __tablename__='csv_uploads'
 id:Mapped[int]=mapped_column(primary_key=True)
 name:Mapped[str]=mapped_column(String(150),unique=True)
 filename:Mapped[str]=mapped_column(String(180))
 description:Mapped[str]=mapped_column(Text,default='')
 size:Mapped[int]=mapped_column(BigInteger)
 sha256:Mapped[str]=mapped_column(String(64))
 chunk_size:Mapped[int]=mapped_column(Integer)
 total_chunks:Mapped[int]=mapped_column(Integer)
 # open：可继续传块；quarantined：损坏隔离，只可读；published：已原子进入底稿
 status:Mapped[str]=mapped_column(String(20),default='open',index=True)
 received_chunks:Mapped[int]=mapped_column(Integer,default=0)
 received_bytes:Mapped[int]=mapped_column(BigInteger,default=0)
 next_expected_chunk:Mapped[int]=mapped_column(Integer,default=0)
 staged_points:Mapped[int]=mapped_column(Integer,default=0)
 # 流式解析器与跨块校验状态（JSON，体积只与当前半条记录有关）
 parser_state:Mapped[dict]=mapped_column(JSON,default=dict)
 validate_state:Mapped[dict]=mapped_column(JSON,default=dict)
 quarantine_reason:Mapped[str]=mapped_column(Text,default='')
 quarantined_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=True)
 batch_id:Mapped[int]=mapped_column(ForeignKey('thermal_batches.id'),nullable=True)
 created_by:Mapped[str]=mapped_column(String(120))
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
 updated_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),onupdate=lambda:datetime.now(timezone.utc))
 published_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=True)

class CsvUploadChunk(Base):
 __tablename__='csv_upload_chunks'
 __table_args__=(UniqueConstraint('upload_id','index',name='uq_upload_chunk'),
  Index('ix_upload_chunk_order','upload_id','index'))
 id:Mapped[int]=mapped_column(primary_key=True)
 upload_id:Mapped[int]=mapped_column(ForeignKey('csv_uploads.id'))
 index:Mapped[int]=mapped_column(Integer)
 start_byte:Mapped[int]=mapped_column(BigInteger)
 end_byte:Mapped[int]=mapped_column(BigInteger)
 sha256:Mapped[str]=mapped_column(String(64))
 payload:Mapped[bytes]=mapped_column(LargeBinary)
 created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))

class CsvStagedPoint(Base):
 __tablename__='csv_staged_points'
 __table_args__=(UniqueConstraint('upload_id','sequence',name='uq_staged_sequence'),)
 id:Mapped[int]=mapped_column(primary_key=True)
 upload_id:Mapped[int]=mapped_column(ForeignKey('csv_uploads.id'))
 sequence:Mapped[int]=mapped_column(Integer)
 recorded_at:Mapped[datetime]=mapped_column(DateTime(timezone=True))
 temperature:Mapped[float]=mapped_column(Float)
