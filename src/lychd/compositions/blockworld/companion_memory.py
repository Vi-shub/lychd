"""Exact reviewed command recipes; recall never grants game authority."""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from lychd.compositions.blockworld.companion import (
    CompanionAction,
    CompanionDocument,
    FollowAction,
    canonical_uuid,
    parse_text,
)

type DialogueLanguage = Literal["sk", "cs", "en"]
MAX_RECIPES = 64
MAX_PHRASE_CHARS = 200
MAX_PHRASE_BYTES = 800
_FOLLOW_TARGET = "00000000-0000-4000-8000-000000000000"


def phrase_key(text: str) -> str:
    """Conservative lexical matching: no fuzzy search, punctuation or accent repair."""
    key = unicodedata.normalize("NFC", text.strip()).casefold()
    if (
        not key
        or len(key) > MAX_PHRASE_CHARS
        or len(key.encode()) > MAX_PHRASE_BYTES
        or key.startswith((":", "/"))
        or any(unicodedata.category(char).startswith("C") for char in key)
        or key in {"yes", "no", "áno", "ano", "nie", "ne", "stop", "zastav", "stoj", "stůj", "stuj"}
    ):
        msg = "Phrase is empty, reserved or outside the recipe bound."
        raise ValueError(msg)
    return key


def recipe_key(language: DialogueLanguage, phrase: str) -> str:
    return hashlib.sha256((language + "\0" + phrase_key(phrase)).encode()).hexdigest()


def command_action(command: str, target_id: str | None) -> CompanionAction:
    """Only exact native grammar is reusable; follow binds the current captured target."""
    action = parse_text(command, target_id=target_id)
    if isinstance(action, FollowAction) and command != "follow me":
        msg = "Recipes cannot retain a player identity."
        raise ValueError(msg)
    return action


def action_command(action: CompanionAction) -> str:
    """Freeze semantic arguments in the closed parser grammar, without player identifiers."""
    match action.kind:
        case "state" | "idle":
            return action.kind
        case "follow":
            return "follow me"
        case "weather" | "time":
            return f"{action.kind} {action.mode}"
        case "say":
            return "say " + action.text
        case "inspect" | "look" | "goto":
            return f"{action.kind} {action.x} {action.y} {action.z}"


class CommandRecipe(CompanionDocument):
    """An operator-approved association, not a consent or a saved game occurrence."""

    revision: Literal[1] = 1
    language: DialogueLanguage
    phrase: str = Field(min_length=1, max_length=200)
    command: str = Field(min_length=1, max_length=400)
    skip_confirmation: bool = False
    learned_request_id: str
    learned_run_id: str
    _ids = field_validator("learned_request_id", "learned_run_id")(canonical_uuid)

    @field_validator("revision", mode="before")
    @classmethod
    def exact_revision(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            msg = "Recipe revision requires original integer 1."
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def closed_recipe(self) -> CommandRecipe:
        if self.phrase != phrase_key(self.phrase):
            msg = "Recipe phrase must be the normalized exact key."
            raise ValueError(msg)
        action = command_action(self.command, _FOLLOW_TARGET)
        if action_command(action) != self.command:
            msg = "Recipe command must use the canonical native grammar."
            raise ValueError(msg)
        try:
            original = parse_text(self.phrase, target_id=_FOLLOW_TARGET)
        except ValueError:
            pass
        else:
            if action_command(original) != self.command:
                msg = "A recipe cannot redefine a strict command."
                raise ValueError(msg)
        return self


class RecipeStore(Protocol):
    """The store is bound by its host to one owner/server/actor namespace."""

    async def find(self, language: DialogueLanguage, phrase: str) -> CommandRecipe | None: ...
    async def remember(self, recipe: CommandRecipe) -> None: ...
    async def forget(self, language: DialogueLanguage, phrase: str) -> bool: ...


class SessionRecipeStore:
    """Explicit disposable lab substitute; restarting loses every recipe."""

    def __init__(self) -> None:
        """Allocate bounded disposable recipe values for one console."""
        self._recipes: dict[str, CommandRecipe] = {}

    async def find(self, language: DialogueLanguage, phrase: str) -> CommandRecipe | None:
        value = self._recipes.get(recipe_key(language, phrase))
        return None if value is None else CommandRecipe.model_validate(value.model_dump())

    async def remember(self, recipe: CommandRecipe) -> None:
        checked = CommandRecipe.model_validate(recipe.model_dump())
        key = recipe_key(checked.language, checked.phrase)
        previous = self._recipes.get(key)
        if previous is not None and previous != checked:
            msg = "Forget the previous recipe before changing it."
            raise ValueError(msg)
        if previous is None and len(self._recipes) >= MAX_RECIPES:
            msg = "Recipe capacity reached."
            raise ValueError(msg)
        self._recipes[key] = checked

    async def forget(self, language: DialogueLanguage, phrase: str) -> bool:
        return self._recipes.pop(recipe_key(language, phrase), None) is not None
