from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from aion.modules.knowledge.service import ArticleInput, ArticleUpdate
from aion.modules.planning.schemas import (
    ActivityInput,
    ActivityUpdate,
    Input,
    ModeInput,
    ResultInput,
    RulesInput,
    TaskInput,
    TaskUpdate,
)


class CreateTask(Input):
    operation: Literal["create_task"]
    data: TaskInput


class UpdateTask(Input):
    operation: Literal["update_task"]
    id: UUID
    data: TaskUpdate


class CreateActivity(Input):
    operation: Literal["create_activity"]
    data: ActivityInput


class UpdateActivity(Input):
    operation: Literal["update_activity"]
    id: UUID
    data: ActivityUpdate


class RecordResult(Input):
    operation: Literal["record_result"]
    id: UUID
    data: ResultInput


class CreateArticle(Input):
    operation: Literal["create_article"]
    data: ArticleInput


class UpdateArticle(Input):
    operation: Literal["update_article"]
    id: UUID
    data: ArticleUpdate


class SetMode(Input):
    operation: Literal["set_mode"]
    data: ModeInput


class SetRules(Input):
    operation: Literal["set_rules"]
    data: RulesInput


class MemoryInput(Input):
    content: str = Field(min_length=1, max_length=2000)


class MemoryUpdate(MemoryInput):
    version: int = Field(ge=1)


class Remember(Input):
    operation: Literal["remember"]
    data: MemoryInput


class CorrectMemory(Input):
    operation: Literal["correct_memory"]
    id: UUID
    data: MemoryUpdate


class Forget(Input):
    operation: Literal["forget"]
    id: UUID
    version: int = Field(ge=1)


class Search(Input):
    operation: Literal["search"]
    query: str = Field(min_length=1, max_length=500)


Operation = Annotated[
    CreateTask
    | UpdateTask
    | CreateActivity
    | UpdateActivity
    | RecordResult
    | CreateArticle
    | UpdateArticle
    | SetMode
    | SetRules
    | Remember
    | CorrectMemory
    | Forget
    | Search,
    Field(discriminator="operation"),
]


class Answer(Input):
    reply: str = Field(min_length=1, max_length=12000)
    clarification: bool = False
    operations: list[Operation] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def no_uncertain_writes(self) -> Self:
        if self.clarification and self.operations:
            raise ValueError("Clarification cannot mutate data")
        return self


class MessageInput(Input):
    content: str = Field(min_length=1, max_length=12000)
    request_key: UUID
