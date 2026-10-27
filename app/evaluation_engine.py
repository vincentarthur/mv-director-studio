#!/usr/bin/env python3
"""Multimodal audio analysis, prompt optimization, and MV Storyline evaluation engine (Tabs #1-#5)."""
from __future__ import annotations

import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import datetime
import json
from pathlib import Path
import threading
import time
import traceback
from typing import Any
import uuid

import app.config as _cfg_mod
from app.config import (
    DATA_DIR,
    DEFAULT_MV_DATASET_SPEC_TXT,
    DEFAULT_MV_RUBRIC_MD,
    DEFAULT_OPTIMIZED_PROMPT,
    DEFAULT_ORIGINAL_AUDIO_PROMPT,
    DEFAULT_RUBRIC_JUDGE_PROMPT_TEMPLATE,
    DEFAULT_STORYLINE_GENERATION_PROMPT,
    DEFAULT_STORYLINE_JUDGE_PROMPT_TEMPLATE,
    MODEL_ARGON_4,
    MODEL_FLASH_38,
    SAMPLES_DIR,
    _BATCH_JOBS,
    _BATCH_LOCK,
    _THREAD_CTX,
    _set_thread_log_ctx,
    load_platform_config,
    log_runtime,
    persist_batch_job_state,
)
from app.storage import (
    TRACK_FILES,
    _parse_gcs_bucket_and_prefix,
    build_media_part_for_gemini,
    call_gemini_model,
    ensure_sample_files_synced,
    guess_media_mime_type,
    load_manifest,
    load_track_store,
    resolve_sample_asset,
    save_sample_to_track_store,
    upload_to_gcs_universal,
)

