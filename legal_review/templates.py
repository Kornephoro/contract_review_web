from __future__ import annotations

from copy import deepcopy
from typing import Iterable, Optional

CONTRACT_TYPE_OPTIONS = [
    ("general", "通用合同"),
    ("sale", "买卖合同"),
    ("labor", "劳动合同"),
    ("lease", "租赁合同"),
    ("service", "服务/委托合同"),
    ("loan", "借款合同"),
    ("nda", "保密协议"),
    ("construction", "建设工程合同"),
]

CONTRACT_TYPE_LABELS = dict(CONTRACT_TYPE_OPTIONS)

BUILTIN_REVIEW_TEMPLATES = [
    {
        "id": "builtin-contract-general",
        "name": "通用合同审校",
        "prompt": (
            "- 重点检查付款、交付、验收、违约、解除和争议解决条款是否完整一致。\n"
            "- 重点识别通知送达、证据留存、补救路径和执行口径是否可落地。\n"
            "- 重点识别是否存在空白授权、单方解释权、免责过宽或责任失衡条款。"
        ),
        "is_builtin": True,
        "bound_contract_type": "general",
    },
    {
        "id": "builtin-sale",
        "name": "买卖合同审校",
        "prompt": (
            "- 标的物质量标准、验收规则与异议期限是否明确。\n"
            "- 价款构成、付款节点、税费承担与发票义务是否清晰。\n"
            "- 所有权与风险转移时点是否衔接一致。\n"
            "- 瑕疵、短缺、延迟交付的责任与索赔流程是否完整。\n"
            "- 违约责任与争议解决条款是否具备可执行性。"
        ),
        "is_builtin": True,
        "bound_contract_type": "sale",
    },
    {
        "id": "builtin-labor",
        "name": "劳动合同审校",
        "prompt": (
            "- 试用期、工资、社保、公积金是否符合法律强制要求。\n"
            "- 竞业限制范围、期限与补偿标准是否合理。\n"
            "- 加班、休假、调岗、解除条件是否约定清楚。\n"
            "- 是否存在免除用人单位法定义务或排除劳动者权利的无效条款。\n"
            "- 经济补偿与违约责任条款是否符合劳动法规则。"
        ),
        "is_builtin": True,
        "bound_contract_type": "labor",
    },
    {
        "id": "builtin-lease",
        "name": "租赁合同审校",
        "prompt": (
            "- 租赁物交付状态、附属设施清单与使用用途限制是否明确。\n"
            "- 租金、押金、递增机制与返还条件是否清晰。\n"
            "- 维修责任、大修责任与损耗边界是否划分明确。\n"
            "- 转租、装修、优先续租等核心安排是否完整。\n"
            "- 违约解除、腾退交还与争议解决条款是否可执行。"
        ),
        "is_builtin": True,
        "bound_contract_type": "lease",
    },
    {
        "id": "builtin-service",
        "name": "服务/委托合同审校",
        "prompt": (
            "- 服务范围、交付成果与验收标准是否可衡量。\n"
            "- 服务费、结算节点、开票义务和违约责任是否清楚。\n"
            "- 知识产权、保密、分包转委托条款是否合理。\n"
            "- 服务期限、终止机制与过渡安排是否完整。\n"
            "- 风险分配是否明显偏向单方。"
        ),
        "is_builtin": True,
        "bound_contract_type": "service",
    },
    {
        "id": "builtin-loan",
        "name": "借款合同审校",
        "prompt": (
            "- 利率、罚息、复利约定是否合法。\n"
            "- 借款用途、提款条件、还款安排是否清楚。\n"
            "- 抵押、质押、保证等担保条款是否有效可执行。\n"
            "- 提前到期、提前还款、违约处置机制是否合理。\n"
            "- 是否存在明显过高的违约成本或变相高利。"
        ),
        "is_builtin": True,
        "bound_contract_type": "loan",
    },
    {
        "id": "builtin-nda",
        "name": "保密协议审校",
        "prompt": (
            "- 保密信息范围是否过宽或缺少合理例外。\n"
            "- 保密期限、披露限制与返还销毁义务是否明确。\n"
            "- 违约责任、禁令救济和损失举证安排是否合理。\n"
            "- 员工、关联方、第三方接触信息的约束机制是否完整。"
        ),
        "is_builtin": True,
        "bound_contract_type": "nda",
    },
    {
        "id": "builtin-construction",
        "name": "建设工程合同审校",
        "prompt": (
            "- 承包资质、施工许可与工程范围是否明确。\n"
            "- 工期、顺延、变更与索赔流程是否可执行。\n"
            "- 进度款、结算、质保金与优先受偿权安排是否合理。\n"
            "- 质量责任、安全责任与竣工验收机制是否完整。"
        ),
        "is_builtin": True,
        "bound_contract_type": "construction",
    },
]

