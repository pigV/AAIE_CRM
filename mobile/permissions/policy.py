"""Derive Mobile UI actions from the existing CRM permission boundary."""

from dataclasses import dataclass
from typing import Callable


CanAccess = Callable[[dict, str], bool]


@dataclass(frozen=True)
class MobileAction:
    key: str
    label: str
    href: str
    state: str = "available"
    badge: str = ""
    description: str = ""


def _state(user: dict, module_key: str, can_access: CanAccess) -> str:
    return "available" if can_access(user, module_key) else "forbidden"


def quick_actions(user: dict, can_access: CanAccess) -> tuple[MobileAction, ...]:
    actions = [
        MobileAction(
            "parent_form",
            "新建表单",
            "/parent-links",
            _state(user, "parent_form", can_access),
            description="创建家长表单链接",
        ),
        MobileAction(
            "contract",
            "新建合同",
            "/contracts?view=new",
            _state(user, "contract_management_center", can_access),
            description="进入现有合同流程",
        ),
        MobileAction(
            "school_plan",
            "制作方案",
            "/school-plan-reports/create",
            _state(user, "school_plan_reports", can_access),
            description="制作学校方案",
        ),
    ]
    if can_access(user, "revenue_commission"):
        actions.append(
            MobileAction(
                "revenue",
                "收入与佣金",
                "/mobile/revenue",
                "available",
                description="收入与佣金管理",
            )
        )
    return tuple(actions)


def bottom_actions(user: dict, can_access: CanAccess) -> tuple[MobileAction, ...]:
    return (
        MobileAction("home", "首页", "/", _state(user, "dashboard", can_access)),
        MobileAction("students", "学生", "/students", _state(user, "students", can_access)),
        MobileAction("plus", "新增", "#mobile-plus", "available"),
        MobileAction("schools", "学校", "/schools", _state(user, "school_library", can_access)),
        MobileAction("visa", "签证", "/visa-management", _state(user, "visa", can_access)),
    )


def account_actions(mode_switch_href: str) -> tuple[MobileAction, ...]:
    """Describe existing account routes for the Mobile auxiliary action area."""
    return (
        MobileAction("account", "账户设置", "/account"),
        MobileAction("password", "修改密码", "/account/password"),
        MobileAction("desktop", "切换桌面版", mode_switch_href),
    )


def plus_actions(user: dict, can_access: CanAccess) -> tuple[MobileAction, ...]:
    username = str((user or {}).get("username") or "").strip().lower()
    if username in {"admin", "sonnyz"}:
        if not can_access(user, "content_center"):
            return ()
        return (
            MobileAction("content_create", "上传文件", "/content?tab=create", description="视频、图片或文档"),
            MobileAction("content_extract", "视频链接", "/content?tab=extract", description="进入现有内容提取功能"),
        )
    if username != "tongy":
        return ()
    definitions = (
        ("followup", "新增跟进记录", "/students?mobile_action=followup", "students"),
        ("parent_form", "新建家长表单", "/parent-links", "parent_form"),
        ("contract", "新建家长合同", "/contracts?view=new", "contract_management_center"),
        ("school_plan", "制作学校方案", "/school-plan-reports/create", "school_plan_reports"),
        ("student_search", "搜索学生", "/students", "students"),
    )
    return tuple(
        MobileAction(key, label, href, _state(user, module_key, can_access))
        for key, label, href, module_key in definitions
    )
