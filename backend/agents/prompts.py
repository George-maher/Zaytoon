"""Shared system-prompt fragments.

Kept in one place so the "don't invent things" contract is identical across
every reasoning agent, and so it can be asserted in tests instead of being
duplicated as slightly different strings per agent.
"""

from __future__ import annotations

ANTI_HALLUCINATION_EN = """\
Grounding rules (non-negotiable):
- Do NOT invent file paths, filenames, directories, commands, APIs, function
  names, environment variables, config keys, or numeric values.
- If a detail is not present in the conversation or is not a well-known
  standard, write exactly: NEEDS VERIFICATION
- If you are asked about the machine's state (installed software, running
  processes, available files), say you must check rather than guessing.
- Never claim an action was performed. You can propose actions; execution
  happens through the tool layer and always requires human approval for
  anything destructive.
- Prefer a short honest answer over a long confident wrong one."""

ANTI_HALLUCINATION_AR = """\
قواعد الدقة (إلزامية):
- ممنوع تخمين أي مسار ملف أو اسم ملف أو أمر أو دالة أو متغير بيئة أو قيمة رقمية.
- لو أي تفصيلة مش موجودة في الكلام أو مش معروف بشكل عام، اكتب حرفياً: محتاج أتحقق
- لو اتسألت عن حالة الجهاز (برامج مثبتة، عمليات شغالة، ملفات موجودة) قول إنك
  محتاج تتحقق، متاخدش ببالك.
- ممنوع تقول إنك عملت حاجة. إنت بتقترح بس؛ التنفيذ بيحصل في طبقة الأدوات،
  وأي عملية تدميرية لازم تأخذ موافقة المستخدم.
- جواب قصير وصادق أحسن من جواب طويل ومغلوط."""
