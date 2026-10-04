import copy
import json
import logging
import math
import os
import threading
import time
from urllib.parse import quote

import requests
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException


app = Flask(
    __name__,
    template_folder="templates",
    static_folder="static",
)

app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

logging.basicConfig(level=logging.INFO)

DEFAULT_API_KEY = os.environ.get(
    "AGNES_API_KEY",
    "sk-niO32ft0zOmYOJSp0OIbPwSKdxVdBupsF6AmB0slcZMUwQjv",
).strip()

DEFAULT_BASE_URL = os.environ.get(
    "AGNES_BASE_URL",
    "https://apihub.agnes-ai.com/v1",
).rstrip("/")

VIDEO_STATUS_URL = os.environ.get(
    "AGNES_VIDEO_STATUS_URL",
    "https://apihub.agnes-ai.com/agnesapi",
)

ALLOWED_BASE_URLS = {
    value.strip().rstrip("/")
    for value in os.environ.get(
        "AGNES_ALLOWED_BASE_URLS",
        DEFAULT_BASE_URL,
    ).split(",")
    if value.strip()
}

STATE_LOCK = threading.RLock()

# Ön yüzden değer GELMEZSE kullanılan video ayarları.
DEFAULT_VIDEO_MODEL = "agnes-video-v2.0"
DEFAULT_VIDEO_SECONDS = 10.0
DEFAULT_VIDEO_SIZE = "1080P"
DEFAULT_VIDEO_RATIO = "16:9"
DEFAULT_VIDEO_FPS = 24.0
DEFAULT_VIDEO_SEED = 42

# Dokümanda önerilen/default inference sayısı belirtilmiyor.
# Bu değer değiştirilebilir bir başlangıç tercihidir.
DEFAULT_VIDEO_STEPS = 30

DEFAULT_VIDEO_NEGATIVE_PROMPT = (
    "blurry, low quality, distorted face, deformed hands, "
    "extra fingers, flickering, unstable anatomy, watermark, text"
)


# ============================================================
# RAM BELLEK DEPOSU
# Sayfa yenilenmesinde korunur, sunucu kapanınca temizlenir.
# Durum tüm ziyaretçiler arasında ortaktır.
# ============================================================

def get_default_state():
    return {
        "active_tab": "chat",
        "chat": {
            "model": "agnes-3.0-flash",
            "system_prompt": "",
            "prompt": "",
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 2048,
            "presence_penalty": 0.0,
            "frequency_penalty": 0.0,
            "enable_thinking": True,
            "image": None,
            "output": "",
            "stats": {
                "duration_ms": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
            },
            "has_output": False,
        },
        "image": {
            "model": "agnes-image-2.5-flash",
            "mode": "t2i",
            "prompt": "",
            "negative_prompt": "",
            "ref_image": None,
            "size": "1K",
            "ratio": "1:1",
            "format": "url",
            "seed": "",
            "guidance_scale": "7.5",
            "steps": "30",
            "image_strength": "0.8",
            "result_url": None,
            "has_result": False,
        },
        "video": {
            "model": DEFAULT_VIDEO_MODEL,
            "mode": "text",
            "prompt": "",
            "negative_prompt": DEFAULT_VIDEO_NEGATIVE_PROMPT,
            "first_frame": "",
            "last_frame": "",
            "ref_images": "",
            "ref_audios": "",
            "ref_videos": "",
            "seconds": "10",
            "aspect_ratio": DEFAULT_VIDEO_RATIO,
            "size": DEFAULT_VIDEO_SIZE,
            "seed": str(DEFAULT_VIDEO_SEED),
            "fps": "24",
            "steps": str(DEFAULT_VIDEO_STEPS),
            "num_frames": 241,
            "task_id": None,
            "video_id": None,
            "provider_task_id": None,
            "query_kind": "video",
            "base_url": DEFAULT_BASE_URL,
            "status": None,
            "progress": 0,
            "url": None,
            "duration_seconds": 0,
            "actual_size": None,
            "has_task": False,
        },
        "last_log": {
            "status": None,
            "duration_ms": 0,
            "endpoint": "",
            "request_payload": None,
            "raw_response": None,
        },
    }


RUNTIME_STATE = get_default_state()

# Görevlerin modelini ve sorgu türünü saklar.
# API anahtarları burada saklanmaz.
VIDEO_TASKS = {}


# ============================================================
# YARDIMCI FONKSİYONLAR
# ============================================================

class InputError(Exception):
    pass


class ConfigurationError(Exception):
    pass


def read_json():
    if not request.is_json:
        if request.get_data():
            raise InputError(
                "İstek gövdesi application/json biçiminde olmalıdır."
            )
        return {}

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        raise InputError("JSON gövdesi bir nesne olmalıdır.")

    return data


def text(value, default=""):
    if value is None:
        return default
    return str(value).strip()


