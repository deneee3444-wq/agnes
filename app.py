import copy
import json
import logging
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

# API anahtarını kaynak koda yazmayın.
DEFAULT_API_KEY = os.environ.get("AGNES_API_KEY", "sk-niO32ft0zOmYOJSp0OIbPwSKdxVdBupsF6AmB0slcZMUwQjv").strip()

DEFAULT_BASE_URL = os.environ.get(
    "AGNES_BASE_URL",
    "https://apihub.agnes-ai.com/v1",
).rstrip("/")

VIDEO_STATUS_URL = os.environ.get(
    "AGNES_VIDEO_STATUS_URL",
    "https://apihub.agnes-ai.com/agnesapi",
)

# İstemcinin sunucuyu rastgele URL'lere istek atmak için kullanmasını
# engellemek amacıyla izin verilen API kök adresleri.
# Birden fazla adres için virgülle ayrılmış değer girilebilir.
ALLOWED_BASE_URLS = {
    value.strip().rstrip("/")
    for value in os.environ.get(
        "AGNES_ALLOWED_BASE_URLS",
        DEFAULT_BASE_URL,
    ).split(",")
    if value.strip()
}

STATE_LOCK = threading.RLock()


# ============================================================
# RAM BELLEK DEPOSU
#
# Sayfa yenilenmesinde korunur.
# Sunucu yeniden başladığında temizlenir.
# Her sunucu işleminin kendi belleği vardır.
# Bu örnekte durum tüm ziyaretçiler arasında ortaktır.
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
            "model": "agnes-video-2.5-flash",
            "mode": "text",
            "prompt": "",
            "negative_prompt": "",
            "first_frame": "",
            "last_frame": "",
            "ref_images": "",
            "ref_audios": "",
            "ref_videos": "",
            "seconds": "4",
            "aspect_ratio": "16:9",
            "size": "720P",
            "seed": "",
            "fps": "",
            "task_id": None,
            "status": None,
            "progress": 0,
            "url": None,
            "duration_seconds": 0,
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
    """0 ve False değerlerini kaybetmeden ilk dolu değeri seç."""
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

    # NaN ve Infinity JSON/API sorunlarına yol açabilir.
    if isinstance(result, float):
        if result != result or result in (float("inf"), float("-inf")):
            raise InputError(f"{name} sonlu bir sayı olmalıdır.")

    return result


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
            "Gerekirse sunucudaki AGNES_ALLOWED_BASE_URLS "
            "ortam değişkenini düzenleyin."
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
    """Yalnızca mevcut state alanlarını, tiplerini koruyarak güncelle."""
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
    # verify=False kullanılmıyor: sertifika doğrulaması açık.
    # Redirect kapalı: kimlik bilgileri başka hedefe taşınmasın.
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

    save_log(
        resp.status_code,
        duration,
        endpoint,
        payload,
        response,
    )

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
        message = (
            "API sunucusunun TLS sertifikası doğrulanamadı. "
            "Sertifika zincirini veya sunucu CA yapılandırmasını kontrol edin."
        )
    elif isinstance(exc, requests.exceptions.Timeout):
        status = 504
        message = (
            "API isteği zaman aşımına uğradı. Video oluşturma isteğiyse "
            "görev sağlayıcıda başlamış olabilir; yeniden göndermeden "
            "önce sağlayıcıdaki görevleri kontrol edin."
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
    """
    URL metni, URL listesi veya {"url": "..."} listesi kabul eder.
    Data URI içindeki virgülü ayırmaz.
    """
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
    else:
        raise InputError(f"{field} bir metin veya liste olmalıdır.")

    result = []

    for item in items:
        if isinstance(item, dict):
            item = item.get("url")

        if not isinstance(item, str):
            raise InputError(
                f"{field} alanındaki her öğe bir URL metni "
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
    for layer in response_layers(raw):
        for key in keys:
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
    """İç içe JSON metinlerinden okunabilir hata mesajı çıkar."""
    def extract(value, depth=0):
        if depth > 6:
            return None

        if isinstance(value, dict):
            for key in ("error", "message", "detail", "raw_text"):
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
# SAYFALAR
# ============================================================

@app.route("/")
@app.route("/index.html")
def index():
    return render_template("index.html")


# ============================================================
# 0. STATE
# ============================================================

@app.route("/api/state", methods=["GET", "POST"])
def api_state():
    start = time.perf_counter()

    try:
        if request.method == "POST":
            data = read_json()
            with STATE_LOCK:
                merge_known_fields(RUNTIME_STATE, data)

        return jsonify({
            "status": 200,
            "state": state_snapshot(),
        })
    except Exception as exc:
        return error_result(exc, start, "/api/state")


@app.route("/api/state/reset", methods=["POST"])
def api_state_reset():
    global RUNTIME_STATE

    with STATE_LOCK:
        RUNTIME_STATE = get_default_state()

    return jsonify({
        "status": 200,
        "state": state_snapshot(),
    })


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

        body, _ = finish(
            resp,
            start,
            endpoint,
            response_key="data",
        )

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
            "top_p": number(
                data.get("top_p"), "top_p", float, 0.9
            ),
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

        response_format = text(
            first_value(data.get("response_format"), data.get("format"))
        ) or "url"

        if not prompt:
            raise InputError("Görsel açıklaması boş olamaz.")

        if response_format not in {"url", "b64_json"}:
            raise InputError("response_format url veya b64_json olmalıdır.")

        references = media_from(
            data, "images", "single_image", "ref_image"
        )

        # Görsel isteğinin mevcut alan yapısı korunuyor.
        payload = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "ratio": ratio,
            "extra_body": {
                "response_format": response_format,
            },
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
                first_value(
                    data.get("steps"),
                    data.get("num_inference_steps"),
                ),
                "steps",
                int,
            ),
            "image_strength": number(
                first_value(
                    data.get("image_strength"),
                    data.get("strength"),
                ),
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
                            "data:image/png;base64,"
                            + item["b64_json"]
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

        model = text(data.get("model")) or "agnes-video-2.5-flash"
        prompt = text(data.get("prompt"))
        negative_prompt = text(data.get("negative_prompt"))
        raw_mode = text(data.get("mode")) or "text"

        ui_modes = {
            "text": "text",
            "ti2vid": "text",
            "keyframe": "keyframe",
            "keyframes": "keyframe",
            "reference": "reference",
            "multi_reference": "reference",
        }

        if raw_mode not in ui_modes:
            raise InputError(f"Geçersiz video modu: {raw_mode}")

        ui_mode = ui_modes[raw_mode]

        # Mevcut kodunuzdaki model ailesi ayrımı korunuyor.
        is_v2 = "v2" in model.lower()

        if is_v2:
            mode = {
                "text": "ti2vid",
                "keyframe": "keyframes",
                "reference": "multi_reference",
            }[ui_mode]
        else:
            mode = ui_mode

        seconds = number(data.get("seconds"), "seconds", int, 4)
        if seconds <= 0:
            raise InputError("Video süresi sıfırdan büyük olmalıdır.")

        size = text(data.get("size")) or "720P"
        aspect_ratio = text(data.get("aspect_ratio")) or "16:9"

        first_frame = text(data.get("first_frame"))
        last_frame = text(data.get("last_frame"))

        images = media_from(data, "images", "image", "ref_images")
        audios = media_from(data, "audios", "ref_audios")
        videos = media_from(data, "videos", "ref_videos")

        payload = {
            "model": model,
            "prompt": prompt,
            "mode": mode,
            "seconds": str(seconds),
            "size": size,
            "aspect_ratio": aspect_ratio,
        }

        if negative_prompt:
            payload["negative_prompt"] = negative_prompt

        add_number(payload, data, "seed", int)
        add_number(payload, data, "fps", int)

        if ui_mode == "keyframe":
            # Öncelik arayüzdeki ilk/son kare alanlarında.
            # Bu alanlar boşsa görsel listesinden al.
            if not first_frame and images:
                first_frame = images[0]

            if not last_frame and len(images) >= 2:
                last_frame = images[1]

            if mode == "keyframes":
                # Ekrandaki hatanın asıl düzeltmesi:
                # keyframes -> image adlı alanda en az iki görsel.
                if not first_frame or not last_frame:
                    raise InputError(
                        "Bu modelin keyframes modu en az iki görsel "
                        "gerektiriyor. İlk kare ve son kare alanlarını "
                        "doldurun. Tek görselle bu mod başlatılamaz."
                    )

                payload["image"] = [first_frame, last_frame]

            else:
                # keyframe adlı diğer model modunda mevcut
                # first_frame / last_frame sözleşmesi korunuyor.
                if not first_frame:
                    raise InputError(
                        "Keyframe modu için ilk kare görseli gereklidir."
                    )

                payload["first_frame"] = first_frame

                if last_frame:
                    payload["last_frame"] = last_frame

        elif ui_mode == "reference":
            if not images and not audios and not videos:
                raise InputError(
                    "Referans modunda en az bir görsel, ses "
                    "veya video referansı ekleyin."
                )

            if images:
                payload["images"] = images

            if audios:
                payload["audios"] = audios

            if videos:
                payload["videos"] = [
                    {"url": url} for url in videos
                ]

        elif not prompt:
            raise InputError("Metinden video üretimi için açıklama girin.")

        resp = upstream_request(
            "POST",
            f"{base_url}/videos",
            get_api_key(data),
            json=payload,
            timeout=(10, 120),
        )

        body, raw = finish(resp, start, endpoint, payload)

        video_id = None

        if 200 <= resp.status_code < 300 and not upstream_failed(raw):
            video_id = response_value(raw, "video_id", "id", "task_id")

        if video_id is not None:
            video_id = str(video_id)

        update_section("video", {
            "model": model,
            "mode": ui_mode,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "seconds": str(seconds),
            "size": size,
            "aspect_ratio": aspect_ratio,
            "seed": text(data.get("seed")),
            "fps": text(data.get("fps")),
            "first_frame": first_frame,
            "last_frame": last_frame,
            "ref_images": ",".join(images),
            "ref_audios": ",".join(audios),
            "ref_videos": ",".join(videos),
            "task_id": video_id,
            "status": "queued" if video_id else "failed",
            "progress": 0,
            "url": None,
            "duration_seconds": 0,
            "has_task": bool(video_id),
        })

        if video_id:
            # Ön yüzün farklı görev kimliği alanlarını
            # okuyabilmesi için ek alanlar.
            body["task_id"] = video_id
            body["video_id"] = video_id

            # Ham API cevabı değiştirilmeden korunuyor.
            return jsonify(body), resp.status_code

        if 200 <= resp.status_code < 300:
            body["status"] = 502
            body["error"] = (
                error_text(raw)
                if upstream_failed(raw)
                else (
                    "API yanıtında görev kimliği bulunamadı. "
                    "raw_response alanını kontrol edin. "
                    "Görev başlamış olabileceği için hemen yeniden göndermeyin."
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
        video_id = text(request.args.get("video_id"))

        if not video_id:
            raise InputError("video_id gereklidir.")

        with STATE_LOCK:
            saved_model = RUNTIME_STATE["video"]["model"]

        model_name = (
            text(request.args.get("model_name"))
            or saved_model
            or "agnes-video-2.5-flash"
        )

        # Eski ön yüzlerle uyumluluk için query parametresi korunuyor.
        # Tercihen X-Api-Key başlığı kullanın.
        api_key = get_api_key({
            "api_key": request.args.get("api_key"),
        })

        resp = upstream_request(
            "GET",
            VIDEO_STATUS_URL,
            api_key,
            params={
                "video_id": video_id,
                "model_name": model_name,
            },
            timeout=(10, 30),
        )

        body, raw = finish(
            resp,
            start,
            endpoint,
        )

        if 200 <= resp.status_code < 300 and isinstance(raw, dict):
            raw_status = text(response_value(raw, "status")).lower()

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

            status = aliases.get(
                raw_status,
                raw_status or "in_progress",
            )

            if upstream_failed(raw):
                status = "failed"

            progress = response_value(raw, "progress")
            video_url = response_value(raw, "url", "video_url")

            updates = {
                "task_id": video_id,
                "status": status,
                "progress": progress if progress is not None else 0,
                "has_task": True,
            }

            if status == "completed" and video_url:
                updates["url"] = video_url
                if progress is None:
                    updates["progress"] = 100

            duration = response_value(
                raw,
                "duration_seconds",
                "duration",
            )
            if duration is not None:
                updates["duration_seconds"] = duration

            # Eski bir görevin sorgusu yeni görevin durumunu ezmesin.
            with STATE_LOCK:
                current_id = RUNTIME_STATE["video"].get("task_id")

                if current_id in (None, video_id):
                    RUNTIME_STATE["video"].update(updates)

            body["task_status"] = status
            body["progress"] = updates["progress"]
            body["video_url"] = video_url

            if status == "failed":
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
