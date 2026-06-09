"""
Policy-aware answer drafting with Claude Opus 4.8.

Given a student's question (or an admin's DM intent) plus the org's enabled
policies, this drafts a short reply. It NEVER sends anything -- the draft is
returned and the service layer routes it into the approval queue. It can also
abstain (should_answer = False) when the question isn't clearly answerable from
the provided policies, which is the main lever for keeping the bot from being
noisy/distracting.

Model: claude-opus-4-8 with adaptive thinking and structured JSON output.
Requires ANTHROPIC_API_KEY. If the key or SDK is missing, draft() returns an
unavailable result so the rest of the app keeps working.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"

# Structured output schema -- forces a clean, parseable decision.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "should_answer": {
            "type": "boolean",
            "description": (
                "True only if the question is clearly and appropriately "
                "answerable from the provided policies. False for anything "
                "off-topic, personal, ambiguous, or requiring information not "
                "in the policies."
            ),
        },
        "reply": {
            "type": "string",
            "description": "The short reply to the student (empty if should_answer is false).",
        },
        "rationale": {
            "type": "string",
            "description": "One line for the reviewing admin: why this answer (or why abstaining).",
        },
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["should_answer", "reply", "rationale", "confidence"],
    "additionalProperties": False,
}


@dataclass
class Draft:
    should_answer: bool
    reply: str
    rationale: str
    confidence: str
    available: bool = True
    error: Optional[str] = None

    @classmethod
    def unavailable(cls, reason: str) -> "Draft":
        return cls(False, "", reason, "low", available=False, error=reason)


def _build_system_prompt(policies: List[Dict[str, Any]], bot_name: str) -> str:
    lines = [
        f"You are {bot_name}, an assistant inside a live online class run by AALB "
        "(American Academy for the Liberal Arts / blind-services education).",
        "Students type questions in the meeting chat. Your job is to draft a SHORT, "
        "friendly reply that stays strictly within the class policies below.",
        "",
        "Hard rules:",
        "- Answer ONLY using the policies and general class-logistics common sense. "
        "Do not invent schedules, grades, links, or facts not given.",
        "- If the question is personal, off-topic, about another student, requires "
        "information you don't have, or isn't clearly covered by a policy, set "
        "should_answer to false and let a human handle it.",
        "- Never give medical, legal, mental-health, or financial advice. Never "
        "reveal personal data about any student.",
        "- Keep replies to 1-3 short sentences. No preamble, no sign-off. Plain text.",
        "- A staff member reviews every message before it is sent, so when in doubt, "
        "abstain rather than guess.",
    ]
    if policies:
        lines.append("")
        lines.append("Class policies:")
        for p in policies:
            title = p.get("title", "Policy")
            content = p.get("content", "")
            lines.append(f"- {title}: {content}")
    else:
        lines.append("")
        lines.append("No specific policies are configured yet; abstain on anything "
                     "not answerable from plain class-logistics common sense.")
    return "\n".join(lines)


class PolicyResponder:
    def __init__(self):
        self._client = None
        self._init_error: Optional[str] = None
        self._init_client()

    def _init_client(self) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            self._init_error = "ANTHROPIC_API_KEY not set"
            logger.warning("[TUTOR] PolicyResponder disabled: %s", self._init_error)
            return
        try:
            import anthropic  # lazy: keep import optional
            self._client = anthropic.AsyncAnthropic(api_key=api_key)
            logger.info("[TUTOR] PolicyResponder ready (model=%s)", MODEL)
        except Exception as e:  # pragma: no cover - import/runtime guard
            self._init_error = f"anthropic SDK unavailable: {e}"
            logger.warning("[TUTOR] PolicyResponder disabled: %s", self._init_error)

    @property
    def available(self) -> bool:
        return self._client is not None

    async def draft(
        self,
        question: str,
        policies: List[Dict[str, Any]],
        *,
        bot_name: str = "AALB Assistant",
        asker_name: Optional[str] = None,
        instruction: Optional[str] = None,
    ) -> Draft:
        """Draft a policy-compliant reply (or abstain). Sends nothing."""
        if not self.available:
            return Draft.unavailable(self._init_error or "responder unavailable")

        system = _build_system_prompt(policies, bot_name)
        parts = []
        if asker_name:
            parts.append(f"Student name: {asker_name}")
        if instruction:
            # Used for admin-initiated DM drafts ("remind them to submit their ID").
            parts.append(f"Staff instruction for this reply: {instruction}")
        parts.append(f"Question / message:\n{question}")
        user_content = "\n\n".join(parts)

        try:
            resp = await self._client.messages.create(
                model=MODEL,
                max_tokens=2048,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": _RESPONSE_SCHEMA},
                },
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
        except Exception as e:
            logger.error("[TUTOR] draft() API error: %s", e)
            return Draft.unavailable(f"draft failed: {e}")

        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if not text:
            return Draft.unavailable("model returned no text block")
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as e:
            logger.error("[TUTOR] draft() JSON parse error: %s; raw=%s", e, text[:200])
            return Draft.unavailable("model returned unparseable output")

        return Draft(
            should_answer=bool(data.get("should_answer")),
            reply=(data.get("reply") or "").strip(),
            rationale=(data.get("rationale") or "").strip(),
            confidence=data.get("confidence") or "low",
        )


_responder: Optional[PolicyResponder] = None


def get_policy_responder() -> PolicyResponder:
    global _responder
    if _responder is None:
        _responder = PolicyResponder()
    return _responder
