#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agnes Studio — Flask backend (tek dosya, API istemcisi dahil)
==============================================================================
ORTAM DEĞİŞKENLERİ
  AGNES_API_KEY  (zorunlu)   sk-...
  APP_PASSWORD   (opsiyonel) Tanımlıysa site Basic Auth şifresi ister. Public deploy'da AYARLA.

OTURUM DEPOLAMA
  Arayüz durumu (ayarlar, promptlar, görevler, galeri, sohbet) SUNUCU BELLEĞİNDE tutulur.
  Sayfa yenilenince geri gelir; app.py yeniden başlayınca sıfırlanır.
  ÖNEMLİ: Bellek worker'lar arasında paylaşılmaz -> gunicorn MUTLAKA --workers 1 çalışmalı.

MODELLER
  Metin : agnes-3.0-flash, agnes-2.5-flash (vars.), agnes-2.0-flash      -> FREE
          agnes-2.5-pro, -pro-alpha, -pro-beta                           -> PRO (403)
  Resim : agnes-image-2.5-flash (vars.), -2.1-flash, -2.0-flash          -> FREE
  Video : agnes-video-v2.0 (vars., kare tabanlı)                         -> FREE
          agnes-video-2.5, agnes-video-2.5-flash (süre tabanlı, R2V)     -> TOKEN (429)

RESİM  POST /v1/images/generations
  size serbest "GxY". Presetler: 16:9 2K=1920x1080, 9:16 4K=2160x3840, 1:1 1K=1024x1024 ...
  n>=1, seed -1..999, response_format url|b64_json
  Referans en fazla 6 (1 -> string, 2-6 -> liste), strength 0.1..1.0 (sadece ref varsa)
  YASAK: quality, style, guidance_scale, cfg, num_inference_steps

VİDEO  POST /v1/videos (asenkron)
  v2.0: width, height, num_frames (8n+1), frame_rate (1-60), aspect_ratio
        480p 854x480 maks 961 kare | 720p 1280x704 maks 481 | 1080p 1920x1088 maks 241
        Örn: 720p 10 sn 24fps -> 241 kare ; 1080p 20 sn -> 241'e kırpılır (~10 sn)
        Sınır kısa kenara göre belirlenir.
  2.5 : duration 4-12 sn, aspect_ratio 16:9|9:16|1:1
  Ortak: negative_prompt, seed, num_inference_steps (vars. 8). CFG sabit.
  Referans: başlangıç 1 görsel (image) | first_frame+last_frame | R2V: 5 görsel, 3 ses, 1 video
  Polling: GET /agnesapi?video_id=&model_name= (canlı %), yedek GET /v1/videos/{task_id}