def first_value(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def as_bool(value, default=False):
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return value != 0

    return text(value).lower() in {"true", "1", "yes", "on"}


def number(value, name, converter, default=None):
    if value is None or text(value) == "":
        return default

    try:
        result = converter(value)
    except (TypeError, ValueError, OverflowError):
        raise InputError(f"{name} geçerli bir sayı olmalıdır.")

    if isinstance(result, float) and not math.isfinite(result):
        raise InputError(f"{name} sonlu bir sayı olmalıdır.")

    return result


def integer(value, name, default=None):
    parsed = number(value, name, float, default)

    if parsed is None:
        return None

    if not math.isfinite(float(parsed)) or int(parsed) != parsed:
        raise InputError(f"{name} tam sayı olmalıdır.")

    return int(parsed)


def add_number(payload, data, name, converter):
    value = number(data.get(name), name, converter)
    if value is not None:
        payload[name] = value


def get_api_key(data=None):
    data = data or {}
    return (
        text(request.headers.get("X-Api-Key"))
        or text(data.get("api_key"))
        or DEFAULT_API_KEY
    )


def get_headers(api_key):
    if not api_key:
        raise ConfigurationError(
            "API anahtarı bulunamadı. AGNES_API_KEY ortam "
            "değişkenini tanımlayın veya arayüzden anahtar girin."
        )

    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def get_base_url(data):
    base_url = (
        text(data.get("base_url")) or DEFAULT_BASE_URL
    ).rstrip("/")

    if base_url not in ALLOWED_BASE_URLS:
        raise InputError(
            "Bu base_url adresine izin verilmiyor. "
            "AGNES_ALLOWED_BASE_URLS ayarını kontrol edin."
        )

    return base_url


def elapsed_ms(start):
    return round((time.perf_counter() - start) * 1000)


def state_snapshot():
    with STATE_LOCK:
        return copy.deepcopy(RUNTIME_STATE)


def update_section(section, values, activate=True):
    with STATE_LOCK:
        RUNTIME_STATE[section].update(values)
        if activate:
            RUNTIME_STATE["active_tab"] = section


def merge_known_fields(target, updates):
    for key, value in updates.items():
        if key not in target:
            continue

        current = target[key]

        if isinstance(current, dict):
            if isinstance(value, dict):
                merge_known_fields(current, value)
            continue

        if current is None:
            target[key] = value
        elif isinstance(current, bool):
            if isinstance(value, bool):
                target[key] = value
        elif isinstance(current, (int, float)):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                target[key] = value
        elif isinstance(value, type(current)):
            target[key] = value


def save_log(status, duration, endpoint, payload, response):
    with STATE_LOCK:
        RUNTIME_STATE["last_log"] = {
            "status": status,
            "duration_ms": duration,
            "endpoint": endpoint,
            "request_payload": copy.deepcopy(payload),
            "raw_response": copy.deepcopy(response),
        }


def parse_response(resp):
    try:
        return resp.json()
    except ValueError:
        return {"raw_text": resp.text}


def upstream_request(method, url, api_key, **kwargs):
    return requests.request(
        method,
        url,
        headers=get_headers(api_key),
        allow_redirects=False,
        **kwargs,
    )


def finish(resp, start, endpoint, payload=None, response_key="raw_response"):
    response = parse_response(resp)
    duration = elapsed_ms(start)

    save_log(resp.status_code, duration, endpoint, payload, response)

    body = {
        "status": resp.status_code,
        "duration_ms": duration,
        "endpoint": endpoint,
        response_key: response,
    }

    if payload is not None:
        body["request_payload"] = payload

    return body, response


def error_result(exc, start, endpoint, payload=None):
    if isinstance(exc, InputError):
        status = 400
        message = str(exc)
    elif isinstance(exc, ConfigurationError):
        status = 500
        message = str(exc)
    elif isinstance(exc, requests.exceptions.SSLError):
        status = 502
        message = "API sunucusunun TLS sertifikası doğrulanamadı."
    elif isinstance(exc, requests.exceptions.Timeout):
        status = 504
        message = (
            "API isteği zaman aşımına uğradı. Video görevi sağlayıcıda "
            "başlamış olabilir; yeniden göndermeden önce kontrol edin."
        )
    elif isinstance(exc, requests.exceptions.RequestException):
        status = 502
        message = "API sunucusuna bağlanılamadı."
    elif isinstance(exc, HTTPException):
        status = exc.code or 500
        message = exc.description
    else:
        status = 500
        message = "Sunucuda beklenmeyen bir hata oluştu."
        app.logger.exception("İstek işlenemedi: %s", endpoint)

    duration = elapsed_ms(start)
    raw = {"error": message}

    save_log(status, duration, endpoint, payload, raw)

    return jsonify({
        "status": status,
        "duration_ms": duration,
        "endpoint": endpoint,
        "request_payload": payload,
        "raw_response": raw,
        "error": message,
    }), status


def normalize_media(value, field):
    if value is None or value == "":
        return []

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []

        if value.startswith("data:"):
            items = [value]
        else:
            items = value.replace("\r", "\n").replace("\n", ",").split(",")
    elif isinstance(value, list):
        items = value
    elif isinstance(value, dict) and "url" in value:
        items = [value]
    else:
        raise InputError(f"{field} bir URL metni veya liste olmalıdır.")

    result = []

    for item in items:
        if isinstance(item, dict):
            item = item.get("url")

        if not isinstance(item, str):
            raise InputError(
                f"{field} alanındaki öğeler URL metni "
                "veya url içeren nesne olmalıdır."
            )

        item = item.strip()
        if item:
            result.append(item)

    return result


def media_from(data, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "" and value != []:
            return normalize_media(value, key)
    return []


def image_url(value):
    value = text(value)
    if value.startswith(("http://", "https://", "data:")):
        return value
    return f"data:image/jpeg;base64,{value}"


def response_layers(raw):
    if not isinstance(raw, dict):
        return []

    layers = [raw]
    nested = raw.get("data")
    if isinstance(nested, dict):
        layers.append(nested)
    return layers


def response_value(raw, *keys):
    # Anahtar önceliğini katmanlar arasında da korur.
    # Örneğin data.video_id, kökteki id'den önce seçilir.
    for key in keys:
        for layer in response_layers(raw):
            value = layer.get(key)
            if value is not None and value != "":
                return value
    return None


def upstream_failed(raw):
    for layer in response_layers(raw):
        if layer.get("error"):
            return True

        status = text(layer.get("status")).lower()
        code = text(layer.get("code")).lower()

        if status in {"failed", "error", "cancelled", "canceled"}:
            return True
        if code.startswith(("fail", "error")):
            return True
        if code.isdigit() and int(code) >= 400:
            return True

    return False


def error_text(raw):
    def extract(value, depth=0):
        if depth > 6:
            return None

        if isinstance(value, dict):
            for key in ("error", "message", "detail", "raw_text", "data"):
                if key in value:
                    found = extract(value[key], depth + 1)
                    if found:
                        return found

        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                return value

            if isinstance(decoded, (dict, str)):
                return extract(decoded, depth + 1)

            return value

        return None

    return extract(raw) or "API isteği başarısız oldu."


# ============================================================
# VİDEO YARDIMCILARI
# ============================================================

def is_video_v20(model):
    # "v2" substring kontrolü yapılmaz:
    # v2.5 gibi başka sürümler yanlışlıkla v2.0 sayılmasın.
    return model.strip().lower() in {
        "agnes-video-v2.0",
        "agnes-video-2.0",
    }


def safe_float(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def video_status(raw, default="queued"):
    status = text(response_value(raw, "status")).lower()

    aliases = {
        "pending": "queued",
        "processing": "in_progress",
        "running": "in_progress",
        "succeeded": "completed",
        "success": "completed",
        "done": "completed",
        "error": "failed",
        "canceled": "cancelled",
    }

    if upstream_failed(raw):
        return "failed"

    return aliases.get(status, status or default)


def video_result_url(raw):
    for field in ("remixed_from_video_id", "video_url", "url"):
        value = response_value(raw, field)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value
    return None


def reported_duration(raw):
    return safe_float(
        response_value(raw, "seconds", "duration_seconds", "duration")
    )


def duration_warning(requested, actual, fps):
    if requested is None or actual is None:
        return None

    tolerance = max(0.15, 1.0 / (fps or DEFAULT_VIDEO_FPS))

    if abs(actual - requested) > tolerance:
        return (
            f"İstenen süre {requested:g} saniye, API'nin bildirdiği "
            f"süre {actual:g} saniye. Sağlayıcı süreyi normalize etmiş "
            "olabilir; gerçek çıktı için API'nin seconds alanını esas alın."
        )

    return None


def resolve_video_dimensions(data, size, ratio):
    width_raw = data.get("width")
    height_raw = data.get("height")

    has_width = width_raw is not None and text(width_raw) != ""
    has_height = height_raw is not None and text(height_raw) != ""

    if has_width or has_height:
        if not (has_width and has_height):
            raise InputError("width ve height birlikte verilmelidir.")

        width = integer(width_raw, "width")
        height = integer(height_raw, "height")

        if width <= 0 or height <= 0:
            raise InputError("width ve height pozitif olmalıdır.")

        return width, height

    # Bunlar istenen boyutlardır; sağlayıcı normalize edebilir.
    resolutions = {
        "480P": {
            "16:9": (854, 480),
            "9:16": (480, 854),
            "1:1": (480, 480),
            "4:3": (640, 480),
            "3:4": (480, 640),
        },
        "720P": {
            "16:9": (1280, 720),
            "9:16": (720, 1280),
            "1:1": (720, 720),
            "4:3": (960, 720),
            "3:4": (720, 960),
        },
        "1080P": {
            "16:9": (1920, 1080),
            "9:16": (1080, 1920),
            "1:1": (1080, 1080),
            "4:3": (1440, 1080),
            "3:4": (1080, 1440),
        },
    }

    if size not in resolutions:
        raise InputError("V2.0 size: 480P, 720P veya 1080P olmalıdır.")

    if ratio not in resolutions[size]:
        raise InputError(
            "V2.0 aspect_ratio: 16:9, 9:16, 1:1, 4:3 veya 3:4 olmalıdır."
        )

    return resolutions[size][ratio]


def calculate_v20_frames(seconds, fps):
    # İstenen süreden kısa olmayacak ilk 8n+1 kare sayısı.
    # 10 saniye * 24 FPS = 240 -> 241 kare.
    target_frames = seconds * fps
    n = max(1, math.ceil((target_frames - 1.0) / 8.0 - 1e-10))
    frames = 8 * n + 1

    if frames > 441:
        raise InputError(
            f"{seconds:g} saniye ve {fps:g} FPS için {frames} kare gerekir. "
            f"V2.0 üst sınırı 441 karedir. Bu FPS ile en fazla "
            f"{441 / fps:.3f} saniye istenebilir. Süreyi veya FPS'i azaltın."
        )

    return frames


def build_video_payload(data):
    model = text(data.get("model")) or DEFAULT_VIDEO_MODEL
    v20 = is_video_v20(model)

    extra = data.get("extra_body")
    if extra is None:
        extra = {}
    if not isinstance(extra, dict):
        raise InputError("extra_body bir JSON nesnesi olmalıdır.")

    raw_mode = text(first_value(
        data.get("mode"),
        extra.get("mode"),
    )).lower() or "text"

    modes = {
        "text": "text",
        "t2v": "text",
        "t2i": "text",
        "ti2vid": "text",
        "image": "text",
        "i2v": "text",
        "image_to_video": "text",
        "keyframe": "keyframe",
        "keyframes": "keyframe",
        "reference": "reference",
        "multi_reference": "reference",
    }

    if raw_mode not in modes:
        raise InputError(f"Geçersiz video modu: {raw_mode}")

    ui_mode = modes[raw_mode]
    prompt = text(data.get("prompt"))

    if not prompt:
        raise InputError("Video açıklaması boş olamaz.")

    # Alan gönderilmemişse varsayılan uygulanır.
    # Bilerek boş gönderilen negative_prompt boş bırakılır.
    if data.get("negative_prompt") is None:
        negative_prompt = DEFAULT_VIDEO_NEGATIVE_PROMPT
    else:
        negative_prompt = text(data.get("negative_prompt"))

    seconds_raw = first_value(
        data.get("seconds"),
        data.get("duration"),
        data.get("duration_seconds"),
        data.get("video_duration"),
    )
    seconds = number(
        seconds_raw, "seconds", float, DEFAULT_VIDEO_SECONDS
    )

    fps = number(
        first_value(data.get("frame_rate"), data.get("fps")),
        "frame_rate",
        float,
        DEFAULT_VIDEO_FPS,
    )

    if seconds <= 0:
        raise InputError("Video süresi sıfırdan büyük olmalıdır.")
    if not 1 <= fps <= 60:
        raise InputError("FPS 1 ile 60 arasında olmalıdır.")

    seed = integer(data.get("seed"), "seed", DEFAULT_VIDEO_SEED)

    steps = integer(
        first_value(
            data.get("num_inference_steps"),
            data.get("steps"),
        ),
        "num_inference_steps",
        DEFAULT_VIDEO_STEPS,
    )
    if steps <= 0:
        raise InputError("num_inference_steps pozitif olmalıdır.")

    size = text(data.get("size")).upper() or DEFAULT_VIDEO_SIZE
    if size in {"480", "720", "1080"}:
        size += "P"

    ratio = text(first_value(
        data.get("aspect_ratio"),
        data.get("ratio"),
    )) or DEFAULT_VIDEO_RATIO

    first_frame = text(data.get("first_frame"))
    last_frame = text(data.get("last_frame"))

    images = media_from(data, "images", "image", "ref_images")
    if not images:
        images = media_from(extra, "image", "images")

    audios = media_from(data, "audios", "ref_audios")
    videos = media_from(data, "videos", "ref_videos")

    frames = None
    estimated_seconds = seconds

    if v20:
        # V2.0 için seconds/fps/size değil, belgelenen alanlar.
        width, height = resolve_video_dimensions(data, size, ratio)

        # Ön yüzde süre seçilmişse eski/stale num_frames'i kullanma.
        # Süre hiç gönderilmezse doğrudan num_frames kullanılabilir.
        frames_raw = data.get("num_frames")
        if seconds_raw is None and frames_raw not in (None, ""):
            frames = integer(frames_raw, "num_frames")
            if frames < 9 or frames > 441 or (frames - 1) % 8:
                raise InputError(
                    "num_frames 9-441 aralığında ve 8n+1 biçiminde "
                    "olmalıdır: 81, 121, 241, 441 gibi."
                )
            seconds = frames / fps
        else:
            frames = calculate_v20_frames(seconds, fps)

        estimated_seconds = frames / fps

        if audios or videos:
            raise InputError(
                "Paylaştığınız V2.0 şemasında ses/video referansı "
                "belgelenmiyor. Bu model için görsel referansı kullanın."
            )

        payload = {
            "model": model,
            "prompt": prompt,
            "width": width,
            "height": height,
            "num_frames": frames,
            "frame_rate": fps,
            "num_inference_steps": steps,
            "seed": seed,
        }

        if negative_prompt:
            payload["negative_prompt"] = negative_prompt

        if ui_mode == "keyframe":
            first_frame = first_frame or (images[0] if images else "")
            last_frame = last_frame or (
                images[1] if len(images) >= 2 else ""
            )

            if not first_frame or not last_frame:
                raise InputError(
                    "V2.0 keyframes modu için başlangıç ve bitiş "
                    "görsellerini birlikte girin."
                )

            payload["extra_body"] = {
                "image": [first_frame, last_frame],
                "mode": "keyframes",
            }

        elif ui_mode == "reference":
            if not images:
                raise InputError(
                    "Referans modunda en az bir görsel URL'si girin."
                )

            # V2.0 için belgelenmeyen mode=multi_reference gönderilmez.
            payload["extra_body"] = {"image": images}

        else:
            if last_frame:
                raise InputError(
                    "Bitiş karesi kullanmak için keyframe modunu seçin."
                )

            if first_frame:
                payload["image"] = first_frame
            elif len(images) == 1:
                payload["image"] = images[0]
            elif len(images) > 1:
                payload["extra_body"] = {"image": images}

    else:
        # Diğer modellerin mevcut sözleşmesi korunuyor.
        # V2.0'a özgü kare/adım alanları bu modellere taşınmıyor.
        payload = {
            "model": model,
            "prompt": prompt,
            "mode": ui_mode,
            "seconds": f"{seconds:g}",
            "size": size,
            "aspect_ratio": ratio,
            "seed": seed,
            "fps": fps,
        }

        if negative_prompt:
            payload["negative_prompt"] = negative_prompt

        if ui_mode == "keyframe":
            first_frame = first_frame or (images[0] if images else "")
            last_frame = last_frame or (
                images[1] if len(images) >= 2 else ""
            )

            if not first_frame:
                raise InputError(
                    "Keyframe modu için ilk kare görseli gereklidir."
                )

            payload["first_frame"] = first_frame
            if last_frame:
                payload["last_frame"] = last_frame

        elif ui_mode == "reference":
            if not images and not audios and not videos:
                raise InputError("En az bir referans ekleyin.")

            if images:
                payload["images"] = images
            if audios:
                payload["audios"] = audios
            if videos:
                payload["videos"] = [{"url": url} for url in videos]

        elif first_frame or last_frame or images:
            raise InputError(
                "Bu modelde görsel girişi için keyframe veya "
                "reference modunu seçin."
            )

    metadata = {
        "model": model,
        "mode": ui_mode,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "seconds": f"{seconds:g}",
        "size": size,
        "aspect_ratio": ratio,
        "seed": str(seed),
        "fps": f"{fps:g}",
        "steps": str(steps) if v20 else "",
        "num_frames": frames,
        "first_frame": first_frame,
        "last_frame": last_frame,
        "ref_images": ",".join(images),
        "ref_audios": ",".join(audios),
        "ref_videos": ",".join(videos),
    }

    calculation = {
        "requested_seconds": seconds,
        "estimated_seconds": round(estimated_seconds, 6),
        "frame_rate": fps,
        "num_frames": frames,
        "duration_source": (
            "frontend"
            if seconds_raw is not None
            else (
                "num_frames"
                if v20 and data.get("num_frames") not in (None, "")
                else "default"
            )
        ),
    }

    return payload, metadata, calculation


# ============================================================
# SAYFALAR VE STATE
# ============================================================

@app.route("/")
@app.route("/index.html")
def index():
    return render_template("index.html")


@app.route("/api/state", methods=["GET", "POST"])
def api_state():
    start = time.perf_counter()
    try:
        if request.method == "POST":
            data = read_json()
            with STATE_LOCK:
                merge_known_fields(RUNTIME_STATE, data)

        return jsonify({"status": 200, "state": state_snapshot()})
    except Exception as exc:
        return error_result(exc, start, "/api/state")


@app.route("/api/state/reset", methods=["POST"])
def api_state_reset():
    global RUNTIME_STATE

    with STATE_LOCK:
        RUNTIME_STATE = get_default_state()
        # Devam eden işleri sorgulayabilmek için VIDEO_TASKS korunur.

    return jsonify({"status": 200, "state": state_snapshot()})


# ============================================================
# 1. MODELLER
# ============================================================

@app.route("/api/models", methods=["GET", "POST"])
def get_models():
    start = time.perf_counter()
    endpoint = "GET /v1/models"

    try:
        data = read_json()
        base_url = get_base_url(data)

        resp = upstream_request(
            "GET",
            f"{base_url}/models",
            get_api_key(data),
            timeout=(10, 30),
        )

        body, _ = finish(resp, start, endpoint, response_key="data")
        return jsonify(body), resp.status_code
    except Exception as exc:
        return error_result(exc, start, endpoint)


# ============================================================
# 2. CHAT
# ============================================================

@app.route("/api/chat", methods=["POST"])
def chat():
    start = time.perf_counter()
    endpoint = "POST /v1/chat/completions"
    payload = None

    try:
        data = read_json()
        base_url = get_base_url(data)

        model = text(data.get("model")) or "agnes-3.0-flash"
        system_prompt = text(data.get("system_prompt"))
        prompt = text(data.get("prompt"))
        image = text(data.get("image")) or None

        if not prompt and not image:
            raise InputError("Bir mesaj veya görsel ekleyin.")

        messages = []
        if system_prompt:
            messages.append({
                "role": "system",
                "content": system_prompt,
            })

        if image:
            content = [
                {
                    "type": "text",
                    "text": prompt or "Bu görseli ayrıntılı olarak analiz et.",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": image_url(image)},
                },
            ]
        else:
            content = prompt

        messages.append({"role": "user", "content": content})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": number(
                data.get("temperature"), "temperature", float, 0.7
            ),
            "top_p": number(data.get("top_p"), "top_p", float, 0.9),
            "max_tokens": number(
                data.get("max_tokens"), "max_tokens", int, 2048
            ),
        }

        if payload["max_tokens"] <= 0:
            raise InputError("max_tokens sıfırdan büyük olmalıdır.")

        add_number(payload, data, "presence_penalty", float)
        add_number(payload, data, "frequency_penalty", float)

        thinking = as_bool(data.get("enable_thinking"), True)
        payload["chat_template_kwargs"] = {
            "enable_thinking": thinking,
        }

        resp = upstream_request(
            "POST",
            f"{base_url}/chat/completions",
            get_api_key(data),
            json=payload,
            timeout=(10, 120),
        )

        body, raw = finish(resp, start, endpoint, payload)

        output = ""
        usage = {}

        if 200 <= resp.status_code < 300 and isinstance(raw, dict):
            choices = raw.get("choices") or []
            if choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                if isinstance(message, dict):
                    output = message.get("content") or ""

            usage = raw.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
        else:
            output = f"Hata ({resp.status_code}):\n{error_text(raw)}"

        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False, indent=2)

        details = usage.get("completion_tokens_details") or {}
        if not isinstance(details, dict):
            details = {}

        update_section("chat", {
            "model": model,
            "system_prompt": system_prompt,
            "prompt": prompt,
            "temperature": payload["temperature"],
            "top_p": payload["top_p"],
            "max_tokens": payload["max_tokens"],
            "presence_penalty": payload.get("presence_penalty", 0.0),
            "frequency_penalty": payload.get("frequency_penalty", 0.0),
            "enable_thinking": thinking,
            "image": image,
            "output": output,
            "stats": {
                "duration_ms": body["duration_ms"],
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "reasoning_tokens": details.get("reasoning_tokens", 0),
            },
            "has_output": True,
        })

        return jsonify(body), resp.status_code
    except Exception as exc:
        return error_result(exc, start, endpoint, payload)


