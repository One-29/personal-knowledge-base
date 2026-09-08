from pydantic import BaseModel, Field
from datetime import datetime
from pydantic import ConfigDict

class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=500)


class KnowledgeBaseOut(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    doc_count: int = Field(default=0)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

