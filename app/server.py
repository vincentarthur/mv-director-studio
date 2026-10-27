#!/usr/bin/env python3
"""FastAPI web server, SHA-256 session authentication middleware, and REST API routes."""
from __future__ import annotations

import base64
import copy
import datetime
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import threading
import traceback
from typing import Any
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app.config import (
    APP_DIR,
    CONFIG_PATH,
    DATA_DIR,
    DEFAULT_MV_DATASET_SPEC_TXT,
    DEFAULT_MV_RUBRIC_MD,
    DEFAULT_OPTIMIZATION_WHY,
    DEFAULT_OPTIMIZED_PROMPT,
    DEFAULT_ORIGINAL_AUDIO_PROMPT,
    DEFAULT_PLATFORM_CONFIG,
    DEFAULT_PROMPT_OPTIMIZER_META_PROMPT,
    DEFAULT_STORYLINE_GENERATION_PROMPT,
    DOCS_DIR,
    MANIFEST_PATH,
    MODEL_ARGON_4,
    MODEL_FLASH_38,
    PROJECT_ROOT,
    RUNTIME_LOG_PATH,
    RUNS_V2_DIR,
    SAMPLES_DIR,
    SNAPSHOTS_DIR,
    UI_HTML_PATH,
    _BATCH_JOBS,
    _BATCH_LOCK,
    _LOG_LOCK,
    _RUNTIME_LOGS,
    _set_thread_log_ctx,
    load_batch_job_state,
    load_platform_config,
    log_runtime,
    persist_batch_job_state,
    save_platform_config,
)
from app.evaluation_engine import (
    TRACK_KEY_ALIASES,
    start_parallel_track_job,
)
from app.storage import (
    TRACK_FILES,
    _LAST_ASSET_RESOLUTION_TRACES,
    _STORE_LOCK,
    _parse_gcs_bucket_and_prefix,
    call_gemini_model,
    discover_storage_samples,
    download_from_gcs_universal,
    get_sample_by_id,
    load_all_persisted_state,
    load_manifest,
    load_track_store,
    resolve_sample_asset,
    save_sample_to_track_store,
    upload_to_gcs_universal,
)

app = FastAPI(title="MV Multimodal Evaluation & Storyline Studio v2.2", version="2.2.0")


