"""
Live Tutor orchestration.

Ties together the store, the bot runtime, and the Opus 4.8 responder, and
enforces the two invariants that make this safe to point at a room full of
students:

  1. Approve-first: anything the AI generates becomes a *pending approval*.
     It is only sent after a human approves it. Admin-authored messages
     (reminders, manually typed DMs) are inherently reviewed, so they send
     directly -- but every send, from any source, is written to the message log.

  2. Don't be distracting: AI drafting is gated by capability toggles, a
     per-session cooldown, a per-session AI-message cap, and a quiet-mode kill
     switch -- checked *before* drafting so we don't pile up stale drafts.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .bot_runtime import BotRuntime, BotRuntimeError, JoinRequest, get_bot_runtime
from .policy_responder import PolicyResponder, get_policy_responder
from . import store as store_mod
from .store import TutorStore, get_tutor_store

logger = logging.getLogger(__name__)


class TutorServiceError(RuntimeError):
    pass


def _looks_like_question(text: str, bot_name: str) -> bool:
    """Cheap gate so we only spend a model call on plausible questions."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if "?" in t:
        return True
    if bot_name and bot_name.split()[0].lower() in t:
        return True
    starters = ("how ", "what ", "when ", "where ", "why ", "who ", "can i",
                "do i", "is there", "are we", "could you", "should i", "will ")
    return t.startswith(starters)


