#!/usr/bin/env python3
"""Universal Gemini model caller and multi-source asset resolver (Local / GCS FUSE / GCS API / Google Drive)."""
from __future__ import annotations

import base64
import copy
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any
import urllib.parse
import urllib.request
import uuid

from app.config import (
    APP_DIR,
    BUILTIN_SAMPLES_MANIFEST,
    CONFIG_PATH,
    DATA_DIR,
    DEFAULT_OPTIMIZATION_WHY,
    DEFAULT_OPTIMIZED_PROMPT,
    DEFAULT_ORIGINAL_AUDIO_PROMPT,
    DEFAULT_PLATFORM_CONFIG,
    DOCS_DIR,
    GDRIVE_BIN,
    GENERATE_BIN,
    MANIFEST_PATH,
    MODEL_ARGON_4,
    MODEL_FLASH_38,
    PROJECT_ROOT,
    RUNTIME_LOG_PATH,
    RUNS_V2_DIR,
    SAMPLES_DIR,
    SNAPSHOTS_DIR,
    _LOG_LOCK,
    _RUNTIME_LOGS,
    _THREAD_CTX,
    _set_thread_log_ctx,
    load_platform_config,
    log_runtime,
    save_platform_config,
)

# ==============================================================================
# 3. Universal Gemini Model Caller (Respects Per-Tab Model, Location & Params)
# ==============================================================================
def _clean_json_output(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    cand = (
        text[first_brace : last_brace + 1]
        if (first_brace != -1 and last_brace != -1 and last_brace > first_brace)
        else (text[first_brace:] if first_brace != -1 else text)
    )
    try:
        return json.loads(cand)
    except json.JSONDecodeError:
        pass

    # Stage 2: Escape unescaped inner double quotes inside single-line string values & array strings
    fixed_lines: list[str] = []
    for line in cand.splitlines():
        m = re.match(r'^(\s*"[^"]+"\s*:\s*")(.*)("\s*,?\s*)$', line)
        if m:
            prefix, middle, suffix = m.groups()
            middle_fixed = middle.replace('\\"', "\x00").replace('"', '\\"').replace("\x00", '\\"')
            fixed_lines.append(prefix + middle_fixed + suffix)
            continue
        m2 = re.match(r'^(\s*")(.*)("\s*,?\s*)$', line)
        if m2 and ":" not in line[: m2.start(2)]:
            prefix, middle, suffix = m2.groups()
            middle_fixed = middle.replace('\\"', "\x00").replace('"', '\\"').replace("\x00", '\\"')
            fixed_lines.append(prefix + middle_fixed + suffix)
            continue
        fixed_lines.append(line)
    repaired = "\n".join(fixed_lines)

    # Stage 3: Fix missing commas across lines ("Expecting ',' delimiter: line X column Y") & trailing commas
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r'(["0-9}\]]|true|false|null)[ \t]*\n([ \t]*"[^"]+"[ \t]*:)', r"\1,\n\2", repaired)
    repaired = re.sub(r"(\})[ \t]*\n([ \t]*\{)", r"\1,\n\2", repaired)
    repaired = re.sub(r"(\])[ \t]*\n([ \t]*\[)", r"\1,\n\2", repaired)
    repaired = re.sub(r'("[^"]*")[ \t]*\n([ \t]*"[^"]*"[ \t]*(?!:))', r"\1,\n\2", repaired)
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # Stage 4: Balance truncated JSON tail if output hit token limit
    stack: list[str] = []
    in_str = False
    esc = False
    out_chars: list[str] = []
    for ch in repaired:
        out_chars.append(ch)
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]" and stack and stack[-1] == ch:
                stack.pop()
    tail_fixed = "".join(out_chars).rstrip()
    if in_str:
        tail_fixed += '"'
    tail_fixed = re.sub(r"[:,]\s*$", "", tail_fixed)
    while stack:
        tail_fixed += stack.pop()
    tail_fixed = re.sub(r",\s*([}\]])", r"\1", tail_fixed)
    return json.loads(tail_fixed)


