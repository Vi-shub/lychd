"""Finite model-free proposal, confirmation and reviewed-recipe conversation.

Dialogue is a pre-admission convenience. It owns no game execution, durable
Consent or inferred authority; every returned request still enters native admission.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from typing import Literal

from lychd.compositions.blockworld.companion import (
    MAX_INSTRUCTION_CHARS,
    CompanionAction,
    CompanionRequest,
    CompanionResult,
    FollowAction,
    parse_text,
)
from lychd.compositions.blockworld.companion_memory import (
    CommandRecipe,
    DialogueLanguage,
    RecipeStore,
    action_command,
    command_action,
    phrase_key,
)
from lychd.domain.artifacts import ArtifactRef

type ConfirmationMode = Literal["always", "unremembered", "never"]
_YES = {"yes", "áno", "ano"}
_NO = {"no", "nie", "ne", "cancel", "zruš", "zrus", "zrušit", "zrušiť"}
_SKIP = {"yes without confirmation", "áno bez potvrdenia", "ano bez potvrdenia", "ano bez potvrzení"}
_MENU = {"options", "commands", "help", "možnosti", "moznosti", "príkazy", "prikazy", "pomoc", "příkazy"}
_WEATHER = {
    "weather options",
    "možnosti počasia",
    "moznosti pocasia",
    "možnosti počasí",
    "zmeň počasie",
    "zmen pocasie",
    "prepni počasie",
    "prepni pocasie",
    "počasie",
    "pocasie",
    "počasí",
    "pocasi",
}
_TIME = {"time options", "možnosti času", "moznosti casu", "zmeň čas", "zmen cas"}
_UNSUPPORTED = {"structure", "structures", "štruktúru", "štruktúra", "strukturu", "struktura", "summon"}
_REVIEW_MS = 60_000
_NUMBERS = {
    "jedna": 1,
    "jeden": 1,
    "prvú": 1,
    "prvu": 1,
    "první": 1,
    "prvni": 1,
    "one": 1,
    "first": 1,
    "dva": 2,
    "druhú": 2,
    "druhu": 2,
    "druhou": 2,
    "two": 2,
    "second": 2,
    "tri": 3,
    "tři": 3,
    "tretiu": 3,
    "three": 3,
    "third": 3,
}


def _words(text: str) -> str:
    return unicodedata.normalize("NFC", text.strip()).casefold().rstrip(".!?")


def _text(language: DialogueLanguage, sk: str, cs: str, en: str) -> str:
    return (sk, cs, en)[("sk", "cs", "en").index(language)]


@dataclass(frozen=True)
class DialogueContext:
    request_id: str
    generation: int
    target_id: str | None
    expires_at_ms: int
    source: Literal["text", "transcript"] = "text"
    source_artifacts: tuple[ArtifactRef, ...] = ()
    question_revision: int = 0


@dataclass(frozen=True)
class DialogueDecision:
    phase: str
    text: str
    request: CompanionRequest | None = None
    speech_text: str | None = None


@dataclass(frozen=True)
class _Pending:
    context: DialogueContext
    phrase: str
    stage: Literal["choose", "confirm", "executing", "review", "remember"]
    language: DialogueLanguage
    options: tuple[str, ...] = ()
    command: str | None = None
    action: CompanionAction | None = None
    request: CompanionRequest | None = None
    run_id: str | None = None
    deadline_ms: int = 0
    learned: bool = False


class CompanionDialogue:
    """One finite conversation; the host serializes receive/complete and storage access."""

    def __init__(
        self,
        store: RecipeStore,
        *,
        confirmation: ConfirmationMode = "unremembered",
        allow_weather: bool = False,
        allow_time: bool = False,
        response_language: DialogueLanguage | None = None,
    ) -> None:
        """Bind explicit recipe storage and conversational preference, without game authority."""
        if confirmation not in {"always", "unremembered", "never"}:
            msg = "Unknown confirmation policy."
            raise ValueError(msg)
        self.store, self.confirmation = store, confirmation
        self.allow_weather, self.allow_time = allow_weather, allow_time
        self.response_language: DialogueLanguage | None = response_language
        self._pending: _Pending | None = None
        self.revision = 0

    def cancel(self) -> None:
        """Forget proposals synchronously at stop, target change or shutdown."""
        self._pending = None
        self.revision += 1

    def _question(self, pending: _Pending) -> None:
        self._pending = pending
        self.revision += 1

    def _options(self, group: str, target: str | None) -> tuple[str, ...]:
        if group == "weather":
            return ("weather clear", "weather rain") if self.allow_weather else ()
        if group == "time":
            return ("time day", "time night", "time toggle") if self.allow_time else ()
        return (
            "state",
            "idle",
            *(("follow me",) if target is not None else ()),
            *(self._options("weather", target)),
            *(self._options("time", target)),
        )

    def _message(self, language: DialogueLanguage, phase: str, sk: str, cs: str, en: str) -> DialogueDecision:
        return DialogueDecision(phase, _text(self.response_language or language, sk, cs, en))

    def _choose(self, text: str, context: DialogueContext, language: DialogueLanguage, group: str) -> DialogueDecision:
        options = self._options(group, context.target_id)
        self._question(_Pending(context, text, "choose", language, options=options, deadline_ms=context.expires_at_ms))
        listing = "; ".join(f"{number}: {command}" for number, command in enumerate(options, 1))
        message = self._message(
            language,
            "choose",
            f"Mám {len(options)} možností: {listing}. Vyber číslo alebo presný príkaz. Odporúčam začať prvou možnosťou. Server ešte overí oprávnenia.",
            f"Mám {len(options)} možností: {listing}. Vyber číslo nebo přesný příkaz. Doporučuji začít první možností. Server ještě ověří oprávnění.",
            f"I have {len(options)} options: {listing}. Choose a number or exact command. Start with the first option. Server permissions still need checking.",
        )
        first = options[0] if options else "—"
        spoken = _text(
            self.response_language or language,
            f"Mám {len(options)} možností. Prvá je {first}. Vyber číslo alebo presný príkaz. Zoznam je v konzole.",
            f"Mám {len(options)} možností. První je {first}. Vyber číslo nebo přesný příkaz. Seznam je v konzoli.",
            f"I have {len(options)} options. First is {first}. Choose a number or exact command. The console has the list.",
        )
        return replace(message, speech_text=spoken)

    async def receive(
        self, text: str, context: DialogueContext, language: DialogueLanguage, *, now_ms: int
    ) -> DialogueDecision:
        """Resolve only an explicit reply to the current finite proposal; never run a tool."""
        if context.expires_at_ms <= now_ms:
            msg = "Dialogue input expired."
            raise ValueError(msg)
        if context.question_revision != self.revision:
            return self._message(
                language,
                "stale_question",
                "Otázka sa medzičasom zmenila. Odpovedz znova na aktuálnu otázku.",
                "Otázka se mezitím změnila. Odpověz znovu na aktuální otázku.",
                "The question changed. Answer the current question again.",
            )
        if (
            not text.strip()
            or len(text) > MAX_INSTRUCTION_CHARS
            or any(unicodedata.category(char).startswith("C") for char in text)
        ):
            msg = "Dialogue input is empty, too large or contains controls."
            raise ValueError(msg)
        word = _words(text)
        pending = self._pending
        if pending is not None and (
            pending.deadline_ms <= now_ms
            or pending.context.generation != context.generation
            or pending.context.target_id != context.target_id
            or pending.language != language
        ):
            self.cancel()
            pending = None
        if word in _NO:
            self.cancel()
            return self._message(
                language, "cancelled", "Zrušené. Nič neukladám.", "Zrušeno. Nic neukládám.", "Cancelled. Nothing saved."
            )
        if pending is not None:
            answer = await self._answer(text, pending, now_ms)
            if answer is not None:
                return answer
            self.cancel()
        if word in _YES | _SKIP:
            return self._message(
                language,
                "no_proposal",
                "Nemám platnú otázku na potvrdenie.",
                "Nemám platnou otázku k potvrzení.",
                "There is no current question to confirm.",
            )
        return await self._start(text, context, language)

    async def _start(self, text: str, context: DialogueContext, language: DialogueLanguage) -> DialogueDecision:
        word = _words(text)
        if any(token.strip("/,.!?:") in _UNSUPPORTED for token in word.split()):
            return self._message(
                language,
                "unavailable",
                "Štruktúry a summon zatiaľ nemajú podporovanú hernú akciu. Nič nevykonám ani nezapamätám.",
                "Struktury a summon zatím nemají podporovanou herní akci. Nic neprovedu ani nezapamatuji.",
                "Structures and summon do not yet have a supported game action. Nothing executed or remembered.",
            )
        if word in _MENU | _WEATHER | _TIME:
            group = "weather" if word in _WEATHER else "time" if word in _TIME else "all"
            return self._choose(text, context, language, group)
        try:
            key = phrase_key(text)
        except ValueError:
            key = None
        cached = None if key is None else await self.store.find(language, key)
        if cached is not None:
            cached = CommandRecipe.model_validate(cached.model_dump())
            if cached.language != language or cached.phrase != key:
                msg = "Recipe lookup returned a different binding."
                raise ValueError(msg)
        try:
            action = parse_text(text.strip(), target_id=context.target_id)
        except ValueError:
            if cached is None:
                return self._choose(text, context, language, "all")
            action = command_action(cached.command, context.target_id)
        command = action_command(action)
        if (
            isinstance(action, FollowAction)
            and word not in {"follow me", "nasleduj ma", "následuj mě", "nasleduj me"}
            and cached is None
        ):
            command = "follow " + action.target_id
        if cached is not None and cached.command != command:
            msg = "Recipe conflicts with strict command meaning."
            raise ValueError(msg)
        pending = _Pending(
            context,
            text,
            "confirm",
            language,
            command=command,
            action=action,
            deadline_ms=context.expires_at_ms,
            learned=cached is not None,
        )
        bypass = self.confirmation == "never" or (
            self.confirmation == "unremembered" and cached is not None and cached.skip_confirmation
        )
        return self._propose(pending, bypass=bypass)

    def _propose(self, pending: _Pending, *, bypass: bool = False) -> DialogueDecision:
        if pending.command is None:
            msg = "Missing concrete proposal command."
            raise ValueError(msg)
        action = pending.action or command_action(pending.command, pending.context.target_id)
        if (action.kind == "weather" and not self.allow_weather) or (action.kind == "time" and not self.allow_time):
            self.cancel()
            return self._message(
                pending.language,
                "disabled",
                "Táto akcia nie je povolená v nastavení relácie.",
                "Tato akce není povolena v nastavení relace.",
                "This action is disabled in the session policy.",
            )
        if bypass or action.kind in {"state", "inspect"}:
            context = pending.context
            request = CompanionRequest(
                request_id=context.request_id,
                action=action,
                transcript=pending.phrase,
                source=context.source,
                generation=context.generation,
                expires_at_ms=context.expires_at_ms,
                source_artifacts=context.source_artifacts,
            )
            self._question(replace(pending, stage="executing", request=request))
            return DialogueDecision("execute", "", request)
        self._question(replace(pending, stage="confirm"))
        suffix = ""
        if action.kind == "weather":
            suffix = _text(
                self.response_language or pending.language,
                " Spoločné počasie na 600 herných tickov.",
                " Společné počasí na 600 herních ticků.",
                " Shared server weather for 600 engine ticks.",
            )
        elif action.kind == "time":
            suffix = _text(
                self.response_language or pending.language,
                " Posun času dopredu; toggle prepína podľa času pri vykonaní.",
                " Posun času dopředu; toggle přepíná podle času při provedení.",
                " Advances time; toggle samples the clock at execution.",
            )
        return self._message(
            pending.language,
            "confirm",
            f"Pochopil som správne, že chceš {pending.command}?{suffix} Povedz áno alebo nie.",
            f"Pochopil jsem správně, že chceš {pending.command}?{suffix} Řekni ano nebo ne.",
            f"Did I understand correctly: {pending.command}?{suffix} Say yes or no.",
        )

    async def _answer(self, text: str, pending: _Pending, now_ms: int) -> DialogueDecision | None:
        word, language = _words(text), pending.language
        if pending.stage == "executing":
            return self._message(
                language,
                "waiting",
                "Čakám na výsledok. Zastavenie je stále dostupné.",
                "Čekám na výsledek. Zastavení je stále dostupné.",
                "Waiting for the result. Stop remains available.",
            )
        if pending.stage == "choose":
            return self._selection(text, pending)
        if pending.stage == "confirm" and word in _YES:
            return self._propose(pending, bypass=True)
        if pending.stage == "review" and word in _YES:
            self._question(replace(pending, stage="remember", deadline_ms=now_ms + _REVIEW_MS))
            return self._message(
                language,
                "remember",
                "Zapamätať túto frázu? Áno uloží význam; áno bez potvrdenia dovolí aj vynechať budúcu otázku. Nie nič neuloží.",
                "Zapamatovat tuto frázi? Ano uloží význam; ano bez potvrzení dovolí i vynechat budoucí otázku. Ne nic neuloží.",
                "Remember this phrase? Yes saves its meaning; yes without confirmation also allows skipping future confirmation. No saves nothing.",
            )
        if pending.stage == "remember" and word in _YES | _SKIP:
            if pending.command is None or pending.run_id is None:
                msg = "Missing reviewed recipe binding."
                raise ValueError(msg)
            recipe = CommandRecipe(
                language=language,
                phrase=phrase_key(pending.phrase),
                command=pending.command,
                skip_confirmation=word in _SKIP,
                learned_request_id=pending.context.request_id,
                learned_run_id=pending.run_id,
            )
            # Consume before the write: an ambiguous commit must never be retried by another yes.
            self.cancel()
            await self.store.remember(recipe)
            return self._message(
                language,
                "remembered",
                "Fráza zapamätaná. Oprávnenia hry sa overia pri každom použití.",
                "Fráze zapamatována. Oprávnění hry se ověří při každém použití.",
                "Phrase remembered. Game authority is checked on every use.",
            )
        return None

    def _selection(self, text: str, pending: _Pending) -> DialogueDecision:
        word, language = _words(text), pending.language
        number = int(word) if word.isascii() and word.isdecimal() else _NUMBERS.get(word, 0)
        command = pending.options[number - 1] if 1 <= number <= len(pending.options) else text.strip()
        try:
            action = parse_text(command, target_id=pending.context.target_id)
        except ValueError:
            return self._message(
                language,
                "choose",
                "Vyber platné číslo alebo presný podporovaný príkaz; nie pre zrušenie.",
                "Vyber platné číslo nebo přesný podporovaný příkaz; ne pro zrušení.",
                "Choose a valid number or exact supported command; no cancels.",
            )
        # A menu is not a reusable utterance; an unknown phrase can become an alias.
        phrase = command if _words(pending.phrase) in _MENU | _WEATHER | _TIME else pending.phrase
        command = action_command(action)
        if (
            isinstance(action, FollowAction)
            and _words(text) not in {"follow me", "nasleduj ma", "následuj mě", "nasleduj me"}
            and not word.isdecimal()
        ):
            command = "follow " + action.target_id
        return self._propose(
            replace(pending, command=command, action=action, phrase=phrase), bypass=self.confirmation == "never"
        )

    def complete(
        self, request: CompanionRequest, result: CompanionResult | None, run_id: str | None, *, now_ms: int
    ) -> DialogueDecision | None:
        """Permit review only after a correlated applied receipt; never learn automatically."""
        pending = self._pending
        if pending is None or pending.stage != "executing" or pending.request != request:
            return None
        self.cancel()
        if (
            pending.learned
            or result is None
            or result.kind != "applied"
            or result.request_id != request.request_id
            or run_id is None
        ):
            return None
        try:
            phrase_key(pending.phrase)
            command_action(pending.command or "", pending.context.target_id)
        except ValueError:
            return None
        self._question(replace(pending, stage="review", run_id=run_id, deadline_ms=now_ms + _REVIEW_MS))
        return self._message(
            pending.language,
            "review",
            "Je výsledok v poriadku? Áno ponúkne zapamätanie, nie nič neuloží.",
            "Je výsledek v pořádku? Ano nabídne zapamatování, ne nic neuloží.",
            "Is the result right? Yes offers remembering; no saves nothing.",
        )
