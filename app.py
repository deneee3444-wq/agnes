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
    "1:1":  {"1K": "1024x1024", "2K": "2048x2048", "3K": "3072x3072", "4K": "4096x4096"},
    "16:9": {"1K": "1280x720",  "2K": "1920x1080", "3K": "2560x1440", "4K": "3840x2160"},
    "9:16": {"1K": "720x1280",  "2K": "1080x1920", "3K": "1440x2560", "4K": "2160x3840"},
    "4:3":  {"1K": "1024x768",  "2K": "2048x1536", "3K": "3072x2304", "4K": "4096x3072"},
    "3:4":  {"1K": "768x1024",  "2K": "1536x2048", "3K": "2304x3072", "4K": "3072x4096"},
    "3:2":  {"1K": "1080x720",  "2K": "2160x1440", "3K": "3240x2160", "4K": "4320x2880"},
    "2:3":  {"1K": "720x1080",  "2K": "1440x2160", "3K": "2160x3240", "4K": "2880x4320"},
    "21:9": {"1K": "1344x576",  "2K": "2560x1080", "3K": "3440x1440", "4K": "5040x2160"},
}

# Boyutlar: 64'ün tam katı olan standart tensör hizalı çözünürlükler
VIDEO_PRESETS = {
    "480p":  {"16:9": [832, 448],   "9:16": [448, 832],   "1:1": [512, 512]},
    "720p":  {"16:9": [1280, 704],  "9:16": [704, 1280],  "1:1": [768, 768]},
    "1080p": {"16:9": [1920, 1088], "9:16": [1088, 1920], "1:1": [1088, 1088]},
}

LIMITS = {"image_refs": 6, "video_ref_images": 5, "video_ref_audios": 3}

# Oturum durumu deposu (disk dosyası + bellek senkronizasyonu)
BOOT_ID = uuid.uuid4().hex
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


def _load_state_file() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            pass
    return {}


