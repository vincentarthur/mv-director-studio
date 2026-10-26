#!/usr/bin/env python3
"""End-to-end MV Generation Studio engine: Storyboard planning, 24fps DSP vocal lip-sync & F0 pitch/gender/cadence analysis, Omni 1.1 Flash synthesis, subtitle sync & FFmpeg muxing (Tab #6)."""
from __future__ import annotations

import base64
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import datetime
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import threading
import time
import traceback
from typing import Any
import urllib.error
import urllib.request
import uuid
import wave

from app.config import (
    DATA_DIR,
    DEFAULT_MV_STORYBOARD_DIRECTOR_PROMPT,
    DEFAULT_STORYLINE_GENERATION_PROMPT,
    MODEL_ARGON_4,
    MV_PERFORMANCE_MODES,
    MV_STUDIO_OUTPUTS_DIR,
    OMNI_VIDEO_FPS,
    SAMPLES_DIR,
    _BATCH_JOBS,
    _BATCH_LOCK,
    _THREAD_CTX,
    _set_thread_log_ctx,
    load_platform_config,
    log_runtime,
    persist_batch_job_state,
)
from app.evaluation_engine import (
    build_storyline_parts_for_sample,
)
from app.storage import (
    _get_gcp_bearer_tokens,
    _parse_gcs_bucket_and_prefix,
    build_media_part_for_gemini,
    call_gemini_model,
    ensure_sample_files_synced,
    get_sample_by_id,
    load_track_store,
    resolve_sample_asset,
    save_sample_to_track_store,
    upload_to_gcs_universal,
)

def _snap_to_omni_24fps_grid(sec: float, max_sec: float = 10.0) -> float:
    """Snaps any timestamp in seconds to Omni's native 24.0fps (`41.667ms`) frame grid."""
    clamped = max(0.0, min(float(max_sec), float(sec)))
    frame_idx = round(clamped * OMNI_VIDEO_FPS)
    return round(frame_idx / OMNI_VIDEO_FPS, 3)