def call_gemini_model(
    parts: list[dict[str, Any]],
    model: str = MODEL_ARGON_4,
    location: str = "",
    thinking_level: str | None = None,
    temperature: float = 0.2,
    max_output_tokens: int = 65535,
    timeout_sec: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Calls the configured model with its independent location, thinking_level, temperature, and project_id."""
    cfg = load_platform_config()
    proj_cfg = cfg["project"]
    eff_timeout = int(timeout_sec or proj_cfg.get("request_timeout_sec", 240))
    eff_project = (proj_cfg.get("project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "genai-dnb-demo").strip()
    eff_location = (location or proj_cfg.get("default_location") or os.environ.get("GOOGLE_CLOUD_LOCATION") or "global").strip()
    eff_api_key = (proj_cfg.get("api_key") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    prefer_vertex = bool(proj_cfg.get("prefer_vertexai", True))

    target_model = model if model.startswith("models/") else f"models/{model}"
    norm_thinking = thinking_level.strip().upper() if thinking_level and thinking_level.strip() else None
    req_id = uuid.uuid4().hex[:10]
    req_path = Path(f"/tmp/argon_v2_req_{req_id}.json")
    last_err = ""

    try:
        # 1. Cloudtop `generate` CLI if present
        if Path(GENERATE_BIN).exists():
            for attempt_idx, sleep_before in enumerate((0.0, 2.0)):
                if sleep_before > 0:
                    time.sleep(sleep_before)
                t0 = time.perf_counter()
                t_cfg: dict[str, Any] = {"include_thoughts": False}
                if norm_thinking:
                    t_cfg["thinking_level"] = norm_thinking
                gen_cfg: dict[str, Any] = {
                    "temperature": float(temperature),
                    "response_mime_type": "application/json",
                    "thinking_config": t_cfg,
                }
                if max_output_tokens and int(max_output_tokens) > 0:
                    gen_cfg["max_output_tokens"] = int(max_output_tokens)
                payload = {
                    "model": target_model,
                    "contents": [{"role": "user", "parts": parts}],
                    "generation_config": gen_cfg,
                }
                req_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                cmd = [
                    GENERATE_BIN,
                    "-prod",
                    "-json",
                    f"-timeout={eff_timeout}s",
                    f"-request={req_path}",
                ]
                res = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=eff_timeout + 15,
                )
                if res.returncode != 0:
                    last_err = res.stderr[:400]
                    continue
                try:
                    envelope = json.loads(res.stdout)
                    text_out = ""
                    for cand in envelope.get("candidates", []):
                        for part in cand.get("content", {}).get("parts", []):
                            if "text" in part and not part.get("thought", False):
                                text_out += part["text"]
                    if not text_out:
                        for cand in envelope.get("candidates", []):
                            for part in cand.get("content", {}).get("parts", []):
                                if "text" in part:
                                    text_out += part["text"]
                    parsed = _clean_json_output(text_out)
                    latency = round(time.perf_counter() - t0, 2)
                    usage = envelope.get("usage_metadata", {})
                    return parsed, {
                        "model_version": envelope.get("model_version", target_model),
                        "requested_model": target_model,
                        "project_id": eff_project,
                        "location": eff_location,
                        "thinking_level": norm_thinking or "DEFAULT",
                        "temperature": temperature,
                        "latency_sec": latency,
                        "prompt_tokens": usage.get("prompt_token_count", 0),
                        "candidate_tokens": usage.get("candidates_token_count", 0),
                        "total_tokens": usage.get("total_token_count", 0),
                    }
                except Exception as pe:
                    last_err = f"JSON parse error: {pe}"

        # 2. Local Laptop / Standard `google-genai` SDK
        from google import genai
        from google.genai import types

        if eff_api_key and not prefer_vertex:
            client = genai.Client(api_key=eff_api_key)
        elif eff_project:
            client = genai.Client(vertexai=True, project=eff_project, location=eff_location)
        elif eff_api_key:
            client = genai.Client(api_key=eff_api_key)
        else:
            client = genai.Client(vertexai=True, project="genai-dnb-demo", location=eff_location)

        sdk_contents: list[Any] = []
        for p in parts:
            if "text" in p:
                sdk_contents.append(p["text"])
            elif "file_data" in p and str(p["file_data"].get("file_uri", "")).startswith("gs://"):
                fd = p["file_data"]
                sdk_contents.append(
                    types.Part.from_uri(
                        file_uri=fd["file_uri"],
                        mime_type=fd.get("mime_type", "application/octet-stream"),
                    )
                )
            elif "inline_data" in p:
                inl = p["inline_data"]
                data_str = str(inl["data"])
                p_cand = Path(data_str)
                if p_cand.exists() and p_cand.is_file():
                    raw_bytes = p_cand.read_bytes()
                    if len(raw_bytes) < 128:
                        raise RuntimeError(f"媒体文件过小或损坏 ({len(raw_bytes)}B): {p_cand}")
                elif data_str.startswith("/") or data_str.endswith((".mp3", ".jpeg", ".jpg", ".png", ".mp4")):
                    # Look up detailed resolution trace if recorded by resolve_sample_asset
                    trace_detail = ""
                    for tk_key, notes in list(_LAST_ASSET_RESOLUTION_TRACES.items()):
                        if data_str.endswith(tk_key):
                            trace_detail = "\n  • 详细排查轨迹:\n    - " + "\n    - ".join(notes)
                            break
                    bucket_name, prefix = _parse_gcs_bucket_and_prefix()
                    err_msg = (
                        f"未找到样本媒体文件: {p_cand} (DATA_DIR={DATA_DIR}, SAMPLES_DIR={SAMPLES_DIR}, "
                        f"GCS_BUCKET={bucket_name or '<未配置>'}, GCS_PREFIX={prefix or '<空>'})。"
                        f"\n请检查：(1) 若使用 GCS，请确认传入了环境变量 ARGON_GCS_BUCKET (或 GCS_BUCKET) 且对象位于 gs://<bucket>/samples/01..10/ 或 gs://<bucket>/argon_eval_data/samples/01..10/；"
                        f"(2) 若使用本地目录，请确认包含 argon_eval_data/samples/01..10；"
                        f"(3) 或在 Tab #7 配置中心直接从浏览器上传素材！{trace_detail}"
                    )
                    log_runtime("ERROR", "MEDIA_FILE_MISSING", err_msg)
                    raise FileNotFoundError(err_msg)
                else:
                    raw_bytes = base64.b64decode(data_str)
                sdk_contents.append(types.Part.from_bytes(data=raw_bytes, mime_type=inl["mime_type"]))

        clean_model = target_model.replace("models/", "")
        # Strict model policy: ONLY allow gemini-4-argon and gemini-3.8-flash! Never switch models on retry!
        if "3.8" in clean_model or "flash" in clean_model:
            strict_model = MODEL_FLASH_38
        else:
            strict_model = MODEL_ARGON_4
        eff_max_tokens = max(int(max_output_tokens or 65535), 65535)
        retry_attempts = 3

        for fb_idx in range(1, retry_attempts + 1):
            fb_model = strict_model
            if fb_idx > 1:
                time.sleep(1.5)
            t0 = time.perf_counter()
            log_runtime(
                "INFO",
                "MODEL_CALL_START",
                f"调用模型 {fb_model} (attempt={fb_idx}/{retry_attempts}, loc={eff_location}, proj={eff_project}, thinking={norm_thinking or 'DEFAULT'}, temp={temperature}, max_tokens={eff_max_tokens})",
            )
            try:
                cfg_kwargs: dict[str, Any] = {
                    "temperature": float(temperature),
                    "response_mime_type": "application/json",
                    "max_output_tokens": eff_max_tokens,
                }
                if norm_thinking:
                    cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=norm_thinking)
                resp = client.models.generate_content(
                    model=fb_model,
                    contents=sdk_contents,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                if resp.text:
                    parsed = _clean_json_output(resp.text)
                    latency = round(time.perf_counter() - t0, 2)
                    um = getattr(resp, "usage_metadata", None)
                    p_tok = getattr(um, "prompt_token_count", 0) or 0
                    c_tok = getattr(um, "candidates_token_count", 0) or 0
                    t_tok = getattr(um, "total_token_count", 0) or 0
                    log_runtime(
                        "INFO",
                        "MODEL_CALL_OK",
                        f"模型 {fb_model} 响应成功 (耗时={latency}s, tokens={p_tok}+{c_tok}={t_tok})",
                    )
                    return parsed, {
                        "model_version": fb_model,
                        "requested_model": target_model,
                        "project_id": eff_project,
                        "location": eff_location,
                        "thinking_level": norm_thinking or "DEFAULT",
                        "temperature": temperature,
                        "attempt": fb_idx,
                        "latency_sec": latency,
                        "prompt_tokens": p_tok,
                        "candidate_tokens": c_tok,
                        "total_tokens": t_tok,
                    }
            except Exception as fb_exc:
                if norm_thinking and "thinking" in str(fb_exc).lower():
                    log_runtime(
                        "WARNING",
                        "MODEL_THINKING_FALLBACK",
                        f"模型 {fb_model} 不支持当前 thinking_config ({norm_thinking})，正在自动降级为无 thinking_config 重试: {fb_exc}",
                    )
                    try:
                        resp = client.models.generate_content(
                            model=fb_model,
                            contents=sdk_contents,
                            config=types.GenerateContentConfig(
                                temperature=float(temperature),
                                response_mime_type="application/json",
                            ),
                        )
                        if resp.text:
                            parsed = _clean_json_output(resp.text)
                            latency = round(time.perf_counter() - t0, 2)
                            log_runtime(
                                "INFO",
                                "MODEL_CALL_OK",
                                f"模型 {fb_model} (fallback-auto) 响应成功 (耗时={latency}s)",
                            )
                            return parsed, {
                                "model_version": fb_model,
                                "requested_model": target_model,
                                "project_id": eff_project,
                                "location": eff_location,
                                "thinking_level": f"{norm_thinking} (fallback-auto)",
                                "temperature": temperature,
                                "latency_sec": latency,
                                "prompt_tokens": 0,
                                "candidate_tokens": 0,
                                "total_tokens": 0,
                            }
                    except Exception as retry_exc:
                        log_runtime("WARNING", "MODEL_RETRY_FAIL", f"模型 {fb_model} 降级重试仍失败: {retry_exc}")
                last_err = f"{fb_model} (loc={eff_location}, proj={eff_project}): {type(fb_exc).__name__}: {fb_exc}"
                log_runtime(
                    "WARNING",
                    "MODEL_ATTEMPT_FAIL",
                    f"模型调用异常 [{fb_model} @ {eff_location} (proj={eff_project})]: {type(fb_exc).__name__}: {fb_exc}",
                    exc=fb_exc if fb_idx == retry_attempts else None,
                )

        raise RuntimeError(f"Model call failed ({target_model}): {last_err}")
    finally:
        if req_path.exists():
            try:
                req_path.unlink()
            except OSError:
                pass


# ==============================================================================
# 4. Multi-Source Asset Resolver (Local / GCS FUSE / GCS Bucket API / Google Drive / URL)
# ==============================================================================
def load_manifest() -> list[dict[str, Any]]:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return copy.deepcopy(BUILTIN_SAMPLES_MANIFEST)


def get_sample_by_id(sample_id: str) -> dict[str, Any]:
    for item in load_manifest():
        if item["sample_id"] == sample_id:
            return item
    raise KeyError(f"Sample {sample_id} not found in manifest.")


def _parse_gcs_bucket_and_prefix() -> tuple[str, str]:
    """Returns (bucket_name, prefix_without_leading_or_trailing_slash) from config or env."""
    cfg = load_platform_config()
    proj = cfg.get("project", {})
    raw_bucket = (proj.get("gcs_bucket") or os.environ.get("ARGON_GCS_BUCKET") or "").strip()
    raw_prefix = (proj.get("gcs_prefix") or os.environ.get("ARGON_GCS_PREFIX") or "argon_eval_data").strip()
    if raw_bucket.startswith("gs://"):
        without_scheme = raw_bucket[5:].strip("/")
        parts = without_scheme.split("/", 1)
        bucket_name = parts[0]
        if len(parts) > 1 and parts[1]:
            raw_prefix = parts[1]
    else:
        bucket_name = raw_bucket.strip("/")
    return bucket_name, raw_prefix.strip("/")


def _get_gcp_bearer_tokens(
    scopes: list[str] | None = None,
    trace_notes: list[str] | None = None,
) -> list[str]:
    """Resolves OAuth2 Bearer tokens via google.auth ADC (works natively on Cloud Run & GCE), env vars, or gcloud CLI."""
    tokens: list[str] = []
    for env_key in ("GDRIVE_ACCESS_TOKEN", "GCP_ACCESS_TOKEN", "GOOGLE_OAUTH_ACCESS_TOKEN"):
        val = os.environ.get(env_key, "").strip()
        if val and val not in tokens:
            tokens.append(val)
            if trace_notes is not None:
                trace_notes.append(f"Token from env {env_key} (len={len(val)})")
    try:
        import google.auth
        import google.auth.transport.requests

        creds, adc_proj = google.auth.default(
            scopes=scopes
            or [
                "https://www.googleapis.com/auth/cloud-platform",
                "https://www.googleapis.com/auth/devstorage.read_write",
                "https://www.googleapis.com/auth/drive.readonly",
            ]
        )
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)
        if creds.token and creds.token not in tokens:
            tokens.append(creds.token)
            if trace_notes is not None:
                sa_info = getattr(creds, "service_account_email", "user-adc")
                trace_notes.append(f"ADC token refreshed OK (identity={sa_info}, adc_project={adc_proj})")
    except Exception as adc_exc:
        if trace_notes is not None:
            trace_notes.append(f"google.auth.default ADC failed: {type(adc_exc).__name__}: {adc_exc}")
    for cmd in (
        ["gcloud", "auth", "application-default", "print-access-token"],
        ["gcloud", "auth", "print-access-token"],
    ):
        try:
            tok = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
            if tok and len(tok) > 20 and tok not in tokens:
                tokens.append(tok)
                if trace_notes is not None:
                    trace_notes.append(f"Token from {' '.join(cmd)} OK")
        except Exception:
            continue
    return tokens


_LAST_ASSET_RESOLUTION_TRACES: dict[str, list[str]] = {}


def download_from_gcs_universal(
    rel_object_path: str,
    dest: Path,
    timeout_sec: int = 120,
    trace_notes: list[str] | None = None,
) -> bool:
    """Downloads `gs://{bucket}/{prefix}/{rel_object_path}` OR `gs://{bucket}/{rel_object_path}` to `dest`."""
    bucket_name, prefix = _parse_gcs_bucket_and_prefix()
    if not bucket_name:
        if trace_notes is not None:
            trace_notes.append(
                "GCS跳过: 未配置 GCS Bucket (环境变量 ARGON_GCS_BUCKET 与 platform_config.project.gcs_bucket 均为空)"
            )
        return False
    clean_rel = rel_object_path.lstrip("/")
    candidate_blobs = []
    if prefix:
        candidate_blobs.append(f"{prefix}/{clean_rel}")
    if clean_rel not in candidate_blobs:
        candidate_blobs.append(clean_rel)
    if clean_rel.startswith("runs_v2/"):
        alt_runs = "runs/" + clean_rel[len("runs_v2/"):]
        if prefix and f"{prefix}/{alt_runs}" not in candidate_blobs:
            candidate_blobs.append(f"{prefix}/{alt_runs}")
        if alt_runs not in candidate_blobs:
            candidate_blobs.append(alt_runs)
    if f"argon_eval_data/{clean_rel}" not in candidate_blobs:
        candidate_blobs.append(f"argon_eval_data/{clean_rel}")

    dest.parent.mkdir(parents=True, exist_ok=True)

    # 1. Try google-cloud-storage SDK if installed
    try:
        from google.cloud import storage  # type: ignore

        proj_id = load_platform_config()["project"].get("project_id") or None
        client = storage.Client(project=proj_id)
        bucket = client.bucket(bucket_name)
        for blob_name in candidate_blobs:
            blob = bucket.blob(blob_name)
            if blob.exists():
                blob.download_to_filename(str(dest), timeout=timeout_sec)
                if dest.exists() and dest.stat().st_size > 0:
                    sz = dest.stat().st_size
                    msg = f"GCS SDK下载成功: gs://{bucket_name}/{blob_name} -> {dest} ({sz} bytes)"
                    if trace_notes is not None:
                        trace_notes.append(msg)
                    log_runtime("INFO", "GCS_DOWNLOAD", msg)
                    return True
            else:
                if trace_notes is not None:
                    trace_notes.append(f"GCS SDK检查: gs://{bucket_name}/{blob_name} 不存在 (404)")
    except Exception as sdk_exc:
        if trace_notes is not None:
            trace_notes.append(f"GCS SDK异常 (bucket={bucket_name}): {type(sdk_exc).__name__}: {sdk_exc}")

    # 2. Fallback to zero-dependency GCS JSON API using ADC / Bearer token
    import urllib.error
    import urllib.parse
    import urllib.request

    tokens = _get_gcp_bearer_tokens(trace_notes=trace_notes)
    if not tokens and trace_notes is not None:
        trace_notes.append("GCS HTTP API警告: 未获取到任何可用 GCP OAuth/ADC Token")
    for blob_name in candidate_blobs:
        encoded_obj = urllib.parse.quote(blob_name, safe="")
        url = f"https://storage.googleapis.com/storage/v1/b/{bucket_name}/o/{encoded_obj}?alt=media"
        for tok_idx, tok in enumerate(tokens, start=1):
            try:
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    data = resp.read()
                    if data:
                        dest.write_bytes(data)
                        msg = f"GCS HTTP下载成功: gs://{bucket_name}/{blob_name} -> {dest} ({len(data)} bytes)"
                        if trace_notes is not None:
                            trace_notes.append(msg)
                        log_runtime("INFO", "GCS_DOWNLOAD", msg)
                        return True
            except urllib.error.HTTPError as he:
                err_body = ""
                try:
                    err_body = he.read().decode("utf-8", errors="replace")[:180]
                except Exception:
                    pass
                if trace_notes is not None:
                    trace_notes.append(
                        f"GCS HTTP {he.code} (gs://{bucket_name}/{blob_name}, token#{tok_idx}): {he.reason} {err_body}"
                    )
            except Exception as urlexc:
                if trace_notes is not None:
                    trace_notes.append(
                        f"GCS HTTP异常 (gs://{bucket_name}/{blob_name}, token#{tok_idx}): {type(urlexc).__name__}: {urlexc}"
                    )
                continue
    return False


def upload_to_gcs_universal(rel_object_path: str, content_bytes: bytes, content_type: str = "application/json") -> bool:
    """Uploads bytes to `gs://{bucket}/{prefix}/{rel_object_path}` so `runs_v2` state persists in GCS on Cloud Run."""
    bucket_name, prefix = _parse_gcs_bucket_and_prefix()
    if not bucket_name:
        return False
    blob_name = f"{prefix}/{rel_object_path.lstrip('/')}" if prefix else rel_object_path.lstrip("/")
    try:
        from google.cloud import storage  # type: ignore

        proj_id = load_platform_config()["project"].get("project_id") or None
        client = storage.Client(project=proj_id)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.upload_from_string(content_bytes, content_type=content_type, timeout=30)
        return True
    except Exception:
        pass

    import urllib.parse
    import urllib.request

    encoded_obj = urllib.parse.quote(blob_name, safe="")
    url = f"https://storage.googleapis.com/upload/storage/v1/b/{bucket_name}/o?uploadType=media&name={encoded_obj}"
    for tok in _get_gcp_bearer_tokens():
        try:
            req = urllib.request.Request(
                url,
                data=content_bytes,
                method="POST",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": content_type},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status in (200, 201):
                    return True
        except Exception:
            continue
    return False


def download_drive_file_universal(
    file_id: str,
    dest: Path,
    timeout_sec: int = 180,
    trace_notes: list[str] | None = None,
) -> bool:
    """Downloads a sample file from Google Drive when assets are NOT on local disk or GCS."""
    if dest.exists() and dest.stat().st_size > 128:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    if Path(GDRIVE_BIN).exists():
        try:
            res = subprocess.run(
                [GDRIVE_BIN, "readonly", "download", file_id, "--out", str(dest)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_sec,
            )
            if res.returncode == 0 and dest.exists() and dest.stat().st_size > 128:
                if trace_notes is not None:
                    trace_notes.append(f"GDrive CLI下载成功: file_id={file_id} -> {dest}")
                return True
            elif trace_notes is not None:
                trace_notes.append(f"GDrive CLI返回 rc={res.returncode}: {res.stderr.strip()[:160]}")
        except Exception as cli_exc:
            if trace_notes is not None:
                trace_notes.append(f"GDrive CLI异常: {cli_exc}")

    import urllib.error
    import urllib.request

    api_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&supportsAllDrives=true"
    quota_proj = load_platform_config()["project"].get("project_id", "genai-dnb-demo")
    for tok_idx, token in enumerate(_get_gcp_bearer_tokens(), start=1):
        for use_proj in (True, False):
            headers = {"Authorization": f"Bearer {token}"}
            if use_proj and quota_proj:
                headers["X-Goog-User-Project"] = quota_proj
            try:
                req = urllib.request.Request(api_url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                    data = resp.read()
                    if data and len(data) > 128:
                        dest.write_bytes(data)
                        msg = f"Drive API v3下载成功: file_id={file_id} -> {dest} ({len(data)} bytes)"
                        if trace_notes is not None:
                            trace_notes.append(msg)
                        log_runtime("INFO", "GDRIVE_DOWNLOAD", msg)
                        return True
            except urllib.error.HTTPError as he:
                if trace_notes is not None and not use_proj:
                    trace_notes.append(f"Drive API v3 HTTP {he.code} (file_id={file_id}, token#{tok_idx}): {he.reason}")
            except Exception as dexc:
                if trace_notes is not None and not use_proj:
                    trace_notes.append(f"Drive API v3异常 (file_id={file_id}): {type(dexc).__name__}: {dexc}")
                continue

    # Direct public/link-shared Drive download fallback (`uc?export=download`)
    direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    try:
        req = urllib.request.Request(direct_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read()
            if data and len(data) > 128 and "text/html" not in ctype.lower():
                dest.write_bytes(data)
                if trace_notes is not None:
                    trace_notes.append(f"Drive公开链接下载成功: file_id={file_id} ({len(data)} bytes)")
                return True
            elif trace_notes is not None:
                trace_notes.append(
                    f"Drive公开链接未返回二进制媒体 (Content-Type={ctype}, size={len(data) if data else 0}B，通常因文件仅内部域可见需ADC或GCS)"
                )
    except Exception as pub_exc:
        if trace_notes is not None:
            trace_notes.append(f"Drive公开链接访问失败 (file_id={file_id}): {type(pub_exc).__name__}: {pub_exc}")
    return dest.exists() and dest.stat().st_size > 128


def resolve_sample_asset(sample_id: str, filename: str, file_id: str = "") -> Path:
    """
    Universal multi-source asset resolver with full diagnostic trace:
    1. Local disk / Cloud Run GCS FUSE mounted volume (`SAMPLES_DIR / sample_id / filename` or nested `argon_eval_data/samples/...`)
    2. Google Cloud Storage bucket (`gs://{gcs_bucket}/{gcs_prefix}/samples/{sample_id}/{filename}` or root `gs://{gcs_bucket}/samples/...`)
    3. Google Drive API / direct download (`file_id` from `samples_manifest.json`)
    """
    trace_key = f"{sample_id}/{filename}"
    trace_notes: list[str] = []
    local_candidates = (
        SAMPLES_DIR / sample_id / filename,
        DATA_DIR / "argon_eval_data" / "samples" / sample_id / filename,
        APP_DIR / "argon_eval_data" / "samples" / sample_id / filename,
        APP_DIR / "samples" / sample_id / filename,
        Path("/tmp/argon_eval_data/samples") / sample_id / filename,
    )
    for cand in local_candidates:
        if cand.exists():
            sz = cand.stat().st_size
            if filename == "prompt.txt" or sz > 128:
                trace_notes.append(f"本地/FUSE命中: {cand} ({sz} bytes)")
                _LAST_ASSET_RESOLUTION_TRACES[trace_key] = trace_notes
                return cand
            else:
                trace_notes.append(f"本地文件过小跳过: {cand} (仅 {sz} bytes)")
        else:
            trace_notes.append(f"本地未找到: {cand}")

    # Inspect what actually exists inside DATA_DIR and SAMPLES_DIR to aid Cloud Run FUSE mount debugging
    try:
        data_dir_entries = [p.name for p in DATA_DIR.iterdir()][:15] if DATA_DIR.exists() else ["<DATA_DIR不存在>"]
        samples_dir_entries = [p.name for p in SAMPLES_DIR.iterdir()][:15] if SAMPLES_DIR.exists() else ["<SAMPLES_DIR不存在>"]
        sample_sub_dir = SAMPLES_DIR / sample_id
        sub_entries = [p.name for p in sample_sub_dir.iterdir()][:15] if sample_sub_dir.exists() else ["<样本子目录不存在>"]
        trace_notes.append(
            f"目录快照: DATA_DIR({DATA_DIR})={data_dir_entries} | SAMPLES_DIR({SAMPLES_DIR})={samples_dir_entries} | SampleDir({sample_sub_dir})={sub_entries}"
        )
    except Exception as dir_exc:
        trace_notes.append(f"目录快照检查异常: {dir_exc}")

    target = SAMPLES_DIR / sample_id / filename
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        target = Path("/tmp/argon_eval_data/samples") / sample_id / filename
        target.parent.mkdir(parents=True, exist_ok=True)

    cfg_proj = load_platform_config().get("project", {})
    mode = (cfg_proj.get("asset_storage_mode") or os.environ.get("ARGON_ASSET_STORAGE_MODE") or "auto").lower()
    bucket_name, prefix = _parse_gcs_bucket_and_prefix()
    trace_notes.append(
        f"远程回退配置: storage_mode={mode} | gcs_bucket={bucket_name or '<未设置>'} | gcs_prefix={prefix or '<空>'} | drive_file_id={file_id or '<无>'}"
    )

    # Try GCS if configured (and mode is 'auto' or 'gcs')
    if mode in ("auto", "gcs"):
        if download_from_gcs_universal(f"samples/{sample_id}/{filename}", target, trace_notes=trace_notes):
            _LAST_ASSET_RESOLUTION_TRACES[trace_key] = trace_notes
            return target

    # Try Google Drive if not on GCS (and mode is 'auto' or 'gdrive' or 'local')
    if mode in ("auto", "gdrive", "local") and file_id:
        if download_drive_file_universal(file_id, target, trace_notes=trace_notes):
            _LAST_ASSET_RESOLUTION_TRACES[trace_key] = trace_notes
            return target

    _LAST_ASSET_RESOLUTION_TRACES[trace_key] = trace_notes
    if filename != "prompt.txt":
        log_runtime(
            "WARNING",
            "ASSET_RESOLVE_MISS",
            f"样本 {sample_id} 素材 `{filename}` 在所有存储源均未找到！诊断轨迹: " + " ; ".join(trace_notes),
            sample_id=sample_id,
        )
    return target


def build_media_part_for_gemini(sample_id: str, filename: str, mime_type: str, file_id: str = "") -> dict[str, Any]:
    """
    Builds a Gemini multimodal Part:
    - First resolves via `resolve_sample_asset` (Local Disk / GCS FUSE -> GCS Bucket -> Google Drive API) and passes local cached path.
    - If local download is skipped/unavailable and `gcs_bucket` + `gcs_use_direct_uri` + `prefer_vertexai` are active,
      passes `gs://{bucket}/{prefix}/samples/{sample_id}/{filename}` URI directly to Vertex AI (`types.Part.from_uri`).
    """
    local_path = resolve_sample_asset(sample_id, filename, file_id=file_id)
    if local_path.exists() and local_path.stat().st_size > 128:
        return {"inline_data": {"mime_type": mime_type, "data": str(local_path)}}

    cfg_proj = load_platform_config().get("project", {})
    bucket_name, prefix = _parse_gcs_bucket_and_prefix()
    if bucket_name and cfg_proj.get("prefer_vertexai", True) and cfg_proj.get("gcs_use_direct_uri", True):
        rel = f"samples/{sample_id}/{filename}"
        gs_uri = f"gs://{bucket_name}/{prefix}/{rel}" if prefix else f"gs://{bucket_name}/{rel}"
        return {"file_data": {"mime_type": mime_type, "file_uri": gs_uri}}

    return {"inline_data": {"mime_type": mime_type, "data": str(local_path)}}


def ensure_sample_files_synced(sample_id: str) -> dict[str, Any]:
    spec = get_sample_by_id(sample_id)
    sdir = SAMPLES_DIR / sample_id
    sdir.mkdir(parents=True, exist_ok=True)
    for fname, fid in spec.get("files", {}).items():
        if fname == "final_mv.mp4":
            continue
        resolve_sample_asset(sample_id, fname, file_id=fid)
    # Also ensure prompt_text in spec is populated if prompt.txt was downloaded or uploaded
    ptxt_path = sdir / "prompt.txt"
    if ptxt_path.exists() and not spec.get("prompt_text"):
        try:
            spec["prompt_text"] = ptxt_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            pass
    return spec


TRACK_FILES = {
    "track1_flash38_orig": RUNS_V2_DIR / "track1_flash38_orig.json",
    "track2_argon4_orig": RUNS_V2_DIR / "track2_argon4_orig.json",
    "step3_prompt_opt": RUNS_V2_DIR / "step3_prompt_opt.json",
    "track4_flash38_opt": RUNS_V2_DIR / "track4_flash38_opt.json",
    "track4_argon4_opt": RUNS_V2_DIR / "track4_argon4_opt.json",
    "track5_storyline_flash38": RUNS_V2_DIR / "track5_storyline_flash38.json",
    "track5_storyline_argon4": RUNS_V2_DIR / "track5_storyline_argon4.json",
    "track6_mv_studio": RUNS_V2_DIR / "track6_mv_studio.json",
}
_STORE_LOCK = threading.Lock()


def load_track_store(track_key: str) -> dict[str, Any]:
    fpath = TRACK_FILES[track_key]
    cfg = load_platform_config()
    data: dict[str, Any] | None = None
    if fpath.exists():
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            data = None

    # Hydrate from GCS if local/FUSE file is missing or empty
    if (not data or not data.get("samples")) and track_key != "step3_prompt_opt" and _parse_gcs_bucket_and_prefix()[0]:
        try:
            if download_from_gcs_universal(f"runs_v2/{fpath.name}", fpath):
                data = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            pass

    if track_key == "step3_prompt_opt":
        if data:
            data["optimized_prompt"] = cfg["prompts"].get("optimized_prompt", data.get("optimized_prompt", DEFAULT_OPTIMIZED_PROMPT))
            return data
        default_opt = {
            "updated_at": cfg.get("updated_at", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "optimized_prompt": cfg["prompts"].get("optimized_prompt", DEFAULT_OPTIMIZED_PROMPT),
            "why_data": DEFAULT_OPTIMIZATION_WHY,
        }
        fpath.write_text(json.dumps(default_opt, ensure_ascii=False, indent=2), encoding="utf-8")
        return default_opt

    if not isinstance(data, dict):
        data = {"track_key": track_key, "updated_at": "", "samples": {}}
    if "samples" not in data or not isinstance(data["samples"], dict):
        data["samples"] = {}

    # For `track6_mv_studio`, auto-discover any `runs/mv_studio_outputs/<sample_id>/mv_studio_plan.json` or `.mp4` files on disk/FUSE!
    if track_key == "track6_mv_studio":
        mv_out_root = RUNS_V2_DIR / "mv_studio_outputs"
        if mv_out_root.exists():
            try:
                for s_dir in sorted(mv_out_root.iterdir()):
                    if not s_dir.is_dir():
                        continue
                    sid = s_dir.name
                    plan_p = s_dir / "mv_studio_plan.json"
                    sub_mp4 = s_dir / "final_mv_subtitled.mp4"
                    clean_mp4 = s_dir / "final_mv_clean.mp4"
                    existing_rec = data["samples"].get(sid)
                    if plan_p.exists() and plan_p.stat().st_size > 64:
                        try:
                            plan_rec = json.loads(plan_p.read_text(encoding="utf-8"))
                            if isinstance(plan_rec, dict):
                                # If final MP4 exists on disk, ensure `final_video_url` and `status='completed'` are populated
                                if (sub_mp4.exists() and sub_mp4.stat().st_size > 1024) or (clean_mp4.exists() and clean_mp4.stat().st_size > 1024):
                                    plan_rec["status"] = "completed"
                                    plan_rec["final_video_url"] = f"/api/v2/mv_studio/media/{sid}/{'final_mv_subtitled.mp4' if sub_mp4.exists() else 'final_mv_clean.mp4'}"
                                    if clean_mp4.exists():
                                        plan_rec["final_clean_video_url"] = f"/api/v2/mv_studio/media/{sid}/final_mv_clean.mp4"
                                if not existing_rec or existing_rec.get("status") != "completed" or plan_rec.get("status") == "completed":
                                    data["samples"][sid] = plan_rec
                        except Exception:
                            pass
                    elif not existing_rec and ((sub_mp4.exists() and sub_mp4.stat().st_size > 1024) or (clean_mp4.exists() and clean_mp4.stat().st_size > 1024)):
                        shot_ffs = sorted(s_dir.glob("shot_*_first_frame.jpg"))
                        shot_vids = sorted(s_dir.glob("shot_*_10s.mp4"))
                        data["samples"][sid] = {
                            "sample_id": sid,
                            "track_key": "track6_mv_studio",
                            "status": "completed",
                            "num_acts": max(len(shot_ffs), len(shot_vids), 1),
                            "shot_first_frame_urls": [f"/api/v2/mv_studio/media/{sid}/{p.name}" for p in shot_ffs],
                            "shot_video_urls": [f"/api/v2/mv_studio/media/{sid}/{p.name}" for p in shot_vids],
                            "final_video_url": f"/api/v2/mv_studio/media/{sid}/{'final_mv_subtitled.mp4' if sub_mp4.exists() else 'final_mv_clean.mp4'}",
                            "final_clean_video_url": f"/api/v2/mv_studio/media/{sid}/final_mv_clean.mp4" if clean_mp4.exists() else "",
                            "srt_url": f"/api/v2/mv_studio/media/{sid}/subtitles.srt" if (s_dir / "subtitles.srt").exists() else "",
                            "vtt_url": f"/api/v2/mv_studio/media/{sid}/subtitles.vtt" if (s_dir / "subtitles.vtt").exists() else "",
                            "evaluated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
            except Exception:
                pass

    return data


def save_sample_to_track_store(track_key: str, sample_id: str, record: dict[str, Any]) -> None:
    with _STORE_LOCK:
        store = load_track_store(track_key)
        if "samples" not in store:
            store["samples"] = {}
        store["samples"][sample_id] = record
        store["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        raw_bytes = json.dumps(store, ensure_ascii=False, indent=2).encode("utf-8")
        TRACK_FILES[track_key].write_bytes(raw_bytes)
        if _parse_gcs_bucket_and_prefix()[0]:
            threading.Thread(
                target=upload_to_gcs_universal,
                args=(f"runs_v2/{TRACK_FILES[track_key].name}", raw_bytes),
                daemon=True,
            ).start()


def load_all_persisted_state() -> dict[str, Any]:
    snapshots = []
    for sp in sorted(SNAPSHOTS_DIR.glob("*.json"), reverse=True):
        try:
            meta = json.loads(sp.read_text(encoding="utf-8"))
            snapshots.append({
                "filename": sp.name,
                "label": meta.get("label", sp.stem),
                "created_at": meta.get("created_at", ""),
                "summary_counts": meta.get("summary_counts", {}),
            })
        except Exception:
            continue

    return {
        "tracks": {
            "track1_flash38_orig": load_track_store("track1_flash38_orig"),
            "track2_argon4_orig": load_track_store("track2_argon4_orig"),
            "step3_prompt_opt": load_track_store("step3_prompt_opt"),
            "track4_flash38_opt": load_track_store("track4_flash38_opt"),
            "track4_argon4_opt": load_track_store("track4_argon4_opt"),
            "track5_storyline_flash38": load_track_store("track5_storyline_flash38"),
            "track5_storyline_argon4": load_track_store("track5_storyline_argon4"),
            "track6_mv_studio": load_track_store("track6_mv_studio"),
        },
        "snapshots": snapshots,
    }


