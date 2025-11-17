import os
from fastapi import FastAPI, HTTPException, UploadFile, File, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List, Literal, Dict, Any
from bson import ObjectId
from datetime import datetime, timedelta, timezone

from database import db, create_document
from schemas import User, Clientprofile, Message, Notification, Document, Invoice, Workrequest, Quote, Token, Kanbantask

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure uploads dir exists
UPLOAD_DIR = os.path.join(os.getcwd(), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------------------- Realtime (WebSockets) ----------------------
class ConnectionManager:
    def __init__(self):
        # client_id -> set of websockets
        self.active: Dict[str, List[WebSocket]] = {}

    async def connect(self, client_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active.setdefault(client_id, []).append(websocket)

    def disconnect(self, client_id: str, websocket: WebSocket):
        arr = self.active.get(client_id)
        if not arr:
            return
        if websocket in arr:
            arr.remove(websocket)
        if not arr:
            self.active.pop(client_id, None)

    async def broadcast(self, client_id: str, message: Dict[str, Any]):
        conns = list(self.active.get(client_id, []))
        for ws in conns:
            try:
                await ws.send_json(message)
            except RuntimeError:
                # skip dead
                pass

manager = ConnectionManager()

# Helpers

def oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID")


def serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return doc
    doc = dict(doc)
    if "_id" in doc:
        doc["id"] = str(doc.pop("_id"))
    # Convert datetime to isoformat
    for k, v in list(doc.items()):
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


def require_pm_or_admin(request: Request):
    role = request.headers.get("X-User-Role", "")
    if role not in ("admin", "project_manager"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")


@app.get("/")
def read_root():
    return {"message": "Client Portal API Running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Connected & Working"
            response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
            response["database_name"] = db.name
            response["connection_status"] = "Connected"
            try:
                response["collections"] = db.list_collection_names()
            except Exception:
                pass
        else:
            response["database"] = "⚠️ Not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:80]}"
    return response


# ---------------------- Auth ----------------------
class LoginPayload(BaseModel):
    email: str
    name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[Literal["admin", "project_manager", "client", "viewer"]] = None
    company: Optional[str] = None


@app.post("/auth/login")
def login(payload: LoginPayload):
    """Very simple login: if user doesn't exist, create it. Returns a token.
    Use role from payload when creating; existing users keep their role.
    """
    user = db["user"].find_one({"email": payload.email})
    if not user:
        new_user = User(
            name=payload.name or payload.email.split("@")[0],
            email=payload.email,
            password_hash=payload.password or "demo",
            role=payload.role or "client",
            company=payload.company,
            is_active=True,
        )
        user_id = create_document("user", new_user)
        user = db["user"].find_one({"_id": ObjectId(user_id)})
        # Auto create client profile if role=client
        if new_user.role == "client":
            prof = Clientprofile(
                user_id=str(user_id),
                display_name=new_user.name,
                theme_color="#4f46e5",
                logo_url=None,
                notes=None,
                custom_domain=None,
            )
            create_document("clientprofile", prof)

    token_value = os.urandom(12).hex()
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    db["token"].insert_one({
        "user_id": str(user["_id"]),
        "token": token_value,
        "expires_at": expires,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })
    return {"token": token_value, "user": serialize(user)}


class RequestOtpPayload(BaseModel):
    email: str
    name: Optional[str] = None
    role: Optional[Literal["admin", "project_manager", "client", "viewer"]] = None


@app.post("/auth/request-otp")
def request_otp(payload: RequestOtpPayload):
    # Create user if not exists
    user = db["user"].find_one({"email": payload.email})
    if not user:
        new_user = User(
            name=payload.name or payload.email.split("@")[0],
            email=payload.email,
            password_hash="otp",
            role=payload.role or "client",
            company=None,
            is_active=True,
        )
        uid = create_document("user", new_user)
        user = db["user"].find_one({"_id": ObjectId(uid)})
        if new_user.role == "client":
            prof = Clientprofile(
                user_id=str(uid),
                display_name=new_user.name,
                theme_color="#4f46e5",
                logo_url=None,
                notes=None,
                custom_domain=None,
            )
            create_document("clientprofile", prof)

    # Generate a 6-digit code valid for 10 minutes
    code = f"{int.from_bytes(os.urandom(3), 'big') % 1000000:06d}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    db["otp"].delete_many({"email": payload.email})
    db["otp"].insert_one({
        "email": payload.email,
        "code": code,
        "expires_at": expires,
        "created_at": datetime.now(timezone.utc)
    })

    # Try to email via SendGrid if configured, otherwise log
    sg_key = os.getenv("SENDGRID_API_KEY")
    from_email = os.getenv("EMAIL_FROM", "no-reply@example.com")
    if sg_key and payload.email:
        try:
            import requests
            data = {
                "personalizations": [{"to": [{"email": payload.email}], "subject": "Your verification code"}],
                "from": {"email": from_email},
                "content": [{"type": "text/plain", "value": f"Your login code is: {code}. It expires in 10 minutes."}],
            }
            requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {sg_key}", "Content-Type": "application/json"},
                json=data,
                timeout=5,
            )
        except Exception as e:
            print(f"[OTP] Email send fallback due to error: {e}")
            print(f"[OTP] Verification code for {payload.email}: {code}")
    else:
        print(f"[OTP] Verification code for {payload.email}: {code}")

    return {"status": "sent"}


class VerifyOtpPayload(BaseModel):
    email: str
    code: str


@app.post("/auth/verify-otp")
def verify_otp(payload: VerifyOtpPayload):
    rec = db["otp"].find_one({"email": payload.email, "code": payload.code})
    if not rec or (rec.get("expires_at") and rec["expires_at"] < datetime.now(timezone.utc)):
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    user = db["user"].find_one({"email": payload.email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Issue token
    token_value = os.urandom(12).hex()
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    db["token"].insert_one({
        "user_id": str(user["_id"]),
        "token": token_value,
        "expires_at": expires,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    })
    # Cleanup used code
    db["otp"].delete_many({"email": payload.email})
    return {"token": token_value, "user": serialize(user)}


@app.get("/auth/me")
def me(token: str):
    t = db["token"].find_one({"token": token})
    if not t or (t.get("expires_at") and t["expires_at"] < datetime.now(timezone.utc)):
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db["user"].find_one({"_id": ObjectId(t["user_id"])})
    return serialize(user)


# ---------------------- Tenant Resolution ----------------------
@app.get("/tenant/resolve")
def resolve_tenant(host: Optional[str] = None):
    host = host or os.getenv("HOSTNAME") or ""
    prof = db["clientprofile"].find_one({"custom_domain": host})
    return serialize(prof) if prof else {}


# ---------------------- Clients ----------------------
class CreateClientPayload(BaseModel):
    email: str
    name: str
    company: Optional[str] = None
    theme_color: Optional[str] = "#4f46e5"
    logo_url: Optional[str] = None


class UpdateClientPayload(BaseModel):
    display_name: Optional[str] = None
    theme_color: Optional[str] = None
    logo_url: Optional[str] = None
    notes: Optional[str] = None
    custom_domain: Optional[str] = None


@app.get("/clients")
def list_clients() -> List[Dict[str, Any]]:
    profiles = list(db["clientprofile"].aggregate([
        {"$lookup": {"from": "user", "localField": "user_id", "foreignField": "_id", "as": "user_obj"}},
    ]))
    return [serialize(p) for p in profiles]


@app.post("/clients")
def create_client(payload: CreateClientPayload):
    # Create user with client role
    u = User(
        name=payload.name,
        email=payload.email,
        password_hash="demo",
        role="client",
        company=payload.company,
        is_active=True,
    )
    user_id = create_document("user", u)
    prof = Clientprofile(
        user_id=str(user_id),
        display_name=payload.name,
        theme_color=payload.theme_color,
        logo_url=payload.logo_url,
        notes=None,
        custom_domain=None,
    )
    prof_id = create_document("clientprofile", prof)
    return {"user_id": user_id, "profile_id": prof_id}


@app.get("/clients/{client_id}")
def get_client(client_id: str):
    prof = db["clientprofile"].find_one({"_id": oid(client_id)})
    if not prof:
        raise HTTPException(status_code=404, detail="Client not found")
    return serialize(prof)


@app.patch("/clients/{client_id}")
def update_client(request: Request, client_id: str, payload: UpdateClientPayload):
    # Restrict branding updates to PM/Admin
    require_pm_or_admin(request)
    update = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update:
        return get_client(client_id)
    update["updated_at"] = datetime.now(timezone.utc)
    res = db["clientprofile"].update_one({"_id": oid(client_id)}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Client not found")
    prof = db["clientprofile"].find_one({"_id": oid(client_id)})
    # Notify branding change
    try:
        import asyncio
        asyncio.create_task(manager.broadcast(client_id, {"type": "brand:update"}))
    except Exception:
        pass
    return serialize(prof)


# Logo upload endpoints
@app.post("/clients/{client_id}/logo")
async def upload_logo(request: Request, client_id: str, file: UploadFile = File(...)):
    # Restrict to PM/Admin
    require_pm_or_admin(request)
    # Save file to uploads directory
    ext = os.path.splitext(file.filename)[1].lower()
    safe_name = f"{client_id}_logo{ext or '.png'}"
    dest = os.path.join(UPLOAD_DIR, safe_name)
    with open(dest, "wb") as f:
        content = await file.read()
        f.write(content)
    # Construct a simple local URL path
    url_path = f"/uploads/{safe_name}"
    # Persist on client profile
    db["clientprofile"].update_one({"_id": oid(client_id)}, {"$set": {"logo_url": url_path, "updated_at": datetime.now(timezone.utc)}})
    # Broadcast change
    try:
        import asyncio
        asyncio.create_task(manager.broadcast(client_id, {"type": "brand:logo", "url": url_path}))
    except Exception:
        pass
    return {"url": url_path}

@app.get("/uploads/{filename}")
def get_uploaded_file(filename: str):
    path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


# Seed three dummy clients if missing

def ensure_dummy_clients():
    samples = [
        {
            "email": "acme.facilities@example.com",
            "name": "Acme Facilities",
            "company": "Acme Facilities Ltd",
            "theme_color": "#F5C518",  # safety yellow
            "logo_url": "https://dummyimage.com/120x120/F5C518/0b1026&text=AF",
        },
        {
            "email": "nottingham.builders@example.com",
            "name": "Nottingham Builders",
            "company": "Nottingham Builders Co",
            "theme_color": "#0ea5e9",  # sky
            "logo_url": "https://dummyimage.com/120x120/0ea5e9/0b1026&text=NB",
        },
        {
            "email": "trent.cafes@example.com",
            "name": "Trent Cafe Group",
            "company": "Trent Cafe Group",
            "theme_color": "#22c55e",  # green
            "logo_url": "https://dummyimage.com/120x120/22c55e/0b1026&text=TC",
        },
    ]
    for s in samples:
        # If user exists, skip
        existing_user = db["user"].find_one({"email": s["email"]})
        if existing_user:
            continue
        # Create user
        u = User(
            name=s["name"],
            email=s["email"],
            password_hash="demo",
            role="client",
            company=s.get("company"),
            is_active=True,
        )
        user_id = create_document("user", u)
        # Create client profile
        prof = Clientprofile(
            user_id=str(user_id),
            display_name=s["name"],
            theme_color=s.get("theme_color") or "#4f46e5",
            logo_url=s.get("logo_url"),
            notes="Dummy seeded client",
            custom_domain=None,
        )
        create_document("clientprofile", prof)


@app.on_event("startup")
async def startup_seed():
    try:
        # Only seed if there are fewer than 3 client profiles
        count = db["clientprofile"].count_documents({})
        if count < 3:
            ensure_dummy_clients()
    except Exception:
        # If DB not available, ignore
        pass


# ---------------------- Messages (Chat) ----------------------
class MessagePayload(BaseModel):
    client_id: str
    sender_id: str
    sender_role: Literal["admin", "project_manager", "client", "viewer"]
    content: str


@app.get("/messages")
def get_messages(client_id: str, limit: int = 100):
    msgs = db["message"].find({"client_id": client_id}).sort("created_at", 1).limit(limit)
    return [serialize(m) for m in msgs]


@app.post("/messages")
def post_message(payload: MessagePayload):
    m = Message(**payload.model_dump())
    mid = create_document("message", m)
    return {"id": mid}


# ---------------------- Notifications ----------------------
class NotificationPayload(BaseModel):
    user_id: str
    text: str


@app.get("/notifications")
def get_notifications(user_id: str):
    notes = db["notification"].find({"user_id": user_id}).sort("created_at", -1)
    return [serialize(n) for n in notes]


@app.post("/notifications")
def create_notification(payload: NotificationPayload):
    n = Notification(**payload.model_dump())
    nid = create_document("notification", n)
    return {"id": nid}


@app.post("/notifications/read")
def mark_notifications_read(user_id: str):
    db["notification"].update_many({"user_id": user_id}, {"$set": {"read": True, "updated_at": datetime.now(timezone.utc)}})
    return {"status": "ok"}


# ---------------------- Documents & Invoices ----------------------
class DocumentPayload(BaseModel):
    client_id: str
    filename: str
    url: str
    kind: Literal["document", "invoice"] = "document"
    uploaded_by: str


@app.get("/documents")
def get_documents_api(client_id: str):
    docs = db["document"].find({"client_id": client_id}).sort("created_at", -1)
    return [serialize(d) for d in docs]


@app.post("/documents")
def create_document_api(payload: DocumentPayload):
    d = Document(**payload.model_dump())
    did = create_document("document", d)
    return {"id": did}


class InvoicePayload(BaseModel):
    client_id: str
    number: str
    amount: float
    status: Optional[Literal["draft", "sent", "paid", "overdue"]] = "sent"
    url: Optional[str] = None


@app.get("/invoices")
def get_invoices(client_id: str):
    invs = db["invoice"].find({"client_id": client_id}).sort("created_at", -1)
    return [serialize(i) for i in invs]


@app.post("/invoices")
def create_invoice(payload: InvoicePayload):
    inv = Invoice(**payload.model_dump())
    iid = create_document("invoice", inv)
    return {"id": iid}


# ---------------------- Work Requests & Quotes ----------------------
class WorkRequestPayload(BaseModel):
    client_id: str
    title: str
    description: str


@app.get("/work-requests")
def list_work_requests(client_id: str):
    wrs = db["workrequest"].find({"client_id": client_id}).sort("created_at", -1)
    return [serialize(w) for w in wrs]


@app.post("/work-requests")
def create_work_request(payload: WorkRequestPayload):
    wr = Workrequest(client_id=payload.client_id, title=payload.title, description=payload.description)
    wid = create_document("workrequest", wr)
    return {"id": wid}


class QuotePayload(BaseModel):
    client_id: str
    work_request_id: str
    amount: float


@app.get("/quotes")
def list_quotes(client_id: str):
    qs = db["quote"].find({"client_id": client_id}).sort("created_at", -1)
    return [serialize(q) for q in qs]


@app.post("/quotes")
def create_quote(payload: QuotePayload):
    q = Quote(client_id=payload.client_id, work_request_id=payload.work_request_id, amount=payload.amount)
    qid = create_document("quote", q)
    return {"id": qid}


@app.post("/quotes/{quote_id}/authorize")
def authorize_quote(quote_id: str, authorize: bool = True):
    status = "authorized" if authorize else "rejected"
    res = db["quote"].update_one({"_id": oid(quote_id)}, {"$set": {"status": status, "updated_at": datetime.now(timezone.utc)}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Quote not found")
    return {"status": status}


# ---------------------- Kanban Board ----------------------
class KanbanCreatePayload(BaseModel):
    client_id: str
    title: str
    description: Optional[str] = ""
    due_date: Optional[datetime] = None
    assignees: Optional[List[str]] = []


class KanbanUpdatePayload(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[Literal["todo", "in_progress", "under_review", "completed"]] = None
    due_date: Optional[datetime] = None
    assignees: Optional[List[str]] = None
    position: Optional[float] = None


@app.get("/kanban/tasks")
def kanban_list(client_id: str, status: Optional[str] = None, search: Optional[str] = None, assignee: Optional[str] = None):
    q: Dict[str, Any] = {"client_id": client_id}
    if status:
        q["status"] = status
    if assignee:
        q["assignees"] = {"$in": [assignee]}
    if search:
        q["$or"] = [
            {"title": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}},
        ]
    tasks = db["kanbantask"].find(q).sort([("status", 1), ("position", 1), ("created_at", 1)])
    return [serialize(t) for t in tasks]


@app.post("/kanban/tasks")
def kanban_create(request: Request, payload: KanbanCreatePayload):
    require_pm_or_admin(request)
    # Compute next position in "todo" by default
    column = "todo"
    last = db["kanbantask"].find({"client_id": payload.client_id, "status": column}).sort("position", -1).limit(1)
    next_pos = 1.0
    if last:
        last_list = list(last)
        if last_list:
            next_pos = float(last_list[0].get("position", 0)) + 1.0
    task = Kanbantask(
        client_id=payload.client_id,
        title=payload.title,
        description=payload.description or "",
        status="todo",
        due_date=payload.due_date,
        assignees=payload.assignees or [],
        position=next_pos,
    )
    tid = create_document("kanbantask", task)
    # Notify
    import asyncio
    asyncio.create_task(manager.broadcast(payload.client_id, {"type": "kanban:create", "id": tid}))
    return {"id": tid}


@app.patch("/kanban/tasks/{task_id}")
def kanban_update(request: Request, task_id: str, payload: KanbanUpdatePayload):
    require_pm_or_admin(request)
    update = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not update:
        t = db["kanbantask"].find_one({"_id": oid(task_id)})
        if not t:
            raise HTTPException(status_code=404, detail="Task not found")
        return serialize(t)
    update["updated_at"] = datetime.now(timezone.utc)
    res = db["kanbantask"].update_one({"_id": oid(task_id)}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    t = db["kanbantask"].find_one({"_id": oid(task_id)})
    # Notify
    import asyncio
    asyncio.create_task(manager.broadcast(t["client_id"], {"type": "kanban:update", "id": str(t["_id"]) }))
    return serialize(t)


class KanbanMovePayload(BaseModel):
    to_status: Literal["todo", "in_progress", "under_review", "completed"]
    before_id: Optional[str] = None
    after_id: Optional[str] = None


@app.post("/kanban/tasks/{task_id}/move")
def kanban_move(request: Request, task_id: str, payload: KanbanMovePayload):
    require_pm_or_admin(request)
    # Determine new position based on neighbors
    to_col = payload.to_status
    pos: float
    if payload.before_id and payload.after_id:
        before = db["kanbantask"].find_one({"_id": oid(payload.before_id)})
        after = db["kanbantask"].find_one({"_id": oid(payload.after_id)})
        if not (before and after):
            raise HTTPException(status_code=400, detail="Invalid neighbor ids")
        pos = (float(before.get("position", 0)) + float(after.get("position", 0))) / 2.0
    elif payload.before_id:
        before = db["kanbantask"].find_one({"_id": oid(payload.before_id)})
        pos = float(before.get("position", 0)) - 1.0
    elif payload.after_id:
        after = db["kanbantask"].find_one({"_id": oid(payload.after_id)})
        pos = float(after.get("position", 0)) + 1.0
    else:
        # Append to end of column
        last = db["kanbantask"].find({"status": to_col}).sort("position", -1).limit(1)
        pos = 1.0
        last_list = list(last)
        if last_list:
            pos = float(last_list[0].get("position", 0)) + 1.0
    res = db["kanbantask"].update_one({"_id": oid(task_id)}, {"$set": {"status": to_col, "position": pos, "updated_at": datetime.now(timezone.utc)}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    t = db["kanbantask"].find_one({"_id": oid(task_id)})
    # Notify
    import asyncio
    asyncio.create_task(manager.broadcast(t["client_id"], {"type": "kanban:move", "id": str(t["_id"]) }))
    return serialize(t)


# WebSocket endpoint for realtime kanban updates
@app.websocket("/ws/kanban/{client_id}")
async def ws_kanban(websocket: WebSocket, client_id: str):
    await manager.connect(client_id, websocket)
    try:
        while True:
            # Keep alive / optional client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(client_id, websocket)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
