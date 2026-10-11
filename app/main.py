"""Employee attendance and analytics API.

Run with ``uvicorn app.main:app --port 8000``. MongoDB connection settings are
read from ``MONGO_URI`` and ``MONGO_DB`` (a local ``.env`` is supported).
"""

import calendar
import json
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, NoReturn, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
attendance_logs = db["attendance_logs"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employee_department_joined",
    )
    employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_punch_in",
    )


def invalid(message: str) -> NoReturn:
    raise HTTPException(status_code=422, detail=message)


def parse_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        invalid(f"{field_name} must be a valid YYYY-MM-DD date")
    if parsed.isoformat() != value:
        invalid(f"{field_name} must be a valid YYYY-MM-DD date")
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        invalid("month must use YYYY-MM format")
    year, month = (int(part) for part in value.split("-"))
    try:
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        invalid("month must be a valid calendar month")
    return first, last


def truncate_seconds(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_millis_to_datetime(value: int) -> datetime:
    return datetime.fromtimestamp((value // 1000), tz=UTC)


def datetime_to_epoch_millis(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int(truncate_seconds(value).timestamp()) * 1000


def now_utc_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def attendance_date(punch_in: datetime, employee: dict[str, Any]) -> date:
    local = punch_in.astimezone(IST)
    start_minutes = _minutes_of_day(employee["shift_start"])
    end_minutes = _minutes_of_day(employee["shift_end"])
    if end_minutes <= start_minutes and local.hour * 60 + local.minute < end_minutes:
        return local.date() - timedelta(days=1)
    return local.date()


def _minutes_of_day(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def compute_late_minutes(punch_in: datetime, shift_start: str, record_date: str) -> int:
    local_in = punch_in.astimezone(IST)
    start_hour, start_minute = (int(part) for part in shift_start.split(":"))
    shift_start_at = datetime.combine(
        parse_date(record_date),
        time(start_hour, start_minute),
        tzinfo=IST,
    )
    elapsed_seconds = int((local_in - shift_start_at).total_seconds())
    if elapsed_seconds <= 10 * 60:
        return 0
    return elapsed_seconds // 60


def _shift_end_datetime(shift_end: str, record_date: str, shift_start: str) -> datetime:
    end_hour, end_minute = (int(part) for part in shift_end.split(":"))
    start_minutes = _minutes_of_day(shift_start)
    end_minutes = _minutes_of_day(shift_end)
    end_date = parse_date(record_date)
    if end_minutes <= start_minutes:
        end_date += timedelta(days=1)
    return datetime.combine(end_date, time(end_hour, end_minute), tzinfo=IST)


def compute_overtime_minutes(punch_out: datetime, shift_end: str, shift_start: str, record_date: str) -> int:
    end_at = _shift_end_datetime(shift_end, record_date, shift_start)
    elapsed_seconds = int((punch_out.astimezone(IST) - end_at).total_seconds())
    if elapsed_seconds < 30 * 60:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def round_number(value: Any, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    decimal_value = value.to_decimal() if isinstance(value, Decimal128) else Decimal(str(value))
    rounded = decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)
    return float(rounded)


def numeric_float(value: Any) -> float:
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    return float(value)


def round_percentage(numerator: Any, denominator: Any) -> Optional[float]:
    if not denominator:
        return None
    value = Decimal(str(numerator)) / Decimal(str(denominator)) * Decimal(100)
    return float(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _serialize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    serialized = []
    for entry in history or []:
        changes: dict[str, Any] = {}
        for field_name, values in entry.get("changes", {}).items():
            before = values.get("from")
            after = values.get("to")
            if field_name in ("punch_in", "punch_out"):
                before = datetime_to_epoch_millis(before)
                after = datetime_to_epoch_millis(after)
            changes[field_name] = {"from": before, "to": after}
        serialized.append(
            {
                "at": datetime_to_epoch_millis(entry.get("at")),
                "by": entry.get("by"),
                "reason": entry.get("reason"),
                "changes": changes,
            }
        )
    return serialized


def serialize_attendance(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": datetime_to_epoch_millis(document.get("punch_in")),
        "punch_out": datetime_to_epoch_millis(document.get("punch_out")),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": _serialize_history(document.get("history", [])),
    }


def serialize_employee(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": document["emp_code"],
        "name": document["name"],
        "email": document["email"],
        "department": document["department"],
        "shift_start": document["shift_start"],
        "shift_end": document["shift_end"],
        "joined_on": document["joined_on"],
        "created_at": datetime_to_epoch_millis(document["created_at"]),
    }


def _working_days(start: date, end: date) -> int:
    if start > end:
        return 0
    full_weeks, remaining_days = divmod((end - start).days + 1, 7)
    days = full_weeks * 5
    for offset in range(remaining_days):
        if (start.weekday() + offset) % 7 < 5:
            days += 1
    return days


def _day_of_week_expression(date_field: str) -> dict[str, Any]:
    return {
        "$cond": [
            {"$eq": [{"$type": date_field}, "string"]},
            {"$dayOfWeek": {"$dateFromString": {"dateString": date_field}}},
            None,
        ]
    }


def _present_day_expression(status_field: str, half_day_field: str, weekday_field: str) -> dict[str, Any]:
    return {
        "$cond": [
            {
                "$and": [
                    {"$in": [{"$ifNull": [status_field, None]}, list(PRESENCE_STATUSES)]},
                    {"$gte": [weekday_field, 2]},
                    {"$lte": [weekday_field, 6]},
                ]
            },
            {"$cond": [{"$ifNull": [half_day_field, False]}, 0.5, 1]},
            0,
        ]
    }


def employee_monthly_pipeline(emp_code: str, start: date, end: date) -> list[dict[str, Any]]:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$addFields": {"_weekday": _day_of_week_expression("$date")}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": _present_day_expression("$status", "$half_day", "$_weekday")
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(
    start: date, end: date, department: Optional[str] = None
) -> list[dict[str, Any]]:
    employee_match: dict[str, Any] = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
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
                                    {"$gte": ["$date", start.isoformat()]},
                                    {"$lte": ["$date", end.isoformat()]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"_record_weekday": _day_of_week_expression("$logs.date")}},
        {
            "$group": {
                "_id": {"emp_code": "$emp_code", "department": "$department"},
                "headcount": {"$first": 1},
                "present_days": {
                    "$sum": _present_day_expression(
                        "$logs.status", "$logs.half_day", "$_record_weekday"
                    )
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            {"$toDecimal": {"$toString": "$logs.work_hours"}},
                            Decimal128("0"),
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
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
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
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
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$work_hours_count", 0]},
                        None,
                        {
                            "$divide": [
                                "$work_hours_sum",
                                {"$toDecimal": "$work_hours_count"},
                            ]
                        },
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def department_summary_index_hint(department: Optional[str]) -> str:
    return "ix_employee_department_joined" if department is not None else "ix_employee_joined"


def late_leaderboard_pipeline(
    start: date, end: date, limit: int, department: Optional[str] = None
) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}, "late_minutes": {"$gt": 0}}},
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
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
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


def _half_up_decimal_expression(value: Any, scale: int) -> dict[str, Any]:
    factor = Decimal128(str(10**scale))
    half = Decimal128("0.5")
    return {
        "$divide": [
            {"$trunc": {"$add": [{"$multiply": [value, factor]}, half]}},
            factor,
        ]
    }


def department_trend_pipeline(department: str, start: date, end: date) -> list[dict[str, Any]]:
    start_expression = {"$dateFromString": {"dateString": start.isoformat()}}
    end_expression = {"$dateFromString": {"dateString": end.isoformat()}}
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "department": {"$literal": department},
                "dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": start_expression,
                                                "endDate": end_expression,
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": start_expression,
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$dates"},
        {"$set": {"date": "$dates", "_date": {"$dateFromString": {"dateString": "$dates"}}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", {"$literal": department}]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_docs",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$headcount_docs.count", 0]}, 0]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "let": {"employee_code": "$emp_code"},
                            "pipeline": [
                                {
                                    "$match": {
                                        "$expr": {
                                            "$and": [
                                                {"$eq": ["$emp_code", "$$employee_code"]},
                                                {"$eq": ["$department", {"$literal": department}]},
                                            ]
                                        }
                                    }
                                },
                                {"$limit": 1},
                            ],
                            "as": "employee",
                        }
                    },
                    {"$match": {"$expr": {"$gt": [{"$size": "$employee"}, 0]}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                {"$ifNull": ["$status", None]},
                                                list(PRESENCE_STATUSES),
                                            ]
                                        },
                                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                                        0,
                                    ]
                                }
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
                        }
                    },
                ],
                "as": "day_stats",
            }
        },
        {
            "$set": {
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$day_stats.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$day_stats.late_count", 0]}, 0]},
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_date"}, 6]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        _half_up_decimal_expression(
                            {
                                "$divide": [
                                    {"$toDecimal": "$present_count"},
                                    {"$toDecimal": "$headcount"},
                                ]
                            },
                            4,
                        ),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_date": 1},
                "output": {
                    "moving_avg_7d": {
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
                        {"$eq": ["$moving_avg_7d", None]},
                        None,
                        _half_up_decimal_expression("$moving_avg_7d", 4),
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        parse_date(value, "joined_on")
        return value

    @model_validator(mode="after")
    def validate_shift(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    document = body.model_dump()
    document["created_at"] = now_utc_seconds()
    try:
        employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = employees.count_documents(query)
    documents = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(
        (page - 1) * page_size
    ).limit(page_size)
    return {
        "items": [serialize_employee(document) for document in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest) -> dict[str, Any]:
    employee = employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = (
        epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else now_utc_seconds()
    )
    record_day = attendance_date(punched_at, employee).isoformat()
    document = {
        "emp_code": body.emp_code,
        "date": record_day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], record_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409, detail="already punched in for this date"
        ) from exc
    return serialize_attendance(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest) -> dict[str, Any]:
    employee = employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = (
        epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else now_utc_seconds()
    )
    document = attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if document is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if document.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    punch_in_at = truncate_seconds(document["punch_in"])
    if punched_at <= punch_in_at or punched_at - punch_in_at > timedelta(hours=24):
        invalid("punched_at must be after punch_in and no more than 24 hours later")
    work_hours = compute_work_hours(punch_in_at, punched_at)
    update = {
        "punch_in": punch_in_at,
        "punch_out": punched_at,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime_minutes(
            punched_at, employee["shift_end"], employee["shift_start"], document["date"]
        ),
        "half_day": work_hours < 4.50,
    }
    result = attendance_logs.update_one(
        {
            "_id": document["_id"],
            "punch_in": document["punch_in"],
            "punch_out": None,
        },
        {"$set": update},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="record is already punched out")
    updated = attendance_logs.find_one({"_id": document["_id"]})
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        invalid("date_from must be on or before date_to")
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    total = attendance_logs.count_documents(query)
    index_hint = (
        "uq_attendance_employee_date"
        if emp_code is not None
        else "ix_attendance_date_employee"
    )
    documents = attendance_logs.find(query).hint(index_hint).sort(
        [("date", DESCENDING), ("emp_code", ASCENDING)]
    ).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [serialize_attendance(document) for document in documents],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _legacy_value(document: dict[str, Any], field_name: str) -> Any:
    defaults = {
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    return document.get(field_name, defaults.get(field_name))


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str, date: str, body: RegularizeRequest
) -> dict[str, Any]:
    parse_date(date, "date")
    fields_set = body.model_fields_set
    for name in ("status", "punch_in", "punch_out"):
        if name in fields_set and getattr(body, name) is None:
            invalid(f"{name} cannot be null")

    employee = employees.find_one({"emp_code": emp_code})
    document = attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if employee is None or document is None:
        raise HTTPException(status_code=404, detail="employee or attendance record not found")

    old_status = document["status"]
    new_status = body.status if "status" in fields_set else old_status
    supplied_punch = "punch_in" in fields_set or "punch_out" in fields_set
    if new_status in ("ABSENT", "LEAVE") and supplied_punch:
        invalid("punch times cannot be supplied for ABSENT or LEAVE")

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = (
            epoch_millis_to_datetime(body.punch_in)
            if "punch_in" in fields_set
            else document.get("punch_in")
        )
        new_punch_out = (
            epoch_millis_to_datetime(body.punch_out)
            if "punch_out" in fields_set
            else document.get("punch_out")
        )
        if new_punch_in is None:
            invalid("a presence status requires punch_in")
        new_punch_in = truncate_seconds(new_punch_in)
        if attendance_date(new_punch_in, employee).isoformat() != date:
            invalid("punch_in must belong to the record's attendance date")
        if new_punch_out is not None:
            new_punch_out = truncate_seconds(new_punch_out)
            if new_punch_out <= new_punch_in:
                invalid("punch_out must be after punch_in")
            if new_punch_out - new_punch_in > timedelta(hours=24):
                invalid("punch_out must be within 24 hours of punch_in")

    next_values: dict[str, Any] = {"status": new_status, "punch_in": new_punch_in, "punch_out": new_punch_out}
    if new_status in ("ABSENT", "LEAVE"):
        next_values.update(
            {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
        )
    else:
        next_values["late_minutes"] = compute_late_minutes(
            new_punch_in, employee["shift_start"], date
        )
        if new_punch_out is None:
            next_values.update({"work_hours": None, "overtime_minutes": 0, "half_day": False})
        else:
            hours = compute_work_hours(new_punch_in, new_punch_out)
            next_values.update(
                {
                    "work_hours": hours,
                    "overtime_minutes": compute_overtime_minutes(
                        new_punch_out,
                        employee["shift_end"],
                        employee["shift_start"],
                        date,
                    ),
                    "half_day": hours < 4.50,
                }
            )

    changed: dict[str, dict[str, Any]] = {}
    for field_name, new_value in next_values.items():
        old_value = _legacy_value(document, field_name)
        if old_value != new_value:
            changed[field_name] = {"from": old_value, "to": new_value}
    if not changed:
        invalid("the correction does not change the record")

    history = document.get("history", [])
    history_entry = {
        "at": now_utc_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changed,
    }
    optimistic_filter: dict[str, Any] = {"_id": document["_id"]}
    for field_name in ("status", "punch_in", "punch_out", "history"):
        if field_name in document:
            optimistic_filter[field_name] = document[field_name]
        else:
            optimistic_filter[field_name] = {"$exists": False}
    result = attendance_logs.update_one(
        optimistic_filter,
        {"$set": next_values, "$push": {"history": history_entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="record changed concurrently; retry the correction")
    updated = attendance_logs.find_one({"_id": document["_id"]})
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    employee = employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    first, last = parse_month(month)
    summary = next(
        iter(
            attendance_logs.aggregate(
                employee_monthly_pipeline(emp_code, first, last),
                hint="uq_attendance_employee_date",
            )
        ),
        {},
    )
    working_start = max(first, parse_date(employee["joined_on"], "joined_on"))
    working_days = _working_days(working_start, last)
    present_days = round_number(summary.get("present_days", 0))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": round_percentage(present_days, working_days),
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict[str, Any]:
    first, last = parse_month(month)
    items = []
    for row in employees.aggregate(
        department_summary_pipeline(first, last, department),
        hint=department_summary_index_hint(department),
    ):
        items.append(
            {
                "department": row["department"],
                "headcount": row["headcount"],
                "present_days": round_number(row["present_days"]),
                "avg_work_hours": (
                    round_number(row["avg_work_hours"])
                    if row["avg_work_hours"] is not None
                    else None
                ),
                "late_count": row["late_count"],
                "total_late_minutes": row["total_late_minutes"],
                "leave_count": row["leave_count"],
                "on_duty_count": row["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    first, last = parse_month(month)
    items = list(
        attendance_logs.aggregate(
            late_leaderboard_pipeline(first, last, limit, department),
            hint="ix_attendance_date_employee",
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
) -> dict[str, Any]:
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start:
        invalid("to must be on or after from")
    if (end - start).days + 1 > 92:
        invalid("trend range cannot exceed 92 days")
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    items = []
    for row in employees.aggregate(
        department_trend_pipeline(department, start, end),
        hint="ix_employee_department_joined",
    ):
        items.append(
            {
                "department": row["department"],
                "date": row["date"],
                "is_working_day": row["is_working_day"],
                "headcount": row["headcount"],
                "present_count": round_number(row["present_count"]),
                "late_count": row["late_count"],
                "attendance_rate": (
                    numeric_float(row["attendance_rate"])
                    if row["attendance_rate"] is not None
                    else None
                ),
                "moving_avg_7d": (
                    numeric_float(row["moving_avg_7d"])
                    if row["moving_avg_7d"] is not None
                    else None
                ),
            }
        )
    return {"department": department, "items": items}


def _explain(operation: dict[str, Any]) -> dict[str, Any]:
    result = db.command(
        "explain",
        operation,
        verbosity="executionStats",
    )
    return json.loads(json_util.dumps(result))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from is not None:
            parse_date(date_from, "date_from")
        if date_to is not None:
            parse_date(date_to, "date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            invalid("date_from must be on or before date_to")
        if date_from is not None or date_to is not None:
            query["date"] = {}
            if date_from is not None:
                query["date"]["$gte"] = date_from
            if date_to is not None:
                query["date"]["$lte"] = date_to
        if status is not None:
            query["status"] = status
        operation = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "hint": (
                "uq_attendance_employee_date"
                if emp_code is not None
                else "ix_attendance_date_employee"
            ),
        }
        collection_name = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            invalid("employee_monthly requires emp_code and month")
        first, last = parse_month(month)
        operation = {
            "aggregate": "attendance_logs",
            "pipeline": employee_monthly_pipeline(emp_code, first, last),
            "cursor": {},
            "hint": "uq_attendance_employee_date",
        }
        collection_name = "attendance_logs"
    elif endpoint == "department_summary":
        if month is None:
            invalid("department_summary requires month")
        first, last = parse_month(month)
        operation = {
            "aggregate": "employees",
            "pipeline": department_summary_pipeline(first, last, department),
            "cursor": {},
            "hint": department_summary_index_hint(department),
        }
        collection_name = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            invalid("late_leaderboard requires month")
        first, last = parse_month(month)
        operation = {
            "aggregate": "attendance_logs",
            "pipeline": late_leaderboard_pipeline(first, last, limit, department),
            "cursor": {},
            "hint": "ix_attendance_date_employee",
        }
        collection_name = "attendance_logs"
    else:
        if department is None or from_date is None or to_date is None:
            invalid("department_trend requires department, from, and to")
        start = parse_date(from_date, "from")
        end = parse_date(to_date, "to")
        if end < start:
            invalid("to must be on or after from")
        if (end - start).days + 1 > 92:
            invalid("trend range cannot exceed 92 days")
        operation = {
            "aggregate": "employees",
            "pipeline": department_trend_pipeline(department, start, end),
            "cursor": {},
            "hint": "ix_employee_department_joined",
        }
        collection_name = "employees"
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": _explain(operation),
    }
