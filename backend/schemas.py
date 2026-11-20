"""Pydantic request models for the local Finance Workbench API."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _validate_money_precision(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("金额必须是有效数字")
    if value.as_tuple().exponent < -2:
        raise ValueError("金额最多保留两位小数")
    return value


class VoucherEntryIn(StrictModel):
    account_code: str = Field(min_length=1, max_length=32)
    contact_id: int | None = None
    summary: str = Field(default="", max_length=500)
    aux_name: str | None = Field(default=None, max_length=200)
    debit_amount: Decimal = Decimal("0.00")
    credit_amount: Decimal = Decimal("0.00")

    @field_validator("debit_amount", "credit_amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        return _validate_money_precision(value)


class VoucherCreate(StrictModel):
    legacy_id: str | None = Field(default=None, max_length=160)
    voucher_no: str | None = Field(default=None, max_length=64)
    voucher_date: date
    summary: str = Field(default="", max_length=500)
    biz_label: str = Field(default="", max_length=120)
    source: str | None = Field(default=None, max_length=160)
    source_type: str | None = Field(default=None, max_length=80)
    counterparty: str | None = Field(default=None, max_length=200)
    memo: str | None = Field(default=None, max_length=1000)
    attachment_name: str | None = Field(default=None, max_length=240)
    status: Literal["DRAFT", "PENDING"] = "DRAFT"
    attachment_urls: list[str] = Field(default_factory=list, max_length=30)
    entries: list[VoucherEntryIn] = Field(min_length=2, max_length=500)


class VoucherUpdate(StrictModel):
    voucher_no: str | None = Field(default=None, min_length=1, max_length=64)
    voucher_date: date | None = None
    summary: str | None = Field(default=None, max_length=500)
    biz_label: str | None = Field(default=None, max_length=120)
    source: str | None = Field(default=None, max_length=160)
    source_type: str | None = Field(default=None, max_length=80)
    counterparty: str | None = Field(default=None, max_length=200)
    memo: str | None = Field(default=None, max_length=1000)
    attachment_name: str | None = Field(default=None, max_length=240)
    attachment_urls: list[str] | None = Field(default=None, max_length=30)
    entries: list[VoucherEntryIn] | None = Field(default=None, min_length=2, max_length=500)


class VoucherIdsRequest(StrictModel):
    ids: list[int] = Field(min_length=1, max_length=1000)


class ReverseVoucherRequest(StrictModel):
    voucher_date: date | None = None
    voucher_no: str | None = Field(default=None, max_length=64)
    summary: str | None = Field(default=None, max_length=500)


class CashierCreate(StrictModel):
    legacy_id: str | None = Field(default=None, max_length=160)
    transfer_id: str | None = Field(default=None, max_length=160)
    direction: Literal["OUT", "IN"] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    status: Literal["PENDING", "DONE"] = "PENDING"
    record_date: date
    biz_type: Literal["RECEIPT", "PAYMENT", "TRANSFER"]
    amount: Decimal
    account_name: str = Field(min_length=1, max_length=120)
    contact_name: str | None = Field(default=None, max_length=120)
    source_doc: str | None = Field(default=None, max_length=120)
    receipt_img_url: str | None = Field(default=None, max_length=500)

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        value = _validate_money_precision(value)
        if value <= 0:
            raise ValueError("收付款金额必须大于零")
        return value


class CashierBatchCreate(StrictModel):
    records: list[CashierCreate] = Field(min_length=1, max_length=500)


class CashierUpdate(StrictModel):
    transfer_id: str | None = Field(default=None, max_length=160)
    direction: Literal["OUT", "IN"] | None = None
    extra: dict[str, Any] | None = None
    record_date: date | None = None
    biz_type: Literal["RECEIPT", "PAYMENT", "TRANSFER"] | None = None
    amount: Decimal | None = None
    account_name: str | None = Field(default=None, min_length=1, max_length=120)
    contact_name: str | None = Field(default=None, max_length=120)
    source_doc: str | None = Field(default=None, max_length=120)
    receipt_img_url: str | None = Field(default=None, max_length=500)
    status: Literal["PENDING", "DONE"] | None = None

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return value
        value = _validate_money_precision(value)
        if value <= 0:
            raise ValueError("收付款金额必须大于零")
        return value


class CashierBatchUpdateItem(StrictModel):
    id: int = Field(ge=1)
    changes: CashierUpdate


class CashierBatchUpdate(StrictModel):
    items: list[CashierBatchUpdateItem] = Field(min_length=1, max_length=500)


class CashierIdsRequest(StrictModel):
    ids: list[int] = Field(min_length=1, max_length=500)


class CashierToVoucherRequest(StrictModel):
    counterpart_account_code: str | None = Field(default=None, max_length=32)
    cash_account_code: str | None = Field(default=None, max_length=32)
    transfer_to_account_code: str | None = Field(default=None, max_length=32)
    voucher_date: date | None = None
    voucher_no: str | None = Field(default=None, max_length=64)
    summary: str | None = Field(default=None, max_length=500)


class InvoiceCreate(StrictModel):
    legacy_id: str | None = Field(default=None, max_length=160)
    source: str | None = Field(default=None, max_length=200)
    extra: dict[str, Any] = Field(default_factory=dict)
    invoice_code: str | None = Field(default=None, max_length=64)
    invoice_no: str = Field(min_length=1, max_length=64)
    invoice_date: date
    buyer_name: str = Field(min_length=1, max_length=200)
    buyer_tax_id: str | None = Field(default=None, max_length=64)
    seller_name: str | None = Field(default=None, max_length=200)
    amount: Decimal = Decimal("0.00")
    tax_amount: Decimal = Decimal("0.00")
    payment_method: str | None = Field(default=None, max_length=64)
    status: str = Field(default="DRAFT", max_length=32)
    pdf_or_img_url: str | None = Field(default=None, max_length=500)
    items: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("amount", "tax_amount")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        value = _validate_money_precision(value)
        if value < 0:
            raise ValueError("发票金额不能为负数")
        return value


class InvoiceUpdate(StrictModel):
    invoice_code: str | None = Field(default=None, max_length=64)
    invoice_no: str | None = Field(default=None, min_length=1, max_length=64)
    invoice_date: date | None = None
    buyer_name: str | None = Field(default=None, min_length=1, max_length=200)
    buyer_tax_id: str | None = Field(default=None, max_length=64)
    seller_name: str | None = Field(default=None, max_length=200)
    amount: Decimal | None = None
    tax_amount: Decimal | None = None
    payment_method: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=32)
    pdf_or_img_url: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=200)
    items: list[dict[str, Any]] | None = None
    extra: dict[str, Any] | None = None

    @field_validator("amount", "tax_amount")
    @classmethod
    def validate_optional_amount(cls, value: Decimal | None) -> Decimal | None:
        if value is None: return value
        value = _validate_money_precision(value)
        if value < 0: raise ValueError("发票金额不能为负数")
        return value


class AccountCreate(StrictModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    category: Literal["资产", "负债", "权益", "成本", "损益"]
    direction: Literal["借", "贷"]
    parent_code: str | None = None
    initial_debit: Decimal = Decimal("0.00")
    initial_credit: Decimal = Decimal("0.00")
    is_active: bool = True

    @field_validator("initial_debit", "initial_credit")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        value = _validate_money_precision(value)
        if value < 0:
            raise ValueError("期初余额不能为负数")
        return value


class AccountOpeningBalanceItem(StrictModel):
    code: str = Field(min_length=1, max_length=32)
    initial_debit: Decimal = Decimal("0.00")
    initial_credit: Decimal = Decimal("0.00")

    @field_validator("initial_debit", "initial_credit")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        value = _validate_money_precision(value)
        if value < 0: raise ValueError("期初余额不能为负数")
        return value


class AccountOpeningBalanceBatch(StrictModel):
    items: list[AccountOpeningBalanceItem] = Field(min_length=1, max_length=1000)


class ContactCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    type: Literal["CUSTOMER", "SUPPLIER"]
    tax_id: str | None = Field(default=None, max_length=64)
    contact_info: str | None = Field(default=None, max_length=500)


class AccountUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    category: Literal["资产", "负债", "权益", "成本", "损益"] | None = None
    direction: Literal["借", "贷"] | None = None
    parent_code: str | None = None
    initial_debit: Decimal | None = None
    initial_credit: Decimal | None = None
    is_active: bool | None = None

    @field_validator("initial_debit", "initial_credit")
    @classmethod
    def validate_optional_balance(cls, value: Decimal | None) -> Decimal | None:
        if value is None: return value
        value = _validate_money_precision(value)
        if value < 0: raise ValueError("期初余额不能为负数")
        return value


class ContactUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    type: Literal["CUSTOMER", "SUPPLIER"] | None = None
    tax_id: str | None = Field(default=None, max_length=64)
    contact_info: str | None = Field(default=None, max_length=500)


class PeriodLockUpdate(StrictModel):
    is_locked: bool
    lock_reason: str | None = Field(default=None, max_length=500)


class ContactOpeningBalanceUpdate(StrictModel):
    account_code: str
    contact_id: int
    as_of_date: date
    initial_debit: Decimal = Decimal("0.00")
    initial_credit: Decimal = Decimal("0.00")

    @field_validator("initial_debit", "initial_credit")
    @classmethod
    def validate_amount(cls, value: Decimal) -> Decimal:
        value = _validate_money_precision(value)
        if value < 0:
            raise ValueError("期初余额不能为负数")
        return value
