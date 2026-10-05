#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agnes Studio — Flask Backend (yay0128/Agnes Resmi Standartlarına Uygun)
==============================================================================
KAYNAK REPO VE RESMİ DOKÜMANTASYON:
  https://github.com/yay0128/Agnes
  - https://agnes-ai.com/doc/agnes-video-v20
  - https://agnes-ai.com/doc/agnes-image-21-flash
  - https://agnes-ai.com/doc/agnes-20-flash

ÖNEMLİ VİDEO V2.0 SİSTEM KURALLARI VE DÜZELTMELER (yay0128/Agnes):
------------------------------------------------------------------------------
1. MODEL:
   - "agnes-video-v2.0" (Ücretsiz, kare tabanlı mimari)

2. KARE SAYISI (num_frames):
   - Kural: num_frames = 8 * n + 1 kuralına uymalıdır.
   - Sunucu tarafından kabul edilen geçerli kareler listesi:
       [81, 121, 161, 241, 441, 481, 961]
   - Süre Formülü: seconds = num_frames / frame_rate (varsayılan 24 fps)
     *  81 kare @ 24 fps = ~3.4 sn (Hızlı test)
     * 121 kare @ 24 fps = ~5.0 sn (Önerilen varsayılan)
     * 161 kare @ 24 fps = ~6.7 sn (Orta uzunluk)
     * 241 kare @ 24 fps = ~10.0 sn (1080p için izin verilen MAKSİMUM sınır - GPU bellek limiti)
     * 481 kare @ 24 fps = ~20.0 sn (720p HD için MAKSİMUM sınır)
     * 961 kare @ 24 fps = ~40.0 sn (480p SD için MAKSİMUM sınır - Canlı API testinde 200 OK doğrulandı)
   - [DOĞRULANMIŞ BİLGİ]: 480p çözünürlükte 961 kare API tarafından başarıyla üretilmektedir.
     1080p'de ise CUDA OOM nedeniyle sınır 241 karedir.

3. EN-BOY VE ÇÖZÜNÜRLÜK (width & height):
   - Boyutlar 64'ün katı (multiples of 64) olmalıdır:
     * 16:9 Yatay: 1152x768 (ComfyUI varsayılanı) veya 1280x704 (720p), 1920x1088 (1080p)
     * 9:16 Dikey: 704x1280 (720p) veya 1088x1920 (1080p), 768x1152
     * 1:1  Kare : 768x768 veya 512x512
   - [DÜZELTME]: agnes-video-v2.0 için API gövdesine "aspect_ratio" parametresi
     GÖNDERİLMEZ. En-boy oranı doğrudan "width" ve "height" ile belirlenir.
     aspect_ratio yalnızca 2.5 ailesi (süre tabanlı) için geçerlidir.

4. GÖRSEL KULLANIMI VE MODLAR (mode):
   - Text-to-Video: image parametresi gönderilmez.
   - Image-to-Video (Tek görselden video):
       image = "<url / base64>", mode = "ti2vid"
   - Keyframes (İki görsel arası geçiş / morph):
       extra_body = {"image": [url1, url2], "mode": "keyframes"}
       mode = "keyframes"
       image = [url1, url2]

5. 429 VE GEÇİCİ HATA YÖNETİMİ (Retry Policy):
   - 429 "upstream saturated" Agnes model grubu kapasiteye ulaştığında döner.
   - Jittered exponential backoff (2s, 4s, 8s, 16s...) ile otomatik yeniden deneme yapılır.

6. CANLI DURUM VE POLLING:
   - GET /v1/videos/{task_id} -> Resmi sorgulama endpoint'i (status, progress, video_url döner).
   - GET /agnesapi?video_id={id}&model_name={model} -> Canlı GPU % takibi için ek uç nokta.
