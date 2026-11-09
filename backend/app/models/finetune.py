"""微调任务 ORM：记录百炼侧 job 与产出模型，供运行时路由切换。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


def _new_id() -> str:
    return uuid.uuid4().hex


class FineTuneJob(Base):
    """一次微调训练任务。

    status: pending(仅本地记录) / uploading / running / succeeded / failed / exported
    served_model_name: 导出后可直接用于 ChatOpenAI 的 model 名
    """
    __tablename__ = "fine_tune_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    dataset_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("datasets.id"), nullable=True)

    base_model: Mapped[str] = mapped_column(String(64), default="qwen3-8b")
    training_type: Mapped[str] = mapped_column(String(16), default="efficient_sft")
    job_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    remote_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    training_file_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    served_model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    hyper_parameters: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    metrics: Mapped[str | None] = mapped_column(Text, nullable=True)           # JSON
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
