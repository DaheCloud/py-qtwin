"""读取解析审计中保存的模式；无模式的历史记录按详细解析处理。"""

import json

from sqlalchemy import select

from models.document import AuditLog
from pdf.parse_profiles import PARSE_DETAILED, PARSE_PROFILE_LABELS


def load_parse_profiles(session, document_id: int | None = None) -> dict[int, str]:
    stmt = select(AuditLog.document_id, AuditLog.detail).where(AuditLog.action == "process")
    if document_id is not None:
        stmt = stmt.where(AuditLog.document_id == document_id)
    profiles = {}
    for doc_id, detail in session.execute(stmt.order_by(AuditLog.id)):
        try:
            payload = json.loads((detail or "").split(" ", 1)[1])
            profile = (payload.get("parse") or {}).get("profile") or payload.get("parse_profile")
        except (ValueError, IndexError, AttributeError, TypeError):
            profile = None
        profiles[doc_id] = profile if profile in PARSE_PROFILE_LABELS else PARSE_DETAILED
    return profiles
