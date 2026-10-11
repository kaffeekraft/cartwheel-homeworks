"""HW1 additional tool: get_store_info (offline, no LLM)."""

from __future__ import annotations

from agent import tools
from agent.agent import TOOLS_BY_ROLE
from agent.auth import AuthContext

SHOPPER_1 = AuthContext(user_id=1, role="shopper")
MERCHANT_STORE_2 = AuthContext(user_id=9002, role="merchant", store_id=2)
SUPPORT = AuthContext(user_id=9501, role="support")


def test_merchant_without_arguments_gets_own_store_override(world: dict) -> None:
    result = tools.get_store_info(MERCHANT_STORE_2)
    assert result["ok"] is True
    assert result["store_id"] == 2
    assert result["name"] == "Juniper Home Goods"
    assert result["return_window_days"] == 14
    assert result["return_window_source"] == "store_override"
    assert result["policy_id"] == "store-juniper-home-goods-policy"


def test_store_without_override_or_policy_page_uses_platform_default(world: dict) -> None:
    result = tools.get_store_info(SHOPPER_1, store_id=1)
    assert result["ok"] is True
    assert result["name"] == "Blue Heron Ceramics"
    assert result["return_window_days"] == 30
    assert result["return_window_source"] == "platform_default"
    assert result["policy_id"] is None


def test_lookup_by_name_is_case_insensitive_for_any_role(world: dict) -> None:
    result = tools.get_store_info(SUPPORT, store="meridian cycles")
    assert result["ok"] is True
    assert result["return_window_days"] == 21
    assert result["policy_id"] == "store-meridian-cycles-policy"


def test_argument_errors(world: dict) -> None:
    assert tools.get_store_info(SHOPPER_1)["error"] == "invalid_argument"
    assert tools.get_store_info(SUPPORT)["error"] == "invalid_argument"
    both = tools.get_store_info(MERCHANT_STORE_2, store_id=2, store="Juniper Home Goods")
    assert both["error"] == "invalid_argument"
    assert tools.get_store_info(SHOPPER_1, store_id=99999)["error"] == "not_found"
    assert tools.get_store_info(SHOPPER_1, store="No Such Store")["error"] == "not_found"


def test_registered_for_every_role() -> None:
    for role, role_tools in TOOLS_BY_ROLE.items():
        assert "get_store_info" in {t.name for t in role_tools}, role
