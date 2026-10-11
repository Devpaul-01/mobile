from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, EmailStr, StringConstraints


class RequestPasswordResetRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: Annotated[str, StringConstraints(min_length=8, max_length=128)]