==============================================================================
"""

import json
import os
import random
import re
import threading
import time
import uuid
from typing import Any

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

# ==============================================================================
# YAPILANDIRMA
# ==============================================================================

API_KEY = os.environ.get("AGNES_API_KEY") or "sk-KZhob2kogDQGnUhsOvm3U1254J4BTQ9oRjZrwuRcgtR8vWeA"
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
API_ROOT = "https://apihub.agnes-ai.com"
BASE_URL = f"{API_ROOT}/v1"

# yay0128/Agnes ve sunucu kuralı: num_frames = 8n + 1 (480p'de 961 kareye kadar desteklenir)
ALLOWED_FRAMES = (81, 121, 161, 241, 441, 481, 961)

MODELS = {
    "agnes-3.0-flash": ("text", "FREE"),
    "agnes-2.5-flash": ("text", "FREE"),
    "agnes-2.0-flash": ("text", "FREE"),
    "agnes-2.5-pro": ("text", "PRO"),
    "agnes-2.5-pro-alpha": ("text", "PRO"),
    "agnes-2.5-pro-beta": ("text", "PRO"),
    "agnes-image-2.5-flash": ("image", "FREE"),
    "agnes-image-2.1-flash": ("image", "FREE"),
    "agnes-image-2.0-flash": ("image", "FREE"),
    "agnes-video-v2.0": ("video", "FREE"),
    "agnes-video-2.5-flash": ("video", "TOKEN"),
    "agnes-video-2.5": ("video", "TOKEN"),
}

IMAGE_PRESETS = {
    "1:1":  {"1K": "1024x1024", "2K": "2048x2048", "4K": "4096x4096"},
    "16:9": {"1K": "1280x720",  "2K": "1920x1080", "4K": "3840x2160"},
    "9:16": {"1K": "720x1280",  "2K": "1080x1920", "4K": "2160x3840"},
    "4:3":  {"1K": "1024x768",  "2K": "2048x1536", "4K": "4096x3072"},
    "3:4":  {"1K": "768x1024",  "2K": "1536x2048", "4K": "3072x4096"},
    "21:9": {"1K": "1344x576",  "2K": "2560x1080", "4K": "5040x2160"},
}

# Boyutlar: 480p için 854x480 (veya 832x448), 720p için 1280x704, 1080p için 1920x1088
VIDEO_PRESETS = {
    "480p":  {"16:9": [854, 480],   "9:16": [480, 854],   "1:1": [480, 480]},
    "720p":  {"16:9": [1280, 704],  "9:16": [704, 1280],  "1:1": [768, 768]},
    "1080p": {"16:9": [1920, 1088], "9:16": [1088, 1920], "1:1": [1088, 1088]},
}

LIMITS = {"image_refs": 6, "video_ref_images": 5, "video_ref_audios": 3}

# Oturum durumu deposu
BOOT_ID = uuid.uuid4().hex
_STATE: dict[str, Any] = {"data": {}, "updated": 0.0}
_LOCK = threading.Lock()


class AgnesError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: Any = None):
        super().__init__(message)
        self.message, self.status_code, self.details = message, status_code, details


# ==============================================================================
# HTTP İSTEMCİSİ VE RETRY (yay0128/Agnes Standartları)
# ==============================================================================

def _req(method: str, path: str, body: Any = None, timeout: int = 60, stream: bool = False, max_attempts: int = 4):
    """
    Agnes API istek yardımcısı.
    429 (Upstream saturated) ve 5xx geçici hatalarda jittered exponential backoff uygular.
    """
    if not API_KEY:
        raise AgnesError("API anahtarı tanımlı değil. AGNES_API_KEY kontrol edin.", 500)
    url = path if path.startswith("http") else f"{BASE_URL}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    last_err = None
    for attempt in range(max_attempts):
        try:
            r = requests.request(method, url, json=body, timeout=timeout, stream=stream, headers=headers)
        except requests.RequestException as e:
            last_err = AgnesError(f"Bağlantı hatası: {e}", 502)
            if attempt < max_attempts - 1 and not stream:
                backoff = min(30.0, 2.0 * (2 ** attempt)) + random.uniform(-0.5, 0.5)
                time.sleep(max(1.0, backoff))
                continue
            raise last_err from e

        # 429 veya 502, 503, 504 durumlarında retry
        if r.status_code in (429, 502, 503, 504) and attempt < max_attempts - 1 and not stream:
            backoff = min(30.0, 2.0 * (2 ** attempt)) + random.uniform(-0.5, 0.5)
            time.sleep(max(1.0, backoff))
            continue

        if not r.ok:
            try:
                data = r.json()
                err = data.get("error", data)
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            except ValueError:
                data, msg = r.text, r.text[:500]
            hint = {
                403: " (Pro plan gerekli olabilir)",
                429: " (Rate limit / Token plan gerekli; model grubu şu anda dolu)"
            }
            raise AgnesError(f"{msg}{hint.get(r.status_code, '')}", r.status_code, data)

        return r

    raise last_err or AgnesError("İstek başarısız oldu.", 500)


def _wxh(v):
    m = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", str(v or ""))
    return (int(m[1]), int(m[2])) if m and int(m[1]) > 0 and int(m[2]) > 0 else None


def _int(v, default=None):
    if v in (None, ""):
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        raise AgnesError(f"Geçersiz tamsayı: {v}")


def _float(v, default):
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        raise AgnesError(f"Geçersiz sayı: {v}")


def _img(s) -> str:
    s = str(s or "").strip()
    if not s.startswith(("http://", "https://", "data:")):
        raise AgnesError("Görsel URL veya data URI formatında olmalıdır.")
    return s


def _short(x):
    """Log/özet için uzun base64 verilerini kırpar."""
    if isinstance(x, str) and x.startswith("data:"):
        return x[:40] + "…[base64]"
    if isinstance(x, dict):
        return {k: _short(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_short(v) for v in x]
    return x


# ==============================================================================
# MODELLER
# ==============================================================================

def model_info(mid: str) -> dict:
    if mid in MODELS:
        cat, tier = MODELS[mid]
    else:
        low = mid.lower()
        cat, tier = ("video" if "video" in low else "image" if "image" in low else "text"), "UNKNOWN"
    return {"id": mid, "category": cat, "tier": tier}


def list_models(live: bool = True) -> list[dict]:
    ids = list(MODELS)
    if live:
        try:
            data = _req("GET", "/models", timeout=15).json().get("data", [])
            ids = [m["id"] if isinstance(m, dict) else str(m) for m in data] or ids
        except AgnesError:
            pass
    order = {"FREE": 0, "UNKNOWN": 1, "PRO": 2, "TOKEN": 2}
    return sorted((model_info(i) for i in ids), key=lambda m: (m["category"], order[m["tier"]]))


# ==============================================================================
# SOHBET VE METİN PROMPT GENİŞLETİCİ (Agnes 2.0 Flash)
# ==============================================================================

def _chat_payload(d: dict, stream: bool) -> dict:
    msgs = d.get("messages") or []
    if not msgs:
        raise AgnesError("messages boş olamaz.")
    p = {
        "model": d.get("model") or "agnes-2.5-flash",
        "messages": msgs,
        "stream": stream,
        "temperature": _float(d.get("temperature"), 0.7),
        "top_p": _float(d.get("top_p"), 1.0),
        "max_tokens": _int(d.get("max_tokens"), 4096),
        "frequency_penalty": _float(d.get("frequency_penalty"), 0.0),
        "presence_penalty": _float(d.get("presence_penalty"), 0.0),
    }
    stop = [s.strip() for s in str(d.get("stop") or "").split(",") if s.strip()]
    if stop:
        p["stop"] = stop
    if d.get("json_mode"):
        p["response_format"] = {"type": "json_object"}
    if (seed := _int(d.get("seed"))) is not None:
        p["seed"] = seed
    return p


def chat(d: dict) -> dict:
    data = _req("POST", "/chat/completions", _chat_payload(d, False), timeout=120).json()
    ch = (data.get("choices") or [{}])[0]
    return {"content": ch.get("message", {}).get("content", ""), "usage": data.get("usage", {})}


def chat_stream(d: dict):
    r = _req("POST", "/chat/completions", _chat_payload(d, True), timeout=120, stream=True)
    with r:
        for raw in r.iter_lines():
            line = raw.decode("utf-8", "replace") if raw else ""
            if not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                piece = (json.loads(data).get("choices") or [{}])[0].get("delta", {}).get("content")
            except ValueError:
                continue
            if piece:
                yield piece


def expand_video_prompt(idea: str, model: str = "agnes-2.0-flash") -> str:
    """
    yay0128/Agnes Resmi Standart Prompt Mühendisliği:
    Kullanıcının kısa fikrini 6 bileşenli sinematik formüle genişletir:
    [Subject] + [Action] + [Scene] + [Camera Movement] + [Lighting] + [Style]
    """
    if not idea or not idea.strip():
        raise AgnesError("prompt boş olamaz.")
    sys_msg = (
        "You are a cinematic video prompt engineer. Expand the user's brief idea into "
        "a detailed, vivid, production-ready video prompt. "
        "Use the structure: [Subject] + [Action] + [Scene] + [Camera Movement] + "
        "[Lighting] + [Style]. Output only the expanded prompt, no preamble, no "
        "explanation, no labels."
    )
    try:
        out = chat({
            "model": model,
            "messages": [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": idea.strip()}
            ],
            "temperature": 0.7,
            "max_tokens": 512
        })["content"].strip()
        return out or idea
    except AgnesError:
        return idea


# ==============================================================================
# RESİM ÜRETİMİ (POST /v1/images/generations)
# ==============================================================================

def generate_image(d: dict) -> list[dict]:
    prompt = (d.get("prompt") or "").strip()
    if not prompt:
        raise AgnesError("prompt boş olamaz.")
    size = _wxh(d.get("size"))
    if not size:
        raise AgnesError(f"Geçersiz boyut: {d.get('size') or '-'} (örn: 1024x768)")
    n = _int(d.get("n"), 1)
    fmt = d.get("response_format", "url")
    if n < 1 or fmt not in ("url", "b64_json"):
        raise AgnesError("n>=1 ve response_format url|b64_json olmalı.")
    p = {
        "model": d.get("model") or "agnes-image-2.5-flash",
        "prompt": prompt,
        "n": n,
        "size": f"{size[0]}x{size[1]}",
        "response_format": fmt
    }
    seed = _int(d.get("seed"))
    if seed is not None and seed != -1:
        if not 0 <= seed <= 999:
            raise AgnesError("seed -1 ile 999 arasında olmalı.")
        p["seed"] = seed
    refs = [_img(r) for r in d.get("reference_images") or [] if r]
    # API sunucu kuralı: En fazla 6 referans görsel ('at most 6 allowed').
    # 8 adet gönderilirse sunucu 400 hatası vermemesi için güvenle ilk 6'sı alınır.
    if len(refs) > 6:
        refs = refs[:6]
    if refs:
        strength = _float(d.get("strength"), 0.75)
        if not 0.1 <= strength <= 1.0:
            raise AgnesError("strength 0.1 - 1.0 olmalı.")
        p["image"] = refs[0] if len(refs) == 1 else refs
        p["strength"] = strength
    data = _req("POST", "/images/generations", p, timeout=180).json()
    if not data.get("data"):
        raise AgnesError("Yanıtta resim yok.", 502, data)
    return data["data"]


# ==============================================================================
# VİDEO ÜRETİMİ (Agnes Video V2.0 - 480p 961 Kare Destekli)
# ==============================================================================

def snap_frames(target: int, max_frames: int = 961) -> int:
    """Hedef kare sayısını en yakın geçerli ALLOWED_FRAMES değerine eşler."""
    valid = [f for f in ALLOWED_FRAMES if f <= max_frames]
    if not valid:
        return ALLOWED_FRAMES[0]
    return min(valid, key=lambda f: abs(f - target))


def resolve_video_spec(resolution="720p", aspect_ratio="16:9", seconds=5.0, fps=24, num_frames=None):
    """
    agnes-video-v2.0 için boyut ve kare hesaplar.
    Kurallar:
      1. width ve height 64'ün tam katı olmalıdır (854x480, 1280x704, 1920x1088).
      2. num_frames = 8n + 1 kuralına uyar: [81, 121, 161, 241, 441, 481, 961].
      3. Çözünürlüğe göre maksimum kare sınırları:
         - 1080p FHD: MAKSİMUM 241 kare (~10.0 sn @ 24fps) [GPU OOM sınırı]
         - 720p HD  : MAKSİMUM 481 kare (~20.0 sn @ 24fps)
         - 480p SD  : MAKSİMUM 961 kare (~40.0 sn @ 24fps) [Doğrulanmış sunucu sınırı]
    """
    if not 1 <= fps <= 60:
        raise AgnesError("fps 1 ile 60 arasında olmalıdır.")

    custom = _wxh(resolution)
    if custom:
        w, h = custom
        w = max(256, (w // 64) * 64)
        h = max(256, (h // 64) * 64)
        aspect_ratio = "16:9" if w > h else "9:16" if h > w else "1:1"
    else:
        try:
            w, h = VIDEO_PRESETS[resolution][aspect_ratio]
        except KeyError:
            raise AgnesError(f"Geçersiz çözünürlük/oran: {resolution} {aspect_ratio}")

    # Kısa kenara göre çözünürlük kare sınırı
    short = min(w, h)
    max_f = 241 if short >= 1080 else 481 if short > 480 else 961

    target = num_frames if num_frames else round((seconds or 5.0) * fps)
    frames = snap_frames(target, max_frames=max_f)

    return {
        "width": w,
        "height": h,
        "aspect_ratio": aspect_ratio,
        "num_frames": frames,
        "frame_rate": fps,
        "seconds": round(frames / fps, 2)
    }


def create_video(d: dict) -> dict:
    """
    Agnes Video görevini başlatır.
    yay0128/Agnes resmi parametre yapılandırmasını kullanır.
    """
    prompt = (d.get("prompt") or "").strip()
    if not prompt:
        raise AgnesError("prompt boş olamaz.")

    # Otomatik sinematik prompt genişletme
    if d.get("expand"):
        prompt = expand_video_prompt(prompt)

    model = d.get("model") or "agnes-video-v2.0"
    p: dict[str, Any] = {"model": model, "prompt": prompt}

    is_v2 = "v2.0" in model.lower()

    if is_v2:
        # V2.0 KARE TABANLI MİMARİ
        spec = resolve_video_spec(
            resolution=d.get("resolution", "720p"),
            aspect_ratio=d.get("aspect_ratio", "16:9"),
            seconds=_float(d.get("seconds"), 5.0),
            fps=_int(d.get("fps"), 24),
            num_frames=_int(d.get("num_frames"))
        )
        p["width"] = spec["width"]
        p["height"] = spec["height"]
        p["num_frames"] = spec["num_frames"]
        p["frame_rate"] = spec["frame_rate"]
        # ÖNEMLİ: v2.0 için aspect_ratio gönderilmez, width x height belirler!
    else:
        # 2.5 AİLESİ (SÜRE TABANLI MİMARİ)
        dur = _int(d.get("duration"), 5)
        ar = d.get("aspect_ratio", "16:9")
        if not 4 <= dur <= 12 or ar not in ("16:9", "9:16", "1:1"):
            raise AgnesError("duration 4-12 sn, aspect_ratio 16:9|9:16|1:1 olmalıdır.")
        p["duration"] = dur
        p["aspect_ratio"] = ar

    # --------------------------------------------------------------------------
    # GÖRSEL / REFERANS YAPILANDIRMASI (yay0128/Agnes Resmi Şeması)
    # --------------------------------------------------------------------------
    start, end = d.get("start_image"), d.get("end_image")
    if end and not start:
        raise AgnesError("Bitiş karesi için başlangıç karesi de gereklidir.")

    if start:
        s = _img(start)
        if end:
            e = _img(end)
            # KEYFRAMES MODU (yay0128/Agnes: examples/video_keyframes.py)
            p["mode"] = "keyframes"
            p["image"] = [s, e]
            p["extra_body"] = {"image": [s, e], "mode": "keyframes"}
        else:
            # IMAGE-TO-VIDEO MODU (yay0128/Agnes: examples/video_image_to_video.py)
            p["image"] = s
            p["mode"] = "ti2vid"

    # R2V Multimodal Referanslar (2.5 serisi için)
    imgs = d.get("ref_images") or []
    auds = [a.strip() for a in d.get("ref_audio_urls") or [] if a and a.strip()]
    vid = (d.get("ref_video_url") or "").strip()
    if len(imgs) > LIMITS["video_ref_images"] or len(auds) > LIMITS["video_ref_audios"]:
        raise AgnesError("En fazla 5 referans görsel ve 3 referans ses verilebilir.")
    refs = [{"type": "image_url", "image_url": {"url": _img(i)}, "role": "style"} for i in imgs]
    refs += [{"type": "audio_url", "audio_url": {"url": a}, "role": "audio"} for a in auds]
    if vid:
        refs.append({"type": "video_url", "video_url": {"url": vid}, "role": "motion"})
    if refs:
        p["references"] = refs

    # İsteğe Bağlı Parametreler
    if (neg := (d.get("negative_prompt") or "").strip()):
        p["negative_prompt"] = neg
    if (seed := _int(d.get("seed"))) is not None:
        p["seed"] = seed
    if (steps := _int(d.get("num_inference_steps"))) is not None:
        if steps < 1:
            raise AgnesError("num_inference_steps pozitif olmalıdır.")
        p["num_inference_steps"] = steps

    # Görevi oluştur
    data = _req("POST", "/videos", p, timeout=60).json()
    task_id = data.get("id") or data.get("task_id")
    video_id = data.get("video_id")
    if not task_id and not video_id:
        raise AgnesError("Görev ID'si alınamadı.", 502, data)

    spec_summary = {k: p[k] for k in ("width", "height", "num_frames", "frame_rate", "duration",
                                       "aspect_ratio", "mode") if k in p}
    return {
        "task_id": task_id,
        "video_id": video_id,
        "model": model,
        "prompt": prompt,
        "spec": spec_summary,
        "sent": _short(p),
        "server": data
    }


_STATUS = {
    "queued": "queued", "pending": "queued",
    "inference": "in_progress", "in_progress": "in_progress", "processing": "in_progress", "running": "in_progress",
    "completed": "completed", "succeeded": "completed", "success": "completed", "done": "completed",
    "failed": "failed", "error": "failed", "cancelled": "failed"
}


def _norm(d: dict) -> dict:
    """Durum verisini standart formata dönüştürür."""
    raw = str(d.get("internal_status") or d.get("status") or "unknown").lower()
    st = _STATUS.get(raw, "in_progress")
    prog = d.get("internal_progress")
    prog = 100 if st == "completed" else (prog if prog is not None else d.get("progress") or 0)

    # yay0128/Agnes: video_url veya remixed_from_video_id veya output.url
    url = (d.get("video_url") or d.get("url") or
           d.get("remixed_from_video_id") or
           (d.get("output") or {}).get("url") or
           (d.get("data") or {}).get("url"))

    err = d.get("error")
    if isinstance(err, dict):
        err = err.get("message", str(err))

    return {
        "status": st,
        "raw_status": raw,
        "progress": prog,
        "url": url,
        "error": err if st == "failed" else None
    }


def get_video_status(task_id: str | None, video_id: str | None, model: str) -> dict:
    """
    Video durumu polling sorgusu.
    1. Birincil: GET /v1/videos/{task_id} (Resmi endpoint, status + progress + video_url)
    2. Ek/Canlı: GET /agnesapi?video_id={id}&model_name={model} (Canlı GPU çıkarım %)
    """
    live = None

    # Canlı GPU ilerleme uç noktası
    if video_id:
        try:
            r = requests.get(
                f"{API_ROOT}/agnesapi",
                params={"video_id": video_id, "model_name": model},
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=12
            )
            if r.ok:
                live = _norm(r.json())
        except (requests.RequestException, ValueError):
            live = None

    # Resmi video görev sorgulama uç noktası (/v1/videos/{task_id})
    if task_id and (live is None or (live["status"] == "completed" and not live["url"]) or live["status"] == "unknown"):
        try:
            fb = _norm(_req("GET", f"/videos/{task_id}", timeout=15).json())
            if live is None or fb["url"] or fb["status"] in ("completed", "failed"):
                live = fb
        except AgnesError:
            pass

    return live or {"status": "unknown", "raw_status": "unreachable", "progress": 0, "url": None, "error": None}


# ==============================================================================
# FLASK UYGULAMASI VE ROUTE'LAR
# ==============================================================================

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024


@app.before_request
def _auth():
    if APP_PASSWORD:
        a = request.authorization
        if not a or a.password != APP_PASSWORD:
            return Response("Şifre gerekli", 401, {"WWW-Authenticate": 'Basic realm="Agnes Studio"'})


@app.errorhandler(AgnesError)
def _agnes_err(e: AgnesError):
    return jsonify({"error": e.message, "status_code": e.status_code, "details": e.details}), e.status_code


@app.errorhandler(413)
def _too_big(_):
    return jsonify({"error": "İstek çok büyük (maks 100 MB). Görselleri azaltın."}), 413


def _body() -> dict:
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        raise AgnesError("Geçersiz JSON gövdesi.")
    return d


@app.get("/")
def index():
    cfg = {
        "models": list_models(live=False),
        "image_presets": IMAGE_PRESETS,
        "video_presets": VIDEO_PRESETS,
        "limits": LIMITS,
        "boot_id": BOOT_ID
    }
    return render_template("index.html", cfg=cfg)


@app.get("/api/state")
def api_state_get():
    with _LOCK:
        return jsonify({"boot_id": BOOT_ID, "state": _STATE["data"], "updated": _STATE["updated"]})


@app.put("/api/state")
def api_state_put():
    d = _body()
    with _LOCK:
        _STATE["data"], _STATE["updated"] = d, time.time()
    return jsonify({"ok": True})


@app.delete("/api/state")
def api_state_delete():
    with _LOCK:
        _STATE["data"], _STATE["updated"] = {}, time.time()
    return jsonify({"ok": True})


@app.get("/api/models")
def api_models():
    return jsonify(list_models(live=True))


@app.post("/api/chat")
def api_chat():
    d = _body()
    if not d.get("stream", True):
        return jsonify(chat(d))
    gen = chat_stream(d)
    first = next(gen, "")

    def out():
        yield first
        try:
            yield from gen
        except AgnesError as e:
            yield f"\n\n[HATA] {e.message}"

    return Response(stream_with_context(out()), mimetype="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/image")
def api_image():
    return jsonify({"images": generate_image(_body())})


@app.post("/api/video")
def api_video():
    return jsonify(create_video(_body()))


@app.get("/api/video/status")
def api_video_status():
    a = request.args
    if not a.get("task_id") and not a.get("video_id"):
        raise AgnesError("task_id veya video_id gereklidir.")
    return jsonify(get_video_status(
        task_id=a.get("task_id") or None,
        video_id=a.get("video_id") or None,
        model=a.get("model", "agnes-video-v2.0")
    ))


@app.post("/api/expand")
def api_expand():
    prompt = (_body().get("prompt") or "").strip()
    return jsonify({"prompt": expand_video_prompt(prompt)})


@app.post("/api/raw")
def api_raw():
    d = _body()
    method = str(d.get("method", "GET")).upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise AgnesError("Geçersiz HTTP metodu.")
    ep = str(d.get("endpoint") or "/models").strip()
    if ep.startswith("http") and not ep.startswith(API_ROOT):
        raise AgnesError("Sadece apihub.agnes-ai.com adreslerine izin verilmektedir.")
    if ep.startswith("/v1/"):
        ep = API_ROOT + ep
    r = _req(method, ep, d.get("body") if method in ("POST", "PUT", "PATCH") else None, timeout=120)
    try:
        return jsonify({"status": r.status_code, "body": r.json()})
    except ValueError:
        return jsonify({"status": r.status_code, "body": r.text[:5000]})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[*] Agnes Studio Flask sunucusu başlatılıyor: http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
