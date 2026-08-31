from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    root: str = Field(..., description="Workspace root directory")
    files: List[str] = Field(..., description="Files or file patterns to provide")
    task: str = Field(..., description="Task description for the agent")


class ResumeRequest(BaseModel):
    decision: bool = Field(..., description="True to approve, False to reject")


class RunResponse(BaseModel):
    thread_id: str
    status: str
    matched_files: List[str] = []
    pending_interrupt: Optional[Dict[str, Any]] = None
    summary: Optional[str] = None
    changed_files: List[str] = []
    error: Optional[str] = None


class StatusResponse(BaseModel):
    status: str
    logs: List[str] = []
    # Index into the full log list where `logs` begins. The client should
    # pass this back as `since` on the next poll to fetch only new lines.
    log_offset: int = 0
    pending_interrupt: Optional[Dict[str, Any]] = None
    summary: Optional[str] = None
    changed_files: List[str] = []
    error: Optional[str] = None
    token_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


class User(BaseModel):
    id: str
    name: str
    uname: str
    upass: str
    folder: str


class RegisterRequest(BaseModel):
    name: str = Field(..., description="Display name")
    uname: str = Field(..., description="Username")
    upass: str = Field(..., description="Password")
    folder: str = Field(..., description="Workspace folder")


class LoginRequest(BaseModel):
    uname: str = Field(..., description="Username")
    upass: str = Field(..., description="Password")


class LoginResponse(BaseModel):
    id: str
    name: str
    uname: str
    folder: str
    token: Optional[str] = None


class LogoutResponse(BaseModel):
    message: str


class LLMRequest(BaseModel):
    prompt: str = Field(..., description="Prompt to send to the model")
    system: Optional[str] = Field(None, description="Optional system message")


class LLMResponse(BaseModel):
    text: str
    token_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
