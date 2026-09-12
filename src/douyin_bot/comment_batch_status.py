from __future__ import annotations


def update_comment_batch_progress(
    *,
    session: dict,
    response_kind: str,
    batch_new_comment_count: int,
    batch_existing_comment_count: int,
) -> dict:
    if str(response_kind or "") != "comment":
        return {
            "duplicate_comment_batch_streak": int(session.get("duplicate_comment_batch_streak", 0) or 0),
            "no_new_comment_batch_streak": int(session.get("no_new_comment_batch_streak", 0) or 0),
            "should_log_no_new_batch": False,
        }

    if int(batch_new_comment_count or 0) > 0:
        session["duplicate_comment_batch_streak"] = 0
        session["no_new_comment_batch_streak"] = 0
        return {
            "duplicate_comment_batch_streak": 0,
            "no_new_comment_batch_streak": 0,
            "should_log_no_new_batch": False,
        }

    session["no_new_comment_batch_streak"] = int(session.get("no_new_comment_batch_streak", 0) or 0) + 1
    if int(batch_existing_comment_count or 0) > 0:
        session["duplicate_comment_batch_streak"] = int(session.get("duplicate_comment_batch_streak", 0) or 0) + 1

    return {
        "duplicate_comment_batch_streak": int(session.get("duplicate_comment_batch_streak", 0) or 0),
        "no_new_comment_batch_streak": int(session.get("no_new_comment_batch_streak", 0) or 0),
        "should_log_no_new_batch": True,
    }
