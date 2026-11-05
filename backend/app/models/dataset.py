"""数据集 ORM（SFT/DPO + 发布给 Agent 的开关）。"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


def _new_id() -> str:
    return uuid.uuid4().hex


class Dataset(Base):
    """SFT / DPO 数据集。is_published=True 时进入 Agent RAG 知识库。"""
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # sft / dpo / raw_knowledge
    type: Mapped[str] = mapped_column(String(16), nullable=False, default="sft")
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    sample_count: Mapped[int] = mapped_column(Integer, default=0)

    # 关键：发布给 Agent 当知识库
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_by: Mapped[str | None] = mapped_column(String(32), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    samples: Mapped[list["DatasetSample"]] = relationship("DatasetSample", back_populates="dataset", cascade="all, delete-orphan")


class DatasetSample(Base):
    """数据集中的单条样本。

    SFT  → instruction + input + output 或 messages_json (ChatML)
    DPO  → prompt + chosen + rejected
    Raw  → content（纯知识条目，直接进 RAG）
    """
    __tablename__ = "dataset_samples"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    dataset_id: Mapped[str] = mapped_column(String(32), ForeignKey("datasets.id"), nullable=False, index=True)

    instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)

    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    chosen: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ChatML / ShareGPT messages
    messages_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 纯知识条目（raw_knowledge 类型数据集用）
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)

    quality: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    dataset: Mapped["Dataset"] = relationship("Dataset", back_populates="samples")
