"""API Key 鉴权与租户解析（P0-3 最小鉴权）。

要解决的真实漏洞（升级前）：
    `POST /api/v1/tickets/{id}/human-reply` 的 `actor` 来自**请求体** ——
    任何人发一个 `{"content": "...", "actor": "human"}` 就能冒充坐席给用户回消息，
    而且审计表里会记成"人工回复"。`GET /admin/escalations` 同样无鉴权。

设计原则：
- **身份从凭据来，不从请求体来**：坐席身份由 `X-API-Key` 解析，请求体里的 actor 一律忽略；
- **租户隔离**：key 绑定租户；跨租户访问工单返回 404（不泄露"这个单存在"）；
- **默认不启用**（`REQUIRE_AUTH=false`）：本地演示/评测/测试零密钥可跑，启动时打 WARNING
  明确提示"当前未启用鉴权"—— 不假装安全，也不让 CI 依赖仓库密钥；
- key 只留前 6 位用于日志/审计（不落全量 key，避免日志泄露凭据）。

key 配置格式（逗号分隔）：`key:租户:坐席名[:角色]`，角色默认 agent
    例：API_KEYS=sk-aaa:tenantA:坐席甲,sk-bbb:tenantB:坐席乙:agent,sk-ccc:tenantC:观察者:viewer
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Header, HTTPException

from project03.config import get_settings

logger = logging.getLogger("project03.auth")

ROLE_AGENT = "agent"
ROLE_VIEWER = "viewer"
ROLE_CUSTOMER = "customer"


@dataclass(frozen=True)
class Principal:
    """请求身份：租户 + 主体名 + 角色（+ key 指纹，便于审计但不泄露凭据）。"""

    tenant: str
    actor: str
    role: str
    key_id: str = ""


def parse_api_keys(raw: str) -> dict[str, Principal]:
    """解析 `API_KEYS` 配置；格式非法直接抛错（配置错误不该静默降级为无鉴权）。"""
    out: dict[str, Principal] = {}
    for idx, item in enumerate((raw or "").split(","), 1):
        item = item.strip()
        if not item:
            continue
        parts = [p.strip() for p in item.split(":")]
        if len(parts) < 3 or not all(parts[:3]):
            raise ValueError(f"API_KEYS 第 {idx} 项格式非法（应为 key:租户:坐席名[:角色]）")
        key, tenant, actor = parts[0], parts[1], parts[2]
        role = parts[3] if len(parts) > 3 and parts[3] else ROLE_AGENT
        out[key] = Principal(tenant=tenant, actor=actor, role=role, key_id=key[:6])
    return out


def principal_from_key(api_key: str | None) -> Principal | None:
    """凭据 → 身份；未命中返回 None（由调用方决定是拒绝还是降级为匿名）。"""
    if not api_key:
        return None
    return parse_api_keys(get_settings().api_keys).get(api_key.strip())


def resolve_principal(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Principal:
    """用户侧入口（chat / ack / 工单详情）：**不强制 API Key**。

    终端用户手上不会有坐席凭据；真实部署里用户认证在登录态/网关，不在业务服务里。
    本函数只做一件事：**把请求归到正确的租户** ——
    - 带 key：用 key 的租户（演示页/BFF 可以替用户带上租户 key）；
    - 不带 key：默认租户的匿名访客 —— 因此**看不到任何具名租户的数据**（隔离天然成立）；
    - 未启用鉴权（REQUIRE_AUTH=false）时行为一致，租户恒为默认租户。
    """
    p = principal_from_key(x_api_key)
    if p is not None:
        return p
    return Principal(tenant=get_settings().default_tenant, actor="anonymous", role=ROLE_CUSTOMER)


def _resolve_staff(x_api_key: str | None, *, write: bool) -> Principal:
    """坐席身份解析：读操作 agent/viewer 均可，写操作必须 agent（最小权限）。

    未启用鉴权时（本地演示/评测）返回 local-agent 身份：审计里写"local-agent"，
    而不是采信请求体里自称的 actor —— 至少让"谁做的"这条留痕不撒谎。
    """
    p = principal_from_key(x_api_key)
    if p is not None:
        allowed = (ROLE_AGENT,) if write else (ROLE_AGENT, ROLE_VIEWER)
        if p.role not in allowed:
            need = "agent（写权限）" if write else "agent/viewer"
            raise HTTPException(status_code=403, detail=f"角色 {p.role} 无越权空间，需 {need}")
        return p
    st = get_settings()
    if st.require_auth:
        raise HTTPException(status_code=401, detail="缺少有效的 X-API-Key（坐席端点需鉴权）")
    return Principal(tenant=st.default_tenant, actor="local-agent", role=ROLE_AGENT)


def require_agent(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Principal:
    """坐席**写**端点（人工回复）：仅 agent 角色。"""
    return _resolve_staff(x_api_key, write=True)


def require_staff(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> Principal:
    """坐席**读**端点（管理面队列）：agent / viewer 均可。"""
    return _resolve_staff(x_api_key, write=False)


def warn_if_auth_disabled() -> None:
    """启动时诚实提示：未启用鉴权 = 仅限本地演示/评测。"""
    if not get_settings().require_auth:
        logger.warning(
            "鉴权未启用（REQUIRE_AUTH=false）：坐席端点接受匿名调用，仅限本地演示/评测；"
            "生产部署请配置 API_KEYS 并设 REQUIRE_AUTH=true"
        )