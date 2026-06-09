"""
Live Tutor package.

A policy-aware, admin-controlled assistant that can join live Zoom meetings
(via a self-hosted Meeting SDK bot), post reminders, draft answers to student
questions with Opus 4.8, and exchange direct messages -- with every AI-generated
message routed through a human approval queue before it reaches a student.

Modules:
  - store: SQLite persistence (settings, reminders, policies, bot sessions,
           approval queue, message log).
  - bot_runtime: adapter interface to the meeting bot (self-hosted / null).
  - policy_responder: Opus 4.8 draft generation.
  - service: orchestration tying the pieces together.
"""
