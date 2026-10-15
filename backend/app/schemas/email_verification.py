from __future__ import annotations

from pydantic import BaseModel, EmailStr


class RequestEmailVerificationRequest(BaseModel):
    email: EmailStr


class VerifyEmailRequest(BaseModel):
    token: str