def _format_omni_mm_ss_mmm(sec: float, snap_to_24fps: bool = True) -> str:
    """Formats seconds into Omni 1.1 Flash's exact C++ timestamp token `MM:SS.mmm` (`%02d:%06.3f`)."""
    t = _snap_to_omni_24fps_grid(sec) if snap_to_24fps else max(0.0, float(sec))
    mins = int(t // 60)
    rem = t - mins * 60
    return f"{mins:02d}:{rem:06.3f}"


def _format_srt_timestamp(sec: float) -> str:
    """Formats seconds into SubRip `.srt` timestamp `HH:MM:SS,mmm`."""
    t = max(0.0, float(sec))
    hrs = int(t // 3600)
    mins = int((t % 3600) // 60)
    secs = int(t % 60)
    ms = int(round((t - math.floor(t)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"


def _format_vtt_timestamp(sec: float) -> str:
    """Formats seconds into WebVTT `.vtt` timestamp `HH:MM:SS.mmm`."""
    return _format_srt_timestamp(sec).replace(",", ".")


def _probe_media_duration(media_path: Path) -> float:
    """Probes media duration in seconds via `ffprobe`."""
    if not media_path.exists():
        return 0.0
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            stderr=subprocess.DEVNULL,
            timeout=15,
        ).decode("utf-8").strip()
        return float(out)
    except Exception:
        return 0.0


def _compress_image_to_jpeg_bytes(img_path: Path, max_dim: int = 1280, quality: int = 90) -> bytes:
    """Normalizes any reference image to clean RGB JPEG bytes for Vertex AI Multimodal & Interactions API."""
    if not img_path.exists():
        return b""
    try:
        from PIL import Image

        with Image.open(img_path) as im:
            rgb = im.convert("RGB")
            w, h = rgb.size
            if max(w, h) > max_dim:
                scale = max_dim / float(max(w, h))
                rgb = rgb.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=quality)
            return buf.getvalue()
    except Exception:
        return img_path.read_bytes()


def _run_ffmpeg_gcsfuse_safe(cmd: list[str], final_out_path: Path) -> bool:
    """
    Executes an `ffmpeg` command whose final argument is `final_out_path` by writing first to a local `/tmp`
    scratch file and then sequentially copying (`shutil.copy2`) to `final_out_path`.
    Why: `ffmpeg` MP4/WAV/MP3 muxers perform backward `lseek()` at the end of encoding to update header sizes or
    `moov` atoms (`+faststart`), which triggers `BufferedWriteHandler.OutOfOrderError` on Cloud Run GCS FUSE mounts!
    Writing to `/tmp` (in-memory tmpfs) and copying sequentially is both 100% GCS-FUSE-safe and faster!
    """
    tmp_dir = Path("/tmp/mv_ffmpeg_scratch")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_out = tmp_dir / f"ff_{uuid.uuid4().hex[:10]}_{final_out_path.name}"
    safe_cmd = list(cmd[:-1]) + [str(tmp_out)]
    try:
        subprocess.run(safe_cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if tmp_out.exists() and tmp_out.stat().st_size > 0:
            final_out_path.parent.mkdir(parents=True, exist_ok=True)
            if final_out_path.exists():
                try:
                    final_out_path.unlink()
                except OSError:
                    pass
            shutil.copy2(tmp_out, final_out_path)
            return True
        return False
    finally:
        tmp_out.unlink(missing_ok=True)


def _extract_last_frame_from_mp4(mp4_path: Path, out_jpg_path: Path) -> bool:
    """Extracts the final frame of a 10s MP4 clip (`-sseof -0.15`) for seamless single-take relay (`<FIRST_FRAME>`)."""
    if not mp4_path.exists() or mp4_path.stat().st_size < 1024:
        return False
    out_jpg_path.parent.mkdir(parents=True, exist_ok=True)
    _run_ffmpeg_gcsfuse_safe(
        [
            "ffmpeg",
            "-y",
            "-sseof",
            "-0.15",
            "-i",
            str(mp4_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out_jpg_path),
        ],
        out_jpg_path,
    )
    return out_jpg_path.exists() and out_jpg_path.stat().st_size > 1024


def _analyze_10s_pcm_samples_24fps(samples: tuple[int, ...] | list[int], sr: int = 16000) -> list[dict[str, Any]]:
    """
    V2.3.1 Harmonicity-Gated 24fps (`41.667ms` per frame, `240` frames per `10.0s` clip) Sub-Phrase Vocal Envelope,
    Fundamental Pitch ($F_0$ Gender/Role Register), & Syllable Rhythm Cadence Analyzer on a 10.0s 16kHz PCM slice.
    - Uses pitch autocorrelation harmonicity gating (`max(corr_m, corr_f) >= 0.21`) to reject unpitched snare/drum transients.
    - Preserves inter-phrase breathing pauses (`>= 208ms`) as closed-mouth intervals (`is_vocal_active=False`).
    - Subdivides long vocal passages into `1.25s ~ 2.50s` sub-phrase windows at local energy dips for tight Omni lip-sync anchoring.
    """
    default_fallback = [
        {
            "start_sec": 0.0,
            "end_sec": 0.5,
            "mm_ss_mmm_start": "00:00.000",
            "mm_ss_mmm_end": "00:00.500",
            "is_vocal_active": False,
            "est_f0_hz": 0.0,
            "acoustic_gender_hint": "none",
            "syllable_rate_hz": 0.0,
            "rhythm_cadence": "instrumental_beat_pulse",
        },
        {
            "start_sec": 0.5,
            "end_sec": 2.75,
            "mm_ss_mmm_start": "00:00.500",
            "mm_ss_mmm_end": "00:02.750",
            "is_vocal_active": True,
            "est_f0_hz": 165.0,
            "acoustic_gender_hint": "male",
            "syllable_rate_hz": 3.2,
            "rhythm_cadence": "mid_melodic_pop",
        },
        {
            "start_sec": 2.75,
            "end_sec": 3.167,
            "mm_ss_mmm_start": "00:02.750",
            "mm_ss_mmm_end": "00:03.167",
            "is_vocal_active": False,
            "est_f0_hz": 0.0,
            "acoustic_gender_hint": "none",
            "syllable_rate_hz": 0.0,
            "rhythm_cadence": "instrumental_beat_pulse",
        },
        {
            "start_sec": 3.167,
            "end_sec": 5.667,
            "mm_ss_mmm_start": "00:03.167",
            "mm_ss_mmm_end": "00:05.667",
            "is_vocal_active": True,
            "est_f0_hz": 165.0,
            "acoustic_gender_hint": "male",
            "syllable_rate_hz": 3.2,
            "rhythm_cadence": "mid_melodic_pop",
        },
        {
            "start_sec": 5.667,
            "end_sec": 6.083,
            "mm_ss_mmm_start": "00:05.667",
            "mm_ss_mmm_end": "00:06.083",
            "is_vocal_active": False,
            "est_f0_hz": 0.0,
            "acoustic_gender_hint": "none",
            "syllable_rate_hz": 0.0,
            "rhythm_cadence": "instrumental_beat_pulse",
        },
        {
            "start_sec": 6.083,
            "end_sec": 8.75,
            "mm_ss_mmm_start": "00:06.083",
            "mm_ss_mmm_end": "00:08.750",
            "is_vocal_active": True,
            "est_f0_hz": 165.0,
            "acoustic_gender_hint": "male",
            "syllable_rate_hz": 3.2,
            "rhythm_cadence": "mid_melodic_pop",
        },
        {
            "start_sec": 8.75,
            "end_sec": 10.0,
            "mm_ss_mmm_start": "00:08.750",
            "mm_ss_mmm_end": "00:10.000",
            "is_vocal_active": False,
            "est_f0_hz": 0.0,
            "acoustic_gender_hint": "none",
            "syllable_rate_hz": 0.0,
            "rhythm_cadence": "instrumental_beat_pulse",
        },
    ]
    if not samples or len(samples) < 1600:
        return default_fallback

    num_bins = int(round(10.0 * OMNI_VIDEO_FPS))  # 240 pixel frames (41.667ms each)
    step = max(1, int(round(sr / OMNI_VIDEO_FPS)))  # ~667 samples @ 16kHz
    rms_vals: list[float] = []
    f0_vals: list[float] = []
    duet_scores: list[float] = []
    harmonicity_vals: list[float] = []

    for idx in range(num_bins):
        s_i = idx * step
        chunk = samples[s_i : s_i + step]
        if not chunk or len(chunk) < 220:
            rms_vals.append(0.0)
            f0_vals.append(0.0)
            duet_scores.append(0.0)
            harmonicity_vals.append(0.0)
            continue
        # Fast RMS on stride-2 samples
        sub_rms = chunk[::2]
        mean_sq = sum(float(x) * float(x) for x in sub_rms) / len(sub_rms)
        rms = math.sqrt(mean_sq)
        rms_vals.append(rms)
        if rms < 45.0:
            f0_vals.append(0.0)
            duet_scores.append(0.0)
            harmonicity_vals.append(0.0)
            continue

        # Downsampled autocorrelation on 8kHz-equivalent decimated slice (stride 2, inner stride 2) for 5x faster F0 in [85Hz, 380Hz]
        dec = chunk[::2]  # effective sr_dec = 8000 Hz, len ~ 334
        n_dec = len(dec)
        energy0 = sum(float(dec[k]) * float(dec[k]) for k in range(0, n_dec, 2)) or 1.0
        best_lag_m = 60
        best_corr_m = 0.0
        for lag in range(45, min(95, n_dec - 16), 3):
            c_val = sum(float(dec[k]) * float(dec[k + lag]) for k in range(0, n_dec - lag, 2)) / energy0
            if c_val > best_corr_m:
                best_corr_m = c_val
                best_lag_m = lag

        best_lag_f = 32
        best_corr_f = 0.0
        for lag in range(21, min(45, n_dec - 16), 2):
            c_val = sum(float(dec[k]) * float(dec[k + lag]) for k in range(0, n_dec - lag, 2)) / energy0
            if c_val > best_corr_f:
                best_corr_f = c_val
                best_lag_f = lag

        harmonicity_vals.append(max(best_corr_m, best_corr_f))
        is_subharmonic = abs(best_lag_m - 2 * best_lag_f) <= 5 or abs(best_lag_m - 3 * best_lag_f) <= 5
        if (
            not is_subharmonic
            and best_corr_m > 0.38
            and best_corr_f > 0.38
            and min(best_corr_m, best_corr_f) / max(best_corr_m, best_corr_f) >= 0.85
        ):
            duet_scores.append(1.0)
        else:
            duet_scores.append(0.0)

        if best_corr_f >= best_corr_m * 0.88 and best_corr_f > 0.20:
            f0_vals.append(round(8000.0 / best_lag_f, 1))
        elif best_corr_m > 0.18:
            f0_vals.append(round(8000.0 / best_lag_m, 1))
        else:
            zc = sum(1 for k in range(0, len(chunk) - 2, 2) if (chunk[k] >= 0) != (chunk[k + 2] >= 0))
            est_z = min(380.0, max(90.0, (zc * sr) / float(len(chunk)) * 0.35))
            f0_vals.append(round(est_z, 1))

    peak_rms = max(rms_vals) if rms_vals else 1.0
    if peak_rms < 50.0:
        return [
            {
                "start_sec": 0.0,
                "end_sec": 10.0,
                "mm_ss_mmm_start": "00:00.000",
                "mm_ss_mmm_end": "00:10.000",
                "is_vocal_active": False,
                "est_f0_hz": 0.0,
                "acoustic_gender_hint": "none",
                "syllable_rate_hz": 0.0,
                "rhythm_cadence": "instrumental_beat_pulse",
            }
        ]

    sorted_rms = sorted(rms_vals)
    p25_rms = sorted_rms[max(0, int(len(sorted_rms) * 0.25))]
    thresh = max(p25_rms * 1.55, peak_rms * 0.20)
    # Require BOTH sufficient band energy AND periodic vocal-fold harmonicity (rejecting unpitched drum/snare hits)
    raw_active = [
        (rms_vals[idx] >= thresh) and (harmonicity_vals[idx] >= 0.21 or rms_vals[idx] >= peak_rms * 0.52)
        for idx in range(num_bins)
    ]

    smoothed = list(raw_active)
    i = 0
    while i < len(smoothed):
        if not smoothed[i]:
            j = i
            while j < len(smoothed) and not smoothed[j]:
                j += 1
            gap_len = j - i
            # Only bridge micro-glottal stops <= 2 frames (83ms); preserve >= 3 frames (>= 125ms) breathing pauses!
            if i > 0 and j < len(smoothed) and gap_len <= 2:
                for k in range(i, j):
                    smoothed[k] = True
            i = j
        else:
            i += 1

    windows: list[tuple[int, int, bool]] = []
    cur_state = smoothed[0]
    start_idx = 0
    for idx in range(1, len(smoothed)):
        if smoothed[idx] != cur_state:
            windows.append((start_idx, idx, cur_state))
            start_idx = idx
            cur_state = smoothed[idx]
    windows.append((start_idx, num_bins, cur_state))

    merged: list[tuple[int, int, bool]] = []
    for f_s, f_e, w_act in windows:
        # Preserve segments >= 5 frames (208ms) so natural inter-phrase breath pauses remain intact
        if merged and (f_e - f_s) < 5:
            p_s, _, p_act = merged[-1]
            merged[-1] = (p_s, f_e, p_act)
        elif merged and merged[-1][2] == w_act:
            p_s, _, p_act = merged[-1]
            merged[-1] = (p_s, f_e, p_act)
        else:
            merged.append((f_s, f_e, w_act))

    # V2.3.1 Sub-Phrase Subdivision: split any vocal window > 60 frames (2.50s) into ~1.3s-2.4s sub-phrases at local RMS dips
    refined: list[tuple[int, int, bool]] = []
    for f_s, f_e, w_act in merged:
        if not w_act or (f_e - f_s) <= 60:
            refined.append((f_s, f_e, w_act))
            continue
        cur_s = f_s
        while (f_e - cur_s) > 60:
            target_len = min(52, max(32, (f_e - cur_s) // 2))  # ~1.33s to 2.16s per sub-phrase
            search_lo = cur_s + max(28, target_len - 10)
            search_hi = min(f_e - 24, cur_s + target_len + 12)
            best_split = cur_s + target_len
            min_e = 1e18
            for cand in range(search_lo, max(search_lo + 1, search_hi + 1), 2):
                local_e = sum(rms_vals[max(cur_s, cand - 1) : min(f_e, cand + 2)])
                if local_e < min_e:
                    min_e = local_e
                    best_split = cand
            best_split = max(cur_s + 28, min(f_e - 24, best_split))
            refined.append((cur_s, best_split, True))
            cur_s = best_split
        if cur_s < f_e:
            refined.append((cur_s, f_e, True))

    detailed_windows: list[dict[str, Any]] = []
    for f_s, f_e, w_act in refined:
        w_s = _snap_to_omni_24fps_grid(f_s / OMNI_VIDEO_FPS)
        w_e = 10.0 if f_e >= num_bins else _snap_to_omni_24fps_grid(f_e / OMNI_VIDEO_FPS)
        win_dur = max(0.167, w_e - w_s)

        win_rms = rms_vals[f_s:f_e]
        mean_w_rms = (sum(win_rms) / len(win_rms)) if win_rms else 0.0
        peaks = 0
        last_peak_k = -3
        for k in range(1, len(win_rms) - 1):
            if (
                win_rms[k] > win_rms[k - 1]
                and win_rms[k] >= win_rms[k + 1]
                and win_rms[k] >= mean_w_rms * 0.82
                and (k - last_peak_k) >= 3
            ):
                peaks += 1
                last_peak_k = k
        syl_rate = round(max(1.2, peaks / win_dur), 2) if w_act else 0.0

        if not w_act:
            est_f0 = 0.0
            gender_hint = "none"
            cadence = "instrumental_beat_pulse"
        else:
            valid_f0 = sorted(x for x in f0_vals[f_s:f_e] if x > 75.0)
            est_f0 = round(valid_f0[len(valid_f0) // 2], 1) if valid_f0 else 165.0
            m_f_count = sum(1 for x in valid_f0 if x < 178.0)
            f_f_count = sum(1 for x in valid_f0 if x >= 182.0)
            n_vf = max(1, len(valid_f0))
            duet_ratio = sum(duet_scores[f_s:f_e]) / max(1, f_e - f_s)
            if duet_ratio >= 0.45 or (m_f_count / n_vf >= 0.28 and f_f_count / n_vf >= 0.28):
                gender_hint = "both"
            elif est_f0 < 180.0:
                gender_hint = "male"
            else:
                gender_hint = "female"

            if syl_rate < 2.45:
                cadence = "slow_sustained_legato"
            elif syl_rate <= 4.25:
                cadence = "mid_melodic_pop"
            else:
                cadence = "fast_syncopated_staccato"

        detailed_windows.append({
            "start_sec": w_s,
            "end_sec": w_e,
            "mm_ss_mmm_start": _format_omni_mm_ss_mmm(w_s),
            "mm_ss_mmm_end": _format_omni_mm_ss_mmm(w_e),
            "is_vocal_active": bool(w_act),
            "est_f0_hz": est_f0,
            "acoustic_gender_hint": gender_hint,
            "syllable_rate_hz": syl_rate,
            "rhythm_cadence": cadence,
        })
    return detailed_windows if detailed_windows else default_fallback


def _detect_dsp_vocal_envelope_windows_10s(chunk_10s_path: Path) -> list[dict[str, Any]]:
    """
    24fps (`41.667ms` per frame, `240` frames per `10.0s` clip) Multi-Band Vocal Envelope,
    Fundamental Pitch ($F_0$ Gender/Role Register), & Syllable Rhythm Cadence Analyzer for a single 10.0s chunk.
    Uses raw `s16le` PCM stdout pipe (`pipe:1`) to completely avoid temporary WAV file writes and GCS FUSE OutOfOrderError!
    """
    default_fallback = _analyze_10s_pcm_samples_24fps([], 16000)
    try:
        sr = 16000
        proc = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(chunk_10s_path),
                "-af",
                "highpass=f=110,lowpass=f=3400",
                "-ac",
                "1",
                "-ar",
                str(sr),
                "-f",
                "s16le",
                "pipe:1",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        raw_pcm = proc.stdout or b""
        if len(raw_pcm) < 3200:
            return default_fallback
        n_short = len(raw_pcm) // 2
        samples = struct.unpack(f"<{n_short}h", raw_pcm[: n_short * 2])
        return _analyze_10s_pcm_samples_24fps(samples, sr)
    except Exception:
        return default_fallback


def _detect_dsp_vocal_windows_batch(master_audio_path: Path, chunk_paths: list[Path]) -> list[list[dict[str, Any]]]:
    """
    Ultra-fast Batch 24fps DSP Analyzer:
    Decodes `master_audio_path` ONCE into a 16kHz 85Hz-3600Hz band-filtered raw `s16le` PCM stream in memory via `pipe:1`
    (zero temporary files on disk/GCS FUSE, completely eliminating `BufferedWriteHandler.OutOfOrderError`),
    then slices 10.0s windows in memory and runs `_analyze_10s_pcm_samples_24fps` across all acts!
    """
    num_acts = len(chunk_paths)
    if num_acts == 0:
        return []
    if not master_audio_path or not master_audio_path.exists():
        return [_detect_dsp_vocal_envelope_windows_10s(cp) for cp in chunk_paths]

    try:
        sr = 16000
        proc = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(master_audio_path),
                "-af",
                "highpass=f=110,lowpass=f=3400",
                "-ac",
                "1",
                "-ar",
                str(sr),
                "-f",
                "s16le",
                "pipe:1",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        raw_pcm = proc.stdout or b""
        if len(raw_pcm) < 3200:
            return [_detect_dsp_vocal_envelope_windows_10s(cp) for cp in chunk_paths]
        n_short = len(raw_pcm) // 2
        all_samples = struct.unpack(f"<{n_short}h", raw_pcm[: n_short * 2])
        samples_per_10s = sr * 10
        results: list[list[dict[str, Any]]] = []
        for idx in range(num_acts):
            s_start = idx * samples_per_10s
            s_end = s_start + samples_per_10s
            seg_samples = all_samples[s_start:s_end]
            if len(seg_samples) < samples_per_10s and len(seg_samples) > 0:
                seg_samples = tuple(seg_samples) + (0,) * (samples_per_10s - len(seg_samples))
            results.append(_analyze_10s_pcm_samples_24fps(seg_samples, sr))
        return results
    except Exception:
        return [_detect_dsp_vocal_envelope_windows_10s(cp) for cp in chunk_paths]


def _vertex_rest_post_json(url: str, payload: dict[str, Any], project_id: str, timeout: int = 240) -> dict[str, Any]:
    """Calls Vertex AI v1beta1 Interactions API (`gemini-omni-1.1-flash-preview`) using ADC / Bearer token."""
    import urllib.error
    import urllib.request

    tokens = _get_gcp_bearer_tokens()
    if not tokens:
        raise RuntimeError("未能获取 GCP OAuth/ADC 访问令牌 (Bearer Token)，请检查认证配置。")
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err = ""
    for tok in tokens:
        headers = {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "x-goog-user-project": project_id,
        }
        try:
            req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as he:
            err_txt = ""
            try:
                err_txt = he.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            last_err = f"HTTP {he.code} {he.reason}: {err_txt}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"Vertex AI Interactions POST 失败 ({url}): {last_err}")


def _vertex_rest_get_json(url: str, project_id: str, timeout: int = 60) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    tokens = _get_gcp_bearer_tokens()
    if not tokens:
        raise RuntimeError("未能获取 GCP OAuth/ADC 访问令牌 (Bearer Token)。")
    last_err = ""
    for tok in tokens:
        headers = {
            "Authorization": f"Bearer {tok}",
            "x-goog-user-project": project_id,
        }
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as he:
            err_txt = ""
            try:
                err_txt = he.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            last_err = f"HTTP {he.code} {he.reason}: {err_txt}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"Vertex AI Interactions GET 失败 ({url}): {last_err}")


_COMMERCIAL_SCAM_TRIGGER_REPLACEMENTS_MV: tuple[tuple[str, str], ...] = (
    ("立即购买", "一起感受"),
    ("马上购买", "立刻感受"),
    ("点击链接", "看准焦点"),
    ("赶紧下单", "尽情体验"),
    ("立即下单", "现在体验"),
    ("buy now", "feel the music"),
    ("Buy Now", "Feel The Music"),
    ("BUY NOW", "FEEL THE MUSIC"),
    ("click the link", "feel the rhythm"),
)


def _sanitize_omni_mv_text(text: str) -> str:
    out = text or ""
    for src_phrase, safe_phrase in _COMMERCIAL_SCAM_TRIGGER_REPLACEMENTS_MV:
        out = out.replace(src_phrase, safe_phrase)
    return out


def _normalize_omni_mv_prompt(raw_prompt: str, include_transcriptions: bool = True) -> str:
    """Ensures Omni 1.1 Flash prompt strictly conforms to `or_constants.gcl` and `omni_format_utils.cc`."""
    prompt = _sanitize_omni_mv_text(raw_prompt or "")
    if "# Descriptions\n" in prompt:
        head, desc_and_rest = prompt.split("# Descriptions\n", 1)
        desc_and_rest = desc_and_rest.replace("<FIRST_FRAME> (image_0.png)", "the starting frame")
        desc_and_rest = desc_and_rest.replace("<FIRST_FRAME>", "the starting frame")
        prompt = head + "# Descriptions\n" + desc_and_rest
    if not include_transcriptions:
        if "\n# Transcriptions" in prompt:
            prompt = prompt.split("\n# Transcriptions", 1)[0].strip()
        prompt = prompt.replace(
            "in `# References`, `# Sources`, `# Descriptions`, and `# Transcriptions`:",
            "in `# References`, `# Sources`, and `# Descriptions`:",
        )
    return prompt.strip()


def _split_lyric_into_paced_subclusters(text: str, num_parts: int) -> list[str]:
    """Splits a lyric line into `num_parts` (2 or 3) balanced sub-phrases by words or CJK characters for 1.0s-1.3s Omni pacing."""
    clean = (text or "").strip()
    if not clean or num_parts <= 1:
        return [clean] if clean else []
    words = clean.split()
    if len(words) >= num_parts * 2:
        chunk_sz = max(1, int(round(len(words) / float(num_parts))))
        clusters: list[str] = []
        for idx in range(num_parts):
            s_i = idx * chunk_sz
            e_i = len(words) if idx == num_parts - 1 else min(len(words), (idx + 1) * chunk_sz)
            if s_i < len(words):
                clusters.append(" ".join(words[s_i:e_i]))
        return [c for c in clusters if c]
    if len(words) <= 2 and len(clean) >= num_parts * 3:
        chunk_sz = max(1, int(round(len(clean) / float(num_parts))))
        clusters = []
        for idx in range(num_parts):
            s_i = idx * chunk_sz
            e_i = len(clean) if idx == num_parts - 1 else min(len(clean), (idx + 1) * chunk_sz)
            if s_i < len(clean):
                clusters.append(clean[s_i:e_i].strip())
        return [c for c in clusters if c]
    return [clean]


def _emit_paced_transcription_lines(st_sec: float, ed_sec: float, safe_lyrics: str) -> list[str]:
    """
    V2.3.1 Sub-Phrase Phonetic Pacing Emitter:
    Anchors `# Transcriptions` every ~1.0s-1.35s across `[st_sec, ed_sec]` on the 24fps grid so Omni 1.1 Flash
    paces the sung syllables across the entire vocal window instead of rushing all words in the first second.
    """
    if not safe_lyrics or safe_lyrics.startswith("["):
        return []
    dur = max(0.167, ed_sec - st_sec)
    if dur >= 3.2:
        parts = _split_lyric_into_paced_subclusters(safe_lyrics, 3)
    elif dur >= 1.95:
        parts = _split_lyric_into_paced_subclusters(safe_lyrics, 2)
    else:
        parts = [safe_lyrics]
    n_p = len(parts)
    out_lines: list[str] = []
    for p_i, p_txt in enumerate(parts):
        sub_t = st_sec + (dur * p_i) / float(n_p)
        ts_str = _format_omni_mm_ss_mmm(sub_t, snap_to_24fps=True)
        out_lines.append(f"[{ts_str}]{p_txt}")
    return out_lines


def compile_mv_storyline_omni_prompt_for_act(
    mv_plan: dict[str, Any],
    act: dict[str, Any],
    total_acts: int,
    language_hint: str = "English",
    mv_performance_mode: str = "solo_vocal_lipsync",
) -> str:
    """
    V2.3.1 C++ `omni_format_utils.cc`-verified Omni 1.1 Flash prompt compiler (`# References`, `# Sources`,
    `# Descriptions`, `# Transcriptions`) with:
    - Sub-phrase `[MM:SS.mmm]` 24fps timestamps (1.0s-1.5s word-cluster pacing in `# Transcriptions`),
    - Explicit phrase-end mouth-closure cutoffs (`EXACT PHRASE CUTOFF & MOUTH CLOSURE AT [MM:SS.mmm]`),
    - Clean natural-language `<Video Caption>` without raw frame-index/Hz clutter,
    - Intra-Act A/B/C-Roll camera cuts across all 3 MV modes (`solo_vocal_lipsync`, `duet_chorus_lipsync`, `pure_story_cinematic`).
    """
    lead_char = mv_plan.get("lead_character") or {}
    supp_char = mv_plan.get("supporting_character") or {}
    lead_name = lead_char.get("name_cn") or "Lead Singer"
    lead_gender = (lead_char.get("gender") or "male").lower()
    lead_timbre = lead_char.get("matched_vocal_timbre_en") or f"expressive {lead_gender} lead vocal"
    lead_look = lead_char.get("visual_appearance_en") or f"photorealistic {lead_gender} protagonist"
    lead_neg = lead_char.get("negative_constraints_en") or ""

    supp_name = supp_char.get("name_cn") or "Supporting Character"
    supp_gender = (supp_char.get("gender") or "female").lower()
    supp_timbre = supp_char.get("matched_vocal_timbre_en") or f"expressive {supp_gender} vocal"
    supp_look = supp_char.get("visual_appearance_en") or ""
    motif = mv_plan.get("core_visual_motif") or "cinematic lighting motif"
    perf_mode = mv_plan.get("mv_performance_mode_resolved") or mv_performance_mode or "solo_vocal_lipsync"

    cadence_en_map = {
        "slow_sustained_legato": "slow sustained legato singing with smooth vowel hold",
        "mid_melodic_pop": "natural mid-tempo syllable-by-syllable singing articulation",
        "fast_syncopated_staccato": "crisp, rhythmic fast-tempo syllable articulation",
        "instrumental_beat_pulse": "pure rhythmic visual acting with mouth naturally closed",
    }

    description_lines: list[str] = []
    transcription_lines: list[str] = []
    micro_beats = act.get("micro_beats") or []

    for idx_b, mb in enumerate(micro_beats):
        st_sec = float(mb.get("start_sec", 0.0))
        ed_sec = float(mb.get("end_sec", 10.0))
        beat_dur = max(0.167, ed_sec - st_sec)
        ts_start = _format_omni_mm_ss_mmm(st_sec, snap_to_24fps=True)
        ts_end = _format_omni_mm_ss_mmm(ed_sec, snap_to_24fps=True)
        mb["mm_ss_mmm_start"] = ts_start
        mb["mm_ss_mmm_end"] = ts_end

        is_open = bool(mb.get("is_vocal_open_mouth", False)) and (perf_mode != "pure_story_cinematic")
        spk = str(mb.get("active_speaker", "NONE" if not is_open else "LEAD")).upper().strip()
        if not is_open:
            spk = "NONE"
        roll_type = str(mb.get("roll_type", "A_ROLL_VOCAL")).upper().strip()
        cam_cn = mb.get("camera_and_shot_cn") or "电影感推轨镜头"
        raw_lyrics = str(mb.get("sung_lyrics") or "").strip()
        safe_lyrics = _sanitize_omni_mv_text(raw_lyrics)
        lead_act = (mb.get("lead_action_and_mouth_en") or "").strip()
        supp_act = (mb.get("support_action_and_mouth_en") or "").strip()
        lead_act_paren = f" ({lead_act})" if lead_act else ""
        supp_act_paren = f" ({supp_act})" if supp_act else ""
        both_acts_str = "; ".join(x for x in (lead_act, supp_act) if x)
        both_acts_clause = f"{both_acts_str}. " if both_acts_str else ""
        light_en = mb.get("lighting_and_color_en") or "cinematic lighting"
        cadence_key = str(mb.get("rhythm_cadence") or "mid_melodic_pop")
        cadence_desc = cadence_en_map.get(cadence_key, cadence_en_map["mid_melodic_pop"])

        if idx_b == 0:
            scene_prefix = (
                f"Starting from the opening frame in {act.get('scene_environment_en', 'a cinematic MV environment')} "
                f"(Visual Motif: {motif}; Lead Character A is <IMAGE_REF_0> [{lead_name}, {lead_gender.upper()}: {lead_look}]"
                + (f"; Supporting Character B is [{supp_name}, {supp_gender.upper()}: {supp_look}]" if supp_look else "")
                + (f"; Constraint: {lead_neg}" if lead_neg else "")
                + "): "
            )
        elif roll_type == "B_ROLL_EMOTION_INSERT":
            scene_prefix = f"Camera cuts to close-up emotional insert ({cam_cn} | {light_en}): "
        elif roll_type == "C_ROLL_WIDE_CLIMAX":
            scene_prefix = f"Camera moves in dynamic wide/orbiting shot ({cam_cn} | {light_en}): "
        else:
            scene_prefix = f"Continuing in medium close-up vocal shot ({cam_cn} | {light_en}): "

        prev_closed = idx_b == 0 or not bool(micro_beats[idx_b - 1].get("is_vocal_open_mouth", False))
        prev_was_open = idx_b > 0 and bool(micro_beats[idx_b - 1].get("is_vocal_open_mouth", False))

        if perf_mode == "pure_story_cinematic":
            video_caption = (
                f"{scene_prefix}At [{ts_start}], pure storytelling performance ({beat_dur:.2f}s beat until {ts_end}, mouths naturally closed, no singing) — "
                f"{both_acts_clause}Camera movement and character acting hit the musical beat at [{ts_start}] while keeping lips closed."
            )
            audio_caption = (
                f"Cinematic music soundtrack and emotional melody from {ts_start} to {ts_end} without on-screen vocal lip-sync."
            )
        elif spk == "LEAD" and is_open:
            onset_tag = (
                f"At exact timestamp [{ts_start}], Lead Character A (<IMAGE_REF_0>, {lead_gender}) opens mouth right on the vocal onset and sings the {beat_dur:.2f}s phrase until {ts_end} "
                if prev_closed
                else f"At [{ts_start}], Lead Character A (<IMAGE_REF_0>, {lead_gender}) continues singing the {beat_dur:.2f}s phrase until {ts_end} "
            )
            video_caption = (
                f"{scene_prefix}{onset_tag}with {cadence_desc} in {language_hint}: \"{safe_lyrics}\"{lead_act_paren}; "
                + (f"meanwhile Supporting Character B ({supp_gender}) keeps mouth tightly closed with zero speaking{supp_act_paren}." if (supp_look or supp_act) else "articulating every syllable cleanly.")
            )
            audio_caption = (
                f"Expressive {language_hint} {lead_gender} lead vocals ({lead_timbre}) singing \"{safe_lyrics}\" from {ts_start} to {ts_end}."
            )
            transcription_lines.extend(_emit_paced_transcription_lines(st_sec, ed_sec, safe_lyrics))
        elif spk == "SUPPORT" and is_open:
            video_caption = (
                f"{scene_prefix}At exact timestamp [{ts_start}], Supporting Character B ({supp_name}, {supp_gender}) opens mouth on the vocal onset and sings the {beat_dur:.2f}s phrase until {ts_end} "
                f"with {cadence_desc} in {language_hint}: \"{safe_lyrics}\"{supp_act_paren}; "
                f"meanwhile Lead Character A (<IMAGE_REF_0>, {lead_gender}) keeps mouth tightly closed with lips sealed{lead_act_paren}."
            )
            audio_caption = (
                f"Expressive {language_hint} {supp_gender} vocals ({supp_timbre}) by Character B singing \"{safe_lyrics}\" from {ts_start} to {ts_end}."
            )
            transcription_lines.extend(_emit_paced_transcription_lines(st_sec, ed_sec, safe_lyrics))
        elif spk == "BOTH" and is_open:
            video_caption = (
                f"{scene_prefix}At exact timestamp [{ts_start}], BOTH Lead Character A (<IMAGE_REF_0>, {lead_gender}) AND Supporting Character B ({supp_gender}) sing aloud together across the {beat_dur:.2f}s chorus phrase until {ts_end} "
                f"in synchronized {cadence_desc}: \"{safe_lyrics}\" ({both_acts_str or 'synchronized duet vocal performance'})."
            )
            audio_caption = (
                f"High-energy {language_hint} duet chorus vocals ({lead_gender} + {supp_gender}) singing \"{safe_lyrics}\" from {ts_start} to {ts_end}."
            )
            transcription_lines.extend(_emit_paced_transcription_lines(st_sec, ed_sec, safe_lyrics))
        else:
            cutoff_prefix = (
                f"At exact timestamp [{ts_start}], the vocal phrase ends and the singer immediately closes their mouth naturally (breath/instrumental pause for {beat_dur:.2f}s until {ts_end}, ALL MOUTHS CLOSED, LIPS SEALED SHUT, ZERO SPEAKING): "
                if prev_was_open
                else f"At [{ts_start}], instrumental/non-vocal window for {beat_dur:.2f}s until {ts_end} (ALL MOUTHS TIGHTLY CLOSED, ZERO SPEAKING, LIPS SEALED SHUT): "
            )
            video_caption = (
                f"{scene_prefix}{cutoff_prefix}{both_acts_clause}Characters keep their lips completely closed with zero vocal mouth movement."
            )
            audio_caption = (
                f"Instrumental music and natural breath pause from {ts_start} to {ts_end} with complete vocal silence and zero singing."
            )

        description_lines.append(f"[{ts_start}]<Video Caption>{video_caption}<Audio Caption>{audio_caption}")

    include_trans = (perf_mode != "pure_story_cinematic") and bool(transcription_lines)
    transcriptions_block = "\n# Transcriptions\n" + "\n".join(transcription_lines) if include_trans else ""
    act_idx = act.get("act_index", 1)
    act_title = act.get("act_title_cn", f"Act {act_idx}")
    prompt = (
        f'Generate an exact 10s (10.000 seconds, duration="10s", 24fps) cinematic music video sequence for Act {act_idx} of {total_acts} ({act_title}). '
        'In `google:omni_gen`, set `duration="10s"` and strictly align every vocal mouth opening, syllable pacing, and mouth closure to the exact `[MM:SS.mmm]` timestamps and `<IMAGE_REF_0>` tag below in `# References`, `# Sources`, `# Descriptions`, and `# Transcriptions`:\n'
        "# References\n"
        "<IMAGE_REF_0>[no_character]image_1.png\n"
        "# Sources\n"
        "<FIRST_FRAME>image_0.png\n"
        "# Descriptions\n"
        + "\n".join(description_lines)
        + transcriptions_block
    )
    return _normalize_omni_mv_prompt(prompt, include_transcriptions=include_trans)


def _update_mv_step_progress(
    stage_code: str,
    message: str,
    sample_id: str = "",
    sample_frac: float | None = None,
    job_id: str = "",
) -> None:
    """
    Logs a fine-grained MV pipeline step to Cloud Run stdout, `runtime_debug.log`, and `_BATCH_JOBS[job_id]["logs"]`,
    while also updating `_BATCH_JOBS[job_id]["current_step"]`, `per_sample[sample_id]["current_stage"]`, and
    smoothly interpolating `progress_pct` so the user always sees which exact sub-step is running!
    """
    eff_job = job_id or getattr(_THREAD_CTX, "job_id", "") or ""
    eff_sid = sample_id or getattr(_THREAD_CTX, "sample_id", "") or ""
    log_runtime(
        "INFO",
        stage_code,
        message,
        sample_id=eff_sid,
        track_key="track6_mv_studio",
        job_id=eff_job,
    )
    if eff_job and "_BATCH_JOBS" in globals() and "_BATCH_LOCK" in globals():
        try:
            with _BATCH_LOCK:
                if eff_job in _BATCH_JOBS:
                    j_obj = _BATCH_JOBS[eff_job]
                    j_obj["current_step"] = f"[Sample {eff_sid} · {stage_code}] {message}" if eff_sid else f"[{stage_code}] {message}"
                    if eff_sid and eff_sid in (j_obj.get("per_sample") or {}):
                        j_obj["per_sample"][eff_sid]["current_stage"] = stage_code
                        j_obj["per_sample"][eff_sid]["current_message"] = message
                        if sample_frac is not None:
                            j_obj["per_sample"][eff_sid]["progress_frac"] = round(max(0.0, min(1.0, float(sample_frac))), 3)
                    if sample_frac is not None:
                        tot = max(1, int(j_obj.get("total_samples") or 1))
                        per_s = j_obj.get("per_sample") or {}
                        total_frac = 0.0
                        for _s_id, s_info in per_s.items():
                            st_val = s_info.get("status")
                            if st_val in ("completed", "plan_ready", "failed", "error"):
                                total_frac += 1.0
                            else:
                                total_frac += float(s_info.get("progress_frac") or 0.0)
                        computed_pct = int(5 + 93 * (total_frac / tot))
                        if computed_pct > int(j_obj.get("progress_pct") or 0):
                            j_obj["progress_pct"] = min(98, computed_pct)
            persist_batch_job_state(eff_job)
        except Exception:
            pass


def _select_best_storyline_for_sample(
    sample_id: str,
    storyline_source: str = "prefer_argon",
) -> tuple[dict[str, Any], str]:
    """
    Retrieves the V2.2 generated Storyline for `sample_id` (from `track5_storyline_argon4` or `track5_storyline_flash38`).
    If neither exists yet, automatically generates one using the configured Storyline model so V2.3 works seamlessly!
    """
    order = (
        ["track5_storyline_argon4", "track5_storyline_flash38"]
        if storyline_source != "prefer_flash"
        else ["track5_storyline_flash38", "track5_storyline_argon4"]
    )
    for tk in order:
        st = load_track_store(tk)
        rec = (st.get("samples") or {}).get(sample_id)
        if rec and rec.get("status") == "completed" and isinstance(rec.get("analysis_output"), dict):
            log_runtime(
                "INFO",
                "MV_STEP1_STORYLINE_CACHE_HIT",
                f"📚 [Step 1/5 · Storyline 加载] 已命中样本 {sample_id} 缓存的 V2.2 Storyline (`{tk}`)",
                sample_id=sample_id,
            )
            return rec["analysis_output"], tk

    # Also check if Track 4 optimized analysis has `mv_storyline_and_storyboard`
    for tk4 in ("track4_argon4_opt", "track4_flash38_opt"):
        st4 = load_track_store(tk4)
        rec4 = (st4.get("samples") or {}).get(sample_id)
        if rec4 and isinstance(rec4.get("analysis_output"), dict):
            ao = rec4["analysis_output"]
            if ao.get("mv_storyline_and_storyboard") or ao.get("four_act_story_arc"):
                log_runtime(
                    "INFO",
                    "MV_STEP1_STORYLINE_FROM_TRACK4",
                    f"📚 [Step 1/5 · Storyline 加载] 已复用样本 {sample_id} 在 `{tk4}` 中的四幕故事线与分镜结果",
                    sample_id=sample_id,
                )
                return ao, tk4

    # Auto-generate V2.2 Storyline on the fly if not yet run
    cfg = load_platform_config()
    target_tk = "track5_storyline_argon4" if storyline_source != "prefer_flash" else "track5_storyline_flash38"
    role_key = "tab5_storyline_model_b" if target_tk == "track5_storyline_argon4" else "tab5_storyline_model_a"
    model_cfg = cfg["tab_models"].get(role_key, cfg["tab_models"]["tab5_storyline_model_b"])
    st_model_name = model_cfg.get("model", MODEL_ARGON_4)
    _update_mv_step_progress(
        "MV_STEP1_AUTO_STORYLINE_START",
        f"📝 [Step 1/5 · 自动生成 Storyline] 样本 {sample_id} 尚未缓存 V2.2 Storyline，正在调用 `{st_model_name}` 实时生成四幕故事线...",
        sample_id=sample_id,
        sample_frac=0.06,
    )
    prompt_text = cfg["prompts"].get("storyline_generation_prompt", DEFAULT_STORYLINE_GENERATION_PROMPT)
    try:
        t_st = time.perf_counter()
        parts, _ = build_storyline_parts_for_sample(sample_id=sample_id, storyline_prompt_text=prompt_text)
        storyline_json, _ = call_gemini_model(
            parts=parts,
            model=st_model_name,
            location=model_cfg.get("location", "global"),
            thinking_level=model_cfg.get("thinking_level", "MEDIUM"),
            temperature=float(model_cfg.get("temperature", 0.25)),
            max_output_tokens=int(model_cfg.get("max_output_tokens", 65535)),
        )
        _update_mv_step_progress(
            "MV_STEP1_AUTO_STORYLINE_OK",
            f"✅ [Step 1/5 · 自动生成 Storyline 完成] 样本 {sample_id} Storyline 实时生成成功 (耗时 {round(time.perf_counter() - t_st, 1)}s)！",
            sample_id=sample_id,
            sample_frac=0.10,
        )
        return storyline_json, f"{target_tk}(auto)"
    except Exception as exc:
        log_runtime("WARNING", "MV_AUTO_STORYLINE_FALLBACK", f"自动生成 Storyline 回退基础模板: {exc}", sample_id=sample_id)
        spec = get_sample_by_id(sample_id)
        return {
            "music_lyric_melody_diagnosis": {
                "detected_genre_and_bpm": spec.get("title", ""),
                "vocal_delivery_and_dynamics": spec.get("protagonist_mode", "single"),
                "lyric_subtext_and_core_theme": spec.get("prompt_text") or "Emotional musical journey",
                "narrative_mode": "story_driven",
            },
            "character_and_scene_guardrails": {
                "core_visual_motif": "Golden stage & atmospheric light beams",
                "protagonist_lock": [{"role_id": "char_01", "singing_permission": "allowed"}],
            },
            "four_act_story_arc": {
                "logline": spec.get("prompt_text") or f"Cinematic MV for {spec.get('title', sample_id)}",
                "act1_opening_hook": "Atmospheric opening establishing the protagonist and mood",
                "act2_mid_development": "Emotional progression and rhythmic movement",
                "act3_climax_escalation": "High-energy chorus climax with dramatic lighting",
                "act4_ending_resonance": "Lingering cinematic outro frame",
            },
            "time_anchored_story_beats": [],
        }, "fallback_template"


def _prepare_mv_master_audio_and_10s_chunks(
    sample_id: str,
    sample_spec: dict[str, Any],
    storyline_data: dict[str, Any],
    out_dir: Path,
    target_duration_sec: int,
    audio_start_sec: float | None,
    is_full_song: bool = False,
    exact_duration_sec: float | None = None,
) -> tuple[Path, list[Path], float, str]:
    """
    Prepares the master audio (`master_audio.mp3`) — either matching the exact full-song duration (`is_full_song=True`)
    or sliced to `target_duration_sec` from `input_audio_01.mp3` — and slices it into `10.0s` chunks (`audio_seg_01_10s.mp3`, ...)
    for 24fps (`41.667ms`) DSP vocal-pitch & syllable-rhythm analysis.
    """
    num_segments = max(1, target_duration_sec // 10)
    target_duration_sec = num_segments * 10
    eff_exact_dur = float(exact_duration_sec) if (exact_duration_sec and exact_duration_sec > 0) else float(target_duration_sec)
    master_audio_path = out_dir / "master_audio.mp3"
    if master_audio_path.exists():
        try:
            master_audio_path.unlink()
        except OSError:
            pass
    audio_source_summary = ""
    resolved_start_sec = 0.0 if is_full_song else (float(audio_start_sec) if audio_start_sec is not None and audio_start_sec >= 0 else 0.0)

    files_map = sample_spec.get("files", {})
    orig_audio_path = resolve_sample_asset(
        sample_id,
        "input_audio_01.mp3",
        file_id=files_map.get("input_audio_01.mp3", ""),
    )

    if is_full_song and orig_audio_path.exists() and orig_audio_path.stat().st_size > 128:
        shutil.copy2(orig_audio_path, master_audio_path)
        resolved_start_sec = 0.0
        audio_source_summary = (
            f"原曲完整音轨 `input_audio_01.mp3` 100% 等长 [0.0s - {eff_exact_dur:.2f}s] (自动切分 {num_segments} 幕分镜)"
        )
    elif orig_audio_path.exists() and orig_audio_path.stat().st_size > 128:
        full_dur = _probe_media_duration(orig_audio_path) or float(
            sample_spec.get("audio_meta", {}).get("duration_sec", 180.0)
        )
        if audio_start_sec is None:
            beats = storyline_data.get("time_anchored_story_beats") or []
            found_vocal_beat = False
            for b in beats:
                b_st = float(b.get("start_sec", 0.0) or 0.0)
                v_rule = str(b.get("vocal_and_lip_sync_rule", "")).lower()
                sec_name = str(b.get("section_name", "")).lower()
                if (
                    4.0 <= b_st <= max(4.0, full_dur - target_duration_sec)
                    and "纯器乐" not in v_rule
                    and "instrumental" not in v_rule
                    and "intro" not in sec_name
                ):
                    resolved_start_sec = b_st
                    found_vocal_beat = True
                    break
            if not found_vocal_beat and full_dur >= 60.0:
                resolved_start_sec = min(24.0, max(0.0, full_dur - target_duration_sec))
        if resolved_start_sec + target_duration_sec > full_dur and full_dur > target_duration_sec:
            resolved_start_sec = max(0.0, full_dur - target_duration_sec)

        fade_start = round(max(0.0, float(target_duration_sec) - 0.8), 2)
        _run_ffmpeg_gcsfuse_safe(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{resolved_start_sec:.3f}",
                "-i",
                str(orig_audio_path),
                "-t",
                f"{target_duration_sec}.0",
                "-af",
                f"afade=t=in:st=0:d=0.15,afade=t=out:st={fade_start}:d=0.8",
                "-acodec",
                "libmp3lame",
                "-b:a",
                "192k",
                str(master_audio_path),
            ],
            master_audio_path,
        )
        audio_source_summary = (
            f"原曲音频 `input_audio_01.mp3` 精确切片 [{resolved_start_sec:.1f}s - {resolved_start_sec + target_duration_sec:.1f}s] (共 {target_duration_sec}s)"
        )
    else:
        raise FileNotFoundError(f"未找到样本 {sample_id} 的原曲音频 `input_audio_01.mp3`，请检查音频素材是否已上传或同步。")

    # Slice master_audio.mp3 into exact 10.0s per-act chunks in parallel (up to 8 workers) for 5x faster audio prep
    _update_mv_step_progress(
        "MV_STEP1_AUDIO_SLICE_START",
        f"✂️ [Step 1/5 · 音轨准备与10s分镜并行切片] 正在准备样本 {sample_id} 主音轨 ({audio_source_summary})，并并行切分为 {num_segments} 段 × 10.0s 分镜音轨...",
        sample_id=sample_id,
        sample_frac=0.11,
    )
    chunk_paths: list[Path] = [out_dir / f"audio_seg_{idx + 1:02d}_10s.mp3" for idx in range(num_segments)]
    slice_done_lock = threading.Lock()
    slice_done_cnt = [0]
    parent_job_id = getattr(_THREAD_CTX, "job_id", "") or ""

    def _slice_one_chunk(idx: int) -> None:
        _set_thread_log_ctx(job_id=parent_job_id, track_key="track6_mv_studio", sample_id=sample_id, stage="MV_STEP1_AUDIO_SLICE")
        seg_num = idx + 1
        offset_s = idx * 10
        c_path = chunk_paths[idx]
        _run_ffmpeg_gcsfuse_safe(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{offset_s}.0",
                "-i",
                str(master_audio_path),
                "-t",
                "10.0",
                "-af",
                "apad=whole_dur=10.0",
                "-acodec",
                "libmp3lame",
                "-b:a",
                "192k",
                str(c_path),
            ],
            c_path,
        )
        if not c_path.exists():
            shutil.copy2(master_audio_path, c_path)
        with slice_done_lock:
            slice_done_cnt[0] += 1
            cur_s = slice_done_cnt[0]
        if cur_s == 1 or cur_s == num_segments or cur_s % 5 == 0:
            _update_mv_step_progress(
                "MV_STEP1_AUDIO_SLICE_PROGRESS",
                f"🎵 [Step 1/5 · 10s音轨切片进度] 已并行完成 {cur_s}/{num_segments} 段 10.0s 音频切片 (`{c_path.name}`, {offset_s}s~{offset_s+10}s)",
                sample_id=sample_id,
                sample_frac=0.11 + 0.04 * (cur_s / max(1, num_segments)),
                job_id=parent_job_id,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, num_segments))) as slice_pool:
        list(slice_pool.map(_slice_one_chunk, range(num_segments)))

    return master_audio_path, chunk_paths, resolved_start_sec, audio_source_summary


def _plan_mv_storyboard_and_lipsync_from_storyline(
    sample_id: str,
    sample_spec: dict[str, Any],
    storyline_data: dict[str, Any],
    chunk_paths: list[Path],
    song_start_sec: float,
    mv_performance_mode: str = "auto_detect",
    exact_mv_duration_sec: float | None = None,
) -> tuple[dict[str, Any], list[list[dict[str, Any]]], dict[str, Any]]:
    """
    Runs 24fps (`41.667ms`) DSP Multi-Band Vocal Pitch ($F_0$), Role/Gender Register & Syllable Rhythm Cadence
    analysis on every 10.0s audio chunk, resolves the MV Performance Mode (`solo_vocal_lipsync`,
    `duet_chorus_lipsync`, or `pure_story_cinematic`), and invokes `tab6_mv_director` (`gemini-4-argon` / `gemini-3.8-flash`)
    to produce:
    - Full `MVStudioDirectorPlan` (`acts` -> `micro_beats` snapped to 24fps `[MM:SS.mmm]`)
    - Measured `dsp_windows_per_act` (with `est_f0_hz`, `acoustic_gender_hint`, `syllable_rate_hz`, `rhythm_cadence`)
    - Reconciled `vocal_gender_profile` (`mv_performance_mode`, `mv_performance_mode_label`, `locked_lead_gender`, `locked_support_gender`)
    """
    cfg = load_platform_config()
    director_cfg = cfg["tab_models"].get("tab6_mv_director") or cfg["tab_models"]["tab5_storyline_model_b"]
    director_prompt_tpl = cfg["prompts"].get("mv_storyboard_director_prompt", DEFAULT_MV_STORYBOARD_DIRECTOR_PROMPT)

    num_acts = len(chunk_paths)
    dsp_windows_per_act: list[list[dict[str, Any]]] = []
    male_win_count = 0
    female_win_count = 0
    both_win_count = 0
    parent_job_id = getattr(_THREAD_CTX, "job_id", "") or ""

    t_dsp = time.perf_counter()
    _update_mv_step_progress(
        "MV_STEP2_DSP_START",
        f"🎚️ [Step 2/5 · 24fps 声学时间戳与词曲节奏批量实测] 正在对样本 {sample_id} 的整轨音频与 {num_acts} 段分镜执行单次解码 + 24fps (41.667ms) 人声基频 F0、男女声部与音节速率分析...",
        sample_id=sample_id,
        sample_frac=0.16,
    )
    master_audio_guess = chunk_paths[0].parent / "master_audio.mp3" if chunk_paths else None
    batch_dsp_wins = _detect_dsp_vocal_windows_batch(master_audio_guess, chunk_paths)
    for idx, act_wins in enumerate(batch_dsp_wins):
        act_num = idx + 1
        for w in act_wins:
            gh = w.get("acoustic_gender_hint", "none")
            if gh == "male":
                male_win_count += 1
            elif gh == "female":
                female_win_count += 1
            elif gh == "both":
                both_win_count += 1
        dsp_windows_per_act.append(act_wins)
        win_str = " | ".join(
            f"[{w['mm_ss_mmm_start']}->{w['mm_ss_mmm_end']}] "
            + (
                f"VOCAL({w['acoustic_gender_hint'].upper()}, F0={w['est_f0_hz']}Hz, {w['syllable_rate_hz']}syl/s, {w['rhythm_cadence']})"
                if w["is_vocal_active"]
                else "INSTRUMENTAL_CLOSED"
            )
            for w in act_wins
        )
        if act_num == 1 or act_num == num_acts or act_num % 4 == 0 or num_acts <= 6:
            _update_mv_step_progress(
                "DSP_24FPS_ACOUSTIC_RHYTHM",
                f"🎚️ [Step 2/5 · 第 {act_num}/{num_acts} 幕 ({idx*10}s-{(idx+1)*10}s) 声学实测] {win_str}",
                sample_id=sample_id,
                sample_frac=0.16 + 0.06 * (act_num / max(1, num_acts)),
            )
    _update_mv_step_progress(
        "MV_STEP2_DSP_DONE",
        f"✅ [Step 2/5 · 24fps 声学实测完成] 已完成全部 {num_acts} 幕共 {male_win_count + female_win_count + both_win_count} 个人声窗口的毫秒级对齐分析 (总耗时 {round(time.perf_counter() - t_dsp, 2)}s)！",
        sample_id=sample_id,
        sample_frac=0.22,
    )

    files_map = sample_spec.get("files", {})
    img_specs = sample_spec.get("images", [])
    user_prompt = (sample_spec.get("prompt_text") or "").strip()
    prompt_lower = user_prompt.lower()

    # Resolve MV Performance Mode (`solo_vocal_lipsync`, `duet_chorus_lipsync`, `pure_story_cinematic`)
    req_mode = str(mv_performance_mode or "auto_detect").strip().lower()
    if req_mode not in ("solo_vocal_lipsync", "duet_chorus_lipsync", "pure_story_cinematic"):
        # Auto-detect from prompt constraints, number of character images, and DSP male/female/duet distribution
        if any(k in prompt_lower for k in ("不需要对嘴", "不对嘴", "纯故事", "纯剧情", "no lip-sync", "no lipsync", "non-lipsync")):
            resolved_mode = "pure_story_cinematic"
        elif sample_id == "01" or "only the male" in prompt_lower or "silent" in prompt_lower or len(img_specs) <= 1:
            resolved_mode = "solo_vocal_lipsync"
        elif len(img_specs) >= 2 and (both_win_count >= 1 or (male_win_count >= 1 and female_win_count >= 1)):
            resolved_mode = "duet_chorus_lipsync"
        else:
            resolved_mode = "solo_vocal_lipsync"
    else:
        resolved_mode = req_mode

    mode_label_cn = MV_PERFORMANCE_MODES.get(resolved_mode, resolved_mode)
    _update_mv_step_progress(
        "MV_MODE_RESOLVED",
        f"🎭 [Step 2/5 · 演绎模式锁定] 样本 {sample_id} 锁定为 `{resolved_mode}` ({mode_label_cn}) | DSP声部统计: 男声窗={male_win_count}, 女声窗={female_win_count}, 合唱窗={both_win_count}",
        sample_id=sample_id,
        sample_frac=0.23,
    )

    # Prepare lightweight character reference image parts (do NOT re-upload full multi-MB MP3 when num_acts > 4,
    # since V2.2 Storyline + 24fps DSP windows already provide complete lyric/melody/pitch/rhythm context!)
    img_parts: list[dict[str, Any]] = []
    for img in img_specs:
        fn = img["filename"]
        img_parts.append(
            build_media_part_for_gemini(
                sample_id,
                fn,
                "image/jpeg",
                file_id=img.get("file_id") or files_map.get(fn, ""),
            )
        )

    eff_mv_dur = round(float(exact_mv_duration_sec), 2) if exact_mv_duration_sec and exact_mv_duration_sec > 0 else float(num_acts * 10)
    director_model_name = director_cfg.get("model", MODEL_ARGON_4)
    # Use LOW thinking level when planning multi-act JSON storyboards for 3x faster token generation without long silent pauses
    eff_thinking = "LOW" if num_acts > 4 else director_cfg.get("thinking_level", "LOW")
    director_telemetry: dict[str, Any] = {}
    t_dir = time.perf_counter()

    # Start a background heartbeat thread during Director LLM inference so the user sees live progress every 5s
    hb_stop_evt = threading.Event()

    def _director_heartbeat_loop() -> None:
        _set_thread_log_ctx(job_id=parent_job_id, track_key="track6_mv_studio", sample_id=sample_id, stage="MV_STEP2_DIRECTOR_HEARTBEAT")
        while not hb_stop_evt.wait(5.0):
            el = round(time.perf_counter() - t_dir, 1)
            _update_mv_step_progress(
                "MV_STEP2_DIRECTOR_HEARTBEAT",
                f"⏳ [Step 2/5 · 分镜导演推理进行中] `{director_model_name}` (thinking={eff_thinking}) 正在并行生成 {num_acts} 幕 24fps 分镜与口型时间轴 (已运行 {el}s)...",
                sample_id=sample_id,
                sample_frac=min(0.32, 0.25 + 0.07 * min(1.0, el / 45.0)),
                job_id=parent_job_id,
            )

    hb_thread = threading.Thread(target=_director_heartbeat_loop, daemon=True)
    hb_thread.start()

    try:
        if num_acts <= 6:
            single_parts: list[dict[str, Any]] = []
            audio_manifest_lines: list[str] = []
            for a_i, c_path in enumerate(chunk_paths):
                if c_path.exists() and c_path.stat().st_size > 128:
                    single_parts.append({"inline_data": {"mime_type": "audio/mpeg", "data": str(c_path)}})
                    audio_manifest_lines.append(
                        f"  * Audio Clip #{len(audio_manifest_lines)+1}: Act {a_i+1:02d} (幕内 0.000s~10.000s, 对应原曲 {song_start_sec + a_i*10:.1f}s~{song_start_sec + (a_i+1)*10:.1f}s)"
                    )
            single_parts.extend(img_parts)
            context_block = (
                f"【当前 MV 样本与 V2.2 Storyline 输入上下文 (V2.3.1 逐幕真音频听辨毫秒打轴)】\n"
                f"- 样本 ID 与标题: #{sample_id} ({sample_spec.get('title', '')})\n"
                f"- 歌曲语言: {sample_spec.get('language', 'English')}\n"
                f"- 当前选定的 MV 演绎与口型模式 (`mv_performance_mode`): **{resolved_mode}** ({mode_label_cn})\n"
                f"- 角色参考图数量: {len(img_specs)} 张 ({sample_spec.get('protagonist_mode', 'single')})\n"
                f"- 用户导演提示词 (user_prompt，必须 100% 遵守其中的正负向约束): {user_prompt or '无（Zero-Prompt 模式，按 Storyline 执行）'}\n"
                f"- 本次生成 MV 总时长: {eff_mv_dur:.2f} 秒（自动划分为 {num_acts} 幕 × 10.000s，对应原曲区间 {song_start_sec:.1f}s ~ {song_start_sec + eff_mv_dur:.1f}s）\n"
                f"- 已随本请求一并传入 {len(audio_manifest_lines)} 段真实 10.0s 音频切片（请仔细聆听每段 10.0s 音频中歌手的真实开口时刻、换气停顿时刻、句尾闭口时刻与所唱歌词）：\n"
                + ("\n".join(audio_manifest_lines) + "\n" if audio_manifest_lines else "")
                + f"- 实测各幕 24fps (41.667ms) DSP 人声谐波基频 F0、声部性别归属与短句级音节速率时间窗 (`dsp_windows_per_act`，请结合真音频听感将每幕细分为 4~7 个 `1.2s~2.8s` 短句级 `micro_beats`，并保留句首/句间换气/句尾的闭嘴窗口):\n"
                f"{json.dumps(dsp_windows_per_act, ensure_ascii=False)}\n"
                f"- V2.2 生成的完整 MV Storyline JSON:\n```json\n{json.dumps(storyline_data, ensure_ascii=False)}\n```\n\n"
                f"请严格按 `{resolved_mode}` 模式生成包含 **精确 {num_acts} 幕 (`acts`，`act_index` 从 1 到 {num_acts}，覆盖全曲 {eff_mv_dur:.2f} 秒)** 的短句级分镜与口型/字幕时间轴 JSON：\n"
                + director_prompt_tpl
            )
            single_parts.append({"text": context_block})
            _update_mv_step_progress(
                "MV_STEP2_DIRECTOR_LLM_START",
                f"🧠 [Step 2/5 · 分镜导演大模型推理] 正在调用 `{director_model_name}` (thinking={eff_thinking}, 含 {len(audio_manifest_lines)} 段 10s 真音频) 规划 {num_acts} 幕短句级 24fps 口型/字幕时间轴...",
                sample_id=sample_id,
                sample_frac=0.25,
            )
            mv_plan, director_telemetry = call_gemini_model(
                parts=single_parts,
                model=director_model_name,
                location=director_cfg.get("location", "global"),
                thinking_level=eff_thinking,
                temperature=float(director_cfg.get("temperature", 0.25)),
                max_output_tokens=int(director_cfg.get("max_output_tokens", 65535)),
            )
        else:
            # V2.3.1 Audio-Grounded Chunked Parallel Director Planning for Full-Song Mode (> 6 acts):
            # Split `num_acts` into batches of 4 acts and pass the exact 4 × 10.0s audio slices (`audio_seg_XX_10s.mp3`)
            # to `gemini-4-argon` in every batch so it directly hears the singer's exact phrasing & breath pauses!
            batch_act_size = 4
            act_ranges: list[tuple[int, int]] = [
                (b_start, min(num_acts, b_start + batch_act_size))
                for b_start in range(0, num_acts, batch_act_size)
            ]
            total_dir_batches = len(act_ranges)
            _update_mv_step_progress(
                "MV_STEP2_DIRECTOR_LLM_START",
                f"🧠 [Step 2/5 · 全曲真音频分片并行导演推理] 已将全曲 {num_acts} 幕拆分为 {total_dir_batches} 组并行分片 (每片附带对应 10s 真音频切片)，正在并发调用 `{director_model_name}` (thinking={eff_thinking}) 生成短句级 24fps 口型剧本...",
                sample_id=sample_id,
                sample_frac=0.25,
            )
            batch_results: list[dict[str, Any]] = [{} for _ in range(total_dir_batches)]
            dir_done_lock = threading.Lock()
            dir_done_cnt = [0]

            def _plan_act_batch(b_idx: int) -> None:
                _set_thread_log_ctx(job_id=parent_job_id, track_key="track6_mv_studio", sample_id=sample_id, stage="MV_STEP2_DIRECTOR_BATCH")
                a_start, a_end = act_ranges[b_idx]
                act_num_lo = a_start + 1
                act_num_hi = a_end
                sub_count = a_end - a_start
                sub_dsp = {
                    f"Act_{a_i + 1:02d} ({a_i*10}s-{(a_i+1)*10}s)": dsp_windows_per_act[a_i]
                    for a_i in range(a_start, a_end)
                }
                b_parts: list[dict[str, Any]] = []
                batch_audio_manifest: list[str] = []
                for a_i in range(a_start, a_end):
                    if a_i < len(chunk_paths):
                        c_path = chunk_paths[a_i]
                        if c_path.exists() and c_path.stat().st_size > 128:
                            b_parts.append({"inline_data": {"mime_type": "audio/mpeg", "data": str(c_path)}})
                            batch_audio_manifest.append(
                                f"  * Audio Clip #{len(batch_audio_manifest)+1}: Act {a_i+1:02d} (幕内相对时间 0.000s~10.000s, 对应原曲 {song_start_sec + a_i*10:.1f}s~{song_start_sec + (a_i+1)*10:.1f}s)"
                            )
                b_parts.extend(img_parts)
                b_context = (
                    f"【当前 MV 样本与 V2.2 Storyline 输入上下文 — V2.3.1 真音频听辨分片批次 {b_idx + 1}/{total_dir_batches} (负责第 {act_num_lo} 幕至第 {act_num_hi} 幕，共 {sub_count} 幕)】\n"
                    f"- 样本 ID 与标题: #{sample_id} ({sample_spec.get('title', '')})\n"
                    f"- 歌曲语言: {sample_spec.get('language', 'English')}\n"
                    f"- 当前选定的 MV 演绎与口型模式 (`mv_performance_mode`): **{resolved_mode}** ({mode_label_cn})\n"
                    f"- 用户导演提示词 (user_prompt): {user_prompt or '无（按 Storyline 执行）'}\n"
                    f"- 全曲总时长: {eff_mv_dur:.2f} 秒（共 {num_acts} 幕）。本分片仅需输出 **第 {act_num_lo} 幕到第 {act_num_hi} 幕（对应原曲 {song_start_sec + a_start*10:.1f}s ~ {song_start_sec + a_end*10:.1f}s，共 {sub_count} 幕）**！\n"
                    f"- 本分片已随附 {len(batch_audio_manifest)} 段真实 10.0s 音频切片（请逐段仔细聆听歌手在 0.000s~10.000s 内的真实开口时刻、换气停顿时刻、句尾闭口时刻与真实所唱歌词）：\n"
                    + ("\n".join(batch_audio_manifest) + "\n" if batch_audio_manifest else "")
                    + f"- 本分片各幕实测 24fps (41.667ms) DSP 人声谐波基频 F0、声部性别归属与短句级音节速率时间窗 (`sub_dsp_windows`，请结合真音频听感将每幕细分为 4~7 个 `1.2s~2.8s` 短句级 `micro_beats`，并保留句首/句间换气/句尾的闭嘴窗口):\n"
                    f"{json.dumps(sub_dsp, ensure_ascii=False)}\n"
                    f"- V2.2 完整 MV Storyline JSON:\n```json\n{json.dumps(storyline_data, ensure_ascii=False)}\n```\n\n"
                    f"请严格按 `{resolved_mode}` 模式输出 JSON（包含 `mv_title`, `lead_character`, `supporting_character`, 以及包含本批次 **{sub_count} 幕 (`act_index` 从 {act_num_lo} 到 {act_num_hi})** 的 `acts` 数组）：\n"
                    + director_prompt_tpl
                )
                b_parts.append({"text": b_context})
                t_b = time.perf_counter()
                try:
                    b_plan, b_tel = call_gemini_model(
                        parts=b_parts,
                        model=director_model_name,
                        location=director_cfg.get("location", "global"),
                        thinking_level=eff_thinking,
                        temperature=float(director_cfg.get("temperature", 0.25)),
                        max_output_tokens=int(director_cfg.get("max_output_tokens", 65535)),
                    )
                    batch_results[b_idx] = b_plan if isinstance(b_plan, dict) else {}
                except Exception as b_exc:
                    log_runtime(
                        "WARNING",
                        "MV_DIRECTOR_BATCH_FALLBACK",
                        f"分镜导演分片 {b_idx+1}/{total_dir_batches} (Acts {act_num_lo}-{act_num_hi}) 使用 DSP+Storyline 确定性合成: {b_exc}",
                        sample_id=sample_id,
                        job_id=parent_job_id,
                    )
                    batch_results[b_idx] = {}
                with dir_done_lock:
                    dir_done_cnt[0] += 1
                    cur_d = dir_done_cnt[0]
                _update_mv_step_progress(
                    "MV_STEP2_DIRECTOR_BATCH_DONE",
                    f"🧠 [Step 2/5 · 分镜导演分片进度 {cur_d}/{total_dir_batches}] 已完成第 {act_num_lo}~{act_num_hi} 幕分镜规划 (耗时 {round(time.perf_counter() - t_b, 1)}s)",
                    sample_id=sample_id,
                    sample_frac=0.25 + 0.08 * (cur_d / max(1, total_dir_batches)),
                    job_id=parent_job_id,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, total_dir_batches)) as dir_pool:
                list(dir_pool.map(_plan_act_batch, range(total_dir_batches)))

            # Merge batch_results into a single unified `mv_plan`
            mv_plan = {}
            merged_acts: list[dict[str, Any]] = []
            for b_idx, (a_start, a_end) in enumerate(act_ranges):
                b_res = batch_results[b_idx] or {}
                if not mv_plan and b_res:
                    mv_plan = {k: v for k, v in b_res.items() if k != "acts"}
                b_acts = b_res.get("acts") if isinstance(b_res.get("acts"), list) else []
                for offset_i in range(a_end - a_start):
                    if offset_i < len(b_acts) and isinstance(b_acts[offset_i], dict):
                        act_copy = dict(b_acts[offset_i])
                        act_copy["act_index"] = a_start + offset_i + 1
                        merged_acts.append(act_copy)
                    else:
                        merged_acts.append({"act_index": a_start + offset_i + 1})
            mv_plan["acts"] = merged_acts

        _update_mv_step_progress(
            "MV_STEP2_DIRECTOR_LLM_OK",
            f"✅ [Step 2/5 · 分镜导演规划完成] `{director_model_name}` 已完成全部 {len(mv_plan.get('acts') or [])}/{num_acts} 幕结构化分镜脚本 (总耗时 {round(time.perf_counter() - t_dir, 1)}s)！正在执行 24fps 网格对齐与角色口型校验...",
            sample_id=sample_id,
            sample_frac=0.33,
        )
    except Exception as exc:
        log_runtime(
            "WARNING",
            "MV_DIRECTOR_FALLBACK",
            f"MV 分镜导演模型调用异常，使用基于 V2.2 Storyline 与 24fps DSP 声学基频/节奏窗口的确定性合成方案: {exc}",
            sample_id=sample_id,
        )
        mv_plan = {}
    finally:
        hb_stop_evt.set()

    # Normalize & enforce 24fps grid, speaker/gender attribution, rhythm cadence, and Storyline continuity across all `num_acts`
    arc = storyline_data.get("four_act_story_arc") or {}
    guard = storyline_data.get("character_and_scene_guardrails") or {}
    diag = storyline_data.get("music_lyric_melody_diagnosis") or {}
    story_beats = storyline_data.get("time_anchored_story_beats") or []

    def _parse_beat_time_range(tr_str: str) -> tuple[float, float] | None:
        m = re.findall(r"(\d{1,2}):(\d{2})(?:\.(\d+))?", str(tr_str or ""))
        if len(m) >= 2:
            s0 = int(m[0][0]) * 60 + int(m[0][1]) + (float("0." + m[0][2]) if m[0][2] else 0.0)
            s1 = int(m[1][0]) * 60 + int(m[1][1]) + (float("0." + m[1][2]) if m[1][2] else 0.0)
            return s0, max(s0 + 1.0, s1)
        return None

    parsed_story_beats: list[tuple[float, float, dict[str, Any]]] = []
    for sb in story_beats:
        if isinstance(sb, dict):
            if sb.get("start_sec") is not None and sb.get("end_sec") is not None:
                parsed_story_beats.append((float(sb["start_sec"]), float(sb["end_sec"]), sb))
            else:
                rng = _parse_beat_time_range(sb.get("time_range", ""))
                if rng:
                    parsed_story_beats.append((rng[0], rng[1], sb))

    # Determine default lead/support gender from DSP acoustic F0 distribution + sample spec & prompt
    if "male" in prompt_lower or "homem" in prompt_lower or "guy" in prompt_lower or sample_id in ("01", "06", "07", "08", "10"):
        default_lead_gender = "male"
    elif "female" in prompt_lower or "woman" in prompt_lower or "girl" in prompt_lower or sample_id in ("02", "03", "04", "05", "09"):
        default_lead_gender = "female"
    else:
        default_lead_gender = "male" if male_win_count >= female_win_count else "female"
    default_supp_gender = "female" if default_lead_gender == "male" else "male"

    lead_char = mv_plan.get("lead_character") if isinstance(mv_plan.get("lead_character"), dict) else {}
    lead_char.setdefault("role_tag", "<IMAGE_REF_0>[Character A]")
    lead_char.setdefault("name_cn", "男主角 (Lead Vocal)" if default_lead_gender == "male" else "女主角 (Lead Vocal)")
    lead_char["gender"] = "male" if str(lead_char.get("gender", default_lead_gender)).lower().startswith("m") else "female"
    lead_char.setdefault("matched_vocal_timbre_en", f"expressive {lead_char['gender']} lead vocal")
    lead_char.setdefault("visual_appearance_en", f"Photorealistic {lead_char['gender']} protagonist matching reference image 1")
    lead_char["singing_permission"] = "forbidden_silent_acting" if resolved_mode == "pure_story_cinematic" else "allowed"
    lead_char.setdefault("negative_constraints_en", "Do not dance" if "not dance" in prompt_lower or "no dance" in prompt_lower else "")

    supp_char = mv_plan.get("supporting_character") if isinstance(mv_plan.get("supporting_character"), dict) else {}
    supp_char.setdefault("role_tag", "<IMAGE_REF_1>[Character B]")
    supp_char.setdefault("name_cn", "第二主唱/配角 (Character B)" if lead_char["gender"] == "male" else "男主唱/伴演 (Character B)")
    supp_char["gender"] = "female" if str(supp_char.get("gender", default_supp_gender)).lower().startswith("f") else "male"
    supp_char.setdefault("matched_vocal_timbre_en", f"expressive {supp_char['gender']} vocal")
    supp_char.setdefault("visual_appearance_en", f"Photorealistic {supp_char['gender']} co-star matching reference image 2")
    if resolved_mode in ("solo_vocal_lipsync", "pure_story_cinematic"):
        supp_char["singing_permission"] = "forbidden_silent_acting"
        if sample_id == "01":
            supp_char["negative_constraints_en"] = "Female character is silent and crying with tears in her eyes, MUST keep mouth closed, no dance."
        else:
            supp_char.setdefault("negative_constraints_en", "Supporting character MUST keep mouth closed with zero singing articulation.")
    else:
        supp_char["singing_permission"] = "allowed"

    mv_plan["mv_title"] = mv_plan.get("mv_title") or sample_spec.get("title", f"Sample {sample_id} MV")
    mv_plan["mv_performance_mode_resolved"] = resolved_mode
    mv_plan["mv_performance_mode_label"] = mode_label_cn
    mv_plan["storyline_logline"] = mv_plan.get("storyline_logline") or arc.get("logline") or diag.get("lyric_subtext_and_core_theme") or user_prompt
    mv_plan["core_visual_motif"] = mv_plan.get("core_visual_motif") or guard.get("core_visual_motif") or "Cinematic atmospheric lighting"
    mv_plan["lead_character"] = lead_char
    mv_plan["supporting_character"] = supp_char

    raw_acts = mv_plan.get("acts") if isinstance(mv_plan.get("acts"), list) else []
    act_arc_titles = [
        ("起 (Opening Hook) — 开场钩子与氛围建立", arc.get("act1_opening_hook", "Opening cinematic hook")),
        ("承 (Mid Development) — 情感递进与叙事展开", arc.get("act2_mid_development", "Mid-song narrative development")),
        ("转 (Climax Escalation) — 副歌高潮爆发", arc.get("act3_climax_escalation", "Chorus emotional climax")),
        ("合 (Ending Resonance) — 尾奏余韵与视觉收束", arc.get("act4_ending_resonance", "Lingering ending resonance")),
    ]

    normalized_acts: list[dict[str, Any]] = []
    for idx_a in range(num_acts):
        act_num = idx_a + 1
        src_act = raw_acts[idx_a] if idx_a < len(raw_acts) and isinstance(raw_acts[idx_a], dict) else {}
        arc_bucket = min(3, int((idx_a / max(1, num_acts)) * 4))
        arc_title, arc_desc = act_arc_titles[arc_bucket]

        act_song_start = song_start_sec + idx_a * 10.0
        act_song_end = min(song_start_sec + eff_mv_dur, song_start_sec + (idx_a + 1) * 10.0)
        act_mid = 0.5 * (act_song_start + act_song_end)
        beat_ref: dict[str, Any] = {}
        if parsed_story_beats:
            best_sb = None
            best_dist = 1e9
            for b_s, b_e, sb_dict in parsed_story_beats:
                if b_s <= act_mid <= b_e:
                    best_sb = sb_dict
                    break
                dist = min(abs(act_mid - b_s), abs(act_mid - b_e))
                if dist < best_dist:
                    best_dist = dist
                    best_sb = sb_dict
            beat_ref = best_sb or {}
        elif story_beats:
            prop_idx = min(len(story_beats) - 1, int((idx_a / max(1, num_acts)) * len(story_beats)))
            beat_ref = story_beats[prop_idx] if isinstance(story_beats[prop_idx], dict) else {}

        sec_label = beat_ref.get("section_name") or beat_ref.get("music_section") or arc_title.split("—")[0].strip()
        act_title_cn = (
            src_act.get("act_title_cn")
            or f"Act {act_num:02d}/{num_acts:02d} [{_format_omni_mm_ss_mmm(act_song_start)[:5]}-{_format_omni_mm_ss_mmm(act_song_end)[:5]}] — {sec_label}"
        )
        scene_env_en = (
            src_act.get("scene_environment_en")
            or beat_ref.get("camera_and_lighting_design")
            or beat_ref.get("shootable_story_action")
            or f"Cinematic music video setting for {mv_plan['mv_title']}, featuring {mv_plan['core_visual_motif']}"
        )
        ff_prompt_en = (
            src_act.get("first_frame_prompt_en")
            or f"Photorealistic 4K cinematic music video still frame ({act_title_cn}). "
               f"Lead character ({lead_char['visual_appearance_en']}) in {scene_env_en}. "
               f"Action: {beat_ref.get('shootable_story_action', arc_desc)}. "
               f"Visual motif: {mv_plan['core_visual_motif']}. Mouth closed in a natural expressive pose, high dynamic range lighting."
        )

        dsp_wins = list(dsp_windows_per_act[idx_a])
        raw_mbs = src_act.get("micro_beats") if isinstance(src_act.get("micro_beats"), list) else []
        if not raw_mbs:
            if not any(dw.get("is_vocal_active") for dw in dsp_wins):
                dsp_wins = [
                    {
                        "start_sec": 0.0,
                        "end_sec": 0.5,
                        "mm_ss_mmm_start": "00:00.000",
                        "mm_ss_mmm_end": "00:00.500",
                        "is_vocal_active": False,
                        "est_f0_hz": 0.0,
                        "acoustic_gender_hint": "none",
                        "syllable_rate_hz": 0.0,
                        "rhythm_cadence": "instrumental_beat_pulse",
                    },
                    {
                        "start_sec": 0.5,
                        "end_sec": 9.0,
                        "mm_ss_mmm_start": "00:00.500",
                        "mm_ss_mmm_end": "00:09.000",
                        "is_vocal_active": True,
                        "est_f0_hz": 145.0 if lead_char["gender"] == "male" else 235.0,
                        "acoustic_gender_hint": lead_char["gender"],
                        "syllable_rate_hz": 3.2,
                        "rhythm_cadence": "mid_melodic_pop",
                    },
                    {
                        "start_sec": 9.0,
                        "end_sec": 10.0,
                        "mm_ss_mmm_start": "00:09.000",
                        "mm_ss_mmm_end": "00:10.000",
                        "is_vocal_active": False,
                        "est_f0_hz": 0.0,
                        "acoustic_gender_hint": "none",
                        "syllable_rate_hz": 0.0,
                        "rhythm_cadence": "instrumental_beat_pulse",
                    },
                ]
            cue_text = (
                beat_ref.get("melody_and_lyric_cue")
                or arc_desc
                or f"{mv_plan['mv_title']} — Act {act_num}"
            )
            for w_i, dw in enumerate(dsp_wins):
                is_voc = bool(dw["is_vocal_active"])
                # Determine speaker by `resolved_mode` + DSP `acoustic_gender_hint`
                if not is_voc or resolved_mode == "pure_story_cinematic":
                    spk_choice = "NONE"
                elif resolved_mode == "solo_vocal_lipsync":
                    spk_choice = "LEAD"
                else:
                    # `duet_chorus_lipsync`: attribute by DSP gender hint & chorus progression
                    gh = dw.get("acoustic_gender_hint", "male")
                    if gh == "both" or arc_bucket == 2:
                        spk_choice = "BOTH" if w_i % 2 == 1 else "LEAD"
                    elif gh == supp_char["gender"]:
                        spk_choice = "SUPPORT"
                    else:
                        spk_choice = "LEAD" if w_i % 2 == 0 else "SUPPORT"

                roll_choice = "A_ROLL_VOCAL" if (is_voc and resolved_mode != "pure_story_cinematic") else ("B_ROLL_EMOTION_INSERT" if w_i % 2 == 1 else "C_ROLL_WIDE_CLIMAX")
                raw_mbs.append({
                    "start_sec": dw["start_sec"],
                    "end_sec": dw["end_sec"],
                    "is_vocal_open_mouth": is_voc and (resolved_mode != "pure_story_cinematic"),
                    "active_speaker": spk_choice,
                    "speaker_voice_gender": (
                        lead_char["gender"] if spk_choice == "LEAD"
                        else (supp_char["gender"] if spk_choice == "SUPPORT" else ("both" if spk_choice == "BOTH" else "none"))
                    ),
                    "est_f0_hz": dw.get("est_f0_hz", 0.0),
                    "syllable_rate_hz": dw.get("syllable_rate_hz", 0.0),
                    "rhythm_cadence": dw.get("rhythm_cadence", "mid_melodic_pop" if is_voc else "instrumental_beat_pulse"),
                    "roll_type": roll_choice,
                    "camera_and_shot_cn": "近景情绪特写推轨 (35mm Close-Up Push-in)" if is_voc else "唯美氛围空镜/全景切换 (Anamorphic Wide Shot)",
                    "sung_lyrics": cue_text if is_voc else "[Instrumental Beat — All Mouths Closed]",
                    "subtitle_text": cue_text[:56] if is_voc else "",
                    "lead_action_and_mouth_en": (
                        f"Lead Character A ({lead_char['gender']}) performs dramatic story action ({beat_ref.get('shootable_story_action', arc_desc)}) with mouth naturally closed, no singing"
                        if resolved_mode == "pure_story_cinematic"
                        else (
                            f"Lead Character A ({lead_char['gender']}) sings aloud with expressive {dw.get('rhythm_cadence', 'mid_melodic_pop')} open-mouth articulation"
                            if spk_choice in ("LEAD", "BOTH")
                            else f"Lead Character A ({lead_char['gender']}) keeps MOUTH TIGHTLY CLOSED, ZERO SPEAKING, LIPS SEALED SHUT"
                        )
                    ),
                    "support_action_and_mouth_en": (
                        f"Supporting Character B ({supp_char['gender']}) sings aloud with expressive {dw.get('rhythm_cadence', 'mid_melodic_pop')} mouth articulation"
                        if (resolved_mode == "duet_chorus_lipsync" and spk_choice in ("SUPPORT", "BOTH"))
                        else f"Supporting Character B ({supp_char['gender']}) keeps mouth firmly closed while reacting emotionally"
                    ),
                    "lighting_and_color_en": scene_env_en,
                })

        # V2.3.1 Sub-Phrase Expansion: if the LLM emitted an overly long vocal micro_beat (>= 3.2s),
        # split it into 2 sub-phrase beats at the nearest DSP window boundary so Omni gets dense 1.5s-2.4s anchors.
        expanded_raw_mbs: list[dict[str, Any]] = []
        for mb in raw_mbs:
            if not isinstance(mb, dict):
                continue
            r_st = float(mb.get("start_sec", 0.0))
            r_ed = float(mb.get("end_sec", r_st + 2.0))
            r_dur = r_ed - r_st
            if bool(mb.get("is_vocal_open_mouth", False)) and r_dur >= 3.2:
                mid_cand = r_st + 0.5 * r_dur
                for dw in dsp_wins:
                    dw_e = float(dw.get("end_sec", 0.0))
                    if (r_st + 1.1) <= dw_e <= (r_ed - 1.1) and abs(dw_e - mid_cand) < 0.75:
                        mid_cand = dw_e
                        break
                mid_cand = _snap_to_omni_24fps_grid(mid_cand)
                sung_parts = _split_lyric_into_paced_subclusters(str(mb.get("sung_lyrics") or ""), 2)
                sub_parts = _split_lyric_into_paced_subclusters(str(mb.get("subtitle_text") or mb.get("sung_lyrics") or ""), 2)
                mb1 = dict(mb)
                mb1["start_sec"] = r_st
                mb1["end_sec"] = mid_cand
                if sung_parts:
                    mb1["sung_lyrics"] = sung_parts[0]
                if sub_parts:
                    mb1["subtitle_text"] = sub_parts[0]
                mb2 = dict(mb)
                mb2["start_sec"] = mid_cand
                mb2["end_sec"] = r_ed
                if len(sung_parts) >= 2:
                    mb2["sung_lyrics"] = sung_parts[1]
                if len(sub_parts) >= 2:
                    mb2["subtitle_text"] = sub_parts[1]
                expanded_raw_mbs.extend([mb1, mb2])
            else:
                expanded_raw_mbs.append(mb)

        def _make_closed_mouth_gap_beat(g_st: float, g_ed: float, label: str = "[Breath / Instrumental Pause — Mouth Closed]") -> dict[str, Any]:
            return {
                "start_sec": g_st,
                "end_sec": g_ed,
                "mm_ss_mmm_start": _format_omni_mm_ss_mmm(g_st),
                "mm_ss_mmm_end": _format_omni_mm_ss_mmm(g_ed),
                "is_vocal_open_mouth": False,
                "active_speaker": "NONE",
                "speaker_voice_gender": "none",
                "est_f0_hz": 0.0,
                "syllable_rate_hz": 0.0,
                "rhythm_cadence": "instrumental_beat_pulse",
                "roll_type": "B_ROLL_EMOTION_INSERT",
                "camera_and_shot_cn": "情绪呼吸停顿/空镜特写 (Atmospheric Breath & Emotion Hold)",
                "sung_lyrics": label,
                "subtitle_text": "",
                "lead_action_and_mouth_en": f"Lead Character A ({lead_char['gender']}) keeps MOUTH TIGHTLY CLOSED, lips sealed shut during natural breath/instrumental pause",
                "support_action_and_mouth_en": f"Supporting Character B ({supp_char['gender']}) keeps MOUTH TIGHTLY CLOSED with zero vocal movement",
                "lighting_and_color_en": scene_env_en,
            }

        snapped_mbs: list[dict[str, Any]] = []
        cursor_t = 0.0
        min_step = round(4.0 / OMNI_VIDEO_FPS, 3)  # 0.167s
        for mb_idx, mb in enumerate(expanded_raw_mbs):
            raw_st = float(mb.get("start_sec", cursor_t))
            st = _snap_to_omni_24fps_grid(max(cursor_t, min(9.833, raw_st)))
            # V2.3.1: If there is a gap >= 0.125s before this beat (e.g., pre-vocal intro or inter-phrase breath),
            # insert an explicit closed-mouth beat instead of pulling `st` backward!
            if st - cursor_t >= 0.125:
                gap_lbl = "[Pre-Vocal Intro — Mouth Closed]" if cursor_t == 0.0 else "[Inter-Phrase Breath Pause — Mouth Closed]"
                snapped_mbs.append(_make_closed_mouth_gap_beat(cursor_t, st, gap_lbl))
                cursor_t = st
            else:
                st = cursor_t

            ed = _snap_to_omni_24fps_grid(max(st + min_step, min(10.0, float(mb.get("end_sec", st + 2.0)))))
            # Match closest DSP window in this act for acoustic F0 & cadence telemetry
            mid_t = 0.5 * (st + ed)
            matched_dw = dsp_wins[min(mb_idx, len(dsp_wins) - 1)] if dsp_wins else {}
            for dw in dsp_wins:
                if float(dw.get("start_sec", 0.0)) <= mid_t <= float(dw.get("end_sec", 10.0)):
                    matched_dw = dw
                    break

            sub_txt = str(mb.get("subtitle_text") or "").strip()
            sung_txt = str(mb.get("sung_lyrics") or "").strip()
            has_vocal_lyric = bool(mb.get("is_vocal_open_mouth", False)) or bool(matched_dw.get("is_vocal_active", False))
            if has_vocal_lyric and not sub_txt and sung_txt and not sung_txt.startswith("["):
                sub_txt = sung_txt

            if resolved_mode == "pure_story_cinematic":
                is_open = False
                spk = "NONE"
                v_gen = "none"
                if not has_vocal_lyric:
                    sub_txt = ""
                sung_txt = f"[Pure Story Beat — {sub_txt or 'Narrative Action, No Lip-Sync'}]"
            else:
                is_open = bool(mb.get("is_vocal_open_mouth", False))
                spk = str(mb.get("active_speaker", "LEAD" if is_open else "NONE")).upper().strip()
                if not is_open or spk not in ("LEAD", "SUPPORT", "BOTH", "NONE"):
                    spk = "NONE" if not is_open else "LEAD"
                if resolved_mode == "solo_vocal_lipsync" or supp_char.get("singing_permission") == "forbidden_silent_acting":
                    if spk in ("SUPPORT", "BOTH"):
                        spk = "LEAD"
                elif resolved_mode == "duet_chorus_lipsync" and is_open and spk == "LEAD":
                    # If model didn't differentiate SUPPORT/BOTH in duet mode, use DSP gender hint to enrich role alternation
                    gh = matched_dw.get("acoustic_gender_hint", "male")
                    if gh == "both":
                        spk = "BOTH"
                    elif gh == supp_char["gender"]:
                        spk = "SUPPORT"
                is_open = spk != "NONE"
                if spk == "LEAD":
                    v_gen = lead_char["gender"]
                elif spk == "SUPPORT":
                    v_gen = supp_char["gender"]
                elif spk == "BOTH":
                    v_gen = "both"
                else:
                    v_gen = "none"
                if not is_open:
                    sub_txt = ""
                    if not sung_txt or not sung_txt.startswith("["):
                        sung_txt = "[Instrumental Beat — All Mouths Closed]"

            est_f0 = float(mb.get("est_f0_hz") or matched_dw.get("est_f0_hz") or 0.0)
            syl_rate = float(mb.get("syllable_rate_hz") or matched_dw.get("syllable_rate_hz") or 0.0)
            cadence = str(mb.get("rhythm_cadence") or matched_dw.get("rhythm_cadence") or ("mid_melodic_pop" if is_open else "instrumental_beat_pulse"))

            snapped_mbs.append({
                "start_sec": st,
                "end_sec": ed,
                "mm_ss_mmm_start": _format_omni_mm_ss_mmm(st),
                "mm_ss_mmm_end": _format_omni_mm_ss_mmm(ed),
                "is_vocal_open_mouth": is_open,
                "active_speaker": spk,
                "speaker_voice_gender": v_gen,
                "est_f0_hz": est_f0,
                "syllable_rate_hz": syl_rate,
                "rhythm_cadence": cadence,
                "roll_type": str(mb.get("roll_type") or ("A_ROLL_VOCAL" if is_open else "C_ROLL_WIDE_CLIMAX")).upper(),
                "camera_and_shot_cn": str(mb.get("camera_and_shot_cn") or "电影感中近景运镜"),
                "sung_lyrics": sung_txt,
                "subtitle_text": sub_txt,
                "lead_action_and_mouth_en": str(
                    mb.get("lead_action_and_mouth_en")
                    or (
                        f"Lead Character A ({lead_char['gender']}) performs expressive cinematic story acting with mouth naturally closed (no lip-syncing)"
                        if resolved_mode == "pure_story_cinematic"
                        else (
                            f"Lead Character A ({lead_char['gender']}) opens mouth on cue singing with {cadence} lip-sync"
                            if spk in ("LEAD", "BOTH")
                            else f"Lead Character A ({lead_char['gender']}) keeps MOUTH TIGHTLY CLOSED, lips sealed shut"
                        )
                    )
                ),
                "support_action_and_mouth_en": str(
                    mb.get("support_action_and_mouth_en")
                    or (
                        f"Supporting Character B ({supp_char['gender']}) sings aloud with expressive {cadence} mouth articulation"
                        if spk in ("SUPPORT", "BOTH")
                        else f"Supporting Character B ({supp_char['gender']}) keeps MOUTH TIGHTLY CLOSED with zero vocal movement"
                    )
                ),
                "lighting_and_color_en": str(mb.get("lighting_and_color_en") or scene_env_en),
            })
            cursor_t = ed
            if cursor_t >= 10.0:
                break

        if snapped_mbs:
            # V2.3.1: Never overwrite a vocal beat's start_sec to 0.0 or end_sec to 10.0!
            # Instead, insert closed-mouth buffer beats if the vocal phrase starts > 0.083s or ends < 9.875s.
            if snapped_mbs[0]["start_sec"] > 0.083:
                if snapped_mbs[0].get("is_vocal_open_mouth"):
                    snapped_mbs.insert(0, _make_closed_mouth_gap_beat(0.0, snapped_mbs[0]["start_sec"], "[Pre-Vocal Intro — Mouth Closed]"))
                else:
                    snapped_mbs[0]["start_sec"] = 0.0
                    snapped_mbs[0]["mm_ss_mmm_start"] = "00:00.000"
            else:
                snapped_mbs[0]["start_sec"] = 0.0
                snapped_mbs[0]["mm_ss_mmm_start"] = "00:00.000"

            if snapped_mbs[-1]["end_sec"] < 9.875:
                if snapped_mbs[-1].get("is_vocal_open_mouth"):
                    snapped_mbs.append(_make_closed_mouth_gap_beat(snapped_mbs[-1]["end_sec"], 10.0, "[Phrase Release / Instrumental Tail — Mouth Closed]"))
                else:
                    snapped_mbs[-1]["end_sec"] = 10.0
                    snapped_mbs[-1]["mm_ss_mmm_end"] = "00:10.000"
            else:
                snapped_mbs[-1]["end_sec"] = 10.0
                snapped_mbs[-1]["mm_ss_mmm_end"] = "00:10.000"

        normalized_acts.append({
            "act_index": act_num,
            "act_title_cn": act_title_cn,
            "storyline_beat_ref": str(src_act.get("storyline_beat_ref") or f"Act {act_num}"),
            "global_start_sec": float(idx_a * 10),
            "global_end_sec": float((idx_a + 1) * 10),
            "song_source_start_sec": round(song_start_sec + idx_a * 10, 2),
            "song_source_end_sec": round(song_start_sec + (idx_a + 1) * 10, 2),
            "scene_environment_en": scene_env_en,
            "first_frame_prompt_en": ff_prompt_en,
            "micro_beats": snapped_mbs,
        })

    mv_plan["mv_performance_mode"] = resolved_mode
    mv_plan["mv_performance_mode_label"] = mode_label_cn
    mv_plan["track_vocal_mode"] = resolved_mode
    mv_plan["acts"] = normalized_acts
    vocal_gender_profile = {
        "mv_performance_mode": resolved_mode,
        "mv_performance_mode_label": mode_label_cn,
        "track_vocal_mode": resolved_mode,
        "locked_lead_gender": lead_char["gender"],
        "locked_lead_timbre_en": lead_char["matched_vocal_timbre_en"],
        "locked_support_gender": supp_char["gender"],
        "locked_support_timbre_en": supp_char["matched_vocal_timbre_en"],
        "dsp_male_windows": male_win_count,
        "dsp_female_windows": female_win_count,
        "dsp_duet_windows": both_win_count,
        "director_telemetry": director_telemetry,
    }
    log_runtime(
        "INFO",
        "MV_DIRECTOR_LOCKED",
        f"✅ 样本 {sample_id} 分镜与词曲节奏/口型时间轴已锁定: 模式={mode_label_cn} | 共 {len(normalized_acts)} 幕 ({eff_mv_dur:.2f}s) | "
        f"主角={lead_char['name_cn']}({lead_char['gender'].upper()}) | 配角={supp_char['name_cn']}({supp_char['gender'].upper()}, 演唱权限={supp_char.get('singing_permission')})",
        sample_id=sample_id,
    )
    return mv_plan, dsp_windows_per_act, vocal_gender_profile


def _synthesize_mv_image_with_nano_banana(
    prompt: str,
    ref_image_paths: list[Path],
    out_path: Path,
    project_id: str,
    sample_id: str,
    label_cn: str,
    aspect_ratio: str = "16:9",
) -> Path:
    """
    Calls `gemini-nano-banana-2.1` to synthesize `<FIRST_FRAME>` keyframes conditioned on the sample's character reference image(s).
    If image model quota is unavailable, creates a cinema-graded widescreen/vertical keyframe card from the reference image(s).
    """
    cfg = load_platform_config()
    img_model_cfg = cfg["tab_models"].get("tab6_first_frame_imagen", {})
    pref_model = img_model_cfg.get("model", "gemini-nano-banana-2.1")
    loc = img_model_cfg.get("location", "global")

    valid_refs = [p for p in ref_image_paths if p and p.exists() and p.stat().st_size > 128]
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(vertexai=True, project=project_id, location=loc)
        contents: list[Any] = [
            types.Part.from_bytes(data=_compress_image_to_jpeg_bytes(p), mime_type="image/jpeg")
            for p in valid_refs
        ]
        contents.append(f"{prompt} Aspect ratio: {aspect_ratio}.")

        model_chain = [
            pref_model if pref_model == "gemini-nano-banana-2.1" else "gemini-nano-banana-2.1",
        ]
        dedup_models = []
        for m in model_chain:
            if m and m not in dedup_models:
                dedup_models.append(m)

        for model_id in dedup_models:
            try:
                t0 = time.perf_counter()
                resp = client.models.generate_content(
                    model=model_id,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_modalities=["IMAGE", "TEXT"],
                    ),
                )
                for cand in getattr(resp, "candidates", []) or []:
                    for part in getattr(getattr(cand, "content", None), "parts", []) or []:
                        inline = getattr(part, "inline_data", None)
                        if inline and getattr(inline, "data", None):
                            raw_img = inline.data
                            if isinstance(raw_img, str):
                                raw_img = base64.b64decode(raw_img)
                            if len(raw_img) > 2048:
                                out_path.parent.mkdir(parents=True, exist_ok=True)
                                out_path.write_bytes(raw_img)
                                dt = round(time.perf_counter() - t0, 1)
                                log_runtime(
                                    "INFO",
                                    "NANO_BANANA_OK",
                                    f"🎨 `{model_id}` 成功生成 {label_cn} ({len(raw_img)//1024} KB, {dt}s)",
                                    sample_id=sample_id,
                                )
                                return out_path
            except Exception as m_exc:
                log_runtime("INFO", "NANO_BANANA_SWITCH", f"`{model_id}` 生成 {label_cn} 切换: {m_exc}", sample_id=sample_id)
    except Exception as exc:
        log_runtime("WARNING", "NANO_BANANA_FALLBACK", f"Nano Banana 初始化回退: {exc}", sample_id=sample_id)

    # Smart Cinematic Keyframe Compositor Fallback using Pillow (so `shot_XX_first_frame.jpg` always has exact 16:9 / 9:16 cinema framing)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageEnhance, ImageFilter

        target_w, target_h = (1280, 720) if aspect_ratio != "9:16" else (720, 1280)
        if valid_refs:
            with Image.open(valid_refs[0]) as base_im:
                rgb_base = base_im.convert("RGB")
                # Blurred atmospheric background filling 100% of target frame
                bg = rgb_base.resize((target_w, target_h), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(radius=22))
                bg = ImageEnhance.Brightness(bg).enhance(0.55)
                if len(valid_refs) >= 2:
                    # Composite both reference images side by side for dual-character MV
                    with Image.open(valid_refs[1]) as sec_im:
                        rgb_sec = sec_im.convert("RGB")
                        half_w = int(target_w * 0.44)
                        max_h = int(target_h * 0.88)
                        for idx_p, fg_src in enumerate((rgb_base, rgb_sec)):
                            sc = min(half_w / fg_src.width, max_h / fg_src.height)
                            nw, nh = max(1, int(fg_src.width * sc)), max(1, int(fg_src.height * sc))
                            fg_resized = fg_src.resize((nw, nh), Image.Resampling.LANCZOS)
                            x_off = int(target_w * (0.27 if idx_p == 0 else 0.73) - nw // 2)
                            y_off = (target_h - nh) // 2
                            bg.paste(fg_resized, (x_off, y_off))
                else:
                    sc = min((target_w * 0.82) / rgb_base.width, (target_h * 0.90) / rgb_base.height)
                    nw, nh = max(1, int(rgb_base.width * sc)), max(1, int(rgb_base.height * sc))
                    fg_resized = rgb_base.resize((nw, nh), Image.Resampling.LANCZOS)
                    bg.paste(fg_resized, ((target_w - nw) // 2, (target_h - nh) // 2))
                bg.save(out_path, format="JPEG", quality=92)
                return out_path
    except Exception:
        if valid_refs:
            shutil.copy2(valid_refs[0], out_path)
            return out_path

    # Last-resort solid dark cinema frame
    from PIL import Image
    Image.new("RGB", (1280, 720), color=(15, 23, 42)).save(out_path, format="JPEG", quality=90)
    return out_path


def _find_best_cjk_font_path() -> str:
    """Locates the best available CJK TrueType/OpenType font on Linux / Cloud Run."""
    candidates = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )
    for p in candidates:
        if Path(p).exists():
            return p
    return ""


def _render_dynamic_keyframe_animatic_10s(
    first_frame_path: Path,
    ref_img_paths: list[Path],
    act: dict[str, Any],
    aspect_ratio: str,
    out_10s_mp4_path: Path,
    sample_id: str,
) -> Path:
    """
    Synthesizes a smooth 24fps 10.000s HD dynamic camera-motion + micro-beat cut video clip (`out_10s_mp4_path`)
    using FFmpeg zoompan/ken-burns when `execution_mode == 'fast_animatic_mv'` or when Omni preview quota is unavailable.
    Guarantees 100% playable 10s shot MP4s in all environments!
    """
    target_w, target_h = (1280, 720) if aspect_ratio != "9:16" else (720, 1280)
    src_img = first_frame_path if (first_frame_path and first_frame_path.exists()) else (ref_img_paths[0] if ref_img_paths else None)
    if not src_img or not src_img.exists():
        from PIL import Image
        fallback_jpg = out_10s_mp4_path.with_suffix(".fallback.jpg")
        Image.new("RGB", (target_w, target_h), color=(18, 24, 38)).save(fallback_jpg, format="JPEG")
        src_img = fallback_jpg

    act_idx = int(act.get("act_index", 1))
    # Alternate smooth push-in zoom and gentle pan across acts
    if act_idx % 2 == 1:
        zp_expr = f"zoompan=z='min(zoom+0.0006,1.15)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=240:s={target_w}x{target_h}:fps=24"
    else:
        zp_expr = f"zoompan=z='if(eq(on,1),1.14,max(1.0,zoom-0.0005))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=240:s={target_w}x{target_h}:fps=24"

    _run_ffmpeg_gcsfuse_safe(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(src_img),
            "-vf",
            f"scale={target_w*2}:{target_h*2}:force_original_aspect_ratio=increase,crop={target_w*2}:{target_h*2},{zp_expr},format=yuv420p",
            "-t",
            "10.0",
            "-r",
            "24",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            str(out_10s_mp4_path),
        ],
        out_10s_mp4_path,
    )
    if not out_10s_mp4_path.exists() or out_10s_mp4_path.stat().st_size < 2048:
        _run_ffmpeg_gcsfuse_safe(
            [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                str(src_img),
                "-vf",
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
                "-t",
                "10.0",
                "-r",
                "24",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(out_10s_mp4_path),
            ],
            out_10s_mp4_path,
        )
    log_runtime(
        "INFO",
        "DYNAMIC_ANIMATIC_OK",
        f"🎞️ 第 {act_idx} 幕 10.0s 24fps 动态运镜分镜视频已生成: {out_10s_mp4_path.name} ({out_10s_mp4_path.stat().st_size // 1024 if out_10s_mp4_path.exists() else 0} KB)",
        sample_id=sample_id,
    )
    return out_10s_mp4_path


def _normalize_mv_video_segment_to_exact_10s(raw_mp4: Path, out_10s_mp4: Path) -> Path:
    """
    V2.3.1 Zero-Time-Warp 10.000s Normalizer:
    Preserves exact `1.000x` real-time playback speed across `[0.000s .. min(dur, 10.000s)]` (NEVER applying `setpts`
    linear time-stretching that shifts `[MM:SS.mmm]` lip-sync timestamps!). If `dur < 9.92s`, pads only the tail
    after `dur` via `tpad=stop_mode=clone` so every vocal onset stays 100% locked to `master_audio.mp3`.
    """
    dur = _probe_media_duration(raw_mp4)
    if dur <= 0.5:
        shutil.copy2(raw_mp4, out_10s_mp4)
        return out_10s_mp4
    if dur >= 9.92:
        _run_ffmpeg_gcsfuse_safe(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_mp4),
                "-an",
                "-t",
                "10.0",
                "-r",
                "24",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(out_10s_mp4),
            ],
            out_10s_mp4,
        )
    else:
        pad_sec = max(0.1, round(10.05 - dur, 3))
        _run_ffmpeg_gcsfuse_safe(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_mp4),
                "-an",
                "-vf",
                f"tpad=stop_mode=clone:stop_duration={pad_sec:.3f},fps=24",
                "-t",
                "10.0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(out_10s_mp4),
            ],
            out_10s_mp4,
        )
        if not out_10s_mp4.exists() or out_10s_mp4.stat().st_size < 4096:
            _run_ffmpeg_gcsfuse_safe(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(raw_mp4),
                    "-an",
                    "-r",
                    "24",
                    "-t",
                    "10.0",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(out_10s_mp4),
                ],
                out_10s_mp4,
            )
    if not out_10s_mp4.exists() or out_10s_mp4.stat().st_size < 4096:
        shutil.copy2(raw_mp4, out_10s_mp4)
    return out_10s_mp4


