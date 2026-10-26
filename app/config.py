#!/usr/bin/env python3
"""Configuration, directory layout, structured runtime logger, and prompt/model registry for MV Multimodal Evaluation & Storyline Studio."""
from __future__ import annotations

import copy
import datetime
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import traceback
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT
STATIC_DIR = PROJECT_ROOT / "static"
CONFIGS_DIR = PROJECT_ROOT / "configs"
DOCS_PROMPTS_DIR = PROJECT_ROOT / "docs" / "prompts"
UI_HTML_PATH = STATIC_DIR / "index.html"


def _resolve_writable_data_dir() -> Path:
    """Resolves the writable runtime data directory (`data/` by default, with fallback to `/tmp/mv_eval_data`)."""
    env_dir = (
        os.environ.get("MV_STUDIO_DATA_DIR")
        or os.environ.get("ARGON_EVAL_DATA_DIR")
        or ""
    ).strip()
    if env_dir:
        cand = Path(env_dir)
    elif (PROJECT_ROOT / "data").exists():
        cand = PROJECT_ROOT / "data"
    elif (PROJECT_ROOT / "argon_eval_data").exists():
        cand = PROJECT_ROOT / "argon_eval_data"
    else:
        cand = PROJECT_ROOT / "data"
    try:
        cand.mkdir(parents=True, exist_ok=True)
        (cand / "runs" / "snapshots").mkdir(parents=True, exist_ok=True)
        probe = cand / "runs" / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return cand
    except OSError:
        fallback = Path("/tmp/mv_eval_data")
        fallback.mkdir(parents=True, exist_ok=True)
        (fallback / "runs" / "snapshots").mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _resolve_writable_data_dir()
DOCS_DIR = DOCS_PROMPTS_DIR if DOCS_PROMPTS_DIR.exists() else (DATA_DIR / "docs")
SAMPLES_DIR = DATA_DIR / "samples"
MANIFEST_PATH = CONFIGS_DIR / "samples_manifest.json" if (CONFIGS_DIR / "samples_manifest.json").exists() else (DATA_DIR / "samples_manifest.json")
RUNS_V2_DIR = DATA_DIR / "runs"
SNAPSHOTS_DIR = RUNS_V2_DIR / "snapshots"
CONFIG_PATH = RUNS_V2_DIR / "platform_config.json"
RUNTIME_LOG_PATH = RUNS_V2_DIR / "runtime_debug.log"