# ============================================================
# 3. GÖRSEL ÜRETİMİ
# ============================================================

@app.route("/api/image", methods=["POST"])
def generate_image():
    start = time.perf_counter()
    endpoint = "POST /v1/images/generations"
    payload = None

    try:
        data = read_json()
        base_url = get_base_url(data)

        model = text(data.get("model")) or "agnes-image-2.5-flash"
        prompt = text(data.get("prompt"))
        negative_prompt = text(data.get("negative_prompt"))
        size = text(data.get("size")) or "1K"
        ratio = text(data.get("ratio")) or "1:1"

        response_format = text(first_value(
            data.get("response_format"),
            data.get("format"),
        )) or "url"

        if not prompt:
            raise InputError("Görsel açıklaması boş olamaz.")

        if response_format not in {"url", "b64_json"}:
            raise InputError("response_format url veya b64_json olmalıdır.")

        references = media_from(data, "images", "single_image", "ref_image")

        payload = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "ratio": ratio,
            "extra_body": {"response_format": response_format},
        }

        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
            payload["extra_body"]["negative_prompt"] = negative_prompt

        numeric_values = {
            "seed": number(data.get("seed"), "seed", int),
            "guidance_scale": number(
                data.get("guidance_scale"), "guidance_scale", float
            ),
            "num_inference_steps": number(
                first_value(data.get("steps"), data.get("num_inference_steps")),
                "steps",
                int,
            ),
            "image_strength": number(
                first_value(data.get("image_strength"), data.get("strength")),
                "image_strength",
                float,
            ),
        }

        for key, value in numeric_values.items():
            if value is not None:
                payload[key] = value
                payload["extra_body"][key] = value

        if references:
            payload["extra_body"]["image"] = references

        if response_format == "b64_json":
            payload["return_base64"] = True

        resp = upstream_request(
            "POST",
            f"{base_url}/images/generations",
            get_api_key(data),
            json=payload,
            timeout=(10, 180),
        )

        body, raw = finish(resp, start, endpoint, payload)
        result_url = None

        if 200 <= resp.status_code < 300 and isinstance(raw, dict):
            items = raw.get("data") or []
            if isinstance(items, list) and items:
                item = items[0]
                if isinstance(item, dict):
                    result_url = item.get("url")
                    if not result_url and item.get("b64_json"):
                        result_url = (
                            "data:image/png;base64," + item["b64_json"]
                        )

        def stored_number(name, default=""):
            value = numeric_values[name]
            return str(value) if value is not None else default

        update_section("image", {
            "model": model,
            "mode": "i2i" if references else "t2i",
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "ref_image": references[0] if references else None,
            "size": size,
            "ratio": ratio,
            "format": response_format,
            "seed": stored_number("seed"),
            "guidance_scale": stored_number("guidance_scale", "7.5"),
            "steps": stored_number("num_inference_steps", "30"),
            "image_strength": stored_number("image_strength", "0.8"),
            "result_url": result_url,
            "has_result": bool(result_url),
        })

        return jsonify(body), resp.status_code
    except Exception as exc:
        return error_result(exc, start, endpoint, payload)


