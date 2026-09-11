"""Pydantic 请求模型：所有写接口的入参校验。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

AllergenStatus = Literal["present", "absent", "may_contain", "unknown"]


class SubComponent(BaseModel):
    """复合原料的次级配料；ingredient_ref 可以是原料 ID、名称或别名。"""

    ingredient_ref: str = Field(min_length=1, description="原料 ID、名称或别名")
    version: Optional[str] = Field(default=None, description="规格版本；缺省取该原料最新版本")
    percentage: Optional[float] = Field(default=None, ge=0, le=100)


class SupplierDeclaration(BaseModel):
    """供应商对某原料某过敏原的声明。"""

    allergen: str = Field(min_length=1)
    status: AllergenStatus
    source: Optional[str] = Field(default=None, description="供应商声明文件编号，便于追溯")


class IngredientCreate(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    aliases: list[str] = []
    is_compound: bool = False


class IngredientVersionCreate(BaseModel):
    version: str = Field(min_length=1)
    sub_components: list[SubComponent] = []
    supplier_declarations: list[SupplierDeclaration] = []


class ProductCreate(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class RecipeItem(BaseModel):
    ingredient_ref: str = Field(min_length=1, description="原料 ID、名称或别名")
    version: Optional[str] = Field(default=None, description="规格版本；缺省取最新")
    percentage: float = Field(ge=0, le=100)


class RecipeCreate(BaseModel):
    version: str = Field(min_length=1)
    items: list[RecipeItem] = Field(min_length=1)


class LineCreate(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    allergens_handled: list[str] = Field(default_factory=list, description="该产线处理过的过敏原")


class LabelCopy(BaseModel):
    """标签文案：已声明过敏原、交叉接触提示、“无某过敏原”宣称与配料表文本。"""

    declared_allergens: list[str] = []
    may_contain: list[str] = []
    free_from_claims: list[str] = []
    ingredients_text: str = ""


class LabelCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    product_id: str
    label_copy: LabelCopy = Field(alias="copy")


class LabelCopyUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    label_copy: LabelCopy = Field(alias="copy")


class ApproveRequest(BaseModel):
    approved_by: str = Field(min_length=1)


class WithdrawRequest(BaseModel):
    reason: str = Field(min_length=1)


class OverrideRequest(BaseModel):
    """审核人覆盖自动结论：必须给出理由并关联输入证据。

    evidence_refs 元素格式（须能在库中解析，否则 422）：
      spec:{ingredient_id}:{version}            原料规格版本
      declaration:{ingredient_id}:{version}:{allergen}  某条供应商声明
      line:{line_id}                            产线
      recipe:{product_id}:{version}             配方版本
    """

    reviewer: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