class LiveTutorService:
    def __init__(
        self,
        store: Optional[TutorStore] = None,
        bot: Optional[BotRuntime] = None,
        responder: Optional[PolicyResponder] = None,
    ):
        self.store = store or get_tutor_store()
        self.bot = bot or get_bot_runtime()
        self.responder = responder or get_policy_responder()

    # ----------------------------------------------------------- capabilities

    def effective_capabilities(self, session: Optional[Dict[str, Any]]) -> Dict[str, bool]:
        settings = self.store.get_settings()
        caps = dict(settings.get("capabilities", {}))
        overrides = (session or {}).get("overrides") or {}
        cap_overrides = overrides.get("capabilities") if isinstance(overrides, dict) else None
        if isinstance(cap_overrides, dict):
            caps.update({k: bool(v) for k, v in cap_overrides.items()})
        return caps

    def _guardrail_ok(self, session: Dict[str, Any]) -> Tuple[bool, str]:
        """Whether an AI message may be drafted/sent for this session right now."""
        import time
        settings = self.store.get_settings()
        g = settings.get("guardrails", {})
        if g.get("quiet_mode"):
            return False, "quiet mode is on"
        sid = session["id"]
        cap = int(g.get("max_ai_messages_per_session", 20) or 0)
        if cap and self.store.count_ai_messages_in_session(sid) >= cap:
            return False, f"per-session AI message cap reached ({cap})"
        cooldown = float(g.get("min_seconds_between_messages", 0) or 0)
        if cooldown:
            last = self.store.last_outbound_message_at(sid)
            if last and (time.time() - last) < cooldown:
                return False, f"cooldown active ({cooldown:.0f}s between messages)"
        return True, ""

    # ------------------------------------------------------------- summon/leave

    async def summon(
        self,
        meeting_id: str,
        *,
        meeting_uuid: Optional[str] = None,
        topic: Optional[str] = None,
        session_code: Optional[str] = None,
        join_url: Optional[str] = None,
        passcode: Optional[str] = None,
        zak: Optional[str] = None,
        role: Optional[int] = None,
        summoned_by: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        settings = self.store.get_settings()
        if not settings.get("capabilities", {}).get("summon_dismiss", True):
            raise TutorServiceError("Summon/dismiss is disabled in settings.")

        existing = self.store.get_active_session_for_meeting(meeting_id)
        if existing:
            return existing

        session = self.store.create_session(
            meeting_id, meeting_uuid=meeting_uuid, topic=topic,
            session_code=session_code, join_url=join_url, summoned_by=summoned_by,
            overrides=overrides, status=store_mod.SESSION_JOINING,
        )

        bot_cfg = settings.get("bot", {})
        try:
            runtime_id = await self.bot.join(JoinRequest(
                meeting_id=meeting_id,
                session_ref=str(session["id"]),
                display_name=bot_cfg.get("display_name", "AALB Assistant"),
                meeting_uuid=meeting_uuid,
                join_url=join_url,
                announce=bool(bot_cfg.get("announce_on_join", True)),
                announcement=bot_cfg.get("announcement"),
                capture=settings.get("capture"),
                passcode=passcode,
                zak=zak,
                role=role,
            ))
        except BotRuntimeError as e:
            self.store.update_session(session["id"], status=store_mod.SESSION_ERROR, error=str(e))
            raise TutorServiceError(f"Bot failed to join: {e}") from e

        status = store_mod.SESSION_IN_MEETING if self.bot.available else store_mod.SESSION_REQUESTED
        return self.store.update_session(session["id"], runtime_id=runtime_id, status=status)

    async def dismiss(self, session_id: int, *, by: Optional[str] = None) -> Dict[str, Any]:
        session = self.store.get_session(session_id)
        if not session:
            raise TutorServiceError("Session not found.")
        self.store.update_session(session_id, status=store_mod.SESSION_LEAVING)
        rid = session.get("runtime_id")
        if rid:
            try:
                await self.bot.leave(rid)
            except BotRuntimeError as e:
                logger.warning("[TUTOR] leave error (marking left anyway): %s", e)
        return self.store.update_session(session_id, status=store_mod.SESSION_LEFT)

    # ---------------------------------------------------------------- sending

    async def _send_now(
        self,
        session: Dict[str, Any],
        *,
        channel: str,
        text: str,
        source: str,
        reason: Optional[str] = None,
        target_id: Optional[str] = None,
        target_name: Optional[str] = None,
        approval_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Actually push a message to the meeting and log it."""
        rid = session.get("runtime_id")
        if not rid:
            raise TutorServiceError("Bot is not in the meeting (no runtime id).")
        try:
            await self.bot.send_message(rid, channel, text, to_participant_id=target_id)
        except BotRuntimeError as e:
            raise TutorServiceError(f"Send failed: {e}") from e
        return self.store.add_message(
            direction="outbound", channel=channel, text=text, source=source,
            session_id=session["id"], meeting_id=session.get("meeting_id"),
            participant_id=target_id, participant_name=target_name,
            reason=reason, approval_id=approval_id,
        )

    async def post_reminder(
        self,
        session_id: int,
        *,
        reminder_id: Optional[int] = None,
        text: Optional[str] = None,
        by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Admin posts a reminder to public chat (direct send -- admin authored)."""
        session = self._require_active_session(session_id)
        if not self.effective_capabilities(session).get("reminders", True):
            raise TutorServiceError("Reminders are disabled for this session.")
        if self.store.get_settings().get("guardrails", {}).get("quiet_mode"):
            raise TutorServiceError("Quiet mode is on.")

        reason = None
        if reminder_id is not None:
            reminder = self.store.get_reminder(reminder_id)
            if not reminder:
                raise TutorServiceError("Reminder template not found.")
            text = reminder["message"]
            reason = reminder["label"]
        if not text:
            raise TutorServiceError("No reminder text provided.")
        return await self._send_now(
            session, channel="public", text=text, source="reminder", reason=reason,
        )

    async def send_manual_message(
        self,
        session_id: int,
        *,
        channel: str,
        text: str,
        target_id: Optional[str] = None,
        target_name: Optional[str] = None,
        by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Admin types a message (public or DM) and sends it directly."""
        session = self._require_active_session(session_id)
        caps = self.effective_capabilities(session)
        if channel == "dm" and not caps.get("direct_messages", True):
            raise TutorServiceError("Direct messages are disabled for this session.")
        if channel == "public" and not caps.get("reminders", True):
            raise TutorServiceError("Public messaging is disabled for this session.")
        if channel == "dm" and not target_id:
            raise TutorServiceError("A target participant is required for a DM.")
        return await self._send_now(
            session, channel=channel, text=text, source="manual",
            target_id=target_id, target_name=target_name,
        )

    # ------------------------------------------------------------- AI drafting

    async def request_ai_dm(
        self,
        session_id: int,
        *,
        target_id: str,
        target_name: Optional[str],
        instruction: str,
    ) -> Dict[str, Any]:
        """Admin asks the AI to draft a DM; result goes to the approval queue."""
        session = self._require_active_session(session_id)
        caps = self.effective_capabilities(session)
        if not caps.get("direct_messages", True):
            raise TutorServiceError("Direct messages are disabled for this session.")
        policies = self.store.list_policies(include_disabled=False)
        bot_name = self.store.get_settings().get("bot", {}).get("display_name", "AALB Assistant")
        prompt = (
            f"Compose a brief, friendly direct message to "
            f"{target_name or 'this student'}. Purpose: {instruction}"
        )
        draft = await self.responder.draft(
            prompt, policies, bot_name=bot_name, asker_name=target_name,
        )
        if not draft.available:
            raise TutorServiceError(f"AI responder unavailable: {draft.error}")
        reply = draft.reply or instruction
        return self.store.create_approval(
            draft_text=reply, channel="dm", source="ai_dm",
            session_id=session_id, meeting_id=session.get("meeting_id"),
            target_id=target_id, target_name=target_name,
            reason="AI-drafted DM", context={"instruction": instruction,
                                             "rationale": draft.rationale},
            confidence=draft.confidence,
        )

    async def handle_inbound_chat(
        self,
        session: Dict[str, Any],
        *,
        channel: str,
        text: str,
        participant_id: Optional[str],
        participant_name: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Log an inbound message and, if appropriate, draft an answer for review."""
        # Always log what students send to us.
        self.store.add_message(
            direction="inbound", channel=channel, text=text, source="student",
            session_id=session["id"], meeting_id=session.get("meeting_id"),
            participant_id=participant_id, participant_name=participant_name,
        )

        caps = self.effective_capabilities(session)
        bot_name = self.store.get_settings().get("bot", {}).get("display_name", "AALB Assistant")

        if channel == "public":
            if not caps.get("answer_questions", False):
                return None
            if not _looks_like_question(text, bot_name):
                return None
        elif channel == "dm":
            if not caps.get("direct_messages", True):
                return None
        else:
            return None

        ok, why = self._guardrail_ok(session)
        if not ok:
            logger.info("[TUTOR] Skipping draft (%s) for session %s", why, session["id"])
            return None

        if not self.responder.available:
            logger.info("[TUTOR] Responder unavailable; not drafting.")
            return None

        policies = self.store.list_policies(include_disabled=False)
        draft = await self.responder.draft(
            text, policies, bot_name=bot_name, asker_name=participant_name,
        )
        if not draft.available or not draft.should_answer or not draft.reply:
            return None

        return self.store.create_approval(
            draft_text=draft.reply, channel=channel, source="ai_answer",
            session_id=session["id"], meeting_id=session.get("meeting_id"),
            target_id=participant_id if channel == "dm" else None,
            target_name=participant_name,
            reason="AI-drafted answer",
            context={"question": text, "rationale": draft.rationale},
            confidence=draft.confidence,
        )

    # -------------------------------------------------------- approval actions

    async def approve(
        self, approval_id: int, *, by: Optional[str] = None, final_text: Optional[str] = None
    ) -> Dict[str, Any]:
        import time
        approval = self.store.get_approval(approval_id)
        if not approval:
            raise TutorServiceError("Approval not found.")
        if approval["status"] != store_mod.APPROVAL_PENDING:
            raise TutorServiceError(f"Approval is already {approval['status']}.")

        session = self.store.get_session(approval["session_id"]) if approval.get("session_id") else None
        if not session or session["status"] not in store_mod.SESSION_ACTIVE_STATES:
            self.store.update_approval(approval_id, status=store_mod.APPROVAL_FAILED,
                                       decided_by=by, decided_at=time.time())
            raise TutorServiceError("The bot is no longer in this meeting.")

        text = (final_text or approval["draft_text"]).strip()
        if not text:
            raise TutorServiceError("Cannot send an empty message.")

        try:
            await self._send_now(
                session, channel=approval["channel"], text=text,
                source=approval["source"], reason=approval.get("reason"),
                target_id=approval.get("target_id"),
                target_name=approval.get("target_name"),
                approval_id=approval_id,
            )
        except TutorServiceError:
            self.store.update_approval(approval_id, status=store_mod.APPROVAL_FAILED,
                                       decided_by=by, decided_at=time.time(),
                                       final_text=text)
            raise

        return self.store.update_approval(
            approval_id, status=store_mod.APPROVAL_SENT, decided_by=by,
            decided_at=time.time(), final_text=text,
        )

    def reject(self, approval_id: int, *, by: Optional[str] = None) -> Dict[str, Any]:
        import time
        approval = self.store.get_approval(approval_id)
        if not approval:
            raise TutorServiceError("Approval not found.")
        if approval["status"] != store_mod.APPROVAL_PENDING:
            raise TutorServiceError(f"Approval is already {approval['status']}.")
        return self.store.update_approval(
            approval_id, status=store_mod.APPROVAL_REJECTED, decided_by=by,
            decided_at=time.time(),
        )

    # ---------------------------------------------------------------- helpers

    def _require_active_session(self, session_id: int) -> Dict[str, Any]:
        session = self.store.get_session(session_id)
        if not session:
            raise TutorServiceError("Session not found.")
        if session["status"] not in store_mod.SESSION_ACTIVE_STATES:
            raise TutorServiceError("The bot is not active in this meeting.")
        return session


_service: Optional[LiveTutorService] = None


def get_tutor_service() -> LiveTutorService:
    global _service
    if _service is None:
        _service = LiveTutorService()
    return _service