# ==============================================================================
# 5. Core Single-Sample & Parallel Batch Execution Engine (Uses Config Store)
# ==============================================================================
def build_multimodal_parts_for_sample(
    sample_id: str,
    prompt_text_to_use: str,
    is_unmodified_prompt: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spec = ensure_sample_files_synced(sample_id)
    files_map = spec.get("files", {})
    img_specs = spec.get("images", [])
    img_paths = [img["filename"] for img in img_specs]
    user_prompt = spec.get("prompt_text", "").strip()
    audio_fn = spec.get("audio_filename") or "input_audio_01.mp3"

    parts: list[dict[str, Any]] = [
        build_media_part_for_gemini(
            sample_id,
            audio_fn,
            guess_media_mime_type(audio_fn, "audio/mpeg"),
            file_id=files_map.get(audio_fn) or files_map.get("input_audio_01.mp3", ""),
        )
    ]
    for img in img_specs:
        fn = img["filename"]
        parts.append(
            build_media_part_for_gemini(
                sample_id,
                fn,
                guess_media_mime_type(fn, "image/jpeg"),
                file_id=img.get("file_id") or files_map.get(fn, ""),
            )
        )

    if is_unmodified_prompt:
        if user_prompt:
            parts.append({"text": f"【测试集样本输入 Prompt】：{user_prompt}"})
        parts.append({"text": prompt_text_to_use})
    else:
        dur_sec = spec.get("audio_meta", {}).get("duration_sec", 180.0)
        dur_str = spec.get("audio_meta", {}).get("duration_str", "03:00")
        protagonist_desc = f"双人主角 ({len(img_paths)}张参考图)" if len(img_paths) > 1 else "单人主角 (1张参考图)"
        user_prompt_str = user_prompt if user_prompt else "【无用户提示词 (Zero-Prompt 模式) — 请根据音乐与参考图自主构建 MV 叙事】"
        context_header = (
            f"【当前样本输入元数据（依据《MV测试集说明.txt》）】\n"
            f"- 样本 ID：{sample_id} ({spec.get('title', '')})\n"
            f"- 音频精确总时长：{dur_sec} 秒 ({dur_str})\n"
            f"- 输入参考图数量：{len(img_paths)} 张 ({protagonist_desc})\n"
            f"- 用户输入提示词 (user_prompt)：{user_prompt_str}\n\n"
        )
        parts.append({"text": context_header + prompt_text_to_use})

    return parts, spec


def evaluate_output_against_mv_rubric(
    sample_spec: dict[str, Any],
    analysis_output: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Scores the analysis output using the configured `rubric_judge` model & `rubric_judge_prompt_template`."""
    cfg = load_platform_config()
    judge_cfg = cfg["tab_models"]["rubric_judge"]
    prompts_cfg = cfg["prompts"]

    sid = sample_spec["sample_id"]
    files_map = sample_spec.get("files", {})
    dur_sec = sample_spec.get("audio_meta", {}).get("duration_sec", 180.0)
    dur_str = sample_spec.get("audio_meta", {}).get("duration_str", "03:00")
    img_paths: list[Path] = []
    for img in sample_spec.get("images", []):
        fn = img["filename"]
        resolved_img = resolve_sample_asset(sid, fn, file_id=img.get("file_id") or files_map.get(fn, ""))
        if resolved_img and resolved_img.exists():
            img_paths.append(resolved_img)
    user_prompt = sample_spec.get("prompt_text", "").strip()
    user_prompt_display = user_prompt if user_prompt else "无（Zero-Prompt 模式）"

    template = prompts_cfg.get("rubric_judge_prompt_template", DEFAULT_RUBRIC_JUDGE_PROMPT_TEMPLATE)
    rubric_md = prompts_cfg.get("mv_rubric_md", DEFAULT_MV_RUBRIC_MD)

    judge_prompt = (
        template.replace("{rubric_md}", rubric_md)
        .replace("{sample_id}", sid)
        .replace("{sample_title}", sample_spec.get("title", sid))
        .replace("{audio_duration_sec}", str(dur_sec))
        .replace("{audio_duration_str}", str(dur_str))
        .replace("{num_images}", str(len(img_paths)))
        .replace("{protagonist_mode}", sample_spec.get("protagonist_mode", "single"))
        .replace("{user_prompt_display}", user_prompt_display)
        .replace("{model_output_json}", json.dumps(analysis_output, ensure_ascii=False, indent=2))
    )

    j_parts: list[dict[str, Any]] = []
    for ip in img_paths:
        j_parts.append({"inline_data": {"mime_type": guess_media_mime_type(ip.name, "image/jpeg"), "data": str(ip)}})
    j_parts.append({"text": judge_prompt})

    score_data, score_meta = call_gemini_model(
        parts=j_parts,
        model=judge_cfg.get("model", MODEL_FLASH_38),
        location=judge_cfg.get("location", "global"),
        thinking_level=judge_cfg.get("thinking_level", ""),
        temperature=float(judge_cfg.get("temperature", 0.1)),
        max_output_tokens=int(judge_cfg.get("max_output_tokens", 65535)),
    )
    return score_data, score_meta




def _get_cached_audio_analysis_hint(sample_id: str) -> str:
    """Checks if earlier audio analysis tracks already analyzed this sample and extracts a concise reference summary."""
    for tk in ("track4_argon4_opt", "track4_flash38_opt", "track2_argon4_orig", "track1_flash38_orig"):
        try:
            st = load_track_store(tk)
            rec = st.get("samples", {}).get(sample_id)
            if rec and isinstance(rec.get("analysis_output"), dict):
                ao = rec["analysis_output"]
                summary_part = ao.get("summary", ao)
                return json.dumps(summary_part, ensure_ascii=False, indent=2)
        except Exception:
            continue
    return ""


def build_storyline_parts_for_sample(
    sample_id: str,
    storyline_prompt_text: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    spec = ensure_sample_files_synced(sample_id)
    files_map = spec.get("files", {})
    img_specs = spec.get("images", [])
    img_paths = [img["filename"] for img in img_specs]
    user_prompt = spec.get("prompt_text", "").strip()
    dur_sec = spec.get("audio_meta", {}).get("duration_sec", 180.0)
    dur_str = spec.get("audio_meta", {}).get("duration_str", "03:00")
    protagonist_desc = f"双人主角 ({len(img_paths)}张参考图)" if len(img_paths) > 1 else "单人主角 (1张参考图)"
    user_prompt_str = user_prompt if user_prompt else "【无用户提示词 (Zero-Prompt 模式) — 请完全基于歌曲旋律、歌词与人物参考图气质自主创作最佳 MV Storyline】"

    audio_fn = spec.get("audio_filename") or "input_audio_01.mp3"
    parts: list[dict[str, Any]] = [
        build_media_part_for_gemini(
            sample_id,
            audio_fn,
            guess_media_mime_type(audio_fn, "audio/mpeg"),
            file_id=files_map.get(audio_fn) or files_map.get("input_audio_01.mp3", ""),
        )
    ]
    for img in img_specs:
        fn = img["filename"]
        parts.append(
            build_media_part_for_gemini(
                sample_id,
                fn,
                guess_media_mime_type(fn, "image/jpeg"),
                file_id=img.get("file_id") or files_map.get(fn, ""),
            )
        )

    cached_hint = _get_cached_audio_analysis_hint(sample_id)
    cached_block = (
        f"\n- 已缓存的音频理解打标参考（供辅助对齐，仍请以你直接聆听到的音频细节为准）：\n```json\n{cached_hint}\n```\n"
        if cached_hint
        else ""
    )

    context_header = (
        f"【当前样本输入元数据（依据《MV测试集说明.txt》）】\n"
        f"- 样本 ID：{sample_id} ({spec.get('title', '')})\n"
        f"- 语言与角色模式：{spec.get('language', 'Multilingual')} · {protagonist_desc}\n"
        f"- 音频精确总时长：{dur_sec} 秒 ({dur_str})\n"
        f"- 输入人物参考图数量：{len(img_paths)} 张\n"
        f"- 用户导演提示词 (user_prompt)：{user_prompt_str}\n"
        f"{cached_block}\n"
    )
    parts.append({"text": context_header + storyline_prompt_text})
    return parts, spec


def evaluate_storyline_against_song(
    sample_spec: dict[str, Any],
    storyline_output: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Compares the generated MV Storyline against the original song (`input_audio_01.mp3`), `input_image`, and `prompt.txt`, outputting scores, where to optimize, and why."""
    cfg = load_platform_config()
    judge_cfg = cfg["tab_models"].get("storyline_judge") or cfg["tab_models"]["rubric_judge"]
    prompts_cfg = cfg["prompts"]

    sid = sample_spec["sample_id"]
    files_map = sample_spec.get("files", {})
    dur_sec = sample_spec.get("audio_meta", {}).get("duration_sec", 180.0)
    dur_str = sample_spec.get("audio_meta", {}).get("duration_str", "03:00")
    img_paths: list[Path] = []
    for img in sample_spec.get("images", []):
        fn = img["filename"]
        resolved_img = resolve_sample_asset(sid, fn, file_id=img.get("file_id") or files_map.get(fn, ""))
        if resolved_img and resolved_img.exists():
            img_paths.append(resolved_img)
    user_prompt = sample_spec.get("prompt_text", "").strip()
    user_prompt_display = user_prompt if user_prompt else "无（Zero-Prompt 模式）"

    template = prompts_cfg.get("storyline_judge_prompt_template", DEFAULT_STORYLINE_JUDGE_PROMPT_TEMPLATE)
    rubric_md = prompts_cfg.get("mv_rubric_md", DEFAULT_MV_RUBRIC_MD)

    judge_text = (
        template.replace("{rubric_md}", rubric_md)
        .replace("{sample_id}", sid)
        .replace("{sample_title}", sample_spec.get("title", ""))
        .replace("{audio_duration_sec}", str(dur_sec))
        .replace("{audio_duration_str}", dur_str)
        .replace("{num_images}", str(len(img_paths)))
        .replace("{protagonist_mode}", sample_spec.get("protagonist_mode", "single"))
        .replace("{user_prompt_display}", user_prompt_display)
        .replace("{storyline_output_json}", json.dumps(storyline_output, ensure_ascii=False, indent=2))
    )

    judge_parts: list[dict[str, Any]] = []
    audio_fn = sample_spec.get("audio_filename") or "input_audio_01.mp3"
    audio_path = resolve_sample_asset(sid, audio_fn, file_id=files_map.get(audio_fn) or files_map.get("input_audio_01.mp3", ""))
    if audio_path and audio_path.exists():
        judge_parts.append({"inline_data": {"mime_type": guess_media_mime_type(audio_path.name, "audio/mpeg"), "data": str(audio_path)}})
    for ip in img_paths:
        judge_parts.append({"inline_data": {"mime_type": guess_media_mime_type(ip.name, "image/jpeg"), "data": str(ip)}})
    judge_parts.append({"text": judge_text})

    score_data, score_meta = call_gemini_model(
        parts=judge_parts,
        model=judge_cfg.get("model", MODEL_FLASH_38),
        location=judge_cfg.get("location", "global"),
        thinking_level=judge_cfg.get("thinking_level", ""),
        temperature=float(judge_cfg.get("temperature", 0.1)),
        max_output_tokens=int(judge_cfg.get("max_output_tokens", 65535)),
    )
    return score_data, score_meta


def run_single_sample_in_track(
    track_key: str,
    sample_id: str,
    model_cfg: dict[str, Any],
    prompt_text: str,
    is_unmodified_prompt: bool,
    is_storyline_track: bool = False,
) -> dict[str, Any]:
    t_start = time.perf_counter()
    if is_storyline_track:
        _set_thread_log_ctx(
            job_id=getattr(_THREAD_CTX, "job_id", ""),
            track_key=track_key,
            sample_id=sample_id,
            stage="STORYLINE_ASSET_SYNC",
        )
        log_runtime("INFO", "STORYLINE_ASSET_SYNC", f"正在校验/同步 Sample {sample_id} 音频与参考图素材...")
        parts, spec = build_storyline_parts_for_sample(
            sample_id=sample_id,
            storyline_prompt_text=prompt_text,
        )
        _set_thread_log_ctx(
            job_id=getattr(_THREAD_CTX, "job_id", ""),
            track_key=track_key,
            sample_id=sample_id,
            stage="STORYLINE_GEN",
        )
        analysis_json, analysis_meta = call_gemini_model(
            parts=parts,
            model=model_cfg.get("model", MODEL_ARGON_4),
            location=model_cfg.get("location", "global"),
            thinking_level=model_cfg.get("thinking_level", ""),
            temperature=float(model_cfg.get("temperature", 0.25)),
            max_output_tokens=int(model_cfg.get("max_output_tokens", 65535)),
        )
        _set_thread_log_ctx(
            job_id=getattr(_THREAD_CTX, "job_id", ""),
            track_key=track_key,
            sample_id=sample_id,
            stage="STORYLINE_JUDGE",
        )
        rubric_json, rubric_meta = evaluate_storyline_against_song(
            sample_spec=spec,
            storyline_output=analysis_json,
        )
    else:
        _set_thread_log_ctx(
            job_id=getattr(_THREAD_CTX, "job_id", ""),
            track_key=track_key,
            sample_id=sample_id,
            stage="AUDIO_ASSET_SYNC",
        )
        log_runtime("INFO", "AUDIO_ASSET_SYNC", f"正在校验/同步 Sample {sample_id} 音频与参考图素材...")
        parts, spec = build_multimodal_parts_for_sample(
            sample_id=sample_id,
            prompt_text_to_use=prompt_text,
            is_unmodified_prompt=is_unmodified_prompt,
        )
        _set_thread_log_ctx(
            job_id=getattr(_THREAD_CTX, "job_id", ""),
            track_key=track_key,
            sample_id=sample_id,
            stage="AUDIO_ANALYZE",
        )
        analysis_json, analysis_meta = call_gemini_model(
            parts=parts,
            model=model_cfg.get("model", MODEL_ARGON_4),
            location=model_cfg.get("location", "global"),
            thinking_level=model_cfg.get("thinking_level", ""),
            temperature=float(model_cfg.get("temperature", 0.2)),
            max_output_tokens=int(model_cfg.get("max_output_tokens", 65535)),
        )
        _set_thread_log_ctx(
            job_id=getattr(_THREAD_CTX, "job_id", ""),
            track_key=track_key,
            sample_id=sample_id,
            stage="RUBRIC_JUDGE",
        )
        rubric_json, rubric_meta = evaluate_output_against_mv_rubric(
            sample_spec=spec,
            analysis_output=analysis_json,
        )
    elapsed = round(time.perf_counter() - t_start, 2)
    record = {
        "sample_id": sample_id,
        "sample_title": spec.get("title", ""),
        "track_key": track_key,
        "is_storyline_track": is_storyline_track,
        "status": "completed",
        "model": model_cfg.get("model", ""),
        "location": model_cfg.get("location", "global"),
        "thinking_level": model_cfg.get("thinking_level") or "DEFAULT",
        "temperature": model_cfg.get("temperature", 0.2),
        "is_unmodified_prompt": is_unmodified_prompt,
        "evaluated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_elapsed_sec": elapsed,
        "analysis_telemetry": analysis_meta,
        "judge_telemetry": rubric_meta,
        "analysis_output": analysis_json,
        "rubric_evaluation": rubric_json,
    }
    save_sample_to_track_store(track_key, sample_id, record)
    return record


# Shared _BATCH_JOBS and _BATCH_LOCK imported from app.config


TRACK_KEY_ALIASES = {
    "step1_flash_raw": "track1_flash38_orig",
    "step2_argon_raw": "track2_argon4_orig",
    "step4_opt_flash": "track4_flash38_opt",
    "step4_opt_argon": "track4_argon4_opt",
    "step5_storyline_flash": "track5_storyline_flash38",
    "step5_storyline_argon": "track5_storyline_argon4",
}


def start_parallel_track_job(
    track_key: str,
    sample_ids: list[str],
    override_workers: int | None = None,
    custom_prompt_override: str = "",
) -> str:
    track_key = TRACK_KEY_ALIASES.get(track_key, track_key)
    cfg = load_platform_config()
    tab_models = cfg["tab_models"]
    prompts_cfg = cfg["prompts"]
    is_storyline = False

    if track_key == "track1_flash38_orig":
        model_cfg = tab_models["tab1_baseline"]
        prompt_text = custom_prompt_override.strip() or prompts_cfg.get("original_audio_prompt", DEFAULT_ORIGINAL_AUDIO_PROMPT)
        is_unmodified = True
        track_label = f"#1 {model_cfg['model']} (loc={model_cfg.get('location','global')}, 原提示词)"
    elif track_key == "track2_argon4_orig":
        model_cfg = tab_models["tab2_argon_baseline"]
        prompt_text = custom_prompt_override.strip() or prompts_cfg.get("original_audio_prompt", DEFAULT_ORIGINAL_AUDIO_PROMPT)
        is_unmodified = True
        track_label = f"#2 {model_cfg['model']} (thinking={model_cfg.get('thinking_level') or 'DEFAULT'}, loc={model_cfg.get('location','global')}, 原提示词)"
    elif track_key == "track4_flash38_opt":
        model_cfg = tab_models["tab4_opt_model_a"]
        prompt_text = custom_prompt_override.strip() or prompts_cfg.get("optimized_prompt", DEFAULT_OPTIMIZED_PROMPT)
        is_unmodified = False
        track_label = f"#4A {model_cfg['model']} (loc={model_cfg.get('location','global')}, 优化后提示词)"
    elif track_key == "track4_argon4_opt":
        model_cfg = tab_models["tab4_opt_model_b"]
        prompt_text = custom_prompt_override.strip() or prompts_cfg.get("optimized_prompt", DEFAULT_OPTIMIZED_PROMPT)
        is_unmodified = False
        track_label = f"#4B {model_cfg['model']} (thinking={model_cfg.get('thinking_level') or 'DEFAULT'}, loc={model_cfg.get('location','global')}, 优化后提示词)"
    elif track_key == "track5_storyline_flash38":
        model_cfg = tab_models["tab5_storyline_model_a"]
        prompt_text = custom_prompt_override.strip() or prompts_cfg.get("storyline_generation_prompt", DEFAULT_STORYLINE_GENERATION_PROMPT)
        is_unmodified = False
        is_storyline = True
        track_label = f"#5 S-A {model_cfg['model']} (loc={model_cfg.get('location','global')}, MV Storyline创作+歌曲对比)"
    elif track_key == "track5_storyline_argon4":
        model_cfg = tab_models["tab5_storyline_model_b"]
        prompt_text = custom_prompt_override.strip() or prompts_cfg.get("storyline_generation_prompt", DEFAULT_STORYLINE_GENERATION_PROMPT)
        is_unmodified = False
        is_storyline = True
        track_label = f"#5 S-B {model_cfg['model']} (thinking={model_cfg.get('thinking_level') or 'DEFAULT'}, loc={model_cfg.get('location','global')}, MV Storyline创作+歌曲对比)"
    else:
        raise ValueError(f"Unknown track_key: {track_key}")

    max_workers = int(override_workers or model_cfg.get("max_workers", 5))
    job_id = f"batch_{track_key}_{uuid.uuid4().hex[:6]}"
    per_sample_status = {sid: {"status": "queued", "score": None, "error": "", "traceback": ""} for sid in sample_ids}
    bucket_name, prefix = _parse_gcs_bucket_and_prefix()

    with _BATCH_LOCK:
        _BATCH_JOBS[job_id] = {
            "job_id": job_id,
            "track_key": track_key,
            "track_label": track_label,
            "model": model_cfg.get("model", ""),
            "location": model_cfg.get("location", "global"),
            "thinking_level": model_cfg.get("thinking_level") or "DEFAULT",
            "status": "running",
            "total_samples": len(sample_ids),
            "completed_samples": 0,
            "succeeded_samples": 0,
            "failed_samples": 0,
            "progress_pct": 4,
            "current_step": f"🚀 正在并行执行 {track_label} (共 {len(sample_ids)} 组样本, 并发度={max_workers})...",
            "per_sample": per_sample_status,
            "logs": [],
        }
    persist_batch_job_state(job_id)

    log_runtime(
        "INFO",
        "BATCH_START",
        f"启动批次 {job_id}: {track_label} | 样本={sample_ids} | 并发={max_workers} | Project={cfg['project']['project_id']} | DATA_DIR={DATA_DIR} | GCS={bucket_name or '<无>'}/{prefix}",
        track_key=track_key,
        job_id=job_id,
    )

    def _runner() -> None:
        def _task(sid: str) -> None:
            _set_thread_log_ctx(job_id=job_id, track_key=track_key, sample_id=sid, stage="TASK_START")
            with _BATCH_LOCK:
                _BATCH_JOBS[job_id]["per_sample"][sid]["status"] = "running"
            log_runtime(
                "INFO",
                "SAMPLE_START",
                f"▶ 样本 {sid} 开始分析 ({model_cfg['model']} @ {model_cfg.get('location','global')})...",
                sample_id=sid,
                track_key=track_key,
                job_id=job_id,
            )
            try:
                rec = run_single_sample_in_track(
                    track_key=track_key,
                    sample_id=sid,
                    model_cfg=model_cfg,
                    prompt_text=prompt_text,
                    is_unmodified_prompt=is_unmodified,
                    is_storyline_track=is_storyline,
                )
                sc = rec.get("rubric_evaluation", {}).get(
                    "overall_score_100",
                    rec.get("rubric_evaluation", {}).get("overall_weighted_score", 0.0),
                )
                with _BATCH_LOCK:
                    _BATCH_JOBS[job_id]["per_sample"][sid] = {
                        "status": "completed",
                        "score": sc,
                        "error": "",
                        "traceback": "",
                    }
                    _BATCH_JOBS[job_id]["completed_samples"] += 1
                    _BATCH_JOBS[job_id]["succeeded_samples"] += 1
                    done = _BATCH_JOBS[job_id]["completed_samples"]
                    succ = _BATCH_JOBS[job_id]["succeeded_samples"]
                    fail = _BATCH_JOBS[job_id]["failed_samples"]
                    tot = _BATCH_JOBS[job_id]["total_samples"]
                    _BATCH_JOBS[job_id]["progress_pct"] = int(5 + 95 * done / max(tot, 1))
                    _BATCH_JOBS[job_id]["current_step"] = (
                        f"已处理 {done}/{tot} 组样本 (✅ 成功: {succ} | ❌ 失败: {fail}) | 最新完成 Sample {sid} (得分: {sc})"
                    )
                persist_batch_job_state(job_id)
                log_runtime(
                    "INFO",
                    "SAMPLE_DONE",
                    f"✅ 样本 {sid} 完成！总分: {sc}/100 (总耗时 {rec['total_elapsed_sec']}s)",
                    sample_id=sid,
                    track_key=track_key,
                    job_id=job_id,
                )
            except Exception as exc:
                tb_full = traceback.format_exc()
                err_stage = getattr(_THREAD_CTX, "stage", "UNKNOWN")
                err_rec = {
                    "sample_id": sid,
                    "track_key": track_key,
                    "model": model_cfg.get("model", ""),
                    "location": model_cfg.get("location", "global"),
                    "thinking_level": model_cfg.get("thinking_level") or "DEFAULT",
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error_stage": err_stage,
                    "error": str(exc),
                    "traceback": tb_full,
                    "evaluated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_sample_to_track_store(track_key, sid, err_rec)
                with _BATCH_LOCK:
                    _BATCH_JOBS[job_id]["per_sample"][sid] = {
                        "status": "failed",
                        "score": None,
                        "error_type": type(exc).__name__,
                        "error_stage": err_stage,
                        "error": str(exc),
                        "traceback": tb_full,
                    }
                    _BATCH_JOBS[job_id]["completed_samples"] += 1
                    _BATCH_JOBS[job_id]["failed_samples"] += 1
                    done = _BATCH_JOBS[job_id]["completed_samples"]
                    succ = _BATCH_JOBS[job_id]["succeeded_samples"]
                    fail = _BATCH_JOBS[job_id]["failed_samples"]
                    tot = _BATCH_JOBS[job_id]["total_samples"]
                    _BATCH_JOBS[job_id]["progress_pct"] = int(5 + 95 * done / max(tot, 1))
                    # Never truncate the exception to 80 chars so users can immediately read the full cause!
                    _BATCH_JOBS[job_id]["current_step"] = (
                        f"已处理 {done}/{tot} 组样本 (✅ 成功: {succ} | ❌ 失败: {fail}) | ❌ Sample {sid} [{err_stage}] {type(exc).__name__}: {exc}"
                    )
                persist_batch_job_state(job_id)
                log_runtime(
                    "ERROR",
                    f"SAMPLE_FAIL:{err_stage}",
                    f"❌ 样本 {sid} 执行失败 ({type(exc).__name__}): {exc}",
                    sample_id=sid,
                    track_key=track_key,
                    job_id=job_id,
                    exc=exc,
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_task, sid) for sid in sample_ids]
            concurrent.futures.wait(futures)

        with _BATCH_LOCK:
            succ = _BATCH_JOBS[job_id]["succeeded_samples"]
            fail = _BATCH_JOBS[job_id]["failed_samples"]
            tot = _BATCH_JOBS[job_id]["total_samples"]
            _BATCH_JOBS[job_id]["status"] = "completed" if fail == 0 else ("partial_error" if succ > 0 else "error")
            _BATCH_JOBS[job_id]["progress_pct"] = 100
            if fail == 0:
                _BATCH_JOBS[job_id]["current_step"] = f"🎉 {track_label} 并行测试已全部成功完成 (✅ {succ}/{tot})！"
            else:
                _BATCH_JOBS[job_id]["current_step"] = (
                    f"⚠️ {track_label} 执行完毕：✅ 成功 {succ}/{tot} 组，❌ 失败 {fail}/{tot} 组（请查看下方实时日志框或点击失败样本查看完整诊断堆栈）"
                )
        persist_batch_job_state(job_id)
        log_runtime(
            "INFO" if fail == 0 else "WARNING",
            "BATCH_FINISH",
            f"批次 {job_id} 结束: 成功={succ}/{tot}, 失败={fail}/{tot}",
            track_key=track_key,
            job_id=job_id,
        )

    threading.Thread(target=_runner, daemon=True).start()
    return job_id


