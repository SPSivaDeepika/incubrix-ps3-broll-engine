"""IncuBrix PS3 starter: script/audio -> scene-timed video job API.

This is an MVP scaffold. Without PEXELS_API_KEY it uses generated title-card visuals,
not stock B-roll; the manifest explicitly warns about this limitation.
"""
from __future__ import annotations
import hashlib, json, logging, os, re, shutil, socket, subprocess, time, uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import requests
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from PIL import Image, ImageDraw, ImageFont

BASE = Path(os.getenv("DATA_DIR", "data")).resolve()
JOBS = BASE / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
BROLL_DIR = Path(__file__).resolve().parent.parent / "assets" / "broll"
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(30 * 1024 * 1024)))
FFMPEG = os.getenv("FFMPEG_PATH", "ffmpeg")
ESPEAK = os.getenv("ESPEAK_PATH", "espeak-ng")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(message)s")
log = logging.getLogger("broll")

app = FastAPI(title="IncuBrix Script and Voiceover to B-roll Engine", version="0.1.0", openapi_url="/api/v1/openapi.json")
JOBS_STATE: dict[str, dict[str, Any]] = {}
IDEMPOTENCY: dict[str, str] = {}

class Scene(BaseModel):
    text: str = Field(min_length=1, max_length=1500)
    visual_hint: str | None = Field(default=None, max_length=200)

class CaptionSettings(BaseModel):
    burn_in: bool = True
    srt: bool = True

class JobRequest(BaseModel):
    audio_url: str | None = None
    script: str | None = Field(default=None, max_length=12000)
    scenes: list[Scene] | None = None
    voice: str = "en-IN-female"
    aspect_ratios: list[str] = Field(default_factory=lambda: ["16:9"])
    style: str = "bright, modern, real-world footage"
    avoid: list[str] = Field(default_factory=list)
    captions: CaptionSettings = Field(default_factory=CaptionSettings)

    @model_validator(mode="after")
    def validate_input(self):
        if not self.audio_url and not (self.script and self.script.strip()) and not self.scenes:
            raise ValueError("Provide audio_url, a non-empty script, or scenes")
        if self.audio_url and (self.script or self.scenes):
            raise ValueError("Use audio_url OR script/scenes, not both")
        allowed = {"16:9", "9:16", "1:1"}
        if not self.aspect_ratios or any(r not in allowed for r in self.aspect_ratios):
            raise ValueError("aspect_ratios must contain 16:9, 9:16, and/or 1:1")
        return self

def error(code: str, message: str, retryable: bool = False, details: Any = None):
    return {"error": {"code": code, "message": message, "retryable": retryable, "details": details}}

def structured_log(level: str, message: str, job_id: str = "", correlation_id: str = ""):
    log.info(json.dumps({"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level,
                         "message": message, "job_id": job_id, "correlation_id": correlation_id}))

