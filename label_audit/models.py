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
    allergens_handled: list[str] = Field(default_factory=list, description="该产线处理过的过敏原（历史登记）")


class EquipmentSegment(BaseModel):
    """生产批次实际使用的设备段；追溯时逐段核对清洁程序与验证。"""

    segment_id: str = Field(min_length=1)
    name: Optional[str] = None


class BatchCreate(BaseModel):
    """生产批次：共线风险逐批次判定的核心对象。

    sequence 为本批次在该产线上的生产序号（越大越晚）；
    过敏原/返工料去向以显式登记为准，不靠产线历史静态推导。
    """

    batch_id: str = Field(min_length=1)
    product_id: str
    line_id: str = Field(min_length=1)
    sequence: int = Field(ge=1, description="该产线上的生产顺序号（越大越晚）")
    started_at: Optional[str] = Field(default=None, description="开工时间 ISO8601（可选）")
    allergens: list[str] = Field(default_factory=list, description="本批次产品含有的过敏原")
    equipment_segments: list[EquipmentSegment] = Field(default_factory=list)


class CleaningProgramCreate(BaseModel):
    """清洁程序版本：覆盖哪些过敏原、必检采样点、有效期与定量限值。"""

    program_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    line_id: Optional[str] = Field(default=None, description="适用产线；缺省表示全产线通用")
    allergens: list[str] = Field(min_length=1, description="该程序能够清除/验证的过敏原")
    required_points: list[str] = Field(min_length=1, description="必检采样点 ID")
    valid_from: Optional[str] = Field(default=None, description="生效日期 YYYY-MM-DD")
    valid_until: Optional[str] = Field(default=None, description="失效日期 YYYY-MM-DD；缺省表示长期有效")
    limit_ppm: float = Field(default=2.0, gt=0, description="拭子定量限值（ppm，含等于为合格）")


class CleaningRecordCreate(BaseModel):
    """某次清洁的执行记录：批次之间、设备段上执行了哪个程序版本。

    缺少清洁记录时对应过敏原路径无法关闭（清洁步骤缺口）。
    """

    record_id: str = Field(min_length=1)
    line_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1, description="本次清洁之后投产的批次（即被该清洁保护的批次）")
    segment_id: str = Field(min_length=1)
    program_id: str = Field(min_length=1)
    program_version: str = Field(min_length=1)
    cleaned_at: Optional[str] = Field(default=None, description="清洁日期 YYYY-MM-DD")


class SwabResultCreate(BaseModel):
    """拭子定量结果（ppm）：事后可补录，补录阳性会沿影响链传播。"""

    swab_id: str = Field(min_length=1)
    record_id: str = Field(min_length=1, description="所属清洁记录")
    point_id: str = Field(min_length=1, description="采样点")
    allergen: str = Field(min_length=1)
    value_ppm: Optional[float] = Field(default=None, ge=0,
                                       description="定量结果；缺省表示已采样但结果未出")
    sampled_at: Optional[str] = Field(default=None)


class SwabBackfill(BaseModel):
    """实验室定量结果补录（可能在标签批准之后到达）。"""

    value_ppm: float = Field(ge=0)
    sampled_at: Optional[str] = Field(default=None)


class ReworkTarget(BaseModel):
    target_batch_id: str = Field(min_length=1)
    percentage: Optional[float] = Field(default=None, ge=0, le=100)


class ReworkPathCreate(BaseModel):
    """返工料去向：含过敏原批次的余料投入另一批次（可能跨产品）。

    target 批次投产前，源批次的每个过敏原都必须能沿目标批次的
    设备段清洁/拭子证据链关闭，否则返工路径保持开放。
    """

    source_batch_id: str = Field(min_length=1)
    target_batch_id: str = Field(min_length=1)
    percentage: Optional[float] = Field(default=None, ge=0, le=100)


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
    batch_id: Optional[str] = Field(default=None,
                                    description="审核所针对的生产批次；缺省取产品最新批次")


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