BUILTIN_TEMPLATE_IDS = {template["id"] for template in BUILTIN_REVIEW_TEMPLATES}


def _normalize_review_template(raw_template: object) -> Optional[dict]:
    if not isinstance(raw_template, dict):
        return None

    template_id = str(raw_template.get("id") or "").strip()
    name = str(raw_template.get("name") or "").strip()
    prompt = str(raw_template.get("prompt") or "").strip()

    bound_contract_type = (
        raw_template.get("bound_contract_type")
        or raw_template.get("boundContractType")
        or raw_template.get("boundDocumentSubtype")
        or ""
    )
    bound_contract_type = str(bound_contract_type).strip() or None

    if not template_id or not name:
        return None

    return {
        "id": template_id,
        "name": name,
        "prompt": prompt,
        "is_builtin": bool(raw_template.get("is_builtin") or raw_template.get("isBuiltin")),
        "bound_contract_type": bound_contract_type,
    }


def get_default_review_templates(saved_templates: Optional[Iterable[object]] = None) -> list[dict]:
    if saved_templates is None:
        return deepcopy(BUILTIN_REVIEW_TEMPLATES)

    builtin_overrides: dict[str, dict] = {}
    custom_templates: list[dict] = []

    for raw_template in saved_templates:
        normalized = _normalize_review_template(raw_template)
        if not normalized:
            continue
        if normalized["id"] in BUILTIN_TEMPLATE_IDS:
            normalized["is_builtin"] = True
            builtin_overrides[normalized["id"]] = normalized
            continue
        if not normalized["prompt"]:
            continue
        normalized["is_builtin"] = False
        custom_templates.append(normalized)

    merged_templates: list[dict] = []
    for builtin in BUILTIN_REVIEW_TEMPLATES:
        current = deepcopy(builtin)
        override = builtin_overrides.get(builtin["id"])
        if override:
            current["name"] = override["name"] or current["name"]
            current["prompt"] = override["prompt"]
            current["bound_contract_type"] = override.get("bound_contract_type") or current.get(
                "bound_contract_type"
            )
        merged_templates.append(current)

    seen_ids = {template["id"] for template in merged_templates}
    for custom_template in custom_templates:
        if custom_template["id"] in seen_ids:
            continue
        seen_ids.add(custom_template["id"])
        merged_templates.append(custom_template)

    return merged_templates


def get_builtin_template(template_id: str) -> Optional[dict]:
    for template in BUILTIN_REVIEW_TEMPLATES:
        if template["id"] == template_id:
            return deepcopy(template)
    return None


def get_review_template_by_id(templates: Iterable[dict], template_id: str) -> Optional[dict]:
    for template in templates:
        if template.get("id") == template_id:
            return deepcopy(template)
    return None


def format_template_option_label(template: dict) -> str:
    scope_label = CONTRACT_TYPE_LABELS.get(template.get("bound_contract_type") or "", "")
    source_label = "内置" if template.get("is_builtin") else "自定义"
    if scope_label:
        return f"{template.get('name', '未命名模板')} · {scope_label} · {source_label}"
    return f"{template.get('name', '未命名模板')} · {source_label}"
