from __future__ import annotations

import os
import secrets
import math
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from runner import FormRunner
from storage import Store


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
store = Store(DATA_DIR)
runner = FormRunner(store)
app = FastAPI(title="Voxtry", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

COOKIE_NAME = "voxtry_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 7
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "admin")
signer = URLSafeTimedSerializer(SECRET_KEY, salt="voxtry-panel")


class LoginReq(BaseModel):
    password: str


class TextReq(BaseModel):
    text: str = Field(default="")


class SettingsReq(BaseModel):
    values: dict[str, Any]


class RunStartReq(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class ProfileCardNameReq(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def validate_settings(values: dict[str, Any]) -> dict[str, Any]:
    values = dict(values)
    for key, minimum, label in (
        ("timeout_ms", 1000, "timeout_ms"),
        ("task_count", 1, "任务次数"),
        ("concurrency", 1, "并发数"),
    ):
        if key not in values:
            continue
        try:
            values[key] = int(values[key])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{label}必须是整数") from exc
        if values[key] < minimum:
            raise HTTPException(status_code=400, detail=f"{label}不能小于 {minimum}")
    if "settlement_wait_minutes" in values:
        try:
            values["settlement_wait_minutes"] = float(values["settlement_wait_minutes"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="结算等待时间必须是数字") from exc
        if not math.isfinite(values["settlement_wait_minutes"]):
            raise HTTPException(status_code=400, detail="结算等待时间必须是有限数字")
        if values["settlement_wait_minutes"] < 0:
            raise HTTPException(status_code=400, detail="结算等待时间不能小于 0")
    return values


def is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        return signer.loads(token, max_age=COOKIE_MAX_AGE) == "ok"
    except (BadSignature, SignatureExpired):
        return False


def require_auth(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="未登录")


def state_payload() -> dict[str, Any]:
    state = store.snapshot()
    return {
        "emails": state["emails"],
        "postcodes": state["postcodes"],
        "profile_cards": state["profile_cards"],
        "settings": state["settings"],
        "task_records": state["task_records"],
        "runtime": state["runtime"],
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/me")
def me(request: Request) -> dict[str, bool]:
    return {"authed": is_authed(request)}


@app.post("/api/login")
def login(req: LoginReq) -> JSONResponse:
    if not secrets.compare_digest(req.password, PANEL_PASSWORD):
        raise HTTPException(status_code=401, detail="密码错误")
    response = JSONResponse({"ok": True})
    response.set_cookie(
        COOKIE_NAME,
        signer.dumps("ok"),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/api/state", dependencies=[Depends(require_auth)])
def get_state() -> dict[str, Any]:
    return state_payload()


@app.get("/api/debug/{filename:path}", dependencies=[Depends(require_auth)])
def get_debug_file(filename: str) -> FileResponse:
    debug_root = (DATA_DIR / "debug").resolve()
    candidate = (debug_root / filename).resolve()
    try:
        candidate.relative_to(debug_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法截图路径") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="截图不存在")
    return FileResponse(candidate)


@app.put("/api/emails", dependencies=[Depends(require_auth)])
def set_emails(req: TextReq) -> dict[str, Any]:
    return {"items": store.replace_emails(req.text)}


@app.post("/api/emails/reset", dependencies=[Depends(require_auth)])
def reset_emails() -> dict[str, bool]:
    store.reset_email_marks()
    return {"ok": True}


@app.put("/api/postcodes", dependencies=[Depends(require_auth)])
def set_postcodes(req: TextReq) -> dict[str, Any]:
    return {"items": store.replace_postcodes(req.text)}


@app.post("/api/profile-cards", dependencies=[Depends(require_auth)])
def create_profile_card() -> dict[str, Any]:
    state = store.snapshot()
    if not state["postcodes"]:
        raise HTTPException(status_code=400, detail="没有可用邮编，请先录入邮编")
    profile = FormRunner._generate_profile()
    cards = state.get("profile_cards", [])
    card = {
        "id": uuid.uuid4().hex,
        "name": f"资料卡 {len(cards) + 1}",
        "email": "",
        "dob": f"{profile['dob_day']}{profile['dob_month']}{profile['dob_year']}",
        "first_name": profile["first_name"],
        "last_name": profile["last_name"],
        "address": secrets.choice(state["postcodes"]),
        "password": profile["password"],
        "pin": profile["pin"],
        "memorable_word": profile["memorable_word"],
        "phone": "",
    }
    return {"item": store.add_profile_card(card)}


@app.put("/api/profile-cards/{card_id}", dependencies=[Depends(require_auth)])
def rename_profile_card(card_id: str, req: ProfileCardNameReq) -> dict[str, Any]:
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="卡片名称不能为空")
    if not store.update_profile_card(card_id, name=name):
        raise HTTPException(status_code=404, detail="资料卡不存在")
    card = next(
        card for card in store.snapshot()["profile_cards"] if card.get("id") == card_id
    )
    return {"item": card}


@app.delete("/api/profile-cards/{card_id}", dependencies=[Depends(require_auth)])
def delete_profile_card(card_id: str) -> dict[str, bool]:
    if not store.delete_profile_card(card_id):
        raise HTTPException(status_code=404, detail="资料卡不存在")
    return {"ok": True}


@app.put("/api/settings", dependencies=[Depends(require_auth)])
def set_settings(req: SettingsReq) -> dict[str, Any]:
    values = validate_settings(req.values)
    return {"settings": store.update_settings(values)}


@app.delete("/api/task-records", dependencies=[Depends(require_auth)])
def clear_task_records() -> dict[str, bool]:
    store.clear_task_records()
    return {"ok": True}


@app.delete("/api/task-records/{record_id}", dependencies=[Depends(require_auth)])
def delete_task_record(record_id: str) -> dict[str, bool]:
    if not store.delete_task_record(record_id):
        raise HTTPException(status_code=404, detail="任务记录不存在")
    return {"ok": True}


@app.post("/api/run/start", dependencies=[Depends(require_auth)])
async def start_run(req: RunStartReq | None = None) -> dict[str, Any]:
    try:
        if req and req.values:
            store.update_settings(validate_settings(req.values))
        return {"runtime": runner.start()}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/run/stop", dependencies=[Depends(require_auth)])
def stop_run() -> dict[str, Any]:
    return {"runtime": runner.stop()}