def safe_download(url: str, target: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("audio_url must be an HTTPS URL")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        for item in addresses:
            ip = item[4][0]
            if ip.startswith(("127.", "10.", "192.168.", "169.254.")) or ip == "::1" or ip.startswith("fc") or ip.startswith("fd"):
                raise ValueError("Private-network URLs are not allowed")
    except socket.gaierror as exc:
        raise ValueError("Could not resolve audio URL host") from exc
    with requests.get(url, stream=True, timeout=(5, 25), allow_redirects=False) as response:
        response.raise_for_status()
        length = int(response.headers.get("content-length", "0") or 0)
        if length > MAX_AUDIO_BYTES:
            raise ValueError("Audio file exceeds configured size limit")
        written = 0
        with target.open("wb") as out:
            for chunk in response.iter_content(1024 * 128):
                written += len(chunk)
                if written > MAX_AUDIO_BYTES:
                    raise ValueError("Audio file exceeds configured size limit")
                out.write(chunk)

def split_scenes(script: str) -> list[dict[str, str]]:
    chunks = [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", script.strip()) if x.strip()]
    if not chunks:
        raise ValueError("Script is empty after trimming")
    return [{"text": c, "visual_hint": c} for c in chunks]

def srt_time(seconds: float) -> str:
    ms = int(max(0, seconds) * 1000)
    h, rem = divmod(ms, 3600000); m, rem = divmod(rem, 60000); s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def get_font(size: int):
    for path in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]:
        if Path(path).exists(): return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()

def make_fallback_visual(text: str, target: Path, width: int, height: int, idx: int):
    # Deliberately abstract/typographic fallback: no realistic faces or invented logos.
    palette = [(22, 42, 77), (28, 73, 87), (61, 54, 100), (34, 78, 65), (93, 55, 50)]
    im = Image.new("RGB", (width, height), palette[idx % len(palette)])
    d = ImageDraw.Draw(im)
    d.ellipse((int(width*.68), int(height*.08), int(width*1.05), int(height*.48)), outline=(125, 190, 210), width=max(3, width//180))
    d.rounded_rectangle((int(width*.07), int(height*.12), int(width*.11), int(height*.88)), radius=12, fill=(92, 204, 181))
    font = get_font(max(24, width // 22))
    words = text.split(); lines=[]; current=""
    for word in words:
        trial = (current + " " + word).strip()
        if d.textbbox((0,0), trial, font=font)[2] > width*.76 and current:
            lines.append(current); current=word
        else: current=trial
    if current: lines.append(current)
    y = int(height*.38); line_h = max(30, width//18)
    for line in lines[:5]:
        d.text((int(width*.16), y), line, font=font, fill=(250, 250, 250), stroke_width=1, stroke_fill=(20,20,20)); y += line_h
    d.text((int(width*.16), int(height*.88)), "GENERATED VISUAL FALLBACK", font=get_font(max(12,width//75)), fill=(180,210,220))
    im.save(target, "PNG")

def choose_pexels_video(query: str, orientation: str, used: set[int]) -> tuple[dict[str, Any] | None, str | None]:
    if not PEXELS_API_KEY: return None, "PEXELS_API_KEY is not configured"
    try:
        resp = requests.get("https://api.pexels.com/videos/search", headers={"Authorization": PEXELS_API_KEY},
                            params={"query": query[:180], "per_page": 15, "orientation": orientation}, timeout=15)
        resp.raise_for_status()
        for video in resp.json().get("videos", []):
            if video.get("id") in used: continue
            files = sorted(video.get("video_files", []), key=lambda f: (abs((f.get("width") or 0) - 1280), -(f.get("height") or 0)))
            if files and files[0].get("link"):
                used.add(video["id"])
                return {"id": video["id"], "url": files[0]["link"], "page": video.get("url"),
                        "photographer": video.get("user", {}).get("name", "Unknown"), "license": "Pexels License",
                        "license_url": "https://www.pexels.com/license/"}, None
        return None, "Pexels returned no unused matching videos"
    except Exception as exc:
        return None, f"Pexels provider failed: {type(exc).__name__}"

def ffmpeg_run(args: list[str]):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffmpeg failed")[-1200:])

def render_job(job_id: str, request_data: dict[str, Any], audio_upload: str | None = None):
    state = JOBS_STATE[job_id]; state["status"] = "running"; state["progress"] = 5
    start = time.monotonic(); jobdir = JOBS / job_id; jobdir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []; audio_path: Path | None = None
    try:
        payload = JobRequest.model_validate(request_data)
        if payload.audio_url or audio_upload:
            if audio_upload:
                audio_path = Path(audio_upload)
            else:
                audio_path = jobdir / "input_audio"
                safe_download(payload.audio_url or "", audio_path)
            # Duration is probed to align scenes to the supplied voiceover.
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)], capture_output=True, text=True, timeout=15)
            if probe.returncode != 0: raise ValueError("Audio input did not provide a readable audio file")
            duration = max(1.0, float(probe.stdout.strip()))
            scenes = [s.model_dump() for s in payload.scenes] if payload.scenes else []
            if not scenes:
                warnings.append("Audio-only input has no transcript; one scene covers the full audio. Speech recognition is not implemented in this MVP.")
                scenes = [{"text": "Voiceover scene", "visual_hint": "abstract voiceover visual"}]
        else:
            scenes = [s.model_dump() for s in payload.scenes] if payload.scenes else split_scenes(payload.script or "")
            script = " ".join(s["text"] for s in scenes)
            audio_path = jobdir / "narration.wav"
            # eSpeak NG is an offline, swappable TTS adapter; configure ESPEAK_PATH if needed.
            voice = "en-us+f3" if "female" in payload.voice.lower() else "en-us+m3"
            tts = subprocess.run([ESPEAK, "-v", voice, "-s", "150", "-w", str(audio_path), script], capture_output=True, text=True, timeout=60)
            if tts.returncode != 0: raise RuntimeError("TTS failed. Check that eSpeak NG is installed and ESPEAK_PATH is correct.")
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)], capture_output=True, text=True, timeout=15)
            duration = max(1.0, float(probe.stdout.strip()))
        n = len(scenes); scene_duration = duration / n
        state["progress"] = 15
        timeline=[]; used_video_ids:set[int]=set(); scene_segments=[]
        for idx, scene in enumerate(scenes):
            start_s = idx * scene_duration; end_s = duration if idx == n-1 else (idx+1)*scene_duration
            hint = (scene.get("visual_hint") or scene["text"]).strip()
            # No brand/logo/real-person generation. Keep named entities as text only.
            lower = (scene["text"] + " " + hint).lower()
            if any(term in lower for term in ["suicide", "fatal", "tragedy", "war crime", "massacre"]):
                warnings.append(f"Scene {idx+1}: sensitive-topic fallback used; review before publication.")
            clip_meta = {"ai_generated": True, "source": "local generated graphic", "source_url": None,
                         "license": "Generated locally by this application; no third-party media used", "attribution": "None",
                         "reason_chosen": "Safe no-key fallback; keyword-driven scene label only, not semantic visual retrieval", "relevance_score": None}
            clip_file = jobdir / f"scene_{idx:03}.mp4"
            visual_file = jobdir / f"scene_{idx:03}.png"
            local_clips = sorted(
                p for p in BROLL_DIR.glob("*")
                if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}
            )
            used_real_clip = False

            # Prefer local B-roll. Cycle through available clips across scenes.
            if local_clips:
                local_clip = local_clips[idx % len(local_clips)]
                try:
                    ffmpeg_run([
                        FFMPEG, "-y", "-stream_loop", "-1", "-i", str(local_clip),
                        "-t", f"{end_s-start_s:.3f}",
                        "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30,format=yuv420p",
                        "-an", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", str(clip_file)
                    ])
                    clip_meta = {
                        "ai_generated": False,
                        "source": "user-provided local B-roll",
                        "source_url": None,
                        "local_file": local_clip.name,
                        "license": "Verify the original source license before commercial use",
                        "attribution": "See original clip source",
                        "reason_chosen": f"Local B-roll clip selected for scene: {hint}",
                        "relevance_score": None
                    }
                    used_real_clip = True
                except Exception as exc:
                    warnings.append(
                        f"Scene {idx+1}: local B-roll processing failed "
                        f"({type(exc).__name__}); trying Pexels or graphic fallback."
                    )

            # If local clips are unavailable or fail, try Pexels.
            pexels = None
            provider_warning = None
            if not used_real_clip:
                pexels, provider_warning = choose_pexels_video(
                    hint,
                    "portrait" if "9:16" in payload.aspect_ratios else "landscape",
                    used_video_ids
                )

                if pexels:
                    try:
                        vpath = jobdir / f"source_{idx:03}.mp4"
                        with requests.get(
                            pexels["url"], stream=True, timeout=(5, 30)
                        ) as rr:
                            rr.raise_for_status()
                            with vpath.open("wb") as out:
                                total = 0
                                for chunk in rr.iter_content(1024 * 128):
                                    total += len(chunk)
                                    if total > 100 * 1024 * 1024:
                                        raise ValueError("Downloaded clip exceeds 100 MB")
                                    out.write(chunk)

                        ffmpeg_run([
                            FFMPEG, "-y", "-stream_loop", "-1", "-i", str(vpath),
                            "-t", f"{end_s-start_s:.3f}",
                            "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,fps=30,format=yuv420p",
                            "-an", "-c:v", "libx264", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", str(clip_file)
                        ])
                        clip_meta = {
                            "ai_generated": False,
                            "source": "Pexels",
                            "source_url": pexels["page"],
                            "license": pexels["license"],
                            "license_url": pexels["license_url"],
                            "attribution": pexels["photographer"],
                            "reason_chosen": f"First unused Pexels result for scene hint: {hint}",
                            "relevance_score": None
                        }
                        used_real_clip = True
                    except Exception as exc:
                        warnings.append(
                            f"Scene {idx+1}: Pexels processing failed "
                            f"({type(exc).__name__}); generated graphic fallback used."
                        )
                        pexels = None

            # Last resort: generate a graphic if no real video was processed.
            if not used_real_clip:
                reason = provider_warning or "No usable local or Pexels video was available"
                warnings.append(
                    f"Scene {idx+1}: {reason}; generated graphic fallback used. "
                    "This is not real B-roll; review before publication."
                )
                width, height = (1920, 1080)
                make_fallback_visual(scene["text"], visual_file, width, height, idx)
                ffmpeg_run([
                    FFMPEG, "-y", "-loop", "1", "-i", str(visual_file),
                    "-t", f"{end_s-start_s:.3f}", "-r", "30",
                    "-vf", "format=yuv420p", "-an", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(clip_file)
                ])

            scene_segments.append(clip_file)
            timeline.append({"scene_id":idx+1,"text":scene["text"],"start":round(start_s,3),"end":round(end_s,3),"duration":round(end_s-start_s,3),"visual_hint":hint,"clip_file":clip_file.name,**clip_meta})
            state["progress"] = 15 + int(55*(idx+1)/n)
        concat_file=jobdir/"concat.txt"
        concat_file.write_text("\n".join("file '"+p.name+"'" for p in scene_segments)+"\n",encoding="utf-8")
        base_video=jobdir/"base.mp4"
        ffmpeg_run([FFMPEG,"-y","-f","concat","-safe","0","-i",str(concat_file),"-c","copy",str(base_video)])
        state["progress"] = 75
        srt=[]
        for row in timeline:
            srt += [str(row["scene_id"]),f"{srt_time(row['start'])} --> {srt_time(row['end'])}",row["text"],""]
        srt_path=jobdir/"captions.srt"; srt_path.write_text("\n".join(srt),encoding="utf-8")
        if payload.captions.burn_in:
            warnings.append("Burn-in captions are not implemented in this MVP; captions.srt is provided separately.")
        output_files=[]
        for ratio in payload.aspect_ratios:
            output=jobdir/f"video_{ratio.replace(':','x')}.mp4"
            if ratio=="16:9": size="1920:1080"
            elif ratio=="9:16": size="1080:1920"
            else: size="1080:1080"
            # crop the landscape base to requested ratio; audio is kept as narration.
            vf=f"scale={size}:force_original_aspect_ratio=increase,crop={size},fps=30,format=yuv420p"
            ffmpeg_run([FFMPEG,"-y","-i",str(base_video),"-i",str(audio_path),"-vf",vf,"-map","0:v:0","-map","1:a:0","-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p","-c:a","aac","-b:a","128k","-shortest",str(output)])
            output_files.append(output.name)
        elapsed=round(time.monotonic()-start,3)
        
        manifest={"job_id":job_id,"application_version":"0.1.0","models_and_providers":{"tts":"eSpeak NG (local)","stock_footage":"User-provided local B-roll clips when available; otherwise Pexels API if configured; otherwise generated title-card fallback","video":"FFmpeg"},"settings":{"voice":payload.voice,"aspect_ratios":payload.aspect_ratios,"style":payload.style,"avoid":payload.avoid,"captions":payload.captions.model_dump()},"seed":None,"timings_seconds":{"total":elapsed},"cost_estimate_usd":None,"warnings":warnings,"rights":{"local_broll":"User-provided media; verify each original source license and attribution requirements before use","stock_license":"Check the applicable Pexels license for each Pexels clip","fallback":"Generated local graphics; no third-party image assets"},"outputs":output_files}

        (jobdir/"timeline.json").write_text(json.dumps(timeline,indent=2,ensure_ascii=False),encoding="utf-8")
        (jobdir/"manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False),encoding="utf-8")
        state.update(status="succeeded",progress=100,completed_at=time.time(),result={"files":output_files+(["captions.srt"] if payload.captions.srt else [])+["timeline.json","manifest.json"],"warnings":warnings,"duration_seconds":duration,"elapsed_seconds":elapsed})
        structured_log("INFO","job_succeeded",job_id,state.get("correlation_id",""))
    except Exception as exc:
        state.update(status="failed",progress=100,error={"code":"JOB_PROCESSING_FAILED","message":str(exc)[:500],"retryable":True,"details":None})
        structured_log("ERROR","job_failed",job_id,state.get("correlation_id",""))

@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    cid=request.headers.get("X-Correlation-Id", str(uuid.uuid4()))
    response=await call_next(request); response.headers["X-Correlation-Id"]=cid; return response

@app.get("/health")
@app.get("/api/v1/health")
def health(): return {"status":"ok"}

@app.get("/ready")
@app.get("/api/v1/ready")
def ready():
    ok=shutil.which(FFMPEG) is not None and shutil.which("ffprobe") is not None and shutil.which(ESPEAK) is not None
    return {"ready":ok,"checks":{"ffmpeg":shutil.which(FFMPEG) is not None,"ffprobe":shutil.which("ffprobe") is not None,"espeak_ng":shutil.which(ESPEAK) is not None}}

@app.post("/api/v1/broll/jobs", status_code=202)
def create_job(body: JobRequest, background_tasks: BackgroundTasks, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), x_correlation_id: str | None = Header(default=None, alias="X-Correlation-Id")):
    if idempotency_key and idempotency_key in IDEMPOTENCY:
        existing=IDEMPOTENCY[idempotency_key]
        return {"job_id":existing,"status_url":f"/api/v1/jobs/{existing}","idempotent_replay":True}
    job_id=str(uuid.uuid4()); JOBS_STATE[job_id]={"job_id":job_id,"status":"queued","progress":0,"created_at":time.time(),"correlation_id":x_correlation_id or ""}
    if idempotency_key: IDEMPOTENCY[idempotency_key]=job_id
    background_tasks.add_task(render_job,job_id,body.model_dump())
    structured_log("INFO","job_queued",job_id,x_correlation_id or "")
    return {"job_id":job_id,"status_url":f"/api/v1/jobs/{job_id}","idempotent_replay":False}

@app.post("/api/v1/broll/jobs/upload", status_code=202)
async def create_job_upload(background_tasks: BackgroundTasks, file: UploadFile = File(...), script: str | None = Form(default=None), aspect_ratios: str = Form(default="16:9"), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    suffix=Path(file.filename or "audio.wav").suffix.lower()
    if suffix not in {".mp3",".wav",".m4a",".aac",".ogg"}: raise HTTPException(400,detail=error("UNSUPPORTED_AUDIO_TYPE","Upload mp3, wav, m4a, aac or ogg audio."))
    data=await file.read(MAX_AUDIO_BYTES+1)
    if len(data)>MAX_AUDIO_BYTES: raise HTTPException(413,detail=error("AUDIO_TOO_LARGE","Audio exceeds configured limit."))
    if script and script.strip(): raise HTTPException(400,detail=error("AMBIGUOUS_INPUT","Provide either audio upload or script, not both."))
    job_id=str(uuid.uuid4()); jobdir=JOBS/job_id; jobdir.mkdir(parents=True,exist_ok=True); audio_path=jobdir/("input_audio"+suffix); audio_path.write_bytes(data)
    ratios=[r.strip() for r in aspect_ratios.split(",") if r.strip()]
    body={"audio_url":None,"script":None,"scenes":[{"text":"Voiceover scene","visual_hint":"abstract voiceover visual"}],"voice":"en-IN-female","aspect_ratios":ratios,"style":"bright, modern, real-world footage","avoid":[],"captions":{"burn_in":True,"srt":True}}
    # Render uses the persisted upload instead of generating speech.
    JOBS_STATE[job_id]={"job_id":job_id,"status":"queued","progress":0,"created_at":time.time(),"correlation_id":"","uploaded_audio":str(audio_path)}
    background_tasks.add_task(render_job,job_id,body,str(audio_path))
    return {"job_id":job_id,"status_url":f"/api/v1/jobs/{job_id}","idempotent_replay":False}

@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str):
    state=JOBS_STATE.get(job_id)
    if not state: raise HTTPException(404,detail=error("JOB_NOT_FOUND","No job exists with this id."))
    out={k:v for k,v in state.items() if k not in {"correlation_id","uploaded_audio"}}
    if state.get("status")=="succeeded": out["download_urls"]={f:"/api/v1/jobs/{job_id}/files/{f}" for f in state["result"]["files"]}
    return out

@app.get("/api/v1/jobs/{job_id}/files/{filename}")
def get_file(job_id: str, filename: str):
    jobdir=(JOBS/job_id).resolve(); target=(jobdir/filename).resolve()
    if jobdir not in target.parents or not target.is_file(): raise HTTPException(404,detail=error("FILE_NOT_FOUND","Output file not found."))
    return FileResponse(target, filename=target.name)

@app.get("/api/v1/openapi.json")
def openapi_json(): return app.openapi()
