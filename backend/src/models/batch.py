from sqlalchemy.orm import Mapped,mapped_column
from sqlalchemy import String,Text,Integer,ForeignKey
from src.config.database import Base
class ThermalBatch(Base):
 __tablename__='thermal_batches'
 id:Mapped[int]=mapped_column(primary_key=True)
 name:Mapped[str]=mapped_column(String(150),unique=True)
 filename:Mapped[str]=mapped_column(String(180))
 description:Mapped[str]=mapped_column(Text,default='')
 # published 才进入研究读取面；CSV 上传在发布成功前不可见
 status:Mapped[str]=mapped_column(String(20),default='published')
 source_upload_id:Mapped[int]=mapped_column(Integer,ForeignKey('csv_uploads.id'),nullable=True)
