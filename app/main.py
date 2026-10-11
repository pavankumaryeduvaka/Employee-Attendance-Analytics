"""Employee Attendance & Analytics API.

Run from the repository root with:
    uvicorn app.main:app --port 8000

Configuration:
    MONGO_URI and MONGO_DB

The canonical API paths are unprefixed, as required by the assignment. A small
ASGI middleware also strips the Replit artifact mount (/api) so the same app
works both in the workspace preview and in a clean assignment checkout.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import os
import re
import threading
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError
from starlette.types import ASGIApp, Receive, Scope, Send


load_dotenv(override=False)

logger = logging.getLogger(__name__)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE")
ExplainEndpoint = Literal[
    "attendance_list",
    "employee_monthly",
    "department_summary",
    "late_leaderboard",
    "department_trend",
]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[
    int,
    Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000),
]
PAGE_SIZE_MAX = 100
EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SHIFT_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

_mongo_client: MongoClient[dict[str, Any]] | None = None
_database: Database[dict[str, Any]] | None = None
_indexes_ready = False
_index_lock = threading.Lock()


class ApiMountMiddleware:
    """Adapt the /api artifact mount without changing the assignment routes."""

    def __init__(self, application: ASGIApp) -> None:
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/api" or path.startswith("/api/"):
                adjusted_scope = dict(scope)
                adjusted_scope["path"] = path[4:] or "/"
                raw_path = scope.get("raw_path", b"")
                adjusted_scope["raw_path"] = raw_path[4:] or b"/"
                current_root = scope.get("root_path", "").rstrip("/")
                if current_root != "/api" and not current_root.endswith("/api"):
                    adjusted_scope["root_path"] = f"{current_root}/api"
                await self.application(adjusted_scope, receive, send)
                return
        await self.application(scope, receive, send)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class HealthResponse(ApiModel):
    status: Literal["ok"]


class ErrorResponse(ApiModel):
    detail: str


class EmployeeCreate(ApiModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        return _validate_date_string(value, "joined_on")

    @model_validator(mode="after")
    def validate_shift(self) -> EmployeeCreate:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(ApiModel):
    emp_code: str
    punched_at: EpochMillis | None = None
    status: PresenceStatus = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_null_punch_time(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("omit punched_at to use the current time")
        return value

    @field_validator("status", mode="before")
    @classmethod
    def reject_null_status(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("status cannot be null")
        return value


class PunchOutRequest(ApiModel):
    emp_code: str
    punched_at: EpochMillis | None = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def reject_null_punch_time(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("omit punched_at to use the current time")
        return value


class RegularizeRequest(ApiModel):
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status", "punch_in", "punch_out", mode="before")
    @classmethod
    def reject_explicit_nulls(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("omit this field instead of sending null")
        return value


class EmployeeResponse(ApiModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: EpochMillis


class EmployeePage(ApiModel):
    items: list[EmployeeResponse]
    total: int
    page: int
    page_size: int


class AttendanceRecordResponse(ApiModel):
    emp_code: str
    date: str
    status: Status
    punch_in: EpochMillis | None
    punch_out: EpochMillis | None
    work_hours: float | None
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[dict[str, Any]]


class AttendancePage(ApiModel):
    items: list[AttendanceRecordResponse]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyResponse(ApiModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: float | None


class DepartmentSummaryItem(ApiModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: float | None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryResponse(ApiModel):
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(ApiModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardResponse(ApiModel):
    month: str
    items: list[LeaderboardItem]


class TrendItem(ApiModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: float | None
    moving_avg_7d: float | None


class TrendResponse(ApiModel):
    department: str
    items: list[TrendItem]


def _validate_date_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid calendar date") from exc
    return value


def _parse_month(month: str) -> tuple[date, date]:
    if not MONTH_PATTERN.fullmatch(month):
        raise HTTPException(status_code=422, detail="month must use YYYY-MM")
    try:
        first_day = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must use YYYY-MM") from exc
    if first_day.month == 12:
        next_month = date(first_day.year + 1, 1, 1)
    else:
        next_month = date(first_day.year, first_day.month + 1, 1)
    return first_day, next_month - timedelta(days=1)


def _raise_query_validation(name: str, message: str, value: Any = None) -> None:
    raise RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("query", name),
                "msg": message,
                "input": value,
            }
        ]
    )


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _epoch_millis_to_datetime(value: int) -> datetime:
    if type(value) is not int or value < EPOCH_MIN or value > EPOCH_MAX:
        raise ValueError("timestamp must be an integer epoch millisecond value")
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def _datetime_to_epoch_millis(value: datetime | None) -> int | None:
    if value is None:
        return None
    return int(_utc_datetime(value).timestamp()) * 1000


def _now_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _round_half_up(value: Any, decimal_places: int = 2) -> float:
    if isinstance(value, Decimal128):
        decimal_value = value.to_decimal()
    elif isinstance(value, Decimal):
        decimal_value = value
    else:
        decimal_value = Decimal(str(value))
    quantum = Decimal("1").scaleb(-decimal_places)
    return float(decimal_value.quantize(quantum, rounding=ROUND_HALF_UP))


def _decimal_value(value: Any) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _parse_shift(shift: str) -> tuple[int, int]:
    if not SHIFT_PATTERN.fullmatch(shift):
        raise ValueError("shift times must use HH:MM")
    hour_text, minute_text = shift.split(":")
    return int(hour_text), int(minute_text)


def _attendance_date_for_punch(punched_at: datetime, employee: dict[str, Any]) -> date:
    local_punch = _utc_datetime(punched_at).astimezone(IST)
    attendance_day = local_punch.date()
    start = _parse_shift(employee["shift_start"])
    end = _parse_shift(employee["shift_end"])
    is_overnight = end <= start
    if is_overnight and local_punch.time().replace(tzinfo=None) < time(*end):
        attendance_day -= timedelta(days=1)
    return attendance_day


def _shift_boundary(
    attendance_day: date,
    employee: dict[str, Any],
    *,
    ending: bool,
) -> datetime:
    start_h, start_m = _parse_shift(employee["shift_start"])
    end_h, end_m = _parse_shift(employee["shift_end"])
    boundary = (end_h, end_m) if ending else (start_h, start_m)
    boundary_day = attendance_day
    if ending and (end_h, end_m) <= (start_h, start_m):
        boundary_day += timedelta(days=1)
    local_boundary = datetime.combine(
        boundary_day,
        time(boundary[0], boundary[1]),
        tzinfo=IST,
    )
    return local_boundary.astimezone(UTC)


def _compute_late_minutes(
    punch_in: datetime,
    attendance_day: date,
    employee: dict[str, Any],
) -> int:
    elapsed_seconds = int(
        (_utc_datetime(punch_in) - _shift_boundary(attendance_day, employee, ending=False))
        .total_seconds()
    )
    return elapsed_seconds // 60 if elapsed_seconds > 10 * 60 else 0


def _compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((_utc_datetime(punch_out) - _utc_datetime(punch_in)).total_seconds())
    return _round_half_up(Decimal(seconds) / Decimal(3600), 2)


def _compute_overtime_minutes(
    punch_out: datetime,
    attendance_day: date,
    employee: dict[str, Any],
) -> int:
    elapsed_seconds = int(
        (_utc_datetime(punch_out) - _shift_boundary(attendance_day, employee, ending=True))
        .total_seconds()
    )
    return elapsed_seconds // 60 if elapsed_seconds >= 30 * 60 else 0


def _working_days(start: date, end: date) -> int:
    if start > end:
        return 0
    total_days = (end - start).days + 1
    whole_weeks, remainder = divmod(total_days, 7)
    count = whole_weeks * 5
    for offset in range(remainder):
        if (start + timedelta(days=whole_weeks * 7 + offset)).weekday() < 5:
            count += 1
    return count


def _employee_to_api(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": _datetime_to_epoch_millis(document["created_at"]),
    }


def _history_value_to_api(value: Any, field_name: str) -> Any:
    if field_name in ("punch_in", "punch_out"):
        return _datetime_to_epoch_millis(value)
    return value


def _attendance_to_api(document: dict[str, Any]) -> dict[str, Any]:
    history: list[dict[str, Any]] = []
    for entry in document.get("history", []) or []:
        changes: dict[str, Any] = {}
        for field_name, change in (entry.get("changes") or {}).items():
            changes[field_name] = {
                "from": _history_value_to_api(change.get("from"), field_name),
                "to": _history_value_to_api(change.get("to"), field_name),
            }
        history.append(
            {
                "at": _datetime_to_epoch_millis(entry.get("at")),
                "by": entry.get("by", ""),
                "reason": entry.get("reason", ""),
                "changes": changes,
            }
        )
    work_hours = document.get("work_hours")
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": _datetime_to_epoch_millis(document.get("punch_in")),
        "punch_out": _datetime_to_epoch_millis(document.get("punch_out")),
        "work_hours": float(work_hours) if work_hours is not None else None,
        "late_minutes": int(document.get("late_minutes", 0) or 0),
        "overtime_minutes": int(document.get("overtime_minutes", 0) or 0),
        "half_day": bool(document.get("half_day", False)),
        "history": history,
    }


def _create_indexes(database: Database[dict[str, Any]]) -> None:
    employees = database["employees"]
    attendance = database["attendance_logs"]
    employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employees_emp_code",
    )
    employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employees_department_emp_code",
    )
    employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employees_joined_on",
    )
    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employees_department_joined_on",
    )
    attendance.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )
    attendance.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp_code",
    )
    attendance.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_status_date_emp_code",
    )
    attendance.create_index(
        [("emp_code", ASCENDING), ("status", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_emp_status_date",
    )
    attendance.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_late_emp_code",
    )


def _ensure_indexes(database: Database[dict[str, Any]]) -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    with _index_lock:
        if not _indexes_ready:
            _create_indexes(database)
            _indexes_ready = True
            logger.info("MongoDB attendance indexes are ready.")


def _required_database() -> Database[dict[str, Any]]:
    if _mongo_client is None or _database is None:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is not configured; set MONGO_URI and MONGO_DB.",
        )
    try:
        _mongo_client.admin.command("ping")
        _ensure_indexes(_database)
    except PyMongoError as exc:
        logger.warning("MongoDB request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable or its indexes could not be prepared.",
        ) from exc
    return _database


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _mongo_client, _database, _indexes_ready
    mongo_uri = os.getenv("MONGO_URI")
    database_name = os.getenv("MONGO_DB")
    _indexes_ready = False
    if not mongo_uri or not database_name:
        logger.warning("MONGO_URI or MONGO_DB is not configured; database routes return 503.")
    else:
        try:
            _mongo_client = MongoClient(
                mongo_uri,
                serverSelectionTimeoutMS=2_000,
                connectTimeoutMS=2_000,
                tz_aware=True,
                tzinfo=UTC,
            )
            _database = _mongo_client[database_name]
            _mongo_client.admin.command("ping")
            _ensure_indexes(_database)
        except PyMongoError as exc:
            logger.warning(
                "MongoDB was not ready during startup: %s",
                type(exc).__name__,
            )
    try:
        yield
    finally:
        if _mongo_client is not None:
            _mongo_client.close()
        _mongo_client = None
        _database = None
        _indexes_ready = False


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="Employee attendance and analytics API following the supplied assignment contract.",
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(ApiMountMiddleware)


@app.exception_handler(PyMongoError)
async def handle_database_error(_: Request, exc: PyMongoError) -> JSONResponse:
    logger.warning("MongoDB operation failed: %s", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": "MongoDB is unavailable or the database operation failed."},
    )


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    operation_id="health",
    responses={503: {"model": ErrorResponse}},
)
def health() -> dict[str, str]:
    # FastAPI dependencies are intentionally avoided here so the readiness
    # route can report an unavailable MongoDB as a clean 503.
    _required_database()
    return {"status": "ok"}


@app.post(
    "/employees",
    response_model=EmployeeResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["employees"],
    operation_id="createEmployee",
    responses={409: {"model": ErrorResponse}},
)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    database = _required_database()
    document = body.model_dump()
    document["created_at"] = _now_truncated()
    try:
        database["employees"].insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return _employee_to_api(document)


@app.get(
    "/employees",
    response_model=EmployeePage,
    tags=["employees"],
    operation_id="listEmployees",
)
def list_employees(
    department: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=PAGE_SIZE_MAX),
) -> dict[str, Any]:
    database = _required_database()
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    collection = database["employees"]
    total = collection.count_documents(query)
    items = collection.find(query).sort("emp_code", ASCENDING).skip(
        (page - 1) * page_size
    ).limit(page_size)
    return {
        "items": [_employee_to_api(document) for document in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post(
    "/attendance/punch-in",
    response_model=AttendanceRecordResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["attendance"],
    operation_id="punchIn",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    database = _required_database()
    employees = database["employees"]
    employee = employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = (
        _epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else _now_truncated()
    )
    attendance_day = _attendance_date_for_punch(punched_at, employee)
    document = {
        "emp_code": body.emp_code,
        "date": attendance_day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": _compute_late_minutes(punched_at, attendance_day, employee),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        database["attendance_logs"].insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this employee and date",
        ) from exc
    return _attendance_to_api(document)


def _attendance_filter(
    emp_code: str | None,
    date_from: date | None,
    date_to: date | None,
    record_status: Status | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        range_filter: dict[str, str] = {}
        if date_from is not None:
            range_filter["$gte"] = date_from.isoformat()
        if date_to is not None:
            range_filter["$lte"] = date_to.isoformat()
        query["date"] = range_filter
    if record_status is not None:
        query["status"] = record_status
    return query


def _check_date_order(date_from: date | None, date_to: date | None) -> None:
    if date_from is not None and date_to is not None and date_from > date_to:
        _raise_query_validation(
            "date_from",
            "date_from must be on or before date_to",
            date_from.isoformat(),
        )


@app.get(
    "/attendance",
    response_model=AttendancePage,
    tags=["attendance"],
    operation_id="listAttendance",
)
def list_attendance(
    emp_code: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    record_status: Status | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=PAGE_SIZE_MAX),
) -> dict[str, Any]:
    _check_date_order(date_from, date_to)
    database = _required_database()
    query = _attendance_filter(emp_code, date_from, date_to, record_status)
    collection = database["attendance_logs"]
    total = collection.count_documents(query)
    cursor = (
        collection.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [_attendance_to_api(document) for document in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post(
    "/attendance/punch-out",
    response_model=AttendanceRecordResponse,
    tags=["attendance"],
    operation_id="punchOut",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    database = _required_database()
    employee = database["employees"].find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = (
        _epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else _now_truncated()
    )
    collection = database["attendance_logs"]
    record = collection.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="No punch-in was found")

    punch_in_at = _utc_datetime(record["punch_in"])
    elapsed_seconds = int((punched_at - punch_in_at).total_seconds())
    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch_in and no more than 24 hours later",
        )

    attendance_day = date.fromisoformat(record["date"])
    work_hours = _compute_work_hours(punch_in_at, punched_at)
    overtime_minutes = _compute_overtime_minutes(punched_at, attendance_day, employee)
    updated = collection.find_one_and_update(
        {
            "_id": record["_id"],
            "$or": [
                {"punch_out": None},
                {"punch_out": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.50,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="This attendance record has already been punched out",
        )
    return _attendance_to_api(updated)


def _same_value(field_name: str, left: Any, right: Any) -> bool:
    if field_name in ("punch_in", "punch_out"):
        if left is None or right is None:
            return left is right
        return _utc_datetime(left) == _utc_datetime(right)
    if field_name == "work_hours":
        if left is None or right is None:
            return left is right
        return _decimal_value(left) == _decimal_value(right)
    return left == right


def _changes_for_record(
    original: dict[str, Any],
    updated_fields: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    defaults = {
        "status": None,
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    changes: dict[str, dict[str, Any]] = {}
    for field_name, after in updated_fields.items():
        before = original.get(field_name, defaults[field_name])
        if not _same_value(field_name, before, after):
            changes[field_name] = {"from": before, "to": after}
    return changes


@app.patch(
    "/attendance/{emp_code}/{date}",
    response_model=AttendanceRecordResponse,
    tags=["attendance"],
    operation_id="regularizeAttendance",
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
def regularize_attendance(
    emp_code: str,
    date: date,
    body: RegularizeRequest,
) -> dict[str, Any]:
    database = _required_database()
    employees = database["employees"]
    employee = employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    record_date = date.isoformat()
    collection = database["attendance_logs"]
    original = collection.find_one({"emp_code": emp_code, "date": record_date})
    if original is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    supplied = body.model_fields_set
    final_status = body.status if "status" in supplied else original["status"]
    final_punch_in = (
        _epoch_millis_to_datetime(body.punch_in)
        if "punch_in" in supplied and body.punch_in is not None
        else original.get("punch_in")
    )
    final_punch_out = (
        _epoch_millis_to_datetime(body.punch_out)
        if "punch_out" in supplied and body.punch_out is not None
        else original.get("punch_out")
    )

    if final_status in ("ABSENT", "LEAVE"):
        if supplied.intersection({"punch_in", "punch_out"}):
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE corrections cannot include punch times",
            )
        final_punch_in = None
        final_punch_out = None
        derived = {
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        if final_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="A presence status requires a punch_in",
            )
        if _attendance_date_for_punch(final_punch_in, employee) != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must remain on the record's attendance date",
            )
        if final_punch_out is not None:
            duration = int((final_punch_out - final_punch_in).total_seconds())
            if duration <= 0 or duration > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in and no more than 24 hours later",
                )
            work_hours = _compute_work_hours(final_punch_in, final_punch_out)
            derived = {
                "work_hours": work_hours,
                "late_minutes": _compute_late_minutes(final_punch_in, date, employee),
                "overtime_minutes": _compute_overtime_minutes(
                    final_punch_out,
                    date,
                    employee,
                ),
                "half_day": work_hours < 4.50,
            }
        else:
            derived = {
                "work_hours": None,
                "late_minutes": _compute_late_minutes(final_punch_in, date, employee),
                "overtime_minutes": 0,
                "half_day": False,
            }

    updated_fields = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        **derived,
    }
    changes = _changes_for_record(original, updated_fields)
    if not changes:
        raise HTTPException(status_code=422, detail="The correction changes no values")

    history_entry = {
        "at": _now_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    compare_fields = (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    )
    compare_filter: dict[str, Any] = {"_id": original["_id"]}
    for field_name in compare_fields:
        if field_name in original:
            compare_filter[field_name] = original[field_name]
        else:
            compare_filter[field_name] = {"$exists": False}
    if "history" in original:
        compare_filter["history"] = original["history"]
    else:
        compare_filter["history"] = {"$exists": False}

    updated = collection.find_one_and_update(
        compare_filter,
        {
            "$set": updated_fields,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="The record changed during regularization; reload and retry",
        )
    return _attendance_to_api(updated)


def _month_match(month_start: date, month_end: date) -> dict[str, Any]:
    return {
        "$gte": month_start.isoformat(),
        "$lte": month_end.isoformat(),
    }


def _employee_monthly_pipeline(
    emp_code: str,
    month_start: date,
    month_end: date,
) -> list[dict[str, Any]]:
    weekday = {
        "$in": [
            {
                "$dayOfWeek": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "timezone": "Asia/Kolkata",
                    }
                }
            },
            [2, 3, 4, 5, 6],
        ]
    }
    presence_and_weekday = {
        "$and": [
            {"$in": ["$status", list(PRESENCE_STATUSES)]},
            weekday,
        ]
    }
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": _month_match(month_start, month_end),
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            presence_and_weekday,
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthlyResponse,
    tags=["analytics"],
    operation_id="employeeMonthly",
    responses={404: {"model": ErrorResponse}},
)
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
) -> dict[str, Any]:
    month_start, month_end = _parse_month(month)
    database = _required_database()
    employee = database["employees"].find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    pipeline = _employee_monthly_pipeline(emp_code, month_start, month_end)
    aggregate = list(database["attendance_logs"].aggregate(pipeline))
    metrics = aggregate[0] if aggregate else {}
    joined_on = date.fromisoformat(employee["joined_on"])
    working_days = _working_days(max(month_start, joined_on), month_end)
    present_days = float(metrics.get("present_days", 0.0))
    attendance_pct = None
    if working_days:
        attendance_pct = _round_half_up(
            Decimal(str(present_days)) / Decimal(working_days) * Decimal(100),
            4,
        )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": int(metrics.get("leave_days", 0)),
        "late_count": int(metrics.get("late_count", 0)),
        "total_late_minutes": int(metrics.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(metrics.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


def _department_summary_pipeline(
    month_start: date,
    month_end: date,
    department: str | None,
) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": month_end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    log_status = {"$ifNull": ["$logs.status", ""]}
    log_day = {
        "$dayOfWeek": {
            "$dateFromString": {
                "dateString": "$logs.date",
                "timezone": "Asia/Kolkata",
            }
        }
    }
    work_hours_eligible = {
        "$and": [
            {"$in": [log_status, list(PRESENCE_STATUSES)]},
            {"$ne": ["$logs.work_hours", None]},
        ]
    }
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {
                                        "$gte": [
                                            "$date",
                                            month_start.isoformat(),
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$date",
                                            month_end.isoformat(),
                                        ]
                                    },
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$set": {
                "log_day_of_week": {
                    "$cond": [
                        {"$eq": [{"$ifNull": ["$logs.date", None]}, None]},
                        0,
                        log_day,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": {
                    "department": "$department",
                    "emp_code": "$emp_code",
                },
                "headcount": {"$first": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": [log_status, list(PRESENCE_STATUSES)]},
                                    {"$in": ["$log_day_of_week", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            work_hours_eligible,
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {"$cond": [work_hours_eligible, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": [log_status, "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": [log_status, "ON_DUTY"]}, 1, 0]}
                },
            }
        },
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummaryResponse,
    tags=["analytics"],
    operation_id="departmentSummary",
)
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
) -> dict[str, Any]:
    month_start, month_end = _parse_month(month)
    database = _required_database()
    pipeline = _department_summary_pipeline(month_start, month_end, department)
    rows = list(database["employees"].aggregate(pipeline, allowDiskUse=True))
    items = [
        {
            **row,
            "present_days": float(row.get("present_days", 0.0)),
            "avg_work_hours": (
                _round_half_up(row["avg_work_hours"], 2)
                if row.get("avg_work_hours") is not None
                else None
            ),
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        }
        for row in rows
        if int(row.get("headcount", 0)) > 0
    ]
    return {"month": month, "items": items}


def _late_leaderboard_pipeline(
    month_start: date,
    month_end: date,
    limit: int,
    department: str | None,
) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": _month_match(month_start, month_end),
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": DESCENDING},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


@app.get(
    "/analytics/leaderboard/late",
    response_model=LeaderboardResponse,
    tags=["analytics"],
    operation_id="lateLeaderboard",
)
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: str | None = None,
) -> dict[str, Any]:
    month_start, month_end = _parse_month(month)
    database = _required_database()
    pipeline = _late_leaderboard_pipeline(
        month_start,
        month_end,
        limit,
        department,
    )
    items = list(database["attendance_logs"].aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}


def _decimal_round_expression(value: Any, decimal_places: int) -> dict[str, Any]:
    factor = Decimal128(str(10**decimal_places))
    scaled = {"$multiply": [value, factor]}
    floor_value = {"$floor": scaled}
    remainder = {"$subtract": [scaled, floor_value]}
    rounded_integer = {
        "$add": [
            floor_value,
            {
                "$cond": [
                    {"$gte": [remainder, Decimal128("0.5")]},
                    Decimal128("1"),
                    Decimal128("0"),
                ]
            },
        ]
    }
    return {"$divide": [rounded_integer, factor]}


def _department_trend_pipeline(
    department: str,
    first_day: date,
    last_day: date,
) -> list[dict[str, Any]]:
    number_of_days = (last_day - first_day).days + 1
    first_day_utc = datetime.combine(first_day, time.min, tzinfo=UTC)
    rate = {
        "$divide": [
            {"$toDecimal": "$present_count"},
            {"$toDecimal": "$headcount"},
        ]
    }
    return [
        {"$documents": [{"calendar": True}]},
        {"$project": {"_id": 0, "offsets": {"$range": [0, number_of_days]}}},
        {"$unwind": "$offsets"},
        {
            "$set": {
                "day": {
                    "$dateAdd": {
                        "startDate": first_day_utc,
                        "unit": "day",
                        "amount": "$offsets",
                    }
                }
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day",
                        "timezone": "UTC",
                    }
                },
                "day_of_week": {"$dayOfWeek": "$day"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"calendar_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {"$lte": ["$joined_on", "$$calendar_date"]},
                        }
                    },
                    {"$count": "value"},
                ],
                "as": "headcount_rows",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_rows.value", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"calendar_date": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$calendar_date"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                list(PRESENCE_STATUSES),
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        {"$ifNull": ["$half_day", False]},
                                                        True,
                                                    ]
                                                },
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily_metrics",
            }
        },
        {
            "$set": {
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily_metrics.present_count", 0]},
                        0.0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily_metrics.late_count", 0]},
                        0,
                    ]
                },
                "is_working_day": {
                    "$in": ["$day_of_week", [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        _decimal_round_expression(rate, 4),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": [{"$ifNull": ["$moving_avg_raw", None]}, None]},
                        None,
                        _decimal_round_expression("$moving_avg_raw", 4),
                    ]
                }
            }
        },
        {"$sort": {"date": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=TrendResponse,
    tags=["analytics"],
    operation_id="departmentTrend",
    responses={404: {"model": ErrorResponse}},
)
def department_trend(
    department: str,
    from_day: date = Query(alias="from"),
    to_day: date = Query(alias="to"),
) -> dict[str, Any]:
    if to_day < from_day:
        _raise_query_validation("to", "to must be on or after from", to_day.isoformat())
    if (to_day - from_day).days + 1 > 92:
        _raise_query_validation("to", "the date range cannot exceed 92 days", to_day.isoformat())
    database = _required_database()
    if database["employees"].count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")
    pipeline = _department_trend_pipeline(department, from_day, to_day)
    rows = list(database.aggregate(pipeline, allowDiskUse=True))
    items = [
        {
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": float(_decimal_value(row["present_count"])),
            "late_count": int(row["late_count"]),
            "attendance_rate": (
                float(_decimal_value(row["attendance_rate"]))
                if row.get("attendance_rate") is not None
                else None
            ),
            "moving_avg_7d": (
                float(_decimal_value(row["moving_avg_7d"]))
                if row.get("moving_avg_7d") is not None
                else None
            ),
        }
        for row in rows
    ]
    return {"department": department, "items": items}


def _explain_find(
    database: Database[dict[str, Any]],
    query: dict[str, Any],
    page: int,
    page_size: int,
) -> dict[str, Any]:
    return database.command(
        {
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": DESCENDING, "emp_code": ASCENDING},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
    )


def _explain_aggregate(
    database: Database[dict[str, Any]],
    collection: str,
    pipeline: list[dict[str, Any]],
    *,
    database_level: bool = False,
) -> dict[str, Any]:
    return database.command(
        {
            "explain": {
                "aggregate": 1 if database_level else collection,
                "pipeline": pipeline,
                "cursor": {},
                "allowDiskUse": True,
            },
            "verbosity": "executionStats",
        }
    )


@app.get(
    "/admin/explain/{endpoint}",
    tags=["admin"],
    operation_id="explainEndpoint",
    responses={422: {"model": ErrorResponse}},
)
def explain_endpoint(
    endpoint: ExplainEndpoint,
    emp_code: str | None = None,
    month: str | None = Query(default=None),
    department: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: date | None = None,
    date_to: date | None = None,
    record_status: Status | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=PAGE_SIZE_MAX),
    from_day: date | None = Query(default=None, alias="from"),
    to_day: date | None = Query(default=None, alias="to"),
) -> JSONResponse:
    database = _required_database()
    explained_collection: str
    if endpoint == "attendance_list":
        _check_date_order(date_from, date_to)
        query = _attendance_filter(emp_code, date_from, date_to, record_status)
        raw = _explain_find(database, query, page, page_size)
        explained_collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None:
            _raise_query_validation("emp_code", "Field required")
        if month is None:
            _raise_query_validation("month", "Field required")
        month_start, month_end = _parse_month(month)
        pipeline = _employee_monthly_pipeline(emp_code, month_start, month_end)
        raw = _explain_aggregate(database, "attendance_logs", pipeline)
        explained_collection = "attendance_logs"
    elif endpoint == "department_summary":
        if month is None:
            _raise_query_validation("month", "Field required")
        month_start, month_end = _parse_month(month)
        pipeline = _department_summary_pipeline(month_start, month_end, department)
        raw = _explain_aggregate(database, "employees", pipeline)
        explained_collection = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            _raise_query_validation("month", "Field required")
        month_start, month_end = _parse_month(month)
        pipeline = _late_leaderboard_pipeline(
            month_start,
            month_end,
            limit,
            department,
        )
        raw = _explain_aggregate(database, "attendance_logs", pipeline)
        explained_collection = "attendance_logs"
    else:
        if department is None:
            _raise_query_validation("department", "Field required")
        if from_day is None:
            _raise_query_validation("from", "Field required")
        if to_day is None:
            _raise_query_validation("to", "Field required")
        if to_day < from_day:
            _raise_query_validation("to", "to must be on or after from", to_day.isoformat())
        if (to_day - from_day).days + 1 > 92:
            _raise_query_validation("to", "the date range cannot exceed 92 days", to_day.isoformat())
        pipeline = _department_trend_pipeline(department, from_day, to_day)
        raw = _explain_aggregate(
            database,
            "attendance_logs",
            pipeline,
            database_level=True,
        )
        explained_collection = "attendance_logs"

    payload = {
        "endpoint": endpoint,
        "collection": explained_collection,
        "explain": json.loads(json_util.dumps(raw)),
    }
    return JSONResponse(content=payload)