@app.middleware("http")
async def add_cors_headers(request: Request, call_next: Any) -> Response:
    if request.method == "OPTIONS":
        resp = Response(status_code=204)
    else:
        resp = await call_next(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp

# ==============================================================================
# 6. FastAPI Routes for V2.1 Platform (Including Full Config & Prompt Management)
# ==============================================================================
def _get_expected_passcode_sha256() -> str:
    """
    Returns the expected 64-char lowercase SHA-256 hex digest of the access passcode:
    - Priority 1: `ACCESS_PASSCODE_SHA256` (Zero-Plaintext mode: Cloud Run only stores the irreversible SHA-256 hash!)
    - Priority 2: `ACCESS_PASSCODE` (mounted from Secret Manager or env, hashed to SHA-256 in memory)
    """
    import hashlib

    raw_hash = (
        os.environ.get("ACCESS_PASSCODE_SHA256")
        or ""
    ).strip().lower()
    if len(raw_hash) == 64 and all(c in "0123456789abcdef" for c in raw_hash):
        return raw_hash
    raw_plain = (
        os.environ.get("ACCESS_PASSCODE")
        or ""
    ).strip()
    if raw_plain:
        return hashlib.sha256(raw_plain.encode("utf-8")).hexdigest().lower()
    return ""


def _compute_session_cookie_token(expected_sha256: str) -> str:
    import hmac
    import hashlib

    return hmac.new(
        key=expected_sha256.encode("utf-8"),
        msg=b"argon_mv_eval_v2_2_authenticated_session",
        digestmod=hashlib.sha256,
    ).hexdigest()


_LOGIN_GATE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>🔒 安全访问验证 — Gemini 4 Argon MV 评测平台 V2.2</title>
  <style>
    body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; background:#090d16; color:#f1f5f9; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
    .gate-card { width:100%; max-width:410px; background:#111827; border:1px solid #26324d; border-radius:14px; padding:28px 26px; box-shadow:0 20px 50px rgba(0,0,0,0.65); }
    .gate-title { font-size:18px; font-weight:700; margin:0 0 6px 0; display:flex; align-items:center; gap:8px; }
    .gate-sub { font-size:12.5px; color:#94a3b8; margin-bottom:20px; line-height:1.5; }
    .gate-input { width:100%; box-sizing:border-box; padding:11px 14px; border-radius:8px; border:1px solid #334155; background:#0b0f19; color:#f8fafc; font-size:14px; outline:none; margin-bottom:14px; }
    .gate-input:focus { border-color:#6366f1; box-shadow:0 0 0 2px rgba(99,102,241,0.25); }
    .gate-btn { width:100%; padding:11px; border-radius:8px; border:none; background:linear-gradient(135deg,#6366f1,#4f46e5); color:#fff; font-weight:600; font-size:14px; cursor:pointer; }
    .gate-btn:hover { opacity:0.95; }
    .gate-err { color:#f87171; font-size:12px; margin-top:10px; display:none; }
    .gate-note { color:#64748b; font-size:11px; margin-top:16px; border-top:1px dashed #1e293b; padding-top:12px; line-height:1.45; }
  </style>
</head>
<body>
  <div class="gate-card">
    <h1 class="gate-title">🔒 Gemini 4 Argon MV 评测平台</h1>
    <div class="gate-sub">当前环境已开启零明文 SHA-256 访问门禁保护。请输入访问口令以解锁工作台：</div>
    <form onsubmit="submitLogin(event)">
      <input type="password" id="passcode-input" class="gate-input" placeholder="请输入访问口令 (输入自动掩码 & SHA-256 加密)" autofocus required />
      <button type="submit" class="gate-btn" id="login-btn">🔐 验证并进入平台</button>
      <div class="gate-err" id="login-err"></div>
    </form>
    <div class="gate-note">
      🛡️ <b>零明文安全机制</b>：您的口令在浏览器端通过 Web Crypto SHA-256 哈希后再经 HTTPS POST 传输，地址栏、访问日志与服务端环境变量均不保存明文密码。
    </div>
  </div>
  <script>
    async function sha256Hex(str) {
      if (window.crypto && window.crypto.subtle) {
        const buf = await window.crypto.subtle.digest('SHA-256', new TextEncoder().encode(str));
        return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
      }
      return '';
    }
    async function submitLogin(e) {
      e.preventDefault();
      const raw = document.getElementById('passcode-input').value.trim();
      const errEl = document.getElementById('login-err');
      const btn = document.getElementById('login-btn');
      errEl.style.display = 'none';
      btn.disabled = true;
      btn.textContent = '⏳ 正在加密验证...';
      try {
        const digest = await sha256Hex(raw);
        const payload = digest ? { passcode_sha256: digest } : { passcode: raw };
        const resp = await fetch('/api/v2/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await resp.json();
        if (resp.ok && data.ok) {
          window.location.replace('/');
        } else {
          errEl.textContent = '❌ ' + (data.detail || '访问口令错误，请重试');
          errEl.style.display = 'block';
          btn.disabled = false;
          btn.textContent = '🔐 验证并进入平台';
        }
      } catch (err) {
        errEl.textContent = '❌ 请求异常: ' + err.message;
        errEl.style.display = 'block';
        btn.disabled = false;
        btn.textContent = '🔐 验证并进入平台';
      }
    }
  </script>
</body>
</html>
"""


@app.post("/api/v2/auth/login")
async def api_v2_auth_login(request: Request) -> JSONResponse:
    """Zero-plaintext login endpoint: compares SHA-256 digest in constant time (`hmac.compare_digest`) and sets HttpOnly session cookie."""
    import hashlib
    import hmac

    expected_sha256 = _get_expected_passcode_sha256()
    if not expected_sha256:
        return JSONResponse({"ok": True, "mode": "open"})

    body = await request.json()
    client_sha256 = str(body.get("passcode_sha256", "")).strip().lower()
    if not client_sha256:
        raw_pass = str(body.get("passcode", "")).strip()
        if raw_pass:
            client_sha256 = hashlib.sha256(raw_pass.encode("utf-8")).hexdigest().lower()

    if not client_sha256 or not hmac.compare_digest(client_sha256, expected_sha256):
        return JSONResponse({"ok": False, "detail": "访问口令不正确 (Invalid passcode)"}, status_code=401)

    session_tok = _compute_session_cookie_token(expected_sha256)
    resp = JSONResponse({"ok": True})
    is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    resp.set_cookie(
        "argon_auth_session",
        session_tok,
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=86400 * 7,
    )
    return resp


@app.post("/api/v2/auth/logout")
async def api_v2_auth_logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("argon_auth_session")
    return resp


@app.middleware("http")
async def optional_passcode_auth_middleware(request: Request, call_next: Any) -> Response:
    """
    Zero-Plaintext Access Gate (`ACCESS_PASSCODE_SHA256` or `ACCESS_PASSCODE`):
    - When unset/empty (default): relies on Cloud Run IAM (`--no-allow-unauthenticated`) or IAP.
    - When set: verifies HMAC-signed `HttpOnly` session cookie.
    - Unauthenticated browser requests see a masked password login card (`_LOGIN_GATE_HTML`) that hashes the passcode with SHA-256 in the browser before POSTing to `/api/v2/auth/login` (never exposing plaintext in the URL or server env!).
    - If a query parameter `?passcode=...` or `?token=...` is used, immediately validates and issues a `303 See Other` redirect to `/` so the parameter is stripped from the browser address bar.
    """
    import hashlib
    import hmac

    expected_sha256 = _get_expected_passcode_sha256()
    if not expected_sha256 or request.url.path in ("/favicon.ico", "/api/v2/auth/login", "/api/v2/auth/logout"):
        return await call_next(request)

    expected_cookie = _compute_session_cookie_token(expected_sha256)

    # 1. Check existing HMAC-signed HttpOnly session cookie
    cur_cookie = request.cookies.get("argon_auth_session", "")
    if cur_cookie and hmac.compare_digest(cur_cookie, expected_cookie):
        # If URL still had ?passcode or ?token, strip it immediately via 303 redirect
        if "passcode" in request.query_params or "token" in request.query_params:
            return RedirectResponse(url=request.url.path, status_code=303)
        return await call_next(request)

    # 2. Optional one-time token/passcode query param -> validate & 303 redirect to strip URL param immediately
    q_val = (request.query_params.get("token") or request.query_params.get("passcode") or "").strip()
    if q_val:
        q_hash = q_val.lower() if (len(q_val) == 64 and all(c in "0123456789abcdef" for c in q_val.lower())) else hashlib.sha256(q_val.encode("utf-8")).hexdigest().lower()
        if hmac.compare_digest(q_hash, expected_sha256):
            is_https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
            redir = RedirectResponse(url=request.url.path, status_code=303)
            redir.set_cookie("argon_auth_session", expected_cookie, httponly=True, secure=is_https, samesite="lax", max_age=86400 * 7)
            return redir

    # 3. For API calls, return 401 JSON; for browser page visits, render the masked SHA-256 Login Gate HTML
    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "detail": "Unauthorized: Please log in first."}, status_code=401)
    return HTMLResponse(_LOGIN_GATE_HTML, status_code=401)


@app.get("/favicon.ico")
async def api_favicon() -> Response:
    return Response(status_code=204)


def build_normalized_ui_state() -> dict[str, Any]:
    manifest = copy.deepcopy(load_manifest())
    for s in manifest:
        lang = s.get("language", "Multilingual")
        pmode = "双人主角" if s.get("protagonist_mode") == "dual" else "单人主角"
        s["genre"] = f"{lang} · {pmode}"
        tags = s.get("stress_tags", [])
        ptxt = (s.get("prompt_text") or "").strip()
        s["testing_focus"] = " · ".join(tags) if tags else (ptxt[:80] if ptxt else "Zero-Prompt 音乐驱动自主叙事")
        s["description"] = ptxt if ptxt else "无用户提示词 (Zero-Prompt 模式)"
        dur_str = s.get("audio_meta", {}).get("duration_str", "03:00")
        audio_fn = s.get("audio_filename") or "input_audio_01.mp3"
        cover_fn = s.get("cover_filename") or "cover.png"
        s["audio_file"] = f"{audio_fn} ({dur_str})"
        s["audio_url"] = f"/api/media/{s['sample_id']}/{audio_fn}"
        s["cover_url"] = f"/api/media/{s['sample_id']}/{cover_fn}"
        imgs = s.get("images", [])
        s["image_count"] = len(imgs)
        for img in imgs:
            img["name"] = img.get("filename", "image.jpeg")
            img["url"] = f"/api/media/{s['sample_id']}/{img['name']}"

    cfg = load_platform_config()
    persisted = load_all_persisted_state()
    raw_tracks = persisted.get("tracks", {})

    key_map = {
        "step1_flash_raw": "track1_flash38_orig",
        "step2_argon_raw": "track2_argon4_orig",
        "step4_opt_flash": "track4_flash38_opt",
        "step4_argon_raw": "track4_argon4_opt",
        "step4_opt_argon": "track4_argon4_opt",
        "step5_storyline_flash": "track5_storyline_flash38",
        "step5_storyline_argon": "track5_storyline_argon4",
    }
    ui_tracks: dict[str, Any] = {}
    summaries: dict[str, Any] = {}

    for ui_key, backend_key in key_map.items():
        t_store = copy.deepcopy(raw_tracks.get(backend_key, {"samples": {}}))
        samples_map = t_store.get("samples", {})
        overall_list: list[float] = []
        l1_list: list[float] = []
        l2_list: list[float] = []
        l3_list: list[float] = []

        for sid, rec in samples_map.items():
            if "status" not in rec:
                rec["status"] = "completed" if rec.get("rubric_evaluation") else "error"
            at = rec.get("analysis_telemetry", {})
            jt = rec.get("judge_telemetry", {})
            rec["analysis_meta"] = {
                "used_model": at.get("model_version") or rec.get("model", ""),
                "thinking_level": rec.get("thinking_level", ""),
                "latency_sec": at.get("latency_sec", 0.0),
            }
            rec["judge_meta"] = {"latency_sec": jt.get("latency_sec", 0.0)}
            rub = rec.get("rubric_evaluation", {})
            if rub:
                ov = float(rub.get("overall_score_100", rub.get("overall_weighted_score", 0.0)) or 0.0)
                ls = rub.get("layer_scores", {})
                l1 = float(ls.get("layer1_stability_100", ls.get("layer1_music_lyric_emotion_fit_100", 0.0)) or 0.0)
                l2 = float(ls.get("layer2_music_expression_100", ls.get("layer2_narrative_progression_sync_100", 0.0)) or 0.0)
                l3 = float(ls.get("layer3_aesthetics_memorability_100", ls.get("layer3_shootability_constraint_motif_100", 0.0)) or 0.0)
                rub["overall_weighted_score"] = ov
                rub["executive_verdict"] = (
                    rub.get("overall_verdict")
                    or rub.get("song_vs_storyline_comparison_verdict")
                    or rub.get("executive_verdict", "")
                )
                rub.setdefault("layer1_music_understanding", {"layer_score_100": l1})
                rub.setdefault("layer2_visual_narrative", {"layer_score_100": l2})
                rub.setdefault("layer3_production_feasibility", {"layer_score_100": l3})
                if ov > 0:
                    overall_list.append(ov)
                    l1_list.append(l1)
                    l2_list.append(l2)
                    l3_list.append(l3)

        ui_tracks[ui_key] = t_store
        ui_tracks[backend_key] = t_store
        cnt = len(overall_list)
        summaries[ui_key] = {
            "completed_count": cnt,
            "avg_overall": round(sum(overall_list) / cnt, 1) if cnt else 0.0,
            "avg_l1": round(sum(l1_list) / cnt, 1) if cnt else 0.0,
            "avg_l2": round(sum(l2_list) / cnt, 1) if cnt else 0.0,
            "avg_l3": round(sum(l3_list) / cnt, 1) if cnt else 0.0,
        }
        summaries[backend_key] = summaries[ui_key]

    st3_raw = copy.deepcopy(raw_tracks.get("step3_prompt_opt", {}))
    why_data = st3_raw.get("why_data") or DEFAULT_OPTIMIZATION_WHY
    reasons = why_data.get("six_core_reasons") or DEFAULT_OPTIMIZATION_WHY["six_core_reasons"]
    step3_ui = {
        "optimized_prompt": cfg["prompts"].get("optimized_prompt") or st3_raw.get("optimized_prompt") or DEFAULT_OPTIMIZED_PROMPT,
        "optimization_summary": why_data.get("executive_summary", DEFAULT_OPTIMIZATION_WHY["executive_summary"]),
        "original_prompt_weaknesses": [r.get("original_gap", "") for r in reasons],
        "gemini_specific_adaptations": [r.get("gemini_trait", "") for r in reasons],
        "intent_preservation_notes": [r.get("rubric_impact", "") for r in reasons],
        "change_rationale": [
            {
                "module": r.get("title", ""),
                "before": r.get("original_gap", ""),
                "after": r.get("why_change", ""),
                "why": f"【Gemini 特性】{r.get('gemini_trait', '')} ｜ 【评判维度提升】{r.get('rubric_impact', '')}",
            }
            for r in reasons
        ],
        "_meta": {
            "used_model": why_data.get("optimized_by_model", "gemini-4-argon (thinking_level=MEDIUM)"),
            "updated_at": st3_raw.get("updated_at", cfg.get("updated_at", "")),
        },
        "why_data": why_data,
    }

    snaps_ui = []
    for sn in persisted.get("snapshots", []):
        sc = sn.get("summary_counts", {})
        snaps_ui.append({
            "filename": sn.get("filename", ""),
            "label": sn.get("label", ""),
            "created_at": sn.get("created_at", ""),
            "track_counts": {
                "step1_flash_raw": sc.get("track1_flash38_orig", sc.get("step1_flash_raw", 0)),
                "step2_argon_raw": sc.get("track2_argon4_orig", sc.get("step2_argon_raw", 0)),
                "step4_opt_flash": sc.get("track4_flash38_opt", sc.get("step4_opt_flash", 0)),
                "step4_opt_argon": sc.get("track4_argon4_opt", sc.get("step4_opt_argon", 0)),
                "step5_storyline_flash": sc.get("track5_storyline_flash38", sc.get("step5_storyline_flash", 0)),
                "step5_storyline_argon": sc.get("track5_storyline_argon4", sc.get("step5_storyline_argon", 0)),
            },
        })

    return {
        "samples": manifest,
        "config": cfg,
        "default_config": DEFAULT_PLATFORM_CONFIG,
        "raw_audio_prompt": cfg["prompts"].get("original_audio_prompt", DEFAULT_ORIGINAL_AUDIO_PROMPT),
        "tracks": ui_tracks,
        "summaries": summaries,
        "step3_prompt_opt": step3_ui,
        "snapshots": snaps_ui,
        "persisted": persisted,
    }


@app.get("/api/v2/state")
async def api_v2_state() -> JSONResponse:
    return JSONResponse(build_normalized_ui_state())


@app.post("/api/v2/config/save")
async def api_v2_save_config(request: Request) -> JSONResponse:
    """Saves updated project settings, per-tab model configs, and/or any of the 6 prompts."""
    body = await request.json()
    cfg = load_platform_config()
    if isinstance(body.get("project"), dict):
        cfg["project"].update(body["project"])
    if isinstance(body.get("tab_models"), dict):
        for k, v in body["tab_models"].items():
            if k in cfg["tab_models"] and isinstance(v, dict):
                cfg["tab_models"][k].update(v)
            elif isinstance(v, dict):
                cfg["tab_models"][k] = v
    if isinstance(body.get("prompts"), dict):
        for pk, pv in body["prompts"].items():
            if isinstance(pv, str):
                cfg["prompts"][pk] = pv
    saved = save_platform_config(cfg)
    return JSONResponse({"ok": True, "status": "saved", "config": saved})


@app.post("/api/v2/config/reset")
async def api_v2_reset_config(request: Request) -> JSONResponse:
    """Resets either a specific prompt/section or the entire platform config back to factory defaults."""
    body = await request.json()
    scope = body.get("scope") or body.get("section") or "all"
    if isinstance(scope, str) and scope.startswith("prompt:"):
        scope = scope.split("prompt:", 1)[1]
    cfg = load_platform_config()
    if scope == "all":
        cfg = copy.deepcopy(DEFAULT_PLATFORM_CONFIG)
    elif scope in ("project_and_models", "tab_models"):
        cfg["project"] = copy.deepcopy(DEFAULT_PLATFORM_CONFIG["project"])
        cfg["tab_models"] = copy.deepcopy(DEFAULT_PLATFORM_CONFIG["tab_models"])
    elif scope == "prompts":
        cfg["prompts"] = copy.deepcopy(DEFAULT_PLATFORM_CONFIG["prompts"])
    elif scope in DEFAULT_PLATFORM_CONFIG["prompts"]:
        cfg["prompts"][scope] = DEFAULT_PLATFORM_CONFIG["prompts"][scope]
    saved = save_platform_config(cfg)
    return JSONResponse({"ok": True, "status": "reset", "scope": scope, "config": saved})


@app.post("/api/v2/run_track")
async def api_v2_run_track(request: Request) -> JSONResponse:
    body = await request.json()
    raw_track_key = body.get("track_key", "track1_flash38_orig")
    track_key = TRACK_KEY_ALIASES.get(raw_track_key, raw_track_key)
    force_rerun = bool(body.get("force_rerun", False))
    all_sids = [item["sample_id"] for item in load_manifest()]

    if body.get("sample_ids"):
        sample_ids = list(body["sample_ids"])
    elif not force_rerun and track_key in TRACK_FILES:
        existing_samples = load_track_store(track_key).get("samples", {})
        pending_sids = [
            sid for sid in all_sids
            if not existing_samples.get(sid) or existing_samples[sid].get("status") == "error" or not existing_samples[sid].get("rubric_evaluation")
        ]
        sample_ids = pending_sids if pending_sids else all_sids
    else:
        sample_ids = all_sids

    max_workers = body.get("max_workers")
    custom_prompt = body.get("custom_prompt", "")

    if track_key == "track4_both_opt":
        j1 = start_parallel_track_job("track4_flash38_opt", sample_ids, override_workers=max_workers, custom_prompt_override=custom_prompt)
        j2 = start_parallel_track_job("track4_argon4_opt", sample_ids, override_workers=max_workers, custom_prompt_override=custom_prompt)
        return JSONResponse({"job_id": j2, "secondary_job_id": j1, "status": "running"})

    if track_key in ("track5_storyline_both", "step5_storyline_both"):
        j1 = start_parallel_track_job("track5_storyline_flash38", sample_ids, override_workers=max_workers, custom_prompt_override=custom_prompt)
        j2 = start_parallel_track_job("track5_storyline_argon4", sample_ids, override_workers=max_workers, custom_prompt_override=custom_prompt)
        return JSONResponse({"job_id": j2, "secondary_job_id": j1, "status": "running"})

    job_id = start_parallel_track_job(
        track_key=track_key,
        sample_ids=sample_ids,
        override_workers=max_workers,
        custom_prompt_override=custom_prompt,
    )
    return JSONResponse({"job_id": job_id, "track_key": track_key, "status": "running"})


@app.get("/api/v2/job/{job_id}")
async def api_v2_job_status(job_id: str) -> JSONResponse:
    st = load_batch_job_state(job_id)
    if not st:
        raise HTTPException(status_code=404, detail="Job not found")
    out = dict(st)
    out["total"] = out.get("total_samples", 10)
    out["completed"] = out.get("completed_samples", 0)
    return JSONResponse(out)


@app.post("/api/v2/optimize_prompt")
async def api_v2_optimize_prompt(request: Request) -> JSONResponse:
    """Requirement #3: Uses configured `tab3_prompt_optimizer` model & `prompt_optimizer_meta_prompt` to optimize the prompt."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    cfg = load_platform_config()
    opt_model_cfg = cfg["tab_models"]["tab3_prompt_optimizer"]
    prompts_cfg = cfg["prompts"]

    meta_template = body.get("meta_prompt") or prompts_cfg.get("prompt_optimizer_meta_prompt", DEFAULT_PROMPT_OPTIMIZER_META_PROMPT)
    orig_prompt = body.get("original_audio_prompt") or prompts_cfg.get("original_audio_prompt", DEFAULT_ORIGINAL_AUDIO_PROMPT)
    ds_spec = prompts_cfg.get("mv_dataset_spec", DEFAULT_MV_DATASET_SPEC_TXT)
    rub_md = prompts_cfg.get("mv_rubric_md", DEFAULT_MV_RUBRIC_MD)

    meta_prompt = (
        meta_template.replace("{{ORIGINAL_PROMPT}}", orig_prompt)
        .replace("{{DATASET_SPEC}}", ds_spec)
        .replace("{{RUBRIC_MD}}", rub_md)
        .replace("{original_prompt}", orig_prompt)
        .replace("{dataset_spec}", ds_spec)
        .replace("{rubric_md}", rub_md)
    )
    parsed, telemetry = call_gemini_model(
        parts=[{"text": meta_prompt}],
        model=opt_model_cfg.get("model", MODEL_ARGON_4),
        location=opt_model_cfg.get("location", "global"),
        thinking_level=opt_model_cfg.get("thinking_level", "MEDIUM"),
        temperature=float(opt_model_cfg.get("temperature", 0.2)),
        max_output_tokens=int(opt_model_cfg.get("max_output_tokens", 65535)),
    )
    opt_prompt = parsed.get("optimized_prompt", DEFAULT_OPTIMIZED_PROMPT)
    why_data = {
        "optimized_by_model": f"{telemetry.get('model_version', opt_model_cfg['model'])} (thinking={telemetry.get('thinking_level')}, loc={telemetry.get('location')})",
        "telemetry": telemetry,
        "executive_summary": parsed.get("executive_summary", DEFAULT_OPTIMIZATION_WHY["executive_summary"]),
        "six_core_reasons": parsed.get("six_core_reasons", DEFAULT_OPTIMIZATION_WHY["six_core_reasons"]),
    }
    # Persist both into `step3_prompt_opt.json` AND `platform_config.json`
    cfg["prompts"]["optimized_prompt"] = opt_prompt
    save_platform_config(cfg)
    store = {
        "updated_at": cfg["updated_at"],
        "optimized_prompt": opt_prompt,
        "why_data": why_data,
    }
    TRACK_FILES["step3_prompt_opt"].write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse(store)


@app.post("/api/v2/save_optimized_prompt")
async def api_v2_save_optimized_prompt(request: Request) -> JSONResponse:
    body = await request.json()
    new_prompt = body.get("optimized_prompt", "").strip()
    if not new_prompt:
        raise HTTPException(status_code=400, detail="optimized_prompt cannot be empty")
    cfg = load_platform_config()
    cfg["prompts"]["optimized_prompt"] = new_prompt
    save_platform_config(cfg)
    return JSONResponse({"status": "saved", "updated_at": cfg["updated_at"]})


@app.post("/api/v2/save_storyline_prompt")
async def api_v2_save_storyline_prompt(request: Request) -> JSONResponse:
    body = await request.json()
    new_prompt = body.get("storyline_generation_prompt", "").strip()
    if not new_prompt:
        raise HTTPException(status_code=400, detail="storyline_generation_prompt cannot be empty")
    cfg = load_platform_config()
    cfg["prompts"]["storyline_generation_prompt"] = new_prompt
    save_platform_config(cfg)
    return JSONResponse({"status": "saved", "updated_at": cfg["updated_at"]})


@app.post("/api/v2/snapshots/save")
async def api_v2_save_snapshot(request: Request) -> JSONResponse:
    body = await request.json()
    label = body.get("label", "Manual Review Snapshot").strip() or "Snapshot"
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^a-zA-Z0-9_\u4e00-\u9fa5-]+", "_", label)[:40]
    fname = f"snapshot_{ts}_{safe_label}.json"
    state = load_all_persisted_state()
    tracks = state["tracks"]
    payload = {
        "filename": fname,
        "label": label,
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": load_platform_config(),
        "summary_counts": {
            k: len(v.get("samples", {}))
            for k, v in tracks.items()
            if k != "step3_prompt_opt"
        },
        "tracks": tracks,
    }
    (SNAPSHOTS_DIR / fname).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"status": "saved", "filename": fname, "created_at": payload["created_at"]})


@app.post("/api/v2/snapshots/load")
async def api_v2_load_snapshot(request: Request) -> JSONResponse:
    body = await request.json()
    fname = body.get("filename", "")
    sp = SNAPSHOTS_DIR / fname
    if not sp.exists():
        raise HTTPException(status_code=404, detail=f"Snapshot {fname} not found")
    data = json.loads(sp.read_text(encoding="utf-8"))
    if isinstance(data.get("config"), dict):
        save_platform_config(data["config"])
    tracks = data.get("tracks", {})
    with _STORE_LOCK:
        for k, v in tracks.items():
            if k in TRACK_FILES:
                TRACK_FILES[k].write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"status": "reloaded", "filename": fname, "persisted": load_all_persisted_state()})


@app.post("/api/v2/import_bundle")
async def api_v2_import_bundle(request: Request) -> JSONResponse:
    body = await request.json()
    if isinstance(body.get("bundle"), dict):
        body = body["bundle"]
    if isinstance(body.get("config"), dict):
        save_platform_config(body["config"])
    tracks = body.get("tracks") or body.get("persisted", {}).get("tracks") or {}
    if not tracks:
        raise HTTPException(status_code=400, detail="Invalid JSON bundle: missing 'tracks' key")
    with _STORE_LOCK:
        for k, v in tracks.items():
            if k in TRACK_FILES:
                TRACK_FILES[k].write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")
    return JSONResponse({"ok": True, "status": "imported", "persisted": load_all_persisted_state()})


@app.get("/api/v2/export_bundle")
async def api_v2_export_bundle() -> Response:
    bundle = {
        "exported_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config": load_platform_config(),
        "persisted": load_all_persisted_state(),
    }
    raw = json.dumps(bundle, ensure_ascii=False, indent=2)
    return Response(
        content=raw,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="argon_mv_eval_v2_bundle.json"'},
    )


@app.get("/api/media/{sample_id}/{filename}")
async def api_serve_media(sample_id: str, filename: str) -> Response:
    fid = ""
    try:
        spec = get_sample_by_id(sample_id)
        fid = spec.get("files", {}).get(filename, "")
    except Exception:
        pass
    fpath = resolve_sample_asset(sample_id, filename, file_id=fid)
    if fpath.exists() and fpath.stat().st_size > 0:
        return FileResponse(fpath)
    if fid:
        if filename.lower().endswith((".jpeg", ".jpg", ".png", ".webp")):
            return RedirectResponse(url=f"https://drive.google.com/thumbnail?id={fid}&sz=w1200", status_code=307)
        return RedirectResponse(url=f"https://drive.google.com/uc?export=download&id={fid}", status_code=307)
    raise HTTPException(status_code=404, detail="Media file not found")


@app.post("/api/v2/samples/upload")
async def api_v2_upload_sample_asset(request: Request) -> JSONResponse:
    """Allows uploading or replacing sample audio/image/prompt assets directly via HTTP JSON (base64) when assets are not on GCS or Drive."""
    body = await request.json()
    sample_id = str(body.get("sample_id", "01")).strip()
    filename = str(body.get("filename", "")).strip()
    b64_data = str(body.get("data_b64", "")).strip()
    if not filename or not b64_data:
        raise HTTPException(status_code=400, detail="filename and data_b64 are required")
    raw = base64.b64decode(b64_data)
    dest = SAMPLES_DIR / sample_id / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    if _parse_gcs_bucket_and_prefix()[0]:
        upload_to_gcs_universal(f"samples/{sample_id}/{filename}", raw, content_type="application/octet-stream")
    log_runtime("INFO", "BROWSER_UPLOAD", f"用户通过浏览器上传了样本 {sample_id}/{filename} ({len(raw)} bytes) -> {dest}", sample_id=sample_id)
    return JSONResponse({"ok": True, "sample_id": sample_id, "filename": filename, "size_bytes": len(raw)})


def _inspect_environment_and_storage() -> dict[str, Any]:
    cfg = load_platform_config()
    proj = cfg.get("project", {})
    bucket_name, prefix = _parse_gcs_bucket_and_prefix()
    discovered = discover_storage_samples(force_refresh=True)
    manifest = load_manifest(force_refresh=True)
    sample_checks: list[dict[str, Any]] = []
    ready_audio_count = 0

    for item in manifest:
        sid = item["sample_id"]
        files_spec = item.get("files", {})
        disc_entry = discovered.get(sid, {})
        disc_gcs_blobs = disc_entry.get("gcs_blobs", {})
        audio_fn = item.get("audio_filename") or "input_audio_01.mp3"
        file_status: dict[str, Any] = {}
        expected_fns = [audio_fn] + [im["filename"] for im in item.get("images", [])] + (["prompt.txt"] if item.get("has_prompt") else [])
        for fn in expected_fns:
            found_path = ""
            found_size = 0
            for cand in (
                SAMPLES_DIR / sid / fn,
                DATA_DIR / "argon_eval_data" / "samples" / sid / fn,
                APP_DIR / "argon_eval_data" / "samples" / sid / fn,
                APP_DIR / "samples" / sid / fn,
                Path("/tmp/argon_eval_data/samples") / sid / fn,
            ):
                if cand.exists():
                    found_path = str(cand)
                    found_size = cand.stat().st_size
                    break
            local_ok = bool(found_path and (fn == "prompt.txt" or found_size > 128))
            gcs_blob_path = disc_gcs_blobs.get(fn, "")
            file_status[fn] = {
                "local_exists": local_ok,
                "local_path": found_path,
                "size_bytes": found_size,
                "gcs_blob": gcs_blob_path,
                "available_in_storage": bool(local_ok or gcs_blob_path),
                "drive_file_id": files_spec.get(fn, ""),
            }
        audio_ready = bool(file_status.get(audio_fn, {}).get("available_in_storage"))
        if audio_ready:
            ready_audio_count += 1
        sample_checks.append({
            "sample_id": sid,
            "title": item.get("title", ""),
            "discovered_from_storage": bool(item.get("discovered_from_storage")),
            "audio_filename": audio_fn,
            "images": [im["filename"] for im in item.get("images", [])],
            "audio_ready_locally": file_status.get(audio_fn, {}).get("local_exists", False),
            "audio_available_in_storage": audio_ready,
            "files": file_status,
            "last_resolution_trace": _LAST_ASSET_RESOLUTION_TRACES.get(f"{sid}/{audio_fn}", [])
            or _LAST_ASSET_RESOLUTION_TRACES.get(f"{sid}/input_audio_01.mp3", []),
        })

    try:
        data_dir_listing = [p.name for p in DATA_DIR.iterdir()][:30] if DATA_DIR.exists() else []
    except Exception:
        data_dir_listing = []
    try:
        samples_dir_listing = sorted([p.name for p in SAMPLES_DIR.iterdir()])[:30] if SAMPLES_DIR.exists() else []
    except Exception:
        samples_dir_listing = []

    return {
        "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "app_dir": str(APP_DIR),
        "data_dir": str(DATA_DIR),
        "samples_dir": str(SAMPLES_DIR),
        "runs_v2_dir": str(RUNS_V2_DIR),
        "runtime_log_path": str(RUNTIME_LOG_PATH),
        "data_dir_listing": data_dir_listing,
        "samples_dir_listing": samples_dir_listing,
        "env_vars": {
            "GOOGLE_CLOUD_PROJECT": os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
            "GOOGLE_CLOUD_LOCATION": os.environ.get("GOOGLE_CLOUD_LOCATION", ""),
            "EVAL_DATA_DIR": os.environ.get("EVAL_DATA_DIR", ""),
            "ASSET_STORAGE_MODE": os.environ.get("ASSET_STORAGE_MODE", ""),
            "GCS_BUCKET": os.environ.get("GCS_BUCKET", ""),
            "GCS_PREFIX": os.environ.get("GCS_PREFIX", ""),
            "K_SERVICE": os.environ.get("K_SERVICE", ""),
            "K_REVISION": os.environ.get("K_REVISION", ""),
        },
        "effective_storage": {
            "project_id": proj.get("project_id", ""),
            "default_location": proj.get("default_location", "global"),
            "asset_storage_mode": proj.get("asset_storage_mode", "auto"),
            "gcs_bucket": bucket_name,
            "gcs_prefix": prefix,
            "ready_audio_samples_count": f"{ready_audio_count}/{len(manifest)}",
        },
        "samples_check": sample_checks,
    }


@app.post("/api/v2/samples/rescan")
async def api_v2_samples_rescan() -> JSONResponse:
    """Forces a fresh scan of GCS Bucket and local/FUSE sample directories, bypassing samples_manifest.json file list."""
    manifest = load_manifest(force_refresh=True)
    return JSONResponse({
        "ok": True,
        "sample_count": len(manifest),
        "samples": [
            {
                "sample_id": s["sample_id"],
                "title": s.get("title", ""),
                "discovered_from_storage": bool(s.get("discovered_from_storage")),
                "audio_filename": s.get("audio_filename", "input_audio_01.mp3"),
                "images": [im["filename"] for im in s.get("images", [])],
                "has_prompt": bool(s.get("has_prompt")),
            }
            for s in manifest
        ],
    })


@app.get("/api/v2/debug/logs")
async def api_v2_debug_logs(limit: int = 250) -> JSONResponse:
    """Returns structured runtime logs, active/recent batch jobs, and a full storage/environment diagnostic report."""
    with _LOG_LOCK:
        recent_logs = list(_RUNTIME_LOGS[-max(1, min(limit, 1000)) :])
    with _BATCH_LOCK:
        jobs_summary = {
            jid: {
                "job_id": j["job_id"],
                "track_key": j["track_key"],
                "track_label": j.get("track_label", ""),
                "status": j["status"],
                "total_samples": j.get("total_samples", 10),
                "completed_samples": j.get("completed_samples", 0),
                "succeeded_samples": j.get("succeeded_samples", 0),
                "failed_samples": j.get("failed_samples", 0),
                "current_step": j.get("current_step", ""),
                "logs": j.get("logs", [])[-80:],
                "per_sample": j.get("per_sample", {}),
            }
            for jid, j in _BATCH_JOBS.items()
        }
    return JSONResponse({
        "ok": True,
        "diagnostics": _inspect_environment_and_storage(),
        "runtime_logs": recent_logs,
        "batch_jobs": jobs_summary,
    })


@app.post("/api/v2/debug/clear_logs")
async def api_v2_debug_clear_logs() -> JSONResponse:
    with _LOG_LOCK:
        _RUNTIME_LOGS.clear()
        try:
            if RUNTIME_LOG_PATH.exists():
                RUNTIME_LOG_PATH.write_text("", encoding="utf-8")
        except OSError:
            pass
    log_runtime("INFO", "LOG_CLEAR", "运行日志已被手动清空")
    return JSONResponse({"ok": True})


@app.get("/", response_class=HTMLResponse)
async def serve_index() -> HTMLResponse:
    html_content = UI_HTML_PATH.read_text(encoding="utf-8")
    return HTMLResponse(
        html_content,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )
