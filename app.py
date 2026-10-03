import os
import json
import time
import base64
import requests
import urllib3
from flask import Flask, render_template, request, jsonify, send_from_directory

# SSL uyarılarını bastır (apihub sertifika kontrollerinde oluşabilecek sorunları önler)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__, template_folder="templates", static_folder="static")

DEFAULT_API_KEY = os.environ.get("AGNES_API_KEY", "sk-niO32ft0zOmYOJSp0OIbPwSKdxVdBupsF6AmB0slcZMUwQjv")
DEFAULT_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")

# ==============================================================================
# GEÇİCİ RUNTIME (RAM) BELLEK DEPOSU
# Sayfa yenilenince (F5) veriler kaybolmaz, fakat Render servisi restart edildiğinde
# veya sunucu baştan başlatıldığında bellek otomatik temizlenir.
# ==============================================================================
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
                "reasoning_tokens": 0
            },
            "has_output": False
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
            "has_result": False
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
            "has_task": False
        },
        "last_log": {
            "status": None,
            "duration_ms": 0,
            "endpoint": "",
            "request_payload": None,
            "raw_response": None
        }
    }

RUNTIME_STATE = get_default_state()

def get_headers(api_key=None):
    key = api_key.strip() if api_key and api_key.strip() else DEFAULT_API_KEY
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/index.html")
def index_file():
    return render_template("index.html")

# 0. Runtime Durum Yönetimi (State Restore / Sync / Reset)
@app.route("/api/state", methods=["GET", "POST"])
def api_state():
    global RUNTIME_STATE
    if request.method == "POST":
        data = request.json or {}
        for key, val in data.items():
            if isinstance(val, dict) and key in RUNTIME_STATE and isinstance(RUNTIME_STATE[key], dict):
                RUNTIME_STATE[key].update(val)
            else:
                RUNTIME_STATE[key] = val
        return jsonify({"status": 200, "state": RUNTIME_STATE})
    return jsonify({"status": 200, "state": RUNTIME_STATE})

@app.route("/api/state/reset", methods=["POST"])
def api_state_reset():
    global RUNTIME_STATE
    RUNTIME_STATE = get_default_state()
    return jsonify({"status": 200, "state": RUNTIME_STATE})

# 1. Canlı Model Listesi Alma
@app.route("/api/models", methods=["GET", "POST"])
def get_models():
    api_key = request.headers.get("X-Api-Key") or (request.json.get("api_key") if request.is_json else None)
    base_url = (request.json.get("base_url") if request.is_json else None) or DEFAULT_BASE_URL
    start_time = time.time()
    try:
        url = f"{base_url.rstrip('/')}/models"
        resp = requests.get(url, headers=get_headers(api_key), timeout=15, verify=False)
        duration = round((time.time() - start_time) * 1000)
        data = resp.json() if resp.status_code == 200 else resp.text
        
        RUNTIME_STATE["last_log"] = {
            "status": resp.status_code,
            "duration_ms": duration,
            "endpoint": "GET /v1/models",
            "raw_response": data
        }

        return jsonify({
            "status": resp.status_code,
            "duration_ms": duration,
            "endpoint": "GET /v1/models",
            "data": data
        }), resp.status_code
    except Exception as e:
        duration = round((time.time() - start_time) * 1000)
        err_data = {"error": str(e)}
        RUNTIME_STATE["last_log"] = {
            "status": 500,
            "duration_ms": duration,
            "endpoint": "GET /v1/models",
            "raw_response": err_data
        }
        return jsonify({
            "status": 500,
            "duration_ms": duration,
            "endpoint": "GET /v1/models",
            "error": str(e)
        }), 500

# 2. Metin & Çok Modlu (Chat / Multimodal) Uç Noktası
@app.route("/api/chat", methods=["POST"])
def chat():
    start_time = time.time()
    try:
        data = request.json or {}
        api_key = data.get("api_key")
        base_url = data.get("base_url") or DEFAULT_BASE_URL
        
        model = data.get("model", "agnes-3.0-flash")
        messages = []
        
        system_prompt = data.get("system_prompt", "").strip()
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
            
        user_prompt = data.get("prompt", "").strip()
        image_data = data.get("image") # URL veya Base64 Data URI
        
        if image_data:
            user_content = [
                {"type": "text", "text": user_prompt if user_prompt else "Describe or analyze this image in detail."}
            ]
            img_url = image_data if image_data.startswith("http") or image_data.startswith("data:") else f"data:image/jpeg;base64,{image_data}"
            user_content.append({
                "type": "image_url",
                "image_url": {"url": img_url}
            })
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": float(data.get("temperature", 0.7)),
            "top_p": float(data.get("top_p", 0.9)),
            "max_tokens": int(data.get("max_tokens", 2048))
        }

        if data.get("presence_penalty") is not None and str(data.get("presence_penalty")).strip() != "":
            try:
                payload["presence_penalty"] = float(data.get("presence_penalty"))
            except:
                pass

        if data.get("frequency_penalty") is not None and str(data.get("frequency_penalty")).strip() != "":
            try:
                payload["frequency_penalty"] = float(data.get("frequency_penalty"))
            except:
                pass

        # Thinking modu
        if data.get("enable_thinking"):
            payload["chat_template_kwargs"] = {"enable_thinking": True}

        url = f"{base_url.rstrip('/')}/chat/completions"
        resp = requests.post(url, headers=get_headers(api_key), json=payload, timeout=60, verify=False)
        duration = round((time.time() - start_time) * 1000)
        
        try:
            resp_json = resp.json()
        except:
            resp_json = {"raw_text": resp.text}

        # Runtime State Güncellemesi
        chat_output_text = ""
        prompt_tokens = 0
        comp_tokens = 0
        reasoning_tokens = 0
        if resp.status_code == 200 and isinstance(resp_json, dict) and "choices" in resp_json and resp_json["choices"]:
            chat_output_text = resp_json["choices"][0].get("message", {}).get("content", "")
            usage = resp_json.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            comp_tokens = usage.get("completion_tokens", 0)
            reasoning_tokens = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
        elif resp.status_code != 200:
            chat_output_text = f"Hata ({resp.status_code}):\n{json.dumps(resp_json, indent=2, ensure_ascii=False)}"

        RUNTIME_STATE["active_tab"] = "chat"
        RUNTIME_STATE["chat"].update({
            "model": model,
            "system_prompt": system_prompt,
            "prompt": user_prompt,
            "temperature": payload["temperature"],
            "top_p": payload["top_p"],
            "max_tokens": payload["max_tokens"],
            "presence_penalty": payload.get("presence_penalty", 0.0),
            "frequency_penalty": payload.get("frequency_penalty", 0.0),
            "enable_thinking": bool(data.get("enable_thinking")),
            "image": image_data if image_data else None,
            "output": chat_output_text,
            "stats": {
                "duration_ms": duration,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": comp_tokens,
                "reasoning_tokens": reasoning_tokens
            },
            "has_output": True
        })
        RUNTIME_STATE["last_log"] = {
            "status": resp.status_code,
            "duration_ms": duration,
            "endpoint": "POST /v1/chat/completions",
            "request_payload": payload,
            "raw_response": resp_json
        }

        return jsonify({
            "status": resp.status_code,
            "duration_ms": duration,
            "endpoint": "POST /v1/chat/completions",
            "request_payload": payload,
            "raw_response": resp_json
        }), resp.status_code
    except Exception as e:
        duration = round((time.time() - start_time) * 1000)
        err_json = {"error": str(e)}
        RUNTIME_STATE["last_log"] = {
            "status": 500,
            "duration_ms": duration,
            "endpoint": "POST /v1/chat/completions",
            "raw_response": err_json
        }
        return jsonify({
            "status": 500,
            "duration_ms": duration,
            "endpoint": "POST /v1/chat/completions",
            "error": str(e)
        }), 500

# 3. Görsel Üretimi (Image Generation & Editing) Uç Noktası
@app.route("/api/image", methods=["POST"])
def generate_image():
    start_time = time.time()
    try:
        data = request.json or {}
        api_key = data.get("api_key")
        base_url = data.get("base_url") or DEFAULT_BASE_URL
        
        model = data.get("model", "agnes-image-2.5-flash")
        prompt = data.get("prompt", "").strip()
        negative_prompt = data.get("negative_prompt", "").strip()
        size = data.get("size", "1K")
        ratio = data.get("ratio", "1:1")
        response_format = data.get("response_format", "url")
        seed = data.get("seed")
        guidance_scale = data.get("guidance_scale")
        steps = data.get("steps") or data.get("num_inference_steps")
        image_strength = data.get("image_strength") or data.get("strength")
        
        payload = {
            "model": model,
            "prompt": prompt,
            "size": size,
            "ratio": ratio,
            "extra_body": {
                "response_format": response_format
            }
        }

        # Negatif Prompt
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
            payload["extra_body"]["negative_prompt"] = negative_prompt

        # Seed (Tohum)
        if seed is not None and str(seed).strip() != "":
            try:
                s_int = int(seed)
                payload["seed"] = s_int
                payload["extra_body"]["seed"] = s_int
            except:
                pass

        # Guidance Scale (CFG)
        if guidance_scale is not None and str(guidance_scale).strip() != "":
            try:
                g_val = float(guidance_scale)
                payload["guidance_scale"] = g_val
                payload["extra_body"]["guidance_scale"] = g_val
            except:
                pass

        # Steps
        if steps is not None and str(steps).strip() != "":
            try:
                st_val = int(steps)
                payload["num_inference_steps"] = st_val
                payload["extra_body"]["num_inference_steps"] = st_val
            except:
                pass

        # Image Strength
        if image_strength is not None and str(image_strength).strip() != "":
            try:
                str_val = float(image_strength)
                payload["image_strength"] = str_val
                payload["extra_body"]["image_strength"] = str_val
            except:
                pass
        
        # Referans Görseller
        reference_images = data.get("images", [])
        if reference_images and isinstance(reference_images, list) and len(reference_images) > 0:
            payload["extra_body"]["image"] = reference_images
        elif data.get("single_image"):
            payload["extra_body"]["image"] = [data.get("single_image")]

        if response_format == "b64_json":
            payload["return_base64"] = True

        url = f"{base_url.rstrip('/')}/images/generations"
        resp = requests.post(url, headers=get_headers(api_key), json=payload, timeout=90, verify=False)
        duration = round((time.time() - start_time) * 1000)
        
        try:
            resp_json = resp.json()
        except:
            resp_json = {"raw_text": resp.text}

        # Runtime State Güncellemesi
        result_url = None
        if resp.status_code == 200 and isinstance(resp_json, dict) and "data" in resp_json and resp_json["data"]:
            item = resp_json["data"][0]
            result_url = item.get("url") or (f"data:image/png;base64,{item.get('b64_json')}" if item.get("b64_json") else None)

        RUNTIME_STATE["active_tab"] = "image"
        RUNTIME_STATE["image"].update({
            "model": model,
            "mode": "i2i" if (payload.get("extra_body", {}).get("image")) else "t2i",
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "ref_image": data.get("single_image") or (reference_images[0] if reference_images else None),
            "size": size,
            "ratio": ratio,
            "format": response_format,
            "seed": str(seed) if seed is not None else "",
            "guidance_scale": str(guidance_scale) if guidance_scale is not None else "7.5",
            "steps": str(steps) if steps is not None else "30",
            "image_strength": str(image_strength) if image_strength is not None else "0.8",
            "result_url": result_url,
            "has_result": result_url is not None
        })
        RUNTIME_STATE["last_log"] = {
            "status": resp.status_code,
            "duration_ms": duration,
            "endpoint": "POST /v1/images/generations",
            "request_payload": payload,
            "raw_response": resp_json
        }

        return jsonify({
            "status": resp.status_code,
            "duration_ms": duration,
            "endpoint": "POST /v1/images/generations",
            "request_payload": payload,
            "raw_response": resp_json
        }), resp.status_code
    except Exception as e:
        duration = round((time.time() - start_time) * 1000)
        err_json = {"error": str(e)}
        RUNTIME_STATE["last_log"] = {
            "status": 500,
            "duration_ms": duration,
            "endpoint": "POST /v1/images/generations",
            "raw_response": err_json
        }
        return jsonify({
            "status": 500,
            "duration_ms": duration,
            "endpoint": "POST /v1/images/generations",
            "error": str(e)
        }), 500

# 4. Video Görevi Oluşturma (Create Video Task)
@app.route("/api/video/create", methods=["POST"])
def create_video():
    start_time = time.time()
    try:
        data = request.json or {}
        api_key = data.get("api_key")
        base_url = data.get("base_url") or DEFAULT_BASE_URL
        
        model = data.get("model", "agnes-video-2.5-flash")
        prompt = data.get("prompt", "").strip()
        negative_prompt = data.get("negative_prompt", "").strip()
        raw_mode = data.get("mode", "text")
        seconds = str(data.get("seconds", "4"))
        size = data.get("size", "720P")
        aspect_ratio = data.get("aspect_ratio", "16:9")
        seed = data.get("seed")
        fps = data.get("fps")
        
        # agnes-video-v2.0 için mode formatı ('ti2vid', 'keyframes', 'multi_reference')
        # agnes-video-2.5 / flash için mode formatı ('text', 'keyframe', 'reference')
        if "v2" in model.lower() or "v2.0" in model.lower():
            mode_map = {
                "text": "ti2vid",
                "keyframe": "keyframes",
                "reference": "multi_reference",
                "ti2vid": "ti2vid",
                "keyframes": "keyframes",
                "multi_reference": "multi_reference"
            }
            mode = mode_map.get(raw_mode, raw_mode)
        else:
            mode_map = {
                "ti2vid": "text",
                "keyframes": "keyframe",
                "multi_reference": "reference",
                "text": "text",
                "keyframe": "keyframe",
                "reference": "reference"
            }
            mode = mode_map.get(raw_mode, raw_mode)

        payload = {
            "model": model,
            "prompt": prompt,
            "mode": mode,
            "seconds": seconds,
            "size": size,
            "aspect_ratio": aspect_ratio
        }

        # Negatif Prompt
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        
        if seed is not None and str(seed).strip() != "":
            try:
                payload["seed"] = int(seed)
            except:
                pass

        if fps is not None and str(fps).strip() != "":
            try:
                payload["fps"] = int(fps)
            except:
                pass
                
        # Mod spesifik parametreler
        if mode in ("keyframe", "keyframes"):
            if data.get("first_frame"):
                payload["first_frame"] = data.get("first_frame")
            if data.get("last_frame"):
                payload["last_frame"] = data.get("last_frame")
        elif mode in ("reference", "multi_reference"):
            if data.get("images") and isinstance(data.get("images"), list):
                payload["images"] = data.get("images")
            elif data.get("ref_images"):
                payload["images"] = [img.strip() for img in str(data.get("ref_images")).split(",") if img.strip()]

            if data.get("audios") and isinstance(data.get("audios"), list):
                payload["audios"] = data.get("audios")
            elif data.get("ref_audios"):
                payload["audios"] = [aud.strip() for aud in str(data.get("ref_audios")).split(",") if aud.strip()]

            if data.get("videos") and isinstance(data.get("videos"), list):
                payload["videos"] = data.get("videos")
            elif data.get("ref_videos"):
                v_list = [v.strip() for v in str(data.get("ref_videos")).split(",") if v.strip()]
                if v_list:
                    payload["videos"] = [{"url": v} for v in v_list]

        url = f"{base_url.rstrip('/')}/videos"
        resp = requests.post(url, headers=get_headers(api_key), json=payload, timeout=60, verify=False)
        duration = round((time.time() - start_time) * 1000)
        
        try:
            resp_json = resp.json()
        except:
            resp_json = {"raw_text": resp.text}

        # Runtime State Güncellemesi
        video_id = None
        if resp.status_code == 200 and isinstance(resp_json, dict):
            video_id = resp_json.get("video_id") or resp_json.get("id")

        RUNTIME_STATE["active_tab"] = "video"
        RUNTIME_STATE["video"].update({
            "model": model,
            "mode": raw_mode,
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "seconds": seconds,
            "size": size,
            "aspect_ratio": aspect_ratio,
            "seed": str(seed) if seed is not None else "",
            "fps": str(fps) if fps is not None else "",
            "first_frame": data.get("first_frame", ""),
            "last_frame": data.get("last_frame", ""),
            "ref_images": ",".join(data.get("images", [])) if isinstance(data.get("images"), list) else (data.get("ref_images") or ""),
            "ref_audios": ",".join(data.get("audios", [])) if isinstance(data.get("audios"), list) else (data.get("ref_audios") or ""),
            "ref_videos": ",".join(data.get("videos", [])) if isinstance(data.get("videos"), list) else (data.get("ref_videos") or ""),
            "task_id": video_id,
            "status": "queued" if video_id else "failed",
            "progress": 0,
            "url": None,
            "has_task": video_id is not None
        })
        RUNTIME_STATE["last_log"] = {
            "status": resp.status_code,
            "duration_ms": duration,
            "endpoint": "POST /v1/videos",
            "request_payload": payload,
            "raw_response": resp_json
        }

        return jsonify({
            "status": resp.status_code,
            "duration_ms": duration,
            "endpoint": "POST /v1/videos",
            "request_payload": payload,
            "raw_response": resp_json
        }), resp.status_code
    except Exception as e:
        duration = round((time.time() - start_time) * 1000)
        err_json = {"error": str(e)}
        RUNTIME_STATE["last_log"] = {
            "status": 500,
            "duration_ms": duration,
            "endpoint": "POST /v1/videos",
            "raw_response": err_json
        }
        return jsonify({
            "status": 500,
            "duration_ms": duration,
            "endpoint": "POST /v1/videos",
            "error": str(e)
        }), 500

# 5. Video Görev Durumunu Sorgulama (Retrieve Video Task)
@app.route("/api/video/status", methods=["GET"])
def check_video_status():
    start_time = time.time()
    try:
        video_id = request.args.get("video_id")
        model_name = request.args.get("model_name", "agnes-video-2.5-flash")
        api_key = request.headers.get("X-Api-Key") or request.args.get("api_key")
        
        if not video_id:
            return jsonify({"status": 400, "error": "video_id is required"}), 400

        # Video durum sorgulama adresi: https://apihub.agnes-ai.com/agnesapi?video_id=...&model_name=...
        url = f"https://apihub.agnes-ai.com/agnesapi?video_id={video_id}&model_name={model_name}"
        resp = requests.get(url, headers=get_headers(api_key), timeout=20, verify=False)
        duration = round((time.time() - start_time) * 1000)
        
        try:
            resp_json = resp.json()
        except:
            resp_json = {"raw_text": resp.text}

        # Runtime State Güncellemesi
        if resp.status_code == 200 and isinstance(resp_json, dict):
            status = resp_json.get("status", "in_progress")
            progress = resp_json.get("progress", 0)
            video_url = resp_json.get("url")
            
            RUNTIME_STATE["video"]["task_id"] = video_id
            RUNTIME_STATE["video"]["status"] = status
            RUNTIME_STATE["video"]["progress"] = progress
            if status == "completed" and video_url:
                RUNTIME_STATE["video"]["url"] = video_url
            RUNTIME_STATE["video"]["has_task"] = True

        RUNTIME_STATE["last_log"] = {
            "status": resp.status_code,
            "duration_ms": duration,
            "endpoint": f"GET /agnesapi?video_id={video_id}",
            "raw_response": resp_json
        }

        return jsonify({
            "status": resp.status_code,
            "duration_ms": duration,
            "endpoint": "GET /agnesapi",
            "raw_response": resp_json
        }), resp.status_code
    except Exception as e:
        duration = round((time.time() - start_time) * 1000)
        err_json = {"error": str(e)}
        RUNTIME_STATE["last_log"] = {
            "status": 500,
            "duration_ms": duration,
            "endpoint": f"GET /agnesapi?video_id={request.args.get('video_id', '')}",
            "raw_response": err_json
        }
        return jsonify({
            "status": 500,
            "duration_ms": duration,
            "endpoint": "GET /agnesapi",
            "error": str(e)
        }), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    print(" Agnes AI Full Suite Web Studio Başlatılıyor...")
    print(f" Adres: http://127.0.0.1:{port}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