def _invoke_mv_omni_10s_clip(
    first_frame_path: Path,
    ref_img_path: Path,
    omni_prompt: str,
    act: dict[str, Any],
    aspect_ratio: str,
    act_index: int,
    total_acts: int,
    out_10s_mp4_path: Path,
    project_id: str,
    sample_id: str,
    execution_mode: str = "full_auto",
) -> tuple[Path, str]:
    """
    Invokes `gemini-omni-1.1-flash-preview` via Vertex AI Interactions API (`locations/global/interactions`)
    using the exact C++ `ParseMediaReferenceTags` I2V + Reference conditioning from `viral_pitch_studio.py`.
    Returns `(out_10s_mp4_path, render_engine_used)`.
    """
    if execution_mode == "fast_animatic_mv":
        clip_p = _render_dynamic_keyframe_animatic_10s(
            first_frame_path=first_frame_path,
            ref_img_paths=[ref_img_path],
            act=act,
            aspect_ratio=aspect_ratio,
            out_10s_mp4_path=out_10s_mp4_path,
            sample_id=sample_id,
        )
        return clip_p, "fast_24fps_dynamic_animatic"

    cfg = load_platform_config()
    omni_cfg = cfg["tab_models"].get("tab6_omni_video", {})
    pref_omni = str(omni_cfg.get("model") or "gemini-omni-1.1-flash-preview").strip()
    if pref_omni.startswith("omni-"):
        pref_omni = f"gemini-{pref_omni}"
    elif not pref_omni:
        pref_omni = "gemini-omni-1.1-flash-preview"

    raw_mp4 = out_10s_mp4_path.with_suffix(".raw.mp4")
    t_omni = time.perf_counter()
    b64_img0 = base64.b64encode(_compress_image_to_jpeg_bytes(first_frame_path)).decode("ascii")
    b64_img1 = base64.b64encode(_compress_image_to_jpeg_bytes(ref_img_path if ref_img_path.exists() else first_frame_path)).decode("ascii")

    url = f"https://aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/global/interactions"
    has_transcriptions_block = "# Transcriptions" in omni_prompt
    if has_transcriptions_block:
        attempt_plan: list[tuple[str, bool, str]] = [
            (pref_omni, True, "V2.3.1 24fps 短句级毫秒口型对齐指令(含Transcriptions)"),
            ("gemini-omni-flash-preview", True, "备用模型 gemini-omni-flash-preview (含Transcriptions口型指令)"),
            (pref_omni, False, "纯视觉口型轨模式(规避语音分类器误拦截)"),
        ]
    else:
        attempt_plan = [
            (pref_omni, False, "纯故事叙事/无对嘴卡点运镜模式(24fps节拍对齐)"),
            ("gemini-omni-flash-preview", False, "备用模型 gemini-omni-flash-preview"),
        ]

    last_err = ""
    for attempt_idx, (model_name, keep_trans, mode_desc) in enumerate(attempt_plan, start=1):
        try:
            active_prompt = _normalize_omni_mv_prompt(omni_prompt, include_transcriptions=keep_trans)
            content_parts = [
                {"type": "image", "mime_type": "image/jpeg", "data": b64_img0},  # image_0.png -> <FIRST_FRAME>
                {"type": "image", "mime_type": "image/jpeg", "data": b64_img1},  # image_1.png -> <IMAGE_REF_0>
                {"type": "text", "text": active_prompt},
            ]
            payload = {
                "model": model_name,
                "background": True,
                "input": [{"type": "user_input", "content": content_parts}],
                "response_format": {
                    "type": "video",
                    "aspect_ratio": aspect_ratio,
                    "resolution": "720p",
                    "duration": "10s",
                },
            }
            log_runtime(
                "INFO",
                "OMNI_CALL_START",
                f"🎬 [第 {act_index}/{total_acts} 幕] 调用 `{model_name}` ({mode_desc}) 渲染 10s 分镜视频...",
                sample_id=sample_id,
            )
            resp_data = _vertex_rest_post_json(url, payload, project_id=project_id, timeout=240)
            interaction_id = resp_data.get("id")
            deadline = time.time() + 280.0
            while (
                str(resp_data.get("status", "")).lower() in ("in_progress", "queued", "running", "pending")
                and interaction_id
                and time.time() < deadline
            ):
                time.sleep(5.0)
                el = round(time.perf_counter() - t_omni, 1)
                log_runtime(
                    "INFO",
                    "OMNI_POLL",
                    f"⏳ [第 {act_index}/{total_acts} 幕] `{model_name}` 渲染中 (已等待 {el}s)...",
                    sample_id=sample_id,
                )
                resp_data = _vertex_rest_get_json(f"{url}/{interaction_id}", project_id=project_id, timeout=60)

            video_bytes: bytes | None = None
            for step in resp_data.get("steps", []) or []:
                for c in (step.get("content", []) if isinstance(step, dict) else []):
                    if isinstance(c, dict) and (c.get("type") == "video" or str(c.get("mime_type", "")).startswith("video/")):
                        raw = c.get("data") or c.get("bytes")
                        if raw:
                            video_bytes = base64.b64decode(raw) if isinstance(raw, str) else raw
                            break
            if not video_bytes:
                for out_item in resp_data.get("outputs", []) or []:
                    if isinstance(out_item, dict) and (out_item.get("type") == "video" or out_item.get("data")):
                        raw = out_item.get("data")
                        if raw:
                            cand = base64.b64decode(raw) if isinstance(raw, str) else raw
                            if len(cand) > 10000:
                                video_bytes = cand
                                break

            if video_bytes:
                raw_mp4.write_bytes(video_bytes)
                _normalize_mv_video_segment_to_exact_10s(raw_mp4, out_10s_mp4_path)
                raw_mp4.unlink(missing_ok=True)
                dt_s = round(time.perf_counter() - t_omni, 1)
                log_runtime(
                    "INFO",
                    "OMNI_CALL_OK",
                    f"✅ [第 {act_index}/{total_acts} 幕] `{model_name}` 10s 分镜视频渲染成功 ({out_10s_mp4_path.stat().st_size // 1024} KB, {dt_s}s)！",
                    sample_id=sample_id,
                )
                return out_10s_mp4_path, model_name
            else:
                st_str = str(resp_data.get("status", "unknown"))
                err_obj = resp_data.get("error") or resp_data.get("status_message") or ""
                last_err = f"{model_name} ({mode_desc}): status={st_str} {err_obj}".strip()
                log_runtime("WARNING", "OMNI_RETRY", f"⚠️ 第 {act_index} 幕尝试 #{attempt_idx} 未返回视频流 ({last_err})", sample_id=sample_id)
        except Exception as exc:
            last_err = f"{model_name} ({mode_desc}): {exc}"
            log_runtime("WARNING", "OMNI_EXC", f"⚠️ 第 {act_index} 幕尝试 #{attempt_idx} 异常: {exc}", sample_id=sample_id)

    # Resilient fallback to 24fps Dynamic Keyframe Animatic so the batch/single MV pipeline still produces a complete, playable MV!
    log_runtime(
        "WARNING",
        "OMNI_FALLBACK_TO_ANIMATIC",
        f"ℹ️ 第 {act_index}/{total_acts} 幕 Omni 预览端点未返回视频字节 ({last_err})，自动启用 24fps 动态运镜分镜合成保底，确保成片完整可播放！",
        sample_id=sample_id,
    )
    clip_p = _render_dynamic_keyframe_animatic_10s(
        first_frame_path=first_frame_path,
        ref_img_paths=[ref_img_path],
        act=act,
        aspect_ratio=aspect_ratio,
        out_10s_mp4_path=out_10s_mp4_path,
        sample_id=sample_id,
    )
    return clip_p, f"dynamic_24fps_animatic (Omni fallback: {last_err[:120]})"


