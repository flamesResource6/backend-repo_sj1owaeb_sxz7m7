import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Literal, Dict, Any
from bson import ObjectId
from datetime import datetime, timedelta, timezone

from database import db, create_document, get_documents
from schemas import User, Clientprofile, Message, Notification, Document, Invoice, Workrequest, Quote, Token

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    role: Optional[Literal["admin", "client"]] = None
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


@app.get("/auth/me")
def me(token: str):
    t = db["token"].find_one({"token": token})
    if not t or (t.get("expires_at") and t["expires_at"] < datetime.now(timezone.utc)):
        raise HTTPException(status_code=401, detail="Invalid token")
    user = db["user"].find_one({"_id": ObjectId(t["user_id"])})
    return serialize(user)


# ---------------------- Clients ----------------------
class CreateClientPayload(BaseModel):
    email: str
    name: str
    company: Optional[str] = None
    theme_color: Optional[str] = "#4f46e5"
    logo_url: Optional[str] = None


@app.get("/clients")
def list_clients() -> List[Dict[str, Any]]:
    profiles = list(db["clientprofile"].aggregate([
        {"$lookup": {"from": "user", "localField": "user_id", "foreignField": "_id", "as": "user_obj"}},
    ]))
    # user_id in profiles is a string; ensure comparison when joining - keep as is
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
    )
    prof_id = create_document("clientprofile", prof)
    return {"user_id": user_id, "profile_id": prof_id}


@app.get("/clients/{client_id}")
def get_client(client_id: str):
    prof = db["clientprofile"].find_one({"_id": oid(client_id)})
    if not prof:
        raise HTTPException(status_code=404, detail="Client not found")
    return serialize(prof)


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
    sender_role: Literal["admin", "client"]
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