# ============================================================
# 4. VİDEO GÖREVİ OLUŞTURMA
# ============================================================

@app.route("/api/video/create", methods=["POST"])
def create_video():
    start = time.perf_counter()
    endpoint = "POST /v1/videos"
    payload = None

    try:
        data = read_json()
        base_url = get_base_url(data)

        payload, metadata, calculation = build_video_payload(data)

        app.logger.info(
            "Video: model=%s requested=%s fps=%s frames=%s estimated=%s",
            payload["model"],
            calculation["requested_seconds"],
            calculation["frame_rate"],
            calculation["num_frames"],
            calculation["estimated_seconds"],
        )

        # POST otomatik tekrar edilmez; çift ücret/görev oluşmasın.
        resp = upstream_request(
            "POST",
            f"{base_url}/videos",
            get_api_key(data),
            json=payload,
            timeout=(10, 120),
        )

        body, raw = finish(resp, start, endpoint, payload)
        body["duration_calculation"] = calculation

        provider_video_id = None
        provider_task_id = None

        if 200 <= resp.status_code < 300 and not upstream_failed(raw):
            provider_video_id = response_value(raw, "video_id")
            provider_task_id = response_value(raw, "task_id", "id")

        if provider_video_id is not None:
            provider_video_id = str(provider_video_id)
        if provider_task_id is not None:
            provider_task_id = str(provider_task_id)

        tracking_id = provider_video_id or provider_task_id
        query_kind = "video" if provider_video_id else "task"

        actual_seconds = reported_duration(raw)
        actual_size = response_value(raw, "size")
        result_url = video_result_url(raw)

        status = video_status(raw) if tracking_id else "failed"
        progress = response_value(raw, "progress")
        if progress is None:
            progress = 100 if status == "completed" else 0

        update_section("video", {
            **metadata,
            # task_id ön yüzün takip kimliği olarak korunur.
            "task_id": tracking_id,
            "video_id": provider_video_id,
            "provider_task_id": provider_task_id,
            "query_kind": query_kind,
            "base_url": base_url,
            "status": status,
            "progress": progress,
            "url": result_url,
            "duration_seconds": (
                actual_seconds if actual_seconds is not None else 0
            ),
            "actual_size": actual_size,
            "has_task": bool(tracking_id),
        })

        body["actual_seconds"] = actual_seconds
        body["actual_size"] = actual_size

        warning = duration_warning(
            calculation["requested_seconds"],
            actual_seconds,
            calculation["frame_rate"],
        )
        if warning:
            body["warning"] = warning

        if tracking_id:
            task_info = {
                "model": metadata["model"],
                "base_url": base_url,
                "video_id": provider_video_id,
                "provider_task_id": provider_task_id,
                "requested_seconds": calculation["requested_seconds"],
                "fps": calculation["frame_rate"],
            }

            with STATE_LOCK:
                for identifier in (provider_video_id, provider_task_id):
                    if identifier:
                        VIDEO_TASKS[identifier] = copy.deepcopy(task_info)

            body["task_id"] = tracking_id
            body["video_id"] = provider_video_id
            body["provider_task_id"] = provider_task_id
            body["tracking_id"] = tracking_id
            body["query_kind"] = query_kind
            body["task_status"] = status
            body["video_url"] = result_url

            return jsonify(body), resp.status_code

        if 200 <= resp.status_code < 300:
            body["status"] = 502
            body["error"] = (
                error_text(raw)
                if upstream_failed(raw)
                else (
                    "API yanıtında görev kimliği bulunamadı. "
                    "Görev başlamış olabilir; yeniden göndermeden "
                    "önce raw_response alanını kontrol edin."
                )
            )
            return jsonify(body), 502

        body["error"] = error_text(raw)
        return jsonify(body), resp.status_code

    except Exception as exc:
        return error_result(exc, start, endpoint, payload)


