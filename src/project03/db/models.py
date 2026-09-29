"""SQLAlchemy 五表模型 + 引擎/会话管理（业务数据真值层）。

设计要点：
- 五表：customers / tickets / messages / ticket_events / reviews；
- ticket_events = 状态迁移审计（from_state→to_state + actor + reason），面试"可审计"卖点；
- **多租户**：customers / tickets 带 tenant_id（P0-3），客户名在租户内唯一，查询按租户过滤；
- 引擎惰性创建（configure 可注入临时库，测试隔离用）；SQLite 单文件零运维；
- 时间统一 naive UTC（SQLite 无时区，落库一律 utcnow()）；
- 轻量迁移：create_all 不会给已存在的表加列 → `_ensure_tenant_columns` 幂等补列
  （生产规模请上 Alembic；这里保持零额外依赖，但迁移这件事必须有位置发生）。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from project03.config import get_settings


DEFAULT_TENANT = "default"   # 未启用多租户时的归属（与 settings.default_tenant 同值）


def utcnow() -> datetime:
    """统一 naive UTC 时钟（与 SQLite 的 datetime 存储兼容）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    # 多租户：同一客户名可存在于不同租户（不同业务线/不同客户公司）
    tenant_id = Column(String(32), default=DEFAULT_TENANT, nullable=False, index=True)
    name = Column(String(64), nullable=False)
    tier = Column(String(16), default="普通", nullable=False)   # 普通 | VIP
    created_at = Column(DateTime, default=utcnow, nullable=False)


class Ticket(Base):
    __tablename__ = "tickets"
    id = Column(Integer, primary_key=True)
    # 租户归属：所有工单/审计/管理面查询的第一道过滤条件（越权 = 404，不泄露存在性）
    tenant_id = Column(String(32), default=DEFAULT_TENANT, nullable=False, index=True)
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


def _prepare_schema(engine) -> None:
    """建表 + 轻量迁移（补 tenant_id 列 + 放宽 customers 唯一约束）。

    两条入口共用：生产/演示走 init_db()，测试/换库走 configure() ——
    迁移绝不能只在其中一条路径上发生（否则测试是绿的、真库起不来）。
    """
    Base.metadata.create_all(engine)
    _ensure_tenant_columns(engine)
    _migrate_customers_unique(engine)


def configure(db_path: Path | str) -> None:
    """测试隔离入口：换库重建（连同建表 + 迁移 + 种子客户一并完成)。"""
    global _engine, _session_factory
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(
        f"sqlite:///{p}", connect_args={"check_same_thread": False}
    )
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    _prepare_schema(_engine)
    seed_customers()


def seed_customers(tenant_id: str = DEFAULT_TENANT) -> None:
    """演示/评测用固定客户（幂等：已存在则跳过）。"""
    with session_scope() as s:
        exists = s.query(Customer).filter(Customer.tenant_id == tenant_id).count() > 0
        if not exists:
            s.add_all([
                Customer(tenant_id=tenant_id, name="张三", tier="普通"),
                Customer(tenant_id=tenant_id, name="李总", tier="VIP"),
            ])


def _ensure_tenant_columns(engine) -> None:
    """幂等补列：老库（升级前建的）没有 tenant_id，create_all 不会补 → 这里 ALTER。

    这就是"迁移"最小可用形态：先建列 + 默认值回填，再让业务代码按租户过滤。
    """
    with engine.begin() as conn:
        for table in ("customers", "tickets"):
            cols = [row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))]
            if "tenant_id" not in cols:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN tenant_id VARCHAR(32) NOT NULL DEFAULT '{DEFAULT_TENANT}'"
                ))
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_tenant_id ON {table} (tenant_id)"))


def _migrate_customers_unique(engine) -> None:
    """把老库 customers.name 的 UNIQUE 放宽为 UNIQUE(tenant_id, name)（多租户必需）。

    为什么必须重建表：SQLite 不能 DROP 约束，官方做法是"建新表 → 拷数据 → 换名"。
    迁移前若不处理，升级后第一次给新租户建同名客户就会 IntegrityError
    （实测：`UNIQUE constraint failed: customers.name`，2026-09-29）。
    只在检测到旧约束时执行；新库本来建的就是 (tenant_id, name) 唯一。
    """
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cols_with_name_unique = False
        for row in cur.execute("PRAGMA index_list(customers)").fetchall():
            idx_name, is_unique = row[1], row[2]      # (seq, name, unique, origin, partial)
            if not is_unique:
                continue
            cols = [r[2] for r in cur.execute(f"PRAGMA index_info({idx_name})").fetchall()]
            if cols == ["name"]:
                cols_with_name_unique = True
        if not cols_with_name_unique:
            return
        cur.execute("PRAGMA foreign_keys=OFF")   # 换表期间不校验外键（SQLite 官方迁移步骤）
        cur.execute(
            """
            CREATE TABLE customers__new (
                id INTEGER NOT NULL PRIMARY KEY,
                tenant_id VARCHAR(32) NOT NULL DEFAULT 'default',
                name VARCHAR(64) NOT NULL,
                tier VARCHAR(16),
                created_at DATETIME,
                UNIQUE (tenant_id, name)
            )
            """
        )
        cur.execute(
            "INSERT INTO customers__new (id, tenant_id, name, tier, created_at) "
            "SELECT id, tenant_id, name, tier, created_at FROM customers"
        )
        cur.execute("DROP TABLE customers")
        cur.execute("ALTER TABLE customers__new RENAME TO customers")
        cur.execute("CREATE INDEX IF NOT EXISTS ix_customers_tenant_id ON customers (tenant_id)")
        raw.commit()
    finally:
        raw.close()


def init_db() -> None:
    """生产/演示入口：建表 + 轻量迁移（补列 + 放宽唯一约束）+ 种子（沿用既有库，不重建）。"""
    engine = get_engine()
    _prepare_schema(engine)
    seed_customers()