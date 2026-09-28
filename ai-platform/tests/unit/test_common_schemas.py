"""单元测试：通用请求 / 响应契约模型。"""

from __future__ import annotations

from app.schemas.common import Page, StrictModel


class _Demo(StrictModel):
    query: str = ""
    use_rag: bool = True


def test_unknown_fields_are_ignored_not_rejected() -> None:
    """``AC-API-05``：传 camelCase（如 ``useRag``）应被忽略并用默认值，而不是 400。"""
    model = _Demo.model_validate({"query": "你好", "useRag": False, "extra": 1})

    assert model.query == "你好"
    assert model.use_rag is True


def test_missing_fields_use_defaults() -> None:
    """缺省与 ``null`` 语义等价（数组默认 ``[]`` 而非 ``null``）。"""
    model = _Demo.model_validate({})

    assert model.query == ""
    assert model.use_rag is True


def test_page_empty_shape() -> None:
    """空列表返回 ``items=[]``、``next_cursor=null``、``has_more=false``。"""
    page: Page[str] = Page[str]()

    assert page.model_dump() == {"items": [], "next_cursor": None, "has_more": False}


def test_page_with_items() -> None:
    """分页信封的字段名固定（客户端契约）。"""
    page = Page[str](items=["a"], next_cursor="abc", has_more=True)

    assert page.model_dump() == {"items": ["a"], "next_cursor": "abc", "has_more": True}
