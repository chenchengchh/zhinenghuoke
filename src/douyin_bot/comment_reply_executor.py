from __future__ import annotations

from typing import Any, Callable


def execute_original_comment_reply_rounds(
    *,
    target: dict,
    reply_text: str,
    aweme_id: str,
    comment_id: str,
    detail_structure: Any,
    surface_selector: str,
    reply_diagnostic_mode: bool,
    enrich_reply_target_with_live_comment_anchor: Callable[..., dict],
    ensure_reply_target_context: Callable[..., dict],
    inspect_detail_page_structure: Callable[[], Any],
    ensure_comment_surface_ready: Callable[..., dict],
    click_reply_button_for_comment_target: Callable[..., dict],
    find_comment_editor_from_exact_dom: Callable[..., Any],
    find_comment_editor_by_js: Callable[..., Any],
    is_reply_context_active: Callable[..., bool],
    attempt_reply_stage_recovery: Callable[..., dict],
    fill_comment_editor: Callable[..., bool],
    find_exact_comment_send_button: Callable[..., Any],
    submit_comment_and_confirm_result: Callable[..., dict],
    scroll_comment_list: Callable[..., Any],
    set_last_original_reply_result: Callable[..., dict],
    build_reply_diagnostics: Callable[..., dict],
    logger: Any,
    sleep_fn: Callable[[float], None],
    random_uniform_fn: Callable[[float, float], float],
) -> bool:
    max_rounds = 4
    last_click_result = {"reason": "target_not_found"}
    upward_probe_rounds = 1
    upward_exhausted = False
    stage_retry_budget = {
        "reply_button_not_effective": 0,
        "reply_editor_missing": 0,
        "reply_context_missing": 0,
        "send_button_missing": 0,
        "submit_verify_failed": 0,
        "submit_request_rejected": 0,
    }
    last_round_context_state = {}

    for round_index in range(max_rounds):
        target = enrich_reply_target_with_live_comment_anchor(
            target,
            aweme_id=aweme_id,
            bootstrap_timeout_seconds=0.25 if round_index == 0 else 0.0,
        )
        round_context_state = ensure_reply_target_context(target, allow_recover=(round_index == 0))
        last_round_context_state = round_context_state if isinstance(round_context_state, dict) else {}
        if not round_context_state.get("matched"):
            set_last_original_reply_result(
                ok=False,
                reason="current_video_context_mismatch",
                stage="validate_context",
                message=(
                    f"定位目标评论前页面视频上下文不匹配，要求 aweme_id={aweme_id}，"
                    f"实际 aweme_id={str(round_context_state.get('current_aweme_id', '') or 'unknown')}"
                ),
                diagnostics=build_reply_diagnostics(
                    target=target,
                    context_state=round_context_state,
                    surface_selector=surface_selector,
                ),
            )
            logger.warning(
                "评论下回复取消执行：定位目标楼层前帖子上下文不匹配，"
                f"target_aweme_id={aweme_id}, current_aweme_id={str(round_context_state.get('current_aweme_id', '') or '')}"
            )
            return False
        if round_context_state.get("recovered"):
            detail_structure = inspect_detail_page_structure()
            surface_info = ensure_comment_surface_ready(detail_structure=detail_structure)
            surface_selector = str(surface_info.get("surface_selector", "") or surface_selector)
        click_result = click_reply_button_for_comment_target(
            target,
            detail_structure=detail_structure,
            preferred_surface_selector=surface_selector,
            allow_document_fallback=bool(reply_diagnostic_mode and round_index >= max_rounds - 2),
        )
        if str((click_result or {}).get("reason", "") or "").strip() == "reply_button_not_effective":
            set_last_original_reply_result(
                ok=False,
                reason="reply_button_not_effective",
                stage="click_reply",
                message=(
                    "已定位到目标评论，但点击回复控件后未进入对应回复状态；"
                    f"细节: {str((click_result or {}).get('activation', {}) or '')[:240]}"
                ),
                diagnostics=build_reply_diagnostics(
                    target=target,
                    context_state=round_context_state,
                    click_result=click_result,
                    activation=(click_result or {}).get("activation") if isinstance(click_result, dict) else {},
                    surface_selector=surface_selector,
                ),
            )
            logger.warning(
                "评论下回复未命中有效回复入口: "
                f"click_result={click_result}"
            )
            return False
        if click_result.get("clicked"):
            sleep_fn(0.35)
            activation = click_result.get("activation") if isinstance(click_result, dict) else {}
            while True:
                editor = find_comment_editor_from_exact_dom(
                    detail_structure=detail_structure,
                    activation=activation,
                ) or find_comment_editor_by_js(
                    detail_structure=detail_structure,
                    activation=activation,
                )
                if not editor:
                    context_active = is_reply_context_active(target, activation=activation)
                    failure_reason = "reply_editor_missing" if context_active else "reply_button_not_effective"
                    if stage_retry_budget.get(failure_reason, 0) > 0:
                        stage_retry_budget[failure_reason] -= 1
                        recovered = attempt_reply_stage_recovery(
                            target,
                            detail_structure=detail_structure,
                            surface_selector=surface_selector,
                            stage=failure_reason,
                            activation=activation,
                        ) or {}
                        detail_structure = recovered.get("detail_structure") or detail_structure
                        surface_selector = str(recovered.get("surface_selector", "") or surface_selector)
                        activation = recovered.get("activation") if isinstance(recovered.get("activation"), dict) else activation
                        if recovered.get("recovered"):
                            continue
                    if not context_active:
                        set_last_original_reply_result(
                            ok=False,
                            reason="reply_button_not_effective",
                            stage="open_editor",
                            message="尝试点击回复入口后页面未进入回复状态",
                            diagnostics=build_reply_diagnostics(
                                target=target,
                                context_state=round_context_state,
                                click_result=click_result,
                                activation=activation,
                                surface_selector=surface_selector,
                            ),
                        )
                        logger.warning(
                            "评论下回复点击后未进入回复态，疑似没有命中有效回复入口: "
                            f"click_result={click_result}"
                        )
                        return False
                    set_last_original_reply_result(
                        ok=False,
                        reason="reply_editor_missing",
                        stage="open_editor",
                        message="已进入回复态，但没有找到输入框",
                        diagnostics=build_reply_diagnostics(
                            target=target,
                            context_state=round_context_state,
                            click_result=click_result,
                            activation=activation,
                            surface_selector=surface_selector,
                        ),
                    )
                    logger.warning("评论下回复已进入回复态，但未找到输入框")
                    return False
                if not is_reply_context_active(target, editor, activation=activation):
                    if stage_retry_budget.get("reply_context_missing", 0) > 0:
                        stage_retry_budget["reply_context_missing"] -= 1
                        recovered = attempt_reply_stage_recovery(
                            target,
                            detail_structure=detail_structure,
                            surface_selector=surface_selector,
                            stage="reply_context_missing",
                            activation=activation,
                        ) or {}
                        detail_structure = recovered.get("detail_structure") or detail_structure
                        surface_selector = str(recovered.get("surface_selector", "") or surface_selector)
                        activation = recovered.get("activation") if isinstance(recovered.get("activation"), dict) else activation
                        if recovered.get("recovered"):
                            continue
                    set_last_original_reply_result(
                        ok=False,
                        reason="reply_context_missing",
                        stage="verify_context",
                        message="没有检测到楼层回复上下文",
                        diagnostics=build_reply_diagnostics(
                            target=target,
                            context_state=round_context_state,
                            click_result=click_result,
                            activation=activation,
                            surface_selector=surface_selector,
                        ),
                    )
                    logger.warning("评论下回复未检测到楼层回复上下文，取消发送以避免误发到普通评论区")
                    return False
                if not fill_comment_editor(editor, reply_text):
                    set_last_original_reply_result(
                        ok=False,
                        reason="reply_fill_failed",
                        stage="fill_editor",
                        message="回复内容写入失败",
                        diagnostics=build_reply_diagnostics(
                            target=target,
                            context_state=round_context_state,
                            click_result=click_result,
                            activation=activation,
                            surface_selector=surface_selector,
                        ),
                    )
                    logger.warning("评论下回复写入文本失败")
                    return False

                send_button = find_exact_comment_send_button(editor, activation=activation)
                if not send_button:
                    if stage_retry_budget.get("send_button_missing", 0) > 0:
                        stage_retry_budget["send_button_missing"] -= 1
                        recovered = attempt_reply_stage_recovery(
                            target,
                            detail_structure=detail_structure,
                            surface_selector=surface_selector,
                            stage="send_button_missing",
                            activation=activation,
                        ) or {}
                        detail_structure = recovered.get("detail_structure") or detail_structure
                        surface_selector = str(recovered.get("surface_selector", "") or surface_selector)
                        activation = recovered.get("activation") if isinstance(recovered.get("activation"), dict) else activation
                        if recovered.get("recovered"):
                            continue
                    set_last_original_reply_result(
                        ok=False,
                        reason="send_button_missing",
                        stage="find_send_button",
                        message="没有定位到发送按钮",
                        diagnostics=build_reply_diagnostics(
                            target=target,
                            context_state=round_context_state,
                            click_result=click_result,
                            activation=activation,
                            surface_selector=surface_selector,
                        ),
                    )
                    logger.warning("评论下回复未定位到发送按钮")
                    return False
                submit_result = submit_comment_and_confirm_result(send_button, editor, reply_text)
                if submit_result.get("ok"):
                    set_last_original_reply_result(
                        ok=True,
                        reason=str(submit_result.get("reason", "") or "success"),
                        stage="submit",
                        message=str(submit_result.get("message", "") or "评论下回复发送成功"),
                        diagnostics=build_reply_diagnostics(
                            target=target,
                            context_state=round_context_state,
                            click_result=click_result,
                            activation=activation,
                            surface_selector=surface_selector,
                        ),
                    )
                    logger.info(f"评论下回复发送成功: aweme_id={aweme_id}, comment_id={comment_id}")
                    return True
                submit_reason = str(submit_result.get("reason", "") or "submit_verify_failed")
                if stage_retry_budget.get(submit_reason, 0) > 0:
                    stage_retry_budget[submit_reason] -= 1
                    recovered = attempt_reply_stage_recovery(
                        target,
                        detail_structure=detail_structure,
                        surface_selector=surface_selector,
                        stage=submit_reason,
                        activation=activation,
                    ) or {}
                    detail_structure = recovered.get("detail_structure") or detail_structure
                    surface_selector = str(recovered.get("surface_selector", "") or surface_selector)
                    activation = recovered.get("activation") if isinstance(recovered.get("activation"), dict) else activation
                    if recovered.get("recovered"):
                        continue
                set_last_original_reply_result(
                    ok=False,
                    reason=submit_reason,
                    stage="submit",
                    message=str(submit_result.get("message", "") or "点击发送后未通过输入框状态校验"),
                    diagnostics=build_reply_diagnostics(
                        target=target,
                        context_state=round_context_state,
                        click_result=click_result,
                        activation=activation,
                        surface_selector=surface_selector,
                    ),
                )
                logger.warning(
                    "评论下回复发送失败: "
                    f"reason={submit_result.get('reason', '')}, "
                    f"message={submit_result.get('message', '')}"
                )
                return False

        last_click_result = click_result if isinstance(click_result, dict) else {"reason": "target_not_found"}

        if round_index > 0:
            try:
                scroll_comment_list(
                    surface_selector,
                    direction="down",
                    max_rounds=1,
                    allow_top_reprobe=False,
                )
                sleep_fn(0.2)
            except Exception:
                pass

        scroll_direction = "up" if not upward_exhausted and round_index < upward_probe_rounds else "down"
        scroll_result = scroll_comment_list(
            surface_selector,
            direction=scroll_direction,
            target=target,
            target_marker_uid=str((click_result or {}).get("markerUid", "") or "").strip(),
            parent_marker_uid=str((click_result or {}).get("parentMarkerUid", "") or "").strip(),
        )
        if isinstance(scroll_result, dict):
            surface_selector = str(scroll_result.get("selector", "") or surface_selector)
            if scroll_direction == "up" and (
                scroll_result.get("near_top") or not scroll_result.get("scrolled")
            ):
                upward_exhausted = True
        sleep_fn(random_uniform_fn(0.2, 0.4))

    final_reason = str((last_click_result or {}).get("reason", "") or "target_not_found").strip()
    final_message_map = {
        "target_ambiguous": "找到了多个近似目标评论，已取消避免误回复",
        "comment_surface_missing": "未找到稳定评论面板，已取消全页面兜底避免误回复",
        "reply_button_missing": "已定位到目标评论，但没有找到回复按钮",
        "reply_button_lookup_failed": "定位回复按钮时页面结构校验失败",
        "target_not_found": "未能在评论区定位到目标楼层",
    }
    set_last_original_reply_result(
        ok=False,
        reason=final_reason,
        stage="locate_target",
        message=final_message_map.get(final_reason, "未能完成评论下回复"),
        diagnostics=build_reply_diagnostics(
            target=target,
            context_state=last_round_context_state,
            click_result=last_click_result,
            surface_selector=surface_selector,
        ),
    )
    logger.warning(
        f"评论下回复未定位到目标楼层: aweme_id={aweme_id}, comment_id={comment_id}, "
        f"nickname={str((target or {}).get('nickname', '') or '')}"
    )
    return False