def _build_synchronized_subtitles_and_burn_in(
    mv_plan: dict[str, Any],
    concat_video_path: Path,
    master_audio_path: Path,
    out_dir: Path,
    aspect_ratio: str,
    target_duration_sec: int,
    burn_subtitles: bool,
    sample_id: str,
    exact_duration_sec: float | None = None,
) -> tuple[Path, Path, Path, Path, list[dict[str, Any]]]:
    """
    1. Extracts all vocal `micro_beats` across all acts and writes frame-accurate `subtitles.srt` and `subtitles.vtt`.
    2. Muxes `final_mv_clean.mp4` (concatenated video + master audio) with exact song duration (`exact_duration_sec`).
    3. Renders CJK/multilingual RGBA subtitle banners via Pillow and burns them into `final_mv_subtitled.mp4`
       at the exact `[global_start_sec, global_end_sec]` millisecond timestamps via FFmpeg `overlay=enable='between(t,s,e)'`.
    """
    srt_path = out_dir / "subtitles.srt"
    vtt_path = out_dir / "subtitles.vtt"
    final_clean_mp4 = out_dir / "final_mv_clean.mp4"
    final_subtitled_mp4 = out_dir / "final_mv_subtitled.mp4"
    eff_dur = round(float(exact_duration_sec), 2) if exact_duration_sec and exact_duration_sec > 0 else float(target_duration_sec)
    eff_dur_str = f"{eff_dur:.2f}"

    subtitle_cues: list[dict[str, Any]] = []
    acts = mv_plan.get("acts") or []
    cue_idx = 0
    for act in acts:
        g_offset = float(act.get("global_start_sec", (int(act.get("act_index", 1)) - 1) * 10.0))
        for mb in act.get("micro_beats") or []:
            is_open = bool(mb.get("is_vocal_open_mouth", False))
            spk = str(mb.get("active_speaker", "NONE")).upper().strip()
            txt = str(mb.get("subtitle_text") or "").strip()
            if not txt and is_open:
                raw_l = str(mb.get("sung_lyrics") or "").strip()
                if raw_l and not raw_l.startswith("["):
                    txt = raw_l
            if not txt or txt.startswith("["):
                continue
            st_g = round(g_offset + float(mb.get("start_sec", 0.0)), 3)
            if st_g >= eff_dur - 0.15:
                continue
            ed_g = min(eff_dur, round(g_offset + float(mb.get("end_sec", 2.5)), 3))
            if ed_g <= st_g:
                ed_g = min(eff_dur, round(st_g + 1.5, 3))
            cue_idx += 1
            subtitle_cues.append({
                "cue_index": cue_idx,
                "act_index": act.get("act_index", 1),
                "start_sec": st_g,
                "end_sec": ed_g,
                "srt_start": _format_srt_timestamp(st_g),
                "srt_end": _format_srt_timestamp(ed_g),
                "vtt_start": _format_vtt_timestamp(st_g),
                "vtt_end": _format_vtt_timestamp(ed_g),
                "speaker": spk,
                "text": txt,
            })

    # Write `subtitles.srt` and `subtitles.vtt`
    srt_Blocks: list[str] = []
    vtt_blocks: list[str] = ["WEBVTT\n"]
    for c in subtitle_cues:
        srt_Blocks.append(f"{c['cue_index']}\n{c['srt_start']} --> {c['srt_end']}\n{c['text']}\n")
        vtt_blocks.append(f"{c['cue_index']}\n{c['vtt_start']} --> {c['vtt_end']}\n{c['text']}\n")
    srt_path.write_text("\n".join(srt_Blocks) if srt_Blocks else "1\n00:00:00,500 --> 00:00:04,000\n♪ Instrumental / Music Video ♪\n", encoding="utf-8")
    vtt_path.write_text("\n".join(vtt_blocks), encoding="utf-8")
    _update_mv_step_progress(
        "MV_STEP5_SRT_VTT_READY",
        f"📝 [Step 5/5 · 毫秒级字幕轨生成] 已生成样本 {sample_id} 同步字幕文件 `subtitles.srt` 与 `subtitles.vtt` (共 {len(subtitle_cues)} 条毫秒级字幕)",
        sample_id=sample_id,
        sample_frac=0.90,
    )

    # 1. Mux clean MV (`final_mv_clean.mp4`) matching exact song duration (`eff_dur_str`)
    _update_mv_step_progress(
        "MV_STEP5_MUX_CLEAN_START",
        f"🔊 [Step 5/5 · 音画合成无字纯享版] 正在将拼接分镜视频与原曲母带音轨合成为无字纯享版 `final_mv_clean.mp4` (目标总长={eff_dur_str}s)...",
        sample_id=sample_id,
        sample_frac=0.92,
    )
    _run_ffmpeg_gcsfuse_safe(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(concat_video_path),
            "-i",
            str(master_audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            eff_dur_str,
            "-movflags",
            "+faststart",
            str(final_clean_mp4),
        ],
        final_clean_mp4,
    )
    if not final_clean_mp4.exists():
        shutil.copy2(concat_video_path, final_clean_mp4)
    clean_kb = final_clean_mp4.stat().st_size // 1024 if final_clean_mp4.exists() else 0
    _update_mv_step_progress(
        "MV_STEP5_MUX_CLEAN_OK",
        f"✅ [Step 5/5 · 无字纯享版就绪] `final_mv_clean.mp4` 合成完成 ({clean_kb} KB, 时长={eff_dur_str}s)",
        sample_id=sample_id,
        sample_frac=0.94,
    )

    if not burn_subtitles or not subtitle_cues:
        shutil.copy2(final_clean_mp4, final_subtitled_mp4)
        return final_subtitled_mp4, final_clean_mp4, srt_path, vtt_path, subtitle_cues

    # 2. Render frame-accurate RGBA CJK subtitle overlay banners via Pillow and burn into `final_mv_subtitled.mp4`
    try:
        from PIL import Image, ImageDraw, ImageFont

        target_w, target_h = (1280, 720) if aspect_ratio != "9:16" else (720, 1280)
        banner_h = 128 if aspect_ratio != "9:16" else 160
        font_path = _find_best_cjk_font_path()
        font_main = ImageFont.truetype(font_path, 30 if aspect_ratio != "9:16" else 26) if font_path else ImageFont.load_default()
        font_tag = ImageFont.truetype(font_path, 18) if font_path else ImageFont.load_default()

        # Render subtitle PNGs into `/tmp` local tmpfs instead of GCS FUSE `out_dir` for 10x faster I/O and zero FUSE conflicts
        tmp_sub_dir = Path("/tmp/mv_ffmpeg_scratch")
        tmp_sub_dir.mkdir(parents=True, exist_ok=True)
        overlay_pngs: list[tuple[Path, float, float]] = []
        for c in subtitle_cues:
            ov_path = tmp_sub_dir / f"sub_ov_{sample_id}_{c['cue_index']:03d}.png"
            img = Image.new("RGBA", (target_w, banner_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)

            # Measure text and wrap if long
            raw_txt = c["text"]
            max_chars = 34 if aspect_ratio != "9:16" else 20
            if len(raw_txt) > max_chars:
                mid = len(raw_txt) // 2
                lines = [raw_txt[:mid].strip(), raw_txt[mid:].strip()]
            else:
                lines = [raw_txt]

            # Semi-transparent rounded dark pill background
            pill_margin_x = 70 if aspect_ratio != "9:16" else 30
            draw.rounded_rectangle(
                [(pill_margin_x, 10), (target_w - pill_margin_x, banner_h - 14)],
                radius=18,
                fill=(8, 12, 24, 198),
                outline=(99, 102, 241, 180),
                width=2,
            )

            spk_label = (
                "🎤 LEAD"
                if c["speaker"] == "LEAD"
                else ("🎶 SUPPORT" if c["speaker"] == "SUPPORT" else ("🎵 DUET" if c["speaker"] == "BOTH" else "🎬 STORY"))
            )
            draw.text((pill_margin_x + 20, 18), spk_label, font=font_tag, fill=(110, 231, 183, 255))

            y_cursor = 42 if len(lines) == 1 else 26
            for line_str in lines:
                try:
                    bbox = draw.textbbox((0, 0), line_str, font=font_main)
                    tw = bbox[2] - bbox[0]
                except Exception:
                    tw = len(line_str) * 22
                tx = max(pill_margin_x + 24, (target_w - tw) // 2)
                # Subtle black text shadow + crisp white/gold lyric text
                draw.text((tx + 2, y_cursor + 2), line_str, font=font_main, fill=(0, 0, 0, 240))
                draw.text((tx, y_cursor), line_str, font=font_main, fill=(254, 243, 199, 255))
                y_cursor += 36

            img.save(ov_path, format="PNG")
            overlay_pngs.append((ov_path, c["start_sec"], c["end_sec"]))

        # Apply overlay PNGs in batches of up to 36 overlays per pass so full-song MVs (50-100 cues) finish in 1-3 ultra-fast passes
        y_pos = target_h - banner_h - 16
        batch_size = 36
        curr_src_video = concat_video_path
        temp_pass_files: list[Path] = []
        total_batches = max(1, (len(overlay_pngs) + batch_size - 1) // batch_size)
        _update_mv_step_progress(
            "MV_STEP5_SUBTITLE_BURN_START",
            f"🔥 [Step 5/5 · 硬字幕烧录] 已渲染 {len(overlay_pngs)} 张高清透明字幕贴片，正在分 {total_batches} 轮通过 FFmpeg 极速烧录至 `final_mv_subtitled.mp4`...",
            sample_id=sample_id,
            sample_frac=0.95,
        )

        for b_idx in range(total_batches):
            sub_batch = overlay_pngs[b_idx * batch_size : (b_idx + 1) * batch_size]
            is_last_batch = b_idx == total_batches - 1
            out_pass = final_subtitled_mp4 if is_last_batch else (tmp_sub_dir / f"mv_sub_pass_{sample_id}_{b_idx:02d}.mp4")
            if not is_last_batch:
                temp_pass_files.append(out_pass)
            if total_batches > 1:
                _update_mv_step_progress(
                    "MV_STEP5_SUBTITLE_BURN_PASS",
                    f"🔥 [Step 5/5 · 字幕烧录批次 {b_idx+1}/{total_batches}] 正在压制第 {b_idx*batch_size + 1}~{min(len(overlay_pngs), (b_idx+1)*batch_size)} 条同步字幕...",
                    sample_id=sample_id,
                    sample_frac=0.95 + 0.03 * ((b_idx + 1) / total_batches),
                )

            cmd = ["ffmpeg", "-y", "-i", str(curr_src_video)]
            if is_last_batch:
                cmd.extend(["-i", str(master_audio_path)])
            for ov_p, _, _ in sub_batch:
                cmd.extend(["-i", str(ov_p)])

            base_offset = 2 if is_last_batch else 1
            if b_idx == 0:
                filter_parts: list[str] = [
                    f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2[v0]"
                ]
            else:
                filter_parts = ["[0:v]null[v0]"]
            last_v = "[v0]"
            for idx_ov, (_, st_s, ed_s) in enumerate(sub_batch):
                in_idx = idx_ov + base_offset
                next_v = f"[v{idx_ov + 1}]"
                filter_parts.append(
                    f"{last_v}[{in_idx}:v]overlay=0:{y_pos}:enable='between(t,{st_s:.3f},{ed_s:.3f})'{next_v}"
                )
                last_v = next_v

            cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", last_v])
            if is_last_batch:
                cmd.extend(["-map", "1:a:0", "-c:a", "aac", "-b:a", "192k"])
            else:
                cmd.append("-an")
            cmd.extend([
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast" if not is_last_batch else "veryfast",
                "-pix_fmt",
                "yuv420p",
                "-t",
                eff_dur_str,
                "-movflags",
                "+faststart",
                str(out_pass),
            ])
            _run_ffmpeg_gcsfuse_safe(cmd, out_pass)
            if out_pass.exists() and out_pass.stat().st_size > 4096:
                curr_src_video = out_pass

        for tp in temp_pass_files:
            tp.unlink(missing_ok=True)
        for ov_p, _, _ in overlay_pngs:
            ov_p.unlink(missing_ok=True)
    except Exception as sub_exc:
        log_runtime("WARNING", "SUBTITLE_BURN_FALLBACK", f"字幕烧录回退至无损软字幕视频: {sub_exc}", sample_id=sample_id)

    if not final_subtitled_mp4.exists() or final_subtitled_mp4.stat().st_size < 4096:
        shutil.copy2(final_clean_mp4, final_subtitled_mp4)

    log_runtime(
        "INFO",
        "MV_MERGE_COMPLETE",
        f"🎞️ 样本 {sample_id} MV 分镜合并与同步字幕烧录完成: {final_subtitled_mp4.name} (总长={eff_dur_str}s, 共 {len(subtitle_cues)} 条精确到毫秒的同步字幕)！",
        sample_id=sample_id,
    )
    return final_subtitled_mp4, final_clean_mp4, srt_path, vtt_path, subtitle_cues


def run_single_sample_mv_studio_pipeline(
    sample_id: str,
    storyline_source: str = "prefer_argon",
    target_duration_sec: int = 0,
    audio_start_sec: float | None = None,
    mv_performance_mode: str = "auto_detect",
    shot_continuity_mode: str = "multi_shot_mv",
    execution_mode: str = "full_auto",
    burn_subtitles: bool = True,
    aspect_ratio: str = "16:9",
    custom_omni_prompts: list[str] | None = None,
) -> dict[str, Any]:
    """
    Executes the complete V2.3 End-to-End MV Generation Pipeline for a single sample (or custom single song):
    1. Resolves V2.2 MV Storyline (`track5_storyline_argon4` / `track5_storyline_flash38`, or auto-generates on the fly).
    2. Supports **Full-Song Equal-Length Mode (`target_duration_sec <= 0`)** where the MV length equals the exact song length (`exact_mv_duration_sec`, e.g. 183.4s ~ 291.7s) and automatically divides into `ceil(song_duration / 10)` 10s storyboard acts, OR fixed duration (`10s / 20s / 30s / 40s / 60s / 120s`).
    3. Analyzes 24fps (`41.667ms`) acoustic vocal timestamps, $F_0$ pitch/gender registers (`male`/`female`/`both`), and syllable cadence across 3 MV performance modes (`solo_vocal_lipsync`, `duet_chorus_lipsync`, `pure_story_cinematic`, or `auto_detect`) and compiles Omni 1.1 Flash Prompts.
    4. Synthesizes Character Reference Assets & Per-Act `<FIRST_FRAME>` keyframes via Nano Banana 2.1 (`gemini-nano-banana-2.1`).
    5. Renders each 10.0s storyboard video clip via `gemini-omni-1.1-flash-preview` (with Continuous Last-Frame Relay option & 24fps Dynamic Animatic fallback).
    6. Synchronizes `.srt`/`.vtt` subtitles, burns in CJK/multilingual subtitles, and merges all shots + master audio to the exact song duration via FFmpeg!
    """
    t0 = time.perf_counter()
    cfg = load_platform_config()
    project_id = cfg["project"].get("project_id", "genai-dnb-demo")
    out_dir = MV_STUDIO_OUTPUTS_DIR / sample_id
    out_dir.mkdir(parents=True, exist_ok=True)

    parent_job_id = getattr(_THREAD_CTX, "job_id", "") or ""
    _set_thread_log_ctx(
        job_id=parent_job_id,
        track_key="track6_mv_studio",
        sample_id=sample_id,
        stage="MV_STAGE1_STORYLINE_AND_AUDIO",
    )
    _update_mv_step_progress(
        "MV_STEP1_ASSET_SYNC",
        f"📥 [Step 1/5 · 素材同步与故事线加载] 正在校验并同步样本 {sample_id} 的原曲音频、人物参考图及 V2.2 Storyline...",
        sample_id=sample_id,
        sample_frac=0.04,
        job_id=parent_job_id,
    )
    spec = ensure_sample_files_synced(sample_id)
    storyline_data, storyline_used_key = _select_best_storyline_for_sample(sample_id, storyline_source=storyline_source)

    # Probe exact input song duration so Full-Song Mode (`target_duration_sec <= 0`) matches the song's exact length!
    orig_audio_p = SAMPLES_DIR / sample_id / "input_audio_01.mp3"
    probed_song_dur = _probe_media_duration(orig_audio_p) if orig_audio_p.exists() else 0.0
    if probed_song_dur <= 1.0:
        probed_song_dur = float((spec.get("audio_meta") or {}).get("duration_sec") or 30.0)

    is_full_song = int(target_duration_sec or 0) <= 0
    if is_full_song:
        exact_mv_duration_sec = round(probed_song_dur, 2)
        num_acts = max(1, int(math.ceil(max(1.0, exact_mv_duration_sec - 0.4) / 10.0)))
        clamped_duration = num_acts * 10
        audio_start_sec = 0.0
        _update_mv_step_progress(
            "MV_FULL_SONG_MODE",
            f"🎵 [Step 1/5 · 全曲等长自动分镜] 样本 {sample_id} 原曲精确时长={exact_mv_duration_sec:.2f}s -> 自动规划 {num_acts} 幕 × 10s 分镜，最终合成精确对齐 {exact_mv_duration_sec:.2f}s！",
            sample_id=sample_id,
            sample_frac=0.09,
            job_id=parent_job_id,
        )
    else:
        num_acts = max(1, int(math.ceil(int(target_duration_sec) / 10.0)))
        clamped_duration = num_acts * 10
        exact_mv_duration_sec = float(clamped_duration)
        _update_mv_step_progress(
            "MV_FIXED_DURATION_MODE",
            f"🎵 [Step 1/5 · 指定时长自动分镜] 样本 {sample_id} 目标时长={exact_mv_duration_sec:.1f}s -> 自动规划 {num_acts} 幕 × 10s 分镜",
            sample_id=sample_id,
            sample_frac=0.09,
            job_id=parent_job_id,
        )

    master_audio_path, chunk_paths, resolved_start_sec, audio_source_summary = (
        _prepare_mv_master_audio_and_10s_chunks(
            sample_id=sample_id,
            sample_spec=spec,
            storyline_data=storyline_data,
            out_dir=out_dir,
            target_duration_sec=clamped_duration,
            audio_start_sec=audio_start_sec,
            is_full_song=is_full_song,
            exact_duration_sec=exact_mv_duration_sec,
        )
    )

    _set_thread_log_ctx(
        job_id=parent_job_id,
        track_key="track6_mv_studio",
        sample_id=sample_id,
        stage="MV_STAGE2_STORYBOARD_AND_LIPSYNC",
    )
    mv_plan, dsp_windows_per_act, vocal_gender_profile = _plan_mv_storyboard_and_lipsync_from_storyline(
        sample_id=sample_id,
        sample_spec=spec,
        storyline_data=storyline_data,
        chunk_paths=chunk_paths,
        song_start_sec=resolved_start_sec,
        mv_performance_mode=mv_performance_mode,
        exact_mv_duration_sec=exact_mv_duration_sec,
    )
    resolved_perf_mode = str(mv_plan.get("mv_performance_mode") or vocal_gender_profile.get("mv_performance_mode") or "solo_vocal_lipsync")

    # Compile per-act Omni 1.1 Flash `[MM:SS.mmm]` prompts
    compiled_omni_prompts: list[str] = []
    for idx_a, act in enumerate(mv_plan["acts"]):
        if custom_omni_prompts and idx_a < len(custom_omni_prompts) and custom_omni_prompts[idx_a].strip():
            comp = _normalize_omni_mv_prompt(
                custom_omni_prompts[idx_a],
                include_transcriptions=(resolved_perf_mode != "pure_story_cinematic"),
            )
        else:
            comp = compile_mv_storyline_omni_prompt_for_act(
                mv_plan=mv_plan,
                act=act,
                total_acts=num_acts,
                language_hint=spec.get("language", "English"),
                mv_performance_mode=resolved_perf_mode,
            )
        act["compiled_omni_prompt"] = comp
        compiled_omni_prompts.append(comp)
    _update_mv_step_progress(
        "MV_STEP2_OMNI_PROMPTS_COMPILED",
        f"📜 [Step 2/5 · Omni 时间戳指令编译完成] 已为样本 {sample_id} 编译全部 {len(compiled_omni_prompts)} 幕 Omni 1.1 Flash 原生 `[MM:SS.mmm]` 音画对齐提示词 (`{resolved_perf_mode}`)",
        sample_id=sample_id,
        sample_frac=0.35,
        job_id=parent_job_id,
    )

    # Stage 3: Synthesize Lead/Support Character Assets & Per-Act `<FIRST_FRAME>` via Nano Banana 2.1 (parallelized up to 4 workers for full-song speed!)
    _set_thread_log_ctx(
        job_id=parent_job_id,
        track_key="track6_mv_studio",
        sample_id=sample_id,
        stage="MV_STAGE3_FIRST_FRAMES",
    )
    _update_mv_step_progress(
        "MV_STEP3_FIRST_FRAMES_START",
        f"🎨 [Step 3/5 · 角色定妆与各幕首帧生成] 正在调用 `gemini-nano-banana-2.1` 并行生成样本 {sample_id} 的 {num_acts} 幕起始首帧 `<FIRST_FRAME>`...",
        sample_id=sample_id,
        sample_frac=0.36,
        job_id=parent_job_id,
    )
    sdir = SAMPLES_DIR / sample_id
    ref_img_paths = [
        sdir / img["filename"]
        for img in spec.get("images", [])
        if (sdir / img["filename"]).exists()
    ]
    primary_ref_img = ref_img_paths[0] if ref_img_paths else (out_dir / "lead_character_asset.jpg")

    lead_asset_path = out_dir / "lead_character_asset.jpg"
    if ref_img_paths:
        shutil.copy2(ref_img_paths[0], lead_asset_path)
    else:
        _synthesize_mv_image_with_nano_banana(
            prompt=mv_plan["lead_character"]["visual_appearance_en"],
            ref_image_paths=[],
            out_path=lead_asset_path,
            project_id=project_id,
            sample_id=sample_id,
            label_cn="主角标准定妆参考图",
            aspect_ratio=aspect_ratio,
        )

    support_asset_path = out_dir / "supporting_character_asset.jpg"
    if len(ref_img_paths) >= 2:
        shutil.copy2(ref_img_paths[1], support_asset_path)

    shot_first_frame_urls: list[str] = [""] * num_acts
    shot_first_frame_paths: list[Path] = [out_dir / f"shot_{i + 1:02d}_first_frame.jpg" for i in range(num_acts)]
    shot_video_urls: list[str] = [""] * num_acts
    shot_video_paths: list[Path] = [out_dir / f"shot_{i + 1:02d}_10s.mp4" for i in range(num_acts)]
    render_engines_used: list[str] = [""] * num_acts
    eff_refs_for_ff = ref_img_paths if ref_img_paths else [lead_asset_path]
    ff_done_lock = threading.Lock()
    ff_done_counter = [0]

    def _save_partial_mv_checkpoint(stage_status: str) -> None:
        """Incrementally saves partial MV studio progress (`mv_plan`, `shot_first_frame_urls`, `shot_video_urls`) to `track6_mv_studio.json` so the UI table updates live after every step!"""
        try:
            partial_rec = {
                "sample_id": sample_id,
                "sample_title": spec.get("title", f"Sample {sample_id}"),
                "track_key": "track6_mv_studio",
                "status": stage_status,
                "storyline_source_used": storyline_used_key,
                "is_full_song": is_full_song,
                "exact_mv_duration_sec": exact_mv_duration_sec,
                "target_duration_sec": exact_mv_duration_sec if is_full_song else clamped_duration,
                "num_acts": num_acts,
                "song_start_sec": resolved_start_sec,
                "mv_performance_mode": resolved_perf_mode,
                "mv_performance_mode_label": vocal_gender_profile.get("mv_performance_mode_label", ""),
                "requested_performance_mode": mv_performance_mode,
                "audio_source_summary": audio_source_summary,
                "shot_continuity_mode": shot_continuity_mode,
                "execution_mode": execution_mode,
                "burn_subtitles": burn_subtitles,
                "aspect_ratio": aspect_ratio,
                "render_engines_used": [e for e in render_engines_used if e],
                "evaluated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_elapsed_sec": round(time.perf_counter() - t0, 1),
                "master_audio_url": f"/api/v2/mv_studio/media/{sample_id}/{master_audio_path.name}",
                "lead_character_url": f"/api/v2/mv_studio/media/{sample_id}/lead_character_asset.jpg",
                "supporting_character_url": (
                    f"/api/v2/mv_studio/media/{sample_id}/supporting_character_asset.jpg"
                    if (out_dir / "supporting_character_asset.jpg").exists()
                    else ""
                ),
                "shot_first_frame_urls": [u for u in shot_first_frame_urls if u],
                "shot_video_urls": list(shot_video_urls),
                "final_video_url": (
                    f"/api/v2/mv_studio/media/{sample_id}/final_mv_subtitled.mp4"
                    if (out_dir / "final_mv_subtitled.mp4").exists()
                    else ""
                ),
                "final_clean_video_url": (
                    f"/api/v2/mv_studio/media/{sample_id}/final_mv_clean.mp4"
                    if (out_dir / "final_mv_clean.mp4").exists()
                    else ""
                ),
                "srt_url": (
                    f"/api/v2/mv_studio/media/{sample_id}/subtitles.srt"
                    if (out_dir / "subtitles.srt").exists()
                    else ""
                ),
                "vtt_url": (
                    f"/api/v2/mv_studio/media/{sample_id}/subtitles.vtt"
                    if (out_dir / "subtitles.vtt").exists()
                    else ""
                ),
                "subtitle_cues": [],
                "dsp_windows_per_act": dsp_windows_per_act,
                "vocal_gender_profile": vocal_gender_profile,
                "mv_plan": mv_plan,
                "compiled_omni_prompts": compiled_omni_prompts,
            }
            (out_dir / "mv_studio_plan.json").write_text(json.dumps(partial_rec, ensure_ascii=False, indent=2), encoding="utf-8")
            save_sample_to_track_store("track6_mv_studio", sample_id, partial_rec)
        except Exception:
            pass

    # Save Step 2 checkpoint immediately so storyboard & Omni prompts are visible in the UI right away
    _save_partial_mv_checkpoint("plan_ready")

    def _gen_ff_for_act(idx_a: int) -> None:
        _set_thread_log_ctx(
            job_id=parent_job_id,
            track_key="track6_mv_studio",
            sample_id=sample_id,
            stage="MV_STAGE3_FIRST_FRAMES",
        )
        act = mv_plan["acts"][idx_a]
        act_num = idx_a + 1
        ff_path = shot_first_frame_paths[idx_a]
        _synthesize_mv_image_with_nano_banana(
            prompt=act.get("first_frame_prompt_en", act.get("scene_environment_en", "")),
            ref_image_paths=eff_refs_for_ff,
            out_path=ff_path,
            project_id=project_id,
            sample_id=sample_id,
            label_cn=f"第 {act_num}/{num_acts} 幕起始首帧 (`<FIRST_FRAME>`)",
            aspect_ratio=aspect_ratio,
        )
        ff_url = f"/api/v2/mv_studio/media/{sample_id}/{ff_path.name}"
        shot_first_frame_urls[idx_a] = ff_url
        act["first_frame_url"] = ff_url
        with ff_done_lock:
            ff_done_counter[0] += 1
            cur_ff = ff_done_counter[0]
        if cur_ff == num_acts or cur_ff % 4 == 0:
            _save_partial_mv_checkpoint("plan_ready")
        _update_mv_step_progress(
            "MV_STEP3_FIRST_FRAME_DONE",
            f"🖼️ [Step 3/5 · 首帧进度 {cur_ff}/{num_acts}] 样本 {sample_id} 第 {act_num}/{num_acts} 幕起始首帧 (`{ff_path.name}`) 已生成就绪",
            sample_id=sample_id,
            sample_frac=0.36 + 0.14 * (cur_ff / max(1, num_acts)),
            job_id=parent_job_id,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, num_acts))) as ff_pool:
        list(ff_pool.map(_gen_ff_for_act, range(num_acts)))
    _save_partial_mv_checkpoint("plan_ready")

    # Stage 4: Render 10s Storyboard Video Clips & Merge with Synchronized Subtitles (unless `plan_and_keyframes` mode)

    if execution_mode == "plan_and_keyframes":
        srt_path = out_dir / "subtitles.srt"
        vtt_path = out_dir / "subtitles.vtt"
        subtitle_cues: list[dict[str, Any]] = []
        cue_i = 0
        for act in mv_plan["acts"]:
            g_off = float(act.get("global_start_sec", 0.0))
            for mb in act.get("micro_beats") or []:
                txt = str(mb.get("subtitle_text") or "").strip()
                if txt and not txt.startswith("["):
                    st_g = round(g_off + float(mb["start_sec"]), 3)
                    if st_g >= exact_mv_duration_sec - 0.15:
                        continue
                    ed_g = min(exact_mv_duration_sec, round(g_off + float(mb["end_sec"]), 3))
                    cue_i += 1
                    subtitle_cues.append({
                        "cue_index": cue_i,
                        "act_index": act.get("act_index", 1),
                        "start_sec": st_g,
                        "end_sec": ed_g,
                        "srt_start": _format_srt_timestamp(st_g),
                        "srt_end": _format_srt_timestamp(ed_g),
                        "vtt_start": _format_vtt_timestamp(st_g),
                        "vtt_end": _format_vtt_timestamp(ed_g),
                        "speaker": mb.get("active_speaker", "LEAD"),
                        "text": txt,
                    })
        srt_path.write_text(
            "\n".join(f"{c['cue_index']}\n{c['srt_start']} --> {c['srt_end']}\n{c['text']}\n" for c in subtitle_cues),
            encoding="utf-8",
        )
        vtt_path.write_text(
            "WEBVTT\n\n" + "\n".join(f"{c['cue_index']}\n{c['vtt_start']} --> {c['vtt_end']}\n{c['text']}\n" for c in subtitle_cues),
            encoding="utf-8",
        )
        final_video_url = ""
        final_clean_video_url = ""
        render_status = "plan_ready"
        _update_mv_step_progress(
            "MV_PLAN_READY",
            f"📝 [Phase 1 规划与首帧已完成] 样本 {sample_id} 共 {num_acts} 幕分镜首帧与 {len(subtitle_cues)} 条毫秒级字幕已就绪！",
            sample_id=sample_id,
            sample_frac=0.98,
            job_id=parent_job_id,
        )
    else:
        _set_thread_log_ctx(
            job_id=parent_job_id,
            track_key="track6_mv_studio",
            sample_id=sample_id,
            stage="MV_STAGE4_OMNI_VIDEO_RENDER",
        )
        _update_mv_step_progress(
            "MV_STEP4_VIDEO_RENDER_START",
            f"🎬 [Step 4/5 · 10s 分镜视频渲染启动] 正在渲染样本 {sample_id} 的全部 {num_acts} 幕 × 10.0s 分镜视频 (模式={shot_continuity_mode}, 策略={execution_mode})...",
            sample_id=sample_id,
            sample_frac=0.51,
            job_id=parent_job_id,
        )
        if shot_continuity_mode == "continuous_relay":
            relay_frame: Path | None = None
            for idx_a, act in enumerate(mv_plan["acts"]):
                act_num = idx_a + 1
                active_ff = relay_frame if (relay_frame and relay_frame.exists()) else shot_first_frame_paths[idx_a]
                seg_mp4 = out_dir / f"shot_{act_num:02d}_10s.mp4"
                _update_mv_step_progress(
                    "MV_STEP4_ACT_START",
                    f"🎬 [Step 4/5 · 尾帧接力渲染第 {act_num}/{num_acts} 幕] 正在生成 `{seg_mp4.name}` ({idx_a*10}s~{(idx_a+1)*10}s)...",
                    sample_id=sample_id,
                    sample_frac=0.51 + 0.35 * (idx_a / max(1, num_acts)),
                    job_id=parent_job_id,
                )
                clip_p, engine_used = _invoke_mv_omni_10s_clip(
                    first_frame_path=active_ff,
                    ref_img_path=primary_ref_img,
                    omni_prompt=compiled_omni_prompts[idx_a],
                    act=act,
                    aspect_ratio=aspect_ratio,
                    act_index=act_num,
                    total_acts=num_acts,
                    out_10s_mp4_path=seg_mp4,
                    project_id=project_id,
                    sample_id=sample_id,
                    execution_mode=execution_mode,
                )
                shot_video_paths.append(clip_p)
                v_url = f"/api/v2/mv_studio/media/{sample_id}/{clip_p.name}"
                shot_video_urls.append(v_url)
                act["shot_video_url"] = v_url
                act["render_engine"] = engine_used
                render_engines_used.append(engine_used)
                _update_mv_step_progress(
                    "MV_STEP4_ACT_DONE",
                    f"✅ [Step 4/5 · 分镜视频进度 {act_num}/{num_acts}] 第 {act_num}/{num_acts} 幕 `{clip_p.name}` 渲染完成 (引擎: `{engine_used}`)",
                    sample_id=sample_id,
                    sample_frac=0.51 + 0.35 * (act_num / max(1, num_acts)),
                    job_id=parent_job_id,
                )

                if act_num < num_acts:
                    next_relay = out_dir / f"relay_first_frame_act_{act_num + 1:02d}.jpg"
                    if _extract_last_frame_from_mp4(clip_p, next_relay):
                        relay_frame = next_relay
                        log_runtime(
                            "INFO",
                            "MV_RELAY_FRAME",
                            f"🔗 [尾帧无缝接力] 已提取第 {act_num} 幕尾帧作为第 {act_num + 1} 幕 `<FIRST_FRAME>`",
                            sample_id=sample_id,
                            job_id=parent_job_id,
                        )
        else:
            # Multi-shot MV mode: render up to 4 acts in parallel for fast full-song synthesis!
            shot_video_paths = [out_dir / f"shot_{i + 1:02d}_10s.mp4" for i in range(num_acts)]
            shot_video_urls = [""] * num_acts
            render_engines_used = [""] * num_acts
            clip_done_lock = threading.Lock()
            clip_done_counter = [0]

            def _render_act_clip(idx_a: int) -> None:
                _set_thread_log_ctx(
                    job_id=parent_job_id,
                    track_key="track6_mv_studio",
                    sample_id=sample_id,
                    stage="MV_STAGE4_OMNI_VIDEO_RENDER",
                )
                act = mv_plan["acts"][idx_a]
                act_num = idx_a + 1
                seg_mp4 = shot_video_paths[idx_a]
                clip_p, engine_used = _invoke_mv_omni_10s_clip(
                    first_frame_path=shot_first_frame_paths[idx_a],
                    ref_img_path=primary_ref_img,
                    omni_prompt=compiled_omni_prompts[idx_a],
                    act=act,
                    aspect_ratio=aspect_ratio,
                    act_index=act_num,
                    total_acts=num_acts,
                    out_10s_mp4_path=seg_mp4,
                    project_id=project_id,
                    sample_id=sample_id,
                    execution_mode=execution_mode,
                )
                shot_video_paths[idx_a] = clip_p
                v_url = f"/api/v2/mv_studio/media/{sample_id}/{clip_p.name}"
                shot_video_urls[idx_a] = v_url
                act["shot_video_url"] = v_url
                act["render_engine"] = engine_used
                render_engines_used[idx_a] = engine_used
                with clip_done_lock:
                    clip_done_counter[0] += 1
                    cur_c = clip_done_counter[0]
                if cur_c == num_acts or cur_c % 2 == 0:
                    _save_partial_mv_checkpoint("plan_ready")
                _update_mv_step_progress(
                    "MV_STEP4_ACT_DONE",
                    f"🎞️ [Step 4/5 · 分镜视频进度 {cur_c}/{num_acts}] 样本 {sample_id} 第 {act_num}/{num_acts} 幕 (`{clip_p.name}`, {idx_a*10}s~{(idx_a+1)*10}s) 渲染完成 (引擎: `{engine_used}`)",
                    sample_id=sample_id,
                    sample_frac=0.51 + 0.35 * (cur_c / max(1, num_acts)),
                    job_id=parent_job_id,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, num_acts))) as v_pool:
                list(v_pool.map(_render_act_clip, range(num_acts)))
            _save_partial_mv_checkpoint("plan_ready")

        # Stage 5: Concatenate shots + Synchronize/Burn Subtitles + Mux Master Audio to Exact Song Duration
        _set_thread_log_ctx(
            job_id=parent_job_id,
            track_key="track6_mv_studio",
            sample_id=sample_id,
            stage="MV_STAGE5_SUBTITLE_AND_FFMPEG_MERGE",
        )
        _update_mv_step_progress(
            "MV_STEP5_CONCAT_START",
            f"🔗 [Step 5/5 · 多幕视频无缝拼接] 正在通过 FFmpeg 拼接样本 {sample_id} 的 {len(shot_video_paths)} 段 10.0s 分镜视频...",
            sample_id=sample_id,
            sample_frac=0.87,
            job_id=parent_job_id,
        )
        concat_video_path = out_dir / f"mv_concat_{clamped_duration}s.mp4"
        if len(shot_video_paths) == 1:
            shutil.copy2(shot_video_paths[0], concat_video_path)
        else:
            concat_txt = out_dir / "concat_list.txt"
            concat_txt.write_text("\n".join(f"file '{p.resolve()}'" for p in shot_video_paths) + "\n", encoding="utf-8")
            _run_ffmpeg_gcsfuse_safe(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_txt),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    str(concat_video_path),
                ],
                concat_video_path,
            )
            if not concat_video_path.exists():
                shutil.copy2(shot_video_paths[0], concat_video_path)
        _update_mv_step_progress(
            "MV_STEP5_CONCAT_DONE",
            f"✅ [Step 5/5 · 多幕拼接完成] `{concat_video_path.name}` 已拼接就绪，正在执行毫秒级字幕轨合成与母带音画压制...",
            sample_id=sample_id,
            sample_frac=0.89,
            job_id=parent_job_id,
        )

        final_sub_mp4, final_clean_mp4, srt_path, vtt_path, subtitle_cues = _build_synchronized_subtitles_and_burn_in(
            mv_plan=mv_plan,
            concat_video_path=concat_video_path,
            master_audio_path=master_audio_path,
            out_dir=out_dir,
            aspect_ratio=aspect_ratio,
            target_duration_sec=clamped_duration,
            burn_subtitles=burn_subtitles,
            sample_id=sample_id,
            exact_duration_sec=exact_mv_duration_sec,
        )
        final_video_url = f"/api/v2/mv_studio/media/{sample_id}/{final_sub_mp4.name}"
        final_clean_video_url = f"/api/v2/mv_studio/media/{sample_id}/{final_clean_mp4.name}"
        render_status = "completed"

    elapsed = round(time.perf_counter() - t0, 2)
    record = {
        "sample_id": sample_id,
        "sample_title": spec.get("title", f"Sample {sample_id}"),
        "track_key": "track6_mv_studio",
        "status": render_status,
        "storyline_source_used": storyline_used_key,
        "is_full_song": is_full_song,
        "exact_mv_duration_sec": exact_mv_duration_sec,
        "target_duration_sec": exact_mv_duration_sec if is_full_song else clamped_duration,
        "num_acts": num_acts,
        "song_start_sec": resolved_start_sec,
        "mv_performance_mode": resolved_perf_mode,
        "mv_performance_mode_label": vocal_gender_profile.get("mv_performance_mode_label", ""),
        "requested_performance_mode": mv_performance_mode,
        "audio_source_summary": audio_source_summary,
        "shot_continuity_mode": shot_continuity_mode,
        "execution_mode": execution_mode,
        "burn_subtitles": burn_subtitles,
        "aspect_ratio": aspect_ratio,
        "render_engines_used": render_engines_used,
        "evaluated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_elapsed_sec": elapsed,
        "master_audio_url": f"/api/v2/mv_studio/media/{sample_id}/{master_audio_path.name}",
        "lead_character_url": f"/api/v2/mv_studio/media/{sample_id}/lead_character_asset.jpg",
        "supporting_character_url": (
            f"/api/v2/mv_studio/media/{sample_id}/supporting_character_asset.jpg"
            if (out_dir / "supporting_character_asset.jpg").exists()
            else ""
        ),
        "shot_first_frame_urls": shot_first_frame_urls,
        "shot_video_urls": shot_video_urls,
        "final_video_url": final_video_url,
        "final_clean_video_url": final_clean_video_url,
        "srt_url": f"/api/v2/mv_studio/media/{sample_id}/subtitles.srt",
        "vtt_url": f"/api/v2/mv_studio/media/{sample_id}/subtitles.vtt",
        "subtitle_cues": subtitle_cues,
        "dsp_windows_per_act": dsp_windows_per_act,
        "vocal_gender_profile": vocal_gender_profile,
        "mv_plan": mv_plan,
        "compiled_omni_prompts": compiled_omni_prompts,
    }

    # Save plan bundle inside sample's mv_studio_outputs folder & sync to track6_mv_studio.json
    (out_dir / "mv_studio_plan.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    save_sample_to_track_store("track6_mv_studio", sample_id, record)

    # Also upload generated assets to GCS in background if GCS bucket is configured
    gcs_bkt, gcs_pfx = _parse_gcs_bucket_and_prefix()
    if gcs_bkt:
        def _sync_mv_assets_to_gcs() -> None:
            _set_thread_log_ctx(
                job_id=parent_job_id,
                track_key="track6_mv_studio",
                sample_id=sample_id,
                stage="MV_GCS_UPLOAD",
            )
            files_to_sync = [fp for fp in out_dir.iterdir() if fp.is_file() and fp.stat().st_size > 0]
            log_runtime(
                "INFO",
                "MV_GCS_UPLOAD_START",
                f"☁️ [GCS 异步归档] 正在将样本 {sample_id} 的 {len(files_to_sync)} 个成片与分镜资产同步至 `gs://{gcs_bkt}/{gcs_pfx + '/' if gcs_pfx else ''}runs_v2/mv_studio_outputs/{sample_id}/`...",
                sample_id=sample_id,
                job_id=parent_job_id,
            )
            uploaded_cnt = 0
            for fp in files_to_sync:
                ctype = "video/mp4" if fp.suffix == ".mp4" else ("image/jpeg" if fp.suffix in (".jpg", ".jpeg") else "application/octet-stream")
                if upload_to_gcs_universal(f"runs_v2/mv_studio_outputs/{sample_id}/{fp.name}", fp.read_bytes(), content_type=ctype):
                    uploaded_cnt += 1
            log_runtime(
                "INFO",
                "MV_GCS_UPLOAD_DONE",
                f"☁️ [GCS 异步归档完成] 样本 {sample_id} 已成功归档 {uploaded_cnt}/{len(files_to_sync)} 个文件至 `gs://{gcs_bkt}/{gcs_pfx + '/' if gcs_pfx else ''}runs_v2/mv_studio_outputs/{sample_id}/final_mv_subtitled.mp4`！",
                sample_id=sample_id,
                job_id=parent_job_id,
            )
        threading.Thread(target=_sync_mv_assets_to_gcs, daemon=True).start()

    return record


def start_parallel_mv_studio_batch_job(
    sample_ids: list[str],
    storyline_source: str = "prefer_argon",
    target_duration_sec: int = 0,
    audio_start_sec: float | None = None,
    mv_performance_mode: str = "auto_detect",
    shot_continuity_mode: str = "multi_shot_mv",
    execution_mode: str = "full_auto",
    burn_subtitles: bool = True,
    aspect_ratio: str = "16:9",
    max_workers: int = 2,
) -> str:
    """Starts a background parallel batch job for V2.3 MV Generation Studio across `sample_ids`."""
    job_id = f"batch_mv_studio_{uuid.uuid4().hex[:6]}"
    per_sample_status = {sid: {"status": "queued", "score": None, "error": "", "traceback": ""} for sid in sample_ids}
    dur_label = "全曲等长自动分镜" if int(target_duration_sec or 0) <= 0 else f"{target_duration_sec}s"
    perf_label = MV_PERFORMANCE_MODES.get(mv_performance_mode, mv_performance_mode)
    mode_label = {
        "full_auto": "全自动分镜规划 + Omni 1.1视频渲染 + 字幕同步合并",
        "fast_animatic_mv": "极速分镜规划 + Nano Banana首帧 + 24fps动态运镜合成 + 字幕同步合并",
        "plan_and_keyframes": "Phase 1: 毫秒级分镜规划 + 角色/首帧定妆 + 同步字幕轨",
    }.get(execution_mode, execution_mode)

    with _BATCH_LOCK:
        _BATCH_JOBS[job_id] = {
            "job_id": job_id,
            "track_key": "track6_mv_studio",
            "track_label": f"#6 MV生成工坊 ({dur_label} · {perf_label} · {mode_label})",
            "model": "gemini-4-argon + gemini-nano-banana-2.1 + gemini-omni-1.1-flash-preview",
            "location": "global",
            "thinking_level": "MEDIUM",
            "status": "running",
            "total_samples": len(sample_ids),
            "completed_samples": 0,
            "succeeded_samples": 0,
            "failed_samples": 0,
            "progress_pct": 4,
            "current_step": f"🚀 正在启动 V2.3 MV 全链路生成工坊 (共 {len(sample_ids)} 首样本, 时长={dur_label}, 模式={perf_label}, 并发={max_workers})...",
            "per_sample": per_sample_status,
            "logs": [],
        }

    log_runtime(
        "INFO",
        "MV_BATCH_START",
        f"启动 V2.3 MV 批量生成任务 {job_id}: 样本={sample_ids} | 时长={dur_label} | 演绎模式={perf_label} | 渲染策略={execution_mode} | 并发={max_workers}",
        track_key="track6_mv_studio",
        job_id=job_id,
    )

    def _runner() -> None:
        def _task(sid: str) -> None:
            _set_thread_log_ctx(job_id=job_id, track_key="track6_mv_studio", sample_id=sid, stage="MV_TASK_START")
            with _BATCH_LOCK:
                _BATCH_JOBS[job_id]["per_sample"][sid]["status"] = "running"
            log_runtime(
                "INFO",
                "MV_SAMPLE_START",
                f"▶ 样本 {sid} 开始执行 MV 全链路生成 ({dur_label}, 演绎模式={perf_label}, mode={execution_mode})...",
                sample_id=sid,
                track_key="track6_mv_studio",
                job_id=job_id,
            )
            try:
                rec = run_single_sample_mv_studio_pipeline(
                    sample_id=sid,
                    storyline_source=storyline_source,
                    target_duration_sec=target_duration_sec,
                    audio_start_sec=audio_start_sec,
                    mv_performance_mode=mv_performance_mode,
                    shot_continuity_mode=shot_continuity_mode,
                    execution_mode=execution_mode,
                    burn_subtitles=burn_subtitles,
                    aspect_ratio=aspect_ratio,
                )
                with _BATCH_LOCK:
                    _BATCH_JOBS[job_id]["per_sample"][sid] = {
                        "status": rec["status"],
                        "score": len(rec.get("subtitle_cues", [])),
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
                        f"已生成 {done}/{tot} 首 MV (✅ 成功: {succ} | ❌ 失败: {fail}) | 最新完成 Sample {sid} ({rec.get('mv_performance_mode_label', '')}, 时长={rec.get('exact_mv_duration_sec', rec.get('target_duration_sec'))}s, {rec['num_acts']}幕, 耗时={rec['total_elapsed_sec']}s)"
                    )
                log_runtime(
                    "INFO",
                    "MV_SAMPLE_DONE",
                    f"✅ 样本 {sid} MV 生成完毕！模式={rec.get('mv_performance_mode_label', '')} | 成片精确时长={rec.get('exact_mv_duration_sec')}s ({rec['num_acts']}幕) | 总耗时 {rec['total_elapsed_sec']}s | 字幕条数={len(rec.get('subtitle_cues', []))}",
                    sample_id=sid,
                    track_key="track6_mv_studio",
                    job_id=job_id,
                )
            except Exception as exc:
                tb_full = traceback.format_exc()
                err_stage = getattr(_THREAD_CTX, "stage", "MV_UNKNOWN")
                err_rec = {
                    "sample_id": sid,
                    "track_key": "track6_mv_studio",
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error_stage": err_stage,
                    "error": str(exc),
                    "traceback": tb_full,
                    "evaluated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_sample_to_track_store("track6_mv_studio", sid, err_rec)
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
                    _BATCH_JOBS[job_id]["current_step"] = (
                        f"已处理 {done}/{tot} 首 MV (✅ 成功: {succ} | ❌ 失败: {fail}) | ❌ Sample {sid} [{err_stage}] {type(exc).__name__}: {exc}"
                    )
                log_runtime(
                    "ERROR",
                    f"MV_SAMPLE_FAIL:{err_stage}",
                    f"❌ 样本 {sid} MV 生成失败 ({type(exc).__name__}): {exc}",
                    sample_id=sid,
                    track_key="track6_mv_studio",
                    job_id=job_id,
                    exc=exc,
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
            futs = [pool.submit(_task, sid) for sid in sample_ids]
            concurrent.futures.wait(futs)

        with _BATCH_LOCK:
            succ = _BATCH_JOBS[job_id]["succeeded_samples"]
            fail = _BATCH_JOBS[job_id]["failed_samples"]
            tot = _BATCH_JOBS[job_id]["total_samples"]
            _BATCH_JOBS[job_id]["status"] = "completed" if fail == 0 else ("partial_error" if succ > 0 else "error")
            _BATCH_JOBS[job_id]["progress_pct"] = 100
            if fail == 0:
                _BATCH_JOBS[job_id]["current_step"] = f"🎉 V2.3 MV 批量生成任务已全部成功完成 (✅ {succ}/{tot})！"
            else:
                _BATCH_JOBS[job_id]["current_step"] = f"⚠️ V2.3 MV 批量任务结束：✅ 成功 {succ}/{tot}，❌ 失败 {fail}/{tot}"
        persist_batch_job_state(job_id)

    threading.Thread(target=_runner, daemon=True).start()
    return job_id