# ============================================================
# 5. VİDEO DURUM SORGULAMA
# ============================================================

@app.route("/api/video/status", methods=["GET"])
def check_video_status():
    start = time.perf_counter()
    endpoint = "GET /agnesapi"

    try:
        identifier = text(first_value(
            request.args.get("video_id"),
            request.args.get("task_id"),
        ))

        if not identifier:
            raise InputError("video_id veya task_id gereklidir.")

        with STATE_LOCK:
            saved = copy.deepcopy(RUNTIME_STATE["video"])
            task_info = copy.deepcopy(VIDEO_TASKS.get(identifier, {}))

        saved_matches = identifier in {
            saved.get("task_id"),
            saved.get("video_id"),
            saved.get("provider_task_id"),
        }

        model_name = (
            text(request.args.get("model_name"))
            or task_info.get("model")
            or (saved.get("model") if saved_matches else None)
            or DEFAULT_VIDEO_MODEL
        )

        api_key = get_api_key({
            "api_key": request.args.get("api_key"),
        })

        provider_video_id = task_info.get("video_id")
        provider_task_id = task_info.get("provider_task_id")

        if not task_info and saved_matches:
            provider_video_id = saved.get("video_id")
            provider_task_id = saved.get("provider_task_id")

        # video_id mevcutsa önerilen sorgu endpoint'i kullanılır.
        # Sadece task_id varsa legacy endpoint kullanılır.
        if provider_video_id:
            use_task_query = False
            query_id = provider_video_id
        elif provider_task_id:
            use_task_query = True
            query_id = provider_task_id
        else:
            use_task_query = (
                text(request.args.get("query_kind")) == "task"
                or identifier.startswith("task_")
                or (
                    not text(request.args.get("video_id"))
                    and bool(text(request.args.get("task_id")))
                )
            )
            query_id = identifier

        if use_task_query:
            base_url = get_base_url({
                "base_url": (
                    text(request.args.get("base_url"))
                    or task_info.get("base_url")
                    or (saved.get("base_url") if saved_matches else None)
                )
            })

            endpoint = "GET /v1/videos/{task_id}"
            resp = upstream_request(
                "GET",
                f"{base_url}/videos/{quote(query_id, safe='')}",
                api_key,
                timeout=(10, 30),
            )
        else:
            resp = upstream_request(
                "GET",
                VIDEO_STATUS_URL,
                api_key,
                params={
                    "video_id": query_id,
                    "model_name": model_name,
                },
                timeout=(10, 30),
            )

        body, raw = finish(resp, start, endpoint)

        if 200 <= resp.status_code < 300 and isinstance(raw, dict):
            status = video_status(raw, default="in_progress")
            progress = response_value(raw, "progress")
            result_url = video_result_url(raw)
            duration = reported_duration(raw)
            actual_size = response_value(raw, "size")

            if progress is None:
                progress = 100 if status == "completed" else 0

            updates = {
                "status": status,
                "progress": progress,
                "has_task": True,
            }

            if status == "completed" and result_url:
                updates["url"] = result_url

            if duration is not None:
                updates["duration_seconds"] = duration

            if actual_size is not None:
                updates["actual_size"] = actual_size

            # Eski görevin sonucu yeni görevin state'ini ezmesin.
            with STATE_LOCK:
                current = RUNTIME_STATE["video"]
                current_ids = {
                    current.get("task_id"),
                    current.get("video_id"),
                    current.get("provider_task_id"),
                }

                if identifier in current_ids:
                    current.update(updates)

            body["task_status"] = status
            body["progress"] = progress
            body["video_url"] = result_url
            body["duration_seconds"] = duration
            body["actual_seconds"] = duration
            body["actual_size"] = actual_size

            requested = task_info.get("requested_seconds")
            fps = task_info.get("fps")

            if requested is None and saved_matches:
                requested = safe_float(saved.get("seconds"))
                fps = safe_float(saved.get("fps"))

            warning = duration_warning(requested, duration, fps)
            if warning:
                body["warning"] = warning

            if status == "failed":
                body["error"] = error_text(raw)

        elif not 200 <= resp.status_code < 300:
            body["error"] = error_text(raw)

        return jsonify(body), resp.status_code

    except Exception as exc:
        return error_result(exc, start, endpoint)


@app.errorhandler(413)
def request_too_large(_exc):
    return jsonify({
        "status": 413,
        "error": (
            "İstek boyutu 32 MB sınırını aşıyor. "
            "Dosyayı küçültün veya erişilebilir bir URL kullanın."
        ),
    }), 413


# ============================================================
# BAŞLATMA
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))

    print("=" * 60)
    print("Agnes AI Full Suite Web Studio başlatılıyor...")
    print(f"Adres: http://127.0.0.1:{port}")
    print("=" * 60)

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