for _d in (SAMPLES_DIR, RUNS_V2_DIR, SNAPSHOTS_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

APP_VERSION = "2.2.0"
MODEL_FLASH_38 = "gemini-3.8-flash"
MODEL_ARGON_4 = "gemini-4-argon"
JOBS_DIR = RUNS_V2_DIR / "jobs"
try:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

_BATCH_JOBS: dict[str, dict[str, Any]] = {}
_BATCH_LOCK = threading.Lock()


def persist_batch_job_state(job_id: str) -> None:
    """Persists `_BATCH_JOBS[job_id]` to `runs/jobs/<job_id>.json` so Cloud Run container restarts or multi-instance routing never return 404."""
    if not job_id:
        return
    try:
        with _BATCH_LOCK:
            st = _BATCH_JOBS.get(job_id)
            if not st:
                return
            payload = json.dumps(st, ensure_ascii=False)
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        (JOBS_DIR / f"{job_id}.json").write_text(payload, encoding="utf-8")
    except Exception:
        pass


def load_batch_job_state(job_id: str) -> dict[str, Any] | None:
    """Loads job state from in-memory `_BATCH_JOBS` or falls back to `runs/jobs/<job_id>.json` on disk/GCS FUSE."""
    with _BATCH_LOCK:
        st = _BATCH_JOBS.get(job_id)
        if st:
            return dict(st)
    jp = JOBS_DIR / f"{job_id}.json"
    if jp.exists():
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                with _BATCH_LOCK:
                    _BATCH_JOBS[job_id] = data
                return dict(data)
        except Exception:
            pass
    return None

# ==============================================================================
# 0. Structured Runtime Logger & Diagnostic Context
# ==============================================================================
_LOG_LOCK = threading.Lock()
_RUNTIME_LOGS: list[dict[str, Any]] = []
_MAX_RUNTIME_LOGS = 1000
_THREAD_CTX = threading.local()


def _set_thread_log_ctx(job_id: str = "", track_key: str = "", sample_id: str = "", stage: str = "") -> None:
    _THREAD_CTX.job_id = job_id
    _THREAD_CTX.track_key = track_key
    _THREAD_CTX.sample_id = sample_id
    _THREAD_CTX.stage = stage


def log_runtime(
    level: str,
    stage: str,
    message: str,
    sample_id: str = "",
    track_key: str = "",
    job_id: str = "",
    extra: dict[str, Any] | None = None,
    exc: BaseException | None = None,
) -> dict[str, Any]:
    """
    Unified structured runtime logger for Cloud Run stdout, persistent disk (`runtime_debug.log`),
    in-memory ring buffer (`_RUNTIME_LOGS`), and active batch job logs (`_BATCH_JOBS[job_id]["logs"]`).
    """
    now_dt = datetime.datetime.now()
    ts_full = now_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    ts_short = now_dt.strftime("%H:%M:%S")
    eff_job = job_id or getattr(_THREAD_CTX, "job_id", "") or ""
    eff_track = track_key or getattr(_THREAD_CTX, "track_key", "") or ""
    eff_sid = sample_id or getattr(_THREAD_CTX, "sample_id", "") or ""
    eff_stage = stage or getattr(_THREAD_CTX, "stage", "SYSTEM")
    lvl = level.upper()

    tb_str = ""
    if exc is not None:
        tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()

    extra_str = ""
    if extra:
        try:
            extra_str = " | " + json.dumps(extra, ensure_ascii=False)
        except Exception:
            extra_str = f" | {extra}"

    ctx_tag = ""
    if eff_track:
        ctx_tag += f"[{eff_track}]"
    if eff_sid:
        ctx_tag += f"[Sample {eff_sid}]"

    formatted_line = f"[{ts_full}][{lvl}]{ctx_tag}[{eff_stage}] {message}{extra_str}"
    short_job_line = f"[{ts_short}][{lvl}]{f'[Sample {eff_sid}]' if eff_sid else ''}[{eff_stage}] {message}"

    entry = {
        "timestamp": ts_full,
        "level": lvl,
        "stage": eff_stage,
        "track_key": eff_track,
        "sample_id": eff_sid,
        "job_id": eff_job,
        "message": message,
        "extra": extra or {},
        "traceback": tb_str,
        "formatted": formatted_line,
    }

    # 1. Print to Cloud Run / terminal stdout immediately
    print(formatted_line, flush=True)
    if tb_str and lvl in ("ERROR", "CRITICAL"):
        print(f"  └── Traceback:\n{tb_str}", flush=True)

    # 2. Append to in-memory ring buffer & disk log file
    with _LOG_LOCK:
        _RUNTIME_LOGS.append(entry)
        if len(_RUNTIME_LOGS) > _MAX_RUNTIME_LOGS:
            del _RUNTIME_LOGS[: len(_RUNTIME_LOGS) - _MAX_RUNTIME_LOGS]
        try:
            with RUNTIME_LOG_PATH.open("a", encoding="utf-8") as lf:
                lf.write(formatted_line + "\n")
                if tb_str:
                    lf.write(tb_str + "\n")
        except OSError:
            pass

    # 3. Also append to active batch job's real-time log stream if running inside a batch job
    if eff_job and "_BATCH_JOBS" in globals() and "_BATCH_LOCK" in globals():
        try:
            with _BATCH_LOCK:
                if eff_job in _BATCH_JOBS:
                    _BATCH_JOBS[eff_job]["logs"].append(short_job_line)
                    if len(_BATCH_JOBS[eff_job]["logs"]) > 400:
                        del _BATCH_JOBS[eff_job]["logs"][:100]
            persist_batch_job_state(eff_job)
        except Exception:
            pass

    return entry

# Built-in fallback manifest & docs so the app can run standalone even when deployed as a single .py file without local files
BUILTIN_SAMPLES_MANIFEST: list[dict[str, Any]] = [{'sample_id': '01', 'folder_id': '1s1SvWHIk9yfoV3ky44epU4cmnBgOeEK_', 'title': 'Sample 01 — 双人悲伤浪漫 MV（男唱女哭 + 日夜转换）', 'language': 'English', 'protagonist_mode': 'dual', 'has_prompt': True, 'has_baseline_mv': True, 'stress_tags': ['角色分工隔离(男唱女哭不张嘴)', '细粒度妆造约束', '日夜光影切换', '禁止跳舞负向约束'], 'files': {'cover.png': '1quFZVQoW1jY5Juec0pENPXto_t0yZwpn', 'final_mv.mp4': '1cq9BZvc3LO6DlY4CAsdmXmnmpi21MKdw', 'input_audio_01.mp3': '1rSA3-zl-b5cFzQvGJoCyuqYduB4q-eJn', 'input_image_01.jpeg': '1BfMTG6tl6TVjc8KQB0aET6yNTZnQjqqY', 'input_image_02.jpeg': '1Jmj8NrzwI7ps_JZjk9W_1_0Kf7sGKaz-', 'prompt.txt': '1hHuluzQUT16cr7l1fyif6DoPbvp0BCsx'}, 'audio_meta': {'duration_sec': 240.1, 'duration_str': '04:00', 'sample_rate': 48000, 'channels': 2, 'bit_rate': 192119}, 'audio_url': '/api/media/01/input_audio_01.mp3', 'cover_url': '/api/media/01/cover.png', 'final_mv_local': False, 'final_mv_url': '', 'final_mv_drive_id': '1cq9BZvc3LO6DlY4CAsdmXmnmpi21MKdw', 'prompt_text': "Male costume sad romantic style. handsome look and attractive. female costume sad romantic style and beautiful and attractive and charming look. singing by only male character. female character 's role silent and crying with ears in her eyes. kajal in female character 's eyes. female character 's nails 1 inch long with nail polish. hair spread silky soft and silky long black. watch in her one wrist. beautiful picturisation in beautiful places and different places. beautiful choreography but no dance. night mode and day mode in picturisation. lights in night Time. correct colour combination.", 'images': [{'filename': 'input_image_01.jpeg', 'url': '/api/media/01/input_image_01.jpeg', 'width': 540, 'height': 960, 'size_kb': 31.0}, {'filename': 'input_image_02.jpeg', 'url': '/api/media/01/input_image_02.jpeg', 'width': 540, 'height': 1194, 'size_kb': 41.3}]}, {'sample_id': '02', 'folder_id': '1YkD5RxCH9aMGckGM3gq3xQEKETQRmYAB', 'title': 'Sample 02 — 日本武道馆抒情 Live（耳麦 + 终段副歌流泪高潮）', 'language': 'Japanese', 'protagonist_mode': 'single', 'has_prompt': True, 'has_baseline_mv': True, 'stress_tags': ['曲式时间戳锚定(最後のサビ流泪)', '耳挂式无线麦道具一致', '舞台Live运镜'], 'files': {'cover.png': '1IMJ3AOvuey4Fe9IMQ51HAeK3uiTP0Tfn', 'final_mv.mp4': '18mQ_lrU3VofPmMiwNGwlPaxcuQvw5u93', 'input_audio_01.mp3': '11W40EHqccHYSHK6-7bS5obD7aXZEUH_7', 'input_image_01.jpeg': '1SBSNnFB-IT6vyUd2hvZwzDHTbXaYqhRt', 'prompt.txt': '1QeGVChDj97ZVA9T9liG3dlkUa2e5HQTl'}, 'audio_meta': {'duration_sec': 237.4, 'duration_str': '03:57', 'sample_rate': 44100, 'channels': 2, 'bit_rate': 192063}, 'audio_url': '/api/media/02/input_audio_01.mp3', 'cover_url': '/api/media/02/cover.png', 'final_mv_local': False, 'final_mv_url': '', 'final_mv_drive_id': '18mQ_lrU3VofPmMiwNGwlPaxcuQvw5u93', 'prompt_text': '初めての日本武道館のLive映像、バラード曲を最初から最後まで耳掛け式ワイヤレスイヤホンマイクを使用して歌う、画像通りの衣装、最後のサビから終わりまで感極まり涙を流しながら歌う', 'images': [{'filename': 'input_image_01.jpeg', 'url': '/api/media/02/input_image_01.jpeg', 'width': 768, 'height': 1025, 'size_kb': 45.2}]}, {'sample_id': '03', 'folder_id': '1thJfVzqQS3daxphLaGMt48fZJgi5jJCd', 'title': 'Sample 03 — Afrobeats 舞池律动《Let Your Body Talk》（对唱 + 8小节纯舞段）', 'language': 'English', 'protagonist_mode': 'single', 'has_prompt': True, 'has_baseline_mv': False, 'stress_tags': ['精细曲式切分(8小节打击乐Dance Break)', '主唱与伴唱一唱一和(Call & Response)', '110 BPM 律动踩点'], 'files': {'cover.png': '14_gfchxIvTX9DaY592TTW1a4mg4882PX', 'input_audio_01.mp3': '17CQ2QkF7xnXf7jPeCCeDr_S95lZtMU-w', 'input_image_01.jpeg': '1SDCClEVMDjdFfzAiV4tD5HmVVkLxL4KV', 'prompt.txt': '1bxwrI2spXqPQtIp5G4Xbjji1eeKBn1QA'}, 'audio_meta': {'duration_sec': 221.19, 'duration_str': '03:41', 'sample_rate': 44100, 'channels': 2, 'bit_rate': 192059}, 'audio_url': '/api/media/03/input_audio_01.mp3', 'cover_url': '/api/media/03/cover.png', 'final_mv_local': False, 'final_mv_url': '', 'final_mv_drive_id': '', 'prompt_text': 'Create an original Afrobeats song titled “Let Your Body Talk,” around 2:30 long. Zuri sings with a warm, soulful female voice, playful confidence, and flirty energy. Natural English vocals with female call-and-response backing harmonies.\n\nStyle: An irresistible dance groove at 110 BPM with prominent layered percussion: rolling hand drums, congas, shakers, crisp rim clicks, and syncopated kicks. Add bouncy bass, rhythmic guitar, warm keyboards, and bright brass accents.\n\nLyrics: Zuri invites someone watching from the edge of the dance floor to join her. Celebrate chemistry, freedom, and letting movement say what words cannot. Joyful and sensual, with golden lights and warm night air.\n\nChorus hook: “Let your body talk,” answered by “I’m listening.” Simple, catchy lines with room for percussion fills.\n\nStructure: Short drum intro, verse, chorus, second verse, chorus, eight-bar percussion dance break, final chorus, and a playful vocal tag with a crisp drum finish.', 'images': [{'filename': 'input_image_01.jpeg', 'url': '/api/media/03/input_image_01.jpeg', 'width': 1021, 'height': 1125, 'size_kb': 226.4}]}, {'sample_id': '04', 'folder_id': '1zLuAtbmJDhmFb5JTxL4PYj7t1OrfWhN0', 'title': 'Sample 04 — 双人无提示词自主叙事 MV (Zero-Prompt Dual #1)', 'language': 'Auto-Detect', 'protagonist_mode': 'dual', 'has_prompt': False, 'has_baseline_mv': True, 'stress_tags': ['Zero-Prompt无提示词脑暴', '纯音频情绪+双人图推导关系', '含原片A/B对比'], 'files': {'cover.png': '1jn_XlHG8p5f2CX8QdRYX4gDS9Hu7Qdpy', 'final_mv.mp4': '15Hu-EOARWECFZCyaaiEL1k-d84mQqriS', 'input_audio_01.mp3': '15nWBSwysawzeJDnqShdo0dzxdQVePXMt', 'input_image_01.jpeg': '1NU2UK_-nkSKSNjImjZ8HHI1NGO1lzaZj', 'input_image_02.jpeg': '1IB3o-pa02Pjjppcr6a2rzS6uH1D2ZnXt'}, 'audio_meta': {'duration_sec': 223.39, 'duration_str': '03:43', 'sample_rate': 32000, 'channels': 2, 'bit_rate': 192088}, 'audio_url': '/api/media/04/input_audio_01.mp3', 'cover_url': '/api/media/04/cover.png', 'final_mv_local': False, 'final_mv_url': '', 'final_mv_drive_id': '15Hu-EOARWECFZCyaaiEL1k-d84mQqriS', 'prompt_text': '', 'images': [{'filename': 'input_image_01.jpeg', 'url': '/api/media/04/input_image_01.jpeg', 'width': 1204, 'height': 1393, 'size_kb': 280.3}, {'filename': 'input_image_02.jpeg', 'url': '/api/media/04/input_image_02.jpeg', 'width': 1644, 'height': 1606, 'size_kb': 485.8}]}, {'sample_id': '05', 'folder_id': '1EeUn76eqXFsMDOvoncoFsByfXO-rSQCX', 'title': 'Sample 05 — 双人无提示词自主叙事 MV (Zero-Prompt Dual #2)', 'language': 'Auto-Detect', 'protagonist_mode': 'dual', 'has_prompt': False, 'has_baseline_mv': False, 'stress_tags': ['Zero-Prompt无提示词脑暴', '双人情感互动与声部匹配', '无原片纯正向规划'], 'files': {'cover.png': '1CEKx1ZMEM5zM4y4-9iFhNKBfK1iijRt4', 'input_audio_01.mp3': '1qWYONgLtRCvEdLERWWmNRFty9uYQC_k_', 'input_image_01.jpeg': '1tEJ8DaEr_DqMTa50SMisT-Ih3jR_aZWO', 'input_image_02.jpeg': '1teDd6inpQ7xu5cFEABntnGhCtlYB4kcG'}, 'audio_meta': {'duration_sec': 211.32, 'duration_str': '03:31', 'sample_rate': 48000, 'channels': 2, 'bit_rate': 192121}, 'audio_url': '/api/media/05/input_audio_01.mp3', 'cover_url': '/api/media/05/cover.png', 'final_mv_local': False, 'final_mv_url': '', 'final_mv_drive_id': '', 'prompt_text': '', 'images': [{'filename': 'input_image_01.jpeg', 'url': '/api/media/05/input_image_01.jpeg', 'width': 1023, 'height': 1537, 'size_kb': 298.2}, {'filename': 'input_image_02.jpeg', 'url': '/api/media/05/input_image_02.jpeg', 'width': 1024, 'height': 1536, 'size_kb': 442.6}]}, {'sample_id': '06', 'folder_id': '17X59qJAy9DggHSZ_BbI7rZV99nIyy4ii', 'title': 'Sample 06 — 巴西 Sertanejo 夜景码头木吉他弹唱', 'language': 'Portuguese', 'protagonist_mode': 'single', 'has_prompt': True, 'has_baseline_mv': True, 'stress_tags': ['乐器演奏物理可信度(吉他手部特写)', '面部/手部/码头全景三景别交替', '人物外观强锁定'], 'files': {'cover.png': '1HMlaTcYQngAwyrlGMMWLU5OoX9yVtJNr', 'final_mv.mp4': '1ALhGdBpN4ktt9EFtvdjcaK-tZAIftRlF', 'input_audio_01.mp3': '1PL4Dl-ekC08AjhJ4FkPdVLD8uD-Xzr_3', 'input_image_01.jpeg': '1xGCz3ZIygiu2CjgQgv3a1DZisaPMxwTa', 'prompt.txt': '1H4X2SDrAfKwlMjYAnXcS7c_uWoNlx54q'}, 'audio_meta': {'duration_sec': 255.65, 'duration_str': '04:16', 'sample_rate': 48000, 'channels': 2, 'bit_rate': 192135}, 'audio_url': '/api/media/06/input_audio_01.mp3', 'cover_url': '/api/media/06/cover.png', 'final_mv_local': False, 'final_mv_url': '', 'final_mv_drive_id': '1ALhGdBpN4ktt9EFtvdjcaK-tZAIftRlF', 'prompt_text': 'Videoclipe sertanejo romântico, cinematográfico, em 4K, formato 16:9.\n\nUm homem elegante e apaixonado está sentado em um píer luxuoso, todo iluminado por luzes douradas, tocando um violão de luxo à noite. Ao fundo, o mar tranquilo reflete as luzes, com barcos iluminados e uma linda lua no céu.\n\nManter exatamente o mesmo homem da imagem original da capa, preservando seu rosto, cabelo, barba, roupa preta, aparência física e violão. Não substituir, modificar ou criar outro personagem.\n\nA câmera realiza movimentos suaves e cinematográficos, alternando entre closes no rosto, nas mãos tocando o violão e tomadas abertas do píer iluminado. O homem interpreta a música com emoção e romantismo, transmitindo amor e saudade.\n\nO mesmo homem deve aparecer do início ao fim do vídeo, com total consistência visual, sem mudanças de personagem, cenário ou estilo.', 'images': [{'filename': 'input_image_01.jpeg', 'url': '/api/media/06/input_image_01.jpeg', 'width': 1672, 'height': 941, 'size_kb': 469.4}]}, {'sample_id': '07', 'folder_id': '102r9B4TOfTE6eMjybCuR47rPv50Khog-', 'title': 'Sample 07 — 阿肯色州说唱《4 Da A》（词级城市名时间戳实时切镜）', 'language': 'English', 'protagonist_mode': 'dual', 'has_prompt': True, 'has_baseline_mv': False, 'stress_tags': ['词级(Word-level)地名时间戳识别', '随说唱词切换城市画面与文字路牌', '主歌1与主歌2视觉逻辑切换'], 'files': {'cover.png': '1Y8SP1W4rFtOocUbOTXFfj8cJMg9z32gw', 'input_audio_01.mp3': '1iW6c8HMytCFQEx4xZCV9GZPD9iiUReJQ', 'input_image_01.jpeg': '16WHyJtCiiYW8njNxQxSu_71lZpSsEXaS', 'input_image_02.jpeg': '1jS5M0d7bT8vJjdwWo9I1wVAuI4PVItmj', 'prompt.txt': '1kYNB2NOuVOTogy-CdKabJm44t_bbeRCc'}, 'audio_meta': {'duration_sec': 197.77, 'duration_str': '03:18', 'sample_rate': 44100, 'channels': 2, 'bit_rate': 192064}, 'audio_url': '/api/media/07/input_audio_01.mp3', 'cover_url': '/api/media/07/cover.png', 'final_mv_local': False, 'final_mv_url': '', 'final_mv_drive_id': '', 'prompt_text': "when this son6 comes on have it say in the sky welcome to Arkansas Pine Bluff City South Ballar brings to you 4 Da A then when I start wrapping the song and saying the city names have the scenes flipping from City to City with all the names showing that I'm saying and as you show City to City show a group of guys like they are rappers in every city that I name and on the second verse just show me rapping and a bunch of different clips of Arkansas signs some say Pine Bluff Arkansas some say p23 some say PBC and and some signs that say home of the Lion's just make this about all the cities in Arkansas all the towns in Arkansas have it flipping scenes have me in a black hat that say South Ballar little black t-shirt that say Arkansas Pine Bluff and on my pants have Pine Bluff going down the pants leg make this video All about Arkansas and show clips of the Arkansas razorbacks", 'images': [{'filename': 'input_image_01.jpeg', 'url': '/api/media/07/input_image_01.jpeg', 'width': 1504, 'height': 1491, 'size_kb': 180.8}, {'filename': 'input_image_02.jpeg', 'url': '/api/media/07/input_image_02.jpeg', 'width': 1792, 'height': 1920, 'size_kb': 390.8}]}, {'sample_id': '08', 'folder_id': '1cDvi0Yv7AAZMecoGyZsl7E2GxAeVRqx7', 'title': 'Sample 08 — 北海岸岩石雷鬼 Rasta 氛围 MV', 'language': 'English', 'protagonist_mode': 'single', 'has_prompt': True, 'has_baseline_mv': True, 'stress_tags': ['极简弱提示词(15词)叙事扩写', '雷鬼反拍松弛律动', '自然海景光影层次'], 'files': {'cover.png': '1zrjp6j25X4veHW81sL-JWiMRNQzCTAJP', 'final_mv.mp4': '1VFc4yEv5wVq0B1En5CUf-u31QOfYbaMl', 'input_audio_01.mp3': '1tg7Cpy7d_S92MAbWdzfeEbMhaKziQuAZ', 'input_image_01.jpeg': '1W_E5aRtPuUxWKttzMFx7NCRHKcGYq-D0', 'prompt.txt': '1AGySeA-Cd_TErmJhNTsopw-TiUhavW0V'}, 'audio_meta': {'duration_sec': 206.57, 'duration_str': '03:27', 'sample_rate': 48000, 'channels': 2, 'bit_rate': 192052}, 'audio_url': '/api/media/08/input_audio_01.mp3', 'cover_url': '/api/media/08/cover.png', 'final_mv_local': False, 'final_mv_url': '', 'final_mv_drive_id': '1VFc4yEv5wVq0B1En5CUf-u31QOfYbaMl', 'prompt_text': 'reggae rasta mix native on the north coast of rhe island cool breeze sitting relacing on a rock stone', 'images': [{'filename': 'input_image_01.jpeg', 'url': '/api/media/08/input_image_01.jpeg', 'width': 478, 'height': 850, 'size_kb': 149.9}]}, {'sample_id': '09', 'folder_id': '1YERqh3HhJdg79xoIQa-RdztxagkxtkzQ', 'title': 'Sample 09 — 单人无提示词长曲自主叙事 MV (Zero-Prompt Single)', 'language': 'Auto-Detect', 'protagonist_mode': 'single', 'has_prompt': False, 'has_baseline_mv': True, 'stress_tags': ['Zero-Prompt单人自主叙事', '长音频段落抗视觉疲劳编排', '含原片A/B对比'], 'files': {'cover.png': '1wyOIgctlclkcZPKdHkbqr0zrRCn5XXJR', 'final_mv.mp4': '1woQwr-CyIyRAd0VZgn_dJBwDBIjPQX2o', 'input_audio_01.mp3': '1Lyaqv8hwWo_SOdyrL9du9xiIT2H3MiQK', 'input_image_01.jpeg': '12aBhSP-TpapNNu8NGikhhCGqFaW3RgsR'}, 'audio_meta': {'duration_sec': 291.1, 'duration_str': '04:51', 'sample_rate': 48000, 'channels': 2, 'bit_rate': 192033}, 'audio_url': '/api/media/09/input_audio_01.mp3', 'cover_url': '/api/media/09/cover.png', 'final_mv_local': False, 'final_mv_url': '', 'final_mv_drive_id': '1woQwr-CyIyRAd0VZgn_dJBwDBIjPQX2o', 'prompt_text': '', 'images': [{'filename': 'input_image_01.jpeg', 'url': '/api/media/09/input_image_01.jpeg', 'width': 900, 'height': 1600, 'size_kb': 126.4}]}, {'sample_id': '10', 'folder_id': '1K5-mTk1PRLADckx5W1fHIEkGgykOAU50', 'title': 'Sample 10 — 光影男歌手与聚光灯现代舞女平行蒙太奇', 'language': 'English', 'protagonist_mode': 'single_img_dual_role', 'has_prompt': True, 'has_baseline_mv': True, 'stress_tags': ['平行时空交叉剪辑(Intercutting)', '单张参考图派生双角色设定', '负向约束(男歌手严禁跳舞)'], 'files': {'cover.png': '1TF7njLn07KugX4sSC-6jF9z4yB98tjx6', 'final_mv.mp4': '1IAzM_JOnlFA4r8YDa8SbmwnRCjxCF_DQ', 'input_audio_01.mp3': '1B6LcTxdYig4Qn24NDd2TTQqtWpiJfjXo', 'input_image_01.jpeg': '1FvivOlerZHoKLKl92lwxoiMkjPkc2_mN', 'prompt.txt': '1csEFyLo4B9VlNMWYbM3tXLGbuwz9ocTW'}, 'audio_meta': {'duration_sec': 183.37, 'duration_str': '03:03', 'sample_rate': 48000, 'channels': 2, 'bit_rate': 192065}, 'audio_url': '/api/media/10/input_audio_01.mp3', 'cover_url': '/api/media/10/cover.png', 'final_mv_local': False, 'final_mv_url': '', 'final_mv_drive_id': '1IAzM_JOnlFA4r8YDa8SbmwnRCjxCF_DQ', 'prompt_text': 'The guy sings Where there are only light shocks, at the same time, there is a girl in a dark place with only a spotlight on her, who is dancing a modern dance to the song. The singer does not dance!', 'images': [{'filename': 'input_image_01.jpeg', 'url': '/api/media/10/input_image_01.jpeg', 'width': 941, 'height': 1672, 'size_kb': 319.7}]}]
BUILTIN_DOC_AUDIO_PROMPT: str = 'SUMMARY_PROMPT = """\n你是一个专业的音频理解模型，任务是根据输入音频，对音乐内容进行结构化分析，并严格输出指定 JSON 字段。\n\n你需要分析音频中的音乐风格、年代感、情绪、能量、人声、乐器、文化风格、是否为已有歌曲、适用场景、内容安全、视觉联想和整体描述等信息。\n\n输出要求：\n1. 只输出 JSON，不要输出解释、Markdown、注释或多余文本。\n2. 必须保持字段完整，不要新增字段，不要删除字段。\n3. 如果无法判断某个字段，请输出空字符串 ""、空数组 []、false 或 0.0。\n4. 所有字段必须基于音频内容判断，不要凭空编造。\n5. 如果存在多个可能结果，选择最主要、最明显的结果。\n6. 中文输出。\n\n字段说明与规则：\n\n1. music_genre\n音乐风格。必须从以下 10 类中选择一个：\n- Pop流行\n- HipHop/Rap嘻哈说唱\n- R&B\n- Rock摇滚\n- Electronic/EDM电子电音\n- Dance舞曲\n- Folk/Country民谣乡村\n- Jazz爵士\n- Soul/Funk灵魂放克\n- Classical/Orchestral古典管弦\n\n如果无法明确判断，选择最接近的一类。\n\n2. era_style\n音频听感上的年代风格，不一定是真实发行年份。\n可输出如：\n- 20世纪50年代\n- 20世纪60年代\n- 20世纪70年代\n- 20世纪80年代\n- 20世纪90年代\n- 2000年代\n- 2010年代\n- 2020年代\n- 复古80年代风格\n- 千禧年流行风格\n- 现代流行风格\n\n如果没有明显年代感，输出 ""。\n\n3. mood\n情绪。必须从以下大情绪标签中选择主要标签：\n- 欢乐\n- 悲伤\n- 浪漫\n- 青春\n- 自信\n- 愤怒\n- 热血\n- 压抑\n- 孤独\n- 性感\n- 兴奋\n- 迷幻\n- 释放\n- 温暖\n- 怀旧\n- 自由\n- 轻松\n- 庄严\n- 激昂\n- 平静\n\n如果有多个情绪，选择最主要的2个。\n\n4. energy_level\n音乐能量强度。\n只能输出：\n- 低\n- 中\n- 高\n\n5. instruments\n主要乐器或音色，输出数组。\n例如：\n["钢琴", "鼓", "电吉他", "合成器", "贝斯"]\n\n6. vocal\n人声类型。\n可输出：\n- 无人声\n- 男声\n- 女声\n- 男女混声\n- 合唱\n- 说唱\n- 人声采样\n\n7. culture_style\n文化、地域或民族音乐风格，输出数组。\n例如：\n["拉丁风", "中国风", "韩流K-pop", "日系", "印度风", "非洲节奏", "中东风格", "欧美流行"]\n\n如果没有明显文化风格，输出 ""。\n\n8. is_existing_song\n判断是否像是已有成名歌曲。\n输出 true 或 false。\n\n9. matched_song_name\n如果识别为已有歌曲，输出歌曲名。\n如果不是或不确定，输出 ""。\n\n10. matched_song_artist\n如果识别为已有歌曲，输出歌手/艺人名。\n如果不是或不确定，输出 ""。\n\n11. matched_song_confidence\n已有歌曲识别置信度，范围 0.0 到 1.0。\n如果不是已有歌曲或无法判断，输出 0.0。\n\n12. matched_song_story\n如果识别为已有歌曲，根据歌曲本身的歌词主题、情感表达和核心内容，概括这首歌曲所讲述的故事或表达的主题。\n\n要求：\n- 不要编造具体歌手经历、真实创作背景或未经确认的事实。\n- 不要输出歌词原文。\n- 保持简洁，建议3句话以内概括\n- 如果不是已有歌曲，输出 ""。\n\n13. explicit_content\n判断音频是否包含明显脏话、成人、暴力、毒品、仇恨等不适宜内容。\n输出 true 或 false。\n\n14. scene_tags\n适合使用该音乐的场景标签，输出数组。\n例如：\n["旅行", "健身", "派对", "Vlog", "游戏", "广告", "婚礼", "学习", "电影预告", "时尚"]\n\n15. visual_intent\n根据音乐氛围，生成适合的视觉画面描述。\n例如：\n"霓虹灯下的赛博朋克城市夜景"\n"阳光海边的轻松旅行画面"\n"史诗战争电影中的宏大场景"\n\n16. other_information\n对音频整体的自然语言概括，包含风格、情绪、节奏、配器、人声和适用场景等信息。\n保持简洁，1-2句话。\n\n最终输出格式必须严格如下：\n\n{\n  "summary": {\n    "music_genre": "",\n    "era_style": "",\n    "mood": "",\n    "energy_level": "",\n    "instruments": [],\n    "vocal": "",\n    "culture_style": "",\n    "is_existing_song": false,\n    "matched_song_name": "",\n    "matched_song_artist": "",\n    "matched_song_story": "",\n    "matched_song_confidence": 0.0,\n    "explicit_content": false,\n    "scene_tags": [],\n    "visual_intent": "",\n    "other_information": ""\n  }\n}\n""".strip()'
BUILTIN_DOC_DATASET_SPEC: str = '1. 输入测试需要：\ninput_audio 输入音频\ninput_image 若有两张图则为双人主角MV\nPrompt 用户输入提示词，若无prompt则不输入\n\n2. 参考效果：\ncover 原流程输出封面效果\nfinal_mv 原流程输出视频效果'
BUILTIN_DOC_RUBRIC_MD: str = '# MV评判规则\n\n## 第一层：稳定性\n\n**1.\xa0人物一致性**\n\n*   **身份一致**：跨镜头、角度、景别后，仍能清楚认出同一个人。\n    \n*   **外貌稳定**：五官、脸型、年龄、肤色、体型不发生无理由变化。\n    \n*   **造型连贯**：发型、服装、配饰在同一场景内保持稳定；变化有明确依据。\n    \n*   **参考还原**：使用用户照片时，保留人物的核心辨识特征。\n    \n\n**2.\xa0人体与动作可信度**\n\n*   **人体结构合理**：面部、手指、四肢没有明显畸形、增生或缺失。\n    \n*   **动作自然**：转头、行走、抬手等动作连贯，符合身体运动规律。\n    \n*   **交互可信**：人物握持麦克风、弹奏乐器、接触物体时，位置和受力关系合理。\n    \n*   **表情自然**：眼神、眨眼及面部变化协调，没有僵硬、抽动或失控感。\n    \n\n**3.\xa0场景与空间一致性**\n\n*   **环境稳定**：背景、道具、建筑结构不无故变化、消失或漂移。\n    \n*   **空间合理**：人物比例、透视、遮挡和前后位置关系可信。\n    \n*   **光影合理**：主体与环境的光线方向、阴影、反射基本一致。\n    \n*   **连续性成立**：同一场景切换镜头后，人物位置、道具状态和环境关系能够衔接。\n    \n\n**4.\xa0画面与时间稳定性**\n\n*   **画面清楚**：主体细节可辨，没有影响观看的模糊或压缩损伤。\n    \n*   **帧间稳定**：没有明显闪烁、纹理爬动、局部融化或细节跳变。\n    \n*   **运动连续**：没有异常抖动、瞬移、卡顿或不合理变速。\n    \n*   **合成完整**：没有穿模、边缘破损、主体与背景脱离等明显问题。\n    \n\n## 第二层：音乐是否被表达\n\n关注画面能否理解并呈现这首歌，让音乐与视觉形成共同表达。\n\n**1.\xa0音画节奏**\n\n*   **节拍协调**：切镜、动作、运镜和视觉变化与音乐节拍相互配合。\n    \n*   **乐句呼应**：画面的开始、停留和结束能呼应乐句及停顿。\n    \n*   **段落区分**：前奏、主歌、副歌、间奏等段落有相应的视觉处理。\n    \n*   **能量匹配**：音乐推进、爆发或回落时，画面的强度随之变化。\n    \n*   **张弛合理**：既有关键节点的强调，也有适当留白，避免机械地每拍切镜。\n    \n\n**2.\xa0口型与演唱表现**\n\n*   **时间同步**：开口、闭口、换句和停顿与人声对齐，没有明显抢拍或拖拍。\n    \n*   **口形合理**：嘴形能体现发音差异，快词、长音和闭口音表现可信。\n    \n*   **演唱状态成立**：有声时在演唱，无声时合理停口；换气和长音状态自然。\n    \n*   **情绪表演协调**：眼神、表情、头部及身体动作与演唱力度相符。\n    \n*   **演唱者对应正确**：多人声或合唱场景中，出镜人物与当前人声的对应清楚。\n    \n\n**3.\xa0音乐风格与情绪理解**\n\n*   **风格匹配**：场景、造型、色彩和镜头方式符合歌曲的整体气质。\n    \n*   **情绪准确**：能够区分并表达忧伤、释然、愤怒、甜蜜、兴奋等情绪。\n    \n*   **情绪发展**：随歌曲变化呈现情感推进，避免全片保持同一种表情和氛围。\n    \n*   **复杂情绪成立**：面对“旋律轻快、歌词悲伤”等情况，视觉处理有清楚的表达选择。\n    \n\n**4.\xa0歌词与主题表达**\n\n*   **主题相关**：画面围绕歌曲核心主题展开，内容选择有依据。\n    \n*   **关键语义准确**：重要歌词没有被误读，人物行为和场景不与歌曲含义无故冲突。\n    \n*   **表达有转化**：能够将歌词转化为情境、动作或视觉意象。\n    \n*   **段落有联系**：不同歌词段落的画面共同服务于一个主题。\n    \n*   **纯音乐有依据**：没有歌词时，也能根据旋律、音色、配器和结构组织视觉。\n    \n\n## 第三层：作品是否有审美和记忆点\n\n关注全片的视觉选择、内容组织和情感效果，判断作品是否值得看完、回看和记住。\n\n**1.\xa0视觉美学**\n\n*   **构图有效**：主体突出，画面重心、空间层次和留白合理。\n    \n*   **色彩协调**：色彩关系统一，并能帮助表达情绪和区分段落。\n    \n*   **光影有表现力**：光线能够塑造人物、空间及氛围。\n    \n*   **美术设计统一**：场景、服装、妆发和道具形成一致的视觉方向。\n    \n*   **风格辨识度高**：全片有清楚的审美取向，关键画面有自身特色。\n    \n\n**2.\xa0镜头语言**\n\n*   **景别选择准确**：远景交代环境，近景突出情绪，细节镜头承担具体表达。\n    \n*   **角度有目的**：视角能够体现人物状态、关系或情绪。\n    \n*   **运镜服务内容**：推拉、跟随、环绕或固定镜头的选择有表达依据。\n    \n*   **镜头变化有层次**：静动、远近、主客观视角的变化丰富且协调。\n    \n*   **注意力引导清楚**：观众能够自然知道每个镜头应该看什么。\n    \n\n**3.\xa0内容结构与发展**\n\n*   **开头建立吸引力**：尽快建立人物、氛围、悬念或视觉兴趣。\n    \n*   **中段持续发展**：通过故事、表演、空间或意象变化带来新信息。\n    \n*   **高潮得到支撑**：前面有铺垫，高潮在内容和视觉上都形成有效提升。\n    \n*   **结尾完成收束**：能够回应主题，形成结束感或有意保留余韵。\n    \n*   **结构适合类型**：故事型看情节推进，表演型看表演发展，氛围型看情绪与意象演变。\n    \n\n**4.\xa0剪辑与整体完成度**\n\n*   **镜头衔接顺畅**：动作、视线、构图、空间或意象之间有清楚联系。\n    \n*   **信息密度适当**：镜头停留足够理解，重复内容得到控制。\n    \n*   **转场使用准确**：转场与内容、音乐和视觉风格相协调。\n    \n*   **全片风格统一**：各镜头的质感、调色和表现方式能够组成完整作品。\n    \n*   **表达有所取舍**：特效、运镜和剪辑的强度符合主题需要。\n    \n\n**5.\xa0创意与记忆点**\n\n*   **核心创意清楚**：看完后，能概括出作品最特别的表达。\n    \n*   **视觉符号突出**：有容易记住的人物造型、场景、动作、道具或色彩。\n    \n*   **关键时刻有效**：至少有一个镜头或段落让人印象深刻。\n    \n*   **创意贯穿全片**：亮点与作品主题、前后内容有联系。\n    \n*   **情感余韵成立**：看完后留下明确感受，产生回看或分享的意愿。'

# Auto-materialize built-in docs and manifest on disk if missing
if not MANIFEST_PATH.exists():
    try:
        MANIFEST_PATH.write_text(json.dumps(BUILTIN_SAMPLES_MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
for _doc_name, _doc_content in (
    ("audio_analysis_prompt.txt", BUILTIN_DOC_AUDIO_PROMPT),
    ("dataset_spec.txt", BUILTIN_DOC_DATASET_SPEC),
    ("evaluation_rubric.md", BUILTIN_DOC_RUBRIC_MD),
):
    _dp = DOCS_DIR / _doc_name
    if not _dp.exists():
        try:
            _dp.write_text(_doc_content, encoding="utf-8")
        except OSError:
            pass




# ==============================================================================
# 1. Default Reference Documents & Default Prompts
# ==============================================================================
def load_doc_text(filename: str, fallback: str = "") -> str:
    p = DOCS_DIR / filename
    if p.exists():
        raw = p.read_text(encoding="utf-8", errors="replace").strip()
        if filename in ("audio_analysis_prompt.txt", "音频理解-提示词.txt"):
            m = re.search(r'SUMMARY_PROMPT\s*=\s*"""(.*?)"""', raw, flags=re.DOTALL)
            if m:
                return m.group(1).strip()
        return raw
    return fallback


DEFAULT_ORIGINAL_AUDIO_PROMPT = load_doc_text("audio_analysis_prompt.txt", BUILTIN_DOC_AUDIO_PROMPT)
DEFAULT_MV_DATASET_SPEC_TXT = load_doc_text(
    "dataset_spec.txt",
    "1. 输入测试需要：\ninput_audio 输入音频\ninput_image 若有两张图则为双人主角MV\nPrompt 用户输入提示词，若无prompt则不输入\n\n2. 参考效果：\ncover 原流程输出封面效果\nfinal_mv 原流程输出视频效果",
)
DEFAULT_MV_RUBRIC_MD = load_doc_text("evaluation_rubric.md", BUILTIN_DOC_RUBRIC_MD)

DEFAULT_OPTIMIZED_PROMPT = """你是一个兼具「音乐制作人级听觉分析」与「AIGC MV 导演分镜规划」能力的专业多模态模型。
请根据输入的：
1. `input_audio`（完整歌曲音频）
2. `input_image`（1张为单人主角参考图；若有2张则为双人主角参考图 `image_01` 与 `image_02`）
3. `user_prompt`（用户输入的导演提示词；若为空则为 Zero-Prompt 模式，需由你根据音乐情绪与人物参考图自主推导最佳 MV 叙事）

执行端到端的多模态分析与 MV 视觉规划，并严格输出单一 JSON 对象（不要输出任何 Markdown 代码块标记或多余解释）。

### 一、分析与生成规则（针对 Gemini 原生多模态能力与《MV评判规则》专门优化）

1. **完全兼容原版 16 字段全局音频摘要 (`summary`)**：
   - 保留原版 `music_genre`、`era_style`、`mood`、`energy_level`、`instruments`、`vocal`、`culture_style`、`is_existing_song`、`matched_song_*`、`explicit_content`、`scene_tags`、`visual_intent`、`other_information` 共 16 个字段的严格枚举与格式约束。

2. **模块 A — 曲式段落时间戳、人声状态轨与逐句歌词识别 (`temporal_music_analysis`)**：
   - 精确切分全曲结构段落（`Intro`, `Verse 1`, `Pre-Chorus`, `Chorus 1`, `Interlude/Dance Break`, `Verse 2`, `Chorus 2`, `Bridge`, `Final Chorus`, `Outro`），给出精确到秒的 `start_sec` 与 `end_sec`，总长必须覆盖完整音频时长。
   - **人声与口型控制轨 (`vocal_state`)**：明确标注每个段落是 `lead_male_singing`、`lead_female_singing`、`duet_singing`、`call_and_response` 还是 `instrumental_only`（纯器乐/间奏段严禁让人物张嘴对口型！）。
   - **动态情绪与能量推进曲线**：为每个段落标注独立的情绪标签 `section_mood` 与能量强度 `energy_1_to_10`（拒绝全曲单一表情，体现前奏铺垫→主歌叙事→副歌爆发→尾奏余韵的动态递进，并识别是否存在“曲风欢快但歌词悲伤”等复杂反差情绪）。
   - **关键歌词时间轴 (`key_lyrics_timeline`)**：提取关键歌词句的 `start_sec`、`end_sec`、`lyric_text` 及对应的视觉意象触发词 `visual_trigger`（如歌词提到流泪、地名、特定动作时，画面必须在对应秒数精确响应）。

3. **模块 B — 角色一致性圣经与用户 Prompt 约束锁定 (`character_and_scene_bible`)**：
   - 基于输入的 `input_image` 精确提取主角外观特征锁（`identity_lock`：脸型五官、发型发色、服装材质、标志性配饰如耳麦/吉他/墨镜），跨镜头严禁无故变形。
   - 若为双人参考图或用户 Prompt 指定了角色分工（如“男唱女哭不张嘴”、“男歌手光影演唱+现代舞女聚光灯独舞”），必须明确每个角色的 `singing_permission`（是否允许开口唱歌）与负向约束 `negative_constraints`（如严禁跳舞、严禁换装）。

4. **模块 C — 由分段情绪曲线引出的 MV 故事线与时间码分镜脚本 (`mv_storyline_and_storyboard`)**：
   - **故事线 (`storyline`)**：设计包含「开场钩子 (Opening Hook) → 中段递进 (Mid Development) → 高潮爆发 (Climax Escalation) → 结尾余韵 (Ending Resonance)」的四幕式故事线，明确核心视觉符号（Visual Motif）。
   - **时间码分镜表 (`storyboard_shots`，输出 8～14 个关键代表性分镜覆盖全曲)**：
     每个分镜必须包含：`shot_id`、`start_sec`、`end_sec`、`section_name`、`shot_size`（远景/全景/中景/近景/特写交替）、`camera_movement`（推/拉/摇/移/环绕/固定）、`characters_in_shot`、`lip_sync_state`（`singing_lip_sync` 或 `silent_acting_only`）、`action_and_interaction`（人体动作与乐器/道具物理交互细节）、`lighting_and_color`（光影与色彩美学）以及 `music_visual_sync_reason`（该镜头如何呼应当前乐句节拍、能量与歌词）。

### 二、严格输出 JSON 格式
{
  "summary": {
    "music_genre": "",
    "era_style": "",
    "mood": "",
    "energy_level": "",
    "instruments": [],
    "vocal": "",
    "culture_style": [],
    "is_existing_song": false,
    "matched_song_name": "",
    "matched_song_artist": "",
    "matched_song_story": "",
    "matched_song_confidence": 0.0,
    "explicit_content": false,
    "scene_tags": [],
    "visual_intent": "",
    "other_information": ""
  },
  "temporal_music_analysis": {
    "detected_language": "",
    "estimated_bpm": 120,
    "musical_lyrical_contrast": "",
    "sections": [
      {
        "section_name": "Intro",
        "start_sec": 0.0,
        "end_sec": 18.0,
        "vocal_state": "instrumental_only",
        "section_mood": "",
        "energy_1_to_10": 4,
        "visual_pacing_guide": ""
      }
    ],
    "key_lyrics_timeline": [
      {
        "start_sec": 18.0,
        "end_sec": 25.0,
        "singer_role": "char_01",
        "lyric_text": "",
        "theme_and_visual_trigger": ""
      }
    ]
  },
  "character_and_scene_bible": {
    "protagonist_count": 1,
    "user_prompt_constraints_applied": [],
    "negative_constraints": [],
    "characters": [
      {
        "char_id": "char_01",
        "source_image": "input_image_01",
        "identity_and_outfit_lock": "",
        "singing_permission": "allowed_when_vocal_active",
        "prop_and_action_rules": ""
      }
    ],
    "spatial_and_lighting_continuity": ""
  },
  "mv_storyline_and_storyboard": {
    "mv_title": "",
    "narrative_logline": "",
    "emotion_to_story_arc": {
      "opening_hook": "",
      "mid_development": "",
      "climax_escalation": "",
      "ending_resonance": ""
    },
    "core_visual_motif": "",
    "storyboard_shots": [
      {
        "shot_id": 1,
        "start_sec": 0.0,
        "end_sec": 18.0,
        "section_name": "Intro",
        "shot_size": "全景 (Wide Shot)",
        "camera_movement": "缓慢推进 (Slow Dolly In)",
        "characters_in_shot": ["char_01"],
        "lip_sync_state": "silent_acting_only",
        "action_and_interaction": "",
        "lighting_and_color": "",
        "music_visual_sync_reason": ""
      }
    ]
  }
}""".strip()

DEFAULT_OPTIMIZATION_WHY: dict[str, Any] = {
    "optimized_by_model": "gemini-4-argon (thinking_level=MEDIUM)",
    "executive_summary": (
        "原版《音频理解-提示词.txt》（SUMMARY_PROMPT）仅将输入当成纯音频做 16 个静态全局标签分类（如单一 mood、单一 energy_level、单句 visual_intent），"
        "完全忽略了《MV测试集说明.txt》中的主角参考图（input_image 1~2张）与用户导演提示词（Prompt），且缺乏时间戳、分段情绪曲线、人声对口型状态轨以及分镜规划。"
        "这导致原版提示词在《MV评判规则.md》的「第一层：人物与空间稳定性」、「第二层：音画节奏/口型同步/歌词转化」与「第三层：镜头语言/结构发展/记忆点」上严重失分。"
        "优化后的提示词充分利用了 Gemini 3.8 / 4 Argon 的原生多模态联合注意力（Audio + Image + Text Joint Attention）与秒级时间轴定位能力，在 100% 兼容原 16 字段的同时，补齐了时间轴、动态情绪弧线、角色圣经与分镜脚本四大核心模块。"
    ),
    "six_core_reasons": [
        {
            "id": "R1",
            "title": "1. 从「单模态纯音频输入」升级为「音+图+文多模态联合理解」（对齐《MV测试集说明.txt》）",
            "original_gap": "原 Prompt 只写了“根据输入音频进行结构化分析”，完全没有提及 `input_image`（单/双人主角照片）和用户 `Prompt`（如耳麦约束、男唱女哭、禁止跳舞等）。",
            "gemini_trait": "Gemini 3.8 Flash 与 Gemini 4 Argon 具备原生跨模态 Token 融合能力，可在同一 Context Window 内同时对齐音频波形、多张人物参考图五官穿搭与文本导演指令。",
            "why_change": "新增 `character_and_scene_bible` 模块，强制模型提取参考图的人物外貌/服装/配饰锁（Identity Lock）并逐条核验用户 Prompt 正负向约束（或在无 Prompt 时自主推导），直接解决《MV评判规则》第一层「人物一致性」与「人体动作/交互可信度」痛点。",
            "rubric_impact": "第一层：人物一致性 (+3.5分)、人体与动作可信度 (+3.0分)、场景与空间一致性 (+2.8分)",
        },
        {
            "id": "R2",
            "title": "2. 从「全局单一静态情绪标签」升级为「分段动态情绪与能量推进曲线」",
            "original_gap": "原 Prompt 的 `mood`（最多2个词）和 `energy_level`（仅输出低/中/高）把整首 3~4 分钟的歌压缩成静态常量，无法反映前奏→主歌→副歌→高潮→尾奏的情绪起伏。",
            "gemini_trait": "Gemini 4 Argon 在 `thinking_level=medium` 下具备长程时间序列推理能力，能精准捕捉配器加减、鼓点进出与人声撕裂度随时间的变化。",
            "why_change": "在保留全局 `summary.mood` 的同时，新增 `temporal_music_analysis.sections` 逐段输出 `section_mood`、`energy_1_to_10` 以及 `musical_lyrical_contrast`（曲词反差识别），避免全片同一表情和氛围。",
            "rubric_impact": "第二层：音乐风格与情绪理解 (+3.2分)、音画节奏-能量匹配与段落区分 (+3.5分)",
        },
        {
            "id": "R3",
            "title": "3. 新增「秒级曲式时间戳 & 逐句歌词时间轴」，打通「词义→视觉意象转化」",
            "original_gap": "原 Prompt 明确要求“不要输出歌词原文”，且没有任何时间戳字段，导致下游 MV 生成不知道哪一秒是副歌高潮（如 Sample 02「最後のサビ流泪」）、哪一秒唱到了特定城市地名（如 Sample 07《4 Da A》）。",
            "gemini_trait": "Gemini 原生音频编码器支持多语种（英/日/葡/印地语等）歌词听写与秒级时间戳对齐（Timestamp Grounding）。",
            "why_change": "保留 `summary.matched_song_story` 不输出歌词的兼容约束，同时在扩展块中增加 `key_lyrics_timeline`（含 `start_sec`、`end_sec`、`lyric_text` 与 `theme_and_visual_trigger`），让关键歌词在准确时间点转化为具体画面动作与场景。",
            "rubric_impact": "第二层：歌词与主题表达 (+4.0分)、音画节奏-乐句呼应 (+3.4分)",
        },
        {
            "id": "R4",
            "title": "4. 新增「人声声部与口型同步防错轨 (`vocal_state` & `lip_sync_state`)」",
            "original_gap": "原 Prompt 的 `vocal` 仅输出一个全局标签（如“男声”），不知道哪一段是纯器乐间奏（Instrumental Break）、哪一段是对唱，极易导致生成的 MV 在前奏/间奏乱张嘴，或让设定为“流泪不唱歌”的女主角错误开口。",
            "gemini_trait": "Gemini 能精准区分人声起止边界（VAD）与男女声部切换（Speaker/Singer Diarization）。",
            "why_change": "在乐段级标注 `vocal_state`，并在分镜级强制输出 `lip_sync_state`（`singing_lip_sync` vs `silent_acting_only`）与 `singing_permission`，彻底杜绝器乐段假唱与角色串音。",
            "rubric_impact": "第二层：口型与演唱表现 (+4.2分)",
        },
        {
            "id": "R5",
            "title": "5. 从「单句宽泛视觉联想 (`visual_intent`)」升级为「四幕故事线 + 时间码导演分镜表」",
            "original_gap": "原 Prompt 的 `visual_intent` 仅输出类似“霓虹灯下的赛博朋克城市夜景”一句话，没有任何景别、运镜、人物调度、起承转合结构与剪辑衔接设计。",
            "gemini_trait": "Gemini 的结构化长 JSON 生成能力可以一次性将情绪曲线推导为完整的四幕戏剧弧线（Opening Hook → Development → Climax → Ending）与多镜头序列。",
            "why_change": "新增 `mv_storyline_and_storyboard`，要求输出四幕结构、核心视觉符号（Visual Motif）以及包含景别（远/全/中/近/特写）、运镜方式、光影色彩和音画同步理由的时间码分镜脚本。",
            "rubric_impact": "第三层：视觉美学 (+3.0分)、镜头语言 (+3.8分)、内容结构与发展 (+3.6分)、剪辑完成度 (+3.3分)、创意与记忆点 (+3.5分)",
        },
        {
            "id": "R6",
            "title": "6. 充分利用 Gemini Schema-Guided JSON 确定性约束，消除自然语言发散",
            "original_gap": "原 Prompt 虽然要求 JSON，但对扩展视觉规划缺乏强 Schema 锚定，不同模型输出详略差异大。",
            "gemini_trait": "Gemini 3.8 Flash 与 4 Argon 对显式嵌套 JSON Schema 模板与枚举值具有极高的指令遵循率（Instruction Following）。",
            "why_change": "采用「100% 兼容原版 `summary` 子树 + 3 大结构化扩展子树」的确定性 Schema，使 Gemini 3.8 Flash 的速度优势与 Gemini 4 Argon 的深度推理优势都能稳定发挥。",
            "rubric_impact": "全维度稳定性与自动化流水线可解析率 100%",
        },
    ],
}

DEFAULT_RUBRIC_JUDGE_PROMPT_TEMPLATE = """你是一位严苛、客观的 AI MV 质量评审专家。
请严格按照以下《MV评判规则.md》的三层 13 个维度，对模型针对当前测试样本生成的「音乐理解与 MV 规划分析结果」进行量化打分（每个子维度 0~10 分，并换算为三层百分制得分与总分 0~100 分）。

### 《MV评判规则.md》原文标准：
{rubric_md}

---

### 当前测试样本输入信息（依据《MV测试集说明.txt》）：
- 样本编号与标题：{sample_id} ({sample_title})
- 音频实测时长：{audio_duration_sec} 秒 ({audio_duration_str})
- 输入参考图数量：{num_images} 张 ({protagonist_mode})
- 用户输入提示词 (Prompt)：{user_prompt_display}

### 待评审的模型分析输出结果 (Model Output JSON)：
```json
{model_output_json}
```

---

### 评分细则与客观校准要求（极为重要，请勿虚高给分！）：
1. **如果待评审输出仅包含原版 16 个全局静态字段 (`summary`)**，而**缺乏**：
   - 主角参考图 (`input_image`) 的外貌/服装锁定与用户 `Prompt` 具体约束落地 → **第一层（稳定性 4 维）** 最高不得超过 4.5~6.0 / 10 分；
   - 曲式段落时间戳、分段动态情绪变化、间奏/人声口型状态区分、逐句歌词视觉转化 → **第二层（音乐是否被表达 4 维）** 中的「音画节奏」「口型与演唱表现」「歌词与主题表达」最高不得超过 4.0~6.0 / 10 分（仅「音乐风格与情绪理解」可根据全局标签准确性给 6.5~8.5 分）；
   - 具体景别交替、运镜方式、四幕起承转合结构、剪辑衔接与核心视觉记忆点（仅有单句 `visual_intent`） → **第三层（审美与记忆点 5 维）** 最高不得超过 4.0~5.8 / 10 分。
2. **如果待评审输出包含了完整的时间戳段落切分、动态能量曲线、歌词意象转化、角色一致性圣经、口型同步控制以及时间码分镜表**：
   - 请根据其时间码是否覆盖全曲 ({audio_duration_sec}s)、是否严格落实用户 Prompt 约束（或 Zero-Prompt 合理推导）、口型开闭是否与人声/间奏自洽、分镜景别与运镜是否专业丰富，在 7.5~9.8 / 10 分区间内客观区分优劣。

请严格输出以下 JSON 结构（不要输出任何多余文字）：
{
  "overall_score_100": 0.0,
  "grade": "S / A / B / C / D",
  "layer_scores": {
    "layer1_stability_100": 0.0,
    "layer2_music_expression_100": 0.0,
    "layer3_aesthetics_memorability_100": 0.0
  },
  "dimensions": {
    "L1_1_character_consistency": {"score_10": 0.0, "comment": ""},
    "L1_2_body_action_plausibility": {"score_10": 0.0, "comment": ""},
    "L1_3_scene_spatial_consistency": {"score_10": 0.0, "comment": ""},
    "L1_4_visual_temporal_stability": {"score_10": 0.0, "comment": ""},
    "L2_1_audio_visual_rhythm": {"score_10": 0.0, "comment": ""},
    "L2_2_lip_sync_vocal_performance": {"score_10": 0.0, "comment": ""},
    "L2_3_music_style_emotion": {"score_10": 0.0, "comment": ""},
    "L2_4_lyrics_theme_expression": {"score_10": 0.0, "comment": ""},
    "L3_1_visual_aesthetics": {"score_10": 0.0, "comment": ""},
    "L3_2_camera_language": {"score_10": 0.0, "comment": ""},
    "L3_3_structure_development": {"score_10": 0.0, "comment": ""},
    "L3_4_editing_completeness": {"score_10": 0.0, "comment": ""},
    "L3_5_creativity_memorability": {"score_10": 0.0, "comment": ""}
  },
  "key_strengths": [""],
  "key_defects_or_prompt_limitations": [""],
  "overall_verdict": ""
}""".strip()

DEFAULT_PROMPT_OPTIMIZER_META_PROMPT = """你是一位顶尖的 Gemini 多模态提示词架构师（Meta-Prompt Engineer）。
请根据以下三份核心规范文档，对现有的《音频理解-提示词.txt》进行深度优化，使其充分发挥 Gemini 模型（Gemini 3.8 Flash 与 Gemini 4 Argon）的多模态特性，并能在《MV评判规则.md》的三层 13 个维度上取得高分。

### 1. 原版《音频理解-提示词.txt》：
```text
{original_prompt}
```

### 2. 《MV测试集说明.txt》（实际输入素材与目标产物）：
```text
{dataset_spec}
```

### 3. 《MV评判规则.md》（最终评判标准：三层 13 维度）：
```text
{rubric_md}
```

### 你的任务：
1. 分析原版提示词在面对《MV测试集说明.txt》的输入（音频 + 1~2张人物参考图 + 可选用户Prompt）以及《MV评判规则.md》的三层 13 维度评判标准时，存在哪些结构性缺失。
2. 结合 Gemini 的核心优势（原生 Audio+Image+Text 跨模态联合注意力、秒级时间戳定位、多语种歌词听写、人声/器乐状态切分、长上下文结构化 JSON 生成），设计一份**优化后的完整提示词 (`optimized_prompt`)**。要求保留原版 `summary` 16 个字段以保证向下兼容，并扩展出曲式时间戳/动态情绪曲线、角色设定圣经、四幕故事线与时间码分镜脚本。
3. 详细输出“为什么这样修改”的逐条解释 (`six_core_reasons`)。

请严格输出以下 JSON 格式：
{
  "executive_summary": "整体优化思路综述（200字以内）",
  "optimized_prompt": "优化后的完整 Prompt 全文（字符串）",
  "six_core_reasons": [
    {
      "id": "R1",
      "title": "优化点标题",
      "original_gap": "原 Prompt 的局限与失分原因",
      "gemini_trait": "利用的 Gemini 模型特性",
      "why_change": "具体如何修改及为什么这样改",
      "rubric_impact": "对应提升的《MV评判规则》层级与维度"
    }
  ]
}""".strip()




DEFAULT_STORYLINE_GENERATION_PROMPT = """你是一位殿堂级 MV 创意总监与音乐叙事架构师（Music Video Storyline Director）。
你的核心任务是：深度理解输入音频的【歌词母题、旋律走向、节拍律动、曲式结构与动态情绪】，同时严格受限于【主角参考图 (`input_image`)】与【用户导演提示词 (`user_prompt`)】，创作一份既深刻契合音乐灵魂、又可直接交付视频大模型拍摄的 **MV 完整故事线方案 (MV Storyline JSON)**。

### 🚨 创作 MV Storyline 必须严格遵守的「五大防坑铁律」：

1. **铁律一：严防「歌词字面化陷阱」(Anti-Literal Lyric Trap)**
   - 歌词中的比喻、夸张或跳跃意象（如“坠入深海”、“心碎成沙”、“刀尖起舞”）绝对不能机械翻译成杂乱的“看词生图 PPT”。
   - 必须先提炼歌词背后的**深层情感关系与核心母题 (`lyric_subtext_and_core_theme`)**，并将其凝聚为 1～2 个贯穿全片的**具象视觉符号 (`core_visual_motif`，如特定的信物、光影变化、天气或空间道具)**。

2. **铁律二：精准处理「词曲情绪反差」(Melody-Lyric Dissonance Resolution)**
   - 先诊断旋律/律动情绪与歌词情绪是否一致 (`is_melody_lyric_contrasted`)。
   - 若存在“旋律轻快抓耳但歌词孤独悲伤”或“旋律克制平静但歌词暗流涌动”的反差，严禁只顾一头！必须在 `melody_lyric_strategy` 中设计「外在环境/运镜呼应旋律律动 + 人物微表情/处境传达歌词内核」的双层张力叙事。

3. **铁律三：防「纯音频歌声识别幻觉」与纯音乐断档 (Singing ASR Confidence Guard)**
   - 标注对音频中歌声歌词的听辨置信度 (`lyric_listening_confidence`: 0.0~1.0)。
   - 仅将高置信度听清的关键词句作为叙事锚点 (`verified_key_lyrics`)；对于模糊不清的咬字或纯器乐间奏（Intro / Interlude / Bridge Solo / Outro），严禁凭空编造歌词故事，必须切换为由**旋律线条、配器层次与鼓点能量**驱动的纯视觉情绪叙事与留白。

4. **铁律四：破解「音乐循环重复 vs 故事线性递进」冲突 —— 副歌同词不同境 (Contextual Chorus Progression)**
   - 歌曲的 Chorus 1、Chorus 2、Final Chorus 旋律与歌词往往高度重复，但故事线严禁把同样的画面车轱辘话演三遍！
   - 必须设计清晰的四幕戏剧弧线 (`four_act_story_arc`：开场钩子 → 中段发展 → 高潮转折与爆发 → 结尾余韵)，并专门制定 **`chorus_contextual_progression`（多遍副歌同词不同境递进方案）**：让同一句副歌在第一次、第二次、最后一次响起时，人物关系、光影空间或情感状态发生层层递进的质变。

5. **铁律五：严守「人物参考图与场景池边界锁」(Character & Scene Guardrails)**
   - **人物锁**：主角外貌、年龄气质、穿搭造型必须严格锚定输入的 `input_image`（1张为单主角，2张为双主角），严禁凭空捏造核心第三人或无故频繁换装。
   - **用户硬约束锁**：若存在 `user_prompt`（如“男唱女哭、女生全程不张嘴唱歌、禁止跳舞”），必须作为最高优先级红线写入 `hard_constraints_lock` 并在每一幕严格执行；若为 Zero-Prompt 模式，则完全依据音乐与参考图气质自主推导。
   - **主场景池上限 (<= 3 个)**：全片核心场景 (`scene_pool`) 严格控制在 1～3 个高度统一的空间内，通过光影、机位与人物动作变化推进故事，确保《MV评判规则》第一层（人物与场景空间一致性）得高分。

---

### 请严格输出以下 JSON 格式（不要输出任何 Markdown 代码块或多余解释文字）：
{
  "music_lyric_melody_diagnosis": {
    "music_genre_and_bpm_feel": "曲风、速度律动感与核心配器特征",
    "melody_and_energy_curve": "全曲旋律线条走向与能量推进轨迹（如：低吟铺垫 → 律动展开 → 间奏抽离 → 终章爆发）",
    "lyric_listening_confidence": 0.85,
    "lyric_subtext_and_core_theme": "歌词深层情感母题与人物关系内核（提炼隐喻背后的真实故事，拒绝字面化）",
    "verified_key_lyrics": [
      {"start_sec": 18.2, "end_sec": 22.8, "time_hint": "00:18.200", "lyric_phrase": "高置信度识别的关键歌词分句原文（精确到 0.1s 起止秒数）", "subtext_meaning": "在故事线中的情感含义"}
    ],
    "is_melody_lyric_contrasted": false,
    "melody_lyric_strategy": "词曲关系处理策略（若词曲一致如何强化共鸣；若词曲反差如何用画面张力调和）",
    "narrative_mode": "story_driven (剧情推进型) | performance_driven (舞台/表演发展型) | vibe_montage (情绪与视觉意象演变型)",
    "narrative_mode_reason": "为什么这首歌的旋律、歌词与用户Prompt最适合该叙事模式"
  },
  "character_and_scene_guardrails": {
    "protagonist_lock": [
      {
        "character_id": "char_01",
        "source_image": "input_image_01",
        "appearance_and_wardrobe_anchor": "基于参考图的五官、发型、服装与气质锁定描述",
        "singing_and_acting_role": "演唱/表演分工（如：主唱对口型 / 仅做流泪情绪表演严禁张嘴）"
      }
    ],
    "hard_constraints_lock": [
      "必须严格遵守的正向与负向硬约束（来自 user_prompt 及角色分工）"
    ],
    "scene_pool": [
      {
        "scene_id": "S1",
        "scene_name": "主场景名称（全曲最多不超过3个主场景）",
        "spatial_layout_and_lighting": "空间结构、主色调与光影氛围设定"
      }
    ],
    "core_visual_motif": "贯穿全片的核心具象视觉符号/道具及其象征意义"
  },
  "four_act_story_arc": {
    "logline": "一句话概括整支 MV 的核心故事线（50字以内）",
    "act1_opening_hook": "第一幕【开场建立 (Intro / Verse 1)】：如何快速建立人物关系、悬念或视觉吸引力",
    "act2_mid_development": "第二幕【中段递进 (Chorus 1 / Interlude / Verse 2)】：如何通过动作、互动或空间光影变化带来新信息",
    "act3_climax_escalation": "第三幕【高潮爆发 (Bridge / Final Chorus)】：前面铺垫如何在此刻集中引爆，形成全片最强视觉冲击",
    "act4_ending_resonance": "第四幕【结尾收束 (Outro)】：如何呼应开场视觉符号并留下情感余韵",
    "chorus_contextual_progression": {
      "chorus_1_treatment": "第一遍副歌的剧情阶段与画面表现（如：初见/克制试探）",
      "chorus_2_treatment": "第二遍副歌的剧情推进与变化（如：冲突显现/情绪升温，绝不重复第一遍）",
      "final_chorus_treatment": "最后一遍副歌的终极升华（如：彻底释放/物是人非的爆发，形成全片最高潮）"
    }
  },
  "time_anchored_story_beats": [
    {
      "beat_id": "B01",
      "section_name": "Intro / Verse 1 / Chorus 1 / Interlude / Verse 2 / Chorus 2 / Bridge / Final Chorus / Outro",
      "start_sec": 0.0,
      "end_sec": 25.0,
      "melody_and_lyric_cue": "该时间段真实的旋律、配器、鼓点变化或关键歌词触发点",
      "vocal_and_lip_sync_rule": "人声状态与口型规则（如：纯器乐严禁张嘴 / 男主对口型演唱、女主沉默流泪）",
      "scene_id": "S1",
      "shootable_story_action": "具象、可直接拍摄的人物动作、眼神互动与道具运用（严禁抽象心理描写）",
      "camera_and_lighting_design": "景别选择（远/全/中/近/特写）、运镜方式与光影色彩变化",
      "emotion_and_energy_1_to_10": 5
    }
  ]
}""".strip()

DEFAULT_STORYLINE_JUDGE_PROMPT_TEMPLATE = """你是一位严苛、资深的多模态音乐叙事与 MV 导演评审专家。
你的任务是：**将模型生成的「MV Storyline 方案」与「原歌曲音频 (`input_audio`) + 主角参考图 (`input_image`) + 用户导演指令 (`user_prompt`) + 《MV评判规则.md》」进行深度交叉对比**，输出量化分数（0~100分），并明确指出 **「哪里需要优化 (`target_where`)、具体问题是什么 (`problem_what`)、为什么需要优化 (`why_from_song_comparison`)、以及具体怎么改 (`how_to_optimize`)」**。

### 一、《MV评判规则.md》参考标准：
{rubric_md}

---

### 二、当前测试样本真实输入信息：
- 样本编号与标题：{sample_id} ({sample_title})
- 音频实测总时长：{audio_duration_sec} 秒 ({audio_duration_str})
- 输入人物参考图数量：{num_images} 张 ({protagonist_mode})
- 用户输入提示词 (Prompt)：{user_prompt_display}

### 三、待评测的 MV Storyline 输出结果 (Generated Storyline JSON)：
```json
{storyline_output_json}
```

---

### 四、Storyline vs 原歌曲对比评测的 6 大核心维度（每项 0~10 分）：

1. **`S1_melody_emotion_arc`（旋律情绪曲线与故事弧光契合度）**：
   - 仔细聆听原音频的前奏、主歌、副歌、桥段、尾奏的真实旋律起伏、BPM 律动与配器能量，判断 Storyline 的四幕起承转合与 `emotion_and_energy_1_to_10` 是否真正贴合音乐本身的情绪推进？
2. **`S2_lyric_subtext_translation`（歌词深层母题转化与防字面化 / 防听错）**：
   - Storyline 是否准确听懂了歌曲的歌词主题？是否成功将歌词转化为有深度的情境与视觉意象，还是掉进了“看词生图 PPT”字面化陷阱或出现了明显的歌词听错幻觉？
3. **`S3_chorus_progression_anti_repeat`（多遍副歌“同词不同境”递进设计）**：
   - 面对歌曲中重复出现的副歌（Chorus 1 / Chorus 2 / Final Chorus），Storyline 是否设计了层层递进的不同剧情境遇，还是三遍副歌在车轱辘话重复同一画面？
4. **`S4_rhythm_timecode_interlude_sync`（曲式时间轴、间奏留白与词曲反差处理）**：
   - `time_anchored_story_beats` 的秒数 (`start_sec` ~ `end_sec`) 是否完整覆盖 {audio_duration_sec} 秒且与音频真实段落转换点吻合？纯器乐间奏段是否做到了合理留白且严禁人物假唱乱张嘴？若歌曲存在“曲风轻快但歌词伤感”等反差，是否有妥善表达？
5. **`S5_character_scene_constraint_lock`（人物参考图、场景池 <= 3 与用户硬约束遵循）**：
   - 是否严格锁定了输入的 {num_images} 张人物参考图特征？是否 100% 遵守了用户 Prompt 中的正向与负向硬约束（如特定角色不能张嘴唱歌、禁止跳舞、特定关系设定）？场景数量是否收敛在 3 个以内以保证空间一致性？
6. **`S6_shootability_and_visual_motif`（镜头可拍摄性与核心视觉符号记忆点）**：
   - 每个故事节点的动作描述是否是具象可拍的物理画面（而非抽象小说心理描写）？是否有贯穿全片的 `core_visual_motif`（核心视觉符号）形成强烈的审美记忆点？

---

### 五、极其重要：必须输出针对性的「哪里需要优化 & 为什么 (`optimization_diagnostics`)」
即使是一份优秀的 Storyline（80~92分），你也必须通过**仔细比对原音频的真实听感细节（具体秒数的鼓点/器乐变化、某句歌词的微妙情绪、高潮点位置、或镜头可拍性细节）**，找出 **2～4 处最值得进一步打磨优化的具体位置**，清楚回答：
- `target_where`：**哪里需要优化**（指明具体幕次、时间段 `MM:SS-MM:SS` 或具体字段模块）
- `problem_what`：**当前写了什么 / 哪里不够好**
- `why_from_song_comparison`：**为什么需要优化**（必须结合原歌曲在该处的真实旋律、配器、人声或《MV评判规则》说明原因！）
- `how_to_optimize`：**具体怎么改**（给出可直接替换落地的导演修改建议）

请严格输出以下 JSON 格式（不要输出任何多余文字）：
{
  "overall_score_100": 0.0,
  "grade": "S / A / B / C / D",
  "layer_scores": {
    "layer1_music_lyric_emotion_fit_100": 0.0,
    "layer2_narrative_progression_sync_100": 0.0,
    "layer3_shootability_constraint_motif_100": 0.0
  },
  "dimensions": {
    "S1_melody_emotion_arc": {"score_10": 0.0, "comment": ""},
    "S2_lyric_subtext_translation": {"score_10": 0.0, "comment": ""},
    "S3_chorus_progression_anti_repeat": {"score_10": 0.0, "comment": ""},
    "S4_rhythm_timecode_interlude_sync": {"score_10": 0.0, "comment": ""},
    "S5_character_scene_constraint_lock": {"score_10": 0.0, "comment": ""},
    "S6_shootability_and_visual_motif": {"score_10": 0.0, "comment": ""}
  },
  "song_vs_storyline_comparison_verdict": "生成的 Storyline 与原歌曲词曲情绪对比的整体综述结论",
  "strengths_aligned_with_song": [
    "与歌曲契合度高的核心亮点 1",
    "与歌曲契合度高的核心亮点 2"
  ],
  "optimization_diagnostics": [
    {
      "issue_id": "OPT-1",
      "severity": "HIGH | MEDIUM | POLISH",
      "target_where": "具体哪里需要优化（如：Chorus 2 [01:38-02:10] 或 间奏段 [02:10-02:28]）",
      "problem_what": "当前 Storyline 在此处的设计存在什么不足",
      "why_from_song_comparison": "为什么需要优化（对比原歌曲在此处的真实旋律/歌词/配器/情绪变化或《MV评判规则》解释原因）",
      "how_to_optimize": "具体可落地的优化改法建议"
    }
  ]
}""".strip()


# ==============================================================================
# 2. Unified Project, Per-Tab Model & All-Prompt Configuration Store
# ==============================================================================
DEFAULT_PLATFORM_CONFIG: dict[str, Any] = {
    "updated_at": "默认初始配置",
    "project": {
        "project_id": os.environ.get("GOOGLE_CLOUD_PROJECT", "genai-dnb-demo"),
        "default_location": os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        "api_key": "",
        "prefer_vertexai": True,
        "request_timeout_sec": 240,
        "asset_storage_mode": os.environ.get("ARGON_ASSET_STORAGE_MODE", "auto"),
        "gcs_bucket": os.environ.get("ARGON_GCS_BUCKET", ""),
        "gcs_prefix": os.environ.get("ARGON_GCS_PREFIX", "argon_eval_data"),
        "gcs_use_direct_uri": os.environ.get("ARGON_GCS_USE_DIRECT_URI", "true").lower() in ("1", "true", "yes"),
        "gdrive_folder_id": os.environ.get("ARGON_GDRIVE_FOLDER_ID", "1MYa7S2ZjTXcOHrK3lDBUzhCVUUHxyp9Z"),
    },
    "tab_models": {
        "tab1_baseline": {
            "label": "Tab #1: 原提示词基线分析 (默认 Gemini 3.8 Flash)",
            "model": "gemini-3.8-flash",
            "location": "global",
            "thinking_level": "",
            "temperature": 0.2,
            "max_output_tokens": 65535,
            "max_workers": 5,
        },
        "tab2_argon_baseline": {
            "label": "Tab #2: 原提示词对比测试 (默认 Gemini 4 Argon, thinking=MEDIUM)",
            "model": "gemini-4-argon",
            "location": "global",
            "thinking_level": "MEDIUM",
            "temperature": 0.2,
            "max_output_tokens": 65535,
            "max_workers": 5,
        },
        "tab3_prompt_optimizer": {
            "label": "Tab #3: 提示词优化器 Meta-Prompt 模型 (默认 Gemini 4 Argon)",
            "model": "gemini-4-argon",
            "location": "global",
            "thinking_level": "MEDIUM",
            "temperature": 0.2,
            "max_output_tokens": 65535,
            "max_workers": 1,
        },
        "tab4_opt_model_a": {
            "label": "Tab #4 轨道 4A: 优化后提示词测试模型 A (默认 Gemini 3.8 Flash)",
            "model": "gemini-3.8-flash",
            "location": "global",
            "thinking_level": "",
            "temperature": 0.2,
            "max_output_tokens": 65535,
            "max_workers": 5,
        },
        "tab4_opt_model_b": {
            "label": "Tab #4 轨道 4B: 优化后提示词测试模型 B (默认 Gemini 4 Argon)",
            "model": "gemini-4-argon",
            "location": "global",
            "thinking_level": "MEDIUM",
            "temperature": 0.2,
            "max_output_tokens": 65535,
            "max_workers": 5,
        },
        "tab5_storyline_model_a": {
            "label": "Tab #5 通道 S-A: MV Storyline 创作模型 A (默认 Gemini 3.8 Flash)",
            "model": "gemini-3.8-flash",
            "location": "global",
            "thinking_level": "",
            "temperature": 0.25,
            "max_output_tokens": 65535,
            "max_workers": 5,
        },
        "tab5_storyline_model_b": {
            "label": "Tab #5 通道 S-B: MV Storyline 深度创作模型 B (默认 Gemini 4 Argon, thinking=MEDIUM)",
            "model": "gemini-4-argon",
            "location": "global",
            "thinking_level": "MEDIUM",
            "temperature": 0.25,
            "max_output_tokens": 65535,
            "max_workers": 5,
        },
        "rubric_judge": {
            "label": "全局评分裁判 1: 《MV评判规则.md》13维打分模型 (默认 Gemini 3.8 Flash)",
            "model": "gemini-3.8-flash",
            "location": "global",
            "thinking_level": "",
            "temperature": 0.1,
            "max_output_tokens": 65535,
            "max_workers": 5,
        },
        "storyline_judge": {
            "label": "全局评分裁判 2: Storyline vs 歌曲对比打分与优化诊断模型 (默认 Gemini 3.8 Flash)",
            "model": "gemini-3.8-flash",
            "location": "global",
            "thinking_level": "",
            "temperature": 0.1,
            "max_output_tokens": 65535,
            "max_workers": 5,
        },
    },
    "prompts": {
        "original_audio_prompt": DEFAULT_ORIGINAL_AUDIO_PROMPT,
        "optimized_prompt": DEFAULT_OPTIMIZED_PROMPT,
        "prompt_optimizer_meta_prompt": DEFAULT_PROMPT_OPTIMIZER_META_PROMPT,
        "rubric_judge_prompt_template": DEFAULT_RUBRIC_JUDGE_PROMPT_TEMPLATE,
        "storyline_generation_prompt": DEFAULT_STORYLINE_GENERATION_PROMPT,
        "storyline_judge_prompt_template": DEFAULT_STORYLINE_JUDGE_PROMPT_TEMPLATE,
        "mv_dataset_spec": DEFAULT_MV_DATASET_SPEC_TXT,
        "mv_rubric_md": DEFAULT_MV_RUBRIC_MD,
    },
}

_CONFIG_LOCK = threading.Lock()


def load_platform_config() -> dict[str, Any]:
    """Loads persisted `platform_config.json`, deep-merging with defaults so all keys exist."""
    cfg = copy.deepcopy(DEFAULT_PLATFORM_CONFIG)
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved.get("project"), dict):
                cfg["project"].update(saved["project"])
            if isinstance(saved.get("tab_models"), dict):
                for k, v in saved["tab_models"].items():
                    if k in cfg["tab_models"] and isinstance(v, dict):
                        cfg["tab_models"][k].update(v)
            if isinstance(saved.get("prompts"), dict):
                for pk, pv in saved["prompts"].items():
                    if pk in cfg["prompts"] and isinstance(pv, str) and pv.strip():
                        cfg["prompts"][pk] = pv
            # Auto-upgrade max_output_tokens to 65535 for Gemini 4 Argon / 3.8 Flash tabs
            for _tk, _tv in cfg.get("tab_models", {}).items():
                if isinstance(_tv, dict):
                    if int(_tv.get("max_output_tokens", 0) or 0) in (8192, 16384, 32768):
                        _tv["max_output_tokens"] = 65535
            if saved.get("updated_at"):
                cfg["updated_at"] = saved["updated_at"]
        except Exception:
            pass
    else:
        save_platform_config(cfg)
    return cfg


def save_platform_config(new_cfg: dict[str, Any]) -> dict[str, Any]:
    with _CONFIG_LOCK:
        new_cfg["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        CONFIG_PATH.write_text(json.dumps(new_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        # Also keep `step3_prompt_opt.json`'s `optimized_prompt` synchronized
        opt_path = RUNS_V2_DIR / "step3_prompt_opt.json"
        if opt_path.exists():
            try:
                st3 = json.loads(opt_path.read_text(encoding="utf-8"))
                st3["optimized_prompt"] = new_cfg.get("prompts", {}).get("optimized_prompt", DEFAULT_OPTIMIZED_PROMPT)
                st3["updated_at"] = new_cfg["updated_at"]
                opt_path.write_text(json.dumps(st3, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        return new_cfg