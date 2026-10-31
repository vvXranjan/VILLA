from typing import List, Optional

from pydantic import BaseModel


class GuestOut(BaseModel):
    id: int
    reg_id: str
    name: str
    phone: str
    email: str
    payment_status: str
    entry_pass: str = ""
    needs_review: bool = False
    review_reason: str = ""
    checked_in_at: Optional[str] = None


class MemberOut(BaseModel):
    id: int
    name: str
    checked_in_at: Optional[str] = None


class RegistrationOut(BaseModel):
    reg_id: str
    entry_pass: str
    phone: str
    email: str
    payment_status: str
    needs_review: bool = False
    review_reason: str = ""
    members: List[MemberOut] = []


class CheckInResult(BaseModel):
    ok: bool
    message: str
    guest: Optional[GuestOut] = None


class Stats(BaseModel):
    registrations: int
    total: int
    paid: int
    unpaid: int
    under_review: int
    checked_in: int
    paid_remaining: int
    pending_total: int
    pct: float


class ImportPreview(BaseModel):
    token: str
    file_name: str
    registration_rows: int
    blank_rows_skipped: int
    individual_guests: int
    solo: int
    couple: int
    duo: int
    trio: int
    other_pass: int
    paid_regs: int
    unpaid_regs: int
    review_regs: int
    manual_review_regs: int
    manual_review_cases: List[dict]
    duplicate_file_warning: Optional[str] = None
    aadhaar_columns_ignored: List[str] = []
    sample_guests: List[dict] = []


class ImportResult(BaseModel):
    ok: bool
    message: str
    registrations: int = 0
    guests: int = 0
    updated: int = 0
    backup_file: str = ""


class ColumnMapping(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    reg_id: Optional[str] = None
    payment_status: Optional[str] = None