==============================================================================
"""
import json
import os
import re
import threading
import time
import uuid
from typing import Any

import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

API_KEY = os.environ.get("AGNES_API_KEY", "sk-KZhob2kogDQGnUhsOvm3U1254J4BTQ9oRjZrwuRcgtR8vWeA")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
API_ROOT = "https://apihub.agnes-ai.com"
BASE_URL = f"{API_ROOT}/v1"

MODELS = {
    "agnes-3.0-flash": ("text", "FREE"), "agnes-2.5-flash": ("text", "FREE"),
    "agnes-2.0-flash": ("text", "FREE"), "agnes-2.5-pro": ("text", "PRO"),
    "agnes-2.5-pro-alpha": ("text", "PRO"), "agnes-2.5-pro-beta": ("text", "PRO"),
    "agnes-image-2.5-flash": ("image", "FREE"), "agnes-image-2.1-flash": ("image", "FREE"),
    "agnes-image-2.0-flash": ("image", "FREE"), "agnes-video-v2.0": ("video", "FREE"),
    "agnes-video-2.5-flash": ("video", "TOKEN"), "agnes-video-2.5": ("video", "TOKEN"),
}
IMAGE_PRESETS = {
    "1:1":  {"1K": "1024x1024", "2K": "2048x2048", "4K": "4096x4096"},
    "16:9": {"1K": "1280x720",  "2K": "1920x1080", "4K": "3840x2160"},
    "9:16": {"1K": "720x1280",  "2K": "1080x1920", "4K": "2160x3840"},
    "4:3":  {"1K": "1024x768",  "2K": "2048x1536", "4K": "4096x3072"},
    "3:4":  {"1K": "768x1024",  "2K": "1536x2048", "4K": "3072x4096"},
    "21:9": {"1K": "1344x576",  "2K": "2560x1080", "4K": "5040x2160"},
}
VIDEO_PRESETS = {
    "480p":  {"16:9": [854, 480],   "9:16": [480, 854],   "1:1": [480, 480]},
    "720p":  {"16:9": [1280, 704],  "9:16": [704, 1280],  "1:1": [768, 768]},
    "1080p": {"16:9": [1920, 1088], "9:16": [1088, 1920], "1:1": [1088, 1088]},
}
LIMITS = {"image_refs": 6, "video_ref_images": 5, "video_ref_audios": 3}

# Runtime oturum deposu (yeniden başlatınca sıfırlanır)
BOOT_ID = uuid.uuid4().hex
_STATE: dict[str, Any] = {"data": {}, "updated": 0.0}
_LOCK = threading.Lock()


class AgnesError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: Any = None):
        super().__init__(message)
        self.message, self.status_code, self.details = message, status_code, details


# ------------------------------------------------------------------ HTTP
def _req(method: str, path: str, body: Any = None, timeout: int = 60, stream: bool = False):
    if not API_KEY:
        raise AgnesError("AGNES_API_KEY tanımlı değil.", 500)
    url = path if path.startswith("http") else f"{BASE_URL}/{path.lstrip('/')}"
    try:
        r = requests.request(method, url, json=body, timeout=timeout, stream=stream,
                             headers={"Authorization": f"Bearer {API_KEY}",
                                      "Content-Type": "application/json"})
    except requests.RequestException as e:
        raise AgnesError(f"Bağlantı hatası: {e}", 502) from e
    if not r.ok:
        try:
            data = r.json()
            err = data.get("error", data)
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        except ValueError:
            data, msg = r.text, r.text[:500]
        hint = {403: " (Pro plan gerekli olabilir)", 429: " (Rate limit / Token plan gerekli)"}
        raise AgnesError(f"{msg}{hint.get(r.status_code, '')}", r.status_code, data)
    return r


def _wxh(v):
    m = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", str(v or ""))
    return (int(m[1]), int(m[2])) if m and int(m[1]) > 0 and int(m[2]) > 0 else None


def _int(v, default=None):
    if v in (None, ""):
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        raise AgnesError(f"Geçersiz sayı: {v}")


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
        raise AgnesError("Görsel URL veya data URI olmalı.")
    return s


# ------------------------------------------------------------------ MODELLER
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


# ------------------------------------------------------------------ CHAT
def _chat_payload(d: dict, stream: bool) -> dict:
    msgs = d.get("messages") or []
    if not msgs:
        raise AgnesError("messages boş olamaz.")
    p = {"model": d.get("model") or "agnes-2.5-flash", "messages": msgs, "stream": stream,
         "temperature": _float(d.get("temperature"), 0.7), "top_p": _float(d.get("top_p"), 1.0),
         "max_tokens": _int(d.get("max_tokens"), 4096),
         "frequency_penalty": _float(d.get("frequency_penalty"), 0.0),
         "presence_penalty": _float(d.get("presence_penalty"), 0.0)}
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


def expand_video_prompt(idea: str) -> str:
    if not idea:
        raise AgnesError("prompt boş olamaz.")
    sys_msg = ("You are a cinematic video director. Expand the user's short idea into a rich, detailed "
               "video generation prompt in English. Include camera movements, lighting, colors, lens, "
               "atmosphere, and visual action. Output ONLY the prompt itself.")
    try:
        out = chat({"messages": [{"role": "system", "content": sys_msg}, {"role": "user", "content": idea}],
                    "temperature": 0.6, "max_tokens": 300})["content"].strip()
        return out or idea
    except AgnesError:
        return idea


# ------------------------------------------------------------------ RESİM
def generate_image(d: dict) -> list[dict]:
    prompt = (d.get("prompt") or "").strip()
    if not prompt:
        raise AgnesError("prompt boş olamaz.")
    size = _wxh(d.get("size"))
    if not size:
        raise AgnesError(f"Geçersiz boyut: {d.get('size') or '-'} (örn: 1920x1080)")
    n = _int(d.get("n"), 1)
    fmt = d.get("response_format", "url")
    if n < 1 or fmt not in ("url", "b64_json"):
        raise AgnesError("n>=1 ve response_format url|b64_json olmalı.")
    p = {"model": d.get("model") or "agnes-image-2.5-flash", "prompt": prompt, "n": n,
         "size": f"{size[0]}x{size[1]}", "response_format": fmt}
    seed = _int(d.get("seed"))
    if seed is not None and seed != -1:
        if not 0 <= seed <= 999:
            raise AgnesError("seed -1 ile 999 arasında olmalı.")
        p["seed"] = seed
    refs = [_img(r) for r in d.get("reference_images") or []]
    if len(refs) > LIMITS["image_refs"]:
        raise AgnesError("En fazla 6 referans görsel.")
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


# ------------------------------------------------------------------ VİDEO
def resolve_video_spec(resolution="720p", aspect_ratio="16:9", seconds=5.0, fps=24, num_frames=None):
    if not 1 <= fps <= 60:
        raise AgnesError("fps 1-60 olmalı.")
    custom = _wxh(resolution)
    if custom:
        w, h = custom
        aspect_ratio = "16:9" if w > h else "9:16" if h > w else "1:1"
    else:
        try:
            w, h = VIDEO_PRESETS[resolution][aspect_ratio]
        except KeyError:
            raise AgnesError(f"Geçersiz çözünürlük/oran: {resolution} {aspect_ratio}")
    short = min(w, h)
    max_f = 241 if short >= 1080 else 481 if short > 480 else 961
    target = num_frames if num_frames else round((seconds or 5.0) * fps)
    frames = min(8 * max(1, round((target - 1) / 8)) + 1, max_f)
    return {"width": w, "height": h, "aspect_ratio": aspect_ratio, "num_frames": frames, "frame_rate": fps}


def create_video(d: dict) -> dict:
    prompt = (d.get("prompt") or "").strip()
    if not prompt:
        raise AgnesError("prompt boş olamaz.")
    if d.get("expand"):
        prompt = expand_video_prompt(prompt)
    model = d.get("model") or "agnes-video-v2.0"
    p: dict[str, Any] = {"model": model, "prompt": prompt}

    if "v2.0" in model.lower():
        p.update(resolve_video_spec(d.get("resolution", "720p"), d.get("aspect_ratio", "16:9"),
                                    _float(d.get("seconds"), 5.0), _int(d.get("fps"), 24),
                                    _int(d.get("num_frames"))))
    else:
        dur, ar = _int(d.get("duration"), 5), d.get("aspect_ratio", "16:9")
        if not 4 <= dur <= 12 or ar not in ("16:9", "9:16", "1:1"):
            raise AgnesError("duration 4-12 sn, aspect_ratio 16:9|9:16|1:1 olmalı.")
        p.update({"duration": dur, "aspect_ratio": ar})

    start, end = d.get("start_image"), d.get("end_image")
    if end:
        if not start:
            raise AgnesError("Bitiş karesi için başlangıç karesi de gerekli.")
        p["first_frame"] = {"type": "image_url", "image_url": {"url": _img(start)}}
        p["last_frame"] = {"type": "image_url", "image_url": {"url": _img(end)}}
    elif start:
        p["image"] = _img(start)

    imgs = d.get("ref_images") or []
    auds = [a.strip() for a in d.get("ref_audio_urls") or [] if a and a.strip()]
    vid = (d.get("ref_video_url") or "").strip()
    if len(imgs) > LIMITS["video_ref_images"] or len(auds) > LIMITS["video_ref_audios"]:
        raise AgnesError("En fazla 5 referans görsel ve 3 ses.")
    refs = [{"type": "image_url", "image_url": {"url": _img(i)}, "role": "style"} for i in imgs]
    refs += [{"type": "audio_url", "audio_url": {"url": a}, "role": "audio"} for a in auds]
    if vid:
        refs.append({"type": "video_url", "video_url": {"url": vid}, "role": "motion"})
    if refs:
        p["references"] = refs

    if (neg := (d.get("negative_prompt") or "").strip()):
        p["negative_prompt"] = neg
    if (seed := _int(d.get("seed"))) is not None:
        p["seed"] = seed
    if (steps := _int(d.get("num_inference_steps"))) is not None:
        if steps < 1:
            raise AgnesError("num_inference_steps pozitif olmalı.")
        p["num_inference_steps"] = steps

    data = _req("POST", "/videos", p, timeout=60).json()
    task_id, video_id = data.get("id") or data.get("task_id"), data.get("video_id")
    if not task_id and not video_id:
        raise AgnesError("Görev ID'si alınamadı.", 502, data)
    spec = {k: p[k] for k in ("width", "height", "num_frames", "frame_rate", "duration", "aspect_ratio") if k in p}
    return {"task_id": task_id, "video_id": video_id, "model": model, "prompt": prompt, "spec": spec}


_STATUS = {"queued": "queued", "pending": "queued", "inference": "in_progress",
           "in_progress": "in_progress", "processing": "in_progress", "running": "in_progress",
           "completed": "completed", "succeeded": "completed", "success": "completed", "done": "completed",
           "failed": "failed", "error": "failed", "cancelled": "failed"}


def _norm(d: dict) -> dict:
    raw = str(d.get("internal_status") or d.get("status") or "unknown").lower()
    st = _STATUS.get(raw, "in_progress")
    prog = d.get("internal_progress")
    prog = 100 if st == "completed" else (prog if prog is not None else d.get("progress") or 0)
    url = (d.get("url") or d.get("video_url") or (d.get("output") or {}).get("url")
           or (d.get("data") or {}).get("url"))
    return {"status": st, "raw_status": raw, "progress": prog, "url": url,
            "error": d.get("error") if st == "failed" else None}


def get_video_status(task_id, video_id, model) -> dict:
    live = None
    if video_id:
        try:
            r = requests.get(f"{API_ROOT}/agnesapi", params={"video_id": video_id, "model_name": model},
                             headers={"Authorization": f"Bearer {API_KEY}"}, timeout=15)
            if r.ok:
                live = _norm(r.json())
        except (requests.RequestException, ValueError):
            live = None
    if task_id and (live is None or (live["status"] == "completed" and not live["url"])):
        try:
            fb = _norm(_req("GET", f"/videos/{task_id}", timeout=15).json())
            if live is None or fb["url"]:
                live = fb
        except AgnesError:
            pass
    return live or {"status": "unknown", "raw_status": "unreachable", "progress": 0, "url": None, "error": None}


# ------------------------------------------------------------------ FLASK
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
    return jsonify({"error": "İstek çok büyük (maks 100 MB). Görselleri azalt."}), 413


def _body() -> dict:
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        raise AgnesError("Geçersiz JSON gövdesi.")
    return d


@app.get("/")
def index():
    cfg = {"models": list_models(live=False), "image_presets": IMAGE_PRESETS,
           "video_presets": VIDEO_PRESETS, "limits": LIMITS, "boot_id": BOOT_ID}
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
    first = next(gen, "")  # bağlantı/API hatası burada AgnesError olarak JSON döner

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
        raise AgnesError("task_id veya video_id gerekli.")
    return jsonify(get_video_status(a.get("task_id") or None, a.get("video_id") or None,
                                    a.get("model", "agnes-video-v2.0")))


@app.post("/api/expand")
def api_expand():
    return jsonify({"prompt": expand_video_prompt((_body().get("prompt") or "").strip())})


@app.post("/api/raw")
def api_raw():
    d = _body()
    method = str(d.get("method", "GET")).upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise AgnesError("Geçersiz HTTP metodu.")
    ep = str(d.get("endpoint") or "/models").strip()
    if ep.startswith("http") and not ep.startswith(API_ROOT):
        raise AgnesError("Sadece apihub.agnes-ai.com adreslerine izin var.")
    if ep.startswith("/v1/"):
        ep = API_ROOT + ep
    r = _req(method, ep, d.get("body") if method in ("POST", "PUT", "PATCH") else None, timeout=120)
    try:
        return jsonify({"status": r.status_code, "body": r.json()})
    except ValueError:
        return jsonify({"status": r.status_code, "body": r.text[:5000]})


if __name__ == "__main__":
    app.run(debug=False, port=int(os.environ.get("PORT", 5000)), threaded=True)
