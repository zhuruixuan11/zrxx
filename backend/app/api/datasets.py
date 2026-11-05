"""数据集管理 API（SFT / DPO / Raw Knowledge + 发布给 Agent RAG）。"""
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select, func

from ..database import async_session
from ..models.dataset import Dataset, DatasetSample
from ..models.user import User
from ..security import get_current_user

router = APIRouter(prefix="/api/datasets", tags=["datasets"])


# ============ Schema ============
class DatasetCreate(BaseModel):
    name: str
    description: str = ""
    type: str = "sft"  # sft / dpo / raw_knowledge


class DatasetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class SampleCreate(BaseModel):
    instruction: Optional[str] = None
    input_text: Optional[str] = None
    output: Optional[str] = None
    prompt: Optional[str] = None
    chosen: Optional[str] = None
    rejected: Optional[str] = None
    topic: Optional[str] = None
    content: Optional[str] = None
    messages: Optional[list[dict]] = None


class SampleBatch(BaseModel):
    samples: list[SampleCreate]


# ============ 工具 ============
def _sample_to_dict(s: DatasetSample) -> dict:
    return {
        "id": s.id,
        "topic": s.topic,
        "content": s.content,
        "instruction": s.instruction,
        "input_text": s.input_text,
        "output": s.output,
        "prompt": s.prompt,
        "chosen": s.chosen,
        "rejected": s.rejected,
        "messages": json.loads(s.messages_json) if s.messages_json else None,
        "quality": s.quality,
        "note": s.note,
        "created_at": s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else None,
    }


def _dataset_to_dict(d: Dataset) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "description": d.description,
        "type": d.type,
        "sample_count": d.sample_count,
        "is_published": d.is_published,
        "meta": json.loads(d.meta_json) if d.meta_json else None,
        "created_at": d.created_at.strftime("%Y-%m-%d %H:%M") if d.created_at else None,
        "updated_at": d.updated_at.strftime("%Y-%m-%d %H:%M") if d.updated_at else None,
    }