def _save_state_file(data: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_INIT_DATA = _load_state_file().get("data", {})
_STATE: dict[str, Any] = {"data": _INIT_DATA, "updated": time.time()}
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

MODEL_NAMES = {
    # Video Modelleri
    "agnes-video-v2.0": "Agnes Video v2.0",
    "agnes-video-2.5-flash": "Agnes Video 2.5 Flash",
    "agnes-video-2.5": "Agnes Video 2.5 Pro",
    # Resim Modelleri
    "agnes-image-2.5-flash": "Agnes Image 2.5 Flash",
    "agnes-image-2.1-flash": "Agnes Image 2.1 Flash",
    "agnes-image-2.0-flash": "Agnes Image 2.0 Flash",
    # Metin / Sohbet Modelleri
    "agnes-3.0-flash": "Agnes 3.0 Flash",
    "agnes-2.5-flash": "Agnes 2.5 Flash",
    "agnes-2.0-flash": "Agnes 2.0 Flash",
    "agnes-2.5-pro": "Agnes 2.5 Pro",
    "agnes-2.5-pro-alpha": "Agnes 2.5 Pro Alpha",
    "agnes-2.5-pro-beta": "Agnes 2.5 Pro Beta",
}


def model_info(mid: str) -> dict:
    if mid in MODELS:
        cat, tier = MODELS[mid]
    else:
        low = mid.lower()
        cat, tier = ("video" if "video" in low else "image" if "image" in low else "text"), "UNKNOWN"
    return {"id": mid, "label": MODEL_NAMES.get(mid, mid), "category": cat, "tier": tier}


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

def _chat_payload(d: dict, stream: bool, fallback_json: bool = False) -> dict:
    msgs = list(d.get("messages") or [])
    if not msgs:
        raise AgnesError("messages boş olamaz.")

    json_mode = bool(d.get("json_mode"))
    if json_mode:
        # OpenAI / Agnes API Kuralı: response_format 'json_object' istendiğinde
        # messages içinde MUTLAKA 'JSON' / 'json' kelimesi geçmelidir.
        has_json_word = any(
            "json" in str(m.get("content", "")).lower()
            for m in msgs
        )
        if not has_json_word:
            # Sistem mesajı varsa genişlet, yoksa ekle
            sys_idx = next((i for i, m in enumerate(msgs) if m.get("role") == "system"), None)
            json_instruction = " You must respond strictly in valid JSON format. Always output a valid JSON object."
            if sys_idx is not None:
                orig = str(msgs[sys_idx].get("content", ""))
                msgs[sys_idx] = {**msgs[sys_idx], "content": orig + json_instruction}
            else:
                msgs.insert(0, {"role": "system", "content": "You are a helpful AI assistant." + json_instruction})

    p: dict[str, Any] = {
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

    # Sadece fallback modunda değilse response_format parametresini gönder
    if json_mode and not fallback_json:
        p["response_format"] = {"type": "json_object"}

    if (seed := _int(d.get("seed"))) is not None:
        p["seed"] = seed
    return p


def chat(d: dict) -> dict:
    try:
        data = _req("POST", "/chat/completions", _chat_payload(d, False, fallback_json=False), timeout=120).json()
    except AgnesError as e:
        # Eğer model 400 INVALID_ARGUMENT (response_format desteklemiyor) hatası verirse
        if e.status_code == 400 and d.get("json_mode"):
            data = _req("POST", "/chat/completions", _chat_payload(d, False, fallback_json=True), timeout=120).json()
        else:
            raise

    ch = (data.get("choices") or [{}])[0]
    content = ch.get("message", {}).get("content", "")
    if content:
        content = re.sub(r"^\s*[\r\n]+", "", content).rstrip()
    return {"content": content, "usage": data.get("usage", {})}


def chat_stream(d: dict):
    payload = _chat_payload(d, True, fallback_json=False)
    try:
        r = _req("POST", "/chat/completions", payload, timeout=120, stream=True)
    except AgnesError as e:
        # Eğer model 400 INVALID_ARGUMENT dönerse ve JSON mode açıksa fallback ile tekrar dene
        if e.status_code == 400 and d.get("json_mode"):
            payload = _chat_payload(d, True, fallback_json=True)
            r = _req("POST", "/chat/completions", payload, timeout=120, stream=True)
        else:
            raise

    first_content = True
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
                if first_content:
                    piece = re.sub(r"^\s*[\r\n]+", "", piece)
                    if piece:
                        first_content = False
                        yield piece
                else:
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
# RESİM ÜRETİMİ (POST /v1/images/generations - Resmi Agnes Image Dokümanlarına Uygun)
# https://wiki.agnes-ai.com/en/docs/agnes-image-21-flash
# ==============================================================================

def generate_image(d: dict) -> list[dict]:
    prompt = (d.get("prompt") or "").strip()
    if not prompt:
        raise AgnesError("prompt boş olamaz.")

    model = d.get("model") or "agnes-image-2.5-flash"
    n = _int(d.get("n"), 1)
    fmt = d.get("response_format", "url")
    if fmt not in ("url", "b64_json"):
        fmt = "url"

    # Boyut ve Oran (Resmi Doküman: size "1K".."4K" ve ratio "16:9", "1:1", vb.)
    size_in = str(d.get("size") or "2K").strip()
    ratio_in = str(d.get("ratio") or d.get("aspect_ratio") or "16:9").strip()

    # Eğer tam çözünürlük gelirse (örn 1920x1080) veya tier ("2K")
    p: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size_in if size_in in ("1K", "2K", "3K", "4K") else (f"{_wxh(size_in)[0]}x{_wxh(size_in)[1]}" if _wxh(size_in) else "2K"),
        "ratio": ratio_in if ratio_in in ("1:1", "16:9", "9:16", "4:3", "3:4", "2:3", "3:2", "21:9") else "16:9",
    }

    if n > 1:
        p["n"] = n

    # Base64 çıktısı için doküman standardı: top-level return_base64: true
    if fmt == "b64_json":
        p["return_base64"] = True

    # Extra body: Dokümanda açıkça uyarılmıştır:
    # "Do not place response_format at the top level of the request body.
    #  For URL output, use extra_body.response_format: 'url'.
    #  For image-to-image Base64 output, use extra_body.response_format: 'b64_json'."
    extra_body: dict[str, Any] = {
        "response_format": fmt
    }

    # Negative Prompt desteği
    if (neg := (d.get("negative_prompt") or "").strip()):
        p["negative_prompt"] = neg
        extra_body["negative_prompt"] = neg

    # Seed desteği
    seed = _int(d.get("seed"))
    if seed is not None and seed != -1:
        if not 0 <= seed <= 999:
            raise AgnesError("seed -1 ile 999 arasında olmalı.")
        p["seed"] = seed

    # Referans Görseller (Image-to-image & Multi-image Composition)
    # Resmi doküman: extra_body.image = ["url1", "url2"]
    raw_refs = d.get("reference_images") or d.get("image") or []
    if isinstance(raw_refs, str):
        raw_refs = [raw_refs]
    refs = [_img(r) for r in raw_refs if r]

    # En fazla 6 referans görsel (sunucu kuralı: at most 6 allowed)
    if len(refs) > 6:
        refs = refs[:6]

    if refs:
        strength = _float(d.get("strength"), 0.75)
        if not 0.1 <= strength <= 1.0:
            raise AgnesError("strength 0.1 - 1.0 olmalı.")
        # Resmi dokümantasyon: extra_body.image içine dizi verilir
        extra_body["image"] = refs
        p["image"] = refs
        p["strength"] = strength

    p["extra_body"] = extra_body

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
        w = max(256, round(w / 64) * 64)
        h = max(256, round(h / 64) * 64)
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
        # V2.0 KARE TABANLI MİMARİ (yay0128/Agnes)
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

        start, end = d.get("start_image"), d.get("end_image")
        if end and not start:
            raise AgnesError("Bitiş karesi için başlangıç karesi de gereklidir.")

        if start:
            s = _img(start)
            if end:
                e = _img(end)
                # KEYFRAME GEÇİŞİ (yay0128/Agnes: examples/video_keyframes.py)
                # DOĞRU KULLANIM: mode ve image doğrudan 'extra_body' sözlüğü içinde gönderilir!
                # Kök düzeyde 'mode' veya 'image' GÖNDERİLMEZ.
                p["extra_body"] = {"image": [s, e], "mode": "keyframes"}
            else:
                # IMAGE-TO-VIDEO (yay0128/Agnes: examples/video_image_to_video.py)
                # Tek görsel kök düzeyde 'image' olarak verilir, kök 'mode' GÖNDERİLMEZ.
                p["image"] = s

    else:
        # ==============================================================================
        # 2.5 VE FLASH MODELLERİ (Resmi Agnes AI API Dokümantasyonu)
        # https://agnes-ai.com/doc/agnes-video-25
        # https://agnes-ai.com/doc/agnes-video-25-flash
        # ==============================================================================
        # 1. SÜRE: "duration" DESTEKLENMEZ! Parametre adı zorunlu olarak string "seconds" olmalıdır!
        sec_val = int(_float(d.get("seconds") or d.get("duration"), 5.0))
        if not 4 <= sec_val <= 12:
            raise AgnesError("seconds 4 ile 12 saniye arasında olmalıdır.")
        p["seconds"] = str(sec_val)

        # 2. EN-BOY ORANI
        ar = d.get("aspect_ratio", "16:9")
        if ar not in ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"):
            ar = "16:9"
        p["aspect_ratio"] = ar

        # 3. ÇÖZÜNÜRLÜK (size)
        # agnes-video-2.5-flash için SADECE "720P" geçerlidir.
        # agnes-video-2.5 için "720P", "1080P", "1K", "2K" desteklenir.
        is_flash = "flash" in model.lower()
        res_req = str(d.get("resolution") or "720p").upper()
        if is_flash:
            p["size"] = "720P"
        else:
            p["size"] = res_req if res_req in ("1080P", "1K", "2K") else "720P"

        # 4. GÖRSEL / KEYFRAME / REFERANS MODLARI (2.5 Standartları)
        start, end = d.get("start_image"), d.get("end_image")
        ref_imgs = d.get("ref_images") or []
        ref_auds = [a.strip() for a in d.get("ref_audio_urls") or [] if a and a.strip()]
        ref_vid = (d.get("ref_video_url") or "").strip()

        if end and not start:
            raise AgnesError("Bitiş karesi için başlangıç karesi de gereklidir.")

        if start:
            # KEYFRAME MODU (2.5 Resmi Dokümantasyonu: mode="keyframe", first_frame, last_frame)
            p["mode"] = "keyframe"
            p["first_frame"] = _img(start)
            if end:
                p["last_frame"] = _img(end)
        elif ref_imgs or ref_auds or ref_vid:
            # MULTIMODAL REFERANS MODU (2.5 Resmi Dokümantasyonu: mode="reference")
            p["mode"] = "reference"
            if len(ref_imgs) > 5:
                ref_imgs = ref_imgs[:5]
            if len(ref_auds) > 3:
                ref_auds = ref_auds[:3]
            p["images"] = [_img(x) for x in ref_imgs]
            if ref_auds:
                p["audios"] = ref_auds
            if ref_vid and not is_flash:
                p["videos"] = [{"url": ref_vid, "start_seconds": 0, "require_audio": False}]
        else:
            # METİNDEN VİDEO (2.5 Resmi Dokümantasyonu: mode="text")
            p["mode"] = "text"

    # İsteğe Bağlı Parametreler (Agnes Video V2.0 ve 2.5)
    # NOT (yay0128/Agnes): Video üretiminde "steps" / "num_inference_steps" parametresi
    # desteklenmez; model kendi dahili difüzyon adım sayısını kullanır.
    if (neg := (d.get("negative_prompt") or "").strip()):
        p["negative_prompt"] = neg
    if (seed := _int(d.get("seed"))) is not None:
        p["seed"] = seed

    # Görevi oluştur
    data = _req("POST", "/videos", p, timeout=60).json()
    task_id = data.get("id") or data.get("task_id")
    video_id = data.get("video_id")
    if not task_id and not video_id:
        raise AgnesError("Görev ID'si alınamadı.", 502, data)

    spec_summary = {k: p[k] for k in ("width", "height", "num_frames", "frame_rate", "seconds", "size",
                                       "aspect_ratio", "mode") if k in p}
    if "extra_body" in p and isinstance(p["extra_body"], dict):
        spec_summary["mode"] = p["extra_body"].get("mode", "keyframes")
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
        _save_state_file({"data": d, "updated": _STATE["updated"]})
    return jsonify({"ok": True})


@app.delete("/api/state")
def api_state_delete():
    with _LOCK:
        _STATE["data"], _STATE["updated"] = {}, time.time()
        _save_state_file({"data": {}, "updated": _STATE["updated"]})
    return jsonify({"ok": True})


@app.get("/api/models")
def api_models():
    return jsonify(list_models(live=True))


@app.post("/api/chat")
def api_chat():
    d = _body()
    if not d.get("stream", True):
        return jsonify(chat(d))

    def out():
        try:
            for piece in chat_stream(d):
                yield piece
        except AgnesError as e:
            yield f"\n\n[HATA] {e.message}"
        except Exception as e:
            yield f"\n\n[HATA] {str(e)}"

    return Response(stream_with_context(out()), mimetype="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


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
