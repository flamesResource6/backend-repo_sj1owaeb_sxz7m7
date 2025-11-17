"""
Database Schemas

Define your MongoDB collection schemas here using Pydantic models.
These schemas are used for data validation in your application.

Each Pydantic model represents a collection in your database.
Model name is converted to lowercase for the collection name:
- User -> "user" collection
- Product -> "product" collection
- BlogPost -> "blogs" collection
"""

from pydantic import BaseModel, Field
from typing import Optional, Literal, List
from datetime import datetime

# Core user and client profile
class User(BaseModel):
    name: str = Field(..., description="Full name")
    email: str = Field(..., description="Email address")
    password_hash: str = Field(..., description="SHA256 password hash")
    role: Literal["admin", "project_manager", "client", "viewer"] = Field("client", description="User role")
    company: Optional[str] = Field(None, description="Company name (for clients)")
    is_active: bool = Field(True, description="Whether user is active")
    email_verified: bool = Field(False, description="Has verified email")
    verification_token: Optional[str] = None
    reset_token: Optional[str] = None


class Clientprofile(BaseModel):
    user_id: str = Field(..., description="Linked user id")
    display_name: str = Field(..., description="Client display name")
    theme_color: Optional[str] = Field("#4f46e5", description="Brand color hex")
    logo_url: Optional[str] = Field(None, description="URL to logo image")
    notes: Optional[str] = Field(None, description="Internal notes")
    custom_domain: Optional[str] = Field(None, description="Custom domain for white-label mapping")

# Communication
class Message(BaseModel):
    client_id: str = Field(..., description="Client profile id this chat belongs to")
    sender_id: str = Field(..., description="User id of sender")
    sender_role: Literal["admin", "project_manager", "client", "viewer"] = Field(..., description="Role of sender")
    content: str = Field(..., description="Message content")
    created_at: Optional[datetime] = None

class Notification(BaseModel):
    user_id: str = Field(..., description="User id to notify")
    text: str = Field(..., description="Notification text")
    read: bool = Field(False, description="Read status")
    created_at: Optional[datetime] = None

# Files & billing
class Document(BaseModel):
    client_id: str = Field(..., description="Client profile id")
    filename: str = Field(..., description="Original filename")
    url: str = Field(..., description="Public URL to access the file")
    kind: Literal["document", "invoice"] = Field("document", description="File type")
    uploaded_by: str = Field(..., description="User id of uploader")

class Invoice(BaseModel):
    client_id: str = Field(..., description="Client profile id")
    number: str = Field(..., description="Invoice number")
    amount: float = Field(..., ge=0, description="Invoice total amount")
    status: Literal["draft", "sent", "paid", "overdue"] = Field("sent")
    url: Optional[str] = Field(None, description="Link to invoice PDF if uploaded")

# Work management
class Workrequest(BaseModel):
    client_id: str = Field(..., description="Client profile id")
    title: str = Field(..., description="Short title of the request")
    description: str = Field(..., description="Detailed description")
    status: Literal["new", "in_review", "in_progress", "completed", "rejected"] = Field("new")

class Quote(BaseModel):
    client_id: str = Field(..., description="Client profile id")
    work_request_id: str = Field(..., description="Linked work request id")
    amount: float = Field(..., ge=0, description="Quoted amount")
    status: Literal["pending", "authorized", "rejected"] = Field("pending")

# Simple auth token storage
class Token(BaseModel):
    user_id: str
    token: str
    expires_at: Optional[datetime] = None

# Kanban board
class Kanbantask(BaseModel):
    client_id: str = Field(..., description="Client profile id")
    title: str = Field(..., description="Task title")
    description: Optional[str] = Field("", description="Task details")
    status: Literal["todo", "in_progress", "under_review", "completed"] = Field("todo")
    due_date: Optional[datetime] = Field(None, description="Deadline")
    assignees: List[str] = Field(default_factory=list, description="User ids assigned")
    position: float = Field(0, description="Ordering within column")