async def _refresh_count(session, dataset_id: str):
    cnt = (await session.execute(
        select(func.count()).select_from(DatasetSample).where(DatasetSample.dataset_id == dataset_id)
    )).scalar_one()
    ds = (await session.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one()
    ds.sample_count = cnt
    ds.updated_at = datetime.utcnow()


# ============ CRUD ============
@router.get("")
async def list_datasets(type: Optional[str] = None, user: User = Depends(get_current_user)):
    async with async_session() as session:
        stmt = select(Dataset).order_by(Dataset.is_published.desc(), Dataset.updated_at.desc())
        if type:
            stmt = stmt.where(Dataset.type == type)
        rows = (await session.execute(stmt)).scalars().all()
    return {"code": 0, "msg": "ok", "data": [_dataset_to_dict(r) for r in rows]}


@router.post("")
async def create_dataset(req: DatasetCreate, user: User = Depends(get_current_user)):
    if req.type not in ("sft", "dpo", "raw_knowledge"):
        raise HTTPException(status_code=400, detail="type 必须是 sft / dpo / raw_knowledge")
    async with async_session() as session:
        ds = Dataset(name=req.name, description=req.description, type=req.type, created_by=user.id)
        session.add(ds)
        await session.commit()
        await session.refresh(ds)
    return {"code": 0, "msg": "ok", "data": _dataset_to_dict(ds)}


@router.get("/{dataset_id}")
async def get_dataset(dataset_id: str, user: User = Depends(get_current_user)):
    async with async_session() as session:
        ds = (await session.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")
        return {"code": 0, "msg": "ok", "data": _dataset_to_dict(ds)}


@router.put("/{dataset_id}")
async def update_dataset(dataset_id: str, req: DatasetUpdate, user: User = Depends(get_current_user)):
    async with async_session() as session:
        ds = (await session.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")
        if req.name is not None:
            ds.name = req.name
        if req.description is not None:
            ds.description = req.description
        ds.updated_at = datetime.utcnow()
        await session.commit()
    return {"code": 0, "msg": "ok", "data": _dataset_to_dict(ds)}


@router.delete("/{dataset_id}")
async def delete_dataset(dataset_id: str, user: User = Depends(get_current_user)):
    async with async_session() as session:
        ds = (await session.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")
        await session.delete(ds)
        await session.commit()
    return {"code": 0, "msg": "ok"}


# ============ 发布给 Agent（关键！让数据集进入 RAG 知识库）============
@router.post("/{dataset_id}/publish")
async def publish_dataset(dataset_id: str, publish: bool = True, user: User = Depends(get_current_user)):
    """发布或取消发布数据集。发布后，Agent 对话时会自动从这些数据集里检索知识。"""
    async with async_session() as session:
        ds = (await session.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")
        ds.is_published = bool(publish)
        ds.updated_at = datetime.utcnow()
        await session.commit()

        msg = f"已发布给 Agent（{ds.sample_count} 条样本）" if publish else "已取消发布"
    return {"code": 0, "msg": msg, "data": _dataset_to_dict(ds)}


# ============ 样本 ============
@router.get("/{dataset_id}/samples")
async def list_samples(dataset_id: str, page: int = 1, size: int = 20, user: User = Depends(get_current_user)):
    async with async_session() as session:
        ds = (await session.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")
        total = (await session.execute(
            select(func.count()).select_from(DatasetSample).where(DatasetSample.dataset_id == dataset_id)
        )).scalar_one()
        offset = (page - 1) * size
        rows = (await session.execute(
            select(DatasetSample).where(DatasetSample.dataset_id == dataset_id)
            .order_by(DatasetSample.created_at.desc()).offset(offset).limit(size)
        )).scalars().all()
    return {
        "code": 0, "msg": "ok",
        "data": {"list": [_sample_to_dict(r) for r in rows], "total": total, "page": page, "size": size},
    }


@router.post("/{dataset_id}/samples")
async def add_sample(dataset_id: str, req: SampleCreate, user: User = Depends(get_current_user)):
    async with async_session() as session:
        ds = (await session.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")
        sample = DatasetSample(
            dataset_id=dataset_id,
            instruction=req.instruction, input_text=req.input_text, output=req.output,
            prompt=req.prompt, chosen=req.chosen, rejected=req.rejected,
            topic=req.topic, content=req.content,
            messages_json=json.dumps(req.messages, ensure_ascii=False) if req.messages else None,
        )
        session.add(sample)
        await session.commit()
        await _refresh_count(session, dataset_id)
        await session.commit()
        await session.refresh(sample)
    return {"code": 0, "msg": "ok", "data": _sample_to_dict(sample)}


@router.post("/{dataset_id}/samples/batch")
async def batch_add_samples(dataset_id: str, req: SampleBatch, user: User = Depends(get_current_user)):
    async with async_session() as session:
        ds = (await session.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")
        created = 0
        for s in req.samples:
            session.add(DatasetSample(
                dataset_id=dataset_id,
                instruction=s.instruction, input_text=s.input_text, output=s.output,
                prompt=s.prompt, chosen=s.chosen, rejected=s.rejected,
                topic=s.topic, content=s.content,
                messages_json=json.dumps(s.messages, ensure_ascii=False) if s.messages else None,
            ))
            created += 1
        await session.commit()
        await _refresh_count(session, dataset_id)
        await session.commit()
    return {"code": 0, "msg": "ok", "data": {"created": created}}


@router.delete("/{dataset_id}/samples/{sample_id}")
async def delete_sample(dataset_id: str, sample_id: str, user: User = Depends(get_current_user)):
    async with async_session() as session:
        s = (await session.execute(
            select(DatasetSample).where(DatasetSample.id == sample_id, DatasetSample.dataset_id == dataset_id)
        )).scalar_one_or_none()
        if not s:
            raise HTTPException(status_code=404, detail="样本不存在")
        await session.delete(s)
        await session.commit()
        await _refresh_count(session, dataset_id)
        await session.commit()
    return {"code": 0, "msg": "ok"}


# ============ 上传 JSONL ============
@router.post("/{dataset_id}/upload")
async def upload_jsonl(dataset_id: str, file: UploadFile = File(...), user: User = Depends(get_current_user)):
    if not file.filename:
        raise HTTPException(status_code=400, detail="未上传文件")
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gbk")

    items = []
    fname = file.filename.lower()
    if fname.endswith(".jsonl"):
        for line in text.strip().splitlines():
            line = line.strip()
            if line:
                items.append(json.loads(line))
    elif fname.endswith(".json"):
        data = json.loads(text)
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and "data" in data:
            items = data["data"]
        elif isinstance(data, dict) and "samples" in data:
            items = data["samples"]
        else:
            raise HTTPException(status_code=400, detail="JSON 格式无法识别")
    else:
        raise HTTPException(status_code=400, detail="只支持 .jsonl / .json")

    created = 0
    async with async_session() as session:
        ds = (await session.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")
        for item in items:
            sample = DatasetSample(dataset_id=dataset_id)
            if "messages" in item:
                sample.messages_json = json.dumps(item["messages"], ensure_ascii=False)
            sample.topic = item.get("topic")
            sample.content = item.get("content")
            sample.instruction = item.get("instruction") or item.get("question") or item.get("prompt")
            sample.input_text = item.get("input") or item.get("context")
            sample.output = item.get("output") or item.get("answer") or item.get("response")
            sample.prompt = item.get("prompt")
            sample.chosen = item.get("chosen")
            sample.rejected = item.get("rejected")
            session.add(sample)
            created += 1
        await session.commit()
        await _refresh_count(session, dataset_id)
        await session.commit()
    return {"code": 0, "msg": "ok", "data": {"created": created, "filename": file.filename}}


# ============ 种子：SFT + DPO + 直接可用的 Raw Knowledge（一发布就被 Agent 吸收）============
_SEED_SFT = [
    {"messages": [
        {"role": "system", "content": "你是衡策，一个专业的 A 股投研助手，擅长技术分析、基本面解读与行业研究。回答要中文、数据驱动、不构成投资建议。"},
        {"role": "user", "content": "贵州茅台最近走势怎么样？帮我看看。"},
        {"role": "assistant", "content": "好的，我来拉取贵州茅台（600519.SH）的实时行情和近期 K 线。你也可以告诉我关注的时间周期（日K / 周K / 月K），我可以做更深入的技术形态分析。⚠️ 以上分析仅供学习参考，不构成投资建议。"},
    ]},
    {"messages": [
        {"role": "system", "content": "你是衡策，一个专业的 A 股投研助手。"},
        {"role": "user", "content": "AI 算力板块还有机会吗？"},
        {"role": "assistant", "content": "AI 算力（CPO、光模块、服务器）是 2024-2025 年的主线之一。建议关注三个方向：\n1）光模块：中际旭创、新易盛（需注意估值回调风险）；\n2）服务器整机：浪潮信息、紫光股份；\n3）液冷：英维克、高澜股份。\n你想让我拉出这几个的行情对比吗？⚠️ 不构成投资建议。"},
    ]},
    {"messages": [
        {"role": "system", "content": "你是衡策，一个专业的 A 股投研助手。"},
        {"role": "user", "content": "怎么看央行降准对市场的影响？"},
        {"role": "assistant", "content": "降准释放长期流动性，一般利好：\n• 银行 / 非银（息差压力缓解）\n• 地产（资金面改善预期）\n• 成长板块（估值分母下降）\n但也要看降准幅度、配套政策和市场所处位置。你想了解历史上几次降准后沪深 300 的表现吗？⚠️ 不构成投资建议。"},
    ]},
    {"messages": [
        {"role": "system", "content": "你是衡策，一个专业的 A 股投研助手。"},
        {"role": "user", "content": "帮我做一个 50 万的稳健型定投计划。"},
        {"role": "assistant", "content": "稳健型建议：\n• 核心 60%：沪深 300 ETF + 中证 A50 ETF（宽基）\n• 卫星 25%：红利低波 ETF + 债券基金\n• 对冲 10%：黄金 ETF\n• 机动 5%：保留现金定投波动率 > 1.5% 时加仓\n每月 2-3 次定投，避开月初月末。⚠️ 仅供参考，不构成投资建议。"},
    ]},
    {"messages": [
        {"role": "system", "content": "你是衡策，一个专业的 A 股投研助手。"},
        {"role": "user", "content": "什么是龙虎榜？怎么解读？"},
        {"role": "assistant", "content": "龙虎榜是交易所每日公布的异动股（涨跌幅 ±7%、换手率 >20%、连续涨停等）的前五买卖席位。解读要点：\n1）看机构专用席位净买入/净卖出（机构动向更有参考性）；\n2）看知名游资（章盟主、方新侠等）席位；\n3）连续 2-3 天上榜且机构净买入，往往有资金持续关注。\n⚠️ 游资动向短期噪音大，不能单独作为买卖依据。"},
    ]},
]

_SEED_DPO = [
    {"prompt": "帮我看看宁德时代最近的技术形态",
     "chosen": "宁德时代（300750.SZ）近期在 60 日均线附近获得支撑，MACD 绿柱缩短有底背离迹象。建议关注能否放量突破 20 日均线（约 XX 元）。⚠️ 不构成投资建议。",
     "rejected": "宁德时代是个好公司，买入吧。"},
    {"prompt": "推荐一下本周关注的板块",
     "chosen": "本周可关注三条主线：1）AI 算力（事件驱动：某大厂算力招标）；2）低空经济（政策催化）；3）创新药（BD 交易密集）。每个方向 2-3 只标的供你进一步研究。⚠️ 不构成投资建议。",
     "rejected": "买券商，牛市要来了。"},
    {"prompt": "中国平安现在能抄底吗？",
     "chosen": "中国平安（601318.SH）当前 PB 约 0.8x，处于历史低位区间。但保险板块受长端利率和地产风险影响，右侧信号需要看到：1）10Y 国债收益率企稳回升；2）地产风险进一步出清。建议分批建仓而非一把梭。⚠️ 不构成投资建议。",
     "rejected": "平安跌这么多了，肯定能涨，满仓干。"},
]

# 这种类型是直接喂给 Agent 当 RAG 知识的，一发布马上生效
_SEED_RAW = [
    {"topic": "量化交易常用因子", "content": "常见量化因子分为：价值（PE/PB/ROE）、动量（收益率/换手率）、质量（毛利率/现金流）、波动（波动率/最大回撤）、成长（营收增速/净利增速）。多因子模型通常用 IC/IR 筛选有效因子，再做加权合成。"},
    {"topic": "A股行业轮动规律", "content": "A股行业轮动遵循「冬季大消费、春季科技成长、夏季周期资源、秋季金融地产」的季节性特征，但近年政策和海外流动性影响更大。建议结合 M2、社融、北向资金三项指标判断大方向。"},
    {"topic": "科创板 vs 创业板", "content": "科创板（688xxx）硬科技定位，50 万门槛，前 5 日无涨跌停，之后 ±20%。创业板（300xxx）成长型创新创业，20 万门槛，同样 ±20%。科创板更偏硬核技术（半导体、AI、生物医药），创业板覆盖更广。"},
    {"topic": "北交所特点", "content": "北交所（BJ）精选层升级而来，服务创新型中小企业，30% 分红要求，整体流动性偏弱。适合挖掘专精特新「小巨人」，但需注意交易清淡带来的滑点风险。"},
]


@router.post("/seed")
async def seed_datasets(user: User = Depends(get_current_user)):
    """一键初始化 SFT + DPO + Raw Knowledge 三个种子集（Raw 直接发布给 Agent）。"""
    async with async_session() as session:
        names_seen = {r[0] for r in (await session.execute(
            select(Dataset.name)
        )).all()}

        created_any = False

        if "A股投研 SFT 种子集" not in names_seen:
            ds = Dataset(name="A股投研 SFT 种子集", type="sft",
                         description="内置 A 股投研指令微调种子数据（5 条 ChatML 风格）",
                         created_by=user.id)
            session.add(ds); await session.commit(); await session.refresh(ds)
            for item in _SEED_SFT:
                session.add(DatasetSample(dataset_id=ds.id, messages_json=json.dumps(item["messages"], ensure_ascii=False)))
            await session.commit(); await _refresh_count(session, ds.id); await session.commit()
            created_any = True

        if "A股投研 DPO 种子集" not in names_seen:
            ds = Dataset(name="A股投研 DPO 种子集", type="dpo",
                         description="偏好优化样本（chosen=专业回答, rejected=韭菜回答）",
                         created_by=user.id)
            session.add(ds); await session.commit(); await session.refresh(ds)
            for item in _SEED_DPO:
                session.add(DatasetSample(dataset_id=ds.id, prompt=item["prompt"],
                                          chosen=item["chosen"], rejected=item["rejected"]))
            await session.commit(); await _refresh_count(session, ds.id); await session.commit()
            created_any = True

        # 这个一创建就自动发布，Agent 对话马上吸收
        if "A股投研知识条目" not in names_seen:
            ds = Dataset(name="A股投研知识条目", type="raw_knowledge",
                         description="纯知识条目（直接发布给 Agent 当 RAG 知识库）",
                         is_published=True, created_by=user.id)
            session.add(ds); await session.commit(); await session.refresh(ds)
            for item in _SEED_RAW:
                session.add(DatasetSample(dataset_id=ds.id, topic=item["topic"], content=item["content"]))
            await session.commit(); await _refresh_count(session, ds.id); await session.commit()
            created_any = True

    return {"code": 0, "msg": "ok", "data": {"seeded": created_any, "hint": "「A股投研知识条目」已自动发布，Agent 现在就能用这些知识回答问题"}}
