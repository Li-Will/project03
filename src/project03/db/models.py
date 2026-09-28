"""SQLAlchemy 五表模型 + 引擎/会话管理（业务数据真值层）。

设计要点：
- 五表：customers / tickets / messages / ticket_events / reviews；
- ticket_events = 状态迁移审计（from_state→to_state + actor + reason），面试"可审计"卖点；
- 引擎惰性创建（configure 可注入临时库，测试隔离用）；SQLite 单文件零运维；
- 时间统一 naive UTC（SQLite 无时区，落库一律 utcnow()）。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from project03.config import get_settings


def utcnow() -> datetime:
    """统一 naive UTC 时钟（与 SQLite 的 datetime 存储兼容）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False)
    tier = Column(String(16), default="普通", nullable=False)   # 普通 | VIP
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    intent = Column(String(32), nullable=False)       # faq/ticket/complaint/need_human
    category = Column(String(64), default="", nullable=False)  # 云盘服务 | 会议支持 | 综合
    priority = Column(String(8), default="P3", nullable=False)  # P1/P2/P3
    state = Column(String(16), default="new", nullable=False)   # TicketState.value
    sla_deadline = Column(DateTime, nullable=True)    # M2 SLA 用
    graph_thread_id = Column(String(32), nullable=True)  # LangGraph checkpoint 线程 id（resume 用）
    assignee = Column(String(16), default="agent", nullable=False)  # agent | human
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False)
    sender = Column(String(16), nullable=False)        # customer | agent | system
    content = Column(Text, nullable=False)
    source = Column(String(16), default="", nullable=False)  # faq/diagnosis/human/template
    created_at = Column(DateTime, default=utcnow, nullable=False)


class TicketEvent(Base):
    """状态迁移审计：每次合法迁移写一行，非法迁移根本不会走到这里。"""
    __tablename__ = "ticket_events"
    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False)
    from_state = Column(String(16), nullable=True)    # 首次创建为 None
    to_state = Column(String(16), nullable=False)
    actor = Column(String(16), nullable=False)        # system | agent | human | customer
    reason = Column(String(256), default="", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Review(Base):
    __tablename__ = "reviews"
    id = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False)
    rating = Column(Integer, nullable=False)          # 1-5
    comment = Column(Text, default="", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


_engine = None
_session_factory: sessionmaker | None = None


def get_engine():
    """惰性创建引擎（第一次访问时才读 settings，避免 import 期副作用）。"""
    global _engine
    if _engine is None:
        db_path = get_settings().db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},  # FastAPI 线程池需要
        )
    return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """会话上下文：提交/回滚/关闭一次管好（业务层全部走这里）。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def configure(db_path: Path | str) -> None:
    """测试隔离入口：换库重建（连同建表+种子客户一并完成)。"""
    global _engine, _session_factory
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(
        f"sqlite:///{p}", connect_args={"check_same_thread": False}
    )
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    Base.metadata.create_all(_engine)
    seed_customers()


def seed_customers() -> None:
    """演示/评测用固定客户（幂等：已存在则跳过）。"""
    with session_scope() as s:
        if s.query(Customer).count() == 0:
            s.add_all([Customer(name="张三", tier="普通"), Customer(name="李总", tier="VIP")])


def init_db() -> None:
    """生产/演示入口：建表 + 种子（沿用既有库，不重建）。"""
    Base.metadata.create_all(get_engine())
    seed_customers()