from pydantic import BaseModel, ConfigDict


class LevelBase(BaseModel):
    name: str


class LevelCreate(LevelBase):
    pass


class LevelResponse(LevelBase):
    id: int

    model_config = ConfigDict(from_attributes=True)
