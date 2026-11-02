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
    added_by: str = "csv"
    checked_in_at: Optional[str] = None
    event_id: Optional[int] = None


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
    expected_capacity: int = 0
    named_count: int = 0
    members: List[MemberOut] = []
    event_id: Optional[int] = None


class CheckInResult(BaseModel):
    ok: bool
    message: str
    guest: Optional[GuestOut] = None


class Stats(BaseModel):
    registrations: int
    total: int
    expected_capacity: int
    paid: int
    unpaid: int
    under_review: int
    checked_in: int
    paid_remaining: int
    pending_total: int
    incomplete_groups: int
    pct: float


class ImportPreview(BaseModel):
    token: str
    file_name: str
    registration_rows: int
    blank_rows_skipped: int
    individual_guests: int
    expected_capacity: int
    incomplete_groups: int
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


class TicketTypeOut(BaseModel):
    id: int
    event_id: int
    name: str
    price_inr: int = 0
    group_size: int = 1
    capacity: int = 0
    sold: int = 0
    sale_open: bool = True


class EventOut(BaseModel):
    id: int
    slug: str
    title: str
    description: str = ""
    poster_url: str = ""
    venue: str = ""
    city: str = ""
    starts_at: str = ""
    ends_at: str = ""
    status: str = "draft"
    created_at: str = ""
    ticket_types: List[TicketTypeOut] = []


class EventCreateIn(BaseModel):
    title: str
    slug: str = ""
    description: str = ""
    poster_url: str = ""
    venue: str = ""
    city: str = ""
    starts_at: str = ""
    ends_at: str = ""
    status: str = "draft"


class EventEditIn(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    poster_url: Optional[str] = None
    venue: Optional[str] = None
    city: Optional[str] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None


class TicketTypeCreateIn(BaseModel):
    name: str
    price_inr: int = 0
    group_size: int = 1
    capacity: int = 0
    sale_open: bool = True


class TicketTypeEditIn(BaseModel):
    name: Optional[str] = None
    price_inr: Optional[int] = None
    group_size: Optional[int] = None
    capacity: Optional[int] = None
    sale_open: Optional[bool] = None
